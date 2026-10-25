"""Shared interpreter and mediated adapters for the sequential IR slice."""
from dataclasses import dataclass
from contextlib import ExitStack, nullcontext
from concurrent.futures import wait
import json
from threading import Lock
from time import monotonic

from .data import Intent, Slot, canonical, digest, resource_vector
from .bindings import normalize_bindings
from .errors import AuthorizationDenied, ContractError, EvidenceMismatch, InvalidData, ReplayMismatch, FlowDenied, EvaluationError, StateConflict, InferenceRefused
from .ir import CheckedPlan, Type, RESOURCES, MOVED_RESOURCES, check_plan
from .custody import ResourceRef
from .bundle_types import describe as describe_type, restore as restore_type, is_affine, is_pure_type, controlled
from .bundles import bundle_fields, variant_payload, value_refs
from .resource_execution import task_resources
from .reconciliation import validate_result
from .flow import Label, PUBLIC, value_record
from .pure import bounded, compute, record
from .limits import ExecutionBudget
from .inference import result_type as inference_type
from .inference_progress import progress_type
from .inference_admission import call_type
from .inference_response import InferenceResponse, classify_inference, refused
from .store import DispatchBlocked


@dataclass(frozen=True)
class Response:
    output: object
    actual: dict | None
    outcome: str = 'Applied'


class Suspended(Exception):
    def __init__(self, operation, business):
        self.operation = operation
        self.business = business
        super().__init__('workflow is waiting for an external operation')


@dataclass(frozen=True)
class Value:
    data: object
    receipts: tuple = ()
    label: Label = PUBLIC
    resource: ResourceRef | None = None
    bundle: Type | None = None
    variant: Type | None = None

    def __post_init__(self):
        if not isinstance(self.label,Label):
            raise InvalidData('runtime value requires a Label')
        if self.resource is not None and (not isinstance(self.resource,ResourceRef)
                or self.data!=self.resource.resource or self.receipts):
            raise InvalidData('resource value requires a matching reference and no check receipts')
        if self.bundle is not None and (not isinstance(self.bundle,Type) or self.bundle.kind!='Bundle'
                or self.resource is not None or self.receipts or type(self.data) is not dict):
            raise InvalidData('bundle value requires a structural interface and field records')
        if self.variant is not None and (not isinstance(self.variant,Type) or self.variant.kind!='Variant'
                or self.bundle is not None or self.resource is not None or self.receipts or type(self.data) is not dict):
            raise InvalidData('Variant value requires an interface and a payload record')

    def record(self):
        result=value_record(self.data,self.receipts,self.label)
        if self.resource is not None:
            result['resource']=self.resource.record()
        if self.bundle is not None:
            result['bundle']=describe_type(self.bundle)
        if self.variant is not None:
            result['variant']=describe_type(self.variant)
        return result

    @classmethod
    def from_record(cls, record):
        return cls(record['data'],tuple(record['receipts']),Label.from_data(record.get('label')),
                   None if 'resource' not in record else ResourceRef(**record['resource']),
                   None if 'bundle' not in record else restore_type(record['bundle']),
                   None if 'variant' not in record else restore_type(record['variant']))

    def restricted(self, label):
        return Value(self.data,self.receipts,self.label.join(label),self.resource,self.bundle,self.variant)


def intent_context(intent, control):
    if intent.label.join(control)!=intent.label:
        raise FlowDenied('intent was prepared before the current sensitive control context; prepare it again')


def authorization_site(kind, node, task_lease):
    return f'{kind}:{node}' if task_lease is None else 'task:'+digest([task_lease.task, kind, node])


def activation(task_lease):
    return '0' if task_lease is None else task_lease.task


def capture_record(value, typ):
    # Descriptors remain data in a checkpoint. Only a statically declared
    # Intent parameter decodes one; ordinary Json never becomes authority.
    if typ.kind=='Intent':
        return Value(value.data.value,label=value.label).record()
    return value.record()


def capture_value(record, typ, store):
    value=Value.from_record(record)
    if typ.kind=='Intent':
        return Value(store._intent(canonical(value.data)),label=value.label)
    return value


class Broker:
    """Only trusted host code installs adapters and supplies authenticated grants.

    Adapter callbacks are currently in-process and part of the TCB. Plan data
    cannot import or name a callback. The broker never retries an exposed attempt.
    """
    def __init__(self, store, catalog, adapters):
        self.store, self.catalog = store, catalog
        self.adapters = dict(adapters)
        self._workers, self._worker_lock = set(), Lock()
        for name, spec in catalog.tools.items():
            if name not in adapters:
                raise ContractError('missing registered adapter')
            store.register_contract(name, spec.version, json.loads(spec.upper_bound_json),
                                    predicates=spec.predicates, retry_window=spec.retry_window)
            store.register_tool_effect(name,spec.version,spec.shared_write)
            policy=spec.flow_policy
            store.register_tool_flow(name,spec.version,recipients=policy['recipients'],
                                     output=spec.output_label,accepted_taints=policy['accepted_taints'])
        for spec in catalog.tools.values():
            if spec.kind == 'reconcile':
                target = catalog.tools[spec.reconciles]
                store.register_reconciler(spec.name, spec.version, target.name, target.version)

    def track_worker(self, future):
        with self._worker_lock:
            self._workers.add(future)
        def finished(done):
            with self._worker_lock:
                self._workers.discard(done)
        future.add_done_callback(finished)

    def prepare_inputs(self, plan, run, principal, inputs, *, _branch_resources=False, _task_caps=False):
        inputs={} if inputs is None else inputs
        if type(inputs) is not dict or set(inputs)!=set(dict(plan.parameters)):
            raise InvalidData('inputs must bind exactly the declared parameters')
        encoded={}
        for name,typ in plan.parameters:
            value=inputs[name]
            if not isinstance(value,Value):
                raise InvalidData('input must be a runtime data value')
            if typ.kind=='Variant':
                from types import SimpleNamespace
                if controlled(typ) and not (_branch_resources or _task_caps):
                    raise InvalidData('controlled Variant parameters require checked captures or task bindings')
                tag,member,payload=variant_payload(value,typ,self.store)
                bound=self.prepare_inputs(SimpleNamespace(parameters=(('payload',member),)),run,principal,{'payload':payload},
                                          _branch_resources=_branch_resources or _task_caps)
                label=value.label.join(Label.from_data(bound['payload'].get('label')))
                encoded[name]=Value({'tag':tag,'payload':bound['payload']},label=label,variant=typ).record()
                continue
            if value.variant is not None:
                raise InvalidData('Variant cannot be used as ordinary data or another resource kind')
            if typ.kind=='Bundle':
                from types import SimpleNamespace
                if not (_branch_resources or _task_caps):
                    raise InvalidData('bundle parameters require checked captures or task bindings')
                fields=bundle_fields(value,typ,self.store)
                self.prepare_inputs(SimpleNamespace(parameters=typ.members),run,principal,fields,_branch_resources=True)
                encoded[name]=value.record()
                continue
            if value.bundle is not None:
                raise InvalidData('bundle cannot be used as ordinary data or a scalar resource')
            if value.resource is not None and not (typ.kind in MOVED_RESOURCES and typ.kind==value.resource.kind
                                                   or typ.kind=='OpRef' and value.resource.kind=='Operation'):
                raise InvalidData('resource reference cannot be used as a data value or another resource kind')
            if typ.kind in RESOURCES:
                if not (_branch_resources or _task_caps and typ.kind in ('Cap','Intent')):
                    raise InvalidData('resource parameters can only receive checked branch captures or workflow invocation inputs')
                if value.receipts:
                    raise EvidenceMismatch('resource capture cannot carry data check receipts')
                if typ.kind=='Cap':
                    info=self.store.capability_info(run,principal,value.data)
                    spec=self.catalog.tools[typ.name]
                    if (info['tool'],info['scope'])!=(spec.name,spec.scope):
                        raise InvalidData('capability capture does not match its tool and scope')
                elif typ.kind=='Budget':
                    if type(value.data) is not str:
                        raise InvalidData('budget capture requires an allocation reference')
                    with self.store._tx() as db:
                        self.store._run(db,run,principal)
                        self.store._allocation(db,run,value.data)
                elif typ.kind=='Intent':
                    if not isinstance(value.data,Intent) or value.data.value['tool']!=typ.name:
                        raise InvalidData('intent capture does not match its action contract')
                elif typ.kind=='Permit':
                    intent=inputs[typ.binding].data
                    with self.store._tx() as db:
                        permit=db.execute('SELECT run,intent FROM permits WHERE id=?',(value.data,)).fetchone()
                        if permit is None or permit['run']!=run or not isinstance(intent,Intent) or permit['intent']!=intent.encoded:
                            raise EvidenceMismatch('permit capture is bound to another intent')
                elif typ.kind in ('Operation','OpRef'):
                    info=self.store.operation_info(value.data,principal)
                    if info['run']!=run or info['intent']['tool']!=typ.name:
                        raise InvalidData('operation capture does not match its run and contract')
                encoded[name]=capture_record(value,typ)
                continue
            self.catalog.validate(typ.name,value.data)
            if typ.kind!='Checked' and value.receipts:
                raise EvidenceMismatch('unchecked parameter cannot transport hidden check receipts')
            self.store.validate_input_evidence(run,principal,value.data,value.receipts,typ.predicates)
            encoded[name]=value.record()
        return normalize_bindings(encoded)

    def capability_resources(self,plan,inputs):
        from .bundles import capability_resources
        return [item for name,typ in plan.parameters for item in capability_resources(inputs[name],typ,self.store)]

    def deliver(self, value, recipient, *, accepted_taints=None):
        """Explicit host export boundary; arbitrary host code remains trusted."""
        value.label.check_sink([recipient],accepted_taints)
        canonical(value.data)
        return value.data

    def wait_for_workers(self, *, timeout=None):
        """Host shutdown barrier for actual local callbacks, not remote effects.

        A race may return while losing callbacks still append receipts. Keep
        the database alive until this barrier before deleting local storage.
        Nested task workers created while waiting are included as well.
        """
        deadline = None if timeout is None else monotonic()+timeout
        while True:
            with self._worker_lock:
                workers = tuple(self._workers)
            if not workers:
                return
            remaining = None if deadline is None else max(0,deadline-monotonic())
            _, pending = wait(workers,timeout=remaining)
            if pending:
                raise TimeoutError('local task workers are still running')
            # Done callbacks may still be awaiting this thread's lock. Removing
            # completed futures is safe because runner code has already exited.
            with self._worker_lock:
                self._workers.difference_update(workers)

    def admit_call(self, run, principal, node, tool, value, grant, *, allocation=None, task_lease=None):
        spec = self.catalog.tools[tool]
        self.catalog.validate(spec.input_type, value.data)
        intent = Intent(spec.name, spec.version, spec.scope, value.data, value.receipts,label=value.label)
        permit = self.store.authorize(run, principal, intent, grant, authorization_key=authorization_site('call', node, task_lease), task_lease=task_lease)
        self.store._present_resource_birth('Permit',permit,principal)
        operation=self.store.admit(run, principal, Slot(node, activation(task_lease)), intent, permit, allocation=allocation, task_lease=task_lease)
        self.store._present_resource_birth('Operation',operation,principal)
        return operation

    def drive(self, operation, principal, *, task_lease=None, control=PUBLIC, allow_blocked=False):
        state = self.store.snapshot(operation, principal)
        queued = [a for a in state['attempts'] if a['phase'] == 'Queued']
        if queued:
            if allow_blocked:
                ticket = self.store.try_start_attempt(operation, queued[0]['id'], principal, task_lease=task_lease, control=control)
                if isinstance(ticket, DispatchBlocked):
                    return self.store.snapshot(operation, principal)
            else:
                descriptor=self.store.operation_info(operation,principal)['intent']
                intent_context(self.store._intent(canonical(descriptor)),control)
                ticket = self.store.start_attempt(operation, queued[0]['id'], principal, task_lease=task_lease)
            spec = self.catalog.tools[ticket.intent.value['tool']]
            try:
                response = self.adapters[spec.name](ticket)
            except (TimeoutError, ConnectionError):
                return self.store.snapshot(operation, principal)
            if isinstance(response, InferenceResponse):
                if spec.kind != 'infer':
                    raise ContractError('inference response envelopes require a model tool')
                outcome, details = 'Applied', response.details()
            elif isinstance(response, Response):
                outcome, details = response.outcome, {'output': response.output}
            else:
                raise ContractError('adapter must return a Response')
            # Record the external fact before validating the application schema.
            # Invalid model output still consumed a real call and may cost money.
            self.store.fault('before_call_business_record')
            self.store.business_result(operation, principal, provider=spec.name,
                                       evidence_id=f'business:{ticket.attempt}', outcome=outcome, details=details)
            self.store.fault('after_call_business_record')
            if response.actual is not None:
                self.store.settle(ticket.attempt, principal, provider=spec.name,
                                  evidence_id=f'billing:{ticket.attempt}', actual=response.actual)
                self.store.fault('after_call_billing_record')
        return self.store.snapshot(operation, principal)

    def output(self, operation, principal, output_type, *, task_lease=None):
        state = self.drive(operation, principal, task_lease=task_lease)
        spec = self.catalog.tools[self.store.operation_info(operation, principal)['intent']['tool']]
        if spec.kind == 'infer' and refused(state):
            raise InferenceRefused('model provider explicitly refused to produce a candidate')
        if state['business'] in ('NotDispatched', 'Unknown', 'Pending'):
            raise Suspended(operation, state['business'])
        if state['business'] != 'Applied':
            raise ContractError('call completed without producing its requested result')
        value = state['business_evidence']['details']['output']
        self.catalog.validate(output_type, value)
        return value

    def inference_output(self, operation, principal, spec, *, task_lease=None, control=PUBLIC):
        contract = {'protocol': 1, 'catalog': self.catalog.fingerprint(),
                    'tool': spec.name, 'version': spec.version, 'output': spec.output_type}
        previous = self.store.inference_result(operation, principal, contract)
        if previous is not None:
            return Value.from_record(previous).restricted(control)
        state = self.drive(operation, principal, task_lease=task_lease, control=control)
        tag, payload = classify_inference(self.catalog, spec, state)
        if tag == 'Waiting':
            raise Suspended(operation, state['business'])
        label = control.join(self.store.operation_label(operation, principal))
        result = Value({'tag': tag, 'payload': Value(payload, label=label).record()},
                       label=label, variant=inference_type(spec.output_type))
        return Value.from_record(self.store.bind_inference_result(
            operation, principal, contract, result.record(), task_lease=task_lease))

    def try_infer(self, run, principal, node, source, spec, grants, *, allocation=None, task_lease=None):
        if spec.kind != 'infer':
            raise ContractError('try_infer requires a model tool')
        self.catalog.validate(spec.input_type, source.data)
        intent=Intent(spec.name,spec.version,spec.scope,source.data,source.receipts,label=source.label)
        contract={'protocol':1,'catalog':self.catalog.fingerprint(),'tool':spec.name,'version':spec.version,'output':spec.output_type}
        decision=self.store.try_inference(run,principal,Slot(node,activation(task_lease)),intent,grants.get(spec.name),contract,
                                          allocation=allocation,task_lease=task_lease,control=source.label)
        label=source.label.join(Label.from_data(decision['label']))
        if decision['kind']=='Rejected':
            payload=Value(decision['reason'],label=label)
        else:
            payload=self.inference_progress(run,principal,node,
                self.store.resource_birth('Operation',decision['operation'],principal),spec,grants,
                task_lease=task_lease,control=label)
            label=label.join(payload.label)
        return Value({'tag':decision['kind'],'payload':payload.record()},label=label,
                     variant=call_type(spec.name,spec.output_type))

    def inference_progress(self, run, principal, node, source, spec, grants, *, task_lease=None, control=PUBLIC):
        if not isinstance(source, ResourceRef) or source.kind != 'Operation' or spec.kind != 'infer':
            raise ContractError('inference progress requires a model Operation')
        contract = {'protocol': 1, 'catalog': self.catalog.fingerprint(),
                    'tool': spec.name, 'version': spec.version, 'output': spec.output_type}
        previous = self.store.inference_observation(run, principal, node, source, contract, task_lease=task_lease)
        if previous is not None:
            return Value.from_record(previous).restricted(control)
        info = self.store.operation_info(source.resource, principal)
        if info['run'] != run or (info['intent']['tool'], info['intent']['version']) != (spec.name, spec.version):
            raise ContractError('model Operation belongs to another run or tool')
        self.store.check_capability_boundary(run, principal, grants, [('Operation', source.resource)])

        def classify(state, label):
            tag, data = classify_inference(self.catalog, spec, state)
            payload = Value(source.resource, label=label, resource=source) if tag == 'Waiting' else Value(data, label=label)
            return Value({'tag': tag, 'payload': payload.record()}, label=label,
                         variant=progress_type(spec.name, spec.output_type)).record()

        with self.store.resource_access(run, principal, [source], task_lease=task_lease):
            # Validate control before even attempting to dispatch an existing queue entry.
            with self.store._tx() as db:
                self.store._control_worker(db, run, principal, task_lease)
                if not self.store._custody_guard(db, 'Operation', source.resource, task_lease):
                    raise StateConflict('model observation requires managed control')
            self.drive(source.resource, principal, task_lease=task_lease, control=control, allow_blocked=True)
            result = self.store.observe_inference(run, principal, node, source, contract, classify,
                                                  task_lease=task_lease, control=control)
        return Value.from_record(result)

    def reconcile(self, run, principal, node, operation, tool, grant, *, allocation=None, task_lease=None, control=PUBLIC):
        """One explicit budgeted query. Replaying node reuses the query result."""
        spec = self.catalog.tools.get(tool)
        info = self.store.operation_info(operation, principal)
        descriptor = info['intent']
        if info['run'] != run:
            raise AuthorizationDenied('reconciliation target must belong to the current run')
        if (spec is None or spec.kind != 'reconcile' or spec.reconciles != descriptor['tool']
                or spec.scope != descriptor['scope']
                or self.catalog.tools[spec.reconciles].version != descriptor['version']):
            raise ContractError('reconciliation route does not cover the target contract')
        intent = Intent(descriptor['tool'], descriptor['version'], descriptor['scope'],
                        descriptor['args'], descriptor['receipts'],label=Label.from_data(descriptor.get('flow')))
        # The request excludes transient state, so the same node binds the same
        # query even when target attempts or billing advance during recovery.
        request = Value({'operation': operation, 'intent_digest': intent.digest},label=control.join(self.store.operation_label(operation,principal)))
        query = self.admit_call(run, principal, node, tool, request, grant, allocation=allocation, task_lease=task_lease)
        output = self.output(query, principal, 'Json', task_lease=task_lease)
        target = self.store.snapshot(operation, principal)
        facts = validate_result(output, operation, intent.digest, target['attempts'])
        self.store.fault('before_reconciliation_apply')
        business = facts['business']
        if business is not None:
            self.store.business_result(operation, principal, provider=spec.reconciles,
                                       evidence_id=business['evidence_id'], outcome=business['outcome'],
                                       details=business['details'])
        self.store.fault('after_reconciliation_business')
        for bill in facts['bills']:
            self.store.settle(bill['attempt'], principal, provider=spec.reconciles,
                              evidence_id=bill['evidence_id'], actual=bill['actual'])
            self.store.fault('after_reconciliation_bill')
        return self.store.snapshot(operation, principal)


class Interpreter:
    def __init__(self, broker):
        self.broker = broker

    def run(self, plan, run, principal, grants, *, allocation=None, task_lease=None, recover_tasks=False, inputs=None, execution=None):
        # Revalidate even a directly constructed CheckedPlan; a Python dataclass
        # constructor or forged JSON must not stand in for checking.
        checked = check_plan(plan.encoded if isinstance(plan, CheckedPlan) else plan, self.broker.catalog)
        if task_lease is not None:
            context = self.broker.store.task_context(task_lease, principal, checked.fingerprint)
            if context['run'] != run or (allocation is not None and allocation != context['allocation']):
                raise ReplayMismatch('task execution changed run or funding')
            allocation = context['allocation']
            if context['caps'] is None:
                raise StateConflict('task execution requires a durable capability map')
            if grants!=context['caps']:
                raise ReplayMismatch('task execution changed its capability map')
            grants=context['caps']
            if context['speculative']:
                checked = check_plan(checked.encoded,self.broker.catalog,speculative=True)
        elif self.broker.store.program(run, principal) != checked.fingerprint:
            raise ReplayMismatch('run is pinned to a different checked program or catalog')
        moved_inputs={} if task_lease is None else self.broker.store.task_resource_inputs(task_lease.task,principal)
        bindings=self.broker.prepare_inputs(checked,run,principal,inputs,_task_caps=task_lease is not None,
                                            _branch_resources=bool(moved_inputs))
        self.custody=bool(moved_inputs) or task_resources(checked,self.broker.catalog) or (
            task_lease is not None and is_affine(checked.result_type))
        self.resources={}
        for value in (inputs or {}).values():
            for ref in value_refs(value).values():
                self.resources[ref.kind,ref.resource]=ref
        self.broker.store.bind_inputs(run,principal,bindings,task=None if task_lease is None else task_lease.task)
        if task_lease is not None:
            self.broker.store.check_capability_boundary(run,principal,grants,
                self.broker.capability_resources(checked,inputs or {}))
        control=PUBLIC if task_lease is None else context['control']
        control=control.join(*(Label.from_data(v.get('label')) for v in bindings.values()))
        execution=ExecutionBudget() if execution is None else execution
        with ExitStack() as contexts:
            return self._execute(checked,run,principal,grants,allocation,task_lease,contexts,recover_tasks,bindings,pc=[control],execution=execution)

    def _remember_resources(self, refs):
        for ref in refs:
            old=self.resources.get((ref.kind,ref.resource))
            if old is None or old.generation<ref.generation:
                self.resources[ref.kind,ref.resource]=ref

    def _inline_bindings(self, plan, inputs):
        # Sources are already typed runtime values or authenticated checkpoints.
        # Pure calls never consult mutable host evidence or external state.
        if set(inputs)!=set(dict(plan.parameters)):
            raise InvalidData('inline inputs must bind every declared parameter')
        for name,typ in plan.parameters:
            if typ.kind=='Variant':
                from types import SimpleNamespace
                _,member,payload=variant_payload(inputs[name],typ,self.broker.store)
                if not is_pure_type(typ):
                    raise InvalidData('pure invocation cannot accept a controlled Variant')
                self._inline_bindings(SimpleNamespace(parameters=(('payload',member),)),{'payload':payload})
            else:
                self.broker.catalog.validate(typ.name,inputs[name].data)
        return normalize_bindings({name:value.record() for name,value in inputs.items()})

    def _execute(self, checked, run, principal, grants, allocation, task_lease, contexts, recover_tasks, bindings, path=(),pc=None,execution=None,pure=False):
        pc=[PUBLIC] if pc is None else pc
        pure=pure or checked.pure
        doc = json.loads(checked.encoded)
        node_types=dict(checked.node_types)
        values = {}
        store = self.broker.store

        def budget_amount(argument,purpose,site):
            if type(argument) is not str:
                return argument
            source=values[argument]
            pc[0]=pc[0].join(source.label)
            saved=store.bind_budget_amount(run,principal,site,purpose,source.data,task_lease=task_lease,control=pc[0])
            pc[0]=pc[0].join(Label.from_data(saved['label']))
            return saved['amount']

        for node in doc['nodes']:
            execution.step()
            name, op = node['id'], node['op']
            site=name if not path else 'branch:'+digest([list(path),name])
            access=store.resource_access(run,principal,self.resources.values(),task_lease=task_lease) if self.custody else nullcontext({})
            with access as presented:
                try:
                    if op=='parameter':
                        bound=bindings[name]
                        result=capture_value(bound,node_types[name],store)
                    elif op=='variant':
                        typ=node_types[name]
                        payload=values[node['input']]
                        pc[0]=pc[0].join(payload.label)
                        result=Value({'tag':node['tag'],'payload':capture_record(payload,dict(typ.members)[node['tag']])},
                                     label=pc[0],variant=typ)
                        if not pure:
                            from types import SimpleNamespace
                            self.broker.prepare_inputs(SimpleNamespace(parameters=((name,typ),)),run,principal,{name:result},_branch_resources=True)
                    elif op=='pack':
                        fields={key:values[ref] for key,ref in node['fields'].items()}
                        pc[0]=pc[0].join(*(field.label for field in fields.values()))
                        typ=node_types[name]
                        result=Value({key:capture_record(fields[key],member) for key,member in typ.members},label=pc[0],bundle=typ)
                        from types import SimpleNamespace
                        self.broker.prepare_inputs(SimpleNamespace(parameters=((name,typ),)),run,principal,{name:result},_branch_resources=True)
                    elif op=='unpack':
                        result=values[node['input']]
                        pc[0]=pc[0].join(result.label)
                    elif op=='take':
                        packet=values[node['input']]
                        result=bundle_fields(packet,packet.bundle,store)[node['field']]
                        pc[0]=pc[0].join(packet.label,result.label)
                    elif op=='restrict':
                        result=values[node['input']].restricted(Label.from_data(node['label']))
                    elif op=='resources':
                        entries={key:values[ref] for key,ref in node['entries'].items()}
                        pc[0]=pc[0].join(*(value.label for value in entries.values()))
                        result=Value(resource_vector(bounded({key:value.data for key,value in entries.items()})))
                    elif op in ('compute','field','record'):
                        refs=node['inputs'] if op=='compute' else [node['input']] if op=='field' else list(node['fields'].values())
                        pc[0]=pc[0].join(*(values[ref].label for ref in refs))
                        if op=='compute':
                            data=compute(node['operator'],[values[ref].data for ref in refs])
                        elif op=='field':
                            data=bounded(values[node['input']].data[node['field']])
                        else:
                            data=record({key:values[ref].data for key,ref in node['fields'].items()})
                        result=Value(data)
                    elif op=='capability':
                        tool=node['tool']
                        if tool not in grants:
                            raise AuthorizationDenied('tool capability is absent from this invocation')
                        saved=store.bind_capability(run,principal,site,tool,grants[tool],scope=self.broker.catalog.tools[tool].scope,
                                                   task_lease=task_lease,control=pc[0])
                        result=Value(saved['grant'],label=Label.from_data(saved['label']))
                    elif op=='attenuate':
                        cap=values[node['cap']]
                        pc[0]=pc[0].join(cap.label)
                        saved=store.attenuate_capability(run,principal,'ir-cap:'+digest([activation(task_lease),site]),cap.data,
                            expires=node['expires'],max_attempts=node['max_attempts'],constraints=node['constraints'],task_lease=task_lease,control=pc[0])
                        result=Value(saved['grant'],label=Label.from_data(saved['label']))
                    elif op=='split_budget':
                        parent=allocation if node['parent'] is None else values[node['parent']].data
                        if node['parent'] is not None:
                            pc[0]=pc[0].join(values[node['parent']].label)
                        result=Value(store.split_budget(run,principal,'ir-budget:'+digest([activation(task_lease),site]),
                                                       budget_amount(node['amount'],'split',site),parent=parent,task_lease=task_lease))
                    elif op=='join_budget':
                        budget=values[node['budget']]
                        pc[0]=pc[0].join(budget.label)
                        store.join_budget(run,principal,budget.data,task_lease=task_lease)
                        result=Value(True)
                    elif op in ('invoke','invoke_caps'):
                        child=check_plan(node['plan'],self.broker.catalog)
                        parameter_types=dict(child.parameters)
                        inputs={key:values[ref] for key,ref in node['inputs'].items()}
                        funding=allocation if node['budget'] is None else values[node['budget']].data
                        pc[0]=pc[0].join(*(value.label for value in inputs.values()))
                        if node['budget'] is not None:
                            pc[0]=pc[0].join(values[node['budget']].label)
                        caps=None
                        if op=='invoke_caps':
                            caps={tool:values[ref].data for tool,ref in node['caps'].items()}
                            pc[0]=pc[0].join(*(values[ref].label for ref in node['caps'].values()))
                        frame=store.begin_invocation(run,principal,site,
                            {key:capture_record(value,parameter_types[key]) for key,value in inputs.items()},
                            allocation=funding,task_lease=task_lease,control=pc[0],caps=caps)
                        pc[0]=pc[0].join(Label.from_data(frame['control']))
                        inputs={key:capture_value(record,parameter_types[key],store) for key,record in frame['inputs'].items()}
                        bound=self.broker.prepare_inputs(child,run,principal,inputs,_branch_resources=True)
                        child_grants=grants
                        if op=='invoke_caps':
                            child_grants=frame['caps']
                            store.check_capability_boundary(run,principal,child_grants,self.broker.capability_resources(child,inputs))
                        result=self._execute(child,run,principal,child_grants,frame['allocation'],task_lease,contexts,recover_tasks,bound,
                                             path+(name,'invoke'),pc,execution)
                    elif op=='call':
                        child=check_plan(node['plan'],self.broker.catalog)
                        inputs={key:values[ref] for key,ref in node['inputs'].items()}
                        pc[0]=pc[0].join(*(value.label for value in inputs.values()))
                        bound=self._inline_bindings(child,inputs)
                        result=self._execute(child,run,principal,grants,allocation,task_lease,contexts,recover_tasks,bound,
                                             path+(name,'call'),pc,execution,True)
                    elif op=='repeat':
                        body=check_plan(node['plan'],self.broker.catalog,_pure=pure)
                        resource_state=is_affine(body.result_type)
                        number=values[node['count']]
                        initial=values[node['initial']]
                        captures={key:values[ref] for key,ref in node['inputs'].items()}
                        pc[0]=pc[0].join(number.label,initial.label,*(value.label for value in captures.values()))
                        if pure:
                            if type(number.data) is not int or not 0<=number.data<=node['limit']:
                                raise EvaluationError('repeat count is outside its declared bound',node=name)
                            progress={'frame':{'count':number.data,'inputs':{k:v.record() for k,v in captures.items()},'control':pc[0].to_data()},
                                      'next':0,'current':initial.restricted(pc[0]).record()}
                        elif resource_state:
                            progress=store.begin_resource_loop(run,principal,site,number.data,node['limit'],
                                initial.restricted(pc[0]).record(),{key:value.record() for key,value in captures.items()},
                                interface=describe_type(body.result_type),task_lease=task_lease,control=pc[0])
                        else:
                            progress=store.begin_loop(run,principal,site,number.data,node['limit'],initial.restricted(pc[0]).record(),
                                                      {key:value.record() for key,value in captures.items()},task_lease=task_lease,control=pc[0])
                        pc[0]=pc[0].join(Label.from_data(progress['frame']['control']))
                        result=Value.from_record(progress['current'])
                        if resource_state:
                            self._remember_resources(value_refs(result).values())
                        pc[0]=pc[0].join(result.label)
                        while progress['next']<progress['frame']['count']:
                            execution.step()
                            index=progress['next']
                            inputs={key:Value.from_record(value) for key,value in progress['frame']['inputs'].items()}
                            inputs.update(index=Value(index,label=pc[0]),state=result.restricted(pc[0]))
                            bound=(self.broker.prepare_inputs(body,run,principal,inputs,_branch_resources=True)
                                   if resource_state else self._inline_bindings(body,inputs))
                            result=self._execute(body,run,principal,grants,allocation,task_lease,contexts,recover_tasks,bound,
                                                 path+(name,'iteration',str(index)),pc,execution,pure)
                            if pure:
                                progress['next']=index+1
                            elif resource_state:
                                with store.resource_access(run,principal,self.resources.values(),task_lease=task_lease):
                                    progress=store.advance_resource_loop(run,principal,site,index,result.record(),task_lease=task_lease)
                                result=Value.from_record(progress['current'])
                                self._remember_resources(value_refs(result).values())
                            else:
                                progress=store.advance_loop(run,principal,site,index,result.record(),task_lease=task_lease)
                                result=Value.from_record(progress['current'])
                            pc[0]=pc[0].join(result.label)
                    elif op == 'scope':
                        from .concurrency import TaskScope
                        amount=budget_amount(node['budget'],'scope',site)
                        scope = TaskScope(self.broker,run,principal,'ir:'+site,amount,allocation=allocation,
                                          parent_lease=task_lease,speculative=node['speculative'],recover_tasks=recover_tasks,control=pc[0],execution=execution)
                        result = Value(contexts.enter_context(scope))
                    elif op in ('spawn','spawn_with','spawn_caps'):
                        scope = values[node['scope']].data
                        inputs={key:values[ref] for key,ref in node.get('inputs',{}).items()}
                        amount=budget_amount(node['budget'],'spawn',site)
                        child_grants=grants
                        if op=='spawn_caps':
                            child_grants={tool:values[ref].data for tool,ref in node['caps'].items()}
                            pc[0]=pc[0].join(*(values[ref].label for ref in node['caps'].values()))
                        task, _ = scope.spawn(site,node['plan'],amount,child_grants,inputs=inputs,control=pc[0])
                        result = Value((scope,task))
                    elif op in ('join','handoff','join_outcome','cancel_outcome'):
                        scope,task = values[node['task']].data
                        pc[0]=pc[0].join(store.task_label(task,principal))
                        if op=='join':
                            result=scope.join(task)
                        else:
                            result=Value({'handoff':scope.handoff,'join_outcome':scope.join_outcome,
                                          'cancel_outcome':scope.cancel_outcome}[op](task))
                            if op!='handoff' and result.data['kind']=='Completed' and self.custody:
                                self._remember_resources(value_refs(Value.from_record(result.data['payload'])).values())
                        pc[0]=pc[0].join(result.label)
                    elif op in ('match_task','if','match_admission','match_variant'):
                        if op=='match_variant':
                            source=values[node['input']]
                            pc[0]=pc[0].join(source.label)
                            if pure:
                                captured={key:values[ref] for key,ref in node['inputs'].items()}
                            else:
                                decision=store.choose_variant_branch(run,principal,site,source.record(),
                                    {key:capture_record(values[ref],node_types[ref]) for key,ref in node['inputs'].items()},
                                    task_lease=task_lease,control=pc[0])
                                source=Value.from_record(decision['condition'])
                                pc[0]=pc[0].join(Label.from_data(decision['control']),source.label)
                                captured={key:capture_value(value,node_types[node['inputs'][key]],store) for key,value in decision['inputs'].items()}
                            selected,_,payload=variant_payload(source,node_types[node['input']],store)
                            pc[0]=pc[0].join(payload.label)
                        elif op=='match_admission':
                            source=values[node['input']]
                            pc[0]=pc[0].join(source.label)
                            admission=source.data
                            selected='Admitted' if admission['kind']=='Admitted' else 'Returned' if admission['permit'] is not None else 'NoPermit'
                            decision=store.choose_admission_branch(run,principal,site,selected,source.record(),
                                {key:capture_record(values[ref],node_types[ref]) for key,ref in node['inputs'].items()},task_lease=task_lease,control=pc[0])
                            selected=decision['case']
                            admission=decision['condition']['data']
                            pc[0]=pc[0].join(Label.from_data(decision['control']),Label.from_data(decision['condition'].get('label')))
                            captured={key:capture_value(value,node_types[node['inputs'][key]],store) for key,value in decision['inputs'].items()}
                        elif op=='match_task':
                            source=values[node['input']]
                            outcome=source.data
                            pc[0]=pc[0].join(source.label,Label.from_data(outcome.get('label')))
                            selected=outcome['kind']
                            captured={key:values[ref] for key,ref in node['inputs'].items()}
                        else:
                            source=values[node['condition']]
                            pc[0]=pc[0].join(source.label)
                            if pure:
                                selected='Then' if source.data else 'Else'
                                captured={key:values[ref] for key,ref in node['inputs'].items()}
                            else:
                                decision=store.choose_branch(run,principal,site,source.record(),
                                    {key:capture_record(values[ref],node_types[ref]) for key,ref in node['inputs'].items()},task_lease=task_lease,control=pc[0])
                                selected=decision['case']
                                pc[0]=pc[0].join(Label.from_data(decision['control']),Label.from_data(decision['condition'].get('label')))
                                captured={key:capture_value(value,node_types[node['inputs'][key]],store) for key,value in decision['inputs'].items()}
                        branch=check_plan(node['cases'][selected],self.broker.catalog,_pure=pure)
                        inputs={}
                        for parameter,parameter_type in branch.parameters:
                            if op=='match_admission' and parameter in ('result','reason'):
                                data=(admission['reason'] if parameter=='reason' or selected=='NoPermit' else
                                      admission['operation'] if selected=='Admitted' else admission['permit'])
                                ref=self.resources.get((parameter_type.kind,data)) if self.custody and parameter_type.kind in MOVED_RESOURCES else None
                                inputs[parameter]=Value(data,label=pc[0],resource=ref)
                            elif op=='match_variant' and parameter=='result':
                                inputs[parameter]=payload.restricted(pc[0])
                            elif op=='match_task' and parameter=='result':
                                payload=outcome['payload']
                                inputs[parameter]=Value.from_record(payload).restricted(pc[0])
                            else:
                                inputs[parameter]=captured[parameter]
                        bound=self._inline_bindings(branch,inputs) if pure else self.broker.prepare_inputs(branch,run,principal,inputs,_branch_resources=True)
                        result=self._execute(branch,run,principal,grants,allocation,task_lease,contexts,recover_tasks,bound,path+(name,selected),pc,execution,pure)
                    elif op == 'race':
                        handles = [values[t].data for t in node['tasks']]
                        pc[0]=pc[0].join(*(store.task_label(task,principal) for _,task in handles))
                        result = handles[0][0].race(site,[task for _,task in handles])
                        pc[0]=pc[0].join(result.label)
                    elif op == 'close_scope':
                        values[node['scope']].data.__exit__(None,None,None)
                        result = Value(True)
                    elif op == 'literal':
                        result = Value(node['value'])
                    elif op == 'try_infer':
                        spec = self.broker.catalog.tools[node['tool']]
                        source = values[node['input']].restricted(pc[0])
                        result = self.broker.try_infer(run,principal,site,source,spec,grants,
                                                     allocation=allocation,task_lease=task_lease)
                        pc[0] = pc[0].join(result.label)
                    elif op in ('infer', 'infer_result', 'infer_progress', 'observe', 'check'):
                        spec = self.broker.catalog.tools[node['tool']]
                        source = values[node['input']].restricted(pc[0])
                        if spec.name not in grants:
                            raise AuthorizationDenied('host did not bind the required grant')
                        operation = self.broker.admit_call(run, principal, site, spec.name, source, grants[spec.name], allocation=allocation, task_lease=task_lease)
                        pc[0]=pc[0].join(store.operation_label(operation,principal))
                        if op == 'infer_progress':
                            result = self.broker.inference_progress(run, principal, site,
                                store.resource_birth('Operation', operation, principal), spec, grants,
                                task_lease=task_lease, control=source.label)
                        elif op == 'infer_result':
                            result = self.broker.inference_output(operation, principal, spec,
                                task_lease=task_lease, control=source.label)
                        elif op == 'check':
                            output = self.broker.output(operation, principal, spec.output_type, task_lease=task_lease)
                            if output is not True:
                                raise EvidenceMismatch('registered predicate rejected this candidate', node=name)
                            receipt = store.record_check(run, principal, spec.predicate, spec.version, source.data,
                                                         {'check_operation': operation}, check_id=f'check:{operation}')
                            result = Value(source.data, source.receipts + (receipt,),source.label)
                        else:
                            output = self.broker.output(operation, principal, spec.output_type, task_lease=task_lease)
                            result = Value(output)
                    elif op == 'resume_infer':
                        source = values[node['operation']].restricted(pc[0])
                        spec = self.broker.catalog.tools[node_types[node['operation']].name]
                        result = self.broker.inference_progress(run, principal, site, source.resource, spec, grants,
                                                              task_lease=task_lease, control=source.label)
                        pc[0] = pc[0].join(result.label)
                    elif op == 'prepare':
                        source = values[node['input']]
                        spec = self.broker.catalog.tools[node['tool']]
                        label=source.label.join(pc[0])
                        result = Value(Intent(spec.name, spec.version, spec.scope, source.data, source.receipts,label=label),label=label)
                    elif op in ('authorize','authorize_with'):
                        intent = values[node['intent']].data
                        if op=='authorize_with':
                            pc[0]=pc[0].join(values[node['cap']].label)
                        intent_context(intent,pc[0])
                        pc[0]=pc[0].join(intent.label)
                        tool = intent.value['tool']
                        if op=='authorize' and tool not in grants:
                            raise AuthorizationDenied('host did not bind the required action grant')
                        grant=values[node['cap']].data if op=='authorize_with' else grants[tool]
                        result = Value(store.authorize(run, principal, intent, grant, authorization_key=authorization_site('authorize', site, task_lease), task_lease=task_lease))
                    elif op in ('admit','try_admit'):
                        if op=='admit':
                            intent_context(values[node['intent']].data,pc[0])
                        pc[0]=pc[0].join(values[node['intent']].data.label)
                        admitted=(store.try_admit if op=='try_admit' else store.admit)(run, principal, Slot(site, activation(task_lease)),
                            values[node['intent']].data, values[node['permit']].data, allocation=allocation, task_lease=task_lease,
                            **({'control':pc[0]} if op=='try_admit' else {}))
                        result=Value(admitted,label=Label.from_data(admitted['label']) if op=='try_admit' else PUBLIC)
                    elif op=='borrow_operation':
                        result=values[node['operation']]
                    elif op in ('inspect', 'await'):
                        operation = values[node['operation']].data
                        result = Value(self.broker.drive(operation, principal, task_lease=task_lease,control=pc[0]) if op == 'await' else store.snapshot(operation, principal))
                        pc[0]=pc[0].join(store.operation_label(operation,principal))
                    elif op == 'reconcile':
                        if node['tool'] not in grants:
                            raise AuthorizationDenied('host did not bind a reconciliation grant')
                        result = Value(self.broker.reconcile(run, principal, site, values[node['operation']].data,
                                                             node['tool'], grants[node['tool']], allocation=allocation, task_lease=task_lease,control=pc[0]))
                        pc[0]=pc[0].join(store.operation_label(values[node['operation']].data,principal),self.broker.catalog.tools[node['tool']].output_label)
                    elif op == 'match_business':
                        pc[0]=pc[0].join(values[node['input']].label)
                        result = Value(node['cases'][values[node['input']].data['business']])
                    if self.custody and node_types[name].kind in MOVED_RESOURCES-{'Bundle'} and result.resource is None:
                        ref=store.resource_birth(node_types[name].kind,result.data,principal)
                        result=Value(result.data,result.receipts,result.label,ref)
                    if result.resource is not None:
                        self._remember_resources([result.resource])
                    if result.bundle is not None or result.variant is not None:
                        self._remember_resources(value_refs(result).values())
                    values[name] = result.restricted(pc[0])
                finally:
                    self._remember_resources(presented.values())
        execution.check_deadline()
        return values[doc['return']].restricted(pc[0])
