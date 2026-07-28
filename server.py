from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import database as db

app = FastAPI(title="Clinic Mini App API")

# Разрешаем запросы от Mini App
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ProfileSchema(BaseModel):
    user_id: int
    full_name: str
    phone: str
    info: str

class ReviewSchema(BaseModel):
    user_id: int
    rating: int
    review_text: str

@app.on_event("startup")
async def startup():
    await db.init_db()

# --- СИНХРОНИЗАЦИЯ АНКЕТ ДЛЯ MINI APP ---
@app.get("/api/profile/{user_id}")
async def get_profile_endpoint(user_id: int):
    """Mini App запрашивает анкету по telegram user_id"""
    profile = await db.get_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Анкета не найдена")
    return profile

@app.post("/api/profile")
async def save_profile_endpoint(profile: ProfileSchema):
    """Mini App отправляет обновленную анкету"""
    await db.upsert_profile(
        user_id=profile.user_id,
        full_name=profile.full_name,
        phone=profile.phone,
        info=profile.info
    )
    return {"status": "ok", "message": "Анкета синхронизирована"}

# --- ОТЗЫВЫ КЛИНИКИ ДЛЯ MINI APP ---
@app.get("/api/reviews")
async def get_reviews_endpoint():
    """Mini App получает список всех отзывов"""
    return await db.get_reviews()

@app.post("/api/reviews")
async def add_review_endpoint(review: ReviewSchema):
    """Mini App отправляет отзыв"""
    await db.add_review(
        user_id=review.user_id,
        rating=review.rating,
        review_text=review.review_text
    )
    return {"status": "ok", "message": "Отзыв сохранен"}    
# --- ОТЗЫВЫ КЛИНИКИ ДЛЯ MINI APP ---
@app.get("/api/reviews")
async def get_reviews_endpoint():
    """Mini App получает список всех отзывов"""
    return await db.get_reviews()

@app.post("/api/reviews")
async def add_review_endpoint(review: ReviewSchema):
    """Mini App отправляет новый отзыв"""
    await db.add_review(
        user_id=review.user_id,
        rating=review.rating,
        review_text=review.review_text
    )
    return {"status": "ok", "message": "Отзыв сохранен"}