"""
Shareify Review Service
- Add rating & review (authenticated)
- Get reviews for an item
"""

import os
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
import jwt
import httpx

app = FastAPI(title="Shareify Review Service", version="1.0.0")
# --
# -- POSTGRESQL HOTFIX: SQLite Polyfill Helper -------------------------------
import psycopg2
from psycopg2.extras import RealDictCursor

def db_execute(conn, query, vars=None):
    if '?' in query:
        query = query.replace('?', '%s')
    cursor = conn.cursor()
    cursor.execute(query, vars)
    return cursor
# ----------------------------------------------------------------------------
import time
from fastapi import Request
from prometheus_client import make_asgi_app, Counter, Histogram

# -- Prometheus Metrics ------------------------------------------------------
REQUEST_COUNT = Counter("http_requests_total", "Total requests", ["method", "endpoint", "http_status"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds", "Latency", ["method", "endpoint"])

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    method = request.method
    endpoint = request.url.path
    if endpoint == "/metrics":
        return await call_next(request)
        
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, http_status=response.status_code).inc()
    REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(process_time)
    
    return response


# ── Config ──────────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET", "shareify-secret-key-2024")
ALGORITHM = "HS256"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:shareify-secure-db-pass@postgres-db:5432/review_service")
BOOKING_SERVICE_URL = os.getenv("BOOKING_SERVICE_URL", "http://localhost:8004")

security = HTTPBearer()


# ── Database ────────────────────────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    db_execute(conn, """
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
        db_execute(conn, 
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
        rows = db_execute(conn, 
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





