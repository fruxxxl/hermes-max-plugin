# Hermes MAX Platform Plugin

Платформенный адаптер мессенджера [MAX](https://max.ru) для
[Hermes Agent](https://hermes-agent.nousresearch.com) — запускайте своего
AI-агента в MAX параллельно с Telegram, WhatsApp, Discord и остальными
платформами Hermes-шлюза.

```
Вы (приложение MAX) ──▶ MAX Bot API ──▶ этот плагин ──▶ Hermes Agent ──▶ инструменты, скиллы, память
```

## Возможности

- **Long-polling транспорт** (`GET /updates`) — не нужны публичный IP, домен
  и TLS-сертификат. Работает за NAT из коробки.
- **Диалоги и групповые чаты** — групповой трафик ключуется по чату, личные
  сообщения — по пользователю.
- **Исходящие текстовые сообщения** с **reply-ссылками** (`link.type=reply`).
- **Загрузка фото** (картинки → `POST /messages` attachments).
- **Индикатор набора текста**, пока агент думает.
- **Дедупликация** апдейтов (безопасные повторные доставки), автоматическое
  управление курсором-маркером, реконнект с backoff.
- **Работает вместе с другими платформами** — MAX и Telegram можно включить
  одновременно на одном шлюзе.

## Требования

- Hermes Agent ≥ 1.16 с загрузчиком плагинов (`plugins/platforms/`)
- Бот в MAX. Создаётся через [@MasterBot](https://max.ru/masterbot)
  (нужна верифицированная бизнес-страница), токен бота копируете оттуда.

## Установка

1. Скопируйте плагин в установленную копию Hermes:

   ```bash
   git clone https://github.com/fruxxxl/hermes-max-plugin.git
   cp -r hermes-max-plugin/plugins/platforms/max \
       /usr/local/lib/hermes-agent/plugins/platforms/
   ```

2. Положите токен бота в `~/.hermes/.env` (не коммитьте его!):

   ```bash
   MAX_BOT_TOKEN=<токен из @MasterBot>
   # Hermes авторизует отправителей по allowlist (по умолчанию: deny)
   MAX_ALLOWED_USERS=<ваш MAX user_id, через запятую>
   ```

3. Включите платформу в `~/.hermes/config.yaml`:

   ```yaml
   max:
     enabled: true
     allowed_chats: ""      # пусто = разрешены личные сообщения
     require_mention: true  # в группах отвечать только на @упоминания
   ```

4. Перезапустите шлюз:

   ```bash
   hermes gateway restart
   ```

   В логах должно появиться:

   ```
   [Max] Connected as @your_bot (…)
   ✓ max connected
   ```

Напишите боту в MAX — он ответит со всем арсеналом агента за спиной.

## Справочник по конфигурации

| Переменная / ключ | Значение |
|---|---|
| `MAX_BOT_TOKEN` | Токен бота из @MasterBot (**обязательно**) |
| `MAX_ALLOWED_USERS` | Allowlist MAX user_id через запятую (**обязательно** — по умолчанию deny) |
| `MAX_ALLOW_ALL_USERS` | `true` — пускать всех (только для разработки) |
| `max.allowed_chats` | Ограничить конкретными chat_id; пусто = личные сообщения |
| `max.require_mention` | В группах реагировать только на @упоминания |

## Грабли MAX Bot API (выучено на собственной шкуре)

Плагин существует потому, что у MAX Bot API есть несколько неочевидных
поведений. Все они здесь учтены и задокументированы, чтобы следующий человек
не потерял вечер:

1. **`chat_id` передаётся в query string, а не в JSON-теле.** `POST /messages`
   принимает `chat_id`/`user_id` как *query-параметры*; в теле — только
   `text`/`attachments`. Отправка `chat_id` в теле возвращает
   `400 proto.payload: Unknown recipient` — сервер просто игнорирует поле.
   (Проверено по официальному TS SDK: `query: { chat_id, user_id }`.)
2. **В апдейтах диалогов нет объекта `chat`.** В апдейтах `message_created`
   id чата лежит в `message.recipient.chat_id`; объект `message.chat`
   появляется только для групп. Данные отправителя — `message.sender`
   (не `from`).
3. **Авторизация в Hermes — deny по умолчанию.** Даже с рабочим poll-циклом
   сообщения молча дропаются, пока `MAX_ALLOWED_USERS` не содержит
   отправителя — в логах нет ошибки, только `Queued inbound message … chat=`.
4. **Любой `GET /updates` двигает курсор.** Поллинг того же токена вторым
   потребителем (отладочным `curl`!) забирает апдейты у шлюза.
   Один токен = один poller.
5. **Заголовок `Authorization` (не `access_token` в query).** Задокументированный
   стиль `?access_token=` работает для `/me`, но форма заголовка надёжна для
   всех эндпоинтов.

## Разработка и тесты

```bash
cd hermes-max-plugin
python -m pytest tests/ -v     # офлайн юнит-тесты, токен не нужен
```

Тесты покрывают разбор апдейтов (диалог/группа), пропуск эха собственных
сообщений бота, дедупликацию и точную wire-форму `send()` (query-параметры,
reply-ссылки, обрезка длинного текста).

## Структура репозитория

```
plugins/platforms/max/
├── __init__.py    # точка входа плагина (export: register)
├── adapter.py     # реализация BasePlatformAdapter
└── plugin.yaml    # метаданные плагина
tests/test_adapter.py
examples/config.yaml.example
examples/dot-env.example
```

## Лицензия

MIT — см. [LICENSE](LICENSE).

## Автор

[Alexander Shulgin](https://github.com/fruxxxl) — плагин написан при подключении
частного Hermes-деплоя к MAX; детали транспорта сверены с официальным
[max-bot-api-client-ts](https://github.com/max-messenger/max-bot-api-client-ts).
Не аффилировано с VK / MAX.
