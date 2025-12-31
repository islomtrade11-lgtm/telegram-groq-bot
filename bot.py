import os
import requests
import psycopg2
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.executor import start_webhook

# ========= ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
WEBHOOK_HOST = os.getenv("WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_LOG_CHAT_ID = int(os.getenv("ADMIN_LOG_CHAT_ID", "0"))
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not all([BOT_TOKEN, CHANNEL_USERNAME, WEBHOOK_HOST, GROQ_API_KEY, DATABASE_URL]):
    raise RuntimeError("❌ Missing ENV variables")

# ========= DB =========
conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True

with conn.cursor() as c:
    c.execute("""
        CREATE TABLE IF NOT EXISTS dialog_messages (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

# ========= BOT =========
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

USERS = set()
ADMIN_WAITING_AD = set()
AD_STATS = {"total_ads": 0, "total_delivered": 0, "total_failed": 0}

# ========= KEYBOARDS =========
keyboard_locked = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_locked.add(KeyboardButton("✅ Проверить подписку"))

keyboard_user = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_user.add(
    KeyboardButton("🧠 Помощь"),
    KeyboardButton("ℹ️ О боте"),
    KeyboardButton("🗑 Очистить диалог")
)

keyboard_admin = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_admin.add(
    KeyboardButton("🧠 Помощь"),
    KeyboardButton("ℹ️ О боте"),
    KeyboardButton("🗑 Очистить диалог"),
    KeyboardButton("📢 Создать рекламу"),
    KeyboardButton("📊 Статистика рекламы")
)

def get_keyboard(uid):
    return keyboard_admin if uid in ADMIN_IDS else keyboard_user

# ========= SUBSCRIPTION =========
async def is_subscribed(uid):
    try:
        m = await bot.get_chat_member(CHANNEL_USERNAME, uid)
        return m.status in ("member", "administrator", "creator")
    except:
        return False

async def require_subscription(msg):
    if not await is_subscribed(msg.from_user.id):
        await msg.answer(
            f"🔒 Подпишитесь на канал:\n{CHANNEL_USERNAME}",
            reply_markup=keyboard_locked
        )
        return False
    return True

# ========= AI (АНТИ-ОБРЫВАНИЕ, БЕЗ ИЗМЕНЕНИЙ) =========
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
            if ADMIN_LOG_CHAT_ID:
                bot.loop.create_task(
                    bot.send_message(
                        ADMIN_LOG_CHAT_ID,
                        f"❌ Ошибка ИИ\nUser ID: `{user_id}`\nStatus: {r.status_code}",
                        parse_mode="Markdown"
                    )
                )
            return "⚠️ ИИ временно недоступен"

        answer = r.json()["choices"][0]["message"]["content"]
        save_message(user_id, "user", prompt)
        save_message(user_id, "assistant", answer)

        if ADMIN_LOG_CHAT_ID:
            bot.loop.create_task(
                bot.send_message(
                    ADMIN_LOG_CHAT_ID,
                    f"🧠 Ответ ИИ\nUser ID: `{user_id}`",
                    parse_mode="Markdown"
                )
            )

        return answer

    except Exception as e:
        if ADMIN_LOG_CHAT_ID:
            bot.loop.create_task(
                bot.send_message(
                    ADMIN_LOG_CHAT_ID,
                    f"❌ Exception ИИ\nUser ID: `{user_id}`\n{e}",
                    parse_mode="Markdown"
                )
            )
        return "⚠️ ИИ временно недоступен"

# ========= HANDLERS =========
@dp.message_handler(commands=["start"])
async def start(msg):
    is_new = msg.from_user.id not in USERS
    USERS.add(msg.from_user.id)
    clear_dialog(msg.from_user.id)

    if is_new and ADMIN_LOG_CHAT_ID:
        await bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"🆕 Новый пользователь\nUser ID: `{msg.from_user.id}`",
            parse_mode="Markdown"
        )

    if not await require_subscription(msg):
        return

    await msg.answer("👋 Добро пожаловать!", reply_markup=get_keyboard(msg.from_user.id))

@dp.message_handler(lambda m: m.text == "🗑 Очистить диалог")
async def clear(msg):
    clear_dialog(msg.from_user.id)
    await msg.answer("🧹 Диалог очищен", reply_markup=get_keyboard(msg.from_user.id))

@dp.message_handler(lambda m: m.text == "✅ Проверить подписку")
async def check_sub(msg):
    if await is_subscribed(msg.from_user.id):
        await msg.answer("✅ Подписка подтверждена", reply_markup=get_keyboard(msg.from_user.id))
    else:
        await msg.answer("❌ Вы не подписаны", reply_markup=keyboard_locked)

@dp.message_handler(lambda m: m.text == "📢 Создать рекламу")
async def create_ad(msg):
    if msg.from_user.id not in ADMIN_IDS:
        return
    ADMIN_WAITING_AD.add(msg.from_user.id)
    if ADMIN_LOG_CHAT_ID:
        await bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"📢 Админ начал рассылку\nAdmin ID: `{msg.from_user.id}`",
            parse_mode="Markdown"
        )
    await msg.answer("📢 Пришлите рекламу")

@dp.message_handler(lambda m: m.from_user.id in ADMIN_WAITING_AD, content_types=types.ContentTypes.ANY)
async def send_ad(msg):
    ADMIN_WAITING_AD.discard(msg.from_user.id)
    AD_STATS["total_ads"] += 1

    d = f = 0
    for uid in USERS:
        try:
            await msg.copy_to(uid)
            d += 1
        except:
            f += 1

    AD_STATS["total_delivered"] += d
    AD_STATS["total_failed"] += f

    await msg.answer(f"📢 Отправлено: {d}, ошибки: {f}")

@dp.message_handler(lambda m: m.text == "📊 Статистика рекламы")
async def stats(msg):
    if msg.from_user.id not in ADMIN_IDS:
        return
    await msg.answer(
        f"📊 Кампаний: {AD_STATS['total_ads']}\n"
        f"📬 Доставлено: {AD_STATS['total_delivered']}\n"
        f"❌ Ошибок: {AD_STATS['total_failed']}\n"
        f"👥 Пользователей: {len(USERS)}"
    )

@dp.message_handler(lambda m: m.text == "ℹ️ О боте")
async def about(msg):
    if not await require_subscription(msg):
        return
    await msg.answer(
        "🤖 AI-ассистент\n"
        "🧠 Память в PostgreSQL\n"
        "⚡ Без обрывов ответов\n"
        "📢 Поддерживается рекламой"
    )

@dp.message_handler(lambda m: m.text == "🧠 Помощь")
async def help_msg(msg):
    if not await require_subscription(msg):
        return
    await msg.answer("Просто напишите вопрос 👌")

@dp.message_handler()
async def chat(msg):
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
