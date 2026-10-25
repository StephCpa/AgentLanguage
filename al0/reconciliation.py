"""Validate a trusted adapter's reconciliation protocol before applying facts.

Authentication/finality are obligations of the registered adapter. A remote JSON
body by itself is not proof. In particular, absence of business evidence remains
unknown: a NotFound response is never translated to ProvenNoEffect here.
"""
import json

from .data import canonical, identifier, resource_vector
from .errors import EvidenceMismatch


def validate_result(result, operation, intent_digest, attempts):
    canonical(result)
    if type(result) is not dict or set(result) != {'operation', 'intent_digest', 'business', 'bills'}:
        raise EvidenceMismatch('invalid reconciliation envelope')
    if result['operation'] != operation or result['intent_digest'] != intent_digest:
        raise EvidenceMismatch('reconciliation evidence binds a different operation or intent')
    business = result['business']
    identifiers = set()
    if business is not None:
        if type(business) is not dict or set(business) != {'evidence_id', 'outcome', 'details'}:
            raise EvidenceMismatch('invalid business evidence record')
        identifier(business['evidence_id'])
        if business['outcome'] not in ('Applied', 'ProvenNoEffect', 'Pending'):
            raise EvidenceMismatch('unsupported business evidence; absence is not final no-effect proof')
        identifiers.add(business['evidence_id'])
    bills = result['bills']
    if type(bills) is not list:
        raise EvidenceMismatch('billing evidence must be a list')
    known = {a['id']: a for a in attempts}
    billed = set()
    for bill in bills:
        if type(bill) is not dict or set(bill) != {'attempt', 'evidence_id', 'actual'}:
            raise EvidenceMismatch('invalid billing evidence record')
        identifier(bill['attempt'])
        identifier(bill['evidence_id'])
        resource_vector(bill['actual'])
        if bill['attempt'] not in known or known[bill['attempt']]['phase'] != 'DispatchStarted':
            raise EvidenceMismatch('bill does not identify an exposed attempt of the target operation')
        if bill['attempt'] in billed or bill['evidence_id'] in identifiers:
            raise EvidenceMismatch('duplicate attempt or evidence identity in reconciliation batch')
        if set(bill['actual']) != set(json.loads(known[bill['attempt']]['upper_bound'])):
            raise EvidenceMismatch('billing dimensions do not match the target attempt')
        billed.add(bill['attempt'])
        identifiers.add(bill['evidence_id'])
    return result
