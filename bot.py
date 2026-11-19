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
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, LinkPreviewOptions, Message
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
PREVIEW_LEN = int(os.getenv("PREVIEW_LEN", "300"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

tg = TelegramSource(int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
                    os.getenv("TG_SESSION", "parser"))
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


# ---------- форматирование ----------

def fmt_post(p: Post) -> str:
    text = re.sub(r"\s+", " ", p.text).strip()
    if len(text) > PREVIEW_LEN:
        text = text[:PREVIEW_LEN].rsplit(" ", 1)[0] + "…"
    when = p.date.astimezone(TZ).strftime("%d.%m %H:%M")
    body = html.escape(text) if text else "<i>без текста</i>"
    return f"• <code>{when}</code> {p.media} {body}\n  <a href=\"{p.url}\">открыть →</a>"


def build_messages(results, errors) -> list[str]:
    blocks = []
    order = {("tg", h): i for i, h in enumerate(sources.TELEGRAM)}
    order.update({("ig", h): 100 + i for i, h in enumerate(sources.INSTAGRAM)})
    found = sorted((r for r in results if r[2]), key=lambda r: order.get((r[0], r[1]), 999))
    count = sum(len(r[2]) for r in found)

    for platform, handle, posts in found:
        icon = "✈️" if platform == "tg" else "📸"
        name = html.escape(posts[0].source)
        head = f"{icon} <b>{name}</b> · @{html.escape(handle)} — {len(posts)}"
        blocks.append(head + "\n" + "\n".join(fmt_post(p) for p in posts))

    empty = [f"@{h}" for pl, h, ps in results if not ps]
    footer = f"Итого новых постов: <b>{count}</b>"
    if empty:
        footer += f"\nБез новых: {html.escape(', '.join(empty))}"
    if errors:
        footer += "\n\n⚠️ Ошибки:\n" + "\n".join(html.escape(e) for e in errors)
    blocks.append(footer)

    # склеиваем блоки в сообщения ≤ 4000 символов
    msgs, cur = [], ""
    for b in blocks:
        while len(b) > 4000:                       # очень длинный блок режем по строкам
            cut = b.rfind("\n", 0, 4000)
            msgs.append(b[:cut]); b = b[cut + 1:]
        if len(cur) + len(b) + 2 > 4000:
            msgs.append(cur); cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs


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
        for text in build_messages(results, errors):
            await msg.answer(text, link_preview_options=LinkPreviewOptions(is_disabled=True))


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
