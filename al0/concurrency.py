"""Trusted Python host for durable structured tasks running the shared IR.

The pool is local worker capacity, not a guarantee about remote concurrency.
Cancellation fences new broker work; Python callbacks already running may finish
later and deliver receipts. It does not forcibly kill arbitrary host code.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from .errors import ReplayMismatch, StateConflict
from .execute import Interpreter, Suspended, Value, capture_value
from .ir import CheckedPlan, MOVED_RESOURCES, check_plan
from .bundle_types import is_affine
from .flow import Label, PUBLIC, plan_label
from .limits import ExecutionBudget
from .bundles import value_refs, return_refs, accept_result, capability_resources


class TaskRunner:
    def __init__(self, broker):
        self.broker = broker

    def run(self, task, principal, plan, grants=None, *, recover=False, execution=None):
        checked = check_plan(plan.encoded if isinstance(plan, CheckedPlan) else plan, self.broker.catalog)
        store = self.broker.store
        state = store.task_snapshot(task, principal)
        if state['kind'] != 'Task':
            raise StateConflict('only child tasks can execute a checked plan')
        if state['program'] != checked.fingerprint:
            raise ReplayMismatch('task cannot resume with a different program')
        if state['speculative']:
            checked = check_plan(checked.encoded,self.broker.catalog,speculative=True)
        if state['state'] == 'Completed':
            saved = state['result']
            result=Value.from_record(saved).restricted(store.task_label(task,principal))
            if checked.result_type.kind in ('Cap','Bundle','Variant'):
                self._check_cap_result(checked,state['run'],principal,result,store.bind_task_capabilities(task,principal,grants))
            return result
        grants=store.bind_task_capabilities(task,principal,grants)
        lease = store.claim_task(task, principal, recover=recover)
        interpreter=Interpreter(self.broker)
        try:
            parameters=dict(checked.parameters)
            inputs={k:capture_value(v,parameters[k],store) for k,v in store.task_execution_inputs(task,principal).items()}
            result = interpreter.run(checked, state['run'], principal, grants, task_lease=lease,recover_tasks=recover,inputs=inputs,execution=execution)
            result=result.restricted(store.task_label(task,principal))
            self._check_cap_result(checked,state['run'],principal,result,grants)
            resources=return_refs(result)
            if not resources:
                store.finish_task(lease, principal, result.record())
            else:
                with store.resource_access(state['run'],principal,resources.values(),task_lease=lease):
                    store.finish_task(lease,principal,result.record(),resources=resources)
            return result
        except Suspended as exc:
            with store.resource_access(state['run'],principal,getattr(interpreter,'resources',{}).values(),task_lease=lease):
                store.wait_task(lease, principal, exc.operation)
            raise
        except BaseException as exc:
            try:
                store.finish_task(lease, principal, {'error_type': type(exc).__name__}, failed=True)
            except StateConflict:
                # Cancellation/recovery may have fenced this worker. Its late
                # local failure must not replace the newer durable decision.
                pass
            raise

    def _check_cap_result(self,plan,run,principal,result,grants):
        if plan.result_type.kind in ('Bundle','Variant'):
            from types import SimpleNamespace
            self.broker.prepare_inputs(SimpleNamespace(parameters=(('result',plan.result_type),)),run,principal,
                                       {'result':result},_branch_resources=True)
            self.broker.store.check_capability_boundary(run,principal,grants,
                capability_resources(result,plan.result_type,self.broker.store))
        if plan.result_type.kind=='Cap':
            spec=self.broker.catalog.tools[plan.result_type.name]
            info=self.broker.store.capability_info(run,principal,result.data)
            if (info['tool'],info['scope'])!=(spec.name,spec.scope) or result.receipts:
                raise StateConflict('task Cap result does not match its declared tool')
            self.broker.store.check_capability_boundary(run,principal,grants,[('Cap',result.data)])


class TaskScope:
    def __init__(self, broker, run, principal, key, amount, *, allocation=None, max_workers=4, parent_lease=None, speculative=False, recover_tasks=False, control=PUBLIC, execution=None):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError('max_workers must be a positive integer')
        self.broker, self.run, self.principal = broker, run, principal
        self.key, self.amount, self.allocation = key, amount, allocation
        self.max_workers = max_workers
        self.parent_lease, self.speculative = parent_lease, speculative
        self.recover_tasks = recover_tasks
        self.control = control
        self.execution=ExecutionBudget() if execution is None else execution
        self.futures = {}
        self.consumed = set()
        self.scope = None
        self.pool = None

    def __enter__(self):
        if self.pool is not None:
            raise StateConflict('task scope is already active')
        self.scope = self.broker.store.open_scope(self.run, self.principal, self.key, self.amount, allocation=self.allocation,
                                                parent_lease=self.parent_lease,speculative=self.speculative,control=self.control)
        try:
            self.pool = ThreadPoolExecutor(max_workers=self.max_workers)
        except BaseException:
            self.broker.store.close_scope(self.scope, self.principal, cancel=True, parent_lease=self.parent_lease)
            raise
        return self

    def spawn(self, key, plan, amount, grants, *, recover=None, inputs=None, control=PUBLIC):
        if self.pool is None:
            raise StateConflict('task scope is not active')
        policy = self.broker.store.task_snapshot(self.scope,self.principal)['speculative']
        checked = check_plan(plan.encoded if isinstance(plan, CheckedPlan) else plan, self.broker.catalog,speculative=policy)
        bindings=self.broker.prepare_inputs(checked,self.run,self.principal,inputs,_task_caps=True,_branch_resources=True)
        moves={}
        for parameter,typ in checked.parameters:
            if typ.kind in ('Bundle','Variant'):
                moved=value_refs(inputs[parameter],(parameter,))
                if set(moved)&set(moves):
                    raise StateConflict('resource input paths collide')
                moves.update(moved)
            elif typ.kind in MOVED_RESOURCES:
                value=inputs[parameter]
                if value.resource is None or value.resource.kind!=typ.kind:
                    raise StateConflict('task resource input requires an explicit managed reference')
                if parameter in moves:
                    raise StateConflict('resource input paths collide')
                moves[parameter]=value.resource
            elif typ.kind=='OpRef':
                raise StateConflict('borrowed operation references cannot cross task boundaries')
        self.broker.store.check_capability_boundary(self.run,self.principal,grants,
            self.broker.capability_resources(checked,inputs or {}))
        control=self.control.join(control,*(value.label for value in (inputs or {}).values()))
        with self.broker.store.resource_access(self.run,self.principal,moves.values(),task_lease=self.parent_lease):
            task = self.broker.store.spawn_task(self.scope, self.principal, key, checked.fingerprint, amount,
                inputs={k:v for k,v in bindings.items() if k not in moves},parent_lease=self.parent_lease,moves=moves,
                control=control,upper_bound=plan_label(checked,self.broker.catalog),caps=grants)
        if task not in self.futures:
            self.futures[task] = self.pool.submit(TaskRunner(self.broker).run, task, self.principal, checked, dict(grants), recover=self.recover_tasks if recover is None else recover,execution=self.execution)
            self.broker.track_worker(self.futures[task])
        return task, self.futures[task]

    def join(self, task):
        if task in self.consumed:
            raise StateConflict('task handle already consumed in this scope execution')
        try:
            result = self.futures[task].result()
        except Suspended as exc:
            self.handoff(task)
            raise StateConflict('child has no result; its unresolved operation was handed off') from exc
        with self.broker.store.resource_access(self.run,self.principal,task_lease=self.parent_lease):
            self.broker.store.join_task(task,self.principal,parent_lease=self.parent_lease)
        if return_refs(result):
            received=self.broker.store.task_resource_result(task,self.principal)
            result=accept_result(result,received)
        self.consumed.add(task)
        return result

    def handoff(self, task):
        if task in self.consumed:
            raise StateConflict('task handle already consumed in this scope execution')
        snapshot = self.broker.store.handoff_task(task,self.principal,parent_lease=self.parent_lease)
        self.consumed.add(task)
        label = self.broker.store.task_label(task,self.principal)
        if isinstance(snapshot['result'],dict):
            label = label.join(Label.from_data(snapshot['result'].get('label')))
        # The language receives a labelled control receipt, not an internal
        # snapshot containing serialized values, resource records and funding.
        return Value({key:snapshot[key] for key in ('state','disposition')}
                     | {'task':task}, label=label)

    def join_outcome(self, task):
        if task in self.consumed:
            raise StateConflict('task handle already consumed in this scope execution')
        store=self.broker.store
        outcome=store.task_outcome(task,self.principal)
        if outcome is None:
            try:
                self.futures[task].result()
            except Exception:
                # Only a durable terminal/Waiting record can become an outcome.
                # A rejected claim on a live worker remains a control error.
                pass
            with store.resource_access(self.run,self.principal,task_lease=self.parent_lease):
                outcome=store.take_task_outcome(task,self.principal,parent_lease=self.parent_lease)
        self.consumed.add(task)
        return outcome

    def cancel_outcome(self, task):
        if task in self.consumed:
            raise StateConflict('task handle already consumed in this scope execution')
        store=self.broker.store
        with store.resource_access(self.run,self.principal,task_lease=self.parent_lease):
            outcome=store.take_task_outcome(task,self.principal,parent_lease=self.parent_lease,cancel=True)
        self.consumed.add(task)
        return outcome

    def race(self, key, tasks):
        if len(tasks)<2 or len(set(tasks))!=len(tasks) or any(t not in self.futures or t in self.consumed for t in tasks):
            raise StateConflict('race requires distinct unconsumed child handles')
        store = self.broker.store
        winner = store.race_winner(self.scope,self.principal,key,tasks)
        if winner is None:
            candidates = {self.futures[t]:t for t in tasks}
            for future in as_completed(candidates):
                try:
                    future.result()
                except Exception:
                    continue
                with store.resource_access(self.run,self.principal,task_lease=self.parent_lease):
                    winner = store.select_race(self.scope,self.principal,key,tasks,candidates[future],parent_lease=self.parent_lease)
                break
        if winner is None:
            for task in tasks:
                self.handoff(task)
            raise StateConflict('race has no successful candidate; all responsibilities handed off')
        self.consumed.update(tasks)
        saved = store.task_snapshot(winner,self.principal)['result']
        label=PUBLIC.join(*(store.task_label(task,self.principal) for task in tasks))
        result=Value.from_record(saved)
        if return_refs(result):
            result=accept_result(result,store.task_resource_result(winner,self.principal))
        return result.restricted(label)

    def __exit__(self, exc_type, exc, traceback):
        if self.pool is None:
            return False
        store = self.broker.store
        clean = False
        try:
            if exc_type is not None:
                store.close_scope(self.scope, self.principal, cancel=True, parent_lease=self.parent_lease)
                return False
            pending = {future: task for task, future in self.futures.items() if task not in self.consumed}
            # Observe failures as they finish, even if an earlier-spawned
            # sibling is still blocked inside its trusted adapter callback.
            for future in as_completed(pending):
                task = pending[future]
                try:
                    result=future.result()
                except Suspended:
                    store.handoff_task(task, self.principal, parent_lease=self.parent_lease)
                else:
                    if return_refs(result) or result.bundle is not None or result.variant is not None and is_affine(result.variant):
                        raise StateConflict('resource-returning tasks require an explicit join or handoff')
                    store.join_task(task, self.principal, parent_lease=self.parent_lease)
            store.close_scope(self.scope, self.principal, parent_lease=self.parent_lease)
            clean = True
        except BaseException:
            store.close_scope(self.scope, self.principal, cancel=True, parent_lease=self.parent_lease)
            raise
        finally:
            self.pool.shutdown(wait=clean and all(f.done() for f in self.futures.values()), cancel_futures=True)
            self.pool = None
        return False
