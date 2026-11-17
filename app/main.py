"""FastAPI app for the MCAT weak-foundations prototype.

Run:  uvicorn app.main:app --reload
Open: http://127.0.0.1:8000      API docs: http://127.0.0.1:8000/docs
"""
import json
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine
from .db import BASE_DIR, PRACTICE_PASS_PCT, get_conn, init_db, now_ms, reset_session

STATIC_DIR = os.path.join(BASE_DIR, "static")
STATUSES = ["unused", "correct", "incorrect", "omitted"]


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="UWorld MCAT – Weak Foundations Prototype", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class DB:
    """Context manager: one connection per request, commit on success."""

    def __enter__(self):
        self.conn = get_conn()
        return self.conn

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.conn.commit()
        self.conn.close()


def bad_request(err: Exception):
    raise HTTPException(status_code=400, detail=str(err))


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ------------------------------------------------------------------ Create Test

@app.get("/api/qbank/counts")
def qbank_counts(statuses: str = "unused"):
    """Counts per status (all topics) and per topic for the selected statuses."""
    selected = [s for s in statuses.split(",") if s in STATUSES]
    with DB() as conn:
        status_counts = {s: 0 for s in STATUSES}
        for r in conn.execute(
            "SELECT s.status, COUNT(*) n FROM questions q JOIN user_question_status s ON s.question_id = q.id"
            " WHERE q.pool = 'qbank' GROUP BY s.status"
        ):
            status_counts[r["status"]] = r["n"]
        topics = []
        for t in conn.execute("SELECT id, name FROM topics ORDER BY sort"):
            n = 0
            if selected:
                ph = ",".join("?" * len(selected))
                n = conn.execute(
                    f"SELECT COUNT(*) FROM questions q JOIN user_question_status s ON s.question_id = q.id"
                    f" WHERE q.pool = 'qbank' AND q.topic_id = ? AND s.status IN ({ph})",
                    (t["id"], *selected),
                ).fetchone()[0]
            topics.append({"id": t["id"], "name": t["name"], "count": n})
        return {"status_counts": status_counts, "topics": topics}


class CreateTestIn(BaseModel):
    statuses: List[str]
    topic_ids: List[int]
    count: int


@app.post("/api/tests")
def create_test(body: CreateTestIn):
    with DB() as conn:
        try:
            test_id = engine.create_custom_test(conn, [s for s in body.statuses if s in STATUSES], body.topic_ids, body.count)
        except ValueError as e:
            bad_request(e)
        return {"test_id": test_id}


# ------------------------------------------------------------------ Test interface

def _test_meta(conn, test):
    topic_ids = json.loads(test["topic_ids"])
    names = [r["name"] for r in conn.execute(
        f"SELECT name FROM topics WHERE id IN ({','.join('?' * len(topic_ids))}) ORDER BY sort", topic_ids
    )] if topic_ids else []
    meta = dict(test)
    meta["topic_ids"] = topic_ids
    meta["topic_names"] = names
    return meta


@app.get("/api/tests")
def list_tests():
    with DB() as conn:
        return [_test_meta(conn, t) for t in conn.execute("SELECT * FROM tests ORDER BY created_at DESC")]


@app.get("/api/tests/{test_id}")
def get_test(test_id: int):
    """Test questions for the test interface. Correct answers are not sent."""
    with DB() as conn:
        test = conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
        if test is None:
            raise HTTPException(404, "Test not found")
        questions = [
            {"question_id": r["question_id"], "position": r["position"], "stem": r["stem"],
             "options": json.loads(r["options"]), "selected": r["selected"], "marked": bool(r["marked"]),
             "topic": r["topic"], "difficulty": r["difficulty"]}
            for r in conn.execute(
                "SELECT tq.*, q.stem, q.options, q.difficulty, t.name AS topic FROM test_questions tq"
                " JOIN questions q ON q.id = tq.question_id JOIN topics t ON t.id = q.topic_id"
                " WHERE tq.test_id = ? ORDER BY tq.position",
                (test_id,),
            )
        ]
        return {"test": _test_meta(conn, test), "questions": questions}


class ProgressIn(BaseModel):
    selected: Optional[int] = None      # set when the student picks an option
    add_time_sec: float = Field(0, ge=0, le=3600)  # active time spent since the last save
    marked: Optional[bool] = None
    current_index: Optional[int] = None
    elapsed_sec: Optional[int] = None


@app.post("/api/tests/{test_id}/questions/{question_id}")
def save_progress(test_id: int, question_id: int, body: ProgressIn):
    """Records every action: answer selection/changes, time on question, mark, position.
    Any save for a question means it was displayed, so it is marked seen."""
    with DB() as conn:
        test = conn.execute("SELECT status FROM tests WHERE id = ?", (test_id,)).fetchone()
        if test is None or test["status"] != "in_progress":
            raise HTTPException(400, "This test has already ended.")
        row = conn.execute(
            "SELECT tq.*, q.correct_index FROM test_questions tq JOIN questions q ON q.id = tq.question_id"
            " WHERE tq.test_id = ? AND tq.question_id = ?",
            (test_id, question_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Question not in this test")

        selected = row["selected"]
        changes = row["answer_changes"]
        changed_c2i = row["changed_correct_to_incorrect"]
        if body.selected is not None and body.selected != selected:
            if selected is not None:
                changes += 1
                if selected == row["correct_index"] and body.selected != row["correct_index"]:
                    changed_c2i = 1
            selected = body.selected
        marked = row["marked"] if body.marked is None else int(body.marked)
        conn.execute(
            "UPDATE test_questions SET selected = ?, answer_changes = ?, changed_correct_to_incorrect = ?, marked = ?,"
            " time_spent_sec = time_spent_sec + ?, seen = 1 WHERE id = ?",
            (selected, changes, changed_c2i, marked, body.add_time_sec, row["id"]),
        )
        if body.current_index is not None or body.elapsed_sec is not None:
            conn.execute(
                "UPDATE tests SET current_index = COALESCE(?, current_index), elapsed_sec = COALESCE(?, elapsed_sec)"
                " WHERE id = ?",
                (body.current_index, body.elapsed_sec, test_id),
            )
        return {"ok": True}


class EndIn(BaseModel):
    elapsed_sec: Optional[int] = None


@app.post("/api/tests/{test_id}/end")
def end_test(test_id: int, body: EndIn):
    with DB() as conn:
        if body.elapsed_sec is not None:
            conn.execute("UPDATE tests SET elapsed_sec = ? WHERE id = ?", (body.elapsed_sec, test_id))
        try:
            return engine.end_test(conn, test_id)
        except ValueError as e:
            raise HTTPException(404, str(e))


@app.get("/api/tests/{test_id}/results")
def test_results(test_id: int):
    with DB() as conn:
        test = conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
        if test is None:
            raise HTTPException(404, "Test not found")
        if test["status"] != "ended":
            raise HTTPException(400, "This test is still in progress.")
        items = []
        for r in conn.execute(
            "SELECT tq.*, q.stem, q.options, q.correct_index, q.explanation, q.avg_time_sec, q.difficulty, q.option_pcts,"
            " t.name AS topic FROM test_questions tq JOIN questions q ON q.id = tq.question_id"
            " JOIN topics t ON t.id = q.topic_id WHERE tq.test_id = ? ORDER BY tq.position",
            (test_id,),
        ):
            e = conn.execute(
                "SELECT * FROM weakness_events WHERE test_id = ? AND question_id = ?", (test_id, r["question_id"])
            ).fetchone()
            weakness = None if e is None else {
                "w": e["weight"], "r": e["r"], "p": e["p"], "u": e["u"], "t": e["t"], "applied": bool(e["applied"]),
            }
            items.append({
                "position": r["position"], "question_id": r["question_id"], "topic": r["topic"],
                "difficulty": r["difficulty"], "stem": r["stem"], "options": json.loads(r["options"]),
                "selected": r["selected"], "correct_index": r["correct_index"], "is_correct": bool(r["is_correct"]),
                "omitted": bool(r["omitted"]), "seen": bool(r["seen"]), "marked": bool(r["marked"]),
                "answer_changes": r["answer_changes"],
                "time_spent_sec": round(r["time_spent_sec"]), "avg_time_sec": r["avg_time_sec"],
                "explanation": r["explanation"], "weakness": weakness,
                "option_pcts": json.loads(r["option_pcts"]) if r["option_pcts"] else None,
            })
        out = {"test": _test_meta(conn, test), "items": items}
        if test["type"] == "practice" and test["task_id"]:
            task = conn.execute("SELECT status FROM planner_tasks WHERE id = ?", (test["task_id"],)).fetchone()
            out["practice"] = {
                "task_id": test["task_id"], "pass_pct": PRACTICE_PASS_PCT,
                "passed": engine.practice_passed(test["correct"], test["total"]),
                "task_done": task is not None and task["status"] == "done",
            }
        return out


# ------------------------------------------------------------------ Popup

@app.get("/api/popup")
def get_popup():
    """Called on every non-test screen. Returns the weak-area popup created after the last test, if any."""
    with DB() as conn:
        popup = engine.pending_popup(conn)
        return {"popup": engine.popup_payload(conn, popup) if popup else None}


class Assignment(BaseModel):
    topic_id: int
    action: str                         # 'unclear' -> revision task, 'practice' -> practice task
    scheduled_at: int                   # epoch ms
    remind_before_min: int = Field(30, ge=0, le=10080)


class PopupSubmitIn(BaseModel):
    assignments: List[Assignment]


@app.post("/api/popup/{popup_id}/submit")
def submit_popup(popup_id: int, body: PopupSubmitIn):
    with DB() as conn:
        try:
            task_ids = engine.submit_popup(conn, popup_id, [a.model_dump() for a in body.assignments])
        except ValueError as e:
            bad_request(e)
        return {"task_ids": task_ids}


@app.post("/api/popup/{popup_id}/dismiss")
def dismiss_popup(popup_id: int):
    with DB() as conn:
        engine.dismiss_popup(conn, popup_id)
        return {"ok": True}


# ------------------------------------------------------------------ Settings

@app.get("/api/settings")
def get_settings():
    """Weakness formula constants and the weak-topic threshold (read-only; edit in app/db.py and restart)."""
    with DB() as conn:
        s = engine.get_settings(conn)
        return {"formula": "W = R × (base + w_p·p) + w_u·u + w_t·t", "constants": s["weights"],
                "weak_threshold": s["threshold"]}


# ------------------------------------------------------------------ Planner + tasks

def _task_out(conn, t):
    out = dict(t)
    out["topic"] = conn.execute("SELECT name FROM topics WHERE id = ?", (t["topic_id"],)).fetchone()["name"]
    if t["type"] == "revision":
        out["progress"] = engine.revision_progress(conn, t["id"])
    else:
        test = conn.execute("SELECT id, status, correct, total FROM tests WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                            (t["id"],)).fetchone()
        out["test"] = dict(test) if test else None
        out["attempts"] = conn.execute(
            "SELECT COUNT(*) FROM tests WHERE task_id = ? AND status = 'ended'", (t["id"],)).fetchone()[0]
        out["pass_pct"] = PRACTICE_PASS_PCT
    return out


@app.get("/api/planner")
def planner(start: int, end: int):
    with DB() as conn:
        return [
            _task_out(conn, t)
            for t in conn.execute(
                "SELECT * FROM planner_tasks WHERE scheduled_at >= ? AND scheduled_at < ? ORDER BY scheduled_at",
                (start, end),
            )
        ]


def _task_items(conn, task_id, kind):
    """Lectures or sections in a revision task, each with the struggled questions it covers."""
    table = "lectures" if kind == "lecture" else "etextbook_sections"
    out = []
    for r in conn.execute(
        f"SELECT x.*, ti.question_ids, (c.id IS NOT NULL) AS completed FROM task_items ti JOIN {table} x ON x.id = ti.item_id"
        " LEFT JOIN completions c ON c.task_id = ti.task_id AND c.item_type = ti.item_type AND c.item_id = ti.item_id"
        " WHERE ti.task_id = ? AND ti.item_type = ? ORDER BY x.sort",
        (task_id, kind),
    ):
        item = dict(r)
        qids = json.loads(item.pop("question_ids"))
        item["completed"] = bool(item["completed"])
        item["questions"] = [
            {"id": q["id"], "stem": q["stem"]}
            for q in conn.execute(
                f"SELECT id, stem FROM questions WHERE id IN ({','.join('?' * len(qids))}) ORDER BY id", qids)
        ] if qids else []
        out.append(item)
    return out


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    with DB() as conn:
        t = conn.execute("SELECT * FROM planner_tasks WHERE id = ?", (task_id,)).fetchone()
        if t is None:
            raise HTTPException(404, "Task not found")
        out = _task_out(conn, t)
        if t["type"] == "revision":
            out["lectures"] = _task_items(conn, task_id, "lecture")
            out["sections"] = _task_items(conn, task_id, "section")
        return out


@app.post("/api/tasks/{task_id}/practice/start")
def start_practice(task_id: int):
    with DB() as conn:
        try:
            return {"test_id": engine.start_practice(conn, task_id)}
        except ValueError as e:
            bad_request(e)


# ------------------------------------------------------------------ Lectures + e-textbook

def _study_items(conn, kind: str, topic_id: Optional[int]):
    table = "lectures" if kind == "lecture" else "etextbook_sections"
    topics = conn.execute(
        "SELECT id, name FROM topics" + (" WHERE id = ?" if topic_id else "") + " ORDER BY sort",
        (topic_id,) if topic_id else (),
    ).fetchall()
    out = []
    for t in topics:
        task = conn.execute(
            "SELECT id FROM planner_tasks WHERE topic_id = ? AND type = 'revision' AND status = 'pending'"
            " ORDER BY scheduled_at LIMIT 1",
            (t["id"],),
        ).fetchone()
        done, in_task = set(), set()
        if task:
            done = {r["item_id"] for r in conn.execute(
                "SELECT item_id FROM completions WHERE task_id = ? AND item_type = ?", (task["id"], kind))}
            in_task = {r["item_id"] for r in conn.execute(
                "SELECT item_id FROM task_items WHERE task_id = ? AND item_type = ?", (task["id"], kind))}
        items = [dict(r, completed=r["id"] in done, in_task=r["id"] in in_task) for r in conn.execute(
            f"SELECT * FROM {table} WHERE topic_id = ? ORDER BY sort", (t["id"],))]
        out.append({"topic_id": t["id"], "topic": t["name"], "revision_task_id": task["id"] if task else None,
                    "items": items})
    return out


@app.get("/api/lectures")
def lectures(topic_id: Optional[int] = None):
    with DB() as conn:
        return _study_items(conn, "lecture", topic_id)


@app.get("/api/etextbook")
def etextbook(topic_id: Optional[int] = None):
    with DB() as conn:
        return _study_items(conn, "section", topic_id)


class CompleteIn(BaseModel):
    task_id: int
    item_type: str                      # 'lecture' or 'section'
    item_id: int


@app.post("/api/study-items/complete")
def complete_item(body: CompleteIn):
    if body.item_type not in ("lecture", "section"):
        raise HTTPException(400, "item_type must be 'lecture' or 'section'")
    with DB() as conn:
        try:
            return engine.mark_item_complete(conn, body.task_id, body.item_type, body.item_id)
        except ValueError as e:
            bad_request(e)


# ------------------------------------------------------------------ Reminders

@app.get("/api/reminders")
def reminders():
    """Pending tasks whose reminder time has passed."""
    with DB() as conn:
        return [
            _task_out(conn, t)
            for t in conn.execute(
                "SELECT * FROM planner_tasks WHERE status = 'pending' AND remind_at <= ? ORDER BY remind_at",
                (now_ms(),),
            )
        ]


@app.post("/api/reminders/{task_id}/seen")
def reminder_seen(task_id: int):
    with DB() as conn:
        conn.execute("UPDATE planner_tasks SET reminder_seen = 1 WHERE id = ?", (task_id,))
        return {"ok": True}


# ------------------------------------------------------------------ Reset

@app.post("/api/reset")
def reset():
    """Clear the student's session data (tests, answers, weak-area scores, pop-ups, planner tasks, completions).
    Questions, lectures, e-textbook sections and settings are kept."""
    with DB() as conn:
        reset_session(conn)
        return {"ok": True}


# ------------------------------------------------------------------ Debug (API docs only)

@app.get("/api/weakness")
def weakness():
    """Current weakness score per topic (for checking the engine while building)."""
    with DB() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT t.name, tw.* FROM topic_weakness tw JOIN topics t ON t.id = tw.topic_id ORDER BY t.sort")]
