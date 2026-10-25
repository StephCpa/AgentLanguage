"""Stable, intentionally data-minimal errors suitable for plan diagnostics."""


class AL0Error(Exception):
    code = "AL0000"

    def __init__(self, message: str, *, node: str | None=None) -> None:
        self.node = node
        super().__init__(message)


class InvalidData(AL0Error):
    code = "AL0001"


class AuthorizationDenied(AL0Error):
    code = "AL1000"


class PermitConsumed(AL0Error):
    code = "AL1001"


class BudgetExceeded(AL0Error):
    code = "AL1002"


class EvidenceMismatch(AL0Error):
    code = "AL2002"


class ContractError(AL0Error):
    code = "AL4001"


class ReplayMismatch(AL0Error):
    code = "AL4002"


class StateConflict(AL0Error):
    code = "AL4003"


class ContractBreach(AL0Error):
    code = "AL4004"


class InferenceRefused(AL0Error):
    code = "AL4005"


class TypeCheckError(AL0Error):
    code = "AL6001"


class FlowDenied(AL0Error):
    code = "AL7001"


class EvaluationError(AL0Error):
    code = "AL6002"


class ExecutionLimitExceeded(AL0Error):
    code = "AL6003"
