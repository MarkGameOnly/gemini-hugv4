# 📌 Gemini Hug V4 — Telegram AI Бот с подпиской

🧠 Telegram-бот на Python с поддержкой ChatGPT, генерации изображений, платной подпиской через CryptoBot и админкой.

## ✨ Особенности

- 🧠 OpenAI ChatGPT (gpt-3.5-turbo)
- 💪 Два режима генерации текста: Умный помощник и Gemini
- 🌟 Генерация изображений по запросу
- 🔐 10 бесплатных использований, затем подписка
- 🌐 Подписка через CryptoBot с Webhook
- 📊 Админ-панель: статистика, логи, экспорт

---

## ⚙️ Установка

```bash
git clone https://github.com/yourusername/gemini-hugv4.git
cd gemini-hugv4
pip install -r requirements.txt
```

Создай файл `.env`:

```
BOT_TOKEN=123456:ABC...
OPENAI_API_KEY=sk-...
CRYPTOBOT_TOKEN=...
CRYPTOBOT_URL=https://t.me/send?start=IVUYDUSQ5khw
ADMIN_ID=123456789
```

---

## ▶️ Запуск

```bash
python launch.py
```

Или через Uvicorn:

```bash
uvicorn main:app --host=0.0.0.0 --port=8000
```

---

## 👥 Команды в боте

| Команда     | Описание                                 |
|--------------|------------------------------------------|
| `/start`     | Главное меню                      |
| `/help`      | Как использовать                        |
| `/buy`       | Оплата подписки                         |
| `/profile`   | Статус, история, лимиты             |
| `/admin`     | Админ-панель (только для ADMIN_ID) |
| `/stop`      | Выход из режимов генерации     |

---

## 💊 Подписка

- Сразу: 10 бесплатных запросов
- После этого: блокировка без оплаты
- Оплата через: [CryptoBot](https://t.me/send?start=IVUYDUSQ5khw)
- После успешного вебхука активируется подписка на 30 дней

---

## 📊 Админка

- Статистика: сегодня, вчера, за неделю, месяц, год
- Кнопка экспорта пользователей в CSV
- Кнопка просмотра `webhook.log`

---

## 🔒 Структура

```
├── main.py              # Логика бота
├── launch.py            # Старт FastAPI + webhook
├── crypto.py            # CryptoBot API webhook + invoice
├── handlers/            # Обработчики состояний и команд
├── .env                 # Переменные среды
├── users.db             # SQLite база
├── requirements.txt     # Зависимости pip
```

---

## 📄 Лицензия

Проект распространяется под лицензией **MIT License**.

---

Made with ❤️ by [Mark Game Only](https://t.me/shemizarabotkaonlineg)
