import os
import json
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import firebase_admin
from firebase_admin import credentials, firestore

# ---------------- Config ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_HOST = (os.environ.get("WEBHOOK_HOST") or os.environ["RENDER_EXTERNAL_URL"]).rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH
PORT = int(os.environ.get("PORT", "10000"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("battlebot")

# ---------------- Firestore ----------------
def init_firestore():
    raw = os.environ.get("FIREBASE_CREDENTIALS")
    if not raw:
        logger.warning("FIREBASE_CREDENTIALS not set - storage disabled")
        return None
    try:
        cred = credentials.Certificate(json.loads(raw))
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        logger.exception("Firestore init failed: %s", e)
        return None

db = init_firestore()

# ---------------- Points table ----------------
TIERS = [
    (0, 2000, 6),
    (2001, 4000, 10),
    (4001, 8000, 14),
    (8001, 15000, 16),
    (15001, 50000, 20),
    (50001, 120000, 24),
    (120001, 260000, 28),
    (260001, 500000, 32),
    (500001, 900000, 36),
    (900001, 2000000, 40),
    (2000001, 8000000, 42),
    (8000001, float("inf"), 44),
]

def points_for(n: int) -> int:
    for lo, hi, pts in TIERS:
        if lo <= n <= hi:
            return pts
    return 0

ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
WESTERN_DIGITS = "0123456789"

def parse_number(text: str):
    if not text:
        return None
    t = text.strip().translate(str.maketrans(ARABIC_DIGITS, WESTERN_DIGITS))
    t = t.replace(",", "").replace("،", "").replace(" ", "")
    if not t.isdigit():
        return None
    return int(t)

def fmt(x) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:.1f}"

# ---------------- Keyboard ----------------
def result_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 معركة جديدة", callback_data="new_battle")
    kb.button(text="📜 سجلي", callback_data="show_history")
    kb.adjust(2)
    return kb.as_markup()

# ---------------- FSM ----------------
class BattleFSM(StatesGroup):
    my_number = State()
    opp_number = State()

dp = Dispatcher()
bot = Bot(BOT_TOKEN)

async def send_battle_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BattleFSM.my_number)
    await message.answer(
        "🔥 حاسبة معركة الشعبية الفردية\n"
        "━━━━━━━━━━━━━━\n"
        "أرسل رقمك (الشعبية/المتابعين):\n\n"
        "للإلغاء في أي وقت: /cancel"
    )

@dp.message(CommandStart())
@dp.message(Command("battle"))
async def start_battle(message: Message, state: FSMContext):
    await send_battle_start(message, state)

@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("تم الإلغاء. أرسل /battle للبدء من جديد.")

@dp.message(StateFilter(BattleFSM.my_number))
async def got_my_number(message: Message, state: FSMContext):
    n = parse_number(message.text or "")
    if n is None:
        await message.answer("❌ أرسل رقمًا صحيحًا فقط (مثال: 100000).")
        return
    await state.update_data(my_number=n)
    await state.set_state(BattleFSM.opp_number)
    await message.answer(
        f"✅ رقمك: {n:,} = {points_for(n)} نقطة\n\n"
        "الآن أرسل رقم الخصم:"
    )

@dp.message(StateFilter(BattleFSM.opp_number))
async def got_opp_number(message: Message, state: FSMContext):
    opp_number = parse_number(message.text or "")
    if opp_number is None:
        await message.answer("❌ أرسل رقمًا صحيحًا فقط (مثال: 60000).")
        return
    data = await state.get_data()
    my_number = data["my_number"]

    my_points = points_for(my_number)
    opp_points = points_for(opp_number)

    # الفائز = صاحب الرقم الأكبر
    if my_number > opp_number:
        result_label = "فوز ✅"
        my_result = my_points + opp_points / 2
        opp_result = opp_points / 2
        note = "في حال الفوز تأخذ + نصف نقاط الخصم"
    elif my_number < opp_number:
        result_label = "خسارة ❌"
        my_result = my_points / 2
        opp_result = opp_points + my_points / 2
        note = "في حال الخسارة تأخذ نصف نقاطك فقط"
    else:
        result_label = "تعادل 🤝"
        my_result = my_points
        opp_result = opp_points
        note = "تعادل: كل طرف يحتفظ بنقاطه كاملة"

    text = (
        "🏆 نتيجة المعركة الفردية\n"
        "━━━━━━━━━━━━━━\n"
        f"👤 نقاطك: {my_number:,}  →  {my_points} نقطة\n"
        f"🎯 نقاط الخصم: {opp_number:,}  →  {opp_points} نقطة\n"
        "━━━━━━━━━━━━━━\n"
        f"النتيجة : {result_label}\n"
        f"     نقاطك : {fmt(my_result)} نقطة\n"
        f"    نقاط الخصم : {fmt(opp_result)} نقطة\n"
        f" {note}"
    )
    await message.answer(text, reply_markup=result_keyboard())

    save_battle(message, my_number, my_points, opp_number, opp_points, result_label, my_result, opp_result)
    await state.clear()

def save_battle(message, my_number, my_points, opp_number, opp_points, result_label, my_result, opp_result):
    if db is None:
        return
    try:
        u = message.from_user
        db.collection("battles").add({
            "user_id": u.id,
            "username": u.username,
            "name": u.full_name,
            "my_number": my_number,
            "my_points": my_points,
            "opp_number": opp_number,
            "opp_points": opp_points,
            "result": result_label,
            "my_result": my_result,
            "opp_result": opp_result,
            "ts": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.exception("Firestore save failed: %s", e)

async def show_history(user_id: int, target: Message):
    if db is None:
        await target.answer("📜 التخزين غير مفعّل حاليًا.")
        return
    try:
        docs = list(db.collection("battles").where("user_id", "==", user_id).limit(20).stream())
        rows = [d.to_dict() for d in docs]
        rows.sort(key=lambda r: r.get("ts") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        rows = rows[:5]
        if not rows:
            await target.answer("📜 لا توجد نتائج محفوظة بعد. أرسل /battle.")
            return
        lines = ["📜 آخر 5 معارك لك:", "━━━━━━━━━━━━━━"]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i}) {r.get('my_number', 0):,} ({r.get('my_points', 0)}ن) "
                f"ضد {r.get('opp_number', 0):,} ({r.get('opp_points', 0)}ن) "
                f"→ {r.get('result', '?')} | لك {fmt(r.get('my_result', 0))}ن"
            )
        await target.answer("\n".join(lines))
    except Exception as e:
        logger.exception("history failed: %s", e)
        await target.answer("⚠️ تعذّر جلب السجل الآن.")

@dp.message(Command("history"))
async def history_cmd(message: Message, state: FSMContext):
    await show_history(message.from_user.id, message)

@dp.callback_query(F.data == "new_battle")
async def cb_new_battle(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_battle_start(callback.message, state)

@dp.callback_query(F.data == "show_history")
async def cb_history(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_history(callback.from_user.id, callback.message)

@dp.message(StateFilter(None))
async def fallback(message: Message):
    await message.answer("👋 أرسل /battle لبدء حساب معركة الشعبية.")

# ---------------- Webhook app ----------------
async def on_startup() -> None:
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info("Webhook set to %s", WEBHOOK_URL)

async def health(request):
    return web.Response(text="ok")

def main():
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
