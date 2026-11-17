"""Weakness engine.

Flow:
  1. end_test()      -> grades a test, scores every seen question with
                         W = R × (0.5 + 0.2·p) + 0.2·u + 0.1·t, and sets each topic's
                         weakness to the average W of its seen questions since its last reset
  2. check_trigger() -> right after a test ends, if any topic is weak (average W ≥ 0.45 with
                         at least 5 seen questions), creates a pending popup
  3. submit_popup()  -> creates planner tasks and FLUSHES the assigned topics
                         (score = 0, status = in_progress; scoring paused)
  4. complete_task() -> topic goes back to normal and scoring restarts from 0
"""
import json
import random
import sqlite3

from .db import FORMULA, MIN_SEEN, PRACTICE_PASS_PCT, WEAK_THRESHOLD, now_ms

def get_settings(conn: sqlite3.Connection) -> dict:
    row = dict(conn.execute("SELECT * FROM settings WHERE id = 1").fetchone())
    row["weights"] = json.loads(row["weights"])
    return row


def question_weakness(selected, correct_index, option_pcts, time_spent, avg_time, seen):
    """Score one question with W = R × (base + w_p·p) + w_u·u + w_t·t.
    Returns None for a question that was never seen (it doesn't count at all)."""
    if selected is None and not seen:
        return None
    result = "omitted" if selected is None else ("correct" if selected == correct_index else "incorrect")
    r = FORMULA["R"][result]

    pcts = json.loads(option_pcts) if option_pcts else None
    p = pcts[correct_index] / 100 if pcts else 0.5          # no statistics: neutral 0.5

    u = 0.0
    if result == "incorrect" and pcts:
        top_wrong = max(pct for i, pct in enumerate(pcts) if i != correct_index)
        if top_wrong > 0:
            u = min(1.0, max(0.0, 1 - pcts[selected] / top_wrong))

    t = min(1.0, max(0.0, time_spent / avg_time - 1)) if avg_time else 0.0

    w = r * (FORMULA["base"] + FORMULA["w_p"] * p) + FORMULA["w_u"] * u + FORMULA["w_t"] * t
    return {"result": result, "r": r, "p": round(p, 3), "u": round(u, 3), "t": round(t, 3), "w": round(min(1.0, w), 3)}


# ---------------------------------------------------------------- tests

def end_test(conn: sqlite3.Connection, test_id: int) -> dict:
    test = conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
    if test is None:
        raise ValueError("Test not found")
    if test["status"] == "ended":
        return {"already_ended": True}

    rows = conn.execute(
        "SELECT tq.*, q.correct_index, q.topic_id, q.avg_time_sec, q.pool, q.option_pcts FROM test_questions tq"
        " JOIN questions q ON q.id = tq.question_id WHERE tq.test_id = ?",
        (test_id,),
    ).fetchall()
    ts = now_ms()
    correct_total = 0
    for r in rows:
        omitted = r["selected"] is None
        is_correct = (not omitted) and r["selected"] == r["correct_index"]
        correct_total += int(is_correct)
        conn.execute(
            "UPDATE test_questions SET is_correct = ?, omitted = ? WHERE id = ?",
            (int(is_correct), int(omitted), r["id"]),
        )
        if r["pool"] == "qbank":
            status = "omitted" if omitted else ("correct" if is_correct else "incorrect")
            conn.execute("UPDATE user_question_status SET status = ? WHERE question_id = ?", (status, r["question_id"]))

    conn.execute(
        "UPDATE tests SET status = 'ended', ended_at = ?, correct = ? WHERE id = ?",
        (ts, correct_total, test_id),
    )

    if test["type"] == "custom":
        _record_weakness(conn, test_id, rows, ts)
        check_trigger(conn)
    elif test["type"] == "practice" and test["task_id"]:
        # Practice tests do not add weakness events. The task completes only on a pass;
        # otherwise it stays pending (topic stays In progress) and the student retakes it.
        passed = practice_passed(correct_total, len(rows))
        if passed:
            complete_task(conn, test["task_id"])
        return {"correct": correct_total, "total": len(rows), "passed": passed, "pass_pct": PRACTICE_PASS_PCT}

    return {"correct": correct_total, "total": len(rows)}


def practice_passed(correct: int, total: int) -> bool:
    return total > 0 and correct * 100 >= PRACTICE_PASS_PCT * total


def _record_weakness(conn, test_id, rows, ts):
    """One event per seen question holding its W; then each topic's weakness = average W."""
    paused = {
        r["topic_id"]
        for r in conn.execute("SELECT topic_id FROM topic_weakness WHERE status = 'in_progress'")
    }
    touched = set()
    for r in rows:
        score = question_weakness(r["selected"], r["correct_index"], r["option_pcts"],
                                  r["time_spent_sec"], r["avg_time_sec"], r["seen"])
        if score is None:
            continue
        applied = r["topic_id"] not in paused
        conn.execute(
            "INSERT INTO weakness_events (test_id, question_id, topic_id, event_type, weight, applied, created_at, r, p, u, t)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (test_id, r["question_id"], r["topic_id"], score["result"], score["w"], int(applied), ts,
             score["r"], score["p"], score["u"], score["t"]),
        )
        if applied:
            touched.add(r["topic_id"])

    for topic_id in touched:
        update_topic_weakness(conn, topic_id)


def update_topic_weakness(conn, topic_id):
    """Topic weakness = average W of the topic's seen questions since its last reset."""
    reset_at = conn.execute("SELECT reset_at FROM topic_weakness WHERE topic_id = ?", (topic_id,)).fetchone()[0]
    avg, seen = conn.execute(
        "SELECT AVG(weight), COUNT(*) FROM weakness_events WHERE topic_id = ? AND applied = 1 AND created_at >= ?",
        (topic_id, reset_at),
    ).fetchone()
    avg = round(avg or 0.0, 3)
    status = "weak" if seen >= MIN_SEEN and avg >= WEAK_THRESHOLD else "normal"
    conn.execute("UPDATE topic_weakness SET score = ?, seen_count = ?, status = ? WHERE topic_id = ?",
                 (avg, seen, status, topic_id))


# ---------------------------------------------------------------- trigger + popup

def _weak_topic_ids(conn):
    return [
        r["topic_id"]
        for r in conn.execute(
            "SELECT tw.topic_id FROM topic_weakness tw JOIN topics t ON t.id = tw.topic_id"
            " WHERE tw.status = 'weak' ORDER BY tw.score DESC, t.sort"
        )
    ]


def check_trigger(conn: sqlite3.Connection):
    """Runs after every custom test. Creates (or refreshes) the pending popup when any topic
    is at or over the threshold. Returns the pending popup row or None.

    The popup is only created here, after a test, so closing it doesn't make it reappear on
    the next screen; it comes back only after another test if the topic is still weak.
    """
    weak = _weak_topic_ids(conn)
    pending = conn.execute("SELECT * FROM popups WHERE status = 'pending' ORDER BY id DESC LIMIT 1").fetchone()

    if pending is not None:
        if not weak:
            conn.execute("UPDATE popups SET status = 'expired' WHERE id = ?", (pending["id"],))
            return None
        conn.execute("UPDATE popups SET topic_ids = ? WHERE id = ?", (json.dumps(weak), pending["id"]))
        return conn.execute("SELECT * FROM popups WHERE id = ?", (pending["id"],)).fetchone()

    if not weak:
        return None
    ts = now_ms()
    cur = conn.execute(
        "INSERT INTO popups (created_at, topic_ids, status) VALUES (?, ?, 'pending')", (ts, json.dumps(weak))
    )
    conn.executemany(
        "UPDATE topic_weakness SET last_triggered_at = ? WHERE topic_id = ?", [(ts, t) for t in weak]
    )
    return conn.execute("SELECT * FROM popups WHERE id = ?", (cur.lastrowid,)).fetchone()


def pending_popup(conn: sqlite3.Connection):
    """The popup waiting to be shown on a non-test screen, if any."""
    return conn.execute("SELECT * FROM popups WHERE status = 'pending' ORDER BY id DESC LIMIT 1").fetchone()


def popup_payload(conn: sqlite3.Connection, popup) -> dict:
    topics = []
    for topic_id in json.loads(popup["topic_ids"]):
        t = conn.execute(
            "SELECT t.id, t.name, tw.score, tw.seen_count, tw.reset_at FROM topics t"
            " JOIN topic_weakness tw ON tw.topic_id = t.id WHERE t.id = ?",
            (topic_id,),
        ).fetchone()
        c = conn.execute(
            "SELECT SUM(event_type = 'incorrect'), SUM(event_type = 'omitted'),"
            " SUM(event_type = 'correct' AND weight > 0) FROM weakness_events"
            " WHERE topic_id = ? AND applied = 1 AND created_at >= ?",
            (topic_id, t["reset_at"]),
        ).fetchone()
        incorrect, omitted, slow_correct = (c[0] or 0), (c[1] or 0), (c[2] or 0)
        topics.append({
            "id": t["id"], "name": t["name"], "score": t["score"], "seen": t["seen_count"],
            "events": {"incorrect": incorrect, "omitted": omitted, "slow_correct": slow_correct},
        })
    return {"id": popup["id"], "created_at": popup["created_at"], "threshold": WEAK_THRESHOLD,
            "pass_pct": PRACTICE_PASS_PCT, "topics": topics}


def submit_popup(conn: sqlite3.Connection, popup_id: int, assignments: list) -> list:
    popup = conn.execute("SELECT * FROM popups WHERE id = ?", (popup_id,)).fetchone()
    if popup is None or popup["status"] != "pending":
        raise ValueError("This pop-up is no longer active.")
    allowed = set(json.loads(popup["topic_ids"]))
    seen = set()
    for a in assignments:
        if a["topic_id"] not in allowed:
            raise ValueError("A topic in the request isn't part of this pop-up.")
        if a["topic_id"] in seen:
            raise ValueError("Each topic can be assigned to only one action.")
        if a["action"] not in ("unclear", "practice"):
            raise ValueError("Unknown action.")
        seen.add(a["topic_id"])
    if not assignments:
        raise ValueError("Assign at least one topic to an action.")

    ts = now_ms()
    task_ids = []
    for a in assignments:
        task_type = "revision" if a["action"] == "unclear" else "practice"
        remind_at = a["scheduled_at"] - a["remind_before_min"] * 60 * 1000
        cur = conn.execute(
            "INSERT INTO planner_tasks (topic_id, type, popup_id, scheduled_at, remind_before_min, remind_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (a["topic_id"], task_type, popup_id, a["scheduled_at"], a["remind_before_min"], remind_at, ts),
        )
        task_ids.append(cur.lastrowid)
        if task_type == "revision":
            _add_revision_items(conn, cur.lastrowid, a["topic_id"])   # must run before the flush below
        # FLUSH: tasks now cover this weakness, so reset the score and pause scoring.
        conn.execute(
            "UPDATE topic_weakness SET score = 0, seen_count = 0, status = 'in_progress', reset_at = ? WHERE topic_id = ?",
            (ts, a["topic_id"]),
        )
    conn.execute("UPDATE popups SET status = 'done' WHERE id = ?", (popup_id,))
    return task_ids


def _add_revision_items(conn, task_id, topic_id):
    """Link the revision task to only the lectures and e-textbook sections that teach the
    questions the student struggled with on this topic (since the topic's last reset)."""
    reset_at = conn.execute("SELECT reset_at FROM topic_weakness WHERE topic_id = ?", (topic_id,)).fetchone()[0]
    rows = conn.execute(
        "SELECT DISTINCT q.id, q.lecture_id, q.section_id FROM weakness_events e JOIN questions q ON q.id = e.question_id"
        " WHERE e.topic_id = ? AND e.applied = 1 AND e.weight > 0 AND e.created_at >= ? AND q.lecture_id IS NOT NULL",
        (topic_id, reset_at),
    ).fetchall()
    items = {}
    for r in rows:
        items.setdefault(("lecture", r["lecture_id"]), []).append(r["id"])
        items.setdefault(("section", r["section_id"]), []).append(r["id"])
    if not items:  # safety net: no linked questions found, so use every lecture and section of the topic
        for r in conn.execute("SELECT id FROM lectures WHERE topic_id = ?", (topic_id,)):
            items[("lecture", r["id"])] = []
        for r in conn.execute("SELECT id FROM etextbook_sections WHERE topic_id = ?", (topic_id,)):
            items[("section", r["id"])] = []
    conn.executemany(
        "INSERT INTO task_items (task_id, item_type, item_id, question_ids) VALUES (?, ?, ?, ?)",
        [(task_id, kind, item_id, json.dumps(sorted(qids))) for (kind, item_id), qids in items.items()],
    )


def dismiss_popup(conn: sqlite3.Connection, popup_id: int) -> None:
    # Scores stay as they are; the next trigger re-checks.
    conn.execute("UPDATE popups SET status = 'dismissed' WHERE id = ? AND status = 'pending'", (popup_id,))


# ---------------------------------------------------------------- tasks

def complete_task(conn: sqlite3.Connection, task_id: int) -> None:
    task = conn.execute("SELECT * FROM planner_tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["status"] == "done":
        return
    ts = now_ms()
    conn.execute("UPDATE planner_tasks SET status = 'done', completed_at = ? WHERE id = ?", (ts, task_id))
    # Resume scoring only when no other open task covers this topic.
    open_tasks = conn.execute(
        "SELECT COUNT(*) FROM planner_tasks WHERE topic_id = ? AND status = 'pending'", (task["topic_id"],)
    ).fetchone()[0]
    if open_tasks == 0:
        conn.execute(
            "UPDATE topic_weakness SET score = 0, seen_count = 0, status = 'normal', reset_at = ? WHERE topic_id = ?",
            (ts, task["topic_id"]),
        )


def revision_progress(conn: sqlite3.Connection, task_id: int) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM task_items WHERE task_id = ?", (task_id,)).fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM completions c JOIN task_items ti"
        " ON ti.task_id = c.task_id AND ti.item_type = c.item_type AND ti.item_id = c.item_id WHERE c.task_id = ?",
        (task_id,),
    ).fetchone()[0]
    return {"done": done, "total": total}


def mark_item_complete(conn: sqlite3.Connection, task_id: int, item_type: str, item_id: int) -> dict:
    task = conn.execute("SELECT * FROM planner_tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["type"] != "revision":
        raise ValueError("Revision task not found.")
    in_task = conn.execute(
        "SELECT 1 FROM task_items WHERE task_id = ? AND item_type = ? AND item_id = ?", (task_id, item_type, item_id)
    ).fetchone()
    if in_task is None:
        raise ValueError("This item isn't part of the revision task.")
    conn.execute(
        "INSERT OR IGNORE INTO completions (task_id, item_type, item_id, completed_at) VALUES (?, ?, ?, ?)",
        (task_id, item_type, item_id, now_ms()),
    )
    progress = revision_progress(conn, task_id)
    if progress["done"] >= progress["total"]:
        complete_task(conn, task_id)
    progress["task_done"] = progress["done"] >= progress["total"]
    progress["topic"] = conn.execute("SELECT name FROM topics WHERE id = ?", (task["topic_id"],)).fetchone()["name"]
    return progress


def start_practice(conn: sqlite3.Connection, task_id: int) -> int:
    """Resume the open attempt, or create a new 10-question building-block attempt: 5 easy, then 5 medium.
    Each topic has exactly 5 easy + 5 medium foundation questions, so a retake uses the same questions
    in a new order within each level."""
    task = conn.execute("SELECT * FROM planner_tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["type"] != "practice":
        raise ValueError("Practice task not found.")
    if task["status"] == "done":
        raise ValueError("This practice task is already complete.")
    # Resume an unfinished attempt; otherwise start a new attempt (first try or retake after a fail).
    existing = conn.execute(
        "SELECT id FROM tests WHERE task_id = ? AND status = 'in_progress' ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    if existing is not None:
        return existing["id"]

    picked = []
    for level in ("easy", "medium"):
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM questions WHERE pool = 'foundation' AND topic_id = ? AND difficulty = ?",
            (task["topic_id"], level),
        )]
        random.shuffle(ids)
        picked.extend(ids[:5])   # easy first, then medium: building blocks
    return _create_test_row(conn, "practice", [task["topic_id"]], picked, task_id)


def create_custom_test(conn: sqlite3.Connection, statuses: list, topic_ids: list, count: int) -> int:
    if not statuses or not topic_ids:
        raise ValueError("Pick at least one question mode and one topic.")
    s_ph = ",".join("?" * len(statuses))
    t_ph = ",".join("?" * len(topic_ids))
    ids = [r["id"] for r in conn.execute(
        f"SELECT q.id FROM questions q JOIN user_question_status s ON s.question_id = q.id"
        f" WHERE q.pool = 'qbank' AND s.status IN ({s_ph}) AND q.topic_id IN ({t_ph})",
        (*statuses, *topic_ids),
    )]
    if count < 1 or count > len(ids):
        raise ValueError(f"Choose between 1 and {len(ids)} questions.")
    random.shuffle(ids)
    return _create_test_row(conn, "custom", topic_ids, ids[:count], None)


def _create_test_row(conn, test_type, topic_ids, question_ids, task_id):
    cur = conn.execute(
        "INSERT INTO tests (type, task_id, topic_ids, created_at, total) VALUES (?, ?, ?, ?, ?)",
        (test_type, task_id, json.dumps(topic_ids), now_ms(), len(question_ids)),
    )
    test_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO test_questions (test_id, question_id, position) VALUES (?, ?, ?)",
        [(test_id, qid, i) for i, qid in enumerate(question_ids)],
    )
    return test_id
