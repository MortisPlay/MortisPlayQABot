import asyncio
from telegram import Bot

async def check_token():
    bot = Bot(token="8433788739:AAENACNxf5sGFL0t5kWjcoTSfhE7RKywAT0")  # Замени на свой токен
    try:
        result = await bot.get_me()
        print(result)
    except Exception as e:
        print(f"Ошибка: {e}")

if __name__ == "__main__":
    # Используем WindowsSelectorEventLoopPolicy для Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(check_token())
    finally:
        loop.close()