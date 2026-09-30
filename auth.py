"""Run once in your own terminal to authorize your personal Telegram account."""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


async def main():
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    sessions = ROOT / "sessions"
    sessions.mkdir(exist_ok=True)
    client = TelegramClient(str(sessions / "owner_search"), api_id, api_hash)
    try:
        # Telethon asks for phone, Telegram login code and 2FA password IN YOUR TERMINAL.
        await client.start()
        me = await client.get_me()
        if getattr(me, "bot", False):
            raise RuntimeError("В auth.py войди в обычный аккаунт, не в бота.")
        print(f"Вход выполнен: {me.first_name} (ID {me.id})")
        print("Если это твой аккаунт, его ID можно указать в ADMIN_ID файла .env.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
