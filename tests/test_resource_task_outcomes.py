import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
import unittest

from al0.bundles import value_refs
from al0.concurrency import TaskRunner, TaskScope
from al0.data import Principal
from al0.errors import FlowDenied, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Suspended, Value
from al0.flow import Label
from al0.ir import Tool, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.resource_task_outcomes import build, catalog, demo, race_plan
from examples.resource_bundles import handled
from tests.test_resource_bundles import budget_packet
from tests.test_typed_task_resources import budget_identity


class ResourceTaskOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.who=Principal('tests','developer')
        self.specs=catalog()
        self.calls=[]
        def send(ticket):
            self.calls.append(ticket.attempt)
            return Response('sent',{'u':1})
        self.broker=Broker(self.store,self.specs,{'send':send})
        self.addCleanup(lambda:self.broker.wait_for_workers(timeout=10))

    def start(self,plan=None):
        self.run=self.store.create_run(self.who,'host' if plan is None else plan.fingerprint,{'u':10})
        self.grants={'send':self.store.grant(self.run,'send','output',expires=self.store.clock()+1000)}

    def execute(self,plan,**kwargs):
        return Interpreter(self.broker).run(plan,self.run,self.who,self.grants,**kwargs)

    def replay(self,plan):
        self.start(plan)
        result=self.execute(plan)
        self.broker.wait_for_workers(timeout=10)
        events=self.store.events(self.run,self.who)
        self.assertEqual(self.execute(plan),result)
        self.broker.wait_for_workers(timeout=10)
        self.assertEqual(self.store.events(self.run,self.who),events)
        return result

    def host(self,*,speculative=False,private=False):
        self.start()
        self.scope=self.store.open_scope(self.run,self.who,'scope',{'u':8},speculative=speculative)
        self.child=budget_packet(self.specs,nested=True,private=private)

    def published(self,key='child'):
        task=self.store.spawn_task(self.scope,self.who,key,self.child.fingerprint,{'u':2},caps=self.grants)
        result=TaskRunner(self.broker).run(task,self.who,self.child,self.grants)
        return task,result

    def take(self,task,*,cancel=False,lease=None):
        with self.store.resource_access(self.run,self.who,task_lease=lease):
            return self.store.take_task_outcome(task,self.who,parent_lease=lease,cancel=cancel)

    def choose(self,tasks,winner=None):
        with self.store.resource_access(self.run,self.who):
            return self.store.select_race(self.scope,self.who,'race',tasks,tasks[0] if winner is None else winner)

    def held(self,ref):
        with self.store._tx() as db:
            return dict(self.store._custody(db,ref.kind,ref.resource))

    def test_source_sdk_mixed_outcome_and_nested_race_independent_endpoint(self):
        result=demo(self.temp.name)
        self.assertEqual(result['provider_calls'],[('first',1),('second',1)])
        for name in ('resource-outcomes','resource-race'):
            self.assertTrue(result[name]['identical_ir'])
            self.assertEqual(result[name]['replay_added_events'],0)
        self.assertEqual(result['resource-outcomes']['budget']['u']['confirmed'],2)
        self.assertEqual(result['resource-race']['budget']['u']['free'],10)

    def test_completed_branch_must_receive_package(self):
        doc=json.loads(build(self.specs).encoded)
        branch=doc['nodes'][-1]
        branch['cases']['Completed']=copy.deepcopy(branch['cases']['Failed'])
        with self.assertRaisesRegex(TypeCheckError,'explicitly receive'):
            check_plan(doc,self.specs)

    def test_resource_outcome_cannot_be_matched_twice(self):
        doc=json.loads(build(self.specs).encoded)
        again=copy.deepcopy(doc['nodes'][-1]);again['id']='again'
        doc['nodes'].append(again);doc['return']='again'
        with self.assertRaisesRegex(TypeCheckError,'AL1001'):
            check_plan(doc,self.specs)

    def test_resource_outcome_must_be_matched(self):
        doc=json.loads(build(self.specs).encoded)
        doc['nodes'].pop();doc['return']='closed'
        with self.assertRaisesRegex(TypeCheckError,'exhaustively matched'):
            check_plan(doc,self.specs)

    def test_race_winning_budget_cannot_be_dropped(self):
        b=PlanBuilder('budget-race',self.specs)
        scope=b.scope('scope',{},speculative=True)
        tasks=[b.spawn(f't{i}',scope,budget_identity(self.specs),{},inputs={'funds':b.split_budget(f'b{i}',{})}) for i in range(2)]
        b.race('winner',tasks)
        closed=b.close_scope('closed',scope)
        with self.assertRaisesRegex(TypeCheckError,'every budget'):
            b.finish(closed)

    def test_race_nested_package_replay(self):
        result=self.replay(race_plan(self.specs))
        self.assertIn(result.data,('first','second'))
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_scalar_operation_completed_branch_drives_once(self):
        child=PlanBuilder('operation-child',self.specs)
        request=child.prepare('request',child.tool('send'),child.literal('text','Text','one'))
        child_plan=child.finish(child.admit('op',request,child.authorize('permit',request)))
        branch=PlanBuilder('completed',self.specs)
        result=branch.operation_parameter('result',branch.tool('send'))
        success=branch.finish(handled(branch,'handled',result))
        other=PlanBuilder('other',self.specs)
        failure=other.finish(other.literal('failed','Text','Failed'))
        b=PlanBuilder('operation-outcome',self.specs)
        scope=b.scope('scope',{'u':1})
        task=b.spawn('task',scope,child_plan,{'u':1})
        outcome=b.join_outcome('outcome',task)
        b.close_scope('closed',scope)
        plan=b.finish(b.match_task('result',outcome,completed=success,failed=failure,waiting=failure,cancelled=failure))
        self.assertEqual(self.replay(plan).data,'Done')
        self.assertEqual(len(self.calls),1)

    def test_race_operation_receives_winner_and_cancels_only_loser(self):
        child=PlanBuilder('operation-identity',self.specs)
        identity=child.finish(child.operation_parameter('op',child.tool('send')))
        b=PlanBuilder('operation-race',self.specs)
        operations=[]
        for i in range(2):
            request=b.prepare(f'request{i}',b.tool('send'),b.literal(f'text{i}','Text',str(i)))
            operations.append(b.admit(f'op{i}',request,b.authorize(f'permit{i}',request)))
        scope=b.scope('scope',{},speculative=True)
        tasks=[b.spawn(f'task{i}',scope,identity,{},inputs={'op':op}) for i,op in enumerate(operations)]
        winner=b.race('winner',tasks)
        b.close_scope('closed',scope)
        self.assertEqual(self.replay(b.finish(handled(b,'done',winner))).data,'Done')
        self.assertEqual(len(self.calls),1)
        budget=self.store.budget(self.run,self.who)['u']
        self.assertEqual((budget['reserved'],budget['confirmed'],budget['free']),(0,1,9))

    def test_race_operation_must_be_handled(self):
        child=PlanBuilder('operation-identity',self.specs)
        identity=child.finish(child.operation_parameter('op',child.tool('send')))
        b=PlanBuilder('operation-race',self.specs)
        scope=b.scope('scope',{},speculative=True)
        tasks=[]
        for i in range(2):
            request=b.prepare(f'request{i}',b.tool('send'),b.literal(f'text{i}','Text',str(i)))
            operation=b.admit(f'op{i}',request,b.authorize(f'permit{i}',request))
            tasks.append(b.spawn(f'task{i}',scope,identity,{},inputs={'op':operation}))
        b.race('winner',tasks)
        closed=b.close_scope('closed',scope)
        with self.assertRaisesRegex(TypeCheckError,'AL3002'):
            b.finish(closed)

    def test_nested_checked_parent_receives_and_handles_mixed_outcome(self):
        b=PlanBuilder('parent',self.specs)
        scope=b.scope('scope',{'u':4})
        task=b.spawn('task',scope,build(self.specs),{'u':4})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        self.assertEqual(self.replay(b.finish(result)).data['second'],'Done')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],2)

    def waiting_plan(self):
        self.specs.register(Tool.define('read','observe','Text','Text','read',{'u':1},output_label=Label()))
        def timeout(ticket):
            self.calls.append(ticket.attempt)
            raise TimeoutError('response lost after exposure')
        self.broker=Broker(self.store,self.specs,{'send':lambda t:Response('sent',{'u':1}),'read':timeout})
        b=PlanBuilder('waiting-packet',self.specs)
        funds=b.split_budget('funds',{'u':2})
        note=b.observe('read',b.tool('read'),b.literal('input','Text','hello'))
        return b.finish(b.pack('packet',{'funds':funds,'note':note}))

    def failed_outcome(self,waiting):
        plan=self.waiting_plan()
        self.start()
        if waiting:
            self.grants['read']=self.store.grant(self.run,'read','read',expires=self.store.clock()+1000)
        with TaskScope(self.broker,self.run,self.who,'scope',{'u':3}) as scope:
            task,_=scope.spawn('task',plan,{'u':3},self.grants)
            outcome=scope.join_outcome(task)
            self.assertEqual(outcome['kind'],'Waiting' if waiting else 'Failed')
            self.assertNotIn('bundle',outcome['payload'])
            self.assertNotIn('resource',outcome['payload'])
        with self.store._tx() as db:
            states=[r['state'] for r in db.execute('SELECT state FROM resource_custody WHERE run=?',(self.run,))]
        self.assertTrue(states)
        self.assertTrue(all(s in ('Retired','Spent') for s in states))
        if waiting:
            self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)
            self.store.settle(self.calls[0],self.who,provider='read',evidence_id='late',actual={'u':1})
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1 if waiting else 0)

    def test_failed_resource_result_exposes_no_guessed_input_package(self):self.failed_outcome(False)
    def test_waiting_resource_result_retires_resources_but_keeps_late_fee(self):self.failed_outcome(True)

    def test_race_loser_exposed_fee_survives_winner_receipt(self):
        plan=self.waiting_plan()
        self.host(speculative=True)
        self.grants['read']=self.store.grant(self.run,'read','read',expires=self.store.clock()+1000)
        winner,_=self.published('winner')
        loser=self.store.spawn_task(self.scope,self.who,'loser',plan.fingerprint,{'u':3},caps=self.grants)
        with self.assertRaises(Suspended):
            TaskRunner(self.broker).run(loser,self.who,plan,self.grants)
        self.choose([winner,loser])
        before=self.store.task_resource_result(winner,self.who)
        self.assertEqual(self.store.task_snapshot(loser,self.who)['state'],'Cancelled')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)
        self.store.settle(self.calls[0],self.who,provider='read',evidence_id='late',actual={'u':1})
        self.assertEqual(self.store.task_resource_result(winner,self.who),before)
        budget=self.store.budget(self.run,self.who)['u']
        self.assertEqual((budget['reserved'],budget['confirmed']),(0,1))

    def test_race_shared_write_remains_rejected(self):
        doc=json.loads(build(self.specs).encoded)
        next(n for n in doc['nodes'] if n['op']=='scope')['speculative']=True
        with self.assertRaisesRegex(TypeCheckError,'shared external writes'):
            check_plan(doc,self.specs)

    def test_completed_cancel_receives_instead_of_retiring_package(self):
        self.host();task,_=self.published()
        outcome=self.take(task,cancel=True)
        self.assertEqual(outcome['kind'],'Completed')
        refs=value_refs(Value.from_record(outcome['payload']))
        self.assertTrue(refs)
        for ref in refs.values():
            self.assertEqual((self.held(ref)['owner_kind'],self.held(ref)['state']),('Root','Live'))
        self.assertEqual(self.take(task,cancel=True),outcome)

    def test_cancel_before_finish_fences_late_publication(self):
        self.host()
        task=self.store.spawn_task(self.scope,self.who,'child',self.child.fingerprint,{'u':2},caps=self.grants)
        lease=self.store.claim_task(task,self.who)
        self.assertEqual(self.take(task,cancel=True)['kind'],'Cancelled')
        with self.assertRaises(StateConflict):
            self.store.finish_task(lease,self.who,Value('late').record())
        self.assertFalse(self.store.task_resource_result(task,self.who))

    def test_retired_completed_package_is_not_a_successful_receipt(self):
        self.host();task,_=self.published()
        self.store.handoff_task(task,self.who)
        with self.assertRaisesRegex(StateConflict,'retired'):
            self.take(task,cancel=True)
        self.assertIsNone(self.store.task_outcome(task,self.who))

    def test_resource_outcome_requires_explicit_receiver_access(self):
        self.host();task,result=self.published()
        with self.assertRaisesRegex(StateConflict,'explicit access'):
            self.store.take_task_outcome(task,self.who)
        self.assertIsNone(self.store.task_outcome(task,self.who))
        self.assertTrue(all(self.held(r)['owner_kind']=='Escrow' for r in value_refs(result).values()))

    def test_data_result_does_not_inherit_task_input_envelope_limit(self):
        self.host()
        task=self.store.spawn_task(self.scope,self.who,'large-data','host',{},caps=self.grants)
        lease=self.store.claim_task(task,self.who)
        text='x'*(1024*1024)
        self.store.finish_task(lease,self.who,Value(text).record())
        self.assertEqual(self.take(task)['payload']['data'],text)

    def test_outcome_replay_never_refreshes_a_forwarded_member(self):
        self.host();task,_=self.published()
        first=self.take(task)
        ref=next(iter(value_refs(Value.from_record(first['payload'])).values()))
        with self.store.resource_access(self.run,self.who,[ref]):
            onward=self.store.spawn_task(self.scope,self.who,'onward','host',{},caps=self.grants,moves={'funds':ref})
        before=self.store.events(self.run,self.who)
        self.assertEqual(self.take(task),first)
        self.assertEqual(self.store.events(self.run,self.who),before)
        self.assertEqual(self.held(ref)['owner'],onward)
        self.assertGreater(self.held(ref)['generation'],ref.generation)
        with self.store.resource_access(self.run,self.who,[ref]), self.assertRaises(StateConflict):
            self.store.join_budget(self.run,self.who,ref.resource)

    def test_race_replay_does_not_move_a_forwarded_winner(self):
        self.host(speculative=True)
        tasks=[self.published(str(i))[0] for i in range(2)]
        self.choose(tasks)
        refs=self.store.task_resource_result(tasks[0],self.who)
        with self.store.resource_access(self.run,self.who,refs.values()):
            onward=self.store.spawn_task(self.scope,self.who,'onward','host',{},caps=self.grants,moves=refs)
        before=self.store.events(self.run,self.who)
        self.assertEqual(self.choose(tasks,tasks[1]),tasks[0])
        self.assertEqual(self.store.events(self.run,self.who),before)
        self.assertEqual(self.store.task_resource_result(tasks[0],self.who),refs)
        self.assertTrue(all(self.held(r)['owner']==onward for r in refs.values()))

    def test_payload_manifest_mismatch_rolls_back_outcome(self):
        self.host();task,result=self.published()
        with self.store._tx() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?',(json.dumps(Value('forged').record()),task))
        before=self.store.events(self.run,self.who)
        with self.assertRaisesRegex(StateConflict,'resource package'):
            self.take(task)
        self.assertEqual(self.store.events(self.run,self.who),before)
        self.assertIsNone(self.store.task_outcome(task,self.who))
        self.assertTrue(all(self.held(r)['owner_kind']=='Escrow' for r in value_refs(result).values()))

    def test_payload_manifest_mismatch_does_not_cancel_race_loser(self):
        self.host(speculative=True)
        tasks=[self.published(str(i))[0] for i in range(2)]
        with self.store._tx() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?',(json.dumps(Value('forged').record()),tasks[0]))
        before=self.store.events(self.run,self.who)
        with self.assertRaisesRegex(StateConflict,'resource package'):
            self.choose(tasks)
        self.assertEqual(self.store.events(self.run,self.who),before)
        self.assertIsNone(self.store.race_winner(self.scope,self.who,'race',tasks))
        self.assertTrue(all(self.store.task_snapshot(t,self.who)['disposition']=='Attached' for t in tasks))

    def test_opaque_host_race_payload_is_not_mistaken_for_resource_metadata(self):
        self.host(speculative=True)
        tasks=[]
        for i in range(2):
            task=self.store.spawn_task(self.scope,self.who,str(i),'host',{},caps=self.grants)
            lease=self.store.claim_task(task,self.who)
            self.store.finish_task(lease,self.who,{'resource':'business-field','bundle':i})
            tasks.append(task)
        self.assertEqual(self.choose(tasks),tasks[0])
        self.assertEqual(self.store.task_snapshot(tasks[0],self.who)['result'],{'resource':'business-field','bundle':0})

    def test_private_outcome_payload_retains_bundle_labels(self):
        self.host(private=True);task,_=self.published()
        outcome=self.take(task)
        value=Value.from_record(outcome['payload'])
        self.assertEqual(value.label,Label({'owner'},{'source:private'}))
        with self.assertRaises(FlowDenied):
            self.broker.deliver(value,'public')

    def test_race_label_includes_losing_candidate(self):
        self.start()
        with TaskScope(self.broker,self.run,self.who,'scope',{'u':4},speculative=True) as scope:
            a,af=scope.spawn('a',budget_packet(self.specs),{'u':2},self.grants)
            z,zf=scope.spawn('z',budget_packet(self.specs,private=True),{'u':2},self.grants)
            af.result();zf.result()
            with self.store.resource_access(self.run,self.who):
                self.store.select_race(scope.scope,self.who,'race',[a,z],a)
            result=scope.race('race',[a,z])
            self.assertEqual(result.label,Label({'owner'},{'source:private'}))

    def test_join_and_cancel_share_one_completed_receipt(self):
        self.host();task,_=self.published()
        gate=Barrier(2)
        def take(cancel):
            gate.wait()
            return self.take(task,cancel=cancel)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(take,False);b=pool.submit(take,True)
            self.assertEqual(a.result(),b.result())
        events=self.store.events(self.run,self.who)
        self.assertEqual(sum(e['kind']=='TaskResourcesReceived' for e in events),1)

    def test_expired_parent_lease_cannot_receive_nested_outcome(self):
        self.host()
        parent=self.store.spawn_task(self.scope,self.who,'parent','host',{'u':4},caps=self.grants)
        lease=self.store.claim_task(parent,self.who)
        nested=self.store.open_scope(self.run,self.who,'nested',{'u':2},parent_lease=lease)
        task=self.store.spawn_task(nested,self.who,'child',self.child.fingerprint,{'u':2},parent_lease=lease,caps=self.grants)
        TaskRunner(self.broker).run(task,self.who,self.child,self.grants)
        current=self.store.claim_task(parent,self.who,recover=True)
        with self.assertRaises(StateConflict):
            self.take(task,lease=lease)
        self.assertIsNone(self.store.task_outcome(task,self.who))
        outcome=self.take(task,lease=current)
        self.assertTrue(all(self.held(r)['owner']==parent for r in value_refs(Value.from_record(outcome['payload'])).values()))

    def test_finish_cancel_race_has_one_resource_disposition(self):
        self.host()
        task=self.store.spawn_task(self.scope,self.who,'child',self.child.fingerprint,{'u':2},caps=self.grants)
        lease=self.store.claim_task(task,self.who)
        result=Interpreter(self.broker).run(self.child,self.run,self.who,self.grants,task_lease=lease)
        from al0.bundles import return_refs
        refs=return_refs(result)
        gate=Barrier(2)
        def finish():
            gate.wait()
            try:
                with self.store.resource_access(self.run,self.who,refs.values(),task_lease=lease):
                    self.store.finish_task(lease,self.who,result.record(),resources=refs)
                return True
            except StateConflict:return False
        def cancel():
            gate.wait()
            return self.take(task,cancel=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f=pool.submit(finish);c=pool.submit(cancel)
            done,outcome=f.result(),c.result()
        self.assertEqual(outcome['kind'],'Completed' if done else 'Cancelled')
        for ref in refs.values():
            owner=self.held(ref)
            self.assertEqual((owner['owner_kind'],owner['state']),('Root','Live') if done else ('Runtime','Retired'))

    def interpreter_crash(self,mode,point):
        plan=build(self.specs) if mode=='outcome' else race_plan(self.specs)
        self.start(plan)
        config=Path(self.temp.name)/'execute.json'
        log=Path(self.temp.name)/'calls.jsonl'
        config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants,
                                    'action':'execute','mode':mode,'log':str(log)}),encoding='utf-8')
        args=[sys.executable,'-m','tests.resource_outcome_worker',str(config)]
        process=subprocess.run([*args,point],capture_output=True,text=True,timeout=30)
        self.assertEqual(process.returncode,77,process.stdout+process.stderr)
        process=subprocess.run([*args,'none'],capture_output=True,text=True,timeout=30)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        result=json.loads(process.stdout)['data']
        self.store=Store(self.path)
        self.broker=Broker(self.store,self.specs,{'send':lambda t:Response('unexpected',{'u':1})})
        count=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(plan).data,result)
        self.broker.wait_for_workers(timeout=10)
        self.assertEqual(len(self.store.events(self.run,self.who)),count)
        if mode=='outcome':
            self.assertEqual(result['second'],'Done')
            self.assertEqual([json.loads(s)[1] for s in log.read_text().splitlines()],['first','second'])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],2 if mode=='outcome' else 0)

    def test_interpreter_crash_during_outcome_receive(self):self.interpreter_crash('outcome','after_task_resources_receive')
    def test_interpreter_crash_after_outcome_commit(self):self.interpreter_crash('outcome','after_task_outcome_commit')
    def test_interpreter_crash_during_race_receive(self):self.interpreter_crash('race','after_task_resources_receive')
    def test_interpreter_crash_after_race_commit(self):self.interpreter_crash('race','after_race_commit')

    def crash(self,action,point):
        self.host(speculative=action=='race')
        tasks=[self.published(str(i))[0] for i in range(2 if action=='race' else 1)]
        config=Path(self.temp.name)/'config.json'
        config.write_text(json.dumps({'database':str(self.path),'run':self.run,'scope':self.scope,'tasks':tasks,'action':action}),encoding='utf-8')
        process=subprocess.run([sys.executable,'-m','tests.resource_outcome_worker',str(config),point],capture_output=True,text=True,timeout=30)
        self.assertEqual(process.returncode,77,process.stdout+process.stderr)
        self.store=Store(self.path)
        if action=='race':
            committed=self.store.race_winner(self.scope,self.who,'race',tasks)
            self.assertEqual(committed,tasks[0] if point=='after_race_commit' else None)
            self.choose(tasks)
            self.assertEqual(self.store.task_snapshot(tasks[1],self.who)['disposition'],'Joined')
        else:
            committed=self.store.task_outcome(tasks[0],self.who)
            self.assertEqual(committed is not None,point=='after_task_outcome_commit')
            self.assertEqual(self.take(tasks[0],cancel=action=='cancel')['kind'],'Completed')
        refs=self.store.task_resource_result(tasks[0],self.who)
        self.assertTrue(all(self.held(r)['owner_kind']=='Root' for r in refs.values()))
        events=self.store.events(self.run,self.who)
        if action=='race':self.choose(tasks,tasks[1])
        else:self.take(tasks[0],cancel=action=='cancel')
        self.assertEqual(self.store.events(self.run,self.who),events)
        with self.store.resource_access(self.run,self.who,refs.values()):
            for ref in refs.values():self.store.join_budget(self.run,self.who,ref.resource)
        self.store.close_scope(self.scope,self.who)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_process_exit_during_outcome_receive(self):self.crash('join','after_task_resources_receive')
    def test_process_exit_before_outcome_commit(self):self.crash('join','before_task_outcome_commit')
    def test_process_exit_after_outcome_commit(self):self.crash('join','after_task_outcome_commit')
    def test_process_exit_during_completed_cancel(self):self.crash('cancel','after_task_resources_receive')
    def test_process_exit_after_completed_cancel(self):self.crash('cancel','after_task_outcome_commit')
    def test_process_exit_during_race_receive(self):self.crash('race','after_task_resources_receive')
    def test_process_exit_before_race_commit(self):self.crash('race','before_race_commit')
    def test_process_exit_after_race_commit(self):self.crash('race','after_race_commit')


if __name__=='__main__':unittest.main()
