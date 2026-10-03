import os
import asyncio
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com")
REQUIRED_CHANNELS = [x.strip().lstrip("@") for x in os.getenv("REQUIRED_CHANNELS", "").split(",") if x.strip()]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

DB = "bear_bot.sqlite3"
app = FastAPI()
router = Router()
bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# Порядок и веса можно менять.
PRIZES = [
    {"id": "teddy", "name": "🧸 Мишка", "weight": 18},
    {"id": "heart", "name": "💝 Сердце", "weight": 18},
    {"id": "gift", "name": "🎁 Подарок", "weight": 12},
    {"id": "rose", "name": "🌹 Роза", "weight": 12},
    {"id": "premium", "name": "⭐ Premium", "weight": 3},
    {"id": "nft", "name": "💎 NFT-граф", "weight": 1},
    {"id": "empty", "name": "Пусто!", "weight": 36},
]

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            streak INTEGER DEFAULT 0,
            chance INTEGER DEFAULT 5,
            last_claim TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            prize_id TEXT NOT NULL,
            prize_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        await db.commit()

def now():
    return datetime.now(timezone.utc)

async def get_user(tg_id: int):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()

async def upsert_user(tg_id: int, username: str, first_name: str):
    u = await get_user(tg_id)
    if not u:
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT INTO users(tg_id,username,first_name,created_at) VALUES(?,?,?,?,?)",
                (tg_id, username, first_name, now().isoformat())
            )
            await db.commit()

async def can_claim(tg_id: int):
    u = await get_user(tg_id)
    if not u or not u["last_claim"]:
        return True
    return now() - datetime.fromisoformat(u["last_claim"]) >= timedelta(hours=24)

async def is_subscribed(tg_id: int, channel: str):
    try:
        member = await bot.get_chat_member("@" + channel, tg_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

def subscription_keyboard():
    b = InlineKeyboardBuilder()
    for c in REQUIRED_CHANNELS:
        b.row(InlineKeyboardButton(text=f"➜ @{c}", url=f"https://t.me/{c}"))
    b.row(InlineKeyboardButton(text="✅ Готово", callback_data="check_subs"))
    return b.as_markup()

def wheel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎡 Открыть рулетку",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )]
    ])

@router.message(CommandStart())
async def start(message: Message):
    await upsert_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or ""
    )
    if REQUIRED_CHANNELS:
        await message.answer(
            "🧸 Чтобы получить подарок —\n"
            "подпишись на наши каналы, затем нажми «Готово»",
            reply_markup=subscription_keyboard()
        )
    else:
        await show_game(message)

async def show_game(message: Message):
    u = await get_user(message.from_user.id)
    available = await can_claim(message.from_user.id)
    if available:
        text = (
            "🧸 <b>Доступен прокрут рулетки!</b>\n\n"
            f"• Текущий шанс: <b>{u['chance']}%</b>\n"
            f"• Серия дней: <b>{u['streak']}/21</b>"
        )
        await message.answer(text, reply_markup=wheel_keyboard(), parse_mode="HTML")
    else:
        remaining = timedelta(hours=24) - (now() - datetime.fromisoformat(u["last_claim"]))
        seconds = max(0, int(remaining.total_seconds()))
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        await message.answer(
            f"🧸 Следующий бесплатный прокрут будет доступен через "
            f"<b>{h}ч {m}м</b>.",
            parse_mode="HTML"
        )

@router.callback_query(F.data == "check_subs")
async def check_subs(call: CallbackQuery):
    missing = [c for c in REQUIRED_CHANNELS if not await is_subscribed(call.from_user.id, c)]
    if missing:
        await call.answer("Подпишись на все каналы и попробуй снова.", show_alert=True)
        return
    await call.answer("Готово!")
    await call.message.answer("🎁 Подписка подтверждена!")
    await show_game(call.message)

dp.include_router(router)

# Mini App API.
@app.get("/")
async def index():
    return FileResponse(Path("web/index.html"))

@app.get("/api/config")
async def config():
    return {
        "prizes": [{"id": p["id"], "name": p["name"]} for p in PRIZES],
        "segments": len(PRIZES),
    }

@app.post("/api/spin")
async def spin(x_telegram_id: int = Header(None, alias="X-Telegram-Id")):
    if not x_telegram_id:
        raise HTTPException(401, "Missing Telegram user id")

    u = await get_user(x_telegram_id)
    if not u:
        raise HTTPException(404, "User not found")
    if not await can_claim(x_telegram_id):
        raise HTTPException(429, "Free spin is not available yet")

    # В первой версии вероятность хранится отдельно как игровой шанс.
    # Результат выбирается сервером, а не браузером.
    prize = random.choices(PRIZES, weights=[p["weight"] for p in PRIZES], k=1)[0]

    new_streak = min(21, u["streak"] + 1)
    new_chance = min(95, 5 + new_streak * 2)

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE users SET last_claim=?, streak=?, chance=? WHERE tg_id=?",
            (now().isoformat(), new_streak, new_chance, x_telegram_id)
        )
        await db.execute(
            "INSERT INTO wins(tg_id,prize_id,prize_name,created_at) VALUES(?,?,?,?)",
            (x_telegram_id, prize["id"], prize["name"], now().isoformat())
        )
        await db.commit()

    return {
        "prize": prize["name"],
        "prize_id": prize["id"],
        "streak": new_streak,
        "chance": new_chance,
    }

@app.get("/api/admin/stats")
async def admin_stats(x_admin_id: int = Header(None, alias="X-Admin-Id")):
    if x_admin_id not in ADMIN_IDS:
        raise HTTPException(403, "Forbidden")
    async with aiosqlite.connect(DB) as db:
        users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        wins = (await (await db.execute("SELECT COUNT(*) FROM wins")).fetchone())[0]
    return {"users": users, "spins": wins}

async def run_bot():
    if not bot:
        print("BOT_TOKEN is empty. Fill .env first.")
        return
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True).start()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
