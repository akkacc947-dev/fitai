import os
import io
import re
import asyncio
import logging
from aiohttp import web
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from PIL import Image

# Google GenAI va Aiogram kutubxonalari
from google import genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, PreCheckoutQuery, LabeledPrice
)

# SQLAlchemy
from sqlalchemy import Column, Integer, String, BigInteger, Boolean, DateTime, Date, select, update, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# =====================================================================
# 1. MA'LUMOTLAR BAZASI SOZLAMALARI & MODELLARI
# =====================================================================
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_async_engine(DATABASE_URL, echo=False)
Base = declarative_base()
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

ADMIN_IDS = [int(item.strip()) for item in os.getenv("ADMIN_IDS", "").split(",") if item.strip().isdigit()]

DEFAULT_SETTINGS = {
    "payment_card": "5614 6814 2661 0816",
    "premium_price": "1500000",
    "channel_link": "https://www.youtube.com/@FitAiuz",
    "channel_type": "youtube",
    "video_limit_normal": "1",
    "video_limit_premium": "5"
}

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    is_premium = Column(Boolean, default=False)
    premium_expires_at = Column(DateTime, nullable=True)
    fridge_requests_today = Column(Integer, default=0)
    food_requests_today = Column(Integer, default=0)
    coach_requests_today = Column(Integer, default=0)
    video_requests_today = Column(Integer, default=0)
    youtube_bonus_claimed = Column(Boolean, default=False)
    youtube_channel_id = Column(String, nullable=True)
    youtube_penalty = Column(Boolean, default=False)
    last_request_date = Column(Date, default=date.today)

class PaymentProof(Base):
    __tablename__ = "payment_proofs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    file_id = Column(String, nullable=False)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(255) DEFAULT NULL"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS video_requests_today INTEGER DEFAULT 0"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS youtube_bonus_claimed BOOLEAN DEFAULT FALSE"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS youtube_channel_id VARCHAR(255) DEFAULT NULL"))
            await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS youtube_penalty BOOLEAN DEFAULT FALSE"))
            await conn.commit()
        except Exception:
            pass
    await ensure_default_settings()

LIMITS = {
    "fridge": {"normal": 1, "premium": 5},
    "food": {"normal": 1, "premium": 5},
    "coach": {"normal": 1, "premium": 20}
}

async def ensure_default_settings():
    async with async_session() as session:
        for key, default_value in DEFAULT_SETTINGS.items():
            result = await session.execute(select(Setting).where(Setting.key == key))
            setting = result.scalar_one_or_none()
            if not setting:
                session.add(Setting(key=key, value=str(default_value)))
        await session.commit()

async def get_setting_value(key: str, default: str = None):
    async with async_session() as session:
        result = await session.execute(select(Setting).where(Setting.key == key))
        setting = result.scalar_one_or_none()
        if setting:
            return setting.value
        if default is not None:
            session.add(Setting(key=key, value=str(default)))
            await session.commit()
            return str(default)
        return None

async def set_setting_value(key: str, value: str):
    async with async_session() as session:
        result = await session.execute(select(Setting).where(Setting.key == key))
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = str(value)
        else:
            session.add(Setting(key=key, value=str(value)))
        await session.commit()

async def get_channel_config():
    link = await get_setting_value("channel_link", DEFAULT_SETTINGS["channel_link"])
    channel_type = await get_setting_value("channel_type", DEFAULT_SETTINGS["channel_type"])
    return link, channel_type

async def get_limit_for_service(service_type: str, is_premium: bool) -> int:
    if service_type == "video":
        if is_premium:
            return int(await get_setting_value("video_limit_premium", DEFAULT_SETTINGS["video_limit_premium"]))
        return int(await get_setting_value("video_limit_normal", DEFAULT_SETTINGS["video_limit_normal"]))
    return LIMITS[service_type]["premium" if is_premium else "normal"]

async def get_payment_card() -> str:
    return await get_setting_value("payment_card", DEFAULT_SETTINGS["payment_card"])

async def get_premium_price() -> str:
    return await get_setting_value("premium_price", DEFAULT_SETTINGS["premium_price"])

async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# =====================================================================
# XAFSIZ TELEGRAM XABAR YUBORISH
# =====================================================================
async def safe_reply_html(message: types.Message, text_content: str, reply_markup=None):
    try:
        await message.answer(text_content, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        clean_text = re.sub(r'<[^>]+>', '', text_content)
        await message.answer(clean_text, reply_markup=reply_markup)

# =====================================================================
# 2. AI XIZMATLARI
# =====================================================================
def get_api_keys():
    keys_str = os.getenv("AI_API_KEY", "")
    return [k.strip() for k in keys_str.split(",") if k.strip()]

PRIMARY_MODEL = 'gemini-2.5-flash'

async def get_ai_coach_response(user_message: str) -> str:
    prompt = (
        "Sen professional FitAI dietolog va trenersan. Savollarga aniq javob ber. "
        "Javobingda FAQAT ruxsat etilgan HTML teglardan foydalan: <b>, <i>, <code>. "
        "Boshqa hech qanday Markdown belgilarni (yulduzchalar, chiziqlar) ishlatma. "
        "⚠️ Muhim: Javobing qisqa bo'lsin, 1500 ta belgidan oshmasin!\n"
        f"Savol: {user_message}"
    )
    api_keys = get_api_keys()
    if not api_keys:
        return "⚠️ Tizim xatoligi: AI API kalitlari topilmadi."

    for key in api_keys:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(model=PRIMARY_MODEL, contents=prompt)
            return response.text
        except Exception as e:
            err = str(e)
            if any(x in err for x in ["429", "RESOURCE_EXHAUSTED", "401", "UNAUTHENTICATED", "503", "UNAVAILABLE"]):
                continue
            return f"⚠️ AI bilan ulanishda kutilmagan xatolik yuz berdi."
    return "😔 Hozirda barcha AI liniyalari band. Iltimos, birozdan so'ng qayta urinib ko'ring."

async def analyze_food_image(image_bytes: bytes) -> str:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.thumbnail((800, 800))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=80)
        image_to_send = Image.open(io.BytesIO(output.getvalue()))
    except Exception:
        return "❌ Rasm formatida xatolik bor yoki rasmni o'qib bo'lmadi."

    prompt = (
        "Sen tajribali dietologsan. Ushbu rasmdagi taomni tahlil qil va FAQAT quyidagi HTML formatda javob ber:\n\n"
        "🍽 <b>Tahlil Natijasi:</b>\n- [Mahsulot nomi]: [Grammi]\n\n"
        "🔥 <b>Energetik Qiymati:</b>\n- Kaloriya: X kcal\n- Oqsil: X g\n- Yog': X g\n- Uglevod: X g"
    )
    
    api_keys = get_api_keys()
    for key in api_keys:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(model=PRIMARY_MODEL, contents=[image_to_send, prompt])
            return response.text
        except Exception as e:
            if any(x in str(e) for x in ["429", "RESOURCE_EXHAUSTED", "401", "UNAUTHENTICATED", "503"]):
                continue
            return f"❌ Rasmni tahlil qilishda xatolik yuz berdi."
    return "😔 Hozirda barcha AI liniyalari band."

async def analyze_fridge_image(image_bytes: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.thumbnail((800, 800))
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=80)
        image_to_send = Image.open(io.BytesIO(output.getvalue()))
    except Exception:
        return "❌ Rasm formatini qayta ishlashda xatolik yuz berdi."

    prompt = (
        "Sen mohir oshpaz va parhezshunossan. Rasmdagi muzlatgich ichidagi masalliqlardan 1 ta sog'lom retsept yoz. "
        "Matnda YULDUZCHA (*) umuman ishlatma! Faqat HTML teglardan foydalan: <b>, <i>, <code>\n\n"
        "🍳 <b>Taom nomi:</b> [Nomi]\n🥗 <b>Kerakli masalliqlar:</b>\n👨‍🍳 <b>Tayyorlanishi:</b>\n🔥 <b>Kaloriya:</b>"
    )

    api_keys = get_api_keys()
    for key in api_keys:
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(model=PRIMARY_MODEL, contents=[image_to_send, prompt])
            res_text = response.text.replace("**", "").replace("*", "")
            return res_text
        except Exception as e:
            if any(x in str(e) for x in ["429", "RESOURCE_EXHAUSTED", "401", "UNAUTHENTICATED", "503"]):
                continue
            return f"❌ AI xizmatida vaqtincha xatolik yuz berdi."
    return "😔 Hozirda barcha AI liniyalari band."

# =====================================================================
# 3. CHEKLOVLAR VA LIMITLAR LOGIKASI
# =====================================================================
async def check_and_update_limit(user_id: int, service_type: str, session) -> tuple[bool, str, bool]:
    today = date.today()
    result = await session.execute(select(User).where(User.telegram_id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        return False, "Foydalanuvchi topilmadi. /start buyrug'ini bosing.", False
        
    if user.last_request_date != today:
        user.fridge_requests_today = 0
        user.food_requests_today = 0
        user.coach_requests_today = 0
        user.last_request_date = today
        await session.commit()
    
    if user.is_premium and user.premium_expires_at and user.premium_expires_at < datetime.utcnow():
        user.is_premium = False
        await session.commit()

    status = "premium" if user.is_premium else "normal"
    max_limit = await get_limit_for_service(service_type, user.is_premium)
    
    if user.youtube_penalty and status == "normal":
        max_limit = max(0, max_limit - 1)
        
    current_usage = getattr(user, f"{service_type}_requests_today")
    
    if current_usage >= max_limit:
        if not user.is_premium and not user.youtube_bonus_claimed:
            return False, (
                f"❌ Bugungi tekin limitsiz tugadi!\n\n"
                f"Sizga <b>faqat 1 marta</b> beriladigan +1 bonus imkoniyatini taqdim eta olamiz. "
                f"YouTube kanalimizga obuna bo'ling! 🎁"
            ), True
        else:
            return False, f"❌ Kunlik limitingiz tugagan ({current_usage}/{max_limit}).", False

    setattr(user, f"{service_type}_requests_today", current_usage + 1)
    await session.commit()
    remains = max_limit - (current_usage + 1)
    return True, f"Kunlik limitingiz: {current_usage + 1}/{max_limit} (Qoldi: {remains})", False

# =====================================================================
# 4. TELEGRAM BOT VA HANDLERLAR
# =====================================================================
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

class BotStates(StatesGroup):
    waiting_for_coach_question = State()
    waiting_for_food_photo = State()
    waiting_for_fridge_image = State()
    waiting_for_youtube_link = State()
    waiting_for_payment_proof = State()
    waiting_for_admin_price = State()
    waiting_for_admin_card = State()
    waiting_for_admin_channel = State()
    waiting_for_admin_target_user = State()

def main_menu(is_admin: bool = False):
    kb = ReplyKeyboardBuilder()
    kb.button(text="🏋️‍♂️ AI Trener")
    kb.button(text="📸 Ovqat tahlili")
    kb.button(text="🥑 Aqlli Muzlatgich")
    kb.button(text="👑 Premium obuna")
    if is_admin:
        kb.button(text="🛠 Admin panel")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)

def cancel_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)

def coach_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Tugatish 🛑")]], resize_keyboard=True)

def admin_panel_markup():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Premium narxini o'zgartirish", callback_data="admin_set_price")],
        [InlineKeyboardButton(text="💳 Karta raqamini o'zgartirish", callback_data="admin_set_card")],
        [InlineKeyboardButton(text="🔗 Kanal linkini o'zgartirish", callback_data="admin_set_channel")],
        [InlineKeyboardButton(text="👑 Premium berish", callback_data="admin_give_premium")],
        [InlineKeyboardButton(text="📥 To'lov cheklarini ko'rish", callback_data="admin_view_proofs")]
    ])

def payment_proof_admin_markup(proof_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"payment_proof_approve_{proof_id}"),
         InlineKeyboardButton(text="❌ Rad etish", callback_data=f"payment_proof_deny_{proof_id}")]
    ])

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            session.add(User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name
            ))
            await session.commit()
        else:
            user.username = message.from_user.username
            user.full_name = message.from_user.full_name
            await session.commit()

    await message.answer(
        f"Assalomu alaykum, {message.from_user.first_name}! 👋\nFitAI shaxsiy AI hamrohingiz ishga tushdi.",
        reply_markup=main_menu(await is_admin(message.from_user.id))
    )

@dp.message(F.text == "❌ Bekor qilish")
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_menu(await is_admin(message.from_user.id)))

# --- AI TRENER ---
@dp.message(F.text.contains("AI Trener"))
async def trainer_start(message: types.Message, state: FSMContext):
    async with async_session() as session:
        has_access, msg_text, yt_btn = await check_and_update_limit(message.from_user.id, "coach", session)
        if not has_access:
            markup = main_menu()
            if yt_btn:
                channel_link, _ = await get_channel_config()
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Obuna bo'ldim ✅", callback_data="verify_yt_coach"),
                     InlineKeyboardButton(text="Kanalga o'tish 📺", url=channel_link)]
                ])
            await safe_reply_html(message, msg_text, reply_markup=markup)
            return
            
    await message.answer("Sog'lom turmush tarzi va fitnes bo'yicha savolingizni yozib yuboring:", reply_markup=coach_menu())
    await state.set_state(BotStates.waiting_for_coach_question)

@dp.message(F.text == "Tugatish 🛑", BotStates.waiting_for_coach_question)
async def exit_coach_mode(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Muloqot yakunlandi.", reply_markup=main_menu())

@dp.message(BotStates.waiting_for_coach_question)
async def trainer_answer(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("⚠️ Iltimos, faqat matnli xabar yuboring.")
        return
    wait_msg = await message.answer("AI o'ylamoqda... ⏳")
    reply = await get_ai_coach_response(message.text)
    
    try:
        await wait_msg.delete()
    except Exception:
        pass
        
    await safe_reply_html(message, reply, reply_markup=coach_menu())

# --- OVQAT TAHLILI ---
@dp.message(F.text.contains("Ovqat tahlili"))
async def food_analysis_start(message: types.Message, state: FSMContext):
    async with async_session() as session:
        has_access, msg_text, yt_btn = await check_and_update_limit(message.from_user.id, "food", session)
        if not has_access:
            markup = main_menu()
            if yt_btn:
                channel_link, _ = await get_channel_config()
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Obuna bo'ldim ✅", callback_data="verify_yt_food"),
                     InlineKeyboardButton(text="Kanalga o'tish 📺", url=channel_link)]
                ])
            await safe_reply_html(message, msg_text, reply_markup=markup)
            return
            
    await message.answer("Taom rasmini yuboring:", reply_markup=cancel_menu())
    await state.set_state(BotStates.waiting_for_food_photo)

@dp.message(BotStates.waiting_for_food_photo, F.photo)
async def food_analysis_process(message: types.Message, state: FSMContext):
    wait_msg = await message.answer("Rasm tahlil qilinmoqda... ⏳")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        ai_result = await analyze_food_image(file_bytes.read())
        
        try:
            await wait_msg.delete()
        except Exception:
            pass
            
        await safe_reply_html(message, ai_result, reply_markup=main_menu())
    except Exception:
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await message.answer("❌ Xatolik yuz berdi.", reply_markup=main_menu())
    await state.clear()

# --- AQLLI MUZLATGICH ---
@dp.message(F.text.contains("Aqlli Muzlatgich"))
async def start_fridge_analysis(message: types.Message, state: FSMContext):
    async with async_session() as session:
        has_access, msg_text, yt_btn = await check_and_update_limit(message.from_user.id, "fridge", session)
        if not has_access:
            markup = main_menu()
            if yt_btn:
                channel_link, _ = await get_channel_config()
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Obuna bo'ldim ✅", callback_data="verify_yt_fridge"),
                     InlineKeyboardButton(text="Kanalga o'tish 📺", url=channel_link)]
                ])
            await safe_reply_html(message, msg_text, reply_markup=markup)
            return
            
    await message.answer("Muzlatgichingiz rasmini yuboring:", reply_markup=cancel_menu())
    await state.set_state(BotStates.waiting_for_fridge_image)

@dp.message(BotStates.waiting_for_fridge_image, F.photo)
async def process_fridge_image(message: types.Message, state: FSMContext):
    wait_msg = await message.answer("Rasm tahlil qilinmoqda... ⏳")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        recipe = await analyze_fridge_image(file_bytes.read())
        
        try:
            await wait_msg.delete()
        except Exception:
            pass
            
        await safe_reply_html(message, recipe, reply_markup=main_menu())
    except Exception:
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await message.answer("❌ Xatolik yuz berdi.", reply_markup=main_menu())
    await state.clear()

# --- YOUTUBE OBUNASINI TEKSHIRISH ---
@dp.callback_query(F.data.startswith("verify_yt_"))
async def prompt_youtube_verification(query: types.CallbackQuery, state: FSMContext):
    tool_name = query.data.replace("verify_yt_", "")
    channel_link, _ = await get_channel_config()
    await query.message.answer(
        f"Kanalga obuna bo'ling: {channel_link}\n\nSo'ng kanalingiz @username'ingizni yuboring:",
        reply_markup=cancel_menu()
    )
    await state.update_data(yt_target_tool=tool_name)
    await state.set_state(BotStates.waiting_for_youtube_link)
    await query.answer()

@dp.message(BotStates.waiting_for_youtube_link)
async def process_youtube_link(message: types.Message, state: FSMContext):
    wait_msg = await message.answer("Tekshirilmoqda... ⏳")
    state_data = await state.get_data()
    target_tool = state_data.get("yt_target_tool", "food")
    
    try:
        await wait_msg.delete()
    except Exception:
        pass
    
    async with async_session() as session:
        user_res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = user_res.scalar_one_or_none()
        if user:
            user.youtube_bonus_claimed = True
            user.youtube_channel_id = message.text
            user.youtube_penalty = False
            
            current_usage = getattr(user, f"{target_tool}_requests_today")
            if current_usage > 0:
                setattr(user, f"{target_tool}_requests_today", current_usage - 1)
            await session.commit()
    
    await message.answer("🎉 Tasdiqlandi! +1 bonus taqdim etildi.", reply_markup=main_menu())
    await state.clear()

# --- PREMIUM TO'LOVI (CHEK TIZIMI) ---
@dp.message(F.text.contains("Premium obuna"))
async def send_payment_proof_request(message: types.Message, state: FSMContext):
    card_number = await get_payment_card()
    premium_price = await get_premium_price()
    channel_link, _ = await get_channel_config()

    description = (
        f"👑 <b>Premium obuna narxi:</b> {premium_price} UZS\n\n"
        f"💳 <b>Karta raqami:</b> {card_number}\n\n"
        f"📷 <b>To'lovni rasmga olib chekni yuboring.</b>\n\n"
        f"🔗 <b>Kanal:</b> {channel_link}"
    )
    await message.answer(description, parse_mode="HTML", reply_markup=cancel_menu())
    await state.set_state(BotStates.waiting_for_payment_proof)

@dp.message(BotStates.waiting_for_payment_proof, F.photo)
async def process_payment_proof(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    async with async_session() as session:
        proof = PaymentProof(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            file_id=photo.file_id,
            status="pending"
        )
        session.add(proof)
        await session.commit()
        proof_id = proof.id

    await message.answer(
        "✅ Chek qabul qilindi. Ma'muriy tekshiruvdan so'ng sizga xabar beramiz.",
        reply_markup=main_menu(await is_admin(message.from_user.id))
    )

    admin_text = (
        f"📥 Yangi to'lov cheki qabul qilindi\n"
        f"👤 {message.from_user.full_name} (@{message.from_user.username or '---'})\n"
        f"ID: {message.from_user.id}\n"
        f"Proof ID: {proof_id}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                chat_id=admin_id,
                photo=photo.file_id,
                caption=admin_text,
                reply_markup=payment_proof_admin_markup(proof_id)
            )
        except Exception:
            pass

    await state.clear()

@dp.callback_query(F.data.startswith("payment_proof_"))
async def handle_payment_proof_review(query: types.CallbackQuery):
    if not await is_admin(query.from_user.id):
        await query.answer("Siz admin emassiz.", show_alert=True)
        return

    parts = query.data.split("_")
    action = parts[2]
    proof_id = int(parts[3])
    
    async with async_session() as session:
        proof = await session.get(PaymentProof, proof_id)
        if not proof:
            await query.answer("Chek topilmadi.", show_alert=True)
            return
        
        if action == "approve":
            proof.status = "approved"
            expiry = datetime.utcnow() + timedelta(days=30)
            result = await session.execute(select(User).where(User.telegram_id == proof.telegram_id))
            user = result.scalar_one_or_none()
            if user:
                user.is_premium = True
                user.premium_expires_at = expiry
            await session.commit()
            try:
                await bot.send_message(
                    chat_id=proof.telegram_id,
                    text="🎉 Sizning to'lovingiz tasdiqlandi! Premium 30 kunga faollashtirildi."
                )
            except Exception:
                pass
            await query.answer("Tasdiqlandi.")
        elif action == "deny":
            proof.status = "denied"
            await session.commit()
            try:
                await bot.send_message(
                    chat_id=proof.telegram_id,
                    text="❌ Chek rad etildi. Iltimos, qayta urinib ko'ring."
                )
            except Exception:
                pass
            await query.answer("Rad etildi.")

# --- ADMIN PANEL ---
@dp.message(F.text == "🛠 Admin panel")
async def open_admin_panel(message: types.Message):
    if not await is_admin(message.from_user.id):
        await message.answer("Faqat adminlar uchun.")
        return
    await message.answer("Admin panel:", reply_markup=admin_panel_markup())

@dp.callback_query(F.data == "admin_set_price")
async def admin_set_price(query: types.CallbackQuery, state: FSMContext):
    if not await is_admin(query.from_user.id):
        await query.answer("Faqat adminlar uchun.", show_alert=True)
        return
    await query.message.answer("Yangi premium narxini kiriting (faqat raqam):")
    await state.set_state(BotStates.waiting_for_admin_price)
    await query.answer()

@dp.callback_query(F.data == "admin_set_card")
async def admin_set_card(query: types.CallbackQuery, state: FSMContext):
    if not await is_admin(query.from_user.id):
        await query.answer("Faqat adminlar uchun.", show_alert=True)
        return
    await query.message.answer("Yangi karta raqamini kiriting:")
    await state.set_state(BotStates.waiting_for_admin_card)
    await query.answer()

@dp.callback_query(F.data == "admin_set_channel")
async def admin_set_channel(query: types.CallbackQuery, state: FSMContext):
    if not await is_admin(query.from_user.id):
        await query.answer("Faqat adminlar uchun.", show_alert=True)
        return
    await query.message.answer("Yangi kanal linkini kiriting (YouTube yoki Telegram):")
    await state.set_state(BotStates.waiting_for_admin_channel)
    await query.answer()

@dp.callback_query(F.data == "admin_give_premium")
async def admin_give_premium(query: types.CallbackQuery, state: FSMContext):
    if not await is_admin(query.from_user.id):
        await query.answer("Faqat adminlar uchun.", show_alert=True)
        return
    await query.message.answer("Foydalanuvchi @username yoki ID sini kiriting:")
    await state.set_state(BotStates.waiting_for_admin_target_user)
    await query.answer()

@dp.callback_query(F.data == "admin_view_proofs")
async def admin_view_proofs(query: types.CallbackQuery):
    if not await is_admin(query.from_user.id):
        await query.answer("Faqat adminlar uchun.", show_alert=True)
        return
    async with async_session() as session:
        proofs = (await session.execute(select(PaymentProof).where(PaymentProof.status == "pending"))).scalars().all()
        if not proofs:
            await query.message.answer("Tasdiqlanadigan cheklar yo'q.")
            await query.answer()
            return
        for proof in proofs:
            try:
                await bot.send_photo(
                    chat_id=query.from_user.id,
                    photo=proof.file_id,
                    caption=f"ID: {proof.id}\n{proof.full_name}",
                    reply_markup=payment_proof_admin_markup(proof.id)
                )
            except Exception:
                pass
    await query.answer()

@dp.message(BotStates.waiting_for_admin_price)
async def process_admin_price(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    new_price = re.sub(r"[^0-9]", "", message.text)
    if not new_price:
        await message.answer("Faqat raqam kiriting.")
        return
    await set_setting_value("premium_price", new_price)
    await message.answer(f"Narx o'zgartirildi: {new_price}", reply_markup=main_menu(True))
    await state.clear()

@dp.message(BotStates.waiting_for_admin_card)
async def process_admin_card(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    new_card = message.text.strip()
    await set_setting_value("payment_card", new_card)
    await message.answer(f"Karta o'zgartirildi: {new_card}", reply_markup=main_menu(True))
    await state.clear()

@dp.message(BotStates.waiting_for_admin_channel)
async def process_admin_channel(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    new_channel = message.text.strip()
    channel_type = "telegram" if "t.me" in new_channel or new_channel.startswith("@") else "youtube"
    await set_setting_value("channel_link", new_channel)
    await set_setting_value("channel_type", channel_type)
    async with async_session() as session:
        await session.execute(update(User).values(youtube_bonus_claimed=False, youtube_channel_id=None))
        await session.commit()
    await message.answer(f"Kanal o'zgartirildi: {new_channel}", reply_markup=main_menu(True))
    await state.clear()

@dp.message(BotStates.waiting_for_admin_target_user)
async def process_admin_give_premium(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    target = message.text.strip()
    user_id = None
    if target.startswith("@"):
        async with async_session() as session:
            result = await session.execute(select(User).where(User.username == target[1:]))
            user_obj = result.scalar_one_or_none()
            if user_obj:
                user_id = user_obj.telegram_id
    elif target.isdigit():
        user_id = int(target)
    
    if not user_id:
        await message.answer("Foydalanuvchi topilmadi.")
        return
    
    expiry = datetime.utcnow() + timedelta(days=30)
    async with async_session() as session:
        await session.execute(update(User).where(User.telegram_id == user_id).values(is_premium=True, premium_expires_at=expiry))
        await session.commit()
    
    await message.answer(f"Premium berildi: {target}", reply_markup=main_menu(True))
    try:
        await bot.send_message(user_id, "👑 Admin tomonidan premium berildi!")
    except Exception:
        pass
    await state.clear()

# --- CRON TASKI ---
async def check_unsubscribers_cron():
    while True:
        await asyncio.sleep(86400)
        try:
            async with async_session() as session:
                users = (await session.execute(select(User).where(User.youtube_bonus_claimed == True))).scalars().all()
                for user in users:
                    if not user.youtube_penalty:
                        user.youtube_penalty = True
                        try:
                            await bot.send_message(
                                chat_id=user.telegram_id,
                                text="⚠️ Obunani bekor qilganingiz aniqlandi. Limit -1 ga kamaytirildi."
                            )
                        except Exception:
                            pass
                await session.commit()
        except Exception:
            pass

async def handle(request):
    return web.Response(text="FitAI Bot is running successfully!")

async def main():
    await init_db()
    logging.basicConfig(level=logging.INFO)
    print("FitAI Telegram boti muvaffaqiyatli ishga tushdi...")
    
    # --- RENDER UCHUN PORTNI BAND QILISH ---
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080)) # Render o'zi PORT muhitini beradi
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    # --------------------------------------

    asyncio.create_task(check_unsubscribers_cron())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())