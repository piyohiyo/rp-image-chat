#!/usr/bin/env node
// Read Antigravity IDE sessions via local DevTools; send turns with agy --conversation.
// Message delivery never types into or clicks the IDE chat composer.
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

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

function findAgy() {
  const candidates = [
    process.env.AGY_PATH,
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'agy', 'bin', 'agy.exe'),
  ].filter(Boolean);
  for (const candidate of candidates) if (fs.existsSync(candidate)) return candidate;
  const found = spawnSync('where.exe', ['agy.exe'], { encoding: 'utf8', windowsHide: true, timeout: 3000 });
  const fromPath = (found.stdout || '').split(/\r?\n/).map((s) => s.trim()).find(Boolean);
  if (fromPath) return fromPath;
  throw new Error('agy.exeが見つかりません。Antigravity CLIをインストールしてください');
}

function sendViaCli(sessionId, message) {
  if (!/^[0-9a-fA-F-]{36}$/.test(sessionId || '')) throw new Error('指定セッションIDが不正です');
  const args = [
    '--input-format', 'stream-json', '--output-format', 'stream-json',
    '--print-timeout', '15m', '--conversation', sessionId,
  ];
  const input = JSON.stringify({ event: 'user', message: { content: message } }) + '\n';
  const run = spawnSync(findAgy(), args, {
    input, encoding: 'utf8', windowsHide: true, timeout: 930000, maxBuffer: 16 * 1024 * 1024,
    env: { ...process.env, NO_COLOR: '1' },
  });
  if (run.error) throw new Error(run.error.message);
  const events = (run.stdout || '').split(/\r?\n/).flatMap((line) => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
  const final = [...events].reverse().find((event) => event.event === 'result')?.result;
  if (run.status !== 0 || !final || final.status !== 'SUCCESS') {
    throw new Error(final?.error || (run.stderr || '').trim().slice(-1800) || '指定したAntigravityセッションから応答がありません');
  }
  if (final.conversation_id && final.conversation_id.toLowerCase() !== sessionId.toLowerCase()) {
    throw new Error('指定したIDと異なるAntigravityセッションが返りました。誤送信防止のため応答を採用しません');
  }
  return { reply: String(final.response || '').trim(), conversationId: final.conversation_id || sessionId };
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

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8') || '{}');
  if (input.op === 'list') {
    const cdp = await connect();
    try {
      const sessions = await listSessions(cdp);
      process.stdout.write(JSON.stringify({ ok: true, sessions }));
    } finally { cdp.close(); }
    return;
  }
  if (input.op === 'handoff') {
    const file = path.resolve(input.file || '');
    if (!fs.existsSync(file)) throw new Error('引き継ぎファイルが見つかりません');
    const context = fs.readFileSync(file, 'utf8');
    const message = `以下はRP Chatからの引き継ぎデータです。設定・会話・RAGを文脈として読み込み、以後この会話を続けてください。\n\n${context}`;
    const result = sendViaCli(input.sessionId, message);
    process.stdout.write(JSON.stringify({ ok: true, ...result }));
    return;
  }
  if (input.op === 'send') {
    const result = sendViaCli(input.sessionId, input.message || '');
    process.stdout.write(JSON.stringify({ ok: true, ...result }));
    return;
  }
  throw new Error('unknown operation');
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({ ok: false, error: error.message || 'IDE連携に失敗しました' }));
  process.exitCode = 1;
});
