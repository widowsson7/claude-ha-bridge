# Setup guide

A complete walkthrough for deploying the bridge, from an empty container to a working Telegram agent.

Anywhere you see `10.0.0.x`, a container ID, or a placeholder like `YOUR_TELEGRAM_USER_ID`, substitute your own values.

---

## 1. What you'll need

**Infrastructure**

- An always-on Linux box. A Proxmox LXC container is what this guide uses, but a VM, a spare Pi, or a Docker container all work.
- Network reachability from that box to Telegram, the Anthropic API, and your Home Assistant instance.

**Credentials**

- **Telegram bot token** — create a bot with [@BotFather](https://t.me/BotFather)
- **Your Telegram user ID** — a number, get it from [@userinfobot](https://t.me/userinfobot)
- **Claude auth** — a `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (subscription), an `ANTHROPIC_API_KEY` (API billing), or an interactive `claude` login on the box. The SDK resolves credentials the same way the CLI does. Details in step 6.
- **A Home Assistant long-lived access token** if you want the HA MCP server (Profile → Security → Long-Lived Access Tokens)

**Assumed knowledge**

Basic Linux and SSH, systemd units, and Python virtualenvs.

---

## 2. Sizing

| Resource | Suggested | Notes |
|---|---|---|
| CPU | 2 cores | Turns are effectively single-threaded |
| RAM | 3 GB | ~450 MB per *live* session; idle tabs cost almost nothing |
| Disk | 10–20 GB | venv, logs, session state, downloaded media |
| Network | Static IP | Only outbound access is required |

The idle reaper is what makes 3 GB workable. A tab that hasn't been used in 10 minutes drops its Claude client and keeps only its session id, so a dozen open topics cost about what one active conversation does.

If you regularly run three or more conversations simultaneously, go to 4 GB.

---

## 3. Create the container

On a Proxmox host:

```bash
pct create 210 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname claude-bridge \
  --cores 2 \
  --memory 3072 \
  --swap 512 \
  --rootfs local-lvm:16 \
  --ostype debian \
  --unprivileged 1 \
  --net0 name=eth0,bridge=vmbr0,ip=10.0.0.20/24,gw=10.0.0.1 \
  --onboot 1
```

Adjust the storage, bridge, and addresses for your network. Then start it:

```bash
pct start 210
```

Not on Proxmox? Skip to step 4 and run the rest on any Debian or Ubuntu box.

---

## 4. Install dependencies

Inside the container (`pct exec 210 -- bash`, or SSH in):

```bash
apt update
apt install -y git curl python3 python3-venv python3-pip openssh-client
```

Now Node. **Do not use Debian's `nodejs` package** — it is version 18, and the Claude Code CLI requires 22 or newer. Installing on 18 appears to succeed while emitting an `EBADENGINE` warning, then fails later in ways that are hard to trace back:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs
node --version    # expect v22.x or newer
```

Then the CLI itself:

```bash
npm install -g @anthropic-ai/claude-code
```

Worth knowing what this is and isn't for. The Agent SDK ships its own bundled copy of the CLI and will use that at runtime, not this one — you'll see `Using bundled Claude Code CLI` in the logs. You still want the global install, because `claude setup-token` and an interactive `claude` login (step 6) come from it. Node 22+ is required either way, since the bundled copy runs on your Node.

---

## 5. Install the bridge

```bash
git clone https://github.com/widowsson7/claude-ha-bridge.git /opt/claude-bridge
cd /opt/claude-bridge
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

---

## 6. Configure

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

At minimum set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, and `CLAUDE_WORKDIR`.

`CLAUDE_WORKDIR` should point at whatever you want the agent working in — a git checkout of your HA config is a good choice, because then every change the agent makes is reviewable and revertable. **The directory has to exist already**; create it now if it doesn't:

```bash
mkdir -p /root/ha-config     # or wherever you pointed CLAUDE_WORKDIR
```

**Authenticate Claude.** Three options, pick one:

```bash
claude setup-token      # subscription auth -> paste result as CLAUDE_CODE_OAUTH_TOKEN in .env
```

or put an `ANTHROPIC_API_KEY` in `.env` to bill the API directly, or just run `claude` once and log in interactively and let the SDK find the stored credential.

If you use that third option, make sure the service has `HOME` set (the bundled unit does). Without it the CLI can't locate its credentials and every turn fails with `Not logged in · Please run /login`, which is a confusing way to discover the problem.

---

## 7. Give the agent access to Home Assistant

Two things are worth wiring up, and they're independent.

### SSH access

Generate a **dedicated** keypair on the container. Don't copy your everyday private key here.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "claude-bridge"
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@10.0.0.10
ssh root@10.0.0.10 'echo ok'
```

If you later want to revoke the agent's access, you delete one key from one `authorized_keys` file.

### MCP servers

The bridge inherits MCP configuration from Claude Code on this machine (`setting_sources=["user", "project", "local"]`), so an `.mcp.json` in your `CLAUDE_WORKDIR` is picked up automatically.

Install a Home Assistant MCP server. [`ha-mcp`](https://www.npmjs.com/package/ha-mcp) is the one this project is developed against:

```bash
npm install -g ha-mcp
```

Then write `.mcp.json` into your `CLAUDE_WORKDIR`:

```json
{
  "mcpServers": {
    "homeassistant": {
      "command": "ha-mcp",
      "args": [],
      "env": {
        "HA_MCP_TOKEN": "your_long_lived_token_here",
        "HA_MCP_URL": "http://10.0.0.10:8123"
      }
    }
  }
}
```

Any other HA MCP server works too, but read its own docs for the command and environment variable names — they differ between implementations, and getting them wrong fails quietly with the tools simply absent rather than with an error.

Keep this file out of git: it holds a token that can control your house.

### Standing instructions

A `CLAUDE.md` in your workdir tells the agent how your setup works, so you don't re-explain it every conversation:

```markdown
# Home Assistant agent instructions

## Access
- HA host: 10.0.0.10
- Timezone: America/Phoenix (no DST)

## Managing automations
Create, edit and delete automations through the REST config API, using the same
long-lived token as the MCP server. `<id>` is the automation's numeric `id`
attribute, NOT its entity_id.

- Read:    GET    /api/config/automation/config/<id>
- Create:  POST   /api/config/automation/config/<id>   (body = the automation)
- Edit:    POST   /api/config/automation/config/<id>   (same endpoint, replaces)
- Delete:  DELETE /api/config/automation/config/<id>

After any change, call the `automation.reload` service. New or removed entities
only appear or disappear after that reload.

If GET returns 404 for an automation that clearly exists, it is defined in a
package or an `!include`d file rather than in automations.yaml. This endpoint
only manages automations.yaml — edit those as files instead.

## Managing helpers
Helpers (input_boolean, input_number, counter, timer, input_text, ...) live in
.storage, NOT in YAML. Never edit those files directly — HA holds them in memory
and will clobber your changes. Use the WebSocket API instead:

- List:   {"type": "input_boolean/list"}
- Create: {"type": "input_boolean/create", "name": "My Helper"}
- Delete: {"type": "input_boolean/delete", "input_boolean_id": "<id>"}

The same verb pattern works for counter/*, input_number/*, timer/*, and the
other helper domains. `<id>` comes from the list command, and is not the
entity_id.

## Renaming entities
Use the entity registry over WebSocket, never a file edit:

{"type": "config/entity_registry/update", "entity_id": "<current>",
 "name": "New Display Name", "new_entity_id": "<new_entity_id>"}

Both fields are optional; send either or both. Note this sets a registry-level
display-name override — a helper's own configured name is separate and will
still show the old value in that helper's list command.

## Dashboards
Lovelace dashboards stored in .storage can be edited, but the changes do not
appear until Home Assistant restarts. Warn the user before doing that.

## Rules
1. Use the config API above rather than hand-editing automations.yaml
2. Never raw-edit anything under .storage — use the WebSocket APIs
3. Run a config check before reloading anything
4. Prefer a targeted reload over restarting HA core
5. Say what you changed and why
```

**Verified.** The automation, helper and entity-registry mechanisms above were
each tested end to end against a live Home Assistant instance (create → read →
edit → delete → confirm gone). The Lovelace restart note reflects operational
experience rather than a test in this repo.

Things that still need a human, whatever the instructions say: OAuth-based
integration setup (browser login), pairing flows needing a physical button press,
and QR-code scans.

That "Managing automations" block is the difference between an agent that can
turn your lights on and one that can write you a new automation. Without it the
agent has the access but not the recipe, and will usually tell you it can only
call services.

---

## 8. Run it as a service

Install the included unit:

```bash
cp /opt/claude-bridge/claude-bridge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claude-bridge
```

Check it:

```bash
systemctl status claude-bridge
journalctl -u claude-bridge -f
```

You should see a startup line reporting the workdir, the allowed user IDs, and the resource limits.

### Optional: container-level memory guardrails

The unit already sets `MemoryHigh`/`MemoryMax`. On Proxmox you can add a second backstop in `/etc/pve/lxc/210.conf`:

```
lxc.cgroup2.memory.high = 2560M
lxc.cgroup2.memory.max = 2816M
```

`MemoryHigh` throttles; `MemoryMax` kills. If a turn is killed, the bridge reconnects on the next message and resumes from the saved session id, so you lose the turn but not the conversation.

---

## 9. Set up Telegram

1. **Create the bot** with @BotFather (`/newbot`) and paste the token into `.env`.
2. **Message the bot directly** and send `/start`. If it replies, you're working.

### Forum topics (recommended)

Topics turn one group into a set of independent conversations, each with its own history.

1. Create a private group containing just you and your bot.
2. Promote the bot to admin.
3. Group settings → enable **Topics**.
4. In @BotFather, `/setprivacy` → your bot → **Disable**, so it receives all group messages.

Now a "Zigbee" topic and an "Irrigation" topic are two separate agents with separate memories. Switching topics is switching context.

---

## 10. Verify

- [ ] `systemctl status claude-bridge` shows active (running)
- [ ] `/start` gets a reply
- [ ] A real question works: *"which lights are on right now?"*
- [ ] `/status` reports a session id after the first exchange
- [ ] Restart the service, send another message, and the conversation still has context
- [ ] A message from a *different* Telegram account is ignored (check the log for `unauthorized user`)

That last one is worth actually testing rather than assuming.

---

## Operations

```bash
systemctl restart claude-bridge     # sessions resume from state.json
journalctl -u claude-bridge -f      # live logs
```

Upgrade:

```bash
cd /opt/claude-bridge
git pull
venv/bin/pip install -r requirements.txt
npm update -g @anthropic-ai/claude-code
systemctl restart claude-bridge
```

Back up `.env` (encrypted) and `state.json`. Losing `state.json` costs you conversation history, nothing more.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| No reply at all | Wrong token, or your user ID isn't allowlisted | `journalctl -u claude-bridge -f` and look for `unauthorized user`; confirm your ID with @userinfobot |
| Replies in DMs but not the group | Privacy mode still on | @BotFather → `/setprivacy` → Disable, then remove and re-add the bot |
| Each topic shares one conversation | Topics not enabled, or bot isn't admin | Enable Topics in group settings; promote the bot |
| Every turn returns `Not logged in · Please run /login` | No credential reached the process | Set `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` in `.env`, or ensure `HOME` is set in the unit so an interactive login is found |
| `npm` warned `EBADENGINE`, CLI behaves oddly | Node 18 from Debian's repo | Install Node 22+ from NodeSource (step 4); `preflight.py` flags this |
| Value set in `.env` seems ignored | The line is still commented out | The auth block in `.env.example` ships commented; uncomment the line you use |
| `--dangerously-skip-permissions cannot be used with root` | `bypassPermissions` as root | Use `acceptEdits`, or run the service as a non-root user |
| `Error: ... Retrying will resume` | Turn crashed, client dropped | Just send another message; it reconnects and resumes |
| Agent can't read a file | Path outside the sandbox boundary | Add it to `CLAUDE_ADD_DIRS`. A `permissions.allow` rule alone does **not** extend the boundary |
| Killed mid-turn, memory warnings | Container is RAM-constrained | `pct set 210 --memory 4096`, or lower `BRIDGE_MAX_CONCURRENT` |
| Photos ignored | Older build | Media arrives as `caption`, not `text`; current `bridge.py` handles this |
| MCP tools missing | `.mcp.json` isn't in the workdir | It's read from `CLAUDE_WORKDIR`; confirm with `/status` |

---

## How it fits together

```
Telegram  ──getUpdates(long poll)──▶  bridge.py
                                        │
                                        ├─ allowlist check
                                        ├─ (chat_id, thread_id) ──▶ ChatSession ──▶ ClaudeSDKClient
                                        │                              │              │
                                        │                          per-tab queue      ├─ MCP: Home Assistant
                                        │                          idle reaper        ├─ Bash / SSH
                                        │                          turn semaphore     └─ Read / Edit / Grep
                                        │
                                        └─ state.json  {"chat:thread": "session_id"}
```

One process, one asyncio loop, one `ChatSession` per tab. Each session owns a queue and a lock; a global semaphore caps concurrent turns; a reaper walks the sessions once a minute and disconnects the idle ones. Session ids persist to `state.json` on every turn, which is what makes restarts invisible.
