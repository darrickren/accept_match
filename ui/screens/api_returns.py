"""Return in api mode: demo order, the backend's questions, shoe photos, then the backend's review.

Rendered by returns.py / return_status.py when ACCEPTMATCH_AGENT[_RETURN]=api.
Statuses, missing views and the demo address come from the FastAPI backend.
APPROVED_DEMO is shown as a demo review result, never as an ALDO refund.
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from .. import api_actions, kit, state
from ..client.api_mapper import VIEW_GUIDE
from ..config import Settings
from ..status import AI_OBSERVATION, DEMO_DISCLAIMER, NEUTRAL, api_return_status
from .api_shop import chat_bubbles, questions, reference

RET = "return"
STEPS = ("Order", "Issue", "Photos", "Review")
TIPS = "Daylight, a plain background, both shoes in frame. Please don't include people, receipts or documents."


def render_flow(settings: Settings) -> None:
    api_actions.return_begin()
    data = state.returns()
    kit.back_button("Home", "home", "v3_r_back_home")
    kit.html(f'<p class="am-eyebrow">Return &nbsp;{kit.source_tag(RET)}</p>')
    if not data.get("session_id"):
        kit.error_notice(state.error_for(RET))
        return
    pending = data.get("pending")
    if pending:
        with st.spinner("Reviewing your photos…" if pending == "evaluate" else "Sending your request…"):
            {"submit": api_actions.return_submit, "evaluate": api_actions.return_evaluate}[pending]()
    step = data.get("step", 1)
    if step == "outcome":
        _outcome(data, settings)
        return
    _stepper(step)
    {1: _order, 2: _issue, 3: _photos, 4: _review}[step](data, settings)
    for message in data.get("form_errors") or []:
        kit.banner(message, "error")
    retry = api_actions.return_retry if data.get("pending_send") else (
        api_actions.return_request_submit if step == 4 else None)
    kit.error_notice(state.error_for(RET), retry_key="v3_r_api_retry" if retry else None, on_retry=retry)


def _stepper(step: int) -> None:
    parts = []
    for number, label in enumerate(STEPS, start=1):
        cls = "on" if number == step else ("done" if number < step else "")
        mark = "✓ " if number < step else f"{number} "
        parts.append(f'<span class="{cls}">{mark}{kit.text(label)}</span>')
    kit.html(f'<div class="am-steps" aria-label="Return steps">{"".join(parts)}</div>')


def _order(data: Dict[str, Any], settings: Settings) -> None:
    kit.html('<h1 class="am-h2">Which order is it from?</h1>'
             '<p class="am-lede">This demo reviews shoe returns from demo orders.</p>')
    left, right = st.columns([3, 1])
    with left:
        st.text_input("Demo order ID", key="v3_r_api_order", placeholder="ORD-001",
                      help="Demo only: please don't enter a real order number.")
    with right:
        st.number_input("Line", min_value=1, max_value=20, step=1, value=1, key="v3_r_api_line",
                        help="Which item on the order. 1 is the first.")
    st.radio("Type of return", list(api_actions.CLAIMS), index=None, key="v3_r_api_claim",
             format_func=api_actions.CLAIMS.get)
    st.selectbox("Reason", list(api_actions.REASONS), index=None, key="v3_r_api_reason",
                 format_func=api_actions.REASONS.get, placeholder="Choose a reason")
    kit.html('<p class="am-small">The purchase date and channel come from the demo order. We don\'t ask for your '
             'name, address or contact details.</p>')
    st.button("Continue", key="v3_r_api_next_1", type="primary", on_click=api_actions.return_save_order)


def _issue(data: Dict[str, Any], settings: Settings) -> None:
    chat = data.get("chat")
    if not chat:
        kit.html('<h1 class="am-h2">What happened?</h1>'
                 '<p class="am-lede">In your own words: where the issue is (heel, sole, toe, stitching), when you '
                 'noticed it, and whether they\'ve been worn or repaired.</p>')
        st.text_area("Describe the issue", key="v3_r_api_desc", height=130, label_visibility="collapsed",
                     placeholder=f"For example: {api_actions.EXAMPLE_DESCRIPTION}", max_chars=2000)
        with st.container(horizontal=True, gap="small"):
            st.button("Back", key="v3_r_api_prev_2", type="tertiary", icon=":material/arrow_back:",
                      on_click=api_actions.return_go, args=(1,))
            st.button("Use the example", key="v3_r_api_example", on_click=api_actions.return_use_example)
            st.button("Continue", key="v3_r_api_next_2", type="primary", on_click=api_actions.return_describe)
        return
    kit.html('<h1 class="am-h2">A few questions</h1>')
    chat_bubbles(data.get("history") or [])
    questions(chat, data, "v3_r_api", api_actions.return_choose)
    for note in chat.get("errors") or []:
        kit.banner(note, "neutral")
    turn = len(data.get("history") or [])
    key = f"v3_r_api_answer_{turn}"
    with st.container(key="am-freetext", horizontal=True, vertical_alignment="bottom", gap="small"):
        st.text_input("Your answer", key=key, max_chars=500)
        st.button("Send", key=f"v3_r_api_send_{turn}", type="primary", on_click=api_actions.return_answer,
                  args=(key,))
    if data.get("required"):
        st.button("Go to photos", key="v3_r_api_to_photos", on_click=api_actions.return_go, args=(3,))
    reference(chat.get("request_id"))


def photo_slot(slot: Dict[str, Any], data: Dict[str, Any], settings: Settings, prefix: str = "v3_r_api_up") -> None:
    view = slot["view"]
    photo = (data.get("photos") or {}).get(view) or {}
    with st.container(key=f"am-slot-{view}"):
        kit.html(f'<p class="am-h3" style="font-size:1.02rem">{kit.text(slot["label"])} '
                 f'{kit.pill("Requested", "possible")}</p>'
                 + (f'<p class="am-small"><b>Why:</b> {kit.text(slot["reason"])}</p>' if slot.get("reason") else "")
                 + f'<p class="am-small">{kit.text(slot.get("hint"))}</p>')
        if photo.get("status") == "accepted":
            st.image(photo["thumb"], width=220)
            kit.html(f'<p class="am-ref">Photo ID {kit.text(photo["photo_id"])}</p>')
        elif photo.get("status") == "rejected":
            kit.banner(photo.get("reason") or "That photo can't be used.", "error")
        key = f"{prefix}_{view}_{data.get('nonce', {}).get(view, 0)}"
        st.file_uploader("Replace photo" if photo.get("status") == "accepted" else "Add photo",
                         type=["jpg", "jpeg", "png", "webp"], key=key, on_change=api_actions.return_upload,
                         args=(view, key), max_upload_size=int(settings.photo_max_mb))


def _slots(slots: List[Dict[str, Any]], data: Dict[str, Any], settings: Settings) -> None:
    for start in range(0, len(slots), 2):
        columns = st.columns(2, gap="medium")
        for column, slot in zip(columns, slots[start:start + 2]):
            with column:
                photo_slot(slot, data, settings)


def _photos(data: Dict[str, Any], settings: Settings) -> None:
    required = data.get("required") or []
    photos = data.get("photos", {})
    done = sum(1 for s in required if (photos.get(s["view"]) or {}).get("status") == "accepted")
    kit.html('<h1 class="am-h2">Photos of both shoes</h1>'
             f'<p class="am-lede">The review asked for {len(required)} photos. {kit.text(TIPS)}</p>'
             f'<p class="am-small" style="margin:.6rem 0 1rem">{done} of {len(required)} added. The review checks '
             'what each photo actually shows; these labels only guide you.</p>')
    _slots(required, data, settings)
    with st.container(horizontal=True, gap="small"):
        st.button("Back", key="v3_r_api_prev_3", type="tertiary", icon=":material/arrow_back:",
                  on_click=api_actions.return_go, args=(2,))
        st.button("Continue", key="v3_r_api_next_3", type="primary", disabled=not api_actions.ready_to_submit(data),
                  on_click=api_actions.return_go, args=(4,))


def _review(data: Dict[str, Any], settings: Settings) -> None:
    order = data.get("order") or {}
    kit.html('<h1 class="am-h2">Check and submit</h1>')
    rows = [("Demo order", order.get("order_id")), ("Line", order.get("order_line")),
            ("Type", api_actions.CLAIMS.get(order.get("claim_type"), "")), ("Reason", order.get("reason_label"))]
    kit.html('<div class="am-report">' + "".join(
        f'<div class="am-row"><span class="am-row-label">{kit.text(k)}</span>'
        f'<span class="am-row-value">{kit.text(v)}</span></div>' for k, v in rows if v) + "</div>")
    if data.get("description"):
        kit.html('<p class="am-h3">Your description</p>')
        kit.plain(data["description"])
    accepted = [(view, p) for view, p in (data.get("photos") or {}).items() if p.get("status") == "accepted"]
    if accepted:
        with st.container(horizontal=True, wrap=True, gap="small"):
            for view, photo in accepted:
                st.image(photo["thumb"], width=120, caption=VIEW_GUIDE.get(view, (None,))[0])
    kit.html(f'<p class="am-small">The review applies demo rules to your answers and photos. '
             f'{kit.text(AI_OBSERVATION)}</p>')
    with st.container(horizontal=True, gap="small"):
        st.button("Back", key="v3_r_api_prev_4", type="tertiary", icon=":material/arrow_back:",
                  on_click=api_actions.return_go, args=(3 if data.get("required") else 2,))
        st.button("Submit return request", key="v3_r_api_submit", type="primary",
                  disabled=bool(data.get("return")) and data.get("evaluated", False),
                  on_click=api_actions.return_request_submit)


def ids(view: Dict[str, Any]) -> None:
    kit.html(f'<div class="am-report"><div class="am-row"><span class="am-row-label" style="white-space:nowrap">'
             f'Return ID</span>'
             f'<span class="am-row-value"><span class="am-code">{kit.text(view["return_id"])}</span>'
             f'<span class="am-row-note">Save it to check your status. Status is linked to this demo account.'
             f'</span></span></div></div>')


def analysis_note(view: Dict[str, Any]) -> None:
    source, model = view.get("analysis_source"), view.get("model_id")
    if not source:
        return
    if str(source).upper() == "SIMULATED":
        kit.html('<p class="am-small">Photo analysis in this environment is simulated, not a real AI model.</p>')
    elif model:
        kit.html(f'<p class="am-small">Photos were analysed by {kit.text(model)}. {kit.text(AI_OBSERVATION)}</p>')
    else:
        kit.html(f'<p class="am-small">{kit.text(AI_OBSERVATION)}</p>')


def _outcome(data: Dict[str, Any], settings: Settings) -> None:
    view = data.get("return")
    if not view:
        kit.html('<h1 class="am-h2">Your request wasn\'t sent yet.</h1>')
        kit.error_notice(state.error_for(RET), retry_key="v3_r_api_retry_submit",
                         on_retry=api_actions.return_request_submit)
        return
    if not data.get("evaluated"):
        kit.html('<h1 class="am-h2">Your request is saved. The review didn\'t finish.</h1>')
        ids(view)
        kit.error_notice(state.error_for(RET) or {"user_message": "The review didn't finish. Please try again.",
                                                  "retryable": True},
                         retry_key="v3_r_api_retry_eval", on_retry=api_actions.return_request_evaluate)
        return
    label = api_return_status(view)
    if label is None:
        state.record_violation(RET, "contract_violation", f"untrusted status combination for {view['return_id']}")
        kit.banner(NEUTRAL, "neutral")
        ids(view)
        return
    status = view["status"]
    if status == "APPROVED_DEMO":
        kit.html('<h1 class="am-h2">Approved in the demo review.</h1>')
        kit.banner(DEMO_DISCLAIMER, "neutral", title="Demo review only.")
        ids(view)
        if view.get("demo_address"):
            kit.html('<p class="am-h3">Where to send it</p>'
                     + "".join(f'<p class="am-small">{kit.text(line)}</p>' for line in view["demo_address"]))
        if view.get("inventory_label"):
            kit.html(f'<p class="am-small">Item status: {kit.text(view["inventory_label"])}. We inspect it when it '
                     'arrives; it isn\'t resold before that.</p>')
    elif status == "NEEDS_MORE_EVIDENCE":
        kit.html('<h1 class="am-h2">We need a few more photos.</h1>')
        ids(view)
        _more_photos(data, view, settings)
    elif status == "HUMAN_REVIEW_REQUIRED":
        kit.html('<h1 class="am-h2">A specialist will review your request.</h1>')
        ids(view)
        kit.banner("This demo uses a simulated review queue. Check your status page for the next update.", "neutral")
    elif status == "REJECTED_AFTER_REVIEW":
        kit.html('<h1 class="am-h2">Your request wasn\'t accepted after review.</h1>')
        ids(view)
        kit.banner("This is a demo review decision, not an ALDO decision.", "neutral")
    else:
        kit.html('<h1 class="am-h2">We\'re still collecting details.</h1>')
        ids(view)
        st.button("Check again", key="v3_r_api_check_again", type="primary", on_click=api_actions.return_refresh)
    if view.get("next_step"):
        kit.html(f'<p class="am-body" style="margin-top:.6rem">{kit.text(view["next_step"])}</p>')
    analysis_note(view)
    reference(view.get("request_id"))
    with st.container(horizontal=True, gap="small"):
        st.button("Check status", key="v3_r_to_status", on_click=state.go, args=("return_status",))
        st.button("Start another return", key="v3_r_restart", type="tertiary", on_click=api_actions.return_restart)
    kit.error_notice(state.error_for(RET))


def _more_photos(data: Dict[str, Any], view: Dict[str, Any], settings: Settings) -> None:
    missing = view.get("missing") or []
    if view.get("retake_reason"):
        kit.banner(view["retake_reason"], "possible", title="Why.")
    if missing:
        kit.html('<p class="am-h3">Please add</p>')
        kit.bullet_list(f"{slot['label']}" + (f": {slot['reason']}" if slot.get("reason") else "") for slot in missing)
        _slots(missing, data, settings)
    ready = api_actions.ready_to_submit(data) and bool(missing)
    st.button("Send for review", key="v3_r_api_send_more", type="primary", disabled=not ready,
              on_click=api_actions.return_request_evaluate)


def render_status(settings: Settings) -> None:
    api_actions.return_begin()
    data = state.returns()
    kit.back_button("Back to returns", "return", "v3_r_status_back")
    kit.heading("Check a return", eyebrow="Return",
                lede="Enter your return ID. You can see the returns made with this demo account.")
    st.text_input("Return ID", key="v3_r_api_status_id", placeholder="RET-001")
    st.button("Check status", key="v3_r_status_go", type="primary", on_click=api_actions.return_check_status)
    lookup = data.get("lookup") or {}
    if lookup.get("message"):
        kit.banner(lookup["message"], "neutral")
    error = state.error_for(RET)
    if lookup and lookup.get("result") is None and error:
        message = error.get("user_message") or NEUTRAL
        if error.get("code") == "not_found":
            message = "We couldn't find a return with that ID for this demo account."
        kit.banner(message, "neutral")
        return
    view = lookup.get("result")
    if not view:
        return
    label = api_return_status(view)
    if label is None:
        state.record_violation(RET, "contract_violation", f"untrusted status combination for {view['return_id']}")
        kit.banner(NEUTRAL, "neutral")
        return
    kit.html(f'<p class="am-h3" style="margin-top:1.4rem">{kit.text(view["return_id"])}</p>'
             f'<p class="am-question" style="font-size:1.8rem">{kit.text(label)}</p>')
    rows = [("Type", view.get("claim_label")), ("Demo order", view.get("order_id")),
            ("Item status", view.get("inventory_label"))]
    present = [(k, v) for k, v in rows if v]
    if present:
        kit.html('<div class="am-report">' + "".join(
            f'<div class="am-row"><span class="am-row-label">{kit.text(k)}</span>'
            f'<span class="am-row-value">{kit.text(v)}</span></div>' for k, v in present) + "</div>")
    if view["status"] == "NEEDS_MORE_EVIDENCE" and view.get("missing"):
        kit.html('<p class="am-small">Still needed: ' + kit.text(", ".join(s["label"] for s in view["missing"])) + "</p>")
    if view["status"] == "APPROVED_DEMO":
        kit.banner(DEMO_DISCLAIMER, "neutral", title="Demo review only.")
        if view.get("demo_address"):
            kit.html("".join(f'<p class="am-small">{kit.text(line)}</p>' for line in view["demo_address"]))
    if view.get("next_step"):
        kit.plain(view["next_step"])
    analysis_note(view)
    reference(view.get("request_id"))
