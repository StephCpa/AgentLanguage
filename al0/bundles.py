"""Bundle value codecs. Descriptors never replace transactional custody checks."""
from .data import canonical
from .custody import ResourceRef
from .errors import InvalidData, StateConflict


def record_refs(record,path=()):
    result={}
    if 'resource' in record:
        result[canonical(list(path))]=ResourceRef(**record['resource'])
    if 'bundle' in record:
        for name,member in record['data'].items():
            result.update(record_refs(member,path+(name,)))
    if 'variant' in record:
        data=record['data']
        result.update(record_refs(data['payload'],path+({'variant':data['tag']},)))
    refs=list(result.values())
    if len({(ref.kind,ref.resource) for ref in refs})!=len(refs):
        raise StateConflict('bundle contains repeated resource aliases')
    return result


def replace_record_refs(record,refs):
    """Apply a fixed transfer receipt by resource identity, not by live lookup."""
    result=dict(record)
    if 'resource' in record:
        source=ResourceRef(**record['resource'])
        target=refs.get((source.kind,source.resource))
        if target is None:
            raise StateConflict('bundle is missing a resource transfer receipt')
        result['resource']=target.record()
    if 'bundle' in record:
        result['data']={name:replace_record_refs(member,refs) for name,member in record['data'].items()}
    if 'variant' in record:
        result['data']={'tag':record['data']['tag'],'payload':replace_record_refs(record['data']['payload'],refs)}
    return result


def value_refs(value,path=()):
    return record_refs(value.record(),path)


def return_refs(value):
    return record_return_refs(value.record())


def record_return_refs(record):
    return record_refs(record) if 'bundle' in record or 'variant' in record else {} if 'resource' not in record else {'result':ResourceRef(**record['resource'])}


def accept_record(record,received):
    expected=record_return_refs(record)
    if set(expected)!=set(received) or any((ref.kind,ref.resource)!=(received[key].kind,received[key].resource)
                                          for key,ref in expected.items()):
        raise StateConflict('task result differs from its accepted resource package')
    return replace_record_refs(record,{(ref.kind,ref.resource):ref for ref in received.values()})


def accept_result(value,received):
    from .execute import Value
    return Value.from_record(accept_record(value.record(),received))


def bundle_fields(value,typ,store):
    from .execute import capture_value
    from .bindings import validate_record
    validate_record(value.record())
    if value.bundle!=typ or type(value.data) is not dict or set(value.data)!=set(dict(typ.members)):
        raise InvalidData('bundle value does not match its checked interface')
    fields={name:capture_value(value.data[name],member,store).restricted(value.label) for name,member in typ.members}
    for name,member in typ.members:
        if member.kind in ('Permit','Operation','Budget') and fields[name].resource is None:
            raise InvalidData('bundle resource field is missing its durable reference')
    record_refs(value.record())
    return fields


def capability_resources(value,typ,store):
    if typ.kind=='Variant':
        _,member,payload=variant_payload(value,typ,store)
        return capability_resources(payload,member,store)
    if typ.kind=='Bundle':
        fields=bundle_fields(value,typ,store)
        return [item for name,member in typ.members for item in capability_resources(fields[name],member,store)]
    return [(typ.kind,value.data)] if typ.kind in ('Cap','Permit','Operation') else []


def variant_payload(value,typ,store):
    from .bindings import validate_record
    from .execute import capture_value
    validate_record(value.record())
    if value.variant!=typ:
        raise InvalidData('Variant value does not match its closed interface')
    tag=value.data['tag']
    member=dict(typ.members)[tag]
    payload=capture_value(value.data['payload'],member,store).restricted(value.label)
    if member.kind in ('Operation','Budget') and payload.resource is None:
        raise InvalidData('Variant resource payload requires a durable reference')
    record_refs(value.record())
    return tag,member,payload
