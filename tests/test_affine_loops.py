from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from al0.bundle_types import describe, variant_type
from al0.data import Principal
from al0.errors import EvaluationError, FlowDenied, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label
from al0.ir import Type, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.affine_loops import build, catalog, demo


BUDGET=Type('Budget','Budget')
MIXED=variant_type({'Funds':BUDGET,'Empty':Type('Data','Text')})


def identity(c,typ=BUDGET,*,nested=False):
    b=PlanBuilder('identity',c);b.parameter('index','Int')
    state=b.budget_parameter('state') if typ==BUDGET else b.variant_parameter('state',typ)
    if nested:state=b.repeat('inner',b.literal('two','Int',2),2,state,identity(c,typ))
    return b.finish(state)


class AffineLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'store.sqlite';self.store=Store(self.path)
        self.who=Principal('tests','developer');self.c=catalog();self.calls=[]
        def model(ticket):self.calls.append(ticket);return Response('candidate',{'u':1})
        self.broker=Broker(self.store,self.c,{'model':model})
        self.addCleanup(lambda:self.broker.wait_for_workers(timeout=10))

    def start(self,p=None):
        self.p=build(self.c) if p is None else p
        self.run=self.store.create_run(self.who,self.p.fingerprint,{'u':10})
        self.grants={name:self.store.grant(self.run,name,spec.scope,expires=self.store.clock()+1000)
                     for name,spec in self.c.tools.items()}

    def execute(self,**kwargs):return Interpreter(self.broker).run(self.p,self.run,self.who,self.grants,**kwargs)

    def query(self,sql,args=()):
        with closing(sqlite3.connect(self.path)) as db:return db.execute(sql,args).fetchall()

    def replay(self):
        value=self.execute();self.broker.wait_for_workers(timeout=10)
        before=len(self.store.events(self.run,self.who));self.assertEqual(self.execute(),value)
        self.broker.wait_for_workers(timeout=10)
        self.assertEqual(len(self.store.events(self.run,self.who)),before)
        return value

    def make_identity(self,count=3,*,nested=False,release=False):
        b=PlanBuilder('main',self.c);initial=b.split_budget('funds',{'u':4})
        result=b.repeat('loop',b.literal('count','Int',count),5,initial,identity(self.c,nested=nested))
        if release:result=b.join_budget('released',result)
        return b.finish(result)

    def test_source_sdk_task_roundtrip_costs_and_replay(self):
        result=demo(Path(self.temp.name)/'example')
        self.assertTrue(result['identical_ir']);self.assertEqual(result['calls'],[(0,1),(1,1),(2,1)])
        self.assertEqual(result['budget']['u']['confirmed'],3);self.assertEqual(result['replay_added_events'],0)
        self.assertEqual(result['receipt_steps'],[-1,0,1,2])

    def test_zero_iterations_still_transfer_initial_generation(self):
        self.start(self.make_identity(0));value=self.replay()
        self.assertEqual(value.resource.generation,1)
        self.assertEqual(self.query('SELECT step FROM loop_resource_receipts'),[(-1,)])

    def test_one_iteration_returns_a_new_generation(self):
        self.start(self.make_identity(1));value=self.replay()
        self.assertEqual(value.resource.generation,2)

    def test_multi_iteration_budget_can_be_consumed_after_loop(self):
        self.start(self.make_identity(3,release=True));self.assertTrue(self.replay().data)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_nested_loops_replay_without_refreshing_old_aliases(self):
        self.start(self.make_identity(3,nested=True,release=True));self.assertTrue(self.replay().data)
        self.assertEqual(len(self.query('SELECT * FROM loop_frames')),4)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_initial_cannot_be_used_after_repeat_even_when_zero(self):
        b=PlanBuilder('alias',self.c);state=b.split_budget('funds',{})
        loop=b.repeat('loop',b.literal('zero','Int',0),3,state,identity(self.c))
        b.join_budget('bad-alias',state)
        with self.assertRaises(TypeCheckError):b.finish(loop)

    def test_returned_resource_cannot_be_dropped(self):
        b=PlanBuilder('drop',self.c);b.repeat('loop',b.literal('one','Int',1),3,b.split_budget('funds',{}),identity(self.c))
        with self.assertRaises(TypeCheckError):b.finish(b.literal('done','Bool',True))

    def test_loop_resource_cannot_be_captured_repeatedly(self):
        body=PlanBuilder('body',self.c);body.parameter('index','Int');state=body.budget_parameter('state')
        extra=body.budget_parameter('extra');body.join_budget('released',extra);body=body.finish(state)
        b=PlanBuilder('capture',self.c);state=b.split_budget('funds',{});extra=b.split_budget('extra',{})
        result=b.repeat('loop',b.literal('one','Int',1),3,state,body,inputs={'extra':extra})
        with self.assertRaises(TypeCheckError):b.finish(result)

    def test_resource_state_requires_same_closed_result_type(self):
        b=PlanBuilder('wrong',self.c);state=b.variant('empty',MIXED,'Empty',b.literal('text','Text','empty'))
        result=b.repeat('loop',b.literal('one','Int',1),1,state,identity(self.c))
        with self.assertRaises(TypeCheckError):b.finish(result)

    def test_pure_loop_still_cannot_hold_resources(self):
        b=PlanBuilder('wrong-pure',self.c,pure=True);state=b.budget_parameter('funds')
        result=b.repeat('loop',b.literal('one','Int',1),1,state,identity(self.c))
        with self.assertRaises(TypeCheckError):b.finish(result)

    def test_actual_empty_variant_state_has_entry_and_iteration_receipts(self):
        b=PlanBuilder('empty',self.c);initial=b.variant('empty',MIXED,'Empty',b.literal('text','Text','empty'))
        result=b.repeat('loop',b.literal('two','Int',2),3,initial,identity(self.c,MIXED))
        self.start(b.finish(result));self.assertEqual(self.replay().data['tag'],'Empty')
        self.assertEqual(self.query('SELECT step FROM loop_resource_receipts ORDER BY step'),[(-1,),(0,),(1,)])

    def test_private_state_label_survives_skipped_iterations(self):
        b=PlanBuilder('private',self.c)
        packet=b.variant('packet',MIXED,'Funds',b.split_budget('funds',{}))
        funds=b.restrict('private',packet,Label({'owner'},{'secret'}))
        result=b.repeat('loop',b.literal('one','Int',1),1,funds,identity(self.c,MIXED))
        self.start(b.finish(result));self.assertEqual(self.replay().label,Label({'owner'},{'secret'}))

    def test_private_count_restricts_loop_result(self):
        b=PlanBuilder('private-count',self.c);funds=b.split_budget('funds',{})
        n=b.restrict('private',b.literal('one','Int',1),Label({'owner'}))
        self.start(b.finish(b.repeat('loop',n,1,funds,identity(self.c))))
        self.assertEqual(self.replay().label.readers,frozenset({'owner'}))

    def test_bad_runtime_count_moves_no_resources(self):
        b=PlanBuilder('count',self.c);state=b.split_budget('funds',{})
        result=b.repeat('loop',b.parameter('n','Int'),2,state,identity(self.c));self.start(b.finish(result))
        with self.assertRaises(EvaluationError):self.execute(inputs={'n':Value(3)})
        self.assertEqual(self.query('SELECT * FROM loop_frames'),[])
        self.assertEqual(self.query("SELECT generation FROM resource_custody WHERE kind='Budget'"),[(0,)])

    def test_each_iteration_can_replace_the_budget_identity(self):
        body=PlanBuilder('replace',self.c);body.parameter('index','Int');state=body.budget_parameter('state')
        body.join_budget('closed',state);body=body.finish(body.split_budget('fresh',{'u':2}))
        b=PlanBuilder('replace-loop',self.c)
        value=b.repeat('loop',b.literal('three','Int',3),3,b.split_budget('initial',{'u':2}),body)
        self.start(b.finish(b.join_budget('released',value)));self.assertTrue(self.replay().data)
        self.assertEqual(self.query("SELECT state,COUNT(*) FROM resource_custody WHERE kind='Budget' GROUP BY state"),[('Spent',4)])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def test_permit_intent_bundle_changes_to_operation_bundle(self):
        from examples.business_variants import catalog as action_catalog, interface, completed
        from examples.typed_task_resources import submit
        self.c=action_catalog()
        def send(ticket):self.calls.append(ticket);return Response('sent',{'u':1})
        self.broker=Broker(self.store,self.c,{'send':send})
        typ=interface();branches={}
        for tag in ('Accepted','Deferred','Denied'):
            b=PlanBuilder('loop-'+tag,self.c);value=b.bundle_parameter('result',dict(typ.members)[tag])
            if tag=='Deferred':
                opened=b.unpack('opened',value)
                request=b.take('request',opened,'request');permission=b.take('permission',opened,'permission');funds=b.take('funds',opened,'funds')
                operation=b.invoke('operation',submit(self.c),inputs={'request':request,'permission':permission},budget=funds)
                result=b.variant('admitted',typ,'Accepted',b.pack('packet',{'operation':operation,'funds':funds}))
            else:result=b.variant('forward',typ,tag,value)
            branches[tag]=b.finish(result)
        b=PlanBuilder('step',self.c);b.parameter('index','Int');state=b.variant_parameter('state',typ)
        body=b.finish(b.match_variant('next',state,branches))
        b=PlanBuilder('business-loop',self.c)
        request=b.prepare('request',b.tool('send'),b.literal('text','Text','hello'))
        permission=b.authorize('permission',request);funds=b.split_budget('funds',{'u':2})
        packet=b.pack('packet',{'request':request,'permission':permission,'funds':funds})
        initial=b.variant('initial',typ,'Deferred',packet)
        result=b.repeat('loop',b.literal('two','Int',2),2,initial,body)
        self.start(b.finish(b.invoke('handled',completed(self.c),inputs={'result':result})))
        self.assertEqual(self.replay().data,'Done');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)
        self.assertEqual(self.query("SELECT state FROM resource_custody WHERE kind='Permit'"),[('Spent',)])

    def test_scalar_operation_state_is_dispatched_once_after_loop(self):
        from examples.business_variants import catalog as action_catalog
        self.c=action_catalog()
        def send(ticket):self.calls.append(ticket);return Response('sent',{'u':1})
        self.broker=Broker(self.store,self.c,{'send':send})
        body=PlanBuilder('carry-operation',self.c);body.parameter('index','Int')
        body=body.finish(body.operation_parameter('state',body.tool('send')))
        b=PlanBuilder('operation-loop',self.c)
        request=b.prepare('request',b.tool('send'),b.literal('text','Text','hello'))
        initial=b.admit('operation',request,b.authorize('permission',request))
        result=b.repeat('loop',b.literal('two','Int',2),2,initial,body)
        snapshot=b.await_result('snapshot',result)
        result=b.match_business('result',snapshot,'Text',applied='Done',proven_no_effect='NoEffect',pending='Pending',unknown='Unknown',not_dispatched='Queued')
        self.start(b.finish(result));self.assertEqual(self.replay().data,'Done')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.query("SELECT generation FROM resource_custody WHERE kind='Operation'"),[(3,)])

    def test_bundle_state_preserves_data_and_complete_unpack_obligations(self):
        typ=Type('Bundle',members=(('funds',BUDGET),('text',Type('Data','Text'))))
        body=PlanBuilder('carry-bundle',self.c);body.parameter('index','Int');body=body.finish(body.bundle_parameter('state',typ))
        b=PlanBuilder('bundle-loop',self.c)
        initial=b.pack('initial',{'funds':b.split_budget('funds',{'u':2}),'text':b.literal('text','Text','retained')})
        result=b.repeat('loop',b.literal('two','Int',2),2,initial,body)
        opened=b.unpack('opened',result);funds=b.take('funds-returned',opened,'funds');text=b.take('text-returned',opened,'text')
        b.join_budget('released',funds)
        self.start(b.finish(text));self.assertEqual(self.replay().data,'retained')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],10)

    def cancellation(self,point,expected_index):
        child=build(self.c);b=PlanBuilder('cancel-parent',self.c)
        scope=b.scope('scope',{'u':5});task=b.spawn('task',scope,child,{'u':5})
        outcome=b.join_outcome('outcome',task)
        cases={}
        for kind in ('Completed','Failed','Waiting','Cancelled'):
            case=PlanBuilder(kind,self.c);cases[kind]=case.finish(case.literal('result','Text',kind))
        result=b.match_task('result',outcome,completed=cases['Completed'],failed=cases['Failed'],waiting=cases['Waiting'],cancelled=cases['Cancelled'])
        b.close_scope('closed',scope);self.start(b.finish(result));cancelled=[]
        def fault(name):
            if name==point and not cancelled:
                from concurrent.futures import ThreadPoolExecutor
                task=self.query('SELECT activation FROM loop_frames')[0][0]
                # The parent cancellation runs outside the child's resource context.
                with ThreadPoolExecutor(max_workers=1) as executor:
                    cancelled.append(executor.submit(self.store.take_task_outcome,task,self.who,cancel=True).result()['kind'])
        self.store.fault=fault
        self.assertEqual(self.execute().data,'Cancelled');self.broker.wait_for_workers(timeout=10)
        self.store.fault=lambda name:None
        self.assertEqual(cancelled,['Cancelled']);self.assertEqual(self.query('SELECT next_index FROM loop_frames'),[(expected_index,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM resource_custody WHERE owner_kind='Task' AND state='Live'"),[(0,)])
        self.assertEqual(len(self.calls),1);self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)

    def test_cancellation_before_loop_commit_fences_worker(self):self.cancellation('before_resource_loop_advance_transaction',0)
    def test_cancellation_after_loop_commit_retires_new_generation(self):self.cancellation('after_resource_loop_advance_commit',1)

    def host_state(self):
        self.start();funds=self.store.split_budget(self.run,self.who,'host',{'u':4})
        ref=self.store.manage_resource(self.run,self.who,'Budget',funds)
        return Value(funds,resource=ref)

    def host_begin(self,value,count=2):
        with self.store.resource_access(self.run,self.who,[value.resource]):
            return self.store.begin_resource_loop(self.run,self.who,'host-loop',count,3,value.record(),{},interface=describe(BUDGET))

    def host_advance(self,index,value):
        with self.store.resource_access(self.run,self.who,[value.resource]):
            return self.store.advance_resource_loop(self.run,self.who,'host-loop',index,value.record())

    def test_old_iteration_replay_returns_its_receipt_not_latest_generation(self):
        source=self.host_state();entry=self.host_begin(source);v1=Value.from_record(entry['current'])
        first=self.host_advance(0,v1);v2=Value.from_record(first['current'])
        last=self.host_advance(1,v2);v3=Value.from_record(last['current'])
        replay=self.host_advance(0,v1)
        self.assertEqual(replay,first);self.assertNotEqual(replay['current'],last['current'])
        with self.store.resource_access(self.run,self.who,[v2.resource]),self.assertRaises(StateConflict):
            self.store.split_budget(self.run,self.who,'stale',{},parent=v2.data)
        with self.store.resource_access(self.run,self.who,[v3.resource]):self.store.join_budget(self.run,self.who,v3.data)

    def test_replayed_iteration_rejects_changed_value(self):
        source=self.host_state();v1=Value.from_record(self.host_begin(source)['current']);self.host_advance(0,v1)
        with self.assertRaises(ReplayMismatch):self.host_advance(0,v1.restricted(Label({'owner'})))

    def test_entry_does_not_rebind_to_a_different_initial_resource(self):
        source=self.host_state();self.host_begin(source)
        other=self.store.split_budget(self.run,self.who,'different',{})
        ref=self.store.manage_resource(self.run,self.who,'Budget',other)
        with self.assertRaises(ReplayMismatch):self.host_begin(Value(other,resource=ref))

    def test_entry_freezes_data_while_verifying_original_resource_binding(self):
        source=self.host_state()
        typ=Type('Bundle',members=(('funds',BUDGET),('text',Type('Data','Text'))))
        packet=Value({'funds':source.record(),'text':Value('first').record()},bundle=typ)
        with self.store.resource_access(self.run,self.who,[source.resource]):
            first=self.store.begin_resource_loop(self.run,self.who,'bundle-loop',0,3,packet.record(),{},interface=describe(typ))
        changed=Value({'funds':source.record(),'text':Value('later').record()},bundle=typ)
        with self.store.resource_access(self.run,self.who,[source.resource]):
            replay=self.store.begin_resource_loop(self.run,self.who,'bundle-loop',3,3,changed.record(),{},interface=describe(typ))
        self.assertEqual(replay,first)
        self.assertEqual(Value.from_record(replay['current']).data['text']['data'],'first')

    def test_out_of_order_iteration_does_not_move_generation(self):
        source=self.host_state();v1=Value.from_record(self.host_begin(source)['current'])
        with self.assertRaises(StateConflict):self.host_advance(1,v1)
        self.assertEqual(self.query('SELECT next_index FROM loop_frames'),[(0,)])
        self.assertEqual(self.query("SELECT generation FROM resource_custody WHERE resource=?",(source.data,)),[(1,)])

    def test_data_loop_api_cannot_bypass_resource_protocol(self):
        source=self.host_state();v1=Value.from_record(self.host_begin(source)['current'])
        with self.assertRaises(ReplayMismatch):self.store.begin_loop(self.run,self.who,'host-loop',2,3,source.record(),{})
        with self.assertRaises(StateConflict):self.store.advance_loop(self.run,self.who,'host-loop',0,v1.record())

    def test_resource_loop_api_cannot_replace_data_loop(self):
        source=self.host_state();self.store.begin_loop(self.run,self.who,'host-loop',1,1,Value(1).record(),{})
        with self.assertRaises(ReplayMismatch):self.host_begin(source)
        with self.assertRaises(StateConflict):self.host_advance(0,source)

    def test_data_loop_cannot_start_with_resource_record(self):
        source=self.host_state()
        with self.assertRaises(StateConflict):self.store.begin_loop(self.run,self.who,'data-loop',1,1,source.record(),{})
        self.assertEqual(self.query('SELECT * FROM loop_frames'),[])

    def test_data_loop_cannot_introduce_resource_on_advance(self):
        source=self.host_state();self.store.begin_loop(self.run,self.who,'data-loop',1,1,Value(1).record(),{})
        with self.assertRaises(StateConflict):self.store.advance_loop(self.run,self.who,'data-loop',0,source.record())
        self.assertEqual(self.query('SELECT next_index FROM loop_frames'),[(0,)])

    def test_move_and_cursor_rollback_together(self):
        source=self.host_state();v1=Value.from_record(self.host_begin(source)['current'])
        def fault(point):
            if point=='after_loop_resource_moves':raise RuntimeError('transaction interrupted')
        self.store.fault=fault
        with self.assertRaises(RuntimeError):self.host_advance(0,v1)
        self.store.fault=lambda point:None
        self.assertEqual(self.query('SELECT next_index FROM loop_frames'),[(0,)])
        self.assertEqual(self.query("SELECT generation FROM resource_custody WHERE resource=?",(source.data,)),[(1,)])
        self.assertEqual(Value.from_record(self.host_advance(0,v1)['current']).resource.generation,2)

    def crash(self,point,occurrence=1):
        self.start();config=Path(self.temp.name)/'config.json';log=Path(self.temp.name)/'endpoint.jsonl'
        config.write_text(json.dumps({'database':str(self.path),'run':self.run,'grants':self.grants,'log':str(log)}),encoding='utf-8')
        args=[sys.executable,'-m','tests.affine_loop_worker',str(config)]
        stopped=subprocess.run([*args,point,str(occurrence)],capture_output=True,text=True,timeout=30)
        self.assertEqual(stopped.returncode,77,stopped.stdout+stopped.stderr)
        recovered=subprocess.run([*args,'none','1'],capture_output=True,text=True,timeout=30)
        self.assertEqual(recovered.returncode,0,recovered.stdout+recovered.stderr)
        self.assertTrue(json.loads(recovered.stdout)['data'])
        calls=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([c['index'] for c in calls],[0,1,2]);self.assertEqual(sum(c['cost'] for c in calls),3)
        self.assertEqual(len({c['attempt'] for c in calls}),3)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],3)
        self.assertEqual(self.store.budget(self.run,self.who)['u']['free'],7)
        self.assertTrue(self.replay().data)

    def test_process_exit_during_entry_move(self):self.crash('after_loop_resource_moves')
    def test_process_exit_before_entry_commit(self):self.crash('before_resource_loop_begin_commit')
    def test_process_exit_after_entry_commit(self):self.crash('after_resource_loop_begin_commit')
    def test_process_exit_during_iteration_move(self):self.crash('after_loop_resource_moves',2)
    def test_process_exit_before_iteration_commit(self):self.crash('before_resource_loop_advance_commit')
    def test_process_exit_after_iteration_commit(self):self.crash('after_resource_loop_advance_commit')
    def test_process_exit_after_second_iteration_commit(self):self.crash('after_resource_loop_advance_commit',2)
    def test_process_exit_before_child_publish(self):self.crash('before_task_finish_commit')
    def test_process_exit_after_child_publish(self):self.crash('after_task_finish_commit')
    def test_process_exit_before_child_receive(self):self.crash('before_task_join_commit')
    def test_process_exit_after_child_receive(self):self.crash('after_task_join_commit')

    def migrate(self,point):
        self.start()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('DROP TABLE loop_resource_receipts');db.execute('PRAGMA user_version=18')
        config=Path(self.temp.name)/'migration.json';config.write_text(json.dumps({'database':str(self.path),'migrate':True}),encoding='utf-8')
        stopped=subprocess.run([sys.executable,'-m','tests.affine_loop_worker',str(config),point,'1'],capture_output=True,text=True,timeout=30)
        self.assertEqual(stopped.returncode,77,stopped.stdout+stopped.stderr)
        self.assertEqual(self.query('PRAGMA user_version'),[(18 if point.startswith('before') else 21,)])
        Store(self.path);self.assertTrue(self.replay().data)

    def test_process_exit_before_v18_migration(self):self.migrate('before_attempt_funding_migration_commit')
    def test_process_exit_after_v18_migration(self):self.migrate('after_attempt_funding_migration_commit')
