from telethon import TelegramClient
from config import API_ID, API_HASH, TELETHON_SESSION_NAME

client = TelegramClient(TELETHON_SESSION_NAME, API_ID, API_HASH)

async def main():
    await client.start()
    me = await client.get_me()
    print(f"OK: @{getattr(me, 'username', None)} id={me.id}")

with client:
    client.loop.run_until_complete(main())
