"""
Run this ONCE in your terminal to authenticate with Telegram.
After this succeeds, run main.py normally.
"""
import asyncio
from telethon import TelegramClient
import config

async def auth():
    client = TelegramClient("tg_session", config.API_ID, config.API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already logged in as: {me.username or me.phone}")
    else:
        # Retry send_code_request on AuthRestartError
        for attempt in range(3):
            try:
                await client.send_code_request(config.PHONE_NUMBER)
                break
            except Exception as e:
                if "AuthRestart" in type(e).__name__ and attempt < 2:
                    print("Telegram restarting auth, retrying...")
                    await asyncio.sleep(2)
                else:
                    raise

        code = input("Enter the Telegram code sent to your phone: ")
        try:
            await client.sign_in(config.PHONE_NUMBER, code)
        except Exception as e:
            if "SessionPasswordNeeded" in type(e).__name__:
                print("2FA is enabled on your account.")
                print("Enter your Telegram 2FA password (Settings → Privacy → Two-Step Verification):")
                password = input("2FA password: ")
                await client.sign_in(password=password)
            else:
                raise
        me = await client.get_me()
        print(f"Successfully logged in as: {me.username or me.phone}")

    await client.disconnect()
    print("Session saved. You can now run: python main.py")

asyncio.run(auth())
