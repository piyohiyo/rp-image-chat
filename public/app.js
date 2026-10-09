const $ = (id) => document.getElementById(id);
const log = $("chatLog");
let sid = localStorage.getItem("sid") || "";
let state = null;
let lastRev = -1;
let sessions = [];
let rpMode = localStorage.getItem("rpMode") === "1";
let currentPromptMsg = null;
let formDirty = false;
let editTarget = null;
let ragCollections = [];
let ragSelected = null;
let ragEditingEntry = null;
const seen = JSON.parse(localStorage.getItem("seenRev") || "{}");

async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  return r.json();
}
const post = (path, body = {}) => api(path, { sid, ...body });
async function checked(path, body) {
  const result = await api(path, body);
  if (result.error) throw new Error(result.error);
  return result;
}
const imgUrl = (f) => "/img?f=" + encodeURIComponent(f);

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function fmt(text) {
  return esc(text)
    .replace(/「[^」]*」/g, (m) => `<span class="speech">${m}</span>`)
    .replace(/（[^）]*）/g, (m) => `<span class="thought">${m}</span>`);
}
function fmtAltered(original, modified) {
  const a = Array.from(original || "");
  const b = Array.from(modified || "");
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let end = 0;
  while (end < a.length - start && end < b.length - start && a[a.length - 1 - end] === b[b.length - 1 - end]) end++;
  if (start === b.length && end === 0) return fmt(modified);
  const prefix = b.slice(0, start).join("");
  const changed = b.slice(start, b.length - end).join("");
  const suffix = b.slice(b.length - end).join("");
  return fmt(prefix) + (changed ? `<span class="edited-text">${fmt(changed)}</span>` : "") + fmt(suffix);
}
function markSeen() {
  if (state) { seen[sid] = state.rev; localStorage.setItem("seenRev", JSON.stringify(seen)); }
}

// ---------------------------------------------------------------- sidebar
function renderSessions() {
  const el = $("sessionList");
  el.innerHTML = "";
  for (const s of sessions) {
    const d = document.createElement("div");
    d.className = "sess" + (s.id === sid ? " active" : "");
    const unread = s.id !== sid && seen[s.id] !== undefined && s.rev > seen[s.id];
    const badges = (s.waiting ? "⏳" : "") + (s.generating ? "🎨" : "") + (unread ? '<span class="dot"></span>' : "");
    d.innerHTML = `<div class="sess-title">${esc(s.title || "(無題)")}<span class="badges">${badges}</span></div>
      <div class="sess-sub">${esc(s.summary || s.last || "")}</div>`;
    d.onclick = () => switchTo(s.id);
    el.appendChild(d);
  }
}

async function refreshSessions() {
  const r = await api("/api/sessions");
  sessions = r.sessions || [];
  if (!sessions.find((s) => s.id === sid) && sessions.length) {
    await switchTo(sessions[0].id);
    return;
  }
  for (const s of sessions) if (seen[s.id] === undefined) seen[s.id] = s.rev;
  renderSessions();
}

async function switchTo(id) {
  sid = id;
  localStorage.setItem("sid", sid);
  lastRev = -1;
  state = null;
  formDirty = false;
  log.innerHTML = "";
  await poll();
  renderSessions();
  document.body.classList.remove("side-open");
}

// ---------------------------------------------------------------- settings form
const FIELDS = ["title", "summary", "instructions", "memo", "user_name", "scenario", "first_message"];
const CHAR = { char_name: "name", char_age: "age", char_speech: "speech", char_profile: "profile" };

function fillForm(st) {
  for (const k of FIELDS) $("f_" + k).value = st[k] || "";
  for (const [id, k] of Object.entries(CHAR)) $("f_" + id).value = (st.character || {})[k] || "";
  $("f_image").value = JSON.stringify(st.image || {}, null, 2);
}
function readForm() {
  const st = JSON.parse(JSON.stringify(state.setting));
  for (const k of FIELDS) st[k] = $("f_" + k).value;
  st.character = st.character || {};
  for (const [id, k] of Object.entries(CHAR)) st.character[k] = $("f_" + id).value;
  st.image = JSON.parse($("f_image").value);
  return st;
}
document.querySelectorAll("#settingsPanel input, #settingsPanel textarea").forEach((el) =>
  el.addEventListener("input", () => (formDirty = true)));

// ---------------------------------------------------------------- chat
function render() {
  const s = state;
  $("title").textContent = s.setting?.title || "RP Image Chat";
  $("ragCount").textContent = (s.setting?.rag_collections || []).length;
  document.title = (s.setting?.title || "") + " - RP Image Chat";
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 120;
  log.innerHTML = "";
  let latestImg = null;

  if (s.setting?.summary) {
    const sm = document.createElement("div");
    sm.className = "summary-card";
    sm.textContent = s.setting.summary;
    log.appendChild(sm);
  }

  for (const m of s.messages) {
    const wrap = document.createElement("div");
    wrap.className = "msg-wrap " + m.role;
    const bubble = document.createElement("div");
    bubble.className = "msg " + m.role;
    bubble.innerHTML = m.edited ? fmtAltered(m.original_text, m.text || "") : fmt(m.text || "");
    if (m.edited) {
      bubble.classList.add("edited");
      bubble.title = "改変前: " + (m.original_text || "");
    }
    wrap.appendChild(bubble);
    if (m.edited) {
      const tag = document.createElement("span");
      tag.className = "edited-label";
      tag.textContent = "改変済";
      wrap.appendChild(tag);
    }

    if (m.image) {
      const box = document.createElement("div");
      box.className = "gen-image";
      const im = m.image;
      if (im.status === "done" && im.file) {
        const img = document.createElement("img");
        img.src = imgUrl(im.file);
        img.onclick = () => { $("lightboxImg").src = img.src; $("lightbox").classList.remove("hidden"); };
        box.appendChild(img);
        latestImg = im.file;
      } else {
        const ph = document.createElement("div");
        ph.className = "placeholder" + (im.status === "error" ? " err" : "");
        ph.textContent = im.status === "error" ? "生成エラー: " + (im.error || "") : im.status === "generating" ? "🎨 生成中…" : "⏳ 生成待ち…";
        box.appendChild(ph);
      }
      const tools = document.createElement("div");
      tools.className = "img-tools";
      const rg = document.createElement("button");
      rg.textContent = "🎲 別シード";
      rg.onclick = () => post("/api/regen", { id: m.id }).then(poll);
      const pe = document.createElement("button");
      pe.textContent = "✏ プロンプト";
      pe.onclick = () => openPrompt(m);
      tools.append(rg, pe);
      if (im.history && im.history.length > 1) {
        const th = document.createElement("div");
        th.className = "thumbs";
        for (const h of im.history.slice(-8)) {
          const t = document.createElement("img");
          t.src = imgUrl(h.file);
          t.title = "seed " + h.seed;
          if (h.file === im.file) t.className = "active";
          t.onclick = () => post("/api/pick", { id: m.id, file: h.file }).then(poll);
          th.appendChild(t);
        }
        tools.appendChild(th);
      }
      box.appendChild(tools);
      wrap.appendChild(box);
    }

    const meta = document.createElement("div");
    meta.className = "meta";
    const st = m.role === "user" ? (m.status === "pending" ? " · 送信済" : m.status === "processing" ? " · 読込済" : "") : "";
    meta.textContent = (m.ts || "").replace("T", " ").slice(5, 16) + st;
    wrap.appendChild(meta);

    const acts = document.createElement("div");
    acts.className = "msg-actions";
    const del = document.createElement("button");
    del.textContent = "削除";
    del.onclick = () => { if (confirm("このメッセージを削除？")) post("/api/delete", { id: m.id }).then(poll); };
    const cp = document.createElement("button");
    cp.textContent = "コピー";
    cp.onclick = () => navigator.clipboard.writeText(m.text || "");
    if (m.role === "user") {
      const edit = document.createElement("button");
      edit.textContent = "編集";
      edit.onclick = () => openEdit(m);
      acts.appendChild(edit);
    } else {
      const alter = document.createElement("button");
      alter.textContent = "改変";
      alter.onclick = () => openEdit(m);
      acts.appendChild(alter);
      if (s.messages.slice(0, s.messages.indexOf(m)).some((x) => x.role === "user")) {
        const regen = document.createElement("button");
        regen.textContent = "再生成";
        regen.onclick = async () => {
          if (!confirm("この応答以降の会話を削除して、AI応答を作り直しますか？")) return;
          try { await checked("/api/message/regenerate", { sid, id: m.id }); await poll(); }
          catch (e) { alert(e.message); }
        };
        acts.appendChild(regen);
      }
    }
    acts.append(cp, del);
    wrap.appendChild(acts);

    log.appendChild(wrap);
  }

  const waiting = s.messages.some((m) => m.role === "user" && (m.status === "pending" || m.status === "processing"));
  if (waiting) {
    const t = document.createElement("div");
    t.className = "typing";
    t.innerHTML = "応答を書いています<span>.</span><span>.</span><span>.</span>";
    log.appendChild(t);
  }
  const pill = $("statusPill");
  pill.className = "pill " + (waiting ? "wait" : "ok");
  pill.textContent = waiting ? "AI応答待ち" : "入力どうぞ";

  log.classList.toggle("rp", rpMode);
  $("rpToggle").classList.toggle("on", rpMode);
  log.style.setProperty("--rp-bg", latestImg ? `url("${imgUrl(latestImg)}")` : "none");

  if (nearBottom) log.scrollTop = log.scrollHeight;
}

async function poll() {
  if (!sid) return refreshSessions();
  try {
    const s = await api("/api/session?sid=" + encodeURIComponent(sid));
    if (s.error) { sid = ""; return refreshSessions(); }
    if (s.rev !== lastRev) {
      const first = lastRev === -1;
      lastRev = s.rev;
      state = s;
      render();
      if (first) log.scrollTop = log.scrollHeight;
      if (first || !formDirty) fillForm(s.setting);
    }
    markSeen();
  } catch (e) {
    $("statusPill").className = "pill err";
    $("statusPill").textContent = "サーバー未接続";
  }
}

async function send() {
  const ta = $("messageInput");
  const text = ta.value.trim();
  if (!text || !sid) return;
  $("sendBtn").disabled = true;
  await post("/api/send", { text });
  ta.value = "";
  $("sendBtn").disabled = false;
  await poll();
  log.scrollTop = log.scrollHeight;
}

function openPrompt(m) {
  currentPromptMsg = m;
  $("promptEdit").value = m.image.prompt || "";
  $("promptFinal").textContent = m.image.final_positive
    ? `最終 positive:\n${m.image.final_positive}\n\nnegative:\n${m.image.final_negative}\n\nseed: ${m.image.seed}`
    : "";
  $("promptModal").classList.remove("hidden");
}
function flash(msg) {
  $("settingMsg").textContent = msg;
  setTimeout(() => ($("settingMsg").textContent = ""), 3000);
}

function openEdit(m) {
  editTarget = m;
  $("editTitle").textContent = m.role === "user" ? "過去の入力を編集" : "AI応答を改変";
  $("editHint").textContent = m.role === "user"
    ? "保存すると、この入力以降の会話を削除し、編集後の入力をAIへ送ります。"
    : "保存すると、この応答がAIに渡す過去の文脈でも書き換わります。後続の会話は残ります。";
  $("editText").value = m.text || "";
  $("editSave").textContent = m.role === "user" ? "編集して再送" : "改変を適用";
  $("editModal").classList.remove("hidden");
  $("editText").focus();
}

async function saveEdit() {
  const m = editTarget;
  const text = $("editText").value.trim();
  if (!m || !text) return;
  if (m.role === "user" && !confirm("この入力以降の会話を削除して再応答を待ちますか？")) return;
  const button = $("editSave");
  button.disabled = true;
  try {
    await checked(m.role === "user" ? "/api/message/edit" : "/api/message/alter", { sid, id: m.id, text });
    $("editModal").classList.add("hidden");
    editTarget = null;
    await poll();
  } catch (e) { alert(e.message); }
  finally { button.disabled = false; }
}

async function openRag() {
  if (!sid) return;
  $("ragModal").classList.remove("hidden");
  ragEditingEntry = null;
  $("ragEditor").classList.add("hidden");
  await loadRagCollections();
}

async function loadRagCollections() {
  try {
    ragCollections = (await checked("/api/rag")).collections || [];
    if (ragSelected && !ragCollections.some((c) => c.id === ragSelected)) ragSelected = null;
    renderRagCollections();
    await renderRagEntries();
  } catch (e) { alert(e.message); }
}

function renderRagCollections() {
  const list = $("ragCollections");
  list.innerHTML = "";
  for (const col of ragCollections) {
    const row = document.createElement("div");
    row.className = "rag-collection" + (col.id === ragSelected ? " selected" : "");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = (state?.setting?.rag_collections || []).includes(col.id);
    check.title = "このチャンネルで参照";
    check.onchange = async () => {
      const ids = new Set(state?.setting?.rag_collections || []);
      if (check.checked) ids.add(col.id); else ids.delete(col.id);
      try { await checked("/api/rag/select", { sid, ids: [...ids] }); await poll(); }
      catch (e) { check.checked = !check.checked; alert(e.message); }
    };
    const label = document.createElement("span");
    label.textContent = `${col.name} (${col.entryCount})`;
    label.onclick = () => { ragSelected = col.id; ragEditingEntry = null; $("ragEditor").classList.add("hidden"); renderRagCollections(); renderRagEntries(); };
    const del = document.createElement("button");
    del.textContent = "×";
    del.title = "コレクションを削除";
    del.onclick = async () => {
      if (!confirm(`「${col.name}」を削除しますか？`)) return;
      try { await checked("/api/rag/delete", { id: col.id }); await poll(); await loadRagCollections(); }
      catch (e) { alert(e.message); }
    };
    row.append(check, label, del);
    list.appendChild(row);
  }
}

function openRagEditor(entry = null) {
  ragEditingEntry = entry?.id || null;
  $("ragEntryTitle").value = entry?.title || "";
  $("ragEntryContent").value = entry?.content || "";
  $("ragEditor").classList.remove("hidden");
  $("ragEntryTitle").focus();
}

async function renderRagEntries() {
  const list = $("ragEntries");
  list.innerHTML = "";
  if (!ragSelected) { list.textContent = "左のコレクションを選択してください。"; return; }
  try {
    const col = (await checked("/api/rag/collection?id=" + encodeURIComponent(ragSelected))).collection;
    const add = document.createElement("button");
    add.textContent = "＋ エントリ追加";
    add.onclick = () => openRagEditor();
    list.appendChild(add);
    for (const entry of col.entries || []) {
      const card = document.createElement("div");
      card.className = "rag-entry";
      const title = document.createElement("strong");
      title.textContent = entry.title;
      const body = document.createElement("div");
      body.textContent = entry.content;
      const edit = document.createElement("button");
      edit.textContent = "編集";
      edit.onclick = () => openRagEditor(entry);
      const del = document.createElement("button");
      del.textContent = "削除";
      del.onclick = async () => {
        if (!confirm(`「${entry.title}」を削除しますか？`)) return;
        try { await checked("/api/rag/entry/delete", { id: ragSelected, entry_id: entry.id }); await loadRagCollections(); }
        catch (e) { alert(e.message); }
      };
      const actions = document.createElement("div");
      actions.className = "rag-entry-actions";
      actions.append(edit, del);
      card.append(title, actions, body);
      list.appendChild(card);
    }
  } catch (e) { list.textContent = e.message; }
}

async function saveRagEntry() {
  const title = $("ragEntryTitle").value.trim();
  const content = $("ragEntryContent").value.trim();
  if (!title || !content || !ragSelected) return;
  try {
    await checked("/api/rag/entry/save", { id: ragSelected, entry_id: ragEditingEntry, title, content });
    ragEditingEntry = null;
    $("ragEditor").classList.add("hidden");
    await loadRagCollections();
  } catch (e) { alert(e.message); }
}

$("sendBtn").onclick = send;
$("messageInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
});
$("settingsToggle").onclick = () => $("settingsPanel").classList.toggle("hidden");
$("ragToggle").onclick = openRag;
$("ragClose").onclick = () => $("ragModal").classList.add("hidden");
$("ragCreate").onclick = async () => {
  const name = $("ragName").value.trim();
  if (!name) return;
  try {
    const r = await checked("/api/rag/create", { name });
    $("ragName").value = "";
    ragSelected = r.collection.id;
    await loadRagCollections();
  } catch (e) { alert(e.message); }
};
$("ragName").onkeydown = (e) => { if (e.key === "Enter") $("ragCreate").click(); };
$("ragEntrySave").onclick = saveRagEntry;
$("ragEditCancel").onclick = () => $("ragEditor").classList.add("hidden");
$("editSave").onclick = saveEdit;
$("editCancel").onclick = () => { editTarget = null; $("editModal").classList.add("hidden"); };
$("sideToggle").onclick = () => document.body.classList.toggle("side-open");
$("rpToggle").onclick = () => { rpMode = !rpMode; localStorage.setItem("rpMode", rpMode ? "1" : "0"); render(); };
$("settingSave").onclick = async () => {
  try {
    await post("/api/setting", { setting: readForm() });
    formDirty = false;
    flash("保存しました");
    await poll();
    refreshSessions();
  } catch (e) { flash("画像設定のJSONエラー: " + e.message); }
};
$("newSession").onclick = async () => {
  const title = prompt("チャンネル名", "新しいチャンネル");
  if (title === null) return;
  const r = await api("/api/session/new", { title });
  await refreshSessions();
  await switchTo(r.sid);
  $("settingsPanel").classList.remove("hidden");
};
$("dupBtn").onclick = async () => {
  const r = await api("/api/session/new", { copy_from: sid });
  await refreshSessions();
  await switchTo(r.sid);
};
$("resetBtn").onclick = () => {
  if (confirm("このチャンネルの会話とAIメモを消去して、最初のメッセージから始めます。よろしい？")) post("/api/reset").then(poll);
};
$("deleteSession").onclick = async () => {
  if (!confirm(`チャンネル「${state?.setting?.title}」を削除？（data/trash に移動）`)) return;
  await post("/api/session/delete");
  sid = "";
  await refreshSessions();
};
$("lightbox").onclick = () => $("lightbox").classList.add("hidden");
$("promptCancel").onclick = () => $("promptModal").classList.add("hidden");
$("promptRegen").onclick = async () => {
  await post("/api/regen", { id: currentPromptMsg.id, prompt: $("promptEdit").value.trim() });
  $("promptModal").classList.add("hidden");
  poll();
};

(async () => {
  await refreshSessions();
  await poll();
  setInterval(poll, 1500);
  setInterval(refreshSessions, 3000);
})();
