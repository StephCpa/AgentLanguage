"""Closed, deterministic data operations with bounded results and no host FFI."""
from .data import MAX_INTEGER, canonical, load_json
from .errors import EvaluationError, TypeCheckError


MAX_VALUE_BYTES=1024*1024
OPERATORS={'add':2,'sub':2,'mul':2,'div':2,'mod':2,'neg':1,
           'eq':2,'lt':2,'le':2,'gt':2,'ge':2,'and':2,'or':2,'not':1,
           'concat':2,'length':1}


def derived_kind(types):
    if any(t.kind=='Candidate' for t in types):
        return 'Candidate'
    if any(t.kind=='Observation' for t in types):
        return 'Observation'
    # Evidence applies to the original artifact, not a computed artifact.
    return 'Data'


def compute_type(operator, types):
    if type(operator) is not str or operator not in OPERATORS or len(types)!=OPERATORS[operator]:
        raise TypeCheckError('unknown pure operator or wrong argument count')
    schemas=[t.name for t in types]
    if operator in ('add','sub','mul','div','mod','neg'):
        expected,result=['Int']*len(types),'Int'
    elif operator in ('and','or','not'):
        expected,result=['Bool']*len(types),'Bool'
    elif operator in ('concat','length'):
        expected,result=['Text']*len(types),'Text' if operator=='concat' else 'Int'
    elif operator=='eq':
        expected,result=[schemas[0]]*2,'Bool'
    else:
        expected,result=[schemas[0]]*2,'Bool'
        if schemas[0] not in ('Int','Text'):
            raise TypeCheckError('ordered comparisons require Int or Text')
    if schemas!=expected:
        raise TypeCheckError('pure operator argument schemas do not match')
    return derived_kind(types),result


def bounded(value):
    """Preflight expanded JSON size before serialization, including repeated refs."""
    size=0
    pending=[(value,0)]
    while pending:
        item,depth=pending.pop()
        if depth>64:
            raise EvaluationError('computed value exceeds the nesting limit')
        if type(item) is str:
            if len(item)>MAX_VALUE_BYTES:
                raise EvaluationError('pure value exceeds 1 MiB')
            size+=len(canonical(item).encode('utf-8'))
        elif item is None or type(item) in (bool,int):
            if type(item) is int and not -MAX_INTEGER<=item<=MAX_INTEGER:
                raise EvaluationError('integer arithmetic overflow')
            size+=len(canonical(item))
        elif type(item) in (list,dict):
            size+=2+max(0,len(item)-1)+(len(item) if type(item) is dict else 0)
            if size>MAX_VALUE_BYTES or len(item)>MAX_VALUE_BYTES:
                raise EvaluationError('pure value exceeds 1 MiB')
            if type(item) is dict:
                if any(type(k) is not str for k in item):
                    raise EvaluationError('pure records require string keys')
                pending.extend((k,depth+1) for k in item)
                pending.extend((v,depth+1) for v in item.values())
            else:
                pending.extend((v,depth+1) for v in item)
        else:
            raise EvaluationError('pure values must be JSON data')
        if size>MAX_VALUE_BYTES:
            raise EvaluationError('pure value exceeds 1 MiB')
    return value


def compute(operator, arguments):
    for value in arguments:
        bounded(value)
    a=arguments[0]
    b=arguments[1] if len(arguments)==2 else None
    if operator=='add': result=a+b
    elif operator=='sub': result=a-b
    elif operator=='mul': result=a*b
    elif operator in ('div','mod'):
        if b==0:
            raise EvaluationError('integer division by zero')
        result=a//b if operator=='div' else a%b
    elif operator=='neg': result=-a
    elif operator=='eq': result=canonical(a)==canonical(b)
    elif operator=='lt': result=a<b
    elif operator=='le': result=a<=b
    elif operator=='gt': result=a>b
    elif operator=='ge': result=a>=b
    elif operator=='and': result=a and b
    elif operator=='or': result=a or b
    elif operator=='not': result=not a
    elif operator=='concat':
        if len(a)+len(b)>MAX_VALUE_BYTES:
            raise EvaluationError('pure value exceeds 1 MiB')
        result=a+b
    elif operator=='length': result=len(a)
    else: raise EvaluationError('unknown pure operator')
    return bounded(result)


def record(fields):
    bounded(fields)
    return load_json(canonical(fields))
