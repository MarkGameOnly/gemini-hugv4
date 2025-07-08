import os
import sqlite3
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import load_dotenv

load_dotenv()

ADMIN_ID = 1082828397
BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")

conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()

logging.basicConfig(level=logging.INFO)

session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Логи", callback_data="logs")],
        [InlineKeyboardButton(text="🗑 Очистить логи", callback_data="clear_logs")]
    ])

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Доступ запрещён")
        return
    await message.answer("🔐 Добро пожаловать, админ!", reply_markup=admin_keyboard())

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Доступ запрещён")
        return

    def count_since(days):
        cursor.execute("SELECT COUNT(*) FROM users WHERE joined_at >= date('now', ?)", (f'-{days} days',))
        return cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM users WHERE subscribed = 1")
    subs = cursor.fetchone()[0]

    stats = f"""📊 <b>Админка</b>:
👤 Всего пользователей: {total}
💼 Подписок активно: {subs}
📅 Сегодня: {count_since(0)}
🗓 За 7 дней: {count_since(7)}
📆 За 30 дней: {count_since(30)}"""
    await message.answer(stats, reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "logs")
async def send_logs(callback: types.CallbackQuery):
    if not os.path.exists("webhook.log"):
        await callback.message.answer("📭 Лог-файл пуст")
        return
    with open("webhook.log", "r", encoding="utf-8") as f:
        logs = f.read().strip().split("\n")[-50:]
        await callback.message.answer(f"<code>{chr(10).join(logs)}</code>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "clear_logs")
async def clear_logs(callback: types.CallbackQuery):
    open("webhook.log", "w").close()
    open("errors.log", "w").close()
    await callback.message.answer("🗑️ Логи очищены")
    await callback.answer()

@dp.message(Command("errors"))
async def send_errors(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Доступ запрещён")
        return
    if not os.path.exists("errors.log"):
        await message.answer("Ошибок нет.")
        return
    with open("errors.log", "r", encoding="utf-8") as f:
        errors = f.read().strip().split("\n")[-30:]
        await message.answer(f"<code>{chr(10).join(errors)}</code>", parse_mode="HTML")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

