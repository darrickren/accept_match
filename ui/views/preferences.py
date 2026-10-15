"""Step 1: free-text buyer preferences, interpreted into editable structured rules."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from .. import state
from ..client.contracts import AREAS, CONDITIONS, validate_rules
from ..components import rules_table

COLUMNS = ["Rule ID", "Area", "Condition", "Requirement"]


def render() -> None:
    st.subheader("Step 1 · Buyer preferences")
    current = state.stage()
    if current not in ("start", "rules_ready"):
        st.markdown("Your confirmed rules:")
        rules_table(state.turn()["rules"])
        st.button("Start a new search", on_click=state.reset, key="ui_new_search")
        return

    st.caption("Describe, in your own words, what you'll accept on a returned bag. "
               "AcceptMatch searches specific returned units, not new stock.")
    st.text_area("What will you accept?", key=state.BUYER_TEXT, height=90)
    st.button("Interpret preferences", key="am_btn_interpret", on_click=_interpret,
              type="primary" if current == "start" else "secondary")
    if current != "rules_ready":
        return

    st.markdown("Rules the agent will apply. Edit, add or delete rows, then search.")
    base_records = [{"Rule ID": r["rule_id"], "Area": r["area"], "Condition": r["condition"],
                     "Requirement": r["requirement"].capitalize()} for r in state.turn()["rules"]]
    editor_key = f"am_rules_editor_{st.session_state[state.EDITOR_VERSION]}"
    edited = st.data_editor(
        pd.DataFrame(base_records, columns=COLUMNS),
        key=editor_key,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        column_config={
            "Rule ID": st.column_config.TextColumn("Rule ID", disabled=True, help="New rows get an ID when you search."),
            "Area": st.column_config.SelectboxColumn("Area", options=AREAS, required=True),
            "Condition": st.column_config.SelectboxColumn("Condition", options=CONDITIONS, required=True),
            "Requirement": st.column_config.SelectboxColumn("Requirement", options=["Acceptable", "Required"], required=True),
        },
    )
    st.caption("Acceptable: a defect you tolerate. Required: a condition that must be verified as passed. "
               "Unknown never counts as passed.")
    _, problems = validate_rules(_rules_from_records(edited.to_dict("records")))
    for problem in problems:
        st.warning(problem)
    st.button("Find matching units", type="primary", disabled=bool(problems), key="am_btn_search",
              on_click=_search, args=(base_records, editor_key))


def _interpret() -> None:
    state.interpret(st.session_state.get(state.BUYER_TEXT, ""))


def _search(base_records: List[Dict[str, Any]], editor_key: str) -> None:
    """Rebuild the edited rules from the editor's change set, so the latest edit is always used."""
    edits = st.session_state.get(editor_key) or {}
    if isinstance(edits, str):
        edits = json.loads(edits)
    records = [dict(r) for r in base_records]
    for index, changes in (edits.get("edited_rows") or {}).items():
        if 0 <= int(index) < len(records):
            records[int(index)].update(changes)
    deleted = {int(i) for i in edits.get("deleted_rows") or []}
    records = [r for i, r in enumerate(records) if i not in deleted]
    records += [{column: added.get(column) for column in COLUMNS} for added in edits.get("added_rows") or []]
    state.search(_rules_from_records(records))  # the backend validates again and assigns IDs


def _rules_from_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rules = []
    for record in records:
        values = {key: (None if value is None or (isinstance(value, float) and pd.isna(value)) else value)
                  for key, value in record.items()}
        if not any(values.get(column) for column in COLUMNS[1:]):
            continue  # ignore blank rows
        rules.append({
            "rule_id": values.get("Rule ID"),
            "area": values.get("Area"),
            "condition": values.get("Condition"),
            "requirement": (values.get("Requirement") or "").lower(),
        })
    return rules
