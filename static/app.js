/* UWorld MCAT – Weak Foundations prototype (vanilla JS, hash routing) */
(function () {
  "use strict";

  // ------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const view = $("#view");
  const LETTERS = "ABCDEFGH";
  const DAY = 24 * 60 * 60 * 1000;
  const REMIND_OPTIONS = [
    { v: 0, l: "At start time" }, { v: 10, l: "10 min before" }, { v: 30, l: "30 min before" },
    { v: 60, l: "1 hour before" }, { v: 1440, l: "1 day before" },
  ];

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: opts.body ? { "Content-Type": "application/json" } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) {
      const detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : "Something went wrong. Please try again.");
    }
    return data;
  }
  const pad = (n) => String(n).padStart(2, "0");
  const startOfDay = (ms) => { const d = new Date(ms); d.setHours(0, 0, 0, 0); return d.getTime(); };
  const fmtTime = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const fmtDay = (ms) => new Date(ms).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const fmtDateTime = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const fmtClock = (sec) => { sec = Math.max(0, Math.floor(sec)); return `${pad(Math.floor(sec / 3600))}:${pad(Math.floor(sec / 60) % 60)}:${pad(sec % 60)}`; };
  const toDateInput = (ms) => { const d = new Date(ms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  const remindLabel = (v) => (REMIND_OPTIONS.find((o) => o.v === v) || { l: `${v} min before` }).l;
  const taskTitle = (t) => t.type === "revision"
    ? `${t.topic}: Lectures + E-textbook`
    : `${t.topic}: Building-block practice (10 questions)`;

  function toast(title, text, actionLabel, onAction) {
    const el = document.createElement("div");
    el.className = "toast";
    el.innerHTML = `<b>${esc(title)}</b>${text ? `<div>${esc(text)}</div>` : ""}` +
      `<div class="row">${actionLabel ? `<button class="btn small" data-act="go">${esc(actionLabel)}</button>` : ""}` +
      `<button class="btn small" data-act="close">Dismiss</button></div>`;
    el.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]");
      if (!act) return;
      if (act.dataset.act === "go" && onAction) onAction();
      el.remove();
    });
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 9000);
  }

  // ------------------------------------------------------------ modal
  let modalOpen = false;
  function openModal(html, { onClose } = {}) {
    const root = $("#modalRoot");
    root.innerHTML = `<div class="overlay"><div class="modal" role="dialog" aria-modal="true">${html}</div></div>`;
    modalOpen = true;
    const close = () => { root.innerHTML = ""; modalOpen = false; document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") { onClose ? onClose(close) : close(); } };
    document.addEventListener("keydown", onKey);
    const modal = $(".modal", root);
    const first = modal.querySelector("button, input, select");
    if (first) first.focus();
    return { modal, close };
  }

  // ------------------------------------------------------------ router
  let cleanup = null;
  const titles = { planner: "Study Planner", create: "Create Test", tests: "Previous Tests", lectures: "Lectures",
    etextbook: "E-textbook", results: "Test Results", task: "Revision Task" };

  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, "") || "planner";
    const [path, query] = raw.split("?");
    const parts = path.split("/");
    return { name: parts[0], id: parts[1] ? Number(parts[1]) : null, params: new URLSearchParams(query || "") };
  }

  async function route() {
    if (cleanup) { const c = cleanup; cleanup = null; await c(); }
    const r = parseHash();
    const isTest = r.name === "test";
    document.body.classList.toggle("test-mode", isTest);
    $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === r.name ||
      (r.name === "results" && a.dataset.nav === "tests") || (r.name === "task" && a.dataset.nav === "planner")));
    $("#pageTitle").textContent = titles[r.name] || "Study Planner";
    $("#bellPanel").hidden = true;
    view.innerHTML = `<p class="muted">Loading…</p>`;
    try {
      if (r.name === "create") await renderCreate();
      else if (r.name === "tests") await renderTests();
      else if (r.name === "test") await renderTest(r.id);
      else if (r.name === "results") await renderResults(r.id);
      else if (r.name === "lectures") await renderStudy("lecture", r.params.get("topic"));
      else if (r.name === "etextbook") await renderStudy("section", r.params.get("topic"));
      else if (r.name === "task") await renderTask(r.id);
      else await renderPlanner();
    } catch (e) {
      view.innerHTML = `<div class="card"><p class="error-text">${esc(e.message)}</p></div>`;
    }
    if (!isTest) { checkPopup(); refreshReminders(); }
  }
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------ Study Planner
  let weekOffset = 0;

  async function renderPlanner() {
    const today = startOfDay(Date.now());
    const dow = (new Date(today).getDay() + 6) % 7;               // Monday = 0
    const weekStart = today - dow * DAY + weekOffset * 7 * DAY;
    const weekEnd = weekStart + 7 * DAY;
    const tasks = await api(`/api/planner?start=${weekStart}&end=${weekEnd}`);

    let days = "";
    for (let i = 0; i < 7; i++) {
      const dayStart = weekStart + i * DAY;
      const dayTasks = tasks.filter((t) => t.scheduled_at >= dayStart && t.scheduled_at < dayStart + DAY);
      const isToday = dayStart === today;
      days += `<section class="day${isToday ? " is-today" : ""}">
        <div class="day-head">${esc(fmtDay(dayStart))}${isToday ? ' <span class="badge today">Today</span>' : ""}
          <span class="spacer"></span><span class="muted" style="font-weight:400">${dayTasks.length ? dayTasks.length + (dayTasks.length === 1 ? " task" : " tasks") : ""}</span></div>
        <div class="day-body">${dayTasks.length ? dayTasks.map(taskRow).join("") : '<p class="empty">No tasks planned.</p>'}</div>
      </section>`;
    }

    view.innerHTML = `
      <div class="planner-head">
        <button class="btn small" data-week="-1" aria-label="Previous week">‹ Prev</button>
        <button class="btn small" data-week="0">This week</button>
        <button class="btn small" data-week="1" aria-label="Next week">Next ›</button>
        <span class="range">${esc(fmtDay(weekStart))} – ${esc(fmtDay(weekEnd - DAY))}</span>
      </div>
      ${days}`;

    $$("[data-week]", view).forEach((b) => b.addEventListener("click", () => {
      const v = Number(b.dataset.week);
      weekOffset = v === 0 ? 0 : weekOffset + v;
      renderPlanner();
    }));
    $$("[data-practice]", view).forEach((b) => b.addEventListener("click", () => openPractice(Number(b.dataset.practice))));
  }

  function taskRow(t) {
    const done = t.status === "done";
    let status = "";
    let action = "";
    if (t.type === "revision") {
      const p = t.progress;
      const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
      status = `<span>${p.done}/${p.total} items complete</span><span class="progress-bar"><span style="width:${pct}%"></span></span>`;
      action = `<a class="btn small${done ? "" : " primary"}" href="#/task/${t.id}">${done ? "Review" : "Open"}</a>`;
    } else {
      const test = t.test;
      const attempt = t.attempts ? ` · attempt ${t.attempts + (test && test.status === "in_progress" ? 1 : 0)}` : "";
      if (done) { status = `<span>Passed ${test.correct}/${test.total}</span>`; action = `<a class="btn small" href="#/results/${test.id}">Results</a>`; }
      else if (!test) { status = `<span>Not started · pass with ${t.pass_pct}%</span>`; action = `<button class="btn small primary" data-practice="${t.id}">Start</button>`; }
      else if (test.status === "in_progress") { status = `<span>In progress${attempt}</span>`; action = `<button class="btn small primary" data-practice="${t.id}">Resume</button>`; }
      else {
        status = `<span class="needs-retake">Last score ${test.correct}/${test.total} · needs ${t.pass_pct}%</span>`;
        action = `<button class="btn small primary" data-practice="${t.id}">Retake</button>`;
      }
    }
    return `<div class="task${done ? " is-done" : ""}">
      <div class="task-time">${esc(fmtTime(t.scheduled_at))}</div>
      <div>
        <div class="task-meta"><span class="badge ${t.type}">${t.type === "revision" ? "Revision" : "Practice"}</span>${done ? '<span class="badge done">Done</span>' : ""}</div>
        <div class="task-title">${esc(taskTitle(t))}</div>
        <div class="task-meta">${status}<span>🔔 ${esc(remindLabel(t.remind_before_min))}</span></div>
      </div>
      <div>${action}</div>
    </div>`;
  }

  async function openPractice(taskId) {
    try {
      const { test_id } = await api(`/api/tasks/${taskId}/practice/start`, { method: "POST" });
      location.hash = `#/test/${test_id}`;
    } catch (e) { toast("Couldn't start practice", e.message); }
  }

  // ------------------------------------------------------------ Create Test
  const STATUS_LABELS = { unused: "Unused", incorrect: "Incorrect", omitted: "Omitted", correct: "Correct" };

  async function renderCreate() {
    const state = { statuses: new Set(["unused"]), topics: null, data: null };

    view.innerHTML = `
      <div class="card"><h2>Test mode</h2><span class="badge practice">Timed</span>
        <span class="muted" style="margin-left:8px">Explanations appear on the results screen.</span></div>
      <div class="card"><h2>Question mode</h2><div class="mode-list" id="modes"></div></div>
      <div class="card"><h2>Topics</h2>
        <div class="subject-head"><label class="check"><input type="checkbox" id="allTopics"> General Chemistry</label></div>
        <div class="topic-list" id="topics"></div></div>
      <div class="card"><h2>Number of questions</h2>
        <div class="row"><input type="number" class="qcount" id="qcount" min="1" value="10" aria-label="Number of questions">
        <span class="muted" id="maxNote"></span></div>
        <p class="error-text" id="createErr" hidden></p>
        <div style="margin-top:16px"><button class="btn primary" id="genBtn">Generate Test</button></div></div>`;

    async function load() {
      state.data = await api(`/api/qbank/counts?statuses=${[...state.statuses].join(",")}`);
      if (state.topics === null) state.topics = new Set(state.data.topics.filter((t) => t.count > 0).map((t) => t.id));
      state.data.topics.forEach((t) => { if (t.count === 0) state.topics.delete(t.id); });
      draw();
    }

    function draw() {
      const d = state.data;
      $("#modes").innerHTML = Object.keys(STATUS_LABELS).map((s) => `
        <label class="check"><input type="checkbox" data-status="${s}" ${state.statuses.has(s) ? "checked" : ""}>
        ${STATUS_LABELS[s]} <span class="count">${d.status_counts[s]}</span></label>`).join("");
      $("#topics").innerHTML = d.topics.map((t) => `
        <label class="check${t.count === 0 ? " disabled" : ""}"><input type="checkbox" data-topic="${t.id}"
          ${state.topics.has(t.id) ? "checked" : ""} ${t.count === 0 ? "disabled" : ""}>
        ${esc(t.name)} <span class="count">${t.count}</span></label>`).join("");
      const available = d.topics.filter((t) => t.count > 0);
      $("#allTopics").checked = available.length > 0 && available.every((t) => state.topics.has(t.id));
      $("#allTopics").disabled = available.length === 0;
      const max = maxAllowed();
      $("#maxNote").textContent = max ? `Max allowed: ${max}` : "No questions match. Change question mode or topics.";
      const input = $("#qcount");
      input.max = max;
      if (Number(input.value) > max) input.value = max;
      if (max && Number(input.value) < 1) input.value = Math.min(10, max);
      $("#genBtn").disabled = !max;

      $$("[data-status]").forEach((c) => c.addEventListener("change", () => {
        c.checked ? state.statuses.add(c.dataset.status) : state.statuses.delete(c.dataset.status);
        load();
      }));
      $$("[data-topic]").forEach((c) => c.addEventListener("change", () => {
        const id = Number(c.dataset.topic);
        c.checked ? state.topics.add(id) : state.topics.delete(id);
        draw();
      }));
    }

    function maxAllowed() {
      return state.data.topics.filter((t) => state.topics.has(t.id)).reduce((a, t) => a + t.count, 0);
    }

    $("#allTopics").addEventListener("change", (e) => {
      state.data.topics.forEach((t) => { if (t.count > 0) e.target.checked ? state.topics.add(t.id) : state.topics.delete(t.id); });
      draw();
    });
    $("#genBtn").addEventListener("click", async () => {
      const err = $("#createErr");
      const count = Number($("#qcount").value);
      const max = maxAllowed();
      if (!state.statuses.size) { err.textContent = "Pick at least one question mode."; err.hidden = false; return; }
      if (!state.topics.size) { err.textContent = "Pick at least one topic."; err.hidden = false; return; }
      if (!count || count < 1 || count > max) { err.textContent = `Enter a number between 1 and ${max}.`; err.hidden = false; return; }
      try {
        const { test_id } = await api("/api/tests", { method: "POST",
          body: { statuses: [...state.statuses], topic_ids: [...state.topics], count } });
        location.hash = `#/test/${test_id}`;
      } catch (e) { err.textContent = e.message; err.hidden = false; }
    });
    await load();
  }

  // ------------------------------------------------------------ Test interface
  async function renderTest(testId) {
    const data = await api(`/api/tests/${testId}`);
    if (data.test.status === "ended") { location.hash = `#/results/${testId}`; return; }
    const test = data.test;
    const qs = data.questions;
    let idx = Math.min(test.current_index || 0, qs.length - 1);
    let elapsed = test.elapsed_sec || 0;
    let activeSince = document.hidden ? null : performance.now();
    let ended = false;
    const label = test.type === "practice"
      ? `Practice · ${esc(test.topic_names.join(", "))} · Building blocks`
      : `Custom test · Timed · ${esc(test.topic_names.length === 1 ? test.topic_names[0] : test.topic_names.length + " topics")}`;

    view.innerHTML = `
      <div class="test">
        <div class="test-top">
          <span class="item" id="itemLabel"></span>
          <span class="qid" id="qidLabel"></span>
          <label class="mark" id="markWrap"><input type="checkbox" id="markBox">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 22V4h12l-2 4 2 4H4"/></svg>Mark</label>
          <span class="label">${label}</span>
        </div>
        <nav class="qnav" id="qnav" aria-label="Questions"></nav>
        <section class="qbody" id="qbody"></section>
        <div class="test-bottom">
          <span class="timer" id="timer"></span>
          <div class="center">
            <button class="test-btn" id="prevBtn">‹ Previous</button>
            <button class="test-btn" id="nextBtn">Next ›</button>
          </div>
          <div class="right"><button class="test-btn end" id="endBtn">End Block</button></div>
        </div>
      </div>`;

    function takeTime() {
      if (activeSince === null) return 0;
      const now = performance.now();
      const t = (now - activeSince) / 1000;
      activeSince = document.hidden ? null : now;
      return t;
    }
    function save(extra = {}) {
      const q = qs[idx];
      return api(`/api/tests/${testId}/questions/${q.question_id}`, { method: "POST",
        body: { add_time_sec: Math.min(3600, takeTime()), elapsed_sec: Math.floor(elapsed), ...extra } }).catch(() => {});
    }

    function drawNav() {
      $("#qnav").innerHTML = qs.map((q, i) => `
        <button class="${i === idx ? "current" : ""}${q.selected !== null && q.selected !== undefined ? " answered" : ""}" data-go="${i}"
          aria-label="Question ${i + 1}${q.marked ? ", marked" : ""}" ${i === idx ? 'aria-current="true"' : ""}>
          <span class="dot"></span>${i + 1}
          ${q.marked ? '<svg class="flag" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 22V4h12l-2 4 2 4H4"/></svg>' : ""}
        </button>`).join("");
      $$("[data-go]").forEach((b) => b.addEventListener("click", () => go(Number(b.dataset.go))));
    }
    function drawQuestion() {
      const q = qs[idx];
      $("#itemLabel").textContent = `Item ${idx + 1} of ${qs.length}`;
      $("#qidLabel").textContent = `Question Id: ${q.question_id}`;
      $("#markBox").checked = !!q.marked;
      $("#markWrap").classList.toggle("on", !!q.marked);
      const levelBadge = test.type === "practice"
        ? `<div class="meta"><span class="badge ${q.difficulty}">${q.difficulty === "easy" ? "Building block · Easy" : "Building block · Medium"}</span></div>` : "";
      $("#qbody").innerHTML = `${levelBadge}
        <p class="stem">${esc(q.stem)}</p>
        <div class="options" role="radiogroup" aria-label="Answer choices">
          ${q.options.map((o, i) => `
            <label class="option"><input type="radio" name="opt" value="${i}" ${q.selected === i ? "checked" : ""}>
            <span class="letter">${LETTERS[i]}.</span><span>${esc(o)}</span></label>`).join("")}
        </div>`;
      $$("input[name=opt]").forEach((r) => r.addEventListener("change", () => {
        const val = Number(r.value);
        if (q.selected === val) return;
        q.selected = val;
        save({ selected: val });
        drawNav();
      }));
      $("#prevBtn").disabled = idx === 0;
      $("#nextBtn").disabled = idx === qs.length - 1;
      $("#qbody").scrollTop = 0;
      drawNav();
    }
    function go(newIdx) {
      if (newIdx < 0 || newIdx >= qs.length || newIdx === idx) return;
      save({ current_index: newIdx });     // time so far belongs to the question we are leaving
      idx = newIdx;
      drawQuestion();
    }
    function tickTimer() { $("#timer").textContent = `Block Time Elapsed: ${fmtClock(elapsed)}`; }

    $("#prevBtn").addEventListener("click", () => go(idx - 1));
    $("#nextBtn").addEventListener("click", () => go(idx + 1));
    $("#markBox").addEventListener("change", (e) => {
      qs[idx].marked = e.target.checked;
      save({ marked: e.target.checked });
      $("#markWrap").classList.toggle("on", e.target.checked);
      drawNav();
    });
    $("#endBtn").addEventListener("click", () => {
      const unanswered = qs.filter((q) => q.selected === null || q.selected === undefined).length;
      const { modal, close } = openModal(`
        <div class="modal-head"><div><h2>End this block?</h2>
        <p>${unanswered ? `You have ${unanswered} unanswered ${unanswered === 1 ? "question" : "questions"}; they will be marked omitted.` : "All questions are answered."}</p></div></div>
        <div class="modal-foot"><button class="btn" data-close>Keep working</button><button class="btn primary" id="confirmEnd">End Block</button></div>`);
      $("[data-close]", modal).addEventListener("click", close);
      $("#confirmEnd", modal).addEventListener("click", async () => {
        $("#confirmEnd", modal).disabled = true;
        await save();
        ended = true;
        try {
          await api(`/api/tests/${testId}/end`, { method: "POST", body: { elapsed_sec: Math.floor(elapsed) } });
          close();
          location.hash = `#/results/${testId}`;
        } catch (e) { close(); toast("Couldn't end the test", e.message); ended = false; }
      });
    });

    // Timers: block clock + periodic save. Time pauses while the tab is hidden.
    const clock = setInterval(() => { if (!document.hidden) { elapsed += 1; tickTimer(); } }, 1000);
    const autosave = setInterval(() => { if (!document.hidden) save(); }, 15000);
    const onVis = () => {
      if (document.hidden) save();                 // flush active time, then pause
      else activeSince = performance.now();
    };
    document.addEventListener("visibilitychange", onVis);
    cleanup = async () => {
      clearInterval(clock); clearInterval(autosave);
      document.removeEventListener("visibilitychange", onVis);
      if (!ended) await save();
    };

    tickTimer();
    drawQuestion();
  }

  // ------------------------------------------------------------ Results
  async function renderResults(testId) {
    const { test, items, practice } = await api(`/api/tests/${testId}/results`);
    const correct = items.filter((i) => i.is_correct).length;
    const omitted = items.filter((i) => i.omitted).length;
    const pct = items.length ? Math.round((correct / items.length) * 100) : 0;
    const isPractice = test.type === "practice";

    const rows = items.map((it, i) => {
      const picked = !it.omitted && it.option_pcts
        ? ` <span class="muted">· picked ${LETTERS[it.selected]} (${it.option_pcts[it.selected]}%)</span>` : "";
      const mark = it.omitted ? (it.seen ? '<span class="res-mark omit">Omitted</span>' : '<span class="res-mark muted">Omitted · not seen</span>')
        : (it.is_correct ? '<span class="res-mark ok">✓ Correct</span>' : '<span class="res-mark bad">✗ Incorrect</span>') + picked;
      const over = it.time_spent_sec > it.avg_time_sec;
      const wk = it.weakness;
      const wCell = !wk ? '<span class="muted">—</span>'
        : `<span class="wval${wk.applied ? "" : " paused"}" title="${wk.applied ? "" : "Topic is in progress in your Study Planner: recorded as history only"}">${wk.w.toFixed(2)}</span>
           <div class="wparts">R ${wk.r} · p ${wk.p.toFixed(2)} · u ${wk.u.toFixed(2)} · t ${wk.t.toFixed(2)}</div>`;
      return `<tr class="res-row" data-row="${i}" style="cursor:pointer">
          <td>${i + 1}${it.marked ? " 🚩" : ""}</td><td>${esc(it.topic)}${isPractice ? ` <span class="badge ${it.difficulty}">${it.difficulty}</span>` : ""}</td>
          <td>${mark}</td><td class="${over ? "over" : ""}">${it.time_spent_sec}s <span class="muted">/ ${it.avg_time_sec}s</span></td>
          <td>${it.answer_changes}</td>${isPractice ? "" : `<td>${wCell}</td>`}
        </tr>
        <tr class="expand" data-exp="${i}" hidden><td colspan="${isPractice ? 5 : 6}">
          <p style="margin:0 0 8px;font-weight:600">${esc(it.stem)}</p>
          ${it.options.map((o, j) => `<div class="review-opt${j === it.correct_index ? " correct" : j === it.selected ? " wrong" : ""}">
            ${LETTERS[j]}. ${esc(o)}${it.option_pcts ? ` <span class="muted">· ${it.option_pcts[j]}% of students</span>` : ""}${j === it.correct_index ? " <b>(correct)</b>" : j === it.selected ? " <b>(your answer)</b>" : ""}</div>`).join("")}
          <div class="explain">${esc(it.explanation)}</div>
        </td></tr>`;
    }).join("");

    let practiceBanner = "";
    if (isPractice && practice) {
      practiceBanner = practice.passed
        ? `<div class="banner ok">Passed with ${pct}%. The practice task is complete and ${esc(test.topic_names.join(", "))} is back to normal tracking.</div>`
        : `<div class="banner warn"><div class="row">You scored ${pct}%. You need ${practice.pass_pct}% or more to complete this practice task.
            ${practice.task_done ? "" : '<span class="spacer"></span><button class="btn small primary" id="retakeBtn">Retake practice</button>'}</div></div>`;
    }
    view.innerHTML = `
      ${practiceBanner}
      <div class="card">
        <div class="score-row">
          <div><div class="score-big">${pct}%</div><div class="muted">${isPractice ? "Practice score" : "Your score"}</div></div>
          <div class="stat"><small>Correct</small><b>${correct}/${items.length}</b></div>
          <div class="stat"><small>Omitted</small><b>${omitted}</b></div>
          <div class="stat"><small>Time</small><b>${fmtClock(test.elapsed_sec)}</b></div>
          <div class="stat"><small>Topics</small><b style="font-size:14px">${esc(test.topic_names.join(", "))}</b></div>
          <span class="spacer"></span>
          <a class="btn" href="#/tests">Previous Tests</a><a class="btn primary" href="#/create">Create Test</a>
        </div>
      </div>
      <div class="table-wrap"><table class="grid">
        <thead><tr><th>#</th><th>Topic</th><th>Result</th><th>Time / Avg</th><th>Answer changes</th>${isPractice ? "" : "<th>Weakness W (0–1)</th>"}</tr></thead>
        <tbody>${rows}</tbody></table></div>
      <p class="muted">Select a row to see the question, how other students answered, and the explanation.${isPractice ? "" :
        " W = R × (0.5 + 0.2·p) + 0.2·u + 0.1·t, where R = result, p = students' % correct, u = how unusual your wrong pick was, t = slowness."}</p>`;

    const retake = $("#retakeBtn");
    if (retake) retake.addEventListener("click", () => openPractice(practice.task_id));
    $$(".res-row").forEach((r) => r.addEventListener("click", () => {
      const exp = $(`[data-exp="${r.dataset.row}"]`);
      exp.hidden = !exp.hidden;
    }));
  }

  // ------------------------------------------------------------ Previous Tests
  async function renderTests() {
    const tests = await api("/api/tests");
    if (!tests.length) {
      view.innerHTML = `<div class="card"><p class="empty">You haven't taken any tests yet.</p><a class="btn primary" href="#/create">Create Test</a></div>`;
      return;
    }
    view.innerHTML = `<div class="table-wrap"><table class="grid">
      <thead><tr><th>Date</th><th>Type</th><th>Topics</th><th>Questions</th><th>Score</th><th></th></tr></thead>
      <tbody>${tests.map((t) => {
        const ended = t.status === "ended";
        return `<tr><td>${esc(fmtDateTime(t.created_at))}</td>
          <td><span class="badge ${t.type === "practice" ? "practice" : "revision"}">${t.type === "practice" ? "Practice" : "Custom"}</span></td>
          <td>${esc(t.topic_names.join(", "))}</td><td>${t.total}</td>
          <td>${ended ? `${Math.round((t.correct / t.total) * 100)}% (${t.correct}/${t.total})` : '<span class="badge progress">In progress</span>'}</td>
          <td>${ended ? `<a class="btn small" href="#/results/${t.id}">Results</a>` : `<a class="btn small primary" href="#/test/${t.id}">Resume</a>`}</td></tr>`;
      }).join("")}</tbody></table></div>`;
  }

  // ------------------------------------------------------------ Lectures / E-textbook
  // opts: taskId (enables Mark as complete), taskDone, onTaskPage (shows linked questions)
  function itemHtml(kind, item, opts = {}) {
    const completed = opts.taskDone || item.completed;
    const short = (t) => (t.length > 120 ? t.slice(0, 117) + "…" : t);
    const context = opts.onTaskPage && !opts.hideContext && item.questions && item.questions.length
      ? `<div class="context"><span>Covers ${item.questions.length === 1 ? "a question" : item.questions.length + " questions"} you struggled with:</span>
          <ul>${item.questions.map((q) => `<li>${esc(short(q.stem))}</li>`).join("")}</ul></div>` : "";
    const badge = !opts.onTaskPage && item.in_task ? ' <span class="badge revision">In your revision task</span>' : "";
    const body = kind === "lecture"
      ? `<h3>${esc(item.title)}${badge}</h3><p>${esc(item.summary)} · ${item.duration_min} min</p>`
      : `<h3>${esc(item.title)}${badge}</h3><details><summary>Read section</summary><div class="read">${esc(item.content)}</div></details>`;
    let action = "";
    const canComplete = opts.taskId && (opts.onTaskPage || item.in_task);
    if (completed && (opts.onTaskPage || item.in_task)) action = '<span class="done-tag">✓ Completed</span>';
    else if (canComplete) action = `<button class="btn small primary" data-complete="${kind}:${item.id}:${opts.taskId}">Mark as complete</button>`;
    return `<div class="item"><div>${body}${context}</div><div>${action}</div></div>`;
  }

  function bindComplete(root, after) {
    $$("[data-complete]", root).forEach((b) => b.addEventListener("click", async () => {
      const [item_type, item_id, task_id] = b.dataset.complete.split(":");
      b.disabled = true;
      try {
        const res = await api("/api/study-items/complete", { method: "POST",
          body: { task_id: Number(task_id), item_type, item_id: Number(item_id) } });
        after();
        if (res.task_done) showKnowledgeCheck(res.topic);
      } catch (e) { b.disabled = false; toast("Couldn't mark complete", e.message); }
    }));
  }

  // Add-on: after a revision task is completed, offer a test on the topic's lectures.
  // For the prototype, "Create test" only shows a success message.
  function showKnowledgeCheck(topic) {
    const { modal, close } = openModal(`
      <div class="modal-head"><div><h2>Check your understanding</h2>
        <p>You've completed all lectures and e-textbook sections for ${esc(topic)}. Take a short test to see whether the concepts are clear now.</p></div>
        <button class="modal-close" data-close aria-label="Close">×</button></div>
      <div class="modal-foot"><button class="btn" data-close>Not now</button><button class="btn primary" id="createCheck">Create test</button></div>`);
    $$("[data-close]", modal).forEach((b) => b.addEventListener("click", close));
    $("#createCheck", modal).addEventListener("click", () => {
      modal.innerHTML = `
        <div class="modal-head"><div><h2>Test created successfully</h2>
          <p>Your ${esc(topic)} knowledge-check test has been created.</p></div></div>
        <div class="modal-foot"><button class="btn primary" data-close>Done</button></div>`;
      $("[data-close]", modal).addEventListener("click", close);
      $("[data-close]", modal).focus();
    });
  }

  async function renderStudy(kind, topicParam) {
    const topicId = topicParam ? Number(topicParam) : null;
    const groups = await api(`/api/${kind === "lecture" ? "lectures" : "etextbook"}${topicId ? `?topic_id=${topicId}` : ""}`);
    const base = kind === "lecture" ? "#/lectures" : "#/etextbook";
    view.innerHTML = `
      ${topicId ? `<div class="banner info">Showing ${esc(groups[0] ? groups[0].topic : "")} only. <a href="${base}">Show all topics</a></div>` : ""}
      ${groups.map((g) => `<div class="card topic-block">
        <div class="topic-block-head"><h2>${esc(g.topic)}</h2>
          ${g.revision_task_id ? '<span class="badge revision">Revision task active</span>' : ""}</div>
        ${g.items.map((it) => itemHtml(kind, it, { taskId: g.revision_task_id })).join("")}
        <p class="muted" style="margin:8px 0 0">${g.revision_task_id
          ? "Mark as complete is available for the items in your revision task."
          : "Mark as complete appears when this topic has a revision task in your Study Planner."}</p>
      </div>`).join("")}`;
    bindComplete(view, () => renderStudy(kind, topicParam));
  }

  async function renderTask(taskId) {
    const t = await api(`/api/tasks/${taskId}`);
    $("#pageTitle").textContent = t.type === "revision" ? "Revision Task" : "Practice Task";
    if (t.type === "practice") {
      const test = t.test;
      const done = t.status === "done";
      let state = "", button = "";
      if (done) { state = `<div class="banner ok">Passed with ${test.correct}/${test.total}. This task is complete.</div>`; button = `<a class="btn" href="#/results/${test.id}">View results</a>`; }
      else if (!test) button = '<button class="btn primary" id="startP">Start practice</button>';
      else if (test.status === "in_progress") button = '<button class="btn primary" id="startP">Resume practice</button>';
      else {
        state = `<div class="banner warn">Last score ${test.correct}/${test.total}. You need ${t.pass_pct}% or more to complete this task.</div>`;
        button = `<button class="btn primary" id="startP">Retake practice</button> <a class="btn" href="#/results/${test.id}">Last results</a>`;
      }
      view.innerHTML = `${state}<div class="card"><h2>${esc(taskTitle(t))}</h2>
        <p class="muted">Scheduled ${esc(fmtDateTime(t.scheduled_at))}. 10 questions: 5 easy, then 5 medium, to build the topic up step by step.
        Score ${t.pass_pct}% or more to complete the task.${t.attempts ? ` Attempts so far: ${t.attempts}.` : ""}</p>
        <div class="row">${button}</div></div>`;
      const sp = $("#startP");
      if (sp) sp.addEventListener("click", () => openPractice(t.id));
      return;
    }
    const done = t.status === "done";
    const p = t.progress;
    const opts = { taskId: done ? null : t.id, taskDone: done, onTaskPage: true, hideContext: true };
    // Pair each lecture with the e-textbook section for the same concept (same position in the topic).
    const pairs = new Map();
    t.lectures.forEach((l) => pairs.set(l.sort, { lecture: l }));
    t.sections.forEach((x) => { const g = pairs.get(x.sort) || {}; g.section = x; pairs.set(x.sort, g); });
    const groups = [...pairs.entries()].sort((a, b) => a[0] - b[0]).map(([, g]) => g);
    const short = (txt) => (txt.length > 120 ? txt.slice(0, 117) + "…" : txt);
    const pairHtml = (g, i) => {
      const qs = (g.lecture || g.section).questions || [];
      const context = qs.length ? `<div class="context"><span>Covers ${qs.length === 1 ? "a question" : qs.length + " questions"} you struggled with:</span>
        <ul>${qs.map((q) => `<li>${esc(short(q.stem))}</li>`).join("")}</ul></div>` : "";
      return `<div class="pair"><div class="pair-num">${i + 1}</div><div class="pair-body">
        ${g.lecture ? `<div class="pair-label">Lecture</div>${itemHtml("lecture", g.lecture, opts)}` : ""}
        ${g.section ? `<div class="pair-label">E-textbook</div>${itemHtml("section", g.section, opts)}` : ""}
        ${context}</div></div>`;
    };
    view.innerHTML = `
      ${done ? `<div class="banner ok">Revision complete. ${esc(t.topic)} is back to normal tracking.</div>` : ""}
      <div class="card"><h2>${esc(taskTitle(t))}</h2>
        <div class="row"><span class="muted">Scheduled ${esc(fmtDateTime(t.scheduled_at))}</span>
          <span class="spacer"></span><span>${done ? p.total : p.done}/${p.total} complete</span>
          <span class="progress-bar"><span style="width:${done || !p.total ? 100 : Math.round((p.done / p.total) * 100)}%"></span></span></div>
        <p class="muted" style="margin:8px 0 0">Each step is a lecture and its e-textbook section, covering a concept behind questions you struggled with. Mark both complete to finish the step.</p></div>
      <div class="card"><h2>Lectures + E-textbook</h2>
        ${groups.length ? groups.map(pairHtml).join("") : '<p class="empty">No items in this task.</p>'}</div>`;
    bindComplete(view, () => renderTask(taskId));
  }

  // ------------------------------------------------------------ Weak-area pop-up
  async function checkPopup() {
    if (modalOpen || document.body.classList.contains("test-mode")) return;
    let res;
    try { res = await api("/api/popup"); } catch (e) { return; }
    if (!res.popup || modalOpen || document.body.classList.contains("test-mode")) return;
    showPopup(res.popup);
  }

  function showPopup(popup) {
    const topics = popup.topics;
    const assign = {};                              // topic_id -> 'unclear' | 'practice' | null
    topics.forEach((t) => { assign[t.id] = null; }); // nothing preselected: the student chooses
    const name = (id) => topics.find((t) => t.id === id).name;

    const dismiss = async (close) => {
      try { await api(`/api/popup/${popup.id}/dismiss`, { method: "POST" }); } catch (e) { /* ignore */ }
      close();
    };
    const { modal, close } = openModal("", { onClose: dismiss });

    function eventsText(t) {
      const e = t.events;
      const parts = [`${t.seen} questions seen`];
      if (e.incorrect) parts.push(`${e.incorrect} incorrect`);
      if (e.omitted) parts.push(`${e.omitted} unanswered`);
      if (e.slow_correct) parts.push(`${e.slow_correct} correct but slow`);
      return parts.join(" · ");
    }

    function actionBox(action, heading, sub) {
      const chosen = topics.filter((t) => assign[t.id] === action);
      const free = topics.filter((t) => !assign[t.id]);   // topics not in either action
      return `<div class="action-box"><h3>${heading}</h3><p class="sub">${sub}</p>
        <div class="chips">
          ${chosen.map((t) => `<span class="chip">${esc(t.name)}<button type="button" data-remove="${t.id}" aria-label="Remove ${esc(t.name)}">×</button></span>`).join("")}
          <select data-add="${action}" aria-label="Add a topic to: ${heading}" ${free.length ? "" : "disabled"}>
            <option value="">${free.length ? "+ Add topic" : chosen.length ? "All topics assigned" : "No topics left"}</option>
            ${free.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}
          </select>
        </div></div>`;
    }

    function step1() {
      const assigned = topics.filter((t) => assign[t.id]).length;
      const unassigned = topics.length - assigned;
      modal.innerHTML = `
        <div class="modal-head"><div><h2>These are your weak areas</h2>
          <p>You found these topics the hardest to solve. What would you like to do to strengthen them?</p></div>
          <button class="modal-close" data-close aria-label="Close">×</button></div>
        <div class="modal-body">
          ${topics.map((t) => `<div class="weak-row"><div><b>${esc(t.name)}</b><small>${esc(eventsText(t))}</small></div>
            <div class="meter" aria-hidden="true"><span style="width:${Math.round(t.score * 100)}%"></span></div><b title="Average weakness W">${t.score.toFixed(2)}</b></div>`).join("")}
          <p class="muted" style="margin:6px 0 0;font-size:12px">Average weakness score (0–1) across the questions you've seen; ${popup.threshold} or more is a weak area.</p>
          ${actionBox("unclear", "Concept is unclear → Lectures + E-textbook", "Adds a revision task with this topic's lectures and e-textbook sections.")}
          ${actionBox("practice", "Need more practice → Building-block tests", `Adds a 10-question practice test: 5 easy, then 5 medium. Score ${popup.pass_pct}% or more to complete it.`)}
          <p class="muted" style="margin:10px 0 0">${assigned === 0
            ? "Use the dropdowns to choose which topics go into each action."
            : unassigned ? `${unassigned} ${unassigned === 1 ? "topic isn't" : "topics aren't"} assigned and will be checked again after your next test.` : "All topics assigned."}</p>
        </div>
        <div class="modal-foot"><button class="btn primary" id="toSchedule" ${assigned ? "" : "disabled"}>Continue to schedule</button></div>`;
      $("[data-close]", modal).addEventListener("click", () => dismiss(close));
      $$("[data-remove]", modal).forEach((b) => b.addEventListener("click", () => { assign[Number(b.dataset.remove)] = null; step1(); }));
      $$("[data-add]", modal).forEach((s) => s.addEventListener("change", () => {
        if (!s.value) return;
        assign[Number(s.value)] = s.dataset.add;
        step1();
      }));
      $("#toSchedule", modal).addEventListener("click", step2);
    }

    function step2() {
      const chosen = topics.filter((t) => assign[t.id]);
      const tomorrow = startOfDay(Date.now()) + DAY;
      modal.innerHTML = `
        <div class="modal-head"><div><h2>Schedule your tasks</h2>
          <p>Pick when to work on each task and when to be reminded.</p></div>
          <button class="modal-close" data-close aria-label="Close">×</button></div>
        <div class="modal-body">
          <div class="sched-row sched-head"><span>Task</span><span>Date</span><span>Time</span><span>Remind me</span></div>
          ${chosen.map((t, i) => `<div class="sched-row" data-sched="${t.id}">
            <div><b>${esc(t.name)}</b><br><span class="badge ${assign[t.id] === "unclear" ? "revision" : "practice"}">
              ${assign[t.id] === "unclear" ? "Revision: Lectures + E-textbook" : "Practice: Building blocks"}</span></div>
            <input type="date" value="${toDateInput(tomorrow + i * DAY)}" aria-label="Date for ${esc(t.name)}">
            <input type="time" value="${assign[t.id] === "unclear" ? "19:00" : "08:00"}" aria-label="Time for ${esc(t.name)}">
            <select aria-label="Reminder for ${esc(t.name)}">${REMIND_OPTIONS.map((o) => `<option value="${o.v}" ${o.v === 30 ? "selected" : ""}>${o.l}</option>`).join("")}</select>
          </div>`).join("")}
          <p class="error-text" id="schedErr" hidden></p>
        </div>
        <div class="modal-foot"><button class="btn" id="backBtn">Back</button><button class="btn primary" id="saveTasks">Add to Study Planner</button></div>`;
      $("[data-close]", modal).addEventListener("click", () => dismiss(close));
      $("#backBtn", modal).addEventListener("click", step1);
      $("#saveTasks", modal).addEventListener("click", async () => {
        const err = $("#schedErr", modal);
        const assignments = [];
        for (const row of $$("[data-sched]", modal)) {
          const [date, time] = $$("input", row).map((i) => i.value);
          const topicId = Number(row.dataset.sched);
          if (!date || !time) { err.textContent = `Pick a date and time for ${name(topicId)}.`; err.hidden = false; return; }
          const scheduledAt = new Date(`${date}T${time}`).getTime();
          if (scheduledAt <= Date.now()) { err.textContent = `${name(topicId)}: pick a time later than now.`; err.hidden = false; return; }
          assignments.push({ topic_id: topicId, action: assign[topicId], scheduled_at: scheduledAt,
            remind_before_min: Number($("select", row).value) });
        }
        $("#saveTasks", modal).disabled = true;
        try {
          await api(`/api/popup/${popup.id}/submit`, { method: "POST", body: { assignments } });
          close();
          toast(`Added ${assignments.length} ${assignments.length === 1 ? "task" : "tasks"} to your Study Planner`,
            "These topics won't trigger the pop-up again while you work on them.", "Open Study Planner",
            () => { weekOffset = 0; location.hash = "#/planner"; });
          // Stay on the current screen (usually Results); refresh only if the planner is open.
          if (parseHash().name === "planner") renderPlanner();
        } catch (e) { err.textContent = e.message; err.hidden = false; $("#saveTasks", modal).disabled = false; }
      });
    }

    step1();
  }

  // ------------------------------------------------------------ Reminders
  const notified = new Set();
  let dueReminders = [];

  async function refreshReminders() {
    try { dueReminders = await api("/api/reminders"); } catch (e) { return; }
    const unseen = dueReminders.filter((r) => !r.reminder_seen);
    const count = $("#bellCount");
    count.textContent = unseen.length;
    count.hidden = unseen.length === 0;
    if (!$("#bellPanel").hidden) drawBellPanel();
    if (document.body.classList.contains("test-mode")) return;   // no interruptions during a test
    unseen.forEach((r) => {
      if (notified.has(r.id)) return;
      notified.add(r.id);
      const text = `${r.type === "revision" ? "Revision" : "Practice"} starts ${fmtDateTime(r.scheduled_at)}`;
      toast(`Reminder: ${taskTitle(r)}`, text, "Open", () => openReminder(r));
      try {
        if ("Notification" in window && Notification.permission === "granted") {
          const n = new Notification(`Reminder: ${taskTitle(r)}`, { body: text, tag: `task-${r.id}` });
          n.onclick = () => { window.focus(); openReminder(r); n.close(); };
        }
      } catch (e) { /* notifications unavailable */ }
    });
  }

  async function openReminder(r) {
    try { await api(`/api/reminders/${r.id}/seen`, { method: "POST" }); } catch (e) { /* ignore */ }
    $("#bellPanel").hidden = true;
    location.hash = `#/task/${r.id}`;
    refreshReminders();
  }

  function drawBellPanel() {
    const panel = $("#bellPanel");
    const canAsk = "Notification" in window && Notification.permission === "default";
    panel.innerHTML = `<h3>Reminders</h3>
      ${canAsk ? '<p class="muted" style="margin:0 0 8px">Get reminders even when this tab is in the background. <button class="btn link" id="askNotif">Turn on browser notifications</button></p>' : ""}
      ${dueReminders.length ? dueReminders.map((r) => `<div class="bell-item"><div><p><b>${esc(taskTitle(r))}</b></p>
        <small>${r.reminder_seen ? "" : "New · "}Starts ${esc(fmtDateTime(r.scheduled_at))}</small></div>
        <button class="btn small" data-rem="${r.id}">Open</button></div>`).join("") : '<p class="empty">No reminders right now.</p>'}`;
    const ask = $("#askNotif", panel);
    if (ask) ask.addEventListener("click", async () => {
      try { await Notification.requestPermission(); } catch (e) { /* ignore */ }
      drawBellPanel();
    });
    $$("[data-rem]", panel).forEach((b) => b.addEventListener("click", () => {
      openReminder(dueReminders.find((r) => r.id === Number(b.dataset.rem)));
    }));
  }

  $("#bellBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    const panel = $("#bellPanel");
    panel.hidden = !panel.hidden;
    $("#bellBtn").setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) drawBellPanel();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".topbar-right")) { $("#bellPanel").hidden = true; $("#bellBtn").setAttribute("aria-expanded", "false"); }
  });
  setInterval(refreshReminders, 20000);

  // ------------------------------------------------------------ Reset session data
  $("#resetBtn").addEventListener("click", () => {
    const { modal, close } = openModal(`
      <div class="modal-head"><div><h2>Reset your data?</h2>
        <p>This deletes your tests, answers, weak-area scores, planner tasks and reminders, so you can start the demo again.
        Questions, lectures and e-textbook content stay as they are.</p></div>
        <button class="modal-close" data-close aria-label="Close">×</button></div>
      <div class="modal-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="confirmReset">Reset my data</button></div>`);
    $$("[data-close]", modal).forEach((b) => b.addEventListener("click", close));
    $("#confirmReset", modal).addEventListener("click", async () => {
      $("#confirmReset", modal).disabled = true;
      try {
        await api("/api/reset", { method: "POST" });
        close();
        notified.clear();
        weekOffset = 0;
        toast("Your data was reset", "All questions are unused again and the Study Planner is empty.");
        if (parseHash().name === "planner") route(); else location.hash = "#/planner";
      } catch (e) { close(); toast("Couldn't reset", e.message); }
    });
  });

  // ------------------------------------------------------------ start
  route();
})();
