import os
import re
import html
import json
import logging
import asyncio
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.filters import Command, Filter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)

# ═══════════════════════════════════════════════════
#          КОНФИГУРАЦИЯ И СИСТЕМНЫЕ НАСТРОЙКИ
# ═══════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))  
ADMIN_SECRET_PASSWORD = os.getenv("ADMIN_SECRET_PASSWORD", "prime_secret_2026")
DB_FILE = os.getenv("DB_FILE", "clinic_bot.db")

# Ссылка на веб-приложение (Mini App) на GitHub Pages
MINI_APP_URL = "https://masynov.github.io/Clinic-Bot/"

ACTIVE_ADMINS = set()

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

DB_DOCTORS = {
    "cardio_1": {
        "name": "Иванов Иван Иванович",
        "spec": "Кардиолог высшей категории",
        "exp": "14 лет",
        "portfolio": "Выпускник Первого МГМУ им. Сеченова. Специалист по превентивной кардиологии.",
        "phone": "+7 (999) 123-45-67",
        "tg": "@dr_ivanov"
    },
    "ther_1": {
        "name": "Петрова Анна Сергеевна",
        "spec": "Терапевт общей практики",
        "exp": "8 лет",
        "portfolio": "Специализируется на комплексной диагностике внутренних органов.",
        "phone": "+7 (999) 765-43-21",
        "tg": "@dr_petrova"
    }
}

class BookingStates(StatesGroup):
    choosing_direction = State()
    choosing_doctor = State()
    choosing_date = State()
    entering_name = State()
    entering_birthdate = State()
    entering_phone = State()
    attaching_file = State()
    entering_comment = State()
    confirming = State()

class AdminStates(StatesGroup):
    entering_broadcast_text = State()
    choosing_broadcast_segment = State()
    typing_reply = State()

# ═══════════════════════════════════════════════════
#        АСИНХРОННАЯ РАБОТА С СУБД (AIOSQLITE)
# ═══════════════════════════════════════════════════

async def init_db():
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                fullname TEXT,
                phone TEXT,
                utm_source TEXT,
                last_direction TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                chat_id INTEGER,
                message_id INTEGER,
                PRIMARY KEY (chat_id, message_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                user_id INTEGER PRIMARY KEY,
                fullname TEXT,
                phone TEXT,
                direction TEXT,
                utm_source TEXT,
                comment TEXT,
                file_id TEXT,
                status TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews_stats (
                id INTEGER PRIMARY KEY,
                total_stars INTEGER,
                count INTEGER
            )
        """)
        
        async with conn.execute("SELECT COUNT(*) FROM reviews_stats") as cursor:
            row = await cursor.fetchone()
            if not row or row[0] == 0:
                await conn.execute("INSERT INTO reviews_stats (id, total_stars, count) VALUES (1, 493, 100)")
                
        await conn.commit()

async def db_track_msg(chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("INSERT OR IGNORE INTO chat_history (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id))
        await conn.commit()

async def db_get_chat_history(chat_id: int):
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT message_id FROM chat_history WHERE chat_id = ?", (chat_id,)) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

async def db_clear_chat_history(chat_id: int):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
        await conn.commit()

async def db_register_user(user_id: int, fullname: str, utm_source: str):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("""
            INSERT OR IGNORE INTO users (user_id, fullname, phone, utm_source, last_direction)
            VALUES (?, ?, NULL, ?, NULL)
        """, (user_id, fullname, utm_source))
        await conn.commit()

async def db_update_user_profile(user_id: int, fullname: str = None, phone: str = None, direction: str = None):
    async with aiosqlite.connect(DB_FILE) as conn:
        if fullname:
            await conn.execute("UPDATE users SET fullname = ? WHERE user_id = ?", (fullname, user_id))
        if phone:
            await conn.execute("UPDATE users SET phone = ? WHERE user_id = ?", (phone, user_id))
        if direction:
            await conn.execute("UPDATE users SET last_direction = ? WHERE user_id = ?", (direction, user_id))
        await conn.commit()

async def db_get_user(user_id: int):
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT fullname, phone, utm_source, last_direction FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"fullname": row[0], "phone": row[1], "utm_source": row[2], "last_direction": row[3]}
    return None

async def db_get_all_users():
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT user_id, fullname, phone, utm_source, last_direction FROM users") as cursor:
            rows = await cursor.fetchall()
            return {r[0]: {"fullname": r[1], "phone": r[2], "utm_source": r[3], "last_direction": r[4]} for r in rows}

async def db_add_application(user_id: int, fullname: str, phone: str, direction: str, utm_source: str, comment: str, file_id: str):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("""
            INSERT OR REPLACE INTO applications (user_id, fullname, phone, direction, utm_source, comment, file_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, '⏳ На модерации')
        """, (user_id, fullname, phone, direction, utm_source, comment, file_id))
        await conn.commit()

async def db_get_user_application(user_id: int):
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT fullname, phone, direction, utm_source, comment, file_id, status FROM applications WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"fullname": row[0], "phone": row[1], "direction": row[2], "utm_source": row[3], "comment": row[4], "file_id": row[5], "status": row[6]}
    return None

async def db_update_application_status(user_id: int, status: str):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("UPDATE applications SET status = ? WHERE user_id = ?", (status, user_id))
        await conn.commit()

async def db_get_all_applications():
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT user_id, fullname, phone, direction, utm_source, comment, file_id, status FROM applications") as cursor:
            rows = await cursor.fetchall()
            return {r[0]: {"fullname": r[1], "phone": r[2], "direction": r[3], "utm_source": r[4], "comment": r[5], "file_id": r[6], "status": r[7]} for r in rows}

async def db_get_reviews_stats():
    async with aiosqlite.connect(DB_FILE) as conn:
        async with conn.execute("SELECT total_stars, count FROM reviews_stats WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            return {"total_stars": row[0], "count": row[1]}

async def db_update_reviews_stats(stars: int):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("UPDATE reviews_stats SET total_stars = total_stars + ?, count = count + 1 WHERE id = 1", (stars,))
        await conn.commit()

# ═══════════════════════════════════════════════════
#    АВТО-ПЕРЕХВАТ (MIDDLEWARE) И ОЧИСТКА ЧАТА
# ═══════════════════════════════════════════════════

class AutoMessageTrackerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            await db_track_msg(event.chat.id, event.message_id)
        return await handler(event, data)

class IsAdminFilter(Filter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == ADMIN_CHAT_ID or message.from_user.id in ACTIVE_ADMINS

def validate_russian_phone(phone: str) -> str | None:
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits.startswith(('7', '8')):
        return f"+7{digits[1:]}"
    elif len(digits) == 10 and digits.startswith('9'):
        return f"+7{digits}"
    return None

def get_progress_bar(step: int, total: int = 8) -> str:
    green_blocks = "🟢" * step
    white_blocks = "⚪" * (total - step)
    percent = int((step / total) * 100)
    return f"\n<b>Этап:</b> {green_blocks}{white_blocks} {percent}%\n"

async def track_msg(chat_id: int, msg_id: int):
    await db_track_msg(chat_id, msg_id)

async def clear_chat_history(chat_id: int):
    messages_to_delete = await db_get_chat_history(chat_id)
    for msg_id in messages_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  
    await db_clear_chat_history(chat_id)

# ═══════════════════════════════════════════════════
#                  ИНТЕРФЕЙСНЫЕ КНОПКИ
# ═══════════════════════════════════════════════════

def get_full_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚀 Открыть Mini App", web_app=WebAppInfo(url=MINI_APP_URL))],
            [KeyboardButton(text="🩺 Оставить заявку на прием")],
            [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="⭐ Отзывы клиники")],
            [KeyboardButton(text="ℹ️ Служба поддержки (FAQ)"), KeyboardButton(text="📞 Связаться с оператором")],
            [KeyboardButton(text="💰 Цены"), KeyboardButton(text="📍 Адреса")],
            [KeyboardButton(text="💝 Пожертвовать клинике")]
        ],
        resize_keyboard=True
    )

def get_directions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🩺 Кардиология", callback_data="dir_cardio")],
        [InlineKeyboardButton(text="🩺 Терапия", callback_data="dir_therapy")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_booking")]
    ])

def get_skip_kb(callback_action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ Пропустить этот шаг", callback_data=callback_action)],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_booking")]
    ])

# ═══════════════════════════════════════════════════
#             ОБНОВЛЕННАЯ КОМАНДА /START
# ═══════════════════════════════════════════════════

@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await clear_chat_history(message.chat.id)
    await state.clear()

    utm_source = command.args if command.args else "Прямой переход"
    await db_register_user(message.from_user.id, message.from_user.full_name, utm_source)

    welcome_text = (
        "<b>🩺 МЕДИЦИНСКИЙ ЦЕНТР «ПРАЙМ»</b>\n"
        "<blockquote>Добро пожаловать в единую цифровую систему управления Вашим здоровьем. All-in-one платформа для связи с клиникой.</blockquote>\n"
        "Все доступные функции структурированы в нижнем меню взаимодействия.\n\n"
        "⚠️ <i><b>Примечание:</b> Данный бот является исключительно демонстрационным проектом. Медицинский центр «ПРАЙМ» вымышлен.</i>"
    )
    
    start_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Запустить Mini App", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])

    res = await message.answer(welcome_text, reply_markup=start_kb, parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)
    
    menu_res = await message.answer("Главное меню:", reply_markup=get_full_main_menu())
    await track_msg(message.chat.id, menu_res.message_id)

# ═══════════════════════════════════════════════════
#  ОБНОВЛЕННЫЙ ЛИЧНЫЙ КАБИНЕТ + ИНТЕГРАЦИЯ MINI APP
# ═══════════════════════════════════════════════════

@router.message(F.text == "👤 Личный кабинет")
async def user_cabinet(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db_get_user(user_id) or {}
    app_data = await db_get_user_application(user_id)

    fullname = user_data.get("fullname") or message.from_user.full_name
    phone = user_data.get("phone") or "Не указан (заполняется при записи)"
    utm = user_data.get("utm_source") or "Прямой переход"
    last_dir = user_data.get("last_direction") or "Нет активных направлений"

    # Формирование статуса заявки из БД
    if app_data:
        app_status_text = (
            f"\n\n<b>📋 ТЕКУЩАЯ ЗАЯВКА НА ПРИЕМ:</b>\n"
            f"• Направление: {app_data['direction']}\n"
            f"• Телефон: <code>{app_data['phone']}</code>\n"
            f"• Статус заявки: <b>{app_data['status']}</b>\n"
            f"• Комментарий: <i>{app_data['comment']}</i>"
        )
    else:
        app_status_text = "\n\n<b>📋 ТЕКУЩАЯ ЗАЯВКА:</b>\n<i>Активных записей на прием не найдено.</i>"

    cabinet_text = (
        "<b>👤 ЭЛЕКТРОННАЯ КАРТА ПАЦИЕНТА</b>\n\n"
        f"• <b>ФИО:</b> {fullname}\n"
        f"• <b>Контактный телефон:</b> <code>{phone}</code>\n"
        f"• <b>ID Пациента:</b> <code>{user_id}</code>\n"
        f"• <b>Источник регистрации:</b> <code>{utm}</code>\n"
        f"• <b>Предпочтительное отделение:</b> {last_dir}"
        f"{app_status_text}\n\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        "💡 <i>Вы можете полностью управлять Вашим профилем, расписанием и медицинскими картами через наш интеракативный <b>Mini App</b>.</i>"
    )

    cabinet_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Кабинет в Mini App", web_app=WebAppInfo(url=MINI_APP_URL))],
        [InlineKeyboardButton(text="🔄 Обновить статус", callback_data="refresh_cabinet")]
    ])

    res = await message.answer(cabinet_text, reply_markup=cabinet_kb, parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.callback_query(F.data == "refresh_cabinet")
async def refresh_cabinet_handler(callback: CallbackQuery):
    await callback.answer("Данные обновлены ✅")
    user_id = callback.from_user.id
    user_data = await db_get_user(user_id) or {}
    app_data = await db_get_user_application(user_id)

    fullname = user_data.get("fullname") or callback.from_user.full_name
    phone = user_data.get("phone") or "Не указан"
    utm = user_data.get("utm_source") or "Прямой переход"
    last_dir = user_data.get("last_direction") or "Нет активных направлений"

    if app_data:
        app_status_text = (
            f"\n\n<b>📋 ТЕКУЩАЯ ЗАЯВКА НА ПРИЕМ:</b>\n"
            f"• Направление: {app_data['direction']}\n"
            f"• Телефон: <code>{app_data['phone']}</code>\n"
            f"• Статус заявки: <b>{app_data['status']}</b>\n"
            f"• Комментарий: <i>{app_data['comment']}</i>"
        )
    else:
        app_status_text = "\n\n<b>📋 ТЕКУЩАЯ ЗАЯВКА:</b>\n<i>Активных записей на прием не найдено.</i>"

    cabinet_text = (
        "<b>👤 ЭЛЕКТРОННАЯ КАРТА ПАЦИЕНТА</b>\n\n"
        f"• <b>ФИО:</b> {fullname}\n"
        f"• <b>Контактный телефон:</b> <code>{phone}</code>\n"
        f"• <b>ID Пациента:</b> <code>{user_id}</code>\n"
        f"• <b>Источник регистрации:</b> <code>{utm}</code>\n"
        f"• <b>Предпочтительное отделение:</b> {last_dir}"
        f"{app_status_text}\n\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        "💡 <i>Вы можете полностью управлять Вашим профилем, расписанием и медицинскими картами через наш интеракативный <b>Mini App</b>.</i>"
    )

    cabinet_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Кабинет в Mini App", web_app=WebAppInfo(url=MINI_APP_URL))],
        [InlineKeyboardButton(text="🔄 Обновить статус", callback_data="refresh_cabinet")]
    ])

    try:
        await callback.message.edit_text(cabinet_text, reply_markup=cabinet_kb, parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ═══════════════════════════════════════════════════
#   ПЕРЕХВАТ ДАННЫХ, ОТПРАВЛЕННЫХ ИЗ MINI APP
# ═══════════════════════════════════════════════════

@router.message(F.web_app_data)
async def handle_web_app_data(message: Message):
    """Ловит отправленные через Telegram.WebApp.sendData() из фронтенда данные"""
    try:
        raw_json = message.web_app_data.data
        data = json.loads(raw_json)
        
        user_id = message.from_user.id
        action = data.get("action", "unknown")

        if action == "update_profile":
            fullname = data.get("fullname")
            phone = data.get("phone")
            await db_update_user_profile(user_id, fullname=fullname, phone=phone)
            await message.answer("✅ <b>Данные вашего профиля успешно синхронизированы из Mini App!</b>", parse_mode=ParseMode.HTML)
            
        elif action == "quick_booking":
            direction = data.get("direction", "Общая диагностика")
            phone = data.get("phone", "Не указан")
            await db_add_application(user_id, message.from_user.full_name, phone, direction, "Mini App Direct", "Заявка создана через Mini App", None)
            await message.answer("🚀 <b>Ваша заявка из Mini App принята и направлена на модерацию!</b>", parse_mode=ParseMode.HTML)
            
        else:
            await message.answer(f"📥 <b>Получены данные из веб-приложения:</b>\n<code>{html.escape(raw_json)}</code>", parse_mode=ParseMode.HTML)

    except Exception as e:
        logging.error(f"Ошибка при обработке WebApp Data: {e}")
        await message.answer("⚠️ Произошла ошибка при обработке данных из Mini App.")

# ═══════════════════════════════════════════════════
#         ДИНАМИЧЕСКАЯ СИСТЕМА ОТЗЫВОВ
# ═══════════════════════════════════════════════════

@router.message(F.text == "⭐ Отзывы клиники")
async def review_handler(message: Message, state: FSMContext):
    stats = await db_get_reviews_stats()
    avg_rating = stats["total_stars"] / stats["count"]
    
    stars_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐", callback_data="rev_1"), InlineKeyboardButton(text="⭐⭐", callback_data="rev_2"), InlineKeyboardButton(text="⭐⭐⭐", callback_data="rev_3")],
        [InlineKeyboardButton(text="⭐⭐⭐⭐", callback_data="rev_4"), InlineKeyboardButton(text="⭐⭐⭐⭐⭐", callback_data="rev_5")],
        [InlineKeyboardButton(text="📖 Прочитать отзывы пациентов", callback_data="view_other_reviews")]
    ])
    
    res = await message.answer(
        f"<b>📊 Динамический рейтинг клиники: {avg_rating:.2f} / 5.00 ⭐</b>\n"
        f"<i>(Всего получено оценок от пациентов: {stats['count']})</i>\n\n"
        f"Пожалуйста, оцените качество обслуживания в нашей сети или прочитайте отзывы других людей:", 
        reply_markup=stars_markup, 
        parse_mode=ParseMode.HTML
    )
    await track_msg(message.chat.id, res.message_id)

@router.callback_query(F.data == "view_other_reviews")
async def callback_view_reviews(callback: CallbackQuery):
    reviews_text = (
        "<b>💬 Последние отзывы наших пациентов:</b>\n\n"
        "1. ⭐⭐⭐⭐⭐ <b>Елена К.</b>\n"
        "<i>«Прекрасный кардиолог Иванов И.И.! Очень внимательно отнесся к проблеме, изучил КТ-снимок и всё подробно объяснил. Клиника чистая, персонал вежливый.»</i>\n\n"
        "2. ⭐⭐⭐⭐⭐ <b>Михаил Т.</b>\n"
        "<i>«Обращался к терапевту Петровой. Быстро поставили диагноз, направили на анализы. Результаты пришли на почту уже вечером. Настоящие профессионалы!»</i>\n\n"
        "3. ⭐⭐⭐⭐ <b>Ольга Б.</b>\n"
        "<i>«Лечение отличное, но парковка перед клиникой была полностью занята, пришлось просить администратора открыть резервный шлагбаум. В остальном всё супер.»</i>"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад к оценке", callback_data="back_to_reviews_main")]
    ])
    await callback.message.edit_text(reviews_text, reply_markup=back_kb, parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "back_to_reviews_main")
async def callback_back_to_reviews_main(callback: CallbackQuery):
    stats = await db_get_reviews_stats()
    avg_rating = stats["total_stars"] / stats["count"]
    
    stars_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐", callback_data="rev_1"), InlineKeyboardButton(text="⭐⭐", callback_data="rev_2"), InlineKeyboardButton(text="⭐⭐⭐", callback_data="rev_3")],
        [InlineKeyboardButton(text="⭐⭐⭐⭐", callback_data="rev_4"), InlineKeyboardButton(text="⭐⭐⭐⭐⭐", callback_data="rev_5")],
        [InlineKeyboardButton(text="📖 Прочитать отзывы пациентов", callback_data="view_other_reviews")]
    ])
    await callback.message.edit_text(
        f"<b>📊 Динамический рейтинг клиники: {avg_rating:.2f} / 5.00 ⭐</b>\n"
        f"<i>(Всего получено оценок от пациентов: {stats['count']})</i>\n\n"
        f"Пожалуйста, оцените качество обслуживания в нашей сети или прочитайте отзывы других людей:", 
        reply_markup=stars_markup, 
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data.startswith("rev_"))
async def process_smart_review(callback: CallbackQuery):
    rating = int(callback.data.split("_")[1])
    await callback.answer()
    
    await db_update_reviews_stats(rating)
    stats = await db_get_reviews_stats()
    new_avg = stats["total_stars"] / stats["count"]
    
    if rating >= 4:
        good_text = (
            f"✅ <b>Большое спасибо за Вашу оценку ({rating}/5)!</b>\n\n"
            f"Благодаря Вам наш текущий рейтинг поднялся до <b>{new_avg:.2f} ⭐</b>.\n"
            "Мы будем искренне признательны, если Вы продублируете свой отзыв на независимых площадках:\n"
            "🌐 <a href='https://yandex.ru/maps'>Яндекс.Карты</a>\n"
            "🌐 <a href='https://prodoctorov.ru'>Портал ПроДокторов</a>"
        )
        await callback.message.edit_text(good_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    else:
        bad_text = (
            f"⚠️ <b>Принято. Нам очень жаль, что Вы столкнулись с неудобствами ({rating}/5).</b>\n\n"
            "Ваш отзыв переведен в статус <b>«Претензия»</b> и направлен напрямую директору клиники. "
            "Служба контроля качества свяжется с Вами для урегулирования ситуации в течение 30 минут."
        )
        await callback.message.edit_text(bad_text, parse_mode=ParseMode.HTML)
        
        targets = list(ACTIVE_ADMINS)
        if ADMIN_CHAT_ID != 0:
            targets.append(ADMIN_CHAT_ID)
        for chat_id in set(targets):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🚨 <b>ЖАЛОБА/НЕГАТИВНЫЙ ОТЗЫВ</b>\n• Пациент: ID {callback.from_user.id}\n• Оценка: {rating} из 5\n• Требуется срочное вмешательство руководства!",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

# ═══════════════════════════════════════════════════
#             БЕСПЛАТНОЕ АНКЕТИРОВАНИЕ (FSM)
# ═══════════════════════════════════════════════════

@router.message(F.text == "🩺 Оставить заявку на прием")
async def start_booking(message: Message, state: FSMContext):
    await state.set_state(BookingStates.choosing_direction)
    progress = get_progress_bar(1)
    res = await message.answer(f"{progress}\n<b>Шаг 1 из 8:</b> Выберите интересующее медицинское направление:", reply_markup=get_directions_kb(), parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.callback_query(F.data == "cancel_booking")
async def cancel_booking_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Заполнение электронной анкеты прервано.")

@router.callback_query(BookingStates.choosing_direction, F.data.startswith("dir_"))
async def process_direction(callback: CallbackQuery, state: FSMContext):
    direction = callback.data
    await state.update_data(direction=direction)
    await db_update_user_profile(callback.from_user.id, direction=direction)
        
    await state.set_state(BookingStates.choosing_doctor)
    progress = get_progress_bar(2)
    buttons = []
    if direction == "dir_cardio":
        buttons.append([InlineKeyboardButton(text="👨‍⚕️ д.м.н. Иванов И.И.", callback_data="doc_cardio_1")])
    elif direction == "dir_therapy":
        buttons.append([InlineKeyboardButton(text="👩‍⚕️ к.м.н.  Петрова А.С.", callback_data="doc_ther_1")])
    buttons.append([InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_booking")])
    
    await callback.message.edit_text(f"{progress}\n<b>Шаг 2 из 8:</b> Выберите лечащего специалиста:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML)

@router.callback_query(BookingStates.choosing_doctor, F.data.startswith("doc_"))
async def process_doctor(callback: CallbackQuery, state: FSMContext):
    await state.update_data(doctor_id=callback.data.replace("doc_", ""))
    await state.set_state(BookingStates.choosing_date)
    
    progress = get_progress_bar(3)
    dates_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ближайший рабочий день", callback_data="date_next_day")],
        [InlineKeyboardButton(text="Выходной день (суббота)", callback_data="date_weekend")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_booking")]
    ])
    await callback.message.edit_text(f"{progress}\n<b>Шаг 3 из 8:</b> Укажите желаемый временной диапазон для визита:", reply_markup=dates_kb, parse_mode=ParseMode.HTML)

@router.callback_query(BookingStates.choosing_date, F.data.startswith("date_"))
async def process_date(callback: CallbackQuery, state: FSMContext):
    date_text = "Будние дни" if callback.data == "date_next_day" else "Субботний прием"
    await state.update_data(date=date_text)
    await state.set_state(BookingStates.entering_name)
    
    progress = get_progress_bar(4)
    await callback.message.edit_text(f"{progress}\n<b>Шаг 4 из 8:</b> Введите Ваши полные ФИО для медицинской карты:", parse_mode=ParseMode.HTML)

@router.message(BookingStates.entering_name, F.text)
async def process_name(message: Message, state: FSMContext):
    fullname = html.escape(message.text.strip())
    await state.update_data(fullname=fullname)
    
    await state.set_state(BookingStates.entering_birthdate)
    progress = get_progress_bar(5)
    res = await message.answer(f"{progress}\n<b>Шаг 5 из 8:</b> Укажите Вашу дату рождения (в формате <i>ДД.ММ.ГГГГ</i>):", parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.message(BookingStates.entering_birthdate, F.text)
async def process_birthdate(message: Message, state: FSMContext):
    await state.update_data(birthdate=html.escape(message.text.strip()))
    await state.set_state(BookingStates.entering_phone)
    progress = get_progress_bar(6)
    res = await message.answer(f"{progress}\n<b>Шаг 6 из 8:</b> Введите контактный номер телефона в российском формате:", parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.message(BookingStates.entering_phone, F.text)
async def process_phone(message: Message, state: FSMContext):
    validated_phone = validate_russian_phone(message.text.strip())
    if not validated_phone:
        res = await message.answer("❌ <b>Ошибка формата.</b> Требуется корректный российский номер (11 цифр). Попробуйте снова:", parse_mode=ParseMode.HTML)
        await track_msg(message.chat.id, res.message_id)
        return
    await state.update_data(phone=validated_phone)
    await state.set_state(BookingStates.attaching_file)
    
    progress = get_progress_bar(7)
    res = await message.answer(
        f"{progress}\n<b>Шаг 7 из 8 (ВТОРОЕ МНЕНИЕ):</b> Прикрепите рентген-снимок или КТ-исследование, если имеются на руках:", 
        reply_markup=get_skip_kb("skip_file"), 
        parse_mode=ParseMode.HTML
    )
    await track_msg(message.chat.id, res.message_id)

@router.message(BookingStates.attaching_file, F.photo | F.document)
async def process_file_upload(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    await state.update_data(file_id=file_id)
    await state.set_state(BookingStates.entering_comment)
    
    progress = get_progress_bar(8)
    res = await message.answer(f"{progress}\n<b>Шаг 8 из 8:</b> Кратко опишите симптомы, жалобы или цель визита:", reply_markup=get_skip_kb("skip_comment"), parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.callback_query(BookingStates.attaching_file, F.data == "skip_file")
async def skip_file_upload(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(file_id=None)
    await state.set_state(BookingStates.entering_comment)
    
    progress = get_progress_bar(8)
    await callback.message.edit_text(f"{progress}\n<b>Шаг 8 из 8:</b> Кратко опишите симптомы, жалобы или цель визита:", reply_markup=get_skip_kb("skip_comment"), parse_mode=ParseMode.HTML)

@router.message(BookingStates.entering_comment, F.text)
async def process_comment(message: Message, state: FSMContext):
    await state.update_data(comment=html.escape(message.text.strip()))
    await render_booking_summary(message, state)

@router.callback_query(BookingStates.entering_comment, F.data == "skip_comment")
async def skip_comment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(comment="Не указано")
    await render_booking_summary(callback.message, state)

async def render_booking_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    doctor_info = DB_DOCTORS.get(data['doctor_id'])
    
    summary = (
        "<b>📋 ПРОВЕРКА ДАННЫХ МЕДИЦИНСКОЙ АНКЕТЫ</b>\n\n"
        f"• Пациент: {data['fullname']}\n"
        f"• Дата рождения: {data['birthdate']}\n"
        f"• Телефон: <code>{data['phone']}</code>\n"
        f"• Специализация врача: {doctor_info['spec']}\n"
        f"• Период: {data['date']}\n"
        f"• Анамнез/Симптомы: {data['comment']}\n"
        f"• Наличие КТ-снимка: {'Загружен ✅' if data.get('file_id') else 'Пропущено ➖'}\n\n"
        "<blockquote>Направляя анкету, Вы соглашаетесь на обработку персональных данных. Подача заявки бесплатна.</blockquote>"
    )
    
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить в лист ожидания", callback_data="final_submit_booking")],
        [InlineKeyboardButton(text="❌ Сбросить анкету", callback_data="cancel_booking")]
    ])
    await state.set_state(BookingStates.confirming)
    res = await message.answer(summary, reply_markup=confirm_kb, parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.callback_query(BookingStates.confirming, F.data == "final_submit_booking")
async def final_submit_booking_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.delete()
    doctor = DB_DOCTORS.get(data['doctor_id'])
    
    profile = await db_get_user(callback.from_user.id) or {}
    utm = profile.get("utm_source", "Прямой переход")
    direction_label = "Кардиология" if data['direction'] == "dir_cardio" else "Терапия"

    success_text = (
        "<b>👑 ЗАЯВКА УСПЕШНО ЗАРЕГИСТРИРОВАНА</b>\n\n"
        "<b>Текущий статус:</b> НА МОДЕРАЦИИ В ЛИСТЕ ОЖИДАНИЯ ⏳\n\n"
        f"<b>👨‍⚕️ Назначенный специалист:</b>\n"
        f"• ФИО: {doctor['name']}\n"
        f"• Квалификация: {doctor['spec']} (Стаж {doctor['exp']})\n"
        f"• Резюме: {doctor['portfolio']}\n\n"
        f"<b>📞 Прямые контакты отделения:</b>\n"
        f"• Телефон: {doctor['phone']}\n"
        f"• Telegram: {doctor['tg']}\n\n"
        "Администратор свяжется с Вами сразу после проверки параметров анкеты."
    )
    res = await callback.message.answer(success_text, parse_mode=ParseMode.HTML, reply_markup=get_full_main_menu())
    await track_msg(callback.message.chat.id, res.message_id)

    # Сохраняем заявку и обновляем данные профиля
    await db_add_application(
        user_id=callback.from_user.id,
        fullname=data['fullname'],
        phone=data['phone'],
        direction=direction_label,
        utm_source=utm,
        comment=data['comment'],
        file_id=data.get('file_id')
    )
    await db_update_user_profile(callback.from_user.id, fullname=data['fullname'], phone=data['phone'])

    targets = list(ACTIVE_ADMINS)
    if ADMIN_CHAT_ID != 0:
        targets.append(ADMIN_CHAT_ID)
        
    for chat_id in set(targets):
        try:
            admin_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔎 Проверить в листе ожидания", callback_data="adm_view_pending")]
            ])
            await bot.send_message(
                chat_id=chat_id,
                text=f"🚨 <b>Поступила новая анкета!</b>\n• Пациент: {data['fullname']}\n• Телефон: {data['phone']}\n• Направление: {direction_label}\n• Трафик: {utm}",
                reply_markup=admin_markup,
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
            
    await state.clear()

# ═══════════════════════════════════════════════════
#             АДМИНИСТРАТИВНЫЙ ИНТЕРФЕЙС
# ═══════════════════════════════════════════════════

@router.message(Command("auth"))
async def cmd_auth_handler(message: Message, state: FSMContext):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        res = await message.answer("❌ <b>Ошибка:</b> Не указан пароль.", parse_mode=ParseMode.HTML)
        await track_msg(message.chat.id, res.message_id)
        return
    if args[1].strip() == ADMIN_SECRET_PASSWORD:
        ACTIVE_ADMINS.add(message.from_user.id)
        res = await message.answer("🔓 <b>Доступ предоставлен.</b> Сессия администратора запущена.\nКоманда управления: /admin", parse_mode=ParseMode.HTML)
    else:
        res = await message.answer("❌ Неверный секретный пароль.")
    await track_msg(message.chat.id, res.message_id)

@router.message(Command("admin"), IsAdminFilter())
async def cmd_admin_panel(message: Message, state: FSMContext):
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Лист ожидания (Модерация)", callback_data="adm_view_pending")],
        [InlineKeyboardButton(text="📢 Рассылка по сегментам", callback_data="adm_start_broadcast")],
        [InlineKeyboardButton(text="📊 Аналитика (UTM)", callback_data="adm_view_analytics")]
    ])
    res = await message.answer("<b>⚡ ПАНЕЛЬ УПРАВЛЕНИЯ КЛИНИКИ</b>\n\nВыберите действие:", reply_markup=admin_kb, parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.callback_query(F.data == "adm_view_analytics", IsAdminFilter())
async def adm_view_analytics(callback: CallbackQuery):
    await callback.answer()
    users = await db_get_all_users()
    sources = {}
    for user in users.values():
        utm = user.get("utm_source", "Прямой переход")
        sources[utm] = sources.get(utm, 0) + 1
    
    report = "<b>📊 МАРКЕТИНГОВЫЙ ОТЧЕТ (UTM):</b>\n\n"
    if not sources:
        report += "Нет собранных данных по источникам."
    for src, count in sources.items():
        report += f"• Источник <code>{src}</code>: {count} пользователей\n"
    await callback.message.answer(report, parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "adm_view_pending", IsAdminFilter())
async def adm_view_pending_applications(callback: CallbackQuery):
    await callback.answer()
    apps = await db_get_all_applications()
    if not apps:
        await callback.message.answer("📥 <b>Лист ожидания пуст.</b> Заявок на модерацию нет.", parse_mode=ParseMode.HTML)
        return
    
    for user_id, app_data in list(apps.items()):
        moderation_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Одобрить и выгрузить в МИС", callback_data=f"adm_approve_{user_id}")],
            [InlineKeyboardButton(text="❌ Отклонить заявку", callback_data=f"adm_reject_{user_id}")]
        ])
        
        info = (
            f"📋 <b>Заявка от пользователя {user_id}:</b>\n"
            f"• ФИО: {app_data['fullname']}\n"
            f"• Телефон: <code>{app_data['phone']}</code>\n"
            f"• Направление: {app_data['direction']}\n"
            f"• Источник: <code>{app_data['utm_source']}</code>\n"
            f"• Статус: {app_data['status']}\n"
            f"• Комментарий: {app_data['comment']}\n"
        )
        
        if app_data.get("file_id"):
            await callback.message.answer_document(document=app_data["file_id"], caption=info, reply_markup=moderation_kb, parse_mode=ParseMode.HTML)
        else:
            await callback.message.answer(info, reply_markup=moderation_kb, parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("adm_approve_"), IsAdminFilter())
@router.callback_query(F.data.startswith("adm_reject_"), IsAdminFilter())
async def process_moderation(callback: CallbackQuery):
    action = "approve" if "approve" in callback.data else "reject"
    user_id = int(callback.data.split("_")[2])
    
    if action == "approve":
        await db_update_application_status(user_id, "Одобрено и выгружено в МИС ✅")
        try:
            await bot.send_message(user_id, "🎉 <b>Ваша анкета успешно верифицирована!</b> Данные внесены в медицинскую систему клиники. Врач готов к приему.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await callback.message.edit_text(callback.message.text + "\n\n🟢 <b>Вердикт: Одобрено и отправлено в МИС клиники</b>")
    else:
        await db_update_application_status(user_id, "Отклонено модератором 🔴")
        try:
            await bot.send_message(user_id, "❌ Ваша медицинская заявка отклонена модератором после проверки данных.")
        except Exception:
            pass
        await callback.message.edit_text(callback.message.text + "\n\n🔴 <b>Вердикт: Анкета отклонена администрацией</b>")

@router.callback_query(F.data == "adm_start_broadcast", IsAdminFilter())
async def adm_start_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdminStates.entering_broadcast_text)
    await callback.message.answer("📝 Введите текст рекламного или информационного сообщения для рассылки:")

@router.message(AdminStates.entering_broadcast_text, F.text)
async def adm_save_broadcast_text(message: Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    await state.set_state(AdminStates.choosing_broadcast_segment)
    
    segments_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Всем пациентам", callback_data="seg_all")],
        [InlineKeyboardButton(text="❤️ Только Кардиология", callback_data="seg_cardio")],
        [InlineKeyboardButton(text="🩺 Только Терапия", callback_data="seg_therapy")]
    ])
    res = await message.answer("🎯 Выберите целевой сегмент аудитории для отправки пуша:", reply_markup=segments_kb)
    await track_msg(message.chat.id, res.message_id)

@router.callback_query(AdminStates.choosing_broadcast_segment, F.data.startswith("seg_"))
async def adm_execute_broadcast(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    segment = callback.data.replace("seg_", "")
    state_data = await state.get_data()
    text = state_data["broadcast_text"]
    await state.clear()
    
    users = await db_get_all_users()
    count = 0
    for uid, profile in users.items():
        if segment == "all" or profile.get("last_direction") == f"dir_{segment}":
            try:
                await bot.send_message(chat_id=uid, text=f"🔔 <b>Сообщение от клиники ПРАЙМ:</b>\n\n{text}", parse_mode=ParseMode.HTML)
                count += 1
            except Exception:
                pass
                
    await callback.message.answer(f"📢 Рассылка завершена успешно. Сообщение доставлено {count} пациентам.")

# ═══════════════════════════════════════════════════
#     НОВАЯ СИСТЕМА ДВУСТОРОННЕЙ ПОДДЕРЖКИ (ТИКЕТЫ)
# ═══════════════════════════════════════════════════

@router.callback_query(F.data.startswith("ans_user_"), IsAdminFilter())
async def ask_admin_reply(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    target_user_id = int(callback.data.split("_")[2])
    
    await state.update_data(reply_target_id=target_user_id)
    await state.set_state(AdminStates.typing_reply)
    
    await callback.message.answer(
        f"📝 <b>Режим ответа. Введите сообщение для пациента (ID: {target_user_id}):</b>\n\n"
        f"<i>Пациент получит ваше сообщение мгновенно. Для отмены введите /admin.</i>", 
        parse_mode=ParseMode.HTML
    )

@router.message(AdminStates.typing_reply, F.text)
async def send_admin_reply(message: Message, state: FSMContext):
    if message.text.startswith("/"):
        await state.clear()
        return 

    data = await state.get_data()
    target_user_id = data.get("reply_target_id")
    await state.clear()

    reply_text = html.escape(message.text.strip())

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=f"✉️ <b>Ответ от службы поддержки клиники ПРАЙМ:</b>\n\n{reply_text}",
            parse_mode=ParseMode.HTML
        )
        await message.answer("✅ Ответ успешно доставлен пациенту.")
    except Exception as e:
        await message.answer(f"❌ <b>Ошибка доставки.</b> Подробнее: {e}", parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════
#          ШТАТНЫЕ ИНФОРМАЦИОННЫЕ ХЭНДЛЕРЫ
# ═══════════════════════════════════════════════════

@router.message(F.text == "ℹ️ Служба поддержки (FAQ)")
async def faq_handler(message: Message, state: FSMContext):
    instruction = (
        "<b>📋 Единая база вопросов и ответов</b>\n\n"
        "Вы можете написать свой вопрос обычным текстом прямо в этот чат, и оператор ответит вам.\n\n"
        "<b>Часто запрашиваемые темы:</b>\n"
        "• <i>«Где вы находитесь?»</i>\n"
        "• <i>«Как подготовиться к анализам?»</i>\n"
        "• <i>«Нужен налоговый вычет»</i>"
    )
    res = await message.answer(instruction, parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.message(F.text == "💰 Цены")
async def prices_handler(message: Message, state: FSMContext):
    res = await message.answer("<b>💰 Цены:</b>\n• Первичный осмотр: 0 руб (по квоте)\n• Анализ снимков КТ: 0 руб.", parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.message(F.text == "📍 Адреса")
async def address_handler(message: Message, state: FSMContext):
    res = await message.answer("🏥 г. Москва, ул. Центральная, д. 45. Режим работы: 24/7.", parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.message(F.text == "📞 Связаться с оператором")
async def operator_handler(message: Message, state: FSMContext):
    res = await message.answer("📞 <b>Запрос отправлен оператору.</b> Напишите ваш вопрос следующим сообщением, и свободный сотрудник подключится к чату.", parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)
    
    targets = list(ACTIVE_ADMINS)
    if ADMIN_CHAT_ID != 0:
        targets.append(ADMIN_CHAT_ID)
        
    reply_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Подключиться к чату", callback_data=f"ans_user_{message.from_user.id}")]
    ])
    
    for chat_id in set(targets):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📞 <b>ВЫЗОВ ОПЕРАТОРА</b>\n\n• Пациент: {message.from_user.full_name} (ID: {message.from_user.id})\n• Статус: Ожидает сотрудника.",
                reply_markup=reply_kb,
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

@router.message(F.text == "💝 Пожертвовать клинике")
async def donation_menu(message: Message, state: FSMContext):
    donation_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Поддержать 10 ⭐️", callback_data="donate_10"),
         InlineKeyboardButton(text="❤️ Поддержать 50 ⭐️", callback_data="donate_50")]
    ])
    res = await message.answer("💝 Вы можете внести благотворительный взнос на развитие IT-инфраструктуры в Telegram Stars:", reply_markup=donation_kb)
    await track_msg(message.chat.id, res.message_id)

# ═══════════════════════════════════════════════════
#  ГЛОБАЛЬНЫЙ ПЕРЕХВАТЧИК СЛУЧАЙНОГО ТЕКСТА (ВОПРОСОВ)
# ═══════════════════════════════════════════════════

@router.message(F.text)
async def handle_user_question(message: Message, state: FSMContext):
    menu_buttons = [
        "🚀 Открыть Mini App", "🩺 Оставить заявку на прием", "👤 Личный кабинет", "⭐ Отзывы клиники",
        "ℹ️ Служба поддержки (FAQ)", "📞 Связаться с оператором", "💰 Цены",
        "📍 Адреса", "💝 Пожертвовать клинике"
    ]
    if message.text in menu_buttons or message.text.startswith("/"):
        return 

    user_id = message.from_user.id
    question = html.escape(message.text.strip())

    res = await message.answer("✨ <b>Ваш вопрос отправлен дежурному оператору клиники!</b>\nОтвет придет в этот чат в ближайшее время.", parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

    targets = list(ACTIVE_ADMINS)
    if ADMIN_CHAT_ID != 0:
        targets.append(ADMIN_CHAT_ID)

    reply_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ответить пациенту", callback_data=f"ans_user_{user_id}")]
    ])

    for chat_id in set(targets):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❓ <b>НОВЫЙ ЗАПРОС В ПОДДЕРЖКУ</b>\n\n• <b>От:</b> {message.from_user.full_name} (ID: {user_id})\n• <b>Текст:</b> {question}",
                reply_markup=reply_kb,
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

# ═══════════════════════════════════════════════════
#                ТОЧКА ВХОДА В ПРОГРАММУ
# ═══════════════════════════════════════════════════

async def render_health_check(request):
    return web.Response(text="ONLINE")

async def main():
    await init_db()
    dp.message.outer_middleware(AutoMessageTrackerMiddleware())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    
    app = web.Application()
    app.router.add_get("/", render_health_check)
    app.router.add_get("/health", render_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
    except Exception as e:
        logging.warning(f"Не удалось запустить веб-сервер на порту {port}: {e}")

    print("🚀 Бот запущен с асинхронной БД и обновленным Личным Кабинетом!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())