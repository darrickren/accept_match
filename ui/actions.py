"""Customer-site actions: the only place screens reach the Agent client.

view -> action (here) -> state.run_v3 -> client. Every action is safe as an
on_click callback, sends a fresh client_request_id, keeps the customer's input
on errors, and discards responses that don't match the expected version.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import streamlit as st

from . import state
from .client.contracts import compact_context, new_request_id

SHOP = "shop"


# ----------------------------------------------------------------- Buy Defective: conversation

def shop_context() -> Optional[Dict[str, Any]]:
    turn = state.shop().get("turn")
    return turn.get("context") if turn else None


def shop_start(auto: bool = False) -> None:
    """Start the conversation. Automatic starts happen once; after a failure only the Retry button retries."""
    data = state.shop()
    if data.get("turn") or (auto and data.get("start_failed")):
        return
    data["start_failed"] = True
    if not data.get("session_id"):
        session_id = state.new_agent_session(SHOP, "shop")
        if not session_id:
            return
        data["session_id"] = session_id
    rid = new_request_id()
    result = state.run_v3(SHOP, "start_conversation",
                          lambda c: c.start_conversation(data["session_id"], rid), rid)
    if result:
        data["turn"] = result["turn"]
        data["start_failed"] = False


def _accept_turn(result: Optional[Dict[str, Any]], sent_version: int) -> bool:
    if not result:
        return False
    turn = result["turn"]
    if turn["context"]["context_version"] != sent_version + 1:
        state.record_violation(SHOP, "stale_response", "turn with an unexpected context version was discarded")
        return False
    data = state.shop()
    data["turn"] = turn
    data.pop("interpretation", None)
    return True


def _budget(context: Dict[str, Any]) -> Dict[str, Any]:
    settings = state.v3_settings()
    return compact_context(context, settings.context_max_answers, settings.context_max_bytes)


def shop_answer(question_id: str, values: List[str], text: Optional[str] = None) -> None:
    data = state.shop()
    context = shop_context()
    if not context:
        return
    ctx = _budget(context)
    rid = new_request_id()
    result = state.run_v3(SHOP, "submit_answer", lambda c: c.submit_answer(
        data["session_id"], question_id, {"values": values, "text": text}, ctx, rid), rid)
    if _accept_turn(result, ctx["context_version"]):
        data.setdefault("selection", {}).pop(question_id, None)
        data.setdefault("edit_defaults", {}).pop(question_id, None)


def shop_toggle(question_id: str, value: str, exclusive: bool) -> None:
    chosen = state.shop().setdefault("selection", {}).setdefault(question_id, [])
    if value in chosen:
        chosen.remove(value)
    elif exclusive:
        chosen[:] = [value]
    else:
        chosen[:] = [v for v in chosen if v not in ("none", "any")] + [value]


def shop_submit_selection(question_id: str) -> None:
    chosen = list(state.shop().get("selection", {}).get(question_id) or [])
    if chosen:
        shop_answer(question_id, chosen)


def shop_interpret(question_id: str, widget_key: str) -> None:
    data = state.shop()
    context = shop_context()
    text = (st.session_state.get(widget_key) or "").strip()
    if not context or not text:
        return
    ctx = _budget(context)
    rid = new_request_id()
    result = state.run_v3(SHOP, "interpret_answer", lambda c: c.interpret_answer(
        data["session_id"], question_id, text, ctx, rid), rid)
    if result and result.get("context_version") == ctx["context_version"]:
        data["interpretation"] = dict(result["interpretation"], text=text)


def shop_confirm_interpretation() -> None:
    found = state.shop().get("interpretation") or {}
    if found.get("understood"):
        shop_answer(found["question_id"], found["values"], found.get("text"))


def shop_dismiss_interpretation() -> None:
    state.shop().pop("interpretation", None)


def shop_update(patches: List[Dict[str, Any]]) -> bool:
    data = state.shop()
    context = shop_context()
    if not context:
        return False
    ctx = _budget(context)
    rid = new_request_id()
    result = state.run_v3(SHOP, "update_preferences", lambda c: c.update_preferences(
        data["session_id"], patches, ctx, rid), rid)
    return _accept_turn(result, ctx["context_version"])


def shop_edit(question_id: str) -> None:
    """Re-ask one question. Its current answer becomes the preselection; dependants are cleared by the Agent."""
    context = shop_context() or {}
    current = (context.get("answers") or {}).get(question_id, {}).get("values") or []
    data = state.shop()
    if shop_update([{"question_id": question_id, "values": None}]):
        data.setdefault("edit_defaults", {})[question_id] = list(current)
        data.setdefault("selection", {})[question_id] = list(current)
    state.go("shop")


def shop_back() -> None:
    order = (shop_context() or {}).get("order") or []
    if order:
        shop_edit(order[-1])


def shop_restart() -> None:
    state.reset_journey(SHOP)
    state.go("shop")


# ----------------------------------------------------------------- Buy Defective: results and detail

def shop_search(sort: Optional[str] = None) -> None:
    data = state.shop()
    context = shop_context()
    if not context:
        return
    if sort:
        data["sort"] = sort
    version = context["context_version"]
    prefs = context["preferences"]
    rid = new_request_id()
    result = state.run_v3(SHOP, "search_products", lambda c: c.search_products(
        data["session_id"], prefs, version, rid, data.get("sort", "best")), rid)
    data["searched_version"] = version
    if result is None:
        return
    if result.get("matched_against_context_version") != version:
        state.record_violation(SHOP, "stale_response", "search results for another preference version were dropped")
        return
    from .guardrails import screen_results

    data["results"] = screen_results(result, prefs)
    state.go("results")


def shop_set_sort(widget_key: str) -> None:
    shop_search(st.session_state.get(widget_key) or "best")


def shop_apply_suggestion(patches: List[Dict[str, Any]]) -> None:
    if shop_update(patches):
        shop_search()


def shop_open_listing(listing_id: str) -> None:
    data = state.shop()
    context = shop_context()
    if not context:
        return
    version = context["context_version"]
    rid = new_request_id()
    result = state.run_v3(SHOP, "get_listing", lambda c: c.get_listing(
        data["session_id"], listing_id, context["preferences"], version, rid), rid)
    if result is None:
        data["listing"] = {"listing_id": listing_id, "missing": True}
    elif result.get("matched_against_context_version") != version:
        state.record_violation(SHOP, "stale_response", "listing explanation for another preference version dropped")
        return
    else:
        from .guardrails import screen_listing

        data["listing"] = screen_listing(result, context["preferences"])
    state.go("listing", listing_id=listing_id)


# ----------------------------------------------------------------- Return

RET = "return"
REASONS = {"damaged": "It arrived damaged or got damaged", "not_working": "Something doesn't work",
           "not_as_described": "It's not as described", "changed_mind": "I changed my mind", "other": "Something else"}
CATEGORIES = {"handbag": "Handbag", "tote": "Tote", "crossbody": "Crossbody", "other": "Other bag"}
EXAMPLE_DESCRIPTION = "The zipper doesn't close properly, and there's a small scratch on the back of the bag."


def return_begin() -> None:
    data = state.returns()
    data.setdefault("step", 1)
    data.setdefault("photos", {})
    data.setdefault("nonce", {})
    if not data.get("session_id"):
        session_id = state.new_agent_session(RET, "return")
        if session_id:
            data["session_id"] = session_id


def return_go(step) -> None:
    state.returns()["step"] = step


def return_save_product() -> None:
    import datetime as dt
    import re

    data = state.returns()
    ss = st.session_state
    errors = []
    category, reason = ss.get("v3_r_category"), ss.get("v3_r_reason")
    order = (ss.get("v3_r_order") or "").strip().upper()
    if not category:
        errors.append("Choose what you're returning.")
    if not reason:
        errors.append("Choose a reason for the return.")
    if order and not re.fullmatch(r"DEMO-\d{4,6}", order):
        errors.append("This demo only accepts demo order IDs like DEMO-10023. Please don't enter a real order number.")
    purchased = ss.get("v3_r_date")
    if isinstance(purchased, dt.date) and purchased > dt.date.today():
        errors.append("The purchase date can't be in the future.")
    data["form_errors"] = errors
    if errors:
        return
    data["product"] = {"category": category, "name": (ss.get("v3_r_name") or "").strip()[:80] or None,
                       "order_ref": order or None,
                       "purchase_date": purchased.isoformat() if isinstance(purchased, dt.date) else None}
    data["reason"] = reason
    data["step"] = 2


def return_use_example() -> None:
    st.session_state["v3_r_desc"] = EXAMPLE_DESCRIPTION


def return_save_description() -> None:
    data = state.returns()
    text = (st.session_state.get("v3_r_desc") or "").strip()
    if len(text) < 8 and data.get("reason") != "changed_mind":
        data["form_errors"] = ["Tell us a little about what's wrong, in your own words."]
        return
    data["form_errors"] = []
    data["description"] = text[:2000]
    rid = new_request_id()
    result = state.run_v3(RET, "get_return_requirements", lambda c: c.get_return_requirements(
        data["session_id"], data["product"]["category"], data["reason"], data["description"], rid), rid)
    if result:
        data["requirements"] = result
        data["step"] = 3


def _bump(slot: str) -> None:
    nonce = state.returns().setdefault("nonce", {})
    nonce[slot] = nonce.get(slot, 0) + 1  # a new widget key clears the uploader


def return_upload(slot: str, widget_key: str) -> None:
    """Clean the photo locally (UX only), then create -> PUT -> complete; the service re-validates."""
    from .images import PhotoRejected, prepare_upload, thumbnail

    data = state.returns()
    file = st.session_state.get(widget_key)
    if file is None:
        return
    settings = state.v3_settings()
    photos = data.setdefault("photos", {})
    try:
        clean, info = prepare_upload(file.getvalue(), settings.photo_max_mb, settings.photo_max_px)
    except PhotoRejected as exc:
        photos[slot] = {"status": "rejected", "reason": str(exc)}
        _bump(slot)
        return
    sid = data["session_id"]
    rid = new_request_id()
    created = state.run_v3(RET, "create_photo_upload", lambda c: c.create_photo_upload(
        sid, sid, slot, info["content_type"], info["bytes"], info["sha256"], rid), rid)
    if not created:
        _bump(slot)
        return
    client = state.agent_client(RET)
    try:
        client.put_upload(created["upload_id"], clean)  # presigned PUT in the live client
    except Exception:  # noqa: BLE001 - surface, never retry silently
        state.set_error(RET, {"code": "agent_unavailable", "retryable": True,
                              "user_message": "We couldn't upload that photo. Please try again."})
        _bump(slot)
        return
    rid = new_request_id()
    done = state.run_v3(RET, "complete_photo_upload", lambda c: c.complete_photo_upload(
        sid, created["upload_id"], rid), rid)
    if done and done.get("validation_status") == "accepted":
        previous = (photos.get(slot) or {}).get("photo_id")
        if previous:
            _discard(previous)
        photos[slot] = {"status": "accepted", "photo_id": done["photo_id"], "thumb": thumbnail(clean),
                        "width": done.get("width"), "height": done.get("height")}
    elif done:
        photos[slot] = {"status": "rejected", "reason": done.get("reason") or "That photo can't be used."}
    _bump(slot)


def _discard(photo_id: str) -> None:
    data = state.returns()
    rid = new_request_id()
    state.run_v3(RET, "discard_photo", lambda c: c.discard_photo(data["session_id"], data["session_id"], photo_id,
                                                                  rid), rid)


def return_remove(slot: str) -> None:
    photo = state.returns().setdefault("photos", {}).pop(slot, None)
    if photo and photo.get("photo_id"):
        _discard(photo["photo_id"])
    _bump(slot)


def return_request_submit() -> None:
    state.returns()["pending"] = "submit"


def return_submit() -> None:
    data = state.returns()
    data.pop("pending", None)
    if data.get("case"):
        return  # already submitted: a double click can't create a second case
    photo_ids = [p["photo_id"] for p in data.get("photos", {}).values() if p.get("status") == "accepted"]
    rid = data.setdefault("submit_request_id", new_request_id())  # reused on retry, so the case is created once
    result = state.run_v3(RET, "submit_return", lambda c: c.submit_return(
        data["session_id"], data["session_id"], data["product"], data["reason"], data.get("description", ""),
        photo_ids, rid), rid)
    if result:
        data["case"] = {"case_id": result["case_id"], "status_token": result["status_token"],
                        "state_version": result["state_version"]}
        data["step"] = "outcome"
        return_assess()


def return_assess() -> None:
    data = state.returns()
    data.pop("pending", None)
    case = data.get("case")
    if not case:
        return
    rid = new_request_id()
    result = state.run_v3(RET, "assess_return", lambda c: c.assess_return(
        data["session_id"], case["case_id"], case["status_token"], rid), rid)
    if result:
        data["view"] = result
        case["state_version"] = result.get("state_version", case["state_version"])
        _keep_for_status(case)
    elif (state.error_for(RET) or {}).get("code") == "agent_timeout":
        data["view"] = {"case_id": case["case_id"], "assessment_status": "pending", "logistics_status": "not_started",
                        "verification_result": None, "shipping": None}


def return_request_more_info() -> None:
    state.returns()["pending"] = "more_info"


def return_add_info() -> None:
    data = state.returns()
    data.pop("pending", None)
    case, view = data.get("case"), data.get("view") or {}
    requested = [r["slot"] for r in ((view.get("assessment") or {}).get("requested_info") or [])]
    photo_ids = [data["photos"][s]["photo_id"] for s in requested
                 if (data.get("photos", {}).get(s) or {}).get("status") == "accepted"]
    text = (st.session_state.get("v3_r_more_text") or "").strip()[:1000]
    rid = new_request_id()
    result = state.run_v3(RET, "add_return_info", lambda c: c.add_return_info(
        data["session_id"], case["case_id"], case["status_token"], text, photo_ids, case["state_version"], rid), rid)
    if result:
        case["state_version"] = result["state_version"]
        return_assess()
    elif (state.error_for(RET) or {}).get("code") == "state_conflict":
        return_refresh()


def return_refresh() -> None:
    data = state.returns()
    case = data.get("case")
    if not case:
        return
    rid = new_request_id()
    result = state.run_v3(RET, "get_return_case", lambda c: c.get_return_case(
        data["session_id"], case["case_id"], case["status_token"], rid), rid)
    if result:
        data["view"] = result
        case["state_version"] = result["state_version"]


def _keep_for_status(case: Dict[str, Any]) -> None:
    st.session_state["v3_r_status_case"] = case["case_id"]
    st.session_state["v3_r_status_code"] = format_code(case["status_token"])


def format_code(token: str) -> str:
    token = (token or "").replace("-", "").upper()
    return f"{token[:4]}-{token[4:]}" if len(token) == 8 else token


def return_check_status() -> None:
    data = state.returns()
    lookup = data.setdefault("lookup", {})
    case_id = (st.session_state.get("v3_r_status_case") or "").strip().upper()
    code = (st.session_state.get("v3_r_status_code") or "").strip()
    if not case_id or not code:
        lookup.update(result=None, message="Enter your return ID and status code.")
        return
    if not data.get("status_session"):
        session_id = state.new_agent_session(RET, "status")
        if not session_id:
            return
        data["status_session"] = session_id
    rid = new_request_id()
    result = state.run_v3(RET, "get_return_case", lambda c: c.get_return_case(
        data["status_session"], case_id, code, rid), rid)
    lookup.update(result=result, message=None)


def return_restart() -> None:
    state.reset_journey(RET)
    state.go("return")
