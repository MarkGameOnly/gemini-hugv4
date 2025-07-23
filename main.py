# === Импорты стандартных библиотек ===
import os
import asyncio
import random
import logging
import sqlite3
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, APIRouter, Response, Form, UploadFile, File
import base64
from fastapi.responses import JSONResponse, HTMLResponse
from contextlib import asynccontextmanager
import json
from pathlib import Path


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
from crypto import create_invoice
from openai import APITimeoutError
import shutil
from aiogram.types import ForceReply

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

BOT_TOKEN = os.getenv("BOT_TOKEN")
DOMAIN_URL = os.getenv("DOMAIN_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1082828397"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
text_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
image_client = AsyncOpenAI(api_key=OPENAI_API_KEY)  # Использовать один и тот же ключ!

# === Инициализация базы данных ===
conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()
FREE_USES_LIMIT = 10

# === Routers объявляем СРАЗУ после импортов и переменных ===
router = APIRouter()
crypto_router = APIRouter()

def init_db():
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            usage_count INTEGER DEFAULT 0,
            subscribed INTEGER DEFAULT 0,
            subscription_expires TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            prompt TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (ADMIN_ID,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (user_id, usage_count, subscribed, subscription_expires, joined_at) VALUES (?, 0, 1, NULL, ?)",
            (ADMIN_ID, datetime.now().strftime("%Y-%m-%d"))
        )
    conn.commit()
init_db()


# === Middleware EnsureUser ===
class EnsureUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.Message) or isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id
            cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
            if not cursor.fetchone():
                is_admin = int(user_id) == ADMIN_ID
                cursor.execute(
                    "INSERT INTO users (user_id, usage_count, subscribed, subscription_expires, joined_at) VALUES (?, 0, ?, NULL, ?)",
                    (user_id, 1 if is_admin else 0, datetime.now().strftime("%Y-%m-%d"))
                )
                conn.commit()
        return await handler(event, data)

session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
storage = MemoryStorage()
dp = Dispatcher(bot=bot, storage=storage)
dp.message.middleware(EnsureUserMiddleware())
dp.callback_query.middleware(EnsureUserMiddleware())

async def weekly_backup():
    while True:
        try:
            now = datetime.now()
            if now.weekday() == 0 and now.hour == 3:  # Понедельник, 03:00
                backup_dir = data_dir / "backups"
                backup_dir.mkdir(exist_ok=True)
                users_backup = backup_dir / f"users_{now.strftime('%Y%m%d_%H%M')}.db"
                payments_backup = backup_dir / f"payments_{now.strftime('%Y%m%d_%H%M')}.json"
                shutil.copy("users.db", users_backup)
                shutil.copy(payments_path, payments_backup)
                logging.info(f"📦 Резервные копии созданы: {users_backup}, {payments_backup}")
                await asyncio.sleep(3600)  # чтобы не делать backup несколько раз за утро
            await asyncio.sleep(1800)  # Проверка дважды в час
        except Exception as e:
            logging.error(f"❌ Ошибка при резервном копировании: {e}", exc_info=True)


# === Вспомогательные функции ===

def ensure_user(user_id: int):
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        is_admin_user = int(user_id) == ADMIN_ID
        cursor.execute(
            "INSERT INTO users (user_id, usage_count, subscribed, subscription_expires, joined_at) VALUES (?, 0, ?, NULL, ?)",
            (user_id, 1 if is_admin_user else 0, datetime.now().strftime("%Y-%m-%d")),
        )
        conn.commit()

def activate_subscription(user_id: int):
    expires = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    cursor.execute(
        "UPDATE users SET subscribed = 1, subscription_expires = ? WHERE user_id = ?",
        (expires, user_id)
    )
    conn.commit()

def is_subscribed(user_id: int) -> bool:
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
    cursor.execute("SELECT usage_count FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else 0

def increment_usage(user_id: int):
    if str(user_id) == str(ADMIN_ID):
        return
    cursor.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit()

def is_limited(user_id: int) -> bool:
    if str(user_id) == str(ADMIN_ID):
        return False
    return not is_subscribed(user_id) and get_usage_count(user_id) >= FREE_USES_LIMIT

def is_admin(user_id: int) -> bool:
    return int(user_id) == ADMIN_ID

# === JSON-логика, автофайлы и т.д. ===
data_dir = Path("data")
data_dir.mkdir(exist_ok=True)
quotes_path = data_dir / "quotes.json"
images_path = data_dir / "images.json"
payments_path = data_dir / "payments.json"
logs_path = data_dir / "logs.json"
for path in [quotes_path, images_path, payments_path, logs_path]:
    if not path.exists():
        with open(path, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)

def append_json(path, record):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = []
    data.append(record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_image_record(prompt, url):
    append_json(images_path, {
        "prompt": prompt,
        "url": url,
        "created_at": datetime.now().isoformat()
    })

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


# === Endpoint для Telegram Webhook ===
@router.post("/webhook", response_class=JSONResponse)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.exception("Ошибка обработки апдейта")
    return JSONResponse(content={"ok": True}, media_type="application/json")

# === Endpoint для CryptoBot Webhook ===
@crypto_router.post("/cryptobot", response_class=JSONResponse)
async def cryptobot_webhook(request: Request):
    """
    Вебхук для CryptoBot.
    Автоматически уведомляет админа и присылает кнопку для активации подписки.
    """
    try:
        data = await request.json()
        logging.info(f"🔔 Webhook от CryptoBot: {data}")

        if data.get("status") == "paid":
            user_id = int(data.get("payload"))
            amount = data.get("amount")
            invoice_id = data.get("invoice_id")
            # Сохраняем платеж в payments.json
            save_payment(user_id, invoice_id, amount)
            # Инлайн-кнопка для активации
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="✅ Активировать",
                        callback_data=f"activate_user_{user_id}"
                    )]
                ]
            )
            text = (
                f"💸 <b>Поступила новая оплата!</b>\n"
                f"🧑‍💻 User ID: <code>{user_id}</code>\n"
                f"💰 Сумма: {amount} USDT\n"
                f"🧾 Invoice: <code>{invoice_id}</code>\n\n"
                f"⚡ Для активации подпишки нажми кнопку ниже."
            )
            await bot.send_message(ADMIN_ID, text, parse_mode="HTML", reply_markup=keyboard)
            logging.info(f"🟢 Админ уведомлён о платеже от {user_id} ({amount})")
    except Exception as e:
        logging.error(f"❌ Ошибка Webhook CryptoBot: {e}", exc_info=True)
    return JSONResponse(content={"status": "ok"}, media_type="application/json")

# === Остальные функции и endpoint'ы ===
# ... manual_activate, admin-панель, генерация изображений и т.д. ...

# === Lifespan ===
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

# === Очистка логов ===
for log_file in ["webhook.log", "errors.log"]:
    if os.path.exists(log_file) and os.path.getsize(log_file) > 5_000_000:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"⚠️ Автоочистка лога {log_file}: {datetime.now()}\n")

reminder_task_started = False  # глобальный флаг вне lifespan

# === Фоновая задача — напоминания о подписках ===
async def check_subscription_reminders():
    while True:
        try:
            print("🔔 Проверка напоминаний о подписках...")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            cursor.execute("SELECT user_id FROM users WHERE subscribed = 1 AND subscription_expires = ?", (tomorrow,))
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

            # Новая часть: уведомление о завершении подписки сегодня
            today = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("SELECT user_id FROM users WHERE subscribed = 1 AND subscription_expires = ?", (today,))
            users_expired = cursor.fetchall()
            for user_id_tuple in users_expired:
                user_id = user_id_tuple[0]
                try:
                    # Снять подписку
                    cursor.execute(
                        "UPDATE users SET subscribed = 0, subscription_expires = NULL WHERE user_id = ?",
                        (user_id,)
                    )
                    conn.commit()
                    await bot.send_message(
                        user_id,
                        "🔴 <b>Ваша подписка завершилась сегодня.</b>\nДля продолжения оформления — оплатите повторно.",
                        parse_mode="HTML"
                    )
                    print(f"📨 Уведомление об окончании подписки отправлено {user_id}")
                except Exception as e:
                    logging.warning(f"❌ Не удалось отправить финальное уведомление {user_id}: {e}")

        except Exception as e:
            logging.error(f"❌ Ошибка при проверке подписок: {e}", exc_info=True)
        await asyncio.sleep(3600)  # Проверка раз в час

# === И только теперь создаём app и регистрируем роутеры! ===
app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.include_router(crypto_router)

@app.get("/")
async def root():
    return {"status": "ok"}

# Не делай asyncio.create_task вне lifespan!
# Все фоновые задачи лучше запускать через lifespan!


# === Сайты ==== 

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://itm-code.ru",
        "https://itm-code.ru/geminiapp",
        "https://www.itm-code.ru",
        "http://localhost:3000",
        "http://localhost"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
            [KeyboardButton(text="✍️ Цитаты дня")],
            [KeyboardButton(text="🌌 Gemini AI"), KeyboardButton(text="🌠 Gemini Примеры")],
            [KeyboardButton(text="🎨 Создать изображение")], 
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🌐 Генерация на сайте")],
            [KeyboardButton(text="📚 Как пользоваться?"), KeyboardButton(text="📎 Остальные проекты")]
        ],
        resize_keyboard=True
    )
# === Создать изображения в боте === 

@dp.message(F.text.in_(["🎨 Создать изображение"]))
async def handle_image_prompt(message: Message, state: FSMContext):
    await state.clear()
    control_buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="stop_generation")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    await state.set_state(GenStates.await_image)
    await message.answer("🖼 Введите промпт для изображения (или /cancel для отмены):", reply_markup=control_buttons)

@dp.message(GenStates.await_image)
async def generate_dalle_image(message: Message, state: FSMContext):
    user_id = message.from_user.id
    prompt = message.text.strip()
    ensure_user(user_id)

    if not prompt or len(prompt) < 3:
        await message.answer("❌ Введите осмысленный запрос для генерации.")
        return

    if str(user_id) != str(ADMIN_ID) and is_limited(user_id):
        await message.answer("🔐 Лимит исчерпан. Купите подписку 💰")
        return

    await message.answer("🎨 Генерирую изображение...")

    try:
        dalle = await image_client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="hd",
            response_format="url"
        )
        image_url = dalle.data[0].url if dalle and dalle.data else None

        if not image_url:
            await message.answer("❌ Не удалось получить изображение.")
            return

        await message.answer_photo(image_url, caption=f"🖼 Ваш запрос: {prompt}")
        save_image_record(prompt, image_url)

        if str(user_id) != str(ADMIN_ID):
            increment_usage(user_id)
            cursor.execute(
                "INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)",
                (user_id, "image", prompt)
            )
            conn.commit()
    except APITimeoutError:
        await message.answer("⏳ OpenAI долго думает или перегружен. Попробуйте снова через минуту!")
    except Exception as e:
        await message.answer(f"❌ Ошибка при генерации: {e}")
    finally:
        await state.clear()




# === Таймаут для скачивания изображений ===
aiohttp_timeout = aiohttp.ClientTimeout(total=180)

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
    await state.clear()
    await callback.message.answer("🔙 Возвращаюсь в главное меню.", reply_markup=main_menu())
    await callback.answer()

@dp.message(Command("stop"))
async def stop_command(message: Message, state: FSMContext):
    await state.clear()
    await state.clear()
    await message.answer("🛑 Режим остановлен. Вы в главном меню:", reply_markup=main_menu())
    
    # === Остальная логика перенесена в следующую часть ===

@dp.message(F.text == "🌐 Генерация на сайте")
async def open_site(message: types.Message):
    await message.answer(
        "Открой наш AI генератор прямо в Telegram! Нажми на кнопку ниже и запускай Mini App:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="✨ Открыть AI Mini App",
                    url="https://t.me/GeminiITMWeb_bot/myapp"
                )
            ]]
        )
    )

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
    awaiting_user_id = State()

def log_admin_action(user_id: int, action: str):
    with open("admin.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} — ADMIN [{user_id}]: {action}\n")

def is_admin(user_id: int) -> bool:
    return str(user_id) == str(ADMIN_ID)

# Сохраняем последние id сообщений с карточками для удаления при смене страницы
admin_last_card_msgs = {}

@dp.message(Command("admin"))
async def admin_panel(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("❌ Доступ запрещён")
        return

    await state.update_data(admin_panel_msg_id=message.message_id)  # Сохраним id входного сообщения

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


def admin_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Логи", callback_data="view_logs")],
        [InlineKeyboardButton(text="🗑 Очистить логи", callback_data="clear_logs")],
        [InlineKeyboardButton(text="📄 Admin лог", callback_data="view_admin_log")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="start_broadcast")],
        [InlineKeyboardButton(text="📋 Список пользователей", callback_data="user_list:1:all")],
        [InlineKeyboardButton(text="🔍 Найти по ID", callback_data="find_user_id")],
    ])

def broadcast_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Всем пользователям", callback_data="broadcast_all")],
        [InlineKeyboardButton(text="Только подписчикам", callback_data="broadcast_subs")]
    ])

# === Универсальная функция отправки логов ===
async def send_log_file(message: Message, filename: str):
    try:
        if not os.path.exists(filename):
            await message.answer(f"📜 Файл <b>{filename}</b> отсутствует.", parse_mode="HTML")
            return

        with open(filename, "r", encoding="utf-8") as f:
            content = f.read()

        if not content.strip():
            await message.answer(f"📭 Файл <b>{filename}</b> пуст.", parse_mode="HTML")
            return

        if len(content) > 4000:
            lines = content.strip().split("\n")
            last_lines = "\n".join(lines[-50:])
            await message.answer(f"<b>Последние 50 строк из {filename}:</b>\n\n<code>{last_lines}</code>", parse_mode="HTML")
        else:
            await message.answer(f"<b>Лог {filename}:</b>\n\n<code>{content}</code>", parse_mode="HTML")

    except Exception as e:
        logging.exception(f"Ошибка при отправке {filename}")
        await message.answer(f"❌ Ошибка при чтении {filename}: {e}")

@dp.callback_query(F.data.startswith("user_list"))
async def admin_show_user_list(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.message.answer("❌ Доступ запрещён")
        return

    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 1
    filter_type = parts[2] if len(parts) > 2 else "all"
    per_page = 10
    offset = (page - 1) * per_page

    # 1. Удаляем старые карточки, если были
    old_msgs = admin_last_card_msgs.get(callback.from_user.id, [])
    for msg_id in old_msgs:
        try:
            await callback.bot.delete_message(callback.message.chat.id, msg_id)
        except Exception:
            pass  # иногда сообщения уже удалены
    admin_last_card_msgs[callback.from_user.id] = []

    # SQL фильтр
# 2. Фильтруем юзеров
    if filter_type == "no_sub":
        cursor.execute(
            "SELECT user_id, usage_count, subscribed, subscription_expires FROM users WHERE subscribed = 0 ORDER BY joined_at DESC LIMIT ? OFFSET ?",
            (per_page, offset)
        )
    else:
        cursor.execute(
            "SELECT user_id, usage_count, subscribed, subscription_expires FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
            (per_page, offset)
        )

    users = cursor.fetchall()
    if not users:
        await callback.message.edit_text(f"Пользователей не найдено на этой странице (страница {page}).", reply_markup=None)
        await callback.answer()
        return

    # 3. Навигация и фильтры
    nav_buttons = [
        InlineKeyboardButton(text="⬅️", callback_data=f"user_list:{max(1, page-1)}:{filter_type}"),
        InlineKeyboardButton(text="➡️", callback_data=f"user_list:{page+1}:{filter_type}"),
        InlineKeyboardButton(text="Все", callback_data="user_list:1:all"),
        InlineKeyboardButton(text="Без подписки", callback_data="user_list:1:no_sub"),
    ]
    nav_keyboard = InlineKeyboardMarkup(inline_keyboard=[nav_buttons[i:i+2] for i in range(0, len(nav_buttons), 2)])
    text = f"👥 <b>Пользователи — страница {page}</b>\nФильтр: <b>{'Без подписки' if filter_type == 'no_sub' else 'Все'}</b>\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=nav_keyboard)
    await callback.answer()

    # 4. Карточки юзеров (старые + новые)
    #  (Сначала новые, потом старые: самые новые сверху, самые ранние — внизу)
    msg_ids = []
    for user_id, usage_count, subscribed, expires in users:
        sub_status = "🟢 Активна" if subscribed else "🔴 Нет подписки"
        user_text = f"👤 <b>ID:</b> <code>{user_id}</code>\nЗапросов: <b>{usage_count}</b>\nПодписка: {sub_status}"
        if subscribed and expires:
            user_text += f"\nДо: <b>{expires}</b>"
        keyboard = None
        if not subscribed:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton("✅ Открыть подписку", callback_data=f"activate_user_{user_id}")]]
            )
        # лимит 0.07 чтобы Telegram не ругался (можно уменьшить до 0.03)
        m = await callback.message.answer(user_text, parse_mode="HTML", reply_markup=keyboard)
        msg_ids.append(m.message_id)
        await asyncio.sleep(0.07)
    # Сохраняем id карточек для автоудаления при перелистывании
    admin_last_card_msgs[callback.from_user.id] = msg_ids


# Поиск по ID
@dp.callback_query(F.data == "find_user_id")
async def start_find_user_id(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.awaiting_user_id)
    await callback.message.answer("Введите ID пользователя для поиска:", reply_markup=ForceReply())
    await callback.answer()

@dp.message(AdminStates.awaiting_user_id)
async def process_find_user_id(message: types.Message, state: FSMContext):
    await state.clear()
    try:
        user_id = int(message.text.strip())
        cursor.execute("SELECT user_id, usage_count, subscribed, subscription_expires FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            await message.answer("Пользователь не найден.")
            return
        user_id, usage_count, subscribed, expires = row
        sub_status = "🟢 Активна" if subscribed else "🔴 Нет подписки"
        text = f"👤 <b>ID:</b> <code>{user_id}</code>\nЗапросов: <b>{usage_count}</b>\nПодписка: {sub_status}"
        if subscribed and expires:
            text += f"\nДо: <b>{expires}</b>"
        keyboard = None
        if not subscribed:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Открыть подписку", callback_data=f"activate_user_{user_id}")]]
            )
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        await message.answer(f"❌ Ошибка поиска: {e}")

# === Команды логов ===
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

# === Кнопки логов ===
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
    log_admin_action(callback.from_user.id, "Просмотрел webhook.log")
    await send_log_file(callback.message, "webhook.log")
    await callback.answer()

@dp.callback_query(F.data == "clear_logs")
async def cb_clear_logs(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.message.answer("❌ Доступ запрещён")
        return

    for log_file in ["webhook.log", "errors.log", "admin.log"]:
        try:
            if os.path.exists(log_file):
                open(log_file, "w", encoding="utf-8").close()
        except Exception as e:
            logging.warning(f"Не удалось очистить {log_file}: {e}")

    log_admin_action(callback.from_user.id, "Очистил все логи")
    await callback.message.answer("🧹 Логи очищены.")
    await callback.answer()


@dp.callback_query(F.data == "start_broadcast")
async def initiate_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        await callback.message.answer("❌ Доступ запрещён")
        return
    await state.set_state(AdminStates.awaiting_broadcast_content)
    await callback.message.answer("📢 Введите сообщение или прикрепите файл/изображение для рассылки:")
    await callback.answer()

@dp.message(AdminStates.awaiting_broadcast_content)
async def process_broadcast_content(message: Message, state: FSMContext):
    await state.clear()
    await state.clear()
    cursor.execute("SELECT user_id FROM users WHERE subscribed = 1")
    users = [row[0] for row in cursor.fetchall()]

    success, failed = 0, 0

    for user_id in users:
        try:
            if message.photo:
                photo = message.photo[-1].file_id
                await bot.send_photo(user_id, photo, caption=message.caption or "")
            elif message.document:
                file = message.document.file_id
                await bot.send_document(user_id, file)
            elif message.text:
                await bot.send_message(user_id, message.text)
            else:
                continue  # игнорировать неподдерживаемые типы
            await asyncio.sleep(0.1)  # задержка чтобы не спамить
            success += 1
        except Exception as e:
            # Записываем ошибку в broadcast.log
            with open("broadcast.log", "a", encoding="utf-8") as logf:
                logf.write(f"[Broadcast Error] User {user_id}: {e}\n")
            failed += 1

    log_admin_action(
        message.from_user.id,
        f"Выполнил рассылку. Успешно: {success}, Ошибок: {failed}"
    )
    await message.answer(
        f"✅ Рассылка завершена.\n\n📬 Успешно: {success}\n❌ Ошибок: {failed}"
    )

@dp.message(Command("cancel"), AdminStates.awaiting_broadcast_content)
async def cancel_broadcast(message: Message, state: FSMContext):
    await state.clear()
    await state.clear()
    await message.answer("❌ Рассылка отменена.")

@dp.message(F.text.in_(["⚙️ Админка", "админ", "Админ", "admin", "Admin"]))
async def alias_admin_panel(message: Message):
    await admin_panel(message)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    await admin_panel(message)

# === Callback обработчик кнопки Криптобота === 

@dp.callback_query(lambda c: c.data.startswith("activate_user_"))
async def activate_user_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администратора!", show_alert=True)
        return

    try:
        user_id = int(callback.data.replace("activate_user_", ""))
        activate_subscription(user_id)
        await callback.message.edit_reply_markup()  # убираем кнопку
        await callback.message.answer(f"✅ Подписка активирована для <code>{user_id}</code>!", parse_mode="HTML")
        await bot.send_message(user_id, "🎉 Ваша подписка активирована администратором! Спасибо за оплату.")
        logging.info(f"[ADMIN] Подписка вручную открыта для {user_id} (через inline)")
        await callback.answer("Готово! Подписка активирована.")
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

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

# === Оплата подписки пользователем === 
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
        await message.answer(
            "⏳ После оплаты администратор вручную активирует вашу подписку.\n"
            "Если хотите ускорить процесс — напишите админу свой ID!"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка создания подписки: {e}")
 

# ========== ТЕСТОВАЯ АКТИВАЦИЯ ==========
@dp.message(Command("testpay"))
async def test_payment(message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("❌ Только для администратора!")
        return
    activate_subscription(user_id)
    await message.answer("✅ Тестовая оплата прошла! Подписка активирована на 30 дней.")
    logging.info(f"🚦 [TESTPAY] Подписка активирована вручную для {user_id}")

        
@dp.message(Command("pending_payments"))
async def show_pending_payments(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён")
        return
    # Загрузить все оплаты
    with open(payments_path, "r", encoding="utf-8") as f:
        payments = json.load(f)
    # Получить всех подписанных пользователей
    cursor.execute("SELECT user_id FROM users WHERE subscribed = 1")
    active_users = set(row[0] for row in cursor.fetchall())
    # Найти тех, у кого есть оплата, но нет подписки
    pending = [p for p in payments if int(p["user_id"]) not in active_users]
    if not pending:
        await message.answer("✅ Нет неоплаченных/неактивированных платежей.")
        return
    msg = "⏳ <b>Ожидают активации:</b>\n" + "\n".join(
        f"• <code>{p['user_id']}</code> — {p['amount']} USDT, invoice: {p['invoice_id']}" for p in pending[-20:]
    )
    await message.answer(msg, parse_mode="HTML")

# === ✍️ Цитаты дня ===

@dp.message(F.text.in_(['✍️ Цитаты дня']))
async def handle_text_generation(message: Message, state: FSMContext):
    await state.clear()
    control_buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="stop_generation")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    await state.set_state("generating_text")
    await message.answer("🔄 Генерация цитаты...", reply_markup=control_buttons)
    await generate_text_logic(message, state)


# === Логика генерации ===

async def generate_text_logic(message: Message, state: FSMContext):
    try:
        user_id = message.from_user.id
        ensure_user(user_id)
        client = text_client

        if client is None:
            await message.answer("❌ Ошибка: AI-клиент не настроен.")
            return

        if str(user_id) != str(ADMIN_ID) and is_limited(user_id):
            await message.answer("🔐 Лимит исчерпан. Купите подписку 💰")
            return

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Напиши вдохновляющую цитату дня"}],
            max_tokens=100,
        )
        text = response.choices[0].message.content.strip()
        await message.answer(f"🗋 Цитата дня:\n{text}")

        if str(user_id) != str(ADMIN_ID):
            increment_usage(user_id)
            cursor.execute(
                "INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)",
                (user_id, "text", "цитата дня")
            )
            conn.commit()

    except Exception as e:
        logging.exception("Ошибка генерации текста:")
        await message.answer(f"❌ Ошибка: {e}")
    finally:
        await state.clear()


# === Управление и отмена ===

@dp.callback_query(F.data == "stop_generation")
async def stop_generation(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("⏹ Генерация остановлена.", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🔙 Возврат в меню", reply_markup=main_menu())
    await callback.answer()

@dp.message(Command("cancel"))
async def cancel_generation(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Генерация отменена.", reply_markup=main_menu())

# === 🌌 Gemini AI — Умный диалог ===

@dp.message(F.text.in_("🌌 Gemini AI"))
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
        return

    try:
        user_id = message.from_user.id
        prompt = message.text.strip()

        if not prompt or len(prompt) < 2:
            await message.answer("❌ Введите более развернутый запрос.")
            return

        ensure_user(user_id)
        client = text_client

        if client is None:
            await message.answer("❌ Ошибка: AI-клиент не настроен.")
            return

        if str(user_id) != str(ADMIN_ID) and is_limited(user_id):
            await message.answer("🔒 Лимит исчерпан. Купите подписку 💰")
            return

        await message.answer("💭 Думаю...")

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
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


# === Обработчик остановки Gemini ===
@dp.callback_query(F.data == "stop_assistant")
async def stop_gemini(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("⏹ Gemini остановлен.", reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🔙 Возврат в меню", reply_markup=main_menu())
    await callback.answer()


# === Gemini Примеры и обработка ===

# === 🌠 Gemini Примеры ===
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
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="stop_assistant"),
         InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ]
    await message.answer("🌠 Выберите пример для Gemini:", reply_markup=InlineKeyboardMarkup(inline_keyboard=examples))
    await state.set_state(StateAssistant.dialog)


@dp.callback_query()
async def gemini_dispatch(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    ensure_user(user_id)
    client = text_client

    if client is None:
        await callback.message.answer("❌ AI-клиент не инициализирован.")
        await callback.answer()
        return

    if str(user_id) != str(ADMIN_ID) and is_limited(user_id):
        await callback.message.answer("🔒 Лимит исчерпан. Купите подписку 💰")
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
        "prompt_example": "Придумай интересный промпт для изображения суперкара",
        "random_example": random.choice([
            "Какая погода в Алматы завтра?",
            "Что случилось в мире за последние 24 часа?",
            "Что посмотреть из новых фильмов?",
            "Как заработать в интернете без вложений?"
        ])
    }

    prompt = prompt_map.get(callback.data)
    if not prompt:
        await callback.answer("❌ Пример не найден", show_alert=True)
        return

    try:
        await callback.message.answer("💭 Думаю...")
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )
        reply = response.choices[0].message.content.strip()
        await callback.message.answer(reply)

        if str(user_id) != str(ADMIN_ID):
            increment_usage(user_id)
            cursor.execute(
                "INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)",
                (user_id, "example", prompt)
            )
            conn.commit()

    except Exception as e:
        logging.exception(f"Ошибка при генерации Gemini-ответа для prompt: {prompt}")
        await callback.message.answer(f"❌ Ошибка при генерации ответа: {e}")

    await callback.answer()

# === Endpoint для сайта /generate-image ===
@app.post("/generate-image")
async def generate_image(prompt: str = Form(...)):
    try:
        dalle = await image_client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="hd",
            response_format="url"
        )
        image_url = dalle.data[0].url if dalle and dalle.data else None
        if not image_url:
            return HTMLResponse(content="<b>❌ Не удалось получить изображение.</b>", status_code=500)
        save_image_record(prompt, image_url)
        return HTMLResponse(content=f"""
            <div style='text-align:center'>
                <img src="{image_url}" style="max-width:320px;border-radius:12px;box-shadow:0 4px 18px #673ab722;">
                <br><a href="{image_url}" target="_blank">Скачать</a>
            </div>
        """)
    except Exception as e:
        logging.exception("Ошибка в /generate-image:")
        return HTMLResponse(content=f"<b>❌ Ошибка: {e}</b>", status_code=500)

# === Endpoint для сайта /analyze-image ===

MAX_IMAGE_SIZE_MB = 10  # Максимальный размер файла (например, 10 МБ)
MAX_PROMPT_LEN = 400    # Максимальная длина текста запроса

@app.post("/analyze-image")
async def analyze_image(
    prompt: str = Form(...), 
    file: UploadFile = File(...)
):
    try:
        # Проверка длины prompt
        if len(prompt.strip()) < 2:
            return HTMLResponse(
                "<b>❌ Введите более развёрнутый вопрос.</b>", status_code=400
            )
        if len(prompt) > MAX_PROMPT_LEN:
            return HTMLResponse(
                f"<b>❌ Слишком длинный запрос (максимум {MAX_PROMPT_LEN} символов).</b>", status_code=400
            )

        image_bytes = await file.read()
        if len(image_bytes) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
            return HTMLResponse(
                f"<b>❌ Файл слишком большой (максимум {MAX_IMAGE_SIZE_MB} МБ).</b>", status_code=400
            )

        # Кодируем картинку для передачи в Vision
        b64_image = base64.b64encode(image_bytes).decode()
        data_url = f"data:image/png;base64,{b64_image}"

        # Запрос к OpenAI Vision (gpt-4o)
        vision_response = await image_client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]
            }],
            max_tokens=500
        )

        answer = vision_response.choices[0].message.content.strip()
        return HTMLResponse(content=f"""
            <div style="padding:16px">
                <b>Ваш вопрос:</b> {prompt}<br>
                <b>Ответ:</b> {answer}
            </div>
        """)
    except Exception as e:
        logging.exception("Ошибка в /analyze-image:")
        return HTMLResponse(
            f"<b>❌ Ошибка: {e}</b>", status_code=500
        )

# === Endpoint для сайта /gallery (коллаж) ===
@app.get("/gallery")
async def gallery():
    try:
        with open(images_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        img_tags = ""
        for entry in reversed(data[-9:]):
            url = entry.get("url")
            if url:
                img_tags += f'<img src="{url}" alt="AI Image" />\n'
        return HTMLResponse(img_tags)
    except Exception as e:
        return HTMLResponse(f"<b>Ошибка загрузки галереи: {e}</b>", status_code=500)

