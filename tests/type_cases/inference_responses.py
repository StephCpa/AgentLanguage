from typing import Any, TYPE_CHECKING
from al0.inference_response import InferenceResponse

if TYPE_CHECKING:
    from typing_extensions import assert_type
else:
    def assert_type(value: Any, typ: Any) -> Any:
        return value


def responses() -> tuple[InferenceResponse, InferenceResponse]:
    output = InferenceResponse('Output', 'candidate', {'u': 1})
    refusal = InferenceResponse('Refused', actual=None)
    assert_type(output, InferenceResponse)
    assert_type(refusal, InferenceResponse)
    return output, refusal
