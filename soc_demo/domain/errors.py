"""Typované výnimky. Správy sú po slovensky, pretože ich zobrazuje UI."""

from __future__ import annotations


class DemoError(Exception):
    """Základ chýb, ktoré API mapuje na HTTP odpoveď."""

    http_status = 400
    code = "DEMO_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DemoError):
    http_status = 404
    code = "NOT_FOUND"


class RunConflictError(DemoError):
    http_status = 409
    code = "RUN_IN_PROGRESS"


class LiveModeUnavailableError(DemoError):
    http_status = 503
    code = "LIVE_MODE_UNAVAILABLE"


class ApprovalStateError(DemoError):
    http_status = 409
    code = "APPROVAL_STATE"


class ToolCallError(Exception):
    """Návrh volania nástroja nie je platný (neznámy nástroj alebo zlé argumenty)."""

    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.message = message


class UnknownToolError(ToolCallError):
    pass


class ToolArgumentsError(ToolCallError):
    pass


class LLMError(Exception):
    """Chyba pri komunikácii s LLM endpointom."""

    kind = "llm_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LLMTimeoutError(LLMError):
    kind = "timeout"


class LLMHTTPError(LLMError):
    kind = "http_error"

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMResponseError(LLMError):
    kind = "invalid_response"


class LLMToolUseError(LLMHTTPError):
    """Server sám odmietol volanie nástroja, lebo argumenty nezodpovedali schéme (Groq: tool_use_failed)."""

    kind = "invalid_tool_call"

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message, 400)
        self.detail = detail


class ModelOutputError(LLMError):
    """Model odpovedal, ale nie podľa JSON protokolu nástrojov."""

    kind = "invalid_model_output"

    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text
