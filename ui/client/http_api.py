"""HTTP transport for the AcceptMatch FastAPI backend (ACCEPTMATCH_AGENT=api).

Standard library only (urllib), so the site gains no new dependency.

- Base URL + `/api` paths, JSON and multipart bodies. Redirects are refused,
  so the bearer token can never be forwarded to another host.
- Bearer token from POST /api/demo-auth/login (or a pre-issued token), kept in
  memory for this browser session only. One automatic re-login on 401.
- Per-call timeouts (normal vs vision). Automatic retries only where safe:
  GET, and POSTs that carry an Idempotency-Key. Never after a timeout.
- HTTP status + error body -> the UI's error codes, with customer-safe copy.
- A redacted call log for diagnostics: method, path, status, duration and
  request id. Never headers, bodies, tokens, passwords or image bytes.
"""

from __future__ import annotations

import collections
import http.client
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .api_schemas import error_problems, first, login_problems, unwrap_error

LOG = logging.getLogger("acceptmatch.api")
USER_AGENT = "AcceptMatch-Streamlit/3 (customer site)"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_IMAGE_CACHE_BYTES = 24 * 1024 * 1024
MAX_RETRY_AFTER_S = 2.0
TOKEN_REFRESH_MARGIN_S = 30.0
LOGIN_PATH = "/api/demo-auth/login"

STATUS_CODES = {400: "invalid_input", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "not_implemented",
                409: "state_conflict", 413: "photo_rejected", 415: "photo_rejected", 422: "invalid_input",
                429: "rate_limited", 500: "internal", 501: "not_implemented", 502: "ai_output_invalid",
                503: "agent_unavailable", 504: "agent_timeout"}
BACKEND_CODES = {"STALE_LISTING_VERSION": "stale_listing", "INCOMPLETE_ACCEPTANCES": "incomplete_acceptances",
                 "ITEM_UNAVAILABLE": "item_unavailable", "AI_OUTPUT_INVALID": "ai_output_invalid",
                 "AI_UNAVAILABLE": "agent_unavailable", "AGENT_TIMEOUT": "agent_timeout",
                 "SESSION_LIMIT_REACHED": "session_limit"}
MESSAGES = {
    "invalid_input": "Some details weren't accepted. Please check them and try again.",
    "unauthorized": "The demo sign-in isn't working right now. Please reload the page or try again later.",
    "forbidden": "This demo account isn't allowed to do that.",
    "not_found": "We couldn't find that.",
    "not_implemented": "This step isn't connected to the AcceptMatch service yet (pending integration).",
    "state_conflict": "This was updated somewhere else. Please reload and try again.",
    "stale_listing": "This piece's details changed since you looked. Please review its imperfections and confirm "
                     "again.",
    "incomplete_acceptances": "Please confirm each imperfection before buying.",
    "item_unavailable": "Sorry, this piece is no longer available.",
    "photo_rejected": "That photo can't be used. Please use a JPG, PNG or WebP photo under 10 MB.",
    "rate_limited": "The service is busy. Please wait a moment and try again.",
    "session_limit": "This conversation reached its demo limit for AI replies. Use Start over to begin a new one.",
    "ai_output_invalid": "The automated review returned a result we couldn't use, so nothing was decided. "
                         "Please try again.",
    "agent_unavailable": "The AcceptMatch service is unavailable right now. Please try again in a moment.",
    "agent_timeout": "The AcceptMatch service took too long to answer. Please try again.",
    "contract_violation": "We couldn't read the service's response. Please try again.",
    "internal": "Something went wrong on the service. Please try again.",
}
SHOW_BACKEND_MESSAGE = ("invalid_input", "not_found")
TECHNICAL = re.compile(r"(?i)traceback|exception|arn:aws|s3://|dynamodb|botocore|boto3|stack trace|\bsql\b")
BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
JWT = re.compile(r"eyJ[a-zA-Z0-9_-]{5,}\.[a-zA-Z0-9_-]{5,}\.[a-zA-Z0-9_-]{5,}")


class ApiError(Exception):
    """A failed call, normalised for the UI. `message` is safe to show to customers."""

    def __init__(self, code: str, message: Optional[str] = None, *, retryable: bool = False,
                 status: Optional[int] = None, backend_code: Optional[str] = None, request_id: Optional[str] = None,
                 retry_after: Optional[float] = None, note: Optional[str] = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message or MESSAGES.get(code, MESSAGES["internal"])
        self.retryable = retryable
        self.status = status
        self.backend_code = backend_code
        self.request_id = request_id
        self.retry_after = retry_after
        self.note = note  # operator-only detail, already redacted

    def details(self) -> Dict[str, Any]:
        return {k: v for k, v in (("status", self.status), ("backend_code", self.backend_code),
                                  ("request_id", self.request_id), ("note", self.note)) if v is not None}


@dataclass
class Credentials:
    persona: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    token: Optional[str] = field(default=None, repr=False)  # pre-issued bearer token

    def can_login(self) -> bool:
        return bool(self.persona and self.password)

    def secrets(self) -> Tuple[str, ...]:
        return tuple(s for s in (self.password, self.token) if s)


@dataclass
class ApiResponse:
    status: int
    body: Any
    headers: Dict[str, str]
    request_id: Optional[str]
    ms: float


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None  # urllib then raises HTTPError for the 3xx response


def origin(url: str) -> Tuple[str, str, Optional[int]]:
    parsed = urllib.parse.urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, (parsed.hostname or "").lower(), port


def redact(text: Any, secrets: Tuple[str, ...] = ()) -> str:
    """Remove bearer tokens, JWT-looking strings and known secret values from operator text."""
    value = str(text if text is not None else "")
    for secret in secrets:
        if secret and len(secret) >= 4:
            value = value.replace(secret, "[redacted]")
    value = BEARER.sub("Bearer [redacted]", value)
    return JWT.sub("[redacted-token]", value)


def safe_backend_message(message: Any, secrets: Tuple[str, ...] = ()) -> Optional[str]:
    """A backend message we are willing to show customers: short, single line, not technical."""
    if not isinstance(message, str):
        return None
    text = " ".join(message.split())
    if not text or len(text) > 200 or TECHNICAL.search(text):
        return None
    return redact(text, secrets)


def map_error(status: int, body: Any, headers: Dict[str, str], secrets: Tuple[str, ...] = ()) -> ApiError:
    err = unwrap_error(body)
    contract = not error_problems(err)
    backend_code = str(err["code"])[:60] if contract else None
    route_missing = body in (None, "", {"detail": "Not Found"}, {"detail": "Method Not Allowed"})
    if status in (404, 405) and route_missing:
        code = "not_implemented"
    elif backend_code in BACKEND_CODES:
        code = BACKEND_CODES[backend_code]
    elif 300 <= status < 400:
        code = "contract_violation"
    elif status == 422 and not contract:
        code = "contract_violation"  # FastAPI request validation: our request didn't match their schema
    else:
        code = STATUS_CODES.get(status, "internal" if status >= 500 else "invalid_input")
    if contract and isinstance(err.get("retryable"), bool):
        retryable = err["retryable"]
    else:
        retryable = status in (429, 502, 503, 504)
    retry_after = _retry_after(headers)
    request_id = (str(err.get("request_id"))[:80] if contract and err.get("request_id") else None) \
        or headers.get("x-request-id")
    message = None
    if code in SHOW_BACKEND_MESSAGE and contract:
        message = safe_backend_message(err.get("message"), secrets)
    note = None
    if status == 422 and isinstance(body, dict) and isinstance(body.get("detail"), list):
        # Field locations only: FastAPI echoes input values, which may include secrets.
        locations = [".".join(str(p) for p in (d.get("loc") or [])) for d in body["detail"] if isinstance(d, dict)]
        note = "request validation failed at: " + ", ".join(locations[:6])
    elif 300 <= status < 400:
        note = "unexpected redirect refused"
    elif code == "not_implemented":
        note = "endpoint not available on this backend"
    return ApiError(code, message, retryable=retryable, status=status, backend_code=backend_code,
                    request_id=request_id, retry_after=retry_after, note=note)


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    raw = (headers.get("retry-after") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None


def encode_multipart(fields: Dict[str, str], file_field: str, filename: str, content_type: str,
                     data: bytes) -> Tuple[bytes, str]:
    """multipart/form-data body. Field names and file names are ours; values are stripped of CR/LF."""
    boundary = "AcceptMatchBoundary" + uuid.uuid4().hex
    clean = lambda value: re.sub(r'[\r\n"]', "", str(value))  # noqa: E731
    parts: List[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{clean(name)}"\r\n\r\n'.encode()
                     + clean(value).encode() + b"\r\n")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{clean(file_field)}"; '
                 f'filename="{clean(filename)}"\r\nContent-Type: {clean(content_type)}\r\n\r\n'.encode()
                 + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class HttpApi:
    """One instance per journey per browser session (it holds that session's demo token)."""

    def __init__(self, base_url: str, *, timeout_s: float = 30.0, vision_timeout_s: float = 60.0,
                 health_timeout_s: float = 5.0, max_retries: int = 1, credentials: Optional[Credentials] = None,
                 opener: Any = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self._origin = origin(self.base_url)
        self.timeout_s = timeout_s
        self.vision_timeout_s = vision_timeout_s
        self.health_timeout_s = health_timeout_s
        self.max_retries = max(0, int(max_retries))
        self.credentials = credentials or Credentials()
        self._token: Optional[str] = self.credentials.token
        self._token_expires: Optional[float] = None
        self.role: Optional[str] = None
        self.signed_in_as: Optional[str] = None
        self.calls: collections.deque = collections.deque(maxlen=40)
        self._sleep = sleep
        self._clock = clock
        self._images: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()
        self._opener = opener or urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def __repr__(self) -> str:  # never show the token
        return f"HttpApi(base_url={self.base_url!r}, signed_in={bool(self._token)})"

    # ------------------------------------------------------------ public calls

    def get(self, path: str, *, query: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None,
            request_id: Optional[str] = None, auth: bool = True) -> ApiResponse:
        if query:
            pairs = [(k, str(v)) for k, v in query.items() if v not in (None, "")]
            if pairs:
                path = f"{path}?{urllib.parse.urlencode(pairs)}"
        return self._send("GET", path, None, None, timeout, request_id, None, auth, retry="safe")

    def post(self, path: str, *, json_body: Any = None, multipart: Optional[Tuple[bytes, str]] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None,
             idempotency_key: Optional[str] = None, auth: bool = True, retry: str = "auto") -> ApiResponse:
        """POSTs are retried automatically only when they carry an Idempotency-Key (and retry='auto')."""
        if multipart is not None:
            body, content_type = multipart
        else:
            body, content_type = json.dumps({} if json_body is None else json_body).encode(), "application/json"
        if retry == "auto":
            retry = "idempotent" if idempotency_key else "never"
        return self._send("POST", path, body, content_type, timeout, request_id, idempotency_key, auth, retry=retry)

    def sign_in(self) -> None:
        """Get a token now (normally this happens lazily before the first protected call)."""
        self._ensure_token(force=True)

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def fetch_image(self, url: str) -> Optional[bytes]:
        """Bytes of a same-origin image (the token is only ever sent to the API host). None if unusable."""
        absolute = urllib.parse.urljoin(self.base_url + "/", str(url or ""))
        if origin(absolute) != self._origin or not absolute.startswith(("https://", "http://")):
            return None
        if absolute in self._images:
            self._images.move_to_end(absolute)
            return self._images[absolute]
        try:
            response = self._send("GET", absolute, None, None, self.timeout_s, None, None, True, retry="never",
                                  accept="image/*", expect="image")
        except ApiError:
            return None
        data = response.body
        if not isinstance(data, bytes) or not data:
            return None
        self._images[absolute] = data
        while sum(len(v) for v in self._images.values()) > MAX_IMAGE_CACHE_BYTES and len(self._images) > 1:
            self._images.popitem(last=False)
        return data

    def recent_calls(self) -> List[Dict[str, Any]]:
        return list(self.calls)

    # ------------------------------------------------------------ auth

    def _ensure_token(self, force: bool = False) -> None:
        fresh = self._token and (self._token_expires is None or self._clock() < self._token_expires)
        if fresh and not force:
            return
        if not self.credentials.can_login():
            return  # a pre-issued token (or none): send what we have and let the backend decide
        try:
            response = self._send("POST", LOGIN_PATH,
                                  json.dumps({"persona": self.credentials.persona,
                                              "password": self.credentials.password}).encode(),
                                  "application/json", self.timeout_s, None, None, False, retry="never")
        except ApiError as exc:
            if exc.code in ("agent_unavailable", "agent_timeout", "rate_limited"):
                raise
            raise ApiError("unauthorized", retryable=False, status=exc.status, request_id=exc.request_id,
                           note=f"demo sign-in failed ({exc.code})") from None
        problems = login_problems(response.body)
        if problems:
            raise ApiError("contract_violation", MESSAGES["unauthorized"], note="login: " + problems[0])
        role = str(response.body.get("role") or "").strip().lower()
        if role and role != "customer":
            self._token = None
            raise ApiError("forbidden", "This demo sign-in isn't a customer account, so it can't be used here.",
                           note=f"login role {role[:20]!r} refused")
        self._token = str(first(response.body, "token"))
        self.role = role or None
        self.signed_in_as = self.credentials.persona
        expires = response.body.get("expires_in")
        self._token_expires = (self._clock() + float(expires) - TOKEN_REFRESH_MARGIN_S) if expires else None

    # ------------------------------------------------------------ transport

    def _headers(self, request_id: str, idempotency_key: Optional[str], content_type: Optional[str], auth: bool,
                 accept: str) -> Dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-Request-ID": request_id}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if content_type:
            headers["Content-Type"] = content_type
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _send(self, method: str, target: str, body: Optional[bytes], content_type: Optional[str],
              timeout: Optional[float], request_id: Optional[str], idempotency_key: Optional[str], auth: bool,
              retry: str, accept: str = "application/json", expect: str = "json") -> ApiResponse:
        """`target` is an /api path, or an absolute URL already checked to be on the API origin."""
        if target.startswith("/"):
            url, path = self.base_url + target, target
        elif origin(target) == self._origin:
            url, path = target, urllib.parse.urlparse(target).path or "/"
        else:
            raise ValueError("refusing to call a URL outside the API origin")
        request_id = request_id or ("cr_" + uuid.uuid4().hex[:20])
        retries_left = self.max_retries if retry in ("safe", "idempotent") else 0
        relogin_left = 1 if auth and self.credentials.can_login() else 0
        attempt = 0
        while True:
            attempt += 1
            if auth:
                self._ensure_token()
            request = urllib.request.Request(url, data=body, method=method,
                                             headers=self._headers(request_id, idempotency_key, content_type, auth,
                                                                   accept))
            started = self._clock()
            status, raw, headers, failure = 0, b"", {}, None
            try:
                with self._opener.open(request, timeout=timeout or self.timeout_s) as response:
                    status = response.status
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    raw = self._read(response, MAX_IMAGE_BYTES if expect == "image" else MAX_JSON_BYTES)
            except urllib.error.HTTPError as exc:
                status = exc.code
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                try:
                    raw = self._read(exc, MAX_JSON_BYTES)
                except ApiError:
                    raw = b""
            except ApiError as exc:
                failure = exc
            except (socket.timeout, TimeoutError):
                failure = ApiError("agent_timeout", retryable=True, note=f"no answer within {timeout or self.timeout_s:g}s")
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    failure = ApiError("agent_timeout", retryable=True,
                                       note=f"no answer within {timeout or self.timeout_s:g}s")
                else:
                    failure = ApiError("agent_unavailable", retryable=True,
                                       note=redact(f"connection failed: {type(exc.reason).__name__}"))
            except (http.client.HTTPException, ConnectionError, OSError) as exc:
                failure = ApiError("agent_unavailable", retryable=True,
                                   note=redact(f"connection failed: {type(exc).__name__}"))
            ms = round((self._clock() - started) * 1000, 1)
            if failure is None and 200 <= status < 300:
                parsed, problem = self._parse(raw, headers, expect)
                if problem:
                    failure = ApiError("contract_violation", note=problem, status=status,
                                       request_id=headers.get("x-request-id"))
                else:
                    rid = (parsed.get("request_id") if isinstance(parsed, dict) else None) or headers.get("x-request-id")
                    self._record(method, path, status, ms, "ok", rid, attempt)
                    return ApiResponse(status, parsed, headers, str(rid)[:80] if rid else None, ms)
            if failure is None:
                parsed, _ = self._parse(raw, headers, "json")
                if status == 401 and relogin_left:
                    relogin_left -= 1
                    self._token = None
                    self._record(method, path, status, ms, "unauthorized (signing in again)", None, attempt)
                    continue
                failure = map_error(status, parsed, headers, self.credentials.secrets())
            self._record(method, path, failure.status or status or None, ms, failure.code, failure.request_id, attempt)
            if retries_left and self._should_retry(failure, retry):
                retries_left -= 1
                wait = failure.retry_after if failure.retry_after is not None else 0.4 * attempt
                self._sleep(min(MAX_RETRY_AFTER_S, wait))
                continue
            raise failure

    @staticmethod
    def _should_retry(error: ApiError, mode: str) -> bool:
        if error.code == "agent_timeout" or not error.retryable:
            return False  # a timed-out request may still finish: the customer decides, with the same key
        if error.code == "rate_limited":
            return error.retry_after is not None and error.retry_after <= MAX_RETRY_AFTER_S
        if mode == "safe":
            return error.code in ("agent_unavailable", "internal") or error.status in (502, 503, 504)
        if mode == "idempotent":
            return error.code == "agent_unavailable"
        return False

    @staticmethod
    def _read(stream: Any, limit: int) -> bytes:
        data = stream.read(limit + 1) or b""
        if len(data) > limit:
            raise ApiError("contract_violation", note="response too large")
        return data

    @staticmethod
    def _parse(raw: bytes, headers: Dict[str, str], expect: str) -> Tuple[Any, Optional[str]]:
        kind = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if expect == "image":
            if not kind.startswith("image/"):
                return None, "image response is not an image"
            return raw, None
        if not raw:
            return None, None
        if kind and kind != "application/json" and not kind.endswith("+json"):
            return None, f"expected JSON, got {kind[:40]}"
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, ValueError):
            return None, "response is not valid JSON"

    def _record(self, method: str, path: str, status: Optional[int], ms: float, outcome: str,
                request_id: Optional[str], attempt: int) -> None:
        entry = {"method": method, "path": path.split("?")[0][:120], "status": status, "ms": ms,
                 "outcome": redact(outcome, self.credentials.secrets())[:60],
                 "request_id": redact(request_id, self.credentials.secrets())[:80] if request_id else None,
                 "attempt": attempt}
        self.calls.append(entry)
        LOG.debug("%s %s -> %s in %sms (%s)", entry["method"], entry["path"], status, ms, entry["outcome"])
