"""Buy Defective in api mode: the backend's conversation, its footwear inventory, and demo checkout.

Rendered by assistant.py / results.py / listing.py when ACCEPTMATCH_AGENT[_SHOP]=api.
Everything shown comes from the FastAPI backend: questions, items, prices,
imperfections and purchase results. Nothing here decides availability; the
buttons only send explicit customer actions (acceptances, checkout).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import streamlit as st

from .. import api_actions, kit, state
from ..client.api_mapper import VIEW_GUIDE, defect_line
from ..config import Settings
from ..images import safe_image_bytes

SHOP = "shop"
EXAMPLE = "Black ankle boots in US women's 7, under CA$100. Scratches are fine, but no stains."
SIZE_SYSTEMS = {"US_WOMEN": "US women's", "US_MEN": "US men's", "EU": "EU"}
ACCEPTANCES = ("POST", "/api/items/{item_id}/acceptances")
CHECKOUT = ("POST", "/api/demo-checkout")


# ----------------------------------------------------------------- shared pieces

def eyebrow() -> None:
    kit.html(f'<p class="am-eyebrow">Buy Defective &nbsp;{kit.source_tag(SHOP)}</p>')


def show_photo(photo: Optional[Dict[str, Any]], settings: Settings, caption: Optional[str] = None) -> bool:
    """Allow-listed https URLs go straight to the browser; same-origin API images are fetched server-side."""
    url = (photo or {}).get("url")
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme == "https" and (parsed.hostname or "").lower() in settings.image_hosts:
        st.image(url, width="stretch", caption=caption)
        return True
    client = state.agent_client(SHOP)
    data = safe_image_bytes(client.fetch_image(url)) if client is not None and hasattr(client, "fetch_image") else None
    if data:
        st.image(data, width="stretch", caption=caption)
        return True
    return False


def chat_bubbles(history: List[Dict[str, str]], limit: int = 6) -> None:
    rows = []
    for row in history[-limit:]:
        who = "you" if row["who"] == "you" else "agent"
        name = "You" if who == "you" else "AcceptMatch"
        rows.append(f'<div class="am-msg {who}"><span class="am-who">{name}</span>{kit.text(row["text"])}</div>')
    if rows:
        kit.html(f'<div class="am-chat">{"".join(rows)}</div>')


def questions(chat: Dict[str, Any], data: Dict[str, Any], prefix: str, on_choose, on_toggle=None,
              on_submit=None) -> None:
    """The backend's questions (1-2 per turn). Choosing an option sends its label as the next message."""
    turn = len(data.get("history") or [])
    for question in chat.get("questions") or []:
        qid = question["question_id"]
        if question.get("text"):
            kit.html(f'<p class="am-h3" style="margin-top:.6rem">{kit.text(question["text"])}</p>')
        if question.get("help"):
            kit.html(f'<p class="am-help">{kit.text(question["help"])}</p>')
        options = question.get("options") or []
        if not options:
            continue
        chosen = (data.get("selection") or {}).get(qid) or []
        with st.container(key=f"am-chips-{prefix}-{qid}-{turn}", horizontal=True, wrap=True, gap="small"):
            for index, option in enumerate(options):
                key = f"{prefix}_opt_{qid}_{index}_{turn}"
                if question.get("multi") and on_toggle:
                    selected = option["label"] in chosen
                    st.button(kit.md_safe(option["label"]), key=key, type="primary" if selected else "secondary",
                              icon=":material/check:" if selected else None, on_click=on_toggle,
                              args=(qid, option["label"]))
                else:
                    st.button(kit.md_safe(option["label"]), key=key, on_click=on_choose, args=(option["label"],))
        if question.get("multi") and on_submit:
            st.button("Continue", key=f"{prefix}_continue_{qid}_{turn}", type="primary", disabled=not chosen,
                      on_click=on_submit, args=(qid,))


def reference(request_id: Optional[str]) -> None:
    if request_id:
        kit.html(f'<p class="am-ref">Reference {kit.text(request_id)}</p>')


# ----------------------------------------------------------------- assistant

def render_assistant(settings: Settings) -> None:
    api_actions.shop_start(auto=True)
    data = state.shop()
    kit.back_button("Home", "home", "v3_s_back_home")
    if not data.get("api_open"):
        eyebrow()
        kit.error_notice(state.error_for(SHOP) or {"user_message": "The assistant isn't available right now.",
                                                   "retryable": True},
                         retry_key="v3_s_retry_start", on_retry=api_actions.shop_start)
        return
    chat = data.get("chat")
    main, side = st.columns([1.7, 1], gap="large")
    with main:
        eyebrow()
        if not chat:
            kit.html('<h1 class="am-question">What are you looking for?</h1>'
                     '<p class="am-help">Tell us the kind of shoe, your size (US or EU), your budget and the '
                     'imperfections you\'d accept. Every piece is an individual returned pair.</p>')
            st.button(f"Try: “{EXAMPLE}”", key="v3_s_api_example", type="tertiary", on_click=api_actions.shop_choose,
                      args=(EXAMPLE,))
        else:
            _turn(chat, data)
        _composer(data)
        error = state.error_for(SHOP)
        kit.error_notice(error, retry_key="v3_s_api_retry", on_retry=api_actions.shop_retry)
    with side:
        _side(data)


def _turn(chat: Dict[str, Any], data: Dict[str, Any]) -> None:
    history = data.get("history") or []
    chat_bubbles(history)
    if not history and chat.get("message"):
        kit.plain(chat["message"])
    questions(chat, data, "v3_s_api", api_actions.shop_choose, api_actions.shop_toggle,
              api_actions.shop_submit_selection)
    for note in chat.get("errors") or []:
        kit.banner(note, "neutral")
    if chat.get("next_action") == "REQUEST_ACCEPTANCE":
        kit.banner("To buy, open the piece and confirm each imperfection yourself. The assistant can't accept them "
                   "for you.", "possible", title="Before you buy.")
    results = data.get("results") or {}
    count = len(results.get("items") or [])
    if results.get("mode") == "chat" and (count or results.get("blocking")):
        label = f"See {count} {'piece' if count == 1 else 'pieces'}" if count else "See why nothing fits yet"
        st.button(label, key=f"v3_s_api_see_{chat['request_id']}", type="primary", on_click=state.go,
                  args=("results",))
    if chat.get("next_action") == "COMPLETE":
        kit.html('<p class="am-small">The assistant has everything it needs.</p>')
    reference(chat.get("request_id"))


def _composer(data: Dict[str, Any]) -> None:
    turn = len(data.get("history") or [])
    key = f"v3_s_api_text_{turn}"
    with st.container(key="am-freetext", horizontal=True, vertical_alignment="bottom", gap="small"):
        st.text_input("Message the assistant", key=key, placeholder="e.g. size 7, under $100, no stains",
                      max_chars=500)
        st.button("Send", key=f"v3_s_api_send_{turn}", type="primary", on_click=api_actions.shop_send_input,
                  args=(key,))


def _side(data: Dict[str, Any]) -> None:
    with st.container(key="am-prefs"):
        kit.html('<p class="am-h3">Your search</p>')
        said = [row["text"] for row in data.get("history") or [] if row["who"] == "you"]
        if said:
            kit.html("".join(f'<p class="am-pref-value">“{kit.text(text)}”</p>' for text in said[-4:]))
        else:
            kit.html('<p class="am-small">What you tell the assistant appears here.</p>')
        kit.html('<p class="am-small" style="margin-top:.7rem">Prefer to look around? Browse every available pair.'
                 '</p>')
        st.button("Browse all pieces", key="v3_s_api_browse", on_click=state.go, args=("results",),
                  kwargs={"browse": True})
        st.button("Start over", key="v3_s_restart", type="tertiary", on_click=api_actions.shop_restart)


# ----------------------------------------------------------------- results

def render_results(settings: Settings) -> None:
    data = state.shop()
    kit.back_button("Back to the assistant", "shop", "v3_s_back_shop")
    results = data.get("results") or {}
    browse = bool(state.params().get("browse")) or results.get("mode") == "browse"
    eyebrow()
    if browse:
        kit.html('<h1 class="am-h2">Available pieces</h1>')
        _browse_bar(results)
        if results.get("mode") != "browse":
            kit.html('<p class="am-small">Choose a size or a budget, or show everything that\'s available.</p>')
            kit.error_notice(state.error_for(SHOP))
            return
    elif not results:
        kit.heading("No pieces yet", lede="Tell the assistant what you're looking for first.")
        st.button("Talk to the assistant", key="v3_s_results_start", type="primary", on_click=state.go,
                  args=("shop",))
        return
    items = results.get("items") or []
    if not browse:
        title = (f"{len(items)} {'piece fits' if len(items) == 1 else 'pieces fit'} what you told us" if items
                 else "No exact matches yet")
        kit.html(f'<h1 class="am-h2">{kit.text(title)}</h1>')
    kit.error_notice(state.error_for(SHOP))
    if not items:
        _empty(results, browse)
        return
    if browse:
        _grid(items, settings)
    else:
        full = [i for i in items if i["match_status"] != "NEEDS_CONFIRMATION"]
        pending = [i for i in items if i["match_status"] == "NEEDS_CONFIRMATION"]
        if full:
            _section("Matches", "Fits everything you told the assistant.", "verified")
            _grid(full, settings)
        if pending:
            _section("Needs your confirmation", "Depends on something you haven't confirmed yet.", "possible")
            _grid(pending, settings)
    if results.get("next_cursor"):
        st.button("Show more", key=f"v3_s_api_more_{len(items)}", on_click=api_actions.shop_more)
    reference(results.get("request_id"))


def _browse_bar(results: Dict[str, Any]) -> None:
    with st.container(horizontal=True, wrap=True, vertical_alignment="bottom", gap="small", key="am-browse"):
        st.selectbox("Size system", [""] + list(SIZE_SYSTEMS), key="v3_s_b_system",
                     format_func=lambda v: SIZE_SYSTEMS.get(v, "Any size"))
        st.text_input("Size", key="v3_s_b_size", placeholder="e.g. 7", max_chars=6)
        st.number_input("Max price (CA$)", min_value=0, max_value=2000, step=10, value=None, key="v3_s_b_max",
                        placeholder="Any")
        st.button("Show pieces", key="v3_s_b_go", type="primary", on_click=api_actions.shop_browse)


def _section(title: str, note: str, tone: str) -> None:
    kit.html(f'<div class="am-section"><p class="am-h3">{kit.text(title)}</p>{kit.pill(note, tone)}</div>')


def _grid(items: List[Dict[str, Any]], settings: Settings) -> None:
    for start in range(0, len(items), 3):
        columns = st.columns(3, gap="medium")
        for column, item in zip(columns, items[start:start + 3]):
            with column:
                card(item, settings)


def meta_line(item: Dict[str, Any]) -> str:
    parts = [item.get("category_label"), item.get("shoe_type"), (item.get("color") or "").title() or None,
             item.get("size_label")]
    return " · ".join(p for p in parts if p)


def badges(item: Dict[str, Any]) -> str:
    out = [kit.pill(item["grade_label"], "off")]
    if item.get("match_status") == "MATCH":
        out.append(kit.pill("Fits your search", "verified"))
    elif item.get("match_status") == "NEEDS_CONFIRMATION":
        out.append(kit.pill("Needs your confirmation", "possible"))
    if not item["purchasable"]:
        out.append(kit.pill(item["status_label"] if item["status"] != "AVAILABLE" else "Not for sale", "off"))
    return " ".join(out)


def card(item: Dict[str, Any], settings: Settings) -> None:
    with st.container(key=f"am-card-{item['item_id']}"):
        if not show_photo((item.get("photos") or [None])[0], settings):
            kit.html('<div class="am-image-missing">Photo coming soon</div>')
        marks = "; ".join(defect_line(d) for d in item["defects"]) or "No imperfections recorded"
        why = (item.get("pending_confirmations") or item.get("match_reasons") or [""])[0]
        kit.html(f'<div>{badges(item)}<p class="am-card-title">{kit.text(item["title"])}</p>'
                 f'<p class="am-card-meta">{kit.text(meta_line(item))}</p>'
                 f'<p class="am-card-line">{kit.text(marks)}</p>'
                 f'<p class="am-price">{kit.text(kit.cad_cents(item["price_cents"]))}</p>'
                 f'<p class="am-id">Item {kit.text(item["item_id"])}</p>'
                 + (f'<p class="am-small">{kit.text(why)}</p>' if why else "") + "</div>")
        st.button("View details", key=f"v3_s_view_{item['item_id']}", on_click=api_actions.shop_open_item,
                  args=(item["item_id"],))


def _empty(results: Dict[str, Any], browse: bool) -> None:
    kit.html('<div class="am-empty"><p class="am-h3">Nothing fits right now.</p>'
             '<p class="am-small">New pairs arrive as returns are received and inspected.</p></div>')
    blocking = results.get("blocking") or []
    if blocking:
        kit.html('<p class="am-h3">What\'s ruling pieces out</p>')
        kit.bullet_list(blocking)
        kit.html('<p class="am-small">The assistant won\'t relax any of these without asking you first.</p>')
    if not browse:
        st.button("Back to the assistant", key="v3_s_empty_back", type="primary", on_click=state.go, args=("shop",))


# ----------------------------------------------------------------- listing detail and purchase

def render_listing(settings: Settings) -> None:
    data = state.shop()
    kit.back_button("Back to results", "results", "v3_s_back_results")
    entry = data.get("item") or {}
    item_id = state.params().get("listing_id") or entry.get("item_id")
    if not item_id or entry.get("item_id") != item_id or entry.get("missing"):
        kit.heading("This piece isn't available", lede="Head back to your results to keep browsing.")
        kit.error_notice(state.error_for(SHOP))
        return
    item = entry["item"]
    card_view = (data.get("cards") or {}).get(item_id) or {}
    gallery, info = st.columns([1.2, 1], gap="large")
    with gallery:
        _gallery(item, settings)
    with info:
        _summary(item)
        _fit(card_view)
        _disclosures(item)
        _purchase(item, data)
        kit.error_notice(state.error_for(SHOP))


def _gallery(item: Dict[str, Any], settings: Settings) -> None:
    photos = item.get("photos") or []
    with st.container(key="am-gallery"):
        shown = 0
        for photo in photos[:8]:
            # Captions only use our own wording for known views; backend text never reaches a Markdown caption.
            caption = VIEW_GUIDE.get(photo.get("view") or "", (None,))[0]
            shown += show_photo(photo, settings, caption=caption)
        if not shown:
            kit.html('<div class="am-image-missing">Photos coming soon</div>')
        kit.html('<p class="am-small">Photos show this exact pair.</p>')


def _summary(item: Dict[str, Any]) -> None:
    kit.html(f'<p class="am-eyebrow">{kit.text(meta_line(item))}</p>'
             f'<h1 class="am-h2">{kit.text(item["title"])}</h1>'
             f'<p class="am-detail-price">{kit.text(kit.cad_cents(item["price_cents"]))}</p>'
             f'<p class="am-id">Item {kit.text(item["item_id"])} · this exact pair · listing version '
             f'{kit.text(item["listing_version"])}</p>'
             f'<div style="margin:.5rem 0">{badges(item)}</div>'
             f'<p class="am-small">{kit.text(item["grade_label"])}: {kit.text(item["grade_note"])}</p>')
    if item.get("description"):
        kit.plain(item["description"])


def _fit(card_view: Dict[str, Any]) -> None:
    if card_view.get("match_reasons"):
        kit.html('<p class="am-h3" style="margin-top:1rem">Why it fits you</p>')
        kit.bullet_list(card_view["match_reasons"])
    if card_view.get("pending_confirmations"):
        kit.banner("; ".join(card_view["pending_confirmations"]), "possible", title="Needs your confirmation.")


def _disclosures(item: Dict[str, Any]) -> None:
    kit.html('<p class="am-h3" style="margin-top:1rem">Every imperfection on this pair</p>')
    defects = item["defects"]
    if not defects:
        kit.html('<p class="am-small">No imperfections are recorded for this pair.</p>')
        return
    rows = []
    for defect in defects:
        notes = [n for n in (defect.get("description"),
                             f"Inspection confidence: {defect['uncertainty']}" if defect.get("uncertainty") else None,
                             "Simulated inspection" if str(defect.get("verified_by") or "").upper().endswith("SIMULATED")
                             else None) if n]
        where = f" · {defect['location']}" if defect.get("location") else ""
        rows.append(f'<div class="am-row"><span class="am-row-label">{kit.text(defect["label"] + where)}</span>'
                    f'<span class="am-row-value">{kit.pill(_grade_short(defect["severity"]), "possible")}'
                    + "".join(f'<span class="am-row-note">{kit.text(n)}</span>' for n in notes) + "</span></div>")
    kit.html(f'<div class="am-report">{"".join(rows)}</div>')


def _grade_short(severity: str) -> str:
    return {"A": "Minor (A)", "B": "Noticeable (B)", "C": "Structural (C)"}.get(severity, "Not graded")


def _purchase(item: Dict[str, Any], data: Dict[str, Any]) -> None:
    item_id, version = item["item_id"], item["listing_version"]
    purchase = (data.get("purchases") or {}).get(item_id)
    with st.container(key="am-purchase"):
        if purchase:
            _confirmation(purchase)
            return
        client = state.agent_client(SHOP)
        supported = client is not None and all(client.supports(*endpoint) is not False
                                               for endpoint in (ACCEPTANCES, CHECKOUT))
        kit.html('<p class="am-h3">Buy this pair</p>')
        if not supported:
            st.button("Buy · pending integration", key="v3_s_buy_pending", disabled=True)
            kit.html('<p class="am-small">Checkout isn\'t connected to the AcceptMatch service yet.</p>')
            return
        if not item["purchasable"]:
            kit.banner(f"This pair can't be bought right now ({item['status_label'].lower()}).", "neutral")
            _purchase_error(data, item_id)
            return
        key = f"{item_id}@v{version}"
        accepted = (data.get("accepted") or {}).get(key)
        defects = item["defects"]
        if defects:
            kit.html('<p class="am-small">Confirm each imperfection on this exact pair. Telling the assistant you\'re '
                     'fine with scratches helps the search; it isn\'t agreement to this specific mark.</p>')
            for defect in defects:
                label = defect_line(defect) + (f". {defect['description']}" if defect.get("description") else "")
                st.checkbox("I accept: " + kit.md_safe(label),
                            key=api_actions.accept_key(item_id, version, defect["defect_id"]), disabled=bool(accepted))
            if accepted:
                kit.html(f'{kit.pill("Imperfections confirmed", "verified")}')
            else:
                ticked = all(st.session_state.get(api_actions.accept_key(item_id, version, d["defect_id"]))
                             for d in defects)
                st.button("Confirm these imperfections", key=f"v3_s_accept_{item_id}_v{version}", type="primary",
                          disabled=not ticked, on_click=api_actions.shop_accept, args=(item_id,))
        st.button(f"Buy for {kit.md_safe(kit.cad_cents(item['price_cents']))} · demo checkout",
                  key=f"v3_s_buy_{item_id}_v{version}",
                  type="primary", disabled=bool(defects) and not accepted, on_click=api_actions.shop_checkout,
                  args=(item_id,))
        kit.html('<p class="am-small">Demo checkout: no payment is taken, nothing ships, and no ALDO order is '
                 'created.</p>')
        _purchase_error(data, item_id)


def _purchase_error(data: Dict[str, Any], item_id: str) -> None:
    error = data.get("purchase_error")
    if not error:
        return
    retry = error.get("code") in ("agent_timeout", "agent_unavailable", "rate_limited") and error.get("retryable")
    kit.error_notice(error, retry_key=f"v3_s_buy_retry_{item_id}" if retry else None,
                     on_retry=api_actions.shop_checkout if retry else None, args=(item_id,))
    if error.get("code") == "agent_timeout":
        kit.html('<p class="am-small">Trying again is safe: it reuses the same order key, so you can\'t buy twice.</p>')


def _confirmation(purchase: Dict[str, Any]) -> None:
    amount = kit.cad_cents(purchase.get("amount_cents")) if purchase.get("amount_cents") is not None else ""
    kit.banner(f"Order {purchase['purchase_id']}{' · ' + amount if amount else ''}. Demo checkout: no payment was "
               "taken and nothing ships.", "verified", title="Demo purchase confirmed.")
    reference(purchase.get("request_id"))
