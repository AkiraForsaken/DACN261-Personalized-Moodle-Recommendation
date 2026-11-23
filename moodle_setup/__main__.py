"""Set up the local Moodle test instance.

python -m moodle_setup build-course       create the English course (--layout kc|dimension)
python -m moodle_setup simulate-learners  add simulated learners and attempts
python -m moodle_setup remove-simulated   delete the simulated learners
python -m moodle_setup check-content      validate the course content only
"""

import argparse
import json
import tempfile
from pathlib import Path

from collector.collect_question_answers import PLAN_MANIFEST_NAME
from course_workspace import CourseWorkspace, default_root

from knowledge_engine.simulator import DEFAULT_LESSON_GAIN

from . import content, docker


def _write_temp_json(payload, name):
    path = Path(tempfile.mkdtemp(prefix="moodle_setup_")) / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# php/build_course.php reads spec["quizzes"] and spec["sections"] (quiz roles,
# random pool slots, question tags). Kept as a switch so a spec with quizzes is
# never sent to a build script that would ignore them.
BUILD_SUPPORTS_QUIZ_ROLES = True


def build_course(args):
    spec = content.build_spec(layout=args.layout)
    if "quizzes" in spec and not BUILD_SUPPORTS_QUIZ_ROLES:
        raise SystemExit(
            f"Layout {args.layout} now has practice, check-in, review and challenge "
            "quizzes, but php/build_course.php cannot build them yet. Nothing was "
            "changed in Moodle. Use `check-content --layout kc` to validate the content."
        )
    remote = docker.copy_in(_write_temp_json(spec, "course_spec.json"))
    result = docker.run_php(
        "build_course.php", remote, *(["--rebuild"] if args.rebuild else [])
    )
    workspace = CourseWorkspace.of(result["course_id"], args.site)
    workspace.write_course_info(
        shortname=result["shortname"], built_by="moodle_setup", layout=args.layout
    )
    print(
        f"\nCourse {result['shortname']} is ready: {result['kcs']} KCs, "
        f"{result['questions']} questions, {result['lessons']} lessons, "
        f"{result['quizzes']} quizzes (pass mark {result['gradepass']}/10), layout "
        f"{result.get('layout', args.layout)}, {result.get('competencies', 0)} KC competencies "
        f"linked to {result.get('competency_links', 0)} activities.\n"
        + (
            f"Quizzes by role: {result['quizzes_by_role']}; {result.get('random_slots', 0)} random slots.\n"
            if result.get("quizzes_by_role")
            else ""
        )
        + f"Workspace: {workspace.base}\n"
        f"For the pipeline:  export MOODLE_COURSE_ID={result['course_id']}"
    )


def simulate_learners(args):
    from knowledge_engine.simulator import simulate_plan

    structure = docker.run_php("export_structure.php", args.shortname)
    lessons = structure.get("lessons") or {}
    if not lessons:
        print(
            "NOTE: the course has no KC lesson pages (pages with a KC ID number); "
            "learners will only take quizzes. Rebuild the course to add them."
        )
    plan, manifest = simulate_plan(
        structure["quizzes"],
        n_learners=args.learners,
        seed=args.seed,
        start_unix=structure["startdate"],
        lessons=lessons,
        overviews=structure.get("overviews") or {},
        lesson_gain=args.lesson_gain,
        session_scale=args.session_scale,
        items=structure.get("items") or {},
    )
    answers = sum(len(a["answers"]) for learner in plan for a in learner["attempts"])
    attempts = sum(len(learner["attempts"]) for learner in plan)
    print(
        f"Plan: {len(plan)} learners, {attempts:,} quiz attempts, {answers:,} answers "
        f"(seed {args.seed}). Generator ceiling AUC: {manifest['ceiling_auc']}."
    )
    if manifest.get("attempts_by_role"):
        print(f"Attempts by quiz role: {manifest['attempts_by_role']}.")
    pages = manifest.get("page_behaviour")
    if pages:
        print(
            f"Pages: {pages['lesson_views']:,} lesson views, {pages['overview_views']:,} overview "
            f"views, {pages['revisions']:,} revision sessions, {pages['reviews']:,} attempt "
            f"reviews (lesson gain {pages['lesson_gain']})."
        )
    if args.plan_out:
        # A freshly built course's workspace has no data/ folder yet.
        Path(args.plan_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.plan_out).write_text(
            json.dumps({"manifest": manifest, "learners": plan}), encoding="utf-8"
        )
        print(f"Plan written to {args.plan_out}")
    if args.dry_run:
        return
    remote = docker.copy_in(_write_temp_json({"learners": plan}, "attempt_plan.json"))
    result = docker.run_php("inject_attempts.php", args.shortname, remote)
    print(
        f"\nMoodle now holds {result['simulated_users']} simulated learners; this run added "
        f"{result['attempts_added']:,} attempts ({result['answers_added']:,} answers)."
    )
    data_dir = args.data_dir or CourseWorkspace.of(structure["course_id"], args.site).data
    save_plan_manifest(manifest, result["simulated_users"], data_dir)


def save_plan_manifest(manifest, simulated_users, data_dir):
    """Keep the plan's manifest (with the ceiling AUC) next to the collector export.

    ``python -m knowledge_engine train`` reads ``ceiling_auc`` from it. The
    plan for N learners covers every simulated learner only if Moodle holds no
    more than N of them; otherwise the existing manifest is left alone.
    """
    path = Path(data_dir) / PLAN_MANIFEST_NAME
    if simulated_users > manifest["learners"]:
        print(
            f"Moodle holds {simulated_users} simulated learners but this plan covers "
            f"{manifest['learners']}; {path} was not updated."
        )
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Plan manifest (ceiling AUC {manifest['ceiling_auc']}) saved to {path}")
    return path


def annotate_plan(args):
    """Attach p_correct and Moodle user ids to a saved plan (needs the live course)."""
    from .plan import PlanMismatch, annotate_plan as annotate

    source = Path(args.plan)
    stored = json.loads(source.read_text(encoding="utf-8"))
    structure = docker.run_php("export_structure.php", args.shortname)
    users = {int(k): int(v) for k, v in docker.run_php("export_simulated_users.php")["users"].items()}
    try:
        annotated = annotate(stored, structure, users)
    except PlanMismatch as error:
        raise SystemExit(f"annotate-plan: {error}")
    target = Path(args.out) if args.out else source.with_name(source.stem + ".annotated.json")
    target.write_text(json.dumps(annotated), encoding="utf-8")
    answers = sum(len(a["answers"]) for learner in annotated["learners"] for a in learner["attempts"])
    print(
        f"Plan reproduced exactly ({answers:,} answers). Wrote {target} with p_correct per answer "
        f"and the Moodle user of {sum('moodle_userid' in l for l in annotated['learners'])} learners."
    )


def remove_simulated(args):
    result = docker.run_php("remove_simulated.php")
    print(
        f"Removed {result['users_removed']} simulated learners and {result['attempts_removed']:,} attempts."
    )
    # Simulated users are site-wide, so no course of this site holds them any more.
    site_dir = default_root() / (args.site or CourseWorkspace.of(2).site)
    for manifest in site_dir.glob(f"*/data/{PLAN_MANIFEST_NAME}"):
        manifest.unlink()
    print("Run the collector again so the export no longer contains them.")


def check_content(args):
    spec = content.build_spec(layout=args.layout)
    print(
        f"Content is valid for layout {args.layout}: {len(spec['units'])} units, {len(spec['kcs'])} KCs, "
        f"{len(spec['questions'])} questions, {len(spec['lessons'])} lessons."
    )


def _layout_option(parser):
    parser.add_argument(
        "--layout",
        choices=content.LAYOUTS,
        default=content.DEFAULT_LAYOUT,
        help="kc: one lesson + one 4-5 question check quiz per KC, KCs attached as "
        f"competencies (course {content.KC_COURSE_SHORTNAME}, default); dimension: one "
        f"quiz per unit and dimension (course {content.COURSE_SHORTNAME}, course 10)",
    )


def _shortname_option(parser):
    parser.add_argument(
        "--shortname",
        default=content.KC_COURSE_SHORTNAME,
        help=f"course to work on (default {content.KC_COURSE_SHORTNAME}; "
        f"plans of course 10 need {content.COURSE_SHORTNAME})",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m moodle_setup",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser(
        "build-course", help="create the English course in Moodle"
    )
    build.add_argument(
        "--rebuild",
        action="store_true",
        help="delete the existing course (and every attempt in it) first",
    )
    _layout_option(build)
    build.set_defaults(run=build_course)

    sim = commands.add_parser(
        "simulate-learners", help="inject simulated learners and quiz attempts"
    )
    sim.add_argument(
        "--learners",
        type=int,
        default=300,
        help="total simulated learners wanted; existing ones are kept and skipped",
    )
    sim.add_argument("--seed", type=int, default=7)
    sim.add_argument(
        "--plan-out", help="also save the attempt plan (with true answers) to this file"
    )
    sim.add_argument(
        "--dry-run", action="store_true", help="make the plan but do not touch Moodle"
    )
    sim.add_argument(
        "--lesson-gain",
        type=float,
        default=DEFAULT_LESSON_GAIN,
        help="knowledge gain from reading a KC lesson, as a share of one practice "
        "answer's gain (generator assumption; 0 = lessons have no effect)",
    )
    sim.add_argument(
        "--session-scale",
        type=float,
        default=3.5,
        help="mean sessions per learner is about 1 + 2 * scale; one session is one "
        "quiz attempt (generator assumption; changing it changes the data volume)",
    )
    _shortname_option(sim)
    sim.add_argument(
        "--data-dir",
        type=Path,
        help="collector output directory that receives the plan manifest "
        "(default: the course workspace's data/)",
    )
    sim.set_defaults(run=simulate_learners)

    annotate = commands.add_parser(
        "annotate-plan",
        help="add each answer's generating probability and each learner's Moodle id to a "
        "saved plan (regenerates it from its seed; read-only in Moodle)",
    )
    annotate.add_argument("--plan", required=True, help="plan file written by --plan-out")
    annotate.add_argument("--out", help="default: <plan>.annotated.json next to it")
    _shortname_option(annotate)
    annotate.set_defaults(run=annotate_plan)

    commands.add_parser(
        "remove-simulated", help="delete simulated learners and their attempts"
    ).set_defaults(run=remove_simulated)
    check = commands.add_parser("check-content", help="validate the course content")
    _layout_option(check)
    check.set_defaults(run=check_content)

    for command in commands.choices.values():
        command.add_argument(
            "--site", help="workspace site name (default: $MICROLEARNING_SITE or 'local')"
        )
    args = parser.parse_args(argv)
    try:
        args.run(args)
    except ValueError as error:
        raise SystemExit(f"moodle_setup: {error}")


if __name__ == "__main__":
    main()
