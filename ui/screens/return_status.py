"""Return status lookup by return ID and status code."""

from __future__ import annotations

import streamlit as st

from .. import actions, kit, state
from ..config import Settings
from ..status import NEUTRAL, customer_status


def render(settings: Settings) -> None:
    actions.return_begin()
    data = state.returns()
    kit.back_button("Back to returns", "return", "v3_r_status_back")
    kit.heading("Check a return", eyebrow="Return", lede="Enter the return ID and status code you were given.")
    left, right = st.columns(2)
    with left:
        st.text_input("Return ID", key="v3_r_status_case", placeholder="RET-100001")
    with right:
        st.text_input("Status code", key="v3_r_status_code", placeholder="ABCD-1234")
    st.button("Check status", key="v3_r_status_go", type="primary", on_click=actions.return_check_status)
    lookup = data.get("lookup") or {}
    if lookup.get("message"):
        kit.banner(lookup["message"], "neutral")
    error = state.error_for("return")
    if lookup and lookup.get("result") is None and error:
        message = error.get("user_message") or NEUTRAL
        if error.get("code") == "not_found" and state.journey_source("return") == "mock":
            message += " Demo returns are only kept in the browser session that created them."
        kit.banner(message, "neutral")
        return
    view = lookup.get("result")
    if not view:
        return
    label = customer_status(view)
    if label is None:
        state.record_violation("return", "contract_violation", f"untrusted status combination for {view.get('case_id')}")
        kit.banner(NEUTRAL, "neutral")
        return
    kit.html(f'<p class="am-h3" style="margin-top:1.4rem">{kit.text(view["case_id"])}</p>'
             f'<p class="am-question" style="font-size:1.8rem">{kit.text(label)}</p>')
    items = "".join(f'<li>{kit.text(event["label"])}<span class="am-small">{kit.text(event["at"][:16].replace("T", " "))}'
                    f'</span></li>' for event in view.get("timeline") or [])
    kit.html(f'<ul class="am-timeline">{items}</ul>')
    shipping = view.get("shipping")
    if shipping:
        kit.html(f'<p class="am-small">Send it to {kit.text(shipping.get("destination"))}. '
                 'Demo only: no shipping label or refund is created.</p>')
