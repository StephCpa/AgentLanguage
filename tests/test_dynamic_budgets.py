from concurrent.futures import ThreadPoolExecutor
import json
from al0.semantics import execution_site
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.data import MAX_INTEGER, Principal
from al0.errors import (AuthorizationDenied, BudgetExceeded, EvaluationError,
                        FlowDenied, InvalidData, ReplayMismatch, StateConflict, TypeCheckError)
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.frontend import compile_source
from al0.ir import Tool
from al0.sdk import PlanBuilder
from al0.source import SourceError
from al0.store import Store
from examples.budget_workflows import emitter
from examples.dynamic_budgets import build, catalog, demo


class DynamicBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.who=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def emit(ticket):
            self.calls.append(ticket.intent.value['args'])
            return Response(ticket.intent.value['args'],{'u':1})
        self.emit=emit
        self.broker=Broker(self.store,self.specs,{'emit':emit})

    def start(self,plan,budget=None):
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':10} if budget is None else budget)
        self.grants={name:self.store.grant(self.run,name,spec.scope,expires=self.store.clock()+1000)
                     for name,spec in self.specs.tools.items()}

    def execute(self,plan,inputs=None):
        return Interpreter(self.broker).run(plan,self.run,self.who,self.grants,inputs=inputs)

    def split_plan(self,schema='Json',kind='Data'):
        b=PlanBuilder('split',self.specs)
        amount=b.parameter('amount',schema,kind=kind)
        funds=b.split_budget('funds',amount)
        return b.finish(b.join_budget('closed',funds))

    def evaluations(self):
        with self.store._tx() as db:
            return [dict(row) for row in db.execute('SELECT * FROM budget_evaluations WHERE run=? ORDER BY rowid',(self.run,))]

    def test_demo_dynamic_scope_task_and_local_budget_replay(self):
        result=demo(self.temp.name)
        self.assertEqual((result['result'],result['replay']),('hello','hello'))
        self.assertEqual(result['calls'],['hello'])
        self.assertEqual(result['evaluations'],3)
        self.assertEqual(result['transfers'],[{'u':3}]*3)
        self.assertEqual(result['replay_added_events'],0)
        self.assertEqual(result['budget']['u']['free'],9)

    def test_integer_record_and_json_vectors_can_fund_allocations(self):
        self.specs.schema('Amounts',{'u':'Int','tokens':'Int'})
        for schema in ('Amounts','Json'):
            plan=self.split_plan(schema)
            self.start(plan,{'u':10,'tokens':100})
            self.assertTrue(self.execute(plan,{'amount':Value({'u':3,'tokens':25})}).data)
            self.assertEqual(self.store.allocation_tree(self.run,self.who)[1]['initial_transfer'],{'u':3,'tokens':25})
            self.assertEqual(self.store.budget(self.run,self.who)['tokens']['free'],100)

    def test_quantity_and_vector_validation_precedes_any_transfer(self):
        plan=self.split_plan()
        for data in ({'u':-1},{'u':True},{'u':1.5},{'u':MAX_INTEGER+1},{'u':'3'},[],None,{'u':{}},{'':1}):
            with self.subTest(data=data):
                self.start(plan)
                with self.assertRaises(InvalidData):
                    self.execute(plan,{'amount':Value(data)})
                self.assertEqual(len(self.store.allocation_tree(self.run,self.who)),1)
                self.assertEqual(self.evaluations(),[])

    def test_zero_empty_and_maximum_integer_vectors(self):
        plan=self.split_plan()
        for data in ({},{'u':0},{'u':MAX_INTEGER}):
            with self.subTest(data=data):
                self.start(plan,{'u':MAX_INTEGER})
                self.assertTrue(self.execute(plan,{'amount':Value(data)}).data)
                self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],MAX_INTEGER)

    def test_unknown_resource_even_zero_cannot_create_budget(self):
        plan=self.split_plan()
        self.start(plan)
        with self.assertRaises(BudgetExceeded):
            self.execute(plan,{'amount':Value({'unknown':0})})
        self.assertEqual(len(self.store.allocation_tree(self.run,self.who)),1)

    def test_pure_resource_construction_has_no_durable_effect(self):
        b=PlanBuilder('construct',self.specs,pure=True)
        n=b.parameter('n','Int')
        plan=b.finish(b.resources('vector',{'u':n}))
        self.start(plan)
        value=self.execute(plan,{'n':Value(3)})
        self.assertEqual(value.data,{'u':3})
        self.assertEqual(self.evaluations(),[])
        self.assertEqual(len(self.store.allocation_tree(self.run,self.who)),1)
        self.assertEqual(json.loads(plan.encoded)['uses'],[])

    def test_construction_preserves_candidate_and_observation_provenance(self):
        for kind in ('Data','Candidate','Observation'):
            b=PlanBuilder('construct',self.specs,pure=True)
            n=b.parameter('n','Int',kind=kind)
            plan=b.finish(b.resources('vector',{'u':n}))
            self.assertEqual((plan.result_type.kind,plan.result_type.name),(kind,'Json'))

    def test_resources_rejects_non_integer_operands_statically(self):
        for schema,value in (('Bool',True),('Text','3'),('Json',3)):
            b=PlanBuilder('construct',self.specs)
            n=b.literal('n',schema,value)
            with self.assertRaises(TypeCheckError):
                b.finish(b.resources('vector',{'u':n}))

    def test_negative_and_overflow_computation_do_not_allocate(self):
        for a,b,expected in ((0,1,InvalidData),(MAX_INTEGER,-1,EvaluationError)):
            builder=PlanBuilder('amounts',self.specs)
            x=builder.literal('a','Int',a)
            y=builder.literal('b','Int',b)
            n=builder.compute('n','sub',x,y)
            amount=builder.resources('amount',{'u':n})
            funds=builder.split_budget('funds',amount)
            plan=builder.finish(builder.join_budget('closed',funds))
            self.start(plan)
            with self.assertRaises(expected):
                self.execute(plan)
            self.assertEqual(self.evaluations(),[])

    def test_dynamic_budget_rejects_wrong_static_shape_and_resources(self):
        self.specs.schema('Bad',{'u':'Text'})
        for schema in ('Text','Int','Bool','Bad'):
            with self.subTest(schema=schema),self.assertRaises(TypeCheckError):
                self.split_plan(schema)
        b=PlanBuilder('bad',self.specs)
        funds=b.split_budget('funds',{'u':2})
        other=b.split_budget('other',funds)
        b.join_budget('a',other)
        with self.assertRaises(TypeCheckError):
            b.finish(b.join_budget('b',funds))

    def test_sdk_amount_references_cannot_cross_builders_or_use_raw_names(self):
        a=PlanBuilder('a',self.specs)
        ref=a.literal('amount','Json',{'u':1})
        b=PlanBuilder('b',self.specs)
        for value in (ref,'amount'):
            with self.assertRaises(TypeCheckError):
                b.split_budget('funds',value)

    def test_insufficient_parent_cannot_borrow_root_credit(self):
        b=PlanBuilder('bounded',self.specs)
        parent=b.split_budget('parent',{'u':1})
        amount=b.literal('amount','Json',{'u':2})
        child=b.split_budget('child',amount,parent=parent)
        b.join_budget('closed-child',child)
        plan=b.finish(b.join_budget('closed-parent',parent))
        self.start(plan)
        with self.assertRaises(BudgetExceeded):
            self.execute(plan)
        tree=self.store.allocation_tree(self.run,self.who)
        self.assertEqual(len(tree),2)
        self.assertEqual(tree[0]['budget']['u']['free'],9)
        self.assertEqual(tree[1]['budget']['u']['free'],1)

    def test_first_evaluation_freezes_amount_and_label(self):
        plan=self.split_plan()
        self.start(plan)
        secret=Label({'owner'})
        first=self.store.bind_budget_amount(self.run,self.who,execution_site('funds'),'split',{'u':2},control=secret)
        replay=self.store.bind_budget_amount(self.run,self.who,execution_site('funds'),'split',{'u':7})
        self.assertEqual(first,replay)
        value=self.execute(plan,{'amount':Value({'u':7})})
        self.assertEqual(value.label,secret)
        self.assertEqual(self.store.allocation_tree(self.run,self.who)[1]['initial_transfer'],{'u':2})

    def test_frozen_amount_does_not_freeze_allocation_success(self):
        plan=self.split_plan()
        self.start(plan)
        held=self.store.split_budget(self.run,self.who,'held',{'u':7})
        with self.assertRaises(BudgetExceeded):
            self.execute(plan,{'amount':Value({'u':6})})
        self.assertEqual(json.loads(self.evaluations()[0]['amount']),{'u':6})
        self.store.join_budget(self.run,self.who,held)
        self.assertTrue(self.execute(plan,{'amount':Value({'u':6})}).data)
        self.assertEqual(len(self.evaluations()),1)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_budget_evaluation_cannot_change_purpose(self):
        self.start(self.split_plan())
        self.store.bind_budget_amount(self.run,self.who,'same','scope',{'u':1})
        with self.assertRaises(ReplayMismatch):
            self.store.bind_budget_amount(self.run,self.who,'same','spawn',{'u':1})

    def test_concurrent_evaluation_chooses_one_durable_amount(self):
        self.start(self.split_plan())
        with ThreadPoolExecutor(max_workers=4) as pool:
            values=list(pool.map(lambda n:self.store.bind_budget_amount(self.run,self.who,'same','split',{'u':n}),range(1,9)))
        self.assertTrue(all(value==values[0] for value in values))
        self.assertEqual(len(self.evaluations()),1)

    def test_evaluation_checks_principal_and_task_lease(self):
        self.start(self.split_plan())
        with self.assertRaises(AuthorizationDenied):
            self.store.bind_budget_amount(self.run,Principal('tenant','other'),'bad','split',{'u':1})
        scope=self.store.open_scope(self.run,self.who,'scope',{'u':3})
        task=self.store.spawn_task(scope,self.who,'task','program',{'u':3})
        old=self.store.claim_task(task,self.who)
        current=self.store.claim_task(task,self.who,recover=True)
        with self.assertRaises(StateConflict):
            self.store.bind_budget_amount(self.run,self.who,'bad','split',{'u':1},task_lease=old)
        self.store.bind_budget_amount(self.run,self.who,'good','split',{'u':1},task_lease=current)
        self.assertEqual(len(self.evaluations()),1)

    def test_amount_label_propagates_to_tool_sink(self):
        b=PlanBuilder('secret',self.specs)
        amount=b.literal('initial','Json',{'u':2})
        amount=b.restrict('amount',amount,Label({'owner'}))
        funds=b.split_budget('funds',amount)
        message=b.literal('message','Text','constant')
        result=b.invoke('result',emitter(self.specs),inputs={'message':message},budget=funds)
        b.join_budget('closed',funds)
        plan=b.finish(result)
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual(self.calls,[])
        self.assertEqual(Label.from_data(json.loads(self.evaluations()[0]['label'])),Label({'owner'}))

    def test_task_amount_label_reaches_worker(self):
        b=PlanBuilder('secret-task',self.specs)
        initial=b.literal('initial','Json',{'u':2})
        scope=b.scope('scope',{'u':2})
        amount=b.restrict('amount',initial,Label({'owner'}))
        message=b.literal('message','Text','constant')
        task=b.spawn('task',scope,emitter(self.specs),amount,inputs={'message':message})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        try:
            with self.assertRaises(FlowDenied):
                self.execute(plan)
        finally:
            self.broker.wait_for_workers(timeout=10)
        self.assertEqual(self.calls,[])

    def test_literal_budget_paths_preserve_old_ir_and_avoid_evaluations(self):
        from examples.budget_workflows import build as old_build
        plan=old_build(self.specs)
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'hello')
        self.assertEqual(self.evaluations(),[])
        source=Path('examples/budget_workflows.al0').read_text(encoding='utf-8')
        self.assertEqual(compile_source(source,self.specs,entry='budget-workflows').plan.encoded,plan.encoded)

    def test_dynamic_split_from_surface_syntax(self):
        source='''workflow main() uses {"budget"} {
 let n: Int=2; let amount=resources({u:n}); let parent=split_budget({u:4});
 let child=split_budget_from(parent,amount); let a=join_budget(child);
 let b=join_budget(parent); return b; }'''
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        self.assertTrue(self.execute(plan).data)

    def test_undefined_budget_references_get_source_locations(self):
        for expression in ('split_budget(missing)','scope(missing,false)','spawn(s,child,missing)','spawn_with(s,child,missing,{})'):
            source='workflow child() uses {} { let v:Text="ok"; return v; }'
            source+='workflow main() uses {"budget","tasks"} { let s=scope({},false); let x='+expression+'; return x; }'
            with self.subTest(expression=expression),self.assertRaises(SourceError) as caught:
                compile_source(source,self.specs)
            diagnostic=caught.exception.diagnostic
            self.assertEqual(source[diagnostic.span.start:diagnostic.span.end],'missing')

    def test_quoted_budget_name_is_not_an_implicit_reference(self):
        source='workflow main() uses {"budget"} { let amount:Json={u:1}; let funds=split_budget("amount"); let c=join_budget(funds); return c; }'
        with self.assertRaises(SourceError):
            compile_source(source,self.specs)

    def test_live_business_change_cannot_rebudget_completed_sites(self):
        self.specs.register(Tool.define('action','action','Text','Text','output',{'u':1}))
        def timeout(ticket):
            self.calls.append('action')
            raise TimeoutError()
        self.broker=Broker(self.store,self.specs,{'emit':self.emit,'action':timeout})
        for purpose in ('split','scope','spawn'):
            with self.subTest(purpose=purpose):
                b=PlanBuilder('changing',self.specs)
                message=b.literal('initial','Text','hello')
                # Inspecting this action joins its output label before await;
                # prepare under that same context from the start.
                message=b.restrict('message',message,self.specs.tools['action'].output_label)
                intent=b.prepare('intent',b.tool('action'),message)
                permit=b.authorize('permit',intent)
                operation=b.admit('operation',intent,permit)
                view=b.borrow_operation('view',operation)
                snapshot=b.inspect('snapshot',view)
                n=b.match_business('n',snapshot,'Int',applied=4,proven_no_effect=2,not_dispatched=2,pending=2,unknown=2)
                amount=b.resources('amount',{'u':n})
                if purpose=='split':
                    funds=b.split_budget('funds',amount)
                    b.join_budget('closed',funds)
                else:
                    scope=b.scope('scope',amount if purpose=='scope' else {'u':5})
                    if purpose=='spawn':
                        child=PlanBuilder('child',self.specs)
                        child=child.finish(child.literal('value','Text','ok'))
                        task=b.spawn('task',scope,child,amount)
                        b.join('joined',task)
                    b.close_scope('closed',scope)
                state=b.await_result('state',operation)
                plan=b.finish(b.match_business('result',state,'Text',applied='Applied',proven_no_effect='ProvenNoEffect',
                                              not_dispatched='NotDispatched',pending='Pending',unknown='Unknown'))
                self.start(plan)
                self.assertEqual(self.execute(plan).data,'Unknown')
                with self.store._tx() as db:
                    op=db.execute('SELECT id FROM operations WHERE run=?',(self.run,)).fetchone()[0]
                attempt=self.store.snapshot(op,self.who)['attempts'][0]['id']
                self.store.business_result(op,self.who,provider='action',evidence_id='done:'+purpose,outcome='Applied',details={'output':'ok'})
                self.store.settle(attempt,self.who,provider='action',evidence_id='fee:'+purpose,actual={'u':1})
                before=self.store.events(self.run,self.who)
                self.assertEqual(self.execute(plan).data,'Applied')
                self.assertEqual(self.store.events(self.run,self.who),before)
                self.assertEqual([json.loads(row['amount']) for row in self.evaluations()],[{'u':2}])
                self.broker.wait_for_workers(timeout=10)

    def test_dynamic_quantities_in_loop_have_distinct_iteration_bindings(self):
        body=PlanBuilder('body',self.specs)
        body.parameter('index','Int')
        state=body.parameter('state','Int')
        one=body.literal('one','Int',1)
        next_state=body.compute('next','add',state,one)
        amount=body.resources('amount',{'u':next_state})
        funds=body.split_budget('funds',amount)
        body.join_budget('closed',funds)
        body=body.finish(next_state)
        b=PlanBuilder('loop',self.specs)
        count=b.literal('count','Int',3)
        initial=b.literal('initial','Int',0)
        plan=b.finish(b.repeat('loop',count,3,initial,body))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,3)
        events=self.store.events(self.run,self.who)
        self.assertEqual(self.execute(plan).data,3)
        self.assertEqual(self.store.events(self.run,self.who),events)
        self.assertEqual([json.loads(row['amount']) for row in self.evaluations()],[{'u':1},{'u':2},{'u':3}])
        self.assertEqual(len({row['node'] for row in self.evaluations()}),3)

    def test_waiting_dynamic_task_preserves_reserved_cost_and_late_refund(self):
        def timeout(ticket):
            self.calls.append(ticket.intent.value['args'])
            raise TimeoutError()
        self.broker=Broker(self.store,self.specs,{'emit':timeout})
        plan=build(self.specs)
        self.start(plan)
        try:
            with self.assertRaises(StateConflict):
                self.execute(plan,{'units':Value(2)})
        finally:
            self.broker.wait_for_workers(timeout=10)
        budget=self.store.budget(self.run,self.who)['u']
        self.assertEqual((budget['free'],budget['reserved']),(9,1))
        tree=self.store.allocation_tree(self.run,self.who)
        self.assertTrue(all(row['state']=='Joined' for row in tree[1:]))
        with self.store._tx() as db:
            attempt=db.execute('SELECT a.id FROM attempts a JOIN operations o ON o.id=a.operation WHERE o.run=?',(self.run,)).fetchone()[0]
        self.store.settle(attempt,self.who,provider='emit',evidence_id='no-charge',actual={'u':0})
        self.assertEqual(self.store.allocation_tree(self.run,self.who)[0]['budget']['u']['free'],10)
        self.assertEqual(self.calls,['hello'])

    def test_v13_migration_preserves_allocations_and_installs_evaluations(self):
        plan=self.split_plan()
        self.start(plan)
        original=self.store.split_budget(self.run,self.who,'old',{'u':2})
        with self.store._tx() as db:
            db.execute('DROP TABLE budget_evaluations')
            db.execute('PRAGMA user_version=13')
        self.store=Store(self.path)
        self.broker=Broker(self.store,self.specs,{'emit':self.emit})
        self.assertTrue(self.execute(plan,{'amount':Value({'u':1})}).data)
        self.assertEqual(self.store.allocation_tree(self.run,self.who)[1]['id'],original)
        with self.store._tx() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)

    def test_process_crashes_between_evaluation_and_transfer(self):
        points=[(point,index) for point in ('before_budget_amount_commit','after_budget_amount_commit') for index in (1,2,3)]
        points += [(point,1) for point in ('after_scope_commit','after_spawn_commit','after_split_commit')]
        for index,(point,occurrence) in enumerate(points):
            with self.subTest(point=point,occurrence=occurrence):
                path=Path(self.temp.name)/('crash'+str(index)+'.sqlite')
                store=Store(path)
                plan=build(self.specs)
                run=store.create_run(self.who,plan.fingerprint,{'u':10})
                grants={'emit':store.grant(run,'emit','output',expires=store.clock()+1000)}
                config=path.with_suffix('.json')
                log=path.with_suffix('.log')
                config.write_text(json.dumps({'database':str(path),'run':run,'grants':grants,'log':str(log)}),encoding='utf-8')
                command=[sys.executable,'-m','tests.dynamic_budget_worker',str(config)]
                crash=subprocess.run(command+[point,str(occurrence)],capture_output=True,text=True,timeout=30)
                self.assertEqual(crash.returncode,77,crash.stderr)
                resumed=subprocess.run(command+['none','1'],capture_output=True,text=True,timeout=30)
                self.assertEqual(resumed.returncode,0,resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout)['data'],'hello')
                self.assertEqual(log.read_text(encoding='utf-8').splitlines(),['hello'])
                events=store.events(run,self.who)
                self.assertEqual(sum(e['kind']=='BudgetAmountBound' for e in events),3)
                self.assertEqual(sum(e['kind']=='BudgetSplit' for e in events),3)
                self.assertEqual(store.budget(run,self.who)['u']['free'],9)
                self.assertTrue(all(row['state']=='Joined' for row in store.allocation_tree(run,self.who)[1:]))
