"""Customer-site actions for api mode (ACCEPTMATCH_AGENT=api): FastAPI backend over HTTP.

view -> action (here) -> state.run_v3 -> ApiAgentClient -> /api/*. Mirrors
ui/actions.py (mock mode) where the flows match and differs where the backend
contract differs: the conversation is server-side, every imperfection is
accepted explicitly, and checkout is idempotent. Safe as on_click callbacks.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

import streamlit as st

from . import state
from .client.contracts import new_request_id

SHOP = "shop"
RET = "return"
HISTORY_LIMIT = 12
ORDER_ID = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")
CLAIMS = {"DEFECT_CLAIM": "It's defective or damaged", "STANDARD_RETURN": "Standard return (unworn, in its box)"}
REASONS = {"defect": "A defect or damage", "fit": "It doesn't fit", "not_as_described": "It's not as described",
           "changed_mind": "I changed my mind", "other": "Something else"}
EXAMPLE_DESCRIPTION = ("There's a scratch on the back of the left heel. I noticed it the first time I wore them "
                       "indoors. They haven't been repaired.")


def _history(data: Dict[str, Any], who: str, text: str) -> None:
    rows = data.setdefault("history", [])
    rows.append({"who": who, "text": text[:600]})
    del rows[:-HISTORY_LIMIT]


# ----------------------------------------------------------------- Buy Defective: conversation

def shop_start(auto: bool = False) -> None:
    """Create the backend session once. After a failure only the Retry button retries."""
    data = state.shop()
    if data.get("api_open") or (auto and data.get("start_failed")):
        return
    data["start_failed"] = True
    if not data.get("session_id"):
        session_id = state.new_agent_session(SHOP, "shop")
        if not session_id:
            return
        data["session_id"] = session_id
    rid = new_request_id()
    result = state.run_v3(SHOP, "open_chat", lambda c: c.open_chat(data["session_id"], "shopping", rid), rid)
    if result is None:
        return
    data["api_open"], data["start_failed"] = True, False
    if result.get("chat"):
        _apply_shop_chat(data, result["chat"])


def _apply_shop_chat(data: Dict[str, Any], chat: Dict[str, Any]) -> None:
    data["chat"] = chat
    data["selection"] = {}
    if chat["message"]:
        _history(data, "assistant", chat["message"])
    cards = data.setdefault("cards", {})
    for item in chat["items"]:
        cards[item["item_id"]] = item
    if chat["items"] or chat["next_action"] == "SHOW_ITEMS" or chat["blocking_constraints"]:
        data["results"] = {"mode": "chat", "items": chat["items"], "next_cursor": None,
                           "blocking": chat["blocking_constraints"], "message": chat["message"],
                           "request_id": chat["request_id"]}
    elif (data.get("results") or {}).get("mode") == "chat" and chat["next_action"] != "REQUEST_ACCEPTANCE":
        data.pop("results")  # the conversation moved on: earlier matches may no longer fit what was said


def shop_send(text: Optional[str], rid: Optional[str] = None) -> bool:
    data = state.shop()
    text = " ".join((text or "").split())[:500]
    if not text or not data.get("api_open"):
        return False
    rid = rid or new_request_id()
    data["pending_send"] = {"text": text, "rid": rid}
    result = state.run_v3(SHOP, "send_message", lambda c: c.send_message(data["session_id"], "shopping", text, [],
                                                                          rid), rid)
    if result is None:
        return False  # the error is shown; pending_send stays for "Try again" with the same request id
    data.pop("pending_send", None)
    _history(data, "you", text)
    _apply_shop_chat(data, result["chat"])
    return True


def shop_send_input(widget_key: str) -> None:
    if shop_send(st.session_state.get(widget_key)):
        st.session_state[widget_key] = ""


def shop_retry() -> None:
    """Re-send the last message with the same client_request_id: a finished turn is replayed, not repeated."""
    pending = state.shop().get("pending_send")
    if pending:
        shop_send(pending["text"], pending["rid"])
    elif not state.shop().get("api_open"):
        shop_start()


def shop_choose(label: str) -> None:
    shop_send(label)


def shop_toggle(question_id: str, label: str) -> None:
    chosen = state.shop().setdefault("selection", {}).setdefault(question_id, [])
    if label in chosen:
        chosen.remove(label)
    else:
        chosen.append(label)


def shop_submit_selection(question_id: str) -> None:
    chosen = list(state.shop().get("selection", {}).get(question_id) or [])
    if chosen:
        shop_send(", ".join(chosen))


def shop_restart() -> None:
    state.reset_journey(SHOP)
    state.go("shop")


# ----------------------------------------------------------------- Buy Defective: inventory

def _cents(dollars: Any) -> Optional[int]:
    try:
        value = float(dollars)
    except (TypeError, ValueError):
        return None
    return int(round(value * 100)) if value > 0 else None


def shop_browse() -> None:
    """GET /api/items with the browse filters (same inventory the assistant searches)."""
    ss = st.session_state
    filters = {"category": "SHOES", "size_system": ss.get("v3_s_b_system") or None,
               "size_value": (str(ss.get("v3_s_b_size") or "").strip()[:6] or None),
               "max_price_cents": _cents(ss.get("v3_s_b_max"))}
    _search(filters, None)


def shop_more() -> None:
    results = state.shop().get("results") or {}
    if results.get("next_cursor"):
        _search(results.get("filters") or {}, results["next_cursor"])


def _search(filters: Dict[str, Any], cursor: Optional[str]) -> None:
    data = state.shop()
    rid = new_request_id()
    result = state.run_v3(SHOP, "search_items", lambda c: c.search_items(filters, cursor, rid), rid)
    if result is None:
        return
    previous = (data.get("results") or {}).get("items") if cursor else []
    items = list(previous or []) + [i for i in result["items"] if i["item_id"] not in
                                    {p["item_id"] for p in previous or []}]
    data["results"] = {"mode": "browse", "items": items, "next_cursor": result["next_cursor"], "filters": filters,
                       "blocking": [], "message": None, "request_id": result.get("request_id")}
    cards = data.setdefault("cards", {})
    for item in result["items"]:
        cards.setdefault(item["item_id"], item)
    state.go("results")


def shop_open_item(item_id: str) -> None:
    data = state.shop()
    data.pop("purchase_error", None)
    if _load_item(item_id) is None:
        data["item"] = {"item_id": item_id, "missing": True}
    state.go("listing", listing_id=item_id)


def _load_item(item_id: str) -> Optional[Dict[str, Any]]:
    data = state.shop()
    rid = new_request_id()
    result = state.run_v3(SHOP, "get_item", lambda c: c.get_item(item_id, rid), rid)
    if result is None:
        return None
    data["item"] = {"item_id": item_id, "item": result["item"]}
    return result["item"]


def accept_key(item_id: str, version: int, defect_id: str) -> str:
    """Checkbox key per listing version: a new version starts with every box unticked."""
    return f"v3_s_acc_{item_id}_v{version}_{defect_id}"


def _current_item() -> Optional[Dict[str, Any]]:
    entry = state.shop().get("item") or {}
    return entry.get("item")


def shop_accept(item_id: str) -> None:
    """POST acceptances for every disclosed imperfection, only after each one is ticked by the customer."""
    data = state.shop()
    item = _current_item()
    if not item or item["item_id"] != item_id:
        return
    version = item["listing_version"]
    defect_ids = [d["defect_id"] for d in item["defects"]]
    if not all(st.session_state.get(accept_key(item_id, version, d)) for d in defect_ids):
        data["purchase_error"] = {"code": "incomplete_acceptances",
                                  "user_message": "Please tick each imperfection to confirm you accept it."}
        return
    data.pop("purchase_error", None)
    rid = new_request_id()
    result = state.run_v3(SHOP, "accept_defects", lambda c: c.accept_defects(item_id, version, defect_ids, rid), rid)
    if result is not None:
        data.setdefault("accepted", {})[f"{item_id}@v{version}"] = sorted(result["defect_ids"])
        return
    _purchase_failed(item_id, version)


def shop_checkout(item_id: str) -> None:
    """POST /api/demo-checkout. The idempotency key is fixed per item version, so retries can't buy twice."""
    data = state.shop()
    item = _current_item()
    if not item or item["item_id"] != item_id or data.get("purchases", {}).get(item_id):
        return
    version = item["listing_version"]
    key = f"{item_id}@v{version}"
    if item["defects"] and not data.get("accepted", {}).get(key):
        data["purchase_error"] = {"code": "incomplete_acceptances",
                                  "user_message": "Please confirm each imperfection before buying."}
        return
    data.pop("purchase_error", None)
    idempotency_key = data.setdefault("checkout_keys", {}).setdefault(key, "am-buy-" + uuid.uuid4().hex)
    rid = new_request_id()
    result = state.run_v3(SHOP, "checkout", lambda c: c.checkout(item_id, version, idempotency_key, rid), rid)
    if result is None:
        _purchase_failed(item_id, version)
        return
    data.setdefault("purchases", {})[item_id] = dict(result["purchase"], request_id=result.get("request_id"))
    _load_item(item_id)  # show the backend's new status (SOLD); a failure here doesn't undo the purchase


def _purchase_failed(item_id: str, version: int) -> None:
    """Show the error next to the purchase controls, and reload the listing when the backend says it changed."""
    data = state.shop()
    error = dict(state.error_for(SHOP) or {})
    state.clear_error(SHOP)
    data["purchase_error"] = error
    code = error.get("code")
    if code in ("stale_listing", "item_unavailable", "incomplete_acceptances"):
        data.get("accepted", {}).pop(f"{item_id}@v{version}", None)
    if code in ("stale_listing", "item_unavailable"):
        _load_item(item_id)  # new listing_version -> fresh, unticked checkboxes; or the new status (SOLD)


# ----------------------------------------------------------------- Return

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


def return_save_order() -> None:
    data = state.returns()
    ss = st.session_state
    order_id = (ss.get("v3_r_api_order") or "").strip().upper()
    claim, reason = ss.get("v3_r_api_claim"), ss.get("v3_r_api_reason")
    line = ss.get("v3_r_api_line") or 1
    errors = []
    if not ORDER_ID.match(order_id):
        errors.append("Enter the demo order ID you were given, like ORD-001. Please don't enter a real order number.")
    if claim not in CLAIMS:
        errors.append("Choose the type of return.")
    if reason not in REASONS:
        errors.append("Choose a reason.")
    data["form_errors"] = errors
    if errors:
        return
    data["order"] = {"order_id": order_id, "order_line": str(int(line)), "claim_type": claim, "reason": reason,
                     "reason_label": REASONS[reason]}
    data["step"] = 2


def return_use_example() -> None:
    st.session_state["v3_r_api_desc"] = EXAMPLE_DESCRIPTION


def return_describe() -> None:
    """Send the facts and the customer's own words as the first message; the backend asks what it still needs."""
    data = state.returns()
    order = data.get("order") or {}
    text = " ".join((st.session_state.get("v3_r_api_desc") or "").split())
    if len(text) < 8 and order.get("claim_type") == "DEFECT_CLAIM":
        data["form_errors"] = ["Tell us what's wrong, in your own words."]
        return
    data["form_errors"] = []
    data["description"] = text[:2000]
    claim = "defect claim" if order.get("claim_type") == "DEFECT_CLAIM" else "standard return"
    message = (f"Return request for demo order {order.get('order_id')}, line {order.get('order_line')}. "
               f"Type: {claim}. Reason: {order.get('reason_label', '').lower()}. "
               f"What happened: {data['description'] or 'nothing to add'}")
    _return_send(message)


def return_answer(widget_key: str) -> None:
    if _return_send(st.session_state.get(widget_key)):
        st.session_state[widget_key] = ""


def return_choose(label: str) -> None:
    _return_send(label)


def return_retry() -> None:
    pending = state.returns().get("pending_send")
    if pending:
        _return_send(pending["text"], pending["rid"])


def _return_send(text: Optional[str], rid: Optional[str] = None) -> bool:
    data = state.returns()
    text = " ".join((text or "").split())[:2400]
    if not text or not data.get("session_id"):
        return False
    rid = rid or new_request_id()
    data["pending_send"] = {"text": text, "rid": rid}
    result = state.run_v3(RET, "send_message", lambda c: c.send_message(data["session_id"], "return", text, [], rid),
                          rid)
    if result is None:
        return False
    data.pop("pending_send", None)
    _history(data, "you", text)
    chat = result["chat"]
    data["chat"] = chat
    if chat["message"]:
        _history(data, "assistant", chat["message"])
    if chat["required_photos"]:
        data["required"] = chat["required_photos"]
    if chat["next_action"] == "UPLOAD_PHOTOS":
        data["step"] = 3
    elif chat["next_action"] in ("HUMAN_REVIEW", "SHOW_RETURN_STATUS", "COMPLETE") and data.get("step") == 2:
        data["step"] = 3 if data.get("required") else 4
    return True


def _bump(view: str) -> None:
    nonce = state.returns().setdefault("nonce", {})
    nonce[view] = nonce.get(view, 0) + 1  # a new widget key clears the uploader


def return_upload(view: str, widget_key: str) -> None:
    """Clean the photo locally (EXIF dropped, re-encoded), then POST /api/uploads; keep the backend's photo_id."""
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
        photos[view] = {"status": "rejected", "reason": str(exc)}
        _bump(view)
        return
    rid = new_request_id()
    result = state.run_v3(RET, "upload_photo", lambda c: c.upload_photo(data["session_id"], view, clean,
                                                                        info["content_type"], rid), rid)
    if result:
        photos[view] = {"status": "accepted", "photo_id": result["photo_id"], "thumb": thumbnail(clean)}
    else:
        error = state.error_for(RET) or {}
        if error.get("code") in ("photo_rejected", "invalid_input"):
            photos[view] = {"status": "rejected", "reason": error.get("user_message")}
            state.clear_error(RET)
    _bump(view)


def return_request_submit() -> None:
    state.returns()["pending"] = "submit"


def return_request_evaluate() -> None:
    state.returns()["pending"] = "evaluate"


def return_submit() -> None:
    """Create the case once (same Idempotency-Key on every retry), then ask the backend to evaluate it."""
    data = state.returns()
    data.pop("pending", None)
    if data.get("evaluated"):
        data["step"] = "outcome"  # already decided: a repeated Submit neither creates nor re-evaluates
        return
    if not data.get("return"):
        order = data.get("order") or {}
        rid = data.setdefault("create_rid", new_request_id())
        result = state.run_v3(RET, "create_return", lambda c: c.create_return(
            data["session_id"], order["order_id"], order["order_line"], order["claim_type"], order["reason"], rid), rid)
        if result is None:
            return
        data["return"] = result["return"]
        st.session_state["v3_r_api_status_id"] = result["return"]["return_id"]
    data["step"] = "outcome"
    return_evaluate()


def return_evaluate() -> None:
    data = state.returns()
    data.pop("pending", None)
    current = data.get("return")
    if not current:
        return
    rid = data.setdefault("evaluate_rid", new_request_id())  # reused if this round is retried
    result = state.run_v3(RET, "evaluate_return", lambda c: c.evaluate_return(current["return_id"], rid), rid)
    if result is None:
        return
    data.pop("evaluate_rid", None)
    data["return"] = result["return"]
    data["evaluated"] = True
    if result["return"]["status"] == "NEEDS_MORE_EVIDENCE":
        data["required"] = result["return"]["missing"]
        for slot in result["return"]["missing"]:  # requested views must be photographed again
            if slot["view"] in data.get("photos", {}):
                data["photos"].pop(slot["view"], None)
                _bump(slot["view"])


def return_refresh() -> None:
    data = state.returns()
    current = data.get("return")
    if not current:
        return
    rid = new_request_id()
    result = state.run_v3(RET, "get_return", lambda c: c.get_return(current["return_id"], rid), rid)
    if result:
        data["return"] = result["return"]


def return_check_status() -> None:
    data = state.returns()
    lookup = data.setdefault("lookup", {})
    return_id = (st.session_state.get("v3_r_api_status_id") or "").strip().upper()
    if not return_id:
        lookup.update(result=None, message="Enter your return ID.")
        return
    rid = new_request_id()
    result = state.run_v3(RET, "get_return", lambda c: c.get_return(return_id, rid), rid)
    lookup.update(result=result["return"] if result else None, message=None)


def return_restart() -> None:
    state.reset_journey(RET)
    state.go("return")


def ready_to_submit(data: Dict[str, Any]) -> bool:
    """Every view the backend asked for has an accepted upload (the backend still decides coverage)."""
    required: List[Dict[str, Any]] = data.get("required") or []
    photos = data.get("photos", {})
    return all((photos.get(slot["view"]) or {}).get("status") == "accepted" for slot in required)
