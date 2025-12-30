import os
import requests
from collections import defaultdict, deque
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.executor import start_webhook

# ===== ENV =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
WEBHOOK_HOST = os.getenv("WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_LOG_CHAT_ID = int(os.getenv("ADMIN_LOG_CHAT_ID", "0"))

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

if not BOT_TOKEN or not CHANNEL_USERNAME or not WEBHOOK_HOST or not GROQ_API_KEY:
    raise RuntimeError("❌ Missing ENV variables")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===== ХРАНИЛИЩА =====
USERS = set()
ADMIN_WAITING_AD = set()
DIALOG_MEMORY = defaultdict(lambda: deque(maxlen=10))  # 5 пар сообщений

AD_STATS = {
    "total_ads": 0,
    "total_delivered": 0,
    "total_failed": 0
}

# ===== КНОПКИ =====
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

# ===== ПОДПИСКА =====
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

# ===== GROQ AI + FALLBACK + LOG =====
def ask_ai(user, prompt: str) -> str:
    if not prompt.strip():
        return "❌ Пустой запрос"

    user_id = user.id
    username = f"@{user.username}" if user.username else "—"

    DIALOG_MEMORY[user_id].append({"role": "user", "content": prompt})
    messages = list(DIALOG_MEMORY[user_id])

    def call(model):
        return requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 600
            },
            timeout=40
        )

    # 1️⃣ пробуем 70B
    try:
        r = call("llama-3.1-70b-versatile")
        if r.status_code == 200:
            answer = r.json()["choices"][0]["message"]["content"]
            DIALOG_MEMORY[user_id].append({"role": "assistant", "content": answer})

            if ADMIN_LOG_CHAT_ID:
                bot.loop.create_task(
                    bot.send_message(
                        ADMIN_LOG_CHAT_ID,
                        f"🧠 *Ответ ИИ*\n"
                        f"Модель: *70B*\n"
                        f"User ID: `{user_id}`\n"
                        f"Username: {username}",
                        parse_mode="Markdown"
                    )
                )
            return answer
    except:
        pass

    # 2️⃣ fallback 8B
    try:
        r = call("llama-3.1-8b-instant")
        if r.status_code == 200:
            answer = r.json()["choices"][0]["message"]["content"]
            DIALOG_MEMORY[user_id].append({"role": "assistant", "content": answer})

            if ADMIN_LOG_CHAT_ID:
                bot.loop.create_task(
                    bot.send_message(
                        ADMIN_LOG_CHAT_ID,
                        f"⚡ *Ответ IИ (fallback)*\n"
                        f"Модель: *8B*\n"
                        f"User ID: `{user_id}`\n"
                        f"Username: {username}",
                        parse_mode="Markdown"
                    )
                )
            return answer

        return "❌ Временная ошибка ИИ, попробуйте позже"

    except Exception:
        return "❌ Временная ошибка ИИ, попробуйте позже"

# ===== ХЭНДЛЕРЫ =====
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    USERS.add(message.from_user.id)
    DIALOG_MEMORY[message.from_user.id].clear()

    if not await require_subscription(message):
        return

    await message.answer(
        "👋 Добро пожаловать!\nМожете задавать вопросы 👇",
        reply_markup=get_keyboard(message.from_user.id)
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
        "📊 *Статистика рекламы*\n\n"
        f"📢 Кампаний: {AD_STATS['total_ads']}\n"
        f"📬 Всего доставлено: {AD_STATS['total_delivered']}\n"
        f"❌ Всего ошибок: {AD_STATS['total_failed']}\n"
        f"👥 Пользователей: {len(USERS)}",
        parse_mode="Markdown"
    )

@dp.message_handler(lambda m: m.text == "ℹ️ О боте")
async def about(message: types.Message):
    if not await require_subscription(message):
        return
    await message.answer(
        "🤖 *AI-ассистент нового поколения*\n\n"
        "🧠 Работает на LLaMA 3.1 (Groq)\n"
        "⚡ Использует мощную модель 70B с авто-fallback\n"
        "💬 Запоминает контекст диалога\n"
        "📢 Поддерживается рекламой",
        parse_mode="Markdown"
    )

@dp.message_handler(lambda m: m.text == "🧠 Помощь")
async def help_msg(message: types.Message):
    if not await require_subscription(message):
        return
    await message.answer("Просто напиши любой вопрос 👌")

@dp.message_handler()
async def chat(message: types.Message):
    USERS.add(message.from_user.id)
    if not await require_subscription(message):
        return

    await message.answer("⏳ Думаю...")
    await message.answer(ask_ai(message.from_user, message.text))

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
