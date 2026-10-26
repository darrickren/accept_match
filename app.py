"""AcceptMatch customer site (V3).

Run from the repo root:  streamlit run app.py
Uses the deterministic mock Agent by default (ACCEPTMATCH_AGENT=mock); all data
is synthetic. ACCEPTMATCH_AGENT=api connects Return and Buy Defective to the
AcceptMatch FastAPI backend (see docs/FRONTEND_AGENT_INTEGRATION.md).
"""

from importlib import import_module

import streamlit as st

from ui import kit, state, theme

st.set_page_config(page_title="AcceptMatch", page_icon="👜", layout="wide", initial_sidebar_state="collapsed")

SCREENS = {
    "home": "ui.screens.home",
    "shop": "ui.screens.assistant",
    "results": "ui.screens.results",
    "listing": "ui.screens.listing",
    "return": "ui.screens.returns",
    "return_status": "ui.screens.return_status",
}


def main() -> None:
    state.v3_init()
    settings = state.v3_settings()
    if settings.warnings and not st.session_state.get("v3_config_logged"):
        for warning in settings.warnings:
            state.record_violation("site", "config", warning)
        st.session_state["v3_config_logged"] = True
    theme.inject()
    kit.nav(settings)
    import_module(SCREENS[state.route()]).render(settings)
    if settings.diagnostics:
        import_module("ui.diagnostics").render(settings)
    kit.footer()


main()
