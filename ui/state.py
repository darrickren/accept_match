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
