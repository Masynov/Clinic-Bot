import sqlite3
from datetime import datetime

DB_NAME = "clinic_bot.db"

def init_db():
    """Создание таблиц при запуске"""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS patients (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                phone TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                service TEXT,
                status TEXT DEFAULT 'Новая',
                created_at TEXT,
                FOREIGN KEY (user_id) REFERENCES patients (user_id)
            )
        ''')
        conn.commit()

def save_patient_request(user_id: int, name: str, phone: str, service: str):
    """Сохранение/обновление пациента и добавление новой заявки"""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        
        # Обновляем профиль пациента
        cursor.execute('''
            INSERT INTO patients (user_id, name, phone)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, phone=excluded.phone
        ''', (user_id, name, phone))
        
        # Добавляем новую заявку
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        cursor.execute('''
            INSERT INTO requests (user_id, service, created_at)
            VALUES (?, ?, ?)
        ''', (user_id, service, now))
        
        conn.commit()

def get_user_requests(user_id: int) -> list:
    """Получение истории заявок пациента"""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, service, status, created_at 
            FROM requests 
            WHERE user_id = ? 
            ORDER BY id DESC
        ''', (user_id,))
        return cursor.fetchall()