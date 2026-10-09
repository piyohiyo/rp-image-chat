# RP Image Chat

A small, standard-library Python chat UI that exchanges roleplay messages with an external AI through JSON files and generates scene images with a local ComfyUI server.

The UI can run on a PC or a phone on the same LAN. Conversations and RAG collections stay on the PC. **This repository does not include Gemini/Antigravity authentication or an automatic agent runner.** You supply an AI agent that reads `wait_input.py` output and writes reply JSON to `outbox/`.

## How it works

1. The browser sends a message to `server.py`, which stores it in `data/sessions/<session-id>.json` as pending.
2. Run `python wait_input.py` from your AI agent. It waits for an input, claims it, then prints the channel settings, selected RAG entries, recent conversation, and the new message.
3. The AI agent writes a JSON reply to `outbox/<unique-name>.json`.
4. The server imports the reply. If it contains an image prompt, the server sends a workflow to ComfyUI and displays the output image in the chat.

`wait_input.py` exits after one message. Call it again for the next turn. Use one consumer at a time unless you coordinate multiple agents yourself.

## Requirements

- Python 3.10 or newer. The chat server has no pip dependencies.
- A running [ComfyUI](https://github.com/comfyanonymous/ComfyUI) instance for image generation. Text-only replies work without it.
- A checkpoint installed in ComfyUI. Set its filename in each channel's image settings. IPAdapter is optional and needs the corresponding ComfyUI custom nodes.
- An AI agent or script to produce the `outbox/` replies. The AI connection is deliberately external to this project.

## Start

Clone the repository, then run from its directory:

```powershell
$env:COMFYUI_OUTPUT_DIR = 'D:\ComfyUI\output' # change to your ComfyUI output folder
python .\server.py
```

Open <http://127.0.0.1:8199/> on the PC. The default ComfyUI API address is <http://127.0.0.1:8188/>.

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI API address |
| `COMFYUI_OUTPUT_DIR` | `../ComfyUI/output` relative to this repository | Folder where ComfyUI saves images |
| `RPCHAT_PORT` | `8199` | Chat server port |
| `RPCHAT_HOST` | `127.0.0.1` | Bind address; `lan` selects the PC's LAN IPv4 address |

To open the UI from a phone on the same local network, set `RPCHAT_HOST=lan` before starting the server, or run `start_mobile.bat` on Windows. Open the URL printed by the server. LAN mode exposes this unauthenticated app to devices on that network; use it only on a network you trust.

## Reply format

After `wait_input.py` prints a pending message, write a UTF-8 JSON file with the printed `session` and `reply_to` values:

```json
{
  "session": "<session id>",
  "reply_to": "<user message id>",
  "text": "Assistant reply",
  "memo": "Optional updated story summary",
  "image": {
    "prompt": "1girl, smiling, cafe",
    "width": 896,
    "height": 1152,
    "raw": false
  }
}
```

`image` is optional. With `raw: false`, the channel's base positive and character tags are prepended. With `raw: true`, the prompt is used as written. The negative prompt defaults to the channel's image settings.

## UI features

- Multiple channels with character, scenario, instruction, memo, and image settings
- Edit a past user message and request a new reply; alter an assistant message or regenerate its reply
- RAG collections with selectable entries per channel
- Image history, new-seed generation, prompt editing, and a latest-image background mode

Local conversations, RAG data, and processed replies are stored under `data/` and `outbox/`. Both directories are ignored by Git.
