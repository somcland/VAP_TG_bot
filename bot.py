import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram import F
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LinkPreviewOptions, Message)
from dotenv import load_dotenv

import sources
from ig_source import InstagramSource
from models import Post
from tg_source import TelegramSource

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("parser")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Astrakhan"))
DEFAULT_HOURS = int(os.getenv("DEFAULT_HOURS", "24"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

tg = TelegramSource()
ig = InstagramSource(os.getenv("IG_USERNAME") or None, os.getenv("IG_SESSION_FILE") or None)

dp = Dispatcher()
run_lock = asyncio.Lock()


# ---------- состояние: время последней успешной проверки по каждому источнику ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    return {}


def save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_FILE)


def key(platform: str, handle: str) -> str:
    return f"{platform}:{handle}"


# ---------- разбор времени для /since ----------

def parse_since(arg: str) -> datetime | None:
    arg = arg.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(m|min|м|мин|h|ч|d|д)", arg)
    if m:
        n, u = int(m[1]), m[2]
        delta = timedelta(minutes=n) if u in ("m", "min", "м", "мин") else \
            timedelta(hours=n) if u in ("h", "ч") else timedelta(days=n)
        return datetime.now(timezone.utc) - delta
    now = datetime.now(TZ)
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%d.%m %H:%M", "%d.%m", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(arg, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            d = d.replace(year=now.year)
        return d.replace(tzinfo=TZ).astimezone(timezone.utc)
    return None


# ---------- сбор ----------

async def collect(chat_id: int, forced_since: datetime | None, progress: Message):
    state = load_state()
    chat_state = state.setdefault(str(chat_id), {})
    started = datetime.now(timezone.utc)
    default = started - timedelta(hours=DEFAULT_HOURS)
    total = len(sources.TELEGRAM) + len(sources.INSTAGRAM)
    done = 0
    results: list[tuple[str, str, list[Post]]] = []
    errors: list[str] = []

    def since_for(k: str) -> datetime:
        if forced_since:
            return forced_since
        v = chat_state.get(k)
        return datetime.fromisoformat(v) if v else default

    async def tick():
        nonlocal done
        done += 1
        if done % 3 == 0 or done == total:
            try:
                await progress.edit_text(f"⏳ Собираю… {done}/{total}")
            except Exception:
                pass

    async def run_tg():
        for h in sources.TELEGRAM:
            k = key("tg", h)
            try:
                posts = await tg.fetch(h, since_for(k))
                results.append(("tg", h, posts))
                chat_state[k] = started.isoformat()
            except Exception as e:
                log.exception("tg %s", h)
                errors.append(f"TG @{h}: {e}")
            await tick()

    async def run_ig():
        for i, h in enumerate(sources.INSTAGRAM):
            k = key("ig", h)
            if i:
                await asyncio.to_thread(ig.polite_pause)
            try:
                posts = await asyncio.to_thread(ig.fetch, h, since_for(k))
                results.append(("ig", h, posts))
                chat_state[k] = started.isoformat()
            except Exception as e:
                log.exception("ig %s", h)
                errors.append(f"IG @{h}: {str(e)[:150]}")
            await tick()

    await asyncio.gather(run_tg(), run_ig())
    save_state(state)
    return results, errors


# ---------- форматирование и отправка ----------

TEXT_MAX = 4096        # лимит обычного сообщения


def tidy(s: str) -> str:
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def post_parts(p: Post) -> tuple[str, str, str]:
    """(шапка, текст, подвал) в HTML."""
    when = p.date.astimezone(TZ).strftime("%d.%m · %H:%M")
    where = "Telegram" if p.platform == "tg" else "Instagram"
    head = f"<b>{html.escape(p.source)}</b>\n<i>{when} · {where}</i>"
    text = html.escape(tidy(p.text or ""))
    foot = f"<a href=\"{p.url}\">Открыть пост →</a>"
    return head, text, foot


def visible_len(h: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", h)))


def compose(p: Post, limit: int, with_link: bool = True) -> str:
    head, text, foot = post_parts(p)
    if not with_link:
        foot = ""
    full = "\n\n".join(x for x in (head, text, foot) if x)
    if visible_len(full) <= limit:
        return full
    # не влезает — режем текст по границе слова (бывает только у очень длинных постов)
    room = limit - visible_len(f"{head}\n\n…\n\n{foot}")
    plain = tidy(p.text)[:max(room, 0)].rsplit(" ", 1)[0]
    return "\n\n".join(x for x in (head, html.escape(plain) + "…", foot) if x)


def summary(results, errors) -> str:
    count = sum(len(r[2]) for r in results)
    out = f"Итого новых постов: <b>{count}</b>"
    empty = [f"@{h}" for pl, h, ps in results if not ps]
    if empty:
        out += f"\nБез новых: {html.escape(', '.join(empty))}"
    if errors:
        out += "\n\n⚠️ Ошибки:\n" + "\n".join(html.escape(e) for e in errors)
    return out


def ordered_posts(results) -> list[Post]:
    order = {("tg", h): i for i, h in enumerate(sources.TELEGRAM)}
    order.update({("ig", h): 100 + i for i, h in enumerate(sources.INSTAGRAM)})
    found = sorted((r for r in results if r[2]), key=lambda r: order.get((r[0], r[1]), 999))
    return [p for _, _, posts in found for p in posts]


async def safe(call):
    """Вызов API с ожиданием при флуд-лимите Telegram."""
    for _ in range(3):
        try:
            return await call()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)


# ---------- карусель: одно сообщение, листаем кнопками ----------

DIGESTS: dict[int, list[Post]] = {}     # id подборки → посты (живут до перезапуска бота)
MAX_DIGESTS = 30
_next_id = 0


def store_digest(posts: list[Post]) -> int:
    global _next_id
    _next_id += 1
    DIGESTS[_next_id] = posts
    while len(DIGESTS) > MAX_DIGESTS:
        DIGESTS.pop(next(iter(DIGESTS)))
    return _next_id


def slide(d: int, i: int, j: int) -> tuple[str, LinkPreviewOptions, InlineKeyboardMarkup]:
    posts = DIGESTS[d]
    p = posts[i]
    text = compose(p, TEXT_MAX, with_link=False)
    img = p.images[j] if p.images else None
    preview = (LinkPreviewOptions(url=img, prefer_large_media=True, show_above_text=True)
               if img else LinkPreviewOptions(is_disabled=True))

    nav = lambda ii, jj: f"nav:{d}:{ii}:{jj}"
    n = len(posts)
    rows = [[
        InlineKeyboardButton(text="◀", callback_data=nav((i - 1) % n, 0)),
        InlineKeyboardButton(text=f"{i + 1} / {n}", callback_data="noop"),
        InlineKeyboardButton(text="▶", callback_data=nav((i + 1) % n, 0)),
    ]]
    k = len(p.images)
    if k > 1:
        rows.append([
            InlineKeyboardButton(text="‹ фото", callback_data=nav(i, (j - 1) % k)),
            InlineKeyboardButton(text=f"🖼 {j + 1} / {k}", callback_data="noop"),
            InlineKeyboardButton(text="фото ›", callback_data=nav(i, (j + 1) % k)),
        ])
    rows.append([InlineKeyboardButton(text="Открыть пост →", url=p.url)])
    return text, preview, InlineKeyboardMarkup(inline_keyboard=rows)


async def send_carousel(msg: Message, posts: list[Post]):
    d = store_digest(posts)
    text, preview, kb = slide(d, 0, 0)
    await safe(lambda: msg.answer(text, link_preview_options=preview, reply_markup=kb))


@dp.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(cq: CallbackQuery):
    _, d, i, j = cq.data.split(":")
    if int(d) not in DIGESTS:
        return await cq.answer("Подборка устарела — запроси /new заново", show_alert=True)
    text, preview, kb = slide(int(d), int(i), int(j))
    try:
        await cq.message.edit_text(text, link_preview_options=preview, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("edit: %s", e)
    await cq.answer()


# ---------- хендлеры ----------

def allowed(msg: Message) -> bool:
    return not ALLOWED or (msg.from_user and msg.from_user.id in ALLOWED)


HELP = (
    "Собираю новые посты из Telegram-каналов и Instagram.\n\n"
    "/new — всё, что вышло с прошлой проверки\n"
    "/since 6h · 2d · 18.11 · 18.11 15:00 — с указанного времени\n"
    "/sources — список источников\n"
    "/status — когда проверял каждый источник\n\n"
    f"При первом запуске беру последние {DEFAULT_HOURS} ч."
)


@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    if not allowed(msg):
        return await msg.answer(f"Нет доступа. Твой id: <code>{msg.from_user.id}</code>")
    await msg.answer(HELP)


@dp.message(Command("sources"))
async def cmd_sources(msg: Message):
    if not allowed(msg):
        return
    tg_list = "\n".join(f"• t.me/{h}" for h in sources.TELEGRAM)
    ig_list = "\n".join(f"• instagram.com/{h}" for h in sources.INSTAGRAM)
    await msg.answer(f"<b>Telegram</b>\n{tg_list}\n\n<b>Instagram</b>\n{ig_list}",
                     link_preview_options=LinkPreviewOptions(is_disabled=True))


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not allowed(msg):
        return
    st = load_state().get(str(msg.chat.id), {})
    lines = []
    for pl, lst in (("tg", sources.TELEGRAM), ("ig", sources.INSTAGRAM)):
        for h in lst:
            v = st.get(key(pl, h))
            t = datetime.fromisoformat(v).astimezone(TZ).strftime("%d.%m %H:%M") if v else "—"
            lines.append(f"<code>{t}</code> {pl.upper()} @{html.escape(h)}")
    await msg.answer("\n".join(lines))


async def run(msg: Message, forced: datetime | None):
    if run_lock.locked():
        return await msg.answer("Уже собираю, подожди пару минут 🙂")
    async with run_lock:
        progress = await msg.answer("⏳ Собираю… Instagram медленный, это займёт 1–3 минуты.")
        results, errors = await collect(msg.chat.id, forced, progress)
        try:
            await progress.delete()
        except Exception:
            pass
        await msg.answer(summary(results, errors))
        posts = ordered_posts(results)
        if posts:
            await send_carousel(msg, posts)


@dp.message(Command("new"))
async def cmd_new(msg: Message):
    if allowed(msg):
        await run(msg, None)


@dp.message(Command("since"))
async def cmd_since(msg: Message, command: CommandObject):
    if not allowed(msg):
        return
    since = parse_since(command.args or "")
    if not since:
        return await msg.answer("Не понял время. Примеры: /since 6h, /since 2d, /since 18.11, /since 18.11 15:00")
    await run(msg, since)


async def main():
    await tg.start()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="new", description="Новое с прошлой проверки"),
        BotCommand(command="since", description="С указанного времени: 6h, 2d, 18.11"),
        BotCommand(command="sources", description="Источники"),
        BotCommand(command="status", description="Когда проверял"),
    ])
    try:
        await dp.start_polling(bot)
    finally:
        await tg.stop()


if __name__ == "__main__":
    asyncio.run(main())
