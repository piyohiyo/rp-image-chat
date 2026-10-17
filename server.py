"""
RP Image Chat - server (multi-session)
  - public/ を配信
  - data/sessions/<sid>.json を各チャンネルの状態として管理
  - wait_input.py が入力を受け取ったら、設定・会話・発言をそのまま表示する
    （担当IDEが前回と同じなら、増えた発言と変わった設定だけ。担当が変わったら全部）
  - outbox/*.json（AIの応答）を監視して該当セッションに取り込み
  - 応答に付いた画像プロンプトを ComfyUI に投げ、完成画像をセッションに反映
標準ライブラリのみ。  python server.py
"""
import json, os, threading, time, uuid, random, shutil, urllib.request, mimetypes, sys, copy, socket, sqlite3, datetime, hashlib, re, base64, binascii
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
import qr  # スマホで開くアドレスの QR コード
try:
    import ide_poke  # 担当の Antigravity 会話に合図を送る（Windows のみ）
except (ImportError, OSError, AttributeError):
    ide_poke = None

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data")
SESS_DIR = os.path.join(DATA, "sessions")
RAG_DIR = os.path.join(DATA, "rag")
TRASH = os.path.join(DATA, "trash")
APP_SETTINGS = os.path.join(DATA, "settings.json")
PRESETS = os.path.join(DATA, "file-presets.json")  # 「読み込むファイル」の選択のプリセット
CHAR_PRESETS = os.path.join(DATA, "char-presets.json")  # キャラ固定タグのプリセット（全チャンネル共通）
WRITING_STYLE = os.path.join(DATA, "writing_style.md")
LEGACY = os.path.join(DATA, "session.json")
SENT = os.path.join(DATA, "sent")  # チャンネルごとに、担当IDEに何を送ったかの記録（同じ担当なら差分だけ送るため）
UPLOADS = os.path.join(DATA, "uploads")  # 発言に添付したファイル（uploads/<sid>/<時刻>_<名前>）
UPLOAD_MAX = 40 * 1024 * 1024  # 1ファイルの上限（base64 で送るので BODY_MAX に収まるように）
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}  # これ以外はページに埋め込まずダウンロードさせる
OUTBOX = os.path.join(ROOT, "outbox")
DONE = os.path.join(OUTBOX, "done")
COMFY = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_OUT = os.path.normpath(os.environ.get(
    "COMFYUI_OUTPUT_DIR", os.path.join(ROOT, "..", "ComfyUI", "output")))
PORT = int(os.environ.get("RPCHAT_PORT", "8200"))
RUNNING = os.path.join(DATA, "server.json")  # 起動中のサーバーの番号（同じ data/ に2台目を立てないため）
HOST = os.environ.get("RPCHAT_HOST", "127.0.0.1")
# Antigravity IDE の会話一覧（読み取り専用で開く。IDE側には何も書き込まない）
IDE_DB = os.environ.get("ANTIGRAVITY_SUMMARIES_DB", os.path.join(
    os.path.expanduser("~"), ".gemini", "antigravity", "conversation_summaries.db"))
ide_waiting = {}  # IDE会話ID -> wait_input.py が最後に問い合わせてきた時刻
POKE_DELAY = 2  # 入力が来てから IDE に合図するまでの秒数
POKE_AGAIN = 30  # スペースの合図のあと、これだけたっても受け取られなければコマンドを送り直す
POKE_TEXT = os.environ.get("RPCHAT_POKE_TEXT", " ")  # 合図として会話に送る文字
# rpc: 会話IDを指定して Antigravity の言語サーバーに直接送る / window: IDE のウィンドウにキーを打つ（予備）
POKE_MODE = os.environ.get("RPCHAT_POKE_MODE", "rpc")
# チャンネルの作業フォルダ（Antigravity の会話を作る場所・AGENTS.md を置く場所）。
# 全チャンネルの既定はこのフォルダそのもの（チャンネルの設定で、この下の名前か絶対パスにも変えられる）
NEW_IDE_DIR = os.path.abspath(os.environ.get("RPCHAT_NEW_IDE_DIR", os.path.join(ROOT, "session")))
poke_log = {}  # IDE会話ID -> 最後の合図の結果 {"ts", "ok", "msg"}
claimed_at = {}  # 発言ID -> wait_input.py が受け取った時刻
# 発言を書き換えたチャンネルID -> {"ide", "msgs": 書き換える前の発言, "keep": 残す最後の発言の番号, "t"}。
# 画面が続けて /api/ide/revert を呼んで担当の会話を巻き戻す。それまで（最長 REVERT_HOLD 秒）はその会話に合図しない
revert_plan = {}
reverting = set()  # 巻き戻している最中の会話ID
REVERT_HOLD = 60
REVERT_BACKUP = os.path.join(DATA, "trash", "ide_revert")  # 巻き戻す前の会話データ（conversations/<ID>.db）の写し
poke_lock = threading.Lock()  # 合図は同時に1つだけ（window のときは前面のウィンドウを入れ替えるので）
# wait_input.py --ide で一度でも問い合わせてきた会話。まだの会話にはスペースではなく実行するコマンドを送る
IDE_KNOWN = os.path.join(DATA, "ide_known.json")
HISTORY_CHARS = 30000  # 会話を全部送るときの上限（新しい方から。それより前はAIメモで補う）
DEFAULT_APP = {
    # 自分の場所は画面のチャンネル設定の「設定ファイル」→「変更…」で変える（data/settings.json に保存される。全チャンネル共通）
    "system_root": os.path.join(ROOT, "systemfiles"),
    "file_folders": [],  # 「読み込むファイル」を選べるフォルダ（いくつでも。この中のファイルだけ読む）
    "new_channel_ide": True,  # 新しいチャンネルを作ったら Antigravity の新規会話も作って担当にする
}
FOLDER_EXT = (".md", ".txt")
FOLDER_MAX = 3000  # 1つのフォルダで一覧に出すファイルの上限（大きなフォルダを登録しても固まらないように）


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]


def bind_host():
    return lan_ip() if HOST == "lan" else HOST


# スマホで開くアドレスとその QR コード（LAN で待ち受けたときだけ。アプリ全体の設定に出す）
phone = {"url": None, "qr": None}


def setup_phone(host):
    if host in LOOPBACK:
        return
    if host in ("0.0.0.0", "::"):
        host = lan_ip()
    phone["url"] = f"http://{'[' + host + ']' if ':' in host else host}:{PORT}/"
    try:
        phone["qr"] = qr.svg(phone["url"])
    except ValueError:
        pass


# ---------------------------------------------------------------- フォルダを選ぶウィンドウ
pick_lock = threading.Lock()  # 一度に1つだけ出す


def from_this_pc(ip):
    """要求がこの PC から来たか（スマホから押すと、選ぶウィンドウが PC の画面に出てしまう）"""
    if ip in LOOPBACK or ip.startswith("127."):
        return True
    try:
        own = set(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        own = set()
    return ip in own or ip == lan_ip()


def pick_folder(initial=""):
    """PC の画面に「フォルダーの選択」ウィンドウを出して、選んだ場所を返す（やめたら ""）"""
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # ブラウザの後ろに隠れないように
    try:
        opt = {"initialdir": initial} if initial and os.path.isdir(initial) else {}
        path = filedialog.askdirectory(parent=root, title="フォルダを選ぶ", mustexist=True, **opt)
    finally:
        root.destroy()
    return os.path.normpath(path) if path else ""


# ---------------------------------------------------------------- access
# ほかのサイトがブラウザ経由でこのサーバーを操作しないように、Host と Origin を確かめる。
# LAN モードは同じネットワークの機器なら誰でも開けるので、信頼できるネットワークでだけ使う
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
BODY_MAX = 64 * 1024 * 1024  # 書き出したチャンネルの読み込みも通るように大きめ
access = {"hosts": set(LOOPBACK)}


def setup_access(host):
    """受け付ける Host 名（この PC の名前とアドレス、待ち受けるアドレス、RPCHAT_ALLOWED_HOSTS）を決める"""
    names = {socket.gethostname().lower()}
    try:
        names.update(a.lower() for a in socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    extra = {x.strip().lower() for x in os.environ.get("RPCHAT_ALLOWED_HOSTS", "").split(",") if x.strip()}
    access["hosts"] = set(LOOPBACK) | names | {host} | extra


def host_name(v):
    """Host ヘッダー（"name:port" や "[::1]:port"）から名前だけを取り出す"""
    v = (v or "").strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


lock = threading.RLock()

DEFAULT_SETTING = {
    "title": "新しいチャンネル",
    "summary": "",        # 概要：このチャンネルがどんな話か（ユーザーが書く）
    "instructions": "",   # 指示：AIへの書き方・方針の指示（ユーザーが書く）
    "memo": "",           # AIメモ：進行状況・あらすじ（AIが応答ごとに更新、手で直してもOK）
    "user_name": "",
    "character": {"name": "", "age": "", "profile": "", "speech": ""},
    "scenario": "",
    "first_message": "",
    "rag_collections": [],
    "project": "",        # サイドバーでの分類
    "system_file": "",    # "folder/file"（System として読む）
    "ref_files": [],      # ["folder/file", ...]（参照ファイルとして読む）
    "folder_files": [],   # 登録フォルダから選んだファイルの実パス（選んだ順に読む）
    "files_extra": "",    # 追加設定：選んだファイルの後に読ませる文
    "image_mode": "always",  # always / auto / off
    # 画像のキャラ固定タグ。AI がキャラの登場時に作り、画像に写るキャラの分だけ前に付ける
    # [{"id", "name", "tags", "locked"(AIに上書きさせない), "source": "ai"|"user"|"preset"}]
    "chars": [],
    "ide_session": "",    # 担当する Antigravity IDE の会話ID（空なら従来どおり誰でも受け取れる）
    "ide_title": "",
    "ide_auto": False,    # 初めての起動で作ったチャンネル1。Antigravity の新規会話をまだ付けていない（付けるまで ide_auto_worker が試す）
    "ide_model": "",     # 合図を送るときのモデル（Antigravity のモデルのキー。空なら会話の最後の入力と同じ）
    "ide_revert": True,   # 発言を編集・削除・作り直し・巻き戻ししたら、担当の会話もその前まで巻き戻す
    # 入力の渡し方。diff: 全部のときは履歴を30000字まで・ファイルの設定も中身を出す、続きは増えた分と変わった分
    # light: 全部のときは直近15件を全文（前はAIメモ）・ファイルの設定と長い会話は場所を出して読ませる、続きは今回の発言（と変わった設定）だけ
    "input_mode": "diff",
    "ide_folder": "",     # 作業フォルダ。NEW_IDE_DIR からの名前か絶対パス（空なら NEW_IDE_DIR そのもの）
    "agents_md": False,   # 作業フォルダに AGENTS.md（System ファイルと追加のルール）を書き出す
    "agents_extra": "",   # AGENTS.md に足すルール（画面から書く）
    "rules_every": False, # agents_extra を入力のたびに wait_input.py の表示の最後に付ける（AGENTS.md と違ってチャンネルごとに効く）
    "rp_state": False,    # キャラの状態（箇条書き）と内心を、AI に応答の state で毎回書かせ、次の入力で毎回見せる
    "image": {
        "checkpoint": "waiIllustriousSDXL_v170.safetensors",
        "width": 896, "height": 1152, "steps": 30, "cfg": 7.0,
        "sampler": "euler_ancestral", "scheduler": "normal",
        "base_positive": "(masterpiece, best quality, ultra-detailed, highres:1.2), anime style, cel shading",
        "character_tags": "",  # 毎回必ず付ける共通タグ（キャラごとの固定は chars）
        "base_negative": "(worst quality, low quality:1.4), bad anatomy, bad proportions, missing limbs, extra limbs, bad hands, mutated hands, missing fingers, text, watermark, signature, multiple girls, ",
        "ipadapter_image": "",
    },
}
# 別のチャンネルとして始めるときに持ち越さない、担当IDEの割り当て
NO_IDE = {"ide_session": "", "ide_title": "", "ide_model": "", "ide_auto": False}
NO_FOLDER = {"ide_folder": ""}  # 作業フォルダも持ち越さない（同じ AGENTS.md を2つのチャンネルで取り合わないように）


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id():
    return uuid.uuid4().hex[:10]


def merge_defaults(setting):
    out = copy.deepcopy(DEFAULT_SETTING)
    for k, v in (setting or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def read_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def app_settings():
    return dict(DEFAULT_APP, **read_json(APP_SETTINGS, {}))


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
        tmp = f"{p}.{os.getpid()}.tmp"  # サーバーが2つ動いていても一時ファイルがぶつからないように
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        for _ in range(10):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                time.sleep(0.1)
        raise


def backup(s, why):
    """消える操作の前に今の状態を data/trash に残しておく"""
    os.makedirs(TRASH, exist_ok=True)
    write_json(os.path.join(TRASH, f"{s['id']}_{int(time.time())}_{why}.json"), s)


def create_session(setting=None, messages=None):
    with lock:
        sid = new_id()
        st = merge_defaults(setting)
        s = {"id": sid, "rev": 0, "created": now(), "updated": now(), "setting": st, "messages": messages or []}
        if messages is None and st.get("first_message"):
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
    write_json(rag_path(col["id"]), col)


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


# ---------------------------------------------------------------- setting files
def safe_name(n):
    return isinstance(n, str) and n and n not in (".", "..") and "/" not in n and "\\" not in n


def system_files():
    root = app_settings()["system_root"]
    out = []
    if not os.path.isdir(root):
        return out
    for folder in sorted(os.listdir(root)):
        d = os.path.join(root, folder)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if os.path.isfile(os.path.join(d, n)) and n.lower().endswith((".md", ".txt")):
                out.append(f"{folder}/{n}")
    return out


def system_file_path(rel):
    parts = rel.split("/") if isinstance(rel, str) else []
    if len(parts) != 2 or not all(safe_name(p) for p in parts):
        raise ValueError(f"ファイル指定が不正です: {rel}")
    return os.path.join(app_settings()["system_root"], *parts)


def str_list(v):
    """文字列のリスト（空・文字列以外は落とす）"""
    return [x for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def file_folders():
    """「読み込むファイル」の欄で登録したフォルダ（全チャンネル共通。重複は1つに）"""
    out = []
    for f in str_list(app_settings().get("file_folders")):
        f = os.path.normpath(f.strip())
        if f not in out:
            out.append(f)
    return out


def collect_files(d, base, out):
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
    except OSError:
        return
    for e in entries:
        if len(out) >= FOLDER_MAX:
            return
        if e.name.startswith("."):
            continue
        if e.is_dir():
            collect_files(e.path, base, out)
        elif e.is_file() and e.name.lower().endswith(FOLDER_EXT):
            out.append({"label": os.path.relpath(e.path, base).replace("\\", "/"), "path": e.path})


def folder_options():
    """登録フォルダごとに、中のファイル（.md .txt、下のフォルダも）を返す"""
    res = []
    for f in file_folders():
        files = []
        if os.path.isdir(f):
            collect_files(f, f, files)
        res.append({"folder": f, "name": os.path.basename(f) or f, "ok": os.path.isdir(f),
                    "files": files, "more": len(files) >= FOLDER_MAX})
    return res


def folder_file_resolve(p):
    """選んだファイルの実パスと、表示用の名前（登録フォルダ名/フォルダ内の場所）。登録フォルダの外は読まない"""
    if not isinstance(p, str) or not p.strip():
        raise ValueError("ファイルの指定が空です")
    full = os.path.normpath(p.strip())
    for f in file_folders():
        try:
            if os.path.commonpath((f, full)) == f and full != f:
                return full, (os.path.basename(f) or f) + "/" + os.path.relpath(full, f).replace("\\", "/")
        except ValueError:  # ドライブが違う
            continue
    raise ValueError(f"登録したフォルダの外のファイルです: {p}")


def file_key(path):
    """設定の差分を見るときの名前。選ぶ順番や数が変わっても、同じファイルは同じ名前になるように"""
    return "file_" + hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest()[:10] + ".md"


def read_text(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read().strip()


def settings_parts(st, agents=None, files=None):
    """設定の各パーツを (ラベル, ファイル名, 内容) のリストで返す。
    agents: 担当の会話が読んでいる AGENTS.md の場所。そこに書いた System と追加のルールはここに出さない
    files: dict を渡すと、ファイルから読んだパーツの {ファイル名: 実際のパス} を入れる"""
    parts, missing = [], []
    files = {} if files is None else files
    if agents:
        if st.get("system_file"):
            parts.append((f"System（{st['system_file']}）", "system.md",
                          f"{agents} にルールとして書いてある（Antigravity が毎ターン読み込んでいる）。ここには出さない"))
    else:
        if st.get("system_file"):
            try:
                files["system.md"] = system_file_path(st["system_file"])
                parts.append((f"System（{st['system_file']}）", "system.md", read_text(files["system.md"])))
            except (OSError, ValueError):
                missing.append(st["system_file"])
        if st.get("agents_md") and (st.get("agents_extra") or "").strip() and not st.get("rules_every"):
            # AGENTS.md を読まない会話が担当のときは、ルールに書いた分もここで見せる（毎回最後に付けるなら、そちらで見せる）
            parts.append(("追加のルール", "agents_extra.md", st["agents_extra"].strip()))
    for i, rel in enumerate(st.get("ref_files") or []):
        try:
            files[f"ref_{i}.md"] = system_file_path(rel)
            parts.append((f"参照ファイル（{rel}）", f"ref_{i}.md", read_text(files[f"ref_{i}.md"])))
        except (OSError, ValueError):
            missing.append(rel)
    for p in str_list(st.get("folder_files")):
        try:
            path, label = folder_file_resolve(p)
            name = file_key(path)
            parts.append((f"読み込むファイル（{label}）", name, read_text(path)))
            files[name] = path
        except (OSError, ValueError):
            missing.append(p)
    if (st.get("files_extra") or "").strip():
        parts.append(("追加設定", "extra.md", st["files_extra"].strip()))
    for title, key, name in (("チャンネル概要", "summary", "summary.md"),
                              ("指示", "instructions", "instructions.md"),
                              ("舞台・状況", "scenario", "scenario.md")):
        if (st.get(key) or "").strip():
            parts.append((title, name, st[key].strip()))
    ch = st.get("character") or {}
    chara = "\n".join(f"{k}: {v}" for k, v in [
        ("名前", ch.get("name")), ("年齢", ch.get("age")),
        ("口調", ch.get("speech")), ("設定", ch.get("profile"))] if v)
    if chara:
        parts.append(("キャラクター", "chara.md", chara))
    if (st.get("user_name") or "").strip():
        parts.append(("ユーザーの呼び名", "username.md", st["user_name"].strip()))
    rag = rag_context(st)
    if rag:
        parts.append(("参照知識（RAG）", "rag.md", rag))
    if os.path.isfile(WRITING_STYLE):
        try:
            parts.append(("執筆スタイル", "writing_style.md", read_text(WRITING_STYLE)))
            files["writing_style.md"] = WRITING_STYLE
        except OSError:
            pass
    parts.append(("応答の書き方（outbox の JSON）", "output.md", output_text(st)))
    return parts, missing


def output_text(st):
    ic = st.get("image") or {}
    mode = st.get("image_mode", "always")
    lines = ["応答は wait_input.py が表示した outbox の場所に、UTF-8 の JSON ファイルで書く"
             "（ファイル名は <session>_<任意>.json。session と reply_to は表示された値をそのまま入れる）。",
             "```json",
             '{"session": "<session>", "reply_to": "<reply_to>", "text": "本文", "memo": "",'
             + ('' if mode == "off" else
                ' "image": {"prompt": "場面のタグ", "characters": ["キャラ名"]}, "characters": [{"name": "キャラ名", "tags": "外見タグ"}]')
             + ('' if not st.get("rp_state") else
                ' "state": [{"name": "キャラ名", "situation": ["今の状態"], "inner": "今の内心"}]')
             + "}",
             "```",
             "- text: ロールプレイの本文。ユーザーへの返事そのもの。メタな説明やファイル操作の話はしない。",
             "- memo: 物語の進行や重要な事実が変わった時だけ、これまでのあらすじ・現在の状況・重要事項を"
             "まとめ直した全文を書く。変化がなければ空文字にする（空なら前のメモがそのまま残る）。"]
    if st.get("rp_state"):
        lines += [
            "- state: 場面に出ているキャラの、この応答を書き終えた時点の状態。毎回入れる。"
            "入力の「キャラの状態」をもとに、この応答で変わったところを直して書く。",
            "  - situation: 外見・服装・姿勢・体の様子・居場所・持ち物など、今の状態を短い文の配列（箇条書き）で。",
            "  - inner: そのキャラの今の内心・本音（本文には出さない心の声）。",
            "  - 何も変わらなかったキャラは入れなくてよい（前の状態が残る）。退場して記録が要らなくなったキャラは"
            ' {"name": "キャラ名", "remove": true}。ユーザーが演じる人物は入れない。',
        ]
    if mode == "off":
        lines.append("- image と characters は付けない（このチャンネルは画像を描かない）。")
    else:
        common = (ic.get("character_tags") or "").strip()
        lines += [
            "- image.prompt: その応答の場面を描く画像生成用の Danbooru タグ（英語・カンマ区切り）。"
            "人数タグ（1girl, 2girls, 1boy など）・構図・表情・服装・ポーズ・場所・時間帯などを具体的に。"
            "品質タグと、固定キャラの外見タグ（下の characters で登録したもの）は自動で前に付くので書かない"
            + (f"（このほか毎回付く共通タグ: {common}）" if common else "") + "。"
            + ("毎回必ず付けること。" if mode == "always" else "場面が大きく変わった時や見せ場だけ付け、それ以外は image を省くこと。"),
            "- image.characters: この画像に写る固定キャラの name の配列（固定キャラ一覧か、今回の characters と同じ表記）。"
            "固定キャラが写らなければ空配列。",
            "- characters: 画像に描く人物が初めて登場したとき、または髪型・髪色など変わらない外見が変わったときだけ {name, tags} を入れる。"
            "name はキャラ名（以後ずっと同じ表記を使う）。tags はそのキャラの変わらない外見の Danbooru タグ"
            "（髪色・髪型・前髪・目の色・体型・肌の色・耳や角など種族の特徴・いつも付けている髪飾りなど。服装・表情・ポーズ・人数タグは入れない）。"
            "固定キャラ一覧にあって変化がなければ入れない。画像に描かない人物（視点人物のユーザーなど）は入れない。それ以外は空配列。",
            f"- 画像の大きさは通常 {ic.get('width')}x{ic.get('height')}。変えたいときだけ image に width / height を入れる。",
        ]
    return "\n".join(lines)


def settings_block(st):
    parts, missing = settings_parts(st)
    return "\n\n".join(f"## {lb}\n{ct}" for lb, _, ct in parts), missing


def sig_of(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def history_text(msgs, user_name, limit=None):
    lines = []
    for m in msgs:
        who = (user_name or "ユーザー") if m["role"] == "user" else "AI"
        line = f"[{who}]\n{m.get('text', '')}"
        if (m.get("image") or {}).get("prompt"):
            line += f"\n（画像: {m['image']['prompt']}）"
        for f in m.get("files") or []:
            line += f"\n（添付: {f['name']} … {upload_abs(f['path'])}）"
        lines.append(line)
    out, total = [], 0
    for line in reversed(lines):
        total += len(line)
        if total > (limit or HISTORY_CHARS):
            out.append("（これより前の会話は省略。AIメモを参照）")
            break
        out.append(line)
    return "\n\n".join(reversed(out))


def upload_abs(rel):
    return os.path.normpath(os.path.join(UPLOADS, rel))


def save_upload(sid, name, data):
    """添付を data/uploads/<sid>/ に置いて、発言に付ける形（{name, path, type, size}）で返す"""
    name = os.path.basename(str(name or "").replace("\\", "/")).strip() or "file"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[-120:]
    d = os.path.join(UPLOADS, sid)
    os.makedirs(d, exist_ok=True)
    fn = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6] + "_" + name
    with open(os.path.join(d, fn), "wb") as f:
        f.write(data)
    return {"name": name, "path": f"{sid}/{fn}", "type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": len(data)}


def clean_files(sid, files):
    """送信で付けてきた添付が、このチャンネルに上げたファイルかを確かめて揃える"""
    out = []
    for f in files if isinstance(files, list) else []:
        rel = f.get("path") if isinstance(f, dict) else None
        if not isinstance(rel, str) or "/" not in rel or rel.split("/", 1)[0] != sid:
            raise ValueError("bad attachment")
        full = upload_abs(rel)
        if os.path.dirname(full) != os.path.join(UPLOADS, sid) or not os.path.isfile(full):
            raise ValueError("bad attachment")
        name = f.get("name") if isinstance(f.get("name"), str) and f["name"].strip() else rel.split("_", 3)[-1]
        out.append({"name": name.strip()[:120], "path": rel, "size": os.path.getsize(full),
                    "type": mimetypes.guess_type(full)[0] or "application/octet-stream"})
    return out


RECENT = 15  # light で全部見せるときに全文で見せる直近の発言数。それより前は AIメモ（あらすじ）で補う
CTX = os.path.join(DATA, "ctx")  # 長い会話の書き出し先（チャンネルごとに上書き。wait_input.py と同じ場所）
LONG_TEXT, LONG_LINES = 1500, 25  # これより長い会話は表示せずファイルに書いて読ませる（Antigravity が途中を省略するので）


def history_section(sid, name, label, text):
    """会話の全文を出す。長ければファイルに書いて場所を出す。戻り値: (セクション, 読ませるパス or None)"""
    if len(text) <= LONG_TEXT and text.count("\n") <= LONG_LINES:
        return {"label": label, "text": text}, None
    os.makedirs(CTX, exist_ok=True)
    path = os.path.join(CTX, f"{sid}_{name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n{text}\n")
    return {"label": label,
            "text": f"ファイル: {path}\n（{text.count(chr(10)) + 1}行。このファイルを最初から最後まで読むこと）"}, path


def msg_sig(m):
    return [m["id"], sig_of(m.get("text", ""))]


def sent_path(sid):
    return os.path.join(SENT, sid + ".json")


def build_context(s, m):
    """wait_input.py が受け取った入力について、AIに見せる内容を組み立てる（そのまま表示する。ファイルには書かない）。
    担当IDEの会話は前に送ったものを覚えているので、担当が前回と同じで、そのとき送った会話が書き換わっていなければ
    増えた発言と変わった設定だけ送る（diff）。担当が変わった・初めて・過去が書き換わったときは全部送る（full）。
    チャンネルの input_mode が light なら、全部のときは直近15件を全文で（それより前は AIメモ。メモが空なら前の分も全文）、
    ファイルの設定と長い会話は場所を出して読ませる。続きのときは今回の発言（と変わった設定）だけ（省いた設定の一覧も出さない）。
    戻り値: {"mode", "reason", "sections": [{"label", "text"}], "same": [省いた設定のラベル], "files": [読ませるパス],
            "light": bool, "missing": [...], "rules": 毎回最後に付けるルール {"label", "text"} か None}"""
    st = s["setting"]
    if st.get("agents_md") and sync_agents(s):  # PC で System ファイルを書き換えていても、ここで AGENTS.md に反映する
        save(s)
    light = st.get("input_mode") == "light"
    paths = {}
    parts, missing = settings_parts(st, agents_in_use(s), paths)
    if (st.get("memo") or "").strip():
        parts.append(("AIメモ（これまでのあらすじ）", "memo", st["memo"].strip()))
    if st.get("image_mode") != "off" and st.get("chars"):
        parts.append(("固定キャラ（画像に写るときこの外見タグが自動で付く）", "chars", chars_block(st["chars"])))
    msgs = s["messages"]
    idx = msgs.index(m)
    ide = st.get("ide_session") or ""
    old = read_json(sent_path(s["id"]), {})
    known = old.get("msgs") or []
    if not ide:
        reason = "担当のIDE会話が決まっていないチャンネル（受け取るAIが前回と同じとは限らない）"
    elif old.get("reset"):
        reason = old.get("why") or "UIで「次は全部送る」が押された"
    elif not known:
        reason = "このチャンネルをこの会話に送るのは初めて"
    elif old.get("ide") != ide:
        reason = "担当のIDE会話が切り替わった"
    elif len(known) > idx + 1 or [msg_sig(x) for x in msgs[:len(known)]] != known:
        reason = "前回送ったあとに会話が書き換えられた（編集・作り直し・巻き戻し）"
    else:
        reason = ""
    user = st.get("user_name")
    sections, same, files = [], [], []

    def section(lb, key, ct):
        if light and key in paths:  # ファイルの設定は中身を出さずに読ませる（長い出力は Antigravity が途中を省略するので）
            files.append(paths[key])
            return {"label": lb, "text": f"ファイル: {paths[key]}\n（このファイルを最初から最後まで読むこと）"}
        return {"label": lb, "text": ct}

    if reason:
        sections = [section(lb, key, ct) for lb, key, ct in parts]
        if idx and light:
            cut = max(0, idx - RECENT)
            if cut and (st.get("memo") or "").strip():
                sections.append({"label": f"それより前の会話（{cut}件）",
                                 "text": "上の「AIメモ（これまでのあらすじ）」がその要約。全文は出さない"})
            elif cut:  # 要約が無いので、前の分も全文で読ませる
                sec, path = history_section(s["id"], "older", f"それより前の会話（{cut}件、全文）",
                                            history_text(msgs[:cut], user, limit=10 ** 9))
                sections.append(sec)
                files += [path] if path else []
            sec, path = history_section(s["id"], "recent", f"直近の会話（{idx - cut}件、全文）",
                                        history_text(msgs[cut:idx], user, limit=10 ** 9))
            sections.append(sec)
            files += [path] if path else []
        elif idx:
            sections.append({"label": f"これまでの会話（全{idx}件）", "text": history_text(msgs[:idx], user)})
    else:
        sigs, labels = old.get("sigs") or {}, old.get("labels") or {}
        for lb, key, ct in parts:
            if sigs.get(key) == sig_of(ct):
                same.append(lb)
            else:
                sections.append(section(lb + "（前回から変わった）", key, ct))
        if light:
            same = []
            gone = [labels.get(k, k) for k in sigs if k not in {key for _, key, _ in parts}]
            if gone:
                sections.append({"label": "使わなくなった設定", "text": "次の設定はもう使わない（前に読んだ内容は忘れる）:\n"
                                 + "\n".join(f"- {x}" for x in gone)})
        new = msgs[len(known):idx]  # 前回送ったあとに増えた、AIがまだ見ていない発言
        if new and light:
            sec, path = history_section(s["id"], "new", "前回のあとに増えた会話", history_text(new, user))
            sections.append(sec)
            files += [path] if path else []
        elif new:
            sections.append({"label": "前回のあとに増えた会話", "text": history_text(new, user)})
    if st.get("rp_state"):  # 全部・差分に関係なく毎回（AI が前のターンを覚えていなくても、手で直した分も届くように）
        sections.append({"label": "キャラの状態（前の応答のとき。これを踏まえて書き、応答の state で更新する）",
                         "text": state_block(state_before(msgs, idx))})
    if m.get("files"):
        paths = [upload_abs(f["path"]) for f in m["files"]]
        sections.append({"label": "ユーザーが今回の発言に添付したファイル（"
                         + ("画像はこの会話のチャット欄にも貼った。そこで見えていれば開き直さなくてよい" if m.get("pasted")
                            else "応答を書く前に開いて見る") + "）",
                         "text": "\n".join(f"ファイル: {p}（{f['name']}）" for p, f in zip(paths, m["files"]))})
        files += paths
    sections.append({"label": "ユーザーの発言（これに応答する）", "text": m["text"] or "（本文なし。添付だけ）"})
    rules = None  # 入力のたびに表示の最後に付けるルール（全部・差分に関係なく毎回。長ければファイルに書いて場所を出す）
    if st.get("rules_every") and (st.get("agents_extra") or "").strip():
        rules, path = history_section(s["id"], "rules", "このチャンネルのルール（毎回必ず守る）", st["agents_extra"].strip())
        files += [path] if path else []
    os.makedirs(SENT, exist_ok=True)
    write_json(sent_path(s["id"]), {"ide": ide, "sigs": {key: sig_of(ct) for _, key, ct in parts},
                                    "labels": {key: lb for lb, key, _ in parts},
                                    "msgs": [msg_sig(x) for x in msgs[:idx + 1]], "ts": now()})
    return {"mode": "full" if reason else "diff", "reason": reason, "sections": sections,
            "same": same, "files": files, "light": light, "missing": missing, "rules": rules}


def note_reply(s, reply_to, msg):
    """取り込んだ応答はAI自身が書いたものなので、送ったものとして記録する（次の差分に入れない）"""
    path = sent_path(s["id"])
    old = read_json(path, {})
    if not old.get("msgs") or old["msgs"][-1][0] != reply_to:
        return
    if msg in s["messages"]:
        old["msgs"].append(msg_sig(msg))
    memo = (s["setting"].get("memo") or "").strip()
    if memo:
        old.setdefault("sigs", {})["memo"] = sig_of(memo)
        old.setdefault("labels", {})["memo"] = "AIメモ（これまでのあらすじ）"
    write_json(path, old)


# ---------------------------------------------------------------- 固定キャラ
COUNT_TAG = re.compile(r"(\d+\+?|multiple |no )(girl|boy|other)s?|solo|solo focus", re.I)


def char_key(name):
    return re.sub(r"\s+", "", name or "").lower()


def split_tags(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def find_char(chars, name):
    k = char_key(name)
    return next((c for c in chars if char_key(c.get("name")) == k), None) if k else None


def char_presets():
    return [p for p in read_json(CHAR_PRESETS, []) if isinstance(p, dict)]


def merge_chars(chars, updates, presets=None):
    """AI が返した characters を一覧に反映する（chars を書き換える）。
    ユーザーが固定したキャラは変えない。初登場のキャラは同名のプリセットがあればそちらを使う"""
    for u in updates or []:
        if not isinstance(u, dict):
            continue
        name = (u.get("name") or "").strip() if isinstance(u.get("name"), str) else ""
        tags = ", ".join(t for t in split_tags(u.get("tags") if isinstance(u.get("tags"), str) else "")
                         if not COUNT_TAG.fullmatch(t))  # 人数タグは場面側で書く
        if not name or not tags:
            continue
        cur = find_char(chars, name)
        if cur:
            if not cur.get("locked"):
                cur.update(tags=tags, source="ai")
            continue
        p = find_char(presets or [], name)
        chars.append({"id": new_id(), "name": name, "tags": p["tags"] if p else tags,
                      "locked": bool(p), "source": "preset" if p else "ai"})
    return chars


def chars_block(chars):
    lines = [f"- {c['name']}: {c['tags']}" + ("（ユーザーが固定。characters で変えても変わらない）" if c.get("locked") else "")
             for c in chars if c.get("name")]
    return "\n".join(lines) or "（まだいない）"


# ---------------------------------------------------------------- キャラの状態
# AI が応答の state で書く、キャラごとの今の状態（箇条書き）と内心。応答（assistant の発言）ごとにその時点の全員分を持つので、
# 巻き戻し・作り直し・分岐をすると、残った発言の最後の状態に戻る
def clean_state(v):
    """[{name, situation?: [str], inner?: str, remove?}] に整える（書いてあった項目だけ持つ）。{"名前": {...}} の形も受け付ける"""
    if isinstance(v, dict):
        v = [dict(x, name=k) for k, x in v.items() if isinstance(x, dict)]
    out = []
    for c in v if isinstance(v, list) else []:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"].strip():
            continue
        u = {"name": c["name"].strip()}
        if c.get("remove") is True:
            u["remove"] = True
        sit = c.get("situation")
        if isinstance(sit, str):
            sit = sit.splitlines()
        if isinstance(sit, list):
            u["situation"] = [x for x in (re.sub(r"^\s*[-・*]\s*", "", y).strip() for y in sit if isinstance(y, str)) if x]
        if isinstance(c.get("inner"), str):
            u["inner"] = c["inner"].strip()
        out.append(u)
    return out


def merge_state(cur, updates):
    """updates に入っているキャラの、書いてある項目だけ入れ替える（remove なら消す）。入っていないキャラは前のまま"""
    out = [dict(c) for c in cur]
    for u in updates:
        i = next((j for j, c in enumerate(out) if char_key(c["name"]) == char_key(u["name"])), -1)
        if u.get("remove"):
            if i >= 0:
                out.pop(i)
            continue
        if i >= 0:
            out[i].update(u)
        else:
            out.append(u)
    return [{"name": c["name"], "situation": c.get("situation") or [], "inner": c.get("inner") or ""} for c in out]


def state_before(msgs, idx):
    """msgs[idx] より前で最後に記録した状態"""
    for m in reversed(msgs[:idx]):
        if m["role"] == "assistant" and isinstance(m.get("state"), list):
            return m["state"]
    return []


def state_block(state):
    if not state:
        return "（まだない。場面に出ているキャラの分を、応答の state で書き始める）"
    return json.dumps(state, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- ide
def ws_path(uri):
    """file:///d%3A/x/y -> D:/x/y（表示用。file:///d:/ の書き方もある）"""
    p = urllib.parse.unquote(uri or "").replace("file:///", "")
    return p[:1].upper() + p[1:] if p[1:2] == ":" else p


def ide_sessions():
    """Antigravity の会話を全部（子エージェントの会話を除く）。UI がフォルダごとにまとめて見せる"""
    if not os.path.exists(IDE_DB):
        raise FileNotFoundError("Antigravity の会話一覧が見つかりません: " + IDE_DB)
    db = sqlite3.connect("file:" + IDE_DB.replace("\\", "/") + "?mode=ro", uri=True, timeout=3)
    try:
        rows = db.execute(
            "SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris"
            " FROM conversation_summaries WHERE nesting_depth = 0 AND killed = 0"
            " ORDER BY last_modified_time LIKE '0001%' DESC, last_modified_time DESC").fetchall()  # まだ何もない新しい会話を先に
    finally:
        db.close()
    out = []
    for cid, title, preview, steps, modified, ws in rows:
        try:  # IDE側はUTCで持っているのでローカル時刻に直す
            updated = datetime.datetime.strptime(modified[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):  # 作ったばかりの会話は 0001-01-01（Windows では変換できない）
            updated = ""
        out.append({
            "id": cid, "title": title or preview or "(無題)", "steps": steps,
            "updated": updated,
            "workspace": ", ".join(ws_path(u) for u in json.loads(ws or "[]")),
            "waiting": time.time() - ide_waiting.get(cid, 0) < 10,
            "poke": poke_log.get(cid),
        })
    return out


def ide_workspaces(cid):
    """IDE会話のワークスペースのフォルダ（合図を送るウィンドウを探すのに使う）"""
    db = sqlite3.connect("file:" + IDE_DB.replace("\\", "/") + "?mode=ro", uri=True, timeout=3)
    try:
        row = db.execute("SELECT workspace_uris FROM conversation_summaries WHERE conversation_id = ?", (cid,)).fetchone()
    finally:
        db.close()
    return [urllib.parse.unquote(u).replace("file:///", "") for u in json.loads((row and row[0]) or "[]")]


known_ides = None


def mark_known(ide):
    global known_ides
    if known_ides is None:
        known_ides = set(read_json(IDE_KNOWN, []))
    if ide not in known_ides:
        known_ides.add(ide)
        write_json(IDE_KNOWN, sorted(known_ides))


# ---------------------------------------------------------------- AGENTS.md（チャンネルの作業フォルダに置くルール）
# Antigravity は会話の作業フォルダ（から上）の AGENTS.md を毎ターン読み直し、ルールとして使う（途中で書き換えても次のターンから効く）。
# System ファイルはここに書けば、wait_input.py の表示で見せるより強く・忘れられずに効く
AGENTS_NAME = "AGENTS.md"
AGENTS_MARK = "<!-- rp-image-chat:"  # アプリが作ったしるし。しるしのないファイル（人が書いたもの）は書き換えない
AGENTS_OWNER = re.compile(re.escape(AGENTS_MARK) + r" channel=([\w-]+) -->")
AGENTS_LOG = os.path.join(DATA, "agents_md.json")  # チャンネルID -> 最後に書いた AGENTS.md の場所（フォルダを変えたら古いほうを片付ける）
agents_state = {}  # チャンネルID -> 最後に書いたときの結果 {"path", "ok", "msg", "ts"}


def path_key(p):
    return os.path.normcase(os.path.normpath(p))


def folder_name(title):
    """チャンネル名をフォルダ名に使える形にする"""
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "").strip().strip(".")
    return n[:40].strip()


def resolve_folder(f):
    return os.path.normpath(os.path.join(NEW_IDE_DIR, os.path.expanduser(f.strip())))  # 絶対パスならそのまま


def channel_folder(s, fix=False):
    """チャンネルの作業フォルダ（新規会話を作る場所・AGENTS.md を置く場所）。
    ide_folder が空なら NEW_IDE_DIR そのもの（チャンネルごとのフォルダは作らない）。
    fix は前の作り（チャンネル名のフォルダを設定に残す）の名残りで、いまは何もしない"""
    st = s["setting"]
    if (st.get("ide_folder") or "").strip():
        return resolve_folder(st["ide_folder"])
    return NEW_IDE_DIR


def agents_text(s):
    """チャンネルの AGENTS.md の中身。System ファイルが読めなければ OSError / ValueError"""
    st = s["setting"]
    title = st.get("title") or s["id"]
    out = [f"{AGENTS_MARK} channel={s['id']} -->",
           f"<!-- RP Image Chat がチャンネル「{title}」の設定から作るファイル。直接書き換えても次に上書きされる。変えるときはチャット画面の設定から -->",
           "", f"# ロールプレイチャット「{title}」", ""]
    ide = st.get("ide_session") or ""
    if ide:
        me = f'python "{os.path.join(ROOT, "wait_input.py")}" --ide {ide}'
        out.append("この会話はロールプレイチャットアプリのこのチャンネルの担当。チャットの入力は wait_input.py で受け取り、"
                   "応答は wait_input.py が表示した outbox に JSON で書く。")
        out.append(f"入力があると、この会話に半角スペース1文字（合図）が届く。届いたら `{me} --wait 30` を実行し、表示に従って応答する。"
                   "書いたら待機せず、この会話への返事は半角スペース1文字だけにしてターンを終える。")
        out.append("")
    if st.get("system_file"):
        out += [f"## System（{st['system_file']}）", "", read_text(system_file_path(st["system_file"])), ""]
    if (st.get("agents_extra") or "").strip():
        out += ["## 追加のルール", "", st["agents_extra"].strip(), ""]
    return "\n".join(out).rstrip() + "\n"


def agents_owner(path):
    """そのファイルを作ったチャンネルのID。アプリが作ったものでなければ ""、ファイルがなければ None"""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            head = f.read(300)
    except FileNotFoundError:
        return None
    m = AGENTS_OWNER.match(head)
    return m.group(1) if m else ""


def retire_agents(sid, path):
    """このチャンネルが前に書いた AGENTS.md を trash へ移す（人が書いたもの・ほかのチャンネルのものは触らない）"""
    if agents_owner(path) != sid:
        return
    os.makedirs(os.path.join(TRASH, "agents_md"), exist_ok=True)
    n, dst = 0, None
    while not dst or os.path.exists(dst):  # 同じ秒に2回片付けても上書きしない
        n += 1
        dst = os.path.join(TRASH, "agents_md", f"{sid}_{int(time.time())}_{n}_{AGENTS_NAME}")
    try:
        shutil.move(path, dst)
        print(f"[agents] {sid} 片付けた {path}")
    except OSError as e:
        print(f"[agents] {sid} 片付けられなかった {path}: {e}")


def sync_agents(s):
    """チャンネルの AGENTS.md を設定に合わせる（中身が変わったときだけ書く）。
    オフにした・フォルダを変えたときは前に書いたものを trash へ。結果は agents_state に残す。
    作業フォルダを新しく決めたら True（呼んだ側で save する）"""
    st, sid = s["setting"], s["id"]
    log = read_json(AGENTS_LOG, {})
    old, path, fixed = log.get(sid), None, False
    if st.get("agents_md"):
        fixed = not (st.get("ide_folder") or "").strip()
        path = os.path.join(channel_folder(s, fix=True), AGENTS_NAME)
    if old and (not path or path_key(old) != path_key(path)):
        retire_agents(sid, old)
        log.pop(sid, None)
    if not path:
        agents_state.pop(sid, None)
        write_json(AGENTS_LOG, log)
        return fixed
    try:
        text = agents_text(s)
        owner = agents_owner(path)
        if owner == "":
            raise ValueError(f"{path} はアプリが作ったものではないので書き換えない（自分で書いた AGENTS.md なら、作業フォルダを別にする）")
        if owner and owner != sid:
            try:
                o = load(owner)
                taken = o["setting"].get("agents_md") and path_key(channel_folder(o)) == path_key(os.path.dirname(path))
            except (OSError, ValueError):
                taken = False
            if taken:
                raise ValueError(f"このフォルダの AGENTS.md はチャンネル「{o['setting'].get('title')}」のもの。作業フォルダを分ける")
        cur = None
        if owner == sid:
            with open(path, encoding="utf-8", errors="replace", newline="") as f:
                cur = f.read()
        if cur != text:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            print(f"[agents] {sid} 書いた {path}")
        log[sid] = path
        agents_state[sid] = {"path": path, "ok": True, "msg": "書いた" if cur != text else "最新", "ts": now()}
    except (OSError, ValueError) as e:
        agents_state[sid] = {"path": path, "ok": False, "msg": str(e), "ts": now()}
    write_json(AGENTS_LOG, log)
    return fixed


def agents_in_use(s):
    """担当の会話がこのチャンネルの AGENTS.md を読んでいれば（会話の作業フォルダが AGENTS.md のフォルダなら）その場所"""
    st = s["setting"]
    if not st.get("agents_md") or not st.get("ide_session"):
        return None
    folder = channel_folder(s)
    path = os.path.join(folder, AGENTS_NAME)
    if agents_owner(path) != s["id"]:
        return None
    try:
        ws = ide_workspaces(st["ide_session"])
    except (OSError, sqlite3.Error, ValueError):
        return None
    return path if any(path_key(w) == path_key(folder) for w in ws) else None


def agents_info(s):
    """画面に出す AGENTS.md の様子"""
    st = s["setting"]
    folder = channel_folder(s)
    info = {"folder": folder, "fixed": bool((st.get("ide_folder") or "").strip()), "enabled": bool(st.get("agents_md")),
            "path": os.path.join(folder, AGENTS_NAME), "state": agents_state.get(s["id"]), "workspace": None, "in_use": False}
    try:
        info["text"] = agents_text(s)
    except (OSError, ValueError) as e:
        info["text"], info["error"] = "", f"System ファイルが読めない: {e}"
    if st.get("ide_session"):
        try:
            info["workspace"] = ", ".join(ide_workspaces(st["ide_session"]))
        except (OSError, sqlite3.Error, ValueError):
            pass
        info["in_use"] = bool(agents_in_use(s))
    return info


# ---------------------------------------------------------------- 会話の引っ越し（担当の会話の作業フォルダをチャンネルのフォルダへ）
# 履歴を残したまま会話がチャンネルの AGENTS.md を読むようにする。Antigravity の再起動が要るので、予約しておいてまとめて1回で移す
IDE_MOVES = os.path.join(DATA, "ide_moves.json")  # 会話ID -> {"folder", "sid", "title", "ts"}（予約）
move_state = {"running": False}  # 最後に移したときの様子 {"running", "msg", "results", "ts"}


def channels_of(ide):
    """その会話を担当にしているチャンネル [(ID, 名前)]"""
    out = []
    for sid in list_ids():
        try:
            st = load(sid)["setting"]
        except (OSError, ValueError):
            continue
        if st.get("ide_session") == ide:
            out.append((sid, st.get("title") or sid))
    return out


def _epoch(ts):
    """now() の時刻（ローカル）も Antigravity の時刻（UTC、末尾 Z）も秒にする。読めなければ None"""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z)?", ts or "")
    if not m:
        return None
    t = time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    sec = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc).timestamp() if m.group(3) else time.mktime(t)
    return sec + (float(m.group(2)) if m.group(2) else 0)


def branch_fork_point(msgs, idx, turns):
    """チャンネルを msgs[idx] までで分岐するとき、担当の会話をどのステップで分岐するか。
    含まれる最後の AI の返事を書いたターンのあと、次の入力を受け取ったターンの前（そのターンの最初の発言の直前）で切る。
    turns は ide_poke.turns の発言一覧（古い順）。(at, None) か (None, 分岐しない理由)。at = -1 は最後まで"""
    a = next((i for i in range(idx, -1, -1) if msgs[i]["role"] == "assistant"), None)
    if a is None:
        return None, "まだAIの返事がない時点なので、Antigravity の会話は分岐しない（新規会話を使う）"
    lower = _epoch(msgs[a].get("ts"))
    if lower is None:
        return None, "返事の時刻が読めないので、Antigravity の会話は分岐しない"
    upper = None  # 次の入力を受け取った時刻（秒未満は切り捨てなので1秒の余裕）
    u = next((i for i in range(a + 1, len(msgs)) if msgs[i]["role"] == "user"), None)
    if u is not None:
        upper = _epoch(msgs[u].get("claimed_ts"))
        if upper is None:  # 受け取った時刻を残す前の入力は、その返事の時刻まで
            r = next((x for x in msgs[u + 1:] if x["role"] == "assistant"), None)
            upper = _epoch(r.get("ts")) if r else None
        if upper is not None:
            upper += 1
    t = next((t for t in turns if (_epoch(t.get("time")) or 0) > lower), None)
    if t is None or (upper is not None and _epoch(t["time"]) > upper):
        if upper is None:
            return -1, None  # 返事のあとに何も来ていない＝いまの会話の最後まで
        return None, ("その返事のあと、次の入力までの間にターンの切れ目がない（1つのターンの中で続けて受け取っていた）ので、"
                      "ちょうどの位置で分岐できない。Antigravity の会話は分岐しない")
    if t["at"] is None:
        return None, "会話の最初の発言より前になるので、Antigravity の会話は分岐しない"
    return t["at"], None


def plan_revert(s, keep):
    """発言を書き換える前に呼ぶ。担当の会話を msgs[keep] まで（keep = -1 なら最初の発言の前まで）に巻き戻す予定を覚える。
    巻き戻しは時間がかかるので lock の外（/api/ide/revert）でする。予定を作ったら True"""
    st = s["setting"]
    if not st.get("ide_session") or not st.get("ide_revert", True):
        return False
    revert_plan[s["id"]] = {"ide": st["ide_session"], "msgs": copy.deepcopy(s["messages"]), "keep": keep, "t": time.time()}
    return True


def moves_info():
    pend = read_json(IDE_MOVES, {})
    return {"pending": [dict(v, ide=k) for k, v in pend.items()], "state": move_state}


def run_moves(force):
    """予約した引っ越しをまとめて実行する（別スレッドで。Antigravity を閉じて書き換え、起動し直す）"""
    pend = read_json(IDE_MOVES, {})

    def backup(cid, blob, row):  # 書き換える前の値を trash に残す（戻すときに使う）
        os.makedirs(os.path.join(TRASH, "ide_moves"), exist_ok=True)
        write_json(os.path.join(TRASH, "ide_moves", f"{cid}_{int(time.time())}.json"),
                   {"ide": cid, "to": pend[cid]["folder"], "blob": blob.hex(), "summary": list(row) if row else None, "ts": now()})
    try:
        res, msg = ide_poke.move_workspaces({c: v["folder"] for c, v in pend.items()}, force=force, backup=backup)
    except Exception as e:
        res, msg = {}, f"移せなかった: {e}"
    with lock:
        cur = read_json(IDE_MOVES, {})
        for cid, (ok, _) in res.items():
            if not ok:
                continue
            cur.pop(cid, None)
            for sid, _ in channels_of(cid):  # 移った先で AGENTS.md を読むので、中身を最新に
                s = load(sid)
                if s["setting"].get("agents_md") and sync_agents(s):
                    save(s)
        write_json(IDE_MOVES, cur)
        move_state.update(running=False, msg=msg, ts=now(),
                          results=[{"ide": c, "title": pend.get(c, {}).get("title", ""), "ok": ok, "msg": m} for c, (ok, m) in res.items()])
    print(f"[move] {msg}")


def poke_text(ide, full=False):
    """合図の文。wait_input.py を実行したことのない会話は、スペースだけでは何をすればいいか分からないのでコマンドを送る。
    full: 実行したことがあっても、コマンドを送る（スペースだけでは動かなかったとき・このチャンネルを初めて受け持つとき）"""
    global known_ides
    if known_ides is None:
        known_ides = set(read_json(IDE_KNOWN, []))
    if ide in known_ides and not full:
        return POKE_TEXT
    script = os.path.join(ROOT, "wait_input.py")
    return (f"（ロールプレイチャットアプリからの合図）このチャットに新しい入力が来た。次のコマンドを実行し、表示に従って応答すること:\n"
            f'python "{script}" --ide {ide} --wait 30\n'
            f"これ以降、この会話に半角スペース1文字だけが届いたら、それが同じ合図。同じコマンドを実行すること。")


def ide_model_of(ide):
    """その会話を担当にしているチャンネルで選んだモデル（なければ空）"""
    for sid, _ in channels_of(ide):
        try:
            m = load(sid)["setting"].get("ide_model")
        except (OSError, ValueError):
            continue
        if m:
            return m
    return ""


PASTE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}  # 合図と一緒に Antigravity のチャット欄に貼る添付
PASTE_MAX = 20 * 1024 * 1024  # これより大きい画像は貼らない（場所だけ渡す）


def paste_media(msgs):
    """入力に添付した画像を、Antigravity の画面で貼り付けたときと同じ形にする"""
    out = []
    for m in msgs:
        for f in m.get("files") or []:
            full = upload_abs(f["path"])
            if f.get("type") in PASTE_TYPES and os.path.isfile(full) and os.path.getsize(full) <= PASTE_MAX:
                with open(full, "rb") as fp:
                    out.append({"mimeType": f["type"], "description": f["name"],
                                "inlineData": base64.b64encode(fp.read()).decode("ascii")})
    return out


def send_poke(ide, full=False, media=None):
    """担当IDE会話に合図を送る。結果は poke_log に残し、UI に出す。
    media: 合図と一緒にチャット欄に貼る画像（paste_media の形）。
    (成功したか, 説明, あとでやり直すべきか) を返す。会話が応答中のときはやり直す"""
    retry = False
    text = poke_text(ide, full)
    if not ide_poke:
        ok, msg = False, "この環境では合図を送れない（Windows のみ）"
    elif POKE_MODE != "window":
        with poke_lock:
            try:
                ok, msg, retry = ide_poke.send_to_conversation(ide, text, ide_model_of(ide), media or None)
                if ok and text != POKE_TEXT:
                    msg += "（スペースだけでは伝わらないかもしれないので、実行するコマンドを送った）"
                if ok and media:
                    msg += f"（画像{len(media)}枚をチャット欄に貼った）"
            except Exception as e:
                ok, msg = False, f"会話に送れなかった: {e}"
    else:
        try:
            ws = ide_workspaces(ide)
        except (OSError, sqlite3.Error, ValueError) as e:
            ws, ok, msg = None, False, f"IDE の会話一覧を読めない: {e}"
        if ws is not None:
            with poke_lock:
                try:
                    ok, msg = ide_poke.poke(ws, text.replace("\n", " "))  # 改行を打つと途中で送信されるので
                except Exception as e:
                    ok, msg = False, f"キー入力に失敗: {e}"
    poke_log[ide] = {"ts": now(), "ok": ok, "msg": msg}
    print(f"[poke] {'ok' if ok else 'wait' if retry else 'NG'} {ide[:8]} {msg}")
    return ok, msg, retry


def new_ide(sid, auto=False):
    """Antigravity にチャンネルの作業フォルダの新しい会話を作って、このチャンネルの担当にする。作るのに数秒かかるので lock の外で呼ぶ。
    (会話ID, 名前, 説明, フォルダ) か (None, None, 失敗の説明, None)。チャンネルがなければ FileNotFoundError / ValueError。
    auto: ide_auto_worker から。作っている間に担当が決まった・自動をやめたなら、作った会話は担当にしない"""
    with lock:
        s = load(sid)
        folder = channel_folder(s)
    try:
        os.makedirs(folder, exist_ok=True)
        cid, msg = ide_poke.new_conversation(folder)
    except Exception as e:
        cid, msg = None, f"会話を作れなかった: {e}"
    if not cid:
        return None, None, msg, None
    title = "新規会話 " + now()[5:16].replace("T", " ")
    with lock:
        s = load(sid)
        st = s["setting"]
        if auto and (st.get("ide_session") or not st.get("ide_auto")):
            return None, None, "その間に担当が決まった", None
        st.update(ide_session=cid, ide_title=title, ide_auto=False)
        if st.get("agents_md"):
            sync_agents(s)  # 最初の合図より前に AGENTS.md を置いておく
        save(s)
    print(f"[ide] new {cid[:8]} {folder} {msg}")
    return cid, title, msg, folder


AUTO_RETRY = 15  # Antigravity がまだ起動していないとき、新規会話を作り直すまでの秒数


def ide_auto_worker():
    """初めての起動で作ったチャンネル1にも、「＋」で作ったチャンネルと同じように Antigravity の新規会話を付ける。
    Antigravity がまだ起動していなければ、起動するまで AUTO_RETRY 秒ごとに試す（入力が先に来ていても、付いたら合図が届く）"""
    told = False
    while True:
        try:
            on = ide_poke and hasattr(ide_poke, "new_conversation") and app_settings().get("new_channel_ide") is not False
            want = []
            with lock:
                for sid in list_ids():
                    s = load(sid)
                    st = s["setting"]
                    if not st.get("ide_auto"):
                        continue
                    if st.get("ide_session") or not on:
                        st["ide_auto"] = False
                        save(s)
                    else:
                        want.append(sid)
            if not want:
                return
            for sid in want:
                cid, _, msg, _ = new_ide(sid, auto=True)
                if not cid and not told:
                    print(f"[ide] チャンネル1の Antigravity 会話はまだ作れていない: {msg}（Antigravity が起動したら作る）")
                    told = True
        except Exception as e:
            print(f"[ide auto] {e}")
        time.sleep(AUTO_RETRY)


def poke_worker():
    """入力が来て POKE_DELAY 秒たっても応答がなければ、担当IDEに合図する（1つの入力につき1回）。
    応答を取り込んだ直後（返事のスペースを打っているところ）は待つ。
    rpc のときは、会話が応答中かどうかを send_to_conversation が会話の状態で確かめる。受け取り済み（processing）でも
    会話が止まっていれば合図する（裏で動いていた wait_input.py が受け取って、誰も読んでいないことがある。
    合図で実行された wait_input.py が、応答の届いていない入力として出し直す）。
    window のときは会話の状態が分からないので、受け取り済み・wait_input 待機中は応答中とみなす。
    スペースだけでは AI が wait_input.py を実行しないことがある（分岐や別の作業で会話の最後が別の話になっているとき）。
    そのチャンネルをこの会話に送ったことがなければ、最初からコマンドを送る。
    スペースを送って POKE_AGAIN 秒たっても受け取られなければ、コマンドを1回だけ送り直す"""
    rpc = POKE_MODE != "window"
    since, done, quiet = {}, set(), {}
    poked, again = {}, set()  # 合図した入力 -> そのときの時刻 / コマンドを送り直した入力
    while True:
        time.sleep(0.5)
        try:
            busy, cands, unclaimed, unseen = set(), {}, {}, set()
            with lock:
                for sid in list_ids():
                    s = load(sid)
                    st, msgs = s["setting"], s["messages"]
                    ide = st.get("ide_session")
                    if not ide:
                        continue
                    for m in msgs:
                        if m["role"] == "assistant":
                            if age(m.get("ts")) < 6:  # 応答を取り込んだ直後
                                busy.add(ide)
                        elif m.get("status") == "processing" and not rpc:
                            # 応答中。ただし5分たっても応答がないものは止まったとみなす（次の合図で wait_input が出し直す）
                            if time.time() - claimed_at.get(m["id"], 0) < 300:
                                busy.add(ide)
                        elif m.get("status") in ("pending", "processing") and m["id"] not in done:
                            cands.setdefault(ide, []).append(m)
                            if read_json(sent_path(sid), {}).get("ide") != ide:  # この会話はこのチャンネルを見たことがない
                                unseen.add(ide)
                        elif (m.get("status") == "pending" and m["id"] in poked
                              and m["id"] not in again):
                            unclaimed.setdefault(ide, []).append(m["id"])
            t = time.time()
            busy |= reverting | {v["ide"] for v in list(revert_plan.values()) if t - v["t"] < REVERT_HOLD}
            for ide, ids in unclaimed.items():  # スペースは届いたのに wait_input.py が実行されていない
                if ide in busy or t < quiet.get(ide, 0) or t - max(poked[i] for i in ids) < POKE_AGAIN:
                    continue
                ok, msg, retry = send_poke(ide, full=True)
                if retry:
                    quiet[ide] = time.time() + 3
                    continue
                again.update(ids)
                quiet[ide] = time.time() + (3 if rpc else 15)
            for ide, ms in cands.items():
                ids = [m["id"] for m in ms]
                if ide in busy or t < quiet.get(ide, 0) or (not rpc and t - ide_waiting.get(ide, 0) < 4):
                    for i in ids:  # 待っている間は数えない（空いてから POKE_DELAY 秒）
                        since.pop(i, None)
                    continue
                fresh = [i for i, m in zip(ids, ms) if age(m.get("ts")) < 600]
                done.update(i for i in ids if i not in fresh)  # 古い入力（再起動前のもの等）には合図しない
                if not fresh:
                    continue
                for i in fresh:
                    since.setdefault(i, t)
                if t - min(since[i] for i in fresh) >= POKE_DELAY:
                    full = ide in unseen
                    # 添付の画像は合図と一緒にチャット欄に貼る（貼るのは最初の合図だけ。送り直しでは貼らない）
                    media = paste_media([m for m in ms if m["id"] in fresh and not m.get("pasted")]) if rpc else []
                    ok, msg, retry = send_poke(ide, full, media)
                    if retry:  # 会話が応答中。終わるのを待って送り直す
                        quiet[ide] = time.time() + 3
                        continue
                    done.update(fresh)
                    if ok and media:
                        with lock:
                            for i in fresh:
                                x = session_of_msg(i)
                                s = load(x) if x else None
                                m = s and find_msg(s, i)
                                if m and m.get("files"):
                                    m["pasted"] = True  # wait_input.py の表示で「チャット欄にも貼った」と伝える
                                    save(s)
                    if ok:
                        poked.update((i, time.time()) for i in fresh)
                        if full:
                            again.update(fresh)
                    # AI が wait_input を実行して受け取るまでの間に重ねて打たない。
                    # rpc は会話が動き出したのを確かめてから戻るので、そのあとは会話の状態で待てる
                    quiet[ide] = time.time() + (3 if rpc else 15)
        except Exception as e:
            print(f"[poke worker] {e}")


def age(ts):
    try:
        return time.time() - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 1e9


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
            if not isinstance(rep, dict):
                raise ValueError("JSON がオブジェクトではない")
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
            st = s["setting"]
            drawing = st.get("image_mode") != "off"
            updates = rep.get("characters") if isinstance(rep.get("characters"), list) else []
            if updates and drawing:
                st["chars"] = merge_chars(st.get("chars") or [], updates, char_presets())
            msg = {"id": new_id(), "role": "assistant", "text": rep.get("text", ""), "ts": now()}
            img = rep.get("image")
            if not isinstance(img, dict):
                img = {"prompt": rep["image_prompt"]} if isinstance(rep.get("image_prompt"), str) else {}
            if img.get("prompt") and drawing:
                names = img.get("characters", rep.get("image_characters"))
                found = [find_char(st.get("chars") or [], n) for n in (names if isinstance(names, list) else []) if isinstance(n, str)]
                msg["image"] = {
                    "prompt": img["prompt"], "negative": img.get("negative"),
                    "width": img.get("width"), "height": img.get("height"),
                    "raw": bool(img.get("raw", False)), "seed": img.get("seed"), "status": "queued",
                    "chars": list(dict.fromkeys(c["name"] for c in found if c)),
                }
            if msg["text"] or msg.get("image"):
                if st.get("rp_state") and isinstance(rep.get("state"), (list, dict)):
                    msg["state"] = merge_state(state_before(s["messages"], len(s["messages"])), clean_state(rep["state"]))
                s["messages"].append(msg)
            if isinstance(rep.get("memo"), str) and rep["memo"].strip():
                st["memo"] = rep["memo"].strip()
            save(s)
            note_reply(s, reply_to, msg)
        shutil.move(path, os.path.join(DONE, f"{int(time.time())}_{name}"))
        print(f"[outbox] imported {name} -> {sid}")


# ---------------------------------------------------------------- comfy
def build_prompt(setting, img):
    ic = setting.get("image", {})
    if img.get("raw"):
        pos = img["prompt"]
    else:
        # 品質タグ → 人数タグ → 共通タグ → 写っている固定キャラの外見 → 場面。同じタグは最初の1つだけ残す
        # 人数タグは場面（AI）のものを優先し、共通タグ等に残っている古い「1girl」とぶつからないようにする
        rest = split_tags(ic.get("character_tags"))
        for name in img.get("chars") or []:
            c = find_char(setting.get("chars") or [], name)
            if c:
                rest += split_tags(c.get("tags"))
        scene = split_tags(img["prompt"])
        if any(COUNT_TAG.fullmatch(t) for t in scene):
            rest = [t for t in rest if not COUNT_TAG.fullmatch(t)]
        rest += scene
        counts = [t for t in rest if COUNT_TAG.fullmatch(t)]
        tags = split_tags(ic.get("base_positive")) + counts + [t for t in rest if not COUNT_TAG.fullmatch(t)]
        pos = ", ".join(dict.fromkeys(tags))
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
        wf, pos, neg = build_workflow(s["setting"], im, f"rpchat/{s['id']}/rp_{m['id']}")
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


def outbox_worker():
    while True:
        try:
            process_outbox()
        except Exception as e:
            print(f"[outbox worker] {e}")
        time.sleep(1)


def image_worker():
    # 画像の生成待ちで応答の取り込みが止まらないよう、別のスレッドで回す
    while True:
        try:
            process_images()
        except Exception as e:
            print(f"[image worker] {e}")
        time.sleep(1)


def summary_of(s):
    st = s["setting"]
    msgs = s["messages"]
    return {
        "id": s["id"], "title": st.get("title"), "summary": st.get("summary", ""), "project": st.get("project", ""),
        "rev": s.get("rev"), "updated": s.get("updated"), "created": s.get("created"),
        "count": len(msgs),
        "waiting": any(m["role"] == "user" and m.get("status") in ("pending", "processing") for m in msgs),
        "generating": any((m.get("image") or {}).get("status") in ("queued", "generating") for m in msgs),
        "last": (msgs[-1]["text"][:60] if msgs else ""),
        "ide_title": st.get("ide_title", "") if st.get("ide_session") else "",
    }


def clean_import(obj):
    """書き出した JSON（または messages だけの配列）を取り込める形に整える。
    agy 版で書き出したものも読める（agy の会話の記録は捨てる）"""
    if isinstance(obj, list):
        obj = {"messages": obj}
    if (not isinstance(obj, dict) or not ("messages" in obj or "setting" in obj)
            or not isinstance(obj.get("messages", []), list) or not isinstance(obj.get("setting") or {}, dict)):
        raise ValueError("チャンネルの JSON ではありません")
    msgs = []
    for m in obj.get("messages", []):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("text", ""), str):
            raise ValueError("messages の中身が不正です")
        m = dict(m, id=new_id())
        m.pop("agy", None)
        if m["role"] == "user":
            m["status"] = "answered"
            m.pop("error", None)
        if m.get("image") and m["image"].get("status") in ("queued", "generating"):
            m["image"]["status"] = "error"
            m["image"]["error"] = "取り込み時に未完成"
        msgs.append(m)
    st = {k: v for k, v in (obj.get("setting") or {}).items() if not k.startswith("agy_") and k != "model"}
    return dict(st, **NO_IDE, **NO_FOLDER), msgs


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

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
        if os.path.basename(path) == "index.html":
            b = b.replace(b"{{PORT}}", str(PORT).encode())  # タブ名の語尾にポート番号
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if n < 0 or n > BODY_MAX:
            raise ValueError("request body too large")
        d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        if not isinstance(d, dict):
            raise ValueError("request body must be a JSON object")
        return d

    # ------------------------------------------------ access
    def send_plain(self, code, text):
        b = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def blocked(self, u, post=False):
        """入れてよいリクエストでなければ、ここで断って True を返す"""
        if host_name(self.headers.get("Host")) not in access["hosts"]:
            # 知らない名前で届いた（DNS リバインディング）。PC の名前で開くなら RPCHAT_ALLOWED_HOSTS に足す
            self.send_plain(403, "unknown Host. Add it to RPCHAT_ALLOWED_HOSTS to allow it.")
            return True
        api = u.path.startswith("/api/") or u.path in ("/img", "/upload")
        if api or post:
            # ほかのサイトのページからの読み書きは断る（このアプリの画面・直接開いたとき・wait_input.py だけ通す）
            site = self.headers.get("Sec-Fetch-Site")
            origin = self.headers.get("Origin")
            if (site and site not in ("same-origin", "none")) or \
                    (origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower()):
                self.send_plain(403, "cross-site request blocked")
                return True
        if post and not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            self.send_plain(415, "Content-Type must be application/json")
            return True
        return False

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if self.blocked(u):
            return
        try:
            if p == "/api/sessions":
                with lock:
                    items = [summary_of(load(sid)) for sid in list_ids()]
                items.sort(key=lambda x: x.get("updated") or "", reverse=True)
                return self.send_json({"sessions": items})
            if p == "/api/session":
                return self.send_json(load(q.get("sid", [""])[0]))
            if p == "/api/session/export":
                s = load(q.get("sid", [""])[0])
                b = json.dumps(s, ensure_ascii=False, indent=2).encode("utf-8")
                name = f"{s['setting'].get('title') or s['id']}_{time.strftime('%Y%m%d_%H%M')}.json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            if p == "/api/pending":
                # ?ide=<会話ID> なら、その会話が担当するチャンネルだけ。無指定なら担当なしのチャンネルだけ
                ide = q.get("ide", [""])[0]
                if ide:
                    ide_waiting[ide] = time.time()
                    mark_known(ide)
                best = None
                with lock:
                    for sid in list_ids():
                        s = load(sid)
                        if s["setting"].get("ide_session", "") != ide:
                            continue
                        for m in s["messages"]:
                            if m["role"] == "user" and m.get("status") == "pending":
                                if not best or m["ts"] < best[1]["ts"]:
                                    best = (sid, m)
                                break
                return self.send_json({"session": best[0] if best else None, "pending": best[1] if best else None})
            if p == "/api/ide/sessions":
                try:
                    return self.send_json({"sessions": ide_sessions(), "new_dir": NEW_IDE_DIR})
                except (OSError, sqlite3.Error) as e:
                    return self.send_json({"error": str(e)}, 500)
            if p == "/api/ide/turns":
                # 分岐する場所を選ぶための、IDE会話のユーザー発言一覧
                if not ide_poke or not hasattr(ide_poke, "turns"):
                    return self.send_json({"error": "この環境では Antigravity の会話を読めない（Windows のみ）"}, 400)
                try:
                    s, res = ide_poke.turns(q.get("ide", [""])[0])
                except Exception as e:
                    s, res = None, f"会話を読めなかった: {e}"
                if s is None:
                    return self.send_json({"error": res}, 500)
                return self.send_json({"title": s.get("summary") or "", "steps": s.get("stepCount"), "turns": res})
            if p == "/api/ide/move":
                return self.send_json(moves_info())
            if p == "/api/ide/models":
                # 合図を送るときに選べるモデル（Antigravity の一覧と残りの利用枠）
                if not ide_poke or not hasattr(ide_poke, "models"):
                    return self.send_json({"error": "この環境では Antigravity のモデルを読めない（Windows のみ）"}, 400)
                try:
                    res, why = ide_poke.models()
                except Exception as e:
                    res, why = None, f"モデルの一覧を読めなかった: {e}"
                if res is None:
                    return self.send_json({"error": why}, 500)
                return self.send_json(res)
            if p == "/api/ide/progress":
                # 応答待ちのあいだ、担当の会話がいま何をしているか（ツール呼び出しの一覧。読むだけ）
                s = load(q.get("sid", [""])[0])
                ide = s["setting"].get("ide_session") or ""
                m = next((x for x in s["messages"] if x["role"] == "user" and x.get("status") in ("pending", "processing")), None)
                if not ide or not m:
                    return self.send_json({"items": []})
                if not ide_poke or not hasattr(ide_poke, "progress"):
                    return self.send_json({"items": [], "note": "この環境では Antigravity の会話を読めない"})
                try:
                    res, why = ide_poke.progress(ide)
                except Exception as e:
                    res, why = None, f"会話を読めなかった: {e}"
                if res is None:
                    return self.send_json({"items": [], "note": why})
                # 止まっている会話の、この入力より前のターンは関係ないので出さない
                if not res["running"] and (_epoch(res["user_time"]) or 0) < (_epoch(m.get("ts")) or 0) - 1:
                    return self.send_json({"items": [], "running": False,
                                           "note": "会話は止まっている（合図を待っている）"})
                return self.send_json(res)
            if p == "/api/agents":
                # チャンネルの作業フォルダと AGENTS.md の様子（中身のプレビューつき）
                with lock:
                    return self.send_json(agents_info(load(q.get("sid", [""])[0])))
            if p == "/api/meta":
                return self.send_json({"app": app_settings(), "phone": phone})
            if p == "/api/files":
                return self.send_json({"files": system_files()})
            if p == "/api/folder-files":
                return self.send_json({"folders": folder_options()})
            if p == "/api/file-presets":
                return self.send_json({"presets": [x for x in read_json(PRESETS, []) if isinstance(x, dict)]})
            if p == "/api/prompt-preview":
                s = load(q.get("sid", [""])[0])
                block, missing = settings_block(s["setting"])
                if s["setting"].get("image_mode") != "off":
                    block += "\n\n## 固定キャラ（画像に写るときこの外見タグが自動で付く）\n" + chars_block(s["setting"].get("chars") or [])
                return self.send_json({"text": block, "missing": missing, "chars": len(block)})
            if p == "/api/char-presets":
                return self.send_json({"presets": char_presets()})
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
        if p == "/upload":
            full = upload_abs(q.get("f", [""])[0])
            if not full.startswith(UPLOADS + os.sep) or not os.path.isfile(full):
                return self.send_error(404)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype if ctype in INLINE_TYPES else "application/octet-stream")
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype not in INLINE_TYPES:  # HTML などをこのアプリのページとして開かせない
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(os.path.basename(full).split("_", 3)[-1]))
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
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
        u = urlparse(self.path)
        p = u.path
        if self.blocked(u, post=True):
            return
        try:
            d = self.body()
        except ValueError as e:
            return self.send_json({"error": f"bad request: {e}"}, 400)
        if p == "/api/ide/poke" and d.get("now"):
            # 今すぐ1回だけ合図する（動作確認・取りこぼし用）。送り終わるまで数秒かかるので、ほかを止めないよう lock の外で
            try:
                ide = load(d.get("sid", ""))["setting"].get("ide_session")
            except (FileNotFoundError, ValueError):
                return self.send_json({"error": "no such session"}, 404)
            if not ide:
                return self.send_json({"error": "担当のIDE会話が決まっていません"}, 400)
            ok, msg, retry = send_poke(ide, full=True)  # 押すのはスペースで動かなかったときなので、コマンドを送る
            return self.send_json({"ok": ok, "msg": msg})
        if p == "/api/ide/new":
            # Antigravity にチャンネルの作業フォルダの新しい会話を作って、このチャンネルの担当にする。作るのに数秒かかるので lock の外で
            if not ide_poke or not hasattr(ide_poke, "new_conversation"):
                return self.send_json({"error": "この環境では Antigravity の会話を作れない（Windows のみ）"}, 400)
            try:
                cid, title, msg, folder = new_ide(d.get("sid", ""))
            except (FileNotFoundError, ValueError):
                return self.send_json({"error": "no such session"}, 404)
            if not cid:
                return self.send_json({"error": msg}, 500)
            return self.send_json({"ok": True, "ide": cid, "title": title, "msg": msg, "folder": folder})
        if p == "/api/pick-folder":
            # 閉じるまで待つので lock の外で
            if not from_this_pc(self.client_address[0]):
                return self.send_json({"error": "フォルダを選ぶウィンドウは PC の画面に出るので、スマホからは選べない"}, 400)
            if not pick_lock.acquire(blocking=False):
                return self.send_json({"error": "フォルダを選ぶウィンドウがもう開いている（PC の画面を見る）", "busy": True}, 409)
            try:
                initial = d.get("initial") if isinstance(d.get("initial"), str) else ""
                return self.send_json({"ok": True, "path": pick_folder(initial)})
            except Exception as e:
                return self.send_json({"error": f"フォルダを選ぶウィンドウを出せなかった: {e}"}, 500)
            finally:
                pick_lock.release()
        if p == "/api/ide/fork":
            # IDE会話を途中（at = ステップ番号、-1 は最後）から分岐する。元の会話はそのまま。assign なら分岐先をこのチャンネルの担当に
            src = d.get("ide") or ""
            if not src:
                return self.send_json({"error": "分岐する会話が決まっていません"}, 400)
            if not ide_poke or not hasattr(ide_poke, "fork"):
                return self.send_json({"error": "この環境では Antigravity の会話を分岐できない（Windows のみ）"}, 400)
            base = ide_poke.FORK_TAG.sub("", d.get("title") or "") or src[:8]
            n = d.get("n")
            title = f"【分岐#{n}】{base}" if n else f"【分岐】{base}"
            try:
                cid, msg = ide_poke.fork(src, int(d.get("at", -1)), title)
            except Exception as e:
                cid, msg = None, f"分岐できなかった: {e}"
            if not cid:
                return self.send_json({"error": msg}, 500)
            # 分岐先は wait_input.py の使い方も覚えているので、合図はスペースだけでよい
            if src in read_json(IDE_KNOWN, []):
                mark_known(cid)
            if d.get("assign"):
                try:
                    with lock:
                        s = load(d.get("sid", ""))
                        s["setting"]["ide_session"] = cid
                        s["setting"]["ide_title"] = title
                        if s["setting"].get("agents_md"):
                            sync_agents(s)
                        save(s)
                except (FileNotFoundError, ValueError):
                    return self.send_json({"error": "分岐はしたが、チャンネルが見つからず担当にできなかった: " + cid}, 404)
            move = None
            if isinstance(d.get("move_to"), str) and d["move_to"].strip():
                # 分岐先は元と同じフォルダにできるので、別のフォルダへ移す予約をする（実際に移すのは /api/ide/move-run）
                folder = resolve_folder(d["move_to"])
                if any(path_key(w) == path_key(folder) for w in ide_workspaces(src)):
                    msg += "。移す先が元と同じフォルダなので移さない"
                else:
                    with lock:
                        try:
                            ch = load(d.get("sid", ""))["setting"].get("title") or d.get("sid", "")
                        except (FileNotFoundError, ValueError):
                            ch = ""
                        pend = read_json(IDE_MOVES, {})
                        move = pend[cid] = {"folder": folder, "sid": d.get("sid", ""), "title": title, "channel": ch, "ts": now()}
                        write_json(IDE_MOVES, pend)
            print(f"[ide] fork {src[:8]} at {d.get('at')} -> {cid[:8]}" + (f" (move to {move['folder']})" if move else ""))
            return self.send_json({"ok": True, "ide": cid, "title": title, "msg": msg, "move": dict(move, ide=cid) if move else None})
        if p == "/api/ide/branch-fork":
            # チャンネルを分岐したとき（src の id のメッセージまでで sid を作った）、担当の会話も同じ時点で分岐して sid の担当にする
            if not ide_poke or not hasattr(ide_poke, "fork"):
                return self.send_json({"ok": True, "skipped": "この環境では Antigravity の会話を分岐できない（Windows のみ）"})
            try:
                with lock:
                    src = load(d.get("src", ""))
                    new = load(d.get("sid", ""))
            except (FileNotFoundError, ValueError):
                return self.send_json({"error": "no such session"}, 404)
            ide = src["setting"].get("ide_session") or ""
            if not ide:
                return self.send_json({"ok": True, "skipped": "元のチャンネルに担当の会話がない"})
            msgs = src["messages"]
            idx = next((i for i, m in enumerate(msgs) if m["id"] == d.get("id")), -1)
            if idx < 0:
                return self.send_json({"error": "no such message"}, 404)
            try:
                info, turns = ide_poke.turns(ide)
                if info is None:
                    return self.send_json({"ok": True, "skipped": turns})
                at, why = branch_fork_point(msgs, idx, turns)
                if why:
                    return self.send_json({"ok": True, "skipped": why})
                title = "【分岐】" + (new["setting"].get("title") or new["id"])
                cid, msg = ide_poke.fork(ide, at, title)
            except Exception as e:
                cid, msg = None, f"分岐できなかった: {e}"
            if not cid:
                return self.send_json({"error": msg}, 500)
            if ide in read_json(IDE_KNOWN, []):
                mark_known(cid)  # 分岐先も wait_input.py の使い方を覚えている
            with lock:
                n = load(new["id"])
                n["setting"].update(ide_session=cid, ide_title=title, ide_model=src["setting"].get("ide_model") or "")
                if n["setting"].get("agents_md"):
                    sync_agents(n)
                save(n)
            print(f"[ide] branch-fork {ide[:8]} at {at} -> {cid[:8]} for {new['id']}")
            return self.send_json({"ok": True, "ide": cid, "title": title, "at": at, "msg": msg})
        if p == "/api/ide/revert":
            # 発言を書き換えたあと（plan_revert で予定を作った）、担当の会話を書き換えた発言の前まで巻き戻す。
            # 巻き戻したら、次の入力ではチャンネルの設定と会話を全部送り直す
            sid = d.get("sid", "")
            with lock:
                plan = revert_plan.pop(sid, None)
            if not plan:
                return self.send_json({"ok": True, "skipped": "巻き戻す予定がない"})
            ide = plan["ide"]
            if not ide_poke or not hasattr(ide_poke, "revert"):
                return self.send_json({"ok": True, "skipped": "この環境では Antigravity の会話を巻き戻せない（Windows のみ）"})
            try:
                with lock:
                    cur = load(sid)["setting"].get("ide_session")
                    shared = [t for x, t in channels_of(ide) if x != sid]
            except (FileNotFoundError, ValueError):
                return self.send_json({"error": "no such session"}, 404)
            if cur != ide:
                return self.send_json({"ok": True, "skipped": "担当の会話が変わったので巻き戻さない"})
            if shared:
                return self.send_json({"ok": True, "skipped": "この会話はほかのチャンネル（" + "、".join(shared)
                                       + "）も担当にしているので巻き戻さない"})
            reverting.add(ide)
            try:
                info, turns = ide_poke.turns(ide)
                if info is None:
                    return self.send_json({"ok": True, "skipped": turns})
                at, why = branch_fork_point(plan["msgs"], plan["keep"], turns)
                if why:
                    why = why.replace("（新規会話を使う）", "").replace("分岐でき", "巻き戻せ").replace("分岐し", "巻き戻さ")
                    return self.send_json({"ok": True, "skipped": why})
                if at == -1:
                    return self.send_json({"ok": True, "skipped": "書き換えた発言はまだ Antigravity の会話に入っていない"})
                ok, msg = ide_poke.revert(ide, at, REVERT_BACKUP)
            except Exception as e:
                ok, msg = False, f"巻き戻せなかった: {e}"
            finally:
                reverting.discard(ide)
            if not ok:
                return self.send_json({"error": msg}, 500)
            os.makedirs(SENT, exist_ok=True)
            write_json(sent_path(sid), {"ide": "", "msgs": [], "reset": True, "ts": now(),
                                        "why": "担当の会話を巻き戻したので、続きがどこまで残っているか分からない"})
            print(f"[ide] revert {ide[:8]} to step {at} for {sid}")
            return self.send_json({"ok": True, "at": at, "msg": msg})
        if p == "/api/ide/rename":
            # Antigravity の会話の名前を変える。担当にしているチャンネルの控え（ide_title）も合わせる
            ide = d.get("ide") or ""
            title = (d.get("title") or "").strip()
            if not ide or not title:
                return self.send_json({"error": "会話と新しい名前を指定してください"}, 400)
            if not ide_poke or not hasattr(ide_poke, "rename"):
                return self.send_json({"error": "この環境では Antigravity の会話の名前を変えられない（Windows のみ）"}, 400)
            try:
                ok, msg = ide_poke.rename(ide, title)
            except Exception as e:
                ok, msg = False, f"名前を変えられなかった: {e}"
            if not ok:
                return self.send_json({"error": msg}, 500)
            with lock:
                for x, _ in channels_of(ide):
                    s = load(x)
                    s["setting"]["ide_title"] = title
                    save(s)
            print(f"[ide] rename {ide[:8]} -> {title}")
            return self.send_json({"ok": True, "title": title})
        if p == "/api/ide/move":
            # 担当の会話をチャンネルの作業フォルダへ移す予約（cancel で取り消し）。実際に移すのは /api/ide/move-run
            try:
                with lock:
                    s = load(d.get("sid", ""))
                    st = s["setting"]
                    ide = st.get("ide_session") or ""
                    if not ide:
                        return self.send_json({"error": "担当のIDE会話が決まっていません"}, 400)
                    pend = read_json(IDE_MOVES, {})
                    if d.get("cancel"):
                        pend.pop(ide, None)
                        write_json(IDE_MOVES, pend)
                        return self.send_json(dict(moves_info(), ok=True))
                    fixed = not (st.get("ide_folder") or "").strip()
                    folder = channel_folder(s, fix=True)
                    if fixed:
                        save(s)
                    if path_key(folder) == path_key(NEW_IDE_DIR):
                        return self.send_json({"error": f"{folder} はどのチャンネルの会話も使う場所なので、ここへは移さない。作業フォルダを別にする"}, 400)
                    if any(path_key(w) == path_key(folder) for w in ide_workspaces(ide)):
                        return self.send_json({"error": "担当の会話はもうこのフォルダにある"}, 400)
                    pend[ide] = {"folder": folder, "sid": s["id"], "title": st.get("ide_title") or ide[:8],
                                 "channel": st.get("title") or s["id"], "ts": now()}
                    write_json(IDE_MOVES, pend)
                    others = [t for x, t in channels_of(ide) if x != s["id"]]
                return self.send_json(dict(moves_info(), ok=True, others=others, folder=folder))
            except (FileNotFoundError, ValueError):
                return self.send_json({"error": "no such session"}, 404)
        if p == "/api/ide/move-run":
            # 予約した引っ越しをまとめて実行（Antigravity を閉じて、書き換えて、起動し直す）。数十秒かかるので別スレッドで
            if not ide_poke or not hasattr(ide_poke, "move_workspaces"):
                return self.send_json({"error": "この環境では会話を移せない（Windows のみ）"}, 400)
            if move_state.get("running"):
                return self.send_json({"error": "いま移している途中"}, 409)
            if not read_json(IDE_MOVES, {}):
                return self.send_json({"error": "移す予約がない"}, 400)
            try:
                busy = ide_poke.busy_conversations()
            except Exception as e:
                return self.send_json({"error": f"Antigravity の様子を読めなかった: {e}"}, 500)
            if busy is None:
                return self.send_json({"error": "Antigravity が起動していない（起動しておく）"}, 400)
            if busy and not d.get("force"):
                return self.send_json({"busy": busy})
            move_state.clear()
            move_state.update(running=True, msg="Antigravity を閉じて移している…", ts=now())
            threading.Thread(target=run_moves, args=(bool(d.get("force")),), daemon=True).start()
            return self.send_json({"ok": True, "started": True})
        with lock:
            if p == "/api/app-settings":
                cur = read_json(APP_SETTINGS, {})
                for k in DEFAULT_APP:
                    if isinstance(d.get(k), str) and isinstance(DEFAULT_APP[k], str):
                        cur[k] = d[k].strip()
                    elif isinstance(d.get(k), bool) and isinstance(DEFAULT_APP[k], bool):
                        cur[k] = d[k]
                    elif isinstance(d.get(k), list) and isinstance(DEFAULT_APP[k], list):
                        cur[k] = list(dict.fromkeys(x.strip() for x in str_list(d[k])))  # 同じものは1つに
                write_json(APP_SETTINGS, cur)
                return self.send_json({"ok": True, "app": app_settings()})
            if p == "/api/file-presets":
                name = d.get("name")
                if not isinstance(name, str) or not name.strip():
                    return self.send_json({"error": "name is required"}, 400)
                preset = {"id": new_id(), "name": name.strip(), "createdAt": now(),
                          "files": str_list(d.get("files")), "extra": d.get("extra") if isinstance(d.get("extra"), str) else ""}
                presets = read_json(PRESETS, [])
                presets.append(preset)
                write_json(PRESETS, presets)
                return self.send_json({"ok": True, "preset": preset})
            if p == "/api/file-presets/delete":
                presets = read_json(PRESETS, [])
                gone = [x for x in presets if x.get("id") == d.get("id")]
                if not gone:
                    return self.send_json({"error": "preset not found"}, 404)
                os.makedirs(TRASH, exist_ok=True)
                write_json(os.path.join(TRASH, f"filepreset_{gone[0]['id']}_{int(time.time())}.json"), gone[0])
                write_json(PRESETS, [x for x in presets if x.get("id") != d.get("id")])
                return self.send_json({"ok": True})
            if p == "/api/char-presets":
                name = (d.get("name") or "").strip() if isinstance(d.get("name"), str) else ""
                tags = ", ".join(split_tags(d.get("tags") if isinstance(d.get("tags"), str) else ""))
                if not name or not tags:
                    return self.send_json({"error": "名前とタグが必要です"}, 400)
                presets = char_presets()
                cur = find_char(presets, name)  # 同じ名前なら上書き
                if cur:
                    cur.update(name=name, tags=tags, updatedAt=now())
                else:
                    cur = {"id": new_id(), "name": name, "tags": tags, "createdAt": now()}
                    presets.append(cur)
                write_json(CHAR_PRESETS, presets)
                return self.send_json({"ok": True, "preset": cur})
            if p == "/api/char-presets/delete":
                presets = char_presets()
                gone = [x for x in presets if x.get("id") == d.get("id")]
                if not gone:
                    return self.send_json({"error": "preset not found"}, 404)
                os.makedirs(TRASH, exist_ok=True)
                write_json(os.path.join(TRASH, f"charpreset_{gone[0]['id']}_{int(time.time())}.json"), gone[0])
                write_json(CHAR_PRESETS, [x for x in presets if x.get("id") != d.get("id")])
                return self.send_json({"ok": True})
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
                    os.makedirs(TRASH, exist_ok=True)
                    shutil.move(rag_path(col["id"]), os.path.join(TRASH, f"rag_{col['id']}_{int(time.time())}.json"))
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
                gone = [e for e in entries if e.get("id") == entry_id]
                if not gone:
                    return self.send_json({"error": "no such entry"}, 404)
                os.makedirs(TRASH, exist_ok=True)
                write_json(os.path.join(TRASH, f"ragentry_{col['id']}_{int(time.time())}.json"), gone[0])
                col["entries"] = [e for e in entries if e.get("id") != entry_id]
                save_rag(col)
                return self.send_json({"ok": True})
            if p == "/api/session/new":
                base = d.get("setting")
                if isinstance(base, dict):  # 今の設定を引き継いで始める。メモ・画像のキャラ固定・担当IDEは持ち越さない
                    base = dict(base, memo="", chars=[], **NO_IDE, **NO_FOLDER)
                else:
                    base = None
                if d.get("copy_from"):
                    try:
                        base = load(d["copy_from"])["setting"]
                        base = dict(base, title=base.get("title", "") + " (コピー)", memo="", ide_auto=False, **NO_FOLDER)
                    except Exception:
                        pass
                if d.get("title"):
                    base = dict(base or {}, title=d["title"])
                s = create_session(base)
                return self.send_json({"ok": True, "sid": s["id"]})
            if p == "/api/session/import":
                try:
                    st, msgs = clean_import(d.get("data"))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                st["title"] = (st.get("title") or "取り込み") + " (取込)"
                s = create_session(st, msgs)
                return self.send_json({"ok": True, "sid": s["id"]})
            try:
                s = load(d.get("sid", ""))
            except (FileNotFoundError, ValueError):
                return self.send_json({"error": "no such session"}, 404)
            sid = s["id"]

            if p == "/api/session/delete":
                old = read_json(AGENTS_LOG, {}).get(sid)
                if old:  # 消したチャンネルのルールが作業フォルダに残らないように
                    retire_agents(sid, old)
                os.makedirs(TRASH, exist_ok=True)
                shutil.move(spath(sid), os.path.join(TRASH, f"{sid}_{int(time.time())}.json"))
                return self.send_json({"ok": True})
            if p == "/api/session/branch":
                idx = next((i for i, m in enumerate(s["messages"]) if m["id"] == d.get("id")), -1)
                if idx < 0:
                    return self.send_json({"error": "no such message"}, 404)
                st, msgs = clean_import({"setting": s["setting"], "messages": copy.deepcopy(s["messages"][:idx + 1])})
                st["title"] = (s["setting"].get("title") or "") + " (分岐)"
                n = create_session(st, msgs)
                return self.send_json({"ok": True, "sid": n["id"]})
            if p == "/api/session/raw":
                try:
                    st = merge_defaults(d.get("setting"))
                    msgs = d.get("messages")
                    if not isinstance(msgs, list) or any(not isinstance(m, dict) or m.get("role") not in ("user", "assistant")
                                                         or not isinstance(m.get("text", ""), str) for m in msgs):
                        raise ValueError("messages が不正です")
                except (ValueError, TypeError, AttributeError) as e:
                    return self.send_json({"error": str(e)}, 400)
                backup(s, "rawedit")
                for m in msgs:
                    m.setdefault("id", new_id())
                    m.setdefault("ts", now())
                s["setting"] = st
                s["messages"] = msgs
                save(s)
                return self.send_json({"ok": True})
            if p == "/api/context":
                # wait_input.py が受け取った入力の、AIに見せる内容（全部か差分）
                m = find_msg(s, d.get("id"))
                if not m or m["role"] != "user":
                    return self.send_json({"error": "no such user message"}, 404)
                return self.send_json(build_context(s, m))
            if p == "/api/context/reset":
                # 次の入力では設定と会話を全部送る（AIが忘れていそうなとき用）
                os.makedirs(SENT, exist_ok=True)
                write_json(sent_path(s["id"]), {"ide": "", "msgs": [], "reset": True, "ts": now()})
                return self.send_json({"ok": True})
            if p == "/api/upload":
                # 発言に付けるファイル。送信するまではどの発言にも付かない（送信で files に入れる）
                data = d.get("data")
                if not isinstance(data, str) or not data:
                    return self.send_json({"error": "data is required"}, 400)
                try:
                    raw = base64.b64decode(data.split(",", 1)[-1] if data.startswith("data:") else data, validate=True)
                except (ValueError, binascii.Error):
                    return self.send_json({"error": "data must be base64"}, 400)
                if len(raw) > UPLOAD_MAX:
                    return self.send_json({"error": f"ファイルが大きすぎます（{UPLOAD_MAX // 1024 // 1024}MB まで）"}, 400)
                return self.send_json({"ok": True, "file": save_upload(sid, d.get("name"), raw)})
            if p == "/api/send":
                text = (d.get("text") or "").strip()
                try:
                    files = clean_files(sid, d.get("files"))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                if not text and not files:
                    return self.send_json({"error": "empty"}, 400)
                m = {"id": new_id(), "role": "user", "text": text, "ts": now(), "status": "pending"}
                if files:
                    m["files"] = files
                s["messages"].append(m)
                save(s)
                return self.send_json({"ok": True, "id": m["id"]})
            if p == "/api/message/alter":
                m = find_msg(s, d.get("id"))
                text = d.get("text")
                if not m or m.get("role") != "assistant":
                    return self.send_json({"error": "no such assistant message"}, 404)
                if not isinstance(text, str) or not text.strip():
                    return self.send_json({"error": "text is required"}, 400)
                rv = text.strip() != m["text"] and plan_revert(s, s["messages"].index(m) - 1)
                m.setdefault("original_text", m["text"])
                m["text"] = text.strip()
                m["edited"] = True
                save(s)
                return self.send_json({"ok": True, "revert": rv})
            if p == "/api/message/state":
                # 応答のときのキャラの状態を手で直す（次の入力でそのまま AI に見せるので、巻き戻しはいらない）
                m = find_msg(s, d.get("id"))
                if not m or m.get("role") != "assistant":
                    return self.send_json({"error": "no such assistant message"}, 404)
                if not isinstance(d.get("state"), list):
                    return self.send_json({"error": "state must be a list"}, 400)
                m["state"] = merge_state([], [u for u in clean_state(d["state"]) if not u.get("remove")])
                save(s)
                return self.send_json({"ok": True, "state": m["state"]})
            if p in ("/api/message/edit", "/api/message/regenerate", "/api/message/truncate"):
                idx = next((i for i, m in enumerate(s["messages"]) if m["id"] == d.get("id")), -1)
                if idx < 0:
                    return self.send_json({"error": "no such message"}, 404)
                new_user, rv = None, False
                if p == "/api/message/truncate":
                    backup(s, "truncate")
                    rv = idx + 1 < len(s["messages"]) and plan_revert(s, idx)
                    s["messages"] = s["messages"][:idx + 1]
                elif p == "/api/message/edit":
                    old = s["messages"][idx]
                    text = d.get("text")
                    if old["role"] != "user":
                        return self.send_json({"error": "not a user message"}, 400)
                    if not isinstance(text, str) or not text.strip():
                        return self.send_json({"error": "text is required"}, 400)
                    backup(s, "edit")
                    rv = plan_revert(s, idx - 1)
                    new_user = {"id": new_id(), "role": "user", "text": text.strip(), "ts": now(), "status": "pending"}
                    if old.get("files"):
                        new_user["files"] = old["files"]
                    s["messages"] = s["messages"][:idx] + [new_user]
                else:
                    if s["messages"][idx]["role"] != "assistant":
                        return self.send_json({"error": "not an assistant message"}, 400)
                    user_idx = next((i for i in range(idx - 1, -1, -1) if s["messages"][i]["role"] == "user"), -1)
                    if user_idx < 0:
                        return self.send_json({"error": "no preceding user message"}, 400)
                    backup(s, "regenerate")
                    rv = plan_revert(s, user_idx - 1)
                    s["messages"] = s["messages"][:user_idx + 1]
                    new_user = s["messages"][-1]
                    new_user["id"] = new_id()
                    new_user["status"] = "pending"
                    new_user.pop("pasted", None)  # 担当の会話は巻き戻すので、画像はもう一度貼る
                    new_user["ts"] = now()
                save(s)
                return self.send_json({"ok": True, "id": new_user["id"] if new_user else None, "revert": rv})
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
            if p == "/api/ide/assign":
                ide, title = d.get("ide", ""), d.get("title", "")
                if not isinstance(ide, str) or not isinstance(title, str) or not all(c.isalnum() or c == "-" for c in ide):
                    return self.send_json({"error": "bad ide session"}, 400)
                # ほかのチャンネルがもうこの会話を担当にしていたら、どこかを知らせて確かめる（force でそのままつなぐ）
                others = []
                if ide and ide != s["setting"].get("ide_session"):
                    for x, t in channels_of(ide):  # 同じ名前のチャンネルもあるので、プロジェクトとIDも付ける
                        if x != s["id"]:
                            pj = load(x)["setting"].get("project") or ""
                            others.append(f"{t}（{pj + '・' if pj else ''}{x[:6]}）")
                if others and not d.get("force"):
                    return self.send_json({"conflict": others})
                s["setting"]["ide_session"] = ide
                s["setting"]["ide_title"] = title if ide else ""
                s["setting"]["ide_auto"] = False  # 自分で選んだ・外したので、あとから新規会話を自動で付けない
                if s["setting"].get("agents_md"):
                    sync_agents(s)  # AGENTS.md に書いてある担当の会話IDを書き換える
                save(s)
                return self.send_json({"ok": True})
            if p == "/api/session/rename":
                # チャンネルの名前だけ変える（作業フォルダは決まっていれば動かない）
                title = (d.get("title") or "").strip() if isinstance(d.get("title"), str) else ""
                if not title:
                    return self.send_json({"error": "新しい名前を入れてください"}, 400)
                if not (s["setting"].get("ide_folder") or "").strip() and s["setting"].get("ide_session"):
                    channel_folder(s, fix=True)  # 担当の会話がある間は、名前を変えてもいまのフォルダのまま
                s["setting"]["title"] = title
                if s["setting"].get("agents_md"):
                    sync_agents(s)  # AGENTS.md にチャンネル名が入っている
                save(s)
                return self.send_json({"ok": True, "title": title})
            if p == "/api/ide/model":
                # 合図を送るときのモデル。空なら会話の最後の入力と同じ
                model = d.get("model") or ""
                if not isinstance(model, str):
                    return self.send_json({"error": "model must be a string"}, 400)
                s["setting"]["ide_model"] = model.strip()
                save(s)
                return self.send_json({"ok": True, "model": s["setting"]["ide_model"]})
            if p == "/api/claim":
                m = find_msg(s, d.get("id"))
                if m and m.get("status") == "pending":
                    m["status"] = "processing"
                    m["claimed_ts"] = now()  # チャンネルを分岐するとき、担当の会話をどこで分けるかの手がかり
                    claimed_at[m["id"]] = time.time()
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
                if not find_msg(s, d.get("id")):
                    return self.send_json({"error": "no such message"}, 404)
                backup(s, "delete")
                rv = plan_revert(s, s["messages"].index(find_msg(s, d.get("id"))) - 1)
                s["messages"] = [m for m in s["messages"] if m["id"] != d.get("id")]
                save(s)
                return self.send_json({"ok": True, "revert": rv})
            if p == "/api/chars/upsert":
                c = d.get("char") if isinstance(d.get("char"), dict) else {}
                name = (c.get("name") or "").strip() if isinstance(c.get("name"), str) else ""
                tags = ", ".join(split_tags(c.get("tags") if isinstance(c.get("tags"), str) else ""))
                if not name or not tags:
                    return self.send_json({"error": "名前とタグが必要です"}, 400)
                chars = s["setting"].setdefault("chars", [])
                cur = next((x for x in chars if c.get("id") and x.get("id") == c["id"]), None)
                same = find_char(chars, name)
                if same and same is not cur:
                    return self.send_json({"error": f"「{name}」はもう登録されています"}, 400)
                if not cur:
                    cur = {"id": new_id()}
                    chars.append(cur)
                cur.update(name=name, tags=tags, locked=bool(c.get("locked")),
                           source=c.get("source") if c.get("source") in ("user", "preset") else "user")
                save(s)
                return self.send_json({"ok": True, "char": cur})
            if p == "/api/chars/delete":
                chars = s["setting"].get("chars") or []
                if not any(x.get("id") == d.get("id") for x in chars):
                    return self.send_json({"error": "no such character"}, 404)
                backup(s, "chardelete")
                s["setting"]["chars"] = [x for x in chars if x.get("id") != d.get("id")]
                save(s)
                return self.send_json({"ok": True})
            if p == "/api/setting":
                new = d.get("setting", s["setting"])
                if not isinstance(new, dict):
                    return self.send_json({"error": "setting must be an object"}, 400)
                # 固定キャラ（AI が応答で足す）と担当IDE（専用の操作で変える）はフォームから上書きさせない
                keep = {k: s["setting"].get(k, v) for k, v in NO_IDE.items()}
                keep["chars"] = s["setting"].get("chars") or []
                s["setting"] = merge_defaults(dict(new, **keep))
                sync_agents(s)  # オンなら書き出し、オフにしたなら前に書いたものを片付ける
                save(s)
                a = agents_state.get(sid)
                return self.send_json({"ok": True, "agents": a})
            if p == "/api/reset":
                backup(s, "reset")
                s["messages"] = []
                s["setting"]["memo"] = ""
                fm = s["setting"].get("first_message")
                if fm:
                    s["messages"].append({"id": new_id(), "role": "assistant", "text": fm, "ts": now()})
                save(s)
                return self.send_json({"ok": True})
        self.send_error(404)


def already_running():
    """同じ data/ を使うサーバーがもう動いていればその番号を返す（番号が違っても outbox を取り合うので2台目は起動しない）"""
    last = read_json(RUNNING, {})
    ports = [PORT, last.get("port") if isinstance(last, dict) else None]
    for port in dict.fromkeys(p for p in ports if isinstance(p, int)):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=1.5).read()
            return port
        except Exception:
            pass
    return None


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for d in (SESS_DIR, RAG_DIR, TRASH, SENT, DONE):
        os.makedirs(d, exist_ok=True)
    port = already_running()
    if port:
        print(f"RP Image Chat はもう起動しています: http://127.0.0.1:{port}/ （2台目は起動しません）")
        print(f"AI側は python wait_input.py で待機してください（つなぎ先 {port}）")
        sys.exit(1)
    write_json(RUNNING, {"port": PORT, "pid": os.getpid(), "ts": now()})
    # 旧形式の data/session.json は取り込まずに退避だけする
    if os.path.exists(LEGACY):
        shutil.move(LEGACY, os.path.join(DATA, f"session_legacy_{int(time.time())}.json"))
        print("[init] 旧 session.json を退避しました")
    if not list_ids():
        create_session({"title": "チャンネル1", "ide_auto": True})
    threading.Thread(target=outbox_worker, daemon=True).start()
    threading.Thread(target=image_worker, daemon=True).start()
    threading.Thread(target=poke_worker, daemon=True).start()
    threading.Thread(target=ide_auto_worker, daemon=True).start()
    host = bind_host()
    setup_access(host)
    setup_phone(host)
    print(f"RP Image Chat: http://{host}:{PORT}/")
    if host != "127.0.0.1":
        # LANモードでも PC 上の wait_input.py（127.0.0.1 宛て）が届くように、ローカルでも待ち受ける
        local = ThreadingHTTPServer(("127.0.0.1", PORT), H)
        threading.Thread(target=local.serve_forever, daemon=True).start()
    ThreadingHTTPServer((host, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
