"""Transformation builder: add, edit, reorder and preview transformation steps."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from src.transformations.engine import TransformResult, apply_transformations
from src.transformations.expressions import FUNCTION_HELP
from src.transformations.steps import STEP_LABELS, describe_step, parse_step
from src.ui.common import dataframe_for_display, show_error
from src.utils.exceptions import TransformationError
from src.validation.profiler import profile_dataframe

STEP_ORDER = [
    "remove_duplicates", "handle_missing", "trim_whitespace", "text_case", "replace_values",
    "cast_types", "parse_dates", "rename_columns", "standardize_column_names", "filter_rows",
    "calculated_column", "select_columns", "drop_columns",
]
STEP_HELP = {
    "remove_duplicates": "Drop rows that repeat. Compare all columns or only key columns.",
    "handle_missing": "Drop rows with empty values or fill them (value, mean, median, most frequent, previous/next).",
    "trim_whitespace": "Remove spaces at the start/end of text; optionally turn blank text into empty values.",
    "text_case": "Make text lower case, UPPER CASE, Title Case or Sentence case.",
    "replace_values": "Map values to standard ones, e.g. 'USA' → 'United States'.",
    "cast_types": "Convert columns to integer, decimal, true/false, date-time, date or text.",
    "parse_dates": "Read dates in any format and output a standard date / date-time / text format.",
    "rename_columns": "Give columns new names.",
    "standardize_column_names": "Make every column name database friendly: 'Order Date' → 'order_date'.",
    "filter_rows": "Keep only rows that match conditions or an expression.",
    "calculated_column": "Create a column from an expression, e.g. price * quantity.",
    "select_columns": "Choose which columns to keep and their order.",
    "drop_columns": "Remove columns you do not need.",
}
OPERATORS = ["==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "starts_with", "ends_with", "in", "not_in", "is_null", "not_null"]
TYPE_CHOICES = ["(keep)", "string", "integer", "float", "boolean", "datetime", "date"]
MISSING_STRATEGIES = {
    "drop_rows": "Drop rows with missing values",
    "fill_value": "Fill with a value",
    "fill_mean": "Fill with the average (numbers)",
    "fill_median": "Fill with the median (numbers)",
    "fill_mode": "Fill with the most frequent value",
    "fill_forward": "Fill with the previous row's value",
    "fill_backward": "Fill with the next row's value",
}


def _columns_before(raw_df: pd.DataFrame | None, steps: list[dict], index: int) -> list[str]:
    """Columns available to the step at ``index`` (after the steps before it)."""
    if raw_df is None:
        return []
    try:
        return [str(c) for c in apply_transformations(raw_df.head(200), steps[:index]).dataframe.columns]
    except Exception:  # noqa: BLE001 - fall back to the raw columns while editing
        return [str(c) for c in raw_df.columns]


def _column_select(label: str, columns: list[str], key: str, default: list[str] | None = None, help: str | None = None) -> list[str]:
    options = list(dict.fromkeys(columns + [c for c in (default or []) if c not in columns]))
    return st.multiselect(label, options, default=[c for c in (default or []) if c in options], key=key, help=help)


def _text_or_column(label: str, columns: list[str], key: str, default: str | None) -> str:
    if columns:
        options = columns if default in columns or not default else columns + [default]
        return st.selectbox(label, options, index=options.index(default) if default in options else 0, key=key)
    return st.text_input(label, value=default or "", key=key)


def _mapping_editor(key: str, rows: list[dict[str, str]], left: str, right: str, left_options: list[str] | None = None) -> dict[str, str]:
    frame = pd.DataFrame(rows or [{left: "", right: ""}], columns=[left, right])
    config: dict[str, Any] = {right: st.column_config.TextColumn(right)}
    config[left] = st.column_config.SelectboxColumn(left, options=left_options) if left_options else st.column_config.TextColumn(left)
    edited = st.data_editor(frame, key=key, num_rows="dynamic", hide_index=True, column_config=config)
    result: dict[str, str] = {}
    for _, row in edited.iterrows():
        a, b = row.get(left), row.get(right)
        if a is not None and str(a).strip() and not (isinstance(a, float) and pd.isna(a)):
            result[str(a)] = "" if b is None or (isinstance(b, float) and pd.isna(b)) else str(b)
    return result


def _step_form(step_type: str, columns: list[str], existing: dict[str, Any] | None, key: str) -> dict[str, Any]:
    """Render the settings for one step type and return the step dictionary."""
    e = existing or {}
    step: dict[str, Any] = {"type": step_type, "enabled": e.get("enabled", True)}
    if e.get("note"):
        step["note"] = e["note"]

    if step_type == "remove_duplicates":
        step["subset"] = _column_select("Compare only these columns (empty = all columns)", columns, f"{key}_subset", e.get("subset"))
        step["keep"] = st.radio("Keep", ["first", "last"], index=["first", "last"].index(e.get("keep", "first")), key=f"{key}_keep", horizontal=True)
    elif step_type == "handle_missing":
        step["columns"] = _column_select("Columns (empty = all columns)", columns, f"{key}_cols", e.get("columns"))
        strategies = list(MISSING_STRATEGIES)
        step["strategy"] = st.selectbox("Strategy", strategies, index=strategies.index(e.get("strategy", "drop_rows")),
                                        format_func=MISSING_STRATEGIES.get, key=f"{key}_strategy")
        if step["strategy"] == "fill_value":
            step["fill_value"] = st.text_input("Fill value", value="" if e.get("fill_value") is None else str(e.get("fill_value")), key=f"{key}_fill") or None
        if step["strategy"] == "drop_rows":
            step["how"] = st.radio("Drop a row when", ["any", "all"], index=["any", "all"].index(e.get("how", "any")),
                                   format_func=lambda h: "any selected column is empty" if h == "any" else "all selected columns are empty",
                                   key=f"{key}_how", horizontal=True)
        step["treat_blank_as_missing"] = st.checkbox("Treat blank text as missing", value=e.get("treat_blank_as_missing", True), key=f"{key}_blank")
    elif step_type == "rename_columns":
        current = e.get("mapping", {})
        rows = [{"Column": c, "New name": current.get(c, "")} for c in columns] or [{"Column": k, "New name": v} for k, v in current.items()]
        st.caption("Type a new name next to each column you want to rename.")
        mapping = _mapping_editor(f"{key}_map", rows, "Column", "New name", columns or None)
        step["mapping"] = {k: v for k, v in mapping.items() if v.strip() and v != k}
    elif step_type == "standardize_column_names":
        step["style"] = st.radio("Style", ["snake_case", "lower"], index=["snake_case", "lower"].index(e.get("style", "snake_case")), key=f"{key}_style", horizontal=True)
    elif step_type == "cast_types":
        current = e.get("casts", {})
        rows = [{"Column": c, "New type": current.get(c, "(keep)")} for c in columns] or [{"Column": k, "New type": v} for k, v in current.items()]
        frame = pd.DataFrame(rows or [{"Column": "", "New type": "(keep)"}])
        edited = st.data_editor(
            frame, key=f"{key}_casts", hide_index=True, num_rows="fixed" if columns else "dynamic",
            column_config={
                "Column": st.column_config.TextColumn("Column", disabled=bool(columns)),
                "New type": st.column_config.SelectboxColumn("New type", options=TYPE_CHOICES, required=True),
            },
        )
        step["casts"] = {str(r["Column"]): r["New type"] for _, r in edited.iterrows() if r["New type"] and r["New type"] != "(keep)" and str(r["Column"]).strip()}
        step["on_error"] = st.radio("Values that cannot be converted", ["set_null", "fail"], index=["set_null", "fail"].index(e.get("on_error", "set_null")),
                                    format_func=lambda v: "Set them to empty (and report)" if v == "set_null" else "Stop with an error", key=f"{key}_err", horizontal=True)
    elif step_type == "parse_dates":
        step["columns"] = _column_select("Date columns", columns, f"{key}_cols", e.get("columns"))
        c1, c2 = st.columns(2)
        step["input_format"] = c1.text_input("Input format (optional)", value=e.get("input_format") or "", key=f"{key}_in",
                                             help="Leave empty to auto-detect. Example: %d/%m/%Y for 31/12/2024.") or None
        step["dayfirst"] = c2.checkbox("Day comes first (31/12/2024)", value=e.get("dayfirst", False), key=f"{key}_dayfirst")
        outputs = ["date", "datetime", "string"]
        step["output"] = c1.selectbox("Output", outputs, index=outputs.index(e.get("output", "date")), key=f"{key}_out",
                                      format_func={"date": "Date", "datetime": "Date and time", "string": "Formatted text"}.get)
        if step["output"] == "string":
            step["output_format"] = c2.text_input("Output format", value=e.get("output_format", "%Y-%m-%d"), key=f"{key}_ofmt")
        step["on_error"] = st.radio("Invalid dates", ["set_null", "fail"], index=["set_null", "fail"].index(e.get("on_error", "set_null")),
                                    format_func=lambda v: "Set to empty (and report)" if v == "set_null" else "Stop with an error", key=f"{key}_err", horizontal=True)
    elif step_type == "trim_whitespace":
        step["columns"] = _column_select("Columns (empty = all text columns)", columns, f"{key}_cols", e.get("columns"))
        step["collapse_inner_spaces"] = st.checkbox("Also collapse repeated inner spaces", value=e.get("collapse_inner_spaces", False), key=f"{key}_collapse")
        step["blank_to_null"] = st.checkbox("Turn blank text into empty values", value=e.get("blank_to_null", True), key=f"{key}_blank")
    elif step_type == "text_case":
        step["columns"] = _column_select("Columns", columns, f"{key}_cols", e.get("columns"))
        cases = ["lower", "upper", "title", "sentence"]
        step["case"] = st.radio("Case", cases, index=cases.index(e.get("case", "lower")), key=f"{key}_case", horizontal=True,
                                format_func={"lower": "lower", "upper": "UPPER", "title": "Title Case", "sentence": "Sentence case"}.get)
    elif step_type == "replace_values":
        step["column"] = _text_or_column("Column", columns, f"{key}_col", e.get("column"))
        rows = [{"Old value": k, "New value": v} for k, v in e.get("mapping", {}).items()]
        step["mapping"] = _mapping_editor(f"{key}_map", rows, "Old value", "New value")
        step["case_sensitive"] = st.checkbox("Case sensitive", value=e.get("case_sensitive", False), key=f"{key}_cs")
    elif step_type == "filter_rows":
        modes = ["conditions", "expression"]
        step["mode"] = st.radio("Filter using", modes, index=modes.index(e.get("mode", "conditions")), key=f"{key}_mode", horizontal=True,
                                format_func=lambda m: "Simple conditions" if m == "conditions" else "Expression")
        if step["mode"] == "conditions":
            rows = [{"Column": c["column"], "Operator": c["operator"], "Value": "" if c.get("value") is None else (", ".join(map(str, c["value"])) if isinstance(c.get("value"), list) else str(c["value"]))}
                    for c in e.get("conditions", [])] or [{"Column": columns[0] if columns else "", "Operator": "==", "Value": ""}]
            st.caption("For 'in' / 'not_in' separate values with commas. 'is_null' / 'not_null' need no value.")
            edited = st.data_editor(
                pd.DataFrame(rows, columns=["Column", "Operator", "Value"]), key=f"{key}_conds", num_rows="dynamic", hide_index=True,
                column_config={
                    "Column": st.column_config.SelectboxColumn("Column", options=columns or None, required=True) if columns else st.column_config.TextColumn("Column"),
                    "Operator": st.column_config.SelectboxColumn("Operator", options=OPERATORS, required=True),
                    "Value": st.column_config.TextColumn("Value"),
                },
            )
            conditions = []
            for _, r in edited.iterrows():
                column = r.get("Column")
                if column is None or not str(column).strip() or (isinstance(column, float) and pd.isna(column)):
                    continue
                value = r.get("Value")
                value = None if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)
                conditions.append({"column": str(column), "operator": r.get("Operator") or "==", "value": value})
            step["conditions"] = conditions
            step["match"] = st.radio("Keep rows that match", ["all", "any"], index=["all", "any"].index(e.get("match", "all")),
                                     format_func=lambda m: "all conditions (AND)" if m == "all" else "any condition (OR)", key=f"{key}_match", horizontal=True)
        else:
            step["expression"] = st.text_area("Keep rows where", value=e.get("expression") or "", key=f"{key}_expr",
                                              placeholder="status == 'active' and lifetime_value >= 100") or None
            _expression_help()
    elif step_type == "calculated_column":
        c1, c2 = st.columns([2, 1])
        step["name"] = c1.text_input("New column name", value=e.get("name", ""), key=f"{key}_name")
        step["overwrite"] = c2.checkbox("Overwrite if it exists", value=e.get("overwrite", False), key=f"{key}_over")
        step["expression"] = st.text_area("Expression", value=e.get("expression", ""), key=f"{key}_expr", placeholder="round(price * quantity, 2)")
        _expression_help()
    elif step_type == "select_columns":
        step["columns"] = _column_select("Columns to keep, in order (select them in the order you want)", columns, f"{key}_cols", e.get("columns") or columns)
    elif step_type == "drop_columns":
        step["columns"] = _column_select("Columns to remove", columns, f"{key}_cols", e.get("columns"))
    return step


def _expression_help() -> None:
    with st.expander("Expression help"):
        st.markdown(
            "Use column names directly (wrap names with spaces in backticks: `` `order total` ``), "
            "numbers, 'text', comparisons `== != > >= < <=`, `and`, `or`, `not`, "
            "`in ['A', 'B']` and `value_if_true if condition else value_if_false`."
        )
        st.markdown("\n".join(f"- `{name}` - {text}" for name, text in FUNCTION_HELP.items()))


def _bump(prefix: str) -> None:
    st.session_state[f"{prefix}_ver"] = st.session_state.get(f"{prefix}_ver", 0) + 1


def _render_step_list(steps: list[dict], prefix: str) -> None:
    ver = st.session_state.get(f"{prefix}_ver", 0)
    if not steps:
        st.info("No transformations yet. Add one below - the preview updates instantly.")
        return
    for i, step in enumerate(steps):
        label = STEP_LABELS.get(step.get("type", ""), step.get("type", "?"))
        try:
            description = describe_step(step)
        except ValidationError:
            description = "⚠️ invalid settings - edit this step"
        with st.container(border=True):
            cols = st.columns([0.45, 6.5, 0.6, 0.6, 0.6, 0.6, 0.6])
            cols[0].markdown(f"**{i + 1}**")
            muted = "" if step.get("enabled", True) else " _(disabled)_"
            cols[1].markdown(f"**{label}**{muted}  \n{description}")
            if cols[2].button("", key=f"{prefix}_up_{ver}_{i}", icon=":material/arrow_upward:", help="Move up", disabled=i == 0):
                steps[i - 1], steps[i] = steps[i], steps[i - 1]
                _bump(prefix)
                st.rerun()
            if cols[3].button("", key=f"{prefix}_down_{ver}_{i}", icon=":material/arrow_downward:", help="Move down", disabled=i == len(steps) - 1):
                steps[i + 1], steps[i] = steps[i], steps[i + 1]
                _bump(prefix)
                st.rerun()
            toggle_icon = ":material/toggle_on:" if step.get("enabled", True) else ":material/toggle_off:"
            if cols[4].button("", key=f"{prefix}_toggle_{ver}_{i}", icon=toggle_icon, help="Enable / disable"):
                step["enabled"] = not step.get("enabled", True)
                _bump(prefix)
                st.rerun()
            if cols[5].button("", key=f"{prefix}_edit_{ver}_{i}", icon=":material/edit:", help="Edit"):
                st.session_state[f"{prefix}_editing"] = i
                _bump(prefix)
                st.rerun()
            if cols[6].button("", key=f"{prefix}_del_{ver}_{i}", icon=":material/delete:", help="Delete"):
                steps.pop(i)
                st.session_state.pop(f"{prefix}_editing", None)
                _bump(prefix)
                st.rerun()


def _render_editor(steps: list[dict], raw_df: pd.DataFrame | None, prefix: str) -> None:
    ver = st.session_state.get(f"{prefix}_ver", 0)
    editing = st.session_state.get(f"{prefix}_editing")
    if editing is not None and editing >= len(steps):
        editing = None
    with st.container(border=True):
        if editing is not None:
            existing = steps[editing]
            step_type = existing["type"]
            st.markdown(f"**Edit step {editing + 1}: {STEP_LABELS.get(step_type, step_type)}**")
            columns = _columns_before(raw_df, steps, editing)
        else:
            existing = None
            st.markdown("**Add a transformation**")
            step_type = st.selectbox("Transformation", STEP_ORDER, format_func=STEP_LABELS.get, key=f"{prefix}_new_type")
            columns = _columns_before(raw_df, steps, len(steps))
        st.caption(STEP_HELP.get(step_type, ""))
        form_key = f"{prefix}_form_{step_type}_{ver}_{editing if editing is not None else 'new'}"
        step = _step_form(step_type, columns, existing, form_key)

        c1, c2, _ = st.columns([1.2, 1, 4])
        label = "Save changes" if editing is not None else "Add step"
        if c1.button(label, type="primary", key=f"{prefix}_save_{ver}", icon=":material/check:"):
            try:
                parse_step(step)
            except ValidationError as exc:
                show_error(exc)
                return
            if editing is not None:
                steps[editing] = step
                st.session_state.pop(f"{prefix}_editing", None)
            else:
                steps.append(step)
            _bump(prefix)
            st.rerun()
        if editing is not None and c2.button("Cancel", key=f"{prefix}_cancel_{ver}"):
            st.session_state.pop(f"{prefix}_editing", None)
            _bump(prefix)
            st.rerun()


def render_transform_preview(raw_df: pd.DataFrame, steps: list[dict]) -> TransformResult | None:
    """Apply the steps to the sample and show the outcome (or the error)."""
    try:
        result = apply_transformations(raw_df, steps)
    except TransformationError as exc:
        show_error(exc, "Transformation error")
        return None
    except ValidationError as exc:
        show_error(exc)
        return None

    if result.reports:
        report = pd.DataFrame(
            [
                {
                    "#": r.index + 1,
                    "Step": r.label,
                    "Details": r.description,
                    "Rows": f"{r.rows_before:,} → {r.rows_after:,}",
                    "Result": r.message,
                    "Warnings": "; ".join(r.warnings),
                }
                for r in result.reports
            ]
        )
        st.markdown("**Step results**")
        st.dataframe(report, hide_index=True)
    for warning in result.warnings[:10]:
        st.warning(warning)

    before = profile_dataframe(raw_df)
    after = profile_dataframe(result.dataframe)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows", f"{len(result.dataframe):,}", f"{len(result.dataframe) - len(raw_df):+,}")
    c2.metric("Columns", f"{len(result.dataframe.columns):,}", f"{len(result.dataframe.columns) - len(raw_df.columns):+,}")
    c3.metric("Quality score", f"{after.quality_score:.0f}", f"{after.quality_score - before.quality_score:+.0f}")
    c4.metric("Duplicates / missing", f"{after.duplicate_rows:,} / {after.missing_cells:,}")
    st.markdown("**Transformed data preview** (first 200 rows)")
    st.dataframe(dataframe_for_display(result.dataframe, 200), hide_index=True)
    return result


def render_transform_builder(steps: list[dict], raw_df: pd.DataFrame | None, prefix: str = "tf") -> TransformResult | None:
    """Full builder: step list, add/edit form and live preview. ``steps`` is edited in place."""
    _render_step_list(steps, prefix)
    _render_editor(steps, raw_df, prefix)
    if raw_df is None:
        st.info("Extract data first to preview the transformations.")
        return None
    st.divider()
    st.subheader("Preview")
    return render_transform_preview(raw_df, steps)
