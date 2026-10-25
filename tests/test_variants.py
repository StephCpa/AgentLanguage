import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.bundle_types import describe, is_affine, variant_type
from al0.bundles import value_refs
from al0.concurrency import TaskRunner, TaskScope
from al0.data import Principal
from al0.errors import AuthorizationDenied, EvidenceMismatch, FlowDenied, InvalidData, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.frontend import compile_source
from al0.ir import Type, Tool, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.business_variants import build, catalog, demo, interface


MIXED=variant_type({'Funds':Type('Budget','Budget'),'Empty':Type('Data','Text')})
DATA=variant_type({'Ok':Type('Data','Int'),'Error':Type('Data','Text')})


def mixed(specs,tag='Funds',*,private=False,bundle=False):
    b=PlanBuilder('mixed-'+tag,specs)
    payload=b.split_budget('funds',{'u':2}) if tag=='Funds' else b.literal('empty','Text','empty')
    result=b.variant('value',MIXED,tag,payload)
    if private:result=b.restrict('private',result,Label({'owner'},{'secret'}))
    if bundle:result=b.pack('bundle',{'choice':result})
    return b.finish(result)


def cases(specs):
    b=PlanBuilder('funds-case',specs)
    funds=b.budget_parameter('result')
    b.join_budget('closed',funds)
    success=b.finish(b.literal('done','Text','released'))
    b=PlanBuilder('empty-case',specs)
    return {'Funds':success,'Empty':b.finish(b.parameter('result','Text'))}


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.specs=catalog();self.who=Principal('tests','developer');self.calls=[]
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
        self.start(plan);result=self.execute(plan)
        self.broker.wait_for_workers(timeout=10)
        count=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(plan),result)
        self.broker.wait_for_workers(timeout=10)
        self.assertEqual(len(self.store.events(self.run,self.who)),count)
        return result

    def test_three_business_alternatives_source_sdk_and_independent_endpoint(self):
        report=demo(self.temp.name)
        self.assertEqual(report['provider_calls'],[('Accepted',1),('Deferred',1)])
        for tag in ('Accepted','Deferred','Denied'):
            self.assertTrue(report[tag]['identical_ir'])
            self.assertEqual(report[tag]['replay_added_events'],0)
            self.assertEqual(report[tag]['budget']['u']['confirmed'],0 if tag=='Denied' else 1)

    def data_cases(self,*,pure=True,variant_result=False):
        choices={}
        for tag in ('Ok','Error'):
            b=PlanBuilder(tag,self.specs,pure=pure)
            value=b.parameter('result','Int' if tag=='Ok' else 'Text')
            if variant_result:value=b.variant('value',DATA,tag,value)
            elif tag=='Error':value=b.literal('zero','Int',0)
            choices[tag]=b.finish(value)
        return choices

    def test_pure_variant_is_copyable_and_call_preserves_metadata(self):
        fn=PlanBuilder('identity',self.specs,pure=True)
        identity=fn.finish(fn.variant_parameter('input',DATA))
        b=PlanBuilder('pure-data',self.specs,pure=True)
        value=b.variant('value',DATA,'Ok',b.literal('n','Int',7))
        value=b.call('returned',identity,inputs={'input':value})
        a=b.match_variant('a',value,self.data_cases())
        z=b.match_variant('z',value,self.data_cases())
        result=b.compute('sum','add',a,z)
        self.assertEqual(self.replay(b.finish(result)).data,14)
        self.assertFalse(any(e['kind']=='BranchChosen' for e in self.store.events(self.run,self.who)))

    def test_pure_variant_loop_state_keeps_its_interface(self):
        body=PlanBuilder('step',self.specs,pure=True)
        body.parameter('index','Int')
        state=body.variant_parameter('state',DATA)
        step=body.finish(body.match_variant('value',state,self.data_cases(variant_result=True)))
        b=PlanBuilder('loop',self.specs)
        initial=b.variant('initial',DATA,'Ok',b.literal('n','Int',4))
        result=b.repeat('loop',b.literal('count','Int',3),3,initial,step)
        self.assertEqual(self.replay(b.finish(result)).data['tag'],'Ok')

    def test_empty_resource_variant_loop_state_uses_the_loop_protocol(self):
        b=PlanBuilder('body',self.specs)
        b.parameter('index','Int');state=b.variant_parameter('state',MIXED)
        body=b.finish(state)
        p=PlanBuilder('loop',self.specs)
        value=p.variant('initial',MIXED,'Empty',p.literal('empty','Text','empty'))
        loop=p.repeat('loop',p.literal('count','Int',1),1,value,body)
        self.assertEqual(self.replay(p.finish(loop)).data['tag'],'Empty')

    def test_empty_resource_case_requires_explicit_handling(self):
        doc=json.loads(mixed(self.specs,'Empty').encoded)
        doc['nodes'].append({'id':'ordinary','op':'literal','type':'Text','value':'done'});doc['return']='ordinary'
        with self.assertRaises(TypeCheckError):check_plan(doc,self.specs)

    def test_resource_variant_cannot_be_matched_twice(self):
        b=PlanBuilder('twice',self.specs)
        value=b.variant('value',MIXED,'Empty',b.literal('empty','Text','empty'))
        b.match_variant('a',value,cases(self.specs))
        result=b.match_variant('z',value,cases(self.specs))
        with self.assertRaisesRegex(TypeCheckError,'AL1001'):b.finish(result)

    def test_matching_payload_cannot_alias_a_capture_of_its_container(self):
        b=PlanBuilder('alias',self.specs)
        value=b.variant('value',MIXED,'Funds',b.split_budget('funds',{'u':2}))
        result=b.match_variant('result',value,cases(self.specs),inputs={'same':value})
        with self.assertRaisesRegex(TypeCheckError,'same Variant'):b.finish(result)

    def test_constructor_consumes_resource_payload(self):
        b=PlanBuilder('stale',self.specs)
        funds=b.split_budget('funds',{})
        value=b.variant('value',MIXED,'Funds',funds)
        b.join_budget('again',funds)
        with self.assertRaisesRegex(TypeCheckError,'AL1001'):b.finish(value)

    def test_unknown_tag_rejected(self):
        doc=json.loads(mixed(self.specs,'Empty').encoded);doc['nodes'][-1]['tag']='Unknown'
        with self.assertRaisesRegex(TypeCheckError,'tag'):check_plan(doc,self.specs)

    def test_wrong_payload_type_rejected(self):
        doc=json.loads(mixed(self.specs,'Empty').encoded);doc['nodes'][-1]['tag']='Funds'
        with self.assertRaisesRegex(TypeCheckError,'payload'):check_plan(doc,self.specs)

    def test_missing_case_rejected(self):
        b=PlanBuilder('missing',self.specs);value=b.variant('value',MIXED,'Empty',b.literal('s','Text','empty'))
        result=b.match_variant('result',value,{'Empty':cases(self.specs)['Empty']})
        with self.assertRaisesRegex(TypeCheckError,'alternatives'):b.finish(result)

    def test_case_must_receive_payload(self):
        b=PlanBuilder('skip',self.specs);skip=b.finish(b.literal('skip','Text','skip'))
        b=PlanBuilder('missing',self.specs);value=b.variant('value',MIXED,'Empty',b.literal('s','Text','empty'))
        choices=cases(self.specs);choices['Empty']=skip
        result=b.match_variant('result',value,choices)
        with self.assertRaisesRegex(TypeCheckError,'explicitly receive'):b.finish(result)

    def test_naked_permit_is_not_a_closed_alternative(self):
        bad=variant_type({'Permit':Type('Permit','send',binding='request')})
        with self.assertRaisesRegex(TypeCheckError,'pack Permit'):
            PlanBuilder('bad',self.specs).variant_parameter('bad',bad)

    def test_duplicate_tags_are_rejected_by_sdk(self):
        bad=Type('Variant',members=(('Same',Type('Data','Int')),('Same',Type('Data','Text'))))
        with self.assertRaisesRegex(TypeCheckError,'distinct'):
            PlanBuilder('bad',self.specs).variant_parameter('bad',bad)

    def test_candidate_cannot_become_checked_by_injection(self):
        bad=variant_type({'Checked':Type('Checked','Text',frozenset({'invented'}))})
        with self.assertRaises(TypeCheckError):PlanBuilder('bad',self.specs).variant_parameter('bad',bad)

    def test_checked_variant_transports_and_revalidates_real_receipts(self):
        self.specs.register(Tool.define('verify','check','Text','Bool','verify',{'u':1},predicate='valid'))
        self.broker=Broker(self.store,self.specs,{'send':lambda t:Response('sent',{'u':1}),'verify':lambda t:Response(True,{'u':1})})
        typ=variant_type({'Checked':Type('Checked','Text',frozenset({'valid'}))})
        identity=PlanBuilder('identity',self.specs);identity=identity.finish(identity.variant_parameter('value',typ))
        b=PlanBuilder('checked',self.specs)
        checked=b.check('checked',b.tool('verify'),b.literal('text','Text','document'))
        value=b.variant('value',typ,'Checked',checked)
        scope=b.scope('scope',{});task=b.spawn('task',scope,identity,{},inputs={'value':value})
        result=b.join('result',task);b.close_scope('closed',scope);plan=b.finish(result)
        self.start(plan);self.grants['verify']=self.store.grant(self.run,'verify','verify',expires=self.store.clock()+1000)
        result=self.execute(plan)
        self.assertEqual(len(result.data['payload']['receipts']),1)
        corrupted=result.record();corrupted['data']['payload']['receipts']=[]
        with TaskScope(self.broker,self.run,self.who,'host',{}) as scope:
            with self.assertRaises(EvidenceMismatch):scope.spawn('bad',identity,{},self.grants,inputs={'value':Value.from_record(corrupted)})

    def test_candidate_payload_cannot_fill_checked_alternative(self):
        self.specs.register(Tool.define('verify','check','Text','Bool','verify',{'u':1},predicate='valid'))
        typ=variant_type({'Checked':Type('Checked','Text',frozenset({'valid'}))})
        b=PlanBuilder('bad',self.specs)
        candidate=b.parameter('input','Text',kind='Candidate')
        value=b.variant('value',typ,'Checked',candidate)
        with self.assertRaisesRegex(TypeCheckError,'payload'):b.finish(value)

    def test_variant_input_template_generation_must_match_move(self):
        result=self.replay(mixed(self.specs))
        ref=next(iter(value_refs(result).values()))
        scope=self.store.open_scope(self.run,self.who,'host',{})
        with self.store.resource_access(self.run,self.who,[ref]),self.assertRaisesRegex(StateConflict,'template'):
            self.store.spawn_task(scope,self.who,'child','host',{},caps=self.grants,
                inputs={'value':result.record()},moves={'funds':replace(ref,generation=ref.generation+1)})
        self.assertEqual(self.store.task_snapshot(scope,self.who)['children'],[])

    def test_variant_path_segment_does_not_collide_with_a_bundle_field(self):
        b=PlanBuilder('paths',self.specs)
        first=b.split_budget('first',{'u':1});second=b.split_budget('second',{'u':1})
        choice=b.variant('choice',MIXED,'Funds',first)
        packet=b.pack('packet',{'choice':choice,'{"variant":"Funds"}':second})
        result=self.replay(b.finish(packet))
        self.assertEqual(len(value_refs(result)),2)
        self.assertTrue(any(isinstance(segment,dict) for key in value_refs(result) for segment in json.loads(key)))

    def test_data_variant_root_input_and_plain_json_rejection(self):
        b=PlanBuilder('identity',self.specs);plan=b.finish(b.variant_parameter('input',DATA));self.start(plan)
        value=Value({'tag':'Ok','payload':Value(5).record()},variant=DATA)
        self.assertEqual(self.execute(plan,inputs={'input':value}),value)
        with self.assertRaises(InvalidData):self.execute(plan,inputs={'input':Value(value.data)})

    def test_controlled_empty_variant_cannot_enter_through_raw_root_inputs(self):
        b=PlanBuilder('identity',self.specs);plan=b.finish(b.variant_parameter('input',MIXED));self.start(plan)
        value=Value({'tag':'Empty','payload':Value('empty').record()},variant=MIXED)
        with self.assertRaisesRegex(InvalidData,'controlled Variant'):self.execute(plan,inputs={'input':value})

    def test_cap_variant_is_copyable_but_not_a_raw_data_parameter(self):
        typ=variant_type({'Cap':Type('Cap','send'),'Text':Type('Data','Text')})
        self.assertFalse(is_affine(typ))
        b=PlanBuilder('cap-variant',self.specs);plan=b.finish(b.variant_parameter('input',typ));self.start(plan)
        value=Value({'tag':'Cap','payload':Value(self.grants['send']).record()},variant=typ)
        with self.assertRaisesRegex(InvalidData,'controlled Variant'):self.execute(plan,inputs={'input':value})

    def test_cap_variant_is_not_pure_even_when_active_case_is_data(self):
        typ=variant_type({'Cap':Type('Cap','send'),'Text':Type('Data','Text')})
        b=PlanBuilder('pure',self.specs,pure=True)
        value=b.variant('value',typ,'Text',b.literal('s','Text','safe'))
        with self.assertRaisesRegex(TypeCheckError,'pure Variant'):b.finish(value)

    def test_cap_inside_variant_cannot_cross_a_narrow_task_table(self):
        typ=variant_type({'Cap':Type('Cap','send')})
        child=PlanBuilder('identity',self.specs);child=child.finish(child.variant_parameter('input',typ))
        b=PlanBuilder('narrow',self.specs)
        cap=b.capability('cap',b.tool('send'));value=b.variant('value',typ,'Cap',cap)
        scope=b.scope('scope',{});task=b.spawn_caps('task',scope,child,{},caps={},inputs={'input':value})
        result=b.join('result',task);b.close_scope('closed',scope);plan=b.finish(result);self.start(plan)
        with self.assertRaises((StateConflict,AuthorizationDenied)):self.execute(plan)

    def test_variant_cannot_be_used_as_ordinary_json(self):
        b=PlanBuilder('json',self.specs);plan=b.finish(b.parameter('input','Json'));self.start(plan)
        value=Value({'tag':'Ok','payload':Value(5).record()},variant=DATA)
        with self.assertRaises(InvalidData):self.execute(plan,inputs={'input':value})

    def test_private_variant_restricts_matching_and_budget(self):
        b=PlanBuilder('private',self.specs)
        value=b.invoke('value',mixed(self.specs,private=True))
        result=self.replay(b.finish(b.match_variant('result',value,cases(self.specs))))
        with self.assertRaises(FlowDenied):self.broker.deliver(result,'public')

    def test_root_nested_variant_propagates_private_payload_to_control(self):
        outer=variant_type({'Nested':DATA})
        private=Label({'owner'},{'source:private'})
        inner=Value({'tag':'Ok','payload':Value(5,label=private).record()},variant=DATA)
        value=Value({'tag':'Nested','payload':inner.record()},variant=outer)
        branch=PlanBuilder('ignore',self.specs)
        branch.variant_parameter('result',DATA)
        ignored=branch.finish(branch.literal('constant','Text','constant'))
        b=PlanBuilder('root',self.specs)
        param=b.variant_parameter('input',outer)
        plan=b.finish(b.match_variant('result',param,{'Nested':ignored}))
        self.start(plan)
        result=self.execute(plan,inputs={'input':value})
        self.assertEqual(result.label,private)
        with self.assertRaises(FlowDenied):self.broker.deliver(result,'public')

    def test_nested_variant_in_bundle_crosses_two_tasks(self):
        child=mixed(self.specs,bundle=True)
        relay=PlanBuilder('relay',self.specs);relay=relay.finish(relay.bundle_parameter('packet',child))
        b=PlanBuilder('nested',self.specs);scope=b.scope('scope',{'u':2})
        task=b.spawn('task',scope,child,{'u':2});packet=b.join('packet',task)
        onward=b.spawn('onward',scope,relay,{},inputs={'packet':packet});returned=b.join('returned',onward)
        b.close_scope('closed',scope);opened=b.unpack('opened',returned)
        value=b.take('value',opened,'choice')
        self.assertEqual(self.replay(b.finish(b.match_variant('result',value,cases(self.specs)))).data,'released')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_empty_variant_scope_exit_requires_explicit_join(self):
        self.start()
        with self.assertRaisesRegex(StateConflict,'explicit join'):
            with TaskScope(self.broker,self.run,self.who,'scope',{}) as scope:
                scope.spawn('task',mixed(self.specs,'Empty'),{},self.grants)

    def test_bundle_containing_empty_variant_also_requires_explicit_join(self):
        self.start()
        with self.assertRaisesRegex(StateConflict,'explicit join'):
            with TaskScope(self.broker,self.run,self.who,'scope',{}) as scope:
                scope.spawn('task',mixed(self.specs,'Empty',bundle=True),{},self.grants)

    def test_race_empty_winner_preserves_type_and_retires_funded_loser(self):
        self.start()
        with TaskScope(self.broker,self.run,self.who,'scope',{'u':2},speculative=True) as scope:
            empty,a=scope.spawn('empty',mixed(self.specs,'Empty'),{},self.grants)
            funds,z=scope.spawn('funds',mixed(self.specs,private=True),{'u':2},self.grants)
            a.result();z.result()
            with self.store.resource_access(self.run,self.who):
                self.store.select_race(scope.scope,self.who,'race',[empty,funds],empty)
            result=scope.race('race',[empty,funds])
            self.assertEqual(result.variant,MIXED)
            self.assertFalse(value_refs(result))
            with self.assertRaises(FlowDenied):self.broker.deliver(result,'public')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_empty_variant_task_outcome_still_requires_completed_parameter(self):
        b=PlanBuilder('skip',self.specs);skip=b.finish(b.literal('skip','Text','skip'))
        b=PlanBuilder('outcome',self.specs);scope=b.scope('scope',{})
        task=b.spawn('task',scope,mixed(self.specs,'Empty'),{});outcome=b.join_outcome('outcome',task)
        b.close_scope('closed',scope)
        result=b.match_task('result',outcome,completed=skip,failed=skip,waiting=skip,cancelled=skip)
        with self.assertRaisesRegex(TypeCheckError,'explicitly receive'):b.finish(result)

    def test_branch_snapshot_keeps_first_tag_and_payload(self):
        self.start()
        first=Value({'tag':'Ok','payload':Value(3).record()},variant=DATA)
        later=Value({'tag':'Error','payload':Value('changed').record()},variant=DATA)
        saved=self.store.choose_variant_branch(self.run,self.who,'choose',first.record(),{})
        self.assertEqual(self.store.choose_variant_branch(self.run,self.who,'choose',later.record(),{}),saved)
        self.assertEqual(saved['case'],'Ok')

    def test_nested_type_depth_is_bounded(self):
        typ=Type('Data','Text')
        for _ in range(18):typ=variant_type({'Next':typ})
        with self.assertRaises(TypeCheckError):PlanBuilder('deep',self.specs).variant_parameter('input',typ)

    def test_source_requires_complete_interface_annotation(self):
        from al0.source import SourceError
        text='workflow main() uses {} { let x: Text="x"; let v=variant("Ok",x); return v; }'
        with self.assertRaises(SourceError):compile_source(text,self.specs)

    def crash(self,point,occurrence=1,*,funded=True):
        plan=build(self.specs,funded=funded);self.start(plan)
        config=Path(self.temp.name)/'crash.json';log=Path(self.temp.name)/'calls.jsonl'
        config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants,
                                     'funded':funded,'log':str(log)}),encoding='utf-8')
        args=[sys.executable,'-m','tests.variant_worker',str(config)]
        crashed=subprocess.run([*args,point,str(occurrence)],capture_output=True,text=True,timeout=30)
        self.assertEqual(crashed.returncode,77,crashed.stdout+crashed.stderr)
        recovered=subprocess.run([*args,'none','1'],capture_output=True,text=True,timeout=30)
        self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)['data'],'Done')
        self.store=Store(self.path)
        self.broker=Broker(self.store,self.specs,{'send':lambda t:Response('unexpected',{'u':1})})
        count=len(self.store.events(self.run,self.who));self.execute(plan)
        self.assertEqual(len(self.store.events(self.run,self.who)),count)
        self.assertEqual(len(log.read_text().splitlines()),1)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)

    def test_process_exit_during_publish(self):self.crash('after_task_resources_publish')
    def test_process_exit_after_publish(self):self.crash('after_task_finish_commit')
    def test_process_exit_during_receive(self):self.crash('after_task_resources_receive')
    def test_process_exit_after_receive(self):self.crash('after_task_outcome_commit')
    def test_process_exit_before_match_commit(self):self.crash('before_branch_commit',2)
    def test_process_exit_after_match_commit(self):self.crash('after_branch_commit',2)
    def test_deferred_exit_before_match_commit(self):self.crash('before_branch_commit',2,funded=False)
    def test_deferred_exit_after_match_commit(self):self.crash('after_branch_commit',2,funded=False)


if __name__=='__main__':unittest.main()
