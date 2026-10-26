"""Model providers for the one Strands orchestration layer (strands_agent.StrandsAgent).

    bedrock  Strands BedrockModel (Amazon Bedrock Converse); AWS credential chain
    gemini   Strands GeminiModel (Google Gemini API); GEMINI_API_KEY, server-side only

A provider builds the Strands model for one step, maps its failures to the error
contract, and names what produced a result (analysis_source + model ids).
Prompts, tools, schemas, matching and return rules are shared, so switching
providers never changes the business logic.
"""

from __future__ import annotations

import collections
import re
import threading
import time
from typing import Any, Dict, List, Optional

from .base import AgentFailure, agent_timeout, ai_output_invalid, ai_throttled, ai_unavailable

BAD_OUTPUT = {"StructuredOutputException", "MaxTokensReachedException"}
TIMEOUTS = {"ReadTimeoutError", "ConnectTimeoutError", "TimeoutError", "ModelTimeoutException", "ReadTimeout",
            "ConnectTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException"}
API_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def failure_chain(exc: BaseException) -> List[str]:
    """Exception class names plus AWS error codes / Google statuses along the cause chain (no messages)."""
    names: List[str] = []
    seen: Optional[BaseException] = exc
    while seen is not None and len(names) < 16:
        names.append(type(seen).__name__)
        response = getattr(seen, "response", None)
        code = (response.get("Error") or {}).get("Code") if isinstance(response, dict) else None
        if code:
            names.append(str(code))
        status = getattr(seen, "status", None)
        if isinstance(status, str) and status:
            names.append(status)
        seen = seen.__cause__ or seen.__context__
    return names


def redact(text: Any, *secrets: str) -> str:
    """Remove API keys from text that may reach a log line."""
    out = API_KEY.sub("[redacted-key]", str(text))
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[redacted]")
    return out


class Provider:
    name = "base"
    source = "AI"
    max_tokens: Dict[str, int] = {}

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    @property
    def model_id(self) -> Optional[str]:
        raise NotImplementedError

    @property
    def vision_model_id(self) -> Optional[str]:
        raise NotImplementedError

    def read_timeout(self, deadline_s: float) -> float:
        return deadline_s

    def build(self, model_id: str, max_tokens: int, read_timeout_s: float) -> Any:
        raise NotImplementedError

    def classify(self, exc: BaseException) -> AgentFailure:
        raise NotImplementedError

    def describe(self, exc: BaseException) -> str:
        """A log-safe description of a failure."""
        return " <- ".join(failure_chain(exc))


# ----------------------------------------------------------------- Amazon Bedrock

THROTTLED = {"ModelThrottledException", "ThrottlingException", "throttlingException", "TooManyRequestsException",
             "ServiceQuotaExceededException"}
BEDROCK_CAUSES = [  # (names, cause, retryable)
    ({"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError", "TokenRetrievalError",
      "UnauthorizedSSOTokenError", "SSOTokenLoadError", "NoRegionError"},
     "no usable AWS credentials or region for the backend", False),
    ({"ExpiredTokenException", "ExpiredToken", "UnrecognizedClientException", "InvalidSignatureException"},
     "the AWS credentials were rejected or have expired", False),
    ({"AccessDeniedException"}, "access to the Bedrock model was denied (IAM permission or model access)", False),
    ({"ResourceNotFoundException"}, "the Bedrock model wasn't found in this region", False),
    ({"ValidationException"}, "Bedrock rejected the request (check BEDROCK_MODEL_ID and the region)", False),
    ({"EndpointConnectionError", "ConnectionClosedError"}, "the Bedrock endpoint couldn't be reached", True),
    ({"ServiceUnavailableException", "InternalServerException", "ModelNotReadyException", "ModelErrorException"},
     "Bedrock reported a temporary problem", True),
]


def classify_bedrock(exc: BaseException) -> AgentFailure:
    names = failure_chain(exc)
    found = set(names)
    if found & THROTTLED:
        return ai_throttled()
    if found & TIMEOUTS:
        return agent_timeout()
    if found & BAD_OUTPUT:
        return ai_output_invalid([f"the model didn't produce a valid result ({names[0]})"])
    for group, cause, retryable in BEDROCK_CAUSES:
        if found & group:
            return ai_unavailable(cause, retryable)
    return ai_unavailable(names[0])


class BedrockProvider(Provider):
    name, source = "bedrock", "BEDROCK"
    max_tokens = {"shopping": 1200, "return": 900, "vision": 2000, "check": 100}

    @property
    def model_id(self) -> Optional[str]:
        return self.settings.bedrock_model_id

    @property
    def vision_model_id(self) -> Optional[str]:
        return self.settings.bedrock_vision_model_id or self.settings.bedrock_model_id

    def read_timeout(self, deadline_s: float) -> float:
        return min(deadline_s, self.settings.bedrock_read_timeout_s)

    def build(self, model_id: str, max_tokens: int, read_timeout_s: float) -> Any:
        from botocore.config import Config

        return bedrock_model_class()(
            model_id=model_id, region_name=self.settings.bedrock_region, temperature=0.0, max_tokens=max_tokens,
            streaming=False, boto_client_config=Config(connect_timeout=self.settings.bedrock_connect_timeout_s,
                                                       read_timeout=read_timeout_s,
                                                       retries={"total_max_attempts": 1, "mode": "standard"}))

    def classify(self, exc: BaseException) -> AgentFailure:
        return classify_bedrock(exc)


NOVA_SCHEMA_KEYS = {"type", "properties", "required", "items", "enum", "description", "minimum", "maximum"}


def nova_schema(schema: Any, top: bool = True) -> Any:
    """JSON Schema reduced to what Amazon Nova's tool use documents: the top level has only type, properties and
    required; nested schemas keep types, enums, descriptions and numeric bounds. Optional fields (anyOf with null,
    or a ["x", "null"] type) become their plain type. Pydantic still validates the full schema afterwards."""
    if isinstance(schema, list):
        return [nova_schema(s, False) for s in schema]
    if not isinstance(schema, dict):
        return schema
    options = schema.get("anyOf")
    if isinstance(options, list):
        real = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(real) == 1 and isinstance(real[0], dict):
            schema = {**{k: v for k, v in schema.items() if k != "anyOf"}, **real[0]}
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = [k for k in kind if k != "null"] or ["string"]
        schema = {**schema, "type": kinds[0] if len(kinds) == 1 else kinds}
    allowed = {"type", "properties", "required"} if top else NOVA_SCHEMA_KEYS
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key not in allowed:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: nova_schema(sub, False) for name, sub in value.items()}
        elif key == "items":
            out[key] = nova_schema(value, False)
        else:
            out[key] = value
    if top:
        out.setdefault("type", "object")
    return out


_BEDROCK_MODEL_CLASS = None


def bedrock_model_class() -> type:
    """Strands' BedrockModel with tool schemas reduced for Amazon Nova (imported lazily)."""
    global _BEDROCK_MODEL_CLASS
    if _BEDROCK_MODEL_CLASS is not None:
        return _BEDROCK_MODEL_CLASS
    from strands.models import BedrockModel

    class AcceptMatchBedrockModel(BedrockModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):  # noqa: ANN001
            reduced = [{**spec, "inputSchema": {"json": nova_schema(spec["inputSchema"]["json"])}}
                       for spec in tool_specs or []] or None
            async for event in super().stream(messages, reduced, system_prompt, **kwargs):
                yield event

    _BEDROCK_MODEL_CLASS = AcceptMatchBedrockModel
    return AcceptMatchBedrockModel


# ----------------------------------------------------------------- Google Gemini

GEMINI_SCHEMA_KEYS = {"type", "title", "description", "properties", "required", "additionalProperties", "enum",
                      "format", "minimum", "maximum", "items", "prefixItems", "minItems", "maxItems", "anyOf"}
GEMINI_FORMATS = {"date-time", "date", "time"}
NETWORK = {"ConnectError", "NetworkError", "RemoteProtocolError", "ProxyError", "ConnectionError", "SSLError"}


def gemini_schema(schema: Any) -> Any:
    """JSON Schema reduced to the subset the Gemini API documents. Pydantic still validates the full schema."""
    if isinstance(schema, list):
        return [gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    options = schema.get("anyOf")
    if isinstance(options, list):  # Optional[X] -> {"type": [X, "null"], ...X}
        real = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(real) == 1 and len(real) < len(options) and isinstance(real[0], dict) and \
                isinstance(real[0].get("type"), str):
            schema = {**{k: v for k, v in schema.items() if k != "anyOf"}, **real[0],
                      "type": [real[0]["type"], "null"]}
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key not in GEMINI_SCHEMA_KEYS or (key == "format" and value not in GEMINI_FORMATS):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: gemini_schema(sub) for name, sub in value.items()}
        elif key in ("items", "additionalProperties", "anyOf", "prefixItems"):
            out[key] = gemini_schema(value)
        else:
            out[key] = value
    return out


class LocalRateLimit(Exception):
    """AcceptMatch's own per-minute cap on model calls was reached (no request was sent)."""


class CallLimiter:
    """Process-wide sliding-window cap on model calls; 0 disables it."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = int(per_minute)
        self._calls: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.per_minute <= 0:
            return
        now = time.monotonic()
        with self._lock:
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) >= self.per_minute:
                raise LocalRateLimit(f"{self.per_minute} model calls per minute")
            self._calls.append(now)


_GEMINI_MODEL_CLASS = None


def gemini_model_class() -> type:
    """Strands' GeminiModel plus schema reduction, the call limiter and per-call latency (imported lazily)."""
    global _GEMINI_MODEL_CLASS
    if _GEMINI_MODEL_CLASS is not None:
        return _GEMINI_MODEL_CLASS
    from strands.models.gemini import GeminiModel

    class AcceptMatchGeminiModel(GeminiModel):
        def __init__(self, *, limiter: Optional[CallLimiter] = None, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._limiter = limiter

        def _format_request_tools(self, tool_specs):  # noqa: ANN001 - Strands' signature
            reduced = [{**spec, "inputSchema": {"json": gemini_schema(spec["inputSchema"]["json"])}}
                       for spec in tool_specs or []]
            return super()._format_request_tools(reduced or None)

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):  # noqa: ANN001
            if self._limiter is not None:
                self._limiter.acquire()
            started = time.monotonic()
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                if "metadata" in event:
                    event["metadata"].setdefault("metrics", {})["latencyMs"] = int((time.monotonic() - started) * 1000)
                yield event

    _GEMINI_MODEL_CLASS = AcceptMatchGeminiModel
    return AcceptMatchGeminiModel


def _gemini_error(exc: BaseException) -> Optional[BaseException]:
    seen: Optional[BaseException] = exc
    while seen is not None:
        if type(seen).__module__.startswith("google.genai") and isinstance(getattr(seen, "code", None), int):
            return seen
        seen = seen.__cause__ or seen.__context__
    return None


def classify_gemini(exc: BaseException, model_id: Optional[str]) -> AgentFailure:
    names = failure_chain(exc)
    found = set(names)
    if "LocalRateLimit" in found:
        return ai_throttled("AcceptMatch's own limit of AI calls per minute was reached (AGENT_MAX_CALLS_PER_MINUTE)")
    api = _gemini_error(exc)
    if api is not None:
        code, status = int(getattr(api, "code")), str(getattr(api, "status", "") or "")
        text = f"{getattr(api, 'message', '') or ''} {getattr(api, 'details', '') or ''}"
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            return ai_throttled("Gemini rate limit or free-tier quota reached")
        if code == 401 or status == "UNAUTHENTICATED" or "API_KEY_INVALID" in text or "API key not valid" in text \
                or "API key expired" in text:
            return ai_unavailable("the Gemini API key was rejected (check GEMINI_API_KEY)", retryable=False)
        if code == 403 or status == "PERMISSION_DENIED":
            return ai_unavailable("this API key isn't allowed to use the Gemini API (permission denied)",
                                  retryable=False)
        if code == 404 or status == "NOT_FOUND":
            return ai_unavailable(f"Gemini model {model_id} wasn't found for this API key", retryable=False)
        if status == "FAILED_PRECONDITION":
            return ai_unavailable("the Gemini API isn't available for this key or location", retryable=False)
        if code == 504 or status == "DEADLINE_EXCEEDED":
            return agent_timeout()
        if code >= 500:
            return ai_unavailable(f"Gemini reported a temporary problem ({code} {status})".replace(" )", ")"))
        return ai_unavailable(f"Gemini rejected the request ({code} {status})".replace(" )", ")"), retryable=False)
    if "ModelThrottledException" in found:
        return ai_throttled("Gemini rate limit or free-tier quota reached")
    if found & TIMEOUTS:
        return agent_timeout()
    if found & BAD_OUTPUT:
        return ai_output_invalid([f"the model didn't produce a valid result ({names[0]})"])
    if found & NETWORK:
        return ai_unavailable("the Gemini API couldn't be reached (network)")
    if "ContextWindowOverflowException" in found:
        return ai_unavailable("the request was too large for the model")
    return ai_unavailable(names[0])


class GeminiProvider(Provider):
    """Gemini 3.x on the Gemini API. Temperature stays at the Gemini 3 default (1.0), as Google recommends."""

    name, source = "gemini", "GEMINI"
    max_tokens = {"shopping": 2048, "return": 1536, "vision": 4096, "check": 512}

    def __init__(self, settings: Any, transport: Any = None) -> None:
        super().__init__(settings)
        self._transport = transport  # an httpx transport, for offline tests only
        self.limiter = CallLimiter(settings.agent_max_calls_per_minute)

    @property
    def model_id(self) -> Optional[str]:
        return self.settings.gemini_model_id

    @property
    def vision_model_id(self) -> Optional[str]:
        return self.settings.gemini_vision_model_id or self.settings.gemini_model_id

    def read_timeout(self, deadline_s: float) -> float:
        return min(deadline_s, self.settings.gemini_http_timeout_s)

    def build(self, model_id: str, max_tokens: int, read_timeout_s: float) -> Any:
        http: Dict[str, Any] = {"timeout": int(read_timeout_s * 1000), "retry_options": {"attempts": 1}}
        if self._transport is not None:
            http["async_client_args"] = {"transport": self._transport}
        return gemini_model_class()(
            limiter=self.limiter, model_id=model_id,
            client_args={"api_key": self.settings.gemini_api_key, "http_options": http},
            params={"max_output_tokens": max_tokens,
                    "thinking_config": {"thinking_level": self.settings.gemini_thinking_level}})

    def classify(self, exc: BaseException) -> AgentFailure:
        return classify_gemini(exc, self.model_id)

    def describe(self, exc: BaseException) -> str:
        api = _gemini_error(exc)
        chain = " <- ".join(failure_chain(exc))
        if api is None:
            return chain
        message = redact(getattr(api, "message", "") or "", self.settings.gemini_api_key)[:200]
        return f"{chain}: {getattr(api, 'code', '')} {getattr(api, 'status', '')} {message}"
