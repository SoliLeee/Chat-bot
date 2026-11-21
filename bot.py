import asyncio
import os
import sqlite3
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

REF_DAYS = 1       # сколько дней VIP получает пригласивший за каждого друга
REF_MIN_MSGS = 5   # сколько сообщений друг должен отправить в чате, чтобы реферал засчитался
REF_DAILY_CAP = 5  # максимум засчитанных рефералов в сутки на одного пригласившего
REF_MAX = 50       # максимум засчитанных рефералов за всё время
AUTOBAN_AT = 5     # авто-бан после стольких жалоб от разных людей
PAGE = 10

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
    banned INTEGER DEFAULT 0
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
"""
)
for _col in ("msgs INTEGER DEFAULT 0", "ref_paid INTEGER DEFAULT 0"):
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {_col}")
    except sqlite3.OperationalError:
        pass  # колонка уже есть
db.execute(
    "CREATE TABLE IF NOT EXISTS ref_rewards ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, referrer INTEGER, "
    "invited INTEGER UNIQUE, ts TEXT DEFAULT CURRENT_TIMESTAMP)"
)
db.commit()


def ensure_user(uid: int) -> bool:
    cur = db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (uid,))
    db.commit()
    return cur.rowcount == 1


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
    db.execute(
        "UPDATE users SET vip_until=? WHERE id=?", (base + days * 86400, uid)
    )
    db.commit()


# ---------------- СОСТОЯНИЕ ----------------
waiting: dict[int, str | None] = {}  # uid -> желаемый пол (None = любой)
pairs: dict[int, int] = {}

PRIVATE = F.chat.type == "private"
ADMIN = F.from_user.id == ADMIN_ID


@dp.message.outer_middleware()
async def ban_filter(handler, event, data):
    u = event.from_user
    if u and u.id != ADMIN_ID and is_banned(u.id):
        return
    return await handler(event, data)


async def leave(uid: int, notify: bool = True):
    waiting.pop(uid, None)
    partner = pairs.pop(uid, None)
    if partner:
        pairs.pop(partner, None)
        if notify:
            await bot.send_message(
                partner, "Собеседник вышел. /search — искать снова."
            )


# ---------------- СТАРТ / ПОЛ / РЕФЕРАЛКА ----------------
@dp.message(CommandStart(), PRIVATE)
async def start(m: Message, command: CommandObject):
    uid = m.from_user.id
    is_new = ensure_user(uid)

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
    await m.answer(HELP)


HELP = (
    "Анонимный чат.\n\n"
    "/search — любой собеседник\n"
    "/search_m — искать парня (VIP)\n"
    "/search_f — искать девушку (VIP)\n"
    "/next — следующий\n"
    "/stop — завершить диалог\n"
    "/report — пожаловаться на собеседника\n"
    "/vip — статус VIP\n"
    "/ref — реферальная ссылка\n\n"
    "⚠️ Фото, отправленные в чат, сохраняются и могут просматриваться "
    "администратором для модерации."
)


@dp.callback_query(F.data.startswith("g:"))
async def set_gender(c: CallbackQuery):
    ensure_user(c.from_user.id)
    db.execute(
        "UPDATE users SET gender=? WHERE id=?", (c.data[2], c.from_user.id)
    )
    db.commit()
    await c.message.edit_text("Пол сохранён ✅")
    await c.message.answer(HELP)
    await c.answer()


@dp.message(Command("ref"), PRIVATE)
async def ref(m: Message):
    me = await bot.me()
    n = db.execute(
        "SELECT refs FROM users WHERE id=?", (m.from_user.id,)
    ).fetchone()
    link = f"https://t.me/{me.username}?start=ref_{m.from_user.id}"
    await m.answer(
        f"Твоя ссылка:\n{link}\n\n"
        f"За каждого нового друга — {REF_DAYS} дн. VIP.\n"
        f"Друг должен выбрать пол и отправить {REF_MIN_MSGS} сообщений "
        f"в чате с незнакомым собеседником.\n"
        f"Засчитано: {n[0] if n else 0}"
    )


@dp.message(Command("vip"), PRIVATE)
async def vip(m: Message):
    until = vip_until(m.from_user.id)
    if until > time.time():
        left = int((until - time.time()) // 86400) + 1
        text = f"💎 VIP активен, осталось примерно {left} дн."
    else:
        text = "У тебя нет VIP."
    await m.answer(
        text + "\n\nVIP даёт поиск по полу (/search_m, /search_f).\n"
        "Получить: приглашай друзей (/ref)."
    )


# ---------------- ПОИСК ----------------
@dp.message(Command("search", "next", "search_m", "search_f"), PRIVATE)
async def search(m: Message, command: CommandObject):
    uid = m.from_user.id
    ensure_user(uid)
    me = get_gender(uid)
    if not me:
        await m.answer("Сначала выбери пол: /start")
        return

    pref = {"search_m": "m", "search_f": "f"}.get(command.command)
    if pref and not is_vip(uid):
        await m.answer("💎 Поиск по полу доступен только с VIP. /vip")
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


@dp.message(Command("stop"), PRIVATE)
async def stop(m: Message):
    await leave(m.from_user.id)
    await m.answer("Диалог завершён. /search — искать снова.")


# ---------------- ЖАЛОБЫ ----------------
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
        await bot.send_message(
            ADMIN_ID, f"🚩 Жалоба на {target} (всего от разных людей: {n})"
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


# ---------------- АДМИН ----------------
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
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await m.answer("Использование: /givevip <id> <дни>")
        return
    ensure_user(int(parts[0]))
    add_vip(int(parts[0]), int(parts[1]))
    await m.answer("VIP выдан.")


@dp.message(Command("refs"), PRIVATE, ADMIN)
async def refs(m: Message, command: CommandObject):
    if not (command.args and command.args.isdigit()):
        await m.answer("Использование: /refs <id пригласившего>")
        return
    rows = db.execute(
        "SELECT id, msgs, ref_paid, banned FROM users WHERE referrer=?",
        (int(command.args),),
    ).fetchall()
    if not rows:
        await m.answer("Рефералов нет.")
        return
    status = {0: "ожидает", 1: "засчитан", 2: "не засчитан", 3: "отозван"}
    lines = [
        f"{uid} | сообщений: {msgs} | {status.get(paid, '?')}"
        f"{' | БАН' if banned else ''}"
        for uid, msgs, paid, banned in rows[:50]
    ]
    await m.answer("\n".join(lines))


@dp.message(Command("stats"), PRIVATE, ADMIN)
async def stats(m: Message):
    q = lambda s: db.execute(s).fetchone()[0]
    await m.answer(
        f"Пользователей: {q('SELECT COUNT(*) FROM users')}\n"
        f"VIP сейчас: {q(f'SELECT COUNT(*) FROM users WHERE vip_until>{int(time.time())}')}\n"
        f"Забанено: {q('SELECT COUNT(*) FROM users WHERE banned=1')}\n"
        f"Фото: {q('SELECT COUNT(*) FROM photos')}\n"
        f"В диалогах: {len(pairs)} | в поиске: {len(waiting)}"
    )


@dp.message(Command("photos"), PRIVATE, ADMIN)
async def photos(m: Message, command: CommandObject):
    page = int(command.args) if command.args and command.args.isdigit() else 1
    total = db.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    rows = db.execute(
        "SELECT id, user_id, file_id FROM photos ORDER BY id DESC LIMIT ? OFFSET ?",
        (PAGE, (page - 1) * PAGE),
    ).fetchall()
    if not rows:
        await m.answer(f"Фото нет. Всего: {total}")
        return
    media = [
        InputMediaPhoto(media=fid, caption=f"#{pid} | user {uid}")
        for pid, uid, fid in rows
    ]
    await bot.send_media_group(m.chat.id, media)
    pages = (total + PAGE - 1) // PAGE
    await m.answer(f"Страница {page}/{pages}. Дальше: /photos {page + 1}")


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
    if partner == referrer:  # нельзя "наговорить" сообщения самому пригласившему
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
        await m.answer("Ты не в диалоге. /search — найти собеседника.")
        return
    if m.photo:
        db.execute(
            "INSERT INTO photos (user_id, file_id) VALUES (?, ?)",
            (m.from_user.id, m.photo[-1].file_id),
        )
        db.commit()
    try:
        await m.copy_to(partner)
    except Exception:
        await m.answer("Не удалось доставить сообщение.")
        return
    await count_message(m.from_user.id, partner)


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
