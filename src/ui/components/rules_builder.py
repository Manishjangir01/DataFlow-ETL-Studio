"""Validation rule builder with a live check against the transformed sample."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from src.ui.common import dataframe_for_display, show_error, status_label
from src.ui.components.transform_builder import _expression_help
from src.validation.rules import RULE_LABELS, ValidationResult, describe_rule, parse_rule, validate_dataframe

RULE_ORDER = ["not_null", "unique", "range", "allowed_values", "pattern", "data_type", "expression", "required_columns", "row_count"]
RULE_HELP = {
    "not_null": "The column must have a value in every row.",
    "unique": "No two rows may share the same value(s) - e.g. an ID.",
    "range": "Numbers must be between a minimum and/or maximum.",
    "allowed_values": "Only values from a list are accepted, e.g. active / inactive.",
    "pattern": "Text must match a regular expression, e.g. an email or postcode format.",
    "data_type": "Values must be valid numbers / dates / true-false.",
    "expression": "Any business rule as an expression that must be true, e.g. end_date >= start_date.",
    "required_columns": "These columns must exist (stops the run when missing).",
    "row_count": "The dataset must have at least / at most this many rows (stops the run).",
}
PATTERN_PRESETS = {
    "Custom": "",
    "Email address": r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}",
    "Digits only": r"\d+",
    "Letters only": r"[A-Za-z]+",
    "ISO date (YYYY-MM-DD)": r"\d{4}-\d{2}-\d{2}",
    "Indian PIN code": r"[1-9]\d{5}",
    "US ZIP code": r"\d{5}(-\d{4})?",
    "Phone (+ and digits)": r"\+?[0-9 ()-]{7,20}",
}


def _rule_form(rule_type: str, columns: list[str], existing: dict[str, Any] | None, key: str) -> dict[str, Any]:
    e = existing or {}
    rule: dict[str, Any] = {"type": rule_type, "enabled": e.get("enabled", True)}
    if e.get("id"):
        rule["id"] = e["id"]

    def column_pick(label: str = "Column") -> str:
        if columns:
            current = e.get("column")
            options = columns if current in columns or not current else columns + [current]
            return st.selectbox(label, options, index=options.index(current) if current in options else 0, key=f"{key}_col")
        return st.text_input(label, value=e.get("column", ""), key=f"{key}_col")

    if rule_type in ("not_null", "range", "allowed_values", "pattern", "data_type"):
        rule["column"] = column_pick()
    if rule_type in ("unique", "required_columns"):
        default = [c for c in e.get("columns", []) if c in columns] if columns else e.get("columns", [])
        if columns:
            rule["columns"] = st.multiselect("Columns", columns + [c for c in e.get("columns", []) if c not in columns],
                                             default=e.get("columns", default), key=f"{key}_cols")
        else:
            typed = st.text_input("Columns (comma separated)", value=", ".join(e.get("columns", [])), key=f"{key}_cols")
            rule["columns"] = [c.strip() for c in typed.split(",") if c.strip()]
    if rule_type == "range":
        c1, c2 = st.columns(2)
        low = c1.text_input("Minimum (optional)", value="" if e.get("min") is None else str(e["min"]), key=f"{key}_min")
        high = c2.text_input("Maximum (optional)", value="" if e.get("max") is None else str(e["max"]), key=f"{key}_max")
        rule["min"] = float(low) if _is_number(low) else None
        rule["max"] = float(high) if _is_number(high) else None
        if (low.strip() and not _is_number(low)) or (high.strip() and not _is_number(high)):
            st.warning("Minimum and maximum must be numbers.")
        rule["allow_null"] = st.checkbox("Allow empty values", value=e.get("allow_null", True), key=f"{key}_null")
    if rule_type == "allowed_values":
        typed = st.text_input("Allowed values (comma separated)", value=", ".join(e.get("values", [])), key=f"{key}_values")
        rule["values"] = [v.strip() for v in typed.split(",") if v.strip()]
        c1, c2 = st.columns(2)
        rule["case_sensitive"] = c1.checkbox("Case sensitive", value=e.get("case_sensitive", False), key=f"{key}_cs")
        rule["allow_null"] = c2.checkbox("Allow empty values", value=e.get("allow_null", True), key=f"{key}_null")
    if rule_type == "pattern":
        preset = st.selectbox("Common patterns", list(PATTERN_PRESETS), key=f"{key}_preset")
        default = PATTERN_PRESETS[preset] or e.get("pattern", "")
        rule["pattern"] = st.text_input("Regular expression (must match the whole value)", value=default, key=f"{key}_pattern_{preset}")
        rule["allow_null"] = st.checkbox("Allow empty values", value=e.get("allow_null", True), key=f"{key}_null")
    if rule_type == "data_type":
        types = ["integer", "float", "boolean", "datetime", "date"]
        rule["expected"] = st.selectbox("Expected type", types, index=types.index(e.get("expected", "integer")), key=f"{key}_type")
    if rule_type == "expression":
        rule["expression"] = st.text_area("Rule (must be true for valid rows)", value=e.get("expression", ""), key=f"{key}_expr",
                                          placeholder="lifetime_value >= 0 and age >= 18")
        _expression_help()
    if rule_type == "row_count":
        c1, c2 = st.columns(2)
        low = c1.number_input("Minimum rows", min_value=0, value=int(e.get("min_rows") or 1), key=f"{key}_minrows")
        high = c2.text_input("Maximum rows (optional)", value="" if e.get("max_rows") is None else str(e["max_rows"]), key=f"{key}_maxrows")
        rule["min_rows"] = int(low)
        rule["max_rows"] = int(high) if high.strip().isdigit() else None

    if rule_type in ("required_columns", "row_count"):
        rule["on_fail"] = "fail"
        st.caption("This rule always stops the run when it fails.")
    else:
        choices = ["reject", "fail"]
        rule["on_fail"] = st.radio(
            "When a row breaks this rule", choices, index=choices.index(e.get("on_fail", "reject")), key=f"{key}_onfail", horizontal=True,
            format_func=lambda v: "Reject the row (load the rest)" if v == "reject" else "Stop the whole run",
        )
    note = st.text_input("Description (optional)", value=e.get("description") or "", key=f"{key}_desc")
    if note.strip():
        rule["description"] = note.strip()
    return rule


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def render_rules_builder(rules: list[dict], data: pd.DataFrame | None, prefix: str = "rule") -> ValidationResult | None:
    """Edit ``rules`` in place and validate the (transformed) sample."""
    ver = st.session_state.get(f"{prefix}_ver", 0)
    columns = [str(c) for c in data.columns] if data is not None else []

    def bump() -> None:
        st.session_state[f"{prefix}_ver"] = ver + 1

    if not rules:
        st.info("No validation rules yet. Rules catch bad records before they reach the destination.")
    for i, rule in enumerate(rules):
        try:
            description = rule.get("description") or describe_rule(rule)
        except ValidationError:
            description = "⚠️ invalid settings"
        action = "reject rows" if rule.get("on_fail", "reject") == "reject" else "stop run"
        if rule.get("type") in ("required_columns", "row_count"):
            action = "stop run"
        with st.container(border=True):
            cols = st.columns([0.45, 6.5, 0.6, 0.6, 0.6])
            cols[0].markdown(f"**{i + 1}**")
            muted = "" if rule.get("enabled", True) else " _(disabled)_"
            cols[1].markdown(f"**{RULE_LABELS.get(rule.get('type'), rule.get('type'))}**{muted} · _{action}_  \n{description}")
            icon = ":material/toggle_on:" if rule.get("enabled", True) else ":material/toggle_off:"
            if cols[2].button("", key=f"{prefix}_toggle_{ver}_{i}", icon=icon, help="Enable / disable"):
                rule["enabled"] = not rule.get("enabled", True)
                bump()
                st.rerun()
            if cols[3].button("", key=f"{prefix}_edit_{ver}_{i}", icon=":material/edit:", help="Edit"):
                st.session_state[f"{prefix}_editing"] = i
                bump()
                st.rerun()
            if cols[4].button("", key=f"{prefix}_del_{ver}_{i}", icon=":material/delete:", help="Delete"):
                rules.pop(i)
                st.session_state.pop(f"{prefix}_editing", None)
                bump()
                st.rerun()

    editing = st.session_state.get(f"{prefix}_editing")
    if editing is not None and editing >= len(rules):
        editing = None
    with st.container(border=True):
        if editing is not None:
            existing = rules[editing]
            rule_type = existing["type"]
            st.markdown(f"**Edit rule {editing + 1}: {RULE_LABELS.get(rule_type)}**")
        else:
            existing = None
            st.markdown("**Add a validation rule**")
            rule_type = st.selectbox("Rule", RULE_ORDER, format_func=RULE_LABELS.get, key=f"{prefix}_new_type")
        st.caption(RULE_HELP.get(rule_type, ""))
        rule = _rule_form(rule_type, columns, existing, f"{prefix}_form_{rule_type}_{ver}_{editing if editing is not None else 'new'}")
        c1, c2, _ = st.columns([1.2, 1, 4])
        if c1.button("Save rule" if editing is not None else "Add rule", type="primary", key=f"{prefix}_save_{ver}", icon=":material/check:"):
            try:
                parsed = parse_rule(rule)
            except ValidationError as exc:
                show_error(exc)
            else:
                stored = parsed.model_dump(mode="json", exclude_none=False)
                if editing is not None:
                    rules[editing] = stored
                    st.session_state.pop(f"{prefix}_editing", None)
                else:
                    rules.append(stored)
                bump()
                st.rerun()
        if editing is not None and c2.button("Cancel", key=f"{prefix}_cancel_{ver}"):
            st.session_state.pop(f"{prefix}_editing", None)
            bump()
            st.rerun()

    if data is None:
        st.info("Extract and transform data first to test the rules.")
        return None
    if not rules:
        return None
    st.divider()
    st.subheader("Validation results on the preview data")
    try:
        result = validate_dataframe(data, rules)
    except ValidationError as exc:
        show_error(exc)
        return None
    render_validation_results(result.results_as_dicts(), len(result.valid), len(result.rejected))
    if not result.rejected.empty:
        with st.expander(f"Rejected rows ({len(result.rejected):,})"):
            st.dataframe(dataframe_for_display(result.rejected, 200), hide_index=True)
    return result


def render_validation_results(results: list[dict[str, Any]], valid_count: int | None = None, rejected_count: int | None = None) -> None:
    if valid_count is not None:
        c1, c2, c3 = st.columns(3)
        c1.metric("Valid rows", f"{valid_count:,}")
        c2.metric("Rejected rows", f"{rejected_count or 0:,}")
        failed = sum(1 for r in results if r["status"] in ("failed", "error"))
        c3.metric("Rules stopping the run", failed)
        if failed:
            st.error("At least one rule would stop the run. Fix the data/transformations or change the rule.")
    frame = pd.DataFrame(
        [
            {
                "Status": status_label({"passed": "success", "rejected": "partial", "failed": "failed", "error": "failed", "skipped": "skipped"}.get(r["status"], r["status"])).split(" ")[0] + " " + r["status"],
                "Rule": r["label"],
                "Details": r["description"],
                "Violations": r["violations"],
                "Result": r["message"],
                "Examples": " | ".join(r.get("examples") or []),
            }
            for r in results
        ]
    )
    if not frame.empty:
        st.dataframe(frame, hide_index=True)
