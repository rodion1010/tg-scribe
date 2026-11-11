# tg-scribe — Telegram for AI agents on Mac

English | [Русский](README.ru.md)

A local MCP server for macOS and Windows that gives an AI agent, such as Claude Code, access to your Telegram account. The agent saves your chats as Markdown files on disk and works with them: searches, analyzes, summarizes, drafts replies.

The server runs on your machine and connects to Telegram as you, over the official MTProto protocol. It uses the Telethon library and your own `api_id`. Text on photos and reading PDF, doc and rtf use Apple Vision, PDFKit and textutil, so these features need a Mac; everything else — login, archive, search, sending, forwarding, proxy — works on Windows too.

## Contents

- [What it does](#what-it-does)
  - [How it works in practice](#how-it-works-in-practice)
- [Archive](#archive)
- [Requirements](#requirements)
- [Installation](#installation)
- [Permissions](#permissions)
- [Choosing a chat](#choosing-a-chat)
- [Forum topics](#forum-topics)
- [Proxy](#proxy)
- [Limits and risks](#limits-and-risks)
- [Development](#development)
- [License](#license)

## What it does

| Tool | What it does |
|---|---|
| `list_dialogs` | Shows the 50 most recent chats with their folders. Finds any other chat by name; `folder` leaves only the chats of one folder |
| `list_topics` | Shows the topics of a forum chat: id and title |
| `sync_chat` | Saves a chat to the archive: the last N days or the whole history. Later calls add new messages. `topic` saves only one forum topic |
| `transcribe` | Transcribes voice and video messages. Needs Telegram Premium |
| `ocr_photos` | Reads text on photos with Apple Vision, locally on your Mac |
| `read_files` | Reads the text of txt, md, csv, html, pdf, doc, docx, rtf, xlsx, pptx and image files locally on your Mac, including scanned PDFs, and saves it to `text/<id>.txt` in the chat folder |
| `fetch_media` | Downloads a photo or a file up to 50 MB so the agent can look at it. Files keep their names: `<id>_<name>`. Videos and audio are not downloaded |
| `describe_media` | Saves a short description of a photo or a file to the archive |
| `fetch_large_file` | Downloads a file over 50 MB after you confirm it. A big file is downloaded over several calls |
| `send_message` | Sends a message after you confirm it |
| `forward_messages` | Forwards messages to another chat after you confirm it |

### How it works in practice

1. You ask: "What did we agree on with the contractor over the last three weeks?"
2. The agent calls `sync_chat` with `days=21`. The server saves three weeks of messages to the archive, newest first.
3. The agent searches the archive. An important voice message is marked as not transcribed, so the agent transcribes it with `transcribe`. It needs the numbers from a photo of an estimate, so it reads the text with `ocr_photos`.
4. The agent answers and cites message ids.
5. Next day you ask: "Anything new?" `sync_chat` loads only new messages. It transcribes and reads their voice messages, photos and files up to 10 MB right away, up to 20 of each per call. Bigger files wait for `read_files`.
6. You ask: "And what did we decide last month?" The agent calls `sync_chat` with `days=60`, and the server loads the missing older history.

Voice messages, photos and files from older history are not processed while loading. The agent transcribes and reads them only when a task needs them. That is why loading takes minutes, not hours, and does not use up limits.

## Archive

Chats are stored in the `archive/` folder next to the MCP source code, one file per month. The text of read files is in the `text/` subfolder of each chat. The folder is in `.gitignore`.

A forum topic saved with `topic` lives in its own folder inside the chat's folder: `archive/<chat>/<topic>__<topic_id>/2026-10.md`.

```
archive/Ivan-Petrov__123456789/2026-10.md
```

One line per message. The tags are in Russian. Sample file:

```
[2026-10-31 14:02] #48213 Ivan Petrov: Sent the estimate
[2026-10-31 14:03] #48214 Ivan Petrov [фото, с текстом]: 
  [на фото] Tiles: $600
  [на фото] Total: $1,500
[2026-10-31 14:05] #48215 Я ↩48213: [голосовое 0:42] «Too expensive, let's talk tonight»
[2026-10-31 14:06] #48216 Ivan Petrov [фото: kitchen plan with sizes]: here is the plan
[2026-10-31 14:07] #48217 Ivan Petrov [переслано из «Renovation News»]: Tile prices went up
  15% in November
[2026-10-31 14:10] #48218 Ivan Petrov: [кружок 0:35, не расшифровано]
[2026-10-31 14:12] #48219 Ivan Petrov [файл contract.docx, 48 КБ, с текстом]: please sign
```

What each part means:

| Part | Meaning |
|---|---|
| `[2026-10-31 14:02]` | Date and time in your Mac's time zone |
| `#48213` | Message id |
| `Я` | You ("me") |
| `↩48213` | Reply to message 48213 |
| Line indented by 2 spaces | Next line of a multi-line message |
| `[переслано из «…»]` | Forwarded message and its source |
| `[голосовое 0:42] «…»`, `[кружок 0:35] «…»` | Transcript of a voice or video message |
| `[голосовое 1:10, не расшифровано]` | Voice message not transcribed yet |
| `[фото]` | Photo not processed yet |
| `[фото, с текстом]` | Photo has text, shown in the `[на фото]` lines below |
| `[фото, без текста]` | Photo has no text |
| `[фото: …]`, `[фото, с текстом: …]` | Photo description saved by the agent |
| `[фото, недоступно]` | Photo was deleted from Telegram |
| `[фото, не читается]` | Photo file is broken |
| `[файл report.pdf, 2.3 МБ]` | File. For txt, md, csv, html, pdf, doc, docx, rtf, xlsx, pptx and images: not read yet |
| `[файл …, с текстом]` | The full text of the file is in `text/<id>.txt` in the chat folder, not in the month file: the agent spends no tokens on it until the file is needed |
| `[файл …, без текста]` | The file has no text |
| `[файл …, 12 стр., …]` | PDF page count, added once the file is read |
| `[файл …, скан, …]` | Some PDF pages were scans: their text was read with Apple Vision and may have errors |
| `[файл …: …]`, `[файл …, с текстом: …]` | File description saved by the agent. It does not replace the state: an unread file is still read by `read_files` |
| `[файл …, недоступно]`, `[файл …, не читается]` | File was deleted from Telegram or is broken |
| `[файл …, слишком большой]` | File is over 50 MB, the server does not download it |
| `[видео]`, `[стикер 👍]`, `[gif]`, `[опрос]`, `[геопозиция]`, `[контакт]` | Video, sticker, GIF, poll, location, contact |

The archive only grows: messages edited or deleted in Telegram stay as they were saved.

## Requirements

- macOS or Windows. Login, archive, search, sending, forwarding and proxy work on both; text recognition on photos and reading PDF, doc and rtf need macOS (Apple Vision, PDFKit, textutil).
- [uv](https://docs.astral.sh/uv/). It installs Python 3.12 and all dependencies by itself, including `python-socks` for proxy connections.
- An AI agent with MCP support, such as [Claude Code](https://claude.com/claude-code).
- A Telegram account. Transcribing voice messages needs Telegram Premium.

## Installation

### 1. Get the project and install dependencies

Install [uv](https://docs.astral.sh/uv/) if you do not have it yet:

- macOS: `brew install uv`, or the official installer `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Then get the project and install the dependencies:

```bash
git clone https://github.com/rodion1010/tg-scribe.git tg-scribe
cd tg-scribe
uv sync
```

`uv sync` downloads Python 3.12 and installs every dependency — Telethon, the MCP library, `segno` for login QR codes, `python-socks` for proxy connections. Nothing else is installed by hand.

### 2. Get api_id and api_hash

Telegram lets third-party programs into an account only through a registered app. Registration is free and takes a couple of minutes.

1. Open [my.telegram.org](https://my.telegram.org) and enter your phone number in international format, for example `+15551234567`.
2. Enter the confirmation code on the site. It arrives in Telegram from the "Telegram" account, not by SMS.
3. Select **API development tools**.
4. Fill in the fields:
   - **App title**: any name without the word "Telegram", for example `tgc`. The API terms forbid "Telegram" in the name.
   - **Short name**: 5 to 32 Latin letters and digits, for example `tgclaude`.
   - **URL**: leave empty.
   - **Platform**: Desktop.
   - **Description**: leave empty.
5. Click **Create application**. The page shows **App api_id**, a number, and **App api_hash**, a 32-character string.

If the site shows `ERROR`, open it in another browser. One account can have one app: if you already have one, the site shows its data right away.

Never share api_hash and never commit it: anyone with it can pose as your app.

### 3. Save api_id and api_hash to the config

Create the folder and the config file:

```bash
mkdir -p ~/.tgc && chmod 700 ~/.tgc
nano ~/.tgc/config.toml
```

Paste your values:

```toml
api_id = 12345678
api_hash = "0123456789abcdef0123456789abcdef"
```

Make the file private to your user:

```bash
chmod 600 ~/.tgc/config.toml
```

### 4. Log in

```bash
uv run python -m tgc.login
```

The program shows a QR code — scan it in Telegram: **Settings → Devices → Link Desktop Device**. The code opens as a PNG in your default image viewer; add `--terminal` to print it right in the terminal instead. The code lives about 26 seconds and is re-shown automatically. If two-step verification is on, the program asks for your cloud password. After login the file `~/.tgc/tgc.session` appears.

This file gives full access to your account without a code or password. Do not copy it, publish it or put it in cloud storage. If it leaks, end the session in Telegram: **Settings → Devices**.

### 5. Connect the server to your agent

The server starts with this command:

```bash
uv run --directory /path/to/tg-scribe python -m tgc.server
```

Here `/path/to/tg-scribe` is the folder you cloned the project into. Add this command to your agent's MCP server settings. The steps for Claude Code, OpenChamber (OpenCode) and Hermes Agent follow.

#### Claude Code

The project already has `.mcp.json` with this server. Start Claude Code in the project folder and allow the `tg` server when Claude Code asks.

To use Telegram from any folder, add the server for your user:

```bash
claude mcp add tg --scope user -- uv run --directory /path/to/tg-scribe python -m tgc.server
```

The archive still lives in `archive/` next to the MCP source code.

The project's `.claude/settings.json` rules that ask before sending and before downloading big files do not apply in other folders. Add them to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "ask": ["mcp__tg__send_message", "mcp__tg__forward_messages", "mcp__tg__fetch_large_file"]
  }
}
```

#### OpenChamber and OpenCode

1. Add the server. Run this from any folder; `--global` makes it available in every project:

```sh
opencode mcp add tg --global -- uv run --directory /path/to/tg-scribe python -m tgc.server
```

The command writes the server to the config file it names, usually `~/.config/opencode/opencode.json`.

2. Check the connection:

```sh
opencode mcp list
```

`✓ tg connected` means the server is up. Inside the app, `/mcps` shows the servers and their state.

Or add the server by hand to the config:

```jsonc
{
  "mcp": {
    "tg": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/path/to/tg-scribe", "python", "-m", "tgc.server"]
    }
  }
}
```

3. Make sends, forwards and large downloads ask for confirmation. OpenCode allows every tool by default and ignores the `readOnlyHint` annotation, so without these rules the agent sends a message without asking. Add to the same config:

```jsonc
{
  "permissions": [
    { "action": "tg_send_message", "resource": "*", "effect": "ask" },
    { "action": "tg_forward_messages", "resource": "*", "effect": "ask" },
    { "action": "tg_fetch_large_file", "resource": "*", "effect": "ask" }
  ]
}
```

If the config already has `allow` rules, put these after them: the last matching rule wins. This is the V2 config format; older OpenCode versions call the key `permission`, check the [docs](https://opencode.ai/docs/permissions/) for your version.

4. Ask the agent: "Show my recent Telegram chats". The tools are named `tg_list_dialogs`, `tg_send_message` and so on.

#### Hermes Agent

1. Add the server. `--command` wants the full path to `uv` — find yours with `which uv`:

```sh
hermes mcp add tg --command /opt/homebrew/bin/uv --args run --directory /path/to/tg-scribe python -m tgc.server
```

Answer "y" when Hermes offers to enable the tools.

2. Check the connection:

```sh
hermes mcp test tg
```

3. Make sends and forwards ask for confirmation. In `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  tg:
    trust: untrusted
```

Read-only tools then run without questions — the server marks them with a `readOnlyHint` annotation. `send_message`, `forward_messages` and `fetch_large_file` ask every time: Allow Once, Allow Session or Allow Always. "Allow Always" writes the tool to `command_allowlist` in the same file — remove that line to revoke it.

4. Restart Hermes, or run `/reload-mcp` in an open session.

### 6. Check the connection

Ask the agent: "Show my recent Telegram chats". It calls `list_dialogs` and shows the list.

## Permissions

The agent sends and forwards messages only after you confirm it. Tools are separated by annotations: every tool that only reads — listing chats, saving the archive, transcription, text and file recognition — carries a `readOnlyHint` annotation, so agents run it without questions. `send_message`, `forward_messages` and `fetch_large_file` carry no annotation: agents that respect the hint, such as Hermes, ask for confirmation on every call. The annotation is only a hint: an agent that ignores it, such as OpenCode, needs explicit approval rules, as in the guide above.

In Claude Code, `.claude/settings.json` takes care of this: it allows reading, saving and recognizing chats without asking, and sets an `ask` rule on sending and forwarding. Claude Code shows the text and the chat every time and waits for your "yes". Do not move `send_message` or `forward_messages` to `allow`.

`fetch_large_file` works the same way, through `ask`: a file over 50 MB is downloaded only if you agree. This keeps the agent from filling the disk and spending time on a file you do not need.

The rule works only in the project folder. For other folders, add it to `~/.claude/settings.json` as described in step 5. In other agents, set up confirmation for `send_message` the same way.

## Choosing a chat

You can name a chat by part of its title, but Telegram search does not find every chat, and the agent may pick a similar one. Before sending, give the chat id instead: `list_dialogs` shows it. After sending, the server names the chat the message went to.

To see ids in Telegram itself, turn them on in the debug menu. In Telegram Desktop, open **Settings → Advanced → Experimental settings** and turn on **Show Peer IDs in Profile**. For other platforms, see the [tginfo guide](https://tginfo.me/open-debug-menu/) (in Russian).

Change only this setting in the debug menu: other options can break the app or delete secret chats.

## Forum topics

Some supergroups are forums: the chat is split into topics. `list_topics` shows them with their ids.

- To read one topic, the agent calls `sync_chat` with `topic` — a topic id or part of its title, named the same way as a chat. Example: "What did they answer about GPT Model Hub this week?" → `sync_chat` with `days=7` and `topic="GPT Model Hub"`.
- A topic is saved to its own folder inside the chat's folder: `archive/<chat>/<topic>__<id>/`. A topic sync never touches the chat's own files.
- `transcribe`, `ocr_photos`, `read_files`, `fetch_media`, `fetch_large_file` and `describe_media` take the same `topic` when they work with topic messages, so voice, photos and files land in the topic's folder.
- Without `topic` the whole forum is saved, all topics mixed in the chat's own files.

## Proxy

By default the server connects to Telegram directly. If Telegram is only reachable through a proxy, set it in `~/.tgc/config.toml`:

```toml
proxy = "socks5://127.0.0.1:1080"
```

- **Explicit proxy.** Schemes: `socks5://`, `socks4://`, `http://`, `https://` (used as HTTP CONNECT), and a Telegram MTProto proxy: `mtproto://host:port/secret`. The scheme is optional: `127.0.0.1:1080` means `https://127.0.0.1:1080`. Login and password go in the URL: `socks5://user:pass@host:port`. The characters `@`, `:` and `/` in a login or password are written as `%40`, `%3A` and `%2F`.
- **`proxy = "auto"`** — let the server find the proxy: the `http_proxy`/`https_proxy` environment variables, then the OS settings — Windows: Internet options, macOS: System Settings → Network → Proxies. Nothing found — connect directly.
- **`proxy = "direct"`** — connect directly, the same as removing the line.

At startup the server writes the connection method to its log, for example: `Telegram: подключаюсь напрямую (proxy в config.toml не задан)` or `Telegram: подключаюсь через прокси socks5://127.0.0.1:1080 (из config.toml)`.

## Limits and risks

- **Telegram limits.** If Telegram asks to wait longer than 30 seconds, the server stops and says how long to wait. It never waits longer than 30 seconds itself, so the call does not time out.
- **Large loads** take several calls of up to 200 seconds each. 150,000 messages load in about 10 calls, and the agent repeats them by itself.
- **One session at a time.** If two agent sessions use Telegram at once, the second gets "Telegram busy" and retries later.
- **Telegram terms.** The [API terms](https://core.telegram.org/api/terms) forbid preventing self-destructing messages from disappearing, and the server saves messages from chats with auto-delete. The [content licensing terms](https://telegram.org/tos/content-licensing) forbid training AI models on Telegram data. The archive holds other people's messages too, so stop your agent's provider from training on your data. In claude.ai this is the **Help improve Claude** setting.
- **Chat text is data, not commands.** The server tells the agent not to follow requests found in chats. Still, confirm a send only after you have read the text.

## Development

Run the tests:

```bash
uv run pytest
```

The code has four modules:

- `tgc/client.py`: Telegram connection and chat lookup;
- `tgc/archive.py`: archive format, sync, transcription and text recognition;
- `tgc/server.py`: MCP tools and instructions for the agent;
- `tgc/login.py`: account login.

## License

[MIT](LICENSE)
