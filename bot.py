import os
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.executor import start_webhook

# ===== ENV =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")   # @your_channel
WEBHOOK_HOST = os.getenv("WEBHOOK_URL")            # https://xxx.onrender.com
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN or not CHANNEL_USERNAME or not WEBHOOK_HOST or not GROQ_API_KEY:
    raise RuntimeError("❌ Missing ENV variables")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===== КНОПКИ =====
keyboard_locked = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_locked.add(KeyboardButton("✅ Проверить подписку"))

keyboard_open = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_open.add(
    KeyboardButton("🧠 Помощь"),
    KeyboardButton("ℹ️ О боте")
)

# ===== ПРОВЕРКА ПОДПИСКИ =====
async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except:
        return False

async def require_subscription(message: types.Message) -> bool:
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            f"🔒 Для доступа подпишитесь на канал:\n"
            f"{CHANNEL_USERNAME}\n\n"
            f"После подписки нажмите кнопку ниже 👇",
            reply_markup=keyboard_locked
        )
        return False
    return True

# ===== GROQ AI (СТАБИЛЬНО) =====
def ask_mistral(prompt: str) -> str:
    if not prompt.strip():
        return "❌ Пустой запрос"

    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "llama3-8b-8192",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 512
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)

        if r.status_code != 200:
            return f"❌ Ошибка ИИ ({r.status_code}): {r.text}"

        data = r.json()
        return data["choices"][0]["message"]["content"]

    except Exception as e:
        return f"❌ Ошибка ИИ: {e}"

# ===== ХЭНДЛЕРЫ =====
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if not await require_subscription(message):
        return

    await message.answer(
        "👋 Добро пожаловать!\n"
        "Можете задавать любой вопрос 👇",
        reply_markup=keyboard_open
    )

@dp.message_handler(lambda m: m.text == "✅ Проверить подписку")
async def check_sub(message: types.Message):
    if await is_subscribed(message.from_user.id):
        await message.answer(
            "✅ Подписка подтверждена!\nМожете пользоваться ботом 🤖",
            reply_markup=keyboard_open
        )
    else:
        await message.answer(
            f"❌ Вы ещё не подписались:\n{CHANNEL_USERNAME}",
            reply_markup=keyboard_locked
        )

@dp.message_handler(lambda m: m.text == "ℹ️ О боте")
async def about(message: types.Message):
    if not await require_subscription(message):
        return

    await message.answer(
        "🤖 Telegram AI бот\n"
        "🧠 Модель: LLaMA 3 (Groq)\n"
        "☁️ Работает бесплатно"
    )

@dp.message_handler(lambda m: m.text == "🧠 Помощь")
async def help_msg(message: types.Message):
    if not await require_subscription(message):
        return

    await message.answer("Просто напиши любой вопрос 👌")

@dp.message_handler()
async def chat(message: types.Message):
    if not await require_subscription(message):
        return

    await message.answer("⏳ Думаю...")
    await message.answer(ask_mistral(message.text))

# ===== WEBHOOK =====
async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(dp):
    await bot.delete_webhook()

if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host="0.0.0.0",
        port=PORT
    )
