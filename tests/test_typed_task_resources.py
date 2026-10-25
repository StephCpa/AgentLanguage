import json
from al0.semantics import execution_site
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from al0.concurrency import TaskRunner, TaskScope
from al0.custody import ResourceRef
from al0.data import Principal
from al0.errors import EvidenceMismatch, FlowDenied, InvalidData, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.ir import Tool, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.typed_task_resources import build, catalog, demo, funded


def budget_identity(specs):
    b=PlanBuilder('identity',specs)
    return b.finish(b.budget_parameter('funds'))


def budget_plan(specs, *, nested=False, onward=False, private=False):
    child=budget_identity(specs)
    if nested:
        b=PlanBuilder('nested',specs)
        funds=b.budget_parameter('funds')
        group=b.scope('group',{})
        task=b.spawn('task',group,child,{},inputs={'funds':funds})
        value=b.join('value',task)
        b.close_scope('closed',group)
        child=b.finish(value)
    b=PlanBuilder('budget-return',specs)
    amount={'u':3}
    if private:
        number=b.literal('number','Int',3)
        number=b.restrict('secret',number,Label({'owner'},{'source:private'}))
        amount=b.resources('amount',{'u':number})
    funds=b.split_budget('funds',amount)
    group=b.scope('group',{})
    task=b.spawn('task',group,child,{},inputs={'funds':funds})
    value=b.join('value',task)
    if onward:
        task=b.spawn('onward',group,child,{},inputs={'funds':value})
        value=b.join('last',task)
    b.close_scope('closed',group)
    return b.finish(b.join_budget('released',value))


class TypedTaskResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.specs=catalog()
        self.store=Store(Path(self.temp.name)/'runtime.sqlite')
        self.who=Principal('tests','developer')
        self.calls=[]
        def send(ticket):
            self.calls.append(ticket.attempt)
            return Response('sent',{'u':1})
        self.broker=Broker(self.store,self.specs,{'send':send})
        self.addCleanup(lambda:self.broker.wait_for_workers(timeout=10))

    def start(self,plan):
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':10})
        self.grants={'send':self.store.grant(self.run,'send','output',expires=self.store.clock()+1000)}

    def execute(self,plan,**kw):
        return Interpreter(self.broker).run(plan,self.run,self.who,self.grants,**kw)

    def replay(self,plan):
        self.start(plan)
        first=self.execute(plan)
        count=len(self.store.events(self.run,self.who))
        again=self.execute(plan)
        self.assertEqual(first,again)
        self.assertEqual(len(self.store.events(self.run,self.who)),count)
        return first

    def test_source_sdk_and_independent_endpoint(self):
        result=demo(self.temp.name)
        self.assertTrue(result['identical_ir'])
        self.assertEqual((result['result'],result['provider_calls'],result['provider_fees'],result['replay_added_events']),('Done',1,1,0))

    def test_returned_budget_and_replay(self):
        self.assertTrue(self.replay(budget_plan(self.specs)).data)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_child_can_create_and_export_budget(self):
        child=PlanBuilder('create',self.specs)
        child=child.finish(child.split_budget('funds',{'u':2}))
        b=PlanBuilder('receive-new',self.specs)
        scope=b.scope('scope',{'u':3})
        task=b.spawn('task',scope,child,{'u':3})
        funds=b.join('funds',task)
        b.close_scope('closed',scope)
        plan=b.finish(b.join_budget('released',funds))
        self.assertTrue(self.replay(plan).data)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_returned_admission_permit_keeps_its_transferred_generation(self):
        from examples.admission_outcomes import alternatives
        accepted,_,denied=alternatives(self.specs)
        # All three branches own the explicit funds capture.
        def add_funds(plan):
            raw=json.loads(plan.encoded)
            raw['uses'].append('budget')
            raw['nodes'].insert(0,{'id':'funds','op':'parameter','type':'Budget','kind':'Budget','predicates':[]})
            raw['nodes'].append({'id':'released','op':'join_budget','budget':'funds'})
            return check_plan(raw,self.specs)
        accepted,denied=add_funds(accepted),add_funds(denied)
        branch=PlanBuilder('returned',self.specs)
        funds=branch.budget_parameter('funds')
        request=branch.intent_parameter('request',branch.tool('send'))
        permit=branch.permit_parameter('result',request)
        branch.parameter('reason','Json')
        scope=branch.scope('scope',{})
        task=branch.spawn('task',scope,funded(self.specs),{},inputs={'funds':funds,'request':request,'permission':permit})
        operation=branch.join('operation',task)
        branch.close_scope('closed',scope)
        state=branch.await_result('state',operation)
        returned=branch.finish(branch.match_business('text',state,'Text',applied='Done',proven_no_effect='Rejected',
                                                    not_dispatched='Waiting',pending='Waiting',unknown='Unknown'))
        child=PlanBuilder('admission-child',self.specs)
        funds=child.budget_parameter('funds')
        request=child.intent_parameter('request',child.tool('send'))
        permit=child.permit_parameter('permission',request)
        decision=child.try_admit('decision',request,permit)
        child=child.finish(child.match_admission('result',decision,admitted=accepted,returned=returned,no_permit=denied,
                                                inputs={'funds':funds,'request':request}))
        b=PlanBuilder('returned-permit',self.specs)
        request=b.prepare('request',b.tool('send'),b.literal('message','Text','hello'))
        permit=b.authorize('permit',request)
        funds=b.split_budget('funds',{'u':2})
        scope=b.scope('scope',{})
        task=b.spawn('task',scope,child,{},inputs={'funds':funds,'request':request,'permission':permit})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        self.assertEqual(self.replay(b.finish(result)).data,'Done')
        self.assertEqual(len(self.calls),1)

    def test_nested_return_uses_parent_lease(self):
        self.assertTrue(self.replay(budget_plan(self.specs,nested=True)).data)

    def test_return_can_move_onward_without_reinstalling_old_receipts(self):
        self.replay(budget_plan(self.specs,onward=True))
        with self.store._tx() as db:
            row=db.execute("SELECT generation,state FROM resource_custody WHERE run=? AND kind='Budget'",(self.run,)).fetchone()
        self.assertEqual((row['generation'],row['state']),(7,'Spent'))

    def test_resource_labels_survive_return_and_replay(self):
        result=self.replay(budget_plan(self.specs,private=True))
        self.assertEqual(result.label,Label({'owner'},{'source:private'}))
        with self.assertRaises(FlowDenied):
            self.broker.deliver(result,'public')

    def test_scalar_operation_input_and_return(self):
        child=PlanBuilder('operation',self.specs)
        child=child.finish(child.operation_parameter('op',child.tool('send')))
        b=PlanBuilder('operation-roundtrip',self.specs)
        request=b.prepare('request',b.tool('send'),b.literal('message','Text','hello'))
        permit=b.authorize('permit',request)
        op=b.admit('op',request,permit)
        group=b.scope('group',{})
        task=b.spawn('task',group,child,{},inputs={'op':op})
        returned=b.join('returned',task)
        b.close_scope('closed',group)
        state=b.await_result('state',returned)
        result=b.match_business('result',state,'Bool',applied=True,proven_no_effect=False,not_dispatched=False,pending=False,unknown=False)
        self.assertTrue(self.replay(b.finish(result)).data)
        self.assertEqual(len(self.calls),1)

    def test_managed_resource_metadata_cannot_be_passed_as_data(self):
        plan=PlanBuilder('data',self.specs)
        plan=plan.finish(plan.parameter('value','Text'))
        self.start(plan)
        value=Value('x',resource=ResourceRef('Budget','x',0))
        with self.assertRaises(InvalidData):
            self.execute(plan,inputs={'value':value})

    def test_task_host_requires_explicit_resource_reference(self):
        plan=budget_identity(self.specs)
        self.start(plan)
        funds=self.store.split_budget(self.run,self.who,'raw',{'u':1})
        with TaskScope(self.broker,self.run,self.who,'host',{}) as scope:
            with self.assertRaisesRegex(StateConflict,'explicit managed'):
                scope.spawn('task',plan,{},self.grants,inputs={'funds':Value(funds)})

    def test_different_runtime_intent_is_rejected_before_spawn(self):
        from al0.data import Intent
        plan=funded(self.specs)
        self.start(plan)
        original=Intent('send','1','output','original')
        permit=self.store.authorize(self.run,self.who,original,self.grants['send'])
        funds=self.store.split_budget(self.run,self.who,'raw',{'u':1})
        inputs={'funds':Value(funds,resource=self.store.manage_resource(self.run,self.who,'Budget',funds)),
                'request':Value(Intent('send','1','output','other')),
                'permission':Value(permit,resource=self.store.manage_resource(self.run,self.who,'Permit',permit))}
        with TaskScope(self.broker,self.run,self.who,'host',{}) as scope:
            with self.assertRaises(EvidenceMismatch):
                scope.spawn('task',plan,{},self.grants,inputs=inputs)

    def test_static_use_after_task_move(self):
        doc=json.loads(budget_plan(self.specs).encoded)
        doc['nodes'].insert(3,{'id':'reuse','op':'join_budget','budget':'funds'})
        with self.assertRaisesRegex(TypeCheckError,'AL1001'):
            check_plan(doc,self.specs)

    def test_static_duplicate_resource_alias(self):
        child=PlanBuilder('two',self.specs)
        a=child.budget_parameter('a')
        b=child.budget_parameter('b')
        child.join_budget('closed',a)
        child=child.finish(b)
        parent=PlanBuilder('alias',self.specs)
        funds=parent.split_budget('funds',{})
        group=parent.scope('group',{})
        task=parent.spawn('task',group,child,{},inputs={'a':funds,'b':funds})
        result=parent.join('result',task)
        parent.close_scope('closed',group)
        with self.assertRaisesRegex(TypeCheckError,'multiple names'):
            parent.finish(result)

    def test_static_permit_intent_identity(self):
        doc=json.loads(build(self.specs).encoded)
        doc['nodes'].insert(2,{'id':'other','op':'prepare','tool':'send','input':'message'})
        spawn=next(n for n in doc['nodes'] if n['op']=='spawn_with')
        spawn['inputs']['request']='other'
        with self.assertRaisesRegex(TypeCheckError,'another intent'):
            check_plan(doc,self.specs)

    def test_joined_operation_obligation_cannot_be_dropped(self):
        doc=json.loads(build(self.specs).encoded)
        doc['nodes']=doc['nodes'][:-2]
        doc['return']='message'
        with self.assertRaisesRegex(TypeCheckError,'AL3002'):
            check_plan(doc,self.specs)

    def test_resource_task_outcome_cannot_be_used_as_a_budget(self):
        doc=json.loads(budget_plan(self.specs).encoded)
        join=next(n for n in doc['nodes'] if n['op']=='join')
        join['op']='join_outcome'
        with self.assertRaises(TypeCheckError):
            check_plan(doc,self.specs)

    def test_resource_race_receives_and_releases_the_winning_budget(self):
        b=PlanBuilder('race-resource',self.specs)
        scope=b.scope('scope',{},speculative=True)
        a=b.split_budget('a',{})
        z=b.split_budget('z',{})
        first=b.spawn('first',scope,budget_identity(self.specs),{},inputs={'funds':a})
        last=b.spawn('last',scope,budget_identity(self.specs),{},inputs={'funds':z})
        result=b.race('winner',[first,last])
        b.close_scope('closed',scope)
        plan=b.finish(b.join_budget('released',result))
        self.start(plan)
        self.execute(plan)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_waiting_child_keeps_exposed_fee_responsibility(self):
        self.child_exit(waiting=True)

    def test_failed_child_retires_moved_budget(self):
        self.child_exit(waiting=False)

    def child_exit(self,*,waiting):
        self.specs.register(Tool.define('read','observe','Text','Text','io',{'u':1},output_label=Label()))
        def timeout(ticket):
            raise TimeoutError('external response lost')
        self.broker=Broker(self.store,self.specs,{'send':timeout,'read':timeout})
        leaf=PlanBuilder('read',self.specs)
        leaf=leaf.finish(leaf.observe('read',leaf.tool('read'),leaf.literal('text','Text','hello')))
        child=PlanBuilder('funded-read',self.specs)
        funds=child.budget_parameter('funds')
        result=child.invoke('result',leaf,budget=funds)
        child.join_budget('closed',funds)
        child=child.finish(result)
        self.start(child)
        if waiting:
            self.grants['read']=self.store.grant(self.run,'read','io',expires=self.store.clock()+1000)
        allocation=self.store.split_budget(self.run,self.who,'export',{'u':2})
        ref=self.store.manage_resource(self.run,self.who,'Budget',allocation)
        with TaskScope(self.broker,self.run,self.who,'host',{}) as scope:
            task,_=scope.spawn('task',child,{},self.grants,inputs={'funds':Value(allocation,resource=ref)})
            outcome=scope.join_outcome(task)
        self.assertEqual(outcome['kind'],'Waiting' if waiting else 'Failed')
        budget=self.store.budget(self.run,self.who)['u']
        self.assertEqual((budget['reserved'],budget['free']),(1,9) if waiting else (0,10))
        with self.store._tx() as db:
            rows=db.execute('SELECT state FROM resource_custody WHERE run=?',(self.run,)).fetchall()
            attempts=db.execute('SELECT id FROM attempts').fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(r['state'] in ('Retired','Spent') for r in rows))
        if waiting:
            self.store.settle(attempts[0]['id'],self.who,provider='read',evidence_id='late',actual={'u':1})
            self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)

    def test_host_must_explicitly_receive_resource_result(self):
        plan=budget_identity(self.specs)
        self.start(plan)
        allocation=self.store.split_budget(self.run,self.who,'export',{})
        ref=self.store.manage_resource(self.run,self.who,'Budget',allocation)
        with self.assertRaisesRegex(StateConflict,'explicit join'):
            with TaskScope(self.broker,self.run,self.who,'host',{}) as scope:
                scope.spawn('task',plan,{},self.grants,inputs={'funds':Value(allocation,resource=ref)})
        with self.store._tx() as db:
            row=db.execute('SELECT state FROM resource_custody WHERE resource=?',(allocation,)).fetchone()
        self.assertEqual(row['state'],'Retired')

    def test_resource_reference_record_validates_identity_and_receipts(self):
        with self.assertRaises(InvalidData):
            Value('another',resource=ResourceRef('Budget','original',0))
        with self.assertRaises(InvalidData):
            Value('original',('receipt',),resource=ResourceRef('Budget','original',0))

    def test_restored_inputs_keep_transfer_generation(self):
        plan=budget_plan(self.specs,onward=True)
        self.replay(plan)
        with self.store._tx() as db:
            task=db.execute('SELECT id FROM tasks WHERE run=? AND task_key=?',(self.run,execution_site('task'))).fetchone()[0]
        original=self.store.task_execution_inputs(task,self.who)['funds']['resource']
        self.assertEqual(original['generation'],1)
        accepted=self.store.task_resource_result(task,self.who)['result']
        self.assertEqual(accepted.generation,3)
        with self.store.resource_access(self.run,self.who,[accepted]):
            with self.assertRaises(StateConflict):
                self.store.split_budget(self.run,self.who,'stale',{},parent=accepted.resource)

    def crash(self,point):
        plan=build(self.specs)
        self.start(plan)
        path=Path(self.temp.name)/'config.json'
        log=Path(self.temp.name)/'calls.txt'
        path.write_text(json.dumps({'database':str(Path(self.temp.name)/'runtime.sqlite'),
            'run':self.run,'grants':self.grants,'log':str(log)}),encoding='utf-8')
        crashed=subprocess.run([sys.executable,'-m','tests.typed_task_resource_worker',str(path),point],
                               capture_output=True,text=True,timeout=30)
        self.assertEqual(crashed.returncode,77,crashed.stderr)
        recovered=subprocess.run([sys.executable,'-m','tests.typed_task_resource_worker',str(path),'none'],
                                 capture_output=True,text=True,timeout=30)
        self.assertEqual(recovered.returncode,0,recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)['data'],'Done')
        events=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(len(self.store.events(self.run,self.who)),events)
        self.assertEqual(len(log.read_text().splitlines()),1)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)

    def test_crash_during_spawn_transfer(self):
        self.crash('after_task_resources_move')

    def test_crash_after_spawn_transfer(self):
        self.crash('after_spawn_commit')

    def test_crash_during_return_publish(self):
        self.crash('after_task_resources_publish')

    def test_crash_after_return_publish(self):
        self.crash('after_task_finish_commit')

    def test_crash_during_return_receive(self):
        self.crash('after_task_resources_receive')

    def test_crash_after_return_receive(self):
        self.crash('after_task_join_commit')

    def test_crash_after_managed_admission(self):
        self.crash('after_admit_commit')

    def test_crash_after_managed_budget_join(self):
        self.crash('after_join_commit')


if __name__=='__main__':
    unittest.main()
