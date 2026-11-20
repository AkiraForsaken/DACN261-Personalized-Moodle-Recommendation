"""Where the artifacts of one Moodle course live.

Every course the engine serves gets its own folder, so two courses (or the
same course id on two Moodle sites) can never overwrite each other's data,
trained model or recommendations:

    <root>/<site>/<course_id>/
        course.json        which course this is (id, names, site)
        data/              collector export
        knowledge/         active_model.json, knowledge_states/
        kt/                the course's deep-KT model: data_config.json,
                           processed/ (maps, splits), pykt/ (sequences),
                           artifacts/ (checkpoints, selected_model.json, evaluation)
        learner_model/     learner-model build
        recommendations/   recommender output
        evaluation/        offline replay
        bundle/            what is imported into Moodle
        refresh/<stamp>/   per-learner refresh runs (same sub-layout as a full run)
        worker/            state, log and lock of the refresh worker

``<root>`` is ``$MICROLEARNING_HOME`` or ``<repository>/courses``; ``<site>``
is ``$MICROLEARNING_SITE`` or ``local``. The benchmark experiment
(``knowledge_tracing`` with ``english_kt``) is not a course and keeps its own
folders.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME_ENV = "MICROLEARNING_HOME"
SITE_ENV = "MICROLEARNING_SITE"
COURSE_ENV = "MOODLE_COURSE_ID"
DEFAULT_SITE = "local"
COURSE_FILE = "course.json"


class WorkspaceError(ValueError):
    """No course was named, or the name is not usable."""


def default_root():
    return Path(os.getenv(HOME_ENV) or PROJECT_ROOT / "courses")


@dataclass(frozen=True)
class RunLayout:
    """The folders one pipeline run writes, from collector to bundle.

    A full run uses the course folder itself; a per-learner refresh uses
    ``refresh/<stamp>/`` with the same names, so every stage runs unchanged.
    """

    base: Path

    @property
    def data(self):
        return self.base / "data"

    @property
    def knowledge_states(self):
        return self.base / "knowledge" / "knowledge_states"

    @property
    def knowledge_state_file(self):
        return self.knowledge_states / "learner_kc_state.csv"

    @property
    def learner_model(self):
        return self.base / "learner_model"

    @property
    def recommendations(self):
        return self.base / "recommendations"

    @property
    def bundle(self):
        return self.base / "bundle"


@dataclass(frozen=True)
class CourseWorkspace(RunLayout):
    """The folder of one course on one Moodle site."""

    course_id: int = 0
    site: str = DEFAULT_SITE

    @classmethod
    def of(cls, course_id, site=None, root=None):
        try:
            course_id = int(course_id)
        except (TypeError, ValueError):
            raise WorkspaceError(f"Not a Moodle course id: {course_id!r}") from None
        if course_id <= 1:
            raise WorkspaceError(f"Not a Moodle course id: {course_id} (the site is id 1)")
        site = site or os.getenv(SITE_ENV) or DEFAULT_SITE
        if not site.replace("-", "").replace("_", "").isalnum():
            raise WorkspaceError(f"A site name may use letters, digits, '-' and '_': {site!r}")
        root = Path(root) if root is not None else default_root()
        return cls(base=root / site / str(course_id), course_id=course_id, site=site)

    @classmethod
    def from_env(cls, course_id=None, site=None, root=None):
        """The workspace of ``course_id``, else of ``$MOODLE_COURSE_ID``."""
        course_id = course_id if course_id is not None else os.getenv(COURSE_ENV)
        if course_id in (None, ""):
            raise WorkspaceError(
                "Which course? Pass --course <id> or set MOODLE_COURSE_ID."
            )
        return cls.of(course_id, site, root)

    # --- course-level folders (shared by full runs and refreshes) -----------
    @property
    def knowledge(self):
        return self.base / "knowledge"

    @property
    def active_model_file(self):
        return self.knowledge / "active_model.json"

    @property
    def kt(self):
        return self.base / "kt"

    @property
    def kt_processed(self):
        return self.kt / "processed"

    @property
    def split_file(self):
        return self.kt_processed / "split_assignments.csv"

    @property
    def evaluation(self):
        return self.base / "evaluation"

    @property
    def refresh(self):
        return self.base / "refresh"

    @property
    def worker(self):
        return self.base / "worker"

    @property
    def course_file(self):
        return self.base / COURSE_FILE

    def refresh_run(self, stamp=None):
        """A fresh per-learner run folder: ``refresh/<UTC stamp>/``."""
        stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return RunLayout(self.refresh / stamp)

    def describe(self):
        """``{name: path}`` of every folder, for ``python -m course_workspace show``."""
        names = (
            "base data knowledge active_model_file knowledge_states kt learner_model "
            "recommendations evaluation bundle refresh worker"
        ).split()
        return {name: getattr(self, name) for name in names}

    def read_course_info(self):
        path = self.course_file
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def write_course_info(self, **info):
        """Record or update what this course is (id, names, site)."""
        current = self.read_course_info()
        current.update(
            {key: value for key, value in info.items() if value is not None},
            course_id=self.course_id,
            site=self.site,
        )
        current.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        self.base.mkdir(parents=True, exist_ok=True)
        self.course_file.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return current


def add_course_arguments(parser):
    """``--course`` and ``--site`` for every command line of the pipeline."""
    parser.add_argument(
        "--course",
        help=f"Moodle course id (default: ${COURSE_ENV}); selects the course's workspace",
    )
    parser.add_argument(
        "--site", help=f"Moodle site name of the workspace (default: ${SITE_ENV} or 'local')"
    )


def workspace_from_args(args):
    return CourseWorkspace.from_env(getattr(args, "course", None), getattr(args, "site", None))


def resolve(path, attribute, workspace=None):
    """``path`` if given, else the workspace's ``attribute`` (course from the env)."""
    if path is not None:
        return Path(path)
    workspace = workspace or CourseWorkspace.from_env()
    return Path(getattr(workspace, attribute))
