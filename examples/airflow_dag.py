"""Example Apache Airflow DAG that runs DataFlow ETL Studio pipelines.

This is the upgrade path from the built-in APScheduler to production orchestration:
Airflow handles scheduling, retries, alerting, back-fills and dependencies, while
DataFlow keeps doing the extraction, transformation, validation and loading - and
still records every run in its own history (visible in the web app).

How to use it
-------------
1. Run Airflow on Linux (or WSL2 / Docker on Windows) with access to this project and
   its virtual environment.
2. Copy this file into Airflow's ``dags/`` folder.
3. Set the Airflow Variables (Admin > Variables):
      dataflow_home    = /opt/dataflow-etl-studio
      dataflow_python  = /opt/dataflow-etl-studio/.venv/bin/python
4. Set DATAFLOW_EMBEDDED_SCHEDULER=false in DataFlow's .env so schedules are not run twice,
   and point DATAFLOW_DATABASE_URL at PostgreSQL so Airflow workers share one history.
5. Adjust PIPELINES below with the pipeline IDs shown in the web app (Overview page).

`python -m src.cli run` exits with 0 on success and 1 on failure, so Airflow marks
the task as failed (and retries it) exactly when the DataFlow run failed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.models import Variable
from airflow.operators.bash import BashOperator

# Pipeline IDs from DataFlow ETL Studio. Tasks run in the listed order.
PIPELINES = {
    "load_customers": 1,
    "load_orders": 2,
}

default_args = {
    "owner": "data-engineering",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(hours=2),
}

with DAG(
    dag_id="dataflow_etl_studio",
    description="Run DataFlow ETL Studio pipelines",
    start_date=datetime(2026, 1, 1),
    schedule="0 2 * * *",          # every day at 02:00
    catchup=False,
    max_active_runs=1,             # like DataFlow itself: no overlapping runs
    default_args=default_args,
    tags=["dataflow", "etl"],
) as dag:
    previous = None
    for task_id, pipeline_id in PIPELINES.items():
        task = BashOperator(
            task_id=task_id,
            bash_command=(
                'cd "{{ var.value.dataflow_home }}" && '
                '"{{ var.value.dataflow_python }}" -m src.cli run --pipeline-id ' + str(pipeline_id)
            ),
        )
        if previous is not None:
            previous >> task
        previous = task

# Keep a module-level reference so `Variable` is not reported as unused by linters;
# templates above read the variables at run time.
_ = Variable
