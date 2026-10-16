"""Home: exactly four primary choices.

New and Past-Season are plain external links to the official ALDO website:
no Agent client, no session, no AcceptMatch data access. Return and Buy
Defective switch to in-site routes.
"""

from __future__ import annotations

import streamlit as st

from .. import kit, state
from ..config import Settings

ENTRIES = ("new", "return", "buy", "past")  # order requested by the product owner


def render(settings: Settings) -> None:
    kit.html('<div class="am-hero">'
             '<p class="am-eyebrow">AcceptMatch</p>'
             '<h1 class="am-display">Imperfect, honestly.</h1>'
             '<p class="am-lede">Shop returned pieces whose imperfections are checked and disclosed, '
             'or start a return in a few simple steps.</p></div>')
    first, second = st.columns(2, gap="medium"), st.columns(2, gap="medium")
    with first[0]:
        _external_tile("new", "From ALDO", "New", "Shop the latest styles at ALDO.", "Shop ALDO ↗",
                       settings.new_url)
    with first[1]:
        _internal_tile("return", "AcceptMatch", "Return",
                       "Tell us what's wrong, add a few photos, and get a decision in minutes.",
                       "Start a return", "return")
    with second[0]:
        _internal_tile("buy", "AcceptMatch", "Buy Defective",
                       "Choose the imperfections you're happy with. We'll show the exact pieces that fit.",
                       "Start shopping", "shop")
    with second[1]:
        _external_tile("past", "From ALDO", "Past-Season",
                       "Browse discounted styles on ALDO's official Sale page.", settings.past_season_cta,
                       settings.past_season_url)


def _copy(eyebrow: str, title: str, body: str) -> None:
    kit.html(f'<div><p class="am-eyebrow">{kit.text(eyebrow)}</p>'
             f'<h2 class="am-tile-title">{kit.text(title)}</h2>'
             f'<p class="am-tile-body">{kit.text(body)}</p></div>')


def _internal_tile(entry: str, eyebrow: str, title: str, body: str, cta: str, route: str) -> None:
    with st.container(key=f"am-tile-{entry}"):
        _copy(eyebrow, title, body)
        st.button(cta, key=f"am_home_{entry}", type="primary", on_click=state.go, args=(route,))


def _external_tile(entry: str, eyebrow: str, title: str, body: str, cta: str, url: str) -> None:
    with st.container(key=f"am-tile-{entry}"):
        _copy(eyebrow, title, body)
        st.link_button(cta, url, key=f"am_home_{entry}", type="secondary",
                       help=f"{kit.EXTERNAL_NOTE} Opens in a new tab.")
        kit.html(f'<p class="am-small">{kit.text(kit.EXTERNAL_NOTE)} You\'ll leave AcceptMatch.</p>')
