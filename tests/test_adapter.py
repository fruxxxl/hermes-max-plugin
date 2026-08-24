"""Offline unit tests for the MAX platform adapter (no token, no network)."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "usr_local_stub")
) if False else None

# The Hermes venv ships pytest plugins that import (and sometimes redirect)
# the installed plugins.* tree before this file runs, so plain sys.path
# tricks cannot guarantee the repo copy of the adapter is tested. Load the
# adapter module directly from its file instead; its own imports (gateway.*,
# agent.*) resolve normally against HERMES_ROOT.
HERMES_ROOT = os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent")
if HERMES_ROOT not in sys.path:
    sys.path.insert(0, HERMES_ROOT)

import importlib.util  # noqa: E402

_ADAPTER = str(Path(__file__).resolve().parents[1] / "plugins" / "platforms" / "max" / "adapter.py")
_spec = importlib.util.spec_from_file_location("max_adapter_under_test", _ADAPTER)
adapter_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adapter_mod)

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.base import MessageType  # noqa: E402
from plugins.platforms.max.adapter import MaxAdapter  # noqa: E402,F401  (re-export; may be the same file or the installed one)

MaxAdapter = adapter_mod.MaxAdapter

DIALOG_UPDATE = {
    "update_type": "message_created",
    "message": {
        "recipient": {"chat_id": 443805158, "chat_type": "dialog", "user_id": 421751262},
        "timestamp": 1787482066199,
        "body": {"mid": "mid.t1", "seq": 1, "text": "привет, ты как?"},
        "sender": {"user_id": 55608376, "first_name": "Александр", "is_bot": False},
    },
}

GROUP_UPDATE = {
    "update_type": "message_created",
    "message": {
        "recipient": {"chat_id": -999, "chat_type": "group", "user_id": 421751262},
        "timestamp": 1787482066200,
        "body": {"mid": "mid.t2", "seq": 2, "text": "группа привет"},
        "sender": {"user_id": 55608376, "first_name": "Александр", "is_bot": False},
        "chat": {"chat_id": -999, "type": "group", "title": "Семья"},
    },
}

OWN_ECHO_UPDATE = {
    "update_type": "message_created",
    "message": {
        "recipient": {"chat_id": 443805158, "chat_type": "dialog", "user_id": 55608376},
        "timestamp": 1787482066201,
        "body": {"mid": "mid.t3", "seq": 3, "text": "мой ответ"},
        "sender": {"user_id": 421751262, "first_name": "Аи ассистент", "is_bot": True},
    },
}

MENU_UPDATE = {
    "update_type": "message_created",
    "message": {
        "recipient": {"chat_id": 443805158, "chat_type": "dialog", "user_id": 421751262},
        "timestamp": 1787482066202,
        "body": {"mid": "mid.m4", "seq": 4, "text": "/menu"},
        "sender": {"user_id": 55608376, "first_name": "Александр", "is_bot": False},
    },
}

CALLBACK_UPDATE = {
    "update_type": "message_callback",
    "callback": {
        "timestamp": 1787482099,
        "callback_id": "cb-1",
        "payload": "/status",
        "user": {"user_id": 55608376, "first_name": "Александр", "is_bot": False},
    },
    "message": {
        "recipient": {"chat_id": 443805158, "chat_type": "dialog"},
        "body": {"mid": "mid.m5", "text": "Меню команд"},
    },
}


class FakeResp:
    status_code = 200
    text = '{"message":{"body":{"mid":"mid.out"}}}'

    def json(self):
        return {"message": {"body": {"mid": "mid.out"}}}


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append((url, kw))
        return FakeResp()


def make_adapter():
    a = MaxAdapter(PlatformConfig(enabled=True))
    a._token = "tok"
    a._running = True
    return a


def collect(a):
    evs = []

    async def h(ev):
        evs.append(ev)

    a.handle_message = h
    return evs


def test_dialog_update_parsed():
    a = make_adapter()
    evs = collect(a)
    asyncio.run(a._on_update(DIALOG_UPDATE))
    assert len(evs) == 1
    src, ev = evs[0].source, evs[0]
    assert src.chat_id == "443805158"
    assert src.chat_type == "dm"
    assert src.user_id == "55608376"
    assert ev.text == "привет, ты как?"


def test_group_update_parsed():
    a = make_adapter()
    evs = collect(a)
    asyncio.run(a._on_update(GROUP_UPDATE))
    assert len(evs) == 1
    src = evs[0].source
    assert src.chat_id == "-999"
    assert src.chat_type == "group"


def test_own_echo_skipped():
    a = make_adapter()
    evs = collect(a)
    asyncio.run(a._on_update(OWN_ECHO_UPDATE))
    assert evs == []


def test_duplicate_skipped():
    a = make_adapter()
    evs = collect(a)
    asyncio.run(a._on_update(DIALOG_UPDATE))
    asyncio.run(a._on_update(DIALOG_UPDATE))
    assert len(evs) == 1


def test_send_wire_shape():
    a = make_adapter()
    fh = FakeHttp()
    a._http_client = fh
    r = asyncio.run(a.send("443805158", "тест", reply_to="mid.t1"))
    assert r.success and r.message_id == "mid.out"
    url, kw = fh.calls[0]
    # pitfall #1: chat_id as QUERY param, never in the body
    assert kw["params"] == {"chat_id": "443805158"}
    assert "chat_id" not in kw["json"]
    assert kw["json"]["text"] == "тест"
    # replies use the link structure
    assert kw["json"]["link"] == {"type": "reply", "mid": "mid.t1"}


def test_send_truncates_long_text():
    a = make_adapter()
    fh = FakeHttp()
    a._http_client = fh
    asyncio.run(a.send("1", "x" * 5000))
    assert len(fh.calls[0][1]["json"]["text"]) <= 4000


def test_typing_uses_absolute_url_and_auth():
    # regression: the typing POST used a relative path against a client with
    # no base_url and no Authorization header — httpx raised, the error was
    # swallowed, and no indicator was ever sent
    a = make_adapter()
    fh = FakeHttp()
    a._http_client = fh

    async def run():
        await a.send_typing("443805158")
        await asyncio.sleep(0.01)  # let the fire-and-forget POST land
        a._stop_typing("443805158")

    asyncio.run(run())
    url, kw = fh.calls[0]
    assert url == "https://botapi.max.ru/chats/443805158/actions"
    assert kw["headers"]["Authorization"] == "tok"
    assert kw["json"] == {"action": "typing_on"}


def test_menu_command_sends_inline_keyboard(monkeypatch):
    monkeypatch.setattr(
        adapter_mod, "_core_menu_commands", lambda: [("status", "s"), ("reset", "r"), ("stop", "t")]
    )
    a = make_adapter()
    fh = FakeHttp()
    a._http_client = fh
    evs = collect(a)
    asyncio.run(a._on_update(MENU_UPDATE))
    assert evs == []  # handled by the adapter, not forwarded to the agent
    url, kw = fh.calls[0]
    assert url.endswith("/messages")
    att = kw["json"]["attachments"][0]
    assert att["type"] == "inline_keyboard"
    buttons = att["payload"]["buttons"]
    assert buttons[0][0]["payload"] == "/status"
    assert buttons[0][1]["payload"] == "/reset"
    assert buttons[1][0]["payload"] == "/stop"  # 3 buttons -> rows of 2 + 1


def test_callback_answered_and_runs_command():
    a = make_adapter()
    fh = FakeHttp()
    a._http_client = fh
    evs = collect(a)
    asyncio.run(a._on_update(CALLBACK_UPDATE))
    # the callback is answered first (stops the client spinner)
    url, kw = fh.calls[0]
    assert url.endswith("/answers")
    assert kw["params"] == {"callback_id": "cb-1"}
    # the button payload re-enters the agent as a slash command
    assert len(evs) == 1
    ev = evs[0]
    assert ev.text == "/status"
    assert ev.message_type == MessageType.COMMAND
    assert ev.source.chat_id == "443805158"
    assert ev.source.user_id == "55608376"


def test_menu_callback_shows_menu_not_unknown_command(monkeypatch):
    # /menu is adapter-local: the callback must re-send the keyboard instead
    # of forwarding /menu to the agent (which would answer "Unknown command")
    monkeypatch.setattr(
        adapter_mod, "_core_menu_commands", lambda: [("status", "s")]
    )
    a = make_adapter()
    fh = FakeHttp()
    a._http_client = fh
    evs = collect(a)
    update = {
        "update_type": "message_callback",
        "callback": {
            "timestamp": 1787482100,
            "callback_id": "cb-2",
            "payload": "/menu",
            "user": {"user_id": 55608376, "first_name": "Александр"},
        },
        "message": {
            "recipient": {"chat_id": 443805158, "chat_type": "dialog"},
            "body": {"mid": "mid.m6", "text": "Меню команд"},
        },
    }
    asyncio.run(a._on_update(update))
    assert evs == []  # never forwarded to the agent
    assert fh.calls[0][0].endswith("/answers")
    assert fh.calls[1][0].endswith("/messages")
    assert fh.calls[1][1]["json"]["attachments"][0]["type"] == "inline_keyboard"


def test_send_media_uses_uploads_flow(tmp_path):
    # regression: POST /photos is 404 on botapi.max.ru — the flow is
    # POST /uploads -> upload url -> multipart "data" -> photos DICT
    a = make_adapter()
    photo = tmp_path / "pic.png"
    photo.write_bytes(b"\x89PNG-fake")
    responses = [
        {"url": "https://upl.max.ru/u/abc"},                      # POST /uploads
        {"photos": {"ph-1": {"token": "photo-token"}}},           # multipart upload
        {"message": {"body": {"mid": "mid.ph"}}},                 # POST /messages
    ]

    class SeqHttp:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kw):
            self.calls.append((url, kw))
            r = FakeResp()
            r._json = responses[len(self.calls) - 1]
            r.json = lambda: r._json
            return r

    fh = SeqHttp()
    a._http_client = fh
    r = asyncio.run(a.send_media("443805158", str(photo), caption="смотри"))
    assert r.success
    # step 1: upload URL is requested with chat_id + type=image
    url, kw = fh.calls[0]
    assert url == "https://botapi.max.ru/uploads"
    assert kw["params"] == {"chat_id": "443805158", "type": "image"}
    assert kw["headers"]["Authorization"] == "tok"
    # step 2: the file goes to the returned URL as multipart field "data",
    # without the bot token (the upload host is not the API host)
    url, kw = fh.calls[1]
    assert url == "https://upl.max.ru/u/abc"
    assert "data" in kw["files"]
    assert not kw.get("headers")
    # step 3: the token from the photos dict lands in the attachment
    url, kw = fh.calls[2]
    assert url.endswith("/messages")
    assert kw["params"] == {"chat_id": "443805158"}
    assert kw["json"]["attachments"] == [
        {"type": "image", "payload": {"token": "photo-token"}}
    ]
    assert kw["json"]["text"] == "смотри"
