"""
AI側の待機用。どれかのチャンネルに新しい入力が来るまで待ち、
来たらそのチャンネルの概要・指示・AIメモ・キャラ設定・直近の会話を表示して終了する。
  python wait_input.py
応答は outbox/<session>_<任意>.json に書く:
  {"session": "...", "reply_to": "...", "text": "...",
   "image": {"prompt": "...", "width": 896, "height": 1152},   # 任意
   "memo": "これまでの進行・重要な事実（毎回まるごと書き直す）"}   # 任意
"""
import json, os, sys, time, urllib.request
from server import rag_context, bind_host, access_token, AUTH_REQUIRED

host = bind_host()
if host in ("0.0.0.0", "::"):
    host = "127.0.0.1"
URL = os.environ.get("RPCHAT_AGENT_URL", "http://" + host + ":" + os.environ.get("RPCHAT_PORT", "8199"))
HEADERS = {"X-RPChat-Token": access_token()} if AUTH_REQUIRED else {}
CONTEXT = 10
sys.stdout.reconfigure(encoding="utf-8")


def get(p):
    req = urllib.request.Request(URL + p, headers=HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def post(p, d):
    req = urllib.request.Request(URL + p, data=json.dumps(d).encode("utf-8"), headers={"Content-Type": "application/json", **HEADERS})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def section(name, body):
    body = (body or "").strip()
    if body:
        print(f"--- {name} ---")
        print(body)


while True:
    try:
        r = get("/api/pending")
        m, sid = r.get("pending"), r.get("session")
        if m:
            post("/api/claim", {"sid": sid, "id": m["id"]})
            s = get(f"/api/session?sid={sid}")
            st = s["setting"]
            ch = st.get("character", {})
            ic = st.get("image", {})
            msgs = s["messages"]
            idx = next(i for i, x in enumerate(msgs) if x["id"] == m["id"])

            print("=== NEW INPUT ===")
            print(f"session : {sid}")
            print(f"title   : {st.get('title')}")
            print(f"reply_to: {m['id']}")
            print(f"outbox  : outbox/{sid}_{int(time.time())}.json")
            section("指示 (instructions)", st.get("instructions"))
            section("概要 (summary)", st.get("summary"))
            section("AIメモ (memo)", st.get("memo"))
            section("RAG参照情報", rag_context(st))
            chara = "\n".join(f"{k}: {v}" for k, v in [
                ("user", st.get("user_name")), ("name", ch.get("name")), ("age", ch.get("age")),
                ("profile", ch.get("profile")), ("speech", ch.get("speech")), ("scenario", st.get("scenario")),
            ] if v)
            section("キャラ/舞台", chara)
            section("画像ベース (自動で前置される)", f"character_tags: {ic.get('character_tags','')}\nsize: {ic.get('width')}x{ic.get('height')}")
            print(f"--- recent context ({min(idx, CONTEXT)}/{idx}) ---")
            for x in msgs[max(0, idx - CONTEXT):idx]:
                t = x["text"].replace("\n", " ")
                print(f"[{x['role']}] {t[:500]}")
                if x.get("image"):
                    print(f"   (image: {x['image'].get('prompt','')[:200]})")
            print("--- user ---")
            print(m["text"])
            sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        pass
    time.sleep(1.5)
