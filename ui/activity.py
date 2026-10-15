"""Agent Activity Panel: plan, tool calls, observations and decisions, with citations."""

from __future__ import annotations

from html import escape
from itertools import groupby
from typing import Any, Dict, List

import streamlit as st

from . import state
from .components import html

TAGS = {"plan": "PLAN", "tool_call": "TOOL", "observation": "OBSERVE", "decision": "DECIDE",
        "human_input": "HUMAN", "guardrail": "CONTROL"}


def _citations(citations: List[Dict[str, Any]]) -> str:
    chips = []
    for citation in citations:
        parts = [str(citation[k]) for k in ("item_id", "field", "rule_id", "record_id") if citation.get(k)]
        if parts:
            chips.append(f'<span class="am-cite">{escape(" · ".join(parts))}</span>')
    return f"<div>{''.join(chips)}</div>" if chips else ""


def _event(event: Dict[str, Any]) -> str:
    kind = event["type"]
    return (f'<div class="am-event"><span class="am-ev-tag {escape(kind)}">{TAGS.get(kind, kind.upper())}</span>'
            f'{escape(event["summary"])}{_citations(event.get("citations", []))}</div>')


def render_activity_panel() -> None:
    simulated = state.client().simulated
    st.markdown("#### Agent activity")
    st.caption("Simulated trace from the mock backend; live agent events will replace it. Newest turn first."
               if simulated else "Live trace from the agent. Newest turn first.")
    events = state.trace()
    with st.container(height=640, border=True):
        if not events:
            st.caption("Nothing yet. The agent's plan, tool calls and decisions will appear here, "
                       "citing item, field and rule IDs.")
            return
        show_payloads = st.session_state.get(state.SHOW_PAYLOADS, False)
        turns = [(key, list(group)) for key, group in groupby(events, key=lambda e: (e["turn"], e["turn_label"]))]
        for (number, label), group in reversed(turns):
            rows = "".join(_event(e) for e in group)
            html(f'<div class="am-turn"><div class="am-turn-head">Turn {number} · {escape(label)}</div>{rows}</div>')
            if show_payloads:
                for event in group:
                    if event.get("tool"):
                        with st.expander(f"Payload: {event['tool']['name']} (turn {number})"):
                            st.json(event["tool"])
