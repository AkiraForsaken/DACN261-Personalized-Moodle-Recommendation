"""From candidates to the final top-N: slots, order and de-duplication.

The list has a fixed shape instead of one global score:

    1. up to MAX_REMEDIATION remediation items, failed-quiz items first, then
       the lowest observed accuracy;
    2. the lessons to prepare for the upcoming quiz (at most MAX_PREPARE);
    3. the next activities in course order, to fill the list.

One slot is always reserved for a forward item when one exists. A learner with
many weak KCs therefore still sees a way forward, the list is never empty
while the course has open activities, and every position can be explained by
the rule that put it there. An activity appears once: its most specific reason
(remediation before prepare before continue) wins.
"""

from dataclasses import replace

from . import schemas


def select(remediation, forward, top_n, max_remediation):
    """Return the chosen candidates in rank order."""
    chosen, seen = [], set()

    def take(candidate):
        if candidate.module_id in seen or len(chosen) >= top_n:
            return False
        seen.add(candidate.module_id)
        chosen.append(candidate)
        return True

    remediation = sorted(remediation, key=lambda candidate: candidate.priority)
    remediation_modules = {candidate.module_id for candidate in remediation}
    forward_left = [c for c in forward if c.module_id not in remediation_modules]
    reserved = 1 if forward_left else 0
    remediation_slots = min(max_remediation, top_n - reserved)
    taken = 0
    for candidate in remediation:
        if taken >= remediation_slots:
            break
        taken += take(candidate)

    prepare = [
        c for c in forward_left if c.reason_code == schemas.PREPARE_PREDICTED_DIFFICULTY
    ]
    onward = [
        c for c in forward_left if c.reason_code != schemas.PREPARE_PREDICTED_DIFFICULTY
    ]
    # Prepare lessons come before plain course order, but never take the last
    # slot from it: the list keeps one "next in the course" item.
    keep_for_onward = 1 if onward else 0
    for candidate in prepare:
        if len(chosen) >= top_n - keep_for_onward:
            break
        take(candidate)
    for candidate in onward:
        take(candidate)
    return chosen


def group_of(candidate):
    """The slot a candidate competes for: ``remediation``, ``prepare``, ``forward``,
    or ``contingency`` for a row of a not (yet) activated failure branch."""
    if candidate.trigger_module_id is not None:
        return schemas.GROUP_CONTINGENCY
    return schemas.REASON_GROUPS[candidate.reason_code]


def effective_queue(queue, active_triggers=()):
    """The queue as the learner meets it after failing the quizzes in ``active_triggers``.

    The branch of every failed quiz comes first, as remediation, in queue
    order; branches of other quizzes are dropped; each activity is kept once
    (its first occurrence, so a branch row replaces the plain queue row).
    Moodle's ``recommendation_window::effective_queue`` is the same rule.
    """
    active = set(active_triggers)
    branch = [
        replace(c, trigger_module_id=None)
        for c in queue
        if c.trigger_module_id is not None and c.trigger_module_id in active
    ]
    plain = [c for c in queue if c.trigger_module_id is None]
    result, seen = [], set()
    for candidate in branch + plain:
        if candidate.module_id not in seen:
            seen.add(candidate.module_id)
            result.append(candidate)
    return result


def order_queue(remediation, forward, top_n, max_remediation):
    """Every candidate in one order: today's top-N first, then everything else.

    The head is exactly ``select(...)``. After it come the remaining
    remediation items (by priority), the remaining prepare lessons, then the
    remaining forward items in course order. Forward holds *every* open
    activity, so the queue covers the rest of the course: whatever the learner
    finishes, ``window`` can still fill the list from it.
    """
    head = select(remediation, forward, top_n, max_remediation)
    seen = {candidate.module_id for candidate in head}
    remediation_modules = {candidate.module_id for candidate in remediation}
    rest = sorted(remediation, key=lambda candidate: candidate.priority) + [
        c for c in forward if c.module_id not in remediation_modules
        and c.reason_code == schemas.PREPARE_PREDICTED_DIFFICULTY
    ] + [
        c for c in forward if c.module_id not in remediation_modules
        and c.reason_code != schemas.PREPARE_PREDICTED_DIFFICULTY
    ]
    queue = list(head)
    for candidate in rest:
        if candidate.module_id not in seen:
            seen.add(candidate.module_id)
            queue.append(candidate)
    return queue


def window(queue, done_module_ids, top_n, max_remediation, active_triggers=()):
    """What the learner sees once ``done_module_ids`` are dealt with.

    ``active_triggers`` are quizzes failed since the queue was made: their
    contingency branches join the remediation items first (``effective_queue``).
    Then the same slot rules as ``select``, applied to the items that are not
    done, in queue order. With nothing done it returns the queue's head. The
    Moodle block implements exactly this rule (``recommendation_window.php``);
    tests/fixtures/recommendation_window.json holds cases both must agree on.
    """
    done = set(done_module_ids)
    remaining = [c for c in effective_queue(queue, active_triggers) if c.module_id not in done]
    remediation = [
        replace(c, priority=(index,))
        for index, c in enumerate(remaining)
        if group_of(c) == schemas.GROUP_REMEDIATION
    ]
    forward = [c for c in remaining if group_of(c) != schemas.GROUP_REMEDIATION]
    chosen = select(remediation, forward, top_n, max_remediation)
    by_module = {c.module_id: c for c in remaining}
    return [by_module[c.module_id] for c in chosen]
