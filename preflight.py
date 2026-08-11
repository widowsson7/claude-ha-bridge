#!/usr/bin/env python3
"""Check that this machine is actually ready to run the bridge.

Run it after configuring .env and before starting the service:

    venv/bin/python preflight.py

Every check prints PASS, WARN, or FAIL with a specific next step. Exits
non-zero if anything is FAIL, so an installer (human or agent) has a clear
signal rather than having to interpret log output.

The only network call is Telegram's getMe, which confirms the bot token is
real. Nothing is sent to any chat.
"""

import os
import sys
from pathlib import Path

RESULTS: list[tuple[str, str, str]] = []
DOTENV = Path(__file__).with_name(".env")


def record(level: str, name: str, detail: str = "") -> None:
    RESULTS.append((level, name, detail))
    colour = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m"}.get(level, "")
    reset = "\033[0m" if colour else ""
    print(f"  {colour}[{level}]{reset} {name}" + (f"\n         {detail}" if detail else ""))


def load_dotenv() -> None:
    """Load .env into os.environ without overriding what's already set.

    systemd does this via EnvironmentFile; when run by hand it wouldn't
    otherwise be loaded, and every check below would fail confusingly.
    """
    if not DOTENV.exists():
        return
    for line in DOTENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def check_python() -> None:
    v = sys.version_info
    if v >= (3, 11):
        record("PASS", f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        record(
            "FAIL",
            f"Python {v.major}.{v.minor} is too old",
            "Need 3.11 or newer (the bridge uses `X | None` syntax).",
        )


def check_imports() -> None:
    for mod, hint in (
        ("claude_agent_sdk", "pip install -r requirements.txt"),
        ("httpx", "pip install -r requirements.txt"),
    ):
        try:
            __import__(mod)
            record("PASS", f"{mod} importable")
        except ImportError:
            record("FAIL", f"{mod} not installed", hint)


def check_cli() -> None:
    """The Agent SDK drives the Claude Code CLI; a bundled copy also counts."""
    from shutil import which

    if which("claude"):
        record("PASS", "claude CLI on PATH")
        return
    try:
        import claude_agent_sdk

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        if bundled.exists():
            record("PASS", "claude CLI (bundled with the SDK)")
            return
    except ImportError:
        pass
    record(
        "FAIL",
        "claude CLI not found",
        "npm install -g @anthropic-ai/claude-code",
    )


def check_credential() -> None:
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        record("PASS", "Claude auth via CLAUDE_CODE_OAUTH_TOKEN")
        return
    if os.environ.get("ANTHROPIC_API_KEY"):
        record("PASS", "Claude auth via ANTHROPIC_API_KEY")
        return
    home = os.environ.get("HOME")
    if not home:
        record(
            "FAIL",
            "No Claude credential and HOME is unset",
            "Set CLAUDE_CODE_OAUTH_TOKEN in .env, or set HOME so a `claude` login can be found.",
        )
        return
    if (Path(home) / ".claude").is_dir():
        record(
            "WARN",
            "No token in .env; relying on an interactive `claude` login",
            f"Found {home}/.claude. This works only if the service also has HOME set "
            "(the bundled unit sets HOME=/root).",
        )
        return
    record(
        "FAIL",
        "No Claude credential found",
        "Run `claude setup-token` and put the result in .env as CLAUDE_CODE_OAUTH_TOKEN, "
        "or set ANTHROPIC_API_KEY, or run `claude` and log in.",
    )


def check_allowed_users() -> None:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        record(
            "FAIL",
            "TELEGRAM_ALLOWED_USER_IDS is not set",
            "Get your numeric id from @userinfobot and put it in .env. "
            "Without it the bridge refuses to start.",
        )
        return
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        record(
            "FAIL",
            "TELEGRAM_ALLOWED_USER_IDS is not numeric",
            f"Got {raw!r}. It must be numeric ids, not usernames: 123456789,987654321",
        )
        return
    if not ids:
        record("FAIL", "TELEGRAM_ALLOWED_USER_IDS is empty")
        return
    record("PASS", f"{len(ids)} allowed user id(s) configured")


def check_bot_token() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        record(
            "FAIL",
            "TELEGRAM_BOT_TOKEN is not set",
            "Create a bot with @BotFather and put the token in .env.",
        )
        return
    try:
        import httpx
    except ImportError:
        record("WARN", "Cannot verify bot token", "httpx is not installed yet.")
        return
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
    except Exception as e:
        record(
            "WARN",
            "Could not reach Telegram to verify the token",
            f"{type(e).__name__}: {e}. Check outbound network access.",
        )
        return
    if r.status_code == 401:
        record(
            "FAIL",
            "Telegram rejected the bot token",
            "The token in .env is wrong or was revoked. Re-issue it with @BotFather.",
        )
        return
    if r.status_code != 200 or not r.json().get("ok"):
        record("FAIL", f"Telegram returned HTTP {r.status_code}", r.text[:160])
        return
    username = r.json()["result"].get("username", "?")
    record("PASS", f"Bot token valid (@{username})")


def check_paths() -> None:
    workdir = Path(os.environ.get("CLAUDE_WORKDIR", ".")).expanduser()
    if workdir.is_dir():
        record("PASS", f"CLAUDE_WORKDIR exists ({workdir})")
    else:
        record(
            "FAIL",
            f"CLAUDE_WORKDIR does not exist ({workdir})",
            "Point it at the directory the agent should work in, e.g. your HA config checkout.",
        )

    media = Path(os.environ.get("MEDIA_DIR", "/opt/claude-bridge/media"))
    try:
        media.mkdir(parents=True, exist_ok=True)
        probe = media / ".preflight"
        probe.write_text("ok")
        probe.unlink()
        record("PASS", f"MEDIA_DIR writable ({media})")
    except Exception as e:
        record("WARN", f"MEDIA_DIR not writable ({media})", f"{type(e).__name__}: {e}. Photos will be dropped.")

    state = Path(os.environ.get("BRIDGE_STATE_FILE", Path(__file__).with_name("state.json")))
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        probe = state.parent / ".preflight"
        probe.write_text("ok")
        probe.unlink()
        record("PASS", f"State directory writable ({state.parent})")
    except Exception as e:
        record("FAIL", f"Cannot write state file ({state})", f"{type(e).__name__}: {e}")


def check_permission_mode() -> None:
    mode = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
    if mode == "bypassPermissions" and hasattr(os, "geteuid") and os.geteuid() == 0:
        record(
            "FAIL",
            "bypassPermissions cannot be used as root",
            "The CLI refuses --dangerously-skip-permissions as root. Use acceptEdits, "
            "or run the service as a non-root user.",
        )
        return
    record("PASS", f"Permission mode: {mode}")


def main() -> int:
    print("\nclaude-ha-bridge preflight\n")
    load_dotenv()
    if not DOTENV.exists():
        record("WARN", ".env not found", "Copy .env.example to .env and fill it in.")

    check_python()
    check_imports()
    check_cli()
    check_credential()
    check_bot_token()
    check_allowed_users()
    check_paths()
    check_permission_mode()

    fails = sum(1 for lvl, _, _ in RESULTS if lvl == "FAIL")
    warns = sum(1 for lvl, _, _ in RESULTS if lvl == "WARN")
    print(f"\n  {len(RESULTS) - fails - warns} passed · {warns} warning(s) · {fails} failure(s)\n")
    if fails:
        print("  Fix the failures above, then run this again.\n")
        return 1
    print("  Ready. Start it with:  systemctl enable --now claude-bridge\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
