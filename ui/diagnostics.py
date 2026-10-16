"""Operator diagnostics (off by default; ACCEPTMATCH_DIAGNOSTICS=1).

Shows which backend each journey uses, recent calls, contract violations and
whitelisted trace summaries. Never shows prompts, raw payloads, credentials or
customer photos.
"""

from __future__ import annotations

import streamlit as st

from . import kit, state
from .config import Settings

TAGS = {"plan": "PLAN", "tool_call": "TOOL", "observation": "OBSERVE", "decision": "DECIDE", "guardrail": "CONTROL",
        "human_input": "HUMAN"}


def render(settings: Settings) -> None:
    with st.expander("Diagnostics (demo operators)", icon=":material/monitoring:"):
        cols = st.columns(2)
        for col, journey, title in ((cols[0], "shop", "Buy Defective"), (cols[1], "return", "Return")):
            with col:
                source = state.journey_source(journey)
                mode = settings.backend_for(journey)
                kit.html(f'<p class="am-h3">{kit.text(title)}</p><p class="am-small">Backend: {kit.text(mode)} · '
                         f'source: {kit.text(source)}</p>')
                events = state.trace_events(journey)
                if events:
                    note = "Scripted mock trace" if all(e["scripted"] for e in events) else "Operation summaries"
                    kit.html(f'<p class="am-small">{kit.text(note)}</p>')
                    rows = "".join(f"<li><b>{kit.text(TAGS.get(e['type'], e['type'].upper()))}</b> "
                                   f"{kit.text(e['summary'])}</li>" for e in events[-12:])
                    kit.html(f'<ul class="am-list">{rows}</ul>')
        calls = st.session_state.get("v3_calls", [])[-15:]
        if calls:
            st.dataframe(calls, hide_index=True)
        violations = st.session_state.get("v3_violations", [])
        if violations:
            kit.html('<p class="am-h3">Contract violations</p>')
            st.dataframe(violations[-15:], hide_index=True)
        st.button("Reset this browser session", key="v3_diag_reset", on_click=_reset)


def _reset() -> None:
    for key in [k for k in st.session_state.keys() if str(k).startswith("v3_")]:
        del st.session_state[key]
