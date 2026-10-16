"""Reusable UI pieces for the customer site.

Every piece of dynamic text is HTML-escaped before it reaches st.html, and
Agent text is always rendered as plain text. Navigation and actions use
native Streamlit buttons, so no whole-card click overlays are needed.
"""

from __future__ import annotations

from html import escape
from typing import Iterable, Optional

import streamlit as st

from . import state
from .config import Settings

EXTERNAL_NOTE = "Opens the official ALDO website."


def html(markup: str) -> None:
    st.html(markup)


def text(value: object) -> str:
    """Escape any dynamic value for use inside our HTML snippets."""
    return escape("" if value is None else str(value))


def money(amount: Optional[float], currency: str = "CAD") -> str:
    if amount is None:
        return ""
    prefix = "CA$" if currency == "CAD" else f"{currency} "
    return f"{prefix}{amount:,.2f}"


def pill(label: str, tone: str = "off") -> str:
    return f'<span class="am-pill {text(tone)}">{text(label)}</span>'


def banner(message: str, tone: str = "neutral", title: Optional[str] = None) -> None:
    head = f"<b>{text(title)}</b> " if title else ""
    html(f'<div class="am-banner {text(tone)}" role="status">{head}{text(message)}</div>')


def plain(value: str, cls: str = "am-body") -> None:
    """Render Agent or customer text as plain text (no Markdown, no HTML)."""
    html(f'<p class="{text(cls)}">{text(value)}</p>')


def heading(title: str, eyebrow: Optional[str] = None, lede: Optional[str] = None, center: bool = False) -> None:
    parts = []
    if eyebrow:
        parts.append(f'<p class="am-eyebrow">{text(eyebrow)}</p>')
    parts.append(f'<h1 class="am-h2">{text(title)}</h1>')
    if lede:
        parts.append(f'<p class="am-lede">{text(lede)}</p>')
    style = ' class="am-center"' if center else ""
    html(f"<div{style}>{''.join(parts)}</div>")


def source_tag(journey: str) -> str:
    """Per-journey label so mock output is never mistaken for the real Agent."""
    source = state.journey_source(journey)
    if source == "mock":
        return pill("Demo assistant · scripted", "demo")
    if source == "agent":
        return pill("Live assistant", "verified")
    return ""


def bullet_list(items: Iterable[str]) -> None:
    rows = "".join(f"<li>{text(item)}</li>" for item in items)
    if rows:
        html(f'<ul class="am-list">{rows}</ul>')


def nav(settings: Settings) -> None:
    current = state.route()
    with st.container(key="am-nav", horizontal=True, vertical_alignment="center", gap="small"):
        html('<div class="am-wordmark">AcceptMatch<span class="am-demo-tag">Demo</span></div>')
        st.space("stretch")
        with st.container(key="am-nav-desktop", horizontal=True, gap="small", width="content",
                          vertical_alignment="center"):
            _nav_items(settings, current, "d")
        with st.container(key="am-nav-mobile", width="content"):
            with st.popover("Menu", type="tertiary", icon=":material/menu:"):
                _nav_items(settings, current, "m")


def _nav_items(settings: Settings, current: str, suffix: str) -> None:
    st.link_button("New ↗", settings.new_url, type="tertiary", key=f"am_nav_new_{suffix}",
                   help=EXTERNAL_NOTE)
    st.button("Return", key=f"am_nav_return_{suffix}", type="tertiary", on_click=state.go, args=("return",),
              disabled=current in ("return", "return_status"))
    st.button("Buy Defective", key=f"am_nav_buy_{suffix}", type="tertiary", on_click=state.go, args=("shop",),
              disabled=current in ("shop", "results", "listing"))
    st.link_button("Past-Season ↗", settings.past_season_url, type="tertiary", key=f"am_nav_past_{suffix}",
                   help=EXTERNAL_NOTE)


def footer() -> None:
    html('<div class="am-footer">AcceptMatch is a hackathon prototype. Products, prices, inspections and returns '
         'shown here are synthetic demo data. "New" and "Past-Season" open the official ALDO website; nothing you do '
         'there is shared with this demo.</div>')


def back_button(label: str, route: str, key: str) -> None:
    st.button(label, key=key, type="tertiary", icon=":material/arrow_back:", on_click=state.go, args=(route,))


def progress(answered: int, total: int) -> None:
    total = max(total, answered, 1)
    width = int(round(100 * min(answered, total) / total))
    html(f'<div class="am-progress" role="progressbar" aria-valuemin="0" aria-valuemax="{total}" '
         f'aria-valuenow="{answered}" aria-label="Question progress"><span style="width:{width}%"></span></div>')


def error_notice(error: Optional[dict], retry_key: Optional[str] = None, on_retry=None, args: tuple = ()) -> None:
    """Customer-facing error: only the user message, with Retry when the error is retryable."""
    if not error:
        return
    banner(error.get("user_message") or "Something went wrong. Please try again.", "error")
    if retry_key and on_retry and error.get("retryable"):
        st.button("Try again", key=retry_key, on_click=on_retry, args=args)
