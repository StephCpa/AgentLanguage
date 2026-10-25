from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.data import MAX_INTEGER, Principal
from al0.errors import EvaluationError, FlowDenied, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label, plan_label
from al0.frontend import compile_source
from al0.ir import Catalog, Tool, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.conditional_repair import demo


def catalog():
    specs=Catalog()
    specs.schema('Row',{'n':'Int','text':'Text'})
    specs.schema('Pair',{'left':'Text','right':'Text'})
    specs.register(Tool.define('model','infer','Text','Row','model',{'u':1}))
    specs.register(Tool.define('check','check','Row','Bool','check',{'u':1},predicate='Valid'))
    specs.register(Tool.define('work','action','Text','Json','work',{'u':1}))
    specs.register(Tool.define('logger','observe','Text','Text','log',{'u':1},shared_write=True))
    specs.register(Tool.define('strict','action','Text','Json','strict',{'u':1},accepted_taints=[]))
    return specs


def constant(specs,name,text):
    b=PlanBuilder(name,specs)
    return b.finish(b.literal('value','Text',text))


def changing_status_plan(specs):
    branches=[]
    for name in ('yes','no'):
        b=PlanBuilder(name,specs)
        message=b.parameter('message','Text')
        branches.append(b.finish(b.observe('logged',b.tool('logger'),message)))
    b=PlanBuilder('changing-status',specs)
    request=b.literal('request','Text','work')
    intent=b.prepare('intent',b.tool('work'),request)
    permit=b.authorize('permit',intent)
    op=b.admit('operation',intent,permit)
    state=b.await_result('state',op)
    condition=b.match_business('condition',state,'Bool',applied=True,proven_no_effect=False,not_dispatched=False,pending=False,unknown=False)
    message=b.match_business('message',state,'Text',applied='after',proven_no_effect='before',not_dispatched='before',pending='before',unknown='before')
    return b.finish(b.if_else('decision',condition,then=branches[0],otherwise=branches[1],inputs={'message':message}))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.who=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def callback(ticket):
            name=ticket.intent.value['tool']
            self.calls.append((name,ticket.intent.value['args']))
            if name=='work':
                raise TimeoutError()
            if name=='check':
                return Response(True,{'u':1})
            if name=='model':
                return Response({'n':7,'text':'candidate'},{'u':1})
            return Response(ticket.intent.value['args'],{'u':1})
        self.callbacks={name:callback for name in self.specs.tools}
        self.broker=Broker(self.store,self.specs,self.callbacks)

    def tearDown(self):
        self.broker.wait_for_workers(timeout=10)

    def start(self,plan):
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':10})
        self.grants={n:self.store.grant(self.run,n,s.scope,expires=self.store.clock()+1000) for n,s in self.specs.tools.items()}

    def execute(self,plan,inputs=None):
        return Interpreter(self.broker).run(plan,self.run,self.who,self.grants,inputs=inputs)

    def parameter_compute(self,operator,schema,arity):
        b=PlanBuilder('pure',self.specs)
        args=[b.parameter('v'+str(i),schema) for i in range(arity)]
        return b.finish(b.compute('computed',operator,*args))

    def test_all_primitive_operators_have_deterministic_values(self):
        cases=[('add','Int',[2,3],5),('sub','Int',[2,3],-1),('mul','Int',[2,3],6),
               ('div','Int',[-7,3],-3),('mod','Int',[-7,3],2),('neg','Int',[-7],7),
               ('eq','Json',[True,1],False),('eq','Json',[{'a':1,'b':2},{'b':2,'a':1}],True),
               ('lt','Text',['a','b'],True),('le','Int',[3,3],True),('gt','Int',[3,2],True),('ge','Int',[2,3],False),
               ('and','Bool',[True,False],False),('or','Bool',[False,True],True),('not','Bool',[True],False),
               ('concat','Text',['中','文'],'中文'),('length','Text',['中文'],2)]
        for operator,schema,args,expected in cases:
            with self.subTest(operator=operator):
                plan=self.parameter_compute(operator,schema,len(args))
                self.start(plan)
                result=self.execute(plan,{f'v{i}':Value(v) for i,v in enumerate(args)})
                self.assertEqual(result.data,expected)
                self.assertIs(type(result.data),type(expected))
        self.assertEqual(self.calls,[])

    def test_overflow_division_zero_and_large_concatenation_are_explicit(self):
        for operator,args in [('add',[MAX_INTEGER,1]),('mul',[MAX_INTEGER,2]),('div',[1,0]),('mod',[1,0])]:
            plan=self.parameter_compute(operator,'Int',2)
            self.start(plan)
            with self.assertRaises(EvaluationError):
                self.execute(plan,{f'v{i}':Value(v) for i,v in enumerate(args)})
        b=PlanBuilder('large',self.specs)
        value=b.parameter('input','Text')
        plan=b.finish(b.compute('double','concat',value,value))
        self.start(plan)
        with self.assertRaises(EvaluationError):
            self.execute(plan,{'input':Value('x'*600000)})

    def test_record_size_counts_shared_input_more_than_once(self):
        b=PlanBuilder('record-limit',self.specs)
        value=b.parameter('input','Text')
        plan=b.finish(b.record('pair','Pair',{'left':value,'right':value}))
        self.start(plan)
        with self.assertRaises(EvaluationError):
            self.execute(plan,{'input':Value('x'*600000)})

    def test_candidate_fields_and_transforms_remain_candidates(self):
        b=PlanBuilder('candidate',self.specs)
        value=b.parameter('input','Row',kind='Candidate')
        n=b.field('n',value,'n')
        text=b.field('text',value,'text')
        sum_=b.compute('sum','add',n,n)
        plan=b.finish(b.record('rebuilt','Row',{'n':sum_,'text':text}))
        self.assertEqual(plan.result_type.kind,'Candidate')
        self.start(plan)
        label=Label({'owner'},{'model:source'})
        result=self.execute(plan,{'input':Value({'n':7,'text':'s'},label=label)})
        self.assertEqual(result.data,{'n':14,'text':'s'})
        self.assertEqual(result.label,label)
        self.assertEqual(result.receipts,())

    def test_checked_projection_drops_artifact_evidence_but_keeps_label(self):
        b=PlanBuilder('projection',self.specs)
        value=b.parameter('input','Row',kind='Checked',predicates=['Valid'])
        plan=b.finish(b.field('n',value,'n'))
        self.assertEqual(plan.result_type.kind,'Data')
        self.assertEqual(plan.result_type.predicates,frozenset())
        self.start(plan)
        data={'n':7,'text':'x'}
        receipt=self.store.record_check(self.run,self.who,'Valid','1',data,{})
        result=self.execute(plan,{'input':Value(data,(receipt,),Label({'owner'}))})
        self.assertEqual(result.receipts,())
        self.assertEqual(result.label.readers,frozenset({'owner'}))

    def test_if_executes_only_the_selected_effect_branch(self):
        yes=PlanBuilder('yes',self.specs)
        message=yes.literal('message','Text','yes')
        yes_plan=yes.finish(yes.observe('log',yes.tool('logger'),message))
        no=PlanBuilder('no',self.specs)
        message=no.literal('message','Text','no')
        no_plan=no.finish(no.observe('log',no.tool('logger'),message))
        b=PlanBuilder('select',self.specs)
        condition=b.parameter('condition','Bool')
        plan=b.finish(b.if_else('select',condition,then=yes_plan,otherwise=no_plan))
        self.start(plan)
        result=self.execute(plan,{'condition':Value(False)})
        self.assertEqual(result.data,'no')
        self.assertEqual(self.calls,[('logger','no')])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)

    def test_if_freezes_choice_and_captures_when_operation_state_changes(self):
        plan=changing_status_plan(self.specs)
        self.start(plan)
        first=self.execute(plan)
        self.assertEqual(first.data,'before')
        self.complete_work()
        before=self.store.events(self.run,self.who)
        replay=self.execute(plan)
        self.assertEqual(replay,first)
        self.assertEqual(self.store.events(self.run,self.who),before)
        self.assertEqual(self.calls,[('work','work'),('logger','before')])

    def complete_work(self):
        with closing(sqlite3.connect(self.path)) as db:
            operation=db.execute("SELECT id FROM operations WHERE run=? AND node='operation'",(self.run,)).fetchone()[0]
        attempt=self.store.snapshot(operation,self.who)['attempts'][0]['id']
        self.store.business_result(operation,self.who,provider='work',evidence_id='late:'+operation,outcome='Applied',details={'output':{}})
        self.store.settle(attempt,self.who,provider='work',evidence_id='bill:'+attempt,actual={'u':1})

    def test_if_crash_before_or_after_decision_commit_has_distinct_recovery(self):
        plan=changing_status_plan(self.specs)
        config=Path(self.temp.name)/'worker.json'
        logfile=Path(self.temp.name)/'calls.txt'
        for point,expected in [('before_branch_commit','after'),('after_branch_commit','before')]:
            with self.subTest(point=point):
                self.start(plan)
                logfile.write_text('',encoding='utf-8')
                config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants,'log':str(logfile)}),encoding='utf-8')
                command=[sys.executable,'-m','tests.control_worker',str(config)]
                crash=subprocess.run(command+[point],capture_output=True,text=True,timeout=20)
                self.assertEqual(crash.returncode,77,crash.stdout+crash.stderr)
                self.complete_work()
                resumed=subprocess.run(command+['recover'],capture_output=True,text=True,timeout=20)
                self.assertEqual(resumed.returncode,0,resumed.stdout+resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout)['data'],expected)
                self.assertEqual(logfile.read_text(encoding='utf-8'),expected+'\n')
                self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],2)

    def test_same_if_node_in_distinct_tasks_uses_distinct_activation(self):
        child=PlanBuilder('child',self.specs)
        condition=child.parameter('condition','Bool')
        child_plan=child.finish(child.if_else('choice',condition,then=constant(self.specs,'yes','yes'),otherwise=constant(self.specs,'no','no')))
        b=PlanBuilder('parent',self.specs)
        yes=b.literal('yes','Bool',True)
        no=b.literal('no','Bool',False)
        scope=b.scope('scope',{})
        left=b.spawn('left',scope,child_plan,{},inputs={'condition':yes})
        right=b.spawn('right',scope,child_plan,{},inputs={'condition':no})
        l=b.join('l',left)
        r=b.join('r',right)
        b.close_scope('closed',scope)
        result=b.compute('both','concat',l,r)
        plan=b.finish(result)
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'yesno')
        self.broker.wait_for_workers(timeout=10)
        choices=[e for e in self.store.events(self.run,self.who) if e['kind']=='BranchChosen']
        self.assertEqual(len(choices),2)
        self.assertEqual(len({e['data']['activation'] for e in choices}),2)

    def test_nested_if_decisions_and_tool_sites_do_not_alias(self):
        source='''workflow leaf(message: Text) uses {"observe:logger"} { let log=observe("logger",message); return log; }
workflow nested(flag: Bool, message: Text) uses {"observe:logger"} {
 let choice=if(flag,leaf,leaf,{message:message}); return choice;
}
workflow main() uses {"observe:logger"} {
 let flag: Bool=true; let message: Text="safe";
 let choice=if(flag,nested,nested,{flag:flag,message:message}); return choice;
}'''
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'safe')
        before=self.store.events(self.run,self.who)
        self.assertEqual(self.execute(plan).data,'safe')
        self.assertEqual(self.store.events(self.run,self.who),before)
        self.assertEqual(len(self.calls),1)
        choices=[e['data']['node'] for e in before if e['kind']=='BranchChosen']
        self.assertEqual(len(set(choices)),2)

    def test_secret_condition_protects_branch_and_following_effects(self):
        for inside in (True,False):
            branch=PlanBuilder('branch',self.specs)
            message=branch.literal('message','Text','constant')
            if inside:
                message=branch.observe('log',branch.tool('logger'),message)
            leaf=branch.finish(message)
            b=PlanBuilder('private-if',self.specs)
            condition=b.parameter('condition','Bool')
            result=b.if_else('choose',condition,then=leaf,otherwise=leaf)
            if not inside:
                message=b.literal('constant','Text','constant')
                result=b.observe('log',b.tool('logger'),message)
            plan=b.finish(result)
            self.start(plan)
            with self.assertRaises(FlowDenied):
                self.execute(plan,{'condition':Value(True,label=Label({'owner'}))})
        self.assertEqual(self.calls,[])

    def test_candidate_condition_taint_is_not_endorsed_by_branch_constants(self):
        action=PlanBuilder('strict-action',self.specs)
        message=action.literal('message','Text','constant')
        intent=action.prepare('intent',action.tool('strict'),message)
        permit=action.authorize('permit',intent)
        leaf=action.finish(action.admit('operation',intent,permit))
        b=PlanBuilder('tainted-control',self.specs)
        request=b.literal('request','Text','model')
        draft=b.infer('draft',b.tool('model'),request)
        n=b.field('n',draft,'n')
        limit=b.literal('limit','Int',0)
        condition=b.compute('condition','gt',n,limit)
        plan=b.finish(b.if_else('select',condition,then=leaf,otherwise=leaf))
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan)
        self.assertEqual(self.calls,[('model','model')])

    def test_pure_result_continuation_inherits_input_label(self):
        source='''workflow main(input: Text) uses {"observe:logger"} {
 let secret=restrict(input,{readers:["owner"],taints:[]}); let size=length(secret);
 let constant: Text="constant"; let sent=observe("logger",constant); return sent;
}'''
        plan=compile_source(source,self.specs).plan
        self.start(plan)
        with self.assertRaises(FlowDenied):
            self.execute(plan,{'input':Value('private')})

    def test_if_task_bound_includes_unselected_private_branch(self):
        yes=constant(self.specs,'yes','public')
        b=PlanBuilder('private',self.specs)
        value=b.restrict('secret',b.literal('value','Text','private'),Label({'owner'}))
        no=b.finish(value)
        b=PlanBuilder('branch',self.specs)
        condition=b.literal('condition','Bool',True)
        plan=b.finish(b.if_else('choose',condition,then=yes,otherwise=no))
        self.assertEqual(plan_label(plan,self.specs).readers,frozenset({'owner'}))

    def test_new_decision_after_run_finish_is_rejected(self):
        plan=constant(self.specs,'main','safe')
        self.start(plan)
        self.store.finish_run(self.run,self.who)
        with self.assertRaises(StateConflict):
            self.store.choose_branch(self.run,self.who,'late',Value(True).record(),{})


class ControlStaticTests(unittest.TestCase):
    def setUp(self):
        self.specs=catalog()

    def test_operators_reject_wrong_types_arity_and_opaque_values(self):
        for operator,schema,count in [('add','Bool',2),('and','Int',2),('add','Int',1),('unknown','Int',2),('lt','Json',2)]:
            b=PlanBuilder('wrong',self.specs)
            refs=[b.parameter('v'+str(i),schema) for i in range(count)]
            with self.assertRaises(TypeCheckError):
                b.finish(b.compute('answer',operator,*refs))
        b=PlanBuilder('opaque',self.specs)
        s=b.scope('scope',{})
        bad=b.compute('bad','eq',s,s)
        b.close_scope('closed',s)
        with self.assertRaises(TypeCheckError):
            b.finish(bad)

    def test_field_and_record_require_declared_shapes(self):
        for field in ('missing','n'):
            b=PlanBuilder('wrong-field',self.specs)
            value=b.parameter('v','Row' if field=='missing' else 'Json')
            with self.assertRaises(TypeCheckError):
                b.finish(b.field('f',value,field))
        b=PlanBuilder('wrong-record',self.specs)
        value=b.literal('v','Text','x')
        with self.assertRaises(TypeCheckError):
            b.finish(b.record('r','Row',{'n':value,'text':value}))

    def test_if_rejects_non_bool_missing_case_and_inconsistent_returns(self):
        leaf=constant(self.specs,'leaf','x')
        b=PlanBuilder('wrong',self.specs)
        value=b.literal('v','Int',1)
        with self.assertRaises(TypeCheckError):
            b.finish(b.if_else('i',value,then=leaf,otherwise=leaf))
        b=PlanBuilder('main',self.specs)
        value=b.literal('v','Bool',True)
        plan=b.finish(b.if_else('i',value,then=leaf,otherwise=leaf))
        document=json.loads(plan.encoded)
        del document['nodes'][-1]['cases']['Else']
        with self.assertRaises(TypeCheckError):
            check_plan(document,self.specs)
        other=PlanBuilder('other',self.specs)
        integer=other.finish(other.literal('n','Int',1))
        b=PlanBuilder('mismatch',self.specs)
        value=b.literal('v','Bool',True)
        with self.assertRaises(TypeCheckError):
            b.finish(b.if_else('i',value,then=leaf,otherwise=integer))

    def test_branch_effects_cannot_expand_parent_or_speculative_boundary(self):
        b=PlanBuilder('writer',self.specs)
        text=b.literal('text','Text','x')
        leaf=b.finish(b.observe('write',b.tool('logger'),text))
        b=PlanBuilder('parent',self.specs,uses=[])
        condition=b.literal('condition','Bool',True)
        with self.assertRaises(TypeCheckError):
            b.finish(b.if_else('choose',condition,then=leaf,otherwise=leaf))
        b=PlanBuilder('branch',self.specs)
        condition=b.literal('condition','Bool',True)
        branch=b.finish(b.if_else('choose',condition,then=leaf,otherwise=leaf))
        parent=PlanBuilder('parent',self.specs)
        scope=parent.scope('scope',{'u':2},speculative=True)
        task=parent.spawn('child',scope,branch,{'u':1})
        result=parent.join('result',task)
        parent.close_scope('closed',scope)
        with self.assertRaises(TypeCheckError):
            parent.finish(result)

    def test_affine_capture_cannot_be_hidden_in_a_data_parameter(self):
        leaf=PlanBuilder('leaf',self.specs)
        plan=leaf.finish(leaf.parameter('handle','Json'))
        b=PlanBuilder('parent',self.specs)
        condition=b.literal('condition','Bool',True)
        scope=b.scope('scope',{})
        selected=b.if_else('if',condition,then=plan,otherwise=plan,inputs={'handle':scope})
        b.close_scope('closed',scope)
        with self.assertRaises(TypeCheckError):
            b.finish(selected)

    def test_branch_operation_return_still_requires_handling(self):
        b=PlanBuilder('action',self.specs)
        text=b.literal('text','Text','x')
        intent=b.prepare('intent',b.tool('work'),text)
        permit=b.authorize('permit',intent)
        leaf=b.finish(b.admit('op',intent,permit))
        b=PlanBuilder('parent',self.specs)
        condition=b.literal('condition','Bool',True)
        b.if_else('ignored',condition,then=leaf,otherwise=leaf)
        answer=b.literal('answer','Text','done')
        with self.assertRaises(TypeCheckError):
            b.finish(answer)

    def test_source_and_sdk_example_share_identity_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            result=demo(directory)
        self.assertTrue(result['identical_ir'])
        self.assertEqual(result['result'],'Done')
        self.assertEqual(result['branch_decisions'],1)
        self.assertEqual(result['replay_added_events'],0)
        self.assertEqual(result['endpoint']['business_operations'],1)

    def test_v8_migration_preserves_prior_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite'
            store=Store(path)
            who=Principal('t','u')
            run=store.create_run(who,'legacy',{'u':3})
            before=store.budget(run,who)
            with closing(sqlite3.connect(path)) as db:
                db.executescript('DROP TABLE branch_choices; PRAGMA user_version=8;')
            upgraded=Store(path)
            self.assertEqual(upgraded.budget(run,who),before)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)


if __name__=='__main__':
    unittest.main()
