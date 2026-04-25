"""
Shareify Review Service
- Add rating & review (authenticated)
- Get reviews for an item
"""

import os
import uuid
import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
import jwt
import httpx

app = FastAPI(title="Shareify Review Service", version="1.0.0")

# ── Config ──────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET", "shareify-secret-key-2024")
ALGORITHM = "HS256"
DATABASE = os.getenv("DATABASE_PATH", "./data/reviews.db")
BOOKING_SERVICE_URL = os.getenv("BOOKING_SERVICE_URL", "http://localhost:8004")

security = HTTPBearer()


# ── Database ────────────────────────────────────────────────────────────────
def get_db():
    os.makedirs(os.path.dirname(DATABASE) or ".", exist_ok=True)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


# ── Schemas ─────────────────────────────────────────────────────────────────
class ReviewCreate(BaseModel):
    item_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: str = ""


# ── Auth ────────────────────────────────────────────────────────────────────
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(
            credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Endpoints ───────────────────────────────────────────────────────────────
@app.post("/reviews")
def add_review(review: ReviewCreate, payload: dict = Depends(verify_token)):
    review_id = str(uuid.uuid4())
    user_id = payload["user_id"]

    # ── Step 1: Verify that the user has a COMPLETED booking for this item ──
    try:
        resp = httpx.get(
            f"{BOOKING_SERVICE_URL}/bookings/verify-completion",
            params={"user_id": user_id, "item_id": review.item_id},
            timeout=5.0
        )
        resp.raise_for_status()
        if not resp.json().get("completed"):
            raise HTTPException(
                status_code=403, 
                detail="You can only review an item after your booking is completed/used."
            )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Booking service unavailable: {e}")

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO reviews (review_id, user_id, item_id, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (review_id, user_id, review.item_id, review.rating, review.comment,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {
            "message": "Review added successfully",
            "review_id": review_id,
        }
    finally:
        conn.close()


@app.get("/reviews")
def get_reviews(item_id: str = Query(...)):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM reviews WHERE item_id = ? ORDER BY created_at DESC",
            (item_id,),
        ).fetchall()
        reviews = [dict(r) for r in rows]

        # Calculate average rating
        avg_rating = None
        if reviews:
            avg_rating = round(sum(r["rating"] for r in reviews) / len(reviews), 2)

        return {
            "item_id": item_id,
            "total_reviews": len(reviews),
            "average_rating": avg_rating,
            "reviews": reviews,
        }
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"status": "healthy", "service": "shareify-review-service"}
