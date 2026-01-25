import os
import requests
import psycopg2
import asyncio
from io import BytesIO
from urllib.parse import quote
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.executor import start_webhook

# ========= ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
WEBHOOK_HOST = os.getenv("WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
OCR_API_KEY = os.getenv("OCR_API_KEY")

# ========= DB SETTINGS =========
DIALOG_LIMIT = int(os.getenv("DIALOG_LIMIT", "40"))          # сколько сообщений хранить на пользователя
DIALOG_TTL_DAYS = int(os.getenv("DIALOG_TTL_DAYS", "30"))   # удалять старше N дней (0 = выключить)
DB_LIMIT_MB = float(os.getenv("DB_LIMIT_MB", "512"))        # для процентов (Neon Free ~512MB)
SUMMARY_TRIGGER = int(os.getenv("SUMMARY_TRIGGER", "18"))  # когда делать summary (если сообщений стало больше)
SUMMARY_KEEP_LAST = int(os.getenv("SUMMARY_KEEP_LAST", "8"))  # сколько последних сообщений оставлять поверх summary

# ========= ADMINS =========
ADMIN_LOG_CHAT_ID = int(os.getenv("ADMIN_LOG_CHAT_ID", "0"))
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

# ========= DB =========
conn = None

def get_conn():
    global conn
    if conn is None or conn.closed != 0:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        conn.autocommit = True
    return conn


def init_db():
    with get_conn().cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS dialog_messages (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS dialog_summary (
                user_id BIGINT PRIMARY KEY,
                summary TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_dialog_user_id_id
            ON dialog_messages (user_id, id DESC)
        """)

# вызываем один раз при старте
init_db()

# ========= DIALOG =========
def cleanup_dialog(user_id: int):
    """Оставляем только последние DIALOG_LIMIT + удаляем старше TTL."""
    with get_conn().cursor() as c:
        # лимит по количеству (оставляем последние DIALOG_LIMIT)
        c.execute("""
            DELETE FROM dialog_messages
            WHERE user_id=%s AND id NOT IN (
                SELECT id FROM dialog_messages
                WHERE user_id=%s
                ORDER BY id DESC
                LIMIT %s
            )
        """, (user_id, user_id, DIALOG_LIMIT))

        # генеральная чистка по времени
        if DIALOG_TTL_DAYS > 0:
            c.execute("""
                DELETE FROM dialog_messages
                WHERE user_id=%s
                  AND created_at < NOW() - (%s || ' days')::interval
            """, (user_id, DIALOG_TTL_DAYS))

def get_dialog(user_id, limit=None):
    if limit is None:
        limit = DIALOG_LIMIT

    with get_conn().cursor() as c:
        c.execute("""
            SELECT role, content FROM dialog_messages
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT %s
        """, (user_id, limit))
        rows = c.fetchall()[::-1]
    return [{"role": r[0], "content": r[1]} for r in rows]

def save_message(user_id, role, content):
    with get_conn().cursor() as c:
        c.execute(
            "INSERT INTO dialog_messages (user_id, role, content) VALUES (%s,%s,%s)",
            (user_id, role, content)
        )
    cleanup_dialog(user_id)

def get_summary(user_id: int) -> str:
    with get_conn().cursor() as c:
        c.execute("SELECT summary FROM dialog_summary WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        return row[0] if row and row[0] else ""


def save_summary(user_id: int, summary: str):
    with get_conn().cursor() as c:
        c.execute("""
            INSERT INTO dialog_summary (user_id, summary, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET summary=EXCLUDED.summary, updated_at=NOW()
        """, (user_id, summary))


def clear_dialog(user_id):
    with get_conn().cursor() as c:
        c.execute("DELETE FROM dialog_messages WHERE user_id=%s", (user_id,))

# ========= IMAGE (FREE, NO LIMIT) =========
def build_image_prompt(user_prompt: str) -> str:
    # общий “усилитель” качества
    quality = (
        "masterpiece, best quality, high detail, sharp focus, 4k, ultra realistic lighting, "
        "clean composition, cinematic, natural colors, empty bottom area, clean background"

    )

    # негативный промпт (убираем мусор)
    negative = (
    "bad quality, lowres, blurry, pixelated, deformed, distorted, ugly, "
    "extra fingers, bad hands, bad anatomy, disfigured face, "
    "text, watermark, logo, caption, signature, frame, "
    "letters, typography, brand name, stamp, overlay"
    )

    # стиль по умолчанию (реалистичный)
    style = "professional photo"

    final = (
    "NEW REQUEST. IGNORE ALL PREVIOUS PROMPTS. "
    f"no watermark, no text, no logo, {style}, {user_prompt}, {quality}. "
    f"Negative prompt: {negative}."
    )

    return final[:900]  # ограничим длину, чтобы генератор не тупил


def generate_image(prompt: str):
    better_prompt = build_image_prompt(prompt)
    return f"https://image.pollinations.ai/prompt/{quote(better_prompt)}"

# ========= AI ANSWERS BY PHOTO (OCR) =========
def ocr_image_bytes(image_bytes: bytes) -> str:
    if not OCR_API_KEY:
        return ""

    files = {
        "filename": ("image.jpg", image_bytes)
    }

    data = {
        "apikey": OCR_API_KEY,
        "language": "rus",  # можно поменять на "eng" если часто английский
        "isOverlayRequired": False
    }

    r = requests.post(
        "https://api.ocr.space/parse/image",
        files=files,
        data=data,
        timeout=60
    )

    if r.status_code != 200:
        return ""

    j = r.json()
    if j.get("IsErroredOnProcessing"):
        return ""

    parsed = j.get("ParsedResults", [])
    if not parsed:
        return ""

    return parsed[0].get("ParsedText", "").strip()

# ========= BOT =========
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

USERS = set()
ADMIN_WAITING_AD = set()
WAITING_IMAGE = set()

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
    KeyboardButton("🗑 Очистить диалог"),
    KeyboardButton("🖼 Создать изображение")
)

keyboard_admin = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard_admin.add(
    KeyboardButton("🧠 Помощь"),
    KeyboardButton("🗑 Очистить диалог"),
    KeyboardButton("🖼 Создать изображение"),
    KeyboardButton("📢 Создать рекламу"),
    KeyboardButton("📊 Статистика рекламы"),
    KeyboardButton("📊 База данных")
)

def get_keyboard(uid):
    return keyboard_admin if uid in ADMIN_IDS else keyboard_user
    
# ========= DB ADMIN PANEL =========
def get_db_stats():
    """
    rows_count, users_count, used_mb, used_percent
    """
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM dialog_messages;")
        rows_count = int(c.fetchone()[0])

        c.execute("SELECT COUNT(DISTINCT user_id) FROM dialog_messages;")
        users_count = int(c.fetchone()[0])

        c.execute("SELECT pg_total_relation_size('dialog_messages');")
        size_bytes = int(c.fetchone()[0])

    used_mb = size_bytes / (1024 * 1024)
    used_percent = (used_mb / DB_LIMIT_MB) * 100 if DB_LIMIT_MB > 0 else 0.0
    return rows_count, users_count, used_mb, used_percent


def db_inline_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🧹 Очистить базу", callback_data="db_clear"))
    return kb


def db_confirm_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Да, очистить", callback_data="db_clear_confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data="db_clear_cancel")
    )
    return kb

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
    cleanup_dialog(user_id)

    # 1) берём summary + последние сообщения
    summary = get_summary(user_id)
    dialog = get_dialog(user_id)

    # 2) если диалог стал слишком большим — обновляем summary
    # (берём более широкий кусок, чем SUMMARY_KEEP_LAST)
    if len(dialog) >= SUMMARY_TRIGGER:
        try:
            old_summary = summary
            new_summary = make_summary_with_ai(user_id, dialog, old_summary)
            save_summary(user_id, new_summary)

            # после summary оставляем только последние SUMMARY_KEEP_LAST сообщений
            dialog = dialog[-SUMMARY_KEEP_LAST:]
            summary = new_summary
        except:
            # если summary не получилось — просто продолжаем без него
            pass

    # 3) собираем финальный контекст для ИИ
    messages = []

    if summary.strip():
        messages.append({
            "role": "system",
            "content": f"Память диалога (summary): {summary}"
        })

    messages.extend(dialog)
    messages.append({"role": "user", "content": prompt})


    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
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
                    f"❌ Ошибка ИИ\nUser ID: {user_id}\n{repr(e)}"
                )
            )
        return "⚠️ ИИ временно недоступен"

def make_summary_with_ai(user_id: int, dialog: list, old_summary: str) -> str:
    """
    dialog: список [{"role": "...", "content": "..."}]
    old_summary: старое summary (может быть пустым)
    """
    system_text = (
        "Ты делаешь краткую память диалога для помощника.\n"
        "Сохрани только важное:\n"
        "- факты о пользователе\n"
        "- цели/задачи\n"
        "- важные решения/договоренности\n"
        "- предпочтения (язык, формат)\n"
        "Не добавляй лишний текст.\n"
        "Ответ только чистым текстом (без списков если можно).\n"
        "Максимум 800 символов."
    )

    messages = [{"role": "system", "content": system_text}]
    if old_summary.strip():
        messages.append({"role": "user", "content": f"Текущее summary:\n{old_summary}"})

    messages.append({"role": "user", "content": f"Обнови summary по новым сообщениям:\n{dialog}"})

    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 250
        },
        timeout=40
    )

    if r.status_code != 200:
        raise RuntimeError(r.text)

    return r.json()["choices"][0]["message"]["content"].strip()

# ========= HANDLERS =========
@dp.message_handler(commands=["start"])
async def start(msg):
    is_new = msg.from_user.id not in USERS
    USERS.add(msg.from_user.id)
    clear_dialog(msg.from_user.id)

    if is_new and ADMIN_LOG_CHAT_ID:
        await bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"👤 Новый пользователь\nID: {msg.from_user.id}\n@{msg.from_user.username}"
        )

    await msg.answer(
    "👋 Добро пожаловать!\n\n"
    "📄 Инструкция и описание бота:\n"
    "https://telegra.ph/Nora-AI-01-04\n\n"
    "Готов к работе 👇",
    reply_markup=get_keyboard(msg.from_user.id),
    disable_web_page_preview=True
)

@dp.message_handler(lambda m: m.text == "🖼 Создать изображение")
async def image_btn(msg):
    WAITING_IMAGE.add(msg.from_user.id)
    await msg.answer("🖼 Напишите описание изображения")

@dp.message_handler(lambda m: m.from_user.id in WAITING_IMAGE)
async def image_prompt(msg):
    WAITING_IMAGE.discard(msg.from_user.id)

    try:
        await msg.answer_photo(generate_image(msg.text))
    except Exception:
        await msg.answer(
            "⚠️ Сейчас не получилось создать изображение.\n"
            "Попробуйте ещё раз или измените запрос (например: «реалистично», «аниме», «логотип»)."
        )

@dp.message_handler(lambda m: m.text == "🗑 Очистить диалог")
async def clear(msg):
    clear_dialog(msg.from_user.id)
    await msg.answer("🧹 Диалог очищен", reply_markup=get_keyboard(msg.from_user.id))

@dp.message_handler(lambda m: m.text == "🧠 Помощь")
async def help_msg(msg):
    await msg.answer("Просто напишите вопрос 👌")
    
@dp.message_handler(lambda m: m.text == "📊 База данных")
async def admin_db_stats(msg):
    if msg.from_user.id not in ADMIN_IDS:
        return

    rows_count, users_count, used_mb, used_percent = get_db_stats()

    text = (
        "📊 База данных\n\n"
        f"🧾 Сообщений в истории: {rows_count}\n"
        f"👥 Пользователей с историей: {users_count}\n"
        f"📦 Заполнено: {used_percent:.2f}% ({used_mb:.2f}MB из {DB_LIMIT_MB:.0f}MB)\n\n"
        "Нажмите кнопку ниже, чтобы очистить историю чатов."
    )

    await msg.answer(text, reply_markup=db_inline_kb())
    
@dp.callback_query_handler(lambda c: c.data == "db_clear")
async def db_clear_ask_confirm(call):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return

    await call.message.edit_text(
        "⚠️ Вы уверены?\n\n"
        "Это удалит всю историю чатов.\n"
        "Действие нельзя отменить.",
        reply_markup=db_confirm_kb()
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "db_clear_cancel")
async def db_clear_cancel(call):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return

    rows_count, users_count, used_mb, used_percent = get_db_stats()

    text = (
        "📊 База данных\n\n"
        f"🧾 Сообщений в истории: {rows_count}\n"
        f"👥 Пользователей с историей: {users_count}\n"
        f"📦 Заполнено: {used_percent:.2f}% ({used_mb:.2f}MB из {DB_LIMIT_MB:.0f}MB)\n\n"
        "Очистка отменена ✅"
    )

    await call.message.edit_text(text, reply_markup=db_inline_kb())
    await call.answer("Отменено ✅")


@dp.callback_query_handler(lambda c: c.data == "db_clear_confirm")
async def db_clear_confirm(call):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return

    # чистим ТОЛЬКО историю диалогов
    with conn.cursor() as c:
        c.execute("TRUNCATE TABLE dialog_messages RESTART IDENTITY;")

    await call.message.edit_text("✅ История чатов очищена.")

    # уведомление пользователям (тем, кто сейчас известен в USERS)
    notify_text = "🧹 Произошла очистка истории чата. Можете продолжать 🙂"

    ok = 0
    bad = 0
    for uid in list(USERS):
        try:
            await bot.send_message(uid, notify_text)
            ok += 1
        except:
            bad += 1

    await bot.send_message(
        call.from_user.id,
        f"📣 Уведомления отправлены.\n✅ Успешно: {ok}\n⚠️ Не дошло: {bad}"
    )
    await call.answer("Готово ✅")



@dp.message_handler(lambda m: m.text == "📢 Создать рекламу")
async def create_ad(msg):
    if msg.from_user.id not in ADMIN_IDS:
        return

    ADMIN_WAITING_AD.add(msg.from_user.id)

    if ADMIN_LOG_CHAT_ID:
        await bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"📢 Админ начал создание рекламы\nAdmin ID: {msg.from_user.id}"
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

    if ADMIN_LOG_CHAT_ID:
        await bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"📤 Реклама разослана\nАдмин: {msg.from_user.id}\nДоставлено: {d}\nОшибки: {f}"
        )

    await msg.answer(f"📢 Отправлено: {d}\n❌ Ошибки: {f}")

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

@dp.message_handler(content_types=types.ContentTypes.PHOTO)
async def photo_auto(msg: types.Message):
    caption = (msg.caption or "").strip()

    try:
        # берём самое большое фото
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)

        # если нет подписи — спросим что делать
        if not caption:
            await msg.answer(
                "📷 Фото получил ✅\n"
                "Напишите, что нужно сделать с этим фото (например: «считай текст», «переведи», «сделай письмо»)."
            )
            return

        await msg.answer("🔎 Обрабатываю фото...")

        # ✅ СКАЧИВАЕМ ФОТО В БАЙТЫ (а не через ссылку)
        downloaded = await bot.download_file(file.file_path)
        image_bytes = downloaded.read()

        # ✅ OCR напрямую байтами
        ocr_text = ocr_image_bytes(image_bytes)

        # если OCR ничего не нашёл — всё равно дадим ответ “по общему описанию”
        if not ocr_text:
            prompt = (
                "Пользователь отправил фото, но текст распознать не удалось.\n"
                f"Задание пользователя: {caption}\n"
                "Ответь максимально полезно. Если нужен текст с фото — попроси прислать фото четче/крупнее."
            )
            await msg.answer(ask_ai(msg.from_user.id, prompt))
            return

        prompt = (
            f"Задание пользователя по фото: {caption}\n\n"
            "Текст с изображения (OCR):\n"
            f"{ocr_text}"
        )

        await msg.answer(ask_ai(msg.from_user.id, prompt))

    except Exception as e:
        # если хочешь увидеть причину в логах:
        # print("PHOTO ERROR:", repr(e))
        await msg.answer("⚠️ Не смог обработать фото. Попробуйте отправить ещё раз (крупнее/четче).")

@dp.message_handler()
async def chat(msg):
    USERS.add(msg.from_user.id)
    if not await require_subscription(msg):
        return
    await msg.answer("⏳ Думаю...")
    await msg.answer(ask_ai(msg.from_user.id, msg.text))

# ========= GLOBAL ERROR LOG =========
async def on_error(update, exception):
    # не логируем ошибки генерации картинок (это не поломка бота)
    if "InvalidHTTPUrlContent" in repr(exception):
        return True

    if ADMIN_LOG_CHAT_ID:
        await bot.send_message(
            ADMIN_LOG_CHAT_ID,
            f"💥 КРИТИЧЕСКАЯ ОШИБКА БОТА\n{repr(exception)}"
        )
    return True

dp.errors_handler()(on_error)

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


