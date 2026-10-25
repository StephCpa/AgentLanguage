"""Versioned data-only IR and static checking for the first executable slice.

All records are SSA values. The checked core includes inference, observation,
trusted checking, affine authorization, exhaustive business-state matches and
structured child plans with explicit data inputs, inherited effects, typed
outcome branches and speculative policies.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from typing import Any, Generic, TypeVar

from .data import canonical, digest, identifier, load_json, quantity, resource_vector
from .errors import InvalidData, TypeCheckError
from .flow import Label, PUBLIC, names
from .pure import compute_type, derived_kind
from .limits import MAX_ITERATIONS
from .bundle_types import is_affine, is_pure_type, controlled
from .inference import ERROR_FIELDS, result_type as inference_type
from .inference_progress import progress_type
from .inference_admission import REJECTION_FIELDS, call_type
from .semantics import FLOW_SEMANTICS, EXECUTION_SEMANTICS


BUSINESS_STATES = frozenset({'NotDispatched', 'Pending', 'Unknown', 'Applied', 'ProvenNoEffect'})
TASK_OUTCOMES = frozenset({'Completed','Failed','Waiting','Cancelled'})
ADMISSION_CASES = frozenset({'Admitted','Returned','NoPermit'})
TRANSFERABLE = frozenset({'Data','Candidate','Observation','Checked'})
RESOURCES = frozenset({'Intent','Permit','Operation','OpRef','Budget','Cap','Bundle','Variant'})
MOVED_RESOURCES = frozenset({'Permit','Operation','Budget','Bundle'})
PURE_NODES=frozenset({'parameter','literal','restrict','compute','field','record','resources','if','call','repeat','variant','match_variant'})
PlanResult = TypeVar('PlanResult')


@dataclass(frozen=True)
class Tool:
    name: str
    version: str
    kind: str
    input_type: str
    output_type: str
    scope: str
    upper_bound_json: str
    predicates: tuple = ()
    predicate: str = ''
    retry_window: int = 0
    reconciles: str = ''
    shared_write: bool = False
    flow_json: str = ''

    @property
    def flow_policy(self):
        return json.loads(self.flow_json) if self.flow_json else {
            'recipients':['tool:'+self.name], 'accepted_taints':None,
            'output':self.default_output_label.to_data()}

    @property
    def default_output_label(self):
        return PUBLIC if self.kind=='check' else Label(taints={('model:' if self.kind=='infer' else 'external:')+self.name})

    @property
    def output_label(self):
        return Label.from_data(self.flow_policy['output'])

    @classmethod
    def define(cls, name: str, kind: str, input_type: str, output_type: str, scope: str,
               upper_bound: dict[str, int], *, version: str='1', predicates: tuple[str, ...] | list[str]=(),
               predicate: str='', retry_window: int=0, reconciles: str='', shared_write: bool | None=None,
               recipients: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None=None,
               output_label: Label | None=None,
               accepted_taints: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None=None) -> Tool:
        for value in (name, version, input_type, output_type, scope):
            identifier(value)
        if not isinstance(predicates,(tuple,list)):
            raise InvalidData('predicates must be a list or tuple of identifiers')
        for value in predicates:
            identifier(value)
        if len(set(predicates))!=len(predicates):
            raise InvalidData('predicate declarations cannot repeat')
        if type(predicate) is not str or type(reconciles) is not str:
            raise InvalidData('predicate and reconciliation target must be strings')
        if predicate:
            identifier(predicate)
        quantity(retry_window)
        if kind not in ('infer', 'observe', 'check', 'action', 'reconcile'):
            raise InvalidData('invalid tool kind')
        if kind == 'check' and (not predicate or output_type != 'Bool'):
            raise InvalidData('checker must name a predicate and return Bool')
        if kind == 'reconcile' and (not reconciles or input_type != 'Json' or output_type != 'Json'):
            raise InvalidData('reconciliation tool must name a target and use the query protocol')
        if kind != 'reconcile' and reconciles:
            raise InvalidData('only reconciliation tools can bind a target')
        resource_vector(upper_bound)
        shared_write = kind == 'action' if shared_write is None else shared_write
        if type(shared_write) is not bool:
            raise InvalidData('shared_write must be a boolean')
        base = cls(name, version, kind, input_type, output_type, scope,
                   canonical(upper_bound), tuple(predicates), predicate, retry_window, reconciles, shared_write)
        output_label=base.default_output_label if output_label is None else output_label
        if not isinstance(output_label,Label):
            raise InvalidData('tool output label must be a Label')
        policy={'recipients':sorted(names(['tool:'+name] if recipients is None else recipients)),
                'accepted_taints':None if accepted_taints is None else sorted(names(accepted_taints)),
                'output':output_label.to_data()}
        from dataclasses import replace
        return replace(base,flow_json='' if policy==base.flow_policy else canonical(policy))


class Catalog:
    def __init__(self) -> None:
        self._schemas: dict[str, str] = {}
        self.tools: dict[str, Tool] = {}

    def schema(self, name: str, fields: dict[str, str]) -> None:
        identifier(name)
        if name in ('Text', 'Int', 'Bool', 'Json', 'InferenceError', 'InferenceRejection') or name in self._schemas:
            raise InvalidData('type name already registered')
        if type(fields) is not dict or not fields:
            raise InvalidData('record schema needs fields')
        for key, field_type in fields.items():
            identifier(key)
            self.require_type(field_type)
        self._schemas[name] = canonical(fields)

    def require_type(self, name: str) -> None:
        if type(name) is not str or (name not in ('Text', 'Int', 'Bool', 'Json', 'InferenceError', 'InferenceRejection') and name not in self._schemas):
            raise TypeCheckError('unknown data type')

    def validate(self, name: str, value: Any) -> None:
        self.require_type(name)
        canonical(value)
        if name == 'Json':
            return
        primitive = {'Text': str, 'Int': int, 'Bool': bool}
        if name in primitive:
            if type(value) is not primitive[name]:
                raise InvalidData(f'value does not satisfy {name}')
            return
        fields = ERROR_FIELDS if name == 'InferenceError' else REJECTION_FIELDS if name == 'InferenceRejection' else json.loads(self._schemas[name])
        if type(value) is not dict or set(value) != set(fields):
            raise InvalidData('record fields do not match the declared schema')
        for field, field_type in fields.items():
            self.validate(field_type, value[field])

    def register(self, tool: Tool) -> None:
        self.require_type(tool.input_type)
        self.require_type(tool.output_type)
        if tool.name in self.tools:
            raise InvalidData('tool already registered')
        if tool.kind == 'reconcile':
            target = self.tools.get(tool.reconciles)
            if target is None or target.kind == 'reconcile' or target.scope != tool.scope:
                raise InvalidData('reconciler needs a registered target with exactly the same resource scope')
        self.tools[tool.name] = tool

    def record_fields(self, name: str) -> dict[str, str]:
        if name == 'InferenceError':
            return dict(ERROR_FIELDS)
        if name == 'InferenceRejection':
            return dict(REJECTION_FIELDS)
        if type(name) is not str or name not in self._schemas:
            raise TypeCheckError('field access and record construction require a registered record schema')
        return json.loads(self._schemas[name])

    def record_schemas(self) -> dict[str, dict[str, str]]:
        """Detached schema definitions for source/type generators."""
        return {name: self.record_fields(name) for name in self._schemas}

    def fingerprint(self) -> str:
        from dataclasses import asdict
        specs = {}
        for name, tool in self.tools.items():
            specs[name] = {**asdict(tool), 'predicates': list(tool.predicates)}
            if not tool.reconciles:
                # Omit default declarations; semantic changes use the version below.
                del specs[name]['reconciles']
            if tool.shared_write == (tool.kind == 'action'):
                del specs[name]['shared_write']
            if not tool.flow_json:
                del specs[name]['flow_json']
        # The new interpreter semantics must not silently resume a pre-label run.
        return digest({'flow_semantics':FLOW_SEMANTICS, 'execution_semantics':EXECUTION_SEMANTICS,
                       'schemas': self._schemas, 'tools': specs})


@dataclass(frozen=True)
class Type:
    kind: str
    name: str = ''
    predicates: frozenset = frozenset()
    binding: str | tuple = ''
    members: tuple = ()


@dataclass(frozen=True)
class CheckedPlan(Generic[PlanResult]):
    encoded: str
    catalog_digest: str
    result_type: Type | None = None
    parameters: tuple = ()
    node_types: tuple = ()
    pure: bool = False

    @property
    def fingerprint(self) -> str:
        return digest({'ir': load_json(self.encoded), 'catalog': self.catalog_digest})


FIELDS = {
    'variant': {'id','op','type','tag','input'},
    'match_variant': {'id','op','input','cases','inputs'},
    'pack': {'id','op','fields'},
    'unpack': {'id','op','input'},
    'take': {'id','op','input','field'},
    'resources': {'id','op','entries'},
    'parameter': {'id','op','type','kind','predicates'},
    'literal': {'id', 'op', 'type', 'value'},
    'restrict': {'id','op','input','label'},
    'compute': {'id','op','operator','inputs'},
    'field': {'id','op','input','field'},
    'record': {'id','op','type','fields'},
    'if': {'id','op','condition','cases','inputs'},
    'call': {'id','op','plan','inputs'},
    'invoke': {'id','op','plan','inputs','budget'},
    'invoke_caps': {'id','op','plan','inputs','budget','caps'},
    'capability': {'id','op','tool'},
    'attenuate': {'id','op','cap','expires','max_attempts','constraints'},
    'authorize_with': {'id','op','intent','cap'},
    'split_budget': {'id','op','amount','parent'},
    'join_budget': {'id','op','budget'},
    'repeat': {'id','op','count','limit','initial','plan','inputs'},
    'infer': {'id', 'op', 'tool', 'input'},
    'infer_result': {'id', 'op', 'tool', 'input'},
    'infer_progress': {'id', 'op', 'tool', 'input'},
    'try_infer': {'id', 'op', 'tool', 'input'},
    'resume_infer': {'id', 'op', 'operation'},
    'observe': {'id', 'op', 'tool', 'input'},
    'check': {'id', 'op', 'tool', 'input'},
    'prepare': {'id', 'op', 'tool', 'input'},
    'authorize': {'id', 'op', 'intent'},
    'admit': {'id', 'op', 'intent', 'permit'},
    'try_admit': {'id','op','intent','permit'},
    'match_admission': {'id','op','input','cases','inputs'},
    'borrow_operation': {'id','op','operation'},
    'inspect': {'id', 'op', 'operation'},
    'await': {'id', 'op', 'operation'},
    'reconcile': {'id', 'op', 'tool', 'operation'},
    'match_business': {'id', 'op', 'input', 'type', 'cases'},
    'scope': {'id', 'op', 'budget', 'speculative'},
    'spawn': {'id', 'op', 'scope', 'plan', 'budget'},
    'spawn_with': {'id','op','scope','plan','budget','inputs'},
    'spawn_caps': {'id','op','scope','plan','budget','inputs','caps'},
    'join': {'id', 'op', 'task'},
    'handoff': {'id', 'op', 'task'},
    'close_scope': {'id', 'op', 'scope'},
    'race': {'id', 'op', 'tasks'},
    'join_outcome': {'id','op','task'},
    'cancel_outcome': {'id','op','task'},
    'match_task': {'id','op','input','cases','inputs'},
}


def accepts(source, target):
    return (source.kind==target.kind and source.name==target.name and source.predicates>=target.predicates
            and source.members==target.members)


def check_plan(document: str | dict[str, Any], catalog: Catalog, *, speculative: bool=False,
               _depth: int=0, _count: list[int] | None=None, _pure: bool=False, _partial: bool=False) -> CheckedPlan[Any]:
    if _depth > 16:
        raise TypeCheckError('task nesting exceeds 16 levels')
    if isinstance(document, str) and len(document.encode('utf-8')) > 1024 * 1024:
        raise TypeCheckError('IR document exceeds the 1 MiB input limit')
    raw = load_json(document) if isinstance(document, str) else load_json(canonical(document))
    if len(canonical(raw).encode('utf-8')) > 1024 * 1024:
        raise TypeCheckError('IR document exceeds the 1 MiB input limit')
    fields={'ir_version','name','uses','nodes','return'}
    if type(raw) is not dict or set(raw) not in (fields,fields|{'pure'}):
        raise TypeCheckError('invalid IR envelope')
    if 'pure' in raw and raw['pure'] is not True:
        raise TypeCheckError('pure function marker must be true')
    pure=_pure or raw.get('pure',False)
    if type(raw['ir_version']) is not int or raw['ir_version'] != 1:
        raise TypeCheckError('unsupported IR version')
    identifier(raw['name'])
    if type(raw['uses']) is not list or any(type(x) is not str for x in raw['uses']):
        raise TypeCheckError('uses must be a list of effect names')
    uses = set(raw['uses'])
    if pure and uses:
        raise TypeCheckError('pure functions cannot declare external effects')
    if len(uses) != len(raw['uses']):
        raise TypeCheckError('duplicate declared effect')
    if type(raw['nodes']) is not list or not 0 < len(raw['nodes']) <= 10000:
        raise TypeCheckError('plan must contain 1 to 10000 nodes')
    count = [0] if _count is None else _count
    count[0] += len(raw['nodes'])
    if count[0] > 10000:
        raise TypeCheckError('combined parent and child plans exceed 10000 nodes')
    allowed_effects = {'tasks','budget'}
    write_effects = set()
    for spec in catalog.tools.values():
        allowed_effects.add(f'cap:{spec.name}@{spec.scope}')
        if spec.kind == 'action':
            allowed_effects.update({f'authorize:{spec.name}@{spec.scope}', f'admit:{spec.name}@{spec.scope}'})
            if spec.shared_write:
                write_effects.update({f'authorize:{spec.name}@{spec.scope}', f'admit:{spec.name}@{spec.scope}'})
        else:
            allowed_effects.add(f'{spec.kind}:{spec.name}')
            if spec.shared_write:
                write_effects.add(f'{spec.kind}:{spec.name}')
    if not uses <= allowed_effects:
        raise TypeCheckError('unknown effect declaration')
    if speculative and uses & write_effects:
        raise TypeCheckError('speculative plans cannot request shared external writes')
    types, consumed, pending_statuses, pending_operations = {}, set(), {}, set()
    scopes, task_results, task_scopes = {}, {}, {}
    parameters, outcome_results, pending_outcomes = {}, {}, set()
    pending_admissions=set()
    pending_budgets=set()
    pending_bundles=set()
    unpacked={}
    for node in raw['nodes']:
        if type(node) is not dict or type(node.get('op')) is not str or node['op'] not in FIELDS or set(node) != FIELDS[node['op']]:
            raise TypeCheckError('invalid or unsupported IR node')
        name, op = identifier(node['id']), node['op']
        if pure and op not in PURE_NODES:
            raise TypeCheckError('operation is not available in a pure function',node=name)
        if name in types:
            raise TypeCheckError('SSA node identity repeated', node=name)

        def reference(ref, allowed):
            if type(ref) is not str or ref not in types:
                raise TypeCheckError('reference must name an earlier node', node=name)
            if ref in consumed:
                raise TypeCheckError('AL1001: affine handle already moved', node=name)
            typ = types[ref]
            if typ.kind not in allowed:
                raise TypeCheckError('value lacks the required type or evidence', node=name)
            return typ

        def value(key, allowed):
            return reference(node[key],allowed)

        def effect(e):
            if e not in uses:
                raise TypeCheckError('effect is outside the declared boundary', node=name)

        def tool():
            if type(node['tool']) is not str or node['tool'] not in catalog.tools:
                raise TypeCheckError('tool is not in the trusted catalog', node=name)
            return catalog.tools[node['tool']]

        def budget_amount(key):
            if type(node[key]) is str:
                typ=value(key,TRANSFERABLE)
                if typ.name!='Json' and any(field!='Int' for field in catalog.record_fields(typ.name).values()):
                    raise TypeCheckError('dynamic budget requires Json or a record of Int quantities',node=name)
            else:
                resource_vector(node[key])

        if op == 'parameter':
            if (type(node['kind']) is not str or node['kind'] not in TRANSFERABLE | RESOURCES or type(node['predicates']) is not list
                    or any(type(p) is not str for p in node['predicates'])):
                raise TypeCheckError('parameter must name a transferable data/evidence type',node=name)
            if node['kind']=='Variant':
                from .bundle_types import restore, validate_variant
                result=validate_variant(restore(node['type']),catalog)
                if node['predicates'] or pure and not is_pure_type(result):
                    raise TypeCheckError('Variant parameter has invalid predicates or is not pure',node=name)
                parameters[name]=result
            elif node['kind'] in RESOURCES:
                if pure or node['predicates']:
                    raise TypeCheckError('resource parameters require an effectful branch and no predicates',node=name)
                if node['kind']=='Bundle':
                    from .bundle_types import restore, validate
                    result=validate(restore(node['type']),catalog)
                    pending_bundles.add(name)
                elif node['kind']=='Budget':
                    if node['type']!='Budget':
                        raise TypeCheckError('budget parameter requires the Budget type',node=name)
                    result=Type('Budget','Budget')
                    pending_budgets.add(name)
                elif node['kind']=='Permit':
                    intent=reference(node['type'],{'Intent'})
                    if node['type'] not in parameters:
                        raise TypeCheckError('permit parameter must bind an earlier intent parameter',node=name)
                    result=Type('Permit',intent.name,binding=intent.binding)
                else:
                    spec=catalog.tools.get(node['type']) if type(node['type']) is str else None
                    if spec is None or node['kind']!='Cap' and spec.kind not in (('action','infer') if node['kind'] in ('Operation','OpRef') else ('action',)):
                        raise TypeCheckError('resource parameter names an incompatible tool contract',node=name)
                    result=Type(node['kind'],spec.name,binding=name if node['kind']=='Intent' else '')
                    if node['kind']=='Operation':
                        pending_operations.add(name)
                parameters[name]=result
                types[name]=result
                continue
            else:
                catalog.require_type(node['type'])
                predicates=frozenset(node['predicates'])
                known={t.predicate for t in catalog.tools.values() if t.kind=='check'}
                if (len(predicates)!=len(node['predicates']) or not predicates<=known
                        or (node['kind']!='Checked' and predicates) or (node['kind']=='Checked' and not predicates)):
                    raise TypeCheckError('parameter predicates must describe registered check evidence',node=name)
                result=Type(node['kind'],node['type'],predicates)
                parameters[name]=result
        elif op == 'restrict':
            result=value('input',TRANSFERABLE | {'Variant'})
            if is_affine(result):
                consumed.add(node['input'])
            Label.from_data(node['label'])
        elif op=='variant':
            from .bundle_types import restore, validate_variant
            result=validate_variant(restore(node['type']),catalog)
            if pure and not is_pure_type(result):
                raise TypeCheckError('pure Variant cannot contain authority or resources',node=name)
            if type(node['tag']) is not str or node['tag'] not in dict(result.members):
                raise TypeCheckError('Variant tag is absent from its closed interface',node=name)
            payload=value('input',TRANSFERABLE | RESOURCES)
            if not accepts(payload,dict(result.members)[node['tag']]):
                raise TypeCheckError('Variant payload differs from the declared alternative',node=name)
            if is_affine(payload):
                if node['input'] in pending_statuses.values():
                    raise TypeCheckError('handle outstanding snapshots before moving their operation',node=name)
                consumed.add(node['input'])
                pending_operations.discard(node['input'])
                pending_budgets.discard(node['input'])
        elif op == 'compute':
            if type(node['inputs']) is not list:
                raise TypeCheckError('pure operator inputs must be a list of references',node=name)
            arguments=[reference(ref,TRANSFERABLE) for ref in node['inputs']]
            try:
                kind,schema=compute_type(node['operator'],arguments)
            except TypeCheckError as error:
                raise TypeCheckError(str(error),node=name) from error
            result=Type(kind,schema)
        elif op=='resources':
            if type(node['entries']) is not dict:
                raise TypeCheckError('resources requires named Int references',node=name)
            quantities=[]
            for resource,ref in node['entries'].items():
                identifier(resource)
                typ=reference(ref,TRANSFERABLE)
                if typ.name!='Int':
                    raise TypeCheckError('resource quantity must have Int schema',node=name)
                quantities.append(typ)
            result=Type(derived_kind(quantities),'Json')
        elif op=='field':
            source=value('input',TRANSFERABLE)
            fields=catalog.record_fields(source.name)
            if type(node['field']) is not str or node['field'] not in fields:
                raise TypeCheckError('field is absent from the registered schema',node=name)
            result=Type(derived_kind([source]),fields[node['field']])
        elif op=='pack':
            from .bundle_types import packed
            if type(node['fields']) is not dict or not node['fields']:
                raise TypeCheckError('pack requires named fields',node=name)
            captured={key:reference(ref,TRANSFERABLE | RESOURCES-{'OpRef'}) for key,ref in node['fields'].items()}
            moving=[node['fields'][key] for key,typ in captured.items() if is_affine(typ)]
            if len(moving)!=len(set(moving)):
                raise TypeCheckError('affine resource cannot be packed under multiple names',node=name)
            if any(ref in pending_statuses.values() for ref in moving):
                raise TypeCheckError('handle outstanding snapshots before packing their operation',node=name)
            result=packed(captured,catalog)
            consumed.update(moving)
            pending_operations.difference_update(moving)
            pending_budgets.difference_update(moving)
        elif op=='unpack':
            from .bundle_types import opened
            packet=value('input',{'Bundle'})
            consumed.add(node['input'])
            unpacked[name]=opened(packet,name)
            result=Type('Unpacked',binding=name,members=packet.members)
        elif op=='take':
            packet=value('input',{'Unpacked'})
            if type(node['field']) is not str or node['field'] not in unpacked[packet.binding]:
                raise TypeCheckError('bundle field is absent or already taken',node=name)
            result=unpacked[packet.binding].pop(node['field'])
            if result.kind=='Operation':
                pending_operations.add(name)
            elif result.kind=='Budget':
                pending_budgets.add(name)
        elif op=='record':
            fields=catalog.record_fields(node['type'])
            if type(node['fields']) is not dict or set(node['fields'])!=set(fields):
                raise TypeCheckError('record construction must supply exactly the schema fields',node=name)
            members=[reference(node['fields'][key],TRANSFERABLE) for key in fields]
            if any(member.name!=schema for member,schema in zip(members,fields.values())):
                raise TypeCheckError('record field schema mismatch',node=name)
            result=Type(derived_kind(members),node['type'])
        elif op=='capability':
            spec=tool()
            effect(f'cap:{spec.name}@{spec.scope}')
            result=Type('Cap',spec.name)
        elif op=='attenuate':
            cap=value('cap',{'Cap'})
            spec=catalog.tools[cap.name]
            effect(f'cap:{spec.name}@{spec.scope}')
            if node['expires'] is not None:
                quantity(node['expires'])
            if node['max_attempts'] is not None:
                quantity(node['max_attempts'])
                if node['max_attempts']<1:
                    raise TypeCheckError('capability attempt limit must be positive',node=name)
            constraints=node['constraints']
            if type(constraints) is not dict:
                raise TypeCheckError('capability constraints must be a field equality map',node=name)
            for field,constraint in constraints.items():
                identifier(field)
                if spec.input_type!='Json':
                    fields=catalog.record_fields(spec.input_type)
                    if field not in fields:
                        raise TypeCheckError('capability constraint names an unknown input field',node=name)
                    catalog.validate(fields[field],constraint)
            result=cap
        elif op=='split_budget':
            effect('budget')
            budget_amount('amount')
            if node['parent'] is not None:
                value('parent',{'Budget'})
            result=Type('Budget','Budget')
            pending_budgets.add(name)
        elif op=='join_budget':
            effect('budget')
            value('budget',{'Budget'})
            consumed.add(node['budget'])
            pending_budgets.discard(node['budget'])
            result=Type('Data','Bool')
        elif op in ('invoke','invoke_caps'):
            body=check_plan(node['plan'],catalog,speculative=speculative,_depth=_depth+1,_count=count)
            if body.pure:
                raise TypeCheckError('invoke requires a workflow; use call for pure functions',node=name)
            if not set(load_json(body.encoded)['uses'])<=uses:
                raise TypeCheckError('workflow effects exceed the parent boundary',node=name)
            if type(node['inputs']) is not dict or set(node['inputs'])!=set(dict(body.parameters)):
                raise TypeCheckError('invoke must bind exactly the workflow parameters',node=name)
            captured={key:reference(ref,TRANSFERABLE | RESOURCES) for key,ref in node['inputs'].items()}
            moving=[node['inputs'][key] for key,typ in captured.items() if is_affine(typ)]
            if len(moving)!=len(set(moving)):
                raise TypeCheckError('affine resource cannot be captured under multiple names',node=name)
            if any(ref in pending_statuses.values() for ref in moving):
                raise TypeCheckError('handle outstanding snapshots before moving their operation',node=name)
            if node['budget'] is not None:
                value('budget',{'Budget'})
                if node['budget'] in moving:
                    raise TypeCheckError('cannot move the same budget that funds this invocation',node=name)
            if op=='invoke_caps':
                if type(node['caps']) is not dict:
                    raise TypeCheckError('invoke_caps requires named capability bindings',node=name)
                for tool_name,ref in node['caps'].items():
                    cap=reference(ref,{'Cap'})
                    if cap.name!=tool_name:
                        raise TypeCheckError('invocation capability covers another tool',node=name)
            for parameter,typ in body.parameters:
                source=captured[parameter]
                if not accepts(source,typ):
                    raise TypeCheckError('workflow parameter kind, schema or evidence mismatch',node=name)
                if typ.kind=='Permit':
                    intent=captured.get(typ.binding)
                    if intent is None or intent.kind!='Intent' or source.binding!=intent.binding:
                        raise TypeCheckError('captured permit is bound to another intent',node=name)
            consumed.update(moving)
            pending_operations.difference_update(moving)
            pending_budgets.difference_update(moving)
            result=body.result_type
            if result.kind=='Operation':
                pending_operations.add(name)
            elif result.kind=='Budget':
                pending_budgets.add(name)
        elif op in ('call','repeat'):
            body=check_plan(node['plan'],catalog,speculative=speculative,_depth=_depth+1,_count=count,_pure=pure or op=='call')
            if op=='call' and not load_json(body.encoded).get('pure',False):
                raise TypeCheckError('call requires a declared pure function',node=name)
            if not is_pure_type(body.result_type) and not (op=='repeat' and not pure and is_affine(body.result_type)):
                raise TypeCheckError('function and loop results must be transferable values',node=name)
            if not set(load_json(body.encoded)['uses'])<=uses:
                raise TypeCheckError('loop body effects exceed the parent boundary',node=name)
            body_parameters=dict(body.parameters)
            if type(node['inputs']) is not dict:
                raise TypeCheckError('call and loop captures must be named references',node=name)
            if op=='repeat':
                count_type=value('count',TRANSFERABLE)
                state=value('initial',TRANSFERABLE | {'Variant','Bundle','Operation','Budget'})
                if not is_pure_type(state) and not is_affine(state):
                    raise TypeCheckError('loop state must be pure data or a closed affine interface',node=name)
                if count_type.name!='Int' or type(node['limit']) is not int or not 0<=node['limit']<=MAX_ITERATIONS:
                    raise TypeCheckError('repeat requires an Int count and a static bound from 0 to 1000000',node=name)
                if (body_parameters.pop('index',None)!=Type('Data','Int') or body_parameters.pop('state',None)!=state
                        or body.result_type!=state):
                    raise TypeCheckError('repeat body needs index: Int, invariant state and the same result type',node=name)
                if is_affine(state):
                    if node['initial'] in pending_statuses.values():
                        raise TypeCheckError('handle outstanding snapshots before moving loop state',node=name)
                    consumed.add(node['initial'])
                    pending_operations.discard(node['initial'])
                    pending_budgets.discard(node['initial'])
            if set(node['inputs'])!=set(body_parameters):
                raise TypeCheckError('call or loop must bind exactly the declared captures',node=name)
            for parameter,typ in body_parameters.items():
                if not is_pure_type(typ) or not accepts(reference(node['inputs'][parameter],TRANSFERABLE | {'Variant'}),typ):
                    raise TypeCheckError('call or loop input schema, kind or evidence mismatch',node=name)
            result=body.result_type
            if result.kind=='Operation':
                pending_operations.add(name)
            elif result.kind=='Budget':
                pending_budgets.add(name)
        elif op == 'scope':
            effect('tasks')
            budget_amount('budget')
            if type(node['speculative']) is not bool:
                raise TypeCheckError('scope speculative flag must be boolean', node=name)
            scopes[name] = speculative or node['speculative']
            result = Type('Scope', binding=name)
        elif op in ('spawn','spawn_with','spawn_caps'):
            effect('tasks')
            owner = value('scope', {'Scope'})
            budget_amount('budget')
            child = check_plan(node['plan'], catalog, speculative=scopes[owner.binding], _depth=_depth+1, _count=count)
            inputs=node.get('inputs',{})
            if type(inputs) is not dict or set(inputs)!=set(dict(child.parameters)):
                raise TypeCheckError('spawn must bind exactly the declared child parameters',node=name)
            captured={key:reference(ref,TRANSFERABLE | MOVED_RESOURCES | {'Cap','Intent','Variant'}) for key,ref in inputs.items()}
            moving=[inputs[key] for key,typ in captured.items() if is_affine(typ)]
            if len(moving)!=len(set(moving)):
                raise TypeCheckError('affine resource cannot be captured under multiple names',node=name)
            if any(ref in pending_statuses.values() for ref in moving):
                raise TypeCheckError('handle outstanding snapshots before moving their operation',node=name)
            for parameter,typ in child.parameters:
                source=captured[parameter]
                if not accepts(source,typ):
                    raise TypeCheckError('child input kind, schema or check evidence mismatch',node=name)
                if typ.kind=='Permit':
                    intent=captured.get(typ.binding)
                    if intent is None or intent.kind!='Intent' or source.binding!=intent.binding:
                        raise TypeCheckError('captured permit is bound to another intent',node=name)
            if not set(load_json(child.encoded)['uses']) <= uses:
                raise TypeCheckError('child effects exceed the parent effect boundary', node=name)
            if op=='spawn_caps':
                if type(node['caps']) is not dict:
                    raise TypeCheckError('spawn_caps requires named capability bindings',node=name)
                for tool_name,ref in node['caps'].items():
                    cap=reference(ref,{'Cap'})
                    if cap.name!=tool_name:
                        raise TypeCheckError('task capability covers another tool',node=name)
            if child.result_type.kind not in TRANSFERABLE | {'Cap','Operation','Budget','Bundle','Variant'}:
                raise TypeCheckError('child plan must return data, evidence, Cap, Operation, Budget or Bundle', node=name)
            consumed.update(moving)
            pending_operations.difference_update(moving)
            pending_budgets.difference_update(moving)
            task_results[name] = child.result_type
            task_scopes[name] = owner.binding
            result = Type('Task', binding=owner.binding)
        elif op in ('join', 'handoff','join_outcome','cancel_outcome'):
            effect('tasks')
            value('task', {'Task'})
            consumed.add(node['task'])
            if op in ('join_outcome','cancel_outcome'):
                outcome_results[name]=task_results[node['task']]
                pending_outcomes.add(name)
                result=Type('TaskOutcome',binding=name)
            else:
                result = task_results[node['task']] if op == 'join' else Type('Data', 'Json')
                if result.kind=='Operation':
                    pending_operations.add(name)
                elif result.kind=='Budget':
                    pending_budgets.add(name)
        elif op in ('match_task','if','match_admission','match_variant'):
            if op=='match_variant':
                variant=value('input',{'Variant'})
                if pure and not is_pure_type(variant):
                    raise TypeCheckError('pure matching requires pure Variant alternatives',node=name)
                cases=set(dict(variant.members))
            elif op=='match_admission':
                admission=value('input',{'Admission'})
                cases=ADMISSION_CASES
            elif op=='match_task':
                outcome=value('input',{'TaskOutcome'})
                cases=TASK_OUTCOMES
            else:
                condition=value('condition',TRANSFERABLE)
                if condition.name!='Bool':
                    raise TypeCheckError('if condition must have Bool schema',node=name)
                cases={'Then','Else'}
            if type(node['cases']) is not dict or set(node['cases'])!=cases:
                raise TypeCheckError('branch must cover exactly its declared alternatives',node=name)
            captures=node['inputs']
            reserved={'result','reason'} if op=='match_admission' else {'result'} if op in ('match_task','match_variant') else set()
            if type(captures) is not dict or reserved & set(captures):
                raise TypeCheckError('branch inputs must be named captures; outcome bindings are reserved',node=name)
            captured={key:reference(ref,TRANSFERABLE | RESOURCES) for key,ref in captures.items()}
            moving=[captures[key] for key,typ in captured.items() if is_affine(typ)]
            if len(moving)!=len(set(moving)):
                raise TypeCheckError('affine resource cannot be captured under multiple names',node=name)
            if op=='match_variant' and is_affine(variant) and node['input'] in moving:
                raise TypeCheckError('cannot capture the same Variant whose payload is being matched',node=name)
            if any(ref in pending_statuses.values() for ref in moving):
                raise TypeCheckError('handle outstanding snapshots before moving their operation',node=name)
            used=set()
            result=None
            for state,document in node['cases'].items():
                branch=check_plan(document,catalog,speculative=speculative,_depth=_depth+1,_count=count,_pure=pure)
                if not set(load_json(branch.encoded)['uses'])<=uses:
                    raise TypeCheckError('branch effects exceed the parent effect boundary',node=name)
                branch_parameters=dict(branch.parameters)
                if op=='match_variant' and 'result' not in branch_parameters:
                    raise TypeCheckError('Variant alternative must explicitly receive its payload',node=name)
                if op=='match_admission' and state in ('Admitted','Returned') and 'result' not in branch_parameters:
                    raise TypeCheckError('admission alternative must explicitly receive its operation or returned permit',node=name)
                if (op=='match_task' and state=='Completed' and is_affine(outcome_results[outcome.binding])
                        and 'result' not in branch_parameters):
                    raise TypeCheckError('completed alternative must explicitly receive its resource result',node=name)
                if any(key not in branch_parameters for key,typ in captured.items() if is_affine(typ)):
                    raise TypeCheckError('every alternative must explicitly receive each moved resource',node=name)
                for parameter,typ in branch.parameters:
                    if op=='match_admission' and parameter in ('result','reason'):
                        if parameter=='reason':
                            if state=='Admitted':
                                raise TypeCheckError('successful admission has no rejection reason',node=name)
                            source=Type('Data','Json')
                        else:
                            source=(Type('Operation',admission.name) if state=='Admitted' else
                                    Type('Permit',admission.name,binding=admission.binding) if state=='Returned' else Type('Data','Json'))
                    elif op=='match_variant' and parameter=='result':
                        source=dict(variant.members)[state]
                    elif op=='match_task' and parameter=='result':
                        source=outcome_results[outcome.binding] if state=='Completed' else Type('Data','Json')
                    else:
                        if parameter not in captured:
                            raise TypeCheckError('branch parameter has no explicit capture',node=name)
                        used.add(parameter)
                        source=captured[parameter]
                    if not accepts(source,typ):
                        raise TypeCheckError('branch parameter cannot claim a different outcome/evidence type',node=name)
                    if typ.kind=='Permit':
                        intent=captured.get(typ.binding)
                        if intent is None or intent.kind!='Intent' or source.binding!=intent.binding:
                            raise TypeCheckError('captured permit is bound to another intent',node=name)
                if result is not None and result!=branch.result_type:
                    raise TypeCheckError('branches must return the same type and evidence',node=name)
                result=branch.result_type
            if used!=set(captures):
                raise TypeCheckError('branch capture does not bind a declared parameter',node=name)
            consumed.update(moving)
            pending_operations.difference_update(moving)
            pending_budgets.difference_update(moving)
            if op=='match_variant' and is_affine(variant):
                consumed.add(node['input'])
            if op=='match_task':
                pending_outcomes.discard(node['input'])
                if is_affine(outcome_results[outcome.binding]):
                    consumed.add(node['input'])
            elif op=='match_admission':
                pending_admissions.discard(node['input'])
                consumed.add(node['input'])
            if result.kind=='Operation':
                pending_operations.add(name)
            elif result.kind=='Budget':
                pending_budgets.add(name)
        elif op == 'race':
            effect('tasks')
            refs = node['tasks']
            if type(refs) is not list or len(refs) < 2 or any(type(r) is not str for r in refs) or len(set(refs)) != len(refs):
                raise TypeCheckError('race needs at least two distinct task handles', node=name)
            for ref in refs:
                if ref not in task_results or ref in consumed:
                    raise TypeCheckError('race requires unmoved task handles', node=name)
            owner = task_scopes[refs[0]]
            if not scopes[owner] or any(task_scopes[r] != owner for r in refs):
                raise TypeCheckError('race candidates must belong to the same speculative scope', node=name)
            result = task_results[refs[0]]
            if any(task_results[r] != result for r in refs):
                raise TypeCheckError('race candidates must return the same data and evidence type', node=name)
            consumed.update(refs)
            if result.kind=='Operation':
                pending_operations.add(name)
            elif result.kind=='Budget':
                pending_budgets.add(name)
        elif op == 'close_scope':
            effect('tasks')
            owner = value('scope', {'Scope'})
            if any(s == owner.binding and t not in consumed for t, s in task_scopes.items()):
                raise TypeCheckError('scope still owns an unjoined task handle', node=name)
            consumed.add(node['scope'])
            result = Type('Data', 'Bool')
        elif op == 'literal':
            catalog.validate(node['type'], node['value'])
            result = Type('Data', node['type'])
        elif op in ('infer', 'infer_result', 'infer_progress', 'try_infer', 'observe', 'check'):
            spec = tool()
            kind = 'infer' if op in ('infer_result','infer_progress','try_infer') else op
            if spec.kind != kind:
                raise TypeCheckError('tool kind does not match the requested operation', node=name)
            source = value('input', {'Data', 'Candidate', 'Observation', 'Checked'})
            if source.name != spec.input_type:
                raise TypeCheckError('tool input schema mismatch', node=name)
            effect(f'{kind}:{spec.name}')
            if op == 'check':
                result = Type('Checked', source.name, source.predicates | {spec.predicate})
            elif op == 'try_infer':
                result = call_type(spec.name, spec.output_type)
            elif op == 'infer_progress':
                result = progress_type(spec.name, spec.output_type)
            elif op == 'infer_result':
                result = inference_type(spec.output_type)
            else:
                result = Type('Candidate' if op == 'infer' else 'Observation', spec.output_type)
        elif op == 'resume_infer':
            operation=value('operation',{'Operation'})
            spec=catalog.tools[operation.name]
            if spec.kind!='infer':
                raise TypeCheckError('resume_infer requires a model operation',node=name)
            if node['operation'] in pending_statuses.values():
                raise TypeCheckError('handle outstanding snapshots before resuming inference',node=name)
            effect(f'infer:{spec.name}')
            consumed.add(node['operation'])
            pending_operations.discard(node['operation'])
            result=progress_type(spec.name,spec.output_type)
        elif op == 'prepare':
            spec = tool()
            if spec.kind != 'action':
                raise TypeCheckError('prepare requires an action tool', node=name)
            source = value('input', {'Data', 'Checked'})
            if source.name != spec.input_type or not set(spec.predicates) <= source.predicates:
                raise TypeCheckError('action lacks required artifact checks', node=name)
            result = Type('Intent', spec.name, binding=name)
        elif op in ('authorize','authorize_with'):
            intent = value('intent', {'Intent'})
            spec = catalog.tools[intent.name]
            if op=='authorize_with' and value('cap',{'Cap'}).name!=intent.name:
                raise TypeCheckError('authorization capability covers another tool',node=name)
            effect(f'authorize:{spec.name}@{spec.scope}')
            result = Type('Permit', intent.name, binding=intent.binding)
        elif op in ('admit','try_admit'):
            intent = value('intent', {'Intent'})
            permit = value('permit', {'Permit'})
            if intent.binding != permit.binding:
                raise TypeCheckError('permit is bound to another intent', node=name)
            spec = catalog.tools[intent.name]
            effect(f'admit:{spec.name}@{spec.scope}')
            consumed.add(node['permit'])
            if op=='try_admit':
                result=Type('Admission',intent.name,binding=intent.binding)
                pending_admissions.add(name)
            else:
                result = Type('Operation', intent.name)
                pending_operations.add(name)
        elif op=='borrow_operation':
            operation=value('operation',{'Operation','OpRef'})
            result=Type('OpRef',operation.name)
        elif op in ('inspect', 'await', 'reconcile'):
            operation = value('operation', {'Operation','OpRef'} if op=='inspect' else {'Operation'})
            if op == 'await' and catalog.tools[operation.name].kind == 'infer':
                raise TypeCheckError('model operations must use resume_infer to advance observations', node=name)
            if op == 'reconcile':
                spec = tool()
                if spec.kind != 'reconcile' or spec.reconciles != operation.name:
                    raise TypeCheckError('reconciliation route does not match operation', node=name)
                effect(f'reconcile:{spec.name}')
            result = Type('Snapshot', operation.name)
            pending_statuses[name] = node['operation']
        elif op == 'match_business':
            value('input', {'Snapshot'})
            if type(node['cases']) is not dict or set(node['cases']) != BUSINESS_STATES:
                raise TypeCheckError('AL3002: all business states including Unknown must be handled', node=name)
            for branch in node['cases'].values():
                catalog.validate(node['type'], branch)
            pending_operations.discard(pending_statuses.pop(node['input'], None))
            result = Type('Data', node['type'])
        types[name] = result
        if result.kind=='Bundle' or result.kind=='Variant' and is_affine(result):
            pending_bundles.add(name)
        pending_bundles.difference_update(consumed)
    if type(raw['return']) is not str or raw['return'] not in types:
        raise TypeCheckError('return must reference a defined node')
    if raw['return'] in consumed:
        raise TypeCheckError('AL1001: cannot return an affine handle already moved')
    if _partial:
        return CheckedPlan(canonical(raw),catalog.fingerprint(),types[raw['return']],tuple(parameters.items()),tuple(types.items()),pure)
    if types[raw['return']].kind not in ('Data', 'Candidate', 'Observation', 'Checked', 'Operation','OpRef','Budget','Cap','Bundle','Variant'):
        raise TypeCheckError('return a handled value or operation reference, not a permit or unchecked status')
    pending_operations.discard(raw['return'])
    pending_budgets.discard(raw['return'])
    pending_bundles.discard(raw['return'])
    if pending_bundles or any(fields for fields in unpacked.values()):
        raise TypeCheckError('every bundle must be moved, returned or completely unpacked')
    if pending_budgets:
        raise TypeCheckError('every budget must be joined, moved or returned explicitly')
    if pending_statuses or pending_operations:
        raise TypeCheckError('AL3002: operation results must be handled or returned explicitly')
    if pending_outcomes:
        raise TypeCheckError('TaskOutcome must be exhaustively matched')
    if pending_admissions:
        raise TypeCheckError('Admission must be exhaustively matched exactly once')
    if any(s not in consumed for s in scopes) or any(t not in consumed for t in task_results):
        raise TypeCheckError('every task must be joined or handed off and every scope closed')
    return CheckedPlan(canonical(raw), catalog.fingerprint(), types[raw['return']],tuple(parameters.items()),tuple(types.items()),pure)
