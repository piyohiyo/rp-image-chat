# RP Image Chat

外部のAIとJSONファイルでメッセージを受け渡しし、ローカルのComfyUIで場面画像を生成する、シンプルなロールプレイ用チャットアプリです。サーバーはPython標準ライブラリだけで動きます。

画面はPCでも、同じLANにつないだスマホでも開けます。会話履歴とRAGのデータはPCに保存されます。設定でAntigravity IDEの会話セッションを選ぶと、そのセッションに履歴ファイルを添付して引き継ぎ、入力をIDEへ送り、画面に表示された応答をチャットへ取り込みます。IDEのログイン情報やAPIキーは使わず、起動中のAntigravity IDEの画面をローカル接続で操作します。

## 動作の流れ

1. ブラウザから送信した入力を`server.py`が受け取り、`data/sessions/<セッションID>.json`に保存します。
2. 設定からAntigravity IDEのセッションを選びます。セッションを切り替えると、それまでの全会話、チャンネル設定、選択中のRAG資料を`data/handoffs/`内のMarkdownに書き、選択先のIDE会話へ添付します。
3. チャットから送信すると、そのIDEセッションに入力が送られます。IDEの応答が安定した時点でRP Chatにも取り込みます。
4. IDE連携を設定しない場合は、従来どおり`wait_input.py`と`outbox/`を使う外部エージェント連携もできます。

`wait_input.py`は入力を1件受け取ると終了します。次の入力を待つときは再実行してください。複数のAI側プロセスで同時に待機させる場合は、受け取りの重複を避けるための調整が必要です。

## 必要なもの

- Python 3.10以降。チャットサーバーに追加のpipパッケージは不要です。
- Antigravity IDE連携を使う場合はNode.js 20以降と、起動中のAntigravity IDEが必要です。IDEのローカルDevTools接続口を使います。連携を使わない場合、Node.jsは不要です。
- 画像を生成する場合は、起動済みの[ComfyUI](https://github.com/comfyanonymous/ComfyUI)。テキストだけの応答ならComfyUIなしでも使えます。
- ComfyUIに導入したチェックポイント。ファイル名を各チャンネルの画像設定で指定します。IPAdapterを使う場合は対応するComfyUIのカスタムノードも必要です。
- 外部エージェント方式を使う場合は、`outbox/`へ応答を書き込むAIエージェントまたはスクリプト。

## 起動方法

リポジトリを取得し、そのフォルダで次を実行します。

```powershell
$env:COMFYUI_OUTPUT_DIR = 'D:\ComfyUI\output' # 自分のComfyUIの出力フォルダに変更
python .\server.py
```

PCのブラウザで <http://127.0.0.1:8199/> を開きます。ComfyUIのAPIは既定で <http://127.0.0.1:8188/> を使います。

環境変数で設定を変更できます。

| 環境変数 | 初期値 | 内容 |
| --- | --- | --- |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUIのAPIアドレス |
| `COMFYUI_OUTPUT_DIR` | このリポジトリから見た`../ComfyUI/output` | ComfyUIの画像出力フォルダ |
| `RPCHAT_PORT` | `8199` | チャットサーバーのポート |
| `RPCHAT_HOST` | `127.0.0.1` | 待受アドレス。`lan`を指定するとPCのLAN用IPv4アドレスを選びます |

Antigravity連携は、Node.jsの`WebSocket`と`C:\Users\<ユーザー名>\AppData\Roaming\Antigravity\DevToolsActivePort`でIDE画面に接続します。IDEを再起動した場合も、接続情報は起動時に読み直します。セッション切替の引き継ぎファイルはローカルの`data/handoffs/`に残り、Gitの公開対象には入りません。

スマホから開く場合は、サーバー起動前に`RPCHAT_HOST=lan`を設定します。Windowsなら`start_mobile.bat`でも起動できます。サーバーに表示されたURLを、同じLANにつないだスマホで開いてください。LAN内で認証なしに使える設定です。信頼できるネットワークで使用してください。

## AI応答のJSON形式

`wait_input.py`が入力を表示したら、出力に含まれる`session`と`reply_to`の値を使い、UTF-8のJSONファイルを作ります。

```json
{
  "session": "<セッションID>",
  "reply_to": "<ユーザー入力ID>",
  "text": "AIの応答本文",
  "memo": "任意の進行メモ",
  "image": {
    "prompt": "1girl, smiling, cafe",
    "width": 896,
    "height": 1152,
    "raw": false
  }
}
```

`image`は省略できます。`raw: false`では、チャンネル設定の共通タグとキャラクタータグをプロンプトの前に追加します。`raw: true`では、指定したプロンプトをそのまま使います。ネガティブプロンプトを省略した場合は、チャンネルの画像設定を使います。

## 主な機能

- 複数チャンネルと、キャラクター・舞台・指示・メモ・画像設定の管理
- 過去のユーザー入力の編集と再応答、AI応答の改変と再生成
- チャンネルごとに選択できるRAGコレクション
- 画像履歴、別シードでの再生成、プロンプト編集、最新画像を背景にするRP表示

会話履歴、RAGデータ、取り込み済みの応答は`data/`と`outbox/`に保存されます。この2つのフォルダはGitの公開対象から除外しています。
