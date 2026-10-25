"""Typed Python authoring facade for the same data-only checked IR.

References are symbolic, never runtime capabilities. Generic annotations assist
host type checkers; the shared IR checker remains authoritative for schemas,
predicate evidence, affine moves and exact intent binding.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, Mapping, Sequence, TypeAlias, TypeVar, overload

from .data import canonical, identifier, load_json
from .errors import TypeCheckError
from .ir import Catalog, CheckedPlan, Tool, Type, check_plan
from .bundle_types import describe, validate, validate_variant
from .flow import Label


I = TypeVar('I')
O = TypeVar('O')
T = TypeVar('T')
A = TypeVar('A')


class Candidate(Generic[T]):
    pass


class Observation(Generic[T]):
    pass


class Checked(Generic[T]):
    pass


class Intent(Generic[A]):
    pass


class Permit(Generic[A]):
    pass


class Operation(Generic[A]):
    pass


class Admission(Generic[A]):
    pass


class OpRef(Generic[A]):
    pass


class Snapshot(Generic[A]):
    pass


class Scope:
    pass


class Budget:
    pass


class Bundle:
    pass


class Variant:
    """Closed result category; case schemas are checked by the shared IR."""
    pass


class Unpacked:
    pass


class Cap(Generic[T]):
    pass


class Task(Generic[T]):
    pass


class TaskOutcome(Generic[T]):
    pass


@dataclass(frozen=True)
class Ref(Generic[T]):
    name: str
    _owner: object


@dataclass(frozen=True)
class ToolRef(Generic[I, O]):
    spec: Tool
    _owner: object


DataRef: TypeAlias = Ref[T] | Ref[Candidate[T]] | Ref[Observation[T]] | Ref[Checked[T]]
Inputs: TypeAlias = Mapping[str, Ref[Any]]
# Registered record fields and dynamically selected schemas are checked by IR.
Amount: TypeAlias = dict[str, int] | Ref[Any]


class PlanBuilder:
    def __init__(self, name: str, catalog: Catalog, *, uses: list[str] | None = None, pure: bool=False) -> None:
        self.name = identifier(name)
        self.catalog = catalog
        self._owner = object()
        self._nodes: list[dict[str, Any]] = []
        self._names: set[str] = set()
        self._effects: list[str] = []
        self._declared = None if uses is None else list(uses)
        self._bound_catalog: str | None = None
        if type(pure) is not bool:
            raise TypeCheckError('pure function flag must be boolean')
        self.pure=pure

    def _bind_catalog(self, fingerprint: str) -> None:
        if self.catalog.fingerprint() != fingerprint or self._bound_catalog not in (None, fingerprint):
            raise TypeCheckError('generated bindings belong to a different catalog; regenerate and recheck the program')
        self._bound_catalog = fingerprint

    def _check_catalog(self) -> None:
        if self._bound_catalog is not None and self.catalog.fingerprint() != self._bound_catalog:
            raise TypeCheckError('catalog changed after binding generated types; regenerate and recheck the program')

    def tool(self, name: str) -> ToolRef[I, O]:
        self._check_catalog()
        if name not in self.catalog.tools:
            raise TypeCheckError('tool is not in this builder catalog')
        return ToolRef(self.catalog.tools[name], self._owner)

    def _ref(self, ref: Ref[Any]) -> str:
        if not isinstance(ref, Ref) or ref._owner is not self._owner or ref.name not in self._names:
            raise TypeCheckError('reference belongs to another builder or is undefined')
        return ref.name

    def _tool(self, tool: ToolRef[Any, Any], kind: str | None) -> Tool:
        if not isinstance(tool, ToolRef) or tool._owner is not self._owner or kind is not None and tool.spec.kind != kind:
            raise TypeCheckError('tool binding has the wrong owner or kind')
        return tool.spec

    def _add(self, name: str, op: str, **fields: Any) -> Ref[Any]:
        self._check_catalog()
        identifier(name)
        if name in self._names:
            raise TypeCheckError('node identity already defined', node=name)
        # Detach user-owned dictionaries, so later edits do not change the plan.
        self._nodes.append(load_json(canonical({'id': name, 'op': op, **fields})))
        self._names.add(name)
        return Ref(name, self._owner)

    def _effect(self, effect: str) -> None:
        self._check_catalog()
        if effect not in self._effects:
            self._effects.append(effect)

    def literal(self, name: str, schema: str, value: T) -> Ref[T]:
        self.catalog.validate(schema, value)
        return self._add(name, 'literal', type=schema, value=value)

    @overload
    def parameter(self, name: str, schema: str, *, kind: Literal['Data']='Data', predicates: Sequence[str]=()) -> Ref[T]: ...
    @overload
    def parameter(self, name: str, schema: str, *, kind: Literal['Candidate'], predicates: Sequence[str]=()) -> Ref[Candidate[T]]: ...
    @overload
    def parameter(self, name: str, schema: str, *, kind: Literal['Observation'], predicates: Sequence[str]=()) -> Ref[Observation[T]]: ...
    @overload
    def parameter(self, name: str, schema: str, *, kind: Literal['Checked'], predicates: Sequence[str]=()) -> Ref[Checked[T]]: ...
    @overload
    def parameter(self, name: str, schema: str, *, kind: str, predicates: Sequence[str]=()) -> Ref[Any]: ...
    def parameter(self, name: str, schema: str, *, kind: str='Data', predicates: Sequence[str]=()) -> Ref[Any]:
        return self._add(name,'parameter',type=schema,kind=kind,predicates=list(predicates))

    def intent_parameter(self, name: str, tool: ToolRef[I, A]) -> Ref[Intent[A]]:
        return self.parameter(name,self._tool(tool,'action').name,kind='Intent')

    def permit_parameter(self, name: str, intent: Ref[Intent[A]]) -> Ref[Permit[A]]:
        return self.parameter(name,self._ref(intent),kind='Permit')

    def operation_parameter(self, name: str, tool: ToolRef[I, A]) -> Ref[Operation[A]]:
        return self.parameter(name,self._operation_tool(tool).name,kind='Operation')

    def opref_parameter(self, name: str, tool: ToolRef[I, A]) -> Ref[OpRef[A]]:
        return self.parameter(name,self._operation_tool(tool).name,kind='OpRef')

    def _operation_tool(self, tool: ToolRef[Any, Any]) -> Tool:
        spec = self._tool(tool, None)
        if spec.kind not in ('action', 'infer'):
            raise TypeCheckError('operation parameters require an action or model tool')
        return spec

    def budget_parameter(self, name: str) -> Ref[Budget]:
        return self.parameter(name,'Budget',kind='Budget')

    def bundle_parameter(self, name: str, prototype: CheckedPlan[Bundle] | Type) -> Ref[Bundle]:
        typ=prototype if isinstance(prototype,Type) else check_plan(prototype.encoded,self.catalog).result_type
        if typ is None or typ.kind!='Bundle':
            raise TypeCheckError('bundle parameter needs a checked bundle result interface')
        validate(typ,self.catalog)
        return self._add(name,'parameter',kind='Bundle',type=describe(typ),predicates=[])

    def variant_parameter(self, name: str, interface: Type) -> Ref[Variant]:
        validate_variant(interface,self.catalog)
        return self._add(name,'parameter',kind='Variant',type=describe(interface),predicates=[])

    def variant(self, name: str, interface: Type, tag: str, payload: Ref[Any]) -> Ref[Variant]:
        validate_variant(interface,self.catalog)
        return self._add(name,'variant',type=describe(interface),tag=tag,input=self._ref(payload))

    def match_variant(self, name: str, value: Ref[Variant], cases: Mapping[str, CheckedPlan[T]], *, inputs: Inputs | None=None) -> Ref[T]:
        documents={}
        for tag,plan in cases.items():
            checked=check_plan(plan.encoded,self.catalog)
            document=load_json(checked.encoded)
            for effect in document['uses']:
                self._effect(effect)
            documents[tag]=document
        return self._add(name,'match_variant',input=self._ref(value),cases=documents,
                         inputs={k:self._ref(v) for k,v in (inputs or {}).items()})

    def pack(self, name: str, fields: Inputs) -> Ref[Bundle]:
        return self._add(name,'pack',fields={key:self._ref(value) for key,value in fields.items()})

    def unpack(self, name: str, packet: Ref[Bundle]) -> Ref[Unpacked]:
        return self._add(name,'unpack',input=self._ref(packet))

    def take(self, name: str, packet: Ref[Unpacked], field: str) -> Ref[Any]:
        return self._add(name,'take',input=self._ref(packet),field=field)

    def _type_of(self, value: Ref[Any]) -> Type:
        document={'ir_version':1,'name':self.name,'uses':self._effects if self._declared is None else self._declared,
                  'nodes':self._nodes,'return':self._ref(value)}
        typ=check_plan(document,self.catalog,_partial=True).result_type
        if typ is None:
            raise TypeCheckError('reference has no checked type')
        return typ

    def cap_parameter(self, name: str, tool: ToolRef[I, O]) -> Ref[Cap[O]]:
        return self.parameter(name,self._tool(tool,None).name,kind='Cap')

    def capability(self, name: str, tool: ToolRef[I, O]) -> Ref[Cap[O]]:
        spec=self._tool(tool,None)
        self._effect(f'cap:{spec.name}@{spec.scope}')
        return self._add(name,'capability',tool=spec.name)

    def _cap_spec(self,cap: Ref[Cap[Any]]) -> Tool:
        typ=self._type_of(cap)
        if typ.kind!='Cap':
            raise TypeCheckError('expected a capability reference')
        return self.catalog.tools[typ.name]

    def attenuate(self,name: str,cap: Ref[Cap[O]],*,expires: int | None=None,
                  max_attempts: int | None=None,constraints: dict[str, Any] | None=None) -> Ref[Cap[O]]:
        spec=self._cap_spec(cap)
        self._effect(f'cap:{spec.name}@{spec.scope}')
        return self._add(name,'attenuate',cap=self._ref(cap),expires=expires,max_attempts=max_attempts,
                         constraints={} if constraints is None else constraints)

    def resources(self, name: str, entries: Mapping[str, DataRef[int]]) -> Ref[Any]:
        return self._add(name,'resources',entries={key:self._ref(value) for key,value in entries.items()})

    def _amount(self, amount: Amount) -> dict[str, int] | str:
        if isinstance(amount,Ref):
            return self._ref(amount)
        if type(amount) is not dict:
            raise TypeCheckError('budget amount requires a literal resource map or Ref')
        return amount

    def split_budget(self, name: str, amount: Amount, *, parent: Ref[Budget] | None=None) -> Ref[Budget]:
        self._effect('budget')
        return self._add(name,'split_budget',amount=self._amount(amount),parent=None if parent is None else self._ref(parent))

    def join_budget(self, name: str, budget: Ref[Budget]) -> Ref[bool]:
        self._effect('budget')
        return self._add(name,'join_budget',budget=self._ref(budget))

    def invoke(self, name: str, workflow: CheckedPlan[T], *, inputs: Inputs | None=None,
               budget: Ref[Budget] | None=None) -> Ref[T]:
        checked=check_plan(workflow.encoded,self.catalog)
        document=load_json(checked.encoded)
        for effect in document['uses']:
            self._effect(effect)
        return self._add(name,'invoke',plan=document,inputs={key:self._ref(value) for key,value in (inputs or {}).items()},
                         budget=None if budget is None else self._ref(budget))

    def invoke_caps(self,name: str,workflow: CheckedPlan[T],*,caps: Mapping[str,Ref[Cap[Any]]],
                    inputs: Inputs | None=None,budget: Ref[Budget] | None=None) -> Ref[T]:
        checked=check_plan(workflow.encoded,self.catalog)
        document=load_json(checked.encoded)
        for effect in document['uses']:
            self._effect(effect)
        return self._add(name,'invoke_caps',plan=document,inputs={key:self._ref(value) for key,value in (inputs or {}).items()},
                         budget=None if budget is None else self._ref(budget),caps={tool:self._ref(cap) for tool,cap in caps.items()})

    def restrict(self, name: str, value: Ref[T], label: Label) -> Ref[T]:
        return self._add(name,'restrict',input=self._ref(value),label=label.to_data())

    def compute(self, name: str, operator: str, *values: DataRef[Any]) -> Ref[Any]:
        return self._add(name,'compute',operator=operator,inputs=[self._ref(value) for value in values])

    def field(self, name: str, value: DataRef[Any], field: str) -> Ref[Any]:
        return self._add(name,'field',input=self._ref(value),field=field)

    def record(self, name: str, schema: str, fields: Inputs) -> Ref[Any]:
        return self._add(name,'record',type=schema,fields={key:self._ref(value) for key,value in fields.items()})

    def if_else(self, name: str, condition: DataRef[bool], *, then: CheckedPlan[T], otherwise: CheckedPlan[T],
                inputs: Inputs | None = None) -> Ref[T]:
        cases={}
        for state,plan in [('Then',then),('Else',otherwise)]:
            branch=check_plan(plan.encoded,self.catalog)
            document=load_json(branch.encoded)
            for effect in document['uses']:
                self._effect(effect)
            cases[state]=document
        return self._add(name,'if',condition=self._ref(condition),cases=cases,
                         inputs={key:self._ref(value) for key,value in (inputs or {}).items()})

    def call(self, name: str, function: CheckedPlan[T], *, inputs: Inputs) -> Ref[T]:
        checked=check_plan(function.encoded,self.catalog)
        return self._add(name,'call',plan=load_json(checked.encoded),inputs={key:self._ref(value) for key,value in inputs.items()})

    def repeat(self, name: str, count: DataRef[int], limit: int, initial: Ref[T], body: CheckedPlan[T], *, inputs: Inputs | None = None) -> Ref[T]:
        checked=check_plan(body.encoded,self.catalog)
        document=load_json(checked.encoded)
        for effect in document['uses']:
            self._effect(effect)
        return self._add(name,'repeat',count=self._ref(count),limit=limit,initial=self._ref(initial),plan=document,
                         inputs={key:self._ref(value) for key,value in (inputs or {}).items()})

    def infer(self, name: str, tool: ToolRef[I, O], value: DataRef[I]) -> Ref[Candidate[O]]:
        spec = self._tool(tool, 'infer')
        ref = self._ref(value)
        self._effect(f'infer:{spec.name}')
        return self._add(name, 'infer', tool=spec.name, input=ref)

    def infer_result(self, name: str, tool: ToolRef[I, O], value: DataRef[I]) -> Ref[Variant]:
        spec = self._tool(tool, 'infer')
        ref = self._ref(value)
        self._effect(f'infer:{spec.name}')
        return self._add(name, 'infer_result', tool=spec.name, input=ref)

    def infer_progress(self, name: str, tool: ToolRef[I, O], value: DataRef[I]) -> Ref[Variant]:
        spec = self._tool(tool, 'infer')
        ref = self._ref(value)
        self._effect(f'infer:{spec.name}')
        return self._add(name, 'infer_progress', tool=spec.name, input=ref)

    def try_infer(self, name: str, tool: ToolRef[I, O], value: DataRef[I]) -> Ref[Variant]:
        spec = self._tool(tool, 'infer')
        ref = self._ref(value)
        self._effect(f'infer:{spec.name}')
        return self._add(name, 'try_infer', tool=spec.name, input=ref)

    def resume_infer(self, name: str, operation: Ref[Operation[A]]) -> Ref[Variant]:
        typ = self._type_of(operation)
        if typ.kind != 'Operation' or self.catalog.tools[typ.name].kind != 'infer':
            raise TypeCheckError('resume_infer requires a model operation')
        self._effect(f'infer:{typ.name}')
        return self._add(name, 'resume_infer', operation=self._ref(operation))

    def observe(self, name: str, tool: ToolRef[I, O], value: DataRef[I]) -> Ref[Observation[O]]:
        spec = self._tool(tool, 'observe')
        ref = self._ref(value)
        self._effect(f'observe:{spec.name}')
        return self._add(name, 'observe', tool=spec.name, input=ref)

    def check(self, name: str, tool: ToolRef[T, bool],
              value: DataRef[T]) -> Ref[Checked[T]]:
        spec = self._tool(tool, 'check')
        ref = self._ref(value)
        self._effect(f'check:{spec.name}')
        return self._add(name, 'check', tool=spec.name, input=ref)

    def prepare(self, name: str, tool: ToolRef[I, A], value: Ref[I] | Ref[Checked[I]]) -> Ref[Intent[A]]:
        spec = self._tool(tool, 'action')
        return self._add(name, 'prepare', tool=spec.name, input=self._ref(value))

    def _prepared_tool(self, intent: Ref[Any]) -> Tool:
        ref = self._ref(intent)
        node = next(n for n in self._nodes if n['id'] == ref)
        if node['op']=='take':
            typ=self._type_of(intent)
            if typ.kind=='Intent':
                return self.catalog.tools[typ.name]
        if node['op']=='parameter' and node['kind']=='Intent':
            spec=self.catalog.tools.get(node['type'])
            if spec is not None and spec.kind=='action':
                return spec
        if node['op'] != 'prepare':
            raise TypeCheckError('expected a prepared intent')
        return self.catalog.tools[node['tool']]

    def authorize(self, name: str, intent: Ref[Intent[A]]) -> Ref[Permit[A]]:
        spec = self._prepared_tool(intent)
        self._effect(f'authorize:{spec.name}@{spec.scope}')
        return self._add(name, 'authorize', intent=self._ref(intent))

    def authorize_with(self,name: str,intent: Ref[Intent[A]],cap: Ref[Cap[A]]) -> Ref[Permit[A]]:
        spec=self._prepared_tool(intent)
        self._effect(f'authorize:{spec.name}@{spec.scope}')
        return self._add(name,'authorize_with',intent=self._ref(intent),cap=self._ref(cap))

    def admit(self, name: str, intent: Ref[Intent[A]], permit: Ref[Permit[A]]) -> Ref[Operation[A]]:
        spec = self._prepared_tool(intent)
        self._effect(f'admit:{spec.name}@{spec.scope}')
        return self._add(name, 'admit', intent=self._ref(intent), permit=self._ref(permit))

    def try_admit(self, name: str, intent: Ref[Intent[A]], permit: Ref[Permit[A]]) -> Ref[Admission[A]]:
        spec=self._prepared_tool(intent)
        self._effect(f'admit:{spec.name}@{spec.scope}')
        return self._add(name,'try_admit',intent=self._ref(intent),permit=self._ref(permit))

    def match_admission(self,name: str,admission: Ref[Admission[A]],*,admitted: CheckedPlan[T],
                        returned: CheckedPlan[T],no_permit: CheckedPlan[T],inputs: Inputs | None=None) -> Ref[T]:
        cases={}
        for state,plan in [('Admitted',admitted),('Returned',returned),('NoPermit',no_permit)]:
            checked=check_plan(plan.encoded,self.catalog)
            document=load_json(checked.encoded)
            for effect in document['uses']:
                self._effect(effect)
            cases[state]=document
        return self._add(name,'match_admission',input=self._ref(admission),cases=cases,
                         inputs={key:self._ref(value) for key,value in (inputs or {}).items()})

    def borrow_operation(self, name: str, operation: Ref[Operation[A]] | Ref[OpRef[A]]) -> Ref[OpRef[A]]:
        return self._add(name,'borrow_operation',operation=self._ref(operation))

    def inspect(self, name: str, operation: Ref[Operation[A]] | Ref[OpRef[A]]) -> Ref[Snapshot[A]]:
        return self._add(name, 'inspect', operation=self._ref(operation))

    def await_result(self, name: str, operation: Ref[Operation[A]]) -> Ref[Snapshot[A]]:
        return self._add(name, 'await', operation=self._ref(operation))

    def reconcile(self, name: str, tool: ToolRef[Any, Any], operation: Ref[Operation[A]]) -> Ref[Snapshot[A]]:
        spec = self._tool(tool, 'reconcile')
        self._effect(f'reconcile:{spec.name}')
        return self._add(name, 'reconcile', tool=spec.name, operation=self._ref(operation))

    def match_business(self, name: str, status: Ref[Snapshot[A]], schema: str,
                       *, applied: T, proven_no_effect: T, pending: T,
                       unknown: T, not_dispatched: T) -> Ref[T]:
        return self._add(name, 'match_business', input=self._ref(status), type=schema,
                         cases={'Applied': applied, 'ProvenNoEffect': proven_no_effect,
                                'Pending': pending, 'Unknown': unknown, 'NotDispatched': not_dispatched})

    def scope(self, name: str, budget: Amount, *, speculative: bool=False) -> Ref[Scope]:
        self._effect('tasks')
        return self._add(name,'scope',budget=self._amount(budget),speculative=speculative)

    def spawn(self, name: str, scope: Ref[Scope], plan: CheckedPlan[T], budget: Amount, *, inputs: Inputs | None = None) -> Ref[Task[T]]:
        child = check_plan(plan.encoded,self.catalog)
        doc = load_json(child.encoded)
        self._effect('tasks')
        for effect in doc['uses']:
            self._effect(effect)
        if inputs is None:
            return self._add(name,'spawn',scope=self._ref(scope),plan=doc,budget=self._amount(budget))
        return self._add(name,'spawn_with',scope=self._ref(scope),plan=doc,budget=self._amount(budget),inputs={k:self._ref(v) for k,v in inputs.items()})

    def join(self, name: str, task: Ref[Task[T]]) -> Ref[T]:
        self._effect('tasks')
        return self._add(name,'join',task=self._ref(task))

    def spawn_caps(self,name: str,scope: Ref[Scope],plan: CheckedPlan[T],budget: Amount,
                   *,caps: Mapping[str,Ref[Cap[Any]]],inputs: Inputs | None=None) -> Ref[Task[T]]:
        child=check_plan(plan.encoded,self.catalog)
        doc=load_json(child.encoded)
        self._effect('tasks')
        for effect in doc['uses']:
            self._effect(effect)
        return self._add(name,'spawn_caps',scope=self._ref(scope),plan=doc,budget=self._amount(budget),
                         caps={key:self._ref(value) for key,value in caps.items()},
                         inputs={key:self._ref(value) for key,value in (inputs or {}).items()})

    def handoff(self, name: str, task: Ref[Task[T]]) -> Ref[dict[str, Any]]:
        self._effect('tasks')
        return self._add(name,'handoff',task=self._ref(task))

    def join_outcome(self, name: str, task: Ref[Task[T]]) -> Ref[TaskOutcome[T]]:
        self._effect('tasks')
        return self._add(name,'join_outcome',task=self._ref(task))

    def cancel_outcome(self, name: str, task: Ref[Task[T]]) -> Ref[TaskOutcome[T]]:
        self._effect('tasks')
        return self._add(name,'cancel_outcome',task=self._ref(task))

    def match_task(self, name: str, outcome: Ref[TaskOutcome[A]], *, completed: CheckedPlan[T],
                   failed: CheckedPlan[T], waiting: CheckedPlan[T], cancelled: CheckedPlan[T],
                   inputs: Inputs | None = None) -> Ref[T]:
        cases={}
        for state,plan in [('Completed',completed),('Failed',failed),('Waiting',waiting),('Cancelled',cancelled)]:
            branch=check_plan(plan.encoded,self.catalog)
            document=load_json(branch.encoded)
            for effect in document['uses']:
                self._effect(effect)
            cases[state]=document
        return self._add(name,'match_task',input=self._ref(outcome),cases=cases,
                         inputs={k:self._ref(v) for k,v in (inputs or {}).items()})

    def race(self, name: str, tasks: Sequence[Ref[Task[T]]]) -> Ref[T]:
        self._effect('tasks')
        return self._add(name,'race',tasks=[self._ref(t) for t in tasks])

    def close_scope(self, name: str, scope: Ref[Scope]) -> Ref[bool]:
        self._effect('tasks')
        return self._add(name,'close_scope',scope=self._ref(scope))

    def finish(self, result: Ref[T]) -> CheckedPlan[T]:
        self._check_catalog()
        document: dict[str, Any] = {'ir_version': 1, 'name': self.name,
                    'uses': self._effects if self._declared is None else self._declared,
                    'nodes': self._nodes, 'return': self._ref(result)}
        if self.pure:
            document['pure']=True
        return check_plan(document, self.catalog)
