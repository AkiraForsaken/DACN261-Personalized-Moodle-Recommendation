"""Command line: python -m knowledge_engine <simulate|status|estimate|train>."""

import argparse
import json
from pathlib import Path

from course_workspace import add_course_arguments, workspace_from_args

from . import config


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m knowledge_engine", description=__doc__
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sim = commands.add_parser(
        "simulate", help="write simulated learners in collector format"
    )
    sim.add_argument("--output-dir", type=Path, default=config.DEFAULT_SIMULATED_DIR)
    sim.add_argument(
        "--course-dir",
        type=Path,
        help="collector export whose KCs and questions to reuse (default: a synthetic course)",
    )
    sim.add_argument("--learners", type=int, default=300)
    sim.add_argument("--seed", type=int, default=7)

    stat = commands.add_parser("status", help="show readiness and the active estimator")
    add_course_arguments(stat)
    stat.add_argument("--data-dir", type=Path, help="default: the course's data/")

    est = commands.add_parser("estimate", help="write learner-by-KC knowledge states")
    add_course_arguments(est)
    est.add_argument("--data-dir", type=Path, help="default: the course's data/")
    est.add_argument(
        "--output-dir", type=Path, help="default: the course's knowledge/knowledge_states/"
    )
    est.add_argument(
        "--as-of", help="inclusive cutoff: Unix seconds or timezone-aware ISO datetime"
    )
    est.add_argument(
        "--estimator",
        choices=["bkt"],
        help="use BKT for every learner even if a deep model is promoted",
    )

    trn = commands.add_parser(
        "train", help="train a deep model and apply the promotion gate"
    )
    add_course_arguments(trn)
    trn.add_argument("--data-dir", type=Path, help="default: the course's data/")
    trn.add_argument(
        "--epochs", type=int, help="override the configured number of epochs"
    )
    trn.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        default=[42, 43, 44],
        help="one model per seed; the best by validation AUC is the candidate",
    )
    trn.add_argument(
        "--force",
        action="store_true",
        help="train even if the readiness thresholds are not met",
    )
    trn.add_argument(
        "--population",
        choices=[config.SIMULATED_POPULATION, config.MOODLE_POPULATION],
        help="learner population to train on; real and simulated answers are never "
        "pooled (default: the only one present, or 'simulated' in a mixed cohort)",
    )

    orc = commands.add_parser(
        "oracle",
        help="oracle AUC (the generator's probability) on the gate's own test targets, with "
        "learner-bootstrap intervals and consistency checks (simulated learners only)",
    )
    add_course_arguments(orc)
    orc.add_argument("--data-dir", type=Path, help="default: the course's data/")
    orc.add_argument(
        "--plan",
        type=Path,
        help="plan annotated by `moodle-setup.py annotate-plan` (default: the newest in data/)",
    )
    orc.add_argument("--reps", type=int, default=2000, help="bootstrap repetitions")
    orc.add_argument("--seed", type=int, default=20261118, help="bootstrap seed")

    args = parser.parse_args(argv)
    if args.command != "simulate":
        # Everything but offline simulation belongs to one course's workspace.
        workspace = workspace_from_args(args)
        args.data_dir = args.data_dir or workspace.data
        if getattr(args, "output_dir", "unset") is None:
            args.output_dir = workspace.knowledge_states
    if args.command == "simulate":
        from .simulator import simulate

        result = simulate(args.output_dir, args.course_dir, args.learners, args.seed)
        print(json.dumps(result, indent=2))
        print(f"Simulated data written to {args.output_dir}")
    elif args.command == "status":
        from .phase import status

        print(json.dumps(status(args.data_dir, workspace.active_model_file), indent=2))
    elif args.command == "estimate":
        from .estimate import estimate

        states = estimate(
            args.data_dir,
            args.output_dir,
            args.as_of,
            active_model_file=workspace.active_model_file,
            force_estimator=args.estimator,
            kt_dir=workspace.kt,
        )
        manifest = json.loads((args.output_dir / config.MANIFEST_FILE_NAME).read_text())
        print(
            f"{len(states):,} learner-KC states written to "
            f"{args.output_dir / config.STATE_FILE_NAME}"
        )
        for population, entry in manifest["by_population"].items():
            print(
                f"{population} ({entry['learners']} learners): {entry['estimator']} "
                f"(phase {entry['estimator_phase']}) - {entry['estimator_reason']}"
            )
    elif args.command == "oracle":
        from .oracle import find_annotated_plan, oracle_report, to_markdown, write_report

        plan = args.plan or find_annotated_plan(args.data_dir)
        if plan is None:
            raise FileNotFoundError(
                f"No annotated plan in {args.data_dir}. Run `python moodle-setup.py annotate-plan "
                "--plan <plan file>` first."
            )
        report = oracle_report(
            args.data_dir, workspace.kt, plan, workspace.active_model_file, args.reps, args.seed
        )
        directory = write_report(report, workspace.kt, workspace.active_model_file)
        print(to_markdown(report))
        print(f"Written to {directory}/oracle_report.md and .json")
        if not report["all_checks_passed"]:
            raise SystemExit("knowledge_engine: a consistency check failed; see the report")
    elif args.command == "train":
        from .phase import train

        record = train(
            args.data_dir,
            workspace.active_model_file,
            workspace.kt,
            epochs=args.epochs,
            seeds=tuple(args.seeds),
            force=args.force,
            population=args.population,
        )
        print(json.dumps(record, indent=2))
        print(
            f"Phase {record['phase']} for {record['training_population']} learners: "
            f"{record['estimator']} - {record['reason']}"
        )
        oracle = record.get("oracle")
        if oracle:
            print(
                f"Oracle AUC on the gate's {oracle['n_targets']:,} targets: {oracle['oracle_auc']} "
                f"(95 % CI {oracle['oracle_auc_ci'][0]}-{oracle['oracle_auc_ci'][1]}); "
                "see oracle_report.md"
            )
        all_answers = record.get("oracle_auc_all_answers", record.get("simulator_ceiling_auc"))
        if all_answers is not None:
            print(
                f"Generator AUC over all simulated answers: {all_answers} "
                "(other answers than the gate's; not comparable with its AUCs)"
            )


def run(argv=None):
    """Entry point for the command line (``knowledge-engine.py`` and ``-m``)."""
    try:
        main(argv)
    except (FileNotFoundError, ValueError) as error:
        # Missing or inconsistent inputs are user-facing conditions, not crashes.
        raise SystemExit(f"knowledge_engine: {error}")


if __name__ == "__main__":
    run()
