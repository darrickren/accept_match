"""Step 3: inspection ticket (buyer view) and inspection input (staff panel)."""

from __future__ import annotations

from html import escape
from typing import Any, Dict, List

import streamlit as st

from .. import state
from ..components import code, html, money, pill, role_banner


def render() -> None:
    st.subheader("Step 3 · Inspection")
    turn = state.turn() or {}
    tickets = turn.get("tickets") or []
    if not tickets:
        if state.stage() in state.OFFER_STAGES:
            st.success("No inspection was needed: the offered unit already had verified evidence for every rule.")
        else:
            st.info("No inspection has been requested.")
        return

    open_tickets = [t for t in tickets if t["status"] == "open"]
    with st.container(border=True):
        role_banner("buyer", "Buyer view")
        if open_tickets:
            for t in open_tickets:
                st.markdown(f"We're verifying the **{t['check_label'].lower()}** on **{t['item_id']}** before making "
                            "you an offer. Unknown never counts as passed, so you'll only get an offer after a "
                            "person has checked it.")
        else:
            st.markdown("All requested checks are done.")
        _ticket_table(tickets)

    for ticket in open_tickets:
        _staff_panel(ticket)

    if not open_tickets and state.stage() in state.OFFER_STAGES:
        st.button("Go to offer", type="primary", key="am_btn_insp_to_offer", on_click=state.set_step, args=(4,))


def _ticket_table(tickets: List[Dict[str, Any]]) -> None:
    body = "".join(
        f"<tr><td>{code(t['ticket_id'])}</td><td>{code(t['item_id'])}</td>"
        f"<td>{escape(t['check_label'])} ({escape(t['rule_id'])})<br><span class='am-muted'>{escape(t['reason'])}</span></td>"
        f"<td>{money(t['cost'])}<br><span class='am-muted'>illustrative</span></td>"
        f"<td>{pill(t['status'])}{_result(t)}</td></tr>" for t in tickets)
    html("<table class='am-table'><thead><tr><th>Ticket</th><th>Unit</th><th>Check</th><th>Cost</th>"
         f"<th>Status</th></tr></thead><tbody>{body}</tbody></table>")


def _result(ticket: Dict[str, Any]) -> str:
    if ticket["status"] != "resolved":
        return ""
    status = "passed" if ticket["result"] == "pass" else "failed"
    detail = f"{ticket['inspector_id']}: {ticket['notes']}" if ticket["notes"] else f"by {ticket['inspector_id']}"
    return f" {pill(status)}<br><span class='am-muted'>{escape(detail)}</span>"


def _staff_panel(ticket: Dict[str, Any]) -> None:
    ticket_id = ticket["ticket_id"]
    inspector_key = f"am_inspector_{ticket_id}"
    st.session_state.setdefault(inspector_key, "INS-01")
    with st.container(border=True):
        role_banner("staff", "Staff panel · Inspection station")
        st.markdown(f"Ticket {ticket_id}: check the **{ticket['check_label'].lower()}** on unit **{ticket['item_id']}** "
                    f"({ticket['rule_id']}). Record what you observe; the agent cannot mark this check itself.")
        result = st.radio("Result", ["Pass", "Fail"], index=None, horizontal=True, key=f"am_result_{ticket_id}")
        st.text_input("Inspector notes", key=f"am_notes_{ticket_id}",
                      placeholder="e.g. Zipper opens and closes smoothly along its full length.")
        st.text_input("Inspector ID (synthetic)", key=inspector_key)
        st.button("Record inspection result", type="primary", disabled=result is None,
                  key=f"am_btn_record_{ticket_id}", on_click=_record, args=(ticket_id,))


def _record(ticket_id: str) -> None:
    result = st.session_state.get(f"am_result_{ticket_id}")
    if not result:
        return
    state.submit_inspection(ticket_id, result.lower(), st.session_state.get(f"am_notes_{ticket_id}", ""),
                            st.session_state.get(f"am_inspector_{ticket_id}", ""))
