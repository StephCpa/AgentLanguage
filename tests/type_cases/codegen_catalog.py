from al0.ir import Catalog, Tool


def catalog() -> Catalog:
    c = Catalog()
    c.schema('Payload', {'text': 'Text', 'count': 'Int'})
    c.schema('Envelope', {'payload': 'Payload', 'extras': 'Json'})
    c.register(Tool.define('compose', 'infer', 'Text', 'Payload', 'model', {'u': 1}))
    c.register(Tool.define('read', 'observe', 'Payload', 'Payload', 'read', {'u': 1}))
    c.register(Tool.define('validate', 'check', 'Payload', 'Bool', 'check', {'u': 1}, predicate='valid'))
    for name in ('send-a', 'send-b'):
        c.register(Tool.define(name, 'action', 'Payload', 'Json', 'outbox', {'u': 1}, predicates=('valid',)))
    for name, target in (('query-a', 'send-a'), ('query-b', 'send-b')):
        c.register(Tool.define(name, 'reconcile', 'Json', 'Json', 'outbox', {'u': 1}, reconciles=target))
    return c
