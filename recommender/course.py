"""The course as the recommender sees it: ordered activities and KC links.

Built from two collector exports, with no hand-maintained mapping:

    activity_catalog.csv   every course module, in course order (``sequence``)
    kc_activity_map.csv    KC -> lesson (role ``learn``) and KC -> quiz that
                           practises it (role ``practice``)

Only activities a learner can be sent to are kept: visible, and tracked for
completion when the course uses completion tracking (which excludes the forum
and the question bank).
"""

from dataclasses import dataclass, field

import pandas as pd

ROLE_LEARN = "learn"
ROLE_PRACTICE = "practice"
NON_LEARNING_TYPES = {"forum", "qbank", "label"}


@dataclass(frozen=True)
class Activity:
    module_id: int
    activity_type: str
    name: str
    topic: str
    section: int
    sequence: int


@dataclass
class CourseGraph:
    """Activities in course order plus the KC links between them."""

    activities: list  # recommendable activities, in course order
    by_module: dict = field(default_factory=dict)  # module_id -> Activity
    lesson_of_kc: dict = field(default_factory=dict)  # kc_id -> lesson module_id
    quiz_of_kc: dict = field(default_factory=dict)  # kc_id -> practice quiz module_id
    kcs_of_quiz: dict = field(default_factory=dict)  # quiz module_id -> [kc_id]
    kc_of_lesson: dict = field(default_factory=dict)  # lesson module_id -> kc_id
    kc_names: dict = field(default_factory=dict)  # kc_id -> name
    kc_dimension: dict = field(default_factory=dict)  # kc_id -> dimension (grammar, ...)

    def unit_of_kc(self, kc_id):
        """The course section of the KC's lesson, or None."""
        lesson = self.by_module.get(self.lesson_of_kc.get(kc_id))
        return lesson.section if lesson else None

    def prerequisite_kcs(self, kc_id):
        """KCs assumed to come before ``kc_id``: same dimension, earlier unit.

        A course-design assumption (``config.PREREQUISITE_RELATION``), not a
        relation learned from data. Nearest unit first, then course order.
        """
        dimension, unit = self.kc_dimension.get(kc_id), self.unit_of_kc(kc_id)
        if not dimension or unit is None:
            return []
        earlier = [
            other
            for other, other_dimension in self.kc_dimension.items()
            if other != kc_id
            and other_dimension == dimension
            and self.unit_of_kc(other) is not None
            and self.unit_of_kc(other) < unit
        ]
        return sorted(
            earlier,
            key=lambda other: (
                -self.unit_of_kc(other),
                self.by_module[self.lesson_of_kc[other]].sequence,
            ),
        )

    def from_section_of(self, module_id):
        """Course order starting at the first activity of ``module_id``'s section.

        Wraps around, so every activity appears once. With an unknown module
        (or None) it is plain course order from the start.
        """
        anchor = self.by_module.get(module_id)
        if anchor is None:
            return list(self.activities)
        start = next(
            index
            for index, activity in enumerate(self.activities)
            if activity.section == anchor.section
        )
        return self.activities[start:] + self.activities[:start]


def _text(value):
    return "" if value is None or pd.isna(value) else str(value)


def build_course_graph(activity_catalog, kc_activity_map, kc_names=None, kc_dimension=None):
    """Turn the two collector exports into a ``CourseGraph``."""
    catalog = activity_catalog.copy()
    if "visible" in catalog.columns:
        catalog = catalog[pd.to_numeric(catalog["visible"], errors="coerce").fillna(1).gt(0)]
    tracking = (
        pd.to_numeric(catalog["completion_tracking"], errors="coerce").fillna(0)
        if "completion_tracking" in catalog.columns
        else pd.Series(0, index=catalog.index)
    )
    if tracking.gt(0).any():
        catalog = catalog[tracking.gt(0)]
    else:  # a course without completion tracking: every learning activity counts
        catalog = catalog[~catalog["activity_type"].astype(str).isin(NON_LEARNING_TYPES)]
    catalog = catalog.sort_values("sequence", kind="mergesort")

    activities = [
        Activity(
            module_id=int(row.course_module_id),
            activity_type=str(row.activity_type),
            name=_text(getattr(row, "activity_name", "")),
            topic=_text(getattr(row, "topic", "")),
            section=int(getattr(row, "section_number", 0) or 0),
            sequence=int(row.sequence),
        )
        for row in catalog.itertuples(index=False)
    ]
    graph = CourseGraph(
        activities=activities,
        by_module={activity.module_id: activity for activity in activities},
        kc_names={str(k): str(v) for k, v in (kc_names or {}).items()},
        kc_dimension={str(k): str(v) for k, v in (kc_dimension or {}).items() if v},
    )
    if kc_activity_map is None or kc_activity_map.empty:
        return graph

    links = kc_activity_map.assign(
        kc_id=kc_activity_map["kc_id"].astype(str),
        course_module_id=pd.to_numeric(kc_activity_map["course_module_id"], errors="coerce"),
    ).dropna(subset=["course_module_id"])
    links["course_module_id"] = links["course_module_id"].astype(int)
    links = links[links["course_module_id"].isin(graph.by_module)]
    links["_sequence"] = links["course_module_id"].map(
        lambda module_id: graph.by_module[module_id].sequence
    )
    questions = (
        pd.to_numeric(links["question_count"], errors="coerce").fillna(0)
        if "question_count" in links.columns
        else 0
    )
    links = links.assign(_questions=questions)

    lessons = links[links["role"].eq(ROLE_LEARN)].sort_values("_sequence", kind="mergesort")
    for row in lessons.itertuples(index=False):
        # A KC with several resources: the first in course order is its lesson.
        graph.lesson_of_kc.setdefault(row.kc_id, row.course_module_id)
        graph.kc_of_lesson.setdefault(row.course_module_id, row.kc_id)
    practice = links[links["role"].eq(ROLE_PRACTICE)].sort_values(
        ["_questions", "_sequence"], ascending=[False, True], kind="mergesort"
    )
    for row in practice.itertuples(index=False):
        # A KC practised in several quizzes: the one with most of its questions.
        graph.quiz_of_kc.setdefault(row.kc_id, row.course_module_id)
    for row in practice.sort_values("kc_id", kind="mergesort").itertuples(index=False):
        graph.kcs_of_quiz.setdefault(row.course_module_id, []).append(row.kc_id)
    return graph
