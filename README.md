# UWorld MCAT – Weak Foundations Prototype

Detects the topics a student struggles with during tests, then helps them rebuild the basics with
lectures, e-textbook sections or building-block practice tests, scheduled in the Study Planner with reminders.

Subject: MCAT General Chemistry (6 topics). Stack: FastAPI + SQLite + plain HTML/CSS/JS (no build step).

## Run

```bash
cd mcat-weakness-prototype
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. API docs: http://127.0.0.1:8000/docs.
The database (`prototype.db`) is created and seeded on first start. A database from an older version of the
prototype is rebuilt automatically.

**Reset my data** (bottom of the left nav) clears the student's session (tests, answers, weak-area scores,
pop-ups, planner tasks, completions, reminders) and keeps all content (questions, lectures, e-textbook, settings).
Use it between demos. Deleting `prototype.db` resets everything, including content.

## Content

| Per topic (6 topics) | Count |
| --- | --- |
| QBank questions (exam level, used by Create Test) | 15, each with an average time |
| Building-block questions (practice tests only) | 10: 5 easy + 5 medium |
| Lectures | 3 |
| E-textbook sections | 3 (one per lecture) |

Every QBank question is linked to the lecture and e-textbook section that teach its concept
(`questions.lecture_id`, `questions.section_id`; set in `app/content.py`).

## Project layout

```
app/
  main.py      FastAPI app and all API routes
  engine.py    Weakness engine: scoring, trigger, pop-up, flush, task completion
  db.py        SQLite schema, seeding, default weights, session reset
  content.py   MCAT chemistry questions (with lecture links), lectures, e-textbook sections
static/
  index.html   App shell (left nav)
  styles.css   UWorld-style theme
  app.js       Pages: Study Planner, Create Test, test interface, Results,
               Previous Tests, Lectures, E-textbook, pop-up, reminders
```

## How the weakness engine works

1. **During a test** the interface records, per question: time spent (paused while the tab is hidden),
   selected answer, answer changes, correct → incorrect changes, Mark, and whether it was **seen**
   (displayed on screen; `test_questions.seen`). Unanswered questions that were never displayed add no points
   and show as "Omitted · not seen" in Results; they are still saved as `omitted` in the question status.
2. **On End Block** every seen question gets a weakness score **W** from 0 to 1 (unseen questions don't count):

   **W = R × (0.5 + 0.2·p) + 0.2·u + 0.1·t**

   | Symbol | Meaning | Value |
   | --- | --- | --- |
   | R | Result | Incorrect 1 · Seen but not answered 0.7 · Correct 0 |
   | p | Share of students who answered it correctly | `option_pcts[correct] ÷ 100` |
   | u | How unusual the wrong pick was | 1 − (chosen option % ÷ most popular wrong option %); 0 if correct or unanswered |
   | t | Slowness | (time ÷ `avg_time_sec`) − 1, kept between 0 and 1 |

   Example: options A 10%, **B 30% (correct)**, C 40%, D 20%; average 60 s. Picking A in 90 s:
   R = 1, p = 0.3, u = 1 − 10/40 = 0.75, t = 0.5 → W = 0.56 + 0.15 + 0.05 = **0.76**.
   Picking the common trap C instead gives 0.61; answering B correctly in 90 s gives 0.05.

   Each W is stored in `weakness_events` (one row per seen question, with r, p, u, t).
   Answer statistics are in `questions.option_pcts` (**demo values**, generated per question, not real UWorld data).
3. **Topic weakness = average W** of the topic's seen questions since its last reset (`topic_weakness.score`,
   `seen_count`). A topic is **weak when the average is 0.45 or more and at least 5 questions were seen**.
   Right after a test ends, weak topics create a pop-up. It shows on the next non-test screen (usually Results),
   never inside the test. If the student closes it, it comes back only after their next test, if a topic is still weak.
   The formula constants, threshold and minimum are in `app/db.py` (`FORMULA`, `WEAK_THRESHOLD`, `MIN_SEEN`)
   and are applied on every server start.
4. **Pop-up:** shows each weak topic's average W. Nothing is preselected: the student uses each action's
   dropdown to choose topics. A topic chosen in one action disappears from the other's dropdown. A topic in one action disappears from the other's dropdown.
5. **Schedule:** date, time and reminder per task → tasks added to the Study Planner. The student stays on the
   Results screen; a toast offers *Open Study Planner*.
6. **Flush:** assigned topics reset to 0 and become *In progress*. While In progress, new answers on that topic
   are stored as history only and the topic never appears in a pop-up.
7. **Completion:**
   - Revision task: contains **only** the lectures and e-textbook sections linked to the questions the student
     struggled with on that topic (any seen question with W > 0 since the topic's last reset), stored in `task_items`.
     The task page shows numbered steps, each a lecture followed by its matching e-textbook section.
     Each item lists the questions it covers. Mark every item complete. A *Check your understanding* pop-up
     then offers a knowledge-check test; for the prototype, *Create test* only shows a success message.
   - Practice task: 10-question test (5 easy, then 5 medium). The task completes only when an attempt scores
     **70% or more** (7/10; `PRACTICE_PASS_PCT` in `app/db.py`). Below that, the task stays pending, the topic stays
     In progress, and the student retakes it (same 10 questions, new order within each level). Practice answers
     never add weakness events.
   - Then the topic returns to *Normal* and scoring restarts from 0.

## Demo script (about 3 minutes)

A topic is weak when its average W is 0.45 or more over at least 5 seen questions. Each wrong answer scores about
0.5–1.0, so roughly 4 wrong out of 6 seen on a topic triggers the pop-up.

0. Click **Reset my data** to start clean.
1. **Create Test** → untick General Chemistry → tick *Acids & Bases* only → 6 questions → Generate Test.
2. Answer at least 4 questions incorrectly (spend a while on one to show slowness). End Block. Results shows each
   question's W with its R, p, u, t; select a row to see how other students answered.
   Results show what was recorded for each question, and the weak-area pop-up opens.
3. Move one topic to *Need more practice* → Continue → set times → **Add to Study Planner**.
4. **Study Planner** shows both tasks. Open the revision task: it shows only the lectures and sections for the
   concepts you missed, with the questions each one covers. Mark every item complete:
   the *Check your understanding* pop-up appears → *Create test* → success message.
5. Start the practice task: 5 easy, then 5 medium questions. Score below 70% to show the Retake flow,
   then 7/10 or more to complete it.
6. To demo a reminder quickly, schedule a task a few minutes ahead with "At start time" or "10 min before".
   The bell and a toast appear when it's due (checked every 20 seconds).

Helpful while building: `GET /api/weakness` shows every topic's current score and status.

## Notes

- Single user, no login. Reminders are in-app (bell + toast) plus browser notifications if allowed.
- Logo: place the official UWorld logo at `static/img/uworld-logo.png`; it appears next to "UWorld" in the left nav
  (hidden while the file is missing). See `static/img/README.txt`.
