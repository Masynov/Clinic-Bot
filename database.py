import aiosqlite

DB_PATH = "clinic.db"

async def init_db():
    """Инициализация таблиц при запуске (данные не перезаписываются)"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица анкет
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id BIGINT PRIMARY KEY,
                full_name TEXT,
                phone TEXT,
                info TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Таблица отзывов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id BIGINT,
                rating INTEGER,
                review_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

# --- РАБОТА С АНКЕТАМИ (UPSERT: создание или обновление) ---
async def upsert_profile(user_id: int, full_name: str, phone: str, info: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO profiles (user_id, full_name, phone, info, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name,
                phone = excluded.phone,
                info = excluded.info,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, full_name, phone, info))
        await db.commit()

async def get_profile(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

# --- РАБОТА С ОТЗЫВАМИ ---
async def add_review(user_id: int, rating: int, review_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO reviews (user_id, rating, review_text)
            VALUES (?, ?, ?)
        """, (user_id, rating, review_text))
        await db.commit()

async def get_reviews(limit: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]