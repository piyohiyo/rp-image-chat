const $ = (id) => document.getElementById(id);
const log = $("chatLog");
let sid = localStorage.getItem("sid") || "";
let state = null;
let lastRev = -1;
let formSid = "";  // 設定フォームに今入っているのはどのチャンネルの設定か
let sessions = [];
let rpMode = localStorage.getItem("rpMode") === "1";
let currentPromptMsg = null;
let formDirty = false;
let editTarget = null;
let ragCollections = [];
let ragSelected = null;
let ragEditingEntry = null;
let meta = { app: {} };
let ideSessions = [];
let ideNewDir = "";  // 「新規会話」で作るフォルダ
let sysFiles = [];
let folderOpts = [];  // 登録フォルダごとの {folder, name, ok, files: [{label, path}], more}
let presets = [];
let textSaveHandler = null;
const collapsed = JSON.parse(localStorage.getItem("collapsedProjects") || "{}");
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
// 発言の表示。Markdown（見出し・太字・斜体・取り消し線・コード・引用・リスト・表・区切り線・リンク）を HTML にする。
// 「」と（）の色分けはそのまま。〜 は fmtAltered が付ける「書き換えた所」の印
const ED = /[]/g;
const fmt = (text) => fmtMarked(String(text).replace(ED, ""));
function fmtMarked(text) {
  return md(text).replace(//g, '<span class="edited-text">').replace(//g, "</span>");
}
function mdInline(s) {
  const stash = [];
  const put = (h) => `\u0000${stash.push(h) - 1}\u0000`;
  s = s.replace(/`([^`\n]+)`/g, (_, c) => put(`<code>${esc(c)}</code>`))
    .replace(/\\([\\`*_{}\[\]()#+\-.!|~>])/g, (_, c) => put(esc(c)));
  const h = esc(s)
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => put(`<a href="${u}" target="_blank" rel="noopener">${t}</a>`))
    .replace(/\*\*(?!\s)([^*\n]+?)\*\*/g, "<strong>$1</strong>")
    .replace(/__(?!\s)([^_\n]+?)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*(?![\s*])([^*\n]+?)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/~~(?!\s)([^~\n]+?)~~/g, "<del>$1</del>")
    .replace(/「[^」]*」/g, (m) => `<span class="speech">${m}</span>`)
    .replace(/（[^）]*）/g, (m) => `<span class="thought">${m}</span>`);
  const back = (x) => x.replace(/\u0000(\d+)\u0000/g, (_, n) => back(stash[n]));  // リンクの文字の中のコードも戻す
  return back(h);
}
const mdBr = (s) => mdInline(s).replace(/\n/g, "<br>");
function md(text) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [];
  const flush = () => { if (para.length) { out.push(`<p>${mdBr(para.join("\n"))}</p>`); para = []; } };
  const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  // 書き換えた所の印が行頭にあっても、見出しやリストの記号として読めるように
  const split = (l) => (l.startsWith("") ? ["", l.slice(1)] : ["", l]);
  for (let i = 0; i < lines.length; i++) {
    const [lead, l] = split(lines[i]);
    const bare = l.replace(ED, "");
    let m;
    if ((m = bare.match(/^\s*(```|~~~)/))) {
      flush();
      const code = [];
      while (++i < lines.length && !lines[i].replace(ED, "").trim().startsWith(m[1])) code.push(lines[i].replace(ED, ""));
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }
    if (!bare.trim()) { flush(); continue; }
    if ((m = l.match(/^(#{1,6})\s+(.*?)(\s+#+)?\s*$/))) {
      flush();
      out.push(`<h${m[1].length}>${mdInline(lead + m[2])}</h${m[1].length}>`);
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(bare)) { flush(); out.push("<hr>"); continue; }
    if (/^\s*>/.test(bare)) {
      flush();
      const q = [];
      for (; i < lines.length && /^\s*>/.test(lines[i].replace(ED, "")); i++) {
        const [ld, x] = split(lines[i]);
        q.push(ld + x.replace(/^\s*> ?/, ""));
      }
      i--;
      out.push(`<blockquote>${md(q.join("\n"))}</blockquote>`);
      continue;
    }
    if (LIST.test(l)) {
      flush();
      const items = [];
      for (; i < lines.length; i++) {
        const [ld, x] = split(lines[i]);
        const lm = x.match(LIST);
        if (lm) items.push({ ind: lm[1].replace(/\t/g, "    ").length, tag: /\d/.test(lm[2]) ? "ol" : "ul", text: ld + lm[3] });
        else if (x.replace(ED, "").trim() && /^\s+/.test(x) && items.length) items[items.length - 1].text += "\n" + ld + x.trim();
        else break;
      }
      i--;
      let h = "";
      const stack = [];
      for (const it of items) {
        while (stack.length > 1 && it.ind < stack[stack.length - 1].ind) h += `</li></${stack.pop().tag}>`;
        const top = stack[stack.length - 1];
        if (!top || it.ind > top.ind + 1) { stack.push({ ind: it.ind, tag: it.tag }); h += `<${it.tag}><li>`; }
        else h += "</li><li>";
        h += mdBr(it.text.replace(/^(?)\[([ xX])\]\s+/, (_, e, c) => e + (c === " " ? "☐ " : "☑ ")));
      }
      while (stack.length) h += `</li></${stack.pop().tag}>`;
      out.push(h);
      continue;
    }
    if (bare.includes("|") && i + 1 < lines.length && SEP.test(lines[i + 1].replace(ED, "")) && lines[i + 1].includes("-")) {
      flush();
      const align = cells(lines[i + 1].replace(ED, "")).map((c) => (c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : ""));
      const row = (l2, tag) => "<tr>" + cells(l2.replace(ED, "")).map((c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ""}>${mdInline(c)}</${tag}>`).join("") + "</tr>";
      let h = "<table><thead>" + row(lines[i], "th") + "</thead><tbody>";
      for (i += 2; i < lines.length && lines[i].replace(ED, "").includes("|") && lines[i].replace(ED, "").trim(); i++) h += row(lines[i], "td");
      i--;
      out.push(h + "</tbody></table>");
      continue;
    }
    para.push(lead + l);
  }
  flush();
  return out.join("");
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
  // 書き換えた所に行ごとの印を付けて、まとめて Markdown にする（行をまたぐ span で HTML が崩れないように）
  const mark = changed.replace(ED, "").split("\n").map((x) => (x.trim() ? "" + x + "" : x)).join("\n");
  return fmtMarked(prefix.replace(ED, "") + mark + suffix.replace(ED, ""));
}
function markSeen() {
  if (state) { seen[sid] = state.rev; localStorage.setItem("seenRev", JSON.stringify(seen)); }
}

// ---------------------------------------------------------------- sidebar
function renderSessions() {
  const el = $("sessionList");
  el.innerHTML = "";
  const q = $("sessionFilter").value.trim().toLowerCase();
  const shown = sessions.filter((s) => !q || [s.title, s.summary, s.project, s.last].some((x) => (x || "").toLowerCase().includes(q)));
  // プロジェクトごとにまとめる（プロジェクトなしは最後）
  const groups = new Map();
  for (const s of shown) {
    const p = s.project || "";
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(s);
  }
  const names = [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b, "ja"));
  $("projectList").innerHTML = names.filter(Boolean).map((n) => `<option value="${esc(n)}">`).join("");
  for (const name of names) {
    if (names.length > 1 || name) {
      const h = document.createElement("div");
      h.className = "proj-head";
      const isCol = collapsed[name] && !q;
      h.textContent = (isCol ? "▸ " : "▾ ") + (name || "（プロジェクトなし）") + ` (${groups.get(name).length})`;
      h.onclick = () => { collapsed[name] = !collapsed[name]; localStorage.setItem("collapsedProjects", JSON.stringify(collapsed)); renderSessions(); };
      el.appendChild(h);
      if (isCol && !groups.get(name).some((s) => s.id === sid)) continue;
    }
    for (const s of groups.get(name)) {
      const d = document.createElement("div");
      d.className = "sess" + (s.id === sid ? " active" : "");
      const unread = s.id !== sid && seen[s.id] !== undefined && s.rev > seen[s.id];
      const badges = (s.ide_title ? `<span title="担当IDE: ${esc(s.ide_title)}">🖥</span>` : "") + (s.waiting ? "⏳" : "") + (s.generating ? "🎨" : "") + (unread ? '<span class="dot"></span>' : "");
      d.innerHTML = `<div class="sess-title">${esc(s.title || "(無題)")}<span class="badges">${badges}<button type="button" class="sess-rename" title="チャンネルの名前を変える">✎</button></span></div>
        <div class="sess-sub">${esc(s.summary || s.last || "")}</div>`;
      d.onclick = () => switchTo(s.id);
      d.querySelector(".sess-rename").onclick = (e) => { e.stopPropagation(); renameChannel(s.id, s.title); };
      d.oncontextmenu = (e) => {
        e.preventDefault();
        if (confirm(`「${s.title || "(無題)"}」を削除？（data/trash に移動）`)) {
          api("/api/session/delete", { sid: s.id }).then(refreshSessions);
        }
      };
      el.appendChild(d);
    }
  }
}

// チャンネルの名前を変える（担当の会話があるなら作業フォルダはいまのまま）
async function renameChannel(id, cur) {
  const title = prompt("チャンネルの新しい名前", cur || "");
  if (title === null || !title.trim() || title.trim() === cur) return;
  try {
    const r = await checked("/api/session/rename", { sid: id, title: title.trim() });
    if (id === sid) {
      $("f_title").value = r.title;  // 設定欄を開いていても、ほかの書きかけはそのまま
      await poll();
    }
    refreshSessions();
    flash("チャンネルの名前を変えました: " + r.title);
  } catch (e) { alert(e.message); }
}
$("title").onclick = () => { if (state) renameChannel(sid, state.setting?.title || ""); };

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
  formSid = "";
  newCharRow = false;
  $("charList").innerHTML = "";  // 前のチャンネルで編集中だった行は捨てる
  attaching = [];  // 添付は上げたチャンネルの発言にしか付けられない
  renderAttach();
  log.innerHTML = "";
  await poll();
  renderSessions();
  document.body.classList.remove("side-open");
}

// ---------------------------------------------------------------- settings form
const FIELDS = ["title", "project", "summary", "instructions", "memo", "user_name", "scenario", "first_message", "image_mode", "input_mode", "system_file",
  "ide_folder", "agents_extra"];
const CHAR = { char_name: "name", char_age: "age", char_speech: "speech", char_profile: "profile" };

// 選択肢にない値（ファイルが消えた・古いパス）も消さずに残して見せる
function fillSelect(el, items, value, empty = "（なし）") {
  el.innerHTML = "";
  const add = (v, label) => { const o = document.createElement("option"); o.value = v; o.textContent = label; el.appendChild(o); };
  if (empty !== null) add("", empty);
  const groups = {};
  for (const it of items) {
    if (!it.group) { add(it.value, it.label); continue; }
    if (!groups[it.group]) { groups[it.group] = document.createElement("optgroup"); groups[it.group].label = it.group; el.appendChild(groups[it.group]); }
    const o = document.createElement("option");
    o.value = it.value;
    o.textContent = it.label;
    groups[it.group].appendChild(o);
  }
  if (value && !items.some((it) => it.value === value)) add(value, "⚠ " + value.split(/[\\/]/).pop() + "（一覧にない）");
  el.value = value || "";
}

function fillFileOptions(st) {
  fillSelect($("f_system_file"), sysFiles.map((f) => ({ value: f, label: f })), st.system_file);
  const refs = st.ref_files || [];
  const list = $("refList");
  list.innerHTML = "";
  const all = [...sysFiles, ...refs.filter((r) => !sysFiles.includes(r))];
  if (!all.length) list.innerHTML = '<span class="muted">ファイルがありません（上の「変更…」でフォルダを確認）</span>';
  for (const f of all) {
    const l = document.createElement("label");
    l.className = "check";
    l.innerHTML = `<input type="checkbox" value="${esc(f)}"${refs.includes(f) ? " checked" : ""}> ${esc(f)}${sysFiles.includes(f) ? "" : " ⚠一覧にない"}`;
    l.querySelector("input").onchange = () => (formDirty = true);
    list.appendChild(l);
  }
  fillFolderFiles(strList(st.folder_files));
  $("f_files_extra").value = st.files_extra || "";
}

// 「読み込むファイル」は登録フォルダの中から複数選べる。選んだものを先頭に、選んだ順で並べる
const strList = (v) => (Array.isArray(v) ? v : []).filter((x) => typeof x === "string" && x);
function fillFolderFiles(selected) {
  const list = $("folderFileList");
  const opts = folderOpts.flatMap((f) => f.files.map((x) => ({ path: x.path, label: `${f.name}/${x.label}` })));
  const items = [...selected.map((p) => opts.find((o) => o.path === p) || { path: p, label: p.split(/[\\/]/).pop() + " ⚠一覧にない", gone: true }),
                 ...opts.filter((o) => !selected.includes(o.path))];
  list.innerHTML = items.length ? "" : `<span class="muted">${folderOpts.length
    ? "ファイルがありません（登録したフォルダに .md .txt がない）"
    : "フォルダがまだありません（上の「＋ フォルダを追加」で選ぶ）"}</span>`;
  for (const o of items) {
    const l = document.createElement("label");
    l.className = "check";
    l.title = o.path;
    l.innerHTML = `<input type="checkbox" value="${esc(o.path)}"${selected.includes(o.path) ? " checked" : ""}> ${esc(o.label)}`;
    l.querySelector("input").onchange = () => (formDirty = true);
    list.appendChild(l);
  }
  filterFolderFiles();
}
function readFolderFiles() {
  return [...$("folderFileList").querySelectorAll("input:checked")].map((x) => x.value);
}
function filterFolderFiles() {
  const q = $("folderFileFilter").value.trim().toLowerCase();
  for (const l of $("folderFileList").querySelectorAll("label.check")) {
    l.hidden = !!q && !l.querySelector("input").checked && !l.textContent.toLowerCase().includes(q);
  }
}
$("folderFileFilter").addEventListener("input", filterFolderFiles);

// 読み込むファイルのフォルダ（全チャンネル共通。＋でいくつでも。足す・外すとすぐ保存）
function fillFolderRows(list) {
  $("folderRows").innerHTML = "";
  for (const path of list) {
    const info = folderOpts.find((f) => f.folder.toLowerCase() === path.replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase());
    const row = document.createElement("div");
    row.className = "folder-row";
    const st = !info ? "" : info.ok ? `${info.files.length}${info.more ? "件以上" : "件"}` : "見つからない";
    row.innerHTML = `<code class="folder-path" title="${esc(path)}">${esc(path)}</code>
      <span class="st${info && !info.ok ? " ng" : ""}">${esc(st)}</span><button type="button" class="sub">外す</button>`;
    row.querySelector("button").onclick = () => saveFolders(list.filter((x) => x !== path));
    $("folderRows").appendChild(row);
  }
}
$("folderAdd").onclick = async () => {
  const path = await pickFolder();
  if (!path) return;
  const list = strList(meta.app?.file_folders);
  if (list.some((x) => x.toLowerCase() === path.toLowerCase())) return flash("そのフォルダはもう入っています");
  await saveFolders([...list, path]);
};
async function saveFolders(list) {
  try { await checked("/api/app-settings", { file_folders: list }); await reloadFileLists(); }
  catch (e) { alert(e.message); }
}
$("sysRootPick").onclick = async () => {
  const path = await pickFolder(meta.app?.system_root || "");
  if (!path) return;
  try { await checked("/api/app-settings", { system_root: path }); await reloadFileLists(); }
  catch (e) { alert(e.message); }
};

// PC の画面に「フォルダーの選択」ウィンドウを出す（やめたら ""）。出せないとき（スマホから・tkinter がない）は場所を打ってもらう
async function pickFolder(initial = "") {
  flash("PC の画面にフォルダを選ぶウィンドウが出ます");
  const r = await api("/api/pick-folder", { initial }).catch((e) => ({ error: e.message }));
  if (!r.error) return r.path || "";
  if (r.busy) { alert(r.error); return ""; }
  return (prompt(r.error + "\nフォルダの場所を入力してください（例: D:\\notes\\characters）", initial) || "").trim();
}

// フォルダを変えたあと一覧を読み直す。チェック中でまだ保存していない選択はそのまま
async function reloadFileLists() {
  const keep = state && { ...state.setting, system_file: $("f_system_file").value, files_extra: $("f_files_extra").value,
    ref_files: [...$("refList").querySelectorAll("input:checked")].map((x) => x.value), folder_files: readFolderFiles() };
  await loadOptions();
  if (keep) fillFileOptions(keep);
}

function fillForm(st) {
  for (const k of FIELDS) if ($("f_" + k).tagName !== "SELECT") $("f_" + k).value = st[k] || "";
  $("f_image_mode").value = st.image_mode || "always";
  $("f_input_mode").value = st.input_mode || "diff";
  $("f_agents_md").checked = !!st.agents_md;
  $("f_ide_revert").checked = st.ide_revert !== false;
  $("f_rules_every").checked = !!st.rules_every;
  $("f_rp_state").checked = !!st.rp_state;
  for (const [id, k] of Object.entries(CHAR)) $("f_" + id).value = (st.character || {})[k] || "";
  $("f_image").value = JSON.stringify(st.image || {}, null, 2);
  fillFileOptions(st);
}
function readForm() {
  const st = JSON.parse(JSON.stringify(state.setting));
  for (const k of FIELDS) st[k] = $("f_" + k).value;
  st.agents_md = $("f_agents_md").checked;
  st.ide_revert = $("f_ide_revert").checked;
  st.rules_every = $("f_rules_every").checked;
  st.rp_state = $("f_rp_state").checked;
  st.character = st.character || {};
  for (const [id, k] of Object.entries(CHAR)) st.character[k] = $("f_" + id).value;
  st.image = JSON.parse($("f_image").value);
  st.ref_files = [...$("refList").querySelectorAll("input:checked")].map((x) => x.value);
  st.folder_files = readFolderFiles();
  st.files_extra = $("f_files_extra").value;
  return st;
}
document.querySelectorAll("#settingsPanel input, #settingsPanel textarea, #settingsPanel select").forEach((el) =>
  el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
    if (!el.closest("#appSettings") && !el.closest("#charBox") && el.id !== "presetSelect" && el.id !== "folderFileFilter" && el.id !== "ideModel") formDirty = true;
  }));

// ---------------------------------------------------------------- 設定ファイル・読み込むファイル
async function loadOptions() {
  try {
    const [m, f, o, p] = await Promise.all([api("/api/meta"), api("/api/files"), api("/api/folder-files"), api("/api/file-presets")]);
    meta = m;
    sysFiles = f.files || [];
    folderOpts = o.folders || [];
    presets = p.presets || [];
  } catch (e) { flash("一覧の読み込みに失敗: " + e.message); }
  $("sysRoot").textContent = $("sysRoot").title = meta.app?.system_root || "";
  fillFolderRows(strList(meta.app?.file_folders));
  $("app_new_channel_ide").checked = meta.app?.new_channel_ide !== false;
  showPhone(meta.phone || {});
  fillSelect($("presetSelect"), presets.map((x) => ({ value: x.id, label: x.name })), $("presetSelect").value, "プリセット…");
  if (state && !formDirty) fillFileOptions(state.setting);
  await loadCharPresets();
}

// スマホで開くアドレスと QR コード（start_mobile.bat で起動したときだけサーバーが返す）
function showPhone(ph) {
  $("phoneOn").hidden = !ph.url;
  $("phoneOff").hidden = !!ph.url;
  $("phoneUrl").textContent = ph.url || "";
  $("phoneQr").hidden = !ph.qr;
  if (ph.qr) $("phoneQr").src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(ph.qr);
}
$("phoneCopy").onclick = async () => {
  try {
    await navigator.clipboard.writeText($("phoneUrl").textContent);
    flash("アドレスをコピーしました");
  } catch {  // http のスマホからはコピーできないので、選ぶだけにする
    getSelection().selectAllChildren($("phoneUrl"));
    flash("コピーできないので、選んだアドレスを長押しでコピーしてください");
  }
};

// ---------------------------------------------------------------- 担当IDE（Antigravity の会話）
async function loadIdeSessions() {
  try {
    const r = await checked("/api/ide/sessions");
    ideSessions = r.sessions || [];
    ideNewDir = r.new_dir || "";
    $("ideNew").title = `Antigravity に新しい会話を作って担当にする（フォルダ: ${ideNewDir}。何も送らない。最初の入力で合図が届く）`;
  } catch (e) {
    ideSessions = [];
    $("ideState").textContent = "IDEの会話一覧を読めません: " + e.message;
  }
  renderIde();
  loadAgents();
}

// AGENTS.md: 作業フォルダの場所、書き出せたか、担当の会話が読んでいるか
let agentsFolder = "";
async function loadAgents() {
  if (!sid) return;
  const want = sid;
  let a;
  try { a = await checked("/api/agents?sid=" + encodeURIComponent(sid)); }
  catch (e) { $("agentsState").innerHTML = `<span class="ide-off">${esc(e.message)}</span>`; return; }
  if (want !== sid) return;
  agentsFolder = a.folder;
  $("f_ide_folder").placeholder = a.fixed ? "" : a.folder.split(/[\\/]/).pop() + "（未保存の既定。保存か新規会話で決まる）";
  $("agentsPreview").textContent = a.text || a.error || "";
  const ide = state?.setting?.ide_session;
  const lines = [];
  if (!a.enabled) {
    lines.push(`<span class="muted">書き出していない（オンにして保存すると ${esc(a.path)} に書く）</span>`);
  } else {
    const s = a.state;
    if (s && !s.ok) lines.push(`<span class="ide-off">✖ ${esc(s.msg)}</span>`);
    else if (s) lines.push(`<span class="ide-on">✔ ${esc(s.path)}（${esc(s.msg)} ${esc(s.ts.slice(11))}）</span>`);
    else lines.push(`<span class="muted">${esc(a.path)}（次の保存・入力の受け取りで書く）</span>`);
    if (a.error) lines.push(`<span class="ide-off">${esc(a.error)}</span>`);
    if (ide && a.in_use) lines.push('<span class="ide-on">担当の会話はこのフォルダなので毎ターン読み込まれる（System は wait_input の表示から外す）</span>');
    else if (ide && a.workspace) lines.push(`<span class="ide-off">⚠ 担当の会話の作業フォルダは ${esc(a.workspace)} なので、この AGENTS.md は読まれない。下の「移す」で会話ごとこのフォルダへ引っ越すか、「新規会話」でこのフォルダの会話を作ると効く（それまで System は今までどおり wait_input で見せる）</span>`);
  }
  $("agentsState").innerHTML = lines.join("<br>");
  let mv;
  try { mv = await checked("/api/ide/move"); } catch (e) { $("moveBox").innerHTML = ""; return; }
  if (want !== sid) return;
  renderMoves(mv, a.enabled && ide && a.workspace && !a.in_use);
}

// 会話の引っ越し: 担当の会話の作業フォルダをチャンネルのフォルダへ（Antigravity の再起動が要るので予約してまとめて移す）
function renderMoves(mv, canMove) {
  const ide = state?.setting?.ide_session;
  const pend = mv.pending || [];
  const mine = pend.find((x) => x.ide === ide);
  const st = mv.state || {};
  const box = $("moveBox");
  const parts = [];
  if (st.running) parts.push(`<span class="ide-on">⏳ ${esc(st.msg)}</span>`);
  else if (st.msg) {
    const res = (st.results || []).map((r) => `${r.ok ? "✔" : "✖"} ${esc(r.title || r.ide.slice(0, 8))}: ${esc(r.msg)}`).join("<br>");
    parts.push(`<span class="muted">前回の引っ越し ${esc((st.ts || "").slice(11))}: ${esc(st.msg)}${res ? "<br>" + res : ""}</span>`);
  }
  if (mine) parts.push(`<span class="ide-off">この会話は ${esc(mine.folder)} へ移す予約中</span><button type="button" class="sub" id="moveCancel">予約を取り消す</button>`);
  else if (canMove && !st.running) parts.push('<button type="button" class="sub" id="moveReserve" title="履歴はそのまま、会話の作業フォルダだけ変える。Antigravity の再起動が要るので、ここでは予約だけ">この会話をチャンネルのフォルダへ移す（予約）</button>');
  if (pend.length && !st.running) {
    parts.push(`<button type="button" id="moveRun" title="${esc(pend.map((x) => `${x.channel}: ${x.title} → ${x.folder}`).join("\n"))}">Antigravity を再起動して移す（${pend.length}件）</button>`);
  }
  box.innerHTML = parts.join(" ");
  if ($("moveCancel")) $("moveCancel").onclick = async () => {
    try { renderMoves(await checked("/api/ide/move", { sid, cancel: true }), canMove); } catch (e) { alert(e.message); }
  };
  if ($("moveReserve")) $("moveReserve").onclick = async () => {
    if (!confirm(`担当の会話の作業フォルダを\n${agentsFolder}\nへ移す予約をする？\n\n・履歴はそのまま。移したあとはこのフォルダの AGENTS.md を毎ターン読む\n・会話からは元のフォルダのファイルが「作業フォルダ」として見えなくなる\n・実際に移すのは「Antigravity を再起動して移す」を押したとき`)) return;
    try {
      const r = await checked("/api/ide/move", { sid });
      if (r.others?.length) alert(`この会話はほかのチャンネルでも担当になっています:\n${r.others.join("\n")}\n移すと、その会話はこのチャンネルの AGENTS.md を読むようになります`);
      renderMoves(r, canMove);
    } catch (e) { alert(e.message); }
  };
  if ($("moveRun")) $("moveRun").onclick = () => runMoves(pend, false);
}

async function runMoves(pend, force) {
  if (!force && !confirm(`Antigravity を一度閉じて、次の会話の作業フォルダを書き換え、起動し直す:\n\n${pend.map((x) => `・${x.title}（${x.channel}）\n   → ${x.folder}`).join("\n")}\n\nAntigravity で開いているほかの会話も一度閉じられる。続ける？`)) return;
  let r;
  try { r = await checked("/api/ide/move-run", { force }); } catch (e) { alert(e.message); return; }
  if (r.busy) {
    if (confirm(`応答中の会話がある:\n${r.busy.join("\n")}\n\n止めて（Antigravity を閉じて）移す？`)) return runMoves(pend, true);
    return;
  }
  flash("Antigravity を閉じて会話を移しています…（数十秒）");
  for (let i = 0; i < 90; i++) {
    await new Promise((ok) => setTimeout(ok, 2000));
    let mv;
    try { mv = await checked("/api/ide/move"); } catch { continue; }
    if (!mv.state?.running) {
      flash("引っ越し: " + (mv.state?.msg || "終わった"));
      loadIdeSessions();
      return;
    }
  }
}

function renderIde() {
  if (!state) return;
  const ide = state.setting?.ide_session || "";
  const cur = ideSessions.find((x) => x.id === ide);
  $("ideCurrent").innerHTML = ide
    ? `${esc(cur?.title && cur.title !== "(無題)" ? cur.title : state.setting.ide_title || "(無題)")} <span class="muted">${esc(ide)}${cur?.workspace ? " ・ " + esc(cur.workspace) : ""}</span>`
    : state.setting.ide_auto
      ? "まだなし（Antigravity が起動していれば、数秒で新規会話が付きます。起動していなければ、起動したら付きます）"
      : "指定なし（--ide なしの wait_input.py が受け取る）";
  $("ideClear").disabled = $("ideFork").disabled = $("ideRename").disabled = !ide;
  $("idePokeNow").disabled = !ide;
  renderModels();
  const pk = cur?.poke;
  $("pokeState").innerHTML = ide && pk
    ? `<span class="${pk.ok ? "ide-on" : "ide-off"}">最後の合図 ${esc(pk.ts.slice(11))}: ${esc(pk.msg)}</span>` : "";
  if (!ide) {
    if (ideSessions.length) $("ideState").textContent = "";
    return;
  }
  $("ideState").innerHTML = cur?.waiting
    ? '<span class="ide-on">● この会話で wait_input.py が待機中です</span>'
    : `<span class="ide-off">○ 未待機（python wait_input.py --ide ${esc(ide)}）</span>`;
}

async function assignIde(ide, title) {
  try {
    let r = await checked("/api/ide/assign", { sid, ide, title });
    if (r.conflict) {  // ほかのチャンネルがもうこの会話を担当にしている
      if (!confirm(`この会話は、すでに次のチャンネルにつながっています:\n\n${r.conflict.map((t) => "・" + t).join("\n")}\n\n` +
        "このチャンネルもつなぐと、どのチャンネルの入力もこの会話に届き、同じ会話の中で混ざります（応答も遅くなる）。\nそれでもつなぐ？")) return false;
      r = await checked("/api/ide/assign", { sid, ide, title, force: true });
    }
    await poll(); refreshSessions(); return true;
  }
  catch (e) { alert(e.message); renderIde(); return false; }
}

// 会話を選ぶ: フォルダ（ワークスペース）ごとにまとめて全部見せる。検索は全フォルダから
let ideFolder = null;  // 選んでいるフォルダ（小文字）。"" は全部
const folderKey = (x) => (x.workspace || "").toLowerCase();
function renderIdePicker() {
  const q = $("ideQuery").value.trim().toLowerCase();
  const ide = state?.setting?.ide_session || "";
  const hit = ideSessions.filter((x) => !q || [x.title, x.workspace, x.id].some((v) => (v || "").toLowerCase().includes(q)));
  const folders = new Map();  // 新しい会話があるフォルダが上（ideSessions は新しい順）
  for (const x of hit) {
    const k = folderKey(x);
    if (!folders.has(k)) folders.set(k, { label: x.workspace || "（フォルダなし）", n: 0 });
    folders.get(k).n++;
  }
  if (ideFolder === null) {  // 開いたときは担当の会話のフォルダ（なければ全部）
    const c = ideSessions.find((x) => x.id === ide);
    ideFolder = c ? folderKey(c) : "";
  }
  if (ideFolder && !folders.has(ideFolder)) ideFolder = "";
  const newKey = (ideNewDir || "").replace(/\\/g, "/").toLowerCase();
  const fl = $("ideFolders");
  fl.innerHTML = "";
  const addFolder = (k, label, n) => {
    const d = document.createElement("div");
    d.className = "ide-folder" + (k === ideFolder ? " selected" : "");
    d.innerHTML = `${k && k === newKey ? "★ " : ""}${esc(label)} <span class="n">(${n})</span>`;
    d.title = k && k === newKey ? "「新規会話」で作るフォルダ" : label;
    d.onclick = () => { ideFolder = k; renderIdePicker(); };
    fl.appendChild(d);
  };
  addFolder("", q ? "検索結果すべて" : "すべて（新しい順）", hit.length);
  for (const [k, f] of folders) addFolder(k, f.label, f.n);
  const list = hit.filter((x) => !ideFolder || folderKey(x) === ideFolder);
  const cl = $("ideConvs");
  cl.innerHTML = list.length ? "" : `<span class="muted">${ideSessions.length ? "会話がありません" : esc($("ideState").textContent || "会話がありません")}</span>`;
  for (const x of list.slice(0, 300)) {
    const d = document.createElement("div");
    d.className = "ide-conv" + (x.id === ide ? " current" : "");
    d.innerHTML = `${x.steps ? '<button type="button" class="sub fork-btn" title="この会話を途中の発言から分岐する">分岐…</button>' : ""}
      <button type="button" class="sub fork-btn rename-btn" title="この会話の名前を変える">名前…</button>
      <div class="t">${x.waiting ? "● " : ""}${x.id === ide ? "✔ " : ""}${esc(x.title)}</div>
      <div class="s">${esc((x.updated || "").slice(0, 16))} ・ ${x.steps ?? "?"}ステップ ・ ${esc(x.id.slice(0, 8))}${ideFolder ? "" : " ・ " + esc(x.workspace || "")}</div>`;
    d.onclick = async () => { if (await assignIde(x.id, x.title)) $("ideModal").classList.add("hidden"); };
    const fb = d.querySelector(".fork-btn:not(.rename-btn)");
    if (fb) fb.onclick = (e) => { e.stopPropagation(); openFork(x.id, x.title); };
    d.querySelector(".rename-btn").onclick = async (e) => {
      e.stopPropagation();
      if (await renameIde(x.id, x.title)) renderIdePicker();
    };
    cl.appendChild(d);
  }
  if (list.length > 300) cl.insertAdjacentHTML("beforeend", `<span class="muted">ほか ${list.length - 300} 件（検索で絞り込む）</span>`);
}
$("idePick").onclick = async () => {
  ideFolder = null;
  $("ideQuery").value = "";
  $("ideIdInput").value = "";
  $("ideFolders").innerHTML = "";
  $("ideConvs").innerHTML = '<span class="muted">読み込み中…</span>';
  $("ideModal").classList.remove("hidden");
  await loadIdeSessions();
  renderIdePicker();
};
$("ideQuery").addEventListener("input", () => { ideFolder = ""; renderIdePicker(); });
$("ideModalClose").onclick = () => $("ideModal").classList.add("hidden");
$("ideModal").addEventListener("click", (e) => { if (e.target === $("ideModal")) $("ideModal").classList.add("hidden"); });
$("ideRefresh").onclick = async () => { await loadIdeSessions(); renderIdePicker(); };
$("ideIdSet").onclick = async () => {
  const id = $("ideIdInput").value.trim();
  if (!/^[0-9a-zA-Z-]+$/.test(id)) return alert("会話IDは英数字とハイフンだけ");
  const x = ideSessions.find((s) => s.id === id || (id.length >= 8 && s.id.startsWith(id)));
  if (!x && !confirm(`一覧にない会話IDです。このまま担当にする？\n${id}`)) return;
  if (await assignIde(x ? x.id : id, x ? x.title : "")) $("ideModal").classList.add("hidden");
};
$("ideClear").onclick = () => assignIde("", "");

// 会話の名前を変える（Antigravity の一覧の名前。担当にしているチャンネルの控えも変わる）
async function renameIde(id, cur) {
  const title = prompt("会話の新しい名前", cur && cur !== "(無題)" ? cur : "");
  if (title === null || !title.trim() || title.trim() === cur) return false;
  try {
    const r = await checked("/api/ide/rename", { ide: id, title: title.trim() });
    const x = ideSessions.find((s) => s.id === id);
    if (x) x.title = r.title;  // 一覧の元（Antigravity の会話一覧）に反映されるまで少しかかることがあるので先に直す
    flash("名前を変えました: " + r.title);
    if (state?.setting?.ide_session === id) { await poll(); refreshSessions(); }
    renderIde();
    return true;
  } catch (e) { alert(e.message); return false; }
}
$("ideRename").onclick = () => {
  const ide = state?.setting?.ide_session;
  if (!ide) return;
  const cur = ideSessions.find((x) => x.id === ide);
  renameIde(ide, cur?.title || state.setting.ide_title || "");
};
$("ideNew").onclick = async () => {
  if (formDirty && ($("f_ide_folder").value.trim() !== (state?.setting?.ide_folder || ""))) {
    alert("作業フォルダを変えたら、先に設定を保存してください"); return;
  }
  const dir = agentsFolder || "チャンネルの作業フォルダ";
  const cur = state?.setting?.ide_session;
  if (!confirm(`Antigravity に新しい会話を作って、このチャンネルの担当にする？\nフォルダ: ${dir}` +
    (cur ? "\n（いまの担当の会話は消えない。担当から外れるだけ）" : ""))) return;
  $("ideNew").disabled = true;
  try {
    const r = await checked("/api/ide/new", { sid });
    flash("新しい会話を作って担当にしました: " + r.ide.slice(0, 8) + (r.msg !== "作った" ? " " + r.msg : ""));
    await poll(); refreshSessions(); loadIdeSessions();
  } catch (e) { alert(e.message); }
  $("ideNew").disabled = false;
};

// 分岐: IDE会話のユーザー発言を並べ、選んだ発言の直前までをコピーした新しい会話を作る（元の会話はそのまま）
let forkSrc = null;  // { id, title, turns }
async function openFork(id, title) {
  forkSrc = { id, title: title || "", turns: null };
  $("forkSrc").textContent = `元: ${title || "(無題)"}（${id.slice(0, 8)}）`;
  $("forkMove").checked = false;
  $("forkMoveTo").disabled = true;
  $("forkMoveTo").value = agentsFolder || "";  // 既定はこのチャンネルの作業フォルダ
  $("forkTurns").innerHTML = '<span class="muted">発言を読み込み中…</span>';
  $("forkModal").classList.remove("hidden");
  try {
    const r = await checked("/api/ide/turns?ide=" + encodeURIComponent(id));
    if (forkSrc?.id !== id) return;
    forkSrc.turns = r.turns;
    if (r.title) forkSrc.title = r.title;
    $("forkSrc").textContent = `元: ${forkSrc.title || "(無題)"}（${id.slice(0, 8)}・${r.steps ?? "?"}ステップ・発言 ${r.turns.length} 件）`;
    renderForkTurns();
  } catch (e) { $("forkTurns").innerHTML = `<span class="ide-off">${esc(e.message)}</span>`; }
}
function renderForkTurns() {
  const box = $("forkTurns");
  if (!forkSrc?.turns) return;
  const hide = $("forkHideSpace").checked;
  const list = forkSrc.turns.filter((t) => !hide || t.text.trim()).reverse();  // 新しい発言が上
  box.innerHTML = list.length ? "" : '<span class="muted">発言がありません</span>';
  for (const t of list) {
    const d = document.createElement("div");
    d.className = "ide-conv fork-turn";
    const when = t.time ? new Date(t.time).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
    d.innerHTML = `<div class="body"><div class="t">${t.text.trim() ? esc(t.text) : '<span class="muted">（合図のスペース）</span>'}</div>
      <div class="s">#${t.n} ・ ${esc(when)} ・ ステップ ${t.step}</div></div>`;
    if (t.at !== null) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "sub"; b.textContent = "この発言からやり直す";
      b.title = `ステップ ${t.at} まで（この発言の直前）をコピーして分岐する`;
      b.onclick = () => doFork(t.at, t.n, `#${t.n}「${t.text.trim().slice(0, 30) || "（スペース）"}」の直前`);
      d.appendChild(b);
    }
    box.appendChild(d);
  }
}
async function doFork(at, n, label) {
  const assign = $("forkAssign").checked;
  const moveTo = $("forkMove").checked ? $("forkMoveTo").value.trim() : "";
  if ($("forkMove").checked && !moveTo) return alert("移す先のフォルダを入れてください");
  if (!confirm(`${label}で分岐する？\n元の会話はそのまま残ります。` + (assign ? "\n分岐した会話をこのチャンネルの担当にします。" : "") +
    (moveTo ? `\n分岐したあと、その会話を\n${moveTo}\nへ移します（Antigravity を再起動。もう一度確かめます）。` : ""))) return;
  $("forkModal").querySelectorAll("button").forEach((b) => (b.disabled = true));
  let r = null;
  try {
    r = await checked("/api/ide/fork", { sid, ide: forkSrc.id, title: forkSrc.title, at, n, assign, move_to: moveTo });
    flash(`分岐しました: ${r.title}（${r.ide.slice(0, 8)}）` + (assign ? "。担当にしました" : "") + (r.msg && r.msg.includes("移さない") ? "。移す先が元と同じフォルダなので移しません" : ""));
    $("forkModal").classList.add("hidden");
    $("ideModal").classList.add("hidden");
    if (assign) { await poll(); refreshSessions(); }
    loadIdeSessions();
  } catch (e) { alert(e.message); }
  $("forkModal").querySelectorAll("button").forEach((b) => (b.disabled = false));
  if (r?.move) {
    // 予約しただけなので、ここで移す（やめても予約は残り、設定の「Antigravity を再起動して移す」からできる）
    let pend = [r.move];
    try { pend = (await checked("/api/ide/move")).pending || pend; } catch { }
    await runMoves(pend, false);
    loadAgents();
  }
}
$("forkMove").onchange = () => { $("forkMoveTo").disabled = !$("forkMove").checked; if ($("forkMove").checked) $("forkMoveTo").focus(); };
$("ideFork").onclick = () => {
  const ide = state?.setting?.ide_session;
  if (!ide) return;
  const cur = ideSessions.find((x) => x.id === ide);
  openFork(ide, cur?.title || state.setting.ide_title || "");
};
$("forkLatest").onclick = () => doFork(-1, 0, "最新の状態");
$("forkHideSpace").onchange = renderForkTurns;
$("forkClose").onclick = () => $("forkModal").classList.add("hidden");
$("forkModal").addEventListener("click", (e) => { if (e.target === $("forkModal")) $("forkModal").classList.add("hidden"); });
// 合図を送るときのモデル。一覧は Antigravity から読む（読めなくても、選んであるモデルは出す）
let ideModels = null;
async function loadModels() {
  try {
    ideModels = await checked("/api/ide/models");
  } catch (e) {
    ideModels = { models: [], error: e.message };
  }
  renderModels();
}
function renderModels() {
  const sel = $("ideModel");
  if (!state || document.activeElement === sel) return;
  const want = state.setting?.ide_model || "";
  const rows = ideModels?.models || [];
  const reset = (r) => {
    const d = new Date(r.reset);
    return isNaN(d) ? "" : `・${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}に回復`;
  };
  const left = (r) => (typeof r.left !== "number" ? ""
    : r.left > 0 ? `（残り${Math.max(1, Math.round(r.left * 100))}%）` : `（使い切り${reset(r)}）`);
  const opts = [`<option value="">会話の最後の入力と同じ${ideModels?.error ? "（一覧を読めない）" : ""}</option>`]
    .concat(rows.map((r) => `<option value="${esc(r.model)}">${esc(r.label)}${left(r)}${r.model === ideModels.default ? " ・既定" : ""}</option>`));
  if (want && !rows.some((r) => r.model === want)) opts.push(`<option value="${esc(want)}">${esc(want)}（いまは選べない）</option>`);
  const html = opts.join("");
  if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
  sel.value = want;
  $("footBar").classList.toggle("hidden", !state.setting?.ide_session);  // 担当の会話がなければ合図もしない
}
$("ideModel").onchange = async () => {
  try {
    await checked("/api/ide/model", { sid, model: $("ideModel").value });
    flash("合図のモデル: " + $("ideModel").selectedOptions[0].textContent);
    await poll();
  } catch (e) { alert(e.message); }
  $("ideModel").blur();
  renderModels();
};
$("ideModelReload").onclick = loadModels;
$("idePokeNow").onclick = async () => {
  $("idePokeNow").disabled = true;
  try {
    const r = await checked("/api/ide/poke", { sid, now: true });
    flash((r.ok ? "合図を送りました: " : "合図できませんでした: ") + r.msg);
  } catch (e) { alert(e.message); }
  $("idePokeNow").disabled = false;
  loadIdeSessions();
};
$("ctxReset").onclick = async () => {
  try {
    await checked("/api/context/reset", { sid });
    $("ideState").textContent = "次の入力では設定と会話を全部送ります";
    setTimeout(renderIde, 3000);
  } catch (e) { alert(e.message); }
};

$("presetLoad").onclick = () => {
  const p = presets.find((x) => x.id === $("presetSelect").value);
  if (!p) return;
  const st = readForm();
  st.folder_files = strList(p.files);
  st.files_extra = p.extra || "";
  fillFileOptions(st);
  formDirty = true;
  flash(`プリセット「${p.name}」を読み込みました（保存で確定）`);
};
$("presetSave").onclick = async () => {
  const name = prompt("プリセット名");
  if (!name) return;
  const body = { name, files: readFolderFiles(), extra: $("f_files_extra").value };
  try { await checked("/api/file-presets", body); await loadOptions(); flash("プリセットを保存しました"); }
  catch (e) { alert(e.message); }
};
$("presetDelete").onclick = async () => {
  const p = presets.find((x) => x.id === $("presetSelect").value);
  if (!p || !confirm(`プリセット「${p.name}」を削除しますか？`)) return;
  try { await checked("/api/file-presets/delete", { id: p.id }); $("presetSelect").value = ""; await loadOptions(); }
  catch (e) { alert(e.message); }
};
// ---------------------------------------------------------------- 画像のキャラ固定
let charPresets = [];
let newCharRow = false;  // 手動追加の空の行を出しているか
const SRC_LABEL = { ai: "AI作成", user: "手動", preset: "プリセット" };
const charKey = (n) => (n || "").replace(/\s+/g, "").toLowerCase();

// 編集中の行があるあいだは、AIの応答などで一覧を描き直さない（打ちかけが消えないように）
function charsDirty() {
  return newCharRow || !!$("charList").querySelector(".char-row.dirty");
}
function renderChars() {
  if (!state || charsDirty()) return;
  const list = $("charList");
  list.innerHTML = "";
  const chars = state.setting.chars || [];
  if (!chars.length) list.innerHTML = '<span class="muted">まだいません（AIがキャラを出すと自動で入ります）</span>';
  for (const c of chars) list.appendChild(charRow(c));
}
function charRow(c) {
  const row = document.createElement("div");
  row.className = "char-row" + (c.id ? "" : " dirty");
  row.innerHTML = `<div class="row tight">
      <input class="char-name" placeholder="キャラ名">
      <span class="char-src"></span>
      <label class="check" title="AIに外見タグを書き換えさせない"><input type="checkbox" class="char-lock"> 🔒固定</label>
    </div>
    <textarea class="char-tags mono" rows="2" spellcheck="false" placeholder="black hair, bob cut, blunt bangs, blue eyes, ..."></textarea>
    <div class="row tight">
      <button type="button" class="sub char-save">保存</button>
      <button type="button" class="sub char-preset">プリセットに保存</button>
      <button type="button" class="sub char-del">削除</button>
    </div>`;
  const q = (s) => row.querySelector(s);
  q(".char-name").value = c.name || "";
  q(".char-tags").value = c.tags || "";
  q(".char-lock").checked = !!c.locked;
  q(".char-src").textContent = c.id ? SRC_LABEL[c.source] || "" : "新規";
  const dirty = () => row.classList.add("dirty");
  q(".char-name").oninput = dirty;
  q(".char-lock").onchange = dirty;
  q(".char-tags").oninput = () => { dirty(); q(".char-lock").checked = true; };  // 手で直したタグはAIに戻されないように
  q(".char-save").onclick = async () => {
    const name = q(".char-name").value.trim(), tags = q(".char-tags").value.trim();
    const source = c.source === "preset" && tags === c.tags ? "preset" : "user";
    try {
      await checked("/api/chars/upsert", { sid, char: { id: c.id, name, tags, locked: q(".char-lock").checked, source } });
      row.classList.remove("dirty");
      if (!c.id) newCharRow = false;
      await poll();
      renderChars();
    } catch (e) { alert(e.message); }
  };
  q(".char-preset").onclick = async () => {
    const name = q(".char-name").value.trim(), tags = q(".char-tags").value.trim();
    if (!name || !tags) return alert("名前とタグを入れてください");
    if (charPresets.some((p) => charKey(p.name) === charKey(name)) && !confirm(`プリセット「${name}」を上書きしますか？`)) return;
    try { await checked("/api/char-presets", { name, tags }); await loadCharPresets(); flash(`キャラプリセット「${name}」を保存しました`); }
    catch (e) { alert(e.message); }
  };
  q(".char-del").onclick = async () => {
    if (c.id) {
      if (!confirm(`「${c.name}」の固定を外しますか？（控えは data/trash に残ります）`)) return;
      try { await checked("/api/chars/delete", { sid, id: c.id }); } catch (e) { return alert(e.message); }
    } else newCharRow = false;
    row.remove();
    await poll();
    renderChars();
  };
  return row;
}
async function loadCharPresets() {
  try { charPresets = (await checked("/api/char-presets")).presets || []; } catch (e) { flash(e.message); }
  fillSelect($("charPresetSelect"), charPresets.map((p) => ({ value: p.id, label: `${p.name}（${p.tags.slice(0, 40)}${p.tags.length > 40 ? "…" : ""}）` })),
    $("charPresetSelect").value, "キャラプリセット…");
}
$("charNew").onclick = () => {
  if (newCharRow) return;
  newCharRow = true;
  $("charList").querySelector(":scope > .muted")?.remove();
  const row = charRow({ id: "", name: "", tags: "", locked: true, source: "user" });
  $("charList").appendChild(row);
  row.querySelector(".char-name").focus();
};
$("charPresetAdd").onclick = async () => {
  const p = charPresets.find((x) => x.id === $("charPresetSelect").value);
  if (!p || !state) return;
  const cur = (state.setting.chars || []).find((c) => charKey(c.name) === charKey(p.name));
  if (cur && !confirm(`このチャンネルの「${cur.name}」をプリセットのタグで上書きしますか？`)) return;
  try {
    await checked("/api/chars/upsert", { sid, char: { id: cur?.id, name: p.name, tags: p.tags, locked: true, source: "preset" } });
    await poll();
    renderChars();
    flash(`「${p.name}」をこのチャンネルに固定しました`);
  } catch (e) { alert(e.message); }
};
$("charPresetDelete").onclick = async () => {
  const p = charPresets.find((x) => x.id === $("charPresetSelect").value);
  if (!p || !confirm(`キャラプリセット「${p.name}」を削除しますか？（各チャンネルに追加済みのものは残ります）`)) return;
  try { await checked("/api/char-presets/delete", { id: p.id }); $("charPresetSelect").value = ""; await loadCharPresets(); }
  catch (e) { alert(e.message); }
};

$("appSave").onclick = async () => {
  try {
    await checked("/api/app-settings", { new_channel_ide: $("app_new_channel_ide").checked });
    await loadOptions();
    $("appMsg").textContent = "保存しました";
    setTimeout(() => ($("appMsg").textContent = ""), 3000);
  } catch (e) { alert(e.message); }
};
$("previewBtn").onclick = async () => {
  if (formDirty && !confirm("未保存の変更があります。保存済みの設定で表示します。")) return;
  const r = await api("/api/prompt-preview?sid=" + encodeURIComponent(sid));
  openText("AIに送る設定（" + (r.chars || 0) + "文字）",
    (r.missing?.length ? "⚠ 読めないファイル: " + r.missing.join(", ") + "\n" : "") + "wait_input.py が入力を受け取ったとき、この設定とAIメモ・会話・発言をそのまま表示します。担当IDEが前回と同じなら、変わった設定と増えた発言だけを出します。",
    r.text || "", null);
};

// ---------------------------------------------------------------- 汎用テキストモーダル
function openText(title, hint, body, onSave) {
  $("textTitle").textContent = title;
  $("textHint").textContent = hint;
  $("textBody").value = body;
  $("textBody").readOnly = !onSave;
  $("textSave").classList.toggle("hidden", !onSave);
  textSaveHandler = onSave;
  $("textModal").classList.remove("hidden");
}
$("textCancel").onclick = () => $("textModal").classList.add("hidden");
$("textSave").onclick = async () => {
  if (!textSaveHandler) return;
  try { await textSaveHandler($("textBody").value); $("textModal").classList.add("hidden"); }
  catch (e) { alert(e.message); }
};

// ---------------------------------------------------------------- 会話管理
$("exportBtn").onclick = () => { location.href = "/api/session/export?sid=" + encodeURIComponent(sid); };
$("importBtn").onclick = () => $("importFile").click();
$("importFile").onchange = async () => {
  const f = $("importFile").files[0];
  $("importFile").value = "";
  if (!f) return;
  try {
    const data = JSON.parse(await f.text());
    const r = await checked("/api/session/import", { data });
    await refreshSessions();
    await switchTo(r.sid);
  } catch (e) { alert("取り込めませんでした: " + e.message); }
};
$("rawBtn").onclick = () => {
  if (!state) return;
  const { setting, messages } = state;
  openText("チャンネルの JSON を編集", "保存すると今の内容は data/trash に控えを取ってから置き換えます。",
    JSON.stringify({ setting, messages }, null, 2), async (text) => {
      const obj = JSON.parse(text);
      await checked("/api/session/raw", { sid, setting: obj.setting, messages: obj.messages });
      lastRev = -1;
      formDirty = false;
      await poll();
    });
};

// ---------------------------------------------------------------- chat
function render() {
  const s = state;
  $("title").textContent = s.setting?.title || "RP Image Chat";
  $("ragCount").textContent = (s.setting?.rag_collections || []).length;
  document.title = (s.setting?.title || "") + " - RP Image Chat";
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 120;
  log.innerHTML = "";

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
    bubble.className = "msg md " + m.role;
    bubble.innerHTML = m.edited ? fmtAltered(m.original_text, m.text || "") : fmt(m.text || "");
    if (m.edited) {
      bubble.classList.add("edited");
      bubble.title = "改変前: " + (m.original_text || "");
    }
    if (m.text || !m.files?.length) wrap.appendChild(bubble);
    if (m.files?.length) wrap.appendChild(filesBox(m.files));
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
        wrap.dataset.img = im.file;  // RP表示で、この発言を読んでいる間の背景にする
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
      if (im.chars?.length) {
        const who = document.createElement("span");
        who.className = "muted";
        who.title = "この画像に外見タグを付けた固定キャラ";
        who.textContent = "👤 " + im.chars.join(", ");
        tools.appendChild(who);
      }
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
    if (m.role === "assistant" && m.state?.length) wrap.appendChild(stateBox(m));

    const info = document.createElement("div");
    info.className = "meta";
    const st = m.role === "user" ? (m.status === "pending" ? " · 送信済" : m.status === "processing" ? " · 読込済" : "") : "";
    info.textContent = (m.ts || "").replace("T", " ").slice(5, 16) + st;
    wrap.appendChild(info);

    const acts = document.createElement("div");
    acts.className = "msg-actions";
    const del = document.createElement("button");
    del.textContent = "削除";
    del.onclick = async () => {
      if (!confirm("このメッセージを削除？")) return;
      const ch = sid, r = await post("/api/delete", { id: m.id });
      await poll();
      await revertIde(r, ch);
    };
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
      if (m.state || s.setting?.rp_state) {
        const sb = document.createElement("button");
        sb.textContent = "状態";
        sb.title = "この応答のときのキャラの状態と内心を直す（次の入力で AI が読む）";
        sb.onclick = () => openState(m, s.messages);
        acts.appendChild(sb);
      }
      if (s.messages.slice(0, s.messages.indexOf(m)).some((x) => x.role === "user")) {
        const regen = document.createElement("button");
        regen.textContent = "再生成";
        regen.onclick = async () => {
          if (!confirm("この応答以降の会話を削除して、AI応答を作り直しますか？")) return;
          const ch = sid;
          let r;
          try { r = await checked("/api/message/regenerate", { sid, id: m.id }); await poll(); }
          catch (e) { alert(e.message); return; }
          await revertIde(r, ch);
        };
        acts.appendChild(regen);
      }
    }
    const br = document.createElement("button");
    br.textContent = "分岐";
    br.title = "ここまでの会話で新しいチャンネルを作る（担当の Antigravity 会話も同じところで分岐して、新しいチャンネルの担当にする）";
    br.onclick = async () => {
      const src = sid, ide = state?.setting?.ide_session;
      let r;
      try { r = await checked("/api/session/branch", { sid, id: m.id }); await refreshSessions(); await switchTo(r.sid); }
      catch (e) { alert(e.message); return; }
      if (!ide) return;
      flash("Antigravity の会話も分岐しています…（大きい会話は数十秒）");
      try {
        const f = await checked("/api/ide/branch-fork", { sid: r.sid, src, id: m.id });
        if (f.skipped) alert("チャンネルは分岐しました。\n" + f.skipped);
        else {
          flash(`Antigravity の会話も分岐して担当にしました: ${f.title}（${f.ide.slice(0, 8)}）`);
          if (sid === r.sid) await poll();
          refreshSessions(); loadIdeSessions();
        }
      } catch (e) { alert("チャンネルは分岐しましたが、Antigravity の会話は分岐できませんでした:\n" + e.message); }
    };
    acts.append(cp, br);
    if (m !== s.messages[s.messages.length - 1]) {
      const tr = document.createElement("button");
      tr.textContent = "ここまで戻す";
      tr.onclick = async () => {
        if (!confirm("このメッセージより後の会話を消して、ここまで戻しますか？（控えは data/trash に残ります）")) return;
        const ch = sid;
        let r;
        try { r = await checked("/api/message/truncate", { sid, id: m.id }); await poll(); }
        catch (e) { alert(e.message); return; }
        await revertIde(r, ch);
      };
      acts.appendChild(tr);
    }
    acts.appendChild(del);
    wrap.appendChild(acts);

    log.appendChild(wrap);
  }

  const waiting = s.messages.some((m) => m.role === "user" && (m.status === "pending" || m.status === "processing"));
  if (waiting) {
    const t = document.createElement("div");
    t.className = "typing";
    t.innerHTML = "応答を書いています<span>.</span><span>.</span><span>.</span>";
    log.appendChild(t);
    if (s.setting?.ide_session) {
      const p = document.createElement("div");
      p.id = "progress";
      p.className = "progress";
      p.innerHTML = progressSid === s.id ? progressHtml : "";
      log.appendChild(p);
      if (!p.innerHTML) setTimeout(pollProgress, 0);  // 送った直後は2秒待たずに読む
    }
  } else if (progressSid === s.id) progressHtml = "";  // 次の入力で前のターンの表示が一瞬出ないように
  const pill = $("statusPill");
  pill.className = "pill " + (waiting ? "wait" : "ok");
  pill.textContent = waiting ? "AI応答待ち" : "入力どうぞ";

  log.classList.toggle("rp", rpMode);
  $("rpToggle").classList.toggle("on", rpMode);

  if (nearBottom) log.scrollTop = log.scrollHeight;
  updateRpBg();
  renderIde();
}

// RP表示の背景：いちばん下にいるときは最新の画像、過去ログを読んでいるときは
// 画面の 6 割の高さにかかっている発言のときに描いた画像（その発言に無ければ直前の画像）
let rpBgFile;
let rpTick = 0;
function updateRpBg() {
  if (!rpMode) return;
  const withImg = [...log.querySelectorAll(".msg-wrap[data-img]")];
  let file = null;
  if (withImg.length) {
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    if (atBottom) file = withImg[withImg.length - 1].dataset.img;
    else {
      const line = log.getBoundingClientRect().top + log.clientHeight * 0.6;
      file = withImg[0].dataset.img;  // 最初の画像より前を読んでいるときは最初の画像
      for (const w of withImg) {
        if (w.getBoundingClientRect().top > line) break;
        file = w.dataset.img;
      }
    }
  }
  if (file === rpBgFile) return;
  rpBgFile = file;
  log.style.setProperty("--rp-bg", file ? `url("${imgUrl(file)}")` : "none");
}
log.addEventListener("scroll", () => {
  if (!rpMode || rpTick) return;
  rpTick = requestAnimationFrame(() => { rpTick = 0; updateRpBg(); });
}, { passive: true });

async function poll() {
  if (!sid) return refreshSessions();
  const want = sid;
  try {
    const s = await api("/api/session?sid=" + encodeURIComponent(want));
    // 待っている間にチャンネルを切り替えていたら、前のチャンネルの結果は捨てる
    // （捨てないと前のチャンネルの設定が今のチャンネルの画面に出て、保存すると上書きしてしまう）
    if (want !== sid) return;
    if (s.error) { sid = ""; return refreshSessions(); }
    const key = s.id + ":" + s.rev;  // rev はチャンネルごとの番号なので id と組にして比べる
    if (key !== lastRev) {
      const first = lastRev === -1;
      lastRev = key;
      state = s;
      render();
      if (first) log.scrollTop = log.scrollHeight;
      if (first || !formDirty) { fillForm(s.setting); formSid = s.id; }
      renderChars();
    }
    markSeen();
  } catch (e) {
    $("statusPill").className = "pill err";
    $("statusPill").textContent = "サーバー未接続";
  }
}

// 応答待ちのあいだ、担当の Antigravity 会話がしていること（ツール呼び出し）を入力中の表示の下に映す
let progressHtml = "", progressSid = "", progressBusy = false;
async function pollProgress() {
  const s = state;
  if (progressBusy || !s || s.id !== sid || !s.setting?.ide_session || !$("progress")) return;
  progressBusy = true;
  try {
    const r = await api("/api/ide/progress?sid=" + encodeURIComponent(s.id));
    if (s.id !== sid) return;
    const items = r.items || [];
    const rows = items.map((it, i) => {
      const last = i === items.length - 1;
      const now = !it.done && last;
      const mark = it.kind === "error" ? "✕" : it.kind === "think" ? "…" : it.done ? "✓" : now ? "●" : "・";
      const cls = "p-row" + (now ? " now" : "") + (it.kind === "error" ? " error" : "");
      return `<div class="${cls}"><span class="p-mark">${mark}</span><span class="p-text">${esc(it.text)}</span>`
        + (it.detail ? `<span class="p-detail" title="${esc(it.detail)}">${esc(it.detail)}</span>` : "") + "</div>";
    });
    if (r.running && (!items.length || items[items.length - 1].done))
      rows.push(`<div class="p-row now"><span class="p-mark">●</span><span class="p-text">次にすることを考えている</span></div>`);
    if (!rows.length && r.note) rows.push(`<div class="p-note">${esc(r.note)}</div>`);
    progressHtml = rows.join("");
    progressSid = s.id;
    const box = $("progress");
    if (box && box.innerHTML !== progressHtml) {
      const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 120;
      box.innerHTML = progressHtml;
      if (nearBottom) log.scrollTop = log.scrollHeight;
    }
  } catch { /* 読めないときは前の表示のまま */ }
  finally { progressBusy = false; }
}

async function send() {
  const ta = $("messageInput");
  const text = ta.value.trim();
  if (attaching.some((a) => !a.file)) return alert("添付をアップロード中です。終わってから送ってください");
  if ((!text && !attaching.length) || !sid) return;
  $("sendBtn").disabled = true;
  const r = await post("/api/send", { text, files: attaching.map((a) => a.file) });
  $("sendBtn").disabled = false;
  if (r.error) return alert("送れませんでした: " + r.error);
  ta.value = "";
  attaching = [];
  renderAttach();
  await poll();
  log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------- 添付
// 選んだ・貼り付けた・ドロップしたファイルはすぐ上げておき、送信でその発言に付ける
const uploadUrl = (path) => "/upload?f=" + encodeURIComponent(path);
const fileSize = (n) => n >= 1048576 ? (n / 1048576).toFixed(1) + "MB" : Math.max(1, Math.round(n / 1024)) + "KB";
let attaching = [];  // { name, preview, file(上げ終わったら) }

function filesBox(files) {
  const box = document.createElement("div");
  box.className = "msg-files";
  for (const f of files) {
    if (/^image\/(png|jpeg|gif|webp|bmp)$/.test(f.type)) {
      const img = document.createElement("img");
      img.src = uploadUrl(f.path);
      img.alt = img.title = f.name;
      img.onclick = () => { $("lightboxImg").src = img.src; $("lightbox").classList.remove("hidden"); };
      box.appendChild(img);
    } else {
      const a = document.createElement("a");
      a.className = "file";
      a.href = uploadUrl(f.path);
      a.download = f.name;
      a.innerHTML = `📄 ${esc(f.name)} <span class="muted">${fileSize(f.size || 0)}</span>`;
      box.appendChild(a);
    }
  }
  return box;
}

function renderAttach() {
  const list = $("attachList");
  list.classList.toggle("hidden", !attaching.length);
  list.innerHTML = "";
  for (const a of attaching) {
    const chip = document.createElement("div");
    chip.className = "attach-chip" + (a.file ? "" : " busy");
    chip.title = a.file ? a.name : a.name + "（アップロード中）";
    if (a.preview) {
      const img = document.createElement("img");
      img.src = a.preview;
      chip.appendChild(img);
    }
    const nm = document.createElement("span");
    nm.className = "a-name";
    nm.textContent = (a.preview ? "" : "📄 ") + a.name;
    const del = document.createElement("button");
    del.className = "a-del";
    del.textContent = "×";
    del.title = "外す";
    del.onclick = () => { attaching = attaching.filter((x) => x !== a); renderAttach(); };
    chip.append(nm, del);
    list.appendChild(chip);
  }
}

function readDataUrl(file) {
  return new Promise((ok, ng) => {
    const fr = new FileReader();
    fr.onload = () => ok(fr.result);
    fr.onerror = () => ng(fr.error);
    fr.readAsDataURL(file);
  });
}

async function addFiles(list) {
  if (!sid) return;
  const ch = sid;
  for (const file of list) {
    if (file.size > 40 * 1048576) { alert(`${file.name} は大きすぎます（40MB まで）`); continue; }
    const name = file.name || (file.type.startsWith("image/") ? "image." + (file.type.split("/")[1] || "png") : "file");
    const a = { name, preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : "", file: null };
    attaching.push(a);
    renderAttach();
    try {
      const r = await checked("/api/upload", { sid: ch, name, data: await readDataUrl(file) });
      a.file = r.file;
    } catch (e) {
      alert(`${name} を添付できませんでした: ${e.message}`);
      attaching = attaching.filter((x) => x !== a);
    }
    renderAttach();
  }
}

// ---------------------------------------------------------------- キャラの状態
// 応答のときのキャラの状態。開いた箱は描き直しても開いたままにする
const openStates = new Set();
function stateBox(m) {
  const d = document.createElement("details");
  d.className = "rp-state";
  d.open = openStates.has(m.id);
  d.ontoggle = () => (d.open ? openStates.add(m.id) : openStates.delete(m.id));
  d.innerHTML = `<summary>📋 状態（${esc(m.state.map((c) => c.name).join("、"))}）</summary>` + m.state.map((c) =>
    `<div class="st-char"><div class="st-name">${esc(c.name)}</div>`
    + (c.situation?.length ? `<ul>${c.situation.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "")
    + (c.inner ? `<div class="st-inner">💭 ${esc(c.inner)}</div>` : "") + "</div>").join("");
  return d;
}
const stateText = (list) => list.map((c) =>
  [`■ ${c.name}`, ...(c.situation || []).map((x) => `- ${x}`), `内心: ${c.inner || ""}`].join("\n")).join("\n\n");
function parseState(text) {
  const out = [];
  let cur = null, inInner = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    const name = line.match(/^[■#]+\s*(.+)$/);
    if (name) { cur = { name: name[1].trim(), situation: [], inner: "" }; out.push(cur); inInner = false; continue; }
    if (!cur || !line) continue;
    const inner = line.match(/^内心\s*[:：]\s*(.*)$/);
    if (inner) { cur.inner = inner[1]; inInner = true; }
    else if (/^[-・*]/.test(line)) { cur.situation.push(line.replace(/^[-・*]\s*/, "")); inInner = false; }
    else if (inInner) cur.inner += (cur.inner ? "\n" : "") + line;
    else cur.situation.push(line);
  }
  return out;
}
function openState(m, msgs) {
  // この応答に無ければ、それより前に記録した状態から始める（サーバーの state_before と同じ）
  const prev = m.state || msgs.slice(0, msgs.indexOf(m)).reverse().find((x) => x.role === "assistant" && Array.isArray(x.state))?.state || [];
  openText("キャラの状態（この応答のとき）",
    "「■ 名前」の下に、今の状態を「- 」で1行ずつ、「内心:」の後に内心を書く。キャラを消すときはその塊ごと消す。保存すると、次の入力で AI がこの状態を読む。",
    stateText(prev) || "■ キャラ名\n- 今の状態\n内心: ",
    async (text) => { await checked("/api/message/state", { sid, id: m.id, state: parseState(text) }); await poll(); });
}

function openPrompt(m) {
  currentPromptMsg = m;
  $("promptEdit").value = m.image.prompt || "";
  const who = m.image.chars?.length ? `固定キャラ: ${m.image.chars.join(", ")}（外見タグは再生成時も今の設定で付く）\n\n` : "";
  $("promptFinal").textContent = who + (m.image.final_positive
    ? `最終 positive:\n${m.image.final_positive}\n\nnegative:\n${m.image.final_negative}\n\nseed: ${m.image.seed}`
    : "");
  $("promptModal").classList.remove("hidden");
}
function flash(msg) {
  $("settingMsg").textContent = msg;
  setTimeout(() => ($("settingMsg").textContent = ""), 3000);
}
// 設定パネルを閉じていても見える知らせ（タップで閉じる。ms = 0 なら次の知らせまで出したまま）
let toastTimer = null;
function toast(msg, ms = 6000) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
}
$("toast").onclick = () => $("toast").classList.add("hidden");

// 発言を書き換えたら、担当の Antigravity 会話も巻き戻す（チャンネルの設定がオンなら、サーバーが revert: true を返す）
async function revertIde(r, chSid) {
  if (!r?.revert) return;
  toast("Antigravity の会話も巻き戻しています…", 0);
  try {
    const v = await checked("/api/ide/revert", { sid: chSid });
    toast(v.skipped ? "Antigravity の会話は巻き戻していません: " + v.skipped : v.msg + "（次の入力では設定と会話を全部送り直します）",
      v.skipped ? 10000 : 6000);
  } catch (e) { toast("アプリの発言は変えましたが、Antigravity の会話は巻き戻せませんでした:\n" + e.message, 15000); }
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
  const ch = sid;
  let r;
  try {
    r = await checked(m.role === "user" ? "/api/message/edit" : "/api/message/alter", { sid, id: m.id, text });
    $("editModal").classList.add("hidden");
    editTarget = null;
    await poll();
  } catch (e) { alert(e.message); return; }
  finally { button.disabled = false; }
  await revertIde(r, ch);
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
$("attachBtn").onclick = () => $("attachInput").click();
$("attachInput").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; };
$("messageInput").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});
for (const el of [document.querySelector("footer"), log]) {
  el.addEventListener("dragover", (e) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    e.preventDefault();
    document.querySelector("footer").classList.add("drag");
  });
  el.addEventListener("dragleave", () => document.querySelector("footer").classList.remove("drag"));
  el.addEventListener("drop", (e) => {
    document.querySelector("footer").classList.remove("drag");
    if (!e.dataTransfer.files.length) return;
    e.preventDefault();
    addFiles([...e.dataTransfer.files]);
  });
}
$("settingsToggle").onclick = () => {
  if (!$("settingsPanel").classList.toggle("hidden")) { loadOptions(); loadIdeSessions(); }
};
$("sessionFilter").oninput = renderSessions;
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
$("sideBackdrop").onclick = () => document.body.classList.remove("side-open");
$("rpToggle").onclick = () => { rpMode = !rpMode; localStorage.setItem("rpMode", rpMode ? "1" : "0"); render(); };
$("settingSave").onclick = async () => {
  // 念のため：フォームの中身が別チャンネルのものなら保存しない
  if (!state || formSid !== sid || state.id !== sid) { flash("設定の読み込みが追いついていません。少し待ってからもう一度保存してください"); poll(); return; }
  try {
    const r = await post("/api/setting", { setting: readForm() });
    formDirty = false;
    flash(r.agents && !r.agents.ok ? "保存しました。ただし AGENTS.md: " + r.agents.msg : "保存しました");
    loadAgents();
    await poll();
    refreshSessions();
  } catch (e) { flash("画像設定のJSONエラー: " + e.message); }
};
// 新規: 今開いているチャンネルの設定（保存前の変更も含む）を引き継いで始める。メモ・会話・担当IDEは持ち越さない
$("newSession").onclick = async () => {
  let base = null;
  if (state && state.id === sid) {
    try { base = formSid === sid ? readForm() : JSON.parse(JSON.stringify(state.setting)); }
    catch { base = JSON.parse(JSON.stringify(state.setting)); }  // 画像設定の JSON が壊れているときは保存済みのものを使う
  }
  const title = prompt(base ? "チャンネル名（今の設定を引き継いで始めます）" : "チャンネル名", "新しいチャンネル");
  if (title === null) return;
  const r = await api("/api/session/new", base ? { title, setting: base } : { title });
  await refreshSessions();
  await switchTo(r.sid);
  if (!base) $("settingsPanel").classList.remove("hidden");
  else flash("今の設定で新しいチャンネルを始めました");
  if (meta.app?.new_channel_ide === false) return;
  // アプリ全体の設定でオンなら、Antigravity の新規会話も作って担当にする
  toast("Antigravity の新規会話を作っています…", 0);
  try {
    const n = await checked("/api/ide/new", { sid: r.sid });
    toast(`Antigravity の新規会話を作って担当にしました: ${n.title}（${String(n.ide).slice(0, 8)}）`);
    if (sid === r.sid) await poll();
    refreshSessions(); loadIdeSessions();
  } catch (e) { toast("チャンネルは作りましたが、Antigravity の新規会話は作れませんでした:\n" + e.message, 15000); }
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
  await loadOptions();
  loadModels();
  await refreshSessions();
  await poll();
  setInterval(poll, 1500);
  setInterval(pollProgress, 2000);
  setInterval(refreshSessions, 3000);
  setInterval(() => { if (!$("settingsPanel").classList.contains("hidden")) loadIdeSessions(); }, 5000);
})();
