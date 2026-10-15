"""Shared UI pieces: styling, status labels, header, sidebar, step indicator, tables."""

from __future__ import annotations

from html import escape
from typing import Any, Dict, List

import streamlit as st

from . import state

CSS = """
<style>
.block-container {padding-top: 1.6rem; padding-bottom: 2rem;}
.am-header {display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;}
.am-title {font-size: 1.9rem; font-weight: 700; letter-spacing: -.01em;}
.am-subtitle {color: #57606a; font-size: .95rem; margin: .1rem 0 .8rem 0;}
.am-badge {display: inline-block; padding: .1rem .55rem; border-radius: 999px; font-size: .7rem;
  font-weight: 700; letter-spacing: .04em; text-transform: uppercase; border: 1px solid;}
.am-badge.synthetic {background: #fff8c5; color: #7d4e00; border-color: #d4a72c;}
.am-badge.mock {background: #ddf4ff; color: #0550ae; border-color: #54aeff;}
.am-badge.live {background: #dafbe1; color: #116329; border-color: #4ac26b;}
.am-pill {display: inline-block; padding: .05rem .45rem; border-radius: 999px; font-size: .76rem;
  font-weight: 600; white-space: nowrap;}
.am-pill.passed, .am-pill.match, .am-pill.keep_candidate, .am-pill.verified, .am-pill.resolved {background: #dafbe1; color: #116329;}
.am-pill.unknown, .am-pill.needs_check, .am-pill.request_human_check, .am-pill.open, .am-pill.request {background: #fff8c5; color: #7d4e00;}
.am-pill.failed, .am-pill.rejected, .am-pill.reject, .am-pill.mismatch {background: #ffebe9; color: #a40e26;}
.am-pill.recorded, .am-pill.reserved {background: #ddf4ff; color: #0550ae;}
.am-pill.none, .am-pill.unavailable, .am-pill.queue, .am-pill.skip {background: #eaeef2; color: #424a53;}
.am-table {width: 100%; border-collapse: collapse; font-size: .85rem; margin: .2rem 0 .6rem 0;}
.am-table th {text-align: left; font-weight: 600; color: #57606a; border-bottom: 1px solid #d0d7de; padding: .35rem .4rem;}
.am-table td {border-bottom: 1px solid #eaeef2; padding: .4rem; vertical-align: top;}
.am-muted {color: #57606a; font-size: .78rem;}
.am-id {font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8rem; background: #f6f8fa;
  border: 1px solid #d0d7de; border-radius: 4px; padding: 0 .3rem; white-space: nowrap;}
.am-card-head {display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; margin-bottom: .4rem;}
.am-agent {border-left: 4px solid #0969da; background: #f6f8fa; padding: .6rem .85rem; border-radius: 6px; margin: 0 0 .9rem 0;}
.am-agent-tag {font-size: .68rem; font-weight: 700; letter-spacing: .06em; color: #0969da; text-transform: uppercase; margin-bottom: .2rem;}
.am-role {display: inline-block; font-size: .7rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
  padding: .25rem .6rem; border-radius: 4px; margin-bottom: .5rem;}
.am-role.buyer {background: #ddf4ff; color: #0550ae;}
.am-role.staff {background: #24292f; color: #ffffff;}
.am-decision {border: 1px solid #d4a72c; background: #fff8c5; border-radius: 6px; padding: .45rem .6rem; font-size: .85rem; margin: .2rem 0 .5rem 0;}
.am-decision.queue, .am-decision.skip {border-color: #d0d7de; background: #f6f8fa;}
.am-decision.match {border-color: #4ac26b; background: #dafbe1;}
.am-photo {height: 130px; border: 1px dashed #8c959f; border-radius: 8px; padding: .5rem; text-align: center;
  display: flex; flex-direction: column; justify-content: center; gap: .15rem; font-size: .8rem; color: #424a53;
  background: repeating-linear-gradient(45deg, #f6f8fa, #f6f8fa 10px, #eef1f4 10px, #eef1f4 20px);}
.am-controls {display: flex; flex-wrap: wrap; gap: .35rem; margin: .5rem 0;}
.am-control {font-size: .74rem; border: 1px solid #d0d7de; border-radius: 999px; padding: .1rem .55rem; background: #ffffff;}
.am-control.ok {border-color: #4ac26b; color: #116329;}
.am-control.pending {border-color: #d4a72c; color: #7d4e00;}
.am-price {font-size: 1.7rem; font-weight: 700; line-height: 1.1;}
.am-turn {margin: .1rem 0 .7rem 0;}
.am-turn-head {font-size: .74rem; font-weight: 700; color: #57606a; text-transform: uppercase; letter-spacing: .05em;
  border-bottom: 1px solid #eaeef2; padding-bottom: .2rem; margin-bottom: .3rem;}
.am-event {font-size: .82rem; line-height: 1.35; margin: .35rem 0;}
.am-ev-tag {display: inline-block; min-width: 4.4rem; text-align: center; font-size: .64rem; font-weight: 700;
  letter-spacing: .05em; padding: .05rem .3rem; border-radius: 4px; margin-right: .4rem;}
.am-ev-tag.plan {background: #eaeef2; color: #424a53;}
.am-ev-tag.tool_call {background: #ddf4ff; color: #0550ae;}
.am-ev-tag.observation {background: #fbefff; color: #6e40c9;}
.am-ev-tag.decision {background: #fff8c5; color: #7d4e00;}
.am-ev-tag.human_input {background: #dafbe1; color: #116329;}
.am-ev-tag.guardrail {background: #ffebe9; color: #a40e26;}
.am-cite {display: inline-block; font-family: ui-monospace, Menlo, monospace; font-size: .68rem; color: #57606a;
  background: #f6f8fa; border: 1px solid #eaeef2; border-radius: 4px; padding: 0 .3rem; margin: .15rem .25rem 0 0;}
</style>
"""

LABELS = {
    "passed": "✓ Passed", "failed": "✕ Failed", "unknown": "? Unknown", "recorded": "● Recorded",
    "none": "○ None recorded", "match": "✓ Verified match", "needs_check": "? Needs check",
    "rejected": "✕ Rejected", "unavailable": "⊘ Unavailable", "keep_candidate": "Keep candidate",
    "request_human_check": "Request human check", "reject": "Reject", "request": "Check requested",
    "queue": "Backup", "skip": "Skipped", "open": "Open", "resolved": "Resolved", "reserved": "Reserved",
    "verified": "✓ Verified", "mismatch": "✕ Mismatch",
}
REQUIREMENT_TEXT = {"acceptable": "Acceptable", "required": "Required", "not_accepted": "Not accepted (SYS-01)"}


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def inject_css() -> None:
    html(CSS)


def money(value: float) -> str:
    return f"${value:,.2f}"


def pill(status: str, text: str = "") -> str:
    return f'<span class="am-pill {escape(status)}">{escape(text or LABELS.get(status, status))}</span>'


def code(value: Any) -> str:
    return f'<span class="am-id">{escape(str(value))}</span>' if value else '<span class="am-muted">none</span>'


def render_header() -> None:
    simulated = state.client().simulated
    badges = '<span class="am-badge synthetic">Synthetic data</span>'
    badges += ('<span class="am-badge mock">Mock backend · simulated agent</span>' if simulated
               else '<span class="am-badge live">Live agent</span>')
    html(f'<div class="am-header"><span class="am-title">AcceptMatch</span>{badges}</div>'
         '<p class="am-subtitle">Exact-unit resale for returned bags: match what a buyer accepts to the '
         'verified evidence on one specific unit.</p>')


def render_sidebar() -> None:
    client = state.client()
    with st.sidebar:
        st.markdown("### Demo controls")
        st.caption(f"Backend: {client.label}")
        st.caption(f"Session: {client.session_id}")
        st.toggle("Show tool payloads", key=state.SHOW_PAYLOADS,
                  help="Show each tool call's input and output in the agent activity panel.")
        st.button("Reset demo", on_click=state.reset, key="ui_reset", use_container_width=True)
        st.divider()
        st.caption("Synthetic data: item IDs, defect records, checks and prices are made up for this demo. "
                   "Money values are illustrative.")
        if client.simulated:
            st.caption("Mock backend: agent responses and the activity trace are simulated. "
                       "The Bedrock agent is not connected yet.")


def render_stepper() -> None:
    current, top, done = state.current_step(), state.max_step(), state.completed_steps()
    for column, (number, label) in zip(st.columns(len(state.STEPS)), state.STEPS.items()):
        text = f"{number} · {label}" + (" ✓" if number in done else "")
        with column:
            st.button(text, key=f"am_nav_{number}", type="primary" if number == current else "secondary",
                      disabled=number > top, use_container_width=True, on_click=state.set_step, args=(number,))


def agent_message(text: str) -> None:
    tag = "Agent · simulated response" if state.client().simulated else "Agent"
    html(f'<div class="am-agent"><div class="am-agent-tag">{escape(tag)}</div>{escape(text)}</div>')


def role_banner(role: str, title: str) -> None:
    html(f'<div class="am-role {escape(role)}">{escape(title)}</div>')


def rules_table(rules: List[Dict[str, Any]]) -> None:
    body = "".join(
        f"<tr><td>{code(r['rule_id'])}</td><td>{escape(r['area'])}</td><td>{escape(r['condition'])}</td>"
        f"<td>{escape(REQUIREMENT_TEXT.get(r['requirement'], r['requirement']))}</td></tr>" for r in rules)
    html("<table class='am-table'><thead><tr><th>Rule</th><th>Area</th><th>Condition</th><th>Requirement</th>"
         f"</tr></thead><tbody>{body}</tbody></table>")


def evidence_table(rows: List[Dict[str, Any]]) -> None:
    """Buyer rule vs unit evidence vs agent action, as in the proposal's AM-007 evidence check."""
    body = "".join(
        f"<tr><td>{code(r['rule_id'])} {escape(r['rule_label'])}<br>"
        f"<span class='am-muted'>{escape(REQUIREMENT_TEXT.get(r['requirement'], r['requirement']))}</span></td>"
        f"<td>{pill(r['status'])}<br><span class='am-muted'>{escape(r['evidence'])}</span></td>"
        f"<td>{code(r.get('source_id'))}<br><span class='am-muted'>{escape(r['field'])}</span></td>"
        f"<td>{pill(r['agent_action'])}</td></tr>" for r in rows)
    html("<table class='am-table'><thead><tr><th>Buyer rule</th><th>Unit evidence</th><th>Source (field)</th>"
         f"<th>Agent action</th></tr></thead><tbody>{body}</tbody></table>")


def photo_placeholder(photo: Dict[str, Any], item_id: str) -> None:
    if photo.get("path"):
        st.image(photo["path"], caption=photo["caption"])
        return
    label = f"Photo placeholder: {item_id}, {photo['view']}, {photo['caption']}"
    html(f'<div class="am-photo" role="img" aria-label="{escape(label)}"><span class="am-muted">PHOTO PLACEHOLDER</span>'
         f"<b>{escape(item_id)} · {escape(photo['view'])}</b><span>{escape(photo['caption'])}</span></div>")


def controls_strip(reserved: bool) -> None:
    items = [
        ("ok", "Unknown never counted as passed"),
        ("ok", "Defects and function human-verified"),
        ("ok", "Photos disclosed before purchase"),
        ("ok", "Price within merchant limits"),
        ("ok", "Reserved only after your confirmation") if reserved else ("pending", "Reservation needs your confirmation"),
        ("ok", "After-sales rights preserved"),
    ]
    chips = "".join(f'<span class="am-control {kind}">{"✓" if kind == "ok" else "…"} {escape(text)}</span>'
                    for kind, text in items)
    html(f'<div class="am-controls">{chips}</div>')
