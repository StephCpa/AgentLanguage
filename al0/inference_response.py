"""Trusted model response envelopes and one shared inference classifier."""
from dataclasses import dataclass
from typing import Literal

from .errors import ContractError, InvalidData


@dataclass(frozen=True)
class InferenceResponse:
    kind: Literal['Output', 'Refused']
    output: object = None
    actual: dict[str, int] | None = None

    def __post_init__(self):
        if self.kind not in ('Output', 'Refused'):
            raise ContractError('invalid inference response kind')
        if self.kind == 'Refused' and self.output is not None:
            raise ContractError('refusal envelopes cannot contain candidate output')

    def details(self):
        return {'output': self.output, 'inference_outcome': {'version': 1, 'kind': self.kind}}


def refused(state):
    evidence = state.get('business_evidence')
    details = {} if evidence is None else evidence.get('details', {})
    if type(details) is not dict:
        if state['business'] == 'Applied':
            raise ContractError('model output evidence requires structured details')
        # Legacy pending/no-effect evidence may carry no application details.
        return False
    if 'inference_outcome' not in details:
        return False
    marker = details['inference_outcome']
    if (state['business'] != 'Applied' or type(marker) is not dict
            or set(marker) != {'version', 'kind'} or type(marker['version']) is not int
            or marker['version'] != 1 or marker['kind'] not in ('Output', 'Refused')):
        raise ContractError('invalid persisted inference response envelope')
    if marker['kind'] == 'Refused':
        if details.get('output') is not None:
            raise ContractError('refusal evidence cannot contain candidate output')
        return True
    return False


def classify_inference(catalog, spec, state):
    is_refusal = refused(state)
    if state['business'] in ('NotDispatched', 'Unknown', 'Pending'):
        return 'Waiting', None
    if is_refusal:
        code = 'Refused'
    elif state['business'] == 'Applied':
        details = state['business_evidence']['details']
        if 'output' not in details:
            raise ContractError('model response evidence has no output')
        output = details['output']
        try:
            catalog.validate(spec.output_type, output)
        except InvalidData:
            code = 'InvalidOutput'
        else:
            return 'Success', output
    elif state['business'] == 'ProvenNoEffect':
        code = 'NoResult'
    else:
        raise ContractError('unknown inference business state')
    return 'Error', {'code': code, 'tool': spec.name, 'version': spec.version}
