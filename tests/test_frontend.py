import json
import random
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.data import Principal
from al0.demo import Endpoint, adapters, catalog
from al0.errors import FlowDenied, InvalidData
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.frontend import compile_source, read_catalog
from al0.ir import FIELDS, Tool, check_plan
from al0.source import SourceError
from al0.store import Store
from examples.repair_sdk import build as sdk_repair
from examples.task_outcomes import build as sdk_outcomes
from examples.frontend_demo import demo


EXAMPLES=Path(__file__).resolve().parents[1]/'examples'
PURE='workflow main() -> Text uses {} { let value: Text = "hello"; return value; }'


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.specs=catalog()
        self.repair=(EXAMPLES/'repair.al0').read_text(encoding='utf-8')
        self.outcomes=(EXAMPLES/'task_outcomes.al0').read_text(encoding='utf-8')

    def compile(self,source,entry=None):
        return compile_source(source,self.specs,filename='input.al0',entry=entry)

    def reject(self,source,*,code=None,entry=None):
        with self.assertRaises(SourceError) as caught:
            self.compile(source,entry)
        diagnostic=caught.exception.diagnostic
        self.assertTrue(diagnostic.hint)
        if code:
            self.assertEqual(diagnostic.code,code)
        return diagnostic

    def test_repair_source_sdk_and_hand_ir_are_identical(self):
        source=self.compile(self.repair)
        raw=check_plan((EXAMPLES/'repair.ir.json').read_text(encoding='utf-8'),self.specs)
        sdk=sdk_repair(self.specs)
        self.assertEqual(source.plan.encoded,sdk.encoded)
        self.assertEqual(source.plan.fingerprint,raw.fingerprint)

    def test_task_outcomes_source_matches_sdk_including_nested_cases(self):
        result=self.compile(self.outcomes,'task-outcomes')
        sdk=sdk_outcomes(self.specs)
        self.assertEqual(result.plan.encoded,sdk.encoded)
        self.assertEqual(result.plan.fingerprint,sdk.fingerprint)

    def test_whitespace_comments_crlf_and_filename_do_not_change_identity(self):
        original=self.compile(PURE)
        modified=compile_source('// comment\r\n'+PURE.replace('let value','\r\n  let value'),self.specs,filename='renamed.al0')
        self.assertEqual(original.plan.fingerprint,modified.plan.fingerprint)
        self.assertNotEqual(original.source_map,modified.source_map)

    def test_literal_or_node_name_change_changes_identity(self):
        plan=self.compile(PURE).plan
        self.assertNotEqual(plan.fingerprint,self.compile(PURE.replace('hello','different')).plan.fingerprint)
        self.assertNotEqual(plan.fingerprint,self.compile(PURE.replace('value','renamed')).plan.fingerprint)

    def test_unicode_and_escapes_round_trip_without_becoming_code(self):
        data='中文\n"quoted" // comment marker and __import__("os")'
        source=PURE.replace('"hello"',json.dumps(data,ensure_ascii=False))
        plan=self.compile(source).plan
        self.assertEqual(json.loads(plan.encoded)['nodes'][0]['value'],data)

    def test_canonical_records_lists_negative_integers_and_null(self):
        source='workflow main() uses {} { let v: Json = {text: "ok", xs: [null, true, false, -3], nested: {x: 1}}; return v; }'
        data=json.loads(self.compile(source).plan.encoded)['nodes'][0]['value']
        self.assertEqual(data,{'text':'ok','xs':[None,True,False,-3],'nested':{'x':1}})

    def test_forward_workflow_reference_is_resolved(self):
        first='workflow main() uses {"tasks"} { let s=scope({},false); let t=spawn(s,child,{}); let v=join(t); let c=close_scope(s); return v; }'
        child='workflow child() uses {} { let v: Text="child"; return v; }'
        self.assertEqual(self.compile(first+child).plan.result_type.name,'Text')

    def test_multi_workflow_entry_is_explicit_without_main(self):
        source=PURE.replace('main','a')+PURE.replace('main','b')
        self.reject(source,code='AL8002')
        self.assertEqual(self.compile(source,'b').entry,'b')
        self.reject(source,entry='missing',code='AL8002')

    def test_unused_workflows_are_still_checked(self):
        self.reject(PURE+'workflow unused() uses {} { return missing; }',code='AL8002')

    def test_source_map_contains_exact_node_and_parameter_ranges(self):
        text='// 中文\nworkflow main(input: Text) uses {} {\n  let x: Text = "ok";\n  return input;\n}\n'
        result=self.compile(text)
        spans=result.source_map['workflows']['main']
        node=spans['nodes']['x']
        self.assertEqual((node['line'],node['column']),(3,3))
        self.assertEqual(text[node['start']:node['end']],'let x: Text = "ok";')
        param=spans['nodes']['input']
        self.assertEqual(text[param['start']:param['end']],'input')
        self.assertNotIn('"ok"',json.dumps(result.source_map))

    def test_undefined_value_points_to_use_not_workflow(self):
        text='workflow main() uses {} {\n let value = check("patch-tests", missing);\n return value;\n}'
        error=self.reject(text,code='AL8002')
        self.assertEqual((error.span.line,error.span.column),(2,35))
        self.assertEqual(text[error.span.start:error.span.end],'missing')

    def test_duplicate_binding_has_related_definition(self):
        text='workflow main() uses {} { let x: Text="first"; let x: Text="second"; return x; }'
        error=self.reject(text,code='AL8002')
        self.assertEqual(len(error.related),1)
        self.assertIn('first',text[error.related[0].start:error.related[0].end])

    def test_duplicate_workflow_and_parameter_names_rejected(self):
        self.reject(PURE+PURE,code='AL8002')
        self.reject('workflow main(x: Text, x: Text) uses {} { return x; }',code='AL8002')

    def test_literal_requires_schema_and_cannot_construct_evidence(self):
        self.reject(PURE.replace('value: Text','value'))
        self.reject(PURE.replace('value: Text','value: Checked<Text, TestsPassed>'))

    def test_annotations_assert_without_casting_candidate(self):
        self.reject(self.repair.replace('let draft =','let draft: Patch ='),code='AL6001')
        self.reject(PURE.replace('-> Text','-> Int'),code='AL6001')

    def test_candidate_cannot_bypass_action_evidence(self):
        error=self.reject(self.repair.replace('prepare("repo.merge", tested)','prepare("repo.merge", draft)'),code='AL6001')
        self.assertEqual(error.span.line,8)
        self.assertTrue(error.related)

    def test_duplicate_affine_use_is_reported_at_second_use(self):
        text=self.repair.replace('let status =','let again = admit(intent, permit);\n    let status =')
        error=self.reject(text,code='AL1001')
        self.assertIn('let again',text[error.span.start:error.span.end])
        self.assertEqual(len(error.related),2)

    def test_effects_are_not_inferred_or_silently_expanded(self):
        self.reject(self.repair.replace('"check:patch-tests",',''),code='AL6001')
        self.reject(PURE.replace('uses {}','uses {"network:any"}'),code='AL6001')

    def test_incomplete_business_outcomes_are_rejected(self):
        self.reject(self.repair.replace('Unknown: "NeedsReconciliation",',''),code='AL3002')

    def test_incomplete_task_outcomes_and_mismatched_captures_rejected(self):
        self.reject(self.outcomes.replace('Waiting: waiting,',''),entry='task-outcomes',code='AL6001')
        self.reject(self.outcomes.replace('{patch: draft}','{}'),entry='task-outcomes',code='AL6001')

    def test_nested_speculative_write_error_points_to_child(self):
        source='''workflow main() uses {"tasks", "authorize:repo.merge@repo", "admit:repo.merge@repo", "check:patch-tests"} {
 let s=scope({units: 3},true); let t=spawn(s,writer,{units: 3});
 let v=join(t); let c=close_scope(s); return v;
}
workflow writer() uses {"authorize:repo.merge@repo", "admit:repo.merge@repo", "check:patch-tests"} {
 let p: Patch={patch:"fix",base_version:7}; let tested=check("patch-tests",p);
 let i=prepare("repo.merge",tested); let a=authorize(i); let op=admit(i,a); return op;
}'''
        error=self.reject(source,code='AL6001')
        self.assertEqual(error.span.line,5)
        self.assertIn('speculative',error.message)

    def test_cycles_rejected_without_python_recursion_error(self):
        source='workflow main() uses {"tasks"} { let s=scope({},false); let t=spawn(s,main,{}); let v=join(t); let c=close_scope(s); return v; }'
        self.assertIn('recursive',self.reject(source,code='AL8002').message)

    def test_dag_expansion_is_bounded_before_materializing_ir(self):
        source='workflow leaf() uses {} { let x: Text="x"; return x; }'
        previous='leaf'
        for index in range(14):
            name='main' if index==13 else 'w'+str(index)
            source+=f'workflow {name}() uses {{"tasks"}} {{ let s=scope({{}},false); let a=spawn(s,{previous},{{}}); let b=spawn(s,{previous},{{}}); let x=join(a); let y=join(b); let c=close_scope(s); return x; }}'
            previous=name
        error=self.reject(source)
        self.assertIn('limit',error.message)

    def test_source_size_and_nested_data_are_bounded(self):
        self.reject('//'+('a'*(1024*1024)))
        nested='['*66+'0'+']'*66
        self.reject(PURE.replace('value: Text = "hello"','value: Json = '+nested))

    def test_module_parameter_limit_applies_across_workflows(self):
        parameters=','.join('p'+str(i)+':Text' for i in range(5001))
        source=f'workflow a({parameters}) uses {{}} {{ return p0; }}workflow b({parameters}) uses {{}} {{ return p0; }}'
        self.assertIn('10000 bindings',self.reject(source).message)

    def test_seeded_source_mutations_return_checked_ir_or_structured_diagnostics(self):
        rng=random.Random(20261025)
        for index in range(150):
            position=rng.randrange(len(self.repair))
            replacement=rng.choice(['(',')','{','}',':',';','"','@','return','let','0','_',''])
            mutated=self.repair[:position]+replacement+self.repair[position+rng.randrange(1,5):]
            try:
                result=self.compile(mutated)
            except SourceError as error:
                self.assertTrue(error.diagnostic.code.startswith('AL'))
                self.assertLessEqual(error.diagnostic.span.end,len(mutated))
            else:
                self.assertEqual(check_plan(result.plan.encoded,self.specs).fingerprint,result.plan.fingerprint)

    def test_truncated_illegal_and_host_syntax_rejected_without_echoing_payload(self):
        for text in ('',PURE[:-1],PURE.replace('"hello"','"TOP_SECRET'),PURE.replace('"hello"','eval("TOP_SECRET")'),
                     PURE.replace('"hello"','1.2'),PURE.replace('"hello"','9223372036854775808'),
                     PURE.replace('"hello"','01'),PURE.replace('"hello"','"\\ud800"'),PURE+' import os;'):
            with self.subTest(text=text[:30]):
                error=self.reject(text)
                self.assertNotIn('TOP_SECRET',error.render())

    def test_duplicate_record_or_case_keys_rejected(self):
        self.reject(PURE.replace('value: Text = "hello"','value: Json = {x:1, x:2}'))
        self.reject(self.repair.replace('Applied: "Done",','Applied: "Done", Applied: "Again",'))

    def test_source_cannot_declare_trusted_tools_or_checker_callbacks(self):
        self.reject('tool evil = "python:os.system";'+PURE)
        self.reject(self.repair.replace('"patch-tests"','"python:os.system"'),code='AL6001')

    def test_every_existing_ir_operation_has_a_source_form(self):
        specs=catalog(reconciliation=True)
        specs.register(Tool.define('read','observe','Text','Text','read',{'units':1}))
        source='''fn pure_identity(input: Text) -> Text { return input; }
fn loop_step(index: Int,state: Text) -> Text { return state; }
workflow child() uses {} { let value: Text="safe"; return value; }
workflow identity(input: Text) uses {} { return input; }
workflow main() uses {"tasks","observe:read","infer:code-model","check:patch-tests",
 "authorize:repo.merge@repo","admit:repo.merge@repo","reconcile:repo.query"} {
 let input: Text="fix";
 let private=restrict(input,{readers:null,taints:["context"]});
 let observed=observe("read",private);
 let draft=infer("code-model",observed); let tested=check("patch-tests",draft);
 let base=field(tested,"base_version"); let one: Int=1; let next=add(base,one);
 let patch=field(tested,"patch"); let rebuilt=record("Patch",{patch:patch,base_version:next});
 let yes: Bool=true; let routed=if(yes,identity,identity,{input:input});
 let called=call(pure_identity,{input:input}); let count: Int=2;
 let repeated=repeat(count,3,called,loop_step,{});
 let intent=prepare("repo.merge",tested); let permit=authorize(intent); let operation=admit(intent,permit);
 let view=borrow_operation(operation); let inspected=inspect(view);
 let first: Text=match_business(inspected,{Applied:"a",NotDispatched:"n",Pending:"p",Unknown:"u",ProvenNoEffect:"x"});
 let state=await(operation);
 let second: Text=match_business(state,{Applied:"a",NotDispatched:"n",Pending:"p",Unknown:"u",ProvenNoEffect:"x"});
 let reconciled=reconcile("repo.query",operation);
 let third: Text=match_business(reconciled,{Applied:"a",NotDispatched:"n",Pending:"p",Unknown:"u",ProvenNoEffect:"x"});
 let s=scope({},true); let a=spawn(s,child,{}); let b=spawn(s,child,{}); let winner=race([a,b]);
 let c=spawn_with(s,identity,{}, {input: input}); let joined=join(c);
 let d=spawn(s,child,{}); let transferred=handoff(d);
 let e=spawn(s,child,{}); let cancelled=cancel_outcome(e);
 let cancel_result=match_task(cancelled,{Completed:child,Failed:child,Waiting:child,Cancelled:child},{});
 let f=spawn(s,child,{}); let finished=join_outcome(f);
 let result=match_task(finished,{Completed:child,Failed:child,Waiting:child,Cancelled:child},{});
 let closed=close_scope(s); return result;
}'''
        compiled=compile_source(source,specs)
        found=set()
        def walk(document):
            for node in document['nodes']:
                found.add(node['op'])
                if node['op'] in ('spawn','spawn_with','spawn_caps','call','repeat','invoke','invoke_caps'):
                    walk(node['plan'])
                if node['op'] in ('match_task','if','match_admission','match_variant'):
                    for branch in node['cases'].values():
                        walk(branch)
        walk(json.loads(compiled.plan.encoded))
        from examples.admission_outcomes import catalog as admission_catalog
        admission=compile_source(Path('examples/admission_outcomes.al0').read_text(encoding='utf-8'),
                                 admission_catalog(),entry='admission-outcomes')
        walk(json.loads(admission.plan.encoded))
        from examples.budget_workflows import catalog as budget_catalog
        budgeted=compile_source(Path('examples/budget_workflows.al0').read_text(encoding='utf-8'),
                                budget_catalog(),entry='budget-workflows')
        walk(json.loads(budgeted.plan.encoded))
        from examples.capability_calls import catalog as capability_catalog
        capabilities=compile_source((EXAMPLES/'capability_calls.al0').read_text(encoding='utf-8'),
                                    capability_catalog(),entry='capability-calls')
        walk(json.loads(capabilities.plan.encoded))
        dynamic=compile_source((EXAMPLES/'dynamic_budgets.al0').read_text(encoding='utf-8'),
                               budget_catalog(),entry='dynamic-budgets')
        walk(json.loads(dynamic.plan.encoded))
        task_caps=compile_source((EXAMPLES/'task_capabilities.al0').read_text(encoding='utf-8'),
                                 capability_catalog(),entry='task-capabilities')
        walk(json.loads(task_caps.plan.encoded))
        from examples.resource_bundles import catalog as bundle_catalog
        bundles=compile_source((EXAMPLES/'resource_bundles.al0').read_text(encoding='utf-8'),
                               bundle_catalog(),entry='resource-bundles')
        walk(json.loads(bundles.plan.encoded))
        from examples.business_variants import catalog as variant_catalog
        variants=compile_source((EXAMPLES/'business_variants.al0').read_text(encoding='utf-8'),
                                variant_catalog(),entry='variant-funded')
        walk(json.loads(variants.plan.encoded))
        from examples.inference_results import catalog as inference_catalog
        inference=compile_source((EXAMPLES/'inference_results.al0').read_text(encoding='utf-8'),
                                 inference_catalog(),entry='inference-results')
        walk(json.loads(inference.plan.encoded))
        progress=compile_source((EXAMPLES/'inference_progress.al0').read_text(encoding='utf-8'),
                                inference_catalog(),entry='inference-progress')
        walk(json.loads(progress.plan.encoded))
        preflight=compile_source((EXAMPLES/'inference_admission.al0').read_text(encoding='utf-8'),
                                 inference_catalog(),entry='inference-admission')
        walk(json.loads(preflight.plan.encoded))
        self.assertEqual(found,set(FIELDS))


class FrontendExecutionTests(unittest.TestCase):
    def test_source_execution_can_resume_from_sdk_without_extra_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            result=demo(directory)
        self.assertEqual(result['result'],'Done')
        self.assertEqual(result['sdk_replay_result'],'Done')
        self.assertEqual(result['replay_added_events'],0)
        self.assertEqual(result['budget']['units']['confirmed'],4)
        self.assertEqual(result['endpoint']['business_operations'],1)

    def test_source_task_outcomes_have_expected_effects_and_replay(self):
        source=(EXAMPLES/'task_outcomes.al0').read_text(encoding='utf-8')
        for mode,message,cost,reserved,mutations in [('success','Done',4,0,1),('failed','NeedsRevision',3,0,0),('waiting','NeedsReconciliation',2,1,0)]:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                specs=catalog()
                compiled=compile_source(source,specs,entry='task-outcomes')
                store=Store(Path(directory)/'state.sqlite')
                endpoint=Endpoint(Path(directory)/'endpoint.sqlite')
                callbacks=adapters(endpoint)
                if mode=='failed':
                    callbacks['patch-tests']=lambda t:Response(False,{'units':1})
                if mode=='waiting':
                    def timeout(ticket):
                        raise TimeoutError()
                    callbacks['patch-tests']=timeout
                who=Principal('t','u')
                run=store.create_run(who,compiled.plan.fingerprint,{'units':10})
                grants={n:store.grant(run,n,s.scope,expires=store.clock()+1000) for n,s in specs.tools.items()}
                broker=Broker(store,specs,callbacks)
                executor=Interpreter(broker)
                result=executor.run(compiled.plan,run,who,grants)
                broker.wait_for_workers(timeout=10)
                before=store.events(run,who)
                replay=executor.run(sdk_outcomes(specs),run,who,grants)
                broker.wait_for_workers(timeout=10)
                self.assertEqual(result,replay)
                self.assertEqual(result.data,message)
                self.assertEqual(store.events(run,who),before)
                self.assertEqual(store.budget(run,who)['units']['confirmed'],cost)
                self.assertEqual(store.budget(run,who)['units']['reserved'],reserved)
                self.assertEqual(endpoint.state()['business_operations'],mutations)

    def test_source_cannot_remove_input_flow_label(self):
        specs=catalog()
        source='workflow main(input: Text) uses {"infer:code-model"} { let relaxed=restrict(input,{readers:null,taints:[]}); let draft=infer("code-model",relaxed); return draft; }'
        plan=compile_source(source,specs).plan
        calls=[]
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'state.sqlite')
            who=Principal('t','u')
            run=store.create_run(who,plan.fingerprint,{'units':10})
            grant=store.grant(run,'code-model','model',expires=store.clock()+1000)
            broker=Broker(store,specs,{n:lambda t:calls.append(t) for n in specs.tools})
            with self.assertRaises(FlowDenied):
                Interpreter(broker).run(plan,run,who,{'code-model':grant},inputs={'input':Value('s',label=Label({'owner'}))})
            self.assertEqual(calls,[])
            self.assertEqual(store.budget(run,who)['units']['free'],10)


class FrontendCLITests(unittest.TestCase):
    def test_cli_emits_recheckable_ir_and_separate_source_map(self):
        with tempfile.TemporaryDirectory() as directory:
            ir=Path(directory)/'plan.json'
            mapping=Path(directory)/'map.json'
            result=subprocess.run([sys.executable,'-m','al0.frontend',str(EXAMPLES/'repair.al0'),'--demo-catalog',
                                   '--emit-ir',str(ir),'--source-map',str(mapping),'--json'],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)
            self.assertTrue(json.loads(result.stdout)['ok'])
            self.assertEqual(check_plan(ir.read_text(encoding='utf-8'),catalog()).encoded,sdk_repair(catalog()).encoded)
            self.assertIn('operation',json.loads(mapping.read_text(encoding='utf-8'))['workflows']['repair-example']['nodes'])

    def test_cli_failed_check_does_not_emit_ir_or_overwrite_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'invalid.al0'
            output=Path(directory)/'ir.json'
            source.write_text(PURE.replace('return value','return missing'),encoding='utf-8')
            command=[sys.executable,'-m','al0.frontend',str(source),'--demo-catalog','--json','--emit-ir']
            result=subprocess.run(command+[str(output)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,1)
            self.assertFalse(output.exists())
            self.assertEqual(json.loads(result.stdout)['diagnostic']['span']['line'],1)
            before=source.read_bytes()
            result=subprocess.run(command+[str(source)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,1)
            self.assertEqual(source.read_bytes(),before)

    def test_trusted_data_catalog_supports_flow_contracts(self):
        doc={'catalog_version':1,'schemas':{'Row':{'name':'Text'}},'tools':[
            {'name':'read','kind':'observe','input_type':'Text','output_type':'Row','scope':'records','upper_bound':{},
             'recipients':[],'output_label':{'readers':['owner'],'taints':['db']}}]}
        specs=read_catalog(json.dumps(doc))
        self.assertEqual(specs.tools['read'].output_label,Label({'owner'},{'db'}))
        source='workflow main() uses {"observe:read"} { let q: Text="key"; let row=observe("read",q); return row; }'
        self.assertEqual(compile_source(source,specs).plan.result_type.name,'Row')

    def test_catalog_loader_rejects_executable_fields_and_invalid_metadata(self):
        entry={'name':'read','kind':'observe','input_type':'Text','output_type':'Text','scope':'s','upper_bound':{}}
        for change in ({'python':'os.system'},{'predicates':2},{'retry_window':True},{'predicate':1}):
            document={'catalog_version':1,'schemas':{},'tools':[{**entry,**change}]}
            with self.assertRaises(InvalidData):
                read_catalog(json.dumps(document))


if __name__=='__main__':
    unittest.main()
