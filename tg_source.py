from datetime import datetime

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready, ChatInvitePeek

from models import Post


class TelegramSource:
    def __init__(self, api_id: int, api_hash: str, session: str):
        self.client = TelegramClient(session, api_id, api_hash)
        self._cache: dict[str, object] = {}

    async def start(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telethon-сессия не авторизована. Запусти: python login_telegram.py")

    async def stop(self):
        await self.client.disconnect()

    async def _resolve(self, handle: str):
        if handle in self._cache:
            return self._cache[handle]
        try:
            ent = await self.client.get_entity(handle)
        except (ValueError, RPCError):
            # Возможно, это хэш приватной инвайт-ссылки
            try:
                res = await self.client(CheckChatInviteRequest(handle))
            except RPCError as e:
                raise RuntimeError(f"канал не найден ({e.__class__.__name__})")
            if isinstance(res, (ChatInviteAlready, ChatInvitePeek)):
                ent = res.chat
            else:
                raise RuntimeError("приватный канал — подпишись на него с аккаунта парсера")
        self._cache[handle] = ent
        return ent

    async def fetch(self, handle: str, since: datetime) -> list[Post]:
        ent = await self._resolve(handle)
        title = getattr(ent, "title", None) or handle
        username = getattr(ent, "username", None)
        albums: dict[int, Post] = {}
        posts: list[Post] = []

        # reverse=True + offset_date → сообщения новее указанной даты, от старых к новым
        async for m in self.client.iter_messages(ent, offset_date=since, reverse=True):
            if m.date <= since or getattr(m, "action", None):
                continue
            text = m.message or ""
            if m.grouped_id and m.grouped_id in albums:      # альбом: склеиваем в один пост
                p = albums[m.grouped_id]
                if text and not p.text:
                    p.text = text
                continue

            if username:
                url = f"https://t.me/{username}/{m.id}"
            else:
                url = f"https://t.me/c/{ent.id}/{m.id}"

            media = ""
            if m.grouped_id:
                media = "🖼"
            elif m.photo:
                media = "📷"
            elif m.video or m.gif:
                media = "🎬"
            elif m.voice or m.audio:
                media = "🎧"
            elif m.poll:
                media = "📊"
            elif m.document:
                media = "📎"

            p = Post("tg", handle, title, m.date, text, url, media)
            if m.grouped_id:
                albums[m.grouped_id] = p
            posts.append(p)
        return posts
