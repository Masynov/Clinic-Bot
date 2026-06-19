import os
import re
import html
import logging
import asyncio
import sqlite3
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.filters import Command, Filter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, PreCheckoutQuery, LabeledPrice, TelegramObject,
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
)

# ═══════════════════════════════════════════════════
#          КОНФИГУРАЦИЯ И СИСТЕМНЫЕ НАСТРОЙКИ
# ═══════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = 5537838624  
ADMIN_SECRET_PASSWORD = os.getenv("ADMIN_SECRET_PASSWORD", "prime_secret_2026")
DB_FILE = "clinic_bot.db"

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

# ═══════════════════════════════════════════════════
#             РАБОТА С ЛОКАЛЬНОЙ БАЗОЙ ДАННЫХ
# ═══════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            fullname TEXT,
            utm_source TEXT,
            last_direction TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            chat_id INTEGER,
            message_id INTEGER,
            PRIMARY KEY (chat_id, message_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            user_id INTEGER PRIMARY KEY,
            fullname TEXT,
            phone TEXT,
            direction TEXT,
            utm_source TEXT,
            comment TEXT,
            file_id TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reviews_stats (
            id INTEGER PRIMARY KEY,
            total_stars INTEGER,
            count INTEGER
        )
    """)
    cursor.execute("SELECT COUNT(*) FROM reviews_stats")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO reviews_stats (id, total_stars, count) VALUES (1, 493, 100)")
        
    conn.commit()
    conn.close()

def db_track_msg(chat_id: int, message_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO chat_history (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id))
    conn.commit()
    conn.close()

def db_get_chat_history(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT message_id FROM chat_history WHERE chat_id = ?", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

def db_clear_chat_history(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def db_register_user(user_id: int, fullname: str, utm_source: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, fullname, utm_source, last_direction) VALUES (?, ?, ?, NULL)", (user_id, fullname, utm_source))
    conn.commit()
    conn.close()

def db_update_user_direction(user_id: int, direction: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_direction = ? WHERE user_id = ?", (direction, user_id))
    conn.commit()
    conn.close()

def db_get_user(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT fullname, utm_source, last_direction FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"fullname": row[0], "utm_source": row[1], "last_direction": row[2]}
    return None

def db_get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, fullname, utm_source, last_direction FROM users")
    rows = cursor.fetchall()
    conn.close()
    return {r[0]: {"fullname": r[1], "utm_source": r[2], "last_direction": r[3]} for r in rows}

def db_add_application(user_id: int, fullname: str, phone: str, direction: str, utm_source: str, comment: str, file_id: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO applications (user_id, fullname, phone, direction, utm_source, comment, file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, fullname, phone, direction, utm_source, comment, file_id))
    conn.commit()
    conn.close()

def db_get_all_applications():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, fullname, phone, direction, utm_source, comment, file_id FROM applications")
    rows = cursor.fetchall()
    conn.close()
    return {r[0]: {"fullname": r[1], "phone": r[2], "direction": r[3], "utm_source": r[4], "comment": r[5], "file_id": r[6]} for r in rows}

def db_pop_application(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT fullname, phone, direction, utm_source, comment, file_id FROM applications WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM applications WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return {"fullname": row[0], "phone": row[1], "direction": row[2], "utm_source": row[3], "comment": row[4], "file_id": row[5]}
    conn.close()
    return None

def db_get_reviews_stats():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT total_stars, count FROM reviews_stats WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return {"total_stars": row[0], "count": row[1]}

def db_update_reviews_stats(stars: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE reviews_stats SET total_stars = total_stars + ?, count = count + 1 WHERE id = 1", (stars,))
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════════════
#    АВТО-ПЕРЕХВАТ (MIDDLEWARE) И ОЧИСТКА ЧАТА
# ═══════════════════════════════════════════════════

class AutoMessageTrackerMiddleware(BaseMiddleware):
    """Глобальный перехватчик: ловит ВСЕ сообщения от пользователя без исключения"""
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message):
            db_track_msg(event.chat.id, event.message_id)
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
    """Принудительное сохранение ID сообщений (для ответов бота)"""
    db_track_msg(chat_id, msg_id)

async def clear_chat_history(chat_id: int):
    """Удаляет всю историю сообщений, хранящуюся в базе данных"""
    messages_to_delete = db_get_chat_history(chat_id)
    for msg_id in messages_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  
    db_clear_chat_history(chat_id)

# ═══════════════════════════════════════════════════
#                  ИНТЕРФЕЙСНЫЕ КНОПКИ
# ═══════════════════════════════════════════════════

def get_full_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
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
    # Сначала удаляем всё старое барахло из чата на базе БД
    await clear_chat_history(message.chat.id)
    
    # Сбрасываем любые зависшие состояния ввода анкеты
    await state.clear()

    utm_source = command.args if command.args else "Прямой переход"
    db_register_user(message.from_user.id, message.from_user.full_name, utm_source)

    welcome_text = (
        "<b>🩺 МЕДИЦИНСКИЙ ЦЕНТР «ПРАЙМ»</b>\n"
        "<code>[БД-РЕЖИМ: АКТИВЕН]</code>\n\n"
        "<blockquote>Добро пожаловать в единую цифровую систему управления Вашим здоровьем. All-in-one платформа для связи с клиникой.</blockquote>\n"
        "Все доступные функции структурированы в нижнем меню взаимодействия."
    )
    res = await message.answer(welcome_text, reply_markup=get_full_main_menu(), parse_mode=ParseMode.HTML)
    
    # Запоминаем новое приветствие бота, чтобы удалить его при следующем /start
    await track_msg(message.chat.id, res.message_id)

# ═══════════════════════════════════════════════════
#         ДИНАМИЧЕСКАЯ СИСТЕМА ОТЗЫВОВ
# ═══════════════════════════════════════════════════

@router.message(F.text == "⭐ Отзывы клиники")
async def review_handler(message: Message, state: FSMContext):
    stats = db_get_reviews_stats()
    avg_rating = stats["total_stars"] / stats["count"]
    
    stars_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐", callback_data="rev_1"), InlineKeyboardButton(text="⭐⭐", callback_data="rev_2"), InlineKeyboardButton(text="⭐⭐⭐", callback_data="rev_3")],
        [InlineKeyboardButton(text="⭐⭐⭐⭐", callback_data="rev_4"), InlineKeyboardButton(text="⭐⭐⭐⭐⭐", callback_data="rev_5")]
    ])
    
    res = await message.answer(
        f"<b>📊 Динамический рейтинг клиники: {avg_rating:.2f} / 5.00 ⭐</b>\n"
        f"<i>(Всего получено оценок от пациентов: {stats['count']})</i>\n\n"
        f"Пожалуйста, оцените качество обслуживания в нашей сети:", 
        reply_markup=stars_markup, 
        parse_mode=ParseMode.HTML
    )
    await track_msg(message.chat.id, res.message_id)

@router.callback_query(F.data.startswith("rev_"))
async def process_smart_review(callback: CallbackQuery):
    rating = int(callback.data.split("_")[1])
    await callback.answer()
    
    db_update_reviews_stats(rating)
    stats = db_get_reviews_stats()
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
    db_update_user_direction(callback.from_user.id, direction)
        
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
    
    profile = db_get_user(callback.from_user.id) or {}
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

    db_add_application(
        user_id=callback.from_user.id,
        fullname=data['fullname'],
        phone=data['phone'],
        direction=direction_label,
        utm_source=utm,
        comment=data['comment'],
        file_id=data.get('file_id')
    )

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
                text=f"🚨 <b>Поступила новая анкету!</b>\n• Пациент: {data['fullname']}\n• Направление: {direction_label}\n• Трафик: {utm}",
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
    users = db_get_all_users()
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
    apps = db_get_all_applications()
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
            f"• Комментарий: {app_data['comment']}\n"
        )
        
        if app_data.get("file_id"):
            await callback.message.answer_document(document=app_data["file_id"], caption=info, reply_markup=moderation_kb, parse_mode=ParseMode.HTML)
        else:
            await callback.message.answer(info, reply_markup=moderation_kb, parse_mode=ParseMode.HTML)

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
    
    users = db_get_all_users()
    count = 0
    for uid, profile in users.items():
        if segment == "all" or profile.get("last_direction") == f"dir_{segment}":
            try:
                await bot.send_message(chat_id=uid, text=f"🔔 <b>Сообщение от клиники ПРАЙМ:</b>\n\n{text}", parse_mode=ParseMode.HTML)
                count += 1
            except Exception:
                pass
                
    await callback.message.answer(f"📢 Рассылка завершена успешно. Сообщение доставлено {count} пациентам.")

@router.callback_query(F.data.startswith("adm_approve_"), IsAdminFilter())
@router.callback_query(F.data.startswith("adm_reject_"), IsAdminFilter())
async def process_moderation(callback: CallbackQuery):
    action = "approve" if "approve" in callback.data else "reject"
    user_id = int(callback.data.split("_")[2])
    
    app_data = db_pop_application(user_id)
        
    if action == "approve":
        try:
            await bot.send_message(user_id, "🎉 <b>Ваша анкета успешно верифицирована!</b> Данные внесены в медицинскую систему клиники. Врач готов к приему.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await callback.message.edit_text(callback.message.text + "\n\n🟢 <b>Вердикт: Одобрено и отправлено в МИС клиники</b>")
    else:
        try:
            await bot.send_message(user_id, "❌ Ваша медицинская заявка отклонена модератором после проверки данных.")
        except Exception:
            pass
        await callback.message.edit_text(callback.message.text + "\n\n🔴 <b>Вердикт: Анкета отклонена администрацией</b>")

# ═══════════════════════════════════════════════════
#          ШТАТНЫЕ ИНФОРМАЦИОННЫЕ ХЭНДЛЕРЫ
# ═══════════════════════════════════════════════════

@router.message(F.text == "👤 Личный кабинет")
async def user_cabinet(message: Message, state: FSMContext):
    profile = db_get_user(message.from_user.id) or {}
    utm = profile.get("utm_source", "Не определен")
    cabinet_text = (
        "<b>👤 КАРТА ПАЦИЕНТА В СИСТЕМЕ</b>\n\n"
        f"• Имя профиля: {message.from_user.first_name}\n"
        f"• Маркетинговый источник: <code>{utm}</code>\n"
        f"• Статус: Верифицированный клиент клиники\n"
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        "<blockquote>Синхронизация истории посещений с базой МИС активна.</blockquote>"
    )
    res = await message.answer(cabinet_text, parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.message(F.text == "ℹ️ Служба поддержки (FAQ)")
async def faq_handler(message: Message, state: FSMContext):
    res = await message.answer("<b>📋 FAQ — Информация:</b>\n\n• Подача заявок бесплатна.\n• Бот поддерживает загрузку снимков КТ для получения второго мнения врача.", parse_mode=ParseMode.HTML)
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
    res = await message.answer("📞 Переключение на оператора клиники... Пожалуйста, ожидайте.", parse_mode=ParseMode.HTML)
    await track_msg(message.chat.id, res.message_id)

@router.message(F.text == "💝 Пожертвовать клинике")
async def donation_menu(message: Message, state: FSMContext):
    donation_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Поддержать 10 ⭐️", callback_data="donate_10"),
         InlineKeyboardButton(text="❤️ Поддержать 50 ⭐️", callback_data="donate_50")]
    ])
    res = await message.answer("💝 Вы можете внести благотворительный взнос на развитие IT-инфраструктуры в Telegram Stars:", reply_markup=donation_kb)
    await track_msg(message.chat.id, res.message_id)

# ═══════════════════════════════════════════════════
#                ТОЧКА ВХОДА В ПРОГРАММУ
# ═══════════════════════════════════════════════════

async def render_health_check(request):
    return web.Response(text="ONLINE")

async def main():
    init_db()
    
    # Подключаем глобальный Middleware для тотального контроля входящего трафика
    dp.message.outer_middleware(AutoMessageTrackerMiddleware())
    
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    
    app = web.Application()
    app.router.add_get("/", render_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
    except Exception:
        pass

    print("🚀 Бот запущен с Middleware-логированием истории в SQLite БД!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())