"""
MAX platform adapter (Hermes plugin).

Connects to the MAX messenger (dev.max.ru) Bot API:
- Long-polls ``/updates`` for new messages (dialogs + chats)
- Sends replies via ``POST /messages``
- Uploads photos via ``POST /uploads`` (get URL -> multipart ``data`` ->
  token); there is no direct ``POST /photos`` endpoint (404)
- Receives photo attachments via body.file_id -> GET /files/{id}
- Sustained typing indicator while the agent thinks (``typing_on`` action)
- ``/menu`` command replies with an inline keyboard of the agent's slash
  commands (MAX has no setMyCommands; button presses arrive as
  ``message_callback`` updates and re-enter the agent as commands)

No external SDK — only httpx, already a Hermes dependency.

Configuration in config.yaml::

    platforms:
      max:
        enabled: true
        extra:
          token: "..."            # or MAX_BOT_TOKEN env

Environment variables (env wins over config.yaml ``extra``):

    MAX_BOT_TOKEN              Bot access token (required)
    MAX_ALLOWED_USERS          Allowlist of MAX user IDs (comma-separated)
    MAX_ALLOW_ALL_USERS        Allow any user (dev only)
    MAX_HOME_CHANNEL           Default chat_id for cron delivery
    MAX_HOME_CHANNEL_NAME      Human label for the home channel

Identity model: MAX user_id is server-assigned and stable. Group chats
have negative-ish chat_ids (``chat_id`` != ``user_id`` on message_created
in chats). The adapter passes both through ``build_source`` untouched.
"""

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with default-profile startup fallback.

    Same pattern as the ntfy adapter / Slack APP_TOKEN read (#59739).
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def _core_menu_commands() -> List[tuple]:
    """(name, description) pairs from the shared Hermes command registry.

    Reuses the Telegram menu source: it reads the same core CommandDef
    registry (platform-agnostic), only sanitizing names for Telegram.
    """
    try:
        from hermes_cli.commands import telegram_bot_commands
        return telegram_bot_commands() or []
    except Exception:
        return []


logger = logging.getLogger(__name__)


DEFAULT_API = "https://botapi.max.ru"
MAX_MESSAGE_LENGTH = 4000  # MAX practical text limit
TYPING_REFRESH_S = 2.0     # re-fire typing_on while a turn is in flight (server expires it in ~3s)
MENU_COMMAND = "/menu"
MENU_MAX_BUTTONS = 12
MENU_BUTTONS_PER_ROW = 2
POLL_TIMEOUT_SECONDS = 30  # server-side long poll window
POLL_HTTP_TIMEOUT = POLL_TIMEOUT_SECONDS + 20
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000
PHOTO_CHUNK = 5  # send photos in small groups


class _FatalPollError(Exception):
    """Unrecoverable poll error (bad token, revoked access)."""


def check_requirements() -> bool:
    if not HTTPX_AVAILABLE:
        return False
    return bool(os.getenv("MAX_BOT_TOKEN", "").strip())


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("token") or os.getenv("MAX_BOT_TOKEN", "").strip())


def is_connected(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("MAX_BOT_TOKEN") or extra.get("token"))


class MaxAdapter(BasePlatformAdapter):
    """MAX messenger adapter — long polling + HTTP send."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        platform = Platform("max")
        super().__init__(config=config, platform=platform)

        extra = config.extra or {}
        self._api: str = (extra.get("api") or os.getenv("MAX_API_URL", DEFAULT_API)).rstrip("/")
        self._token: str = (extra.get("token") or _get_scoped_secret("MAX_BOT_TOKEN", "")).strip()

        self._poll_task: Optional[asyncio.Task] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._marker: Optional[int] = None  # long-poll cursor
        self._seen_messages: Dict[str, float] = {}
        self._typing_tasks: Dict[str, "asyncio.Task[None]"] = {}

    # -- HTTP helpers -------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self._token,
            "Content-Type": "application/json",
        }

    async def _api_get(self, path: str, params: Optional[Dict[str, Any]] = None):
        resp = await self._http_client.get(
            f"{self._api}{path}",
            headers=self._headers(),
            params=params,
            timeout=POLL_HTTP_TIMEOUT if path == "/updates" else 20.0,
        )
        if resp.status_code in (401, 403):
            raise _FatalPollError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json()

    # -- Connection lifecycle ------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx not installed. Run: pip install httpx", self.name)
            return False
        if not self._token:
            logger.warning("[%s] MAX_BOT_TOKEN not configured", self.name)
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=None)
            me = await self._api_get("/me")
            logger.info(
                "[%s] Connected as @%s (%s)",
                self.name, me.get("username", "?"), me.get("name", "?"),
            )
            self._poll_task = asyncio.create_task(self._run_poll())
            self._mark_connected()
            return True
        except _FatalPollError as e:
            logger.error("[%s] Auth failed: %s", self.name, e)
            self._set_fatal_error("max_unauthorized", str(e), retryable=False)
            return False
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _run_poll(self) -> None:
        backoff_idx = 0
        while self._running:
            try:
                params: Dict[str, Any] = {
                    "timeout": POLL_TIMEOUT_SECONDS,
                    "types": "message_created,message_callback",
                }
                if self._marker is not None:
                    params["marker"] = self._marker
                data = await self._api_get("/updates", params=params)
                poll_start = time.monotonic()

                for update in data.get("updates", []):
                    try:
                        self._marker = update.get("marker", self._marker)
                        await self._on_update(update)
                    except Exception:
                        logger.exception("[%s] Update handler failed", self.name)

                if time.monotonic() - poll_start >= 30.0:
                    backoff_idx = 0
            except asyncio.CancelledError:
                return
            except _FatalPollError as e:
                logger.error("[%s] Fatal poll error: %s — stopping", self.name, e)
                self._set_fatal_error("max_fatal", str(e), retryable=False)
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Poll error: %s", self.name, e)
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                await asyncio.sleep(delay)
                backoff_idx += 1

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        for task in self._typing_tasks.values():
            if not task.done():
                task.cancel()
        self._typing_tasks.clear()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._seen_messages.clear()
        logger.info("[%s] Disconnected", self.name)

    # -- Inbound ---------------------------------------------------------------

    async def _on_update(self, update: Dict[str, Any]) -> None:
        utype = update.get("update_type")
        if utype == "bot_started":
            chat = (update.get("chat") or {})
            logger.info("[%s] Bot started in chat %s", self.name, chat.get("chat_id"))
            return
        if utype == "message_callback":
            await self._on_callback(update)
            return
        if utype != "message_created":
            return

        message = update.get("message") or {}
        msg_id = str(message.get("body", {}).get("mid") or message.get("timestamp") or uuid.uuid4().hex)
        if self._is_duplicate(msg_id):
            return

        # MAX update layout: sender lives in "sender" (not "from"), the chat id
        # lives in message.recipient.chat_id for dialogs (chat object only
        # appears for group chats).
        sender = message.get("sender") or message.get("from") or {}
        recipient = message.get("recipient") or {}
        chat = message.get("chat") or {}
        chat_id = (
            str(chat.get("chat_id") or recipient.get("chat_id") or "")
            or str(sender.get("user_id", ""))
        )
        user_id = str(sender.get("user_id", ""))
        user_name = " ".join(
            x for x in (sender.get("first_name"), sender.get("last_name")) if x
        ) or sender.get("username") or user_id
        chat_title = chat.get("title") or user_name
        is_group = bool(chat.get("chat_id")) or recipient.get("chat_type") in ("chat", "group", "channel")
        chat_type = "group" if is_group else "dm"

        # skip bot's own outgoing messages (recipient.user_id == our bot id in dialogs)
        if sender.get("is_bot"):
            return

        text = (message.get("body") or {}).get("text") or ""

        if text.strip().lower() == MENU_COMMAND:
            await self.send_menu(chat_id)
            return

        # photo attachments -> fetch and expose as file refs
        attachments = []
        for att in message.get("attachments", []) or []:
            info = att.get("info") or {}
            if att.get("type") == "image" and info.get("file_id"):
                attachments.append(("image", str(info["file_id"])))

        if not text and not attachments:
            logger.debug("[%s] Empty message, skipping", self.name)
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_title,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )
        ts = message.get("created_at") or message.get("timestamp")
        try:
            timestamp = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc) if int(ts) > 1e12 \
                else datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        event = MessageEvent(
            text=text or "[photo]",
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            raw_message=message,
            timestamp=timestamp,
        )
        logger.debug("[%s] Message from %s: %s", self.name, user_name, text[:80])
        await self.handle_message(event)

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}
        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    async def _on_callback(self, update: Dict[str, Any]) -> None:
        """Inline-keyboard button press -> answer the callback, run the command.

        The button payload is a slash command (``/status``), so the press is
        re-surfaced to the agent as a COMMAND event from the pressing user.
        """
        callback = update.get("callback") or {}
        payload = (callback.get("payload") or "").strip()
        callback_id = callback.get("callback_id")
        user = callback.get("user") or {}
        message = update.get("message") or {}

        recipient = message.get("recipient") or {}
        chat = message.get("chat") or {}
        chat_id = (
            str(chat.get("chat_id") or recipient.get("chat_id") or "")
            or str(user.get("user_id", ""))
        )

        # stop the client-side spinner even if we can't run the payload
        if callback_id and self._http_client:
            try:
                await self._http_client.post(
                    f"{self._api}/answers",
                    headers=self._headers(),
                    params={"callback_id": callback_id},
                    json={},
                    timeout=10.0,
                )
            except Exception as e:
                logger.debug("[%s] answer callback failed (ignored): %s", self.name, e)

        if not payload:
            return
        if payload.strip().lower() == MENU_COMMAND:
            # /menu is adapter-local (not a Hermes slash command) — show the
            # menu instead of forwarding it to the agent
            await self.send_menu(chat_id)
            return

        user_id = str(user.get("user_id", ""))
        user_name = " ".join(
            x for x in (user.get("first_name"), user.get("last_name")) if x
        ) or user.get("username") or user_id

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat.get("title") or user_name,
            chat_type="group" if chat.get("chat_id") else "dm",
            user_id=user_id,
            user_name=user_name,
        )
        event = MessageEvent(
            text=payload,
            message_type=MessageType.COMMAND,
            source=source,
            message_id=f"cb.{callback_id}",
            raw_message=update,
            timestamp=datetime.now(tz=timezone.utc),
        )
        await self.handle_message(event)

    # -- Outbound --------------------------------------------------------------

    def _build_menu_keyboard(self) -> List[List[Dict[str, Any]]]:
        rows: List[List[Dict[str, Any]]] = []
        row: List[Dict[str, Any]] = []
        for name, _desc in _core_menu_commands()[:MENU_MAX_BUTTONS]:
            row.append({"type": "callback", "text": f"/{name}", "payload": f"/{name}"})
            if len(row) >= MENU_BUTTONS_PER_ROW:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    async def send_menu(self, chat_id: str) -> None:
        """Send the bot command menu as an inline keyboard (MAX Bot API has
        no setMyCommands equivalent — menus are inline keyboards)."""
        if not self._http_client:
            return
        keyboard = self._build_menu_keyboard()
        try:
            resp = await self._http_client.post(
                f"{self._api}/messages",
                headers=self._headers(),
                params={"chat_id": chat_id},
                json={
                    "text": "Меню команд — нажмите кнопку:",
                    "attachments": [{
                        "type": "inline_keyboard",
                        "payload": {"buttons": keyboard},
                    }],
                },
                timeout=20.0,
            )
            if resp.status_code >= 300:
                logger.warning(
                    "[%s] send_menu HTTP %s: %s",
                    self.name, resp.status_code, resp.text[:200],
                )
        except Exception as e:
            logger.warning("[%s] send_menu failed: %s", self.name, e)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to a MAX chat.

        NOTE: MAX Bot API takes ``chat_id``/``user_id`` as QUERY parameters,
        not JSON body fields (official SDK: ``query: { chat_id, user_id }``).
        Putting them in the body yields ``Unknown recipient``.
        """
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        # dialog chat_id == peer user_id on MAX; pass it as user_id query param
        self._stop_typing(chat_id)
        params: Dict[str, str] = {"chat_id": chat_id}
        payload: Dict[str, Any] = {"text": content[:MAX_MESSAGE_LENGTH]}
        if reply_to and str(reply_to).startswith("mid."):
            payload["link"] = {"type": "reply", "mid": reply_to}
        try:
            resp = await self._http_client.post(
                f"{self._api}/messages",
                headers=self._headers(),
                params=params,
                json=payload,
                timeout=20.0,
            )
            if resp.status_code < 300:
                data = resp.json() if resp.text else {}
                return SendResult(
                    success=True,
                    message_id=str((data.get("message") or {}).get("body", {}).get("mid", "")),
                )
            logger.warning("[%s] Send failed HTTP %d: %s", self.name, resp.status_code, resp.text[:200])
            return SendResult(success=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error("[%s] Send error: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def send_media(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a photo and send it to a MAX chat.

        There is no ``POST /photos`` endpoint (404). The working flow:
        ``POST /uploads?chat_id=&type=image`` returns an ``url``; the file
        is POSTed to that url as multipart field ``data``; the response's
        ``photos`` is a **dict** ``{photoId: {"token": ...}}`` (not a list);
        the token goes into the message attachment payload.
        """
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")
        try:
            resp = await self._http_client.post(
                f"{self._api}/uploads",
                headers=self._headers(),
                params={"chat_id": chat_id, "type": "image"},
                timeout=20.0,
            )
            if resp.status_code >= 300:
                return SendResult(success=False, error=f"get upload URL HTTP {resp.status_code}: {resp.text[:200]}")
            upload_url = (resp.json() or {}).get("url")
            if not upload_url:
                return SendResult(success=False, error="uploads returned no url")

            with open(file_path, "rb") as f:
                resp = await self._http_client.post(
                    upload_url,
                    files={"data": f},
                    timeout=120.0,
                )
            if resp.status_code >= 300:
                return SendResult(success=False, error=f"upload HTTP {resp.status_code}: {resp.text[:200]}")
            photos = (resp.json() or {}).get("photos") or {}
            token = next(iter(photos.values()), {}).get("token") if photos else None
            if not token:
                return SendResult(success=False, error="upload returned no photo token")

            payload: Dict[str, Any] = {
                "text": caption[:MAX_MESSAGE_LENGTH] if caption else None,
                "attachments": [{"type": "image", "payload": {"token": token}}],
            }
            resp = await self._http_client.post(
                f"{self._api}/messages",
                headers=self._headers(),
                params={"chat_id": chat_id},
                json=payload,
                timeout=20.0,
            )
            if resp.status_code < 300:
                return SendResult(success=True, message_id="")
            return SendResult(success=False, error=f"send HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error("[%s] send_media error: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Show a typing indicator via POST /chats/{chat_id}/actions.

        The action type is ``typing_on`` (see the official SDK's SenderAction).
        The indicator auto-expires server-side after a few seconds, so this
        also (re)spawns a lightweight refresher task that re-fires the action
        every TYPING_REFRESH_S until the next outbound message lands in the
        chat or the task is cancelled — giving a sustained Telegram-like
        indicator while the agent thinks. Failures are swallowed: a dropped
        indicator must never break a turn.
        """
        # one-shot fire
        self._fire_typing(chat_id)
        # refresh loop (single task per chat)
        prev = self._typing_tasks.get(chat_id)
        if prev is not None and not prev.done():
            return  # already refreshing
        try:
            self._typing_tasks[chat_id] = asyncio.create_task(
                self._typing_refresher(chat_id)
            )
        except RuntimeError:  # no running loop (unit tests) — one-shot only
            pass

    def _stop_typing(self, chat_id: str) -> None:
        """Cancel the typing refresher once a real message lands in the chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _fire_typing(self, chat_id: str) -> None:
        # NOTE: the shared client from connect() has no base_url, so the URL
        # must be absolute — a relative path makes httpx raise, which the old
        # code swallowed (no indicator was ever sent). Same for the auth
        # header: it is not set on the client, only per-request.
        if self._http_client is None:
            return
        try:
            resp = asyncio.ensure_future(
                self._http_client.post(
                    f"{self._api}/chats/{chat_id}/actions",
                    headers=self._headers(),
                    json={"action": "typing_on"},
                    timeout=10.0,
                )
            )
            _ = resp  # fire-and-forget; errors surface in the refresher loop
        except Exception as e:
            logger.debug("[%s] send_typing failed (ignored): %s", self.name, e)

    async def _typing_refresher(self, chat_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(TYPING_REFRESH_S)
                client = self._http_client
                if client is None:
                    return
                try:
                    resp = await client.post(
                        f"{self._api}/chats/{chat_id}/actions",
                        headers=self._headers(),
                        json={"action": "typing_on"},
                        timeout=10.0,
                    )
                    if resp.status_code >= 300:
                        logger.debug(
                            "[%s] typing refresh HTTP %s (stopping)",
                            self.name, resp.status_code,
                        )
                        return
                except Exception:
                    return  # network gone — stop silently
        except asyncio.CancelledError:
            pass
        finally:
            self._typing_tasks.pop(chat_id, None)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        try:
            chats = await self._api_get("/chats")
            for c in chats.get("chats", []):
                if str(c.get("chat_id")) == str(chat_id):
                    return {"name": c.get("title", chat_id), "type": "group" if c.get("type") == "chat" else "dm"}
        except Exception:
            pass
        return {"name": chat_id, "type": "dm"}


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def _env_enablement() -> dict | None:
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if not token:
        return None
    seed: dict = {"token": token}
    home = os.getenv("MAX_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("MAX_HOME_CHANNEL_NAME", home),
        }
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process send for cron / send_message_tool fallbacks."""
    if not HTTPX_AVAILABLE:
        return {"error": "max standalone send: httpx not installed"}
    extra = getattr(pconfig, "extra", {}) or {}
    token = extra.get("token") or _get_scoped_secret("MAX_BOT_TOKEN", "")
    api = (extra.get("api") or os.getenv("MAX_API_URL", DEFAULT_API)).rstrip("/")
    if not token:
        return {"error": "max standalone send: MAX_BOT_TOKEN not configured"}
    if not chat_id:
        home = extra.get("home_channel")
        chat_id = getattr(home, "chat_id", None) if home else None
        if not chat_id:
            return {"error": "max standalone send: no chat_id"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{api}/messages",
                headers={"Authorization": token, "Content-Type": "application/json"},
                params={"chat_id": chat_id},
                json={"text": message[:MAX_MESSAGE_LENGTH]},
            )
        if resp.status_code >= 300:
            return {"error": f"max HTTP {resp.status_code}: {resp.text[:200]}"}
        return {"success": True, "platform": "max", "chat_id": chat_id}
    except Exception as e:
        return {"error": f"max standalone send failed: {e}"}


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="max",
        label="MAX",
        adapter_factory=lambda cfg: MaxAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["MAX_BOT_TOKEN"],
        install_hint="pip install httpx   # already a Hermes dependency",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MAX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="MAX_ALLOWED_USERS",
        allow_all_env="MAX_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        platform_hint=(
            "You are communicating via the MAX messenger. MAX supports "
            "markdown in messages. Keep responses under 4000 characters "
            "per message. The command menu is available via /menu."
        ),
    )
