"""Customer-site client for the AcceptMatch FastAPI backend (ACCEPTMATCH_AGENT=api).

Every operation returns the same envelope as the mock ({ok, data, error, meta}),
so state.run_v3 handles errors, stale responses and diagnostics the same way
for both. Differences from the mock, by design:

- The conversation lives on the server (POST /api/sessions, then messages).
  The site's own session id is mapped to the backend's session id here.
- Ownership comes from the demo bearer token, not from status codes.
- `meta.analysis_source` / `meta.model_id` come from the backend. Real AI is
  only claimed when a successful response says so (see api_mapper.analysis_label).
- Nothing falls back to the mock. A backend that is down, slow or returns an
  invalid payload produces a visible error.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from . import api_mapper as mapper
from .api_schemas import (acceptance_problems, chat_problems, checkout_problems, item_from_detail, item_problems,
                          items_page, return_problems, session_problems, upload_problems)
from .contracts import envelope, error_body
from .http_api import MESSAGES, ApiError, Credentials, HttpApi

SUPPORTED_API_MAJORS = (0, 1)
VERSION = re.compile(r"^v?(\d+)(\.\d+)*")


class SchemaError(Exception):
    def __init__(self, problems: List[str], request_id: Optional[str] = None) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems
        self.request_id = request_id


def _template(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _segment(value: str) -> str:
    """One path segment. IDs come from the backend or the customer, so nothing may escape the path."""
    return quote(str(value), safe="")


class ApiAgentClient:
    source = "agent"
    kind = "api"
    label = "AcceptMatch service"

    def __init__(self, journey: str, settings: Any, http: Optional[HttpApi] = None) -> None:
        self.journey = journey
        self.settings = settings
        self.http = http or HttpApi(
            settings.api_base_url, timeout_s=settings.api_timeout_s, vision_timeout_s=settings.api_vision_timeout_s,
            health_timeout_s=settings.api_health_timeout_s, max_retries=settings.api_max_retries,
            credentials=Credentials(persona=settings.persona_for(journey), password=settings.password_for(journey),
                                    token=settings.api_token))
        self.health: Optional[Dict[str, Any]] = None
        self.api_version: Optional[str] = None
        self.capabilities: Optional[Set[Tuple[str, str]]] = None
        self._capabilities_checked = False
        self.analysis: Dict[str, Optional[str]] = {"analysis_source": None, "model_id": None, "request_id": None}
        self._sessions: Dict[str, str] = {}
        self._openings: Dict[str, Optional[Dict[str, Any]]] = {}

    def __repr__(self) -> str:
        return f"ApiAgentClient(journey={self.journey!r}, http={self.http!r})"

    # ------------------------------------------------------------ plumbing

    def _call(self, operation: str, client_request_id: Optional[str], body: Callable[[List[str]], Any],
              ready: bool = True) -> Dict[str, Any]:
        notes: List[str] = []
        try:
            if ready:
                self._ready()
            response = envelope(operation, client_request_id, self.source, data=body(notes))
        except ApiError as exc:
            response = envelope(operation, client_request_id, self.source,
                                error=error_body(exc.code, exc.message, exc.retryable, exc.retry_after, exc.details()))
        except SchemaError as exc:
            notes.extend(exc.problems[:5])
            details = {"problems": exc.problems[:5]}
            if exc.request_id:
                details["request_id"] = exc.request_id
            response = envelope(operation, client_request_id, self.source,
                                error=error_body("contract_violation", MESSAGES["contract_violation"], True,
                                                 details=details))
        response["meta"].update({"analysis_source": self.analysis["analysis_source"],
                                 "model_id": self.analysis["model_id"], "request_id": self.analysis["request_id"],
                                 "backend_mode": (self.health or {}).get("mode"),
                                 "backend_version": self.api_version, "violations": notes[:8]})
        return response

    def _ready(self) -> None:
        """Health check once per client: a backend that is down fails fast and visibly."""
        if self.health is not None:
            return
        try:
            response = self.http.get("/health", timeout=self.http.health_timeout_s, auth=False)
            body = response.body if isinstance(response.body, dict) else {}
        except ApiError as exc:
            if exc.code == "not_implemented":
                body = {"note": "no /health endpoint"}
            else:
                raise ApiError("agent_unavailable" if exc.code != "agent_timeout" else "agent_timeout",
                               "The AcceptMatch service isn't reachable right now. Please try again in a moment.",
                               retryable=True, status=exc.status, request_id=exc.request_id,
                               note="health check failed: " + (exc.note or exc.code)) from None
        version = str(body.get("version") or "")
        match = VERSION.match(version)
        if match and int(match.group(1)) not in SUPPORTED_API_MAJORS:
            raise ApiError("agent_unavailable", "This site doesn't support this version of the AcceptMatch service yet.",
                           note=f"backend version {version[:20]} is outside the supported majors {SUPPORTED_API_MAJORS}")
        self.health = {"mode": str(body.get("mode"))[:30] if body.get("mode") else None,
                       "version": version[:30] or None,
                       "database_ok": (body.get("database") or {}).get("ok") if isinstance(body.get("database"), dict)
                       else None,
                       "agent_configured": (body.get("agent") or {}).get("configured")
                       if isinstance(body.get("agent"), dict) else None}
        self.api_version = self.health["version"]

    def supports(self, method: str, path: str) -> Optional[bool]:
        """Is an endpoint in the backend's /openapi.json? None when the spec can't be read (unknown)."""
        if not self._capabilities_checked:
            self._capabilities_checked = True
            try:
                spec = self.http.get("/openapi.json", timeout=self.http.health_timeout_s, auth=False).body
                paths = spec.get("paths") if isinstance(spec, dict) else None
                if isinstance(paths, dict):
                    self.capabilities = {(m.upper(), _template(p)) for p, ops in paths.items()
                                         if isinstance(ops, dict) for m in ops}
                    info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
                    self.api_version = self.api_version or (str(info.get("version"))[:30] if info.get("version")
                                                            else None)
            except ApiError:
                self.capabilities = None
        if self.capabilities is None:
            return None
        return (method.upper(), _template(path)) in self.capabilities

    def _remember(self, body: Dict[str, Any]) -> None:
        """Keep the analysis source of the latest successful backend answer that reported one."""
        if isinstance(body, dict) and "analysis_source" in body:
            self.analysis = {"analysis_source": body.get("analysis_source"),
                             "model_id": mapper._plain(body.get("model_id"), 120),
                             "request_id": mapper._plain(body.get("request_id"), 80),
                             "agent": mapper.agent_run_view(body.get("agent"))}

    def _session(self, ui_session_id: str, mode: str, client_request_id: str) -> str:
        sid = self._sessions.get(ui_session_id)
        if sid:
            return sid
        response = self.http.post("/api/sessions", json_body=mapper.session_body(mode), request_id=client_request_id)
        problems = session_problems(response.body)
        if problems:
            raise SchemaError(problems, response.request_id)
        sid = str(response.body["session_id"])[:120]
        self._sessions[ui_session_id] = sid
        self._openings[ui_session_id] = response.body if not chat_problems(response.body) else None
        return sid

    def backend_session(self, ui_session_id: str) -> Optional[str]:
        return self._sessions.get(ui_session_id)

    def fetch_image(self, url: str) -> Optional[bytes]:
        return self.http.fetch_image(url)

    # ------------------------------------------------------------ conversation

    def open_chat(self, ui_session_id: str, mode: str, client_request_id: str) -> Dict[str, Any]:
        """Create the backend session. Some backends greet with a ChatResponse; otherwise chat is None."""
        def body(notes):
            sid = self._session(ui_session_id, mode, client_request_id)
            opening = self._openings.get(ui_session_id)
            chat = None
            if opening:
                self._remember(opening)
                chat = mapper.chat_view(opening)
                notes.extend(chat["notes"])
            return {"session_id": sid, "chat": chat}
        return self._call("open_chat", client_request_id, body)

    def send_message(self, ui_session_id: str, mode: str, text: str, photo_ids: List[str],
                     client_request_id: str) -> Dict[str, Any]:
        def body(notes):
            sid = self._session(ui_session_id, mode, client_request_id)
            timeout = self.http.vision_timeout_s if (mode == "return" or photo_ids) else self.http.timeout_s
            response = self.http.post(f"/api/sessions/{_segment(sid)}/messages",
                                      json_body=mapper.message_body(text, photo_ids, client_request_id),
                                      request_id=client_request_id, idempotency_key=client_request_id,
                                      timeout=timeout)
            problems = chat_problems(response.body)
            if not problems and response.body["session_id"] != sid:
                problems = ["chat response belongs to another session"]
            if not problems and response.body["mode"] != mode:
                problems = [f"chat response mode {response.body['mode']!r} doesn't match {mode!r}"]
            if problems:
                raise SchemaError(problems, response.request_id)
            self._remember(response.body)
            chat = mapper.chat_view(response.body)
            notes.extend(chat["notes"])
            return {"chat": chat}
        return self._call("send_message", client_request_id, body)

    # ------------------------------------------------------------ inventory and purchase

    def search_items(self, filters: Dict[str, Any], cursor: Optional[str], client_request_id: str) -> Dict[str, Any]:
        def body(notes):
            response = self.http.get("/api/items", query=mapper.items_query(filters, cursor),
                                     request_id=client_request_id)
            raw, next_cursor, problems = items_page(response.body)
            if problems:
                raise SchemaError(problems, response.request_id)
            items, dropped = mapper.screen_items(raw)
            notes.extend(dropped)
            return {"items": items, "next_cursor": next_cursor, "request_id": response.request_id}
        return self._call("search_items", client_request_id, body)

    def get_item(self, item_id: str, client_request_id: str) -> Dict[str, Any]:
        def body(notes):
            response = self.http.get(f"/api/items/{_segment(item_id)}", request_id=client_request_id)
            item = item_from_detail(response.body)
            problems = item_problems(item)
            if not problems and item["item_id"] != item_id:
                problems = ["item detail belongs to another item"]
            if problems:
                raise SchemaError(problems, response.request_id)
            views, dropped = mapper.screen_items([item])
            notes.extend(dropped)
            return {"item": views[0]}
        return self._call("get_item", client_request_id, body)

    def accept_defects(self, item_id: str, listing_version: int, defect_ids: List[str],
                       client_request_id: str) -> Dict[str, Any]:
        def body(notes):
            response = self.http.post(f"/api/items/{_segment(item_id)}/acceptances",
                                      json_body=mapper.acceptance_body(listing_version, defect_ids),
                                      request_id=client_request_id, idempotency_key=client_request_id)
            problems = acceptance_problems(response.body)
            if not problems and response.body.get("listing_version", listing_version) != listing_version:
                problems = ["acceptance recorded for another listing version"]
            if problems:
                raise SchemaError(problems, response.request_id)
            accepted = response.body.get("accepted_defect_ids")
            return {"item_id": item_id, "listing_version": listing_version,
                    "defect_ids": [str(d) for d in accepted] if isinstance(accepted, list) else list(defect_ids),
                    "request_id": response.request_id}
        return self._call("accept_defects", client_request_id, body)

    def checkout(self, item_id: str, listing_version: int, idempotency_key: str,
                 client_request_id: str) -> Dict[str, Any]:
        def body(notes):
            response = self.http.post("/api/demo-checkout",
                                      json_body=mapper.checkout_body(item_id, listing_version, idempotency_key),
                                      request_id=client_request_id, idempotency_key=idempotency_key)
            problems = checkout_problems(response.body)
            if not problems and response.body["item_id"] != item_id:
                problems = ["checkout response belongs to another item"]
            if problems:
                raise SchemaError(problems, response.request_id)
            return {"purchase": mapper.purchase_view(response.body), "request_id": response.request_id}
        return self._call("checkout", client_request_id, body)

    # ------------------------------------------------------------ returns

    def upload_photo(self, ui_session_id: str, view: str, data: bytes, content_type: str,
                     client_request_id: str) -> Dict[str, Any]:
        """POST /api/uploads (multipart). Never retried automatically: a retry could store the photo twice."""
        from .http_api import encode_multipart

        def body(notes):
            sid = self._session(ui_session_id, "return", client_request_id)
            extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
            multipart = encode_multipart(mapper.upload_fields(sid, view), "file", f"{view}.{extension}",
                                         content_type, data)
            response = self.http.post("/api/uploads", multipart=multipart, request_id=client_request_id,
                                      retry="never")
            problems = upload_problems(response.body)
            if problems:
                raise SchemaError(problems, response.request_id)
            return {"photo_id": str(response.body["photo_id"])[:80],
                    "view": mapper._plain(response.body.get("declared_view"), 40) or view,
                    "request_id": response.request_id}
        return self._call("upload_photo", client_request_id, body)

    def create_return(self, ui_session_id: str, order_id: str, order_line: str, claim_type: str, reason: str,
                      client_request_id: str) -> Dict[str, Any]:
        """POST /api/returns. Sends an Idempotency-Key (the same one on every retry) but never retries by itself."""
        def body(notes):
            sid = self._session(ui_session_id, "return", client_request_id)
            response = self.http.post("/api/returns",
                                      json_body=mapper.return_body(sid, order_id, order_line, claim_type, reason),
                                      request_id=client_request_id, idempotency_key=client_request_id, retry="never")
            return {"return": self._return(response)}
        return self._call("create_return", client_request_id, body)

    def evaluate_return(self, return_id: str, client_request_id: str) -> Dict[str, Any]:
        def body(notes):
            response = self.http.post(f"/api/returns/{_segment(return_id)}/evaluate",
                                      json_body=mapper.evaluate_body(client_request_id), request_id=client_request_id,
                                      idempotency_key=client_request_id, timeout=self.http.vision_timeout_s)
            view = self._return(response)
            if view["return_id"] != return_id:
                raise SchemaError(["evaluation belongs to another return"], response.request_id)
            return {"return": view}
        return self._call("evaluate_return", client_request_id, body)

    def get_return(self, return_id: str, client_request_id: str) -> Dict[str, Any]:
        def body(notes):
            response = self.http.get(f"/api/returns/{_segment(return_id)}", request_id=client_request_id)
            view = self._return(response)
            if view["return_id"] != return_id:
                raise SchemaError(["status belongs to another return"], response.request_id)
            return {"return": view}
        return self._call("get_return", client_request_id, body)

    def _return(self, response) -> Dict[str, Any]:
        problems = return_problems(response.body)
        if problems:
            raise SchemaError(problems, response.request_id)
        self._remember(response.body)
        view = mapper.return_view(response.body)
        view["request_id"] = view["request_id"] or response.request_id
        return view

    # ------------------------------------------------------------ operator info

    def info(self) -> Dict[str, Any]:
        """What diagnostics may show. No URLs with credentials, no tokens."""
        return {"api_host": self.http.base_url.split("//", 1)[-1].split("/", 1)[0],
                "backend_mode": (self.health or {}).get("mode"), "api_version": self.api_version,
                "database_ok": (self.health or {}).get("database_ok"),
                "agent_configured": (self.health or {}).get("agent_configured"),
                "signed_in": self.http.has_token, "persona": self.http.signed_in_as,
                "analysis_source": self.analysis["analysis_source"], "model_id": self.analysis["model_id"],
                "last_request_id": self.analysis["request_id"], "last_ai_run": self.analysis.get("agent")}
