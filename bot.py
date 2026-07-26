import asyncio
import logging
import os
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("suggestion-bot")

# ---------- Конфигурация (берётся из переменных окружения) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]  # "@channelusername" или "-1001234567890"
DB_PATH = os.environ.get("DB_PATH", "bot.db")

router = Router()
bot: Bot = None
db: aiosqlite.Connection = None


# ---------- База данных ----------
async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            banned_until TEXT,
            banned_forever INTEGER DEFAULT 0
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            admin_msg_id INTEGER,
            info_msg_id INTEGER,
            content_type TEXT,
            text_content TEXT,
            file_id TEXT,
            created_at TEXT
        )
        """
    )
    await db.commit()


async def upsert_user(user_id: int, username: str | None, first_name: str | None):
    await db.execute(
        """
        INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
        """,
        (user_id, username, first_name),
    )
    await db.commit()


async def get_user(user_id: int):
    cur = await db.execute(
        "SELECT user_id, username, first_name, banned_until, banned_forever FROM users WHERE user_id=?",
        (user_id,),
    )
    return await cur.fetchone()


async def is_banned(user_id: int):
    row = await get_user(user_id)
    if not row:
        return False, None
    _, _, _, banned_until, banned_forever = row
    if banned_forever:
        return True, "forever"
    if banned_until:
        until_dt = datetime.fromisoformat(banned_until)
        if until_dt > datetime.utcnow():
            return True, until_dt
    return False, None


async def get_submission(sub_id: int):
    cur = await db.execute(
        "SELECT id, user_id, admin_msg_id, info_msg_id, content_type, text_content, file_id "
        "FROM submissions WHERE id=?",
        (sub_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    keys = ["id", "user_id", "admin_msg_id", "info_msg_id", "content_type", "text_content", "file_id"]
    return dict(zip(keys, row))


def kb_for_submission(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🆔", callback_data=f"id:{sub_id}"),
                InlineKeyboardButton(text="📤", callback_data=f"pub:{sub_id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"clr:{sub_id}"),
                InlineKeyboardButton(text="🔇", callback_data=f"mute:{sub_id}"),
                InlineKeyboardButton(text="🚫", callback_data=f"ban:{sub_id}"),
            ]
        ]
    )


def display_name(username: str | None, first_name: str | None) -> str:
    if username:
        return f"@{username}"
    return first_name or "без имени"


ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🗑 Очистить предложку"),
            KeyboardButton(text="🚫 Список забаненных"),
        ]
    ],
    resize_keyboard=True,
)

WELCOME_TEXT = (
    "👋 Привет! Здесь можно оставить идею, мысль или интересный пост для канала — "
    "текстом или с фото.\n\n"
    "Всё уходит на модерацию, лучшее опубликуется.\n\n"
    "Владелец - @Shap14K 💛"
)


# ---------- /start ----------
@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "Бот-предложка запущен.\n\n"
            "На каждое сообщение пользователя будет карточка с кнопками:\n"
            "🆔 — узнать ID и ник отправителя\n"
            "📤 — опубликовать в канал (с указанием автора)\n"
            "🗑 — очистить всю историю сообщений этого пользователя\n"
            "🔇 — запретить писать боту на 7 дней\n"
            "🚫 — заблокировать навсегда (повторное нажатие — разблокировать)\n\n"
            "Чтобы ответить пользователю — сделайте Reply (ответить) на его сообщение прямо здесь.",
            reply_markup=ADMIN_KEYBOARD,
        )
    else:
        await message.answer(WELCOME_TEXT)


# ---------- Ответ админа пользователю через Reply ----------
@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.reply_to_message)
async def admin_reply(message: Message):
    reply_to_id = message.reply_to_message.message_id
    cur = await db.execute(
        "SELECT user_id FROM submissions WHERE admin_msg_id=? OR info_msg_id=? ORDER BY id DESC LIMIT 1",
        (reply_to_id, reply_to_id),
    )
    row = await cur.fetchone()
    if not row:
        await message.answer("⚠️ Это сообщение не привязано ни к одному пользователю.")
        return

    user_id = row[0]
    try:
        await message.copy_to(chat_id=user_id)
        await message.answer("✅ Ответ отправлен пользователю.")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить ответ: {e}")


# ---------- Кнопка: очистить всю предложку ----------
@router.message(
    F.chat.type == "private", F.from_user.id == ADMIN_ID, F.text == "🗑 Очистить предложку"
)
async def clear_queue(message: Message):
    cur = await db.execute("SELECT admin_msg_id, info_msg_id FROM submissions")
    rows = await cur.fetchall()
    deleted = 0
    for admin_msg_id, info_msg_id in rows:
        for mid in (admin_msg_id, info_msg_id):
            try:
                await bot.delete_message(ADMIN_ID, mid)
                deleted += 1
            except Exception:
                pass

    await db.execute("DELETE FROM submissions")
    await db.commit()
    await message.answer(f"🗑 Предложка очищена. Удалено сообщений: {deleted}")


# ---------- Кнопка: список забаненных ----------
@router.message(
    F.chat.type == "private", F.from_user.id == ADMIN_ID, F.text == "🚫 Список забаненных"
)
async def banned_list(message: Message):
    cur = await db.execute(
        "SELECT user_id, username, first_name, banned_until, banned_forever FROM users "
        "WHERE banned_forever=1 OR banned_until IS NOT NULL"
    )
    rows = await cur.fetchall()
    now = datetime.utcnow()
    lines = []
    for user_id, username, first_name, banned_until, banned_forever in rows:
        name = display_name(username, first_name)
        if banned_forever:
            lines.append(f"🚫 {name} (ID: {user_id}) — навсегда")
        elif banned_until:
            until_dt = datetime.fromisoformat(banned_until)
            if until_dt > now:
                lines.append(f"🔇 {name} (ID: {user_id}) — до {until_dt.strftime('%d.%m.%Y %H:%M')} UTC")

    if not lines:
        await message.answer("Список забаненных пуст.")
    else:
        await message.answer("Забаненные пользователи:\n\n" + "\n".join(lines))


# ---------- Прочие сообщения от админа (не reply) ----------
@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID)
async def admin_other(message: Message):
    await message.answer(
        "ℹ️ Чтобы ответить пользователю — сделайте Reply (ответить) на его сообщение."
    )


# ---------- Сообщения от пользователей ----------
@router.message(F.chat.type == "private", F.from_user.id != ADMIN_ID)
async def user_submission(message: Message):
    user = message.from_user
    await upsert_user(user.id, user.username, user.first_name)

    banned, until = await is_banned(user.id)
    if banned:
        if until == "forever":
            await message.answer("🚫 Вы заблокированы и не можете отправлять сообщения этому боту.")
        else:
            await message.answer(
                f"🔇 Вам временно запрещено писать. Попробуйте после "
                f"{until.strftime('%d.%m.%Y %H:%M')} UTC."
            )
        return

    info_text = f"👤 От: {display_name(user.username, user.first_name)} (ID: {user.id})"
    info_msg = await bot.send_message(ADMIN_ID, info_text)

    admin_msg = await message.copy_to(chat_id=ADMIN_ID)

    content_type = message.content_type
    text_content = message.text or message.caption or ""
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.document:
        file_id = message.document.file_id
    elif message.animation:
        file_id = message.animation.file_id

    cur = await db.execute(
        """
        INSERT INTO submissions (user_id, admin_msg_id, info_msg_id, content_type, text_content, file_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user.id,
            admin_msg.message_id,
            info_msg.message_id,
            content_type,
            text_content,
            file_id,
            datetime.utcnow().isoformat(),
        ),
    )
    await db.commit()
    sub_id = cur.lastrowid

    await bot.edit_message_reply_markup(
        ADMIN_ID, admin_msg.message_id, reply_markup=kb_for_submission(sub_id)
    )

    await message.answer("✅ Спасибо! Ваше сообщение отправлено на модерацию.")


# ---------- Callback: узнать ID ----------
@router.callback_query(F.data.startswith("id:"))
async def cb_id(callback: CallbackQuery):
    sub_id = int(callback.data.split(":")[1])
    sub = await get_submission(sub_id)
    if not sub:
        await callback.answer("Не найдено", show_alert=True)
        return
    urow = await get_user(sub["user_id"])
    username = urow[1] if urow else None
    text = f"ID: {sub['user_id']}\n{display_name(username, urow[2] if urow else None)}"
    await callback.answer(text, show_alert=True)


# ---------- Callback: опубликовать в канал ----------
@router.callback_query(F.data.startswith("pub:"))
async def cb_publish(callback: CallbackQuery):
    sub_id = int(callback.data.split(":")[1])
    sub = await get_submission(sub_id)
    if not sub:
        await callback.answer("Не найдено", show_alert=True)
        return

    urow = await get_user(sub["user_id"])
    username = urow[1] if urow else None
    first_name = urow[2] if urow else None
    author = display_name(username, first_name)
    attribution = f"\n\n— {author}"
    text = (sub["text_content"] or "") + attribution

    try:
        ctype = sub["content_type"]
        if ctype == "text":
            await bot.send_message(CHANNEL_ID, text)
        elif ctype == "photo":
            await bot.send_photo(CHANNEL_ID, sub["file_id"], caption=text)
        elif ctype == "video":
            await bot.send_video(CHANNEL_ID, sub["file_id"], caption=text)
        elif ctype == "animation":
            await bot.send_animation(CHANNEL_ID, sub["file_id"], caption=text)
        elif ctype == "document":
            await bot.send_document(CHANNEL_ID, sub["file_id"], caption=text)
        else:
            await bot.copy_message(CHANNEL_ID, ADMIN_ID, sub["admin_msg_id"])
            await bot.send_message(CHANNEL_ID, f"— {author}")
    except Exception as e:
        await callback.answer(f"Ошибка публикации: {e}", show_alert=True)
        return

    await callback.answer("✅ Опубликовано в канал")
    try:
        await bot.edit_message_reply_markup(ADMIN_ID, sub["admin_msg_id"], reply_markup=None)
        await bot.send_message(
            ADMIN_ID, "☑️ Опубликовано в канал.", reply_to_message_id=sub["admin_msg_id"]
        )
    except Exception:
        pass


# ---------- Callback: очистить историю пользователя ----------
@router.callback_query(F.data.startswith("clr:"))
async def cb_clear(callback: CallbackQuery):
    sub_id = int(callback.data.split(":")[1])
    sub = await get_submission(sub_id)
    if not sub:
        await callback.answer("Не найдено", show_alert=True)
        return

    user_id = sub["user_id"]
    cur = await db.execute(
        "SELECT admin_msg_id, info_msg_id FROM submissions WHERE user_id=?", (user_id,)
    )
    rows = await cur.fetchall()
    deleted = 0
    for admin_msg_id, info_msg_id in rows:
        for mid in (admin_msg_id, info_msg_id):
            try:
                await bot.delete_message(ADMIN_ID, mid)
                deleted += 1
            except Exception:
                pass

    await db.execute("DELETE FROM submissions WHERE user_id=?", (user_id,))
    await db.commit()
    await callback.answer(f"🗑 Удалено сообщений: {deleted}", show_alert=True)


# ---------- Callback: мут на 7 дней ----------
@router.callback_query(F.data.startswith("mute:"))
async def cb_mute(callback: CallbackQuery):
    sub_id = int(callback.data.split(":")[1])
    sub = await get_submission(sub_id)
    if not sub:
        await callback.answer("Не найдено", show_alert=True)
        return

    until = datetime.utcnow() + timedelta(days=7)
    await db.execute(
        "UPDATE users SET banned_until=? WHERE user_id=?", (until.isoformat(), sub["user_id"])
    )
    await db.commit()
    await callback.answer(
        f"🔇 Пользователь не сможет писать 7 дней (до {until.strftime('%d.%m %H:%M')} UTC)",
        show_alert=True,
    )


# ---------- Callback: бан навсегда (toggle) ----------
@router.callback_query(F.data.startswith("ban:"))
async def cb_ban(callback: CallbackQuery):
    sub_id = int(callback.data.split(":")[1])
    sub = await get_submission(sub_id)
    if not sub:
        await callback.answer("Не найдено", show_alert=True)
        return

    urow = await get_user(sub["user_id"])
    currently_banned = urow[4] if urow else 0
    new_state = 0 if currently_banned else 1
    await db.execute(
        "UPDATE users SET banned_forever=? WHERE user_id=?", (new_state, sub["user_id"])
    )
    await db.commit()

    if new_state:
        await callback.answer("🚫 Пользователь заблокирован навсегда", show_alert=True)
    else:
        await callback.answer("✅ Пользователь разблокирован", show_alert=True)


# ---------- Запуск ----------
async def main():
    global bot
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
