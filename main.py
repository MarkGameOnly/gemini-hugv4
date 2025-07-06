# === Часть 1: Импорты, init, FSM, меню ===
import os
import asyncio
import random
import logging
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, Request, APIRouter
from handlers import logic

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

from openai import AsyncOpenAI

from crypto import create_invoice

conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()
FREE_USES_LIMIT = 10

def ensure_user(user_id: int):
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (user_id, usage_count, subscribed, subscription_expires, joined_at) VALUES (?, 0, 0, NULL, ?)",
            (user_id, datetime.now().strftime("%Y-%m-%d")),
        )
        conn.commit()

def is_subscribed(user_id: int) -> bool:
    cursor.execute("SELECT subscribed, subscription_expires FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        return False
    subscribed, expires = result
    if not subscribed or not expires:
        return False
    return datetime.strptime(expires, "%Y-%m-%d") >= datetime.now()

def get_usage_count(user_id: int) -> int:
    cursor.execute("SELECT usage_count FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result[0] if result else 0

def init_db():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        usage_count INTEGER DEFAULT 0,
        subscribed BOOLEAN DEFAULT 0,
        subscription_expires TEXT,
        joined_at TEXT
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
    conn.commit()

init_db()

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID", "1082828397")
DOMAIN_URL = os.getenv("DOMAIN_URL")

session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

class GenStates(StatesGroup):
    await_text = State()
    await_image = State()

class AssistantState(StatesGroup):
    chatting = State()

class StateAssistant(StatesGroup):
    dialog = State()

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Сгенерировать текст"), KeyboardButton(text="🖼 Создать изображение")],
            [KeyboardButton(text="🌌 Gemini AI"), KeyboardButton(text="🌠 Gemini Примеры")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💰 Купить подписку")],
            [KeyboardButton(text="📚 Как пользоваться?"), KeyboardButton(text="📎 Остальные проекты")]
        ],
        resize_keyboard=True
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
        "💡 Для умного помощника используйте 🧠.\n"
        "ℹ️ Подписка дает больше запросов и скорость ответа."
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профиль")
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    ensure_user(user_id)
    cursor.execute("SELECT usage_count, subscribed, subscription_expires FROM users WHERE user_id = ?", (user_id,))
    usage_count, subscribed, expires = cursor.fetchone()

    if str(user_id) == ADMIN_ID:
        sub_status = "🟢 Администратор — доступ всегда активен"
    elif subscribed and expires:
        expires_date = datetime.strptime(expires, "%Y-%m-%d").strftime("%d.%m.%Y")
        sub_status = f"🟢 Активна до {expires_date}"
    else:
        sub_status = "🔴 Нет подписки"

    profile_text = (
        f"🧾 Ваш ID: {user_id}\n"
        f"📊 Генераций: {usage_count}\n"
        f"💼 Подписка: {sub_status}"
    )
    await message.answer(profile_text)

    cursor.execute("SELECT type, prompt, created_at FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        await message.answer("📭 История пуста")
    else:
        history_lines = [f"[{t}] {p[:40]}... ({c[:10]})" for t, p, c in rows]
        await message.answer("🕘 Последние действия:\n" + "\n".join(history_lines))

@dp.message(Command("admin"))
@dp.message(F.text == "📊 Админка")
async def admin_panel(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        await message.answer("❌ Доступ запрещён")
        return

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

    text = f"📈 Админка:\nПодписок активно: {total_subs}\n"
    text += "\n".join([f"{k}: {v}" for k, v in stats.items()])
    await message.answer(text)

@dp.message(F.text == "📎 Остальные проекты")
async def project_links(message: Message):
    buttons = [
        [InlineKeyboardButton(text="🔗 It Market", url="https://t.me/Itmarket1_bot")],
        [InlineKeyboardButton(text="🎮 Игры с заработком", url="https://t.me/One1WinOfficial_bot")],
        [InlineKeyboardButton(text="📱 Мобильные прокси", url="https://t.me/Proxynumber_bot")],
        [InlineKeyboardButton(text="🦁 УБТ Связки", url="https://t.me/LionMarket1_bot")],
        [InlineKeyboardButton(text="🌕 Криптомаркет", url="https://t.me/CryptoMoneyMark_bot")],
        [InlineKeyboardButton(text="🎬 Фильмы и сериалы", url="https://t.me/RedirectIT_bot")],
    ]
    await message.answer("🔗 Мои другие проекты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

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

# === Умный помощник ===
@dp.message(F.text == "🧠 Умный помощник")
async def start_assistant(message: Message, state: FSMContext):
    await state.set_state(AssistantState.chatting)
    await message.answer("🧠 Помощник включен! Напишите ваш вопрос. Чтобы остановить, введите /stop")

@dp.message(Command("stop"))
async def stop_assistant(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🚩 Помощник остановлен. Возвращаю в главное меню.")

@dp.message(AssistantState.chatting)
async def handle_assistant_message(message: Message, state: FSMContext):
    user_input = message.text
    await message.answer("⏳ Думаю...")
    try:
        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Ты умный Telegram-помощник, помогай кратко и понятно."},
                {"role": "user", "content": user_input}
            ],
            temperature=0.8,
            max_tokens=1024,
        )
        ai_reply = response.choices[0].message.content.strip()
        await message.answer(ai_reply)
    except Exception as e:
        await message.answer(f"❌ Ошибка генерации ответа: {e}")

# === Генерация текста ===
@dp.message(F.text == "✍️ Сгенерировать текст")
async def handle_text_generation(message: Message, state: FSMContext):
    await message.answer("🔄 Генерация текста началась. Пожалуйста, подождите...")
    asyncio.create_task(generate_text_logic(message))

async def generate_text_logic(message: Message):
    try:
        user_id = message.from_user.id
        ensure_user(user_id)

        if not is_subscribed(user_id) and get_usage_count(user_id) >= FREE_USES_LIMIT:
            await message.answer("🔐 Лимит исчерпан. Купите подписку для продолжения.")
            return

        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Напиши вдохновляющую цитату"}],
            max_tokens=100,
        )
        text = response.choices[0].message.content.strip()
        await message.answer(f"📝 {text}")

        cursor.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    except Exception as e:
        await message.answer(f"❌ Ошибка генерации текста: {e}")

# === Генерация изображения ===
@dp.message(F.text == "🖼 Создать изображение")
async def handle_image_prompt(message: Message, state: FSMContext):
    await state.set_state(GenStates.await_image)
    await message.answer("🔼️ Напишите промпт для изображения")

@dp.message(GenStates.await_image)
async def generate_image(message: Message, state: FSMContext):
    try:
        user_id = message.from_user.id
        ensure_user(user_id)

        if not is_subscribed(user_id) and get_usage_count(user_id) >= FREE_USES_LIMIT:
            await message.answer("🔐 Лимит исчерпан. Купите подписку для продолжения.")
            return

        prompt = message.text
        await message.answer("🧠 Генерирую изображение...")

        dalle = await client.images.generate(prompt=prompt, model="dall-e-3", n=1, size="1024x1024")
        image_url = dalle.data[0].url

        async with aiohttp.ClientSession() as s:
            async with s.get(image_url) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    await message.answer_photo(types.BufferedInputFile(image_bytes, filename="image.png"))
                else:
                    await message.answer("❌ Не удалось загрузить изображение с DALL-E")

        cursor.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (user_id,))
        cursor.execute("INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)", (user_id, "image", prompt))
        conn.commit()

        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# === Часть 3: Gemini AI + Примеры + Webhook ===

@dp.message(F.text == "🌌 Gemini AI")
async def start_gemini_dialog(message: Message, state: FSMContext):
    await message.answer("🌌 Добро пожаловать в режим Gemini! Напиши свой вопрос или запрос:")
    await state.set_state(StateAssistant.dialog)

@dp.message(StateAssistant.dialog)
async def handle_gemini_dialog(message: Message, state: FSMContext):
    user_id = message.from_user.id
    ensure_user(user_id)
    if str(user_id) != ADMIN_ID and not is_subscribed(user_id) and get_usage_count(user_id) >= FREE_USES_LIMIT:
        await message.answer("🔒 Лимит исчерпан. Купите подписку 💰")
        return

    try:
        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": message.text}]
        )
        reply = response.choices[0].message.content
        await message.answer(reply)
        cursor.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (user_id,))
        cursor.execute("INSERT INTO history (user_id, type, prompt) VALUES (?, ?, ?)", (user_id, "gemini", message.text))
        conn.commit()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(F.text == "🌠 Gemini Примеры")
async def gemini_examples(message: Message, state: FSMContext):
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
         InlineKeyboardButton(text="Заработок", callback_data="money_example")]
    ]
    extra_buttons = [[InlineKeyboardButton(text="🌹 Случайный", callback_data="random_example")],
                     [InlineKeyboardButton(text="🔄 Новый запрос", callback_data="new_query")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=examples + extra_buttons)
    await message.answer("🌠 Выберите пример или задайте свой вопрос:", reply_markup=keyboard)
    await state.set_state(StateAssistant.dialog)

@dp.callback_query(F.data == "random_example")
async def gemini_random_example(callback: types.CallbackQuery, state: FSMContext):
    examples = [
        "img_landscape", "img_anime_girl", "img_fantasy_city", "img_modern_office",
        "img_food_dessert", "img_luxury_car", "img_loft_interior", "weather_example",
        "news_example", "movies_example", "money_example", "prompt_example"
    ]
    await gemini_dispatch(callback, state, random.choice(examples))

@dp.callback_query(F.data == "new_query")
async def gemini_new_query(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("✏️ Введите свой вопрос или тему")
    await state.set_state(StateAssistant.dialog)
    await callback.answer()

@dp.callback_query()
async def gemini_dispatch(callback: types.CallbackQuery, state: FSMContext, example_id=None):
    data_id = example_id or callback.data
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
    prompt = prompt_map.get(data_id)
    if prompt:
        await handle_gemini_dialog(types.Message(message_id=callback.message.message_id, from_user=callback.from_user, text=prompt), state)
    await callback.answer()

router = APIRouter()

@router.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return {"ok": True}

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
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

    asyncio.create_task(logic.check_subscription_reminders())

    yield

    await session.close()

app = FastAPI(lifespan=lifespan)
app.include_router(router)

@app.get("/")
async def root():
    return {"status": "ok"}