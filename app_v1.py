"""AcceptMatch Streamlit frontend (owner: Yanyu).

V1 buyer/staff demo, preserved unchanged. Run: streamlit run app_v1.py
Uses the mock backend by default: synthetic data and a simulated agent.
"""

import streamlit as st

from ui import state
from ui.activity import render_activity_panel
from ui.components import agent_message, inject_css, render_header, render_sidebar, render_stepper
from ui.views import inspection, matches, preferences, reservation

st.set_page_config(page_title="AcceptMatch demo", page_icon="👜", layout="wide")

VIEWS = {1: preferences.render, 2: matches.render, 3: inspection.render, 4: reservation.render}


def main() -> None:
    try:
        state.init()
    except RuntimeError as exc:  # e.g. an unavailable backend was selected
        st.error(str(exc))
        st.stop()

    inject_css()
    render_sidebar()
    render_header()
    render_stepper()
    error = state.error()
    if error:
        st.error(error["message"])

    main_column, activity_column = st.columns([3, 2], gap="large")
    with main_column:
        turn = state.turn()
        if turn and turn.get("message") and state.current_step() == state.natural_step():
            agent_message(turn["message"])
        VIEWS[state.current_step()]()
    with activity_column:
        render_activity_panel()


main()
