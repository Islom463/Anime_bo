"""
Anime / Kino boti: videolar o'zida saqlanadi (file_id bazada), foydalanuvchi havola orqali yoki qidirib topadi.

Admin (siz) botga video yuborasiz -> bot saqlaydi -> havola beradi: https://t.me/BOT?start=t12
Foydalanuvchi havolani bossa, anime kartasi va qismlar tugmalari chiqadi.

Render Environment:
  BOT_TOKEN          - majburiy
  ADMIN_ID           - sizning Telegram ID'ingiz (botda /myid yozing). Bir nechta bo'lsa vergul bilan
  REQUIRED_CHANNELS  - ixtiyoriy majburiy obuna: @kanal1,@kanal2 (bot kanalda admin bo'lishi kerak)
  PROTECT_CONTENT    - ixtiyoriy: 1 bo'lsa videoni forward/saqlash taqiqlanadi
  DB_PATH            - ixtiyoriy (Persistent Disk bo'lsa: /var/data/anime.db)
  BACKUP_MINUTES     - ixtiyoriy: avtomatik zaxira oralig'i (daqiqa), standart 30
  POST_CHANNELS      - ixtiyoriy: postlar yuboriladigan kanallar: @kanal1,@kanal2 (bot kanalda admin bo'lishi kerak)
"""
import os
import re
import html
import hashlib
import time
import shutil
import asyncio
import logging
import sqlite3
import tempfile
import threading
import urllib.request
from contextlib import closing
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError, TelegramRetryAfter, TelegramBadRequest
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton,
                           FSInputFile, InputMediaDocument, ErrorEvent)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BOT_STATUS = {"text": "Bot ishga tushmoqda..."}

DB_PATH = os.getenv("DB_PATH", "anime.db")
_db_dir = os.path.dirname(os.path.abspath(DB_PATH))
os.makedirs(_db_dir, exist_ok=True)

ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_ID", "") + " " + os.getenv("ADMIN_IDS", ""))}
REQUIRED = [c.strip() for c in re.split(r"[,\s]+", os.getenv("REQUIRED_CHANNELS", "")) if c.strip()]
REQUIRED = [c if c.startswith("@") or c.startswith("-100") else "@" + c for c in REQUIRED]
PROTECT = os.getenv("PROTECT_CONTENT", "0") == "1"
POST_CHANNELS = [c.strip() for c in re.split(r"[,\s]+", os.getenv("POST_CHANNELS", "")) if c.strip()]
POST_CHANNELS = [c if c.startswith("@") or c.startswith("-100") else "@" + c for c in POST_CHANNELS]
try:
    BACKUP_MINUTES = float(os.getenv("BACKUP_MINUTES", "30"))
except ValueError:
    BACKUP_MINUTES = 30.0
PAGE = 10
EP_PAGE = 40

# Fon vazifalari garbage collector tomonidan o'chib ketmasligi uchun ularni saqlab turamiz
_BG_TASKS = set()


def run_bg(coro):
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


# ---------------- Render uchun veb-server va keep-alive ----------------
async def _root_handler(request):
    return web.Response(text=BOT_STATUS["text"])


async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", _root_handler), web.get("/ping", _root_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()
    logging.info("Veb-server ishga tushdi (port %s)", os.getenv("PORT", 8080))


def self_ping():
    while True:
        time.sleep(240)
        try:
            url = os.getenv("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{os.environ.get('PORT', 8080)}"
            urllib.request.urlopen(url, timeout=20).read()
        except Exception:
            pass


def detect_token():
    tok_re = r"\d{6,}:[A-Za-z0-9_-]{30,}"
    env = {k: (v or "").strip().strip('"').strip("'") for k, v in os.environ.items()}
    for k in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN"):
        m = re.search(tok_re, env.get(k, ""))
        if m:
            return m.group(0)
    for v in env.values():
        m = re.search(tok_re, v)
        if m:
            return m.group(0)
    return ""


TOKEN = detect_token()
if not TOKEN:
    while True:
        logging.error("BOT_TOKEN topilmadi! Render Environment'ga BOT_TOKEN ni kiriting.")
        time.sleep(30)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------------- Token / ulanish diagnostikasi ----------------
class _StatusLogHandler(logging.Handler):
    """aiogram loglaridagi Conflict/Unauthorized xatolarini '/' sahifasida ko'rsatadi."""
    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        if "TelegramConflictError" in msg or "terminated by other getUpdates" in msg:
            BOT_STATUS["text"] = (
                f"XATO (Conflict, {stamp} UTC): bu token boshqa joyda ham ishlayapti. "
                "Eski Render xizmatini, kompyuter yoki boshqa hostingdagi nusxani to'xtating."
            )
        elif "TelegramUnauthorizedError" in msg:
            BOT_STATUS["text"] = f"XATO (Unauthorized, {stamp} UTC): Telegram tokenni qabul qilmadi."


logging.getLogger("aiogram").addHandler(_StatusLogHandler())


async def wait_for_valid_token():
    """Token Telegram'da yaroqli ekanini tekshiradi; xatoni logda va '/' sahifada tushunarli ko'rsatadi."""
    while True:
        try:
            me = await bot.get_me()
            BOT_STATUS["text"] = f"OK: @{me.username} ishlayapti"
            logging.info("Bot ulandi: @%s", me.username)
            return
        except TelegramUnauthorizedError:
            BOT_STATUS["text"] = (
                "XATO: Telegram tokenni qabul qilmadi (Unauthorized). Sabablari: token noto'g'ri nusxalangan yoki "
                "BotFather'da /revoke qilingan. BotFather'dan joriy tokenni oling, Render'da BOT_TOKEN ni yangilab qayta deploy qiling."
            )
            logging.error(BOT_STATUS["text"])
        except Exception as ex:
            BOT_STATUS["text"] = f"Telegramga ulanib bo'lmadi ({type(ex).__name__}). Qayta urinilmoqda..."
            logging.error("%s: %s", BOT_STATUS["text"], ex)
        await asyncio.sleep(60)


# ---------------- Baza ----------------
def db():
    return closing(sqlite3.connect(DB_PATH, timeout=30))


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, joined TEXT);
        CREATE TABLE IF NOT EXISTS titles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, name TEXT, description TEXT, poster TEXT,
            views INTEGER DEFAULT 0, created TEXT);
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title_id INTEGER, ep_no INTEGER, file_id TEXT, ftype TEXT);
        CREATE INDEX IF NOT EXISTS idx_ep_title ON episodes (title_id, ep_no);
        """)
        c.commit()


init_db()


def e(s):
    return html.escape(str(s), quote=False)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def add_user(user):
    if not user:
        return
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id, name, joined) VALUES (?, ?, ?)",
                  (user.id, user.full_name, datetime.now().strftime("%Y-%m-%d %H:%M")))
        c.commit()


def get_title(tid):
    with db() as c:
        return c.execute("SELECT id, category, name, description, poster, views FROM titles WHERE id = ?", (tid,)).fetchone()


def get_episodes(tid):
    with db() as c:
        return c.execute("SELECT id, ep_no, file_id, ftype FROM episodes WHERE title_id = ? ORDER BY ep_no, id", (tid,)).fetchall()


def get_episode(eid):
    with db() as c:
        return c.execute("SELECT id, title_id, ep_no, file_id, ftype FROM episodes WHERE id = ?", (eid,)).fetchone()


def add_episode(tid, file_id, ftype):
    with db() as c:
        n = c.execute("SELECT COALESCE(MAX(ep_no), 0) + 1 FROM episodes WHERE title_id = ?", (tid,)).fetchone()[0]
        c.execute("INSERT INTO episodes (title_id, ep_no, file_id, ftype) VALUES (?, ?, ?, ?)", (tid, n, file_id, ftype))
        c.commit()
    return n


def to_int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def btn(text, data=None, url=None):
    return InlineKeyboardButton(text=text, callback_data=data) if data else InlineKeyboardButton(text=text, url=url)


MENU = {"🎌 Animelar": "anime", "🎬 Kinolar": "kino", "🔥 Mashhur": "pop", "🔎 Qidirish": "search"}


def menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎌 Animelar"), KeyboardButton(text="🎬 Kinolar")],
        [KeyboardButton(text="🔥 Mashhur"), KeyboardButton(text="🔎 Qidirish")],
    ], resize_keyboard=True)


async def bot_username():
    return (await bot.me()).username


# ---------------- Xatolarni ushlash (bot yiqilib qolmasligi uchun) ----------------
@dp.errors()
async def on_error(event: ErrorEvent):
    logging.exception("Handler xatosi: %s", event.exception, exc_info=event.exception)
    return True


# ---------------- Majburiy obuna ----------------
async def missing_channels(user_id):
    missing = []
    for ch in REQUIRED:
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ("left", "kicked"):
                missing.append(ch)
        except Exception as ex:
            logging.warning("obuna tekshirib bo'lmadi (%s): %s — bot kanalda admin ekanini tekshiring", ch, ex)
    return missing


async def gate(chat_id, user_id, payload=""):
    """True — o'tish mumkin. False — obuna xabari yuborildi."""
    if not REQUIRED or is_admin(user_id):
        return True
    missing = await missing_channels(user_id)
    if not missing:
        return True
    kb = [[btn(f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")] for ch in missing if ch.startswith("@")]
    kb.append([btn("✅ Tekshirish", f"chk:{payload}")])
    await bot.send_message(chat_id, "❗ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    return False


# ---------------- Foydalanuvchi qismi ----------------
@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(f"🆔 Sizning ID: <code>{message.from_user.id}</code>")


async def open_payload(chat_id, user_id, payload):
    m = re.fullmatch(r"([te]?)(\d+)", payload or "")
    if not m:
        return await bot.send_message(chat_id, "❌ Havola noto'g'ri.", reply_markup=menu_kb())
    kind, num = m.group(1), int(m.group(2))
    if kind == "e":
        return await send_episode(chat_id, num)
    await show_title(chat_id, num)


@dp.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    restore_wait.discard(message.from_user.id)
    add_user(message.from_user)
    payload = (command.args or "").strip()[:20]
    if not await gate(message.chat.id, message.from_user.id, payload):
        return
    if payload:
        return await open_payload(message.chat.id, message.from_user.id, payload)
    await message.answer(
        "👋 Salom! Bu yerda anime va kinolarni topishingiz mumkin.\n\n"
        "🔎 Nomini yozing yoki pastdagi tugmalardan foydalaning.", reply_markup=menu_kb())


@dp.callback_query(F.data.startswith("chk:"))
async def cb_check(callback: types.CallbackQuery):
    payload = callback.data.split(":", 1)[1]
    if await missing_channels(callback.from_user.id):
        return await callback.answer("Hali hamma kanallarga obuna bo'lmadingiz ❌", show_alert=True)
    await callback.answer("✅ Rahmat!")
    try:
        await callback.message.delete()
    except Exception:
        pass
    if payload:
        await open_payload(callback.message.chat.id, callback.from_user.id, payload)
    else:
        await bot.send_message(callback.message.chat.id, "✅ Endi foydalanishingiz mumkin!", reply_markup=menu_kb())


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    restore_wait.discard(message.from_user.id)
    await message.answer("✅ Bekor qilindi.", reply_markup=menu_kb())


@dp.message(F.text.in_(set(MENU)))
async def on_menu(message: types.Message, state: FSMContext):
    await state.clear()
    add_user(message.from_user)
    if not await gate(message.chat.id, message.from_user.id):
        return
    act = MENU[message.text]
    if act == "search":
        return await message.answer("🔎 Anime yoki kino nomini yozing:")
    await send_list(message.chat.id, act, 0)


def list_query(kind):
    if kind == "pop":
        return "SELECT id, name, category FROM titles ORDER BY views DESC, id DESC", ()
    return "SELECT id, name, category FROM titles WHERE category = ? ORDER BY id DESC", (kind,)


async def send_list(chat_id, kind, page, edit_message=None):
    q, args = list_query(kind)
    with db() as c:
        rows = c.execute(q, args).fetchall()
    total = len(rows)
    if not total:
        text = "😕 Hozircha hech narsa yo'q."
        if edit_message:
            try:
                return await edit_message.edit_text(text)
            except TelegramBadRequest:
                return
        return await bot.send_message(chat_id, text)
    pages = (total - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE:(page + 1) * PAGE]
    icon = {"anime": "🎌", "kino": "🎬"}
    kb = [[btn(f"{icon.get(cat, '🎞')} {name[:40]}", f"t:{tid}")] for tid, name, cat in chunk]
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"list:{kind}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("➡️", f"list:{kind}:{page + 1}"))
    kb.append(nav)
    title = {"anime": "🎌 Animelar", "kino": "🎬 Kinolar", "pop": "🔥 Mashhurlar"}[kind]
    text, markup = f"{title} ({total} ta):", InlineKeyboardMarkup(inline_keyboard=kb)
    if edit_message:
        try:
            return await edit_message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            return
    await bot.send_message(chat_id, text, reply_markup=markup)


@dp.callback_query(F.data.startswith("list:"))
async def cb_list(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split(":")
    if len(parts) == 3 and parts[1] in ("anime", "kino", "pop"):
        await send_list(callback.message.chat.id, parts[1], to_int(parts[2]), edit_message=callback.message)


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()


def episodes_kb(tid, eps, page=0):
    pages = (len(eps) - 1) // EP_PAGE + 1
    page = max(0, min(page, pages - 1))
    chunk = eps[page * EP_PAGE:(page + 1) * EP_PAGE]
    kb, row = [], []
    for eid, no, _, _ in chunk:
        row.append(btn(str(no), f"ep:{eid}"))
        if len(row) == 5:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"tp:{tid}:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("➡️", f"tp:{tid}:{page + 1}"))
        kb.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def show_title(chat_id, tid):
    t = get_title(tid)
    if not t:
        return await bot.send_message(chat_id, "❌ Topilmadi. Ehtimol o'chirilgan.", reply_markup=menu_kb())
    with db() as c:
        c.execute("UPDATE titles SET views = views + 1 WHERE id = ?", (tid,))
        c.commit()
    eps = get_episodes(tid)
    if not eps:
        return await bot.send_message(chat_id, f"🎞 <b>{e(t[2])}</b>\n\nHozircha qismlar yo'q.")
    if len(eps) == 1 and t[1] == "kino":
        return await send_episode(chat_id, eps[0][0])
    icon = "🎌" if t[1] == "anime" else "🎬"
    desc = e(t[3])[:700] if t[3] else ""
    caption = f"{icon} <b>{e(t[2])}</b>\n" + (f"\n{desc}\n" if desc else "") + f"\n📺 Qismlar: {len(eps)} ta\nQismni tanlang 👇"
    kb = episodes_kb(tid, eps)
    if t[4]:
        try:
            return await bot.send_photo(chat_id, t[4], caption=caption, reply_markup=kb)
        except Exception as ex:
            logging.warning("poster yuborilmadi: %s", ex)
    await bot.send_message(chat_id, caption, reply_markup=kb)


@dp.callback_query(F.data.startswith("t:"))
async def cb_title(callback: types.CallbackQuery):
    await callback.answer()
    tid = to_int(callback.data.split(":")[1])
    if not await gate(callback.message.chat.id, callback.from_user.id, f"t{tid}"):
        return
    await show_title(callback.message.chat.id, tid)


@dp.callback_query(F.data.startswith("tp:"))
async def cb_title_page(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    tid, page = to_int(parts[1]), to_int(parts[2])
    eps = get_episodes(tid)
    if eps:
        try:
            await callback.message.edit_reply_markup(reply_markup=episodes_kb(tid, eps, page))
        except TelegramBadRequest:
            pass


async def send_episode(chat_id, eid):
    ep = get_episode(eid)
    if not ep:
        return await bot.send_message(chat_id, "❌ Video topilmadi.")
    _, tid, no, file_id, ftype = ep
    t = get_title(tid)
    eps = get_episodes(tid)
    idx = next((i for i, x in enumerate(eps) if x[0] == eid), 0)
    nav = []
    if idx > 0:
        nav.append(btn("⬅️ Oldingi", f"ep:{eps[idx - 1][0]}"))
    if len(eps) > 1:
        nav.append(btn("📋 Qismlar", f"t:{tid}"))
    if idx < len(eps) - 1:
        nav.append(btn("Keyingi ➡️", f"ep:{eps[idx + 1][0]}"))
    name = t[2] if t else ""
    caption = f"🎬 <b>{e(name)}</b>" + (f"\n{no}-qism" if len(eps) > 1 else "") + "\n\n🍿 Yoqimli tomosha!"
    kb = InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None
    try:
        if ftype == "document":
            await bot.send_document(chat_id, file_id, caption=caption, reply_markup=kb, protect_content=PROTECT)
        else:
            await bot.send_video(chat_id, file_id, caption=caption, reply_markup=kb, protect_content=PROTECT,
                                 supports_streaming=True)
    except Exception as ex:
        logging.warning("episode yuborilmadi: %s", ex)
        await bot.send_message(chat_id, "❌ Videoni yuborib bo'lmadi (file_id yaroqsiz bo'lishi mumkin).")


@dp.callback_query(F.data.startswith("ep:"))
async def cb_episode(callback: types.CallbackQuery):
    await callback.answer()
    eid = to_int(callback.data.split(":")[1])
    if not await gate(callback.message.chat.id, callback.from_user.id, f"e{eid}"):
        return
    await send_episode(callback.message.chat.id, eid)


# ---------------- Admin ----------------
class Add(StatesGroup):
    name = State()
    desc = State()
    poster = State()
    eps = State()


class Bc(StatesGroup):
    msg = State()


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Yangi qo'shish", "adm:add")],
        [btn("📋 Ro'yxat / tahrirlash", "adm:list:0"), btn("📊 Statistika", "adm:stats")],
        [btn("📢 Xabar yuborish", "adm:bc"), btn("💾 Backup", "adm:backup")],
    ])


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.answer(f"⛔ Siz admin emassiz. ID'ingiz: <code>{message.from_user.id}</code>\n"
                                    "Render'da ADMIN_ID ga shu raqamni yozing.")
    await state.clear()
    await message.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())


async def admin_only(callback):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Ruxsat yo'q", show_alert=True)
        return False
    return True


@dp.callback_query(F.data == "adm:add")
async def adm_add(callback: types.CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await callback.answer()
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🎌 Anime", "cat:anime"), btn("🎬 Kino", "cat:kino")]])
    await callback.message.answer("Turini tanlang:", reply_markup=kb)


@dp.callback_query(F.data.startswith("cat:"))
async def adm_cat(callback: types.CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await callback.answer()
    cat = callback.data.split(":")[1]
    if cat not in ("anime", "kino"):
        return
    await state.update_data(category=cat)
    await state.set_state(Add.name)
    await callback.message.answer("✏️ Nomini yuboring:\n(Bekor qilish: /cancel)")


@dp.message(Add.name, F.text)
async def add_name(message: types.Message, state: FSMContext):
    if message.text.startswith("/"):
        return await message.answer("✏️ Nomni oddiy matn qilib yuboring (yoki /cancel).")
    await state.update_data(name=message.text.strip()[:150])
    await state.set_state(Add.desc)
    await message.answer("📝 Qisqa tavsif yuboring (yoki /skip):")


@dp.message(Add.desc, F.text)
async def add_desc(message: types.Message, state: FSMContext):
    desc = "" if message.text.strip() == "/skip" else message.text.strip()[:700]
    await state.update_data(desc=desc)
    await state.set_state(Add.poster)
    await message.answer("🖼 Poster rasmini yuboring (yoki /skip):")


async def create_title(message, state, poster):
    data = await state.get_data()
    if "category" not in data or "name" not in data:
        await state.clear()
        return await message.answer("⚠️ Ma'lumot yo'qolgan. /admin orqali qaytadan boshlang.")
    with db() as c:
        cur = c.execute("INSERT INTO titles (category, name, description, poster, created) VALUES (?, ?, ?, ?, ?)",
                        (data["category"], data["name"], data.get("desc", ""), poster,
                         datetime.now().strftime("%Y-%m-%d %H:%M")))
        tid = cur.lastrowid
        c.commit()
    await state.update_data(title_id=tid)
    await state.set_state(Add.eps)
    await message.answer(
        f"✅ <b>{e(data['name'])}</b> yaratildi.\n\n🎞 Endi videolarni <b>ketma-ket</b> yuboring (1-qism, 2-qism...).\n"
        "Kino bo'lsa — bitta video. Tugagach: /done")


@dp.message(Add.poster, F.photo)
async def add_poster(message: types.Message, state: FSMContext):
    await create_title(message, state, message.photo[-1].file_id)


@dp.message(Add.poster, F.text)
async def add_poster_skip(message: types.Message, state: FSMContext):
    if message.text.strip() == "/skip":
        return await create_title(message, state, "")
    await message.answer("🖼 Rasm yuboring yoki /skip yozing.")


@dp.message(Add.eps, Command("done"))
async def add_done(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("title_id")
    await state.clear()
    eps = get_episodes(tid) if tid else []
    if not eps:
        return await message.answer("⚠️ Hech qanday video yuborilmadi.", reply_markup=menu_kb())
    link = f"https://t.me/{await bot_username()}?start=t{tid}"
    rows = [[btn("📣 Kanalga yuborish", f"adm:post:{tid}")]] if POST_CHANNELS else []
    await message.answer(f"✅ Tayyor! {len(eps)} ta qism saqlandi.\n\n🔗 Havola:\n<code>{link}</code>",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else menu_kb())
    if rows:
        await message.answer("👇 Pastdagi menyu qaytarildi.", reply_markup=menu_kb())
    run_bg(safe_push_backup())


_ep_lock = asyncio.Lock()


@dp.message(Add.eps, F.video | F.document | F.animation)
async def add_episode_msg(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("title_id")
    if not tid or not get_title(tid):
        return await message.answer("⚠️ Avval /admin orqali yangi qo'shing.")
    if message.video:
        fid, ftype = message.video.file_id, "video"
    elif message.animation:
        fid, ftype = message.animation.file_id, "video"
    else:
        fid, ftype = message.document.file_id, "document"
    async with _ep_lock:
        n = add_episode(tid, fid, ftype)
    await message.answer(f"✅ {n}-qism saqlandi. Yana yuboring yoki /done")


@dp.message(Add.eps)
async def add_eps_hint(message: types.Message):
    await message.answer("🎞 Video yuboring yoki tugatish uchun /done")


@dp.callback_query(F.data.startswith("adm:list:"))
async def adm_list(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    page = to_int(callback.data.split(":")[2])
    with db() as c:
        rows = c.execute("SELECT t.id, t.name, t.category, (SELECT COUNT(*) FROM episodes e WHERE e.title_id = t.id) "
                         "FROM titles t ORDER BY t.id DESC").fetchall()
    if not rows:
        return await callback.message.answer("Hali hech narsa qo'shilmagan.")
    pages = (len(rows) - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    kb = [[btn(f"{'🎌' if cat == 'anime' else '🎬'} {name[:30]} ({n})", f"adm:t:{tid}")]
          for tid, name, cat, n in rows[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"adm:list:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("➡️", f"adm:list:{page + 1}"))
    kb.append(nav)
    text, markup = f"📋 Ro'yxat ({len(rows)} ta):", InlineKeyboardMarkup(inline_keyboard=kb)
    # Sahifa almashtirilganda yangi xabar emas, shu xabarni o'zgartiramiz
    if callback.message.text and callback.message.text.startswith("📋 Ro'yxat"):
        try:
            return await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            return
    await callback.message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("adm:t:"))
async def adm_title(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    tid = to_int(callback.data.split(":")[2])
    t = get_title(tid)
    if not t:
        return await callback.message.answer("Topilmadi.")
    n = len(get_episodes(tid))
    link = f"https://t.me/{await bot_username()}?start=t{tid}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Qism qo'shish", f"adm:eps:{tid}"), btn("➖ Oxirgi qismni o'chirish", f"adm:dl:{tid}")],
        [btn("👁 Ko'rish", f"t:{tid}"), btn("🗑 O'chirish", f"adm:del:{tid}")],
    ] + ([[btn("📣 Kanalga yuborish", f"adm:post:{tid}")]] if POST_CHANNELS else []))
    await callback.message.answer(f"<b>{e(t[2])}</b>\n📺 Qismlar: {n} · 👁 {t[5]}\n🔗 <code>{link}</code>", reply_markup=kb)


@dp.callback_query(F.data.startswith("adm:post:"))
async def adm_post_to_channels(callback: types.CallbackQuery):
    """Anime/kino kartasini kanal(lar)ga 'Tomosha qilish' tugmasi bilan yuboradi."""
    if not await admin_only(callback):
        return
    tid = to_int(callback.data.split(":")[2])
    t = get_title(tid)
    if not t:
        return await callback.answer("Topilmadi", show_alert=True)
    if not POST_CHANNELS:
        return await callback.answer("Render'da POST_CHANNELS ni kiriting (masalan @kanal1,@kanal2)", show_alert=True)
    await callback.answer("Yuborilmoqda...")
    eps = get_episodes(tid)
    icon = "🎌" if t[1] == "anime" else "🎬"
    desc = e(t[3])[:600] if t[3] else ""
    caption = (f"{icon} <b>{e(t[2])}</b>\n" + (f"\n{desc}\n" if desc else "")
               + (f"\n📺 Qismlar: {len(eps)} ta" if len(eps) > 1 else "")
               + f"\n\n▶️ Tomosha qilish: @{await bot_username()}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("▶️ Tomosha qilish", url=f"https://t.me/{await bot_username()}?start=t{tid}")]])
    ok, bad = [], []
    for ch in POST_CHANNELS:
        try:
            if t[4]:
                await bot.send_photo(ch, t[4], caption=caption, reply_markup=kb)
            else:
                await bot.send_message(ch, caption, reply_markup=kb)
            ok.append(ch)
        except Exception as ex:
            logging.warning("kanalga yuborib bo'lmadi (%s): %s", ch, ex)
            bad.append(f"{ch} — {type(ex).__name__}")
    text = ""
    if ok:
        text += "✅ Yuborildi: " + ", ".join(ok)
    if bad:
        text += ("\n" if text else "") + "❌ Yuborilmadi (bot kanalda admin ekanini tekshiring):\n" + "\n".join(bad)
    await callback.message.answer(text or "Hech narsa yuborilmadi.")


@dp.callback_query(F.data.startswith("adm:eps:"))
async def adm_more_eps(callback: types.CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await callback.answer()
    tid = to_int(callback.data.split(":")[2])
    if not get_title(tid):
        return await callback.message.answer("Topilmadi.")
    await state.clear()
    await state.update_data(title_id=tid)
    await state.set_state(Add.eps)
    await callback.message.answer("🎞 Qo'shiladigan videolarni yuboring. Tugagach: /done")


@dp.callback_query(F.data.startswith("adm:dl:"))
async def adm_del_last(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    tid = to_int(callback.data.split(":")[2])
    with db() as c:
        row = c.execute("SELECT id, ep_no FROM episodes WHERE title_id = ? ORDER BY ep_no DESC, id DESC LIMIT 1",
                        (tid,)).fetchone()
        if row:
            c.execute("DELETE FROM episodes WHERE id = ?", (row[0],))
            c.commit()
    if not row:
        return await callback.answer("Qismlar yo'q", show_alert=True)
    await callback.answer(f"{row[1]}-qism o'chirildi", show_alert=True)
    run_bg(safe_push_backup())


@dp.callback_query(F.data.startswith("adm:del:"))
async def adm_del(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    tid = to_int(callback.data.split(":")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Ha, o'chir", f"adm:delok:{tid}"), btn("↩️ Yo'q", "adm:cancel")]])
    await callback.message.answer("⚠️ Rostdan ham o'chirilsinmi? (barcha qismlari bilan)", reply_markup=kb)


@dp.callback_query(F.data == "adm:cancel")
async def adm_cancel_cb(callback: types.CallbackQuery):
    await callback.answer("Bekor qilindi")
    try:
        await callback.message.delete()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("adm:delok:"))
async def adm_delok(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer("O'chirildi")
    tid = to_int(callback.data.split(":")[2])
    with db() as c:
        c.execute("DELETE FROM episodes WHERE title_id = ?", (tid,))
        c.execute("DELETE FROM titles WHERE id = ?", (tid,))
        c.commit()
    try:
        await callback.message.edit_text("🗑 O'chirildi.")
    except Exception:
        pass
    run_bg(safe_push_backup())


@dp.callback_query(F.data == "adm:stats")
async def adm_stats(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    with db() as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        titles = c.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
        eps = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        views = c.execute("SELECT COALESCE(SUM(views), 0) FROM titles").fetchone()[0]
        top = c.execute("SELECT name, views FROM titles ORDER BY views DESC LIMIT 5").fetchall()
    lines = ["📊 <b>Statistika</b>\n", f"👥 Foydalanuvchilar: {users}", f"🎞 Nomlar: {titles}",
             f"📺 Qismlar: {eps}", f"👁 Ko'rishlar: {views}"]
    if top:
        lines.append("\n🔥 Top 5:")
        lines += [f"{i}. {e(n)} — {v}" for i, (n, v) in enumerate(top, 1)]
    await callback.message.answer("\n".join(lines))


@dp.callback_query(F.data == "adm:bc")
async def adm_bc(callback: types.CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await callback.answer()
    await state.set_state(Bc.msg)
    await callback.message.answer("📢 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring (matn, rasm, video...).\n"
                                  "Bekor qilish: /cancel")


async def do_broadcast(from_chat, msg_id, status):
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT user_id FROM users")]
    ok = bad = 0
    for i, uid in enumerate(ids, 1):
        for _ in range(3):
            try:
                await bot.copy_message(uid, from_chat, msg_id)
                ok += 1
                break
            except TelegramRetryAfter as ex:
                await asyncio.sleep(ex.retry_after + 1)
            except Exception:
                bad += 1
                break
        await asyncio.sleep(0.05)
        if i % 200 == 0:
            try:
                await status.edit_text(f"⏳ Yuborilmoqda... {i}/{len(ids)}")
            except Exception:
                pass
    try:
        await status.edit_text(f"✅ Yuborildi: {ok}\n❌ Yetmadi (bloklagan): {bad}")
    except Exception:
        await bot.send_message(from_chat, f"✅ Yuborildi: {ok}\n❌ Yetmadi (bloklagan): {bad}")


@dp.message(Bc.msg)
async def bc_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        return await message.answer("Xabarni yuboring yoki /cancel")
    await state.clear()
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    status = await message.answer(f"⏳ Yuborilmoqda... ({total} ta)")
    # Uzoq davom etadigan ishni fonda bajaramiz — bot boshqa foydalanuvchilarga javob berishda davom etadi
    run_bg(do_broadcast(message.chat.id, message.message_id, status))


# ---------------- Zaxira nusxa ----------------
restore_wait = set()


def make_backup_file():
    fd, path = tempfile.mkstemp(prefix="anime_backup_", suffix=".db")
    os.close(fd)
    with db() as src, closing(sqlite3.connect(path)) as dst:
        src.backup(dst)
    return path


def restore_db_file(path):
    try:
        with closing(sqlite3.connect(path)) as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
    except sqlite3.Error:
        return False
    if not {"titles", "episodes"} <= names:
        return False
    shutil.copyfile(path, DB_PATH + ".new")
    os.replace(DB_PATH + ".new", DB_PATH)
    init_db()
    return True


async def send_backup(chat_id, caption):
    path = await asyncio.to_thread(make_backup_file)
    try:
        await bot.send_document(chat_id, FSInputFile(path, filename=f"anime_{datetime.now():%Y%m%d_%H%M}.db"), caption=caption)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@dp.callback_query(F.data == "adm:backup")
async def adm_backup(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    await send_backup(callback.message.chat.id, "💾 Baza nusxasi. Tiklash: /restore")


@dp.message(Command("backup"))
async def cmd_backup(message: types.Message):
    if is_admin(message.from_user.id):
        await send_backup(message.chat.id, "💾 Baza nusxasi. Tiklash: /restore")


@dp.message(Command("restore"))
async def cmd_restore(message: types.Message, state: FSMContext):
    if is_admin(message.from_user.id):
        await state.clear()
        restore_wait.add(message.from_user.id)
        await message.answer("📥 Endi avval olingan .db faylni yuboring. ⚠️ Hozirgi baza almashtiriladi!\nBekor qilish: /cancel")


@dp.message(StateFilter(None), F.document)
async def on_restore_document(message: types.Message):
    uid = message.from_user.id
    if uid not in restore_wait or not is_admin(uid):
        return
    restore_wait.discard(uid)
    fd, tmp = tempfile.mkstemp(prefix=f"restore_{uid}_", suffix=".db")
    os.close(fd)
    try:
        await bot.download(message.document.file_id, destination=tmp)
        ok = await asyncio.to_thread(restore_db_file, tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    await message.answer("✅ Baza tiklandi!" if ok else "❌ Bu fayl mos emas (titles/episodes jadvallari yo'q yoki fayl buzilgan).")
    if ok:
        run_bg(safe_push_backup(force=True))


def _admin_chat_id():
    return sorted(ADMIN_IDS)[0] if ADMIN_IDS else None


# ===================== AVTOMATIK ZAXIRA VA TIKLASH (qadalgan xabar orqali) =====================
BACKUP_NAME = "anime_backup.db"
_backup_state = {"hash": None}
_restore_ran = [False]
RESTORE_DONE = None      # asyncio.Event — main() ichida yaratiladi (to'g'ri event loop'ga bog'lanishi uchun)
_backup_lock = None      # asyncio.Lock — main() ichida yaratiladi


def db_fingerprint():
    # views (ko'rishlar) har safar o'zgaradi — shuning uchun faqat kontent va foydalanuvchilar soni hisoblanadi
    h = hashlib.sha1()
    with db() as c:
        for row in c.execute("SELECT id, category, name, description, poster FROM titles ORDER BY id"):
            h.update(repr(row).encode("utf-8", "ignore"))
        for row in c.execute("SELECT id, title_id, ep_no, file_id, ftype FROM episodes ORDER BY id"):
            h.update(repr(row).encode("utf-8", "ignore"))
        h.update(str(c.execute("SELECT COUNT(*) FROM users").fetchone()[0]).encode())
    return h.hexdigest()


def db_has_data():
    with db() as c:
        return bool(c.execute("SELECT (SELECT COUNT(*) FROM titles)+(SELECT COUNT(*) FROM episodes)").fetchone()[0])


async def _pinned_backup(chat_id):
    """Admin chatida botning qadalgan zaxira xabarini topadi (bo'lmasa None)."""
    try:
        chat = await bot.get_chat(chat_id)
        pm = getattr(chat, "pinned_message", None)
        me = await bot.me()
        doc = getattr(pm, "document", None) if pm else None
        sender = getattr(pm, "from_user", None) if pm else None
        if doc and (doc.file_name or "") == BACKUP_NAME and getattr(sender, "id", None) == me.id:
            return pm
    except Exception as ex:
        logging.info("qadalgan zaxira topilmadi: %s", ex)
    return None


async def push_backup(chat_id, force=False):
    """Baza o'zgargan bo'lsa, zaxirani admin chatidagi bitta qadalgan xabarda yangilaydi."""
    async with _backup_lock:
        fp = await asyncio.to_thread(db_fingerprint)
        if not force and fp == _backup_state["hash"]:
            return False
        path = await asyncio.to_thread(make_backup_file)
        try:
            caption = "💾 Avtomatik zaxira (yangilanib turadi) · " + time.strftime("%d.%m %H:%M", time.gmtime()) + " UTC"
            pm = await _pinned_backup(chat_id)
            done = False
            if pm:
                try:
                    await bot.edit_message_media(
                        chat_id=chat_id, message_id=pm.message_id,
                        media=InputMediaDocument(media=FSInputFile(path, filename=BACKUP_NAME), caption=caption))
                    done = True
                except Exception as ex:
                    logging.info("zaxirani joyida yangilab bo'lmadi: %s", ex)
            if not done:
                m = await bot.send_document(chat_id, FSInputFile(path, filename=BACKUP_NAME), caption=caption,
                                            disable_notification=True)
                try:
                    await bot.pin_chat_message(chat_id, m.message_id, disable_notification=True)
                except Exception as ex:
                    logging.warning("zaxirani qadab bo'lmadi: %s", ex)
            _backup_state["hash"] = fp
            return True
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


async def safe_push_backup(force=False):
    """O'zgarishdan keyin darhol zaxira (xato bo'lsa jim o'tadi)."""
    if RESTORE_DONE is None or not RESTORE_DONE.is_set():
        return
    chat_id = _admin_chat_id()
    if not chat_id:
        return
    try:
        await push_backup(chat_id, force=force)
    except Exception as ex:
        logging.warning("zaxira xatosi: %s", ex)


async def startup_restore():
    """Baza bo'sh bo'lsa (Render qayta ishga tushgan), qadalgan zaxiradan avtomatik tiklaydi."""
    try:
        chat_id = _admin_chat_id()
        if not chat_id:
            logging.info("ADMIN_ID yo'q: avtomatik tiklash o'chiq")
            return
        if await asyncio.to_thread(db_has_data):
            _backup_state["hash"] = await asyncio.to_thread(db_fingerprint)
            return
        pm = await _pinned_backup(chat_id)
        if not pm:
            logging.info("qadalgan zaxira yo'q: bo'sh baza bilan boshlanadi")
            return
        fd, tmp = tempfile.mkstemp(prefix="auto_restore_", suffix=".db")
        os.close(fd)
        try:
            await bot.download(pm.document.file_id, destination=tmp)
            ok = await asyncio.to_thread(restore_db_file, tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if ok:
            _backup_state["hash"] = await asyncio.to_thread(db_fingerprint)
            logging.info("Baza avtomatik tiklandi")
            try:
                await bot.send_message(chat_id, "✅ Baza avtomatik tiklandi (qadalgan zaxiradan).")
            except Exception:
                pass
    except Exception:
        logging.exception("avtomatik tiklash xatosi")
    finally:
        RESTORE_DONE.set()


async def startup_restore_once():
    if _restore_ran[0]:
        return
    _restore_ran[0] = True
    await startup_restore()


async def backup_worker():
    await RESTORE_DONE.wait()
    await asyncio.sleep(30)
    while True:
        try:
            chat_id = _admin_chat_id()
            if chat_id:
                await push_backup(chat_id)
        except Exception as ex:
            logging.warning("auto-backup xatosi: %s", ex)
        await asyncio.sleep(max(BACKUP_MINUTES, 1) * 60)


# ---------------- Qidiruv (eng oxirida) ----------------
@dp.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_search(message: types.Message):
    add_user(message.from_user)
    if not await gate(message.chat.id, message.from_user.id):
        return
    q = message.text.strip().lower()
    if len(q) < 2:
        return await message.answer("🔎 Kamida 2 ta harf yozing.")
    # Raqam yozilsa (masalan, kanalda e'lon qilingan kod) — to'g'ridan-to'g'ri ochamiz
    if q.isdigit() and get_title(int(q)):
        return await show_title(message.chat.id, int(q))
    with db() as c:
        rows = c.execute("SELECT id, name, category FROM titles ORDER BY views DESC").fetchall()
    norm = lambda s: re.sub(r"[ʻʼ'`‘’]", "", s.lower())
    nq = norm(q)
    found = [(tid, name, cat) for tid, name, cat in rows if nq in norm(name)][:20]
    if not found:
        return await message.answer("😕 Hech narsa topilmadi. Boshqa nom bilan urinib ko'ring.")
    kb = [[btn(f"{'🎌' if cat == 'anime' else '🎬'} {name[:40]}", f"t:{tid}")] for tid, name, cat in found]
    await message.answer(f"🔎 Topildi: {len(found)} ta", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.message(StateFilter(None), F.text.startswith("/"))
async def unknown_command(message: types.Message):
    await message.answer("❓ Noma'lum buyruq. /start ni bosing.", reply_markup=menu_kb())


async def main():
    global RESTORE_DONE, _backup_lock
    RESTORE_DONE = asyncio.Event()
    _backup_lock = asyncio.Lock()
    threading.Thread(target=self_ping, daemon=True).start()
    await start_web_server()
    run_bg(backup_worker())
    while True:
        try:
            await wait_for_valid_token()
            await startup_restore_once()
            logging.info("Bot polling boshlandi...")
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, drop_pending_updates=True)
            # start_polling faqat to'xtatish signali (Render qayta deploy) kelganda qaytadi.
            # Shu yerda chiqib ketamiz — aks holda eski nusxa ishlashda davom etib, yangisi bilan Conflict beradi.
            logging.info("Polling to'xtatildi, bot o'chmoqda.")
            break
        except Exception as ex:
            logging.error("Polling xatoligi: %s", ex)
            await asyncio.sleep(5)
    try:
        if _admin_chat_id():
            await asyncio.wait_for(push_backup(_admin_chat_id()), timeout=8)
    except Exception:
        pass
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
