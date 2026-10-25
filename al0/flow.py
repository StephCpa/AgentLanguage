"""Conservative confidentiality restrictions and independent integrity taints.

None readers means public; an empty set permits no external recipient. Joining
intersects reader sets and unions taints. Checking a predicate never declassifies.
"""
from dataclasses import dataclass
from typing import Any

from .errors import FlowDenied, InvalidData


def names(values):
    if not isinstance(values,(list,tuple,set,frozenset)) or any(type(v) is not str or not v or len(v)>512 for v in values):
        raise InvalidData('flow policy names must be nonempty strings of at most 512 characters')
    return frozenset(values)


@dataclass(frozen=True)
class Label:
    readers: frozenset | None = None
    taints: frozenset = frozenset()

    def __post_init__(self):
        object.__setattr__(self,'readers',None if self.readers is None else names(self.readers))
        object.__setattr__(self,'taints',names(self.taints))

    def join(self, *others):
        readers,taints=self.readers,self.taints
        for other in others:
            if not isinstance(other,Label):
                raise InvalidData('expected an information-flow label')
            readers=other.readers if readers is None else readers if other.readers is None else readers & other.readers
            taints=taints | other.taints
        return Label(readers,taints)

    def to_data(self) -> dict[str, Any]:
        return {'readers':None if self.readers is None else sorted(self.readers),'taints':sorted(self.taints)}

    @classmethod
    def from_data(cls, value=None):
        if value is None:
            return cls()
        if type(value) is not dict or set(value)!={'readers','taints'}:
            raise InvalidData('flow label must contain readers and taints')
        if value['readers'] is not None and type(value['readers']) is not list:
            raise InvalidData('flow readers must be a list or null')
        if type(value['taints']) is not list:
            raise InvalidData('flow taints must be a list')
        return cls(value['readers'],value['taints'])

    def check_sink(self, recipients, accepted_taints=None):
        recipients=names(recipients)
        if self.readers is not None and not recipients<=self.readers:
            raise FlowDenied('recipient is outside the value and control-flow confidentiality policy')
        if accepted_taints is not None and not self.taints<=names(accepted_taints):
            raise FlowDenied('sink does not accept the value and control-flow integrity taints')


PUBLIC=Label()


def value_record(data, receipts=(), label=PUBLIC):
    result={'data':data,'receipts':list(receipts)}
    if label!=PUBLIC:
        result['label']=label.to_data()
    return result


def plan_label(plan, catalog):
    """A conservative bound across all statically embedded branches/tasks.

    A task's completion, failure and race eligibility may depend on any declared
    source, so its boundary result includes this bound, even for constant returns.
    """
    import json
    from .ir import check_plan
    document=json.loads(plan.encoded)
    result=PUBLIC
    def walk(doc):
        nonlocal result
        checked = check_plan(doc, catalog)
        def sources(typ):
            nonlocal result
            if typ.kind in ('Operation', 'OpRef', 'Snapshot'):
                result = result.join(catalog.tools[typ.name].output_label)
            for _, member in typ.members:
                sources(member)
        for _, typ in checked.node_types:
            sources(typ)
        for node in doc['nodes']:
            if node['op']=='restrict':
                result=result.join(Label.from_data(node['label']))
            if 'tool' in node:
                result=result.join(catalog.tools[node['tool']].output_label)
            if node['op'] in ('spawn','spawn_with','spawn_caps','call','repeat','invoke','invoke_caps'):
                walk(node['plan'])
            if node['op'] in ('match_task','if','match_admission','match_variant'):
                for branch in node['cases'].values():
                    walk(branch)
    walk(document)
    return result
