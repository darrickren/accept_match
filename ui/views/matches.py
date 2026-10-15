"""Step 2: matching results with the per-unit evidence check."""

from __future__ import annotations

from html import escape
from typing import Any, Dict

import streamlit as st

from .. import state
from ..components import code, evidence_table, html, money, pill


def render() -> None:
    st.subheader("Step 2 · Matching results")
    turn = state.turn() or {}
    candidates = turn.get("candidates") or []
    if not candidates:
        st.info("Run a search in step 1 first.")
        return

    counts = {s: sum(c["status"] == s for c in candidates) for s in ("match", "needs_check", "rejected", "unavailable")}
    columns = st.columns(4)
    columns[0].metric("Units searched", len(candidates))
    columns[1].metric("Verified matches", counts["match"])
    columns[2].metric("Need a check", counts["needs_check"])
    columns[3].metric("Ruled out", counts["rejected"] + counts["unavailable"])

    for candidate in candidates:
        if candidate["status"] in ("match", "needs_check"):
            _candidate_card(candidate)

    ruled_out = [c for c in candidates if c["status"] in ("rejected", "unavailable")]
    if ruled_out:
        with st.expander(f"Ruled-out units ({len(ruled_out)})"):
            body = "".join(f"<tr><td>{code(c['item_id'])}</td><td>{pill(c['status'])}</td>"
                           f"<td>{escape(c['reason'])}</td></tr>" for c in ruled_out)
            html("<table class='am-table'><thead><tr><th>Unit</th><th>Status</th><th>Reason (rule, source)</th>"
                 f"</tr></thead><tbody>{body}</tbody></table>")

    current = state.stage()
    if current == "awaiting_inspection":
        open_tickets = [t for t in turn["tickets"] if t["status"] == "open"]
        label = "Go to inspection: " + ", ".join(f"{t['ticket_id']} for {t['item_id']}" for t in open_tickets)
        st.button(label, type="primary", key="am_btn_to_inspection", on_click=state.set_step, args=(3,))
    elif current in state.OFFER_STAGES:
        st.button("Go to offer", type="primary", key="am_btn_to_offer", on_click=state.set_step, args=(4,))
    elif current == "no_match":
        st.button("Start a new search", on_click=state.reset, key="ui_no_match_restart")


def _candidate_card(candidate: Dict[str, Any]) -> None:
    decision = candidate.get("check_decision") or {}
    with st.container(border=True):
        html(f"<div class='am-card-head'>{code(candidate['item_id'])}"
             f"<span>{escape(candidate['product'])} · {escape(candidate['colour'])}</span>"
             f"{pill(candidate['status'])}{pill(decision['decision']) if decision else ''}</div>")
        if candidate["status"] == "match":
            html("<div class='am-decision match'>Agent: meets every rule with verified evidence. "
                 "No extra check needed.</div>")
        elif decision:
            html(f"<div class='am-decision {escape(decision['decision'])}'>{escape(_decision_text(decision))}</div>")
        evidence_table(candidate["evidence"])


def _decision_text(decision: Dict[str, Any]) -> str:
    checks = ", ".join(c.replace("_", " ") for c in decision["checks"])
    economics = (f"check cost {money(decision['cost'])} vs expected extra recovery "
                 f"{money(decision['expected_extra_recovery'])} (illustrative)")
    if decision["decision"] == "request":
        return (f"Agent decision: {checks} unknown ({', '.join(decision['rule_ids'])}). "
                f"{economics[0].upper()}{economics[1:]}, so a human check was requested.")
    if decision["decision"] == "queue":
        return (f"Agent decision: backup candidate. {checks} unknown; {economics}, net {money(decision['net'])}. "
                "Checked only if a better unit fails.")
    return f"Agent decision: skipped. {economics[0].upper()}{economics[1:]}, so the check isn't worth it."
