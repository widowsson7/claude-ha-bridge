"""Telegram -> Claude Agent SDK bridge.

Runs a persistent Claude agent on an always-on box (e.g. the machine next to
Home Assistant) and lets you drive it from Telegram. Telegram queues messages
through any connection quality, and all agent state lives server-side here, so
a flaky phone connection never loses a session.

Sessions are per chat *and* per forum topic: in a Telegram group with Topics
enabled, each topic ("subject tab") gets its own persistent Claude session,
keyed "chat_id:thread_id" in state.json. Private chats and a forum's General
topic use thread id 0.

Resource controls (single small container, many possible tabs):
  - Idle reaper: a tab's Claude client is disconnected after
    BRIDGE_IDLE_TIMEOUT seconds (default 600) of inactivity and resumes
    transparently from its saved session id on the next message.
  - Turn cap: at most BRIDGE_MAX_CONCURRENT (default 2) tabs run a Claude
    turn simultaneously; further tabs queue and say so.

Auth for Claude resolves the same way the SDK/CLI does: ANTHROPIC_API_KEY, or
a `claude` CLI login on this machine. MCP servers (e.g. Home Assistant) are
inherited from the existing Claude Code config via setting_sources.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

log = logging.getLogger("claude-bridge")


def _load_dotenv() -> None:
    """Load a .env sitting next to this file, without overriding the real env.

    systemd supplies these through EnvironmentFile=, so this is a no-op under
    the service. It exists so that running `python bridge.py` by hand works
    instead of dying on the first os.environ[...] lookup below.
    """
    path = Path(__file__).with_name(".env")
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(x) for x in os.environ["TELEGRAM_ALLOWED_USER_IDS"].split(",") if x.strip()
}

WORKDIR = Path(os.environ.get("CLAUDE_WORKDIR", ".")).expanduser().resolve()
# Downloaded Telegram media (photos/documents/voice) land here; Claude Code
# can read local files, so passing the path + caption is enough for the
# agent to actually see the image.
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/opt/claude-bridge/media"))
# Directories Claude Code may access beyond the project dir (the
# --add-dir / ClaudeAgentOptions.add_dirs sandbox boundary). A
# permissions.allow rule does NOT extend this boundary; without the dir
# being listed here, Read/Grep on e.g. /opt/claude-bridge/bridge.py are
# blocked even with an allow rule present.
ADD_DIRS = [
    Path(p)
    for p in os.environ.get("CLAUDE_ADD_DIRS", "/opt/claude-bridge").split(",")
    if p.strip()
]
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
MODEL = os.environ.get("CLAUDE_MODEL") or None
SHOW_TOOLS = os.environ.get("BRIDGE_SHOW_TOOLS", "0") == "1"
STATE_FILE = Path(
    os.environ.get("BRIDGE_STATE_FILE", Path(__file__).with_name("state.json"))
)
IDLE_TIMEOUT = int(os.environ.get("BRIDGE_IDLE_TIMEOUT", "600"))
MAX_CONCURRENT = int(os.environ.get("BRIDGE_MAX_CONCURRENT", "2"))
REAPER_INTERVAL = 60

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_MSG_LIMIT = 4000  # hard limit is 4096; leave headroom

SYSTEM_APPEND = (
    "You are being driven from a phone via Telegram. Keep responses concise and "
    "phone-readable: plain text only (no markdown tables or headers), short "
    "paragraphs, lead with the answer. This machine hosts the user's Home "
    "Assistant tooling; use the configured MCP tools to inspect state, logs, "
    "and configuration when relevant."
)


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # Migrate pre-topics keys ("<chat_id>") to "<chat_id>:0".
    migrated = {}
    changed = False
    for k, v in state.items():
        if ":" not in k:
            migrated[f"{k}:0"] = v
            changed = True
        else:
            migrated[k] = v
    if changed:
        STATE_FILE.write_text(json.dumps(migrated, indent=2))
    return migrated


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def media_to_text(tg: "Telegram", msg: dict) -> str | None:
    """If msg carries a photo/document/voice/video, download it and return a
    text description Claude can use (local path + caption); else None.

    Media messages arrive with `caption`, not `text`; before this helper the
    update loop's `if not text: continue` silently dropped them all.
    """
    caption = (msg.get("caption") or "").strip()
    photo = msg.get("photo")
    document = msg.get("document") or {}
    voice = msg.get("voice") or {}
    video = msg.get("video") or {}

    if photo:
        # photo is a list of sizes ordered small->large; take the largest.
        file_id = photo[-1]["file_id"]
        label = "📷 Photo"
    elif document:
        file_id = document.get("file_id")
        fname = document.get("file_name") or ""
        label = f"📄 Document{f' ({fname})' if fname else ''}"
    elif voice:
        file_id = voice.get("file_id")
        label = "🎤 Voice message"
    elif video:
        file_id = video.get("file_id")
        label = "🎬 Video"
    else:
        return None

    if not file_id:
        return None

    local = await tg.download_file(file_id)
    if local is None:
        return None
    desc = f"{label}: {local}"
    if caption:
        desc += f"\nCaption: {caption}"
    return desc


class Telegram:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(70, connect=10))

    async def get_updates(self, offset: int) -> list[dict]:
        r = await self.http.get(
            f"{API}/getUpdates",
            params={"offset": offset, "timeout": 50, "allowed_updates": '["message"]'},
        )
        r.raise_for_status()
        return r.json()["result"]

    async def send(self, chat_id: int, text: str, thread_id: int | None = None) -> None:
        if not text.strip():
            return
        for i in range(0, len(text), TELEGRAM_MSG_LIMIT):
            payload = {"chat_id": chat_id, "text": text[i : i + TELEGRAM_MSG_LIMIT]}
            if thread_id:
                payload["message_thread_id"] = thread_id
            r = await self.http.post(f"{API}/sendMessage", json=payload)
            # A failed send is otherwise silent -- log it so a bad thread id
            # (e.g. a DM topic id reaching a group) is visible, not dropped.
            if r.status_code >= 300:
                log.warning(
                    "tg.send failed: HTTP %s chat=%s thread=%s: %s",
                    r.status_code, chat_id, thread_id, r.text[:200],
                )

    async def download_file(self, file_id: str) -> Path | None:
        """Download a Telegram file (photo/document/voice) by file_id to
        MEDIA_DIR; returns the local path, or None on failure."""
        try:
            r = await self.http.get(f"{API}/getFile", params={"file_id": file_id})
            r.raise_for_status()
            file_path = r.json()["result"]["file_path"]
            r2 = await self.http.get(
                f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            )
            r2.raise_for_status()
            MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            dest = MEDIA_DIR / Path(file_path).name
            dest.write_bytes(r2.content)
            log.info(
                "downloaded media %s -> %s (%d bytes)", file_id, dest, len(r2.content)
            )
            return dest
        except Exception:
            log.warning("media download failed for file_id=%s", file_id, exc_info=True)
            return None

    async def typing(self, chat_id: int, thread_id: int | None = None) -> None:
        payload = {"chat_id": chat_id, "action": "typing"}
        if thread_id:
            payload["message_thread_id"] = thread_id
        await self.http.post(f"{API}/sendChatAction", json=payload)


class ChatSession:
    """One persistent Claude session per Telegram chat/topic ("tab")."""

    def __init__(
        self,
        chat_id: int,
        thread_id: int | None,
        tg: Telegram,
        state: dict,
        turn_sem: asyncio.Semaphore,
    ):
        self.chat_id = chat_id
        self.thread_id = thread_id  # None = private chat or forum General topic
        self.tg = tg
        self.state = state
        self.turn_sem = turn_sem
        self.client: ClaudeSDKClient | None = None
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.busy = False
        self.last_activity = time.monotonic()
        # Guards client lifecycle: the worker holds it while processing a
        # message, the reaper holds it while disconnecting an idle client.
        self.lock = asyncio.Lock()
        self.worker_task = asyncio.create_task(self._worker())

    @property
    def state_key(self) -> str:
        return f"{self.chat_id}:{self.thread_id or 0}"

    @property
    def session_id(self) -> str | None:
        return self.state.get(self.state_key)

    async def _send(self, text: str) -> None:
        await self.tg.send(self.chat_id, text, self.thread_id)

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self.client is None:
            options = ClaudeAgentOptions(
                permission_mode=PERMISSION_MODE,
                model=MODEL,
                cwd=str(WORKDIR),
                # Inherit the machine's existing Claude Code config, including
                # MCP servers (Home Assistant etc.) and permission settings.
                setting_sources=["user", "project", "local"],
                system_prompt={
                    "type": "preset",
                    "preset": "claude_code",
                    "append": SYSTEM_APPEND,
                },
                resume=self.session_id,
                # Extend the sandbox boundary beyond the project dir so the
                # agent can Read/Grep its own bridge source. permissions.allow
                # alone cannot do this.
                add_dirs=ADD_DIRS,
            )
            self.client = ClaudeSDKClient(options=options)
            await self.client.connect()
        return self.client

    async def _drop_client(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def _reset(self) -> None:
        await self._drop_client()
        self.state.pop(self.state_key, None)
        save_state(self.state)

    async def _remember_session_id(self) -> None:
        try:
            info = await self.client.get_server_info()
            sid = (info or {}).get("session_id")
            if sid and sid != self.session_id:
                self.state[self.state_key] = sid
                save_state(self.state)
        except Exception:
            log.debug("could not read session id", exc_info=True)

    async def _typing_loop(self) -> None:
        while True:
            try:
                await self.tg.typing(self.chat_id, self.thread_id)
            except Exception:
                pass
            await asyncio.sleep(4)

    async def _run_query(self, text: str) -> None:
        client = await self._ensure_client()
        typing = asyncio.create_task(self._typing_loop())
        try:
            await client.query(text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    parts = []
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                        elif isinstance(block, ToolUseBlock) and SHOW_TOOLS:
                            await self._send(f"🔧 {block.name}")
                    if parts:
                        await self._send("\n".join(parts))
                elif isinstance(message, ResultMessage):
                    # ResultMessage reliably carries the session id; get_server_info
                    # does not on all SDK versions.
                    sid = getattr(message, "session_id", None)
                    if sid and sid != self.session_id:
                        self.state[self.state_key] = sid
                        save_state(self.state)
                    if message.subtype == "error":
                        await self._send(f"⚠️ Turn ended with an error: {message.result}")
            await self._remember_session_id()
        finally:
            typing.cancel()

    async def _worker(self) -> None:
        while True:
            text = await self.queue.get()
            async with self.lock:
                self.busy = True
                try:
                    if text == "/new":
                        await self._reset()
                        await self._send("🆕 Started a fresh session.")
                    else:
                        if self.turn_sem.locked():
                            await self._send("⏳ Queued behind another tab's turn...")
                        async with self.turn_sem:
                            await self._run_query(text)
                except Exception as e:
                    log.exception("query failed")
                    await self._send(f"⚠️ Error: {e}\nRetrying will resume the session.")
                    # Drop the client; next message reconnects with resume=session_id.
                    await self._drop_client()
                finally:
                    self.busy = False
                    self.last_activity = time.monotonic()
                    self.queue.task_done()

    async def maybe_reap(self) -> None:
        """Disconnect this tab's client if it has been idle long enough.

        The saved session id stays in state.json, so the next message in the
        tab resumes the same conversation; only the process memory is freed.
        """
        if (
            self.client is None
            or self.busy
            or self.lock.locked()
            or not self.queue.empty()
            or time.monotonic() - self.last_activity < IDLE_TIMEOUT
        ):
            return
        async with self.lock:
            if (
                self.client is not None
                and not self.busy
                and self.queue.empty()
                and time.monotonic() - self.last_activity >= IDLE_TIMEOUT
            ):
                await self._drop_client()
                log.info("reaped idle client for %s (session kept)", self.state_key)

    async def interrupt(self) -> None:
        if self.client is not None and self.busy:
            await self.client.interrupt()
            await self._send("🛑 Interrupted.")
        else:
            await self._send("Nothing running.")

    async def status(self) -> None:
        queued = self.queue.qsize()
        await self._send(
            f"{'🏃 Working' if self.busy else '💤 Idle'}"
            f" · queued: {queued}"
            f" · session: {self.session_id or 'none yet'}"
            f" · client: {'live' if self.client else 'sleeping (resumes on next message)'}"
            f" · workdir: {WORKDIR}",
        )


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs the full request URL at INFO, and the bot token is part of
    # every Telegram URL -- that would write the credential into the journal
    # in plaintext on every poll. Only raise this if you are debugging, and
    # remember what it exposes.
    logging.getLogger("httpx").setLevel(
        os.environ.get("BRIDGE_HTTPX_LOG_LEVEL", "WARNING")
    )
    tg = Telegram()
    state = load_state()
    turn_sem = asyncio.Semaphore(MAX_CONCURRENT)
    sessions: dict[tuple[int, int], ChatSession] = {}
    offset = 0
    log.info(
        "bridge up · workdir=%s · allowed=%s · idle_timeout=%ss · max_concurrent=%s",
        WORKDIR,
        sorted(ALLOWED_USER_IDS),
        IDLE_TIMEOUT,
        MAX_CONCURRENT,
    )

    async def reaper() -> None:
        while True:
            await asyncio.sleep(REAPER_INTERVAL)
            for s in list(sessions.values()):
                try:
                    await s.maybe_reap()
                except Exception:
                    log.exception("reaper failed for %s", s.state_key)

    asyncio.create_task(reaper())

    while True:
        try:
            updates = await tg.get_updates(offset)
        except Exception:
            log.warning("getUpdates failed; retrying in 5s", exc_info=True)
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            text = msg.get("text")
            user_id = (msg.get("from") or {}).get("id")
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id is None:
                continue
            # Media messages carry caption, not text: download the media and
            # hand Claude a local path + caption so photos actually reach the
            # agent instead of being silently dropped.
            if not text:
                text = await media_to_text(tg, msg)
            if not text:
                continue

            if user_id not in ALLOWED_USER_IDS:
                log.warning("ignored message from unauthorized user %s", user_id)
                continue

            # In group chats Telegram may suffix commands with the bot's
            # username ("/status@your_bot"); strip it.
            if text.startswith("/"):
                first, _, rest = text.partition(" ")
                first = first.split("@", 1)[0]
                text = first + ((" " + rest) if rest else "")

            # Forum-topic messages carry a thread id; each topic is its own tab.
            thread_id = (
                msg.get("message_thread_id") if msg.get("is_topic_message") else None
            )
            key = (chat_id, thread_id or 0)

            session = sessions.get(key)
            if session is None:
                session = sessions[key] = ChatSession(
                    chat_id, thread_id, tg, state, turn_sem
                )

            # /stop and /status act immediately; everything else is queued and
            # processed in order, so rapid-fire messages from a moving phone
            # are never lost.
            if text == "/stop":
                await session.interrupt()
            elif text == "/status":
                await session.status()
            elif text == "/start":
                await tg.send(
                    chat_id,
                    "👋 Claude bridge ready.\n"
                    "Just type to talk. Each forum topic is its own session (tab).\n"
                    "Commands (per tab):\n"
                    "/new — fresh session\n"
                    "/stop — interrupt the current task\n"
                    "/status — what's happening",
                    thread_id,
                )
            else:
                session.last_activity = time.monotonic()
                await session.queue.put(text)


if __name__ == "__main__":
    asyncio.run(main())
