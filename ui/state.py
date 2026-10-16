"""Session state and user actions.

Views read state through these helpers and trigger actions through them. Only
this module talks to the backend client. Each action runs once per button
click and the client is idempotent, so Streamlit reruns cannot duplicate
inspection tickets or reservations.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import streamlit as st

from .client import get_client

PREFIX = "am_"  # keys cleared by reset()
CLIENT = "am_client"
TURN = "am_turn"
TRACE = "am_trace"
STEP = "am_step"
ERROR = "am_error"
EDITOR_VERSION = "am_editor_version"
BUYER_TEXT = "am_buyer_text"
SHOW_PAYLOADS = "ui_show_payloads"  # display preference, kept across resets

EXAMPLE_BUYER_TEXT = "A minor scratch on the back is fine. The front and zipper must be intact."

STEPS = {1: "Preferences", 2: "Matches", 3: "Inspection", 4: "Reserve & verify"}
OFFER_STAGES = ("offer_ready", "reserved", "verified")
MAX_STEP = {"start": 1, "rules_ready": 1, "no_match": 2, "awaiting_inspection": 3,
            "offer_ready": 4, "reserved": 4, "verified": 4}
NATURAL_STEP = {"start": 1, "rules_ready": 1, "no_match": 2, "awaiting_inspection": 2,
                "offer_ready": 4, "reserved": 4, "verified": 4}


def init() -> None:
    if CLIENT in st.session_state:
        return
    st.session_state[CLIENT] = get_client()
    st.session_state[TURN] = None
    st.session_state[TRACE] = []
    st.session_state[STEP] = 1
    st.session_state[ERROR] = None
    st.session_state[EDITOR_VERSION] = 0
    st.session_state[BUYER_TEXT] = EXAMPLE_BUYER_TEXT


def reset() -> None:
    """Start a fresh demo session. Use as an on_click callback."""
    for key in [k for k in st.session_state.keys() if str(k).startswith(PREFIX)]:
        del st.session_state[key]
    init()


# ---------------------------------------------------------------- readers

def client():
    return st.session_state[CLIENT]


def turn() -> Optional[Dict[str, Any]]:
    return st.session_state.get(TURN)


def stage() -> str:
    current = turn()
    return current["stage"] if current else "start"


def error() -> Optional[Dict[str, str]]:
    return st.session_state.get(ERROR)


def trace() -> List[Dict[str, Any]]:
    return st.session_state.get(TRACE, [])


def max_step() -> int:
    return MAX_STEP.get(stage(), 1)


def natural_step() -> int:
    return NATURAL_STEP.get(stage(), 1)


def current_step() -> int:
    return max(1, min(st.session_state.get(STEP, 1), max_step()))


def set_step(step: int) -> None:
    st.session_state[STEP] = max(1, min(step, max_step()))


def completed_steps() -> set:
    current = stage()
    done = set()
    if current not in ("start", "rules_ready"):
        done.add(1)
    if current in ("awaiting_inspection",) + OFFER_STAGES:
        done.add(2)
    if current in OFFER_STAGES:
        done.add(3)
    if current == "verified":
        done.add(4)
    return done


# ---------------------------------------------------------------- actions

def _run(method: str, *args: Any) -> None:
    try:
        result = getattr(client(), method)(*args)
    except Exception as exc:  # surface backend failures; never fall back silently
        st.session_state[ERROR] = {"code": "client_error", "message": f"Backend call failed: {exc}"}
        return
    st.session_state[TURN] = result
    st.session_state[ERROR] = result.get("error")
    merged = {event["seq"]: event for event in trace()}
    for event in result.get("trace", []):
        merged[event["seq"]] = event  # de-duplicate on seq
    st.session_state[TRACE] = [merged[seq] for seq in sorted(merged)]
    if not result.get("error"):
        st.session_state[STEP] = natural_step()


def interpret(text: str) -> None:
    _run("interpret_preferences", text)
    st.session_state[EDITOR_VERSION] = st.session_state.get(EDITOR_VERSION, 0) + 1


def search(rules: List[Dict[str, Any]]) -> None:
    _run("search_candidates", rules)


def submit_inspection(ticket_id: str, result: str, notes: str, inspector_id: str) -> None:
    _run("submit_inspection_result", ticket_id, result, notes, inspector_id)


def confirm_reservation(offer_id: str, item_id: str, acknowledged: bool) -> None:
    _run("confirm_reservation", offer_id, item_id, acknowledged)


def verify_handover(reservation_id: str, scanned_item_id: str) -> None:
    _run("verify_handover", reservation_id, scanned_item_id)


# ===================================================================== V3 customer site
# The customer-facing site (app.py) uses the helpers below. Everything above
# serves the V1 buyer/staff demo (app_v1.py) and is intentionally unchanged.
# All V3 session keys start with "v3_".

import time  # noqa: E402

from .config import ConfigError, Settings, load_settings  # noqa: E402

V3_ROUTES = ("home", "shop", "results", "listing", "return", "return_status")
TRACE_TYPES = {"plan", "tool_call", "observation", "decision", "guardrail", "human_input"}
MAX_CALL_LOG = 60


def v3_init() -> None:
    ss = st.session_state
    ss.setdefault("v3_route", "home")
    ss.setdefault("v3_params", {})
    ss.setdefault("v3_shop", {})
    ss.setdefault("v3_return", {})
    ss.setdefault("v3_clients", {})
    ss.setdefault("v3_errors", {})
    ss.setdefault("v3_violations", [])
    ss.setdefault("v3_calls", [])
    ss.setdefault("v3_trace", {})
    ss.setdefault("v3_agent_sessions", 0)


def v3_settings() -> Settings:
    if "v3_settings" not in st.session_state:
        try:
            st.secrets.load_if_toml_exists()  # Streamlit promotes root-level secrets to environment variables
        except Exception:  # noqa: BLE001 - no secrets file or an older Streamlit: environment only
            pass
        st.session_state["v3_settings"] = load_settings()
    return st.session_state["v3_settings"]


def api_mode(journey: str) -> bool:
    """True when this journey talks to the FastAPI backend (ACCEPTMATCH_AGENT[_JOURNEY]=api)."""
    return v3_settings().backend_for(journey) == "api"


def route() -> str:
    name = st.session_state.get("v3_route", "home")
    return name if name in V3_ROUTES else "home"


def params() -> Dict[str, Any]:
    return st.session_state.get("v3_params", {})


def go(name: str, **kwargs: Any) -> None:
    """Switch screens. Safe as an on_click callback. Unknown routes fall back to home."""
    st.session_state["v3_route"] = name if name in V3_ROUTES else "home"
    st.session_state["v3_params"] = dict(kwargs)


def shop() -> Dict[str, Any]:
    return st.session_state.setdefault("v3_shop", {})


def returns() -> Dict[str, Any]:
    return st.session_state.setdefault("v3_return", {})


def reset_journey(journey: str) -> None:
    """Start a journey over. Clears only that journey's partition and its widget keys."""
    st.session_state["v3_shop" if journey == "shop" else "v3_return"] = {}
    st.session_state.get("v3_errors", {}).pop(journey, None)
    prefix = "v3_s_" if journey == "shop" else "v3_r_"
    for key in [k for k in st.session_state.keys() if str(k).startswith(prefix)]:
        del st.session_state[key]


# ---------------------------------------------------------------- clients and calls

def journey_source(journey: str) -> str:
    client = st.session_state.get("v3_clients", {}).get(journey)
    if client is not None:
        return client.source
    return "mock" if v3_settings().backend_for(journey) == "mock" else "agent"


def analysis_label(journey: str):
    """(label, tone) for a live journey, from the backend's latest analysis_source and model_id."""
    from .client.api_mapper import analysis_label as label_for

    client = st.session_state.get("v3_clients", {}).get(journey)
    analysis = getattr(client, "analysis", None) or {}
    return label_for(analysis.get("analysis_source"), analysis.get("model_id"))


def agent_client(journey: str):
    """The journey's client, created on first use. Config problems surface as an error, never as a mock."""
    clients = st.session_state.setdefault("v3_clients", {})
    if journey in clients:
        return clients[journey]
    from .client import get_agent_client  # local import keeps the home page free of client code

    try:
        client = get_agent_client(journey, v3_settings(), overlay())
    except ConfigError as exc:
        set_error(journey, {"code": "internal", "retryable": False,
                            "user_message": "This part of the site isn't available right now.",
                            "details": {"config": str(exc)}})
        record_call(journey, "configure", 0.0, False, "internal", str(exc))
        return None
    clients[journey] = client
    return client


def overlay() -> Dict[str, Any]:
    """Session-only mock data (drafts, photos, returns). Never shared between browser sessions."""
    if "v3_overlay" not in st.session_state:
        from .client.mock_store import new_overlay

        st.session_state["v3_overlay"] = new_overlay()
    return st.session_state["v3_overlay"]


def new_agent_session(journey: str, kind: str) -> Optional[str]:
    """Create a runtime session id, enforcing the per-browser cap for real backends."""
    from .client.contracts import new_session_id

    if journey_source(journey) != "mock":
        used = st.session_state.get("v3_agent_sessions", 0)
        if used >= v3_settings().max_agent_sessions_per_browser:
            set_error(journey, {"code": "rate_limited", "retryable": False,
                                "user_message": "You've started several sessions in this window. "
                                                "Please continue one of them or open a new window later."})
            return None
        st.session_state["v3_agent_sessions"] = used + 1
    return new_session_id(kind)


def run_v3(journey: str, operation: str, call, expected_request_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Call the journey's client and return `data` on success.

    Errors keep the customer's input, show only the user message, and never
    switch to another backend. Mismatched or malformed responses are discarded
    and recorded as contract violations.
    """
    from .client.contracts import validate_envelope

    client = agent_client(journey)
    if client is None:
        return None
    started = time.perf_counter()
    try:
        response = call(client)
    except Exception as exc:  # surface failures; never fall back silently
        set_error(journey, {"code": "internal", "retryable": True,
                            "user_message": "Something went wrong on our side. Please try again.",
                            "details": {"exception": type(exc).__name__}})
        record_call(journey, operation, _ms(started), False, "internal", type(exc).__name__)
        return None
    problems = validate_envelope(response)
    if problems:
        record_violation(journey, "contract_violation", f"{operation}: {'; '.join(problems)}")
        set_error(journey, {"code": "contract_violation", "retryable": True,
                            "user_message": "We couldn't read the latest response. Please try again."})
        record_call(journey, operation, _ms(started), False, "contract_violation", problems[0])
        return None
    meta = response.get("meta") or {}
    merge_trace(journey, meta.get("trace"))
    for note in meta.get("violations") or []:  # api mode: items dropped or downgraded by the client checks
        record_violation(journey, "contract_violation", f"{operation}: {note}")
    if expected_request_id and meta.get("client_request_id") != expected_request_id:
        record_violation(journey, "stale_response", f"{operation}: response for another request was discarded")
        record_call(journey, operation, _ms(started), False, "stale_response", "discarded")
        return None
    if not response.get("ok"):
        error = response.get("error") or {}
        set_error(journey, error)
        details = error.get("details") or {}
        note = details.get("request_id") or error.get("user_message", "")
        record_call(journey, operation, _ms(started), False, error.get("code", "internal"), str(note))
        return None
    clear_error(journey)
    record_call(journey, operation, _ms(started), True, None, meta.get("source", ""))
    return response.get("data") or {}


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def error_for(journey: str) -> Optional[Dict[str, Any]]:
    return st.session_state.get("v3_errors", {}).get(journey)


def set_error(journey: str, error: Dict[str, Any]) -> None:
    st.session_state.setdefault("v3_errors", {})[journey] = error


def clear_error(journey: str) -> None:
    st.session_state.setdefault("v3_errors", {}).pop(journey, None)


def record_call(journey: str, operation: str, ms: float, ok: bool, code: Optional[str], note: str) -> None:
    calls = st.session_state.setdefault("v3_calls", [])
    calls.append({"journey": journey, "operation": operation, "ms": ms, "ok": ok, "code": code, "note": note[:160]})
    del calls[:-MAX_CALL_LOG]


def record_violation(journey: str, kind: str, detail: str) -> None:
    st.session_state.setdefault("v3_violations", []).append(
        {"journey": journey, "kind": kind, "detail": detail[:300], "at": time.strftime("%H:%M:%S")})


def merge_trace(journey: str, events: Optional[List[Dict[str, Any]]]) -> None:
    """Keep only whitelisted, summarised trace events, de-duplicated by seq."""
    if not events:
        return
    store = st.session_state.setdefault("v3_trace", {}).setdefault(journey, {})
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in TRACE_TYPES or "seq" not in event:
            continue
        store[event["seq"]] = {
            "seq": event["seq"],
            "type": event["type"],
            "summary": str(event.get("summary", ""))[:240],
            "citations": [{k: str(v)[:60] for k, v in c.items() if k in ("item_id", "field", "rule_id", "record_id")}
                          for c in (event.get("citations") or []) if isinstance(c, dict)][:6],
            "scripted": bool(event.get("scripted")),
        }


def trace_events(journey: str) -> List[Dict[str, Any]]:
    store = st.session_state.get("v3_trace", {}).get(journey, {})
    return [store[seq] for seq in sorted(store)]
