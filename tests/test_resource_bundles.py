import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.bundle_types import describe, restore
from al0.bundles import bundle_fields, value_refs
from al0.concurrency import TaskRunner, TaskScope
from al0.data import Intent, Principal
from al0.errors import AuthorizationDenied, FlowDenied, InvalidData, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.frontend import compile_source
from al0.source import SourceError
from al0.ir import check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.resource_bundles import build, catalog, demo, producer


def budget_packet(specs,*,nested=False,private=False):
    b=PlanBuilder('budget-packet',specs)
    funds=b.split_budget('funds',{'u':2})
    note=b.literal('note','Text','secret' if private else 'hello')
    if private:
        note=b.restrict('private',note,Label({'owner'},{'source:private'}))
    packet=b.pack('packet',{'funds':funds,'note':note})
    if nested:
        packet=b.pack('outer',{'inner':packet})
    return b.finish(packet)


class ResourceBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.store=Store(self.path)
        self.who=Principal('tests','developer')
        self.specs=catalog()
        self.calls=[]
        def send(ticket):
            self.calls.append(ticket.intent.value['args'])
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
        result=self.execute(plan)
        count=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(plan),result)
        self.assertEqual(len(self.store.events(self.run,self.who)),count)
        return result

    def test_source_sdk_mixed_bundle_independent_endpoint(self):
        result=demo(self.temp.name)
        self.assertTrue(result['identical_ir'])
        self.assertEqual(result['provider_calls'],[('first',1),('second',1)])
        self.assertEqual((result['budget']['u']['confirmed'],result['replay_added_events']),(2,0))

    def test_nested_bundle_parameter_and_return(self):
        prototype=budget_packet(self.specs,nested=True)
        child=PlanBuilder('identity',self.specs)
        child=child.finish(child.bundle_parameter('packet',prototype))
        b=PlanBuilder('nested',self.specs)
        packet=b.invoke('created',prototype)
        scope=b.scope('scope',{})
        task=b.spawn('task',scope,child,{},inputs={'packet':packet})
        received=b.join('received',task)
        b.close_scope('closed',scope)
        outer=b.unpack('outer',received)
        inner=b.unpack('inner',b.take('packet',outer,'inner'))
        funds=b.take('funds',inner,'funds')
        note=b.take('note',inner,'note')
        b.join_budget('released',funds)
        self.assertEqual(self.replay(b.finish(note)).data,'hello')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_private_field_restricts_bundle_and_taken_budget(self):
        plan=budget_packet(self.specs,private=True)
        result=self.replay(plan)
        fields=bundle_fields(result,plan.result_type,self.store)
        self.assertEqual(fields['funds'].label,Label({'owner'},{'source:private'}))
        with self.assertRaises(FlowDenied):
            self.broker.deliver(fields['note'],'public')

    def test_ordinary_data_cannot_import_a_bundle(self):
        packet_plan=budget_packet(self.specs)
        packet=self.replay(packet_plan)
        b=PlanBuilder('data',self.specs)
        plan=b.finish(b.parameter('data','Json'))
        self.start(plan)
        with self.assertRaises(InvalidData):
            self.execute(plan,inputs={'data':packet})

    def test_root_entry_cannot_inject_resource_bundle(self):
        prototype=budget_packet(self.specs)
        packet=self.replay(prototype)
        b=PlanBuilder('input',self.specs)
        plan=b.finish(b.bundle_parameter('packet',prototype))
        self.start(plan)
        with self.assertRaisesRegex(InvalidData,'checked captures'):
            self.execute(plan,inputs={'packet':packet})

    def test_static_duplicate_alias(self):
        doc=json.loads(budget_packet(self.specs).encoded)
        doc['nodes'][-1]['fields']['alias']='funds'
        with self.assertRaisesRegex(TypeCheckError,'multiple names'):
            check_plan(doc,self.specs)

    def test_pack_cannot_leave_original_budget_usable(self):
        doc=json.loads(budget_packet(self.specs).encoded)
        doc['nodes'].append({'id':'stale','op':'join_budget','budget':'funds'})
        with self.assertRaisesRegex(TypeCheckError,'AL1001'):
            check_plan(doc,self.specs)

    def test_unclosed_permit_dependency_is_rejected(self):
        doc=json.loads(producer(self.specs).encoded)
        del doc['nodes'][-1]['fields']['request']
        with self.assertRaisesRegex(TypeCheckError,'matching Intent'):
            check_plan(doc,self.specs)

    def test_wrong_intent_dependency_is_rejected(self):
        doc=json.loads(producer(self.specs).encoded)
        doc['nodes'][-1]['fields']['request']='first'
        with self.assertRaisesRegex(TypeCheckError,'matching Intent'):
            check_plan(doc,self.specs)

    def test_unpacked_intent_identity_cannot_collide_with_an_ssa_name(self):
        b=PlanBuilder('identity-collision',self.specs)
        text=b.literal('text','Text','first')
        first=b.prepare('["opened","request"]',b.tool('send'),text)
        permit=b.authorize('permit',first)
        second=b.prepare('second',b.tool('send'),b.literal('other','Text','other'))
        funds=b.split_budget('funds',{})
        packet=b.pack('packet',{'request':second,'funds':funds})
        opened=b.unpack('opened',packet)
        returned=b.take('request',opened,'request')
        budget=b.take('budget',opened,'funds')
        bad=b.pack('bad',{'request':returned,'permission':permit,'funds':budget})
        with self.assertRaisesRegex(TypeCheckError,'matching Intent'):
            b.finish(bad)

    def test_take_requires_complete_single_unpack(self):
        b=PlanBuilder('incomplete',self.specs)
        packet=b.invoke('packet',budget_packet(self.specs))
        opened=b.unpack('opened',packet)
        funds=b.take('funds',opened,'funds')
        result=b.join_budget('closed',funds)
        with self.assertRaisesRegex(TypeCheckError,'completely unpacked'):
            b.finish(result)

    def test_take_cannot_repeat_field(self):
        b=PlanBuilder('duplicate',self.specs)
        packet=b.invoke('packet',budget_packet(self.specs))
        opened=b.unpack('opened',packet)
        b.take('a',opened,'funds')
        b.take('b',opened,'funds')
        with self.assertRaisesRegex(TypeCheckError,'already taken'):
            b.finish(b.literal('result','Bool',True))

    def test_json_field_operator_cannot_project_bundle(self):
        b=PlanBuilder('project',self.specs)
        packet=b.invoke('packet',budget_packet(self.specs))
        result=b.field('field',packet,'funds')
        with self.assertRaises(TypeCheckError):
            b.finish(result)

    def test_partial_sdk_type_query_is_not_an_executable_plan(self):
        doc=json.loads(budget_packet(self.specs).encoded)
        doc['nodes'].append({'id':'opened','op':'unpack','input':'packet'})
        doc['return']='opened'
        partial=check_plan(doc,self.specs,_partial=True)
        self.start(partial)
        with self.assertRaises(TypeCheckError):
            self.execute(partial)

    def test_dangling_descriptor_dependency_rejected(self):
        typ=describe(producer(self.specs).result_type)
        typ['members']['permission']['binding']='missing'
        doc={'ir_version':1,'name':'forged','uses':[],
             'nodes':[{'id':'packet','op':'parameter','kind':'Bundle','type':typ,'predicates':[]}],'return':'packet'}
        with self.assertRaisesRegex(TypeCheckError,'matching Intent'):
            check_plan(doc,self.specs)

    def test_runtime_alias_is_rejected(self):
        plan=budget_packet(self.specs)
        packet=self.replay(plan)
        record=packet.record()
        record['data']['alias']=copy.deepcopy(record['data']['funds'])
        record['bundle']['members']['alias']=copy.deepcopy(record['bundle']['members']['funds'])
        with self.assertRaisesRegex(StateConflict,'repeated resource'):
            value_refs(Value.from_record(record))

    def test_runtime_bundle_field_kind_is_checked(self):
        plan=budget_packet(self.specs)
        packet=self.replay(plan)
        record=packet.record()
        record['data']['funds']['resource']['kind']='Operation'
        forged=Value.from_record(record)
        from types import SimpleNamespace
        with self.assertRaises(InvalidData):
            self.broker.prepare_inputs(SimpleNamespace(parameters=(('packet',plan.result_type),)),self.run,self.who,
                                       {'packet':forged},_branch_resources=True)

    def test_bundle_task_outcome_cannot_be_silently_flattened(self):
        doc=json.loads(build(self.specs).encoded)
        next(n for n in doc['nodes'] if n['op']=='join')['op']='join_outcome'
        with self.assertRaises(TypeCheckError):
            check_plan(doc,self.specs)

    def test_closed_result_type_does_not_depend_on_local_node_names(self):
        original=producer(self.specs)
        doc=json.loads(original.encoded)
        for node in doc['nodes']:
            if node['id']=='operation':
                node['id']='different-operation'
            if node['op']=='pack':
                node['fields']['operation']='different-operation'
        changed=check_plan(doc,self.specs)
        self.assertEqual(original.result_type,changed.result_type)
        self.assertNotEqual(original.fingerprint,changed.fingerprint)

    def test_source_rejects_mismatched_permit_field_annotation(self):
        source=Path('examples/resource_bundles.al0').read_text(encoding='utf-8')
        source=source.replace('permission: Permit<request>','permission: Permit<note>')
        with self.assertRaises(SourceError):
            compile_source(source,self.specs,entry='resource-bundles')

    def test_bundle_observer_cannot_use_escrow_before_join(self):
        plan=budget_packet(self.specs)
        packet=self.replay(plan)
        b=PlanBuilder('identity',self.specs)
        child=b.finish(b.bundle_parameter('packet',plan))
        with TaskScope(self.broker,self.run,self.who,'host',{}) as scope:
            task,future=scope.spawn('task',child,{},self.grants,inputs={'packet':packet})
            saved=future.result()
            self.assertIsNotNone(saved.bundle)
            # Before join the observer's pre-publication refs cannot act.
            ref=next(iter(value_refs(saved).values()))
            with self.store.resource_access(self.run,self.who,[ref]):
                with self.assertRaises(StateConflict):
                    self.store.split_budget(self.run,self.who,'premature',{},parent=ref.resource)
            received=scope.join(task)
        with self.store.resource_access(self.run,self.who,value_refs(received).values()):
            self.store.join_budget(self.run,self.who,ref.resource)

    def test_input_template_cannot_refresh_a_stale_generation(self):
        plan=budget_packet(self.specs)
        packet=self.replay(plan)
        refs=value_refs(packet,('packet',))
        forged=packet.record()
        forged['data']['funds']['resource']['generation']+=1
        scope=self.store.open_scope(self.run,self.who,'host',{})
        with self.store.resource_access(self.run,self.who,refs.values()):
            with self.assertRaisesRegex(StateConflict,'template differs'):
                self.store.spawn_task(scope,self.who,'task','trusted',{},caps=self.grants,
                                      inputs={'packet':forged},moves=refs)

    def test_cap_inside_bundle_cannot_cross_narrow_task_boundary(self):
        b=PlanBuilder('cap-packet',self.specs)
        cap=b.capability('cap',b.tool('send'))
        funds=b.split_budget('funds',{})
        packet=b.pack('packet',{'cap':cap,'funds':funds})
        prototype=b.finish(packet)
        child=PlanBuilder('identity',self.specs)
        child=child.finish(child.bundle_parameter('packet',prototype))
        scope=b.scope('scope',{})
        narrow=b.attenuate('narrow',cap,max_attempts=1)
        task=b.spawn_caps('task',scope,child,{},caps={'send':narrow},inputs={'packet':packet})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        with self.assertRaises(AuthorizationDenied):
            self.execute(plan)

    def crash(self,point,occurrence=1):
        plan=build(self.specs)
        self.start(plan)
        config=Path(self.temp.name)/'config.json'
        log=Path(self.temp.name)/'calls.jsonl'
        config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants,'log':str(log)}),encoding='utf-8')
        args=[sys.executable,'-m','tests.resource_bundle_worker',str(config)]
        stopped=subprocess.run(args+[point,str(occurrence)],capture_output=True,text=True,timeout=30)
        self.assertEqual(stopped.returncode,77,stopped.stderr)
        resumed=subprocess.run(args+['none','1'],capture_output=True,text=True,timeout=30)
        self.assertEqual(resumed.returncode,0,resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)['data'],{'first':'Done','second':'Done','note':'ready'})
        events=len(self.store.events(self.run,self.who))
        self.execute(plan)
        self.assertEqual(len(self.store.events(self.run,self.who)),events)
        self.assertEqual([json.loads(line)[1] for line in log.read_text().splitlines()],['first','second'])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],2)

    def test_crash_during_bundle_input_move(self):
        self.crash('after_task_resources_move',2)

    def test_crash_after_bundle_input_move(self):
        self.crash('after_spawn_commit',2)

    def test_crash_during_bundle_publish(self):
        self.crash('after_task_resources_publish')

    def test_crash_after_bundle_publish(self):
        self.crash('after_task_finish_commit')

    def test_crash_during_bundle_receive(self):
        self.crash('after_task_resources_receive')

    def test_crash_after_bundle_receive(self):
        self.crash('after_task_join_commit')

    def test_crash_during_forwarded_bundle_receive(self):
        self.crash('after_task_resources_receive',2)

    def test_crash_after_forwarded_bundle_receive(self):
        self.crash('after_task_join_commit',2)


if __name__=='__main__':
    unittest.main()
