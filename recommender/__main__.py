"""Command line: python -m recommender <generate|evaluate>."""

import argparse
import json
from pathlib import Path

from course_workspace import add_course_arguments, workspace_from_args

from . import config


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m recommender", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    gen = commands.add_parser("generate", help="write recommendations for every learner")
    add_course_arguments(gen)
    gen.add_argument(
        "--profile-dir",
        type=Path,
        help="learner-model build (default: the course's learner_model/)",
    )
    gen.add_argument(
        "--data-dir",
        type=Path,
        help="collector export the profiles were built from (default: the course's data/)",
    )
    gen.add_argument(
        "--output-dir", type=Path, help="default: the course's recommendations/"
    )
    gen.add_argument("--top-n", type=int, default=config.TOP_N)
    gen.add_argument(
        "--policy",
        default="rules",
        choices=["rules", "course_order", "popularity", "random"],
        help="'rules' is the recommender; the others are evaluation baselines",
    )
    gen.add_argument(
        "--generated-at", help="pin the generation timestamp (ISO) for identical reruns"
    )

    ev = commands.add_parser(
        "evaluate", help="offline replay: compare policies at past cutoffs"
    )
    add_course_arguments(ev)
    ev.add_argument("--data-dir", type=Path, help="default: the course's data/")
    ev.add_argument("--output-dir", type=Path, help="default: the course's evaluation/")
    ev.add_argument("--cutoffs", type=int, default=6, help="number of cutoff times")
    ev.add_argument(
        "--horizon-days",
        type=float,
        default=7.0,
        help="what a learner did within this many days after a cutoff is the outcome",
    )
    ev.add_argument("--top-n", type=int, default=config.TOP_N)
    ev.add_argument(
        "--estimators",
        nargs="+",
        default=["bkt", "active"],
        choices=["bkt", "active"],
        help="knowledge states to replay with: BKT for everyone, and/or the active "
        "estimator (a promoted deep model is then scored on its held-out learners only)",
    )
    ev.add_argument(
        "--work-dir", type=Path, help="keep the per-cutoff snapshots here (default: temp)"
    )

    args = parser.parse_args(argv)
    workspace = workspace_from_args(args)
    args.data_dir = args.data_dir or workspace.data
    if args.command == "generate":
        args.profile_dir = args.profile_dir or workspace.learner_model
        args.output_dir = args.output_dir or workspace.recommendations
    else:
        args.output_dir = args.output_dir or workspace.evaluation
    if args.command == "generate":
        from .generate import generate

        frame = generate(
            profile_dir=args.profile_dir,
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            policy=args.policy,
            top_n=args.top_n,
            generated_at=args.generated_at,
        )
        manifest = json.loads((args.output_dir / config.MANIFEST_FILE).read_text())
        summary = manifest["summary"]
        print(
            f"{len(frame):,} recommendations for {summary['learners_with_recommendations']} "
            f"of {summary['learners']} learners written to "
            f"{args.output_dir / config.RECOMMENDATIONS_FILE}"
        )
        print(f"By basis: {summary['rows_by_basis']}")
        print(f"By reason: {summary['rows_by_reason_code']}")
        print(
            f"Learners with at least one evidence- or prediction-based item: "
            f"{summary['learners_with_personalised_item']}"
        )
    elif args.command == "evaluate":
        from .evaluate import evaluate

        report = evaluate(
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            n_cutoffs=args.cutoffs,
            horizon_days=args.horizon_days,
            top_n=args.top_n,
            estimators=tuple(args.estimators),
            work_dir=args.work_dir,
            active_model_file=workspace.active_model_file,
        )
        print(f"Evaluation written to {args.output_dir}")
        print(report)


def run(argv=None):
    """Entry point for the command line (``recommend.py`` and ``-m``)."""
    try:
        main(argv)
    except (FileNotFoundError, ValueError) as error:
        # Missing or inconsistent inputs are user-facing conditions, not crashes.
        raise SystemExit(f"recommender: {error}")


if __name__ == "__main__":
    run()
