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
                                lesson or quiz, quizzes per role, pool
                                difficulty coverage)
    question_metadata.csv       question_bank_entry_id -> authored difficulty,
                                item set (check/pool) and CEFR level, read
                                from the question tags ``ml-difficulty-N``,
                                ``ml-check``/``ml-pool`` and ``ml-cefr-xx``
                                (empty fields for untagged questions)

A KC's learning resource (page, URL, book, ...) is found in Moodle, in this
order:

  1. Course-module competencies (``competency_modulecomp``): a non-quiz module
     linked to the competency whose ID number is the kc_id. This is the
     Moodle-native way and the convention of the one-KC-per-activity course
     (ENG-MICRO-KC).
  2. Only when the course has no module competencies at all: the module whose
     course-module ID number equals the kc_id (course ENG-MICRO, id 10).

A quiz's KCs always come from the questions it uses (what it measures): its
fixed slots, its random slots (each random slot draws from one KC category),
and the questions drawn in attempts. A quiz's own competency links are only
checked against them. A quiz's role (check, practice, checkin, review,
challenge) is its course-module ID number prefix (``collect_activities``). Questions keep
their KC from the question category, because Moodle has no per-question
competency. This is how a recommender finds what to show for a weak KC
without any hand-maintained mapping.

Nothing here reads any benchmark dataset: KC identity belongs to the course.
"""

import hashlib
import json
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
RANDOM_SLOT_MAPPING_SOURCE = "quiz_random_slot"
QUESTION_METADATA_NAME = "question_metadata.csv"
QUESTION_METADATA_COLUMNS = [
    "question_bank_entry_id",
    "kc_id",
    "difficulty",
    "item_set",
    "cefr_level",
    "tags",
]
# Question tags of the authoring convention (moodle_setup.content); Moodle
# stores tag names in lower case.
TAG_PREFIX = "ml-"
DIFFICULTY_TAG = re.compile(r"^ml-difficulty-([1-9])$")
CEFR_TAG = re.compile(r"^ml-cefr-([a-c][12])$")
ITEM_SET_TAGS = {"ml-check": "check", "ml-pool": "pool"}
HARDEST_DIFFICULTY = "3"
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


def _filter_category_id(filtercondition):
    """Question category id of a random slot's filter, or None.

    Moodle 4.3+ stores ``{"filter": {"category": {"values": [id], ...}, ...}}``;
    older versions ``{"questioncategoryid": id, ...}``.
    """
    try:
        condition = json.loads(filtercondition or "{}")
    except (TypeError, ValueError):
        return None
    values = (((condition.get("filter") or {}).get("category") or {}).get("values")) or []
    candidate = values[0] if values else condition.get("questioncategoryid")
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def _read_random_slot_kcs(cursor, course_id):
    """``[(quiz_id, kc_id), ...]``: one pair per random slot of the course's quizzes.

    A random slot draws from one question category; the category's ID number
    is its KC. Read from the slot design, so a random quiz maps to its KCs
    before anyone has attempted it. Slots whose category has no ID number
    (or that include subcategories of a non-KC category) are skipped.
    """
    cursor.execute(
        f"""
        SELECT qs.quizid, qsr.filtercondition
        FROM {t('question_set_references')} qsr
        JOIN {t('quiz_slots')} qs ON qs.id = qsr.itemid
        JOIN {t('quiz')} q ON q.id = qs.quizid
        WHERE qsr.component = 'mod_quiz' AND qsr.questionarea = 'slot'
          AND q.course = ?
        ORDER BY qs.quizid, qs.slot
        """,
        (course_id,),
    )
    slots = [
        (int(quiz_id), _filter_category_id(condition))
        for quiz_id, condition in cursor.fetchall()
    ]
    category_ids = sorted({category for _, category in slots if category is not None})
    if not category_ids:
        return []
    placeholders = ", ".join("?" for _ in category_ids)
    cursor.execute(
        f"""
        SELECT qc.id, qc.idnumber
        FROM {t('question_categories')} qc
        WHERE qc.id IN ({placeholders})
        """,
        tuple(category_ids),
    )
    kc_of_category = {
        int(category_id): (idnumber or "").strip()
        for category_id, idnumber in cursor.fetchall()
    }
    return [
        (quiz_id, kc_of_category[category])
        for quiz_id, category in slots
        if kc_of_category.get(category)
    ]


def _read_question_tags(cursor, entry_ids):
    """``{question_bank_entry_id: [tag name, ...]}`` of each entry's latest version."""
    if not entry_ids:
        return {}
    placeholders = ", ".join("?" for _ in entry_ids)
    cursor.execute(
        f"""
        SELECT qv.questionbankentryid, qv.version, tg.name
        FROM {t('question_versions')} qv
        JOIN {t('tag_instance')} ti
          ON ti.component = 'core_question' AND ti.itemtype = 'question'
         AND ti.itemid = qv.questionid
        JOIN {t('tag')} tg ON tg.id = ti.tagid
        WHERE qv.questionbankentryid IN ({placeholders})
        """,
        tuple(int(entry_id) for entry_id in entry_ids),
    )
    latest, tags = {}, {}
    for entry_id, version, name in cursor.fetchall():
        entry_id, version = int(entry_id), int(version or 0)
        if version > latest.get(entry_id, -1):
            latest[entry_id], tags[entry_id] = version, []
        if version == latest[entry_id]:
            tags[entry_id].append(str(name).strip().lower())
    return tags


def build_question_metadata(question_map, tags_by_entry):
    """Rows of ``question_metadata.csv`` for every mapped question.

    Difficulty, item set and CEFR level are authored labels carried by
    question tags; an untagged question gets empty fields. The KC's CEFR
    level is a property of the KC, so it is read from each of its questions.
    """
    rows = []
    for link in question_map:
        entry_id = str(link["question_bank_entry_id"])
        tags = sorted(set(tags_by_entry.get(int(entry_id), [])))
        difficulty = item_set = cefr = ""
        for tag in tags:
            if DIFFICULTY_TAG.match(tag):
                difficulty = DIFFICULTY_TAG.match(tag).group(1)
            elif CEFR_TAG.match(tag):
                cefr = CEFR_TAG.match(tag).group(1).upper()
            elif tag in ITEM_SET_TAGS:
                item_set = ITEM_SET_TAGS[tag]
        rows.append(
            {
                "question_bank_entry_id": entry_id,
                "kc_id": link["kc_id"],
                "difficulty": difficulty,
                "item_set": item_set,
                "cefr_level": cefr,
                "tags": ";".join(tag for tag in tags if tag.startswith(TAG_PREFIX)),
            }
        )
    return rows


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


def build_kc_activity_map(
    kc_catalog, question_map, usage, catalog_rows, module_kcs=None, random_slots=None
):
    """KC -> activity rows: learning resources and the quizzes that practise it.

    ``random_slots`` is ``[(quiz_id, kc_id), ...]``, one pair per random slot.
    For a (KC, quiz) pair with random slots, ``question_count`` is the number
    of those slots (what one attempt asks), not the number of different
    questions drawn so far; otherwise it is the number of the KC's questions
    the quiz uses. A KC's questions in one quiz are either fixed or random in
    the courses built here.

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
    slots = {}
    for quiz_id, kc_id in random_slots or []:
        if kc_id in known and quiz_id in quiz_cm:
            slots[(kc_id, quiz_id)] = slots.get((kc_id, quiz_id), 0) + 1
    questions.update(slots)
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
                "mapping_source": (
                    RANDOM_SLOT_MAPPING_SOURCE
                    if (kc_id, quiz_id) in slots
                    else PRACTICE_MAPPING_SOURCE
                ),
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


def _roles_report(known, quiz_kcs, catalog_rows, question_metadata):
    """Checks of the quiz-role convention and of the practice pool's difficulty."""
    role_of = {
        int(row["course_module_id"]): row.get("quiz_role") or "check"
        for row in catalog_rows
        if row.get("activity_type") == "quiz"
        and str(row.get("course_module_id", "")).strip().isdigit()
    }
    by_role = {}
    for cmid, role in role_of.items():
        by_role[role] = by_role.get(role, 0) + 1
    checks = [cmid for cmid, role in role_of.items() if role == "check"]
    practised = {
        kc
        for cmid, kcs in quiz_kcs.items()
        if role_of.get(cmid) == "practice"
        for kc in kcs
    }
    pool = {}
    cefr = {}
    for row in question_metadata or []:
        if row.get("item_set") == "pool":
            per_kc = pool.setdefault(row["kc_id"], {})
            per_kc[row["difficulty"]] = per_kc.get(row["difficulty"], 0) + 1
        if row.get("cefr_level"):
            cefr.setdefault(row["kc_id"], set()).add(row["cefr_level"])
    pool_by_difficulty = {}
    for per_kc in pool.values():
        for level, count in per_kc.items():
            pool_by_difficulty[level or "untagged"] = (
                pool_by_difficulty.get(level or "untagged", 0) + count
            )
    has_roles = set(by_role) != {"check"} and bool(by_role)
    return {
        "quizzes_by_role": dict(sorted(by_role.items())),
        "check_quizzes_single_kc": sum(len(quiz_kcs.get(cmid, ())) == 1 for cmid in checks),
        "check_quizzes": len(checks),
        # Only meaningful once the course has practice quizzes.
        "kcs_without_practice": sorted(known - practised) if has_roles else [],
        "pool_items_by_difficulty": dict(sorted(pool_by_difficulty.items())),
        "kcs_missing_hard_pool_items": sorted(
            kc for kc, per_kc in pool.items() if not per_kc.get(HARDEST_DIFFICULTY)
        ) + (sorted(known - set(pool)) if pool else []),
        "cefr_disagreements": sorted(kc for kc, levels in cefr.items() if len(levels) > 1),
    }


def build_mapping_report(
    kc_catalog, kc_activity_map, catalog_rows, module_kcs, question_metadata=None
):
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
        **_roles_report(known, quiz_kcs, catalog_rows, question_metadata),
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
    random_slots = _read_random_slot_kcs(cursor, course_id)
    kc_activity_map, unknown_idnumbers = build_kc_activity_map(
        kc_catalog, question_map, usage, catalog_rows, module_kcs, random_slots
    )
    question_metadata = build_question_metadata(
        question_map,
        _read_question_tags(cursor, [row["question_bank_entry_id"] for row in question_map]),
    )
    report = build_mapping_report(
        kc_catalog, kc_activity_map, catalog_rows, module_kcs, question_metadata
    )
    write_csv("kc_catalog.csv", kc_catalog, KC_CATALOG_COLUMNS)
    write_csv(QUESTION_METADATA_NAME, question_metadata, QUESTION_METADATA_COLUMNS)
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
    print(
        f"  Quiz roles: {report['quizzes_by_role']}; {report['check_quizzes_single_kc']} of "
        f"{report['check_quizzes']} check quizzes test one KC. Tagged difficulty: "
        f"{sum(bool(row['difficulty']) for row in question_metadata):,} of "
        f"{len(question_metadata):,} mapped questions."
    )
    if report["kcs_without_practice"] or report["kcs_missing_hard_pool_items"]:
        print(
            f"WARNING: {len(report['kcs_without_practice'])} KCs have no practice quiz and "
            f"{len(report['kcs_missing_hard_pool_items'])} have no hard pool question; see "
            f"{MAPPING_REPORT_NAME}."
        )
    if report["cefr_disagreements"]:
        print(
            f"WARNING: questions of {len(report['cefr_disagreements'])} KCs carry different "
            "CEFR tags."
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
