import os
import requests
import psycopg2
import asyncio
from datetime import date
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.executor import start_webhook

# ========= ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
WEBHOOK_HOST = os.getenv("WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PRODIA_API_KEY = os.getenv("PRODIA_API_KEY")

ADMIN_LOG_CHAT_ID = int(os.getenv("ADMIN_LOG_CHAT_ID", "0"))
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

# ========= DB =========
conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True

with conn.cursor() as c:
    # диалоги (БЕЗ ИЗМЕНЕНИЙ)
    c.execute("""
        CREATE TABLE IF NOT EXISTS dialog_messages (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 👇 НОВОЕ (users)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            images_used INT DEFAULT 0,
            images_date DATE
        )
    """)

def get_dialog(user_id, limit=6):
    with conn.cursor() as c:
        c.execute("""
            SELECT role, content FROM dialog_messages
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT %s
        """, (user_id, limit))
        rows = c.fetchall()[::-1]
    return [{"role": r[0], "content": r[1]} for r in rows]

def save_message(user_id, role, content):
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO dialog_messages (user_id, role, content) VALUES (%s,%s,%s)",
            (user_id, role, content)
        )
        c.execute("""
            DELETE FROM dialog_messages
            WHERE id NOT IN (
                SELECT id FROM dialog_messages
                WHERE user_id=%s
                ORDER BY id DESC
                LIMIT 6
            ) AND user_id=%s
        """, (user_id, user_id))

def clear_dialog(user_id):
    with conn.cursor() as c:
        c.execute("DELETE FROM dialog_messages WHERE user_id=%s", (user_id,))

# ========= IMAGE LIMIT =========
def can_generate_image(user_id):
    today = date.today()
    with conn.cursor() as c:
        c.execute("SELECT images_used, images_date FROM users WHERE user_id=%s", (user_id,))
        row = c.fetchone()

        if not row:
            c.execute(
                "INSERT INTO users (user_id, images_used, images_date) VALUES (%s,1,%s)",
                (user_id, today)
            )
            return True, 2

        used, d = row
        if d != today:
            c.execute(
                "UPDATE users SET images_used=1, images_date=%s WHERE user_id=%s",
                (today, user_id)
            )
            return True, 2

        if used >= 3:
            return False, 0

        c.execute(
            "UPDATE users SET images_used=images_used+1 WHERE user_id=%s",
            (user_id,)
        )
        return True, 3 - (used + 1)

# ========= BOT =========
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

USERS = set()
ADMIN_WAITING_AD = set()
AD_STATS = {"total_ads": 0, "total_delivered": 0, "total_failed": 0}
WAITING_IMAGE = set()

# ========= KEYBOARDS =========
keyboard_locked = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_locked.add(KeyboardButton("✅ Проверить подписку"))

keyboard_user = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_user.add(
    KeyboardButton("🧠 Помощь"),
    KeyboardButton("ℹ️ О боте"),
    KeyboardButton("🗑 Очистить диалог"),
    KeyboardButton("🖼 Создать изображение")
)

keyboard_admin = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_admin.add(
    KeyboardButton("🧠 Помощь"),
    KeyboardButton("ℹ️ О боте"),
    KeyboardButton("🗑 Очистить диалог"),
    KeyboardButton("🖼 Создать изображение"),
    KeyboardButton("📢 Создать рекламу"),
    KeyboardButton("📊 Статистика рекламы")
)

BUTTON_TEXTS = {
    "🧠 Помощь",
    "ℹ️ О боте",
    "🗑 Очистить диалог",
    "🖼 Создать изображение",
    "📢 Создать рекламу",
    "📊 Статистика рекламы",
    "✅ Проверить подписку",
}

def get_keyboard(uid):
    return keyboard_admin if uid in ADMIN_IDS else keyboard_user

# ========= SUBSCRIPTION =========
async def is_subscribed(uid):
    if not CHANNEL_USERNAME:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL_USERNAME, uid)
        return m.status in ("member", "administrator", "creator")
    except:
        return False

async def require_subscription(msg):
    if not CHANNEL_USERNAME:
        return True
    if not await is_subscribed(msg.from_user.id):
        await msg.answer("🔒 Подпишитесь на канал", reply_markup=keyboard_locked)
        return False
    return True

# ========= AI =========
def ask_ai(user_id, prompt):
    messages = get_dialog(user_id)
    messages.append({"role": "user", "content": prompt})

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 800
            },
            timeout=40
        )

        if r.status_code != 200:
            raise RuntimeError(r.text)

        answer = r.json()["choices"][0]["message"]["content"]
        save_message(user_id, "user", prompt)
        save_message(user_id, "assistant", answer)
        return answer

    except Exception as e:
        if ADMIN_LOG_CHAT_ID:
            asyncio.create_task(
                bot.send_message(
                    ADMIN_LOG_CHAT_ID,
                    f"❌ Ошибка ИИ\nUser ID: {user_id}\n{e}"
                )
            )
        return "⚠️ ИИ временно недоступен"

# ========= IMAGE =========
import urllib.parse

def generate_image(prompt):
    safe_prompt = urllib.parse.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{safe_prompt}"

# ========= HANDLERS =========
@dp.message_handler(commands=["start"])
async def start(msg):
    USERS.add(msg.from_user.id)
    clear_dialog(msg.from_user.id)
    await msg.answer("👋 Добро пожаловать!", reply_markup=get_keyboard(msg.from_user.id))

@dp.message_handler(lambda m: m.text == "🖼 Создать изображение")
async def image_btn(msg):
    ok, left = can_generate_image(msg.from_user.id)
    if not ok:
        await msg.answer("❌ Лимит 3 изображения в день исчерпан")
        return
    WAITING_IMAGE.add(msg.from_user.id)
    await msg.answer(f"🖼 Опишите изображение\nОсталось сегодня: {left}")

@dp.message_handler(lambda m: m.from_user.id in WAITING_IMAGE)
async def image_prompt(msg):
    WAITING_IMAGE.discard(msg.from_user.id)
    await msg.answer("🎨 Генерирую изображение...")
    url = generate_image(msg.text)
    if not url:
        await msg.answer("❌ Не удалось создать изображение")
        return
    await msg.answer_photo(url)

# ======= СТАРЫЕ ХЕНДЛЕРЫ (1 В 1) =======
@dp.message_handler(lambda m: m.text == "🗑 Очистить диалог")
async def clear(msg):
    clear_dialog(msg.from_user.id)
    await msg.answer("🧹 Диалог очищен", reply_markup=get_keyboard(msg.from_user.id))

@dp.message_handler(lambda m: m.text == "🧠 Помощь")
async def help_msg(msg):
    await msg.answer("Просто напишите вопрос 👌")

@dp.message_handler()
async def chat(msg):
    if msg.text in BUTTON_TEXTS or msg.from_user.id in WAITING_IMAGE:
        return
    USERS.add(msg.from_user.id)
    if not await require_subscription(msg):
        return
    await msg.answer("⏳ Думаю...")
    await msg.answer(ask_ai(msg.from_user.id, msg.text))

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



