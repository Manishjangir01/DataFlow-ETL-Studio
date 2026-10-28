"""Command-line interface.

Examples (Windows PowerShell or any shell, from the project folder)::

    python -m src.cli generate-key            # print a new DATAFLOW_SECRET_KEY
    python -m src.cli init-db                 # create the metadata tables
    python -m src.cli list                    # list pipelines
    python -m src.cli run --pipeline-id 1     # run a pipeline (exit code 0 = success)
    python -m src.cli run --pipeline-id 1 --retry-of 7 --cached
    python -m src.cli runs --limit 20         # recent runs
    python -m src.cli purge --days 90         # delete old run history
    python -m src.cli worker                  # start the scheduler worker
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from src.utils.exceptions import DataFlowError


def _services():
    from src.orchestration.runner import PipelineRunner
    from src.security.secrets import SecretBox, load_or_create_key
    from src.storage.repository import Database
    from src.utils.config import get_settings
    from src.utils.logging_setup import configure_logging

    settings = get_settings()
    configure_logging(settings.logs_dir, filename="cli.log")
    key, _ = load_or_create_key(settings.secret_key, settings.config_dir)
    box = SecretBox(key)
    db = Database(settings.database_url)
    return settings, db, PipelineRunner(settings, db, box)


def cmd_generate_key(_args: argparse.Namespace) -> int:
    from src.security.secrets import generate_key

    print(generate_key())
    return 0


def cmd_init_db(_args: argparse.Namespace) -> int:
    settings, db, _ = _services()
    recovered = db.recover_interrupted_runs(timedelta(minutes=settings.lock_timeout_minutes))
    print(f"Metadata database ready ({'SQLite' if settings.is_sqlite else 'PostgreSQL'}).")
    if recovered:
        print(f"Marked interrupted runs as failed: {recovered}")
    db.dispose()
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    _, db, _ = _services()
    pipelines = db.list_pipelines()
    if not pipelines:
        print("No pipelines yet. Create one in the web app.")
    for p in pipelines:
        schedule = "scheduled" if p.schedule_enabled else "manual"
        print(f"#{p.id:<4} {p.name:<40} {schedule}")
    db.dispose()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _, db, runner = _services()
    try:
        if args.retry_of:
            outcome = runner.retry(args.retry_of, use_cached_extract=args.cached)
        else:
            outcome = runner.run(args.pipeline_id, trigger="cli")
    except DataFlowError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        db.dispose()
        return 2
    print(f"Run #{outcome.run_id}: {outcome.status.upper()} in {outcome.duration_seconds:.1f}s - "
          f"extracted {outcome.extracted}, transformed {outcome.transformed}, rejected {outcome.rejected}, loaded {outcome.loaded}")
    if outcome.error_message:
        print(f"Failed at {outcome.error_step}: {outcome.error_message}", file=sys.stderr)
    db.dispose()
    return 0 if outcome.succeeded else 1


def cmd_runs(args: argparse.Namespace) -> int:
    _, db, _ = _services()
    for r in db.list_runs(args.pipeline_id, limit=args.limit):
        print(f"#{r.id:<5} {r.pipeline_name:<30} {r.status:<8} {r.trigger:<9} {r.started_at:%Y-%m-%d %H:%M} "
              f"loaded={r.loaded_count} rejected={r.rejected_count}")
    db.dispose()
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    from src.orchestration.runner import purge_history

    settings, db, _ = _services()
    print(f"Deleted {purge_history(db, settings, args.days)} run(s) older than {args.days} days.")
    db.dispose()
    return 0


def cmd_worker(_args: argparse.Namespace) -> int:
    from src.orchestration.worker import main

    return main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description="DataFlow ETL Studio command line")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-key", help="print a new encryption key for DATAFLOW_SECRET_KEY").set_defaults(func=cmd_generate_key)
    sub.add_parser("init-db", help="create the metadata database tables").set_defaults(func=cmd_init_db)
    sub.add_parser("list", help="list pipelines").set_defaults(func=cmd_list)
    run = sub.add_parser("run", help="run a pipeline now")
    target = run.add_mutually_exclusive_group(required=True)
    target.add_argument("--pipeline-id", type=int)
    target.add_argument("--retry-of", type=int, help="retry the pipeline of this run")
    run.add_argument("--cached", action="store_true", help="with --retry-of: reuse that run's extracted data")
    run.set_defaults(func=cmd_run)
    runs = sub.add_parser("runs", help="show recent runs")
    runs.add_argument("--pipeline-id", type=int)
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)
    purge = sub.add_parser("purge", help="delete old run history")
    purge.add_argument("--days", type=int, required=True)
    purge.set_defaults(func=cmd_purge)
    sub.add_parser("worker", help="start the standalone scheduler worker").set_defaults(func=cmd_worker)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DataFlowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
