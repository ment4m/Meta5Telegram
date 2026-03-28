"""
Run this to find the numeric ID of your private channel.
Usage: python3 find_channel.py
"""
import asyncio
from telethon import TelegramClient
from telethon.tl.types import Channel
import config

async def find():
    client = TelegramClient("tg_session", config.API_ID, config.API_HASH)
    await client.connect()

    print("\nChannels and groups you have access to:\n")
    async for dialog in client.iter_dialogs():
        if isinstance(dialog.entity, Channel):
            print(f"  Name : {dialog.name}")
            print(f"  ID   : {dialog.entity.id}")
            print(f"  User : @{dialog.entity.username or '(private)'}")
            print()

    await client.disconnect()

asyncio.run(find())
