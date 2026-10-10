"""
RP Image Chat - server (multi-session)
  - public/ を配信
  - data/sessions/<sid>.json を各チャンネルの状態として管理
  - outbox/*.json（AIの応答）を監視して該当セッションに取り込み
  - 応答に付いた画像プロンプトを ComfyUI に投げ、完成画像をセッションに反映
標準ライブラリのみ。  python server.py
"""
import json, os, threading, time, uuid, random, shutil, urllib.request, mimetypes, sys, copy, socket, re, subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data")
SESS_DIR = os.path.join(DATA, "sessions")
RAG_DIR = os.path.join(DATA, "rag")
LEGACY = os.path.join(DATA, "session.json")
OUTBOX = os.path.join(ROOT, "outbox")
DONE = os.path.join(OUTBOX, "done")
HANDOFF_DIR = os.path.join(DATA, "handoffs")
BRIDGE_SCRIPT = os.path.join(ROOT, "antigravity_bridge.js")
COMFY = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_OUT = os.path.normpath(os.environ.get(
    "COMFYUI_OUTPUT_DIR", os.path.join(ROOT, "..", "ComfyUI", "output")))
PORT = int(os.environ.get("RPCHAT_PORT", "8199"))
HOST = os.environ.get("RPCHAT_HOST", "127.0.0.1")


def bind_host():
    if HOST != "lan":
        return HOST
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]


lock = threading.RLock()
bridge_lock = threading.Lock()

DEFAULT_SETTING = {
    "title": "新しいチャンネル",
    "summary": "",        # 概要：このチャンネルがどんな話か（ユーザーが書く）
    "instructions": "",   # 指示：AIへの書き方・方針の指示（ユーザーが書く）
    "memo": "",           # AIメモ：進行状況・あらすじ（AIが応答ごとに更新、手で直してもOK）
    "user_name": "あなた",
    "character": {"name": "", "age": "", "profile": "", "speech": ""},
    "scenario": "",
    "first_message": "",
    "rag_collections": [],
    "antigravity_session_id": "",
    "antigravity_status": "",
    "antigravity_error": "",
    "image": {
        "checkpoint": "waiIllustriousSDXL_v170.safetensors",
        "width": 896, "height": 1152, "steps": 30, "cfg": 7.0,
        "sampler": "euler_ancestral", "scheduler": "normal",
        "base_positive": "(masterpiece, best quality, ultra-detailed, highres:1.2), anime style, cel shading",
        "character_tags": "1girl",
        "base_negative": "(worst quality, low quality:1.4), bad anatomy, bad proportions, missing limbs, extra limbs, bad hands, mutated hands, missing fingers, text, watermark, signature, multiple girls, ",
        "ipadapter_image": "",
    },
}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id():
    return uuid.uuid4().hex[:10]


def merge_defaults(setting):
    if setting is not None and not isinstance(setting, dict):
        raise ValueError("setting must be an object")
    out = copy.deepcopy(DEFAULT_SETTING)
    for k, v in (setting or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- storage
def spath(sid):
    if not sid or not all(c.isalnum() or c in "-_" for c in sid):
        raise ValueError("bad sid")
    return os.path.join(SESS_DIR, sid + ".json")


def list_ids():
    return [n[:-5] for n in os.listdir(SESS_DIR) if n.endswith(".json")]


def load(sid):
    with lock:
        with open(spath(sid), encoding="utf-8-sig") as f:
            s = json.load(f)
        s["setting"] = merge_defaults(s.get("setting"))
        return s


def save(s):
    with lock:
        s["rev"] = int(s.get("rev", 0)) + 1
        s["updated"] = now()
        p = spath(s["id"])
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        for _ in range(10):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                time.sleep(0.1)
        raise


def create_session(setting=None):
    with lock:
        sid = new_id()
        st = merge_defaults(setting)
        s = {"id": sid, "rev": 0, "created": now(), "updated": now(), "setting": st, "messages": []}
        if st.get("first_message"):
            s["messages"].append({"id": new_id(), "role": "assistant", "text": st["first_message"], "ts": now()})
        save(s)
        return s


def find_msg(s, mid):
    return next((m for m in s["messages"] if m["id"] == mid), None)


def session_of_msg(mid):
    for sid in list_ids():
        s = load(sid)
        if find_msg(s, mid):
            return sid
    return None


def rag_path(cid):
    if not isinstance(cid, str) or not cid or not all(c.isalnum() or c in "-_" for c in cid):
        raise ValueError("bad collection id")
    return os.path.join(RAG_DIR, cid + ".json")


def load_rag(cid):
    with open(rag_path(cid), encoding="utf-8") as f:
        return json.load(f)


def save_rag(col):
    path = rag_path(col["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(col, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def rag_summary():
    result = []
    for name in sorted(os.listdir(RAG_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            col = load_rag(name[:-5])
            result.append({"id": col["id"], "name": col["name"], "entryCount": len(col.get("entries", []))})
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return result


def rag_context(setting):
    parts = []
    for cid in setting.get("rag_collections", []):
        try:
            col = load_rag(cid)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if col.get("entries"):
            parts.append("【" + col.get("name", "") + "】")
            parts.extend("## " + e.get("title", "") + "\n" + e.get("content", "") for e in col["entries"])
    return "\n\n".join(parts)


def antigravity_call(payload, timeout=900):
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.jsが見つかりません。Antigravity連携にNode.js 20以降が必要です")
    result = subprocess.run(
        [node, BRIDGE_SCRIPT], input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        cwd=ROOT, check=False,
    )
    try:
        answer = json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError):
        raise RuntimeError("Antigravity IDEから応答を取得できません")
    if result.returncode or not answer.get("ok"):
        raise RuntimeError(answer.get("error") or "Antigravity連携に失敗しました")
    return answer


def handoff_context(s):
    setting = s.get("setting", {})
    rag = []
    for cid in setting.get("rag_collections", []):
        try:
            rag.append(load_rag(cid))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    snapshot = {
        "rpchat_channel": {"id": s["id"], "title": setting.get("title", "")},
        "settings": setting,
        "rag_collections": rag,
        "conversation": [
            {key: m.get(key) for key in ("role", "text", "ts", "edited", "image") if key in m}
            for m in s.get("messages", [])
        ],
    }
    return "# RP Chat セッション引き継ぎ\n\n次のJSONに、このチャンネルの設定、RAG資料、これまでの会話を記録しています。内容を会話の継続用コンテキストとして扱ってください。\n\n```json\n" + json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n```\n"


def write_handoff(s):
    os.makedirs(HANDOFF_DIR, exist_ok=True)
    name = f"{s['id']}_{int(time.time())}_{new_id()}.md"
    path = os.path.join(HANDOFF_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(handoff_context(s))
    return path


def begin_handoff(sid, target_id):
    try:
        with bridge_lock:
            with lock:
                current = load(sid)
                if current["setting"].get("antigravity_session_id") != target_id:
                    return
                path = write_handoff(current)
            antigravity_call({"op": "handoff", "sessionId": target_id, "file": path}, timeout=930)
            error = ""
    except Exception as e:
        error = str(e)
    with lock:
        try:
            current = load(sid)
        except (FileNotFoundError, ValueError):
            return
        if current["setting"].get("antigravity_session_id") != target_id:
            return
        current["setting"]["antigravity_status"] = "error" if error else "ready"
        current["setting"]["antigravity_error"] = error
        save(current)


def deliver_to_antigravity(sid, mid, target_id, text):
    error = ""
    reply = ""
    try:
        with bridge_lock:
            result = antigravity_call({"op": "send", "sessionId": target_id, "message": text}, timeout=930)
            reply = result.get("reply", "").strip()
    except Exception as e:
        error = str(e)
    with lock:
        try:
            s = load(sid)
        except (FileNotFoundError, ValueError):
            return
        m = find_msg(s, mid)
        if not m:
            return
        if error:
            m["status"] = "error"
            m["error"] = error
            s["setting"]["antigravity_status"] = "error"
            s["setting"]["antigravity_error"] = error
        else:
            m["status"] = "answered" if reply else "sent"
            if reply:
                s["messages"].append({"id": new_id(), "role": "assistant", "text": reply, "ts": now(), "source": "antigravity"})
            s["setting"]["antigravity_status"] = "ready" if reply else "sent"
            s["setting"]["antigravity_error"] = ""
        save(s)


# ---------------------------------------------------------------- outbox
def process_outbox():
    os.makedirs(DONE, exist_ok=True)
    for name in sorted(os.listdir(OUTBOX)):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(OUTBOX, name)
        try:
            with open(path, encoding="utf-8-sig") as f:
                rep = json.load(f)
            if not isinstance(rep, dict) or not isinstance(rep.get("text", ""), str) or (rep.get("image") is not None and not isinstance(rep["image"], dict)):
                raise ValueError("reply must be a JSON object with text and optional image object")
            if (rep.get("session") is not None and not isinstance(rep["session"], str)) or (rep.get("reply_to") is not None and not isinstance(rep["reply_to"], str)):
                raise ValueError("session and reply_to must be strings")
            image = rep.get("image") or {}
            if any(key in image and image[key] is not None and not isinstance(image[key], str) for key in ("prompt", "negative")):
                raise ValueError("image prompts must be strings")
        except Exception as e:
            if time.time() - os.path.getmtime(path) > 10:
                print(f"[outbox] invalid json {name}: {e}")
                shutil.move(path, os.path.join(DONE, name + ".bad"))
            continue

        with lock:
            sid = rep.get("session") or (rep.get("reply_to") and session_of_msg(rep["reply_to"]))
            if not sid or not os.path.exists(spath(sid)):
                print(f"[outbox] no session for {name}")
                shutil.move(path, os.path.join(DONE, name + ".nosession"))
                continue
            s = load(sid)
            reply_to = rep.get("reply_to")
            if reply_to and not any(m["id"] == reply_to and m["role"] == "user" and m.get("status") in ("pending", "processing") for m in s["messages"]):
                print(f"[outbox] stale reply {name}")
                shutil.move(path, os.path.join(DONE, name + ".stale"))
                continue
            for m in s["messages"]:
                if m["role"] == "user" and m.get("status") in ("pending", "processing") and (not reply_to or m["id"] == reply_to):
                    m["status"] = "answered"
            msg = {"id": new_id(), "role": "assistant", "text": rep.get("text", ""), "ts": now()}
            img = rep.get("image")
            if img and img.get("prompt"):
                msg["image"] = {
                    "prompt": img["prompt"], "negative": img.get("negative"),
                    "width": img.get("width"), "height": img.get("height"),
                    "raw": bool(img.get("raw", False)), "seed": img.get("seed"), "status": "queued",
                }
            if msg["text"] or msg.get("image"):
                s["messages"].append(msg)
            if isinstance(rep.get("memo"), str) and rep["memo"].strip():
                s["setting"]["memo"] = rep["memo"].strip()
            save(s)
        shutil.move(path, os.path.join(DONE, f"{int(time.time())}_{name}"))
        print(f"[outbox] imported {name} -> {sid}")


# ---------------------------------------------------------------- comfy
def build_prompt(setting, img):
    ic = setting.get("image", {})
    if img.get("raw"):
        pos = img["prompt"]
    else:
        pos = ", ".join(p.strip(" ,") for p in [ic.get("base_positive", ""), ic.get("character_tags", ""), img["prompt"]] if p and p.strip())
    neg = img.get("negative") or ic.get("base_negative", "")
    return pos, neg


def build_workflow(setting, img, prefix):
    ic = setting.get("image", {})
    pos, neg = build_prompt(setting, img)
    wf = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ic.get("checkpoint", "waiIllustriousSDXL_v170.safetensors")}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": int(img.get("width") or ic.get("width", 896)), "height": int(img.get("height") or ic.get("height", 1152)), "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": int(img["seed"]), "steps": int(ic.get("steps", 30)), "cfg": float(ic.get("cfg", 7.0)),
            "sampler_name": ic.get("sampler", "euler_ancestral"), "scheduler": ic.get("scheduler", "normal"),
            "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["8", 0]}},
    }
    if ic.get("ipadapter_image"):
        wf["10"] = {"class_type": "LoadImage", "inputs": {"image": ic["ipadapter_image"]}}
        wf["12"] = {"class_type": "IPAdapterUnifiedLoader", "inputs": {"model": ["4", 0], "preset": "PLUS FACE (portraits)"}}
        wf["13"] = {"class_type": "IPAdapter", "inputs": {
            "model": ["12", 0], "ipadapter": ["12", 1], "image": ["10", 0],
            "weight": float(ic.get("ipadapter_weight", 0.85)), "start_at": 0.0, "end_at": 1.0, "weight_type": "standard"}}
        wf["3"]["inputs"]["model"] = ["13", 0]
    return wf, pos, neg


def comfy_run(workflow, timeout=900):
    payload = json.dumps({"prompt": workflow, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(f"{COMFY}/prompt", data=payload, headers={"Content-Type": "application/json"})
    pid = json.loads(urllib.request.urlopen(req, timeout=15).read())["prompt_id"]
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(1.5)
        try:
            h = json.loads(urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=10).read())
        except Exception:
            continue
        if pid not in h:
            continue
        entry = h[pid]
        st = entry.get("status", {})
        if st.get("status_str") == "error":
            for kind, info in st.get("messages", []):
                if kind == "execution_error":
                    raise RuntimeError(info.get("exception_message", "ComfyUI error").strip())
            raise RuntimeError("ComfyUI error")
        for out in entry.get("outputs", {}).values():
            for im in out.get("images", []):
                sub = im.get("subfolder", "")
                return (sub + "/" if sub else "") + im["filename"]
        if st.get("completed"):
            raise RuntimeError("no image output")
    raise RuntimeError("timeout")


def process_images():
    with lock:
        target = None
        for sid in sorted(list_ids()):
            s = load(sid)
            for m in s["messages"]:
                im = m.get("image")
                if im and im.get("status") == "queued":
                    target = (s, m)
                    break
            if target:
                break
        if not target:
            return
        s, m = target
        im = m["image"]
        if not im.get("seed"):
            im["seed"] = random.randint(0, 2**32 - 1)
        im["status"] = "generating"
        im.pop("error", None)
        try:
            wf, pos, neg = build_workflow(s["setting"], im, f"rpchat/{s['id']}/rp_{m['id']}")
        except (KeyError, TypeError, ValueError) as e:
            im["status"] = "error"
            im["error"] = f"invalid image settings: {e}"
            save(s)
            return
        im["final_positive"], im["final_negative"] = pos, neg
        sid, mid, seed = s["id"], m["id"], im["seed"]
        save(s)

    print(f"[comfy] generating {sid}/{mid} seed={seed}")
    try:
        rel, err = comfy_run(wf), None
    except Exception as e:
        rel, err = None, str(e)
        print(f"[comfy] error {sid}/{mid}: {err}")

    with lock:
        if not os.path.exists(spath(sid)):
            return
        s = load(sid)
        m = find_msg(s, mid)
        if m and m.get("image"):
            if rel:
                m["image"]["file"] = rel
                m["image"]["status"] = "done"
                m["image"].setdefault("history", []).append({"file": rel, "seed": m["image"]["seed"]})
            else:
                m["image"]["status"] = "error"
                m["image"]["error"] = err
            save(s)


def worker():
    while True:
        try:
            process_outbox()
            process_images()
        except Exception as e:
            print(f"[worker] {e}")
        time.sleep(1)


def summary_of(s):
    st = s["setting"]
    msgs = s["messages"]
    return {
        "id": s["id"], "title": st.get("title"), "summary": st.get("summary", ""),
        "rev": s.get("rev"), "updated": s.get("updated"), "created": s.get("created"),
        "count": len(msgs),
        "waiting": any(m["role"] == "user" and m.get("status") in ("pending", "processing") for m in msgs),
        "generating": any((m.get("image") or {}).get("status") in ("queued", "generating") for m in msgs),
        "last": (msgs[-1]["text"][:60] if msgs else ""),
    }


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc == self.headers.get("Host")

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def send_file(self, path, ctype=None):
        if not os.path.isfile(path):
            return self.send_error(404)
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 4 * 1024 * 1024:
            raise ValueError("request body too large")
        value = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p == "/api/antigravity/sessions":
                return self.send_json(antigravity_call({"op": "list"}, timeout=30))
            if p == "/api/sessions":
                with lock:
                    items = [summary_of(load(sid)) for sid in list_ids()]
                items.sort(key=lambda x: x.get("updated") or "", reverse=True)
                return self.send_json({"sessions": items})
            if p == "/api/session":
                return self.send_json(load(q.get("sid", [""])[0]))
            if p == "/api/pending":
                best = None
                with lock:
                    for sid in list_ids():
                        s = load(sid)
                        for m in s["messages"]:
                            if m["role"] == "user" and m.get("status") == "pending":
                                if not best or m["ts"] < best[1]["ts"]:
                                    best = (sid, m)
                                break
                return self.send_json({"session": best[0] if best else None, "pending": best[1] if best else None})
            if p == "/api/rag":
                with lock:
                    return self.send_json({"collections": rag_summary()})
            if p == "/api/rag/collection":
                with lock:
                    return self.send_json({"collection": load_rag(q.get("id", [""])[0])})
        except (FileNotFoundError, ValueError):
            return self.send_json({"error": "no such session"}, 404)
        if p == "/img":
            f = q.get("f", [""])[0]
            full = os.path.normpath(os.path.join(COMFY_OUT, f))
            if not full.startswith(COMFY_OUT + os.sep):
                return self.send_error(403)
            return self.send_file(full)
        if p == "/":
            p = "/index.html"
        full = os.path.normpath(os.path.join(PUBLIC, p.lstrip("/")))
        try:
            inside_public = os.path.commonpath((PUBLIC, full)) == PUBLIC
        except ValueError:
            inside_public = False
        if not inside_public:
            return self.send_error(403)
        return self.send_file(full)

    def do_POST(self):
        p = urlparse(self.path).path
        if not self.same_origin():
            return self.send_json({"error": "wrong origin"}, 403)
        try:
            d = self.body()
        except (ValueError, UnicodeDecodeError):
            return self.send_json({"error": "invalid request body"}, 400)
        with lock:
            if p == "/api/rag/create":
                name = d.get("name", "")
                if not isinstance(name, str) or not name.strip():
                    return self.send_json({"error": "name is required"}, 400)
                col = {"id": new_id(), "name": name.strip(), "entries": []}
                save_rag(col)
                return self.send_json({"ok": True, "collection": col})
            if p in ("/api/rag/delete", "/api/rag/entry/save", "/api/rag/entry/delete"):
                try:
                    col = load_rag(d.get("id"))
                except (OSError, ValueError, json.JSONDecodeError):
                    return self.send_json({"error": "no such collection"}, 404)
                if p == "/api/rag/delete":
                    os.remove(rag_path(col["id"]))
                    for sid in list_ids():
                        session = load(sid)
                        selected = session["setting"].get("rag_collections", [])
                        if col["id"] in selected:
                            session["setting"]["rag_collections"] = [x for x in selected if x != col["id"]]
                            save(session)
                    return self.send_json({"ok": True})
                entries = col.setdefault("entries", [])
                if p == "/api/rag/entry/save":
                    title, content = d.get("title"), d.get("content")
                    if not isinstance(title, str) or not title.strip() or not isinstance(content, str) or not content.strip():
                        return self.send_json({"error": "title and content are required"}, 400)
                    entry_id = d.get("entry_id")
                    if entry_id:
                        entry = next((e for e in entries if e.get("id") == entry_id), None)
                        if entry is None:
                            return self.send_json({"error": "no such entry"}, 404)
                        entry.update(title=title.strip(), content=content.strip())
                    else:
                        entry = {"id": new_id(), "title": title.strip(), "content": content.strip()}
                        entries.append(entry)
                    save_rag(col)
                    return self.send_json({"ok": True, "entry": entry})
                entry_id = d.get("entry_id")
                remaining = [e for e in entries if e.get("id") != entry_id]
                if len(remaining) == len(entries):
                    return self.send_json({"error": "no such entry"}, 404)
                col["entries"] = remaining
                save_rag(col)
                return self.send_json({"ok": True})
            if p == "/api/session/new":
                base = d.get("setting")
                if d.get("copy_from"):
                    try:
                        base = load(d["copy_from"])["setting"]
                        base = dict(base, title=base.get("title", "") + " (コピー)", memo="")
                    except Exception:
                        pass
                if d.get("title"):
                    base = dict(base or {}, title=d["title"])
                s = create_session(base)
                return self.send_json({"ok": True, "sid": s["id"]})
            try:
                s = load(d.get("sid", ""))
            except (FileNotFoundError, ValueError):
                return self.send_json({"error": "no such session"}, 404)

            if p == "/api/session/delete":
                trash = os.path.join(DATA, "trash")
                os.makedirs(trash, exist_ok=True)
                shutil.move(spath(s["id"]), os.path.join(trash, f"{s['id']}_{int(time.time())}.json"))
                return self.send_json({"ok": True})
            if p == "/api/send":
                text = (d.get("text") or "").strip()
                if not text:
                    return self.send_json({"error": "empty"}, 400)
                target_id = s["setting"].get("antigravity_session_id")
                m = {"id": new_id(), "role": "user", "text": text, "ts": now(), "status": "processing" if target_id else "pending"}
                s["messages"].append(m)
                save(s)
                if target_id:
                    threading.Thread(target=deliver_to_antigravity, args=(s["id"], m["id"], target_id, text), daemon=True).start()
                return self.send_json({"ok": True, "id": m["id"]})
            if p == "/api/message/alter":
                m = find_msg(s, d.get("id"))
                text = d.get("text")
                if not m or m.get("role") != "assistant":
                    return self.send_json({"error": "no such assistant message"}, 404)
                if not isinstance(text, str) or not text.strip():
                    return self.send_json({"error": "text is required"}, 400)
                m.setdefault("original_text", m["text"])
                m["text"] = text.strip()
                m["edited"] = True
                save(s)
                return self.send_json({"ok": True})
            if p in ("/api/message/edit", "/api/message/regenerate"):
                idx = next((i for i, m in enumerate(s["messages"]) if m["id"] == d.get("id")), -1)
                if idx < 0:
                    return self.send_json({"error": "no such message"}, 404)
                if p == "/api/message/edit":
                    old = s["messages"][idx]
                    text = d.get("text")
                    if old["role"] != "user":
                        return self.send_json({"error": "not a user message"}, 400)
                    if not isinstance(text, str) or not text.strip():
                        return self.send_json({"error": "text is required"}, 400)
                    new_user = {"id": new_id(), "role": "user", "text": text.strip(), "ts": now(), "status": "pending"}
                    s["messages"] = s["messages"][:idx] + [new_user]
                else:
                    if s["messages"][idx]["role"] != "assistant":
                        return self.send_json({"error": "not an assistant message"}, 400)
                    user_idx = next((i for i in range(idx - 1, -1, -1) if s["messages"][i]["role"] == "user"), -1)
                    if user_idx < 0:
                        return self.send_json({"error": "no preceding user message"}, 400)
                    s["messages"] = s["messages"][:user_idx + 1]
                    new_user = s["messages"][-1]
                    new_user["id"] = new_id()
                    new_user["status"] = "pending"
                    new_user["ts"] = now()
                save(s)
                return self.send_json({"ok": True, "id": new_user["id"]})
            if p == "/api/rag/select":
                ids = d.get("ids")
                if not isinstance(ids, list) or any(not isinstance(cid, str) for cid in ids):
                    return self.send_json({"error": "ids must be a list"}, 400)
                known = {c["id"] for c in rag_summary()}
                if any(cid not in known for cid in ids):
                    return self.send_json({"error": "unknown collection"}, 400)
                s["setting"]["rag_collections"] = list(dict.fromkeys(ids))
                save(s)
                return self.send_json({"ok": True})
            if p == "/api/claim":
                m = find_msg(s, d.get("id"))
                if m and m.get("status") == "pending":
                    m["status"] = "processing"
                    save(s)
                return self.send_json({"ok": True})
            if p == "/api/regen":
                m = find_msg(s, d.get("id"))
                if m and m.get("image"):
                    m["image"]["seed"] = d.get("seed") or random.randint(0, 2**32 - 1)
                    if d.get("prompt"):
                        m["image"]["prompt"] = d["prompt"]
                    m["image"]["status"] = "queued"
                    save(s)
                return self.send_json({"ok": True})
            if p == "/api/pick":
                m = find_msg(s, d.get("id"))
                if m and m.get("image"):
                    m["image"]["file"] = d.get("file")
                    save(s)
                return self.send_json({"ok": True})
            if p == "/api/delete":
                s["messages"] = [m for m in s["messages"] if m["id"] != d.get("id")]
                save(s)
                return self.send_json({"ok": True})
            if p == "/api/setting":
                s["setting"] = merge_defaults(d.get("setting", s["setting"]))
                save(s)
                return self.send_json({"ok": True})
            if p == "/api/antigravity/target":
                target_id = d.get("sessionId", "")
                if not isinstance(target_id, str) or (target_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", target_id)):
                    return self.send_json({"error": "IDEセッションIDが不正です"}, 400)
                previous_id = s["setting"].get("antigravity_session_id", "")
                if target_id == previous_id:
                    return self.send_json({"ok": True, "changed": False})
                s["setting"]["antigravity_session_id"] = target_id
                s["setting"]["antigravity_status"] = "switching" if target_id else "disconnected"
                s["setting"]["antigravity_error"] = ""
                save(s)
                if target_id:
                    threading.Thread(target=begin_handoff, args=(s["id"], target_id), daemon=True).start()
                return self.send_json({"ok": True, "changed": True})
            if p == "/api/reset":
                s["messages"] = []
                s["setting"]["memo"] = ""
                fm = s["setting"].get("first_message")
                if fm:
                    s["messages"].append({"id": new_id(), "role": "assistant", "text": fm, "ts": now()})
                save(s)
                return self.send_json({"ok": True})
        self.send_error(404)


def main():
    os.makedirs(SESS_DIR, exist_ok=True)
    os.makedirs(RAG_DIR, exist_ok=True)
    os.makedirs(DONE, exist_ok=True)
    # 旧形式の data/session.json は取り込まずに退避だけする
    if os.path.exists(LEGACY):
        shutil.move(LEGACY, os.path.join(DATA, f"session_legacy_{int(time.time())}.json"))
        print("[init] 旧 session.json を退避しました")
    if not list_ids():
        create_session({"title": "チャンネル1"})
    threading.Thread(target=worker, daemon=True).start()
    host = bind_host()
    print(f"RP Image Chat: http://{host}:{PORT}/")
    ThreadingHTTPServer((host, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
