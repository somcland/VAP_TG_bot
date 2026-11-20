import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LinkPreviewOptions, Message)
from dotenv import load_dotenv

import sources as seed
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
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "settings.json"))
SOURCES_FILE = Path(os.getenv("SOURCES_FILE", "sources.json"))

DEFAULT_SCHEDULE = [10, 19]
PRESETS = {1: [10], 2: [10, 19], 3: [9, 14, 20]}
HOUR_GRID = list(range(8, 24))

tg = TelegramSource()
dp = Dispatcher()
run_lock = asyncio.Lock()
awaiting_source: set[int] = set()     # чаты, от которых ждём ссылку на канал


# ---------- файлы ----------

def read_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return default


def write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def load_state() -> dict:
    return read_json(STATE_FILE, {})


def save_state(state: dict):
    write_json(STATE_FILE, state)


def load_sources() -> dict[str, str]:
    """handle → название канала. При первом запуске берём список из sources.py."""
    data = read_json(SOURCES_FILE, None)
    if data is None:
        data = {h: h for h in seed.TELEGRAM}
        write_json(SOURCES_FILE, data)
    return data


def save_sources(data: dict[str, str]):
    write_json(SOURCES_FILE, data)


def load_settings() -> dict:
    return read_json(SETTINGS_FILE, {})


def save_settings(data: dict):
    write_json(SETTINGS_FILE, data)


def chat_settings(chat_id: int) -> dict:
    return load_settings().get(str(chat_id), {"hours": DEFAULT_SCHEDULE})


def update_chat_settings(chat_id: int, **kw):
    data = load_settings()
    cur = data.setdefault(str(chat_id), {"hours": DEFAULT_SCHEDULE})
    cur.update(kw)
    save_settings(data)


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

async def collect(chat_id: int, forced_since: datetime | None, progress: Message | None = None) -> list[Post]:
    state = load_state()
    chat_state = state.setdefault(str(chat_id), {})
    srcs = load_sources()
    started = datetime.now(timezone.utc)
    default = started - timedelta(hours=DEFAULT_HOURS)
    posts: list[Post] = []
    titles_changed = False

    for n, h in enumerate(srcs, 1):
        k = key("tg", h)
        since = forced_since or (datetime.fromisoformat(chat_state[k]) if k in chat_state else default)
        try:
            found = await tg.fetch(h, since)
            posts.extend(found)
            chat_state[k] = started.isoformat()
            if found and found[0].source != srcs[h]:
                srcs[h] = found[0].source
                titles_changed = True
        except Exception:
            log.exception("tg %s", h)           # ошибки — только в лог, в чат не шлём
        if progress and (n % 3 == 0 or n == len(srcs)):
            try:
                await progress.edit_text(f"⏳ Собираю… {n}/{len(srcs)}")
            except Exception:
                pass

    save_state(state)
    if titles_changed:
        save_sources(srcs)
    return posts


# ---------- форматирование ----------

TEXT_MAX = 4096


def tidy(s: str) -> str:
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def visible_len(h: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", h)))


def compose(p: Post, limit: int = TEXT_MAX) -> str:
    when = p.date.astimezone(TZ).strftime("%d.%m · %H:%M")
    head = f"<b>{html.escape(p.source)}</b>\n<i>{when}</i>"
    text = html.escape(tidy(p.text or ""))
    full = f"{head}\n\n{text}" if text else head
    if visible_len(full) <= limit:
        return full
    room = limit - visible_len(head) - 4
    plain = tidy(p.text)[:max(room, 0)].rsplit(" ", 1)[0]
    return f"{head}\n\n{html.escape(plain)}…"


def order_posts(posts: list[Post]) -> list[Post]:
    order = {h: i for i, h in enumerate(load_sources())}
    return sorted(posts, key=lambda p: (order.get(p.handle, 999), p.date))


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


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
    return compose(p), preview, InlineKeyboardMarkup(inline_keyboard=rows)


async def send_digest(bot: Bot, chat_id: int, posts: list[Post], title: str):
    posts = order_posts(posts)
    n = len(posts)
    await safe(lambda: bot.send_message(chat_id, f"{title} · {n} {plural(n, 'пост', 'поста', 'постов')}"))
    d = store_digest(posts)
    text, preview, kb = slide(d, 0, 0)
    await safe(lambda: bot.send_message(chat_id, text, link_preview_options=preview, reply_markup=kb))


@dp.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(cq: CallbackQuery):
    _, d, i, j = cq.data.split(":")
    if int(d) not in DIGESTS:
        return await cq.answer("Подборка устарела — запроси /new заново", show_alert=True)
    text, preview, kb = slide(int(d), int(i), int(j))
    await edit(cq.message, text, kb, preview)
    await cq.answer()


async def edit(msg: Message, text: str, kb: InlineKeyboardMarkup, preview: LinkPreviewOptions | None = None):
    try:
        await msg.edit_text(text, reply_markup=kb,
                            link_preview_options=preview or LinkPreviewOptions(is_disabled=True))
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("edit: %s", e)


# ---------- настройки ----------

WELCOME = (
    "Привет! Я собираю свежие посты из астраханских каналов и присылаю их одной подборкой, "
    "листай кнопками ◀ ▶.\n\nСначала настроим, когда присылать."
)

HELP = (
    "/new — всё, что вышло с прошлой проверки\n"
    "/since 6h · 2d · 18.11 — с указанного времени\n"
    "/settings — время рассылки и источники"
)


def fmt_hours(hours: list[int]) -> str:
    hs = [f"{h:02d}:00" for h in sorted(hours)]
    return " и ".join([", ".join(hs[:-1]), hs[-1]]) if len(hs) > 1 else (hs[0] if hs else "")


def times_word(n: int) -> str:
    return f"{n} {plural(n, 'раз', 'раза', 'раз')} в день"


def settings_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hours = chat_settings(chat_id)["hours"]
    sched = f"{times_word(len(hours))}\n<i>{fmt_hours(hours)}</i>" if hours else "выключена, только по /new"
    n = len(load_sources())
    text = f"<b>Настройки</b>\n\nПодборка: {sched}\nИсточников: {n}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 Время и частота", callback_data="set:time")],
        [InlineKeyboardButton(text="➕ Добавить источник", callback_data="set:add"),
         InlineKeyboardButton(text="📋 Мои источники", callback_data="set:list")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="set:done")],
    ])
    return text, kb


def time_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hours = set(chat_settings(chat_id)["hours"])
    now = f"Сейчас: {fmt_hours(list(hours))}" if hours else "Сейчас: выключено"
    text = ("<b>Когда присылать подборку?</b>\n\n"
            "Отметь часы: сколько отметишь, столько раз в день. Можно выбрать готовый вариант.\n\n"
            f"<i>{now}</i>")
    preset_of = next((k for k, v in PRESETS.items() if set(v) == hours), 0 if not hours else None)
    mark = lambda label, on: f"• {label} •" if on else label
    rows = [[
        InlineKeyboardButton(text=mark("1 раз", preset_of == 1), callback_data="time:p:1"),
        InlineKeyboardButton(text=mark("2 раза", preset_of == 2), callback_data="time:p:2"),
        InlineKeyboardButton(text=mark("3 раза", preset_of == 3), callback_data="time:p:3"),
        InlineKeyboardButton(text=mark("Выкл", preset_of == 0), callback_data="time:p:0"),
    ]]
    for r in range(0, len(HOUR_GRID), 4):
        rows.append([
            InlineKeyboardButton(text=f"{h:02d} ✓" if h in hours else f"{h:02d}", callback_data=f"time:h:{h}")
            for h in HOUR_GRID[r:r + 4]
        ])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="set:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def list_view() -> tuple[str, InlineKeyboardMarkup]:
    srcs = load_sources()
    text = "<b>Мои источники</b>\n\nНажми на канал, чтобы удалить его." if srcs else \
        "<b>Мои источники</b>\n\nПока пусто — добавь первый канал."
    rows = [[InlineKeyboardButton(text=f"✕ {title[:40]}", callback_data=f"src:del:{h}")]
            for h, title in srcs.items()]
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="set:add"),
                 InlineKeyboardButton(text="← Настройки", callback_data="set:main")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


ADD_TEXT = ("<b>Добавить источник</b>\n\nПришли ссылку на Telegram-канал или его @username\n"
            "<i>например t.me/kudapoyti_ast</i>")
ADD_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="← Настройки", callback_data="set:main")]])


def allowed(user_id: int | None) -> bool:
    return not ALLOWED or (user_id in ALLOWED)


@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(cq: CallbackQuery):
    if not allowed(cq.from_user.id):
        return await cq.answer()
    action = cq.data.split(":", 1)[1]
    chat_id = cq.message.chat.id
    awaiting_source.discard(chat_id)
    if action == "main":
        await edit(cq.message, *settings_view(chat_id))
    elif action == "time":
        await edit(cq.message, *time_view(chat_id))
    elif action == "list":
        await edit(cq.message, *list_view())
    elif action == "add":
        awaiting_source.add(chat_id)
        await edit(cq.message, ADD_TEXT, ADD_KB)
    elif action == "done":
        text, _ = settings_view(chat_id)
        await edit(cq.message, text + "\n\n" + HELP, InlineKeyboardMarkup(inline_keyboard=[]))
    await cq.answer()


@dp.callback_query(F.data.startswith("time:"))
async def cb_time(cq: CallbackQuery):
    if not allowed(cq.from_user.id):
        return await cq.answer()
    _, kind, val = cq.data.split(":")
    chat_id = cq.message.chat.id
    hours = set(chat_settings(chat_id)["hours"])
    if kind == "p":
        hours = set(PRESETS.get(int(val), []))
    else:
        hours ^= {int(val)}
    update_chat_settings(chat_id, hours=sorted(hours))
    await edit(cq.message, *time_view(chat_id))
    await cq.answer()


@dp.callback_query(F.data.startswith("src:del:"))
async def cb_src_del(cq: CallbackQuery):
    if not allowed(cq.from_user.id):
        return await cq.answer()
    h = cq.data.split(":", 2)[2]
    srcs = load_sources()
    title = srcs.pop(h, h)
    save_sources(srcs)
    await edit(cq.message, *list_view())
    await cq.answer(f"Удалил «{title}»")


HANDLE_RE = re.compile(r"^(?:(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,})\S*|@?([A-Za-z0-9_]{4,}))$")


async def add_source(msg: Message):
    m = HANDLE_RE.search(msg.text.strip())
    h = m and (m[1] or m[2])
    back = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Ещё", callback_data="set:add"),
        InlineKeyboardButton(text="← Настройки", callback_data="set:main")]])
    if not h:
        return await msg.answer("Не понял ссылку. Пришли t.me/имя_канала или @имя_канала", reply_markup=ADD_KB)
    srcs = load_sources()
    if h in srcs:
        awaiting_source.discard(msg.chat.id)
        return await msg.answer(f"«{html.escape(srcs[h])}» уже есть в источниках", reply_markup=back)
    try:
        title, _, _ = tg._parse(await tg._page(h, None), h)
    except Exception:
        return await msg.answer(
            "Не получается читать этот канал: он приватный или у него закрыто веб-превью. "
            "Попробуй другой.", reply_markup=ADD_KB)
    srcs[h] = title
    save_sources(srcs)
    awaiting_source.discard(msg.chat.id)
    await msg.answer(f"✅ Добавил «{html.escape(title)}»\n<i>Теперь источников: {len(srcs)}</i>",
                     reply_markup=back)


# ---------- команды ----------

@dp.message(Command("start", "help"))
async def cmd_start(msg: Message):
    if not allowed(msg.from_user.id):
        return await msg.answer(f"Нет доступа. Твой id: <code>{msg.from_user.id}</code>")
    s = load_settings()
    if str(msg.chat.id) not in s:
        update_chat_settings(msg.chat.id, hours=DEFAULT_SCHEDULE)
        await msg.answer(WELCOME)
    else:
        await msg.answer(HELP)
    text, kb = settings_view(msg.chat.id)
    await msg.answer(text, reply_markup=kb)


@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    if allowed(msg.from_user.id):
        text, kb = settings_view(msg.chat.id)
        await msg.answer(text, reply_markup=kb)


async def run_manual(msg: Message, forced: datetime | None):
    if run_lock.locked():
        return await msg.answer("Уже собираю, подожди пару минут 🙂")
    async with run_lock:
        progress = await msg.answer("⏳ Собираю…")
        posts = await collect(msg.chat.id, forced, progress)
        try:
            await progress.delete()
        except Exception:
            pass
    if posts:
        await send_digest(msg.bot, msg.chat.id, posts, "🗞 Новое")
    else:
        await msg.answer("Новых постов нет")


@dp.message(Command("new"))
async def cmd_new(msg: Message):
    if allowed(msg.from_user.id):
        await run_manual(msg, None)


@dp.message(Command("since"))
async def cmd_since(msg: Message, command: CommandObject):
    if not allowed(msg.from_user.id):
        return
    since = parse_since(command.args or "")
    if not since:
        return await msg.answer("Не понял время. Примеры: /since 6h, /since 2d, /since 18.11, /since 18.11 15:00")
    await run_manual(msg, since)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message):
    if allowed(msg.from_user.id) and msg.chat.id in awaiting_source:
        await add_source(msg)


# ---------- рассылка по расписанию ----------

async def scheduler(bot: Bot):
    while True:
        try:
            now = datetime.now(TZ)
            slot = now.strftime("%Y-%m-%d %H")
            for chat, cfg in load_settings().items():
                if now.hour not in cfg.get("hours", []) or cfg.get("last_slot") == slot:
                    continue
                update_chat_settings(int(chat), last_slot=slot)
                async with run_lock:
                    posts = await collect(int(chat), None)
                if posts:                                  # пусто — молчим
                    await send_digest(bot, int(chat), posts, f"🗞 Подборка на {now.hour:02d}:00")
        except Exception:
            log.exception("scheduler")
        await asyncio.sleep(30)


async def main():
    await tg.start()
    load_sources()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="new", description="Новое с прошлой проверки"),
        BotCommand(command="since", description="С указанного времени: 6h, 2d, 18.11"),
        BotCommand(command="settings", description="Время рассылки и источники"),
    ])
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await tg.stop()


if __name__ == "__main__":
    asyncio.run(main())
