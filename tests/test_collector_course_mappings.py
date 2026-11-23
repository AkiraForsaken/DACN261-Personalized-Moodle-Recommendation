import importlib

import pytest

# The package re-exports a function of the same name, so load the module itself.
mappings = importlib.import_module("collector.collect_course_mappings")


class RoutingCursor:
    """Fake cursor that answers each query by a distinctive table name."""

    def __init__(self, entries=(), usage=(), outside=(), competencies=(),
                 random_slots=(), categories=(), tags=()):
        self.results = {
            "entries": list(entries),
            "usage": list(usage),
            "outside": list(outside),
            "competencies": list(competencies),
            "random_slots": list(random_slots),
            "categories": list(categories),
            "tags": list(tags),
        }
        self.current = []

    def execute(self, query, params=()):
        if "competency_modulecomp" in query:
            self.current = self.results["competencies"]
        elif "question_set_references" in query:
            self.current = self.results["random_slots"]
        elif "tag_instance" in query:
            self.current = self.results["tags"]
        elif "qc.id IN" in query:
            self.current = self.results["categories"]
        elif "quiz_slots" in query:
            self.current = self.results["usage"]
        elif "qbe.id IN" in query:
            self.current = self.results["outside"]
        else:
            self.current = self.results["entries"]

    def fetchall(self):
        return self.current


# (entry id, category idnumber, category name, info, parent name, parent.parent)
ENTRIES = [
    (10, "KC-G1", "Present simple -s", "<p>Apply <b>-s/-es</b> rules.</p>", "Grammar", 1),
    (11, "KC-G1", "Present simple -s", "", "Grammar", 1),
    (20, "KC-V1", "Daily routine words", None, "Vocabulary", 1),
    (30, "", "Scratch questions", None, "Grammar", 1),
    (40, "KC-X1", "Uncategorised skill", None, "top", 0),
]
CATALOG = [
    {"activity_type": "quiz", "activity_id": 5, "topic": "Unit 1"},
    {"activity_type": "quiz", "activity_id": 6, "topic": "Unit 2"},
    {"activity_type": "page", "activity_id": 5, "topic": "Unit 9"},
    {"activity_type": "quiz", "activity_id": 7, "topic": None},
]


def test_question_categories_become_kcs_dimensions_and_objectives():
    catalog, question_map = mappings.build_course_mappings(ENTRIES)

    assert [kc["kc_id"] for kc in catalog] == ["KC-G1", "KC-V1", "KC-X1"]
    grammar = catalog[0]
    assert grammar["kc_name"] == "Present simple -s"
    assert grammar["kc_dimension"] == "grammar"
    assert grammar["learning_objective"] == "Apply -s/-es rules."
    # A KC category directly under the bank root has no dimension category.
    assert catalog[2]["kc_dimension"] == "general"
    # The category without an idnumber is not a KC; its question stays unmapped.
    assert [row["question_bank_entry_id"] for row in question_map] == ["10", "11", "20", "40"]
    assert {row["mapping_source"] for row in question_map} == {"moodle_question_category"}
    assert len({row["mapping_version"] for row in question_map}) == 1


def test_mapping_version_changes_only_when_the_mapping_changes():
    _, first = mappings.build_course_mappings(ENTRIES)
    _, same = mappings.build_course_mappings(list(reversed(ENTRIES)))
    moved = [(11, "KC-V1", "Daily routine words", None, "Vocabulary", 1) if row[0] == 11 else row for row in ENTRIES]
    _, changed = mappings.build_course_mappings(moved)

    assert first[0]["mapping_version"] == same[0]["mapping_version"]
    assert first[0]["mapping_version"] != changed[0]["mapping_version"]


def test_conflicting_kc_declarations_are_rejected():
    conflict = ENTRIES + [(50, "KC-G1", "Present simple -s", None, "Reading", 1)]
    with pytest.raises(ValueError, match="KC-G1"):
        mappings.build_course_mappings(conflict)


def test_collect_writes_catalog_question_map_and_topic_links(monkeypatch):
    written = {}
    monkeypatch.setattr(
        mappings, "write_csv", lambda name, rows, columns: written.update({name: (list(rows), list(columns))})
    )
    monkeypatch.setattr(mappings, "write_json", lambda name, payload: written.update({name: payload}))
    # Entry 60 is used by a course quiz but lives in a bank outside the course.
    cursor = RoutingCursor(
        entries=ENTRIES,
        usage=[(5, 10), (6, 10), (5, 20), (7, 20), (5, 30), (6, 60)],
        outside=[(60, "KC-R1", "Read a timetable", None, "Reading", 1)],
    )

    catalog, question_map, topic_map, _ = mappings.collect_course_mappings(
        cursor, 3, CATALOG, mapping_file="moodle_question_kc_map.csv", use_override=False
    )

    assert set(written) == {
        "moodle_question_kc_map.csv",
        "kc_catalog.csv",
        "kc_topic_map.csv",
        "kc_activity_map.csv",
        "kc_mapping_report.json",
        "question_metadata.csv",
    }
    assert written["kc_mapping_report.json"]["activity_kc_source"] == "course_module_idnumber"
    # Untagged questions (course 10 style) get metadata rows with empty labels.
    metadata = written["question_metadata.csv"][0]
    assert [row["question_bank_entry_id"] for row in metadata] == [
        row["question_bank_entry_id"] for row in question_map
    ]
    assert {(row["difficulty"], row["item_set"], row["cefr_level"]) for row in metadata} == {("", "", "")}
    assert written["kc_mapping_report.json"]["quizzes_by_role"] == {}
    assert written["kc_catalog.csv"][1] == mappings.KC_CATALOG_COLUMNS
    assert "KC-R1" in {kc["kc_id"] for kc in catalog}
    assert "60" in {row["question_bank_entry_id"] for row in question_map}
    # Quiz 7 has no topic and entry 30 has no KC, so neither creates a link.
    assert [(row["kc_id"], row["topic"]) for row in topic_map] == [
        ("KC-G1", "Unit 1"),
        ("KC-G1", "Unit 2"),
        ("KC-R1", "Unit 2"),
        ("KC-V1", "Unit 1"),
    ]


def test_curated_override_is_used_and_never_rewritten(tmp_path, monkeypatch):
    override = tmp_path / "curated.csv"
    override.write_text(
        "question_bank_entry_id,kc_id,kc_dimension,mapping_version,mapping_source\n"
        "10,KC001,Grammar,1.0,reviewed crosswalk\n",
        encoding="utf-8",
    )
    before = override.read_text(encoding="utf-8")
    written = {}
    monkeypatch.setattr(mappings, "write_csv", lambda name, rows, columns: written.update({name: list(rows)}))
    monkeypatch.setattr(mappings, "write_json", lambda name, payload: written.update({name: payload}))

    catalog, question_map, topic_map, _ = mappings.collect_course_mappings(
        RoutingCursor(usage=[(5, 10)]), 3, CATALOG, mapping_file=override, use_override=True
    )

    assert override.read_text(encoding="utf-8") == before
    assert "curated.csv" not in written
    assert catalog == [
        {"kc_id": "KC001", "kc_name": "KC001", "kc_dimension": "grammar", "learning_objective": ""}
    ]
    assert question_map[0]["mapping_version"] == "1.0"
    assert topic_map == [{"kc_id": "KC001", "topic": "Unit 1", "mapping_version": "1.0"}]


def test_kc_activity_map_links_lessons_by_idnumber_and_quizzes_by_questions():
    kc_catalog, question_map = mappings.build_course_mappings(ENTRIES)
    catalog_rows = [
        {"course_module_id": 51, "activity_type": "quiz", "activity_id": 5, "activity_name": "Q1", "topic": "Unit 1", "idnumber": ""},
        {"course_module_id": 61, "activity_type": "quiz", "activity_id": 6, "activity_name": "Q2", "topic": "Unit 2", "idnumber": "KC-G1"},
        {"course_module_id": 70, "activity_type": "page", "activity_id": 9, "activity_name": "Lesson G1", "topic": "Unit 1", "idnumber": "KC-G1"},
        {"course_module_id": 71, "activity_type": "page", "activity_id": 10, "activity_name": "Overview", "topic": "Unit 1", "idnumber": ""},
        {"course_module_id": 72, "activity_type": "url", "activity_id": 11, "activity_name": "Typo", "topic": "Unit 1", "idnumber": "KC-G9"},
    ]
    usage = [(5, 10), (5, 11), (5, 20), (6, 10), (6, 10), (5, 30)]

    rows, unknown = mappings.build_kc_activity_map(kc_catalog, question_map, usage, catalog_rows)

    summary = [(r["kc_id"], r["role"], r["course_module_id"], r["question_count"]) for r in rows]
    assert summary == [
        ("KC-G1", "learn", 70, ""),
        ("KC-G1", "practice", 51, 2),
        ("KC-G1", "practice", 61, 1),
        ("KC-V1", "practice", 51, 1),
    ]
    # A quiz's own ID number never makes it a learning resource; a typo is reported.
    assert unknown == ["KC-G9"]
    assert {r["mapping_version"] for r in rows} == {question_map[0]["mapping_version"]}


# --- one KC per activity, KCs attached as Moodle competencies ------------------

# A microlearning course: lesson 70 + check quiz 51 for KC-G1, lesson 71 +
# check quiz 61 for KC-V1. The lessons have no ID number; competencies name
# their KCs. Page 72 carries an old-style ID number that must be ignored.
KC_COURSE = [
    {"course_module_id": 70, "activity_type": "page", "activity_id": 9, "activity_name": "Lesson: G1", "topic": "Unit 1", "idnumber": ""},
    {"course_module_id": 51, "activity_type": "quiz", "activity_id": 5, "activity_name": "Check: G1", "topic": "Unit 1", "idnumber": ""},
    {"course_module_id": 71, "activity_type": "page", "activity_id": 10, "activity_name": "Lesson: V1", "topic": "Unit 1", "idnumber": ""},
    {"course_module_id": 61, "activity_type": "quiz", "activity_id": 6, "activity_name": "Check: V1", "topic": "Unit 1", "idnumber": ""},
    {"course_module_id": 72, "activity_type": "page", "activity_id": 11, "activity_name": "Old", "topic": "Unit 1", "idnumber": "KC-X1"},
]
KC_USAGE = [(5, 10), (5, 11), (6, 20)]
KC_COMPETENCIES = {70: ["KC-G1"], 51: ["KC-G1"], 71: ["KC-V1", "DIM-Vocabulary"], 61: ["KC-V1"]}


def test_lessons_come_from_module_competencies_not_idnumbers():
    kc_catalog, question_map = mappings.build_course_mappings(ENTRIES)

    rows, unknown = mappings.build_kc_activity_map(
        kc_catalog, question_map, KC_USAGE, KC_COURSE, KC_COMPETENCIES
    )

    summary = [(r["kc_id"], r["role"], r["course_module_id"], r["mapping_source"]) for r in rows]
    assert summary == [
        ("KC-G1", "learn", 70, "course_module_competency"),
        ("KC-G1", "practice", 51, "quiz_question_usage"),
        ("KC-V1", "learn", 71, "course_module_competency"),
        ("KC-V1", "practice", 61, "quiz_question_usage"),
    ]
    # Page 72's ID number is ignored once the course uses competencies, and a
    # dimension competency is a parent, not an unknown KC.
    assert unknown == []


def test_mapping_report_counts_single_kc_quizzes_and_flags_disagreements():
    kc_catalog, question_map = mappings.build_course_mappings(ENTRIES)
    # Quiz 61 is linked to KC-G1 but its question tests KC-V1; quiz 51 has no link.
    links = {70: ["KC-G1"], 71: ["KC-V1"], 61: ["KC-G1"], 72: ["KC-ZZ"]}
    rows, _ = mappings.build_kc_activity_map(kc_catalog, question_map, KC_USAGE, KC_COURSE, links)

    report = mappings.build_mapping_report(kc_catalog, rows, KC_COURSE, links)

    assert report["activity_kc_source"] == "course_module_competency"
    assert (report["quizzes"], report["single_kc_quizzes"], report["max_kcs_per_quiz"]) == (2, 2, 1)
    assert report["multi_kc_quizzes"] == []
    assert report["quizzes_without_competency"] == [51]
    assert report["quiz_competency_mismatches"] == [
        {"course_module_id": 61, "activity_name": "Check: V1", "competency_kcs": ["KC-G1"], "question_kcs": ["KC-V1"]}
    ]
    assert report["competencies_without_questions"] == ["KC-ZZ"]
    assert report["kcs_without_lesson"] == ["KC-X1"]
    assert report["kcs_without_quiz"] == ["KC-X1"]


def test_mapping_report_lists_mixed_quizzes_of_the_old_layout():
    kc_catalog, question_map = mappings.build_course_mappings(ENTRIES)
    mixed = [(5, 10), (5, 20)]  # one quiz testing two KCs
    rows, _ = mappings.build_kc_activity_map(kc_catalog, question_map, mixed, KC_COURSE)

    report = mappings.build_mapping_report(kc_catalog, rows, KC_COURSE, {})

    assert report["activity_kc_source"] == "course_module_idnumber"
    assert report["single_kc_quizzes"] == 0 and report["max_kcs_per_quiz"] == 2
    assert report["multi_kc_quizzes"] == [
        {"course_module_id": 51, "activity_name": "Check: G1", "kc_ids": ["KC-G1", "KC-V1"]}
    ]


def test_kc_of_module_keeps_only_single_kc_modules():
    rows = [
        {"kc_id": "KC-G1", "course_module_id": 70, "role": "learn"},
        {"kc_id": "KC-G1", "course_module_id": 51, "role": "practice"},
        {"kc_id": "KC-V1", "course_module_id": 51, "role": "practice"},
        {"kc_id": "KC-V1", "course_module_id": 61, "role": "practice"},
    ]
    assert mappings.kc_of_module(rows) == {70: "KC-G1", 61: "KC-V1"}


def test_collect_reads_competencies_from_moodle(monkeypatch):
    written = {}
    monkeypatch.setattr(mappings, "write_csv", lambda name, rows, columns: written.update({name: list(rows)}))
    monkeypatch.setattr(mappings, "write_json", lambda name, payload: written.update({name: payload}))
    cursor = RoutingCursor(
        entries=ENTRIES,
        usage=KC_USAGE,
        competencies=[(70, "KC-G1"), (51, "KC-G1"), (71, "KC-V1"), (61, "KC-V1"), (71, None)],
    )

    *_, activity_map = mappings.collect_course_mappings(
        cursor, 3, KC_COURSE, mapping_file="moodle_question_kc_map.csv", use_override=False
    )

    assert {(r["kc_id"], r["course_module_id"]) for r in activity_map if r["role"] == "learn"} == {
        ("KC-G1", 70),
        ("KC-V1", 71),
    }
    report = written["kc_mapping_report.json"]
    assert report["module_competency_links"] == 4
    assert report["quiz_competency_mismatches"] == [] and report["single_kc_quizzes"] == 2


# --- quiz roles, random slots, question tags (layout kc) --------------------------

from collector import collect_activities  # noqa: E402


def test_quiz_role_comes_from_the_id_number_prefix():
    role = collect_activities.quiz_role
    assert role("CHECK-KC001", "quiz") == "check"
    assert role("practice-KC001", "quiz") == "practice"
    assert role("CHECKIN-U3", "quiz") == "checkin"
    assert role("REVIEW-U3", "quiz") == "review"
    assert role("CHALLENGE-KC001", "quiz") == "challenge"
    # Course 10 style: no or unknown ID number is a check quiz; pages have no role.
    assert role("", "quiz") == role(None, "quiz") == role("KC001", "quiz") == "check"
    assert role("PRACTICE-KC001", "page") == ""


def test_quiz_role_prefixes_match_the_course_builder():
    from moodle_setup import content

    assert collect_activities.QUIZ_ROLE_PREFIXES == content.QUIZ_ROLES
    assert mappings.TAG_PREFIX == content.TAG_PREFIX
    assert set(mappings.ITEM_SET_TAGS) == {content.TAG_CHECK, content.TAG_POOL}
    for level in content.DIFFICULTY_LEVELS:
        assert mappings.DIFFICULTY_TAG.match(content.difficulty_tag(level))
    for level in content.CEFR_LEVELS:
        assert mappings.CEFR_TAG.match(content.cefr_tag(level)).group(1).upper() == level


def test_random_slot_filter_category_both_moodle_formats():
    new = '{"filter": {"category": {"jointype": 1, "values": [42], "filteroptions": {"includesubcategories": false}}, "qtagids": {"jointype": 2, "values": [7]}}}'
    assert mappings._filter_category_id(new) == 42
    assert mappings._filter_category_id('{"questioncategoryid": "17", "includingsubcategories": 0}') == 17
    assert mappings._filter_category_id("not json") is None
    assert mappings._filter_category_id(None) is None


def test_random_slots_map_to_the_kc_of_their_category():
    cond = '{{"filter": {{"category": {{"values": [{}]}}}}}}'.format
    cursor = RoutingCursor(
        random_slots=[(8, cond(100)), (8, cond(100)), (9, cond(101)), (9, cond(102)), (9, "bad")],
        categories=[(100, "KC-G1"), (101, "KC-V1"), (102, "")],
    )
    # Category 102 has no ID number and the last slot has no category: skipped.
    assert mappings._read_random_slot_kcs(cursor, 3) == [(8, "KC-G1"), (8, "KC-G1"), (9, "KC-V1")]


ROLE_CATALOG = [
    {"course_module_id": 50, "activity_type": "quiz", "activity_id": 5, "topic": "Unit 1", "quiz_role": "check"},
    {"course_module_id": 51, "activity_type": "quiz", "activity_id": 8, "topic": "Unit 1", "quiz_role": "practice"},
    {"course_module_id": 52, "activity_type": "quiz", "activity_id": 9, "topic": "Unit 1", "quiz_role": "checkin"},
]


def test_random_slot_quizzes_map_before_any_attempt_and_count_slots():
    catalog, question_map = mappings.build_course_mappings(ENTRIES)
    # Check quiz 5 uses fixed entries 10 and 11; practice quiz 8 has two random
    # KC-G1 slots and attempts drew entries 10 and 11; the check-in (9) draws
    # one question from each of two KCs and has no attempts yet.
    usage = [(5, 10), (5, 11), (8, 10), (8, 11)]
    slots = [(8, "KC-G1"), (8, "KC-G1"), (9, "KC-G1"), (9, "KC-V1"), (9, "KC-UNKNOWN")]
    rows, _ = mappings.build_kc_activity_map(catalog, question_map, usage, ROLE_CATALOG, {}, slots)
    practice = {(r["kc_id"], r["course_module_id"]): r for r in rows if r["role"] == "practice"}

    assert practice[("KC-G1", 50)]["question_count"] == 2
    assert practice[("KC-G1", 50)]["mapping_source"] == "quiz_question_usage"
    # Slots, not the different questions drawn so far.
    assert practice[("KC-G1", 51)]["question_count"] == 2
    assert practice[("KC-G1", 51)]["mapping_source"] == "quiz_random_slot"
    assert practice[("KC-G1", 52)]["question_count"] == 1 and ("KC-V1", 52) in practice
    assert not any(kc == "KC-UNKNOWN" for kc, _ in practice)
    # Events on the practice quiz get its KC; the mixed check-in gets none.
    kc_of = mappings.kc_of_module(rows)
    assert kc_of[51] == "KC-G1" and 52 not in kc_of


def test_question_metadata_reads_tags_of_the_latest_version():
    _, question_map = mappings.build_course_mappings(ENTRIES)
    cursor = RoutingCursor(tags=[
        (10, 1, "ml-check"), (10, 1, "ML-Difficulty-1"), (10, 1, "ml-cefr-a1"),
        # Entry 11 was edited: version 2's tags replace version 1's.
        (11, 1, "ml-difficulty-1"), (11, 2, "ml-pool"), (11, 2, "ml-difficulty-3"),
        (11, 2, "ml-cefr-a1"), (11, 2, "grammar"),
    ])
    tags = mappings._read_question_tags(cursor, ["10", "11", "20"])
    rows = {r["question_bank_entry_id"]: r for r in mappings.build_question_metadata(question_map, tags)}

    assert (rows["10"]["difficulty"], rows["10"]["item_set"], rows["10"]["cefr_level"]) == ("1", "check", "A1")
    assert (rows["11"]["difficulty"], rows["11"]["item_set"]) == ("3", "pool")
    # Only the convention's tags are kept; untagged questions stay empty.
    assert rows["11"]["tags"] == "ml-cefr-a1;ml-difficulty-3;ml-pool"
    assert (rows["20"]["difficulty"], rows["20"]["item_set"], rows["20"]["tags"]) == ("", "", "")
    assert list(rows["10"]) == mappings.QUESTION_METADATA_COLUMNS


def test_mapping_report_checks_roles_pool_difficulty_and_cefr():
    catalog = [{"kc_id": "KC-G1"}, {"kc_id": "KC-V1"}]
    activity_map = [
        {"kc_id": "KC-G1", "course_module_id": 50, "role": "practice"},
        {"kc_id": "KC-G1", "course_module_id": 51, "role": "practice"},
        {"kc_id": "KC-G1", "course_module_id": 52, "role": "practice"},
        {"kc_id": "KC-V1", "course_module_id": 52, "role": "practice"},
    ]
    metadata = [
        {"kc_id": "KC-G1", "item_set": "pool", "difficulty": "1", "cefr_level": "A1"},
        {"kc_id": "KC-G1", "item_set": "pool", "difficulty": "3", "cefr_level": "A1"},
        {"kc_id": "KC-G1", "item_set": "check", "difficulty": "2", "cefr_level": "A2"},
        {"kc_id": "KC-V1", "item_set": "pool", "difficulty": "2", "cefr_level": "A2"},
    ]
    report = mappings.build_mapping_report(catalog, activity_map, ROLE_CATALOG, {}, metadata)

    assert report["quizzes_by_role"] == {"check": 1, "checkin": 1, "practice": 1}
    assert (report["check_quizzes_single_kc"], report["check_quizzes"]) == (1, 1)
    assert report["kcs_without_practice"] == ["KC-V1"]
    assert report["pool_items_by_difficulty"] == {"1": 1, "2": 1, "3": 1}
    assert report["kcs_missing_hard_pool_items"] == ["KC-V1"]
    assert report["cefr_disagreements"] == ["KC-G1"]
    # The check-in mixes two KCs, as designed.
    assert [q["course_module_id"] for q in report["multi_kc_quizzes"]] == [52]
