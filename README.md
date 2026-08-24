# Hermes MAX Platform Plugin

Платформенный адаптер мессенджера [MAX](https://max.ru) для
[Hermes Agent](https://hermes-agent.nousresearch.com). Позволяет запускать
AI-агента в MAX параллельно с Telegram, WhatsApp, Discord и другими
платформами Hermes-шлюза.

```
MAX (клиент) ──▶ MAX Bot API ──▶ плагин ──▶ Hermes Agent ──▶ инструменты, скиллы, память
```

## Возможности

- **Long-polling транспорт** (`GET /updates`) — не требуются публичный IP,
  домен и TLS-сертификат; работает за NAT.
- **Диалоги и групповые чаты** — групповой трафик ключуется по чату,
  личные сообщения — по пользователю.
- **Исходящие текстовые сообщения** с reply-ссылками (`link.type=reply`).
- **Загрузка и отправка фото** (attachments).
- **Командное меню** — команда `/menu` отправляет inline-клавиатуру со
  списком команд агента (общий реестр команд Hermes). Нажатие кнопки
  выполняет соответствующую команду от имени пользователя. MAX Bot API
  не имеет аналога `setMyCommands`, поэтому меню реализовано через
  inline-клавиатуры и callback-и (`message_callback`).
- **Индикатор набора текста** на всё время обработки запроса
  (`typing_on` с периодическим обновлением).
- **Дедупликация апдейтов**, управление курсором-маркером, реконнект
  с экспоненциальным backoff.
- **Совместная работа с другими платформами** — MAX и Telegram можно
  включить одновременно на одном шлюзе.

## Требования

- Hermes Agent ≥ 1.16 с загрузчиком плагинов (`plugins/platforms/`).
- Бот в MAX, созданный через [@MasterBot](https://max.ru/masterbot)
  (требуется верифицированная бизнес-страница). Токен бота выдаётся там же.

## Установка

1. Скопируйте плагин в установленную копию Hermes:

   ```bash
   git clone https://github.com/fruxxxl/hermes-max-plugin.git
   cp -r hermes-max-plugin/plugins/platforms/max \
       /usr/local/lib/hermes-agent/plugins/platforms/
   ```

2. Добавьте токен бота в `~/.hermes/.env` (файл не должен попадать
   в систему контроля версий):

   ```bash
   MAX_BOT_TOKEN=<токен из @MasterBot>
   # Hermes авторизует отправителей по allowlist (по умолчанию доступ закрыт)
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

   Признак успешного запуска в логах:

   ```
   [Max] Connected as @your_bot (…)
   ✓ max connected
   ```

После этого сообщения, отправленные боту в MAX, обрабатываются агентом
в полном объёме (инструменты, скиллы, память). Команда `/menu` открывает
меню команд.

## Справочник по конфигурации

| Переменная / ключ | Значение |
|---|---|
| `MAX_BOT_TOKEN` | Токен бота из @MasterBot (**обязательно**) |
| `MAX_ALLOWED_USERS` | Allowlist MAX user_id через запятую (**обязательно** — по умолчанию доступ закрыт) |
| `MAX_ALLOW_ALL_USERS` | `true` — допускать всех отправителей (только для разработки) |
| `max.allowed_chats` | Ограничить конкретными chat_id; пусто = личные сообщения |
| `max.require_mention` | В группах реагировать только на @упоминания |

## Особенности MAX Bot API

Реализация учитывает ряд неочевидных особенностей API, подтверждённых
официальным SDK ([max-bot-api-client-ts](https://github.com/max-messenger/max-bot-api-client-ts)):

1. **`chat_id` передаётся в query string, а не в JSON-теле.** `POST /messages`
   принимает `chat_id`/`user_id` как query-параметры; тело содержит только
   `text`/`attachments`. Передача `chat_id` в теле возвращает
   `400 proto.payload: Unknown recipient`.
2. **В апдейтах диалогов нет объекта `chat`.** В `message_created`
   идентификатор чата находится в `message.recipient.chat_id`; объект
   `message.chat` присутствует только для групп. Данные отправителя —
   `message.sender`.
3. **Авторизация в Hermes по умолчанию запрещает всех.** Пока
   `MAX_ALLOWED_USERS` не содержит отправителя, сообщения отбрасываются
   без записи в error-лог.
4. **Любой `GET /updates` сдвигает курсор.** Параллельный опрос того же
   токена другим потребителем (например, отладочным `curl`) забирает
   апдейты у шлюза. Один токен — один poller.
5. **Авторизация заголовком `Authorization`.** Форма `?access_token=`
   работает для `/me`, но заголовок надёжен для всех эндпоинтов.
6. **Индикатор `typing_on` истекает через ~3 секунды.** Для непрерывной
   индикации действие отправляется повторно каждые 2 секунды, пока
   обрабатывается запрос, и сбрасывается при отправке ответа.

## Разработка и тесты

```bash
cd hermes-max-plugin
python -m pytest tests/ -v     # офлайн-юнит-тесты, токен не нужен
```

Тесты покрывают разбор апдейтов (диалог/группа), пропуск эха собственных
сообщений бота, дедупликацию, wire-форму `send()` (query-параметры,
reply-ссылки, обрезка длинного текста), команду `/menu` (клавиатура и
колбэк) и запросы индикатора набора текста.

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

[Alexander Shulgin](https://github.com/fruxxxl). Плагин разработан для
частного Hermes-деплоя; поведение транспорта сверено с официальным
[max-bot-api-client-ts](https://github.com/max-messenger/max-bot-api-client-ts).
Проект не аффилирован с VK / MAX.
