"""Step 4: offer with full disclosure, buyer confirmation, reservation, and the staff handover check."""

from __future__ import annotations

from typing import Any, Dict, Optional

import streamlit as st

from .. import state
from ..components import controls_strip, evidence_table, html, money, photo_placeholder, role_banner


def render() -> None:
    st.subheader("Step 4 · Offer, reservation and handover")
    turn = state.turn() or {}
    offer = turn.get("offer")
    if not offer:
        st.info("No offer yet. An offer appears once a unit meets every rule with verified evidence.")
        return
    reservation, handover = turn.get("reservation"), turn.get("handover")
    item_id = offer["item_id"]

    with st.container(border=True):
        role_banner("buyer", "Buyer view · Offer")
        title, price = st.columns([3, 1])
        title.markdown(f"#### {item_id} · {offer['product']}, {offer['colour']}")
        title.caption(f"Quote {offer['offer_id']} for this exact returned unit.")
        with price:
            html(f"<div class='am-price'>{money(offer['price'])}</div>"
                 f"<div class='am-muted'>{offer['currency']} · illustrative</div>")

        st.markdown("Photos of this exact unit")
        for column, photo in zip(st.columns(len(offer["photos"])), offer["photos"]):
            with column:
                photo_placeholder(photo, item_id)

        st.markdown("Disclosed defects and check results")
        evidence_table(offer["evidence"])
        checks = "; ".join(f"{c['label']}: {c['status']} ({c['source_id']})" for c in offer["checks"])
        st.caption(f"Human-verified checks: {checks}.")
        pricing = offer["pricing_check"]
        limits = "within merchant limits" if pricing["within_limits"] else "adjusted to stay within merchant limits"
        st.caption(f"Pricing policy {pricing['rule_id']}: {limits}. "
                   f"After-sales ({offer['after_sales']['rule_id']}): {offer['after_sales']['text']}")
        controls_strip(reserved=reservation is not None)

        if reservation is None:
            acknowledged = st.checkbox(
                f"I've reviewed the disclosed defects, photos and check results for {item_id}.", key="am_ack")
            st.button(f"Confirm and reserve {item_id}", type="primary", disabled=not acknowledged,
                      key="am_btn_reserve", on_click=_reserve, args=(offer["offer_id"], item_id))
        else:
            # Plain markdown treats "$" as LaTeX, so prices outside HTML blocks use the currency code.
            st.success(f"Reserved: {reservation['reservation_id']} · {item_id} · "
                       f"{reservation['currency']} {reservation['price']:.2f}. Staff verify the item ID at handover.")

    if reservation is not None:
        _handover_panel(reservation, handover)


def _handover_panel(reservation: Dict[str, Any], handover: Optional[Dict[str, Any]]) -> None:
    reservation_id, expected = reservation["reservation_id"], reservation["item_id"]
    with st.container(border=True):
        role_banner("staff", "Staff panel · Handover check")
        if reservation["status"] == "verified":
            st.success(f"Verified: tag {handover['scanned_item_id']} matches reservation {reservation_id}. "
                       "Hand the item over.")
            st.button("Start a new demo", on_click=state.reset, key="ui_restart")
            return
        st.markdown(f"Reservation {reservation_id} is for unit **{expected}**. Scan or type the ID on the item's "
                    "tag before handing it over.")
        st.text_input("Item ID on the tag", key="am_handover_id", placeholder="e.g. AM-007")
        st.button("Verify item ID", type="primary", key="am_btn_verify", on_click=_verify, args=(reservation_id,))
        if handover and handover["status"] == "mismatch":
            st.error(f"Mismatch: the tag reads {handover['scanned_item_id']}, but {reservation_id} is for {expected}. "
                     "Do not hand this item over.")


def _reserve(offer_id: str, item_id: str) -> None:
    state.confirm_reservation(offer_id, item_id, bool(st.session_state.get("am_ack")))


def _verify(reservation_id: str) -> None:
    state.verify_handover(reservation_id, st.session_state.get("am_handover_id", ""))
