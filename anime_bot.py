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
"""
import os
import re
import html
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, FSInputFile

logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("DB_PATH", "anime.db")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_ID", "") + " " + os.getenv("ADMIN_IDS", ""))}
REQUIRED = [c.strip() for c in re.split(r"[,\s]+", os.getenv("REQUIRED_CHANNELS", "")) if c.strip()]
REQUIRED = [c if c.startswith("@") else "@" + c for c in REQUIRED]
PROTECT = os.getenv("PROTECT_CONTENT", "0") == "1"
try:
    BACKUP_HOURS = float(os.getenv("BACKUP_HOURS", "6"))
except ValueError:
    BACKUP_HOURS = 6.0
PAGE = 10
EP_PAGE = 40


# ---------------- Render uchun veb-server va keep-alive ----------------
async def _root_handler(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", _root_handler), web.get("/ping", _root_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()


def self_ping():
    while True:
        time.sleep(240)
        try:
            url = os.getenv("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{os.environ.get('PORT', 8080)}"
            urllib.request.urlopen(url, timeout=20).read()
        except Exception:
            pass


threading.Thread(target=self_ping, daemon=True).start()


def detect_token():
    tok_re = r"\d{6,}:[A-Za-z0-9_-]{30,}"
    env = {k: (v or "").strip().strip('"').strip("'") for k, v in os.environ.items()}
    for k in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN"):
        if re.fullmatch(tok_re, env.get(k, "")):
            return env[k]
    for v in env.values():
        if re.fullmatch(tok_re, v):
            return v
    return ""


TOKEN = detect_token()
if not TOKEN:
    while True:
        logging.error("BOT_TOKEN topilmadi! Render Environment'ga BOT_TOKEN ni kiriting.")
        time.sleep(30)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------------- Baza ----------------
def db():
    return closing(sqlite3.connect(DB_PATH))


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
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id, name, joined) VALUES (?, ?, ?)",
                  (user.id, user.full_name, datetime.now().strftime("%Y-%m-%d %H:%M")))
        c.commit()


def get_title(tid):
    with db() as c:
        return c.execute("SELECT id, category, name, description, poster, views FROM titles WHERE id = ?", (tid,)).fetchone()


def get_episodes(tid):
    with db() as c:
        return c.execute("SELECT id, ep_no, file_id, ftype FROM episodes WHERE title_id = ? ORDER BY ep_no", (tid,)).fetchall()


def get_episode(eid):
    with db() as c:
        return c.execute("SELECT id, title_id, ep_no, file_id, ftype FROM episodes WHERE id = ?", (eid,)).fetchone()


def add_episode(tid, file_id, ftype):
    with db() as c:
        n = c.execute("SELECT COALESCE(MAX(ep_no), 0) + 1 FROM episodes WHERE title_id = ?", (tid,)).fetchone()[0]
        c.execute("INSERT INTO episodes (title_id, ep_no, file_id, ftype) VALUES (?, ?, ?, ?)", (tid, n, file_id, ftype))
        c.commit()
    return n


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


# ---------------- Majburiy obuna ----------------
async def missing_channels(user_id):
    missing = []
    for ch in REQUIRED:
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ("left", "kicked"):
                missing.append(ch)
        except Exception as ex:
            logging.warning("obuna tekshirib bo'lmadi (%s): %s", ch, ex)
    return missing


async def gate(chat_id, user_id, payload=""):
    """True — o'tish mumkin. False — obuna xabari yuborildi."""
    if not REQUIRED or is_admin(user_id):
        return True
    missing = await missing_channels(user_id)
    if not missing:
        return True
    kb = [[btn(f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")] for ch in missing]
    kb.append([btn("✅ Tekshirish", f"chk:{payload}")])
    await bot.send_message(chat_id, "❗ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
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
    add_user(message.from_user)
    payload = (command.args or "").strip()
    if not await gate(message.chat.id, message.from_user.id, payload[:20]):
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
        text, kb = "😕 Hozircha hech narsa yo'q.", None
        if edit_message:
            return await edit_message.edit_text(text)
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
        except Exception:
            return
    await bot.send_message(chat_id, text, reply_markup=markup)


@dp.callback_query(F.data.startswith("list:"))
async def cb_list(callback: types.CallbackQuery):
    await callback.answer()
    _, kind, page = callback.data.split(":")
    if kind in ("anime", "kino", "pop"):
        await send_list(callback.message.chat.id, kind, int(page), edit_message=callback.message)


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
    caption = f"{icon} <b>{e(t[2])}</b>\n" + (f"\n{e(t[3])}\n" if t[3] else "") + f"\n📺 Qismlar: {len(eps)} ta\nQismni tanlang 👇"
    kb = episodes_kb(tid, eps)
    if t[4]:
        try:
            return await bot.send_photo(chat_id, t[4], caption=caption[:1024], reply_markup=kb)
        except Exception as ex:
            logging.warning("poster yuborilmadi: %s", ex)
    await bot.send_message(chat_id, caption, reply_markup=kb)


@dp.callback_query(F.data.startswith("t:"))
async def cb_title(callback: types.CallbackQuery):
    await callback.answer()
    if not await gate(callback.message.chat.id, callback.from_user.id, "t" + callback.data.split(":")[1]):
        return
    await show_title(callback.message.chat.id, int(callback.data.split(":")[1]))


@dp.callback_query(F.data.startswith("tp:"))
async def cb_title_page(callback: types.CallbackQuery):
    await callback.answer()
    _, tid, page = callback.data.split(":")
    eps = get_episodes(int(tid))
    if eps:
        try:
            await callback.message.edit_reply_markup(reply_markup=episodes_kb(int(tid), eps, int(page)))
        except Exception:
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
    nav.append(btn("📋 Qismlar", f"t:{tid}"))
    if idx < len(eps) - 1:
        nav.append(btn("Keyingi ➡️", f"ep:{eps[idx + 1][0]}"))
    name = t[2] if t else ""
    caption = f"🎬 <b>{e(name)}</b>" + (f"\n{no}-qism" if len(eps) > 1 else "") + "\n\n🍿 Yoqimli tomosha!"
    kb = InlineKeyboardMarkup(inline_keyboard=[nav])
    try:
        if ftype == "document":
            await bot.send_document(chat_id, file_id, caption=caption, reply_markup=kb, protect_content=PROTECT)
        else:
            await bot.send_video(chat_id, file_id, caption=caption, reply_markup=kb, protect_content=PROTECT, supports_streaming=True)
    except Exception as ex:
        logging.warning("episode yuborilmadi: %s", ex)
        await bot.send_message(chat_id, "❌ Videoni yuborib bo'lmadi (file_id yaroqsiz bo'lishi mumkin).")


@dp.callback_query(F.data.startswith("ep:"))
async def cb_episode(callback: types.CallbackQuery):
    await callback.answer()
    eid = int(callback.data.split(":")[1])
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
        return await message.answer(f"⛔ Siz admin emassiz. ID'ingiz: <code>{message.from_user.id}</code>\nRender'da ADMIN_ID ga shu raqamni yozing.")
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


def plain_text(m):
    return bool(m.text)


@dp.message(Add.name, plain_text)
async def add_name(message: types.Message, state: FSMContext):
    if message.text.startswith("/"):
        return
    await state.update_data(name=message.text.strip()[:150])
    await state.set_state(Add.desc)
    await message.answer("📝 Qisqa tavsif yuboring (yoki /skip):")


@dp.message(Add.desc, plain_text)
async def add_desc(message: types.Message, state: FSMContext):
    desc = "" if message.text.strip() == "/skip" else message.text.strip()[:700]
    await state.update_data(desc=desc)
    await state.set_state(Add.poster)
    await message.answer("🖼 Poster rasmini yuboring (yoki /skip):")


async def create_title(message, state, poster):
    data = await state.get_data()
    with db() as c:
        cur = c.execute("INSERT INTO titles (category, name, description, poster, created) VALUES (?, ?, ?, ?, ?)",
                        (data["category"], data["name"], data.get("desc", ""), poster, datetime.now().strftime("%Y-%m-%d %H:%M")))
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


@dp.message(Add.poster, plain_text)
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
    await message.answer(f"✅ Tayyor! {len(eps)} ta qism saqlandi.\n\n🔗 Havola:\n<code>{link}</code>", reply_markup=menu_kb())


@dp.message(Add.eps, F.video | F.document)
async def add_episode_msg(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data.get("title_id")
    if not tid:
        return await message.answer("⚠️ Avval /admin orqali yangi qo'shing.")
    if message.video:
        n = add_episode(tid, message.video.file_id, "video")
    else:
        n = add_episode(tid, message.document.file_id, "document")
    await message.answer(f"✅ {n}-qism saqlandi. Yana yuboring yoki /done")


@dp.message(Add.eps, plain_text)
async def add_eps_hint(message: types.Message):
    await message.answer("🎞 Video yuboring yoki tugatish uchun /done")


@dp.callback_query(F.data.startswith("adm:list:"))
async def adm_list(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    page = int(callback.data.split(":")[2])
    with db() as c:
        rows = c.execute("SELECT t.id, t.name, t.category, (SELECT COUNT(*) FROM episodes e WHERE e.title_id = t.id) FROM titles t ORDER BY t.id DESC").fetchall()
    if not rows:
        return await callback.message.answer("Hali hech narsa qo'shilmagan.")
    pages = (len(rows) - 1) // PAGE + 1
    page = max(0, min(page, pages - 1))
    kb = [[btn(f"{'🎌' if cat == 'anime' else '🎬'} {name[:30]} ({n})", f"adm:t:{tid}")] for tid, name, cat, n in rows[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"adm:list:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("➡️", f"adm:list:{page + 1}"))
    if nav:
        kb.append(nav)
    await callback.message.answer(f"📋 Ro'yxat ({len(rows)} ta):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("adm:t:"))
async def adm_title(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    tid = int(callback.data.split(":")[2])
    t = get_title(tid)
    if not t:
        return await callback.message.answer("Topilmadi.")
    n = len(get_episodes(tid))
    link = f"https://t.me/{await bot_username()}?start=t{tid}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Qism qo'shish", f"adm:eps:{tid}"), btn("🗑 O'chirish", f"adm:del:{tid}")],
        [btn("👁 Ko'rish", f"t:{tid}")],
    ])
    await callback.message.answer(f"<b>{e(t[2])}</b>\n📺 Qismlar: {n} · 👁 {t[5]}\n🔗 <code>{link}</code>", reply_markup=kb)


@dp.callback_query(F.data.startswith("adm:eps:"))
async def adm_more_eps(callback: types.CallbackQuery, state: FSMContext):
    if not await admin_only(callback):
        return
    await callback.answer()
    tid = int(callback.data.split(":")[2])
    if not get_title(tid):
        return await callback.message.answer("Topilmadi.")
    await state.clear()
    await state.update_data(title_id=tid)
    await state.set_state(Add.eps)
    await callback.message.answer("🎞 Qo'shiladigan videolarni yuboring. Tugagach: /done")


@dp.callback_query(F.data.startswith("adm:del:"))
async def adm_del(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer()
    tid = int(callback.data.split(":")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Ha, o'chir", f"adm:delok:{tid}"), btn("↩️ Yo'q", "noop")]])
    await callback.message.answer("⚠️ Rostdan ham o'chirilsinmi? (barcha qismlari bilan)", reply_markup=kb)


@dp.callback_query(F.data.startswith("adm:delok:"))
async def adm_delok(callback: types.CallbackQuery):
    if not await admin_only(callback):
        return
    await callback.answer("O'chirildi")
    tid = int(callback.data.split(":")[2])
    with db() as c:
        c.execute("DELETE FROM episodes WHERE title_id = ?", (tid,))
        c.execute("DELETE FROM titles WHERE id = ?", (tid,))
        c.commit()
    try:
        await callback.message.edit_text("🗑 O'chirildi.")
    except Exception:
        pass


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
    lines = ["📊 <b>Statistika</b>\n", f"👥 Foydalanuvchilar: {users}", f"🎞 Nomlar: {titles}", f"📺 Qismlar: {eps}", f"👁 Ko'rishlar: {views}"]
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
    await callback.message.answer("📢 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring (matn, rasm, video...).\nBekor qilish: /cancel")


@dp.message(Bc.msg)
async def bc_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        return
    await state.clear()
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT user_id FROM users")]
    status = await message.answer(f"⏳ Yuborilmoqda... ({len(ids)} ta)")
    ok = bad = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, message.chat.id, message.message_id)
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Yuborildi: {ok}\n❌ Yetmadi (bloklagan): {bad}")


# ---------------- Zaxira nusxa ----------------
restore_wait = set()


def make_backup_file():
    path = os.path.join(tempfile.gettempdir(), f"anime_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db")
    with db() as src, closing(sqlite3.connect(path)) as dst:
        src.backup(dst)
    return path


def restore_db_file(path):
    try:
        with closing(sqlite3.connect(path)) as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return False
    if not {"titles", "episodes"} <= names:
        return False
    shutil.copyfile(path, DB_PATH + ".new")
    os.replace(DB_PATH + ".new", DB_PATH)
    init_db()
    return True


async def send_backup(chat_id, caption):
    path = make_backup_file()
    try:
        await bot.send_document(chat_id, FSInputFile(path), caption=caption)
    finally:
        os.remove(path)


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
        await message.answer("📥 Endi avval olingan .db faylni yuboring. ⚠️ Hozirgi baza almashtiriladi!")


@dp.message(StateFilter(None), F.document)
async def on_restore_document(message: types.Message):
    uid = message.from_user.id
    if uid not in restore_wait or not is_admin(uid):
        return
    restore_wait.discard(uid)
    tmp = os.path.join(tempfile.gettempdir(), f"restore_{uid}.db")
    try:
        await bot.download(message.document.file_id, destination=tmp)
        ok = restore_db_file(tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    await message.answer("✅ Baza tiklandi!" if ok else "❌ Bu fayl mos emas (titles/episodes jadvallari yo'q).")


async def backup_worker():
    await asyncio.sleep(90)
    while True:
        try:
            if ADMIN_IDS:
                await send_backup(sorted(ADMIN_IDS)[0], "💾 Avtomatik zaxira nusxa")
        except Exception as ex:
            logging.warning("auto-backup xatosi: %s", ex)
        await asyncio.sleep(max(BACKUP_HOURS, 0.1) * 3600)


# ---------------- Qidiruv (eng oxirida) ----------------
@dp.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_search(message: types.Message):
    add_user(message.from_user)
    if not await gate(message.chat.id, message.from_user.id):
        return
    q = message.text.strip().lower()
    if len(q) < 2:
        return await message.answer("🔎 Kamida 2 ta harf yozing.")
    with db() as c:
        rows = c.execute("SELECT id, name, category FROM titles").fetchall()
    found = [(tid, name, cat) for tid, name, cat in rows if q in name.lower()][:20]
    if not found:
        return await message.answer("😕 Hech narsa topilmadi. Boshqa nom bilan urinib ko'ring.")
    kb = [[btn(f"{'🎌' if cat == 'anime' else '🎬'} {name[:40]}", f"t:{tid}")] for tid, name, cat in found]
    await message.answer(f"🔎 Topildi: {len(found)} ta", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


async def main():
    asyncio.create_task(start_web_server())
    asyncio.create_task(backup_worker())
    while True:
        try:
            print("Bot polling boshlandi...")
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, drop_pending_updates=True)
        except Exception as ex:
            logging.error(f"Polling xatoligi: {ex}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
