"""Canonical value records; resource descriptors require independent custody checks."""
from .data import canonical, identifier, load_json
from .errors import InvalidData
from .flow import Label, PUBLIC
from .custody import ResourceRef
from .bundle_types import restore


def validate_record(value,depth=0):
    if (depth>16 or type(value) is not dict or not {'data','receipts'} <= set(value)
            or set(value)-{'data','receipts','label','resource','bundle','variant'} or type(value['receipts']) is not list):
        raise InvalidData('input records contain data and a receipt list')
    if 'resource' in value:
        record=value['resource']
        if type(record) is not dict or set(record)!={'kind','resource','generation'}:
            raise InvalidData('invalid resource reference record')
        ref=ResourceRef(**record)
        if value['data']!=ref.resource or value['receipts'] or 'bundle' in value or 'variant' in value:
            raise InvalidData('resource reference must match its value and cannot carry check receipts')
    if 'bundle' in value:
        typ=restore(value['bundle'])
        if typ.kind!='Bundle' or type(value['data']) is not dict or set(value['data'])!=set(dict(typ.members)) or value['receipts']:
            raise InvalidData('invalid bundle value record')
        for field in value['data'].values():
            validate_record(field,depth+1)
    if 'variant' in value:
        typ=restore(value['variant'])
        data=value['data']
        if (typ.kind!='Variant' or 'bundle' in value or value['receipts'] or type(data) is not dict
                or set(data)!={'tag','payload'} or type(data['tag']) is not str or data['tag'] not in dict(typ.members)):
            raise InvalidData('invalid Variant value record')
        validate_record(data['payload'],depth+1)
    if 'label' in value:
        Label.from_data(value['label'])
    for receipt in value['receipts']:
        identifier(receipt)


def normalize_bindings(bindings):
    if bindings is None:
        bindings = {}
    if type(bindings) is not dict:
        raise InvalidData('inputs must be named value records')
    for name, value in bindings.items():
        identifier(name)
        validate_record(value)
    encoded = canonical(bindings)
    if len(encoded.encode('utf-8')) > 1024*1024:
        raise InvalidData('input bindings exceed 1 MiB')
    result=load_json(encoded)
    for value in result.values():
        if 'label' in value:
            label=Label.from_data(value['label'])
            if label==PUBLIC:
                del value['label']
            else:
                value['label']=label.to_data()
    return result
