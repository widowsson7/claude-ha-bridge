# Instructions for an AI agent installing this project

This file is for a coding agent (Claude Code or similar) that has been asked to
install claude-ha-bridge on a machine. If you are a human, read
[SETUP.md](SETUP.md) instead — it covers the same ground in a friendlier order.

## What you are building

A Python service that bridges Telegram to a persistent Claude Code agent, so the
user can manage Home Assistant from their phone. One process, one systemd unit,
one `.env` file.

## Before you touch anything: collect five values

Five things cannot be obtained by you. Four require a human with a phone and a
browser; the fifth is a decision only they can make. **Ask for all five up
front, in one message, before you start installing.** Do not get halfway
through and then discover you need them.

1. **Telegram bot token** — the user opens Telegram, messages
   [@BotFather](https://t.me/BotFather), sends `/newbot`, picks a name, and
   copies the token. Looks like `123456789:AAH...`.
2. **Their numeric Telegram user ID** — from
   [@userinfobot](https://t.me/userinfobot). A number, not an @username. This is
   the entire access control model, so getting it wrong locks them out or, worse,
   lets someone else in.
3. **Claude authentication** — one of:
   - `claude setup-token` run on the target machine (subscription), or
   - an `ANTHROPIC_API_KEY` (API billing), or
   - an interactive `claude` login on the target machine.
4. **A Home Assistant long-lived access token** — only if they want the HA MCP
   server. HA → Profile → Security → Long-Lived Access Tokens. Optional; the
   bridge works without it, just with less HA awareness.
5. **Where the agent should work** (`CLAUDE_WORKDIR`) — a git checkout of their
   HA config is the best answer, because it makes every change the agent makes
   reviewable and revertable. If they don't have one, say so and suggest it.
   **This directory must exist before you start the service**; create it if it
   does not, and say that you did.

**Never invent, guess, or placeholder any of these values.** A fabricated token
produces a service that starts cleanly and silently never works, which is the
worst possible failure mode here.

## Then detect the environment

Do not assume Proxmox. SETUP.md leads with `pct create` because that is the
author's setup, but the software does not care.

- If you are already on the target machine, install in place.
- If the user wants a new Proxmox LXC, follow SETUP.md section 3.
- Debian/Ubuntu, a Pi, a VM, or a container are all fine.

Check before installing: `python3 --version` (need 3.11+), whether `node`/`npm`
exist, and whether you have root or need `sudo`.

## Install

Read the "Traps" section below **before** you write `.env`, not after. Two of
those traps are configuration-time decisions.

This block is self-contained; you should not need any other document.

```bash
# 1. OS packages. A stock Debian 12 container has none of these.
apt-get update
apt-get install -y git curl python3 python3-venv python3-pip openssh-client

# 2. Node 22+. Debian's own `nodejs` package is version 18, which is BELOW the
#    Claude Code CLI's requirement and installs with an EBADENGINE warning.
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

# 3. The CLI the Agent SDK drives.
npm install -g @anthropic-ai/claude-code

# 4. The bridge itself.
git clone https://github.com/widowsson7/claude-ha-bridge.git /opt/claude-bridge
cd /opt/claude-bridge
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env

# 5. The working directory you agreed with the user. It must exist.
mkdir -p /root/ha-config     # or wherever they chose
```

Then write the collected values into `.env`. Note that the authentication
block in `.env.example` ships fully commented out — you must uncomment the one
line you are using, not just paste a value next to it. Keep `chmod 600`. Never
echo token values back into the transcript, and never commit `.env`.

## Verify before declaring success

```bash
venv/bin/python preflight.py
```

This checks the Python version, dependencies, CLI presence, Node version, bot
token validity (a real `getMe` call), allowlist format, and directory
permissions. It exits non-zero on failure and tells you what to fix.

**Preflight cannot validate the Claude credential.** There is no cheap endpoint
to test it against, so it only checks that something plausibly-shaped is
present. A passing preflight therefore does not mean Claude will authenticate.
The first real turn is the only proof.

**Do not tell the user it works until preflight passes and they have exchanged a
real message with the bot.** You cannot send that message yourself — it has to
come from their Telegram account. Ask them to message the bot and report what
came back.

## Install the service

```bash
cp claude-bridge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claude-bridge
journalctl -u claude-bridge -f
```

If the unit's paths differ from where you installed, edit
`WorkingDirectory`, `EnvironmentFile`, and `ExecStart` to match.

## Traps that will cost you an hour

- **`HOME` must be set in the unit.** systemd does not reliably provide it.
  Without it the Claude CLI cannot find an interactive login and every turn fails
  with `Not logged in · Please run /login`. The shipped unit sets `HOME=/root`;
  if you run as another user, change it to that user's home.
- **`bypassPermissions` cannot run as root.** It maps to
  `--dangerously-skip-permissions`, which the CLI refuses under root. Leave
  `CLAUDE_PERMISSION_MODE=acceptEdits` unless the user asks otherwise and is
  running as a non-root user.
- **`CLAUDE_ADD_DIRS` is the sandbox boundary.** A `permissions.allow` rule does
  not extend it. If the agent needs to read a path outside `CLAUDE_WORKDIR`, that
  path must be listed here.
- **`.mcp.json` is read from `CLAUDE_WORKDIR`,** not from the repo directory.
- **The allowlist is the only authentication.** Anyone whose ID is in
  `TELEGRAM_ALLOWED_USER_IDS` gets shell access to this machine through Claude.
  Confirm the ID with the user rather than inferring it.

## What to tell the user at the end

- Which machine it is installed on, and the service name
- That anyone able to message the bot can reach their Home Assistant
- That `.env` holds live credentials and is `chmod 600` and gitignored
- How to see logs: `journalctl -u claude-bridge -f`
- The commands: `/new`, `/stop`, `/status`
- If they enabled Topics, that each topic is a separate conversation
