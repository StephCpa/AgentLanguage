import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.data import Principal
from al0.errors import FlowDenied, InvalidData, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.frontend import compile_source
from al0.ir import Tool, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.resource_branches import build, catalog, demo, handled


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.who=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def send(ticket):
            self.calls.append(ticket.intent.value['args'])
            return Response({'sent':True},{'u':1})
        self.broker=Broker(self.store,self.specs,{'send':send})

    def start(self,plan):
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':10})
        self.grants={'send':self.store.grant(self.run,'send','output',expires=self.store.clock()+1000)}

    def execute(self,plan,inputs=None):
        return Interpreter(self.broker).run(plan,self.run,self.who,self.grants,inputs=inputs)

    def submit(self):
        b=PlanBuilder('submit',self.specs)
        request=b.intent_parameter('request',b.tool('send'))
        permission=b.permit_parameter('permission',request)
        return b.finish(b.admit('operation',request,permission))

    def parent(self):
        b=PlanBuilder('parent',self.specs)
        message=b.literal('message','Text','hello')
        intent=b.prepare('request',b.tool('send'),message)
        permit=b.authorize('permission',intent)
        condition=b.literal('condition','Bool',True)
        return b,intent,permit,condition

    def moved_operation(self):
        b,intent,permit,condition=self.parent()
        op=b.admit('operation',intent,permit)
        branch=PlanBuilder('identity',self.specs)
        forwarded=branch.finish(branch.operation_parameter('operation',branch.tool('send')))
        result=b.if_else('forwarded',condition,then=forwarded,otherwise=forwarded,inputs={'operation':op})
        return b,op,result

    def test_demo_source_sdk_and_replay(self):
        result=demo(self.temp.name)
        self.assertEqual(result['result'],'Done')
        self.assertEqual(result['replay'],'Done')
        self.assertEqual(result['calls'],['hello'])
        self.assertEqual(result['branches'],2)
        self.assertEqual(result['replay_added_events'],0)
        self.assertEqual(result['budget']['u']['confirmed'],1)

    def test_intent_and_permit_are_frozen_as_descriptors_and_ids(self):
        plan=build(self.specs)
        self.start(plan)
        self.execute(plan)
        with self.store._tx() as db:
            decisions=[json.loads(row[0]) for row in db.execute('SELECT decision FROM branch_choices')]
        first=next(d for d in decisions if 'permission' in d['inputs'])
        self.assertEqual(first['inputs']['request']['data']['args'],'hello')
        self.assertIsInstance(first['inputs']['permission']['data'],str)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.calls,['hello'])

    def test_permit_can_be_dropped_in_an_unselected_action_path(self):
        # Both alternatives explicitly receive the capability. The deferred
        # branch drops its affine permit and performs no external operation.
        yes=PlanBuilder('yes',self.specs)
        request=yes.intent_parameter('request',yes.tool('send'))
        permission=yes.permit_parameter('permission',request)
        yes_plan=yes.finish(handled(yes,yes.admit('operation',request,permission)))
        no=PlanBuilder('no',self.specs)
        request=no.intent_parameter('request',no.tool('send'))
        no.permit_parameter('permission',request)
        no_plan=no.finish(no.literal('deferred','Text','Deferred'))
        b,intent,permit,_=self.parent()
        condition=b.literal('defer','Bool',False)
        plan=b.finish(b.if_else('result',condition,then=yes_plan,otherwise=no_plan,inputs={'request':intent,'permission':permit}))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Deferred')
        self.assertEqual(self.execute(plan).data,'Deferred')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],0)

    def test_operation_return_transfers_responsibility_back_to_parent(self):
        b,_,result=self.moved_operation()
        plan=b.finish(handled(b,result))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.calls,['hello'])

    def test_use_of_moved_operation_is_rejected(self):
        b,op,result=self.moved_operation()
        b.inspect('old',op)
        with self.assertRaisesRegex(TypeCheckError,'already moved'):
            b.finish(result)

    def test_return_of_moved_operation_is_rejected(self):
        b,op,_=self.moved_operation()
        with self.assertRaisesRegex(TypeCheckError,'already moved'):
            b.finish(op)

    def test_use_of_moved_permit_is_rejected(self):
        b,intent,permit,condition=self.parent()
        branch=self.submit()
        op=b.if_else('first',condition,then=branch,otherwise=branch,inputs={'request':intent,'permission':permit})
        b.admit('second',intent,permit)
        with self.assertRaisesRegex(TypeCheckError,'already moved'):
            b.finish(op)

    def test_duplicate_capture_of_affine_resource_is_rejected(self):
        raw=json.loads(build(self.specs).encoded)
        raw['nodes'][4]['inputs']['second']=raw['nodes'][4]['inputs']['permission']
        with self.assertRaisesRegex(TypeCheckError,'multiple names'):
            check_plan(raw,self.specs)

    def test_permit_cannot_be_rebound_to_another_intent_with_same_tool(self):
        b,intent,permit,condition=self.parent()
        message=b.literal('other','Text','different')
        other=b.prepare('other-intent',b.tool('send'),message)
        branch=self.submit()
        op=b.if_else('result',condition,then=branch,otherwise=branch,inputs={'request':other,'permission':permit})
        with self.assertRaisesRegex(TypeCheckError,'another intent'):
            b.finish(op)

    def test_missing_resource_in_one_alternative_is_rejected(self):
        raw=json.loads(build(self.specs).encoded)
        branch=raw['nodes'][4]['cases']['Else']
        branch['nodes']=[{'id':'value','op':'literal','type':'Text','value':'dropped'}]
        branch['return']='value'
        with self.assertRaisesRegex(TypeCheckError,'every alternative'):
            check_plan(raw,self.specs)

    def test_branch_cannot_drop_operation_responsibility(self):
        b=PlanBuilder('drop',self.specs)
        b.operation_parameter('operation',b.tool('send'))
        value=b.literal('value','Bool',True)
        with self.assertRaisesRegex(TypeCheckError,'handled or returned'):
            b.finish(value)

    def test_outstanding_snapshot_must_be_handled_before_move(self):
        b,intent,permit,condition=self.parent()
        op=b.admit('operation',intent,permit)
        b.inspect('snapshot',op)
        branch=PlanBuilder('identity',self.specs)
        plan=branch.finish(branch.operation_parameter('operation',branch.tool('send')))
        result=b.if_else('moved',condition,then=plan,otherwise=plan,inputs={'operation':op})
        with self.assertRaisesRegex(TypeCheckError,'outstanding snapshots'):
            b.finish(result)

    def test_resource_contract_mismatch_is_rejected(self):
        self.specs.register(Tool.define('other','action','Text','Json','other',{'u':1}))
        raw=json.loads(build(self.specs).encoded)
        raw['nodes'][5]['cases']['Then']['nodes'][0]['type']='other'
        with self.assertRaisesRegex(TypeCheckError,'different outcome/evidence'):
            check_plan(raw,self.specs)

    def test_pure_functions_cannot_accept_resource_parameters(self):
        b=PlanBuilder('pure',self.specs,pure=True)
        b.intent_parameter('request',b.tool('send'))
        value=b.literal('value','Bool',True)
        with self.assertRaisesRegex(TypeCheckError,'effectful branch'):
            b.finish(value)

    def test_resources_cannot_be_manufactured_from_json_inputs(self):
        plan=self.submit()
        self.start(plan)
        with self.assertRaisesRegex(InvalidData,'checked branch captures'):
            self.execute(plan,{'request':Value({'tool':'send'}),'permission':Value('fake')})
        self.assertEqual(self.calls,[])

    def test_permit_moves_to_task_and_operation_returns_by_join(self):
        b,intent,permit,_=self.parent()
        scope=b.scope('scope',{'u':1})
        task=b.spawn('task',scope,self.submit(),{'u':1},inputs={'request':intent,'permission':permit})
        result=b.join('operation',task)
        b.close_scope('closed',scope)
        plan=b.finish(handled(b,result))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        count=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(len(self.store.events(self.run,self.who)),count)
        self.assertEqual(self.calls,['hello'])
        kinds=[event['kind'] for event in self.store.events(self.run,self.who)]
        self.assertEqual(kinds.count('TaskResourcesMoved'),1)
        self.assertEqual(kinds.count('TaskResourcesReceived'),1)

    def test_resource_bindings_survive_nested_branch_moves(self):
        inner=self.submit()
        b=PlanBuilder('outer',self.specs)
        request=b.intent_parameter('request',b.tool('send'))
        permission=b.permit_parameter('permission',request)
        condition=b.literal('condition','Bool',False)
        outer=b.finish(b.if_else('nested',condition,then=inner,otherwise=inner,inputs={'request':request,'permission':permission}))
        b,intent,permit,condition=self.parent()
        op=b.if_else('outer',condition,then=outer,otherwise=outer,inputs={'request':intent,'permission':permit})
        plan=b.finish(handled(b,op))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.calls,['hello'])

    def test_sensitive_branch_cannot_upgrade_a_prepared_intent(self):
        b,intent,permit,condition=self.parent()
        sensitive=b.restrict('secret-condition',condition,Label({'owner','tool:send'}))
        branch=self.submit()
        plan=b.finish(b.if_else('result',sensitive,then=branch,otherwise=branch,inputs={'request':intent,'permission':permit}))
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],0)

    def test_unknown_operation_keeps_reservation_across_move_and_replay(self):
        def timeout(ticket):
            self.calls.append('timeout')
            raise TimeoutError()
        self.broker.adapters['send']=timeout
        plan=build(self.specs)
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'NeedsReconciliation')
        self.assertEqual(self.execute(plan).data,'NeedsReconciliation')
        self.assertEqual(self.calls,['timeout'])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)

    def test_permit_parameter_must_bind_an_earlier_intent_parameter(self):
        raw=json.loads(self.submit().encoded)
        raw['nodes'][1]['type']='missing'
        with self.assertRaises(TypeCheckError):
            check_plan(raw,self.specs)

    def test_source_rejects_post_move_use_at_its_location(self):
        source=Path('examples/resource_branches.al0').read_text(encoding='utf-8')
        source=source.replace('    let result = if(selected, complete', '    let reused = admit(request, permission);\n    let result = if(selected, complete')
        from al0.source import SourceError
        with self.assertRaises(SourceError) as caught:
            compile_source(source,self.specs,entry='resource-branches')
        self.assertIn('already moved',str(caught.exception))
        self.assertEqual(caught.exception.diagnostic.code,'AL1001')

    def test_task_outcome_can_move_permit_and_return_operation(self):
        child=PlanBuilder('child',self.specs)
        child_plan=child.finish(child.literal('value','Bool',True))
        b,intent,permit,_=self.parent()
        scope=b.scope('scope',{'u':1})
        task=b.spawn('task',scope,child_plan,{'u':1})
        outcome=b.join_outcome('outcome',task)
        b.close_scope('closed',scope)
        branch=self.submit()
        op=b.match_task('moved',outcome,completed=branch,failed=branch,waiting=branch,cancelled=branch,
                        inputs={'request':intent,'permission':permit})
        plan=b.finish(handled(b,op))
        self.start(plan)
        try:
            self.assertEqual(self.execute(plan).data,'Done')
            self.assertEqual(self.execute(plan).data,'Done')
            self.assertEqual(self.calls,['hello'])
        finally:
            self.broker.wait_for_workers(timeout=10)

    def test_resource_branches_work_inside_a_task_owned_allocation(self):
        child=build(self.specs)
        b=PlanBuilder('parent-task',self.specs)
        scope=b.scope('scope',{'u':2})
        task=b.spawn('task',scope,child,{'u':2})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        try:
            self.assertEqual(self.execute(plan).data,'Done')
            self.assertEqual(self.execute(plan).data,'Done')
            self.assertEqual(self.calls,['hello'])
            self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)
        finally:
            self.broker.wait_for_workers(timeout=10)

    def test_intent_capture_retains_check_receipts(self):
        self.specs.register(Tool.define('verify','check','Text','Bool','verify',{'u':1},predicate='Approved'))
        self.specs.register(Tool.define('checked-send','action','Text','Json','output',{'u':1},predicates=['Approved']))
        self.broker=Broker(self.store,self.specs,{'send':self.broker.adapters['send'],
                    'verify':lambda ticket:Response(True,{'u':1}),'checked-send':self.broker.adapters['send']})
        branch=PlanBuilder('checked-submit',self.specs)
        request=branch.intent_parameter('request',branch.tool('checked-send'))
        permission=branch.permit_parameter('permission',request)
        child=branch.finish(branch.admit('op',request,permission))
        b=PlanBuilder('checked-root',self.specs)
        text=b.literal('text','Text','checked')
        checked=b.check('checked',b.tool('verify'),text)
        intent=b.prepare('request',b.tool('checked-send'),checked)
        permit=b.authorize('permission',intent)
        condition=b.literal('condition','Bool',True)
        op=b.if_else('op',condition,then=child,otherwise=child,inputs={'request':intent,'permission':permit})
        plan=b.finish(handled(b,op))
        self.start(plan)
        self.grants.update({name:self.store.grant(self.run,name,spec.scope,expires=self.store.clock()+1000)
                            for name,spec in self.specs.tools.items() if name!='send'})
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.calls,['checked'])
        with self.store._tx() as db:
            decision=json.loads(db.execute('SELECT decision FROM branch_choices WHERE run=?',(self.run,)).fetchone()[0])
        self.assertEqual(len(decision['inputs']['request']['data']['receipts']),1)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],2)

    def test_intent_capture_retains_private_label_and_output_restriction(self):
        b=PlanBuilder('private',self.specs)
        message=b.restrict('private',b.literal('text','Text','secret'),Label({'owner','tool:send'}))
        intent=b.prepare('request',b.tool('send'),message)
        permit=b.authorize('permission',intent)
        condition=b.literal('condition','Bool',True)
        branch=self.submit()
        op=b.if_else('operation',condition,then=branch,otherwise=branch,inputs={'request':intent,'permission':permit})
        plan=b.finish(handled(b,op))
        self.start(plan)
        result=self.execute(plan)
        self.assertEqual(result.label.readers,frozenset({'owner','tool:send'}))
        self.assertEqual(self.execute(plan).label,result.label)
        with self.assertRaises(FlowDenied):
            self.broker.deliver(result,'public')
        self.assertEqual(self.broker.deliver(result,'owner'),'Done')

    def test_new_authorization_inside_branch_accepts_intent_capture(self):
        branch=PlanBuilder('authorize-inside',self.specs)
        request=branch.intent_parameter('request',branch.tool('send'))
        permission=branch.authorize('permission',request)
        child=branch.finish(branch.admit('op',request,permission))
        b=PlanBuilder('parent',self.specs)
        intent=b.prepare('request',b.tool('send'),b.literal('text','Text','hello'))
        condition=b.literal('condition','Bool',True)
        op=b.if_else('operation',condition,then=child,otherwise=child,inputs={'request':intent})
        plan=b.finish(handled(b,op))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.calls,['hello'])

    def test_copyable_opref_captures_do_not_move_parent_handle(self):
        branch=PlanBuilder('observers',self.specs)
        a=branch.opref_parameter('a',branch.tool('send'))
        b=branch.opref_parameter('b',branch.tool('send'))
        for name,view in [('a',a),('b',b)]:
            status=branch.inspect(name+'-status',view)
            branch.match_business(name+'-result',status,'Bool',applied=True,proven_no_effect=False,
                                  not_dispatched=False,pending=False,unknown=False)
        child=branch.finish(a)
        b,intent,permit,condition=self.parent()
        operation=b.admit('operation',intent,permit)
        before=b.await_result('before',operation)
        b.match_business('before-result',before,'Bool',applied=True,proven_no_effect=False,
                         not_dispatched=False,pending=False,unknown=False)
        view=b.borrow_operation('view',operation)
        b.if_else('observed',condition,then=child,otherwise=child,inputs={'a':view,'b':view})
        plan=b.finish(handled(b,operation))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.calls,['hello'])

    def test_borrow_does_not_allow_dropping_owner_obligation(self):
        b,intent,permit,_=self.parent()
        op=b.admit('operation',intent,permit)
        view=b.borrow_operation('view',op)
        state=b.inspect('state',view)
        result=b.match_business('result',state,'Bool',applied=True,proven_no_effect=False,
                                not_dispatched=False,pending=False,unknown=False)
        with self.assertRaisesRegex(TypeCheckError,'handled or returned'):
            b.finish(result)

    def test_opref_cannot_drive_or_reconcile_an_operation(self):
        self.specs.register(Tool.define('query','reconcile','Json','Json','output',{'u':1},reconciles='send'))
        for method in ('await','reconcile'):
            b=PlanBuilder('borrower',self.specs)
            view=b.opref_parameter('view',b.tool('send'))
            state=b.await_result('state',view) if method=='await' else b.reconcile('state',b.tool('query'),view)
            result=b.match_business('result',state,'Bool',applied=True,proven_no_effect=False,
                                    not_dispatched=False,pending=False,unknown=False)
            with self.assertRaises(TypeCheckError):
                b.finish(result)

    def test_existing_opref_remains_observable_after_owner_move(self):
        b,intent,permit,condition=self.parent()
        op=b.admit('operation',intent,permit)
        view=b.borrow_operation('view',op)
        branch=PlanBuilder('complete',self.specs)
        child=branch.finish(handled(branch,branch.operation_parameter('operation',branch.tool('send'))))
        b.if_else('moved',condition,then=child,otherwise=child,inputs={'operation':op})
        status=b.inspect('after',view)
        result=b.match_business('result',status,'Text',applied='Done',proven_no_effect='No',
                                not_dispatched='No',pending='No',unknown='No')
        plan=b.finish(result)
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.calls,['hello'])

    def test_source_opref_parameter_and_borrow(self):
        source='''workflow peek(view: OpRef<"send">) -> OpRef<"send"> uses {} { return view; }
workflow main() -> Text uses {"authorize:send@output","admit:send@output"} {
 let text: Text="hello"; let request=prepare("send",text); let permission=authorize(request);
 let operation=admit(request,permission); let view=borrow_operation(operation); let yes: Bool=true;
 let copied=if(yes,peek,peek,{view:view});
 let state=await(operation);
 let result: Text=match_business(state,{Applied:"Done",NotDispatched:"No",Pending:"No",Unknown:"No",ProvenNoEffect:"No"});
 return result;
}'''
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')

    def test_process_crashes_restore_same_resource_move(self):
        for index,point in enumerate(('before_branch_commit','after_branch_commit','after_permit_consumption','after_admit_commit')):
            with self.subTest(point=point):
                path=Path(self.temp.name)/('crash'+str(index)+'.sqlite')
                store=Store(path)
                plan=build(self.specs)
                run=store.create_run(self.who,plan.fingerprint,{'u':10})
                grant=store.grant(run,'send','output',expires=store.clock()+1000)
                log=path.with_suffix('.log')
                config=path.with_suffix('.json')
                config.write_text(json.dumps({'database':str(path),'log':str(log),'run':run,'grants':{'send':grant}}),encoding='utf-8')
                crashed=subprocess.run([sys.executable,'-m','tests.resource_worker',str(config),point],capture_output=True,text=True,timeout=30)
                self.assertEqual(crashed.returncode,77,crashed.stderr)
                recovered=subprocess.run([sys.executable,'-m','tests.resource_worker',str(config),'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(recovered.returncode,0,recovered.stderr)
                self.assertEqual(json.loads(recovered.stdout)['data'],'Done')
                self.assertEqual(log.read_text(encoding='utf-8').splitlines(),['hello'])
                self.assertEqual(store.budget(run,self.who)['u']['confirmed'],1)
                self.assertEqual(store.budget(run,self.who)['u']['reserved'],0)
