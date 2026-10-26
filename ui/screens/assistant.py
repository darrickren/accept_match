"""Buy Defective assistant: one question at a time, chips or free text, editable preferences."""

from __future__ import annotations

from typing import Any, Dict

import streamlit as st

from .. import actions, kit, state
from ..config import Settings

PLACEHOLDERS = {
    "q_category": "e.g. a crossbody for everyday", "q_location": "e.g. a small scratch on the back is fine",
    "q_types": "e.g. scratches or scuffs are fine", "q_severity": "e.g. only small marks",
    "q_components": "e.g. the zipper must work", "q_colour": "e.g. black or brown",
    "q_style": "e.g. something structured", "q_price": "e.g. under $120",
}


def render(settings: Settings) -> None:
    if state.api_mode("shop"):
        from . import api_shop

        api_shop.render_assistant(settings)
        return
    actions.shop_start(auto=True)
    data = state.shop()
    kit.back_button("Home", "home", "v3_s_back_home")
    turn = data.get("turn")
    if not turn:
        kit.error_notice(state.error_for("shop") or {"user_message": "The assistant isn't available right now.",
                                                     "retryable": True},
                         retry_key="v3_s_retry_start", on_retry=actions.shop_start)
        return
    main, side = st.columns([1.7, 1], gap="large")
    with main:
        kit.html(f'<p class="am-eyebrow">Buy Defective &nbsp;{kit.source_tag("shop")}</p>')
        progress = turn.get("progress") or {}
        kit.progress(progress.get("answered", 0), progress.get("estimated_total", 1))
        if turn.get("question"):
            _question(turn, data)
        else:
            _complete(turn)
        kit.error_notice(state.error_for("shop"))
    with side:
        _preferences(turn)


def _label(option: Dict[str, Any]) -> str:
    count = option.get("count")
    return f"{option['label']} · {count}" if isinstance(count, int) else option["label"]


def _question(turn: Dict[str, Any], data: Dict[str, Any]) -> None:
    question = turn["question"]
    qid = question["question_id"]
    version = turn["context"]["context_version"]
    kit.html(f'<h1 class="am-question">{kit.text(turn.get("message"))}</h1>')
    if question.get("help"):
        kit.html(f'<p class="am-help">{kit.text(question["help"])}</p>')
    options = question.get("options") or []
    if question["type"] == "single_choice":
        with st.container(key=f"am-chips-{qid}", horizontal=True, wrap=True, gap="small"):
            for option in options:
                st.button(_label(option), key=f"v3_s_opt_{qid}_{option['value']}_{version}",
                          on_click=actions.shop_answer, args=(qid, [option["value"]]))
    else:
        chosen = data.get("selection", {}).get(qid) or []
        with st.container(key=f"am-chips-{qid}", horizontal=True, wrap=True, gap="small"):
            for option in options:
                selected = option["value"] in chosen
                st.button(_label(option), key=f"v3_s_opt_{qid}_{option['value']}_{version}",
                          type="primary" if selected else "secondary",
                          icon=":material/check:" if selected else None,
                          on_click=actions.shop_toggle, args=(qid, option["value"], bool(option.get("exclusive"))))
        st.button("Continue", key=f"v3_s_continue_{qid}_{version}", type="primary", disabled=not chosen,
                  on_click=actions.shop_submit_selection, args=(qid,))
    if question.get("allow_free_text"):
        text_key = f"v3_s_text_{qid}_{version}"
        with st.container(key="am-freetext", horizontal=True, vertical_alignment="bottom", gap="small"):
            st.text_input("Or tell us in your own words", key=text_key, placeholder=PLACEHOLDERS.get(qid, ""))
            st.button("Send", key=f"v3_s_send_{qid}_{version}", on_click=actions.shop_interpret,
                      args=(qid, text_key))
    found = data.get("interpretation")
    if found and found.get("question_id") == qid:
        with st.container(key="am-understood"):
            if found.get("understood"):
                kit.html(f'<p class="am-body"><b>We understood:</b> {kit.text(found["summary"])}</p>')
                with st.container(horizontal=True, gap="small"):
                    st.button("Use this", key=f"v3_s_use_{qid}_{version}", type="primary",
                              on_click=actions.shop_confirm_interpretation)
                    st.button("Not quite", key=f"v3_s_notquite_{qid}_{version}",
                              on_click=actions.shop_dismiss_interpretation)
            else:
                kit.html('<p class="am-body">Sorry, I couldn\'t match that to the options. '
                         'Please choose one above, or try different words.</p>')
    if (turn.get("progress") or {}).get("answered"):
        st.button("Back", key=f"v3_s_prev_{version}", type="tertiary", icon=":material/arrow_back:",
                  on_click=actions.shop_back)


def _complete(turn: Dict[str, Any]) -> None:
    count = turn.get("match_count", 0)
    kit.html('<h1 class="am-question">That\'s everything I need.</h1>')
    noun = "piece fits" if count == 1 else "pieces fit"
    kit.html(f'<p class="am-help">{count} {noun} your preferences right now.</p>')
    with st.container(horizontal=True, gap="small"):
        st.button(f"See {count} matches" if count else "See results", key="v3_s_see_done", type="primary",
                  on_click=actions.shop_search)
        st.button("Back", key="v3_s_prev_done", type="tertiary", icon=":material/arrow_back:",
                  on_click=actions.shop_back)


def _preferences(turn: Dict[str, Any]) -> None:
    with st.container(key="am-prefs"):
        kit.html('<p class="am-h3">Your preferences</p>')
        rows = turn.get("summary") or []
        if not rows:
            kit.html('<p class="am-small">Your answers will appear here.</p>')
        for row in rows:
            with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                kit.html(f'<div><p class="am-pref-label">{kit.text(row["title"])}</p>'
                         f'<p class="am-pref-value">{kit.text(row["value"])}</p></div>')
                st.space("stretch")
                st.button("Edit", key=f"v3_s_edit_{row['question_id']}", type="tertiary",
                          on_click=actions.shop_edit, args=(row["question_id"],))
        count = turn.get("match_count", 0)
        kit.html(f'<p class="am-small" style="margin-top:.6rem">{count} '
                 f'{"piece fits" if count == 1 else "pieces fit"} so far</p>')
        if turn.get("can_search") and turn.get("question"):
            st.button(f"See {count} matches now", key="v3_s_see_now", on_click=actions.shop_search)
        st.button("Start over", key="v3_s_restart", type="tertiary", on_click=actions.shop_restart)
