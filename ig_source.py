import random
import time
from datetime import datetime, timezone

import instaloader

from models import Post


class InstagramSource:
    """Синхронный (instaloader), поэтому вызывается из бота через asyncio.to_thread."""

    def __init__(self, username: str | None, session_file: str | None):
        self.L = instaloader.Instaloader(
            download_pictures=False, download_videos=False,
            download_video_thumbnails=False, download_geotags=False,
            download_comments=False, save_metadata=False,
            compress_json=False, quiet=True, max_connection_attempts=2,
        )
        self.logged_in = False
        if username:
            self.L.load_session_from_file(username, session_file or None)
            self.logged_in = True

    def fetch(self, handle: str, since: datetime) -> list[Post]:
        profile = instaloader.Profile.from_username(self.L.context, handle)
        title = profile.full_name or handle
        posts: list[Post] = []
        checked = 0
        for p in profile.get_posts():          # от новых к старым, но закреплённые идут первыми
            checked += 1
            d = p.date_utc.replace(tzinfo=timezone.utc)
            if d <= since:
                if getattr(p, "is_pinned", False) or checked <= 3:
                    continue                    # старый закреп — пропускаем, смотрим дальше
                break
            if p.typename == "GraphSidecar":
                media = "🖼"
            elif p.is_video:
                media = "🎬"
            else:
                media = "📷"
            posts.append(Post("ig", handle, title, d, p.caption or "",
                              f"https://www.instagram.com/p/{p.shortcode}/", media))
            if checked > 50:                    # страховка от выкачивания всей ленты
                break
        posts.sort(key=lambda x: x.date)
        return posts

    @staticmethod
    def polite_pause():
        time.sleep(random.uniform(4, 9))        # чтобы Instagram не забанил за частые запросы
