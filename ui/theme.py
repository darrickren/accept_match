"""Design system for the customer site: tokens and the single CSS layer.

All custom CSS lives here. Selectors target stable hooks only: keyed
containers (`.st-key-<key>`), Streamlit's documented test ids for buttons,
and our own `am-*` classes.
"""

from __future__ import annotations

import streamlit as st

CSS = """
<style>
:root {
  --am-ink: #1d1d1f;
  --am-muted: #6e6e73;
  --am-hairline: #d2d2d7;
  --am-hairline-soft: #e8e8ed;
  --am-surface: #f5f5f7;
  --am-surface-2: #fbfbfd;
  --am-link: #0066cc;
  --am-verified-bg: #e8f5ec; --am-verified-ink: #1b6b3a;
  --am-possible-bg: #fdf2e0; --am-possible-ink: #8a5300;
  --am-off-bg: #f0f0f2; --am-off-ink: #5f5f64;
  --am-error-bg: #fdecec; --am-error-ink: #8e1b1b;
  --am-radius: 22px;
  --am-radius-sm: 14px;
  --am-shadow: 0 10px 30px rgba(0, 0, 0, .07);
  --am-ease: cubic-bezier(.25, .1, .25, 1);
}
[data-testid="stAppViewContainer"], .stApp { background: #ffffff; }
[data-testid="stHeader"] { background: rgba(255, 255, 255, 0); }
[data-testid="stDecoration"] { display: none; }
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 1120px; padding-top: 1.1rem; padding-bottom: 3rem;
}

/* Type */
.am-eyebrow { font-size: .78rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  color: var(--am-muted); margin: 0 0 .35rem; }
.am-eyebrow .am-pill { text-transform: none; letter-spacing: 0; vertical-align: 1px; }
.am-section { display: flex; align-items: baseline; gap: .7rem; flex-wrap: wrap; margin: 1.6rem 0 .2rem; }
.am-section .am-h3 { margin: 0; }
.am-chipline { display: flex; flex-wrap: wrap; gap: .4rem; margin: .6rem 0 .2rem; }
.am-chipline span { font-size: .82rem; background: var(--am-surface); border-radius: 999px; padding: .2rem .7rem; color: var(--am-ink); }
.am-empty { text-align: center; padding: 2.4rem 1rem 1.2rem; }
.am-detail-price { font-size: 1.6rem; font-weight: 650; color: var(--am-ink); margin: .3rem 0 0; }
.am-image-missing { aspect-ratio: 1 / 1; border-radius: var(--am-radius-sm); background: var(--am-surface);
  display: flex; align-items: center; justify-content: center; color: var(--am-muted); font-size: .85rem; }
.am-display { font-size: clamp(2.3rem, 5.4vw, 3.9rem); line-height: 1.04; font-weight: 700;
  letter-spacing: -.028em; color: var(--am-ink); margin: .2rem 0 .7rem; }
.am-lede { font-size: clamp(1.05rem, 1.7vw, 1.32rem); line-height: 1.45; color: var(--am-muted);
  max-width: 44rem; margin: 0; }
.am-h2 { font-size: clamp(1.7rem, 3.4vw, 2.5rem); font-weight: 700; letter-spacing: -.022em;
  line-height: 1.1; color: var(--am-ink); margin: .1rem 0 .4rem; }
.am-h3 { font-size: 1.22rem; font-weight: 650; letter-spacing: -.012em; color: var(--am-ink); margin: 0 0 .2rem; }
.am-body { font-size: 1.02rem; line-height: 1.5; color: var(--am-ink); margin: 0; }
.am-muted { color: var(--am-muted); }
.am-small { font-size: .82rem; line-height: 1.4; color: var(--am-muted); margin: 0; }
.am-center { text-align: center; margin-left: auto; margin-right: auto; }
.am-hero { text-align: center; padding: 2.6rem 0 2.2rem; }
.am-hero .am-lede { margin: 0 auto; }

/* Navigation */
.st-key-am-nav { border-bottom: 1px solid var(--am-hairline-soft); padding-bottom: .35rem; margin-bottom: .4rem; }
.am-wordmark { font-weight: 700; font-size: 1.12rem; letter-spacing: -.02em; color: var(--am-ink); white-space: nowrap; }
.am-demo-tag { display: inline-block; margin-left: .45rem; font-size: .66rem; font-weight: 600; color: var(--am-muted);
  border: 1px solid var(--am-hairline); border-radius: 999px; padding: .05rem .45rem; vertical-align: 2px; }
.st-key-am-nav-desktop button, .st-key-am-nav-desktop a,
.st-key-am-nav-mobile button, .st-key-am-nav-mobile a { font-size: .9rem; }
@media (max-width: 720px) { .st-key-am-nav-desktop { display: none; } }
@media (min-width: 721px) { .st-key-am-nav-mobile { display: none; } }

/* Buttons */
.stButton button, .stLinkButton a, .stDownloadButton button {
  transition: background-color .18s var(--am-ease), box-shadow .18s var(--am-ease), transform .18s var(--am-ease);
  font-weight: 560;
}
[data-testid="stBaseButton-primary"], [data-testid="stBaseLinkButton-primary"] {
  background: var(--am-ink); border-color: var(--am-ink); color: #ffffff;
}
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseLinkButton-primary"]:hover {
  background: #000000; border-color: #000000; color: #ffffff;
}
[data-testid="stBaseButton-secondary"], [data-testid="stBaseLinkButton-secondary"] {
  background: #ffffff; border: 1px solid var(--am-hairline); color: var(--am-ink);
}
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseLinkButton-secondary"]:hover {
  border-color: var(--am-ink); color: var(--am-ink); background: #ffffff;
}
[data-testid="stBaseButton-tertiary"], [data-testid="stBaseLinkButton-tertiary"] { color: var(--am-ink); }
[data-testid="stBaseButton-tertiary"]:hover, [data-testid="stBaseLinkButton-tertiary"]:hover { color: var(--am-link); }
button:focus-visible, a:focus-visible { outline: 2px solid var(--am-link) !important; outline-offset: 2px; }

/* Home tiles */
[class*="st-key-am-tile-"] {
  background: var(--am-surface); border-radius: var(--am-radius); padding: 2.1rem 1.9rem 1.8rem;
  min-height: 16.5rem; justify-content: space-between;
  transition: transform .25s var(--am-ease), box-shadow .25s var(--am-ease);
}
[class*="st-key-am-tile-"]:hover { transform: translateY(-3px); box-shadow: var(--am-shadow); }
/* Equal-height tiles in a row: stretch the column's block chain down to the tile. */
[data-testid="stHorizontalBlock"]:has([class*="st-key-am-tile-"]) { align-items: stretch; }
[data-testid="stColumn"]:has([class*="st-key-am-tile-"]) { display: flex; flex-direction: column; }
[data-testid="stColumn"]:has([class*="st-key-am-tile-"]) > [data-testid="stVerticalBlock"] { height: 100%; flex: 1 1 auto; }
[data-testid="stColumn"]:has([class*="st-key-am-tile-"]) > [data-testid="stVerticalBlock"] > [data-testid="stLayoutWrapper"] {
  flex: 1 1 auto; }
[class*="st-key-am-tile-"] { height: 100%; }
.st-key-am-tile-buy { background: var(--am-ink); }
.st-key-am-tile-buy .am-eyebrow, .st-key-am-tile-buy .am-tile-title, .st-key-am-tile-buy .am-tile-body,
.st-key-am-tile-buy .am-small { color: #f5f5f7; }
.st-key-am-tile-buy [data-testid="stBaseButton-primary"] { background: #ffffff; color: var(--am-ink); border-color: #ffffff; }
.st-key-am-tile-buy [data-testid="stBaseButton-primary"]:hover { background: #e8e8ed; color: var(--am-ink); }
.st-key-am-tile-return { background: var(--am-surface-2); border: 1px solid var(--am-hairline-soft); }
.am-tile-title { font-size: clamp(1.7rem, 3vw, 2.3rem); font-weight: 700; letter-spacing: -.024em; line-height: 1.06;
  color: var(--am-ink); margin: 0 0 .45rem; }
.am-tile-body { font-size: 1.04rem; line-height: 1.45; color: var(--am-muted); margin: 0 0 1.1rem; max-width: 26rem; }

/* Pills and banners */
.am-pill { display: inline-flex; align-items: center; gap: .3rem; font-size: .74rem; font-weight: 600;
  border-radius: 999px; padding: .16rem .62rem; white-space: nowrap; line-height: 1.35; }
.am-pill.verified { background: var(--am-verified-bg); color: var(--am-verified-ink); }
.am-pill.possible { background: var(--am-possible-bg); color: var(--am-possible-ink); }
.am-pill.off { background: var(--am-off-bg); color: var(--am-off-ink); }
.am-pill.demo { background: #eef1f6; color: #34445e; }
.am-banner { border-radius: 16px; padding: .95rem 1.15rem; font-size: .97rem; line-height: 1.5; margin: .2rem 0 .9rem; }
.am-banner.verified { background: var(--am-verified-bg); color: var(--am-verified-ink); }
.am-banner.possible { background: var(--am-possible-bg); color: var(--am-possible-ink); }
.am-banner.neutral { background: var(--am-surface); color: var(--am-ink); }
.am-banner.error { background: var(--am-error-bg); color: var(--am-error-ink); }
.am-banner b { font-weight: 650; }

/* Cards */
[class*="st-key-am-card-"] {
  background: #ffffff; border: 1px solid var(--am-hairline-soft); border-radius: 20px; padding: .8rem .8rem 1rem;
  transition: box-shadow .25s var(--am-ease), transform .25s var(--am-ease);
}
[class*="st-key-am-card-"]:hover { box-shadow: var(--am-shadow); transform: translateY(-2px); }
[class*="st-key-am-card-"] img, [class*="st-key-am-gallery"] img { border-radius: var(--am-radius-sm); }
.am-card-title { font-size: 1.06rem; font-weight: 650; letter-spacing: -.01em; color: var(--am-ink); margin: .55rem 0 .05rem; }
.am-card-meta { font-size: .86rem; color: var(--am-muted); margin: 0 0 .45rem; }
.am-card-line { font-size: .88rem; color: var(--am-ink); margin: .1rem 0; }
.am-price { font-size: 1.12rem; font-weight: 650; color: var(--am-ink); margin: .55rem 0 .1rem; }
.am-price-ref { font-size: .82rem; color: var(--am-muted); margin: 0; }
.am-id { font-size: .76rem; color: var(--am-muted); letter-spacing: .01em; margin: .1rem 0 .5rem; }

/* Assistant */
.am-question { font-size: clamp(1.7rem, 3.4vw, 2.45rem); font-weight: 700; letter-spacing: -.022em; line-height: 1.1;
  color: var(--am-ink); margin: .2rem 0 .5rem; }
.am-help { font-size: 1rem; color: var(--am-muted); margin: 0 0 1.2rem; }
.am-progress { height: 4px; background: var(--am-surface); border-radius: 999px; overflow: hidden; margin: .5rem 0 1.4rem; }
.am-progress > span { display: block; height: 100%; background: var(--am-ink); border-radius: 999px;
  transition: width .3s var(--am-ease); }
.st-key-am-prefs { background: var(--am-surface); border-radius: 20px; padding: 1.25rem 1.2rem .8rem; }
.am-pref-label { font-size: .76rem; font-weight: 600; letter-spacing: .03em; text-transform: uppercase;
  color: var(--am-muted); margin: .4rem 0 0; }
.am-pref-value { font-size: .98rem; color: var(--am-ink); margin: .05rem 0 0; }
[class*="st-key-am-chips"] button { border-radius: 999px; padding-left: 1.05rem; padding-right: 1.05rem; }
.st-key-am-understood { background: var(--am-surface); border-radius: 18px; padding: 1rem 1.1rem; }

/* Condition report */
.am-report { border-top: 1px solid var(--am-hairline-soft); margin: .4rem 0 1rem; }
.am-row { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; padding: .78rem 0;
  border-bottom: 1px solid var(--am-hairline-soft); font-size: .96rem; }
.am-row .am-row-label { color: var(--am-ink); font-weight: 560; }
.am-row .am-row-value { color: var(--am-ink); text-align: right; }
.am-row .am-row-note { display: block; font-size: .8rem; color: var(--am-muted); margin-top: .1rem; }
.am-list { margin: .2rem 0 1rem 1.1rem; padding: 0; }
.am-list li { margin: .25rem 0; font-size: .97rem; line-height: 1.45; color: var(--am-ink); }

/* Steps (return) */
.am-steps { display: flex; gap: 1.4rem; flex-wrap: wrap; font-size: .86rem; color: var(--am-muted); margin: .2rem 0 1.4rem; }
.am-steps .on { color: var(--am-ink); font-weight: 650; }
.am-steps .done { color: var(--am-verified-ink); }
[class*="st-key-am-slot-"] { background: var(--am-surface-2); border: 1px solid var(--am-hairline-soft);
  border-radius: 18px; padding: 1rem 1rem .6rem; }
.am-timeline { list-style: none; margin: .4rem 0 1rem; padding: 0; }
.am-timeline li { position: relative; padding: .1rem 0 .9rem 1.4rem; font-size: .95rem; color: var(--am-ink); }
.am-timeline li::before { content: ""; position: absolute; left: .2rem; top: .45rem; width: .5rem; height: .5rem;
  border-radius: 50%; background: var(--am-ink); }
.am-timeline li.pending::before { background: var(--am-hairline); }
.am-timeline .am-small { display: block; }
.am-code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 1.05rem; letter-spacing: .04em;
  background: var(--am-surface); border-radius: 10px; padding: .15rem .55rem; }

/* Footer */
.am-footer { margin-top: 4rem; padding-top: 1.1rem; border-top: 1px solid var(--am-hairline-soft);
  font-size: .78rem; line-height: 1.5; color: var(--am-muted); }

@media (max-width: 640px) {
  [class*="st-key-am-tile-"] { min-height: 0; padding: 1.6rem 1.3rem 1.4rem; }
  .am-hero { padding: 1.4rem 0 1.4rem; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation: none !important; scroll-behavior: auto !important; }
  [class*="st-key-am-tile-"]:hover, [class*="st-key-am-card-"]:hover { transform: none; }
}
</style>
"""


def inject() -> None:
    st.html(CSS)
