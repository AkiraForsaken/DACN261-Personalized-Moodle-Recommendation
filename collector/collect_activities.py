"""Step 1 - activity catalog: course modules -> topic + sequence (+ ID number)."""

from .config import t
from .db import write_csv

# Third authoring convention (next to the KC question category and the KC
# competency): a quiz's role is the prefix of its course-module ID number,
# e.g. ``CHECK-KC001``, ``PRACTICE-KC001``, ``CHECKIN-U3``. Kept in step with
# ``moodle_setup.content.QUIZ_ROLES`` (a test checks this).
QUIZ_ROLE_CHECK = "check"
QUIZ_ROLE_PREFIXES = {
    "CHECK": QUIZ_ROLE_CHECK,
    "PRACTICE": "practice",
    "CHECKIN": "checkin",
    "REVIEW": "review",
    "CHALLENGE": "challenge",
}
CATALOG_COLUMNS = [
    "course_module_id",
    "section_id",
    "section_number",
    "activity_type",
    "activity_id",
    "activity_name",
    "topic",
    "sequence",
    "visible",
    "completion_tracking",
    "idnumber",
    "pass_fraction",
    "quiz_role",
]


def quiz_role(idnumber, activity_type):
    """Role of a quiz from its course-module ID number; "" for other modules.

    A quiz without a known prefix is a check quiz: the role every quiz had
    before roles existed, so older courses (course 10) read as before.
    """
    if activity_type != "quiz":
        return ""
    prefix = (idnumber or "").strip().split("-", 1)[0].upper()
    return QUIZ_ROLE_PREFIXES.get(prefix, QUIZ_ROLE_CHECK)


def pass_fraction(grade_pass, grade_max):
    """A quiz's pass mark as a share of its maximum grade, or "" if it has none."""
    try:
        grade_pass, grade_max = float(grade_pass), float(grade_max)
    except (TypeError, ValueError):
        return ""
    if grade_pass <= 0 or grade_max <= 0:
        return ""
    return round(min(grade_pass / grade_max, 1.0), 6)


def collect_activity_catalog(cursor, course_id):
    query = f"""
        SELECT
            cm.id AS course_module_id,
            cs.id AS section_id,
            cs.section AS section_number,
            CASE
                WHEN cs.section = 0 THEN NULL
                ELSE COALESCE(NULLIF(cs.name, ''), CONCAT('Topic ', cs.section))
            END AS topic,
            m.name AS activity_type,
            cm.instance AS activity_id,
            CASE m.name
                WHEN 'quiz' THEN q.name
                WHEN 'lesson' THEN l.name
                WHEN 'resource' THEN r.name
                WHEN 'page' THEN p.name
                WHEN 'forum' THEN f.name
                WHEN 'assign' THEN a.name
                WHEN 'url' THEN u.name
                ELSE NULL
            END AS activity_name,
            cm.visible,
            cm.completion,
            cm.idnumber,
            gi.gradepass,
            gi.grademax
        FROM {t('course_modules')} cm
        JOIN {t('modules')} m ON m.id = cm.module
        JOIN {t('course_sections')} cs ON cs.id = cm.section
        LEFT JOIN {t('quiz')} q ON m.name = 'quiz' AND q.id = cm.instance
        LEFT JOIN {t('grade_items')} gi
               ON m.name = 'quiz'
              AND gi.itemtype = 'mod'
              AND gi.itemmodule = 'quiz'
              AND gi.iteminstance = cm.instance
              AND gi.itemnumber = 0
              AND gi.courseid = cm.course
        LEFT JOIN {t('lesson')} l ON m.name = 'lesson' AND l.id = cm.instance
        LEFT JOIN {t('resource')} r ON m.name = 'resource' AND r.id = cm.instance
        LEFT JOIN {t('page')} p ON m.name = 'page' AND p.id = cm.instance
        LEFT JOIN {t('forum')} f ON m.name = 'forum' AND f.id = cm.instance
        LEFT JOIN {t('assign')} a ON m.name = 'assign' AND a.id = cm.instance
        LEFT JOIN {t('url')} u ON m.name = 'url' AND u.id = cm.instance
        WHERE cm.course = ?
        ORDER BY cs.section, cm.id
    """
    cursor.execute(query, (course_id,))
    rows = []
    for i, row in enumerate(cursor.fetchall(), 1):
        (
            cmid,
            section_id,
            section_number,
            topic,
            activity_type,
            activity_id,
            name,
            visible,
            completion,
            idnumber,
            grade_pass,
            grade_max,
        ) = row
        rows.append(
            {
                "course_module_id": cmid,
                "section_id": section_id,
                "section_number": section_number,
                "activity_type": activity_type,
                "activity_id": activity_id,
                "activity_name": name or "",
                "topic": topic,
                "sequence": i,
                "visible": visible,
                "completion_tracking": completion,
                # Course-module ID number. A lesson page whose ID number is a
                # kc_id teaches that KC (see collect_course_mappings.py).
                "idnumber": (idnumber or "").strip(),
                # Share of the marks a quiz attempt needs to pass (its grade
                # item's pass grade / maximum grade). Empty for other
                # activities and for quizzes without a pass grade.
                "pass_fraction": pass_fraction(grade_pass, grade_max),
                # check / practice / checkin / review / challenge for quizzes.
                "quiz_role": quiz_role(idnumber, activity_type),
            }
        )

    write_csv("activity_catalog.csv", rows, CATALOG_COLUMNS)
    return rows
