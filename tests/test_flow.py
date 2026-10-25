from al0.semantics import execution_site
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
import unittest

from al0.bindings import normalize_bindings
from al0.concurrency import TaskScope
from al0.data import Intent, Principal, Slot
from al0.errors import FlowDenied, InvalidData, ReplayMismatch, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label, PUBLIC, plan_label
from al0.ir import Catalog, Tool, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store


SECRET=Label({'owner','tool:model','tool:check','tool:vault','tool:query'},{'private-input'})


def catalog():
    result=Catalog()
    result.register(Tool.define('model','infer','Text','Text','model',{'u':1}))
    result.register(Tool.define('check','check','Text','Bool','sandbox',{'u':1},predicate='Valid'))
    result.register(Tool.define('vault','observe','Text','Text','private',{'u':1},output_label=SECRET))
    result.register(Tool.define('public','observe','Text','Text','public',{'u':1},output_label=PUBLIC))
    result.register(Tool.define('send','action','Text','Json','network',{'u':1}))
    result.register(Tool.define('strict','action','Text','Json','strict',{'u':1},accepted_taints=[]))
    result.register(Tool.define('query','reconcile','Json','Json','network',{'u':1},reconciles='send'))
    return result


def text_branch(specs, name, *, send=False):
    b=PlanBuilder(name,specs)
    v=b.literal('constant','Text',name)
    if send:
        intent=b.prepare('intent',b.tool('send'),v)
        permit=b.authorize('permit',intent)
        return b.finish(b.admit('op',intent,permit))
    return b.finish(v)


def secret_task(specs):
    b=PlanBuilder('secret-task',specs)
    request=b.literal('request','Text','read')
    b.observe('read',b.tool('vault'),request)
    return b.finish(b.literal('answer','Text','constant'))


def outcome_plan(specs):
    b=PlanBuilder('parent',specs)
    scope=b.scope('scope',{'u':2})
    task=b.spawn('child',scope,secret_task(specs),{'u':1})
    outcome=b.join_outcome('outcome',task)
    b.close_scope('close',scope)
    branches={k:text_branch(specs,k) for k in ('completed','failed','waiting','cancelled')}
    return b.finish(b.match_task('match',outcome,**branches))


class LabelTests(unittest.TestCase):
    def test_join_is_associative_commutative_and_idempotent(self):
        labels=[PUBLIC,SECRET,Label(set()),Label({'owner','auditor'},{'web'}),Label(taints={'model'})]
        for a in labels:
            self.assertEqual(a.join(a),a)
            self.assertEqual(a.join(PUBLIC),a)
            for b in labels:
                self.assertEqual(a.join(b),b.join(a))
                for c in labels:
                    self.assertEqual(a.join(b).join(c),a.join(b.join(c)))

    def test_empty_readers_blocks_every_external_recipient(self):
        with self.assertRaises(FlowDenied):
            Label(set()).check_sink(['owner'])
        Label(set()).check_sink([])

    def test_integrity_and_confidentiality_are_independent(self):
        SECRET.check_sink(['owner'],['private-input'])
        for recipient,accepted in [('stranger',None),('owner',[])]:
            with self.assertRaises(FlowDenied):
                SECRET.check_sink([recipient],accepted)

    def test_invalid_labels_are_rejected(self):
        for data in ({'readers':[]},{'readers':'owner','taints':[]},{'readers':None,'taints':[1]}):
            with self.assertRaises(InvalidData):
                Label.from_data(data)

    def test_labels_are_canonical_detached_and_part_of_intent_identity(self):
        readers=['b','a']
        label=Label(readers)
        readers.clear()
        intent=Intent('x','1','s',{},label=label)
        self.assertEqual(intent,Intent('x','1','s',{},label=Label(['a','b'])))
        self.assertNotEqual(intent.digest,Intent('x','1','s',{}).digest)
        self.assertEqual(Store._intent(intent.encoded).label,label)
        self.assertNotIn('flow',Intent('x','1','s',{}).value)

    def test_bindings_normalize_public_and_preserve_restrictions(self):
        self.assertEqual(normalize_bindings({'x':Value(1,label=PUBLIC).record()}),{'x':{'data':1,'receipts':[]}})
        record=Value('s',label=SECRET).record()
        self.assertEqual(Value.from_record(normalize_bindings({'x':record})['x']).label,SECRET)


class FlowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.sqlite'
        self.store=Store(self.path)
        self.principal=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def callback(ticket):
            name=ticket.intent.value['tool']
            self.calls.append((name,ticket.intent.label))
            return Response(True if name=='check' else ticket.intent.value['args'],{'u':1})
        self.adapters={name:callback for name in self.specs.tools}
        self.broker=Broker(self.store,self.specs,self.adapters)

    def tearDown(self):
        self.broker.wait_for_workers(timeout=10)

    def start(self,plan):
        self.run=self.store.create_run(self.principal,plan.fingerprint,{'u':20})
        self.grants={n:self.store.grant(self.run,n,s.scope,expires=self.store.clock()+1000) for n,s in self.specs.tools.items()}

    def execute(self,plan,*,inputs=None):
        return Interpreter(self.broker).run(plan,self.run,self.principal,self.grants,inputs=inputs)

    def action(self,b,value,tool='send'):
        intent=b.prepare('intent',b.tool(tool),value)
        permit=b.authorize('permit',intent)
        return b.admit('operation',intent,permit)

    def test_confidential_action_denied_before_budget_reservation(self):
        b=PlanBuilder('deny',self.specs)
        v=b.restrict('secret',b.literal('value','Text','sensitive'),SECRET)
        plan=b.finish(self.action(b,v))
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.budget(self.run,self.principal)['u']['free'],20)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operations WHERE run=?',(self.run,)).fetchone()[0],0)

    def test_model_and_checker_retain_confidentiality_and_model_taint(self):
        b=PlanBuilder('checked-secret',self.specs)
        v=b.restrict('secret',b.literal('value','Text','sensitive'),SECRET)
        draft=b.infer('draft',b.tool('model'),v)
        checked=b.check('checked',b.tool('check'),draft)
        plan=b.finish(checked)
        self.start(plan)
        value=self.execute(plan)
        self.assertEqual(value.data,'sensitive')
        self.assertEqual(value.label,SECRET.join(Label(taints={'model:model'})))
        self.assertEqual(len(value.receipts),1)
        self.assertEqual(self.broker.deliver(value,'owner'),'sensitive')
        with self.assertRaises(FlowDenied):
            self.broker.deliver(value,'outsider')
        before=self.store.events(self.run,self.principal)
        self.store=Store(self.path)
        self.broker=Broker(self.store,self.specs,self.adapters)
        self.assertEqual(self.execute(plan),value)
        self.assertEqual(self.store.events(self.run,self.principal),before)
        self.assertEqual(len(self.calls),2)

    def test_checked_model_output_still_rejected_by_strict_integrity_sink(self):
        b=PlanBuilder('tainted-check',self.specs)
        v=b.infer('draft',b.tool('model'),b.literal('request','Text','work'))
        v=b.check('checked',b.tool('check'),v)
        plan=b.finish(self.action(b,v,'strict'))
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual([n for n,_ in self.calls],['model','check'])

    def test_model_is_a_sink_before_it_can_generate_a_candidate(self):
        b=PlanBuilder('private-model-input',self.specs)
        request=b.restrict('secret',b.literal('request','Text','s'),Label({'owner'}))
        plan=b.finish(b.infer('draft',b.tool('model'),request))
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.budget(self.run,self.principal)['u']['free'],20)

    def test_business_state_constant_retains_action_label(self):
        b=PlanBuilder('business-branch',self.specs)
        label=Label({'tool:send','owner'})
        value=b.restrict('secret',b.literal('request','Text','s'),label)
        operation=self.action(b,value)
        state=b.await_result('await',operation)
        result=b.match_business('result',state,'Text',applied='yes',proven_no_effect='no',pending='wait',unknown='wait',not_dispatched='wait')
        plan=b.finish(result)
        self.start(plan)
        result=self.execute(plan)
        self.assertEqual(result.data,'yes')
        self.assertEqual(result.label.readers,label.readers)
        self.assertIn('external:send',result.label.taints)
        with self.assertRaises(FlowDenied):
            self.broker.deliver(result,'outsider')

    def test_restrict_cannot_remove_existing_taints_or_widen_readers(self):
        b=PlanBuilder('no-laundering',self.specs)
        v=b.parameter('input','Text')
        v=b.restrict('relax',v,PUBLIC)
        plan=b.finish(v)
        self.start(plan)
        self.assertEqual(self.execute(plan,inputs={'input':Value('s',label=SECRET)}).label,SECRET)

    def test_input_label_is_part_of_recovery_binding(self):
        b=PlanBuilder('identity',self.specs)
        plan=b.finish(b.parameter('input','Text'))
        self.start(plan)
        self.execute(plan,inputs={'input':Value('s',label=SECRET)})
        with self.assertRaises(ReplayMismatch):
            self.execute(plan,inputs={'input':Value('s')})

    def test_source_contract_labels_constant_return_and_blocks_later_sink(self):
        b=PlanBuilder('implicit',self.specs)
        b.observe('read',b.tool('vault'),b.literal('request','Text','read'))
        constant=b.literal('constant','Text','done')
        plan=b.finish(self.action(b,constant))
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual([n for n,_ in self.calls],['vault'])

    def test_old_intent_cannot_be_authorized_under_new_secret_control(self):
        b=PlanBuilder('stale-intent',self.specs)
        constant=b.literal('value','Text','constant')
        intent=b.prepare('intent',b.tool('send'),constant)
        b.observe('secret',b.tool('vault'),constant)
        permit=b.authorize('permit',intent)
        plan=b.finish(b.admit('operation',intent,permit))
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual([n for n,_ in self.calls],['vault'])

    def test_queued_low_intent_cannot_dispatch_under_new_secret_control(self):
        b=PlanBuilder('late-dispatch',self.specs)
        constant=b.literal('value','Text','constant')
        operation=self.action(b,constant)
        b.observe('secret',b.tool('vault'),constant)
        state=b.await_result('await',operation)
        result=b.match_business('result',state,'Text',applied='yes',proven_no_effect='no',pending='wait',unknown='wait',not_dispatched='wait')
        plan=b.finish(result)
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual([n for n,_ in self.calls],['vault'])
        with closing(sqlite3.connect(self.path)) as db:
            phase=db.execute("SELECT phase FROM attempts JOIN operations ON attempts.operation=operations.id WHERE node=?",(execution_site('operation'),)).fetchone()[0]
        self.assertEqual(phase,'Queued')

    def test_task_inputs_and_completed_return_preserve_label(self):
        child=PlanBuilder('identity',self.specs)
        plan=child.finish(child.parameter('input','Text'))
        self.start(plan)
        with TaskScope(self.broker,self.run,self.principal,'scope',{'u':2}) as scope:
            task,_=scope.spawn('child',plan,{'u':1},{},inputs={'input':Value('s',label=SECRET)})
            value=scope.join(task)
        self.assertEqual(value.label,SECRET)
        self.assertEqual(Value.from_record(self.store.task_snapshot(task,self.principal)['result']).label,SECRET)

    def test_failed_and_waiting_outcomes_preserve_declared_secret_bound(self):
        for exception,kind in [(ValueError,'Failed'),(TimeoutError,'Waiting')]:
            def fail(ticket):
                raise exception('private data must not be copied into failure payload')
            self.broker=Broker(self.store,self.specs,{**self.adapters,'vault':fail})
            plan=secret_task(self.specs)
            self.start(plan)
            with TaskScope(self.broker,self.run,self.principal,'scope',{'u':2}) as scope:
                task,_=scope.spawn('child',plan,{'u':1},self.grants)
                outcome=scope.join_outcome(task)
            self.assertEqual(outcome['kind'],kind)
            self.assertEqual(Label.from_data(outcome['label']),SECRET)
            self.assertEqual(Value.from_record(outcome['payload']).label,SECRET)
            self.assertNotIn('private data must',json.dumps(outcome))

    def test_constant_outcome_branches_inherit_secret_control(self):
        plan=outcome_plan(self.specs)
        self.start(plan)
        value=self.execute(plan)
        self.assertEqual(value.data,'completed')
        self.assertEqual(value.label,SECRET)
        with self.assertRaises(FlowDenied):
            self.broker.deliver(value,'public')

    def test_cancelled_outcome_is_labelled_before_response_arrives(self):
        entered,release=Event(),Event()
        def private(ticket):
            entered.set()
            release.wait(timeout=10)
            return Response('late',{'u':1})
        self.broker=Broker(self.store,self.specs,{**self.adapters,'vault':private})
        plan=secret_task(self.specs)
        self.start(plan)
        try:
            with TaskScope(self.broker,self.run,self.principal,'scope',{'u':2}) as scope:
                task,_=scope.spawn('child',plan,{'u':1},self.grants)
                self.assertTrue(entered.wait(timeout=5))
                outcome=scope.cancel_outcome(task)
            self.assertEqual(outcome['kind'],'Cancelled')
            self.assertEqual(Label.from_data(outcome['label']),SECRET)
        finally:
            release.set()
        self.broker.wait_for_workers(timeout=10)
        self.assertEqual(self.store.task_outcome(task,self.principal),outcome)

    def test_secret_task_and_outcome_survive_process_crashes(self):
        config=Path(self.temp.name)/'worker.json'
        plan=outcome_plan(self.specs)
        for point in ('before_task_finish_commit','after_task_finish_commit','before_task_outcome_commit','after_task_outcome_commit'):
            with self.subTest(point=point):
                self.start(plan)
                config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants}),encoding='utf-8')
                command=[sys.executable,'-m','tests.flow_worker',str(config)]
                crashed=subprocess.run(command+[point],capture_output=True,text=True,timeout=20)
                self.assertEqual(crashed.returncode,77,crashed.stdout+crashed.stderr)
                recovered=subprocess.run(command+['recover'],capture_output=True,text=True,timeout=20)
                self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
                value=Value.from_record(json.loads(recovered.stdout))
                self.assertEqual(value.data,'completed')
                self.assertEqual(value.label,SECRET)
                self.assertEqual(self.store.budget(self.run,self.principal)['u']['confirmed'],1)
                self.assertEqual(sum(e['kind']=='DispatchStarted' for e in self.store.events(self.run,self.principal)),1)

    def test_effect_inside_outcome_branch_and_continuation_are_protected(self):
        for inside in (True,False):
            b=PlanBuilder('parent',self.specs)
            scope=b.scope('scope',{'u':2})
            task=b.spawn('child',scope,secret_task(self.specs),{'u':1})
            outcome=b.join_outcome('outcome',task)
            b.close_scope('close',scope)
            branches={k:text_branch(self.specs,k,send=inside) for k in ('completed','failed','waiting','cancelled')}
            result=b.match_task('match',outcome,**branches)
            if not inside:
                result=self.action(b,b.literal('public-constant','Text','constant'))
            plan=b.finish(result)
            self.start(plan)
            with self.assertRaises(FlowDenied):
                self.execute(plan)
        self.assertNotIn('send',[n for n,_ in self.calls])

    def test_race_result_includes_cancelled_loser_label_before_late_response(self):
        entered,release=Event(),Event()
        def secret(ticket):
            entered.set()
            release.wait(timeout=10)
            return Response('late',{'u':1})
        def public(ticket):
            if not entered.wait(timeout=5):
                raise TimeoutError()
            return Response('winner',{'u':1})
        self.broker=Broker(self.store,self.specs,{**self.adapters,'vault':secret,'public':public})
        child=PlanBuilder('public-child',self.specs)
        request=child.literal('request','Text','read')
        child.observe('read',child.tool('public'),request)
        public_plan=child.finish(child.literal('answer','Text','public-winner'))
        b=PlanBuilder('race',self.specs)
        scope=b.scope('scope',{'u':2},speculative=True)
        left=b.spawn('left',scope,secret_task(self.specs),{'u':1})
        right=b.spawn('right',scope,public_plan,{'u':1})
        winner=b.race('winner',[left,right])
        b.close_scope('close',scope)
        plan=b.finish(winner)
        self.start(plan)
        try:
            value=self.execute(plan)
            self.assertEqual(value.data,'public-winner')
            self.assertEqual(value.label,SECRET)
            self.assertEqual(self.store.budget(self.run,self.principal)['u']['reserved'],1)
            with self.assertRaises(FlowDenied):
                self.broker.deliver(value,'outsider')
        finally:
            release.set()
        self.broker.wait_for_workers(timeout=10)
        self.assertEqual(self.execute(plan),value)

    def test_reconciliation_query_inherits_target_flow_and_keeps_digest(self):
        self.start(text_branch(self.specs,'host'))
        label=Label({'tool:send','tool:query'},{'private'})
        intent=Intent('send','1','network','s',label=label)
        permit=self.store.authorize(self.run,self.principal,intent,self.grants['send'])
        op=self.store.admit(self.run,self.principal,Slot('send'),intent,permit)
        query=[]
        def lookup(ticket):
            query.append(ticket.intent)
            return Response({'operation':op,'intent_digest':intent.digest,'business':None,'bills':[]},{'u':1})
        self.broker=Broker(self.store,self.specs,{**self.adapters,'query':lookup})
        self.broker.reconcile(self.run,self.principal,'query',op,'query',self.grants['query'])
        self.assertEqual(query[0].value['args']['intent_digest'],intent.digest)
        self.assertEqual(query[0].label.readers,label.readers)
        self.assertIn('private',query[0].label.taints)

    def test_reconciliation_is_also_a_confidentiality_sink(self):
        self.start(text_branch(self.specs,'host'))
        intent=Intent('send','1','network','s',label=Label({'tool:send'}))
        permit=self.store.authorize(self.run,self.principal,intent,self.grants['send'])
        op=self.store.admit(self.run,self.principal,Slot('send'),intent,permit)
        with self.assertRaises(FlowDenied):
            self.broker.reconcile(self.run,self.principal,'query',op,'query',self.grants['query'])
        self.assertEqual(self.calls,[])


class FlowBoundaryTests(unittest.TestCase):
    def test_labelled_raw_store_call_requires_registered_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite')
            who=Principal('t','u')
            run=store.create_run(who,'host',{'u':2})
            store.register_contract('send','1',{'u':1})
            grant=store.grant(run,'send','scope',expires=store.clock()+1000)
            intent=Intent('send','1','scope','s',label=Label({'tool:send'}))
            with self.assertRaises(FlowDenied):
                store.authorize(run,who,intent,grant)
            store.register_tool_flow('send','1',recipients=['tool:send'])
            permit=store.authorize(run,who,intent,grant)
            op=store.admit(run,who,Slot('op'),intent,permit)
            attempt=store.snapshot(op,who)['attempts'][0]['id']
            ticket=store.start_attempt(op,attempt,who)
            self.assertEqual(ticket.intent,intent)

    def test_dispatch_fails_closed_if_labelled_call_policy_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite')
            who=Principal('t','u')
            run=store.create_run(who,'host',{'u':2})
            store.register_contract('send','1',{'u':1})
            grant=store.grant(run,'send','scope',expires=store.clock()+1000)
            store.register_tool_flow('send','1',recipients=['tool:send'])
            intent=Intent('send','1','scope','s',label=Label({'tool:send'}))
            permit=store.authorize(run,who,intent,grant)
            op=store.admit(run,who,Slot('op'),intent,permit)
            # Simulate missing trusted metadata on recovery, rather than relax it.
            with closing(sqlite3.connect(store.path)) as db:
                db.execute('DELETE FROM tool_flows')
                db.commit()
            attempt=store.snapshot(op,who)['attempts'][0]['id']
            with self.assertRaises(FlowDenied):
                store.start_attempt(op,attempt,who)
            self.assertEqual(store.snapshot(op,who)['attempts'][0]['phase'],'Queued')

    def test_tool_policy_change_requires_new_version(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite')
            store.register_contract('x','1',{'u':1})
            store.register_tool_flow('x','1',recipients=['a'])
            with self.assertRaises(ReplayMismatch):
                store.register_tool_flow('x','1',recipients=['a','b'])

    def test_retry_rechecks_flow_policy_without_spending_more_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'state.sqlite')
            who=Principal('t','u')
            run=store.create_run(who,'host',{'u':3})
            store.register_contract('send','1',{'u':1},retry_window=300)
            store.register_tool_flow('send','1',recipients=['tool:send'])
            grant=store.grant(run,'send','scope',expires=store.clock()+1000,max_attempts=2)
            intent=Intent('send','1','scope','s',label=Label({'tool:send'}))
            permit=store.authorize(run,who,intent,grant)
            op=store.admit(run,who,Slot('op'),intent,permit)
            attempt=store.snapshot(op,who)['attempts'][0]['id']
            store.start_attempt(op,attempt,who)
            before=store.budget(run,who)
            with closing(sqlite3.connect(store.path)) as db:
                db.execute('DELETE FROM tool_flows')
                db.commit()
            with self.assertRaises(FlowDenied):
                store.retry(op,who)
            self.assertEqual(store.budget(run,who),before)

    def test_internal_adapter_must_be_explicit_and_taints_are_still_checked(self):
        spec=Tool.define('local','check','Text','Bool','sandbox',{},predicate='Valid',recipients=[])
        Label(set()).check_sink(spec.flow_policy['recipients'])
        with self.assertRaises(FlowDenied):
            Label(set(),{'untrusted'}).check_sink([],[])
        self.assertEqual(Tool.define('remote','infer','Text','Text','remote',{}).flow_policy['recipients'],['tool:remote'])

    def test_restrict_cannot_cast_an_affine_handle_to_data(self):
        specs=catalog()
        b=PlanBuilder('bad',specs)
        v=b.literal('v','Text','x')
        intent=b.prepare('i',b.tool('send'),v)
        bad=b.restrict('bad',intent,PUBLIC)
        with self.assertRaises(TypeCheckError):
            b.finish(bad)

    def test_plan_bound_includes_unselected_branch_sources(self):
        specs=catalog()
        b=PlanBuilder('outer',specs)
        scope=b.scope('scope',{'u':1})
        task=b.spawn('child',scope,text_branch(specs,'pure'),{})
        outcome=b.join_outcome('outcome',task)
        b.close_scope('close',scope)
        branches={k:text_branch(specs,k) for k in ('completed','waiting','cancelled')}
        branches['failed']=secret_task(specs)
        plan=b.finish(b.match_task('match',outcome,**branches))
        self.assertEqual(plan_label(plan,specs),SECRET)

    def test_v7_migration_preserves_ledger_and_creates_flow_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.sqlite'
            store=Store(path)
            who=Principal('t','u')
            run=store.create_run(who,'legacy',{'u':3})
            before=store.budget(run,who)
            with closing(sqlite3.connect(path)) as db:
                db.executescript('DROP TABLE task_flows; DROP TABLE tool_flows; PRAGMA user_version=7;')
            store=Store(path)
            self.assertEqual(store.budget(run,who),before)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)


if __name__=='__main__':
    unittest.main()
