"""Buy Defective results: verified matches first, possible matches labelled, honest empty state."""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from .. import actions, kit, state
from ..client.contracts import AREA_LABELS, CATEGORY_LABELS, COMPONENT_LABELS
from ..config import Settings
from ..images import resolve

SORTS = {"best": "Best match", "price_asc": "Price ↑", "price_desc": "Price ↓"}
POSSIBLE_NOTE = "Needs inspection before purchase."


def render(settings: Settings) -> None:
    data = state.shop()
    context = actions.shop_context()
    kit.back_button("Back to the assistant", "shop", "v3_s_back_shop")
    if not context:
        kit.heading("Let's find your piece", lede="Answer a few quick questions first.")
        st.button("Start shopping", key="v3_s_results_start", type="primary", on_click=state.go, args=("shop",))
        return
    version = context["context_version"]
    results = data.get("results")
    if (not results or results.get("matched_against_context_version") != version) \
            and data.get("searched_version") != version:
        actions.shop_search()  # preferences changed: refresh once for the current version
        results = data.get("results")
    if not results or results.get("matched_against_context_version") != version:
        kit.error_notice(state.error_for("shop") or {"user_message": "We couldn't load matches right now.",
                                                     "retryable": True},
                         retry_key="v3_s_retry_search", on_retry=actions.shop_search)
        return
    _header(results, context)
    items = results.get("results") or []
    verified = [r for r in items if r["match"]["level"] == "verified"]
    possible = [r for r in items if r["match"]["level"] == "possible"]
    if not items:
        _empty(results)
        return
    if verified:
        _section("Verified matches", "Every preference you set is backed by checked evidence.", "verified")
        _grid(verified, context)
    if possible:
        _section("Possible matches", POSSIBLE_NOTE, "possible")
        _grid(possible, context)
    if len(items) <= 2 and results.get("suggestions"):
        kit.html('<p class="am-h3" style="margin-top:2rem">Want more options?</p>')
        _suggestions(results)


def _header(results: Dict[str, Any], context: Dict[str, Any]) -> None:
    total = results.get("total", 0)
    kit.html(f'<p class="am-eyebrow">Buy Defective &nbsp;{kit.source_tag("shop")}</p>')
    if total:
        title = f"{total} {'piece fits' if total == 1 else 'pieces fit'} your preferences"
    else:
        title = "No exact matches yet"
    kit.html(f'<h1 class="am-h2">{kit.text(title)}</h1>')
    turn = state.shop().get("turn") or {}
    chips = "".join(f"<span>{kit.text(row['value'])}</span>" for row in turn.get("summary") or [])
    if chips:
        kit.html(f'<div class="am-chipline">{chips}</div>')
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.button("Edit preferences", key="v3_s_edit_prefs", type="tertiary", icon=":material/tune:",
                  on_click=state.go, args=("shop",))
        st.space("stretch")
        if total > 1:
            sort_key = "v3_s_sort"
            st.session_state.setdefault(sort_key, state.shop().get("sort", "best"))
            st.segmented_control("Sort", list(SORTS), key=sort_key, format_func=SORTS.get,
                                 label_visibility="collapsed", on_change=actions.shop_set_sort, args=(sort_key,))


def _section(title: str, note: str, tone: str) -> None:
    kit.html(f'<div class="am-section"><p class="am-h3">{kit.text(title)}</p>'
             f'{kit.pill(note if tone == "possible" else "Checked evidence", tone)}</div>'
             + (f'<p class="am-small">{kit.text(note)}</p>' if tone == "verified" else ""))


def _grid(items: List[Dict[str, Any]], context: Dict[str, Any]) -> None:
    for start in range(0, len(items), 3):
        columns = st.columns(3, gap="medium")
        for column, listing in zip(columns, items[start:start + 3]):
            with column:
                card(listing, context)


def condition_line(listing: Dict[str, Any]) -> str:
    marks = [f"{AREA_LABELS.get(a['area'], a['area'])}: {d['severity']} {d['type'].replace('_', ' ')}"
             for a in listing["condition"]["areas"] for d in a.get("defects") or []]
    return "; ".join(marks) if marks else "No marks recorded"


def function_line(listing: Dict[str, Any], context: Dict[str, Any]) -> str:
    required = set((context.get("preferences") or {}).get("required_working") or [])
    words = {"verified_ok": "verified", "failed": "failed its check", "unknown": "not checked yet"}
    parts = [f"{COMPONENT_LABELS.get(c['component'], c['component'])} {words.get(c['status'], c['status'])}"
             for c in listing["condition"]["components"] if c["component"] in required]
    return "; ".join(parts)


def card(listing: Dict[str, Any], context: Dict[str, Any]) -> None:
    product = listing["product"]
    level = listing["match"]["level"]
    with st.container(key=f"am-card-{listing['listing_id']}"):
        images = listing.get("images") or []
        path = resolve(images[0]) if images else None
        if path:
            st.image(path, width="stretch")
        else:
            kit.html('<div class="am-image-missing">Photo coming soon</div>')
        badges = kit.pill("Verified match", "verified") if level == "verified" else kit.pill("Possible match", "possible")
        if not listing["availability"].get("purchasable"):
            badges += " " + kit.pill("Currently unavailable", "off")
        meta = f"{CATEGORY_LABELS.get(product.get('category'), product.get('category'))} · {str(product.get('color', '')).title()}"
        price = listing["price"]
        reference = (f'<p class="am-price-ref">Reference price {kit.text(kit.money(price["reference_amount"]))}</p>'
                     if price.get("reference_amount") else "")
        function = function_line(listing, context)
        why = (listing["match"].get("reasons") or [""])[0] if level == "verified" \
            else POSSIBLE_NOTE + " " + "; ".join(listing["match"].get("missing_evidence") or [])
        kit.html(f'<div>{badges}<p class="am-card-title">{kit.text(product.get("name"))}</p>'
                 f'<p class="am-card-meta">{kit.text(meta)}</p>'
                 f'<p class="am-card-line">{kit.text(condition_line(listing))}</p>'
                 + (f'<p class="am-card-line">{kit.text(function)}</p>' if function else "")
                 + f'<p class="am-price">{kit.text(kit.money(price["amount"], price["currency"]))}</p>{reference}'
                 f'<p class="am-id">Item {kit.text(listing["unit_id"])}</p>'
                 f'<p class="am-small">{kit.text(why)}</p></div>')
        st.button("View details", key=f"v3_s_view_{listing['listing_id']}", on_click=actions.shop_open_listing,
                  args=(listing["listing_id"],))


def _empty(results: Dict[str, Any]) -> None:
    kit.html('<div class="am-empty"><p class="am-h3">Nothing fits all of your preferences right now.</p>'
             '<p class="am-small">New pieces arrive as returns are inspected. You can also widen a preference:</p>'
             '</div>')
    if results.get("suggestions"):
        _suggestions(results)
    else:
        st.button("Edit preferences", key="v3_s_empty_edit", type="primary", on_click=state.go, args=("shop",))


def _suggestions(results: Dict[str, Any]) -> None:
    with st.container(horizontal=True, wrap=True, gap="small", horizontal_alignment="center"):
        for i, suggestion in enumerate(results.get("suggestions") or []):
            label = f"{suggestion['label']} · {suggestion['adds']} more"
            st.button(label, key=f"v3_s_sugg_{i}_{results.get('matched_against_context_version')}",
                      on_click=actions.shop_apply_suggestion, args=(suggestion["patches"],))
