# Парсер постов: Telegram + Instagram

Бот по команде `/new` присылает всё, что вышло в источниках с прошлой проверки.
`/since 6h`, `/since 2d`, `/since 18.11 15:00` присылает посты с указанного времени.
Время последней проверки хранится отдельно для каждого источника, поэтому если один упал с ошибкой, в следующий раз он догонит своё.

## 1. Установка

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить
```

## 2. Telegram

Нужен только токен бота от @BotFather (`BOT_TOKEN`).
Каналы читаются через их публичную веб-версию `t.me/s/<канал>` — без аккаунта и API-ключей.
Ограничение: приватные каналы и каналы с отключённым превью так не читаются — бот покажет по ним ошибку.

## 3. Instagram

Без логина Instagram почти сразу отдаёт 401/429, поэтому нужна сессия. Лучше завести отдельный аккаунт.

```bash
instaloader --login ИМЯ_АККАУНТА     # на своём компе (Mac), не на сервере
```

Instaloader покажет, куда сохранил сессию (обычно `~/.config/instaloader/session-ИМЯ`).
Файл скопировать на сервер и указать в `.env`: `IG_USERNAME` и `IG_SESSION_FILE`.
Если вход просит подтверждение (checkpoint), сначала зайди в этот аккаунт в браузере, подтверди вход и повтори.

## 4. Запуск

```bash
python bot.py
```

### Как сервис на VPS (systemd)

`/etc/systemd/system/astr-parser.service`:

```ini
[Unit]
Description=Astr posts parser bot
After=network-online.target

[Service]
WorkingDirectory=/opt/astr-parser
ExecStart=/opt/astr-parser/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now astr-parser
journalctl -u astr-parser -f
```

## Источники

Список хранится в `sources.py`, это просто usernames.
