import os
import asyncio
import json
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ChosenInlineResult,
)
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
BOT_USERNAME = None

# ---------------- Points tables ----------------
TIERS_INDIVIDUAL = [
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

TIERS_TEAM = [
    (0, 5000, 6),
    (5001, 12000, 10),
    (12001, 26000, 14),
    (26001, 48000, 16),
    (48001, 120000, 20),
    (120001, 200000, 24),
    (200001, 400000, 28),
    (400001, 560000, 32),
    (560001, 800000, 34),
    (800001, 1600000, 36),
    (1600001, 3200000, 38),
    (3200001, 6000000, 40),
    (6000001, float("inf"), 42),
]

MODE_LABELS = {"individual": "الفردية", "team": "الفريق"}
TEAM_WORDS = {"فريق", "الفريق", "team", "Team", "TEAM"}

def points_for(n: int, mode: str = "individual") -> int:
    tiers = TIERS_TEAM if mode == "team" else TIERS_INDIVIDUAL
    for lo, hi, pts in tiers:
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

def parse_inline(text: str):
    if not text:
        return None
    t = text.translate(str.maketrans(ARABIC_DIGITS, WESTERN_DIGITS)).replace(",", "").replace("،", "")
    mode = "individual"
    nums = []
    for tok in t.split():
        if tok in TEAM_WORDS:
            mode = "team"
        elif tok.isdigit():
            nums.append(int(tok))
        else:
            return None
    if len(nums) != 2:
        return None
    return mode, nums[0], nums[1]

def fmt(x) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:.1f}"

# ---------------- Core compute ----------------
def compute_battle(my_number: int, opp_number: int, mode: str):
    my_points = points_for(my_number, mode)
    opp_points = points_for(opp_number, mode)

    if my_number > opp_number:
        result_label = "فوز ✅"
        my_result = my_points + opp_points / 2
        opp_result = opp_points / 2
        note = "في حال الفوز تأخذ نصف نقاط الخصم"
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
        f"🏆 نتيجة معركة الشعبية {MODE_LABELS.get(mode, '')}\n"
        "━━━━━━━━━━━━━━\n"
        f"👤 نقاطك: {my_number:,}  →  {my_points} نقطة\n"
        f"🎯 نقاط الخصم: {opp_number:,}  →  {opp_points} نقطة\n"
        "━━━━━━━━━━━━━━\n"
        f"النتيجة : {result_label}\n"
        f"     نقاطك : {fmt(my_result)} نقطة\n"
        f"    نقاط الخصم : {fmt(opp_result)} نقطة\n"
        f" {note}"
    )
    return {
        "text": text,
        "my_points": my_points,
        "opp_points": opp_points,
        "result_label": result_label,
        "my_result": my_result,
        "opp_result": opp_result,
    }

# ---------------- Keyboards ----------------
def menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ معركة الشعبية الفردية", callback_data="battle_individual")
    kb.button(text="👥 معركة الشعبية فريق", callback_data="battle_team")
    kb.adjust(1)
    return kb.as_markup()

def landing_keyboard(mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 سجلي", callback_data=f"hist:{mode}")
    kb.button(text="▶️ ابدأ الحسبة", callback_data=f"calc:{mode}")
    kb.button(text="🔙 رجوع", callback_data="menu")
    kb.adjust(2, 1)
    return kb.as_markup()

def result_keyboard(mode: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 معركة جديدة", callback_data=f"calc:{mode}")
    kb.button(text="📜 سجلي", callback_data=f"hist:{mode}")
    kb.button(text="🔙 القائمة", callback_data="menu")
    kb.adjust(2, 1)
    return kb.as_markup()

def history_picker_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📜 سجل الفردية", callback_data="hist:individual")
    kb.button(text="📜 سجل الفريق", callback_data="hist:team")
    kb.adjust(1)
    return kb.as_markup()

def cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ إلغاء", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()

def inline_help_keyboard():
    if not BOT_USERNAME:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="⚔️ معركة شعبية الفردية", url=f"https://t.me/{BOT_USERNAME}?start=individual")
    kb.button(text="👥 معركة شعبية الفريق", url=f"https://t.me/{BOT_USERNAME}?start=team")
    kb.adjust(1)
    return kb.as_markup()

# ---------------- FSM ----------------
class BattleFSM(StatesGroup):
    my_number = State()
    opp_number = State()

dp = Dispatcher()
bot = Bot(BOT_TOKEN)

async def send_menu(message: Message, state: FSMContext = None):
    if state:
        await state.clear()
    await message.answer(
        "👋 أهلًا بك في حاسبة معركة الشعبية\n"
        "اختر نوع المعركة:",
        reply_markup=menu_keyboard(),
    )

async def send_landing(message: Message, mode: str, state: FSMContext):
    await state.clear()
    await message.answer(
        f"🔥 معركة الشعبية - {MODE_LABELS.get(mode, '')}\n"
        "━━━━━━━━━━━━━━\n"
        "اختر من الأزرار:",
        reply_markup=landing_keyboard(mode),
    )

async def start_calc(message: Message, mode: str, state: FSMContext):
    await state.clear()
    await state.set_state(BattleFSM.my_number)
    await state.update_data(mode=mode)
    await message.answer(
        f"🔥 معركة الشعبية - {MODE_LABELS.get(mode, '')}\n"
        "━━━━━━━━━━━━━━\n"
        "أرسل عدد شعبيتك",
        reply_markup=cancel_keyboard(),
    )

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext, command: CommandObject = None):
    arg = (command.args or "").strip() if command else ""
    if arg == "team":
        await send_landing(message, "team", state)
    elif arg == "individual":
        await send_landing(message, "individual", state)
    else:
        await send_menu(message, state)

@dp.message(Command("battle"))
async def battle_cmd(message: Message, state: FSMContext):
    await send_menu(message, state)

@dp.message(Command("history"))
async def history_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("اختر السجل:", reply_markup=history_picker_keyboard())

@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("تم الإلغاء. أرسل /start للبدء من جديد.")

@dp.callback_query(F.data == "battle_individual")
async def cb_individual(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_landing(callback.message, "individual", state)

@dp.callback_query(F.data == "battle_team")
async def cb_team(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_landing(callback.message, "team", state)

@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_menu(callback.message, state)

@dp.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await send_menu(callback.message, state)

@dp.callback_query(F.data.startswith("calc:"))
async def cb_calc(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mode = callback.data.split(":", 1)[1]
    await start_calc(callback.message, mode, state)

@dp.callback_query(F.data.startswith("hist:"))
async def cb_hist(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mode = callback.data.split(":", 1)[1]
    await show_history(callback.from_user.id, callback.message, mode)

@dp.message(StateFilter(BattleFSM.my_number))
async def got_my_number(message: Message, state: FSMContext):
    n = parse_number(message.text or "")
    if n is None:
        await message.answer("❌ أرسل رقمًا صحيحًا فقط (مثال: 100000).")
        return
    data = await state.get_data()
    mode = data.get("mode", "individual")
    await state.update_data(my_number=n)
    await state.set_state(BattleFSM.opp_number)
    await message.answer(
        f"✅ شعبيتك: {n:,} = {points_for(n, mode)} نقطة\n\n"
        "الآن أرسل عدد شعبية الخصم:",
        reply_markup=cancel_keyboard(),
    )

@dp.message(StateFilter(BattleFSM.opp_number))
async def got_opp_number(message: Message, state: FSMContext):
    opp_number = parse_number(message.text or "")
    if opp_number is None:
        await message.answer("❌ أرسل رقمًا صحيحًا فقط (مثال: 60000).")
        return
    data = await state.get_data()
    mode = data.get("mode", "individual")
    my_number = data["my_number"]
    r = compute_battle(my_number, opp_number, mode)
    await message.answer(r["text"], reply_markup=result_keyboard(mode))
    save_battle(message.from_user, mode, my_number, r["my_points"], opp_number,
                r["opp_points"], r["result_label"], r["my_result"], r["opp_result"], source="calc")
    await state.clear()

# ---------------- Online (inline) ----------------
INLINE_HELP_TEXT = (
    "🔥 حاسبة معركة الشعبية\n"
    "━━━━━━━━━━━━━━\n"
    "احسب نتيجة معركتك في أي محادثة:\n\n"
    "• الفردية: اكتب يوزر البوت ثم رقمك ورقم الخصم\n"
    "  مثال: 1200000 900000\n"
    "• الفريق: أضف كلمة (فريق) قبل الرقمين\n"
    "  مثال: فريق 1200000 900000\n\n"
    "أو اختر نوع المعركة من الأزرار 👇"
)

@dp.inline_query()
async def inline_battle(query: InlineQuery):
    parsed = parse_inline(query.query or "")
    if parsed is None:
        await query.answer([], cache_time=1, is_personal=True)
        return

    mode, my_number, opp_number = parsed
    r = compute_battle(my_number, opp_number, mode)
    article = InlineQueryResultArticle(
        id=f"{mode}|{my_number}|{opp_number}",
        title=f"{MODE_LABELS[mode]}: {my_number:,} ضد {opp_number:,}",
        description=f"النتيجة: {r['result_label']} — اضغط للإرسال",
        input_message_content=InputTextMessageContent(message_text=r["text"]),
    )
    await query.answer([article], cache_time=1, is_personal=True)

@dp.chosen_inline_result()
async def online_chosen(chosen: ChosenInlineResult):
    try:
        mode, my_s, opp_s = chosen.result_id.split("|")
        my_number, opp_number = int(my_s), int(opp_s)
    except Exception:
        return
    r = compute_battle(my_number, opp_number, mode)
    save_battle(chosen.from_user, mode, my_number, r["my_points"], opp_number,
                r["opp_points"], r["result_label"], r["my_result"], r["opp_result"], source="online")

# ---------------- Storage ----------------
def save_battle(user, mode, my_number, my_points, opp_number, opp_points,
                result_label, my_result, opp_result, source="calc"):
    if db is None:
        return
    try:
        db.collection("battles").add({
            "user_id": user.id,
            "username": user.username,
            "name": user.full_name,
            "mode": mode,
            "source": source,
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

async def show_history(user_id: int, target: Message, mode: str):
    label = MODE_LABELS.get(mode, "")
    if db is None:
        await target.answer("📜 التخزين غير مفعّل حاليًا.")
        return
    try:
        docs = list(db.collection("battles").where("user_id", "==", user_id).limit(100).stream())
        rows = [d.to_dict() for d in docs]
        rows = [r for r in rows if r.get("mode") == mode]
        rows.sort(key=lambda r: r.get("ts") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        rows = rows[:5]
        if not rows:
            await target.answer(f"📜 لا توجد نتائج في سجل {label} بعد.")
            return
        sep = "━━━━━━━━━━━━━━"
        lines = [f"📜 آخر 5 معارك - {label}:", sep]
        for i, r in enumerate(rows, 1):
            res = r.get("result", "?").replace("✅", "").replace("❌", "").replace("🤝", "").strip()
            lines.append(
                f"{i}- النتيجة: {res}\n"
                f"دعمي : {r.get('my_number', 0):,} ({r.get('my_points', 0)}نقطة)\n"
                f"دعم خصمي : {r.get('opp_number', 0):,} ({r.get('opp_points', 0)}نقطة)\n"
                f"نقاطي : {fmt(r.get('my_result', 0))} نقطة\n"
                f"نقاط الخصم : {fmt(r.get('opp_result', 0))} نقطة"
            )
            lines.append(sep)
        await target.answer("\n".join(lines))
    except Exception as e:
        logger.exception("history failed: %s", e)
        await target.answer("⚠️ تعذّر جلب السجل الآن.")

@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def guest_mention(message: Message, state: FSMContext):
    await _handle_guest_text(message)

@dp.guest_message()
async def guest_message_handler(message: Message):
    await _handle_guest_text(message)

async def _handle_guest_text(message: Message):
    if not BOT_USERNAME:
        return
    text = message.text or ""
    if f"@{BOT_USERNAME}".lower() not in text.lower():
        return
    tokens = [t for t in text.split() if not t.startswith("@")]
    parsed = parse_inline(" ".join(tokens))
    if parsed is None:
        return  # silent: don't spam the group
    mode, my_number, opp_number = parsed
    r = compute_battle(my_number, opp_number, mode)
    await message.answer(r["text"])
    save_battle(message.from_user, mode, my_number, r["my_points"], opp_number,
                r["opp_points"], r["result_label"], r["my_result"], r["opp_result"], source="guest")

@dp.message(StateFilter(None), F.chat.type == "private")
async def fallback(message: Message):
    await message.answer("👋 أرسل /start لاختيار نوع المعركة.")

# ---------------- Webhook app ----------------
async def _startup_bg() -> None:
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        logger.info("Bot username: @%s", BOT_USERNAME)
    except Exception as e:
        logger.exception("get_me failed: %s", e)
    try:
        await bot.set_webhook(
            WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result", "guest_message"],
        )
        logger.info("Webhook set to %s", WEBHOOK_URL)
    except Exception as e:
        logger.exception("set_webhook failed (service still running): %s", e)

async def on_startup() -> None:
    asyncio.create_task(_startup_bg())

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
