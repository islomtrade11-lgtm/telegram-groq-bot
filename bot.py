import os
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.executor import start_webhook

# ========= ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
WEBHOOK_HOST = os.getenv("WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_LOG_CHAT_ID = int(os.getenv("ADMIN_LOG_CHAT_ID", "0"))

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not all([BOT_TOKEN, CHANNEL_USERNAME, WEBHOOK_HOST, GROQ_API_KEY]):
    raise RuntimeError("❌ Missing ENV variables")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ========= STORAGE =========
USERS = set()
ADMIN_WAITING_AD = set()

AD_STATS = {
    "total_ads": 0,
    "total_delivered": 0,
    "total_failed": 0
}

# ========= KEYBOARDS =========
keyboard_locked = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_locked.add(KeyboardButton("✅ Проверить подписку"))

keyboard_user = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_user.add(
    KeyboardButton("🧠 Помощь"),
    KeyboardButton("ℹ️ О боте")
)

keyboard_admin = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_admin.add(
    KeyboardButton("🧠 Помощь"),
    KeyboardButton("ℹ️ О боте"),
    KeyboardButton("📢 Создать рекламу"),
    KeyboardButton("📊 Статистика рекламы")
)

def get_keyboard(user_id):
    return keyboard_admin if user_id in ADMIN_IDS else keyboard_user

# ========= SUBSCRIPTION =========
async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except:
        return False

async def require_subscription(message):
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            f"🔒 Подпишитесь на канал:\n{CHANNEL_USERNAME}\n\n"
            "После подписки нажмите кнопку 👇",
            reply_markup=keyboard_locked
        )
        return False
    return True

# ========= AI (БЕЗ ПАМЯТИ — КЛЮЧЕВО) =========
def ask_ai(user, prompt: str) -> str:
    user_id = user.id
    username = f"@{user.username}" if user.username else "—"

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 300
            },
            timeout=25
        )

        if r.status_code != 200:
            return "⚠️ ИИ временно недоступен, попробуйте позже"

        answer = r.json()["choices"][0]["message"]["content"]

        if ADMIN_LOG_CHAT_ID:
            bot.loop.create_task(
                bot.send_message(
                    ADMIN_LOG_CHAT_ID,
                    f"🧠 Ответ ИИ (8B)\nUser: `{user_id}` {username}",
                    parse_mode="Markdown"
                )
            )

        return answer

    except Exception:
        return "⚠️ ИИ временно недоступен, попробуйте позже"

# ========= HANDLERS =========

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    USERS.add(message.from_user.id)

    if not await require_subscription(message):
        return

    await message.answer(
        "👋 Добро пожаловать!\nМожете задавать вопросы 👇",
        reply_markup=get_keyboard(message.from_user.id)
    )

@dp.message_handler(lambda m: m.text == "✅ Проверить подписку")
async def check_subscription(message: types.Message):
    if await is_subscribed(message.from_user.id):
        await message.answer(
            "✅ Подписка подтверждена!",
            reply_markup=get_keyboard(message.from_user.id)
        )
    else:
        await message.answer(
            f"❌ Вы не подписаны:\n{CHANNEL_USERNAME}",
            reply_markup=keyboard_locked
        )

@dp.message_handler(lambda m: m.text == "📢 Создать рекламу")
async def create_ad(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    ADMIN_WAITING_AD.add(message.from_user.id)
    await message.answer("📢 Скиньте сообщение рекламы")

@dp.message_handler(lambda m: m.from_user.id in ADMIN_WAITING_AD, content_types=types.ContentTypes.ANY)
async def send_ad(message: types.Message):
    ADMIN_WAITING_AD.discard(message.from_user.id)
    AD_STATS["total_ads"] += 1

    delivered = failed = 0
    for uid in USERS:
        try:
            await message.copy_to(uid)
            delivered += 1
        except:
            failed += 1

    AD_STATS["total_delivered"] += delivered
    AD_STATS["total_failed"] += failed

    await message.answer(
        f"✅ Реклама отправлена\n"
        f"📬 Доставлено: {delivered}\n"
        f"❌ Ошибки: {failed}"
    )

@dp.message_handler(lambda m: m.text == "📊 Статистика рекламы")
async def stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    await message.answer(
        f"📊 Статистика\n\n"
        f"📢 Кампаний: {AD_STATS['total_ads']}\n"
        f"📬 Доставлено: {AD_STATS['total_delivered']}\n"
        f"❌ Ошибок: {AD_STATS['total_failed']}\n"
        f"👥 Пользователей: {len(USERS)}"
    )

@dp.message_handler(lambda m: m.text == "ℹ️ О боте")
async def about(message: types.Message):
    if not await require_subscription(message):
        return
    await message.answer(
        "🤖 AI-ассистент\n\n"
        "🧠 Работает на LLaMA 3.1 (Groq)\n"
        "⚡ Стабильный режим без памяти\n"
        "📢 Поддерживается рекламой"
    )

@dp.message_handler(lambda m: m.text == "🧠 Помощь")
async def help_msg(message: types.Message):
    if not await require_subscription(message):
        return
    await message.answer("Просто напиши вопрос 👌")

@dp.message_handler()
async def chat(message: types.Message):
    USERS.add(message.from_user.id)

    if not await require_subscription(message):
        return

    await message.answer("⏳ Думаю...")
    await message.answer(ask_ai(message.from_user, message.text))

# ========= WEBHOOK =========
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
