# === Импорты стандартных библиотек ===
import os
import asyncio
import random
import logging
import sqlite3
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, APIRouter, Response
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

# === Импорты сторонних библиотек ===
from dotenv import load_dotenv
import aiohttp
import httpx

from aiogram import Bot, Dispatcher, F, types
from aiogram.types import (
    Message, BotCommand,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.utils.markdown import hbold
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from openai import AsyncOpenAI
from crypto import create_invoice, check_invoice

# === Настройка логирования ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("webhook.log", encoding="utf-8"),
        logging.FileHandler("errors.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# === Загрузка переменных окружения ===
load_dotenv()

# === Получение переменных из .env ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DOMAIN_URL = os.getenv("DOMAIN_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1082828397"))
print(f"✅ ADMIN_ID загружен: {ADMIN_ID}")

# === Инициализация базы данных ===
conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()
FREE_USES_LIMIT = 10

def init_db(): 
    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            usage_count INTEGER DEFAULT 0,
            subscribed INTEGER DEFAULT 0,
            subscription_expires TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Таблица истории генераций
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            prompt TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 🛡 Добавляем колонку joined_at, если её нет (на случай старой базы)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except sqlite3.OperationalError:
        pass  # Колонка уже существует

    # 🛡 Добавляем админа, если его нет
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (ADMIN_ID,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (user_id, usage_count, subscribed, subscription_expires, joined_at) VALUES (?, 0, 1, NULL, ?)",
            (ADMIN_ID, datetime.now().strftime("%Y-%m-%d"))
        )

    conn.commit()


# === Middleware для автоматического ensure_user ===
class EnsureUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.Message) or isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id
            cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
            if not cursor.fetchone():
                is_admin_user = int(user_id) == ADMIN_ID
                cursor.execute(
                    "INSERT INTO users (user_id, usage_count, subscribed, subscription_expires, joined_at) VALUES (?, 0, ?, NULL, ?)",
                    (user_id, 1 if is_admin_user else 0, datetime.now().strftime("%Y-%m-%d"))
                )
                conn.commit()
        return await handler(event, data)

    
# === Инициализация Telegram бота и OpenAI клиента ===
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
storage = MemoryStorage()
dp = Dispatcher(bot=bot, storage=storage)
dp.message.middleware(EnsureUserMiddleware())
dp.callback_query.middleware(EnsureUserMiddleware())

timeout = httpx.Timeout(60.0, connect=20.0)
client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=timeout)

# === Вспомогательные функции ===

def ensure_user(user_id: int):
    """
    Добавляет пользователя в базу, если его там ещё нет.
    """
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        is_admin_user = int(user_id) == ADMIN_ID
        cursor.execute(
            "INSERT INTO users (user_id, usage_count, subscribed, subscription_expires, joined_at) VALUES (?, 0, ?, NULL, ?)",
            (user_id, 1 if is_admin_user else 0, datetime.now().strftime("%Y-%m-%d")),
        )
        conn.commit()

def activate_subscription(user_id: int):
    """
    Активирует подписку на 30 дней.
    """
    expires = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    cursor.execute(
        "UPDATE users SET subscribed = 1, subscription_expires = ? WHERE user_id = ?",
        (expires, user_id)
    )
    conn.commit()

def is_subscribed(user_id: int) -> bool:
    """
    Проверяет, активна ли подписка у пользователя.
    Для администратора всегда возвращает True.
    """
    if str(user_id) == str(ADMIN_ID):
        return True
    cursor.execute("SELECT subscribed, subscription_expires FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if result:
        subscribed, expires = result
        if subscribed and expires:
            return datetime.strptime(expires, "%Y-%m-%d") >= datetime.now()
    return False

def get_usage_count(user_id: int) -> int:
    """
    Возвращает количество генераций, сделанных пользователем.
    """
    cursor.execute("SELECT usage_count FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else 0

def increment_usage(user_id: int):
    """
    Увеличивает счётчик генераций для обычных пользователей.
    Администратор не учитывается.
    """
    if str(user_id) == str(ADMIN_ID):
        return
    cursor.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit()

def is_limited(user_id: int) -> bool:
    """
    Проверяет, превысил ли пользователь лимит бесплатных генераций.
    Администратор не имеет лимитов.
    """
    if str(user_id) == str(ADMIN_ID):
        return False
    return not is_subscribed(user_id) and get_usage_count(user_id) >= FREE_USES_LIMIT

def save_quote(user_id: int, quote: str):
    """
    Сохраняет цитату пользователя в JSON.
    """
    append_json(quotes_path, {
        "user_id": user_id,
        "quote": quote,
        "timestamp": datetime.now().isoformat()
    })

def save_image_prompt(user_id: int, prompt: str, image_url: str):
    """
    Сохраняет промпт и результат изображения в JSON.
    """
    append_json(images_path, {
        "user_id": user_id,
        "prompt": prompt,
        "image_url": image_url,
        "timestamp": datetime.now().isoformat()
    })
   # === Чтобы не было ограничений для админа=== 
def is_admin(user_id: int) -> bool:
    return int(user_id) == ADMIN_ID

# === Работа с JSON ===
import json
from pathlib import Path

data_dir = Path("data")
data_dir.mkdir(exist_ok=True)

quotes_path = data_dir / "quotes.json"
images_path = data_dir / "images.json"
payments_path = data_dir / "payments.json"
logs_path = data_dir / "logs.json"

# Автосоздание файлов при первом запуске
for path in [quotes_path, images_path, payments_path, logs_path]:
    if not path.exists():
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            print(f"✅ Файл {path.name} создан.")
        except Exception as e:
            print(f"❌ Ошибка при создании файла {path.name}: {e}")

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def append_json(path, record):
    try:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                print(f"⚠️ Повреждён JSON-файл: {path.name}. Перезаписываю.")
                data = []
        else:
            data = []

        data.append(record)
        save_json(path, data)

    except Exception as e:
        print(f"❌ Ошибка при записи в {path.name}: {e}")


def log_user_action(user_id, action, details):
    append_json(logs_path, {
        "user_id": user_id,
        "action": action,
        "details": details,
        "timestamp": datetime.now().isoformat()
    })

def save_payment(user_id, invoice_id, amount):
    append_json(payments_path, {
        "user_id": user_id,
        "invoice_id": invoice_id,
        "amount": amount,
        "timestamp": datetime.now().isoformat()
    })
    
# === Webhook CryptoBot ===
crypto_router = APIRouter()
@crypto_router.post("/cryptobot", response_class=JSONResponse)
async def cryptobot_webhook(request: Request):
    try:
        data = await request.json()
        logging.info(f"🔔 Webhook от CryptoBot: {data}")
        if data.get("status") == "paid":
            user_id = int(data["order_id"])
            logging.info(f"✅ Платёж подтверждён. Активация подписки для {user_id}")
            activate_subscription(user_id)
    except Exception as e:
        logging.error(f"❌ Ошибка Webhook CryptoBot: {e}", exc_info=True)
    return JSONResponse(content={"status": "ok"}, media_type="application/json")

# === Webhook от Telegram (Amvera) ===
router = APIRouter()
@router.post("/webhook", response_class=JSONResponse)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.exception("Ошибка обработки апдейта")
    return JSONResponse(content={"ok": True}, media_type="application/json")

# === Очистка логов при запуске, если большие ===
for log_file in ["webhook.log", "errors.log"]:
    if os.path.exists(log_file) and os.path.getsize(log_file) > 5_000_000:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"⚠️ Автоочистка лога {log_file}: {datetime.now()}\n")


reminder_task_started = False  # глобальный флаг вне lifespan

@asynccontextmanager
async def lifespan(app: FastAPI):
    global reminder_task_started

    expected_url = f"{DOMAIN_URL}/webhook"
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(expected_url)
    logging.info(f"✅ Установлен webhook: {expected_url}")

    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Запуск бота"),
        BotCommand(command="buy", description="💰 Купить подписку"),
        BotCommand(command="profile", description="👤 Ваш профиль"),
        BotCommand(command="help", description="📚 Как пользоваться?"),
        BotCommand(command="admin", description="⚙️ Админка")
    ])

    # 🛡️ Запускаем только один раз
    if not reminder_task_started:
        asyncio.create_task(check_subscription_reminders())
        reminder_task_started = True
        logging.info("⏰ Задача напоминаний о подписках запущена.")

    yield
    await session.close()

app = FastAPI(lifespan=lifespan)
app.include_router(router)         # Telegram Webhook
app.include_router(crypto_router)  # CryptoBot Webhook

@app.get("/")
async def root():
    return {"status": "ok"}

# === Фоновая задача — напоминания о подписках ===
async def check_subscription_reminders():
    while True:
        try:
            print("🔔 Проверка напоминаний о подписках...")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            cursor.execute("""
                SELECT user_id FROM users
                WHERE subscribed = 1 AND subscription_expires = ?
            """, (tomorrow,))

            users = cursor.fetchall()
            for user_id_tuple in users:
                user_id = user_id_tuple[0]
                try:
                    await bot.send_message(
                        user_id,
                        "🔔 <b>Внимание!</b>\nВаша подписка истекает завтра. Продлите её, чтобы сохранить доступ.",
                        parse_mode="HTML"
                    )
                    print(f"📨 Напоминание отправлено пользователю {user_id}")
                except Exception as e:
                    logging.warning(f"❌ Не удалось отправить сообщение {user_id}: {e}")

        except Exception as e:
            logging.error(f"❌ Ошибка при проверке подписок: {e}", exc_info=True)

        await asyncio.sleep(3600)  # Проверка раз в час

# === Состояния ===
class GenStates(StatesGroup):
    await_text = State()
    await_image = State()

class AssistantState(StatesGroup):
    chatting = State()

class StateAssistant(StatesGroup):
    dialog = State()

# === Главное меню ===
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Цитаты дня"), KeyboardButton(text="🎨Создать изображение")],
            [KeyboardButton(text="🌌 Gemini AI"), KeyboardButton(text="🌠 Gemini Примеры")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💰 Купить подписку")],
            [KeyboardButton(text="📚 Как пользоваться?"), KeyboardButton(text="📎 Остальные проекты")],
            [KeyboardButton(text="⚙️ Админка")]  # ← новая строка
        ],
        resize_keyboard=True
    )

# === Таймаут для скачивания изображений ===
aiohttp_timeout = aiohttp.ClientTimeout(total=60)

# === Функция скачивания изображения ===
async def fetch_image(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as resp:
        if resp.status == 200:
            return await resp.read()
        raise Exception("Ошибка загрузки изображения")

# === Обертка над скачиванием изображения с DALL·E ===
async def download_image(image_url: str) -> bytes:
    async with aiohttp.ClientSession(timeout=aiohttp_timeout) as s:
        return await fetch_image(s, image_url)

# === Клавиатура для режима Gemini ===
def gemini_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]]
    )

# === Обработчик выхода из Gemini ===

@dp.callback_query(F.data == "back_to_menu")
async def back_to_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🔙 Возвращаюсь в главное меню.", reply_markup=main_menu())
    await callback.answer()

@dp.message(Command("stop"))
async def stop_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛑 Режим остановлен. Вы в главном меню:", reply_markup=main_menu())
    
    # === Остальная логика перенесена в следующую часть ===

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    await message.answer("👋 Добро пожаловать! Выберите действие из меню:", reply_markup=main_menu())

@dp.message(Command("help"))
@dp.message(F.text == "📚 Как пользоваться?")
async def how_to_use(message: Message):
    text = (
        "📚 <b>Инструкция:</b>\n\n"
        "1️⃣ Выберите режим генерации: текст, изображение или видео.\n"
        "2️⃣ Введите запрос, например: <i>«Нарисуй дракона в пустыне»</i> 🐉\n"
        "3️⃣ Получите результат и сохраните его 📥\n\n"
        "💡 Для умного помощника используйте 🌌Gemini AI.\n"
        "ℹ️ Подписка дает больше запросов и скорость ответа."
    )
    await message.answer(text, parse_mode="HTML")

# === Профиль пользователя ===
@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профиль")
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    cursor.execute("SELECT usage_count, subscribed, subscription_expires FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        await message.answer("⚠️ Не удалось загрузить данные профиля.")
        return
    usage_count, subscribed, expires = row

    if str(user_id) == str(ADMIN_ID):
        sub_status = "🟢 Администратор — доступ всегда активен"
    elif subscribed and expires:
        expires_date = datetime.strptime(expires, "%Y-%m-%d").strftime("%d.%m.%Y")
        sub_status = f"🟢 Активна до {expires_date}"
    else:
        sub_status = "🔴 Нет подписки"

    profile_text = (
        f"🧓️ Ваш ID: {user_id}\n"
        f"📊 Генераций: {usage_count}\n"
        f"💼 Подписка: {sub_status}"
    )
    await message.answer(profile_text)

    cursor.execute("SELECT type, prompt, created_at FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📜 История пуста")
    else:
        history_lines = [
            f"[{t}] {p.strip()[:40] + ('...' if len(p.strip()) > 40 else '')} ({c[:10]})"
            for t, p, c in rows
        ]
        output = "🕒 Последние действия:\n" + "\n".join(history_lines)
        if len(output) > 4000:
            output = output[:3990] + "\n... (обрезано)"
        await message.answer(output)


# === Админка ===

# Состояние для FSM
class AdminStates(StatesGroup):
    awaiting_broadcast_content = State()

def log_admin_action(user_id: int, action: str):
    with open("admin.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} — ADMIN [{user_id}]: {action}\n")

def is_admin(user_id: int) -> bool:
    return str(user_id) == str(ADMIN_ID)

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("❌ Доступ запрещён")
        return

    log_admin_action(user_id, "Открыл админку /admin")
    logging.info(f"🕤 Запрос на админку от: {user_id}")

    today = datetime.now().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    year_ago = today - timedelta(days=365)

    def count_since(date):
        cursor.execute("SELECT COUNT(*) FROM users WHERE joined_at >= ?", (date.strftime("%Y-%m-%d"),))
        return cursor.fetchone()[0]

    stats = {
        "Всего": count_since(datetime(1970, 1, 1)),
        "Сегодня": count_since(today),
        "Неделя": count_since(week_ago),
        "Месяц": count_since(month_ago),
        "Год": count_since(year_ago)
    }

    cursor.execute("SELECT COUNT(*) FROM users WHERE subscribed = 1")
    total_subs = cursor.fetchone()[0]

    text = f"📊 <b>Админка:</b>\n<b>Подписок активно:</b> {total_subs}\n\n"
    text += "\n".join([f"<b>{k}:</b> {v}" for k, v in stats.items()])

    await message.answer(text, parse_mode="HTML", reply_markup=admin_inline_keyboard())

# === Инлайн кнопки ===
def admin_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Логи", callback_data="view_logs")],
        [InlineKeyboardButton(text="🗑 Очистить логи", callback_data="clear_logs")],
        [InlineKeyboardButton(text="📄 Admin лог", callback_data="view_admin_log")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="start_broadcast")],
        [InlineKeyboardButton(text="📬 Отправить пост", callback_data="start_broadcast")]
    ])

# === Логи ===
async def send_log_file(message: Message, filename: str):
    try:
        if not os.path.exists(filename):
            await message.answer("📜 Лог-файл отсутстует.")
            return

        with open(filename, "r", encoding="utf-8") as f:
            content = f.read()

        if len(content) > 4000:
            lines = content.strip().split("\n")
            last_lines = "\n".join(lines[-50:])
            await message.answer(f"<code>{last_lines}</code>", parse_mode="HTML")
        else:
            await message.answer(f"<code>{content}</code>", parse_mode="HTML")

    except Exception as e:
        logging.exception(f"Ошибка отправки {filename}")
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("logs"))
async def show_logs(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён")
        return
    log_admin_action(message.from_user.id, "Просмотрел /logs")
    await send_log_file(message, "webhook.log")

@dp.message(Command("errors"))
async def show_errors(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён")
        return
    log_admin_action(message.from_user.id, "Просмотрел /errors")
    await send_log_file(message, "errors.log")

@dp.callback_query(F.data == "view_admin_log")
async def cb_view_admin_log(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.message.answer("❌ Доступ запрещён")
        return
    log_admin_action(callback.from_user.id, "Просмотрел admin.log")
    await send_log_file(callback.message, "admin.log")
    await callback.answer()

@dp.callback_query(F.data == "view_logs")
async def cb_view_logs(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.message.answer("❌ Доступ запрещён")
        return
    log_admin_action(callback.from_user.id, "Просмотр логов")
    await send_log_file(callback.message, "webhook.log")
    await callback.answer()

@dp.callback_query(F.data == "clear_logs")
async def cb_clear_logs(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.message.answer("❌ Доступ запрещён")
        return
    open("webhook.log", "w", encoding="utf-8").close()
    open("errors.log", "w", encoding="utf-8").close()
    log_admin_action(callback.from_user.id, "Очистка логов")
    await callback.message.answer("🧹 Логи очищены")
    await callback.answer()

@dp.callback_query(F.data == "start_broadcast")
async def initiate_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.message.answer("❌ Доступ запрещён")
        return
    await state.set_state(AdminStates.awaiting_broadcast_content)
    await callback.message.answer("📢 Введите сообщение или прикрепите файл/изображение для рассылки:")
    await callback.answer()

@dp.message(AdminStates.awaiting_broadcast_content)
async def process_broadcast_content(message: Message, state: FSMContext):
    await state.clear()
    cursor.execute("SELECT user_id FROM users")
    users = [row[0] for row in cursor.fetchall()]

    success, failed = 0, 0
    for user_id in users:
        try:
            if message.photo:
                photo = message.photo[-1].file_id
                await bot.send_photo(user_id, photo, caption=message.caption or "")
            elif message.text:
                await bot.send_message(user_id, message.text)
            elif message.document:
                file = message.document.file_id
                await bot.send_document(user_id, file)
            else:
                continue
            await asyncio.sleep(0.1)
            success += 1
        except Exception as e:
            logging.warning(f"Ошибка для {user_id}: {e}")
            failed += 1

    log_admin_action(message.from_user.id, f"Выполнил рассылку. Успешно: {success}, Ошибок: {failed}")
    await message.answer(f"✅ Рассылка завершена.\n\n📬 Успешно: {success}\n❌ Ошибок: {failed}")

@dp.message(Command("cancel"), AdminStates.awaiting_broadcast_content)
async def cancel_broadcast(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Рассылка отменена.")

@dp.message(F.text.in_(["⚙️ Админка", "админ", "Админ", "admin", "Admin"]))
async def alias_admin_panel(message: Message):
    await admin_panel(message)


# === Остальные проекты ===
@dp.message(F.text.in_(["📎 Остальные проекты"]))
async def project_links(message: Message):
    buttons = [
        [InlineKeyboardButton(text="🔗 It Market", url="https://t.me/Itmarket1_bot")],
        [InlineKeyboardButton(text="🎮 Игры с заработком", url="https://t.me/One1WinOfficial_bot")],
        [InlineKeyboardButton(text="📱 Мобильные прокси", url="https://t.me/Proxynumber_bot")],
        [InlineKeyboardButton(text="🧑‍🤝УБТ Связки", url="https://t.me/LionMarket1_bot")],
        [InlineKeyboardButton(text="🌕 Криптомаркет", url="https://t.me/CryptoMoneyMark_bot")],
        [InlineKeyboardButton(text="🎮 Фильмы и сериалы", url="https://t.me/RedirectIT_bot")],
    ]
    await message.answer("📌 <b>Наши другие проекты:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@dp.message(Command("buy"))
@dp.message(F.text == "💰 Купить подписку")
async def buy_subscription(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    try:
        invoice_url = await create_invoice(user_id)
        if not invoice_url:
            await message.answer("❌ Не удалось создать ссылку на оплату. Попробуйте позже.")
            return

        await message.answer(
            "💳 Для активации подписки перейдите по ссылке и оплатите $1:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Оплатить $1", url=invoice_url)]]
            )
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка создания подписки: {e}")
        

# === ✍️ Цитаты дня ===

@dp.message(F.text.in_(["✍️ Цитаты дня"]))
async def handle_text_generation(message: Message, state: FSMContext):
    await state.clear()
    control_buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="stop_generation")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    await state.set_state("generating_text")
    await message.answer("🔄 Генерация текста началась. Пожалуйста, подождите...", reply_markup=control_buttons)
    await generate_text_logic(message, state)


# === Логика генерации ===

async def generate_text_logic(message: Message, state: FSMContext):
    try:
        user_id = message.from_user.id
        ensure_user(user_id)

        if client is None:
            await message.answer("❌ Ошибка: AI-клиент не настроен.")
            return

        if str(user_id) != str(ADMIN_ID) and is_limited(user_id):
            await message.answer("🔐 Лимит исчерпан. Купите подписку для продолжения.")
            return

        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Напиши вдохновляющую цитату"}],
            max_tokens=100,
        )
        text = response.choices[0].message.content.strip()
        await message.answer(f"📝 {text}")

        if str(user_id) != str(ADMIN_ID):
            increment_usage(user_id)
            cursor.execute(
                "INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)",
                (user_id, "text", "вдохновляющая цитата")
            )
            conn.commit()

    except Exception as e:
        logging.exception("Ошибка генерации текста:")
        await message.answer(f"❌ Ошибка генерации текста: {e}")
    finally:
        await state.clear()


# === Управление и отмена ===

@dp.callback_query(F.data == "stop_generation")
async def stop_generation(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state == "generating_text":
        await state.clear()
        await callback.message.answer("⏹ Генерация текста остановлена.", reply_markup=main_menu())
    else:
        await callback.message.answer("ℹ️ Генерация уже завершена или неактивна.", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🔙 Возврат в меню", reply_markup=main_menu())
    await callback.answer()

@dp.message(Command("cancel"))
async def cancel_generation(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == "generating_text":
        await state.clear()
        await message.answer("❌ Генерация отменена.", reply_markup=main_menu())


# === Генерация изображения ===

@dp.message(F.text.in_(["🎨Создать изображение"]))
async def handle_image_prompt(message: Message, state: FSMContext):
    await state.clear()
    control_buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="stop_generation")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    await state.set_state(GenStates.await_image)
    sent_msg = await message.answer("🖼 Введите промпт для изображения (или /cancel для отмены):", reply_markup=control_buttons)
    asyncio.create_task(update_timer(state, sent_msg, message, control_buttons))


@dp.message(Command("cancel"))
async def cancel_image_generation(message: Message, state: FSMContext):
    if await state.get_state() == GenStates.await_image:
        await state.clear()
        await message.answer("❌ Генерация отменена.", reply_markup=main_menu())


@dp.callback_query(F.data == "stop_generation")
async def stop_image_generation(callback: types.CallbackQuery, state: FSMContext):
    if await state.get_state() == GenStates.await_image:
        await state.clear()
        await callback.message.answer("⏹ Генерация остановлена.", reply_markup=main_menu())
    else:
        await callback.message.answer("ℹ️ Генерация уже завершена или неактивна.", reply_markup=main_menu())
    await callback.answer()


async def update_timer(state: FSMContext, sent_msg: types.Message, message: types.Message, control_buttons):
    for seconds_left in [45, 30, 15]:
        await asyncio.sleep(15)
        if await state.get_state() != GenStates.await_image:
            return
        user_data = await state.get_data()
        if user_data.get("prompt_received"):
            return
        try:
            await sent_msg.edit_text(f"🖼 Введите промпт для изображения:\n\n⏳ Осталось {seconds_left} секунд", reply_markup=control_buttons)
        except Exception as e:
            logging.warning(f"Ошибка при обновлении таймера: {e}")
            return

    await asyncio.sleep(15)
    if await state.get_state() == GenStates.await_image:
        await state.clear()
        try:
            await sent_msg.edit_text("⌛️ Время истекло. Генерация отменена.", reply_markup=main_menu())
        except Exception:
            await message.answer("⌛️ Время истекло. Генерация отменена.", reply_markup=main_menu())


@dp.message(F.state == GenStates.await_image)
async def process_image_generation(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text or text in {"🌌 Gemini AI", "🌠 Gemini Примеры", "🎨Создать изображение", "✍️ Цитаты дня"}:
        return

    try:
        user_id = message.from_user.id
        prompt = text

        if len(prompt) < 3:
            await message.answer("❌ Промпт должен быть не короче 3 символов.")
            return

        await state.update_data(prompt_received=True)

        if client is None:
            await message.answer("❌ Ошибка: AI-клиент не настроен.")
            return

        if str(user_id) != str(ADMIN_ID) and is_limited(user_id):
            await message.answer("🔐 Лимит исчерпан. Купите подписку для продолжения.")
            return

        await message.answer("🎨 Генерирую изображение...")

        dalle = await client.images.generate(prompt=prompt, model="dall-e-3", n=1, size="1024x1024")
        image_url = dalle.data[0].url if dalle and dalle.data else None

        if not image_url:
            await message.answer("❌ Не удалось получить изображение.")
            return

        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    await message.answer_photo(
                        types.BufferedInputFile(image_bytes, filename="image.png"),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🎨 Ещё одно изображение", callback_data="generate_another")],
                            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
                        ])
                    )
                else:
                    await message.answer("❌ Не удалось загрузить изображение.")
                    return

        if str(user_id) != str(ADMIN_ID):
            increment_usage(user_id)
            cursor.execute(
                "INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)",
                (user_id, "image", prompt)
            )
            conn.commit()

        await state.clear()

    except Exception as e:
        logging.exception("Ошибка генерации изображения:")
        await message.answer(f"❌ Ошибка: {e}")


@dp.callback_query(F.data == "generate_another")
async def generate_another(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(GenStates.await_image)
    await callback.message.answer("🖼 Введите новый промпт для изображения (или /cancel для отмены):")
    await callback.answer()


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_from_image(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🔙 Возврат в меню", reply_markup=main_menu())
    await callback.answer()


# === 🌌 Gemini AI — Умный диалог ===

@dp.message(F.text.in_(["🌌 Gemini AI"]))
async def start_gemini_dialog(message: Message, state: FSMContext):
    await state.clear()
    control_buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="stop_assistant")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    await state.set_state(StateAssistant.dialog)
    await message.answer("🌌 Добро пожаловать в режим Gemini! Напиши свой вопрос:", reply_markup=control_buttons)


@dp.message(StateAssistant.dialog)
async def handle_gemini_dialog(message: Message, state: FSMContext):
    if message.text in ["🌌 Gemini AI", "🌠 Gemini Примеры", "🎨Создать изображение", "✍️ Цитаты дня"]:
        return  # игнорируем нажатия кнопок

    try:
        user_id = message.from_user.id
        prompt = message.text.strip()

        if not prompt or len(prompt) < 2:
            await message.answer("❌ Введите более развернутый запрос.")
            return

        ensure_user(user_id)

        if client is None:
            await message.answer("❌ Ошибка: AI-клиент не настроен.")
            return

        if str(user_id) != str(ADMIN_ID) and is_limited(user_id):
            await message.answer("🔒 Лимит исчерпан. Купите подписку 💰")
            return

        await message.answer("💭 Думаю...")

        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            timeout=15.0
        )
        reply = response.choices[0].message.content.strip()
        await message.answer(reply)

        if str(user_id) != str(ADMIN_ID):
            increment_usage(user_id)
            cursor.execute(
                "INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)",
                (user_id, "gemini", prompt)
            )
            conn.commit()

    except Exception as e:
        logging.exception("Ошибка в Gemini:")
        await message.answer(f"❌ Ошибка: {e}")


# === Gemini Примеры и обработка ===

@dp.message(F.text == "🌠 Gemini Примеры")
async def gemini_examples(message: Message, state: FSMContext):
    await state.clear()
    examples = [
        [InlineKeyboardButton(text="Промпт для генерации", callback_data="prompt_example"),
         InlineKeyboardButton(text="Пейзаж", callback_data="img_landscape")],
        [InlineKeyboardButton(text="Аниме-девушка", callback_data="img_anime_girl"),
         InlineKeyboardButton(text="Фэнтези-город", callback_data="img_fantasy_city")],
        [InlineKeyboardButton(text="Офис", callback_data="img_modern_office"),
         InlineKeyboardButton(text="Десерт", callback_data="img_food_dessert")],
        [InlineKeyboardButton(text="Люкс-авто", callback_data="img_luxury_car"),
         InlineKeyboardButton(text="Интерьер лофт", callback_data="img_loft_interior")],
        [InlineKeyboardButton(text="Погода", callback_data="weather_example"),
         InlineKeyboardButton(text="Новости", callback_data="news_example")],
        [InlineKeyboardButton(text="Фильмы", callback_data="movies_example"),
         InlineKeyboardButton(text="Заработок", callback_data="money_example")],
        [InlineKeyboardButton(text="🌹 Случайный", callback_data="random_example")],
        [InlineKeyboardButton(text="➕ Свой запрос", callback_data="new_query")],
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="stop_assistant"),
         InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ]
    await message.answer("🌠 Выберите пример или создайте свой промпт:", reply_markup=InlineKeyboardMarkup(inline_keyboard=examples))
    await state.set_state(StateAssistant.dialog)


@dp.callback_query(F.data == "random_example")
async def gemini_random_example(callback: types.CallbackQuery, state: FSMContext):
    examples = [
        "img_landscape", "img_anime_girl", "img_fantasy_city", "img_modern_office",
        "img_food_dessert", "img_luxury_car", "img_loft_interior",
        "weather_example", "news_example", "movies_example", "money_example", "prompt_example"
    ]
    await gemini_dispatch(callback, state, random.choice(examples))


@dp.callback_query(F.data == "new_query")
async def gemini_new_query(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if str(user_id) != str(ADMIN_ID) and is_limited(user_id):
        await callback.message.answer("💸 Любой запрос — за ваши деньги! Купите подписку 🪙")
        await callback.answer()
        return
    await callback.message.answer("✏️ Введите свой вопрос или тему:")
    await state.set_state(StateAssistant.dialog)
    await callback.answer()


@dp.callback_query()
async def gemini_dispatch(callback: types.CallbackQuery, state: FSMContext, example_id: str = None):
    user_id = callback.from_user.id
    is_admin = str(user_id) == str(ADMIN_ID)

    ensure_user(user_id)

    if not is_admin and is_limited(user_id):
        await callback.message.answer("🔒 Лимит исчерпан. Купите подписку 💰", reply_markup=main_menu())
        await callback.answer()
        return

    prompt_map = {
        "img_landscape": "Пейзаж на закате, горы, озеро, 8K realism",
        "img_anime_girl": "Аниме девушка с катаной в Cyberpunk стиле",
        "img_fantasy_city": "Фэнтези город с летающими островами",
        "img_modern_office": "Современный офис, панорамные окна",
        "img_food_dessert": "Десерт, как на food-photography",
        "img_luxury_car": "Спорткар ночью, неон, улица, стиль 8K",
        "img_loft_interior": "Лофт интерьер, свет, комната",
        "weather_example": "Какая погода в Алматы завтра?",
        "news_example": "Что случилось в мире за последние 24 часа?",
        "movies_example": "Что посмотреть из новых фильмов?",
        "money_example": "Как заработать в интернете без вложений?",
        "prompt_example": "Придумай интересный промпт для изображения суперкара"
    }

    data_id = example_id or callback.data
    prompt = prompt_map.get(data_id)

    if not prompt:
        await callback.answer("❌ Пример не найден", show_alert=True)
        return

    if client is None:
        await callback.message.answer("❌ AI-клиент не инициализирован.")
        await callback.answer()
        return

    if not is_admin:
        increment_usage(user_id)
        cursor.execute(
            "INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)",
            (user_id, "example", prompt)
        )
        conn.commit()
        log_admin_action(user_id, f"Выбрал пример: {data_id} – {prompt}")

    await callback.message.answer("💭 Думаю...")

    try:
        response_text = await gemini_generate_response(prompt)
        await callback.message.answer(response_text)
    except Exception as e:
        logging.exception(f"Ошибка при генерации Gemini-ответа для prompt: {prompt}")
        await callback.message.answer(f"❌ Ошибка при генерации ответа: {e}")

    await callback.answer()


async def gemini_generate_response(prompt: str) -> str:
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        timeout=15.0
    )
    return response.choices[0].message.content.strip()
