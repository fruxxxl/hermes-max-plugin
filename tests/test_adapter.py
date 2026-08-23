"""Offline unit tests for the MAX platform adapter (no token, no network)."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "usr_local_stub")
) if False else None

# Make the hermes gateway + plugin importable: tests run either from the repo
# (plugin copied under tests/../plugins) with the real gateway package
# available, or inside a Hermes installation.
HERMES_ROOT = os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent")
for p in (HERMES_ROOT, str(Path(__file__).resolve().parents[1])):
    if p not in sys.path:
        sys.path.insert(0, p)

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.max.adapter import MaxAdapter  # noqa: E402

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
