import os
import asyncio
import tempfile
import sqlite3
import base64
import logging
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import OpenAI
from openai import APIError, AuthenticationError, RateLimitError, APITimeoutError
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeDefault, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / "claude.env"
loaded = load_dotenv(ENV_PATH)

TG_TOKEN = os.getenv("TG_TOKEN")
BASE_URL = os.getenv("BASE_URL")
API_KEY = os.getenv("API_KEY")
MODEL = os.getenv("MODEL")
PROXY_URL = os.getenv("PROXY_URL")
ALLOWED_USERS = set(int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip().isdigit())
DB_PATH = os.getenv("DB_PATH", "bot_memory.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("claude_tg_bot")

def status(msg):
    print(f"\r{msg:<120}", end="", flush=True)

def ok(msg):
    logger.info("✅ %s", msg)

def fail(msg):
    logger.error("❌ %s", msg)

def mask_secret(s):
    if not s:
        return "пусто"
    if len(s) <= 12:
        return "*" * len(s)
    return f"{s[:8]}...{s[-6:]}"

logger.info("=== ЗАПУСК БОТА ===")
logger.info("BASE_DIR: %s", BASE_DIR)
logger.info("ENV_PATH: %s", ENV_PATH)
logger.info("ENV exists: %s", ENV_PATH.exists())
logger.info("load_dotenv result: %s", loaded)
logger.info("Файл базы: %s", DB_PATH)
logger.info("Разрешённые пользователи: %s", sorted(ALLOWED_USERS) if ALLOWED_USERS else "все пользователи")
logger.info("Прокси: %s", PROXY_URL if PROXY_URL else "не используется")
logger.info("TG_TOKEN: %s", mask_secret(TG_TOKEN))
logger.info("BASE_URL: %s", BASE_URL if BASE_URL else "пусто")
logger.info("API_KEY: %s", mask_secret(API_KEY))
logger.info("MODEL: %s", MODEL if MODEL else "пусто")

if not ENV_PATH.exists():
    fail(f"Файл {ENV_PATH.name} не найден")
    raise FileNotFoundError(f"{ENV_PATH} not found")

if not TG_TOKEN:
    fail("Переменная TG_TOKEN не задана")
    raise RuntimeError("TG_TOKEN is not set")
if not BASE_URL:
    fail("Переменная BASE_URL не задана")
    raise RuntimeError("BASE_URL is not set")
if not API_KEY:
    fail("Переменная API_KEY не задана")
    raise RuntimeError("API_KEY is not set")
if not MODEL:
    fail("Переменная MODEL не задана")
    raise RuntimeError("MODEL is not set")

bot_session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else AiohttpSession()
bot = Bot(token=TG_TOKEN, session=bot_session)
dp = Dispatcher()

http_timeout = httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=10.0)
http_client = httpx.Client(proxy=PROXY_URL, timeout=http_timeout) if PROXY_URL else httpx.Client(timeout=http_timeout)

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL,
    http_client=http_client
)

DAILY_LIMIT = 50
MAX_HISTORY = 10

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, created_at TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS usage (user_id INTEGER, day TEXT, count INTEGER, PRIMARY KEY(user_id, day))")
conn.commit()

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💬 Задать вопрос"), KeyboardButton(text="🖼 Анализ фото")],
        [KeyboardButton(text="📄 Анализ PDF"), KeyboardButton(text="🧹 Очистить память")],
        [KeyboardButton(text="📊 Лимит"), KeyboardButton(text="ℹ️ Помощь")]
    ],
    resize_keyboard=True
)

def hist(uid):
    rows = cur.execute(
        "SELECT role, content FROM history WHERE user_id=? ORDER BY rowid DESC LIMIT ?",
        (uid, MAX_HISTORY * 2)
    ).fetchall()
    out = []
    for r in reversed(rows):
        out.append({"role": r["role"], "content": r["content"]})
    return out

def save(uid, role, content):
    cur.execute(
        "INSERT INTO history(user_id, role, content, created_at) VALUES(?,?,?,?)",
        (uid, role, content, str(date.today()))
    )
    conn.commit()

def clear(uid):
    cur.execute("DELETE FROM history WHERE user_id=?", (uid,))
    conn.commit()

def used(uid):
    row = cur.execute(
        "SELECT count FROM usage WHERE user_id=? AND day=?",
        (uid, str(date.today()))
    ).fetchone()
    return row["count"] if row else 0

def inc(uid):
    cur.execute(
        "INSERT INTO usage(user_id, day, count) VALUES(?,?,1) "
        "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1",
        (uid, str(date.today()))
    )
    conn.commit()

def api_error_text(e: Exception) -> str:
    s = str(e)
    if "401" in s or "authentication" in s.lower():
        return "Ошибка авторизации. Проверьте BASE_URL, API_KEY и MODEL."
    return s

def build_message_content(text=None, extra=None):
    content = []
    if extra:
        content.extend(extra)
    if text:
        content.append({"type": "text", "text": text})
    return content

def ask(uid, text, extra=None):
    messages = hist(uid)
    messages.append({"role": "user", "content": build_message_content(text=text, extra=extra)})

    status("🤖 Отправляю запрос в модель...")
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=1000,
        )
        ans = resp.choices[0].message.content or ""
        ok("Модель ответила успешно")
        save(uid, "user", text)
        save(uid, "assistant", ans)
        return ans
    except AuthenticationError as e:
        fail(f"API auth: {api_error_text(e)}")
        raise
    except RateLimitError as e:
        fail(f"API rate limit: {e}")
        raise
    except APITimeoutError as e:
        fail(f"API timeout: {e}")
        raise
    except APIError as e:
        fail(f"API error: {e}")
        raise
    except Exception as e:
        fail(f"Неожиданная ошибка API: {e}")
        raise

async def allowed(m):
    return not ALLOWED_USERS or m.from_user.id in ALLOWED_USERS

async def help_text(m):
    await m.answer(
        "Кнопки:\n"
        "💬 Задать вопрос — обычный текстовый чат\n"
        "🖼 Анализ фото — пришли картинку\n"
        "📄 Анализ PDF — пришли PDF\n"
        "🧹 Очистить память — сбросить историю\n"
        "📊 Лимит — показать дневной лимит\n"
        "\nКоманды:\n/start, /help, /reset, /limit",
        reply_markup=keyboard
    )

@dp.message(Command("start"))
async def start(m: types.Message):
    await m.answer("Привет! Я личный бот через кастомный провайдер. Нажми кнопку или отправь сообщение.", reply_markup=keyboard)

@dp.message(Command("help"))
async def help_cmd(m: types.Message):
    await help_text(m)

@dp.message(Command("reset"))
async def reset_cmd(m: types.Message):
    clear(m.from_user.id)
    await m.answer("История диалога очищена.", reply_markup=keyboard)

@dp.message(Command("limit"))
async def limit_cmd(m: types.Message):
    await m.answer(f"Использовано сегодня: {used(m.from_user.id)}/{DAILY_LIMIT}", reply_markup=keyboard)

@dp.message(F.text == "ℹ️ Помощь")
async def help_btn(m: types.Message):
    await help_text(m)

@dp.message(F.text == "💬 Задать вопрос")
async def mode_text(m: types.Message):
    await m.answer("Режим: текст. Просто напиши свой вопрос.", reply_markup=keyboard)

@dp.message(F.text == "🖼 Анализ фото")
async def mode_photo(m: types.Message):
    await m.answer("Режим: фото. Пришли изображение.", reply_markup=keyboard)

@dp.message(F.text == "📄 Анализ PDF")
async def mode_pdf(m: types.Message):
    await m.answer("Режим: PDF. Пришли PDF-файл.", reply_markup=keyboard)

@dp.message(F.text == "🧹 Очистить память")
async def clear_btn(m: types.Message):
    clear(m.from_user.id)
    await m.answer("Память очищена.", reply_markup=keyboard)

@dp.message(F.text == "📊 Лимит")
async def limit_btn(m: types.Message):
    await m.answer(f"Использовано сегодня: {used(m.from_user.id)}/{DAILY_LIMIT}", reply_markup=keyboard)

@dp.message(F.text)
async def text_handler(m: types.Message):
    if not await allowed(m):
        await m.answer("Доступ только для разрешённых пользователей.", reply_markup=keyboard)
        return
    if m.text.startswith("/"):
        return
    uid = m.from_user.id
    if used(uid) >= DAILY_LIMIT:
        await m.answer("Дневной лимит исчерпан.", reply_markup=keyboard)
        return
    try:
        inc(uid)
        logger.info("Получен текстовый запрос от user_id=%s", uid)
        ans = await asyncio.to_thread(ask, uid, m.text)
        await m.answer(ans, reply_markup=keyboard)
    except Exception as e:
        logger.exception("Ошибка в обработчике текста")
        await m.answer(f"Ошибка: {api_error_text(e)}", reply_markup=keyboard)

@dp.message(F.photo)
async def photo_handler(m: types.Message):
    if not await allowed(m):
        await m.answer("Доступ только для разрешённых пользователей.", reply_markup=keyboard)
        return
    uid = m.from_user.id
    if used(uid) >= DAILY_LIMIT:
        await m.answer("Дневной лимит исчерпан.", reply_markup=keyboard)
        return
    try:
        inc(uid)
        logger.info("Получено фото от user_id=%s", uid)
        photo = m.photo[-1]
        file = await bot.get_file(photo.file_id)
        data = await bot.download_file(file.file_path)
        raw = data.read()
        image_part = {
            "type": "image",
            "image_url": {
                "url": f"data:image/jpeg;base64,{base64.b64encode(raw).decode()}"
            }
        }
        prompt = m.caption or "Проанализируй это изображение."
        ans = await asyncio.to_thread(ask, uid, prompt, [image_part])
        await m.answer(ans, reply_markup=keyboard)
    except Exception as e:
        logger.exception("Ошибка в обработчике фото")
        await m.answer(f"Ошибка: {api_error_text(e)}", reply_markup=keyboard)

@dp.message(F.document)
async def document_handler(m: types.Message):
    if not await allowed(m):
        await m.answer("Доступ только для разрешённых пользователей.", reply_markup=keyboard)
        return
    uid = m.from_user.id
    if used(uid) >= DAILY_LIMIT:
        await m.answer("Дневной лимит исчерпан.", reply_markup=keyboard)
        return
    try:
        inc(uid)
        logger.info("Получен документ от user_id=%s", uid)
        doc = m.document
        if doc.mime_type == "application/pdf" or (doc.file_name and doc.file_name.lower().endswith(".pdf")):
            file = await bot.get_file(doc.file_id)
            data = await bot.download_file(file.file_path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(data.read())
                tmp_path = tmp.name
            reader = PdfReader(tmp_path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            os.unlink(tmp_path)
            prompt = m.caption or "Проанализируй этот PDF."
            ans = await asyncio.to_thread(ask, uid, prompt + "\n\nТекст документа:\n" + text[:20000])
            await m.answer(ans, reply_markup=keyboard)
        else:
            await m.answer("Пока обрабатываю только PDF-файлы.", reply_markup=keyboard)
    except Exception as e:
        logger.exception("Ошибка в обработчике документа")
        await m.answer(f"Ошибка: {api_error_text(e)}", reply_markup=keyboard)

async def run_polling_forever():
    while True:
        try:
            logger.info("Start polling")
            await dp.start_polling(bot)
            logger.warning("Polling завершился без ошибки, перезапуск через 3 секунды...")
        except asyncio.CancelledError:
            logger.info("Polling отменён")
            raise
        except Exception as e:
            logger.exception("Ошибка polling: %s", e)
            logger.info("Перезапуск polling через 3 секунды...")
            await asyncio.sleep(3)

async def main():
    try:
        status("🔍 Проверяю подключение к Telegram...")
        me = await bot.get_me()
        ok(f"Подключение к Telegram успешно. Бот: @{me.username}, id={me.id}")

        status("⚙️ Устанавливаю команды бота...")
        await bot.set_my_commands([
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="reset", description="Очистить историю"),
            BotCommand(command="limit", description="Показать лимит"),
        ], scope=BotCommandScopeDefault())
        ok("Команды установлены успешно.")

        status("🚀 Запускаю polling...")
        logger.info("Start polling")
        await run_polling_forever()
    except Exception:
        logger.exception("❌ Критическая ошибка в main()")
        raise
    finally:
        logger.info("♻️ Завершаю работу...")
        await bot.session.close()
        http_client.close()
        conn.close()
        logger.info("✅ Работа завершена.")

if __name__ == "__main__":
    asyncio.run(main())