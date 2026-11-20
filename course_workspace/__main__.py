"""Command line: python -m course_workspace <show|list|migrate>.

    show     the folders of one course's workspace
    list     every course workspace on this machine
    migrate  move the pre-workspace artifacts of one course into its workspace
             (dry run unless --apply)
"""

import argparse
import json

from . import add_course_arguments, default_root, workspace_from_args
from . import migrate as migration


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m course_workspace",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="print the folders of one course")
    add_course_arguments(show)
    commands.add_parser("list", help="list the course workspaces")
    mig = commands.add_parser("migrate", help="move old project-level artifacts in")
    add_course_arguments(mig)
    mig.add_argument("--apply", action="store_true", help="move (default: only list)")
    args = parser.parse_args(argv)

    if args.command == "list":
        root = default_root()
        for course_file in sorted(root.glob("*/*/course.json")):
            info = json.loads(course_file.read_text(encoding="utf-8"))
            print(f"{course_file.parent}  {info.get('shortname', '')}  {info.get('fullname', '')}")
        return
    workspace = workspace_from_args(args)
    if args.command == "show":
        for name, path in workspace.describe().items():
            print(f"{name:18} {path}")
        return
    if not args.apply:
        moves = migration.plan(workspace)
        for source, target in moves:
            print(f"would move {source} -> {target}")
        print(f"{len(moves)} item(s). Run again with --apply to move them.")
        return
    moves = migration.apply(workspace)
    print(f"Moved {len(moves)} item(s) into {workspace.base}. The old folders are now empty or gone;")
    print("delete what is left of them yourself once the pipeline runs from the workspace.")


def run(argv=None):
    try:
        main(argv)
    except ValueError as error:
        raise SystemExit(f"course_workspace: {error}")


if __name__ == "__main__":
    run()
