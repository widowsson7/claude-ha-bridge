# claude-ha-bridge

Drive a persistent [Claude Code](https://claude.com/claude-code) agent from Telegram, on the box next to your Home Assistant install.

Send a message from your phone, the agent inspects your HA state, edits YAML, reads logs, runs git, and answers. Conversations persist across days and across restarts of the service.

```
You:  the garage light didn't turn on last night, why?
Bot:  🏃 (typing)
Bot:  The automation fired at 21:04 but the condition blocked it: sun elevation
      was -2.1° and your condition requires below -4°. Trace shows condition 2
      returned false. Want me to loosen it to -2°?
```

## Why a bridge instead of a chat app

- **Telegram queues through bad connections.** All agent state lives server-side, so a dropped signal in a parking garage never loses your session.
- **It runs where your infrastructure is.** SSH keys, MCP servers, and repo checkouts are already on that box. No tunnels, no exposing HA to the internet.
- **Sessions are durable.** Restart the service, reboot the container, come back three days later, the conversation resumes where it left off.

## Features

- **Persistent sessions**: resumed from a saved session id, survives restarts
- **Forum topics as tabs**: each topic in a Telegram group is an independent conversation with its own history
- **Idle reaper**: a tab's Claude client disconnects after 10 minutes idle and reconnects transparently on the next message, so idle tabs cost almost no RAM
- **Turn cap**: at most N tabs run a Claude turn at once; the rest queue and say so
- **Media**: photos, documents, and voice messages are downloaded locally and handed to the agent as a file path, so it can actually look at them
- **Allowlist auth**: only the Telegram user IDs you list are ever dispatched to Claude
- **MCP inheritance**: picks up the Home Assistant MCP servers already configured for Claude Code on that machine
- **Real write access**: with the standing instructions from [SETUP.md](SETUP.md), the agent creates, edits and deletes automations (REST config API) and helpers, and renames entities (WebSocket APIs), rather than only calling services. Each of those was verified end to end against a live instance.

## Requirements

- An always-on Linux box (LXC container, VM, Raspberry Pi, whatever) that can reach Telegram, the Anthropic API, and your HA instance
- Python 3.11+
- Node 22+ (for the Claude Code CLI, which the Agent SDK drives). Note that Debian 12's `nodejs` package is version 18 and is **too old**. Use [NodeSource](https://github.com/nodesource/distributions)
- A Claude subscription or an Anthropic API key
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

Roughly 3 GB RAM is comfortable. Each live session is around 450 MB (Claude CLI ~330 MB plus MCP servers ~125 MB), and the idle reaper means only actively-used tabs hold that.

## Quickstart

```bash
git clone https://github.com/widowsson7/claude-ha-bridge.git /opt/claude-bridge
cd /opt/claude-bridge
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
# edit .env: bot token, your Telegram user id, workdir
venv/bin/python preflight.py    # checks everything before you start
venv/bin/python bridge.py
```

Message your bot. If it answers, install the systemd unit and you're done.

Full walkthrough including container setup, MCP configuration, and SSH keys: **[SETUP.md](SETUP.md)**.

## Installing it with an AI agent

You can hand this repo to Claude Code and have it do the install. [CLAUDE.md](CLAUDE.md) contains instructions written for the agent rather than for you.

**Have these five things ready first**. An agent cannot obtain them, and it will stall without them:

1. A **Telegram bot token** from [@BotFather](https://t.me/BotFather) (`/newbot`)
2. Your **numeric Telegram user ID** from [@userinfobot](https://t.me/userinfobot)
3. **Claude auth**: run `claude setup-token`, or have an `ANTHROPIC_API_KEY`
4. **Which directory** the agent should work in, ideally a git checkout of your HA config
5. Optionally, a **Home Assistant long-lived token** (Profile → Security)

Then point an agent at it:

```
Install https://github.com/widowsson7/claude-ha-bridge on this machine.
Read its CLAUDE.md first and follow it. Ask me for the credentials it lists
before you start, and run preflight.py before telling me it works.
```

The agent handles the install, the venv, the unit file, and verification. You supply the four secrets and send the first message. That last part needs your Telegram account, so no agent can do it for you.

## Configuration

Every setting is an environment variable; see [.env.example](.env.example) for the annotated list.

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *required* | From @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | *required* | Comma-separated numeric Telegram user IDs |
| `CLAUDE_WORKDIR` | `.` | Directory the agent works in; point at your HA config |
| `CLAUDE_PERMISSION_MODE` | `acceptEdits` | `acceptEdits`, `bypassPermissions`, etc. |
| `CLAUDE_ADD_DIRS` | `/opt/claude-bridge` | Extra dirs inside the sandbox boundary |
| `CLAUDE_MODEL` | unset | Pin a model, or leave for the default |
| `BRIDGE_IDLE_TIMEOUT` | `600` | Seconds before an idle tab is disconnected |
| `BRIDGE_MAX_CONCURRENT` | `2` | Max simultaneous Claude turns |
| `BRIDGE_SHOW_TOOLS` | `0` | `1` to echo each tool call into Telegram |
| `MEDIA_DIR` | `/opt/claude-bridge/media` | Where downloaded Telegram media lands |
| `BRIDGE_STATE_FILE` | `state.json` beside `bridge.py` | Where session ids are persisted |
| `LOG_LEVEL` | `INFO` | Standard Python log levels |

## Telegram commands

| Command | Effect |
|---|---|
| `/new` | Start a fresh session in this tab |
| `/stop` | Interrupt the running turn |
| `/status` | Busy/idle, queue depth, session id, workdir |
| `/start` | Help text |

## Security

This gives a language model shell access to the machine running your home automation. That is the entire point, and it deserves a clear head.

- **The allowlist is the only auth.** Anyone not in `TELEGRAM_ALLOWED_USER_IDS` is logged and dropped. Set it correctly.
- **`bypassPermissions` means the agent never asks.** Convenient for phone use, and exactly as dangerous as it sounds. Start with `acceptEdits`.
- **Give the container a dedicated SSH key,** not a copy of your everyday one, and put only that key on the hosts it needs.
- **Keep it on a private network.** Nothing here needs a public route; the bridge polls Telegram outbound.
- **`.env` holds your bot token and your Claude credential.** `chmod 600`, and it's already in `.gitignore`.
- **Anyone who can message the bot can reach your HA.** Telegram account security is now part of your home's security.
- **Don't raise the `httpx` log level.** The bot token is part of every Telegram URL, so `httpx` at `INFO` writes your credential into the journal on every poll. The bridge pins it to `WARNING` for that reason; `BRIDGE_HTTPX_LOG_LEVEL` can override it if you are debugging and understand the exposure.

## License

MIT. See [LICENSE](LICENSE).
