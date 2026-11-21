import asyncio
import os
import sqlite3
import time
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

REF_DAYS = 1        # дней VIP за засчитанного друга
REF_MIN_MSGS = 5    # сообщений друга для засчёта реферала
REF_DAILY_CAP = 5   # макс. засчитанных рефералов в сутки
REF_MAX = 50        # макс. засчитанных рефералов всего
AUTOBAN_AT = 5      # автобан после стольких жалоб от разных людей
PAGE = 10           # фото на страницу
USERS_PER_PAGE = 8  # пользователей на страницу в админ-панели

bot = Bot(TOKEN)
dp = Dispatcher()

# ---------------- БАЗА ----------------
db = sqlite3.connect(os.getenv("DB_PATH", "bot.db"))
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    gender TEXT,
    vip_until INTEGER DEFAULT 0,
    referrer INTEGER,
    refs INTEGER DEFAULT 0,
    banned INTEGER DEFAULT 0,
    msgs INTEGER DEFAULT 0,
    ref_paid INTEGER DEFAULT 0,
    username TEXT,
    name TEXT,
    created INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    file_id TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter INTEGER,
    target INTEGER,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ref_rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer INTEGER,
    invited INTEGER UNIQUE,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
"""
)
# миграция для старых баз
for _col in (
    "msgs INTEGER DEFAULT 0",
    "ref_paid INTEGER DEFAULT 0",
    "username TEXT",
    "name TEXT",
    "created INTEGER DEFAULT 0",
):
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {_col}")
    except sqlite3.OperationalError:
        pass  # колонка уже есть
db.commit()


def ensure_user(uid: int) -> bool:
    cur = db.execute(
        "INSERT OR IGNORE INTO users (id, created) VALUES (?, ?)",
        (uid, int(time.time())),
    )
    db.commit()
    return cur.rowcount == 1


def touch(u):
    """Обновляет имя и username (только если пользователь уже есть в базе)."""
    db.execute(
        "UPDATE users SET username=?, name=? WHERE id=?",
        (u.username, u.full_name, u.id),
    )
    db.commit()


def user_exists(uid: int) -> bool:
    return db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None


def get_gender(uid: int):
    row = db.execute("SELECT gender FROM users WHERE id=?", (uid,)).fetchone()
    return row[0] if row else None


def is_banned(uid: int) -> bool:
    row = db.execute("SELECT banned FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row[0])


def vip_until(uid: int) -> int:
    row = db.execute("SELECT vip_until FROM users WHERE id=?", (uid,)).fetchone()
    return row[0] if row else 0


def is_vip(uid: int) -> bool:
    return vip_until(uid) > time.time()


def add_vip(uid: int, days: int):
    base = max(int(time.time()), vip_until(uid))
    db.execute("UPDATE users SET vip_until=? WHERE id=?", (base + days * 86400, uid))
    db.commit()


# ---------------- СОСТОЯНИЕ ----------------
waiting: dict[int, str | None] = {}  # uid -> желаемый пол (None = любой)
pairs: dict[int, int] = {}
admin_wait: set[int] = set()  # админ ждёт ввода ID

PRIVATE = F.chat.type == "private"
ADMIN = F.from_user.id == ADMIN_ID

# ---------------- КНОПКИ ----------------
BTN_SEARCH = "🔎 Поиск"
BTN_NEXT = "⏭ Следующий"
BTN_STOP = "⛔ Стоп"
BTN_SEARCH_M = "👨 Искать парня 💎"
BTN_SEARCH_F = "👩 Искать девушку 💎"
BTN_VIP = "💎 VIP"
BTN_REF = "👥 Пригласить друга"
BTN_REPORT = "🚩 Пожаловаться"
BTN_HELP = "ℹ️ Помощь"
BTN_ADMIN = "🛠 Админ-панель"


def main_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_NEXT), KeyboardButton(text=BTN_STOP)],
        [KeyboardButton(text=BTN_SEARCH_M), KeyboardButton(text=BTN_SEARCH_F)],
        [KeyboardButton(text=BTN_VIP), KeyboardButton(text=BTN_REF)],
        [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_HELP)],
    ]
    if uid == ADMIN_ID:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


HELP = (
    "Анонимный чат 🎭\n\n"
    "Пользуйся кнопками внизу или командами:\n"
    "/search — найти собеседника\n"
    "/next — следующий\n"
    "/stop — завершить диалог\n"
    "/report — пожаловаться на собеседника\n"
    "/vip — статус VIP\n"
    "/ref — пригласить друга\n\n"
    "⚠️ Для модерации администратор может видеть ID и имя аккаунта, "
    "а также фото, отправленные в чат. Собеседнику твои данные не показываются."
)


# ---------------- MIDDLEWARE ----------------
@dp.message.outer_middleware()
async def pre(handler, event, data):
    u = event.from_user
    if u:
        if u.id != ADMIN_ID and is_banned(u.id):
            return  # забаненным бот не отвечает
        touch(u)
    return await handler(event, data)


async def leave(uid: int, notify: bool = True):
    waiting.pop(uid, None)
    partner = pairs.pop(uid, None)
    if partner:
        pairs.pop(partner, None)
        if notify:
            await bot.send_message(
                partner, "Собеседник вышел. Нажми «🔎 Поиск», чтобы найти нового."
            )


# ---------------- СТАРТ / ПОЛ ----------------
@dp.message(CommandStart(), PRIVATE)
async def start(m: Message, command: CommandObject):
    uid = m.from_user.id
    is_new = ensure_user(uid)
    touch(m.from_user)

    arg = command.args or ""
    if is_new and arg.startswith("ref_") and arg[4:].isdigit():
        ref = int(arg[4:])
        if ref != uid and user_exists(ref):
            # награда придёт позже, см. count_message()
            db.execute("UPDATE users SET referrer=? WHERE id=?", (ref, uid))
            db.commit()

    if not get_gender(uid):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="👨 Парень", callback_data="g:m"),
                    InlineKeyboardButton(text="👩 Девушка", callback_data="g:f"),
                ]
            ]
        )
        await m.answer("Выбери свой пол:", reply_markup=kb)
        return
    await m.answer(HELP, reply_markup=main_kb(uid))


@dp.callback_query(F.data.startswith("g:"))
async def set_gender(c: CallbackQuery):
    uid = c.from_user.id
    if is_banned(uid):
        await c.answer()
        return
    ensure_user(uid)
    touch(c.from_user)
    db.execute("UPDATE users SET gender=? WHERE id=?", (c.data[2], uid))
    db.commit()
    await c.message.edit_text("Пол сохранён ✅")
    await c.message.answer(HELP, reply_markup=main_kb(uid))
    await c.answer()


@dp.message(F.text == BTN_HELP, PRIVATE)
@dp.message(Command("help"), PRIVATE)
async def help_cmd(m: Message):
    await m.answer(HELP, reply_markup=main_kb(m.from_user.id))


# ---------------- РЕФЕРАЛКА / VIP ----------------
@dp.message(F.text == BTN_REF, PRIVATE)
@dp.message(Command("ref"), PRIVATE)
async def ref(m: Message):
    me = await bot.me()
    n = db.execute("SELECT refs FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    link = f"https://t.me/{me.username}?start=ref_{m.from_user.id}"
    await m.answer(
        f"Твоя ссылка:\n{link}\n\n"
        f"За каждого нового друга — {REF_DAYS} дн. VIP.\n"
        f"Друг должен выбрать пол и отправить {REF_MIN_MSGS} сообщений "
        f"в чате с незнакомым собеседником.\n"
        f"Засчитано: {n[0] if n else 0}"
    )


@dp.message(F.text == BTN_VIP, PRIVATE)
@dp.message(Command("vip"), PRIVATE)
async def vip(m: Message):
    until = vip_until(m.from_user.id)
    if until > time.time():
        left = int((until - time.time()) // 86400) + 1
        text = f"💎 VIP активен, осталось примерно {left} дн."
    else:
        text = "У тебя нет VIP."
    await m.answer(
        text + "\n\nVIP даёт поиск по полу — кнопки «👨 Искать парня» "
        "и «👩 Искать девушку».\n"
        "Получить VIP: приглашай друзей, кнопка «👥 Пригласить друга»."
    )


# ---------------- ПОИСК ----------------
async def find(m: Message, pref):
    uid = m.from_user.id
    ensure_user(uid)
    me = get_gender(uid)
    if not me:
        await m.answer("Сначала выбери пол: /start")
        return
    if pref and not is_vip(uid):
        await m.answer("💎 Поиск по полу доступен только с VIP. Нажми «💎 VIP».")
        return

    await leave(uid)

    for w, wpref in list(waiting.items()):
        if w == uid:
            continue
        if pref and get_gender(w) != pref:
            continue
        if wpref and me != wpref:
            continue
        del waiting[w]
        pairs[uid], pairs[w] = w, uid
        await m.answer("✅ Собеседник найден. Пиши!")
        await bot.send_message(w, "✅ Собеседник найден. Пиши!")
        return

    waiting[uid] = pref
    await m.answer("🔎 Ищу собеседника...")


@dp.message(F.text.in_({BTN_SEARCH, BTN_NEXT}), PRIVATE)
@dp.message(Command("search", "next"), PRIVATE)
async def h_search(m: Message):
    await find(m, None)


@dp.message(F.text == BTN_SEARCH_M, PRIVATE)
@dp.message(Command("search_m"), PRIVATE)
async def h_search_m(m: Message):
    await find(m, "m")


@dp.message(F.text == BTN_SEARCH_F, PRIVATE)
@dp.message(Command("search_f"), PRIVATE)
async def h_search_f(m: Message):
    await find(m, "f")


@dp.message(F.text == BTN_STOP, PRIVATE)
@dp.message(Command("stop"), PRIVATE)
async def stop(m: Message):
    await leave(m.from_user.id)
    await m.answer("Диалог завершён. Нажми «🔎 Поиск», чтобы найти нового.")


# ---------------- ЖАЛОБЫ / БАН ----------------
@dp.message(F.text == BTN_REPORT, PRIVATE)
@dp.message(Command("report"), PRIVATE)
async def report(m: Message):
    uid = m.from_user.id
    target = pairs.get(uid)
    if not target:
        await m.answer("Ты не в диалоге.")
        return
    db.execute("INSERT INTO reports (reporter, target) VALUES (?, ?)", (uid, target))
    db.commit()
    n = db.execute(
        "SELECT COUNT(DISTINCT reporter) FROM reports WHERE target=?", (target,)
    ).fetchone()[0]
    await leave(uid)
    await m.answer("Жалоба отправлена, диалог завершён. Спасибо!")
    if ADMIN_ID:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="👤 Открыть профиль", callback_data=f"adm:u:{target}")]
            ]
        )
        await bot.send_message(
            ADMIN_ID,
            f"🚩 Жалоба на {target} (от разных людей: {n})",
            reply_markup=kb,
        )
    if n >= AUTOBAN_AT:
        await do_ban(target)


async def do_ban(uid: int):
    ensure_user(uid)
    row = db.execute(
        "SELECT referrer, ref_paid FROM users WHERE id=?", (uid,)
    ).fetchone()
    if row and row[0] and row[1] == 1:
        # награда за забаненного реферала отзывается
        db.execute(
            "UPDATE users SET vip_until=MAX(0, vip_until-?) WHERE id=?",
            (REF_DAYS * 86400, row[0]),
        )
        db.execute("UPDATE users SET ref_paid=3 WHERE id=?", (uid,))
    db.execute("UPDATE users SET banned=1 WHERE id=?", (uid,))
    db.commit()
    await leave(uid)


# ---------------- АДМИН: ВСПОМОГАТЕЛЬНОЕ ----------------
def stats_text() -> str:
    q = lambda s, a=(): db.execute(s, a).fetchone()[0]
    now = int(time.time())
    return (
        "📊 Статистика\n\n"
        f"Пользователей: {q('SELECT COUNT(*) FROM users')}\n"
        f"VIP сейчас: {q('SELECT COUNT(*) FROM users WHERE vip_until>?', (now,))}\n"
        f"Забанено: {q('SELECT COUNT(*) FROM users WHERE banned=1')}\n"
        f"Фото: {q('SELECT COUNT(*) FROM photos')}\n"
        f"Жалоб: {q('SELECT COUNT(*) FROM reports')}\n"
        f"В диалогах: {len(pairs)} | в поиске: {len(waiting)}"
    )


def refs_text(uid: int) -> str:
    rows = db.execute(
        "SELECT id, msgs, ref_paid, banned FROM users WHERE referrer=?", (uid,)
    ).fetchall()
    if not rows:
        return "Рефералов нет."
    status = {0: "ожидает", 1: "засчитан", 2: "не засчитан", 3: "отозван"}
    return "\n".join(
        f"{rid} | сообщений: {msgs} | {status.get(paid, '?')}{' | БАН' if banned else ''}"
        for rid, msgs, paid, banned in rows[:50]
    )


def profile(uid: int):
    """Возвращает (текст, клавиатура) или None, если пользователя нет."""
    row = db.execute(
        "SELECT gender, vip_until, referrer, refs, banned, msgs, username, name, created "
        "FROM users WHERE id=?",
        (uid,),
    ).fetchone()
    if not row:
        return None
    gender, vip_ts, referrer, refs, banned, msgs, username, name, created = row
    q = lambda s: db.execute(s, (uid,)).fetchone()[0]
    reports_on = q("SELECT COUNT(DISTINCT reporter) FROM reports WHERE target=?")
    photos_n = q("SELECT COUNT(*) FROM photos WHERE user_id=?")
    invited = q("SELECT COUNT(*) FROM users WHERE referrer=?")
    gl = {"m": "👨 парень", "f": "👩 девушка"}.get(gender, "не выбран")
    vip_text = (
        "до " + datetime.fromtimestamp(vip_ts).strftime("%d.%m.%Y")
        if vip_ts > time.time()
        else "нет"
    )
    reg = datetime.fromtimestamp(created).strftime("%d.%m.%Y %H:%M") if created else "—"
    state = (
        "в диалоге" if uid in pairs else "в поиске" if uid in waiting else "не в диалоге"
    )
    text = (
        "👤 Профиль\n\n"
        f"ID: {uid}\n"
        f"Имя: {name or '—'}\n"
        f"Username: {'@' + username if username else '—'}\n"
        f"Пол: {gl}\n"
        f"VIP: {vip_text}\n"
        f"Статус: {'🚫 забанен' if banned else 'активен'} ({state})\n"
        f"Регистрация: {reg}\n"
        f"Сообщений: {msgs}\n"
        f"Фото: {photos_n}\n"
        f"Жалоб на него: {reports_on}\n"
        f"Пригласил: {referrer or '—'}\n"
        f"Приглашено им: {invited} (засчитано: {refs})"
    )
    ban_btn = (
        InlineKeyboardButton(text="✅ Разбанить", callback_data=f"adm:unban:{uid}")
        if banned
        else InlineKeyboardButton(text="🚫 Забанить", callback_data=f"adm:ban:{uid}")
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [ban_btn, InlineKeyboardButton(text="💎 +7 дн.", callback_data=f"adm:vip:{uid}:7")],
            [
                InlineKeyboardButton(text="🖼 Фото", callback_data=f"adm:uphotos:{uid}"),
                InlineKeyboardButton(text="👥 Приглашённые", callback_data=f"adm:refs:{uid}"),
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")],
        ]
    )
    return text, kb


def adm_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
                InlineKeyboardButton(text="🖼 Все фото", callback_data="adm:photos:1"),
            ],
            [
                InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users:1"),
                InlineKeyboardButton(text="🚩 Жалобы", callback_data="adm:reports"),
            ],
            [InlineKeyboardButton(text="🔍 Найти по ID", callback_data="adm:find")],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")]]
    )


def users_page(page: int):
    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    rows = db.execute(
        "SELECT id, name, gender, banned, vip_until FROM users "
        "ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
        (USERS_PER_PAGE, (page - 1) * USERS_PER_PAGE),
    ).fetchall()
    now = time.time()
    buttons = []
    for uid, name, g, banned, vip_ts in rows:
        gl = {"m": "👨", "f": "👩"}.get(g, "❔")
        mark = "🚫" if banned else ("💎" if vip_ts > now else "")
        label = f"{gl}{mark} {uid} {name or ''}"[:60]
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"adm:u:{uid}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:users:{page - 1}"))
    if page * USERS_PER_PAGE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:users:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")])
    return f"👥 Пользователи: {total}, стр. {page}", InlineKeyboardMarkup(inline_keyboard=buttons)


def reports_page():
    rows = db.execute(
        "SELECT target, COUNT(DISTINCT reporter) c FROM reports "
        "GROUP BY target ORDER BY c DESC LIMIT 10"
    ).fetchall()
    buttons = [
        [InlineKeyboardButton(text=f"{t} — жалоб: {c}", callback_data=f"adm:u:{t}")]
        for t, c in rows
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="adm:menu")])
    text = "🚩 Больше всего жалоб:" if rows else "Жалоб пока нет."
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_photos(chat_id: int, page: int, user_id: int | None = None):
    where = "WHERE user_id=?" if user_id else ""
    args = (user_id,) if user_id else ()
    total = db.execute(f"SELECT COUNT(*) FROM photos {where}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT id, user_id, file_id FROM photos {where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        args + (PAGE, (page - 1) * PAGE),
    ).fetchall()
    if not rows:
        await bot.send_message(chat_id, f"Фото нет. Всего: {total}")
        return
    media = [
        InputMediaPhoto(media=fid, caption=f"#{pid} | user {uid}") for pid, uid, fid in rows
    ]
    await bot.send_media_group(chat_id, media)
    pages = (total + PAGE - 1) // PAGE
    kb = None
    if page < pages:
        cb = f"adm:uphotos:{user_id}:{page + 1}" if user_id else f"adm:photos:{page + 1}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➡️ Дальше", callback_data=cb)]]
        )
    await bot.send_message(chat_id, f"Страница {page}/{pages}, всего: {total}", reply_markup=kb)


async def edit(msg: Message, text: str, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except Exception:
        pass  # например, текст не изменился


async def show_profile(msg: Message, uid: int):
    p = profile(uid)
    if not p:
        await edit(msg, "Пользователь не найден.", back_kb())
    else:
        await edit(msg, p[0], p[1])


# ---------------- АДМИН: ПАНЕЛЬ ----------------
@dp.message(F.text == BTN_ADMIN, PRIVATE, ADMIN)
@dp.message(Command("admin"), PRIVATE, ADMIN)
async def admin_panel(m: Message):
    await m.answer("🛠 Админ-панель", reply_markup=adm_menu_kb())


@dp.callback_query(F.data.startswith("adm:"), F.from_user.id == ADMIN_ID)
async def adm_cb(c: CallbackQuery):
    p = c.data.split(":")
    act, msg = p[1], c.message
    await c.answer()
    if act == "menu":
        await edit(msg, "🛠 Админ-панель", adm_menu_kb())
    elif act == "stats":
        await edit(msg, stats_text(), back_kb())
    elif act == "users":
        text, kb = users_page(int(p[2]))
        await edit(msg, text, kb)
    elif act == "reports":
        text, kb = reports_page()
        await edit(msg, text, kb)
    elif act == "find":
        admin_wait.add(c.from_user.id)
        await edit(msg, "🔍 Пришли числовой ID пользователя.", back_kb())
    elif act == "photos":
        await send_photos(msg.chat.id, int(p[2]))
    elif act == "u":
        await show_profile(msg, int(p[2]))
    elif act == "ban":
        await do_ban(int(p[2]))
        await show_profile(msg, int(p[2]))
    elif act == "unban":
        db.execute("UPDATE users SET banned=0 WHERE id=?", (int(p[2]),))
        db.commit()
        await show_profile(msg, int(p[2]))
    elif act == "vip":
        add_vip(int(p[2]), int(p[3]))
        await show_profile(msg, int(p[2]))
    elif act == "uphotos":
        await send_photos(msg.chat.id, int(p[3]) if len(p) > 3 else 1, user_id=int(p[2]))
    elif act == "refs":
        await msg.answer(refs_text(int(p[2])))


@dp.message(Command("user"), PRIVATE, ADMIN)
async def user_cmd(m: Message, command: CommandObject):
    if not (command.args and command.args.strip().isdigit()):
        await m.answer("Использование: /user <id>")
        return
    p = profile(int(command.args.strip()))
    if not p:
        await m.answer("Пользователь не найден.")
        return
    await m.answer(p[0], reply_markup=p[1])


@dp.message(PRIVATE, ADMIN, lambda m: m.from_user.id in admin_wait)
async def admin_find(m: Message):
    admin_wait.discard(m.from_user.id)
    if not (m.text and m.text.strip().isdigit()):
        await m.answer("Нужен числовой ID.", reply_markup=adm_menu_kb())
        return
    p = profile(int(m.text.strip()))
    if not p:
        await m.answer("Пользователь не найден.", reply_markup=adm_menu_kb())
        return
    await m.answer(p[0], reply_markup=p[1])


@dp.message(Command("ban"), PRIVATE, ADMIN)
async def ban(m: Message, command: CommandObject):
    if not (command.args and command.args.isdigit()):
        await m.answer("Использование: /ban <id>")
        return
    await do_ban(int(command.args))
    await m.answer("Забанен.")


@dp.message(Command("unban"), PRIVATE, ADMIN)
async def unban(m: Message, command: CommandObject):
    if not (command.args and command.args.isdigit()):
        await m.answer("Использование: /unban <id>")
        return
    db.execute("UPDATE users SET banned=0 WHERE id=?", (int(command.args),))
    db.commit()
    await m.answer("Разбанен.")


@dp.message(Command("givevip"), PRIVATE, ADMIN)
async def givevip(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 2 or not all(x.isdigit() for x in parts):
        await m.answer("Использование: /givevip <id> <дни>")
        return
    ensure_user(int(parts[0]))
    add_vip(int(parts[0]), int(parts[1]))
    await m.answer("VIP выдан.")


@dp.message(Command("stats"), PRIVATE, ADMIN)
async def stats(m: Message):
    await m.answer(stats_text())


@dp.message(Command("photos"), PRIVATE, ADMIN)
async def photos(m: Message, command: CommandObject):
    page = int(command.args) if command.args and command.args.isdigit() else 1
    await send_photos(m.chat.id, page)


@dp.message(Command("refs"), PRIVATE, ADMIN)
async def refs(m: Message, command: CommandObject):
    if not (command.args and command.args.isdigit()):
        await m.answer("Использование: /refs <id пригласившего>")
        return
    await m.answer(refs_text(int(command.args)))


# ---------------- ПЕРЕСЫЛКА ----------------
async def count_message(uid: int, partner: int):
    """Засчитывает реферала, когда друг реально начал общаться."""
    db.execute("UPDATE users SET msgs=msgs+1 WHERE id=?", (uid,))
    db.commit()
    row = db.execute(
        "SELECT referrer, ref_paid, msgs, gender FROM users WHERE id=?", (uid,)
    ).fetchone()
    if not row:
        return
    referrer, paid, msgs, gender = row
    if not referrer or paid or msgs < REF_MIN_MSGS or not gender:
        return
    if partner == referrer:  # сообщения самому пригласившему не считаются
        return
    if is_banned(referrer):
        db.execute("UPDATE users SET ref_paid=2 WHERE id=?", (uid,))
        db.commit()
        return
    total = db.execute("SELECT refs FROM users WHERE id=?", (referrer,)).fetchone()[0]
    if total >= REF_MAX:
        db.execute("UPDATE users SET ref_paid=2 WHERE id=?", (uid,))
        db.commit()
        return
    today = db.execute(
        "SELECT COUNT(*) FROM ref_rewards "
        "WHERE referrer=? AND ts > datetime('now','-1 day')",
        (referrer,),
    ).fetchone()[0]
    if today >= REF_DAILY_CAP:
        return  # останется в ожидании, засчитается позже
    db.execute("UPDATE users SET ref_paid=1 WHERE id=?", (uid,))
    db.execute(
        "INSERT OR IGNORE INTO ref_rewards (referrer, invited) VALUES (?, ?)",
        (referrer, uid),
    )
    db.execute("UPDATE users SET refs=refs+1 WHERE id=?", (referrer,))
    db.commit()
    add_vip(referrer, REF_DAYS)
    try:
        await bot.send_message(
            referrer, f"🎉 Друг начал общаться по твоей ссылке! +{REF_DAYS} дн. VIP."
        )
    except Exception:
        pass


@dp.message(PRIVATE)
async def relay(m: Message):
    if m.text and m.text.startswith("/"):
        await m.answer("Неизвестная команда.")
        return
    partner = pairs.get(m.from_user.id)
    if not partner:
        await m.answer("Ты не в диалоге. Нажми «🔎 Поиск».")
        return
    if m.photo:
        db.execute(
            "INSERT INTO photos (user_id, file_id) VALUES (?, ?)",
            (m.from_user.id, m.photo[-1].file_id),
        )
        db.commit()
    try:
        await m.copy_to(partner)  # без пометки "переслано" — анонимно
    except Exception:
        await m.answer("Не удалось доставить сообщение.")
        return
    await count_message(m.from_user.id, partner)


# ---------------- ЗАПУСК ----------------
async def health(request):
    return web.Response(text="ok")


async def main():
    port = int(os.getenv("PORT", "0"))
    if port:  # на Render PORT задаётся автоматически
        app = web.Application()
        app.router.add_get("/", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
