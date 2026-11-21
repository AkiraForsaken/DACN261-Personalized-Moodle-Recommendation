"""Derive the course's KC catalog and mappings from the Moodle question bank.

Authoring convention (the course designer's side of the contract):

  * A question category with a non-empty ``idnumber`` is one Knowledge
    Component: ``idnumber`` -> kc_id, ``name`` -> kc_name, ``info`` -> learning
    objective.
  * Its parent category's name is the KC dimension (grammar, vocabulary, ...).
    A KC category directly under the question bank's top category gets the
    dimension ``general``.
  * A question belongs to exactly one category, hence to at most one KC.

Questions in categories without an ``idnumber`` are left unmapped; the answer
exporter counts them in ``question_answer_mapping_report.csv``.

Outputs (written to the collector output directory):

    kc_catalog.csv              kc_id, kc_name, kc_dimension, learning_objective
    moodle_question_kc_map.csv  question_bank_entry_id -> kc_id (versioned)
    kc_topic_map.csv            kc_id -> course topic, from the quizzes that
                                use (or have used) the KC's questions
    kc_activity_map.csv         kc_id -> activities: role "learn" for a
                                learning resource of the KC (see below), role
                                "practice" for a quiz that uses the KC's
                                questions (question_count of them)
    kc_mapping_report.json      where the activity links came from and the
                                granularity checks (KCs per quiz, competency
                                vs question disagreements, KCs without a
                                lesson or quiz)

A KC's learning resource (page, URL, book, ...) is found in Moodle, in this
order:

  1. Course-module competencies (``competency_modulecomp``): a non-quiz module
     linked to the competency whose ID number is the kc_id. This is the
     Moodle-native way and the convention of the one-KC-per-activity course
     (ENG-MICRO-KC).
  2. Only when the course has no module competencies at all: the module whose
     course-module ID number equals the kc_id (course ENG-MICRO, id 10).

A quiz's KCs always come from the questions it uses (what it measures); a
quiz's own competency links are only checked against them. Questions keep
their KC from the question category, because Moodle has no per-question
competency. This is how a recommender finds what to show for a weak KC
without any hand-maintained mapping.

Nothing here reads any benchmark dataset: KC identity belongs to the course.
"""

import hashlib
import re
from pathlib import Path

from .config import QUESTION_KC_MAPPING_IS_OVERRIDE, t
from .db import question_kc_mapping_file, write_csv, write_json

KC_CATALOG_COLUMNS = ["kc_id", "kc_name", "kc_dimension", "learning_objective"]
QUESTION_KC_MAP_COLUMNS = [
    "question_bank_entry_id",
    "kc_id",
    "kc_dimension",
    "mapping_version",
    "mapping_source",
]
KC_TOPIC_MAP_COLUMNS = ["kc_id", "topic", "mapping_version"]
KC_ACTIVITY_MAP_COLUMNS = [
    "kc_id",
    "course_module_id",
    "activity_type",
    "activity_name",
    "topic",
    "role",
    "question_count",
    "mapping_source",
    "mapping_version",
]
ROLE_LEARN = "learn"
ROLE_PRACTICE = "practice"
LEARN_MAPPING_SOURCE = "course_module_idnumber"
COMPETENCY_MAPPING_SOURCE = "course_module_competency"
MAPPING_REPORT_NAME = "kc_mapping_report.json"
PRACTICE_MAPPING_SOURCE = "quiz_question_usage"
MAPPING_SOURCE = "moodle_question_category"
DEFAULT_DIMENSION = "general"
CONTEXT_COURSE = 50
CONTEXT_MODULE = 70

_ENTRY_COLUMNS = """
            qbe.id,
            qc.idnumber,
            qc.name,
            qc.info,
            parent.name,
            parent.parent
"""


def _entry_joins():
    return f"""
        FROM {t('question_bank_entries')} qbe
        JOIN {t('question_categories')} qc ON qc.id = qbe.questioncategoryid
        LEFT JOIN {t('question_categories')} parent ON parent.id = qc.parent
    """


def _plain_text(value):
    """Category ``info`` is stored as HTML; keep a single readable line."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value))).strip()


def _read_course_bank_entries(cursor, course_id):
    """Question-bank entries whose category lives in this course.

    Moodle 5 keeps question banks in module contexts (level 70); earlier
    versions used the course context (level 50). Both are accepted.
    """
    cursor.execute(
        f"""
        SELECT {_ENTRY_COLUMNS}
        {_entry_joins()}
        JOIN {t('context')} ctx ON ctx.id = qc.contextid
        LEFT JOIN {t('course_modules')} cm
          ON ctx.contextlevel = {CONTEXT_MODULE} AND cm.id = ctx.instanceid
        WHERE (ctx.contextlevel = {CONTEXT_MODULE} AND cm.course = ?)
           OR (ctx.contextlevel = {CONTEXT_COURSE} AND ctx.instanceid = ?)
        ORDER BY qbe.id
        """,
        (course_id, course_id),
    )
    return cursor.fetchall()


def _read_entries_by_id(cursor, entry_ids):
    """Entries used by course quizzes but stored in a bank outside the course."""
    if not entry_ids:
        return []
    placeholders = ", ".join("?" for _ in entry_ids)
    cursor.execute(
        f"""
        SELECT {_ENTRY_COLUMNS}
        {_entry_joins()}
        WHERE qbe.id IN ({placeholders})
        ORDER BY qbe.id
        """,
        tuple(entry_ids),
    )
    return cursor.fetchall()


def _read_quiz_question_usage(cursor, course_id):
    """Distinct (quiz_id, question_bank_entry_id) pairs for the course.

    Fixed quiz slots give the designed usage; finished attempts add questions
    that were drawn through random slots.
    """
    cursor.execute(
        f"""
        SELECT qs.quizid, qr.questionbankentryid
        FROM {t('quiz_slots')} qs
        JOIN {t('quiz')} q ON q.id = qs.quizid
        JOIN {t('question_references')} qr
          ON qr.component = 'mod_quiz'
         AND qr.questionarea = 'slot'
         AND qr.itemid = qs.id
        WHERE q.course = ?
        UNION
        SELECT qa.quiz, qv.questionbankentryid
        FROM {t('quiz_attempts')} qa
        JOIN {t('quiz')} q ON q.id = qa.quiz
        JOIN {t('question_attempts')} qat ON qat.questionusageid = qa.uniqueid
        JOIN {t('question_versions')} qv ON qv.questionid = qat.questionid
        WHERE q.course = ? AND qa.preview = 0
        """,
        (course_id, course_id),
    )
    return [(int(quiz_id), int(entry_id)) for quiz_id, entry_id in cursor.fetchall()]


def _read_module_competencies(cursor, course_id):
    """``{course_module_id: [competency idnumber, ...]}`` for the course's modules.

    Competencies without an ID number cannot name a KC and are skipped.
    """
    cursor.execute(
        f"""
        SELECT mc.cmid, c.idnumber
        FROM {t('competency_modulecomp')} mc
        JOIN {t('competency')} c ON c.id = mc.competencyid
        JOIN {t('course_modules')} cm ON cm.id = mc.cmid
        WHERE cm.course = ?
        ORDER BY mc.cmid, mc.sortorder, mc.id
        """,
        (course_id,),
    )
    links = {}
    for cmid, idnumber in cursor.fetchall():
        kc_id = (idnumber or "").strip()
        if kc_id:
            links.setdefault(int(cmid), []).append(kc_id)
    return links


def _mapping_version(pairs):
    """Short content hash: changes exactly when the question->KC mapping does."""
    digest = hashlib.sha256()
    for entry_id, kc_id, dimension in sorted(pairs):
        digest.update(f"{entry_id},{kc_id},{dimension}\n".encode("utf-8"))
    return f"qcat-{digest.hexdigest()[:12]}"


def build_course_mappings(entry_rows):
    """Turn question-bank rows into ``(kc_catalog, question_kc_map)`` row lists.

    Raises ValueError when one kc_id is declared by categories that disagree on
    its name or dimension: KC identity must be unambiguous.
    """
    catalog = {}
    question_kcs = {}
    for entry_id, idnumber, name, info, parent_name, parent_parent in entry_rows:
        kc_id = (idnumber or "").strip()
        if not kc_id:
            continue
        # The bank's root category ("top") has parent 0 and is not a dimension.
        is_root_parent = parent_name is None or not parent_parent
        dimension = (
            DEFAULT_DIMENSION if is_root_parent else str(parent_name).strip().lower()
        ) or DEFAULT_DIMENSION
        entry = {
            "kc_id": kc_id,
            "kc_name": (name or "").strip() or kc_id,
            "kc_dimension": dimension,
            "learning_objective": _plain_text(info),
        }
        known = catalog.setdefault(kc_id, entry)
        if (known["kc_name"], known["kc_dimension"]) != (
            entry["kc_name"],
            entry["kc_dimension"],
        ):
            raise ValueError(
                f"KC {kc_id!r} is declared by question categories that disagree: "
                f"{known['kc_name']!r}/{known['kc_dimension']!r} vs "
                f"{entry['kc_name']!r}/{entry['kc_dimension']!r}"
            )
        if not known["learning_objective"]:
            known["learning_objective"] = entry["learning_objective"]
        question_kcs[str(entry_id)] = (kc_id, dimension)

    version = _mapping_version(
        (entry_id, kc_id, dimension)
        for entry_id, (kc_id, dimension) in question_kcs.items()
    )
    question_map = [
        {
            "question_bank_entry_id": entry_id,
            "kc_id": kc_id,
            "kc_dimension": dimension,
            "mapping_version": version,
            "mapping_source": MAPPING_SOURCE,
        }
        for entry_id, (kc_id, dimension) in sorted(
            question_kcs.items(), key=lambda item: (len(item[0]), item[0])
        )
    ]
    kc_catalog = [catalog[kc_id] for kc_id in sorted(catalog)]
    return kc_catalog, question_map


def build_kc_topic_map(question_map, usage, catalog_rows):
    """KC -> topic rows from the quizzes that use each KC's questions."""
    quiz_topic = {
        int(row["activity_id"]): row["topic"]
        for row in catalog_rows
        if row.get("activity_type") == "quiz" and row.get("topic")
    }
    entry_kc = {row["question_bank_entry_id"]: row["kc_id"] for row in question_map}
    version = question_map[0]["mapping_version"] if question_map else ""
    pairs = set()
    for quiz_id, entry_id in usage:
        kc_id = entry_kc.get(str(entry_id))
        topic = quiz_topic.get(quiz_id)
        if kc_id and topic:
            pairs.add((kc_id, topic))
    return [
        {"kc_id": kc_id, "topic": topic, "mapping_version": version}
        for kc_id, topic in sorted(pairs)
    ]


def build_kc_activity_map(kc_catalog, question_map, usage, catalog_rows, module_kcs=None):
    """KC -> activity rows: learning resources and the quizzes that practise it.

    ``module_kcs`` is ``{course_module_id: [kc_id, ...]}`` from the module
    competencies. When it is non-empty, learning resources come from it
    (``mapping_source=course_module_competency``) and course-module ID numbers
    are ignored; otherwise the ID-number convention applies.

    Returns ``(rows, unknown)``. ``unknown`` lists non-quiz ID numbers (or
    competency ID numbers on non-quiz modules) that match no catalog KC, so a
    typo is reported instead of silently ignored. Dimension competencies
    (``DIM-...``) are parents, not KCs, and are not reported.
    """
    known = {row["kc_id"] for row in kc_catalog}
    version = question_map[0]["mapping_version"] if question_map else ""
    module_kcs = module_kcs or {}
    rows, unknown = [], []

    def learn_row(kc_id, activity, source):
        return {
            "kc_id": kc_id,
            "course_module_id": activity["course_module_id"],
            "activity_type": activity["activity_type"],
            "activity_name": activity.get("activity_name", ""),
            "topic": activity.get("topic") or "",
            "role": ROLE_LEARN,
            "question_count": "",
            "mapping_source": source,
            "mapping_version": version,
        }

    for activity in catalog_rows:
        if activity.get("activity_type") == "quiz":
            continue
        if module_kcs:
            try:
                linked = module_kcs.get(int(activity.get("course_module_id")), [])
            except (TypeError, ValueError):
                linked = []
            for kc_id in linked:
                if kc_id in known:
                    rows.append(learn_row(kc_id, activity, COMPETENCY_MAPPING_SOURCE))
                elif not kc_id.startswith("DIM-"):
                    unknown.append(kc_id)
            continue
        idnumber = (activity.get("idnumber") or "").strip()
        if not idnumber:
            continue
        if idnumber not in known:
            unknown.append(idnumber)
            continue
        rows.append(learn_row(idnumber, activity, LEARN_MAPPING_SOURCE))

    quiz_cm = {
        int(row["activity_id"]): row
        for row in catalog_rows
        if row.get("activity_type") == "quiz"
    }
    entry_kc = {row["question_bank_entry_id"]: row["kc_id"] for row in question_map}
    questions = {}
    for quiz_id, entry_id in sorted(set(usage)):
        kc_id = entry_kc.get(str(entry_id))
        if kc_id and quiz_id in quiz_cm:
            questions[(kc_id, quiz_id)] = questions.get((kc_id, quiz_id), 0) + 1
    for (kc_id, quiz_id), count in questions.items():
        activity = quiz_cm[quiz_id]
        rows.append(
            {
                "kc_id": kc_id,
                "course_module_id": activity.get("course_module_id", ""),
                "activity_type": "quiz",
                "activity_name": activity.get("activity_name", ""),
                "topic": activity.get("topic") or "",
                "role": ROLE_PRACTICE,
                "question_count": count,
                "mapping_source": PRACTICE_MAPPING_SOURCE,
                "mapping_version": version,
            }
        )
    rows.sort(key=lambda row: (row["kc_id"], row["role"], str(row["course_module_id"])))
    return rows, sorted(set(unknown))


def kc_of_module(kc_activity_map):
    """``{course_module_id: kc_id}`` for modules that concern exactly one KC.

    Used to put a KC on each logstore event. Modules with several KCs (a
    mixed quiz) or none are left out: their events get no KC.
    """
    kcs = {}
    for row in kc_activity_map:
        try:
            cmid = int(row["course_module_id"])
        except (TypeError, ValueError):
            continue
        kcs.setdefault(cmid, set()).add(row["kc_id"])
    return {cmid: next(iter(ids)) for cmid, ids in kcs.items() if len(ids) == 1}


def build_mapping_report(kc_catalog, kc_activity_map, catalog_rows, module_kcs):
    """Granularity and consistency checks of the KC <-> activity links."""
    known = {row["kc_id"] for row in kc_catalog}
    names = {
        int(row["course_module_id"]): row.get("activity_name", "")
        for row in catalog_rows
        if str(row.get("course_module_id", "")).strip().isdigit()
    }
    quiz_kcs, lesson_kcs = {}, set()
    for row in kc_activity_map:
        if row["role"] == ROLE_PRACTICE:
            if str(row["course_module_id"]).strip().isdigit():
                quiz_kcs.setdefault(int(row["course_module_id"]), set()).add(row["kc_id"])
        elif row["role"] == ROLE_LEARN:
            lesson_kcs.add(row["kc_id"])
    practised = set().union(*quiz_kcs.values()) if quiz_kcs else set()
    sizes = [len(kcs) for kcs in quiz_kcs.values()]
    mismatches = []
    for cmid, kcs in sorted(quiz_kcs.items()):
        if cmid not in module_kcs:
            continue
        linked = {kc for kc in module_kcs[cmid] if kc in known}
        if linked != kcs:
            mismatches.append(
                {
                    "course_module_id": cmid,
                    "activity_name": names.get(cmid, ""),
                    "competency_kcs": sorted(linked),
                    "question_kcs": sorted(kcs),
                }
            )
    linked_kcs = {kc for kcs in module_kcs.values() for kc in kcs}
    return {
        "activity_kc_source": (
            COMPETENCY_MAPPING_SOURCE if module_kcs else LEARN_MAPPING_SOURCE
        ),
        "kcs": len(known),
        "module_competency_links": sum(len(kcs) for kcs in module_kcs.values()),
        "quizzes": len(quiz_kcs),
        "single_kc_quizzes": sum(size == 1 for size in sizes),
        "max_kcs_per_quiz": max(sizes, default=0),
        "multi_kc_quizzes": [
            {
                "course_module_id": cmid,
                "activity_name": names.get(cmid, ""),
                "kc_ids": sorted(kcs),
            }
            for cmid, kcs in sorted(quiz_kcs.items())
            if len(kcs) > 1
        ],
        "quizzes_without_competency": (
            sorted(cmid for cmid in quiz_kcs if cmid not in module_kcs) if module_kcs else []
        ),
        "quiz_competency_mismatches": mismatches,
        "competencies_without_questions": sorted(
            kc for kc in linked_kcs if kc not in known and not kc.startswith("DIM-")
        ),
        "kcs_without_lesson": sorted(known - lesson_kcs),
        "kcs_without_quiz": sorted(known - practised),
    }


def _catalog_from_override(mapping_file):
    """A hand-curated crosswalk names KCs only by id; reuse it as the catalog."""
    # Imported lazily: collect_question_answers imports this module's constants.
    from .collect_question_answers import load_question_kc_mapping

    mapping = load_question_kc_mapping(mapping_file)
    if mapping is None:
        return [], []
    question_map = [
        {"question_bank_entry_id": entry_id, **link}
        for entry_id, link in mapping.items()
    ]
    catalog = {}
    for link in mapping.values():
        catalog.setdefault(
            link["kc_id"],
            {
                "kc_id": link["kc_id"],
                "kc_name": link["kc_id"],
                "kc_dimension": link["kc_dimension"],
                "learning_objective": "",
            },
        )
    return [catalog[kc_id] for kc_id in sorted(catalog)], question_map


def collect_course_mappings(
    cursor,
    course_id,
    catalog_rows,
    mapping_file=None,
    use_override=QUESTION_KC_MAPPING_IS_OVERRIDE,
):
    """Write the KC catalog, the maps and the mapping report.

    Returns ``(kc_catalog, question_map, kc_topic_map, kc_activity_map)``.
    With ``use_override`` (``MOODLE_QUESTION_KC_MAPPING`` set), the curated
    crosswalk file is the source of truth and is never rewritten.
    """
    mapping_file = mapping_file if mapping_file is not None else question_kc_mapping_file()
    usage = _read_quiz_question_usage(cursor, course_id)
    if use_override:
        kc_catalog, question_map = _catalog_from_override(mapping_file)
        print(f"Question-to-KC mapping: curated override {mapping_file}")
    else:
        entry_rows = list(_read_course_bank_entries(cursor, course_id))
        known = {int(row[0]) for row in entry_rows}
        outside = sorted({entry_id for _, entry_id in usage} - known)
        entry_rows.extend(_read_entries_by_id(cursor, outside))
        kc_catalog, question_map = build_course_mappings(entry_rows)
        unmapped = len({int(row[0]) for row in entry_rows}) - len(question_map)
        write_csv(Path(mapping_file).name, question_map, QUESTION_KC_MAP_COLUMNS)
        if unmapped:
            print(
                f"WARNING: {unmapped:,} question-bank entries sit in categories "
                "without an idnumber and are not mapped to a KC."
            )
    kc_topic_map = build_kc_topic_map(question_map, usage, catalog_rows)
    module_kcs = _read_module_competencies(cursor, course_id)
    kc_activity_map, unknown_idnumbers = build_kc_activity_map(
        kc_catalog, question_map, usage, catalog_rows, module_kcs
    )
    report = build_mapping_report(kc_catalog, kc_activity_map, catalog_rows, module_kcs)
    write_csv("kc_catalog.csv", kc_catalog, KC_CATALOG_COLUMNS)
    write_csv("kc_topic_map.csv", kc_topic_map, KC_TOPIC_MAP_COLUMNS)
    write_csv("kc_activity_map.csv", kc_activity_map, KC_ACTIVITY_MAP_COLUMNS)
    write_json(MAPPING_REPORT_NAME, report)
    if unknown_idnumbers:
        print(
            "WARNING: activities have ID numbers or competencies that match no KC: "
            f"{unknown_idnumbers[:10]}"
        )
    print(
        f"Course mappings: {len(kc_catalog):,} KCs, {len(question_map):,} mapped "
        f"questions, {len(kc_topic_map):,} KC-topic links, "
        f"{sum(row['role'] == ROLE_LEARN for row in kc_activity_map):,} KC learning resources "
        f"(from {report['activity_kc_source']})."
    )
    print(
        f"  Granularity: {report['single_kc_quizzes']} of {report['quizzes']} quizzes test "
        f"one KC (max {report['max_kcs_per_quiz']} KCs per quiz)."
    )
    if report["quiz_competency_mismatches"]:
        print(
            f"WARNING: {len(report['quiz_competency_mismatches'])} quizzes are linked to "
            "competencies other than the KCs of their questions; see "
            f"{MAPPING_REPORT_NAME}."
        )
    if report["quizzes_without_competency"]:
        print(
            f"WARNING: {len(report['quizzes_without_competency'])} quizzes have no "
            "competency although the course uses module competencies."
        )
    return kc_catalog, question_map, kc_topic_map, kc_activity_map
