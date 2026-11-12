/* Huddle front end - plain JS, no build step. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const state = {
  overview: null,
  // The chat lives here, not in a page, so it keeps going while you browse. See the chat section.
  chat: { msgs: [], session: null, provider: null, busy: false, pending: null, unread: 0, gen: 0, ctrl: null,
          ai: null, aiAt: 0, ideas: null, dock: false },
  trade: { give: [], get: [], result: null },
  finder: { pos: "", partner: "", ideas: null },
  matchup: null,
};

// ------------------------------------------------------------------ theme
const THEMES = [
  ["field", "Light", ["#0f3324", "#ff6a13", "#f3f5f1"]],
  ["dark", "Dark", ["#0c0e10", "#ff7a2e", "#1b1e22"]],
];
function applyTheme(t) {
  if (!THEMES.some(x => x[0] === t)) t = "field";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("huddle-theme", t); } catch (_) { /* storage unavailable */ }
}
try { applyTheme(localStorage.getItem("huddle-theme") || "field"); } catch (_) { applyTheme("field"); }

// ------------------------------------------------------------------ utils
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const fmt = (n, d = 1) => (n === null || n === undefined || Number.isNaN(Number(n))) ? "–" : Number(n).toFixed(d);
const signed = (n, d = 1) => { const t = Math.abs(Number(n)).toFixed(d); return (Number(t) === 0 ? "" : n > 0 ? "+" : "−") + t; };
const toneOf = n => (n > 0.05 ? "good" : n < -0.05 ? "bad" : "even");
const params = () => new URLSearchParams(location.hash.split("?")[1] || "");

class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) throw new ApiError((data && (data.error || data.detail)) || `Request failed (${res.status})`, res.status);
  return data;
}
function loading(on, text) {
  $("#loading").hidden = !on;
  if (text) $("#loading-text").textContent = text;
  if (on) view.innerHTML = "";
}

function initials(name) {
  if (!name) return "?";
  if (/D\/ST$/.test(name)) return name.replace(" D/ST", "").slice(0, 3).toUpperCase();
  const parts = name.replace(/\b(Jr|Sr|II|III|IV)\.?$/i, "").trim().split(/[\s-]+/);
  return ((parts[0] || "")[0] + ((parts[parts.length - 1] || "")[0] || "")).toUpperCase();
}
const avatar = (p, cls = "") => `<span class="av ${esc(p.pos || "")} ${cls}" aria-hidden="true">${esc(p.pos === "DEF" && p.team ? p.team : initials(p.name))}</span>`;
function injuryTag(s) {
  if (!s) return "";
  const cls = ["Questionable", "Doubtful"].includes(s) ? "q" : "o";
  const short = { Questionable: "Q", Doubtful: "D", Out: "Out", IR: "IR", PUP: "PUP", Sus: "Susp" }[s] || s;
  return `<span class="tag ${cls}" title="${esc(s)}">${esc(short)}</span>`;
}
const pill = label => label ? `<span class="pill ${esc(label)}">${esc(label)}</span>` : "";
const pname = p => `<button class="pname" data-pid="${esc(p.id)}">${esc(p.name)}</button>${injuryTag(p.injury)}`;
const pcell = (p, meta) => `<div class="pcell">${avatar(p, "sm")}<div>${pname(p)}<div class="m">${meta ?? esc([p.pos, p.team || "FA"].join(", "))}</div></div></div>`;

function showError(err) {
  loading(false);
  if (err.status === 409) { location.hash = "#settings"; renderSettings(err.message); return; }
  view.innerHTML = `<div class="error"><h3>That didn't load</h3><p>${esc(err.message)}</p>
    <p class="small muted">Check your internet connection, then select Refresh in the sidebar.</p></div>`;
}

// ------------------------------------------------------------------ boot / nav
async function loadOverview() {
  state.overview = await api("/api/overview");
  const o = state.overview;
  if (o.theme) applyTheme(o.theme);
  $("#teamcard").innerHTML = `<div class="t-name">${esc(o.team.name)}</div>
    <div class="t-meta"><span class="t-rec">${o.team.wins}-${o.team.losses}${o.team.ties ? "-" + o.team.ties : ""}</span> in ${esc(o.league.name)}</div>
    <div class="t-meta">Week ${o.league.week}, ${esc(o.league.scoring)}</div>`;
  const t = new Date(o.data.built_at * 1000);
  $("#railfoot").innerHTML = `<span>Synced ${t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span><button id="refresh">Refresh</button>`;
  $("#refresh").onclick = async () => {
    loading(true, "Pulling the latest rosters, injuries and stats…");
    try { await api("/api/refresh", { method: "POST" }); state.matchup = null; chat.ideas = null; await loadOverview(); route(); }
    catch (e) { showError(e); }
  };
}

const VIEWS = { ask: renderAsk, lineup: renderLineup, matchup: renderMatchup, trades: renderTrades,
  waivers: renderWaivers, league: renderLeague, report: renderReport, settings: renderSettings };

async function route() {
  closeDrawer();
  const name = (location.hash || "#ask").slice(1).split("?")[0];
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.view === name));
  const fn = VIEWS[name] || renderAsk;
  if (fn === renderAsk) { closeDock(); if (!document.hidden) clearUnread(); }
  chatChanged();
  if (name !== "settings" && !state.overview) {
    loading(true, "Syncing your league with Sleeper and loading this season's stats…");
    try { await loadOverview(); } catch (e) { showError(e); return; }
    loading(false);
  }
  try { await fn(); } catch (e) { showError(e); }
}
window.addEventListener("hashchange", route);
document.addEventListener("click", e => {
  const b = e.target.closest(".pname[data-pid]");
  if (b) openPlayer(b.dataset.pid);
  const c = e.target.closest("button[data-copy]");
  if (c) { navigator.clipboard?.writeText(c.dataset.copy); c.textContent = "Copied"; setTimeout(() => { c.textContent = "Copy"; }, 1500); }
});

let matchupReq = null;
async function getMatchup() {
  if (state.matchup) return state.matchup;
  if (!matchupReq) matchupReq = api("/api/matchup").then(d => { state.matchup = d; return d; }).finally(() => { matchupReq = null; });
  return matchupReq;
}

// ------------------------------------------------------------------ shared pieces
function scoreboard(d, compact = false) {
  if (!d || !d.opponent) return "";
  const wp = Math.round(100 * d.win_probability);
  return `<div class="scoreboard ${compact ? "compact" : ""}">
    <div class="wk">Week ${d.week} projection</div>
    <div class="sb-row">
      <div class="sb-team"><div class="sb-name">${esc(d.you.team)}</div><div class="sb-score">${fmt(d.you.projected_total)}</div></div>
      <div class="sb-vs">vs</div>
      <div class="sb-team right"><div class="sb-name">${esc(d.opponent.team)}</div><div class="sb-score">${fmt(d.opponent.projected_total)}</div></div>
    </div>
    <div class="sb-bar" role="img" aria-label="Win chance ${wp} percent"><i style="width:${wp}%"></i></div>
    <div class="sb-foot"><span>Win chance <strong>${wp}%</strong></span><span>${(d.actual && (d.actual.you || d.actual.opponent)) ? `Live ${fmt(d.actual.you || 0)} to ${fmt(d.actual.opponent || 0)}` : ""}</span></div>
  </div>`;
}

function boardRow(p, slot) {
  const empty = !p.id;
  const zero = !empty && (p.proj ?? 0) <= 0.5;
  const meta = empty ? "No eligible player"
    : [`${p.team || "FA"} ${p.opp || ""}`.trim(), p.implied_total ? `team total ${fmt(p.implied_total)}` : null,
       p.opp === "BYE" ? null : p.note].filter(Boolean).map(esc).join(", ");
  return `<li class="row ${zero ? "zero" : ""}">
    <span class="slot">${esc(slot)}</span>
    ${empty ? '<span class="av">–</span>' : avatar(p)}
    <span class="who">${empty ? '<span class="muted">Empty</span>' : pname(p)}<div class="meta">${meta}</div></span>
    <span class="mu">${pill(p.matchup)}</span>
    <span class="pts">${fmt(p.proj)}<small>${p.ros_ppg !== undefined ? fmt(p.ros_ppg) + " ROS" : ""}</small></span>
  </li>`;
}

function fixes(d) {
  const out = [];
  for (const s of d.swaps || []) out.push(`<div class="line"><span class="in">Start ${esc(s.start.name)}</span> <span class="muted">${fmt(s.start.proj)}</span>${s.sit ? ` <span class="out">bench ${esc(s.sit.name)}</span> <span class="muted">${fmt(s.sit.proj)}</span>` : ""}</div>`);
  for (const h of d.holes || []) out.push(`<div class="line"><strong>${esc(h.slot)}</strong> <span>${h.current ? esc(h.current) + ": " + esc(h.reason || "no projection") : "nobody eligible"}.</span>
    ${h.free_agents.length ? `<span><span class="muted">Pick up</span> ${h.free_agents.slice(0, 2).map(f => `${pname(f)} <span class="muted">${fmt(f.proj)}</span>`).join(", ")}</span>` : ""}</div>`);
  return out;
}

// ------------------------------------------------------------------ chat
// The conversation lives at the app level, not inside the Ask page, so an answer keeps
// streaming while you move around. Nothing here holds on to page elements: every change
// updates state.chat and calls chatChanged(), which repaints whatever chat views are on
// screen (the Ask page and the corner panel), the corner button and the tab title.
const PROVIDER_NAMES = { claude: "Claude", chatgpt: "ChatGPT", ollama: "Ollama on this Mac", api: "API key" };
const CHAT_KEY = "huddle-chat";
const chat = state.chat;

try {
  const saved = JSON.parse(sessionStorage.getItem(CHAT_KEY) || "null");
  if (saved && Array.isArray(saved.msgs)) Object.assign(chat, { msgs: saved.msgs, session: saved.session || null, provider: saved.provider || null });
} catch (_) { /* storage unavailable */ }
function saveChat() {
  try { sessionStorage.setItem(CHAT_KEY, JSON.stringify({ msgs: chat.msgs.slice(-60), session: chat.session, provider: chat.provider })); }
  catch (_) { /* storage unavailable */ }
}

const currentView = () => (location.hash || "#ask").slice(1).split("?")[0] || "ask";
const onAskPage = () => (VIEWS[currentView()] ? currentView() : "ask") === "ask";
const chatOnScreen = () => !document.hidden && (onAskPage() || chat.dock);

function ideasFor(m) {
  const ideas = [];
  const you = m && m.you;
  if (you) {
    const h = (you.holes || [])[0];
    if (h && h.current) {
      const why = h.reason === "bye week" ? "is on bye" : h.reason ? `is ${h.reason}` : "has no projection";
      ideas.push(["Fix your lineup", `${h.current} ${why}. Who should I pick up for ${h.slot}?`]);
    }
    const s = (you.swaps || [])[0];
    if (s && s.sit) ideas.push(["Start or sit", `Should I start ${s.start.name} over ${s.sit.name} this week?`]);
  }
  if (ideas.length < 2) ideas.push(["Start or sit", "Who should I start this week, and who's a risk?"]);
  if (m && m.opponent) ideas.push(["Matchup", `What gives me the best shot at beating ${m.opponent.team} this week?`]);
  ideas.push(["Trades", "Find me a realistic trade that upgrades my weakest starter."]);
  ideas.push(["Waivers", "Who's the best pickup on waivers for my team right now?"]);
  ideas.push(["Season", "How does my roster look for the fantasy playoffs?"]);
  return ideas.slice(0, 4);
}

// AI status and question ideas, shared by the Ask page and the corner panel.
async function loadChatInfo(force = false) {
  const jobs = [];
  if (force || !chat.ai || Date.now() - chat.aiAt > 60000) {
    jobs.push(api("/api/ai/status").catch(() => ({ ready: false, message: "Couldn't check the AI." })).then(ai => {
      if (chat.provider && chat.provider !== ai.provider) chat.session = null;
      chat.provider = ai.provider; chat.ai = ai; chat.aiAt = Date.now();
    }));
  }
  if (!chat.ideas && state.overview) jobs.push(getMatchup().then(m => { chat.ideas = ideasFor(m); }).catch(() => { chat.ideas = ideasFor(null); }));
  await Promise.all(jobs);
  chatChanged();
}

async function renderAsk() {
  const [, m] = await Promise.all([loadChatInfo(), getMatchup().catch(() => null)]);
  chat.ideas = ideasFor(m);
  const fixList = m ? fixes(m.you) : [];
  view.innerHTML = `<div class="ask">
    <div class="chat">
      <div class="chat-head"><span id="ai-slot"></span>
        <button class="btn ghost small" id="newchat" hidden>New chat</button></div>
      <div class="msgs" id="msgs" aria-live="polite"></div>
      <div class="composer"><div class="composer-box">
        <textarea id="q" rows="1" placeholder="Ask about your lineup, a trade, a player…" aria-label="Message"></textarea>
        <button class="btn primary" id="send">Send</button></div></div>
    </div>
    <aside class="side">
      ${m && m.opponent ? `<a href="#matchup" style="text-decoration:none" class="keep">${scoreboard(m, true)}</a>` : ""}
      <div class="card pad"><h3>Before kickoff</h3>${fixList.length ? fixList.join("") : '<p class="small muted" style="margin:0">Your lineup is set. Nothing to change.</p>'}
        <p class="small" style="margin:8px 0 0"><a href="#lineup">Full lineup</a></p></div>
      <div class="card pad" id="toppick"><h3>Top pickup</h3><p class="small muted" style="margin:0">Checking the wire…</p></div>
    </aside>
  </div>`;
  const msgs = $("#msgs");
  msgs._forceBottom = true;
  msgs.onclick = ideaClick;
  bindComposer($("#q"), $("#send"));
  $("#newchat").onclick = () => resetChat();
  paintChat();
  $("#q").focus();
  api("/api/waivers").then(w => {
    const t = w.recommendations[0], box = $("#toppick");
    if (!box) return;
    box.innerHTML = t ? `<h3>Top pickup</h3><div class="mini">${avatar(t, "sm")}<div class="grow"><div>${pname(t)}</div><div class="small muted">${esc(t.pos)}, ${esc(t.team || "")} ${esc(t.opp || "")}</div></div>
      <div class="good" style="font:700 18px var(--display)">${signed(t.net_gain)}</div></div>
      <p class="small muted" style="margin:6px 0 0">${esc(t.reasons[0] || "")}. ${moveText(t.move)}. <a href="#waivers">More</a></p>`
      : `<h3>Top pickup</h3><p class="small muted" style="margin:0">Nobody on waivers beats your bench.</p>`;
  }).catch(() => { const box = $("#toppick"); if (box) box.remove(); });
}

function ideaClick(e) {
  const b = e.target.closest(".ideas button");
  if (b && !chat.busy) send(b.dataset.q);
}

function bindComposer(ta, btn) {
  const grow = () => { ta.style.height = "26px"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; };
  const submit = () => { const v = ta.value.trim(); if (!v || chat.busy) return; ta.value = ""; grow(); send(v); };
  ta.oninput = grow;
  ta.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } };
  btn.onclick = () => (chat.busy ? stopChat() : submit());
}

function setupNotice(ai) {
  return `<div class="setup-card"><h3>Finish setting up ${esc(PROVIDER_NAMES[ai.provider] || "the AI")}</h3>
    <p class="small" style="margin:0 0 10px">${esc(ai.message || "")}</p><a class="btn primary" href="#settings">Open Settings</a></div>`;
}

function helloHTML(compact) {
  const ai = chat.ai, team = state.overview ? state.overview.team.name : "your team";
  const ideas = (chat.ideas || ideasFor(null)).slice(0, compact ? 3 : 4);
  const btns = `<div class="ideas">${ideas.map(([k, q]) => `<button data-q="${esc(q)}"><b>${esc(k)}</b>${esc(q)}</button>`).join("")}</div>`;
  const setup = ai && !ai.ready ? setupNotice(ai) : "";
  if (compact) {
    return `<div class="hello compact"><h3>What's the call?</h3>
      <p>Ask about your lineup, a trade or a player. Keep browsing while Huddle works; it'll let you know when the answer is ready.</p>${setup}${btns}</div>`;
  }
  return `<div class="hello"><h1>What's the call this week?</h1>
    <p>Huddle checks ${esc(team)}'s roster, matchups, injuries, Vegas lines and the waiver wire before it answers.</p>${setup}${btns}</div>`;
}

function aiBadgeHTML() {
  const ai = chat.ai;
  if (!ai) return `<span class="ai-badge"><i class="wait"></i>Checking the AI…</span>`;
  return `<a class="ai-badge ${ai.ready ? "" : "off"}" href="#settings" title="Change in Settings"><i></i>${esc(PROVIDER_NAMES[ai.provider] || "AI")}${ai.ready ? "" : ": needs setup"}</a>`;
}

function msgHTML(m) {
  if (m.role === "user") return `<div class="msg user">${esc(m.content)}</div>`;
  if (m.note) return `<div class="msg note">${esc(m.content)}</div>`;
  return `<div class="msg bot">${m.steps && m.steps.length ? `<div class="steps">${m.steps.map(s => `<div>${esc(s)}</div>`).join("")}</div>` : ""}
    <div class="body">${md(m.content)}${m.fix ? `<p class="small"><a href="#settings">Open Settings</a></p>` : ""}</div>
    ${m.meta ? `<div class="prov">${esc(m.meta)}</div>` : ""}</div>`;
}

// What Huddle is doing right now, in a few words.
function statusText() {
  const p = chat.pending;
  if (!p) return "";
  if (p.phase === "writing") return "Writing the answer";
  if (p.phase === "tool") return p.steps[p.steps.length - 1];
  return p.steps.length ? "Thinking it over" : "Thinking";
}
function elapsed() {
  const s = chat.pending ? Math.max(0, Math.floor((Date.now() - chat.pending.started) / 1000)) : 0;
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function pendingHTML() {
  const p = chat.pending;
  const done = p.phase === "tool" ? p.steps.slice(0, -1) : p.steps;
  return `${done.length ? `<div class="steps">${done.map(s => `<div>${esc(s)}</div>`).join("")}</div>` : ""}
    <div class="thinking"><span class="spin" aria-hidden="true"></span><span class="what">${esc(statusText())}…</span><span class="el">${elapsed()}</span></div>
    ${p.live ? `<div class="body">${md(p.live)}</div>` : ""}`;
}

function setHTML(el, html) {
  if (el._html === html) return;
  el.innerHTML = html; el._html = html;
}

function paintThread(el, inPanel) {
  if (!el) return;
  const box = inPanel ? el : document.scrollingElement;
  const nearBottom = el._forceBottom || box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  el._forceBottom = false;
  el.setAttribute("aria-busy", chat.busy ? "true" : "false");
  if (!chat.msgs.length && !chat.pending) {
    el._mode = "hello"; el._n = 0;
    setHTML(el, helloHTML(inPanel));
    return;
  }
  if (el._mode !== "chat" || (el._n || 0) > chat.msgs.length) {
    el.innerHTML = ""; el._html = null; el._n = 0; el._mode = "chat";
  }
  let pend = el.querySelector(".msg.pending");
  for (let n = el._n; n < chat.msgs.length; n++) {
    const h = msgHTML(chat.msgs[n]);
    if (pend) pend.insertAdjacentHTML("beforebegin", h); else el.insertAdjacentHTML("beforeend", h);
  }
  el._n = chat.msgs.length;
  if (chat.pending) {
    if (!pend) { el.insertAdjacentHTML("beforeend", '<div class="msg bot pending"></div>'); pend = el.lastElementChild; }
    setHTML(pend, pendingHTML());
  } else if (pend) pend.remove();
  if (nearBottom) {
    if (inPanel) el.scrollTop = el.scrollHeight;
    else window.scrollTo(0, document.body.scrollHeight);
  }
}

let paintQueued = false;
function chatChanged() {
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(() => { paintQueued = false; paintChat(); });
}

function paintChat() {
  paintThread($("#msgs"), false);
  paintThread($("#dock-msgs"), true);
  for (const id of ["#ai-slot", "#dock-ai"]) { const el = $(id); if (el) setHTML(el, aiBadgeHTML()); }
  for (const id of ["#send", "#dock-send"]) {
    const b = $(id); if (!b) continue;
    b.textContent = chat.busy ? "Stop" : "Send";
    b.classList.toggle("stop", chat.busy);
    b.title = chat.busy ? "Stop this answer" : "";
  }
  for (const id of ["#q", "#dock-q"]) {
    const t = $(id); if (t) t.placeholder = chat.busy ? "Type your next question…" : (id === "#q" ? "Ask about your lineup, a trade, a player…" : "Ask Huddle…");
  }
  for (const id of ["#newchat", "#dock-new"]) { const b = $(id); if (b) b.hidden = !chat.msgs.length && !chat.busy; }

  // Corner button: hidden on the Ask page and while the panel is open.
  const l = $("#launcher");
  l.hidden = onAskPage() || chat.dock;
  l.classList.toggle("busy", chat.busy);
  l.classList.toggle("new", !chat.busy && chat.unread > 0);
  $("#launcher-text").textContent = chat.busy ? `${statusText()}… ${elapsed()}` : chat.unread ? (chat.unread > 1 ? `${chat.unread} new answers` : "New answer") : "Ask Huddle";
  l.setAttribute("aria-label", chat.busy ? `Huddle is working: ${statusText()}. Open chat` : chat.unread ? "Huddle answered. Open chat" : "Open Ask Huddle");

  // Dot on the Ask Huddle link in the sidebar.
  const dot = $("#nav-dot");
  dot.className = "nav-dot" + (chat.busy ? " busy" : chat.unread ? " new" : "");
  dot.textContent = !chat.busy && chat.unread ? String(chat.unread) : "";
  dot.hidden = !chat.busy && !chat.unread;

  document.title = chat.unread ? `(${chat.unread}) Huddle` : chat.busy ? `${statusText()}… · Huddle` : "Huddle";
}

let ticker = null;
function startTicker() { if (!ticker) ticker = setInterval(chatChanged, 1000); }
function stopTicker() { clearInterval(ticker); ticker = null; }

function cancelOnServer() {
  const id = chat.pending && chat.pending.id;
  if (id) fetch("/api/chat/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chat_id: id }) }).catch(() => {});
}
function resetChat(provider) {
  if (chat.busy) cancelOnServer();
  if (chat.ctrl) chat.ctrl.abort();
  stopTicker();
  Object.assign(chat, { msgs: [], session: null, busy: false, pending: null, ctrl: null, gen: chat.gen + 1, unread: 0 });
  if (provider !== undefined) chat.provider = provider;
  clearToasts(); saveChat(); chatChanged();
}
function stopChat() { cancelOnServer(); if (chat.ctrl) chat.ctrl.abort(); }

// Earlier turns to send along. Skips failed answers and the questions they were answering.
function historyForServer() {
  const out = [];
  chat.msgs.forEach((m, i) => {
    if (m.error || m.note) return;
    if (m.role === "user") { const next = chat.msgs[i + 1]; if (!next || next.error || next.note) return; }
    out.push({ role: m.role, content: m.content });
  });
  return out;
}

async function send(text) {
  text = (text || "").trim();
  if (!text || chat.busy) return;
  askNotifyPermission();
  const gen = chat.gen;
  const history = historyForServer();
  chat.msgs.push({ role: "user", content: text });
  chat.busy = true;
  const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  chat.pending = { id, steps: [], live: "", phase: "thinking", started: Date.now() };
  const ctrl = new AbortController();
  chat.ctrl = ctrl;
  for (const el of [$("#msgs"), $("#dock-msgs")]) if (el) el._forceBottom = true;
  startTicker(); saveChat(); chatChanged();
  let result = null;
  try {
    const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, signal: ctrl.signal,
      body: JSON.stringify({ message: text, history, session: chat.session, chat_id: id }) });
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || d.detail || "Chat failed"); }
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      if (gen !== chat.gen) return;   // the chat was cleared while this was running
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch (_) { continue; }
        const p = chat.pending;
        if (ev.type === "tool") { p.steps.push(ev.label); p.phase = "tool"; p.live = ""; }
        else if (ev.type === "tool_done" || ev.type === "status") { if (p.phase !== "writing") p.phase = "thinking"; }
        else if (ev.type === "message_start") { p.live = ""; if (p.phase === "writing") p.phase = "thinking"; }
        else if (ev.type === "delta") { p.live += ev.text; p.phase = "writing"; }
        else if (ev.type === "answer") result = ev;
      }
      chatChanged();
    }
    if (!result) throw new Error("The answer got cut off. Try asking again.");
    const n = (result.tools_used || []).length;
    const meta = result.error ? "" : `${PROVIDER_NAMES[result.provider] || result.provider || "AI"}${result.model && result.model !== "default" ? " (" + result.model + ")" : ""}, ${n} lookup${n === 1 ? "" : "s"}`;
    finishChat(gen, { content: result.answer || "(no answer)", meta, error: !!result.error, fix: !!result.error }, result.session);
  } catch (e) {
    if (e.name === "AbortError") finishChat(gen, { content: "Stopped.", note: true, error: true });
    else finishChat(gen, { content: e.message || "Chat failed.", error: true });
  }
}

function finishChat(gen, msg, session) {
  if (gen !== chat.gen) return;
  const steps = chat.pending ? chat.pending.steps.slice() : [];
  const m = { role: "assistant", steps, ...msg };
  chat.msgs.push(m);
  if (session) chat.session = session;
  Object.assign(chat, { busy: false, pending: null, ctrl: null });
  stopTicker(); saveChat();
  if (!m.note) notifyAnswered(m);
  chatChanged();
}

// ------------------------------------------------------------------ chat panel, alerts
function openDock() {
  if (onAskPage()) { clearUnread(); window.scrollTo(0, document.body.scrollHeight); $("#q")?.focus(); return; }
  chat.dock = true;
  $("#dock").hidden = false;
  document.body.classList.add("dock-open");
  $("#dock-msgs")._forceBottom = true;
  clearUnread(); paintChat(); loadChatInfo();
  if (window.matchMedia("(min-width: 861px)").matches) $("#dock-q").focus();
}
function closeDock() {
  if (!chat.dock) return;
  chat.dock = false;
  $("#dock").hidden = true;
  document.body.classList.remove("dock-open");
  chatChanged();
}

function clearUnread() { chat.unread = 0; clearToasts(); chatChanged(); }
function clearToasts() { document.querySelectorAll("#toasts .toast").forEach(t => t.remove()); }

const plainText = s => String(s || "").replace(/[*`_]+/g, "").replace(/^\s*[-•]\s+/gm, "").replace(/[#|>]+/g, " ").replace(/\s+/g, " ").trim();
const canNotify = () => "Notification" in window && window.isSecureContext;
function askNotifyPermission() {
  if (canNotify() && Notification.permission === "default") {
    try { Notification.requestPermission().catch(() => {}); } catch (_) { /* old Safari */ }
  }
}

function notifyAnswered(m) {
  if (chatOnScreen()) return;
  chat.unread += 1;
  const title = m.error ? "Huddle couldn't answer" : "Huddle answered";
  const body = plainText(m.content).slice(0, 150) + (plainText(m.content).length > 150 ? "…" : "");
  toast(title, body, !!m.error);
  if (document.hidden && canNotify() && Notification.permission === "granted") {
    try {
      const n = new Notification(title, { body, tag: "huddle-answer" });
      n.onclick = () => { window.focus(); openDock(); n.close(); };
    } catch (_) { /* notifications unavailable */ }
  }
}

function toast(title, body, isError) {
  const box = $("#toasts");
  clearToasts();
  box.insertAdjacentHTML("beforeend", `<div class="toast ${isError ? "err" : ""}" role="status">
    <span class="t-ico" aria-hidden="true">${isError ? "!" : "✓"}</span>
    <div class="grow"><b>${esc(title)}</b><p>${esc(body)}</p></div>
    <div class="t-act"><button class="btn primary small" data-open>Read it</button><button class="t-x" aria-label="Dismiss">×</button></div></div>`);
  const t = box.lastElementChild;
  t.querySelector("[data-open]").onclick = () => { t.remove(); openDock(); };
  t.querySelector(".t-x").onclick = () => t.remove();
  requestAnimationFrame(() => t.classList.add("in"));
  const arm = () => setTimeout(() => t.remove(), 15000);
  if (document.hidden) document.addEventListener("visibilitychange", arm, { once: true }); else arm();
}

$("#launcher").onclick = openDock;
$("#dock-close").onclick = closeDock;
$("#dock-full").onclick = closeDock;
$("#dock-new").onclick = () => resetChat();
$("#dock-msgs").onclick = ideaClick;
bindComposer($("#dock-q"), $("#dock-send"));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && chatOnScreen() && chat.unread) clearUnread();
  chatChanged();
});
window.addEventListener("beforeunload", e => { if (chat.busy) { e.preventDefault(); e.returnValue = ""; } });

function md(text) {
  const lines = esc(text).split("\n");
  let html = "", list = null, table = [];
  const inline = s => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter(r => !/^\s*\|?\s*:?-{2,}/.test(r));
    html += "<table>" + rows.map((r, i) => "<tr>" + r.replace(/^\s*\||\|\s*$/g, "").split("|").map(c => i === 0 ? `<th>${inline(c.trim())}</th>` : `<td>${inline(c.trim())}</td>`).join("") + "</tr>").join("") + "</table>";
    table = [];
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\s*\|.*\|\s*$/.test(line)) { closeList(); table.push(line); continue; } else flushTable();
    let m;
    if ((m = line.match(/^#{1,4}\s+(.*)/))) { closeList(); html += `<h4>${inline(m[1])}</h4>`; }
    else if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
    else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { closeList(); }
    else if (!line.trim()) { closeList(); }
    else { closeList(); html += `<p>${inline(line)}</p>`; }
  }
  closeList(); flushTable();
  return html;
}

// ------------------------------------------------------------------ lineup
async function renderLineup() {
  const o = state.overview;
  const wk = Number(params().get("week")) || o.league.week;
  loading(true, "Optimizing your lineup…");
  const d = await api(`/api/lineup?week=${wk}`);
  loading(false);
  const f = fixes(d);
  view.innerHTML = `
    <div class="pagehead"><div><h1>Week ${wk} lineup</h1><p class="sub">Your best possible lineup in your league's scoring. Byes and players ruled out project to zero.</p></div>
      <div class="seg" id="weeks">${o.league.ros_weeks.map(w => `<button class="${w === wk ? "on" : ""}" data-w="${w}">Wk ${w}</button>`).join("")}</div></div>
    ${f.length ? `<div class="alert"><div><h3>${wk === o.league.week ? "Change before kickoff" : "Plan ahead"}</h3>${f.join("")}
      ${d.current_lineup_total !== null && d.swaps.length ? `<p class="small muted" style="margin:6px 0 0">Your current lineup projects ${fmt(d.current_lineup_total)}; this one projects ${fmt(d.projected_total)}.</p>` : ""}</div></div>` : ""}
    <div class="card"><ul class="board">${d.starters.map(s => boardRow(s, s.slot)).join("")}</ul>
      <div class="board-total"><span>Projected total</span><span class="v">${fmt(d.projected_total)}</span></div></div>
    <h2>Bench</h2>
    <div class="card"><ul class="board">${d.bench.map(b => boardRow(b, "BN")).join("") || '<li class="row"><span class="muted">Empty</span></li>'}</ul></div>`;
  $("#weeks").onclick = e => { const w = e.target.dataset.w; if (w) location.hash = `#lineup?week=${w}`; };
}

// ------------------------------------------------------------------ matchup
async function renderMatchup() {
  loading(true, "Projecting this week's matchup…");
  state.matchup = null;
  const d = await getMatchup();
  loading(false);
  if (!d.opponent) { view.innerHTML = `<h1>Week ${d.week} matchup</h1><p class="sub">Sleeper hasn't posted a matchup for you this week.</p>`; return; }
  const side = s => `<div><h3>${esc(s.team)}</h3><div class="card"><ul class="board">${s.starters.map(x => boardRow(x, x.slot)).join("")}</ul>
    <div class="board-total"><span>Projected</span><span class="v">${fmt(s.projected_total)}</span></div></div></div>`;
  view.innerHTML = `<div class="pagehead"><div><h1>You vs ${esc(d.opponent.team)}</h1><p class="sub">Both teams' best lineups for week ${d.week}.</p></div></div>
    <div style="margin-bottom:22px">${scoreboard(d)}</div>
    <div class="grid2">${side(d.you)}${side(d.opponent)}</div>`;
}

// ------------------------------------------------------------------ trades
const rk = p => `${esc(p.name)} ${p.consensus_rank ? "#" + Math.round(p.consensus_rank) : "(unranked)"}`;

function renderTrades() {
  const mode = params().get("mode") || "analyze";
  view.innerHTML = `<div class="pagehead"><div><h1>Trades</h1><p class="sub">Every trade is replayed week by week for both teams, then checked against what players are worth in your league so it's fair to them too.</p></div>
    <div class="seg" id="tmode"><button data-m="analyze" class="${mode === "analyze" ? "on" : ""}">Analyze a trade</button><button data-m="find" class="${mode === "find" ? "on" : ""}">Find trades</button></div></div>
    <div id="tbody"></div>`;
  $("#tmode").onclick = e => { const m = e.target.dataset.m; if (m) location.hash = `#trades?mode=${m}`; };
  return mode === "find" ? renderFinder() : renderAnalyzer();
}

function picker(side, scope) {
  return `<div class="card pad side-pick" data-side="${side}">
    <h3>${side === "give" ? "You give" : "You get"}</h3>
    <div class="picker"><input type="text" placeholder="${side === "give" ? "Search your roster" : "Search other teams"}" data-scope="${scope}" aria-label="${side === "give" ? "Add a player you give" : "Add a player you get"}" autocomplete="off">
    <div class="suggest" hidden></div></div>
    <div class="chips">${state.trade[side].map(p => `<div class="chip">${avatar(p, "sm")}<div class="grow">${pname(p)}<div class="small muted">${esc(p.pos)}, ${esc(p.team || "FA")}${side === "get" ? ", " + esc(p.owner) : ""}${p.consensus_rank ? `, #${Math.round(p.consensus_rank)} overall` : ""}</div></div>
      <button class="rm" data-rm="${esc(p.id)}" aria-label="Remove ${esc(p.name)}">×</button></div>`).join("") || '<span class="small muted">No players yet</span>'}</div>
  </div>`;
}

function renderAnalyzer() {
  const body = $("#tbody");
  body.innerHTML = `<div class="grid2">${picker("give", "mine")}${picker("get", "others")}</div>
    <div class="toolbar" style="margin-top:16px"><button class="btn primary" id="analyze" ${state.trade.give.length && state.trade.get.length ? "" : "disabled"}>Analyze trade</button>
    ${state.trade.give.length || state.trade.get.length ? '<button class="btn ghost" id="clear">Clear</button>' : ""}</div>
    <div id="tresult"></div>`;
  body.querySelectorAll(".side-pick").forEach(sideEl => {
    const side = sideEl.dataset.side;
    const input = $("input", sideEl), box = $(".suggest", sideEl);
    let timer;
    input.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim();
        if (q.length < 2) { box.hidden = true; return; }
        const res = await api(`/api/players/search?q=${encodeURIComponent(q)}&scope=${input.dataset.scope}`);
        box.innerHTML = res.map(p => `<button data-add='${esc(JSON.stringify(p))}'>${avatar(p, "sm")}<span>${esc(p.name)} <span class="small muted">${esc(p.pos)}, ${esc(p.team || "FA")}, ${esc(p.owner)}</span></span></button>`).join("")
          || '<div class="small muted" style="padding:10px 12px">No match</div>';
        box.hidden = false;
      }, 180);
    };
    box.onclick = e => {
      const b = e.target.closest("button[data-add]"); if (!b) return;
      const p = JSON.parse(b.dataset.add);
      if (!state.trade[side].some(x => x.id === p.id)) state.trade[side].push(p);
      state.trade.result = null;
      renderAnalyzer();
      $(`.side-pick[data-side=${side}] input`).focus();
    };
    sideEl.addEventListener("click", e => {
      const rm = e.target.closest("button[data-rm]"); if (!rm) return;
      state.trade[side] = state.trade[side].filter(x => x.id !== rm.dataset.rm);
      state.trade.result = null;
      renderAnalyzer();
    });
  });
  const clr = $("#clear"); if (clr) clr.onclick = () => { state.trade = { give: [], get: [], result: null }; renderAnalyzer(); };
  $("#analyze").onclick = runAnalysis;
  if (state.trade.result) showTradeResult(state.trade.result);
}

async function runAnalysis() {
  const out = $("#tresult");
  out.innerHTML = `<div class="loading"><div class="ball"></div><p>Replaying the rest of the season with and without this trade…</p></div>`;
  try {
    const r = await api("/api/trade/analyze", { method: "POST",
      body: JSON.stringify({ give: state.trade.give.map(p => p.id), get: state.trade.get.map(p => p.id) }) });
    state.trade.result = r;
    showTradeResult(r);
  } catch (e) { out.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}

function gradingList(items) {
  if (!items) return "";
  return `<ul class="checks">${items.map(g => `<li class="${g.pass ? "ok" : "no"}"><span class="mark" aria-hidden="true">${g.pass ? "✓" : "✗"}</span>
    <span><strong>${esc(g.test)}</strong> <span class="${g.pass ? "good" : "bad"}">${esc(g.value)}</span><br><span class="small muted">${esc(g.rule)}</span></span></li>`).join("")}</ul>`;
}

function showTradeResult(r) {
  const out = $("#tresult"); if (!out) return;
  const tone = r.verdict.tone;
  const max = Math.max(...r.weeks.map(w => Math.max(w.before, w.after)), 1);
  const bars = r.weeks.map(w => `<div class="wkcol ${w.playoffs ? "po" : ""}" title="Week ${w.week}: ${fmt(w.before)} now, ${fmt(w.after)} after">
      <span class="d ${toneOf(w.delta)}">${signed(w.delta)}</span>
      <div class="bars"><div class="b before" style="height:${(100 * w.before / max).toFixed(0)}px"></div>
      <div class="b after ${w.delta >= 0 ? "up" : "down"}" style="height:${(100 * w.after / max).toFixed(0)}px"></div></div>
      <span class="l">Wk ${w.week}</span></div>`).join("");
  const fa = x => x.free_agent ? ' <span class="small muted">(waivers)</span>' : "";
  const lineups = `<table class="data"><thead><tr><th>Slot</th><th>Now</th><th class="n"></th><th>After the trade</th><th class="n"></th></tr></thead><tbody>
    ${r.lineup_now.map((a, i) => { const b = r.lineup_after[i];
      return `<tr><td class="muted">${esc(a.slot)}</td><td>${esc(a.name)}${fa(a)}</td><td class="n">${fmt(a.proj)}</td><td${a.name !== b.name ? ' style="font-weight:600"' : ""}>${esc(b.name)}${fa(b)}</td><td class="n">${fmt(b.proj)}</td></tr>`; }).join("")}
    </tbody></table>`;
  const acc = r.acceptance.call;
  out.innerHTML = `
    <div class="card" style="margin-top:6px">
      <div class="verdict">
        <div class="big ${tone}">${signed(r.you.per_week_delta)}<small>lineup pts per week</small></div>
        <div class="call ${tone}">${esc(r.verdict.call)}</div>
        <div>${esc(r.verdict.why)} ${signed(r.you.ros_delta)} over the rest of the season, ${signed(r.you.this_week_delta)} this week.</div>
      </div>
      <div class="weeks">${bars}</div>
      <div class="legend"><span><i style="background:var(--line-2)"></i>Lineup now</span><span><i style="background:var(--gain)"></i>After the trade</span><span style="color:var(--pylon)">Orange weeks are your fantasy playoffs</span></div>
    </div>
    <div class="grid2" style="margin-top:18px">
      <div class="card pad"><h3>Will ${esc(r.partner.name)} say yes? <span class="${acc === "Likely" ? "good" : acc === "Unlikely" ? "bad" : "even"}">${esc(acc)}</span></h3>
        <p class="small" style="margin:0 0 10px">${esc(r.acceptance.why)}</p>${gradingList(r.grading)}</div>
      <div class="card pad"><h3>What changes for you</h3><ul class="small" style="padding-left:18px;margin:0 0 12px">${r.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>
        <div class="kv"><div><span>You give</span><br>${r.give.map(rk).join("<br>")}</div><div><span>You get</span><br>${r.get.map(rk).join("<br>")}</div>
        <div><span>They'd drop</span><br>${r.them.drops.map(p => esc(p.name)).join(", ") || "nobody"}</div><div><span>Their lineup</span><br><span class="${toneOf(r.them.per_week_delta)}">${signed(r.them.per_week_delta)} pts/wk</span></div></div></div>
    </div>
    <h2>Your lineup this week</h2><div class="card tablewrap">${lineups}</div>
    ${r.you.assumed_pickups.length ? `<p class="small muted">Both lineups include the waiver pickups Huddle recommends anyway (${r.you.assumed_pickups.map(esc).join(", ")}), so the difference is the trade alone.</p>` : ""}`;
}

const GRADING_HELP = `<details class="help"><summary>How Huddle grades trades</summary>
  <ol>
    <li><strong>It helps your starting lineup</strong> by at least 0.5 points a week, replayed for every remaining week with byes, injuries, roster limits and who you'd cut. Playoff weeks count extra.</li>
    <li><strong>It doesn't hurt theirs.</strong> The same replay runs for their team. Costing them more than 0.1 points a week makes it "possible" at best. If they'd give up the best player in the deal, their lineup can't get worse at all.</li>
    <li><strong>It's fair to them by trade value</strong>: what a player is worth (65% FantasyPros' expert rest-of-season ranking, 35% Huddle's projections) above what your league's waiver wire offers. Depth players count for little, so two of them don't buy a star. Anyone they'd have to cut counts against the deal, and the team giving up the best player needs about 1.1x back.</li>
  </ol></details>`;

async function renderFinder() {
  const body = $("#tbody");
  const teams = state.overview.teams.filter(t => t.roster_id !== state.overview.team.roster_id);
  body.innerHTML = `<div class="toolbar">
      <div class="seg" id="fpos">${["", "QB", "RB", "WR", "TE"].map(p => `<button data-p="${p}" class="${state.finder.pos === p ? "on" : ""}">${p || "Any position"}</button>`).join("")}</div>
      <select id="fpartner" style="width:auto"><option value="">Any team</option>${teams.map(t => `<option value="${t.roster_id}" ${String(state.finder.partner) === String(t.roster_id) ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
      <button class="btn primary" id="fgo">Find trades</button></div>
    ${GRADING_HELP}<div id="fres"></div>`;
  $("#fpos").onclick = e => { if (e.target.dataset.p !== undefined) { state.finder.pos = e.target.dataset.p; state.finder.ideas = null; renderFinder(); } };
  $("#fpartner").onchange = e => { state.finder.partner = e.target.value; state.finder.ideas = null; };
  const show = ideas => {
    const res = $("#fres");
    res.innerHTML = ideas.length ? `<div class="card">${ideas.map((r, i) => `<div class="idea">
        <div><div class="deal">${r.give.map(p => esc(p.name)).join(" + ")}<span class="arrow">for</span>${r.get.map(p => esc(p.name)).join(" + ")}</div>
          <div class="small muted">With ${esc(r.partner.name)}. Consensus: ${r.give.map(rk).join(", ")} for ${r.get.map(rk).join(", ")}.</div>
          <div class="small">Their lineup <span class="${toneOf(r.them.per_week_delta)}">${signed(r.them.per_week_delta)}/wk</span>. Acceptance: <strong class="${r.acceptance.call === "Likely" ? "good" : "even"}">${esc(r.acceptance.call.toLowerCase())}</strong>.</div></div>
        <div><div class="gain">${signed(r.you.per_week_delta)}<span> /wk</span></div><button class="btn small" data-open="${i}">Open</button></div></div>`).join("")}</div>`
      : `<p>No trades pass every check right now. Try another position or team, or check the waiver wire.</p>`;
    res.onclick = e => {
      const b = e.target.closest("button[data-open]"); if (!b) return;
      const r = ideas[Number(b.dataset.open)];
      state.trade = { give: r.give, get: r.get, result: null };
      location.hash = "#trades?mode=analyze";
      setTimeout(runAnalysis, 60);
    };
  };
  if (state.finder.ideas) show(state.finder.ideas);
  $("#fgo").onclick = async () => {
    $("#fres").innerHTML = `<div class="loading"><div class="ball"></div><p>Testing thousands of trades against both lineups…</p></div>`;
    try {
      const q = new URLSearchParams(); if (state.finder.pos) q.set("position", state.finder.pos); if (state.finder.partner) q.set("partner", state.finder.partner);
      state.finder.ideas = await api(`/api/trade/find?${q}`);
      show(state.finder.ideas);
    } catch (e) { $("#fres").innerHTML = `<div class="error">${esc(e.message)}</div>`; }
  };
}

// ------------------------------------------------------------------ waivers
const WAIVER_VIEWS = [["best", "Best for you"], ["week", "This week"], ["stash", "Stashes"], ["playoffs", "Playoffs"]];

function moveText(m) {
  if (!m) return "";
  if (m.kind === "open") return `<span class="muted">Open roster spot</span>`;
  if (m.kind === "ir") return `Move <strong>${esc(m.player)}</strong> to IR`;
  return `Drop <strong>${esc(m.player)}</strong>`;
}

async function renderWaivers() {
  const pr = params(), pos = pr.get("pos") || "", tab = pr.get("view") || "best";
  loading(true, "Checking every free agent against your roster…");
  const d = await api(`/api/waivers${pos ? "?position=" + pos : ""}`);
  loading(false);
  const nd = d.needs;
  const needChips = [
    ...nd.weakest.map(n => `<span class="need"><b>${esc(n.pos)}</b> ${ordinal(n.rank)} of ${n.of} in the league</span>`),
    ...nd.holes.slice(0, 3).map(h => `<span class="need warn"><b>Week ${h.week}</b> ${esc(h.slot)}: ${esc(h.player || "empty")} (${esc(h.reason)})</span>`),
    d.faab ? `<span class="need"><b>FAAB</b> $${d.faab.left} of $${d.faab.budget} left</span>` : "",
  ].join("");
  const recs = d.recommendations.map(r => `<div class="card rec">
      <div class="rec-head">${avatar(r)}<div class="grow">${pname(r)}<div class="small muted"><span class="pos ${esc(r.pos)}">${esc(r.pos)}</span> ${esc(r.team || "")} ${esc(r.opp || "")}</div></div>
        <div class="rec-gain"><b>${signed(r.net_gain)}</b><span>pts rest of season</span></div></div>
      <div class="rec-nums"><span>This week <b class="${toneOf(r.this_week_gain)}">${signed(r.this_week_gain)}</b></span><span>Playoffs <b class="${toneOf(r.playoff_gain)}">${signed(r.playoff_gain)}</b></span><span>Starts <b>${r.starts}/${r.weeks}</b> wks</span></div>
      <ul class="rec-why">${r.reasons.map(x => `<li>${esc(x)}</li>`).join("")}</ul>
      <div class="rec-move">${moveText(r.move)}${r.bid ? `<span class="bid">Bid $${r.bid.low}–$${r.bid.high}</span>` : ""}</div>
    </div>`).join("");
  const list = (d.views[tab] || []).slice(0, 12);
  const rows = list.map(r => `<tr><td>${pcell(r, esc(`${r.pos}, ${r.team || ""} ${r.opp || ""}`))}</td>
      <td class="why small" title="${esc(r.reasons.join(". "))}">${esc(r.reasons[0] || "")}<div class="muted">${moveText(r.move)}</div></td>
      <td class="n ${toneOf(r.this_week_gain)}">${signed(r.this_week_gain)}</td>
      <td class="n ${toneOf(r.net_gain)}"><strong>${signed(r.net_gain)}</strong></td>
      <td class="n ${toneOf(r.playoff_gain)}">${signed(r.playoff_gain)}</td>
      <td class="n">${r.starts}/${r.weeks}</td>
      <td class="n">${r.snap_last3 != null ? fmt(r.snap_last3, 0) + "%" : "–"}${r.snap_trend != null && Math.abs(r.snap_trend) >= 5 ? ` <span class="${toneOf(r.snap_trend)} small">${r.snap_trend > 0 ? "▲" : "▼"}</span>` : ""}</td></tr>`).join("");
  const board = d.drop_board.map(b => `<div class="dropline"><div class="pcell">${avatar(b, "sm")}<div>${pname(b)}<div class="m">${esc(b.reasons.slice(0, 2).join(". "))}.</div></div></div>
      <span class="verdict-tag ${b.verdict.replace(/ /g, "")}">${esc(b.verdict)}</span></div>`).join("");
  view.innerHTML = `<div class="pagehead"><div><h1>Waiver wire</h1><p class="sub">Who's worth adding for your team, and the smartest way to make room, judged over the rest of the season and your playoffs.</p></div>
    <div class="seg" id="wpos">${["", "QB", "RB", "WR", "TE", "K", "DEF"].map(p => `<button data-p="${p}" class="${pos === p ? "on" : ""}">${p || "All"}</button>`).join("")}</div></div>
    ${needChips ? `<div class="needs"><span class="small muted">Your needs</span>${needChips}</div>` : ""}
    ${recs ? `<div class="recs">${recs}</div>` : `<div class="card pad"><p style="margin:0">Nobody on waivers improves your team right now${pos ? " at " + esc(pos) : ""}. Your bench is already better than what's out there.</p></div>`}
    <div class="wgrid">
      <div><div class="toolbar" style="margin-top:22px"><div class="seg" id="wview">${WAIVER_VIEWS.map(([k, l]) => `<button data-v="${k}" class="${tab === k ? "on" : ""}">${l}</button>`).join("")}</div>
        <span class="small muted">${{ best: "Ranked for your season", week: "Who helps most this week (byes, injuries)", stash: "Rising roles and injury fill-ins to grab early", playoffs: "Most lineup points in your fantasy playoffs" }[tab]}</span></div>
      <div class="card tablewrap"><table class="data"><thead><tr><th>Player</th><th>Why</th><th class="n">This week</th><th class="n">Season</th><th class="n">Playoffs</th><th class="n">Starts</th><th class="n">Snaps L3</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7" class="muted">Nobody fits here right now.</td></tr>'}</tbody></table></div></div>
      <div><h2 style="margin-top:22px">Your drop list</h2><div class="card pad droplist">${board || '<p class="small muted" style="margin:0">Nothing to drop.</p>'}</div>
        <p class="small muted">Judged long term: weeks he'd start for you, injury length, IR slots and trade value. Short-term injuries don't make a player droppable.</p></div>
    </div>`;
  $("#wpos").onclick = e => { if (e.target.dataset.p !== undefined) location.hash = `#waivers?pos=${e.target.dataset.p}&view=${tab}`; };
  $("#wview").onclick = e => { if (e.target.dataset.v) location.hash = `#waivers?pos=${pos}&view=${e.target.dataset.v}`; };
}
const ordinal = n => n + (["th", "st", "nd", "rd"][((n % 100) - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");

// ------------------------------------------------------------------ report card (backtest)
// Huddle replays every finished week using only what it knew before kickoff, then grades itself.
const mixText = b => `${Math.round(b * 100)}/${Math.round((1 - b) * 100)}`;
const recText = r => `${r.W}-${r.L}${r.T ? "-" + r.T : ""}`;
const pct = v => (v === null || v === undefined ? "–" : `${fmt(v, 0)}%`);

const BACKTEST_HELP = `<details class="help"><summary>What is a backtest?</summary>
  <p>A backtest checks a prediction tool against games that have already happened. For every finished week, Huddle pretends it's the morning of kickoff: it only uses stats from earlier weeks, that week's Vegas lines and injury report, and Sleeper's projection for that week. Then it compares what it would have said with what really happened. It's the honest way to know whether Huddle's advice is any good.</p>
  <ul>
    <li><strong>Points off per player</strong>: the average gap between a projection and the real score. Lower is better. Even the best projections miss by 5 to 7 points; fantasy football is noisy.</li>
    <li><strong>Picked the better of two</strong>: for every two players at the same position in the same week, how often the one projected higher actually scored more. That's the start/sit question. 50% would be a coin flip.</li>
    <li><strong>Over/under</strong>: whether projections ran high (+) or low (−) on average.</li>
    <li><strong>Lineup efficiency</strong>: what a lineup scored as a share of the best lineup you could have set in hindsight.</li>
    <li><strong>Sleeper/Huddle mix</strong>: Huddle's projection is a blend of Sleeper's projection and its own model (recent form, matchup, Vegas line). The backtest tries every mix to find the most accurate one.</li>
  </ul></details>`;

async function renderReport() {
  const refresh = params().get("refresh") === "1";
  loading(true, "Replaying the season so far… The first run downloads every past week from Sleeper, so it can take a little while.");
  const r = await api("/api/backtest" + (refresh ? "?refresh=1" : ""));
  loading(false);
  if (refresh) history.replaceState(null, "", "#report");
  if (!r.ready) {
    view.innerHTML = `<div class="pagehead"><div><h1>Report card</h1></div></div>${BACKTEST_HELP}<div class="card pad"><p style="margin:0">${esc(r.message || "Nothing to replay yet.")}</p></div>`;
    return;
  }
  const ws = r.weeks, span = ws.length > 1 ? `weeks ${ws[0]}–${ws[ws.length - 1]}` : `week ${ws[0]}`;
  const a = r.accuracy || {}, lu = r.lineups || {};
  view.innerHTML = `<div class="pagehead"><div><h1>Report card</h1>
      <p class="sub">Huddle replayed ${span} using only what it knew before each kickoff, then checked itself against what actually happened.</p></div>
      <button class="btn ghost" id="rerun">Run again</button></div>
    ${BACKTEST_HELP}
    <div class="card pad report-sum"><h3>The short version</h3><ul>${r.summary.map(s => `<li>${esc(s)}</li>`).join("")}</ul></div>
    ${lu.available ? lineupSection(lu) : ""}
    ${a.n ? accuracySection(a, r) : ""}
    <ul class="small muted report-notes">${r.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`;
  $("#rerun").onclick = () => { location.hash = "#report?refresh=1"; };
  const use = $("#use-blend");
  if (use) use.onclick = async () => {
    use.disabled = true; use.textContent = "Saving…";
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({ projection_blend: Number(use.dataset.b) }) });
      state.overview = null; state.matchup = null; chat.ideas = null;
      route();
    } catch (e) { use.disabled = false; use.textContent = e.message; }
  };
}

function lineupSection(lu) {
  const d = lu.huddle_vs_you, e = lu.efficiency, rec = lu.record;
  const flipText = lu.flips.length
    ? lu.flips.map(f => `week ${f.week} (${f.from === "W" ? "win" : "loss"} → ${f.to === "W" ? "win" : f.to === "L" ? "loss" : "tie"})`).join(", ")
    : "no results would have changed";
  const deltas = lu.weeks.map(w => w.huddle - w.you);
  const maxUp = Math.max(0, ...deltas), maxDown = Math.max(0, ...deltas.map(x => -x));
  const scale = 84 / Math.max(maxUp, maxDown, 1);
  const cols = lu.weeks.map(w => {
    const dd = w.huddle - w.you, up = dd > 0 ? dd * scale : 0, down = dd < 0 ? -dd * scale : 0;
    const flip = w.result && w.result_huddle && w.result !== w.result_huddle;
    const tip = `Week ${w.week}: you scored ${fmt(w.you)}, Huddle's lineup ${fmt(w.huddle)} (${signed(dd)}). Best possible ${fmt(w.best)}.`;
    return `<div class="dcol" title="${esc(tip)}">
      <div class="up" style="height:${(maxUp * scale + 22).toFixed(0)}px">${dd > 0.05 ? `<span class="lbl">${signed(dd)}</span>` : ""}<i style="height:${up.toFixed(0)}px"></i></div>
      <div class="down" style="height:${(maxDown * scale + (maxDown ? 22 : 4)).toFixed(0)}px"><i style="height:${down.toFixed(0)}px"></i>${dd < -0.05 ? `<span class="lbl">${signed(dd)}</span>` : ""}</div>
      <span class="wk">Wk ${w.week}</span>${flip ? `<span class="flip ${w.result_huddle === "W" ? "good" : "bad"}">${w.result}→${w.result_huddle}</span>` : ""}</div>`;
  }).join("");
  const calls = w => {
    if (!w.huddle_would_start.length) return '<span class="muted">Same lineup as yours</span>';
    const one = c => `${esc(c.name)} <span class="muted">${fmt(c.pts)}</span>`;
    return `Start ${w.huddle_would_start.map(one).join(", ")}<br><span class="muted">over</span> ${w.you_started_instead.map(one).join(", ") || "an empty slot"}`;
  };
  const res = x => x ? `<span class="${x === "W" ? "good" : x === "L" ? "bad" : "even"}">${x}</span>` : "–";
  return `<h2>Your lineups</h2>
    <div class="grid3">
      <div class="card stat"><div class="k">Huddle's lineups vs yours</div><div class="v ${toneOf(d)}">${signed(d)}</div>
        <div class="small muted">points a week. Better in ${lu.huddle_better_weeks} of ${lu.n} weeks, worse in ${lu.you_better_weeks}.</div></div>
      <div class="card stat"><div class="k">Your record → with Huddle's lineups</div><div class="v">${recText(rec.you)} → ${recText(rec.huddle)}</div>
        <div class="small muted">${esc(flipText.charAt(0).toUpperCase() + flipText.slice(1))}.</div></div>
      <div class="card stat"><div class="k">Lineup efficiency</div><div class="v">${pct(e.you)} → ${pct(e.huddle)}</div>
        <div class="small muted">Share of the best possible score: yours, then Huddle's. League average ${pct(e.league)}.</div></div>
    </div>
    <div class="card" style="margin-top:16px">
      <h3 class="pad" style="padding-bottom:0;margin:0">Huddle's lineup minus yours, each week</h3>
      <div class="dchart" role="img" aria-label="Points Huddle's lineup would have scored minus your actual lineup, by week. The table below has every number.">${cols}</div>
      <div class="legend"><span><i style="background:var(--gain)"></i>Huddle's lineup scored more</span><span><i style="background:var(--loss)"></i>Your lineup scored more</span><span>W→L marks a result that would have changed</span></div>
    </div>
    <div class="card tablewrap" style="margin-top:16px"><table class="data"><thead><tr><th class="n">Week</th><th>Opponent</th><th class="n">You</th><th class="n">Huddle</th><th class="n">Sleeper</th><th class="n">Best possible</th><th class="n">Opp.</th><th>Result</th><th>Huddle's calls (actual points)</th></tr></thead>
      <tbody>${lu.weeks.map(w => `<tr><td class="n">${w.week}</td><td>${esc(w.opponent_name || "–")}</td><td class="n">${fmt(w.you)}</td>
        <td class="n ${toneOf(w.huddle - w.you)}">${fmt(w.huddle)}</td><td class="n">${fmt(w.sleeper)}</td><td class="n muted">${fmt(w.best)}</td><td class="n">${fmt(w.opponent)}</td>
        <td>${res(w.result)}${w.result_huddle && w.result_huddle !== w.result ? ` → ${res(w.result_huddle)}` : ""}</td><td class="small calls">${calls(w)}</td></tr>`).join("")}</tbody>
      <tfoot><tr><td></td><td><strong>Per week</strong></td><td class="n"><strong>${fmt(lu.per_week.you)}</strong></td><td class="n"><strong>${fmt(lu.per_week.huddle)}</strong></td>
        <td class="n"><strong>${fmt(lu.per_week.sleeper)}</strong></td><td class="n muted">${fmt(lu.per_week.best)}</td><td></td><td>${recText(rec.you)}</td><td></td></tr></tfoot></table></div>`;
}

function accuracySection(a, r) {
  const order = ["huddle", "sleeper", "model", "naive"].filter(k => a.methods[k]);
  const best = Math.min(...order.map(k => a.methods[k].mae));
  const rows = order.map(k => { const m = a.methods[k];
    return `<tr class="${k === "huddle" ? "me" : ""}"><td><strong>${esc(m.name)}</strong>${k === "huddle" ? ` <span class="muted small">(${mixText(r.blend)} Sleeper/model)</span>` : ""}</td>
      <td class="n ${m.mae === best ? "good" : ""}">${fmt(m.mae, 2)}</td><td class="n">${pct(m.within5)}</td><td class="n">${pct(m.head_to_head)}</td><td class="n">${signed(m.bias, 1)}</td></tr>`; }).join("");
  const posRows = Object.entries(a.by_position).map(([pos, v]) => {
    const vals = order.map(k => v[k]); const lo = Math.min(...vals);
    return `<tr><td><strong>${pos}</strong> <span class="muted small">${v.n}</span></td>${order.map(k => `<td class="n ${v[k] === lo ? "good" : ""}">${fmt(v[k], 2)}</td>`).join("")}<td class="n">${signed(v.huddle_bias)}</td></tr>`; }).join("");
  const b = a.blend;
  let blend = "";
  if (b) {
    const head = b.recommend
      ? `<p style="margin:0 0 10px">A <strong>${mixText(b.best)}</strong> Sleeper/model mix would have been more accurate than your ${mixText(b.current)} (${fmt(b.best_mae, 2)} vs ${fmt(b.current_mae, 2)} points off, better in ${b.better_weeks} of ${b.weeks} weeks).</p>
         <button class="btn primary" id="use-blend" data-b="${b.best}">Use ${mixText(b.best)}</button>`
      : `<p style="margin:0">Your ${mixText(b.current)} mix is already about as accurate as any other (best was ${mixText(b.best)} at ${fmt(b.best_mae, 2)} vs your ${fmt(b.current_mae, 2)}). Huddle only suggests a change when it's clearly better and holds up in most weeks.</p>`;
    blend = `<div class="card pad" style="margin-top:16px"><h3>Sleeper/Huddle mix</h3>${head}
      <details class="small" style="margin-top:10px"><summary>Every mix Huddle tried</summary>
      <table class="data" style="max-width:360px"><thead><tr><th>Sleeper/model</th><th class="n">Points off</th></tr></thead>
      <tbody>${b.curve.map(c => `<tr class="${Math.abs(c.blend - b.current) < 0.01 ? "me" : ""}"><td>${mixText(c.blend)}${c.blend === b.best ? ' <span class="good">best</span>' : ""}${Math.abs(c.blend - b.current) < 0.01 ? ' <span class="muted">yours</span>' : ""}</td><td class="n">${fmt(c.mae, 3)}</td></tr>`).join("")}</tbody></table></details></div>`;
  }
  const misses = (a.biggest_misses || []).map(m => `<tr><td class="n">${m.week}</td><td>${esc(m.name)} <span class="muted small">${esc(m.pos)}</span></td><td class="n">${fmt(m.projected)}</td><td class="n">${fmt(m.actual)}</td><td class="n">${signed(m.actual - m.projected)}</td></tr>`).join("");
  return `<h2>Projection accuracy</h2>
    <p class="sub">${a.n.toLocaleString()} player-games: ${a.players} players who were startable or on a roster in your league, all graded on the same games.</p>
    <div class="card tablewrap"><table class="data"><thead><tr><th>Projection</th><th class="n">Points off per player</th><th class="n">Within 5 pts</th><th class="n">Picked the better of two</th><th class="n">Over/under</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${blend}
    <div class="grid2" style="margin-top:16px">
      <div class="card tablewrap"><h3 class="pad" style="padding-bottom:0;margin:0">Points off, by position</h3><table class="data"><thead><tr><th>Pos</th>${order.map(k => `<th class="n">${esc(a.methods[k].name.replace("Huddle's model alone", "Model").replace("Season average so far", "Season avg").replace("Sleeper alone", "Sleeper"))}</th>`).join("")}<th class="n">Huddle over/under</th></tr></thead><tbody>${posRows}</tbody></table></div>
      <div class="card tablewrap"><h3 class="pad" style="padding-bottom:0;margin:0">Biggest misses</h3><table class="data"><thead><tr><th class="n">Wk</th><th>Player</th><th class="n">Projected</th><th class="n">Scored</th><th class="n">Off by</th></tr></thead><tbody>${misses}</tbody></table></div>
    </div>`;
}

// ------------------------------------------------------------------ league
async function renderLeague() {
  const p = params(), tab = p.get("tab") || "standings";
  const head = `<div class="pagehead"><div><h1>League</h1></div>
    <div class="seg" id="ltab"><button data-t="standings" class="${tab === "standings" ? "on" : ""}">Standings</button><button data-t="defense" class="${tab === "defense" ? "on" : ""}">Defense vs position</button></div></div>`;
  if (tab === "defense") {
    const pos = p.get("pos") || "WR";
    const d = await api(`/api/dvp?position=${pos}`);
    view.innerHTML = head + `<p class="sub">Fantasy points each defense allows per game to ${esc(pos === "DEF" ? "D/STs" : pos + "s")}, in your scoring. Rank 1 is the softest matchup.</p>
      <div class="toolbar"><div class="seg" id="dpos">${["QB", "RB", "WR", "TE", "K", "DEF"].map(x => `<button data-p="${x}" class="${pos === x ? "on" : ""}">${x === "DEF" ? "D/ST" : x}</button>`).join("")}</div></div>
      <div class="card tablewrap"><table class="data"><thead><tr><th class="n">Rank</th><th>Defense</th><th class="n">Allowed / game</th><th class="n">vs average</th><th>Matchup</th></tr></thead>
      <tbody>${d.map(r => `<tr><td class="n">${r.rank}</td><td>${esc(r.name)} <span class="muted small">${esc(r.team)}</span></td><td class="n">${fmt(r.allowed_ppg)}</td>
        <td class="n ${toneOf(r.mult - 1)}">${signed(100 * (r.mult - 1), 0)}%</td><td>${pill(r.label)}</td></tr>`).join("")}</tbody></table></div>`;
    $("#dpos").onclick = e => { if (e.target.dataset.p) location.hash = `#league?tab=defense&pos=${e.target.dataset.p}`; };
  } else {
    const rid = Number(p.get("team")) || 0;
    const rows = await api("/api/standings");
    let roster = "";
    if (rid) {
      const r = await api(`/api/roster?roster_id=${rid}`);
      const team = rows.find(x => x.roster_id === rid);
      roster = `<h2>${esc(team ? team.team : "Roster")}</h2><div class="card tablewrap"><table class="data"><thead><tr><th>Player</th><th class="n">This week</th><th class="n">ROS/game</th><th class="n">Trade value</th><th class="n">Bye</th></tr></thead>
        <tbody>${r.map(x => `<tr><td>${pcell(x)}</td><td class="n">${fmt(x.proj)}</td><td class="n">${fmt(x.ros_ppg)}</td><td class="n">${fmt(x.value, 0)}</td><td class="n">${x.bye || "–"}</td></tr>`).join("")}</tbody></table></div>`;
    }
    view.innerHTML = head + `<p class="sub">Power rank orders teams by how strong their best lineup is over the rest of the season. Select a team to see its roster.</p>
      <div class="card tablewrap"><table class="data"><thead><tr><th>Team</th><th class="n">Record</th><th class="n">Points for</th><th class="n">Against</th><th class="n">Lineup / wk ahead</th><th class="n">Power</th></tr></thead>
      <tbody>${rows.map(r => `<tr class="${r.is_you ? "me" : ""}"><td><a href="#league?team=${r.roster_id}"><strong>${esc(r.team)}</strong></a> <span class="muted small">${esc(r.manager)}</span></td>
        <td class="n">${r.wins}-${r.losses}${r.ties ? "-" + r.ties : ""}</td><td class="n">${fmt(r.points_for)}</td><td class="n">${fmt(r.points_against)}</td><td class="n">${fmt(r.ros_lineup_ppg)}</td><td class="n">#${r.power_rank}</td></tr>`).join("")}</tbody></table></div>
      ${roster}`;
  }
  $("#ltab").onclick = e => { const t = e.target.dataset.t; if (t) location.hash = `#league?tab=${t}`; };
}

// ------------------------------------------------------------------ settings
const AI_CHOICES = [
  ["claude", "Claude", "Uses your Claude subscription"],
  ["chatgpt", "ChatGPT", "Uses your ChatGPT subscription"],
  ["ollama", "Free", "A local model on this Mac"],
  ["api", "API key", "Pay per question"],
];

async function renderSettings(problem) {
  loading(false);
  const { settings: s } = await api("/api/settings");
  const form = { provider: s.ai_provider, vendor: s.api_vendor || "anthropic", theme: s.theme || "field" };
  view.innerHTML = `<div class="settings">
    <h1>Settings</h1>
    ${problem ? `<div class="error" style="margin:12px 0 18px"><strong>${esc(problem)}</strong></div>` : '<p class="sub">Everything stays on this Mac.</p>'}
    <div class="section"><h3>Your league</h3><p class="sub">Huddle reads your Sleeper league. No password needed.</p>
      <div class="inline"><label class="field"><span>Sleeper username</span><input type="text" id="s-user" value="${esc(s.sleeper_username)}" autocomplete="username"></label>
        <button class="btn" id="s-find">Find my leagues</button></div>
      <label class="field" style="margin-top:14px"><span>League</span><select id="s-league"><option value="${esc(s.league_id)}">${s.league_id ? "Current league" : "First league found"}</option></select></label>
      <span id="s-findmsg" class="small muted"></span>
    </div>
    <div class="section"><h3>AI for Ask Huddle</h3><p class="sub">Pick what answers your questions. Every option gets the same league data and tools.</p>
      <div class="choices" id="s-choices">${AI_CHOICES.map(([k, t, d]) => `<button class="choice ${form.provider === k ? "on" : ""}" data-k="${k}" aria-pressed="${form.provider === k}"><b>${t}</b><span>${d}</span></button>`).join("")}</div>
      <div id="s-ai"></div>
    </div>
    <div class="section"><h3>Appearance</h3><p class="sub">Light or dark.</p>
      <div class="themes" id="s-themes">${THEMES.map(([k, label, c]) => `<button class="theme ${form.theme === k ? "on" : ""}" data-t="${k}" aria-pressed="${form.theme === k}">
        <span class="sw" aria-hidden="true">${c.map(x => `<i style="background:${x}"></i>`).join("")}</span><b>${label}</b></button>`).join("")}</div></div>
    <div class="section"><h3>Notifications</h3><p class="sub">When an answer lands while you're on another page, Huddle pops up a note and puts a count on the browser tab. Mac notifications also tell you when Huddle isn't the window you're looking at.</p>
      <div id="s-notify"></div></div>
    <div class="section"><h3>Advice style</h3><p class="sub">The AI follows these on every answer.</p>
      <textarea id="s-prefs" aria-label="Advice style">${esc(s.preferences)}</textarea></div>
    <details class="section"><summary style="cursor:pointer;font-weight:600">Advanced</summary>
      <div style="margin-top:14px">
      <label class="field"><span>Weight on Sleeper's projections: <b id="blendv">${Math.round(100 * s.projection_blend)}%</b></span><input type="range" id="s-blend" min="0" max="1" step="0.05" value="${s.projection_blend}">
        <em>The rest comes from Huddle's model of recent usage, matchup and Vegas totals.</em></label>
      <label class="field"><span>How much fantasy playoff weeks count</span><input type="number" id="s-po" min="1" max="3" step="0.05" value="${s.playoff_weight}" style="width:110px">
        <em>1.25 means a playoff week counts 25% more than a regular week in trade and waiver values.</em></label></div>
    </details>
    <div class="savebar"><button class="btn primary" id="s-save">Save</button><span id="s-msg" class="small"></span></div>
  </div>`;

  const aiBox = $("#s-ai");
  async function drawAI() {
    const p = form.provider;
    aiBox.innerHTML = `<div class="status"><i></i><div class="grow">Checking…</div></div>`;
    const st = await api(`/api/ai/status?provider=${p}`).catch(e => ({ ready: false, message: e.message }));
    const status = `<div class="status ${st.ready ? "ready" : ""}"><i></i><div class="grow">${esc(st.message || "")}</div><button class="btn ghost small" id="s-recheck">Check again</button></div>`;
    const steps = (st.steps || []).length ? `<ol class="steplist">${st.steps.map(x => `<li><div><div class="t">${esc(x.title)}</div><div class="n">${esc(x.note || "")}</div>
        ${x.command ? `<div class="cmd"><code>${esc(x.command)}</code><button data-copy="${esc(x.command)}">Copy</button></div>` : ""}</div></li>`).join("")}</ol>
        <p class="small muted" style="margin:0 0 14px">Then come back and select Check again.</p>` : "";
    let fields = "";
    if (p === "claude") {
      fields = `<label class="field"><span>Model</span><select id="s-cmodel" style="max-width:280px">${[["", "Your plan's default"], ["sonnet", "Sonnet (faster)"], ["opus", "Opus (deepest)"]].map(([v, l]) => `<option value="${v}" ${s.claude_sub_model === v ? "selected" : ""}>${l}</option>`).join("")}</select>
        <em>Runs through Claude Code on this Mac and counts toward your Claude plan's usage limits. No API key.</em></label>`;
    } else if (p === "chatgpt") {
      fields = `<label class="field"><span>Model</span><input type="text" id="s-gmodel" value="${esc(s.chatgpt_model)}" placeholder="Leave blank for your plan's default" style="max-width:360px">
        <em>Runs through Codex on this Mac and counts toward your ChatGPT plan's limits. No API key.</em></label>`;
    } else if (p === "ollama") {
      fields = `<label class="field"><span>Model</span><input type="text" id="s-omodel" value="${esc(st.model || s.ollama_model)}" list="omodels" style="max-width:280px"><datalist id="omodels">${(st.models || []).map(m => `<option value="${esc(m)}">`).join("")}</datalist>
        <em>Free and private, but noticeably weaker at this than Claude or ChatGPT. Needs 16 GB of memory for qwen3:8b.</em></label>`;
    } else {
      const v = form.vendor;
      const keySet = v === "anthropic" ? s.anthropic_key_set : s.openai_key_set;
      fields = `<div class="toolbar"><div class="seg" id="s-vendor"><button data-v="anthropic" class="${v === "anthropic" ? "on" : ""}">Anthropic (Claude)</button><button data-v="openai" class="${v === "openai" ? "on" : ""}">OpenAI (ChatGPT)</button></div></div>
        <label class="field"><span>${v === "anthropic" ? "Anthropic" : "OpenAI"} API key</span><input type="password" id="s-key" autocomplete="off" placeholder="${keySet ? "Saved. Paste a new key to replace it" : (v === "anthropic" ? "sk-ant-…" : "sk-…")}">
          <em>${v === "anthropic" ? "From console.anthropic.com" : "From platform.openai.com/api-keys"}. Billed per use, separately from any subscription.${keySet ? ' <a href="#" id="s-clearkey">Remove saved key</a>' : ""}</em></label>
        <label class="field"><span>Model</span><input type="text" id="s-amodel" value="${esc(v === "anthropic" ? s.claude_model : s.openai_model)}" list="amodels" style="max-width:280px"><datalist id="amodels"></datalist></label>`;
    }
    aiBox.innerHTML = status + steps + fields;
    $("#s-recheck").onclick = drawAI;
    const vend = $("#s-vendor");
    if (vend) vend.onclick = e => { if (e.target.dataset.v) { form.vendor = e.target.dataset.v; drawAI(); } };
    const ck = $("#s-clearkey");
    if (ck) ck.onclick = async e => {
      e.preventDefault();
      await api("/api/settings", { method: "POST", body: JSON.stringify(form.vendor === "anthropic" ? { clear_api_key: true } : { clear_openai_key: true }) });
      renderSettings();
    };
    if (p === "api" && form.vendor === "openai" && s.openai_key_set) {
      api("/api/openai/models").then(ms => { const dl = $("#amodels"); if (dl) dl.innerHTML = ms.map(m => `<option value="${esc(m)}">`).join(""); }).catch(() => {});
    }
  }
  drawAI();
  function drawNotify() {
    const box = $("#s-notify");
    if (!canNotify()) {
      box.innerHTML = `<div class="status"><i></i><div class="grow">This browser can't show Mac notifications for Huddle${window.isSecureContext ? "" : `. Open Huddle at http://localhost:${location.port || 8765} instead`}. You'll still get the pop-up and the tab count.</div></div>`;
      return;
    }
    const perm = Notification.permission;
    if (perm === "granted") {
      box.innerHTML = `<div class="status ready"><i></i><div class="grow">Mac notifications are on.</div><button class="btn ghost small" id="s-ntest">Send a test</button></div>
        <p class="small muted" style="margin:0">Nothing showed up? Allow notifications for your browser in System Settings, then Notifications.</p>`;
      $("#s-ntest").onclick = () => { try { new Notification("Huddle answered", { body: "This is how answers will show up when Huddle is in the background.", tag: "huddle-test" }); } catch (_) { /* unavailable */ } };
    } else if (perm === "denied") {
      box.innerHTML = `<div class="status"><i></i><div class="grow">Your browser is blocking notifications from Huddle. Allow them in the site settings (the icon to the left of the address), then reload.</div></div>`;
    } else {
      box.innerHTML = `<div class="status"><i></i><div class="grow">Mac notifications are off.</div><button class="btn primary small" id="s-non">Turn on</button></div>`;
      $("#s-non").onclick = async () => { try { await Notification.requestPermission(); } catch (_) { /* unavailable */ } drawNotify(); };
    }
  }
  drawNotify();
  $("#s-choices").onclick = e => {
    const b = e.target.closest(".choice"); if (!b) return;
    form.provider = b.dataset.k;
    document.querySelectorAll(".choice").forEach(c => { c.classList.toggle("on", c === b); c.setAttribute("aria-pressed", c === b); });
    drawAI();
  };
  $("#s-themes").onclick = e => {
    const b = e.target.closest(".theme"); if (!b) return;
    form.theme = b.dataset.t; applyTheme(form.theme);
    document.querySelectorAll(".theme").forEach(c => { c.classList.toggle("on", c === b); c.setAttribute("aria-pressed", c === b); });
    api("/api/settings", { method: "POST", body: JSON.stringify({ theme: form.theme }) }).catch(() => {});
  };
  $("#s-blend").oninput = e => { $("#blendv").textContent = Math.round(100 * e.target.value) + "%"; };
  $("#s-find").onclick = async () => {
    const u = $("#s-user").value.trim(); if (!u) return;
    $("#s-findmsg").textContent = "Looking…";
    try {
      const leagues = await api(`/api/leagues?username=${encodeURIComponent(u)}`);
      $("#s-league").innerHTML = leagues.map(l => `<option value="${esc(l.league_id)}" ${l.league_id === s.league_id ? "selected" : ""}>${esc(l.name)}${l.teams ? ` (${l.teams} teams)` : ""}</option>`).join("") || "<option value=''>No leagues this season</option>";
      $("#s-findmsg").textContent = `Found ${leagues.length} league${leagues.length === 1 ? "" : "s"}.`;
    } catch (e) { $("#s-findmsg").textContent = e.message; }
  };
  $("#s-save").onclick = async () => {
    const body = { sleeper_username: $("#s-user").value.trim(), league_id: $("#s-league").value, ai_provider: form.provider,
      preferences: $("#s-prefs").value, projection_blend: Number($("#s-blend").value), playoff_weight: Number($("#s-po").value), theme: form.theme };
    const val = id => { const el = $(id); return el ? el.value.trim() : undefined; };
    if (form.provider === "claude") body.claude_sub_model = val("#s-cmodel");
    if (form.provider === "chatgpt") body.chatgpt_model = val("#s-gmodel");
    if (form.provider === "ollama") body.ollama_model = val("#s-omodel");
    if (form.provider === "api") {
      body.api_vendor = form.vendor;
      if (form.vendor === "anthropic") { body.anthropic_api_key = val("#s-key"); body.claude_model = val("#s-amodel"); }
      else { body.openai_api_key = val("#s-key"); body.openai_model = val("#s-amodel"); }
    }
    $("#s-msg").textContent = "Saving…";
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
      if (form.provider !== chat.provider) resetChat(form.provider);
      chat.ai = null; chat.ideas = null;
      $("#s-msg").textContent = "Saved. Loading your league…";
      state.overview = null; state.matchup = null; chat.ideas = null;
      await loadOverview();
      location.hash = "#ask";
    } catch (e) { $("#s-msg").textContent = e.message; }
  };
}

// ------------------------------------------------------------------ player drawer
async function openPlayer(pid) {
  const drawer = $("#drawer"), body = $("#drawer-body");
  body.innerHTML = `<div class="loading"><div class="ball"></div><p>Researching…</p></div>`;
  drawer.classList.add("open"); drawer.setAttribute("aria-hidden", "false"); $("#scrim").hidden = false;
  try {
    const r = await api(`/api/player/${encodeURIComponent(pid)}`);
    const p = r.player, ng = r.next_game, pr = r.projection, us = r.usage, inj = r.injury;
    const u = (k, label, suf = "") => (us.season[k] != null && (us.season[k] || us.last3[k])) ? `<tr><td>${label}</td><td class="n">${fmt(us.last3[k])}${suf}</td><td class="n">${fmt(us.season[k])}${suf}</td></tr>` : "";
    body.innerHTML = `<div class="phead">${avatar({ name: p.name, pos: p.pos })}<div><h1>${esc(p.name)}</h1>
      <div class="small muted"><span class="pos ${esc(p.pos)}">${esc(p.pos)}</span> ${esc(p.team || "Free agent")}${p.age ? ", age " + p.age : ""}. ${p.fantasy_owner === "You" ? "On your team" : esc(p.fantasy_owner)}. Bye week ${p.bye_week || "–"}.</div></div></div>
      <div class="grid3" style="gap:10px">
        <div class="card stat"><div class="k">This week</div><div class="v">${fmt(pr.this_week)}</div></div>
        <div class="card stat"><div class="k">Per game, rest of season</div><div class="v">${fmt(pr.ros_ppg)}</div></div>
        <div class="card stat"><div class="k">Trade value</div><div class="v">${fmt(pr.trade_value, 0)}</div></div>
      </div>
      <p class="small muted">Sleeper projects ${fmt(pr.sleeper)}; Huddle's model says ${fmt(pr.huddle_model)}.${pr.note ? " Adjusted for: " + esc(pr.note) + "." : ""}</p>
      <h2>Health</h2>
      <p>${esc(inj.status)}${inj.body_part ? " (" + esc(inj.body_part) + ")" : ""}${inj.practice ? ". Practice: " + esc(inj.practice) : ""}${inj.official_report && inj.official_report.practice_status ? ". Week " + inj.official_report.week + " report: " + esc(inj.official_report.practice_status) : ""}.</p>
      ${inj.notes ? `<p class="small">${esc(inj.notes)}</p>` : ""}
      ${ng ? (ng.bye ? `<h2>Week ${ng.week}</h2><p>Bye week.</p>` : `<h2>Week ${ng.week}: ${ng.home_away === "away" ? "at" : "vs"} ${esc(ng.opponent)}</h2>
        <div class="kv">
          <div><span>Kickoff</span><br>${esc(ng.kickoff)}</div>
          <div><span>Venue</span><br>${esc(ng.stadium || "")}${ng.roof ? ", " + esc(ng.roof) : ""}</div>
          <div><span>Vegas total / spread</span><br>${fmt(ng.vegas.total)} / ${ng.vegas.spread === null ? "–" : signed(ng.vegas.spread)}</div>
          <div><span>Team implied points</span><br>${fmt(ng.vegas.implied_team_total)}</div>
          ${ng.defense_vs_position ? `<div><span>Defense vs ${esc(p.pos)}</span><br>${fmt(ng.defense_vs_position.allowed_ppg)} allowed/game (#${ng.defense_vs_position.rank_most_allowed}) ${pill(ng.defense_vs_position.label)}</div>` : ""}
          ${ng.weather ? `<div><span>Weather</span><br>${fmt(ng.weather.temp_f, 0)}°F, wind ${fmt(ng.weather.wind_mph, 0)} mph</div>` : ""}
        </div>`) : ""}
      ${p.pos !== "DEF" && p.pos !== "K" && us.season.snap_pct !== undefined ? `<h2>Usage</h2><div class="card"><table class="data"><thead><tr><th></th><th class="n">Last 3</th><th class="n">Season</th></tr></thead><tbody>
        ${u("snap_pct", "Snap share", "%")}${u("targets_pg", "Targets / game")}${u("target_share", "Target share", "%")}${u("carries_pg", "Carries / game")}${u("carry_share", "Carry share", "%")}${u("wopr", "WOPR")}</tbody></table></div>` : ""}
      <h2>Form</h2>
      <p>${fmt(r.season.ppg)} pts/game over ${r.season.games} games; ${fmt(r.form.last3_ppg)} over the last 3${r.form.trend ? " (" + esc(r.form.trend) + ")" : ""}. Home ${fmt(r.splits.home_ppg)}, away ${fmt(r.splits.away_ppg)}.</p>
      <div class="card"><table class="data"><thead><tr><th>Wk</th><th>Opp</th><th class="n">Pts</th><th>Line</th></tr></thead><tbody>
      ${r.form.last3_games.map(g => `<tr><td>${g.week}</td><td>${g.home_away === "away" ? "@" : "vs"} ${esc(g.opp)}</td><td class="n"><strong>${fmt(g.pts)}</strong></td><td class="small">${gameLine(p.pos, g)}</td></tr>`).join("")}</tbody></table></div>
      <h2>Next four weeks</h2>
      <div class="card"><table class="data"><tbody>${r.upcoming.map(g => `<tr><td>Wk ${g.week}</td><td>${esc(g.opp)}</td><td>${pill(g.matchup)}</td><td class="n">${g.proj !== undefined ? fmt(g.proj) : ""}</td></tr>`).join("")}</tbody></table></div>`;
  } catch (e) { body.innerHTML = `<div class="error">${esc(e.message)}</div>`; }
}
function gameLine(pos, g) {
  if (pos === "DEF") return `${g.sacks} sacks, ${g.ints} INT, ${g.pts_allowed} pts allowed`;
  if (pos === "K") return `${g.fgm}/${g.fga} FG, ${g.xpm} XP`;
  const parts = [];
  if (pos === "QB") parts.push(`${g.pass_yd} pass yds, ${g.pass_td} TD, ${g.int} INT`);
  if (g.carries) parts.push(`${g.carries} car, ${g.rush_yd} yds`);
  if (g.targets) parts.push(`${g.rec}/${g.targets} rec, ${g.rec_yd} yds`);
  if (g.td && pos !== "QB") parts.push(`${g.td} TD`);
  if (g.snap_pct != null) parts.push(`${g.snap_pct}% snaps`);
  return esc(parts.join("; "));
}
function closeDrawer() { $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); $("#scrim").hidden = true; }
$("#drawer-close").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if ($("#drawer").classList.contains("open")) closeDrawer(); else closeDock();
});

route();
