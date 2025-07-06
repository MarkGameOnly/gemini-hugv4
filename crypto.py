import os
from aiocryptopay import AioCryptoPay, Networks
from dotenv import load_dotenv

load_dotenv()

# === Проверка API-ключа ===
CRYPTOPAY_API_KEY = os.getenv("CRYPTOPAY_API_KEY")
if not CRYPTOPAY_API_KEY:
    raise ValueError("❌ Не задан CRYPTOPAY_API_KEY в .env")

# === Инициализация клиента ===
cryptopay = AioCryptoPay(token=CRYPTOPAY_API_KEY, network=Networks.MAIN_NET)

# === Создание инвойса ===
async def create_invoice(user_id: int) -> str | None:
    if not user_id:
        raise ValueError("❌ user_id не может быть None")

    try:
        invoice = await cryptopay.create_invoice(
            asset="USDT",
            amount="1.00",  # обязательно строка!
            hidden_message="Спасибо за покупку!",
            payload=str(user_id)  # payload должен быть строкой
        )
        return invoice.bot_invoice_url  # либо invoice.pay_url
    except Exception as e:
        print(f"[CryptoBot] ❌ Ошибка создания инвойса: {e}")
        return None

# === Проверка Webhook-события ===
async def check_invoice(payload: dict) -> int | None:
    try:
        if payload.get("status") != "paid" or not payload.get("invoice_id"):
            return None
        return int(payload.get("payload"))
    except Exception as e:
        print(f"[CryptoBot] ❌ Ошибка при проверке инвойса: {e}")
        return None
