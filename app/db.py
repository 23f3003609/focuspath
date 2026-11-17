"""SQLite database: connection, schema and seed data."""
import json
import os
import random
import sqlite3
import time

from .content import TOPICS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("MCAT_DB", os.path.join(BASE_DIR, "prototype.db"))

# Question weakness score (0 to 1):
#     W = R × (base + w_p·p) + w_u·u + w_t·t
#   R = result: incorrect 1, seen but not answered 0.7, correct 0
#   p = share of students who answered the question correctly (0–1)
#   u = how unusual the wrong pick was: 1 − (chosen option % ÷ most popular wrong option %); 0 if correct/unanswered
#   t = slowness: (time ÷ students' average time) − 1, kept between 0 and 1
FORMULA = {
    "R": {"incorrect": 1.0, "omitted": 0.7, "correct": 0.0},
    "base": 0.5,
    "w_p": 0.2,
    "w_u": 0.2,
    "w_t": 0.1,
}

# Topic weakness = average W over the topic's seen questions since its last reset.
WEAK_THRESHOLD = 0.45   # weak when the average W reaches this
MIN_SEEN = 5            # ...and at least this many questions have been seen

# A practice task completes only when a practice test scores at least this % (7/10 on 10 questions).
PRACTICE_PASS_PCT = 70

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sort INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    pool TEXT NOT NULL CHECK (pool IN ('qbank', 'foundation')),
    difficulty TEXT NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard')),
    stem TEXT NOT NULL,
    options TEXT NOT NULL,              -- JSON list
    correct_index INTEGER NOT NULL,
    explanation TEXT NOT NULL,
    avg_time_sec INTEGER NOT NULL,
    lecture_id INTEGER REFERENCES lectures(id),          -- QBank only: lecture that teaches this concept
    section_id INTEGER REFERENCES etextbook_sections(id),-- QBank only: matching e-textbook section
    option_pcts TEXT                                     -- QBank only: JSON list, % of students who picked each option
);
CREATE TABLE IF NOT EXISTS user_question_status (
    question_id INTEGER PRIMARY KEY REFERENCES questions(id),
    status TEXT NOT NULL DEFAULT 'unused' CHECK (status IN ('unused', 'correct', 'incorrect', 'omitted'))
);
CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('custom', 'practice')),
    task_id INTEGER,                    -- practice tests belong to a planner task
    topic_ids TEXT NOT NULL,            -- JSON list
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'ended')),
    created_at INTEGER NOT NULL,
    ended_at INTEGER,
    total INTEGER NOT NULL,
    correct INTEGER,
    current_index INTEGER NOT NULL DEFAULT 0,
    elapsed_sec INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS test_questions (
    id INTEGER PRIMARY KEY,
    test_id INTEGER NOT NULL REFERENCES tests(id),
    question_id INTEGER NOT NULL REFERENCES questions(id),
    position INTEGER NOT NULL,
    selected INTEGER,
    is_correct INTEGER,
    time_spent_sec REAL NOT NULL DEFAULT 0,
    answer_changes INTEGER NOT NULL DEFAULT 0,
    changed_correct_to_incorrect INTEGER NOT NULL DEFAULT 0,
    marked INTEGER NOT NULL DEFAULT 0,
    omitted INTEGER NOT NULL DEFAULT 0,
    seen INTEGER NOT NULL DEFAULT 0,    -- 1 once the question was displayed in the test interface
    UNIQUE (test_id, question_id)
);
CREATE TABLE IF NOT EXISTS weakness_events (
    id INTEGER PRIMARY KEY,
    test_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,           -- result: incorrect / omitted / correct (one row per seen question)
    weight REAL NOT NULL,               -- the question's weakness score W (0–1)
    applied INTEGER NOT NULL,           -- 0 when the topic was In progress (history only)
    created_at INTEGER NOT NULL,
    r REAL, p REAL, u REAL, t REAL      -- the formula inputs, kept so Results can show the breakdown
);
CREATE TABLE IF NOT EXISTS topic_weakness (
    topic_id INTEGER PRIMARY KEY REFERENCES topics(id),
    score REAL NOT NULL DEFAULT 0,      -- average W over seen questions since reset_at
    seen_count INTEGER NOT NULL DEFAULT 0,  -- seen questions behind that average
    status TEXT NOT NULL DEFAULT 'normal' CHECK (status IN ('normal', 'weak', 'in_progress')),
    reset_at INTEGER NOT NULL DEFAULT 0,
    last_triggered_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    threshold REAL NOT NULL,            -- average W at which a topic is weak
    weights TEXT NOT NULL               -- JSON: formula constants and minimum seen questions
);
CREATE TABLE IF NOT EXISTS popups (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    topic_ids TEXT NOT NULL,            -- JSON list
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'dismissed', 'expired'))
);
CREATE TABLE IF NOT EXISTS planner_tasks (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    type TEXT NOT NULL CHECK (type IN ('revision', 'practice')),
    popup_id INTEGER,
    scheduled_at INTEGER NOT NULL,
    remind_before_min INTEGER NOT NULL,
    remind_at INTEGER NOT NULL,
    reminder_seen INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS lectures (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    sort INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS etextbook_sections (
    id INTEGER PRIMARY KEY,
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    sort INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_items (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES planner_tasks(id),
    item_type TEXT NOT NULL CHECK (item_type IN ('lecture', 'section')),
    item_id INTEGER NOT NULL,
    question_ids TEXT NOT NULL,         -- JSON list: struggled questions this item covers
    UNIQUE (task_id, item_type, item_id)
);
CREATE TABLE IF NOT EXISTS completions (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES planner_tasks(id),
    item_type TEXT NOT NULL CHECK (item_type IN ('lecture', 'section')),
    item_id INTEGER NOT NULL,
    completed_at INTEGER NOT NULL,
    UNIQUE (task_id, item_type, item_id)
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA_VERSION = 2

# Student activity, cleared by "Reset my data". Content tables are never touched.
SESSION_TABLES = ["completions", "task_items", "planner_tasks", "popups", "weakness_events", "test_questions", "tests"]


def init_db() -> None:
    """Create tables and seed content on first run. Rebuilds a database from an older prototype version."""
    conn = get_conn()
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            conn.execute("PRAGMA foreign_keys = OFF")
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        if conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0:
            _seed(conn)
        # db.py is the source of truth for threshold and weights: apply them on every start,
        # so editing the constants above takes effect after a restart (no need to delete the DB).
        conn.execute("UPDATE settings SET threshold = ?, weights = ? WHERE id = 1",
                     (WEAK_THRESHOLD, json.dumps(_formula_settings())))
        conn.commit()
    finally:
        conn.close()


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _migrate(conn: sqlite3.Connection) -> None:
    """Small in-place upgrades that keep existing content."""
    if "seen" not in _columns(conn, "test_questions"):
        conn.execute("ALTER TABLE test_questions ADD COLUMN seen INTEGER NOT NULL DEFAULT 0")
        # Existing rows: treat a question as seen if it was answered or had time recorded.
        conn.execute("UPDATE test_questions SET seen = 1 WHERE selected IS NOT NULL OR time_spent_sec > 0")

    if "option_pcts" not in _columns(conn, "questions"):
        conn.execute("ALTER TABLE questions ADD COLUMN option_pcts TEXT")
    for q in conn.execute(
        "SELECT id, stem, options, correct_index, difficulty FROM questions WHERE pool = 'qbank' AND option_pcts IS NULL"
    ).fetchall():
        pcts = demo_option_pcts(q["stem"], len(json.loads(q["options"])), q["correct_index"], q["difficulty"])
        conn.execute("UPDATE questions SET option_pcts = ? WHERE id = ?", (json.dumps(pcts), q["id"]))

    if "seen_count" not in _columns(conn, "topic_weakness"):
        conn.execute("ALTER TABLE topic_weakness ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 0")

    if "p" not in _columns(conn, "weakness_events"):
        for col in ("r", "p", "u", "t"):
            conn.execute(f"ALTER TABLE weakness_events ADD COLUMN {col} REAL")
        # Scores from the old points system can't be mixed with the new 0–1 scores: clear the session.
        reset_session(conn)


def _formula_settings() -> dict:
    return dict(FORMULA, min_seen=MIN_SEEN)


def demo_option_pcts(stem: str, n_options: int, correct_index: int, difficulty: str) -> list:
    """Demo answer statistics (not real data): % of students picking each option, summing to 100.
    Deterministic per question, so every run gets the same numbers."""
    rng = random.Random(stem)
    correct = rng.randint(55, 82) if difficulty == "medium" else rng.randint(28, 55)
    wrong = [i for i in range(n_options) if i != correct_index]
    raw = [rng.random() ** 2 + 0.05 for _ in wrong]       # squared: usually one strong trap, some rare picks
    rest = 100 - correct
    shares = [max(2, round(rest * x / sum(raw))) for x in raw]
    shares[shares.index(max(shares))] += rest - sum(shares)   # make the total exactly 100
    pcts = [0] * n_options
    pcts[correct_index] = correct
    for i, share in zip(wrong, shares):
        pcts[i] = share
    return pcts


def reset_session(conn: sqlite3.Connection) -> None:
    """Delete the student's activity (tests, answers, weak-area scores, pop-ups, planner tasks,
    completions) and keep all prototype content (questions, lectures, e-textbook, settings)."""
    for table in SESSION_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.execute("UPDATE user_question_status SET status = 'unused'")
    conn.execute("UPDATE topic_weakness SET score = 0, seen_count = 0, status = 'normal', reset_at = 0, last_triggered_at = NULL")


def _seed(conn: sqlite3.Connection) -> None:
    for t_index, topic in enumerate(TOPICS):
        cur = conn.execute("INSERT INTO topics (name, sort) VALUES (?, ?)", (topic["name"], t_index))
        topic_id = cur.lastrowid
        conn.execute("INSERT INTO topic_weakness (topic_id) VALUES (?)", (topic_id,))

        lecture_ids, section_ids = [], []
        for i, (title, summary, minutes) in enumerate(topic["lectures"]):
            cur = conn.execute(
                "INSERT INTO lectures (topic_id, title, summary, duration_min, sort) VALUES (?, ?, ?, ?, ?)",
                (topic_id, title, summary, minutes, i),
            )
            lecture_ids.append(cur.lastrowid)
        for i, (title, content) in enumerate(topic["sections"]):
            cur = conn.execute(
                "INSERT INTO etextbook_sections (topic_id, title, content, sort) VALUES (?, ?, ?, ?)",
                (topic_id, title, content, i),
            )
            section_ids.append(cur.lastrowid)

        for stem, options, correct, explanation, avg, difficulty, concept in topic["qbank"]:
            cur = conn.execute(
                "INSERT INTO questions (topic_id, pool, difficulty, stem, options, correct_index, explanation, avg_time_sec,"
                " lecture_id, section_id, option_pcts) VALUES (?, 'qbank', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (topic_id, difficulty, stem, json.dumps(options), correct, explanation, avg,
                 lecture_ids[concept], section_ids[concept],
                 json.dumps(demo_option_pcts(stem, len(options), correct, difficulty))),
            )
            conn.execute("INSERT INTO user_question_status (question_id) VALUES (?)", (cur.lastrowid,))

        for level in ("easy", "medium"):
            for stem, options, correct, explanation, avg in topic[level]:
                conn.execute(
                    "INSERT INTO questions (topic_id, pool, difficulty, stem, options, correct_index, explanation, avg_time_sec)"
                    " VALUES (?, 'foundation', ?, ?, ?, ?, ?, ?)",
                    (topic_id, level, stem, json.dumps(options), correct, explanation, avg),
                )

    conn.execute(
        "INSERT INTO settings (id, threshold, weights) VALUES (1, ?, ?)",
        (WEAK_THRESHOLD, json.dumps(_formula_settings())),
    )
