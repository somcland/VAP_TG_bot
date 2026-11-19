"""Однократный вход в Telegram-аккаунт, от имени которого читаются каналы.
Запусти один раз: python login_telegram.py — введёшь телефон и код из Telegram."""
import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient

load_dotenv()
with TelegramClient(os.getenv("TG_SESSION", "parser"),
                    int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]) as client:
    me = client.get_me()
    print(f"Готово, вошёл как {me.first_name} (@{me.username}). Файл сессии создан.")
