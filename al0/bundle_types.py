"""Closed structural interfaces for affine result bundles."""
from __future__ import annotations
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from .ir import Type, Catalog
from .data import identifier
from .errors import TypeCheckError


def describe(typ: Type) -> dict[str, Any]:
    result={'kind':typ.kind,'name':typ.name,'predicates':sorted(typ.predicates),'binding':typ.binding}
    if typ.members:
        result['members']={name:describe(member) for name,member in typ.members}
    return result


def restore(record, *, depth=0):
    from .ir import Type
    if (depth>16 or type(record) is not dict or not {'kind','name','predicates','binding'}<=set(record)
            or set(record)-{'kind','name','predicates','binding','members'}
            or any(type(record[k]) is not str for k in ('kind','name','binding'))
            or type(record['predicates']) is not list or any(type(p) is not str for p in record['predicates'])
            or len(set(record['predicates']))!=len(record['predicates'])):
        raise TypeCheckError('invalid bundle type descriptor')
    members=record.get('members',{})
    if type(members) is not dict or len(members)>256:
        raise TypeCheckError('bundle interface requires at most 256 named fields')
    return Type(record['kind'],record['name'],frozenset(record['predicates']),record['binding'],
                tuple((identifier(k),restore(v,depth=depth+1)) for k,v in sorted(members.items())))


def _validate_leaf(name,member,fields,catalog):
    from .ir import TRANSFERABLE
    predicates={tool.predicate for tool in catalog.tools.values() if tool.kind=='check'}
    if member.members:
        raise TypeCheckError('only structural fields can contain nested members')
    if member.kind in TRANSFERABLE:
        catalog.require_type(member.name)
        if member.binding or (member.kind!='Checked' and member.predicates) or (
            member.kind=='Checked' and (not member.predicates or not member.predicates<=predicates)):
            raise TypeCheckError('bundle data field has invalid evidence requirements')
    elif member.kind=='Budget':
        if member.name!='Budget' or member.binding or member.predicates:
            raise TypeCheckError('invalid bundle Budget field')
    elif member.kind in ('Intent','Permit','Operation','Cap'):
        spec=catalog.tools.get(member.name)
        if spec is None or member.kind!='Cap' and spec.kind not in (('action','infer') if member.kind=='Operation' else ('action',)) or member.predicates:
            raise TypeCheckError('bundle resource field has an invalid tool contract')
        if member.kind=='Intent' and member.binding!=name:
            raise TypeCheckError('bundle Intent must bind its own field')
        if member.kind=='Permit':
            intent=fields.get(member.binding)
            if intent is None or intent.kind!='Intent' or intent.name!=member.name:
                raise TypeCheckError('bundle Permit requires its matching Intent field')
        elif member.kind!='Intent' and member.binding:
            raise TypeCheckError('unexpected bundle resource binding')
    else:
        raise TypeCheckError('unsupported bundle member kind')


def validate(typ: Type, catalog: Catalog, *, depth: int=0) -> Type:
    if depth>16 or typ.kind!='Bundle' or typ.name or typ.binding or typ.predicates or not typ.members:
        raise TypeCheckError('bundle requires a nonempty closed field interface')
    for name,member in typ.members:
        identifier(name)
        if member.kind=='Variant':
            validate_variant(member,catalog,depth=depth+1)
        elif member.kind=='Bundle':
            validate(member,catalog,depth=depth+1)
        else:
            _validate_leaf(name,member,dict(typ.members),catalog)
    if not any(is_affine(member) for _,member in typ.members):
        raise TypeCheckError('use a data record for a bundle without affine resources')
    return typ


def is_affine(typ: Type) -> bool:
    return typ.kind in ('Permit','Operation','Budget','Bundle') or (
        typ.kind=='Variant' and any(is_affine(member) for _,member in typ.members))


def is_pure_type(typ: Type) -> bool:
    return typ.kind in ('Data','Candidate','Observation','Checked') or (
        typ.kind=='Variant' and all(is_pure_type(member) for _,member in typ.members))


def controlled(typ: Type) -> bool:
    return not is_pure_type(typ)


def variant_type(cases: dict[str, Type]) -> Type:
    """Describe a closed interface without constructing or executing payloads."""
    from .ir import Type
    return Type('Variant',members=tuple(sorted(cases.items())))


def validate_variant(typ: Type, catalog: Catalog, *, depth: int=0) -> Type:
    from .ir import TRANSFERABLE
    if depth>16 or typ.kind!='Variant' or typ.name or typ.binding or typ.predicates or not 1<=len(typ.members)<=256:
        raise TypeCheckError('Variant requires a nonempty closed alternative interface')
    if len(dict(typ.members))!=len(typ.members):
        raise TypeCheckError('Variant alternatives must have distinct tags')
    for tag,member in typ.members:
        identifier(tag)
        if member.kind=='Variant':
            validate_variant(member,catalog,depth=depth+1)
        elif member.kind=='Bundle':
            validate(member,catalog,depth=depth+1)
        elif member.kind in TRANSFERABLE | {'Operation','Budget','Cap'}:
            _validate_leaf(tag,member,{},catalog)
        else:
            raise TypeCheckError('Variant payload requires a closed transferable type; pack Permit with Intent')
    return typ


def packed(fields,catalog):
    from dataclasses import replace
    from .ir import Type
    members=[]
    for name,typ in sorted(fields.items()):
        if typ.kind=='Intent':
            typ=replace(typ,binding=name)
        elif typ.kind=='Permit':
            targets=[key for key,value in fields.items() if value.kind=='Intent' and value.binding==typ.binding]
            if len(targets)!=1:
                raise TypeCheckError('pack each Permit with exactly one matching Intent field')
            typ=replace(typ,binding=targets[0])
        members.append((name,typ))
    return validate(Type('Bundle',members=tuple(members)),catalog)


def opened(typ,identity):
    from dataclasses import replace
    # A tagged identity cannot collide with an arbitrary user-chosen SSA string.
    return {name:replace(member,binding=('bundle-field',identity,member.binding))
            if member.kind in ('Intent','Permit') else member for name,member in typ.members}
