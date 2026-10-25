from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from al0.capabilities import MAX_CAP_DEPTH
from al0.data import Intent, Principal, Slot
from al0.errors import AuthorizationDenied, InvalidData, ReplayMismatch, StateConflict, TypeCheckError
from al0.execute import Broker, Interpreter, Response, Value
from al0.flow import Label, plan_label
from al0.frontend import compile_source
from al0.ir import Tool, check_plan
from al0.sdk import PlanBuilder
from al0.store import Store
from examples.capability_calls import build, catalog, deliver, demo, handled


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'runtime.sqlite'
        self.now=100
        self.store=Store(self.path,clock=lambda:self.now)
        self.who=Principal('tenant','user')
        self.specs=catalog()
        self.calls=[]
        def send(ticket):
            self.calls.append((ticket.intent.value['tool'],ticket.intent.value['args']))
            return Response({'sent':True},{'u':1})
        self.send=send
        self.broker=Broker(self.store,self.specs,{name:send for name in self.specs.tools})
        self.start(build(self.specs))
        self.intent=Intent('send','1','outbox',{'channel':'reports','text':'hello'})

    def start(self,plan):
        self.run=self.store.create_run(self.who,plan.fingerprint,{'u':10})
        self.grants={name:self.store.grant(self.run,name,spec.scope,expires=1000,max_attempts=4)
                     for name,spec in self.specs.tools.items()}

    def execute(self,plan):
        return Interpreter(self.broker).run(plan,self.run,self.who,self.grants)

    def derive(self,key='child',parent=None,**options):
        return self.store.attenuate_capability(self.run,self.who,key,parent or self.grants['send'],**options)['grant']

    def authorize(self,grant,intent=None):
        return self.store.authorize(self.run,self.who,intent or self.intent,grant)

    def root(self):
        b=PlanBuilder('parent',self.specs)
        base=b.capability('base',b.tool('send'))
        narrow=b.attenuate('narrow',base,constraints={'channel':'reports'},max_attempts=2)
        message=b.literal('message','Message',self.intent.value['args'])
        return b,base,narrow,message

    def test_demo_source_sdk_and_replay(self):
        result=demo(self.temp.name)
        self.assertEqual(result['result'],'Done')
        self.assertEqual(result['replay'],'Done')
        self.assertEqual(len(result['calls']),1)
        self.assertEqual(result['derived_max_attempts'],1)
        self.assertEqual((result['capability_bindings'],result['derivations']),(1,1))
        self.assertEqual(result['replay_added_events'],0)

    def test_limits_can_only_narrow(self):
        for options in ({'expires':1001},{'expires':100},{'max_attempts':5}):
            with self.assertRaises(AuthorizationDenied):
                self.derive(**options)
        child=self.derive(expires=500,max_attempts=2)
        info=self.store.capability_info(self.run,self.who,child)
        self.assertEqual((info['tool'],info['scope'],info['expires'],info['max_attempts']),('send','outbox',500,2))

    def test_omitted_constraints_do_not_remove_ancestor_constraints(self):
        child=self.derive(constraints={'channel':'reports'})
        grandchild=self.derive('grandchild',child,constraints={})
        self.authorize(grandchild)
        other=Intent('send','1','outbox',{'channel':'admin','text':'hello'})
        with self.assertRaises(AuthorizationDenied):
            self.authorize(grandchild,other)

    def test_conflicting_constraint_cannot_replace_ancestor(self):
        child=self.derive(constraints={'channel':'reports'})
        with self.assertRaises(AuthorizationDenied):
            self.derive('grandchild',child,constraints={'channel':'admin'})

    def test_constraint_equality_distinguishes_boolean_and_integer(self):
        self.store.register_contract('json-action','1',{'u':1})
        grant=self.store.grant(self.run,'json-action','json',expires=1000)
        child=self.derive('json',grant,constraints={'flag':True})
        good=Intent('json-action','1','json',{'flag':True})
        bad=Intent('json-action','1','json',{'flag':1})
        self.authorize(child,good)
        with self.assertRaises(AuthorizationDenied):
            self.authorize(child,bad)

    def test_parent_revocation_invalidates_all_descendants(self):
        child=self.derive()
        grandchild=self.derive('grandchild',child)
        self.store.revoke(self.grants['send'])
        for cap in (child,grandchild):
            with self.assertRaises(AuthorizationDenied):
                self.authorize(cap)
        # Historical derivation references remain replayable, without authority.
        self.assertEqual(self.derive(),child)

    def test_revoking_child_does_not_revoke_parent_or_sibling(self):
        child=self.derive()
        sibling=self.derive('sibling')
        self.store.revoke(child)
        with self.assertRaises(AuthorizationDenied):
            self.authorize(child)
        self.authorize(self.grants['send'])
        self.authorize(sibling)

    def test_parent_epoch_change_cannot_reactivate_old_delegation(self):
        child=self.derive()
        self.store.revoke(self.grants['send'])
        # Isolate epoch fencing even if a future host API re-enables the row.
        with self.store._tx() as db:
            db.execute('UPDATE grants SET active=1 WHERE id=?',(self.grants['send'],))
        self.authorize(self.grants['send'])
        with self.assertRaises(AuthorizationDenied):
            self.authorize(child)

    def test_expiry_and_revocation_are_checked_again_at_dispatch(self):
        child=self.derive(expires=200)
        permit=self.authorize(child)
        op=self.store.admit(self.run,self.who,Slot('op'),self.intent,permit)
        attempt=self.store.snapshot(op,self.who)['attempts'][0]['id']
        self.now=200
        with self.assertRaises(AuthorizationDenied):
            self.store.start_attempt(op,attempt,self.who)
        self.assertEqual(self.store.snapshot(op,self.who)['attempts'][0]['phase'],'Queued')
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)

    def test_parent_revocation_after_admission_blocks_dispatch(self):
        child=self.derive()
        permit=self.authorize(child)
        op=self.store.admit(self.run,self.who,Slot('op'),self.intent,permit)
        attempt=self.store.snapshot(op,self.who)['attempts'][0]['id']
        self.store.revoke(self.grants['send'])
        with self.assertRaises(AuthorizationDenied):
            self.store.start_attempt(op,attempt,self.who)

    def test_attempt_limit_is_enforced_on_retries(self):
        child=self.derive(max_attempts=2)
        op=self.store.admit(self.run,self.who,Slot('op'),self.intent,self.authorize(child))
        first=self.store.snapshot(op,self.who)['attempts'][0]['id']
        self.store.start_attempt(op,first,self.who)
        second=self.store.retry(op,self.who)
        self.store.start_attempt(op,second,self.who)
        with self.assertRaisesRegex(AuthorizationDenied,'attempt limit'):
            self.store.retry(op,self.who)
        self.assertEqual(len(self.store.snapshot(op,self.who)['attempts']),2)

    def test_revocation_does_not_discard_receipts_for_exposed_work(self):
        child=self.derive()
        op=self.store.admit(self.run,self.who,Slot('op'),self.intent,self.authorize(child))
        attempt=self.store.snapshot(op,self.who)['attempts'][0]['id']
        self.store.start_attempt(op,attempt,self.who)
        self.store.revoke(self.grants['send'])
        self.store.business_result(op,self.who,provider='send',evidence_id='late-result',outcome='Applied',details={'output':True})
        self.store.settle(attempt,self.who,provider='send',evidence_id='late-bill',actual={'u':1})
        self.assertEqual(self.store.budget(self.run,self.who)['u']['confirmed'],1)
        self.assertEqual(self.store.snapshot(op,self.who)['business'],'Applied')

    def test_derivation_and_binding_reject_changed_replay_requests(self):
        self.derive(max_attempts=2)
        with self.assertRaises(ReplayMismatch):
            self.derive(max_attempts=1)
        self.store.bind_capability(self.run,self.who,'binding','send',self.grants['send'])
        other=self.store.grant(self.run,'send','outbox',expires=1000)
        with self.assertRaises(ReplayMismatch):
            self.store.bind_capability(self.run,self.who,'binding','send',other)

    def test_derivation_preserves_principal_and_run_boundaries(self):
        with self.assertRaises(AuthorizationDenied):
            self.store.attenuate_capability(self.run,Principal('tenant','other'),'bad',self.grants['send'])
        other=self.store.create_run(self.who,'other',{'u':1})
        with self.assertRaises(AuthorizationDenied):
            self.store.attenuate_capability(other,self.who,'bad',self.grants['send'])

    def test_wrong_host_tool_or_scope_binding_is_rejected(self):
        with self.assertRaises(AuthorizationDenied):
            self.store.bind_capability(self.run,self.who,'bad','send',self.grants['admin'])
        grant=self.store.grant(self.run,'send','other-scope',expires=1000)
        with self.assertRaises(AuthorizationDenied):
            self.store.bind_capability(self.run,self.who,'bad','send',grant,scope='outbox')

    def test_parent_depth_is_bounded(self):
        parent=self.grants['send']
        for index in range(MAX_CAP_DEPTH-1):
            parent=self.derive('level'+str(index),parent)
        self.authorize(parent)
        with self.assertRaisesRegex(AuthorizationDenied,'ancestry'):
            self.derive('too-deep',parent)

    def test_parallel_derivation_creates_one_child(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            children=list(pool.map(lambda _:self.derive(),range(8)))
        self.assertEqual(len(set(children)),1)
        self.assertEqual(sum(e['kind']=='CapabilityAttenuated' for e in self.store.events(self.run,self.who)),1)

    def test_stale_task_cannot_derive_new_capability(self):
        scope=self.store.open_scope(self.run,self.who,'scope',{'u':2})
        task=self.store.spawn_task(scope,self.who,'task','child',{'u':2})
        old=self.store.claim_task(task,self.who)
        current=self.store.claim_task(task,self.who,recover=True)
        with self.assertRaises(StateConflict):
            self.derive(task_lease=old)
        self.derive(task_lease=current)

    def test_missing_ambient_tool_cannot_fall_back_to_host_grants(self):
        child=PlanBuilder('escape',self.specs)
        message=child.parameter('message','Message')
        intent=child.prepare('intent',child.tool('admin'),message)
        permit=child.authorize('permit',intent)
        workflow=child.finish(handled(child,child.admit('operation',intent,permit)))
        b,_,narrow,message=self.root()
        plan=b.finish(b.invoke_caps('result',workflow,caps={'send':narrow},inputs={'message':message}))
        self.start(plan)
        self.assertIn('admin',self.grants)
        with self.assertRaises(AuthorizationDenied):
            self.execute(plan)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],0)

    def test_broader_cap_input_cannot_cross_narrower_boundary(self):
        b,base,narrow,message=self.root()
        plan=b.finish(b.invoke_caps('result',deliver(self.specs),caps={'send':narrow},inputs={'permission':base,'message':message}))
        self.start(plan)
        with self.assertRaisesRegex(AuthorizationDenied,'boundary'):
            self.execute(plan)
        self.assertEqual(self.calls,[])

    def test_broader_permit_input_cannot_cross_narrower_boundary(self):
        child=PlanBuilder('submit',self.specs)
        intent=child.intent_parameter('intent',child.tool('send'))
        permit=child.permit_parameter('permit',intent)
        workflow=child.finish(child.admit('operation',intent,permit))
        b,base,narrow,message=self.root()
        intent=b.prepare('intent',b.tool('send'),message)
        permit=b.authorize_with('permit',intent,base)
        op=b.invoke_caps('operation',workflow,caps={'send':narrow},inputs={'intent':intent,'permit':permit})
        plan=b.finish(handled(b,op))
        self.start(plan)
        with self.assertRaisesRegex(AuthorizationDenied,'boundary'):
            self.execute(plan)
        self.assertEqual(self.calls,[])

    def test_broader_operation_input_cannot_cross_narrower_boundary(self):
        child=PlanBuilder('drive',self.specs)
        workflow=child.finish(handled(child,child.operation_parameter('operation',child.tool('send'))))
        b,base,narrow,message=self.root()
        intent=b.prepare('intent',b.tool('send'),message)
        permit=b.authorize_with('permit',intent,base)
        op=b.admit('operation',intent,permit)
        plan=b.finish(b.invoke_caps('result',workflow,caps={'send':narrow},inputs={'operation':op}))
        self.start(plan)
        with self.assertRaisesRegex(AuthorizationDenied,'boundary'):
            self.execute(plan)
        self.assertEqual(self.calls,[])
        # The caller already admitted this operation; boundary failure does not
        # pretend its queued reservation never existed.
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],1)

    def test_descendant_cap_input_is_allowed(self):
        b,_,narrow,message=self.root()
        deeper=b.attenuate('deeper',narrow,constraints={'text':'hello'})
        plan=b.finish(b.invoke_caps('result',deliver(self.specs),caps={'send':narrow},inputs={'permission':deeper,'message':message}))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(len(self.calls),1)

    def test_constraints_are_enforced_in_interpreted_authorization(self):
        raw=json.loads(build(self.specs).encoded)
        raw['nodes'][2]['value']['channel']='admin'
        plan=check_plan(raw,self.specs)
        self.start(plan)
        with self.assertRaises(AuthorizationDenied):
            self.execute(plan)
        self.assertEqual(self.calls,[])

    def test_empty_cap_map_does_not_inherit_any_grants(self):
        child=PlanBuilder('get',self.specs)
        workflow=child.finish(child.capability('cap',child.tool('send')))
        b=PlanBuilder('parent',self.specs)
        plan=b.finish(b.invoke_caps('result',workflow,caps={}))
        self.start(plan)
        with self.assertRaisesRegex(AuthorizationDenied,'absent'):
            self.execute(plan)

    def test_plain_nested_invoke_inherits_restricted_map(self):
        inner=PlanBuilder('get',self.specs)
        workflow=inner.finish(inner.capability('cap',inner.tool('admin')))
        middle=PlanBuilder('middle',self.specs)
        workflow=middle.finish(middle.invoke('result',workflow))
        b,_,narrow,_=self.root()
        plan=b.finish(b.invoke_caps('result',workflow,caps={'send':narrow}))
        self.start(plan)
        with self.assertRaises(AuthorizationDenied):
            self.execute(plan)

    def test_spawned_task_inherits_invocation_capability_boundary(self):
        child=PlanBuilder('escape',self.specs)
        message=child.literal('message','Message',self.intent.value['args'])
        intent=child.prepare('intent',child.tool('admin'),message)
        permit=child.authorize('permit',intent)
        workflow=child.finish(handled(child,child.admit('op',intent,permit)))
        middle=PlanBuilder('middle',self.specs)
        scope=middle.scope('scope',{'u':2})
        task=middle.spawn('task',scope,workflow,{'u':2})
        result=middle.join('result',task)
        middle.close_scope('closed',scope)
        workflow=middle.finish(result)
        b,_,narrow,_=self.root()
        plan=b.finish(b.invoke_caps('result',workflow,caps={'send':narrow}))
        self.start(plan)
        try:
            with self.assertRaises(AuthorizationDenied):
                self.execute(plan)
        finally:
            self.broker.wait_for_workers(timeout=10)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.budget(self.run,self.who)['u']['reserved'],0)

    def test_non_action_calls_use_attenuated_ambient_capability(self):
        self.specs.register(Tool.define('read','observe','Message','Json','records',{'u':1}))
        self.broker=Broker(self.store,self.specs,{name:self.send for name in self.specs.tools})
        child=PlanBuilder('reader',self.specs)
        message=child.parameter('message','Message')
        workflow=child.finish(child.observe('result',child.tool('read'),message))
        for channel in ('reports','admin'):
            with self.subTest(channel=channel):
                b=PlanBuilder('parent',self.specs)
                cap=b.capability('cap',b.tool('read'))
                narrow=b.attenuate('narrow',cap,constraints={'channel':'reports'})
                message=b.literal('message','Message',{'channel':channel,'text':'hello'})
                plan=b.finish(b.invoke_caps('result',workflow,caps={'read':narrow},inputs={'message':message}))
                self.start(plan)
                if channel=='reports':
                    self.assertEqual(self.execute(plan).data,{'sent':True})
                    self.assertEqual(self.execute(plan).data,{'sent':True})
                else:
                    with self.assertRaises(AuthorizationDenied):
                        self.execute(plan)
        self.assertEqual(self.calls,[('read',{'channel':'reports','text':'hello'})])

    def test_capability_can_return_through_branch_and_invoke_then_attenuate(self):
        identity=PlanBuilder('identity',self.specs)
        identity=identity.finish(identity.cap_parameter('cap',identity.tool('send')))
        choose=PlanBuilder('choose',self.specs)
        cap=choose.cap_parameter('cap',choose.tool('send'))
        yes=choose.literal('yes','Bool',True)
        branch=choose.if_else('selected',yes,then=identity,otherwise=identity,inputs={'cap':cap})
        selected=choose.attenuate('selected-cap',branch,max_attempts=2)
        workflow=choose.finish(selected)
        b,_,narrow,message=self.root()
        returned=b.invoke_caps('returned',workflow,caps={'send':narrow},inputs={'cap':narrow})
        deeper=b.attenuate('deeper',returned,constraints={'text':'hello'},max_attempts=1)
        plan=b.finish(b.invoke_caps('result',deliver(self.specs),caps={'send':deeper},inputs={'permission':deeper,'message':message}))
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(len(self.calls),1)

    def test_parent_revocation_blocks_admission_and_retry(self):
        child=self.derive()
        first=self.authorize(child)
        queued=self.authorize(child)
        operation=self.store.admit(self.run,self.who,Slot('first'),self.intent,first)
        attempt=self.store.snapshot(operation,self.who)['attempts'][0]['id']
        self.store.start_attempt(operation,attempt,self.who)
        self.store.revoke(self.grants['send'])
        rejected=self.store.try_admit(self.run,self.who,Slot('second'),self.intent,queued)
        self.assertEqual(rejected['kind'],'Rejected')
        self.assertIsNone(rejected['permit'])
        self.assertEqual(rejected['reason']['code'],AuthorizationDenied.code)
        with self.assertRaises(AuthorizationDenied):
            self.store.retry(operation,self.who)
        self.assertEqual(len(self.store.snapshot(operation,self.who)['attempts']),1)

    def test_capability_labels_survive_binding_derivation_and_replay(self):
        secret=Label({'owner','tool:send'})
        bound=self.store.bind_capability(self.run,self.who,'binding','send',self.grants['send'],control=secret)
        self.assertEqual(self.store.bind_capability(self.run,self.who,'binding','send',self.grants['send']),bound)
        derived=self.store.attenuate_capability(self.run,self.who,'secret',bound['grant'],control=secret)
        replay=self.store.attenuate_capability(self.run,self.who,'secret',bound['grant'])
        self.assertEqual(derived,replay)
        rebound=self.store.bind_capability(self.run,self.who,'derived','send',derived['grant'])
        self.assertEqual(Label.from_data(rebound['label']),secret)

    def test_boundary_cannot_change_on_invocation_replay(self):
        child=self.derive()
        self.store.begin_invocation(self.run,self.who,'call',{},caps={'send':child})
        with self.assertRaises(ReplayMismatch):
            self.store.begin_invocation(self.run,self.who,'call',{},caps={'send':self.grants['send']})

    def test_capability_parameter_cannot_be_forged_at_root(self):
        b=PlanBuilder('identity',self.specs)
        plan=b.finish(b.cap_parameter('cap',b.tool('send')))
        self.start(plan)
        with self.assertRaisesRegex(InvalidData,'checked branch captures'):
            Interpreter(self.broker).run(plan,self.run,self.who,self.grants,inputs={'cap':Value(self.grants['send'])})

    def test_wrong_tool_capabilities_are_rejected_statically(self):
        raw=json.loads(build(self.specs).encoded)
        raw['nodes'][-1]['caps']={'admin':'reports'}
        with self.assertRaisesRegex(TypeCheckError,'another tool'):
            check_plan(raw,self.specs)
        child=PlanBuilder('wrong',self.specs)
        cap=child.cap_parameter('cap',child.tool('admin'))
        intent=child.prepare('intent',child.tool('send'),child.literal('message','Message',self.intent.value['args']))
        permit=child.authorize_with('permit',intent,cap)
        with self.assertRaisesRegex(TypeCheckError,'another tool'):
            child.finish(child.admit('op',intent,permit))

    def test_constraints_are_checked_against_catalog_schema(self):
        raw=json.loads(build(self.specs).encoded)
        raw['nodes'][1]['constraints']={'missing':'reports'}
        with self.assertRaisesRegex(TypeCheckError,'unknown input field'):
            check_plan(raw,self.specs)
        raw['nodes'][1]['constraints']={'channel':123}
        with self.assertRaises(InvalidData):
            check_plan(raw,self.specs)

    def test_pure_code_cannot_derive_capabilities(self):
        b=PlanBuilder('pure',self.specs,pure=True)
        with self.assertRaises(TypeCheckError):
            b.finish(b.capability('cap',b.tool('send')))

    def test_completed_workflow_replays_after_parent_revocation(self):
        plan=build(self.specs)
        self.assertEqual(self.execute(plan).data,'Done')
        self.store.revoke(self.grants['send'])
        before=len(self.store.events(self.run,self.who))
        self.assertEqual(self.execute(plan).data,'Done')
        self.assertEqual(len(self.store.events(self.run,self.who)),before)
        self.assertEqual(len(self.calls),1)

    def test_capability_workflow_runs_inside_task_and_replays(self):
        b=PlanBuilder('parent',self.specs)
        scope=b.scope('scope',{'u':2})
        task=b.spawn('task',scope,build(self.specs),{'u':2})
        result=b.join('result',task)
        b.close_scope('closed',scope)
        plan=b.finish(result)
        self.start(plan)
        try:
            self.assertEqual(self.execute(plan).data,'Done')
            self.assertEqual(self.execute(plan).data,'Done')
            self.assertEqual(len(self.calls),1)
        finally:
            self.broker.wait_for_workers(timeout=10)

    def test_source_caps_with_budget(self):
        source=Path('examples/capability_calls.al0').read_text(encoding='utf-8')
        source=source.replace('uses {"cap:send@outbox",','uses {"budget", "cap:send@outbox",')
        source=source.replace('    let base =','    let funds = split_budget({u:2});\n    let base =')
        source=source.replace('invoke_caps(deliver,','invoke_caps_with(deliver, funds,')
        position=source.rfind('    return result;')
        source=source[:position]+'    let closed = join_budget(funds);\n'+source[position:]
        plan=compile_source(source,self.specs,entry='capability-calls').plan
        self.start(plan)
        self.assertEqual(self.execute(plan).data,'Done')

    def test_v12_migration_preserves_base_grants(self):
        with self.store._tx() as db:
            db.execute('DROP TABLE capability_bindings')
            db.execute('DROP TABLE grant_derivations')
            db.execute('PRAGMA user_version=12')
        self.store=Store(self.path,clock=lambda:self.now)
        self.broker=Broker(self.store,self.specs,{name:self.send for name in self.specs.tools})
        self.assertEqual(self.execute(build(self.specs)).data,'Done')
        with self.store._tx() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)
            self.assertEqual(db.execute('SELECT count(*) FROM grants WHERE id IN (?,?)',tuple(self.grants.values())).fetchone()[0],2)

    def test_process_crashes_do_not_duplicate_grants_or_actions(self):
        points=('before_capability_bind_commit','after_capability_bind_commit','before_capability_derive_commit',
                'after_capability_derive_commit','before_invocation_commit','after_invocation_commit')
        for index,point in enumerate(points):
            with self.subTest(point=point):
                path=Path(self.temp.name)/('crash'+str(index)+'.sqlite')
                store=Store(path)
                plan=build(self.specs)
                run=store.create_run(self.who,plan.fingerprint,{'u':10})
                grants={name:store.grant(run,name,spec.scope,expires=store.clock()+1000,max_attempts=3)
                        for name,spec in self.specs.tools.items()}
                config=path.with_suffix('.json')
                log=path.with_suffix('.log')
                config.write_text(json.dumps({'database':str(path),'run':run,'grants':grants,'log':str(log)}),encoding='utf-8')
                crashed=subprocess.run([sys.executable,'-m','tests.capability_worker',str(config),point],capture_output=True,text=True,timeout=30)
                self.assertEqual(crashed.returncode,77,crashed.stderr)
                recovered=subprocess.run([sys.executable,'-m','tests.capability_worker',str(config),'none'],capture_output=True,text=True,timeout=30)
                self.assertEqual(recovered.returncode,0,recovered.stderr)
                self.assertEqual(json.loads(recovered.stdout)['data'],'Done')
                self.assertEqual(log.read_text(encoding='utf-8').splitlines(),['hello'])
                events=store.events(run,self.who)
                self.assertEqual(sum(e['kind']=='CapabilityBound' for e in events),1)
                self.assertEqual(sum(e['kind']=='CapabilityAttenuated' for e in events),1)
                self.assertEqual(store.budget(run,self.who)['u']['confirmed'],1)
