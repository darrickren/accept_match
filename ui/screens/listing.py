"""Listing detail: the exact unit, its condition report, evidence and why it fits."""

from __future__ import annotations

from typing import Any, Dict

import streamlit as st

from .. import actions, kit, state
from ..client.contracts import CATEGORY_LABELS
from ..config import Settings
from ..images import resolve
from .results import POSSIBLE_NOTE

STATUS_TONE = {"verified_ok": "verified", "defect": "possible", "failed": "off", "unknown": "off"}


def render(settings: Settings) -> None:
    if state.api_mode("shop"):
        from . import api_shop

        api_shop.render_listing(settings)
        return
    data = state.shop()
    kit.back_button("Back to results", "results", "v3_s_back_results")
    entry = data.get("listing") or {}
    context = actions.shop_context()
    listing_id = state.params().get("listing_id") or entry.get("listing_id")
    if not context or not listing_id:
        kit.heading("This piece isn't available", lede="Head back to your results to keep browsing.")
        return
    version = context["context_version"]
    if entry.get("matched_against_context_version") != version and not entry.get("missing") \
            and data.get("listing_refreshed") != (listing_id, version):
        data["listing_refreshed"] = (listing_id, version)
        actions.shop_open_listing(listing_id)  # preferences changed since this explanation was made
        entry = data.get("listing") or {}
    if entry.get("missing") or entry.get("matched_against_context_version") != version:
        kit.error_notice(state.error_for("shop") or {"user_message": "This piece is no longer listed."})
        return
    listing = entry["listing"]
    gallery, info = st.columns([1.2, 1], gap="large")
    with gallery:
        _gallery(listing)
    with info:
        _summary(listing)
        _match(listing)
        _report(listing)
        _details(listing)


def _gallery(listing: Dict[str, Any]) -> None:
    images = listing.get("images") or []
    with st.container(key="am-gallery"):
        if not images:
            kit.html('<div class="am-image-missing">Photos coming soon</div>')
            return
        first = resolve(images[0])
        if first:
            st.image(first, width="stretch", caption=images[0].get("alt"))
        rest = [img for img in images[1:] if resolve(img)]
        for start in range(0, len(rest), 2):
            columns = st.columns(2, gap="small")
            for column, image in zip(columns, rest[start:start + 2]):
                with column:
                    st.image(resolve(image), width="stretch", caption=image.get("alt"))
        kit.html('<p class="am-small">Photos show this exact item. Demo images are synthetic illustrations.</p>')


def _summary(listing: Dict[str, Any]) -> None:
    product = listing["product"]
    price = listing["price"]
    category = CATEGORY_LABELS.get(product.get("category"), product.get("category"))
    kit.html(f'<p class="am-eyebrow">{kit.text(category)} · {kit.text(str(product.get("color", "")).title())}</p>'
             f'<h1 class="am-h2">{kit.text(product.get("name"))}</h1>'
             f'<p class="am-detail-price">{kit.text(kit.money(price["amount"], price["currency"]))}</p>'
             + (f'<p class="am-price-ref">Reference price {kit.text(kit.money(price["reference_amount"]))}</p>'
                if price.get("reference_amount") else "")
             + f'<p class="am-id">Item {kit.text(listing["unit_id"])} · this exact piece</p>')
    if not listing["availability"].get("purchasable"):
        kit.banner("Someone else is holding this piece right now.", "neutral", title="Currently unavailable.")


def _match(listing: Dict[str, Any]) -> None:
    match = listing["match"]
    level = match["level"]
    if level == "verified":
        kit.banner("Every preference you set is backed by checked evidence. Checkout isn't part of this demo.",
                   "verified", title="Verified match.")
    elif level == "possible":
        kit.banner(POSSIBLE_NOTE, "possible", title="Possible match.")
        kit.html('<p class="am-h3">Still to be checked</p>')
        kit.bullet_list(match.get("missing_evidence") or [])
    else:
        kit.banner("This piece doesn't fit your current preferences.", "neutral")
        kit.bullet_list(match.get("exclusions") or [])
    if match.get("reasons"):
        kit.html('<p class="am-h3">Why it fits you</p>')
        kit.bullet_list(match["reasons"])


def _report(listing: Dict[str, Any]) -> None:
    rows = listing.get("evidence") or []
    if not rows:
        return
    kit.html('<p class="am-h3" style="margin-top:1rem">Condition report</p>')
    body = []
    for row in rows:
        note = f"{row['source']} · {row['date']}" if row.get("source") and row.get("date") else "No inspection record"
        body.append(f'<div class="am-row"><span class="am-row-label">{kit.text(row["label"])}</span>'
                    f'<span class="am-row-value">{kit.pill(row["value"], STATUS_TONE.get(row["status"], "off"))}'
                    f'<span class="am-row-note">{kit.text(note)}</span></span></div>')
    kit.html(f'<div class="am-report">{"".join(body)}</div>')
    kit.html('<p class="am-small">Unchecked items are never counted as verified.</p>')


def _details(listing: Dict[str, Any]) -> None:
    details = listing.get("details") or {}
    product = listing["product"]
    rows = [("Description", details.get("description")), ("Material", details.get("material")),
            ("Dimensions", details.get("dimensions")), ("Season", product.get("season")),
            ("Collection", product.get("collection"))]
    present = [(label, value) for label, value in rows if value]
    if present:
        with st.expander("Product details"):
            kit.html("".join(f'<div class="am-row"><span class="am-row-label">{kit.text(label)}</span>'
                             f'<span class="am-row-value">{kit.text(value)}</span></div>' for label, value in present))
