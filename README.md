# 🤖 Gemini AI Telegram Bot

AI-бизнес в одном боте: генерация изображений, умный чат, подписка и примеры. Работает 24/7, принимает оплату, показывает статистику.

## 🚀 Возможности

- 🎨 Генерация изображений (DALL·E 3)
- 🌌 Умный чат Gemini (тексты, советы, стили)
- 🌠 Примеры для вдохновения с кнопками
- 💰 Подписка через CryptoBot ($1, 10 бесплатных попыток)
- 📊 Админка со статистикой и логами
- 🧠 Команда /cancel, автоотмена, лимиты
- 💾 SQLite-хранилище с историей

## 🛠 Технологии

- Python 3.10+
- aiogram 3.3.0
- FastAPI
- AIOHTTP
- SQLite
- OpenAI API (для изображений и текста)
- CryptoBot API (`aiocryptopay`)

---

## 🧩 Установка

### 1. Клонируй проект
```bash
git clone https://github.com/твоя-ссылка/gemini-bot.git
cd gemini-bot
Установи зависимости
pip install -r requirements.txt

Создай .env файл:
BOT_TOKEN=твой_токен_бота
OPENAI_API_KEY=твой_openai_ключ
CRYPTOBOT_TOKEN=токен_CryptoBot
ADMIN_ID=123456789

🚀 Запуск
Локально:
bash
Копировать
Редактировать
python main.py
Через FastAPI:
bash
Копировать
Редактировать
uvicorn launch:app --host=0.0.0.0 --port=8000

| Файл               | Описание                      |
| ------------------ | ----------------------------- |
| `main.py`          | Логика Telegram-бота          |
| `launch.py`        | Webhook-сервер (FastAPI)      |
| `crypto.py`        | Создание и проверка платежей  |
| `users.db`         | База пользователей и истории  |
| `.env`             | Ключи и настройки             |
| `requirements.txt` | Зависимости проекта           |
| `Procfile`         | Для деплоя на Amvera / Heroku |

👑 Админка

/admin — панель администратора

/logs — посмотреть логи

/broadcast — рассылка по всем пользователям

/export_users — экспорт в CSV

📲 Автор
Telegram: @MarkGameOnly

GitHub: github.com/MarkGameOnly

🔮 Идеи для улучшения
Добавить генерацию видео (Moonvalley / Veo)

Сохранять изображения в облако

Интеграция с Notion / Google Sheets
