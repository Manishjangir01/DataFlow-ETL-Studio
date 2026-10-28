"""UI tests using Streamlit's AppTest: the real app script runs headless."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src.utils.config import reset_settings_cache

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def app_env(settings_env, monkeypatch):
    for key, value in settings_env.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    st.cache_resource.clear()
    yield
    reset_settings_cache()
    st.cache_resource.clear()


def _new_app(page: str | None = "pages/create_pipeline.py") -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    if page:
        at.switch_page(page).run()
        assert not at.exception, at.exception
    return at


def test_upload_and_preview_sample(app_env, samples_dir):
    at = _new_app()
    at.text_input(key="src_name").input("Customers").run()
    at.selectbox(key="src_sample_pick").select("customers.csv").run()
    at.button(key="src_extract_sample").click().run()
    assert not at.exception, at.exception
    assert any("21" in m.value for m in at.metric), [m.value for m in at.metric]
    assert len(at.dataframe) >= 1
    uploads = list(Path(os.environ["DATAFLOW_DATA_DIR"], "uploads").glob("*customers.csv"))
    assert uploads, "the sample should be stored in data/uploads for later runs"


def test_api_source_test_connection_and_extract(app_env, monkeypatch):
    import responses

    monkeypatch.setattr("src.connectors.factory.default_resolver", lambda host, port: ["93.184.216.34"])
    payload = {"meta": {"n": 3}, "data": {"items": [{"id": i, "name": f"n{i}", "tags": ["a"]} for i in range(3)]}}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.get("https://api.example.com/v1/items", json=payload)
        at = _new_app()
        at.radio(key="src_kind").set_value("api").run()
        at.text_input(key="src_api_url").input("https://api.example.com/v1/items")
        at.checkbox(key="src_api_authorized").check().run()
        at.button(key="src_api_test").click().run()
        assert not at.exception, at.exception
        assert not at.error, [e.value for e in at.error]
        assert any("3 records" in m.value for m in at.markdown)
        at.button(key="src_api_extract").click().run()
        assert not at.exception, at.exception
        assert not at.error, [e.value for e in at.error]
        assert any(m.value == "3" for m in at.metric)


def test_api_source_blocks_private_address(app_env):
    at = _new_app()
    at.radio(key="src_kind").set_value("api").run()
    at.text_input(key="src_api_url").input("https://127.0.0.1/admin")
    at.checkbox(key="src_api_authorized").check().run()
    at.button(key="src_api_test").click().run()
    assert not at.exception
    assert any("blocked" in e.value for e in at.error), [e.value for e in at.error]


def test_api_secret_is_encrypted_and_never_rendered(app_env, monkeypatch):
    import responses

    monkeypatch.setattr("src.connectors.factory.default_resolver", lambda host, port: ["93.184.216.34"])
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.get("https://api.example.com/v1/items", json=[{"a": 1}])
        at = _new_app()
        at.radio(key="src_kind").set_value("api").run()
        at.text_input(key="src_api_url").input("https://api.example.com/v1/items")
        at.radio(key="src_api_auth_type").set_value("bearer").run()
        at.text_input(key="src_api_secret").input("tok_SUPER_SECRET_42")
        at.checkbox(key="src_api_authorized").check().run()
        at.button(key="src_api_extract").click().run()
        assert not at.exception, at.exception
        assert mock.calls[0].request.headers["Authorization"] == "Bearer tok_SUPER_SECRET_42"
        # After extraction the plaintext input is cleared and only ciphertext is kept.
        at.button(key="wiz_back").click().run()
        assert at.radio(key="src_kind").value == "api"
        assert at.text_input(key="src_api_url").value == "https://api.example.com/v1/items"
        assert at.text_input(key="src_api_secret").value == ""
        draft = at.session_state["pipeline_draft"]
        secret = draft["source"]["api"]["auth"]["secret"]
        assert secret["kind"] == "encrypted" and "tok_SUPER_SECRET_42" not in secret["value"]
        rendered = " ".join(str(getattr(e, "value", "")) for e in at.main)
        assert "tok_SUPER_SECRET_42" not in rendered


def _load_customers(at: AppTest) -> AppTest:
    at.selectbox(key="src_sample_pick").select("customers.csv").run()
    at.button(key="src_extract_sample").click().run()
    assert not at.exception, at.exception
    return at


def test_wizard_transform_and_validate(app_env):
    at = _load_customers(_new_app())
    # Preview step shows the quality report.
    assert any("Quality score" in m.label for m in at.metric)
    at.button(key="wiz_next").click().run()
    # Transform: add 'remove duplicates' and 'trim whitespace'.
    at.selectbox(key="tf_new_type").select("remove_duplicates").run()
    at.button(key="tf_save_0").click().run()
    assert not at.exception, at.exception
    at.selectbox(key="tf_new_type").select("trim_whitespace").run()
    at.button(key="tf_save_1").click().run()
    assert not at.exception, at.exception
    draft = at.session_state["pipeline_draft"]
    assert [s["type"] for s in draft["transforms"]] == ["remove_duplicates", "trim_whitespace"]
    assert any(m.label == "Rows" and m.value == "20" for m in at.metric), [(m.label, m.value) for m in at.metric]
    # Validate: add a not-null rule on email that rejects rows.
    at.button(key="wiz_next").click().run()
    at.selectbox(key="rule_new_type").select("not_null").run()
    at.selectbox(key="rule_form_not_null_0_new_col").select("email").run()
    at.button(key="rule_save_0").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    rejected = next(m for m in at.metric if m.label == "Rejected rows")
    assert rejected.value == "1"


def test_transform_error_is_reported_with_step(app_env):
    at = _load_customers(_new_app())
    at.session_state["pipeline_draft"]["transforms"] = [
        {"type": "rename_columns", "mapping": {"email": "mail"}},
        {"type": "text_case", "columns": ["email"], "case": "lower"},
    ]
    at.button(key="wiz_next").click().run()
    assert not at.exception, at.exception
    assert any("step 2" in e.value and "email" in e.value for e in at.error), [e.value for e in at.error]


def test_destination_step_saves_and_warns_before_replace(app_env):
    at = _load_customers(_new_app())  # lands on step 2 (Preview)
    for _ in range(3):
        at.button(key="wiz_next").click().run()
    assert not at.exception, at.exception
    at.radio(key="dest_type").set_value("sqlite").run()
    at.text_input(key="dest_table").input("customers").run()
    at.button(key="dest_test").click().run()
    assert any("Connected to SQLite" in s.value for s in at.success), [e.value for e in at.error]
    at.button(key="dest_save").click().run()
    assert at.session_state["pipeline_draft"]["destination"]["table"] == "customers"
    # Replace needs explicit confirmation.
    at.radio(key="dest_strategy").set_value("replace").run()
    assert any("Replace" in w.value for w in at.warning)
    at.button(key="dest_save").click().run()
    assert any("confirm" in e.value.lower() for e in at.error)
    at.checkbox(key="dest_confirm_replace").check().run()
    at.button(key="dest_save").click().run()
    assert at.session_state["pipeline_draft"]["destination"]["strategy"] == "replace"


def _build_full_pipeline(at: AppTest, table: str = "customers_clean") -> AppTest:
    """Drive the whole wizard: sample → transforms → rule → SQLite destination → save & run."""
    at = _load_customers(at)
    at.button(key="wiz_next").click().run()                     # → Transform
    at.selectbox(key="tf_new_type").select("remove_duplicates").run()
    at.button(key="tf_save_0").click().run()
    at.button(key="wiz_next").click().run()                     # → Validate
    at.selectbox(key="rule_new_type").select("not_null").run()
    at.selectbox(key="rule_form_not_null_0_new_col").select("email").run()
    at.button(key="rule_save_0").click().run()
    at.button(key="wiz_next").click().run()                     # → Destination
    at.radio(key="dest_type").set_value("sqlite").run()
    at.text_input(key="dest_table").input(table).run()
    at.button(key="dest_save").click().run()
    at.button(key="wiz_next").click().run()                     # → Review
    assert not at.exception, at.exception
    at.text_input(key="pl_name").input("Customer cleanup").run()
    at.button(key="pl_save_run").click().run()
    assert not at.exception, at.exception
    return at


def test_end_to_end_wizard_save_and_run(app_env):
    at = _build_full_pipeline(_new_app())
    assert not at.error, [e.value for e in at.error]
    assert any("saved" in s.value for s in at.success)
    loaded = next(m for m in at.metric if m.label == "Loaded")
    rejected = next(m for m in at.metric if m.label == "Rejected")
    assert (loaded.value, rejected.value) == ("19", "1")

    # The overview reflects the run.
    at.switch_page("pages/overview.py").run()
    assert not at.exception, at.exception
    values = {m.label: m.value for m in at.metric}
    assert values["Total runs"] == "1" and values["Successful runs"] == "1" and values["Records loaded"] == "19"

    # Every other page renders with data present.
    for page in ["pages/pipeline_runs.py", "pages/data_quality.py", "pages/data_sources.py",
                 "pages/transformation_builder.py", "pages/destinations.py", "pages/settings.py"]:
        at.switch_page(page).run()
        assert not at.exception, (page, at.exception)
        assert not at.error, (page, [e.value for e in at.error])


def test_pipeline_runs_page_runs_and_shows_logs(app_env):
    at = _build_full_pipeline(_new_app())
    at.switch_page("pages/pipeline_runs.py").run()
    at.selectbox(key="runs_pipeline").select_index(1).run()
    at.button(key="runs_run_now").click().run()
    assert not at.exception, at.exception
    # Second append run of the same data succeeds and is listed.
    assert any(m.label == "Loaded" and m.value == "19" for m in at.metric)
    assert any("Download log" in str(getattr(b, "label", "")) for b in at.get("download_button"))


def test_pages_render_without_pipelines(app_env):
    at = _new_app(page=None)
    assert any("Get started" in s.value for s in at.subheader)
    for page in ["pages/pipeline_runs.py", "pages/data_quality.py", "pages/data_sources.py",
                 "pages/transformation_builder.py", "pages/destinations.py", "pages/settings.py"]:
        at.switch_page(page).run()
        assert not at.exception, (page, at.exception)


def test_embedded_scheduler_runs_with_the_app(app_env, monkeypatch):
    monkeypatch.setenv("DATAFLOW_EMBEDDED_SCHEDULER", "true")
    reset_settings_cache()
    st.cache_resource.clear()
    at = _new_app(page=None)
    try:
        captions = [c.value for c in at.sidebar.caption]
        assert any("Scheduler: 🟢 running" in c for c in captions), captions
        at.switch_page("pages/settings.py").run()
        assert not at.exception, at.exception
        assert any("scheduler is running" in s.value for s in at.success), [s.value for s in at.success]
    finally:
        from src.ui.services import get_services

        scheduler = get_services().scheduler
        if scheduler is not None:
            scheduler.shutdown()
