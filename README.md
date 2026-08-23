# Hermes MAX Platform Plugin

[MAX messenger](https://max.ru) (VK's national messenger) platform adapter for
[Hermes Agent](https://hermes-agent.nousresearch.com) — run your AI agent in
MAX alongside Telegram, WhatsApp, Discord and the rest of the Hermes gateway
family.

```
You (MAX app) ──▶ MAX Bot API ──▶ this plugin ──▶ Hermes Agent ──▶ your tools, skills, memory
```

## Features

- **Long-polling transport** (`GET /updates`) — no public IP, domain, or TLS
  certificate needed. Works behind NAT out of the box.
- **Dialogs and group chats** — group traffic is keyed per chat, DMs per user.
- **Outbound text messages** with **reply links** (`link.type=reply`).
- **Photo upload** (images → `POST /messages` attachments).
- **Typing indicator** while the agent is thinking.
- **Deduplication** of updates (safe redelivery), automatic marker cursor
  management, reconnect with backoff.
- **Runs next to other platforms** — MAX and Telegram can be enabled at the
  same time on one gateway.

## Requirements

- Hermes Agent ≥ 1.16 with the plugin loader (`plugins/platforms/`)
- A MAX bot. Create one via [@MasterBot](https://max.ru/masterbot) (verified
  business account required) and copy the bot token.

## Installation

1. Copy the plugin into your Hermes installation:

   ```bash
   git clone https://github.com/fruxxxl/hermes-max-plugin.git
   cp -r hermes-max-plugin/plugins/platforms/max \
       /usr/local/lib/hermes-agent/plugins/platforms/
   ```

2. Put the bot token in `~/.hermes/.env` (do **not** commit it):

   ```bash
   MAX_BOT_TOKEN=<your token from @MasterBot>
   # Hermes authorizes senders via allowlists (default: deny)
   MAX_ALLOWED_USERS=<your MAX user_id, comma-separated>
   ```

3. Enable the platform in `~/.hermes/config.yaml`:

   ```yaml
   max:
     enabled: true
     allowed_chats: ""      # empty = DMs allowed
     require_mention: true  # groups: only react when mentioned
   ```

4. Restart the gateway:

   ```bash
   hermes gateway restart
   ```

   You should see in the logs:

   ```
   [Max] Connected as @your_bot (…)
   ✓ max connected
   ```

Write to your bot in MAX — it answers with the full agent behind it.

## Configuration reference

| Env / config | Meaning |
|---|---|
| `MAX_BOT_TOKEN` | Bot token from @MasterBot (**required**) |
| `MAX_ALLOWED_USERS` | Comma-separated allowlist of MAX user IDs (**required** — default deny) |
| `MAX_ALLOW_ALL_USERS` | `true` to allow anyone (dev only) |
| `max.allowed_chats` | Restrict to specific chat IDs; empty = DMs |
| `max.require_mention` | In groups, only respond to @mentions |

## MAX Bot API pitfalls (learned the hard way)

This plugin exists because the MAX Bot API has a few non-obvious behaviors.
All of them are handled here, documented so the next person doesn't lose an
evening:

1. **`chat_id` goes in the query string, not the JSON body.** `POST /messages`
   takes `chat_id`/`user_id` as *query parameters*; the body carries only
   `text`/`attachments`. Sending `chat_id` in the body returns
   `400 proto.payload: Unknown recipient` — the server simply ignores the body
   field. (Confirmed against the official TS SDK: `query: { chat_id, user_id }`.)
2. **Dialog updates have no `chat` object.** In `message_created` updates the
   chat id lives at `message.recipient.chat_id`; a top-level `message.chat`
   only appears for group chats. Sender info is `message.sender` (not `from`).
3. **Authorization is default-deny in Hermes.** Even with a working poll loop,
   messages are silently dropped until `MAX_ALLOWED_USERS` lists the sender —
   there is no error in the logs, just `Queued inbound message … chat=`.
4. **Any `GET /updates` advances the cursor.** Polling the same token from a
   second consumer (a debugging `curl`!) consumes updates from the gateway.
   One token = one poller.
5. **`Authorization` header (not `access_token` query param).** The documented
   `?access_token=` style works for `/me` but the header form is the reliable
   one across endpoints.

## Local development & tests

```bash
cd hermes-max-plugin
python -m pytest tests/ -v     # offline unit tests, no token needed
```

The test suite covers update parsing (dialog/group), own-message echo
skipping, deduplication, and the exact wire shape of `send()` (query params,
reply links, truncation).

## Repository layout

```
plugins/platforms/max/
├── __init__.py    # plugin entrypoint (export: register)
├── adapter.py     # BasePlatformAdapter implementation
└── plugin.yaml    # plugin metadata
tests/test_adapter.py
examples/config.yaml.example
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

Built while wiring a private Hermes deployment to MAX; transport details
cross-checked against the official
[max-bot-api-client-ts](https://github.com/max-messenger/max-bot-api-client-ts).
Not affiliated with VK / MAX.
