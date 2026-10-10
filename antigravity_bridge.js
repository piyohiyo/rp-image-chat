#!/usr/bin/env node
// Direct Antigravity IDE bridge using its local Chrome DevTools Protocol endpoint.
// No extension or Antigravity CLI is used.
const fs = require('node:fs');

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class CDP {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.nextId = 0;
    this.pending = new Map();
    this.ws.addEventListener('message', (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (!message.id) return;
      const entry = this.pending.get(message.id);
      if (!entry) return;
      this.pending.delete(message.id);
      clearTimeout(entry.timer);
      if (message.error) entry.reject(new Error(message.error.message || 'CDP error'));
      else entry.resolve(message.result || {});
    });
  }

  async open() {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('IDE接続がタイムアウトしました')), 5000);
      this.ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
      this.ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('IDEに接続できません')); }, { once: true });
    });
  }

  call(method, params = {}, timeout = 5000) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`IDE応答待ちタイムアウト: ${method}`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression, awaitPromise = false) {
    const result = await this.call('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise, userGesture: true,
    }, 10000);
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text || 'IDE画面の操作に失敗しました');
    }
    return result.result?.value;
  }

  close() { try { this.ws.close(); } catch {} }
}

async function connect() {
  const portFile = `${process.env.APPDATA}\\Antigravity\\DevToolsActivePort`;
  let port;
  try { port = fs.readFileSync(portFile, 'utf8').trim().split(/\r?\n/)[0]; }
  catch { throw new Error('Antigravity IDEの接続情報が見つかりません'); }
  if (!/^\d{2,5}$/.test(port)) throw new Error('Antigravity IDEの接続情報が不正です');
  const response = await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(3000) });
  if (!response.ok) throw new Error('Antigravity IDEに接続できません');
  const pages = await response.json();
  const page = pages.find((item) => item.type === 'page' && /Antigravity/i.test(item.title || ''));
  if (!page?.webSocketDebuggerUrl) throw new Error('操作できるAntigravity IDEの画面がありません');
  const cdp = new CDP(page.webSocketDebuggerUrl);
  await cdp.open();
  return cdp;
}

async function listSessions(cdp) {
  const sessions = new Map();
  const oldTop = await cdp.evaluate(`(() => { const e=[...document.querySelectorAll('div')].find(x=>x.classList.contains('overflow-y-auto')&&x.classList.contains('overscroll-none')&&x.scrollHeight>x.clientHeight); return e?.scrollTop ?? 0 })()`);
  for (let i = 0; i < 30; i++) {
    const rows = await cdp.evaluate(`Array.from(document.querySelectorAll('[data-cascade-id].cursor-pointer.select-none')).map(e=>({id:e.getAttribute('data-cascade-id'),title:(e.innerText||'').trim().split('\\n')[0],subtext:e.getAttribute('data-subtext')||'',selected:e.getAttribute('data-selected')==='true'})).filter(x=>x.id&&x.title)`);
    for (const row of rows || []) if (!sessions.has(row.id)) sessions.set(row.id, row);
    const moved = await cdp.evaluate(`(() => { const e=[...document.querySelectorAll('div')].find(x=>x.classList.contains('overflow-y-auto')&&x.classList.contains('overscroll-none')&&x.scrollHeight>x.clientHeight); if(!e)return false; const old=e.scrollTop; e.scrollTop=Math.min(e.scrollTop+Math.max(400,e.clientHeight*.8),e.scrollHeight); return e.scrollTop!==old })()`);
    if (!moved) break;
    await delay(180);
  }
  await cdp.evaluate(`(() => { const e=[...document.querySelectorAll('div')].find(x=>x.classList.contains('overflow-y-auto')&&x.classList.contains('overscroll-none')&&x.scrollHeight>x.clientHeight); if(e)e.scrollTop=${JSON.stringify(oldTop)} })()`);
  return [...sessions.values()];
}

async function selectSession(cdp, id) {
  const safeId = JSON.stringify(id);
  return cdp.evaluate(`(() => { const e=[...document.querySelectorAll('[data-cascade-id].cursor-pointer.select-none')].find(x=>x.getAttribute('data-cascade-id')===${safeId}); if(!e)return false; e.click(); return true })()`);
}

async function attachFile(cdp, filePath) {
  const path = require('node:path').resolve(filePath);
  if (!fs.existsSync(path)) throw new Error('引き継ぎファイルが見つかりません');
  const doc = await cdp.call('DOM.getDocument', { depth: -1, pierce: true });
  const found = await cdp.call('DOM.querySelector', { nodeId: doc.root.nodeId, selector: 'input[type="file"]' });
  if (!found.nodeId) throw new Error('IDEのファイル添付欄が見つかりません');
  await cdp.call('DOM.setFileInputFiles', { nodeId: found.nodeId, files: [path] });
  await delay(500);
}

async function sendText(cdp, text) {
  const composer = await cdp.evaluate(`(() => { const e=document.querySelector('[role="combobox"][aria-label="Message input"]'); if(!e)return false; e.focus(); return true })()`);
  if (!composer) throw new Error('IDEのチャット入力欄が見つかりません');
  await cdp.call('Input.insertText', { text });
  await delay(100);
  const sent = await cdp.evaluate(`(() => { const e=document.querySelector('button[aria-label="Send message"]'); if(!e||e.disabled)return false; e.click(); return true })()`);
  if (!sent) throw new Error('IDEの送信ボタンが使えません');
}

async function latestReply(cdp) {
  return cdp.evaluate(`(() => { const a=Array.from(document.querySelectorAll('[data-testid="commentable-content"]')).map(e=>(e.innerText||'').trim()).filter(Boolean); if(a.length)return a[a.length-1]; const b=Array.from(document.querySelectorAll('[data-testid="planner-response-text"]')).map(e=>(e.innerText||'').trim()).filter(Boolean); return b[b.length-1]||'' })()`);
}

async function waitReply(cdp, before, timeoutMs = 900000) {
  const started = Date.now();
  let candidate = '';
  let stableSince = 0;
  while (Date.now() - started < timeoutMs) {
    await delay(1200);
    const current = await latestReply(cdp);
    const composerReady = await cdp.evaluate(`(() => { const e=document.querySelector('[role="combobox"][aria-label="Message input"]'); const b=document.querySelector('button[aria-label="Send message"]'); return !!e && !!b && !b.disabled && !(e.innerText||'').trim() })()`);
    if (current && current !== before && composerReady) {
      if (candidate === current) {
        if (Date.now() - stableSince >= 2200) return current;
      } else {
        candidate = current;
        stableSince = Date.now();
      }
    } else {
      candidate = '';
      stableSince = 0;
    }
  }
  return '';
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
  const cdp = await connect();
  try {
    if (input.op === 'list') {
      const sessions = await listSessions(cdp);
      process.stdout.write(JSON.stringify({ ok: true, sessions }));
      return;
    }
    if (input.op === 'select') {
      const selected = await selectSession(cdp, input.sessionId);
      if (!selected) throw new Error('指定したIDEセッションが一覧にありません。更新してください');
      await delay(350);
      process.stdout.write(JSON.stringify({ ok: true }));
      return;
    }
    if (input.op === 'handoff') {
      if (!await selectSession(cdp, input.sessionId)) throw new Error('指定したIDEセッションが一覧にありません。更新してください');
      await delay(350);
      await attachFile(cdp, input.file);
      await sendText(cdp, '添付した引き継ぎファイルを読んで、そこに記載された設定・会話履歴を踏まえてチャットを続けてください。');
      await delay(350);
      if (input.message) await sendText(cdp, input.message);
      process.stdout.write(JSON.stringify({ ok: true }));
      return;
    }
    if (input.op === 'send') {
      if (!await selectSession(cdp, input.sessionId)) throw new Error('指定したIDEセッションが一覧にありません。更新してください');
      await delay(200);
      const before = await latestReply(cdp);
      await sendText(cdp, input.message || '');
      const reply = await waitReply(cdp, before, input.timeoutMs || 900000);
      process.stdout.write(JSON.stringify({ ok: true, reply }));
      return;
    }
    throw new Error('unknown operation');
  } finally { cdp.close(); }
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({ ok: false, error: error.message || 'IDE連携に失敗しました' }));
  process.exitCode = 1;
});
