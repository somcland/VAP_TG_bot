"""Чтение публичных Telegram-каналов через веб-превью t.me/s/<канал> — без аккаунта и API-ключей.
Работает только для публичных каналов, у которых не отключено превью."""
import asyncio
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup

from models import Post

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
MAX_PAGES = 10      # по ~20 постов на страницу — до ~200 постов назад


class TelegramSource:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        self.session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30))

    async def stop(self):
        if self.session:
            await self.session.close()

    async def _page(self, handle: str, before: int | None) -> str:
        url = f"https://t.me/s/{handle}" + (f"?before={before}" if before else "")
        async with self.session.get(url, allow_redirects=False) as r:
            if r.status in (301, 302):
                raise RuntimeError("нет веб-превью (канал приватный или превью отключено)")
            r.raise_for_status()
            return await r.text()

    @staticmethod
    def _parse(html: str, handle: str) -> tuple[str, list[Post], int | None]:
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.select_one(".tgme_channel_info_header_title")
        title = title_el.get_text(strip=True) if title_el else handle
        posts, ids = [], []

        for msg in soup.select(".tgme_widget_message[data-post]"):
            data_post = msg["data-post"]                     # "channel/123"
            try:
                mid = int(data_post.rsplit("/", 1)[1])
            except ValueError:
                continue
            ids.append(mid)
            if "service_message" in msg.get("class", []):
                continue
            t = msg.select_one(".tgme_widget_message_date time[datetime]")
            if not t:
                continue
            date = datetime.fromisoformat(t["datetime"])

            text_el = msg.select_one(".tgme_widget_message_text")
            if text_el:
                for br in text_el.find_all("br"):
                    br.replace_with("\n")
                text = text_el.get_text()
            else:
                text = ""

            if msg.select_one(".tgme_widget_message_grouped_wrap"):
                media = "🖼"
            elif msg.select_one(".tgme_widget_message_video_player, .tgme_widget_message_roundvideo_player"):
                media = "🎬"
            elif msg.select_one(".tgme_widget_message_photo_wrap"):
                media = "📷"
            elif msg.select_one(".tgme_widget_message_voice, .tgme_widget_message_audio"):
                media = "🎧"
            elif msg.select_one(".tgme_widget_message_poll"):
                media = "📊"
            elif msg.select_one(".tgme_widget_message_document"):
                media = "📎"
            else:
                media = ""

            posts.append(Post("tg", handle, title, date, text, f"https://t.me/{data_post}", media))

        if not ids and not soup.select_one(".tgme_channel_info"):
            raise RuntimeError("канал не найден или без веб-превью")
        return title, posts, (min(ids) if ids else None)

    async def fetch(self, handle: str, since: datetime) -> list[Post]:
        collected: dict[str, Post] = {}
        before = None
        for _ in range(MAX_PAGES):
            html = await self._page(handle, before)
            _, posts, oldest_id = self._parse(html, handle)
            reached_old = False
            for p in posts:
                if p.date > since:
                    collected[p.url] = p
                else:
                    reached_old = True
            if reached_old or not oldest_id or oldest_id <= 1:
                break
            before = oldest_id
            await asyncio.sleep(1)
        return sorted(collected.values(), key=lambda p: p.date)
