"""Command line: python -m moodle_sync <bundle|push|stages|refresh|worker>.

bundle    build the import bundle of the course (a full snapshot)
push      build it and import it into Moodle
stages    run the model stages on one run folder (used by refresh/worker
          in the PyTorch environment)
refresh   recompute and publish named learners only (--learners 7,12)
worker    answer Moodle's refresh requests; full rebuild once a day
"""

import argparse
import json
from pathlib import Path

from course_workspace import RunLayout, add_course_arguments, workspace_from_args

from . import bundle as bundle_module


def _learner_list(text):
    return [int(part) for part in text.split(",") if part.strip()]


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m moodle_sync",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("bundle", "push"):
        command = commands.add_parser(
            name, help=f"{name} a full snapshot of the course"
        )
        add_course_arguments(command)
        command.add_argument(
            "--profile-dir", type=Path, help="default: the course's learner_model/"
        )
        command.add_argument(
            "--recommendation-dir",
            type=Path,
            help="default: the course's recommendations/",
        )
        command.add_argument(
            "--data-dir", type=Path, help="default: the course's data/"
        )
        command.add_argument(
            "--bundle-dir", type=Path, help="default: the course's bundle/"
        )
        if name == "push":
            command.add_argument(
                "--transport", default="docker", choices=["docker", "webservice"]
            )

    stages = commands.add_parser("stages", help="model stages for one run folder")
    add_course_arguments(stages)
    stages.add_argument(
        "--run-dir", type=Path, help="run folder (default: the course folder)"
    )
    stages.add_argument(
        "--kind",
        default=bundle_module.KIND_SNAPSHOT,
        choices=[bundle_module.KIND_SNAPSHOT, bundle_module.KIND_LEARNER_UPDATE],
    )

    refresh = commands.add_parser(
        "refresh", help="recompute and publish named learners only"
    )
    add_course_arguments(refresh)
    refresh.add_argument(
        "--learners",
        type=_learner_list,
        required=True,
        help="comma-separated Moodle user ids",
    )
    refresh.add_argument(
        "--transport", default="docker", choices=["docker", "webservice"]
    )
    refresh.add_argument(
        "--model-python", help="interpreter for the model stages (default .venv-kt)"
    )

    worker = commands.add_parser("worker", help="answer Moodle's refresh requests")
    add_course_arguments(worker)
    worker.add_argument("--poll", type=float, default=15, help="seconds between polls")
    worker.add_argument(
        "--full-at",
        default="03:00",
        help="local time of the daily full rebuild, HH:MM ('' = never)",
    )
    worker.add_argument(
        "--once", action="store_true", help="one poll, then exit (for cron)"
    )
    worker.add_argument(
        "--transport", default="docker", choices=["docker", "webservice"]
    )
    worker.add_argument(
        "--model-python", help="interpreter for the model stages (default .venv-kt)"
    )

    args = parser.parse_args(argv)
    workspace = workspace_from_args(args)

    if args.command in ("bundle", "push"):
        manifest = bundle_module.build_bundle(
            args.profile_dir or workspace.learner_model,
            args.recommendation_dir or workspace.recommendations,
            args.data_dir or workspace.data,
            args.bundle_dir or workspace.bundle,
        )
        rows = {name: entry["rows"] for name, entry in manifest["files"].items()}
        bundle_dir = args.bundle_dir or workspace.bundle
        print(
            f"Bundle for course {manifest['course_id']} written to {bundle_dir}: {json.dumps(rows)}"
        )
        if args.command == "push":
            from .transport import make_transport

            make_transport(args.transport).push(bundle_dir, manifest["course_id"])
    elif args.command == "stages":
        from .pipeline import run_model_stages

        layout = RunLayout(args.run_dir) if args.run_dir else workspace
        manifest = run_model_stages(layout, workspace, args.kind)
        rows = {name: entry["rows"] for name, entry in manifest["files"].items()}
        print(f"{args.kind} bundle written to {layout.bundle}: {json.dumps(rows)}")
    elif args.command == "refresh":
        from .refresh import refresh_learners
        from .transport import make_transport

        summary = refresh_learners(
            workspace, args.learners, make_transport(args.transport), args.model_python
        )
        print(json.dumps(summary, indent=2))
    elif args.command == "worker":
        from collector import connect
        from collector.config import DB_PREFIX

        from .transport import make_transport
        from .worker import Worker

        Worker(
            workspace,
            make_transport(args.transport),
            connect,
            poll_seconds=args.poll,
            full_at=args.full_at,
            python=args.model_python,
            table_prefix=DB_PREFIX,
        ).run(once=args.once)


def run(argv=None):
    """Entry point for the command line (``moodle-sync.py`` and ``-m``)."""
    from .transport import TransportError
    from .worker import WorkerBusy

    try:
        main(argv)
    except (FileNotFoundError, ValueError, TransportError, WorkerBusy) as error:
        raise SystemExit(f"moodle_sync: {error}")
    except KeyboardInterrupt:
        raise SystemExit("moodle_sync: stopped")


if __name__ == "__main__":
    run()
