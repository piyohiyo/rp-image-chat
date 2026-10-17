"""
AI側の待機用。どれかのチャンネルに新しい入力が来るまで待ち、
来たらそのチャンネルの設定・AIメモ・固定キャラ・会話・発言をそのまま表示して終了する。
担当IDEの会話が前回と同じなら、増えた発言と変わった設定だけを表示する（前に表示したものは会話に残っているので）。
担当が切り替わった・初めて・過去が書き換えられたときは全部表示する。
  python wait_input.py
  python wait_input.py --ide <Antigravityの会話ID>   # UIでこの会話を担当にしたチャンネルだけ受け取る
  python wait_input.py --ide <会話ID> --wait 60      # 待つのは60秒まで（--wait なしは30秒）
担当IDE会話があるチャンネルでは、入力から2秒後にその会話へスペース1文字（合図）が送られる（会話IDを指定して送る）。
AIはそれを合図に wait_input.py を実行し、応答を書いたら待機せずにスペース1文字だけ返してターンを終える。
（--ide なしの場合は、担当IDE会話が指定されていないチャンネルだけを受け取る。このときは毎回全部表示する）
どちらでも、応答を書いたあとに wait_input.py で次の入力を待ち続けることはさせない。
待つ間も AI が何度も様子を見に動くので、入力がなくても使用量が減っていくため。
応答の書き方は「応答の書き方」の項に出る:
  outbox/<session>_<任意>.json に
  {"session": "...", "reply_to": "...", "text": "...", "memo": "",
   "image": {"prompt": "...", "characters": ["キャラ名"]},   # 任意
   "characters": [{"name": "キャラ名", "tags": "外見タグ"}]}  # 任意
"""
import json, os, sys, time, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
OUTBOX = os.path.join(HERE, "outbox")
DONE = os.path.join(OUTBOX, "done")
CTX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ctx")  # 長い内容の書き出し先（チャンネルごとに上書き）
LONG, LONG_LINES = 800, 25  # これより長いと表示せずファイルに書く
IDE = sys.argv[sys.argv.index("--ide") + 1] if "--ide" in sys.argv[:-1] else ""
WAIT = float(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv[:-1] else 30
WAIT = WAIT if WAIT > 0 else 30  # 待ち続けはしない
sys.stdout.reconfigure(encoding="utf-8")


def server_url():
    """つなぎ先。RPCHAT_PORT がなければ、このフォルダで起動中のサーバーの番号（data/server.json）を使う。
    合図のコマンドには番号が入らないので、8200 以外で起動しても別のアプリにつながないように"""
    port = os.environ.get("RPCHAT_PORT")
    if not port:
        try:
            with open(os.path.join(HERE, "data", "server.json"), encoding="utf-8") as f:
                port = str(int(json.load(f)["port"]))
        except Exception:
            port = "8200"
    return "http://127.0.0.1:" + port


def get(p):
    return json.loads(urllib.request.urlopen(server_url() + p, timeout=10).read())


def post(p, d):
    req = urllib.request.Request(server_url() + p, data=json.dumps(d).encode("utf-8"), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def show(sid, m, note=""):
    s = get(f"/api/session?sid={sid}")
    ctx = post("/api/context", {"sid": sid, "id": m["id"]})

    print("=== NEW INPUT ===")
    if note:
        print(f"!!! {note}")
    outbox = os.path.join(OUTBOX, f"{sid}_{int(time.time())}.json")
    print(f"session : {sid}")
    print(f"title   : {s['setting'].get('title')}")
    print(f"reply_to: {m['id']}")
    if ctx["mode"] == "full":
        print(f"内容    : 全部（{ctx['reason']}）。これまでに覚えているこのチャンネルの内容より、下の内容を正とする")
        if ctx.get("files"):
            print("「ファイル:」とある項目は、応答を書く前にそのファイルを全部読む")
    elif ctx.get("light"):
        print("内容    : 前回の続き。設定・会話はこの会話でもう見たものを使う。下は今回の発言（と、変わった設定があればその分）だけ")
        if ctx.get("files"):
            print("「ファイル:」とある項目は、応答を書く前にそのファイルを全部読む")
    else:
        print("内容    : 前回からの続き。設定と会話は前に表示したものをそのまま使い、増えた分・変わった分だけ下に出す")
    # 足りない情報（キャラが決まっていないなど）をフォルダやファイルを漁って探しに行かないように、毎回出す
    print("決まり  : 読むのはこの表示と、この表示が読めと指定したファイルだけ。ほかのフォルダの一覧・ファイル・API・プロセスは見ない（ユーザーの発言で頼まれたときを除く）。"
          "分からないこと（演じるキャラが決まっていない等）は探さずに、応答の text でユーザーに短く質問する（このときだけメタな質問でよい）")
    # 思考に入力や応答の本文を書き写すと、そのぶん時間とトークンを使うので、毎回出す
    print("思考    : 思考では、この表示の内容（設定・会話・ユーザーの発言）も、これから書く応答の本文も、復唱・書き写しをしない。"
          "触れる必要があるときは短い要約にとどめる。応答の本文は outbox の JSON に直接書く")
    for x in ctx.get("missing") or []:
        print(f"!!! 設定ファイルが読めなかった: {x}")
    body = "".join(f"\n--- {sec['label']} ---\n{sec['text']}\n" for sec in ctx["sections"])
    if ctx["same"]:
        body += "\n（前回と同じなので省略: " + "、".join(ctx["same"]) + "）\n"
    # light はファイルの設定と長い会話を場所で渡すので、その場で表示する
    if not ctx.get("light") and (len(body) > LONG or body.count("\n") > LONG_LINES):
        # 長い出力は Antigravity が途中を省略して AI に見せない。ファイルに書いて読ませる
        os.makedirs(CTX_DIR, exist_ok=True)
        path = os.path.join(CTX_DIR, f"{sid}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {s['setting'].get('title')} / reply_to {m['id']}\n{body}")
        print(f"\n設定・会話・発言はファイルに書いた（{body.count(chr(10)) + 1}行）。このコマンドの表示ではなく、必ずこのファイルを最初から最後まで読んでから応答する:")
        print(f"  {path}")
        print("（/api/context などを自分で呼んで集め直さないこと。呼ぶと送った扱いになり、次からは差分しか返らない）")
        for sec in ctx["sections"]:
            if sec["label"].startswith("ユーザーが今回の発言に添付"):  # 添付はここでも見せる
                print(f"\n--- {sec['label']} ---\n{sec['text']}")
        last = ctx["sections"][-1] if ctx["sections"] else None
        if last and len(last["text"]) <= 1500:
            print(f"\n--- {last['label']} ---\n{last['text']}")
    else:
        print(body)
    print()
    print(f"応答は {outbox} に JSON で書く（書き方は「応答の書き方」のとおり）")
    me = f'python "{os.path.abspath(__file__)}" --ide {IDE}'
    if IDE:
        print("書いたら待機はしないで、このターンを終える。この会話への返事は半角スペース1文字だけにする（問題があったときだけ、その内容を書く）")
        print("wait_input.py をバックグラウンドで常駐させないこと（受け取った入力を誰も読めなくなる）")
        print(f"次の入力があると、チャットからこの会話にスペース1文字（合図）が届く。届いたら {me} --wait 30 を実行する（前回の応答が届いたかもそこで確認される）")
    else:
        print("書いたら wait_input.py をもう一度実行せずに、このターンを終える（待ち続けると、入力がなくても使用量が減っていく）。"
              "次の入力が来たら、ユーザーがこの会話で知らせる")
    # チャンネルで「入力のたびに最後にルールを付ける」がオンなら、いちばん最後に出す（長い会話でも忘れないように）
    if ctx.get("rules"):
        print(f"\n--- {ctx['rules']['label']} ---\n{ctx['rules']['text']}")
    sys.exit(0)


def check_previous():
    """前回書いた応答が本当に取り込まれたか確かめる。
    取り込めなかった応答ファイルの理由と、受け取ったまま応答が届いていない入力を返す"""
    # 書いたばかりの応答をサーバーが取り込むのを待つ（壊れたJSONは10秒後に .bad へ移される）
    for _ in range(15):
        if not any(n.lower().endswith(".json") for n in os.listdir(OUTBOX)):
            break
        time.sleep(1)
    time.sleep(1.5)
    all_ids = [x["id"] for x in get("/api/sessions")["sessions"]]
    mine = [s for s in (get(f"/api/session?sid={i}") for i in all_ids) if s["setting"].get("ide_session", "") == IDE]
    ids = {s["id"] for s in mine}

    def ours(name):  # 応答ファイル名は <session>_<任意>.json
        head = name.split("_")[0]
        return head in ids or head not in all_ids

    warn = []
    for n in sorted(os.listdir(OUTBOX)):
        if os.path.isfile(os.path.join(OUTBOX, n)) and ours(n):
            warn.append(f"{n}: まだ取り込まれていない" if n.lower().endswith(".json")
                        else f"{n}: 拡張子が .json ではないので取り込まれない")
    reasons = {".bad": "JSONとして読めない", ".stale": "reply_to の入力が応答待ちではない（IDが違う/もう応答済み）",
               ".nosession": "session が存在しない"}
    if os.path.isdir(DONE):
        for n in sorted(os.listdir(DONE)):
            p = os.path.join(DONE, n)
            ext = os.path.splitext(n)[1]
            if ext in reasons and time.time() - os.path.getmtime(p) < 600 and ours(n):
                why = reasons[ext]
                if ext == ".bad":
                    try:
                        with open(p, encoding="utf-8-sig") as f:
                            json.load(f)
                    except Exception as e:
                        why += f" ({e})"
                warn.append(f"{n}: 取り込み失敗 - {why}")

    for s in mine:
        for m in s["messages"]:
            if m["role"] == "user" and m.get("status") == "processing":
                return warn, (s["id"], m)
    # 届いていない入力がなければ、過去の失敗は解決済みなので outbox に残っている分だけ伝える
    return [w for w in warn if "取り込み失敗" not in w], None


try:
    warn, left = check_previous()
except Exception:
    warn, left = [], None
if warn:
    print("=== 前回の応答ファイルについて ===")
    for w in warn:
        print(f"- {w}")
    print(f"（outbox の場所: {OUTBOX}）")
if left:
    show(*left, note="この入力は受け取り済みだが、応答がチャットに届いていない。もう一度 outbox に書き直すこと")

told = False
start = time.time()
while True:
    if time.time() - start > WAIT:
        print(f"（{WAIT:g}秒待ったが新しい入力はない。もう一度実行せずに、" +
              ("この会話への返事は半角スペース1文字だけにして終える）" if IDE else "このターンを終える）"))
        sys.exit(0)
    try:
        r = get("/api/pending" + ("?ide=" + urllib.parse.quote(IDE) if IDE else ""))
        m, sid = r.get("pending"), r.get("session")
        if m:
            post("/api/claim", {"sid": sid, "id": m["id"]})
            show(sid, m)
    except SystemExit:
        raise
    except Exception:
        if not told:  # 一度だけ伝える。サーバーはユーザーが起動するもの（2台目を立てると outbox を取り合う）
            print(f"（{server_url()} につながらない。server.py の起動を待っている。server.py を自分で起動しないこと）", flush=True)
            told = True
    time.sleep(1.5)
