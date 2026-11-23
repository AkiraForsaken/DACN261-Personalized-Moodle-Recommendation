"""Load and validate the English microlearning course content.

Source of truth (edit these, not Moodle):

    course/kc_catalog.csv        kc_id, unit, unit_name, dimension, kc_name,
                                 learning_objective, cefr_level (A1-B2)
    course/questions/unitN.txt   the check questions, one per line:
                                 kc_id | difficulty | stem | correct | wrong | wrong | wrong
    course/questions/pool/unitN.txt
                                 the practice pool (layout ``kc`` only), same format
    course/lessons/unitN.md      one short lesson per KC (format below)

``difficulty`` is the author's rating 1 (easy), 2 (medium) or 3 (hard). It
and the CEFR level are authored labels, not calibrated on learner data.

The correct option is always written first; Moodle shuffles the options for
every attempt. Lines starting with ``#`` and blank lines are ignored.

Lesson format (one block per KC; text before the first ``## `` is ignored)::

    ## KC001
    video: https://...           optional; leave empty until a video exists
    ### Explanation
    One or more paragraphs.
    ### Examples
    - one example per bullet
    ### Common mistake
    - one mistake per bullet
    ### Try it
    - A self-check question => its answer

Inline ``**bold**`` and ``~~wrong form~~`` (struck through) are supported. Lessons must not reuse quiz questions, so
reading a lesson never shows a quiz answer.

``build_spec`` turns the content into the JSON the Moodle build script reads:
one question category per KC (ID number = kc_id) under a category per
dimension and one lesson page per KC, plus, depending on the layout:

* ``kc`` (default, course ENG-MICRO-KC): the microlearning layout, modelled on
  EdNet/Santa (diagnose, practise with feedback, check, review). Each unit
  runs: overview, a check-in (one pool question per KC of the unit, before
  teaching), then per KC its lesson, a short practice quiz (random pool
  questions, no pass mark) and its 4-5 question check quiz (fixed questions,
  pass mark), and from unit 2 a mixed review quiz on earlier units. A final
  section holds one challenge quiz per KC (hard pool questions only). Every
  quiz's role is its course-module ID number (``QUIZ_ROLES``), the third
  authoring convention next to the KC category and the KC competency. The KC
  of an activity is attached in Moodle as a course-module competency
  (framework ``KC_FRAMEWORK_IDNUMBER``, competency ID number = kc_id).
  Questions keep their KC question category: Moodle has no per-question
  competency. Check and pool questions share that category and are told apart
  by question tags, which also carry difficulty and CEFR level.
* ``dimension`` (course ENG-MICRO, id 10): one practice quiz per unit and
  dimension mixing ~3 KCs; the lesson's KC is its course-module ID number.
"""

import csv
import html
import re
from pathlib import Path

COURSE_DIR = Path(__file__).resolve().parent / "course"
COURSE_FULLNAME = "English Microlearning"
COURSE_SHORTNAME = "ENG-MICRO"
LAYOUT_DIMENSION = "dimension"  # one quiz per unit and dimension (course 10)
LAYOUT_KC = "kc"  # one lesson + one quiz per KC
LAYOUTS = (LAYOUT_KC, LAYOUT_DIMENSION)
DEFAULT_LAYOUT = LAYOUT_KC
COURSES = {
    LAYOUT_DIMENSION: (COURSE_FULLNAME, COURSE_SHORTNAME),
    LAYOUT_KC: ("English Microlearning (one KC per activity)", "ENG-MICRO-KC"),
}
KC_COURSE_SHORTNAME = COURSES[LAYOUT_KC][1]
# Competency framework holding one competency per KC (layout ``kc``).
KC_FRAMEWORK_IDNUMBER = "ENG-MICRO-KCS"
MIN_QUESTIONS_PER_KC = 4
# A one-KC check quiz stays short: 3-5 items (only enforced for layout ``kc``).
MAX_QUESTIONS_PER_KC = 5
OPTIONS_PER_QUESTION = 4
DIMENSION_ORDER = ["Grammar", "Vocabulary", "Reading", "Writing"]
CATALOG_COLUMNS = [
    "kc_id",
    "unit",
    "unit_name",
    "dimension",
    "kc_name",
    "learning_objective",
    "cefr_level",
]
LESSON_SECTIONS = ["Explanation", "Examples", "Common mistake", "Try it"]
MIN_TRY_IT_ITEMS = 2
# A check quiz counts as complete only with at least this share of the marks.
PASS_FRACTION = 0.7

# --- layout ``kc``: difficulty, pool and quiz roles -----------------------------
# Authored labels, not calibrated on learner data.
DIFFICULTY_LEVELS = (1, 2, 3)
CEFR_LEVELS = ("A1", "A2", "B1", "B2")
# Practice pool per KC: at least one item of every difficulty, and enough hard
# items for a challenge quiz.
POOL_MIN_PER_KC = 4
PRACTICE_SLOTS = 2
CHALLENGE_SLOTS = 2
# A review quiz asks this many earlier-unit KCs per dimension (units 2+).
REVIEW_KCS_PER_DIMENSION = 2
# Question tags (Moodle lower-cases tag names).
TAG_PREFIX = "ml-"
TAG_CHECK = TAG_PREFIX + "check"
TAG_POOL = TAG_PREFIX + "pool"
CHECK_SET, POOL_SET = "check", "pool"


def difficulty_tag(level):
    return f"{TAG_PREFIX}difficulty-{level}"


def cefr_tag(level):
    return f"{TAG_PREFIX}cefr-{level.lower()}"


# Quiz role = course-module ID number prefix (``CHECK-KC001``, ``CHECKIN-U3``).
# A quiz without a known prefix is a check quiz, so courses built before this
# convention (course 10) read exactly as before.
ROLE_CHECK, ROLE_PRACTICE, ROLE_CHECKIN = "check", "practice", "checkin"
ROLE_REVIEW, ROLE_CHALLENGE = "review", "challenge"
QUIZ_ROLES = {
    "CHECK": ROLE_CHECK,
    "PRACTICE": ROLE_PRACTICE,
    "CHECKIN": ROLE_CHECKIN,
    "REVIEW": ROLE_REVIEW,
    "CHALLENGE": ROLE_CHALLENGE,
}
ROLE_PREFIX = {role: prefix for prefix, role in QUIZ_ROLES.items()}
CHALLENGE_SECTION_NAME = "Challenge practice"


def quiz_idnumber(role, suffix):
    return f"{ROLE_PREFIX[role]}-{suffix}"


def load_catalog(course_dir=COURSE_DIR):
    with (Path(course_dir) / "kc_catalog.csv").open(
        newline="", encoding="utf-8-sig"
    ) as source:
        reader = csv.DictReader(source)
        missing = set(CATALOG_COLUMNS).difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"kc_catalog.csv is missing columns: {sorted(missing)}")
        return [{key: row[key].strip() for key in CATALOG_COLUMNS} for row in reader]


def _read_question_files(folder, label):
    questions = []
    for path in sorted(Path(folder).glob("unit*.txt")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 3 + OPTIONS_PER_QUESTION:
                raise ValueError(
                    f"{label}{path.name}:{number}: expected kc_id | difficulty | stem | "
                    f"correct | {OPTIONS_PER_QUESTION - 1} wrong options, got "
                    f"{len(parts)} fields"
                )
            questions.append(
                {
                    "kc_id": parts[0],
                    "difficulty": parts[1],  # checked by ``validate``
                    "stem": parts[2],
                    "options": parts[3:],  # options[0] is the correct one
                    "source": f"{label}{path.name}:{number}",
                }
            )
    return questions


def load_questions(course_dir=COURSE_DIR):
    """The check questions (``questions/unit*.txt``)."""
    return _read_question_files(Path(course_dir) / "questions", "")


def load_pool(course_dir=COURSE_DIR):
    """The practice pool (``questions/pool/unit*.txt``); empty if absent."""
    return _read_question_files(Path(course_dir) / "questions" / "pool", "pool/")


def load_lessons(course_dir=COURSE_DIR):
    """Parse ``lessons/unit*.md`` into ``[{kc_id, video, sections, source}]``.

    ``sections`` maps each ``### `` heading to its lines (blank lines kept as
    paragraph breaks). Structure is checked by ``validate``.
    """
    lessons, current, section = [], None, None
    for path in sorted((Path(course_dir) / "lessons").glob("unit*.md")):
        current = section = None
        for number, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw.rstrip()
            if line.startswith("## "):
                current = {
                    "kc_id": line[3:].strip(),
                    "video": "",
                    "sections": {},
                    "source": f"{path.name}:{number}",
                }
                lessons.append(current)
                section = None
            elif current is None:
                continue  # file preamble
            elif line.startswith("### "):
                section = line[4:].strip()
                if section in current["sections"]:
                    raise ValueError(
                        f"{path.name}:{number}: repeated section {section!r}"
                    )
                current["sections"][section] = []
            elif section is None and line.lower().startswith("video:"):
                current["video"] = line.split(":", 1)[1].strip()
            elif section is not None:
                current["sections"][section].append(line)
            elif line.strip():
                raise ValueError(f"{path.name}:{number}: text outside a ### section")
    return lessons


def _bullets(lines):
    return [line[2:].strip() for line in lines if line.startswith("- ")]


def _inline(text):
    """Escape HTML, then turn ``**bold**`` into <strong> and ``~~wrong~~`` into <del>."""
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)


def _paragraphs(lines):
    blocks, block = [], []
    for line in [*lines, ""]:
        if line.strip():
            block.append(line.strip())
        elif block:
            blocks.append(" ".join(block))
            block = []
    return "".join(f"<p>{_inline(block)}</p>" for block in blocks)


def render_lesson(lesson, kc, layout=LAYOUT_DIMENSION):
    """HTML for one lesson page. Self-check answers stay hidden until opened.

    Only the closing pointer depends on the layout: the next activity is the
    unit's mixed practice quiz (``dimension``) or this KC's own practice and
    check quizzes (``kc``).
    """
    sections = lesson["sections"]
    parts = [f"<p><em>Learning goal:</em> {_inline(kc['learning_objective'])}</p>"]
    video = lesson["video"]
    if video:
        url = html.escape(video)
        if video.lower().split("?")[0].endswith((".mp4", ".webm")):
            parts.append(f'<video controls width="100%" src="{url}"></video>')
        parts.append(
            f'<p><a href="{url}">Watch the short video for this lesson</a></p>'
        )
    parts.append("<h3>Explanation</h3>" + _paragraphs(sections["Explanation"]))
    for heading in ("Examples", "Common mistake"):
        items = "".join(
            f"<li>{_inline(item)}</li>" for item in _bullets(sections[heading])
        )
        parts.append(f"<h3>{heading}</h3><ul>{items}</ul>")
    checks = []
    for item in _bullets(sections["Try it"]):
        question, answer = (part.strip() for part in item.split("=>", 1))
        checks.append(
            f"<li>{_inline(question)}<details><summary>Show answer</summary>"
            f"{_inline(answer)}</details></li>"
        )
    parts.append(f"<h3>Try it</h3><ol>{''.join(checks)}</ol>")
    if layout == LAYOUT_KC:
        parts.append(
            "<p>Next, try the short practice quiz on this point as often as you "
            "like, then take its check quiz.</p>"
        )
    else:
        parts.append(
            "<p>When you feel ready, practise this point in the unit's "
            f"{_inline(kc['dimension'])} practice quiz.</p>"
        )
    return "".join(parts)


def _lesson_problems(catalog, lessons):
    problems = []
    known = {kc["kc_id"] for kc in catalog}
    seen = {}
    for lesson in lessons:
        where, kc_id = lesson["source"], lesson["kc_id"]
        if kc_id not in known:
            problems.append(f"{where}: lesson for unknown kc_id {kc_id!r}")
            continue
        if kc_id in seen:
            problems.append(
                f"{where}: second lesson for {kc_id} (first at {seen[kc_id]})"
            )
            continue
        seen[kc_id] = where
        sections = lesson["sections"]
        missing = [name for name in LESSON_SECTIONS if name not in sections]
        extra = sorted(set(sections) - set(LESSON_SECTIONS))
        if missing:
            problems.append(f"{where}: {kc_id} lesson is missing sections {missing}")
            continue
        if extra:
            problems.append(f"{where}: {kc_id} lesson has unknown sections {extra}")
        if not any(line.strip() for line in sections["Explanation"]):
            problems.append(f"{where}: {kc_id} lesson has an empty explanation")
        for name in ("Examples", "Common mistake"):
            if not _bullets(sections[name]):
                problems.append(
                    f"{where}: {kc_id} lesson has no '- ' items under {name}"
                )
        checks = _bullets(sections["Try it"])
        if len(checks) < MIN_TRY_IT_ITEMS:
            problems.append(
                f"{where}: {kc_id} lesson needs {MIN_TRY_IT_ITEMS}+ Try it items"
            )
        if any("=>" not in item for item in checks):
            problems.append(
                f"{where}: {kc_id} Try it items must read 'question => answer'"
            )
        video = lesson["video"]
        if video and not video.startswith(("https://", "http://")):
            problems.append(f"{where}: {kc_id} video must be an http(s) URL")
    for kc_id in sorted(known - set(seen)):
        problems.append(f"{kc_id}: no lesson")
    return problems


def _question_problems(question, known):
    """Problems of one question on its own (KC, difficulty, stem, options)."""
    where = question["source"]
    if question["kc_id"] not in known:
        return [f"{where}: unknown kc_id {question['kc_id']!r}"]
    problems = []
    if question["difficulty"] not in {str(level) for level in DIFFICULTY_LEVELS}:
        problems.append(
            f"{where}: difficulty must be one of {list(DIFFICULTY_LEVELS)}, "
            f"got {question['difficulty']!r}"
        )
    if not question["stem"]:
        problems.append(f"{where}: empty stem")
    options = question["options"]
    if any(not option for option in options):
        problems.append(f"{where}: empty option")
    if len({option.lower() for option in options}) != len(options):
        problems.append(f"{where}: options are not distinct")
    return problems


def _pool_problems(catalog, questions, pool):
    """Layout ``kc``: the practice pool feeds practice, check-in, review and
    challenge quizzes through random slots, so it must never show a check
    question and must hold every difficulty for every KC."""
    problems = []
    known = {kc["kc_id"] for kc in catalog}

    def key(question):
        # Same identity as for check questions: generic stems ("Which sentence
        # is correct?") recur with different options.
        return (question["stem"].lower(), question["options"][0].lower())

    check_keys = {key(question): question["source"] for question in questions}
    seen = {}
    levels = {kc_id: [] for kc_id in known}
    for question in pool:
        own = _question_problems(question, known)
        problems.extend(own)
        if any("unknown kc_id" in problem for problem in own):
            continue
        where = question["source"]
        if key(question) in check_keys:
            problems.append(
                f"{where}: repeats the check question at {check_keys[key(question)]}"
            )
        if key(question) in seen:
            problems.append(f"{where}: duplicates the pool question at {seen[key(question)]}")
        seen[key(question)] = where
        levels[question["kc_id"]].append(question["difficulty"])
    for kc_id in sorted(known):
        found = levels[kc_id]
        if len(found) < POOL_MIN_PER_KC:
            problems.append(
                f"{kc_id}: {len(found)} pool question(s), need at least {POOL_MIN_PER_KC}"
            )
        missing = [level for level in DIFFICULTY_LEVELS if str(level) not in found]
        if missing:
            problems.append(f"{kc_id}: pool has no question of difficulty {missing}")
        hard = found.count(str(DIFFICULTY_LEVELS[-1]))
        if hard < CHALLENGE_SLOTS:
            problems.append(
                f"{kc_id}: {hard} hard pool question(s), a challenge quiz needs "
                f"{CHALLENGE_SLOTS}"
            )
    return problems


def validate(catalog, questions, lessons=None, layout=LAYOUT_DIMENSION, pool=None):
    """Return a list of problems; an empty list means the content is usable.

    ``lessons`` is checked when given (``build_spec`` always gives it). With
    ``layout="kc"`` a KC's questions form its whole check quiz, so a KC may
    have at most ``MAX_QUESTIONS_PER_KC`` of them, and ``pool`` (when given;
    ``build_spec`` gives it for that layout) is checked by ``_pool_problems``.
    """
    problems = []
    kc_ids = [kc["kc_id"] for kc in catalog]
    if len(set(kc_ids)) != len(kc_ids):
        problems.append("kc_catalog.csv contains duplicate kc_id values")
    for kc in catalog:
        if not all(kc.values()):
            problems.append(f"{kc['kc_id'] or '?'}: catalog row has an empty field")
        if kc["dimension"] not in DIMENSION_ORDER:
            problems.append(f"{kc['kc_id']}: unknown dimension {kc['dimension']!r}")
        if kc["cefr_level"] and kc["cefr_level"] not in CEFR_LEVELS:
            problems.append(
                f"{kc['kc_id']}: CEFR level must be one of {list(CEFR_LEVELS)}, "
                f"got {kc['cefr_level']!r}"
            )
    names = {}
    for kc in catalog:
        names.setdefault((kc["unit"], kc["unit_name"]), None)
    units = {}
    for unit, unit_name in names:
        units.setdefault(unit, set()).add(unit_name)
    for unit, unit_names in units.items():
        if len(unit_names) > 1:
            problems.append(f"{unit} has several names: {sorted(unit_names)}")

    known = set(kc_ids)
    per_kc = dict.fromkeys(kc_ids, 0)
    stems = {}
    for question in questions:
        where = question["source"]
        own = _question_problems(question, known)
        problems.extend(own)
        if any("unknown kc_id" in problem for problem in own):
            continue
        per_kc[question["kc_id"]] += 1
        key = (question["stem"].lower(), question["options"][0].lower())
        if key in stems:
            problems.append(f"{where}: duplicates the question at {stems[key]}")
        stems[key] = where
    for kc_id, count in per_kc.items():
        if count < MIN_QUESTIONS_PER_KC:
            problems.append(
                f"{kc_id}: {count} question(s), need at least {MIN_QUESTIONS_PER_KC}"
            )
        if layout == LAYOUT_KC and count > MAX_QUESTIONS_PER_KC:
            problems.append(
                f"{kc_id}: {count} questions, a one-KC quiz takes at most "
                f"{MAX_QUESTIONS_PER_KC}"
            )
    if layout == LAYOUT_KC and pool is not None:
        problems.extend(_pool_problems(catalog, questions, pool))
    if lessons is not None:
        problems.extend(_lesson_problems(catalog, lessons))
        stems = {question["stem"].lower() for question in [*questions, *(pool or [])]}
        for lesson in lessons:
            for item in _bullets(lesson["sections"].get("Try it", [])):
                if item.split("=>", 1)[0].strip().lower() in stems:
                    problems.append(
                        f"{lesson['source']}: Try it item repeats a quiz question"
                    )
    return problems


def _unit_number(unit):
    return int("".join(ch for ch in unit if ch.isdigit()))


def _review_kcs(catalog, unit):
    """KCs a unit's review quiz asks about (units 2+), one random pool
    question each.

    Per dimension, ``REVIEW_KCS_PER_DIMENSION`` KCs: one from the previous
    unit (recent material) and one from about halfway back (older material),
    rotating through each unit's KCs so different units review different KCs.
    Deterministic, so a rebuild asks about the same KCs.
    """
    by_unit_dim = {}
    for kc in catalog:
        by_unit_dim.setdefault((_unit_number(kc["unit"]), kc["dimension"]), []).append(
            kc["kc_id"]
        )
    picks = []
    sources = [unit - 1, (unit - 1 + 1) // 2]  # previous unit, ceil((unit-1)/2)
    for dimension in DIMENSION_ORDER:
        chosen = []
        for offset, source in enumerate(sources[:REVIEW_KCS_PER_DIMENSION]):
            candidates = by_unit_dim.get((source, dimension), [])
            for step in range(len(candidates)):
                kc_id = candidates[(unit + offset + step) % len(candidates)]
                if kc_id not in chosen:
                    chosen.append(kc_id)
                    break
        picks.extend(chosen)
    return picks


def _kc_layout(catalog, check_names):
    """Quizzes and section order of layout ``kc`` (see the module docstring).

    ``check_names`` maps kc_id to the names of its check questions, in order.
    """
    by_id = {kc["kc_id"]: kc for kc in catalog}
    units = sorted({(_unit_number(kc["unit"]), kc["unit_name"]) for kc in catalog})

    def random_slots(kc_ids, tags):
        return [{"kc_id": kc_id, "tags": list(tags)} for kc_id in kc_ids]

    def quiz(role, suffix, name, unit, kc_ids, **settings):
        return {
            "role": role,
            "idnumber": quiz_idnumber(role, suffix),
            "name": name,
            "unit": unit,
            "kc_ids": kc_ids,
            "questions": [],  # fixed questions (check quizzes)
            "slots": [],  # random questions: one per slot
            "pass_fraction": None,  # None: no pass mark, complete on any grade
            "max_attempts": 0,  # 0: unlimited
            **settings,
        }

    quizzes, sections = [], []
    for unit, unit_name in units:
        kc_ids = [kc["kc_id"] for kc in catalog if _unit_number(kc["unit"]) == unit]
        checkin = quiz(
            ROLE_CHECKIN, f"U{unit}", f"Unit {unit} check-in", unit, kc_ids,
            slots=random_slots(kc_ids, [TAG_POOL]), max_attempts=1,
        )
        quizzes.append(checkin)
        items = [{"type": "overview"}, {"type": "quiz", "idnumber": checkin["idnumber"]}]
        for kc_id in kc_ids:
            name = by_id[kc_id]["kc_name"]
            practice = quiz(
                ROLE_PRACTICE, kc_id, f"Practice: {name}", unit, [kc_id],
                slots=random_slots([kc_id] * PRACTICE_SLOTS, [TAG_POOL]),
            )
            check = quiz(
                ROLE_CHECK, kc_id, f"Check: {name}", unit, [kc_id],
                questions=check_names[kc_id], pass_fraction=PASS_FRACTION,
            )
            quizzes.extend([practice, check])
            items.extend(
                [
                    {"type": "lesson", "kc_id": kc_id},
                    {"type": "quiz", "idnumber": practice["idnumber"]},
                    {"type": "quiz", "idnumber": check["idnumber"]},
                ]
            )
        if unit > units[0][0]:
            reviewed = _review_kcs(catalog, unit)
            review = quiz(
                ROLE_REVIEW, f"U{unit}", f"Unit {unit} review", unit, reviewed,
                slots=random_slots(reviewed, [TAG_POOL]),
            )
            quizzes.append(review)
            items.append({"type": "quiz", "idnumber": review["idnumber"]})
        sections.append({"number": unit, "name": unit_name, "items": items})
    challenge_items = []
    for kc in catalog:
        challenge = quiz(
            ROLE_CHALLENGE, kc["kc_id"], f"Challenge: {kc['kc_name']}",
            _unit_number(kc["unit"]), [kc["kc_id"]],
            slots=random_slots(
                [kc["kc_id"]] * CHALLENGE_SLOTS,
                [TAG_POOL, difficulty_tag(DIFFICULTY_LEVELS[-1])],
            ),
        )
        quizzes.append(challenge)
        challenge_items.append({"type": "quiz", "idnumber": challenge["idnumber"]})
    sections.append(
        {"number": units[-1][0] + 1, "name": CHALLENGE_SECTION_NAME, "items": challenge_items}
    )
    return quizzes, sections


def build_spec(course_dir=COURSE_DIR, layout=DEFAULT_LAYOUT):
    """Validated course specification for ``php/build_course.php``.

    Layout ``dimension`` (course 10) keeps exactly the keys and values it had
    before difficulty, CEFR levels and quiz roles existed; layout ``kc`` adds
    them (``questions[].difficulty/item_set/tags``, ``kcs[].cefr``,
    ``quizzes`` and ``sections``).
    """
    if layout not in LAYOUTS:
        raise ValueError(f"Unknown course layout {layout!r}; use one of {LAYOUTS}")
    fullname, shortname = COURSES[layout]
    catalog, questions = load_catalog(course_dir), load_questions(course_dir)
    lessons = load_lessons(course_dir)
    pool = load_pool(course_dir) if layout == LAYOUT_KC else None
    problems = validate(catalog, questions, lessons, layout=layout, pool=pool)
    if problems:
        raise ValueError("Course content is invalid:\n  " + "\n  ".join(problems))
    units = sorted({(_unit_number(kc["unit"]), kc["unit_name"]) for kc in catalog})
    by_id = {kc["kc_id"]: kc for kc in catalog}
    is_kc = layout == LAYOUT_KC

    spec_questions, check_names = [], {}
    for item_set, letter, source in ((CHECK_SET, "Q", questions), (POOL_SET, "P", pool or [])):
        counters = {}
        for question in source:
            kc_id = question["kc_id"]
            counters[kc_id] = counters.get(kc_id, 0) + 1
            entry = {
                "kc_id": kc_id,
                "name": f"{kc_id} {letter}{counters[kc_id]:02d}",
                "stem": question["stem"],
                "options": question["options"],
            }
            if is_kc:
                difficulty = int(question["difficulty"])
                entry.update(
                    difficulty=difficulty,
                    item_set=item_set,
                    tags=[
                        TAG_CHECK if item_set == CHECK_SET else TAG_POOL,
                        difficulty_tag(difficulty),
                        cefr_tag(by_id[kc_id]["cefr_level"]),
                    ],
                )
                if item_set == CHECK_SET:
                    check_names.setdefault(kc_id, []).append(entry["name"])
            spec_questions.append(entry)

    kcs = []
    for kc in catalog:
        entry = {
            "kc_id": kc["kc_id"],
            "unit": _unit_number(kc["unit"]),
            "dimension": kc["dimension"],
            "name": kc["kc_name"],
            "objective": kc["learning_objective"],
        }
        if is_kc:
            entry["cefr"] = kc["cefr_level"]
        kcs.append(entry)

    spec = {
        "fullname": fullname,
        "shortname": shortname,
        "layout": layout,
        "kc_framework_idnumber": KC_FRAMEWORK_IDNUMBER,
        "dimensions": DIMENSION_ORDER,
        "units": [{"number": number, "name": name} for number, name in units],
        "kcs": kcs,
        "questions": spec_questions,
        "lessons": [
            {
                "kc_id": lesson["kc_id"],
                "video": lesson["video"],
                "html": render_lesson(lesson, by_id[lesson["kc_id"]], layout),
            }
            for lesson in lessons
        ],
        "pass_fraction": PASS_FRACTION,
    }
    if is_kc:
        spec["quizzes"], spec["sections"] = _kc_layout(catalog, check_names)
        spec["question_tags"] = {
            "check": TAG_CHECK,
            "pool": TAG_POOL,
            "difficulty": {str(level): difficulty_tag(level) for level in DIFFICULTY_LEVELS},
        }
    return spec
