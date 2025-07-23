import os
from aiocryptopay import AioCryptoPay, Networks
from dotenv import load_dotenv

load_dotenv()

CRYPTOPAY_API_KEY = os.getenv("CRYPTOPAY_API_KEY")
if not CRYPTOPAY_API_KEY:
    raise ValueError("❌ Не задан CRYPTOPAY_API_KEY в .env")

cryptopay = AioCryptoPay(token=CRYPTOPAY_API_KEY, network=Networks.MAIN_NET)

# === Создание инвойса ===
async def create_invoice(user_id: int) -> str | None:
    if not user_id:
        raise ValueError("❌ user_id не может быть None")
    try:
        invoice = await cryptopay.create_invoice(
            asset="USDT",
            amount="1.00",
            hidden_message="Спасибо за покупку!",
            payload=str(user_id)
        )
        return invoice.bot_invoice_url
    except Exception as e:
        print(f"[CryptoBot] ❌ Ошибка создания инвойса: {e}")
        return None
