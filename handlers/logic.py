# handlers/logic.py
import asyncio

# Пример фоновой задачи — напоминание о подписке
async def check_subscription_reminders():
    while True:
        print("🔔 Проверка напоминаний о подписках...")
        await asyncio.sleep(3600)  # каждую 1 час
