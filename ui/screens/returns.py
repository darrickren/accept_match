"""Return flow: item, issue, photos, review, then the assessment outcome."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

import streamlit as st

from .. import actions, kit, state
from ..config import Settings
from ..status import NEUTRAL, customer_status

STEPS = ("Item", "Issue", "Photos", "Review")
TIPS = "Daylight, a plain background and the whole item in frame. Please don't include people, receipts or documents."


def render(settings: Settings) -> None:
    if state.api_mode("return"):
        from . import api_returns

        api_returns.render_flow(settings)
        return
    actions.return_begin()
    data = state.returns()
    kit.back_button("Home", "home", "v3_r_back_home")
    kit.html(f'<p class="am-eyebrow">Return &nbsp;{kit.source_tag("return")}</p>')
    if not data.get("session_id"):
        kit.error_notice(state.error_for("return"))
        return
    pending = data.get("pending")
    if pending:
        with st.spinner("Reviewing your request…"):
            {"submit": actions.return_submit, "assess": actions.return_assess,
             "more_info": actions.return_add_info}[pending]()
    step = data.get("step", 1)
    if step == "outcome":
        _outcome(data, settings)
        return
    _stepper(step)
    {1: _item, 2: _issue, 3: _photos, 4: _review}[step](data, settings)
    for message in data.get("form_errors") or []:
        kit.banner(message, "error")
    kit.error_notice(state.error_for("return"))


def _stepper(step: int) -> None:
    parts = []
    for number, label in enumerate(STEPS, start=1):
        cls = "on" if number == step else ("done" if number < step else "")
        mark = "✓ " if number < step else f"{number} "
        parts.append(f'<span class="{cls}">{mark}{kit.text(label)}</span>')
    kit.html(f'<div class="am-steps" aria-label="Return steps">{"".join(parts)}</div>')


def _item(data: Dict[str, Any], settings: Settings) -> None:
    kit.html('<h1 class="am-h2">What are you returning?</h1>')
    st.segmented_control("Type of bag", list(actions.CATEGORIES), key="v3_r_category",
                         format_func=actions.CATEGORIES.get)
    st.text_input("Product name (optional)", key="v3_r_name", placeholder="e.g. Corvo crossbody")
    left, right = st.columns(2)
    with left:
        st.text_input("Order number (optional)", key="v3_r_order", placeholder="DEMO-10023",
                      help="Demo only: please don't enter a real order number.")
    with right:
        st.date_input("Purchase date (optional)", value=None, key="v3_r_date", max_value=dt.date.today(),
                      format="YYYY-MM-DD")
    st.selectbox("Reason for return", list(actions.REASONS), index=None, key="v3_r_reason",
                 format_func=actions.REASONS.get, placeholder="Choose a reason")
    kit.html('<p class="am-small">We don\'t ask for your name, address or contact details in this demo.</p>')
    st.button("Continue", key="v3_r_next_1", type="primary", on_click=actions.return_save_product)


def _issue(data: Dict[str, Any], settings: Settings) -> None:
    kit.html('<h1 class="am-h2">What\'s wrong with it?</h1>'
             '<p class="am-lede">Describe it in your own words. Say where it is (front, back, zipper, strap, handle, '
             'lining) and what you see. No technical terms needed.</p>')
    st.text_area("Describe the issue", key="v3_r_desc", height=130, label_visibility="collapsed",
                 placeholder=f"For example: {actions.EXAMPLE_DESCRIPTION}")
    with st.container(horizontal=True, gap="small"):
        st.button("Back", key="v3_r_prev_2", type="tertiary", icon=":material/arrow_back:",
                  on_click=actions.return_go, args=(1,))
        st.button("Use the example", key="v3_r_example", on_click=actions.return_use_example)
        st.button("Continue", key="v3_r_next_2", type="primary", on_click=actions.return_save_description)


def _photos(data: Dict[str, Any], settings: Settings) -> None:
    reqs = data.get("requirements") or {}
    slots = reqs.get("slots") or []
    photos = data.get("photos", {})
    required = [s for s in slots if s["required"]]
    done = sum(1 for s in required if (photos.get(s["slot"]) or {}).get("status") == "accepted")
    kit.html('<h1 class="am-h2">Add a few photos</h1>'
             f'<p class="am-lede">Based on what you told us, we need {len(required)} photos. {kit.text(TIPS)}</p>')
    kit.html(f'<p class="am-small" style="margin:.6rem 0 1rem">{done} of {len(required)} required photos added</p>')
    for start in range(0, len(slots), 2):
        columns = st.columns(2, gap="medium")
        for column, slot in zip(columns, slots[start:start + 2]):
            with column:
                _slot(slot, photos, data, settings)
    with st.container(horizontal=True, gap="small"):
        st.button("Back", key="v3_r_prev_3", type="tertiary", icon=":material/arrow_back:",
                  on_click=actions.return_go, args=(2,))
        st.button("Continue", key="v3_r_next_3", type="primary", disabled=done < len(required),
                  on_click=actions.return_go, args=(4,))


def _slot(slot: Dict[str, Any], photos: Dict[str, Any], data: Dict[str, Any], settings: Settings) -> None:
    name = slot["slot"]
    photo = photos.get(name) or {}
    with st.container(key=f"am-slot-{name}"):
        tag = kit.pill("Required", "possible") if slot["required"] else kit.pill("Optional", "off")
        kit.html(f'<p class="am-h3" style="font-size:1.02rem">{kit.text(slot["label"])} {tag}</p>'
                 f'<p class="am-small">{kit.text(slot.get("hint"))}</p>')
        if photo.get("status") == "accepted":
            st.image(photo["thumb"], width=220)
            st.button("Remove", key=f"v3_r_remove_{name}", type="tertiary", icon=":material/delete:",
                      on_click=actions.return_remove, args=(name,))
        elif photo.get("status") == "rejected":
            kit.banner(photo.get("reason") or "That photo can't be used.", "error")
        key = f"v3_r_up_{name}_{data.get('nonce', {}).get(name, 0)}"
        st.file_uploader("Replace photo" if photo.get("status") == "accepted" else "Add photo",
                         type=["jpg", "jpeg", "png", "webp"], key=key, on_change=actions.return_upload,
                         args=(name, key), max_upload_size=int(settings.photo_max_mb))


def _review(data: Dict[str, Any], settings: Settings) -> None:
    product = data.get("product") or {}
    kit.html('<h1 class="am-h2">Check and submit</h1>')
    rows = [("Item", actions.CATEGORIES.get(product.get("category"), "")),
            ("Product", product.get("name")), ("Order", product.get("order_ref")),
            ("Purchased", product.get("purchase_date")), ("Reason", actions.REASONS.get(data.get("reason"), ""))]
    kit.html('<div class="am-report">' + "".join(
        f'<div class="am-row"><span class="am-row-label">{kit.text(k)}</span>'
        f'<span class="am-row-value">{kit.text(v)}</span></div>' for k, v in rows if v) + "</div>")
    kit.html('<p class="am-h3">Your description</p>')
    kit.plain(data.get("description") or "")
    accepted = [p for p in data.get("photos", {}).values() if p.get("status") == "accepted"]
    if accepted:
        with st.container(horizontal=True, wrap=True, gap="small"):
            for photo in accepted:
                st.image(photo["thumb"], width=120)
    if state.journey_source("return") == "mock":
        kit.html('<p class="am-small">Demo assessment: it checks your answers and that the required photos are '
                 'present, using draft demo rules. It doesn\'t analyse the photos.</p>')
    with st.container(horizontal=True, gap="small"):
        st.button("Back", key="v3_r_prev_4", type="tertiary", icon=":material/arrow_back:",
                  on_click=actions.return_go, args=(3,))
        st.button("Submit return request", key="v3_r_submit", type="primary", disabled=bool(data.get("case")),
                  on_click=actions.return_request_submit)


def _ids(case: Dict[str, Any]) -> None:
    code = actions.format_code(case.get("status_token", ""))
    kit.html(f'<div class="am-report"><div class="am-row"><span class="am-row-label">Return ID</span>'
             f'<span class="am-row-value"><span class="am-code">{kit.text(case["case_id"])}</span></span></div>'
             f'<div class="am-row"><span class="am-row-label">Status code</span>'
             f'<span class="am-row-value"><span class="am-code">{kit.text(code)}</span>'
             f'<span class="am-row-note">Save both to check your status later.</span></span></div></div>')


def _outcome(data: Dict[str, Any], settings: Settings) -> None:
    case, view = data.get("case") or {}, data.get("view")
    if not case:
        kit.error_notice(state.error_for("return"))
        return
    if not view:
        kit.html('<h1 class="am-h2">We\'re still reviewing your request.</h1>')
        _ids(case)
        kit.error_notice(state.error_for("return"))
        st.button("Check again", key="v3_r_check_again", type="primary", on_click=actions.return_assess)
        return
    label = customer_status(view)
    if label is None:
        state.record_violation("return", "contract_violation",
                               f"untrusted status combination for {case.get('case_id')}")
        kit.banner(NEUTRAL, "neutral")
        _ids(case)
        return
    status = view["assessment_status"]
    assessment = view.get("assessment") or {}
    if status == "approved":
        kit.html('<h1 class="am-h2">Your return is approved.</h1>')
        _ids(case)
        shipping = view.get("shipping") or {}
        kit.html('<p class="am-h3">Where to send it</p>'
                 f'<p class="am-body">{kit.text(shipping.get("destination"))}</p>'
                 + "".join(f'<p class="am-small">{kit.text(line)}</p>' for line in shipping.get("address_lines", [])))
        kit.bullet_list(shipping.get("instructions") or [])
        kit.banner("We inspect your item when it arrives. Final acceptance happens after that inspection. "
                   "Demo only: no shipping label or refund is created.", "neutral", title="Next steps.")
    elif status == "needs_more_info":
        kit.html('<h1 class="am-h2">We need a little more to decide.</h1>')
        _ids(case)
        _more_info(data, assessment, settings)
    elif status == "manual_review":
        kit.html('<h1 class="am-h2">A specialist will take a closer look.</h1>')
        _ids(case)
        kit.banner("Check this status page for the next update. Your photos are kept while we review them.",
                   "neutral")
    elif status == "rejected":
        kit.html('<h1 class="am-h2">We can\'t accept this return.</h1>')
        reasons = [r["text"] for r in assessment.get("reasons") or [] if r.get("result") == "fail"]
        kit.bullet_list(reasons)
        kit.html(f'<p class="am-small">Reference {kit.text(case["case_id"])}. No shipping instructions were created. '
                 'Photos from rejected requests are deleted under the demo retention rule.</p>')
    else:
        kit.html('<h1 class="am-h2">We\'re still reviewing your request.</h1>')
        _ids(case)
        st.button("Check again", key="v3_r_check_again", type="primary", on_click=actions.return_assess)
    if state.journey_source("return") == "mock":
        kit.html('<p class="am-small">Demo decision from draft rules, not ALDO policy. Photos were checked for '
                 'presence only.</p>')
    with st.container(horizontal=True, gap="small"):
        st.button("Check status", key="v3_r_to_status", on_click=state.go, args=("return_status",))
        st.button("Start another return", key="v3_r_restart", type="tertiary", on_click=actions.return_restart)
    kit.error_notice(state.error_for("return"))


def _more_info(data: Dict[str, Any], assessment: Dict[str, Any], settings: Settings) -> None:
    requested: List[Dict[str, Any]] = assessment.get("requested_info") or []
    kit.html('<p class="am-h3">Please add</p>')
    photos = data.get("photos", {})
    for item in requested:
        _slot({"slot": item["slot"], "label": item["label"], "hint": item.get("hint"), "required": True}, photos, data,
              settings)
    st.text_input("Anything else we should know? (optional)", key="v3_r_more_text")
    ready = all((photos.get(i["slot"]) or {}).get("status") == "accepted" for i in requested)
    st.button("Send", key="v3_r_send_more", type="primary", disabled=not ready,
              on_click=actions.return_request_more_info)
