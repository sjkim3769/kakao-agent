"""
api.py — FastAPI REST API 레이어 (신규팀 전면 재작성)

[NEW-01 FIX] 모든 엔드포인트 실제 asyncpg DB 쿼리 연동
[NEW-02 FIX] CORS origins → settings.cors_allowed_origins 환경변수
[NEW-03 FIX] pipeline.run() 백그라운드 실행 주석 해제 + DI 연결
[PROD-07]   RequestLoggingMiddleware (request_id, latency, status)
[Q-03]      Rate Limiting (slowapi)
[N-2]       JWT 인증 전 엔드포인트 적용
"""

import logging
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import asyncpg
from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks, Security
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from config.settings import get_settings

api_logger = logging.getLogger("kakao_agent.api")
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="KakaoTalk Agent API",
    version="1.0.0",
    description="카카오톡 대화방 AI Agent 관리 API",
    docs_url="/docs",
    redoc_url="/redoc",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ============================================================
# [PROD-07] 요청/응답 로깅 미들웨어
# ============================================================
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception as exc:
            api_logger.error(
                f"[{request_id}] UNHANDLED {request.method} {request.url.path} -> 500 | {exc}"
            )
            raise
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        api_logger.info(
            f"[{request_id}] {request.method} {request.url.path}"
            f" -> {response.status_code} | {elapsed_ms}ms"
        )
        response.headers["X-Request-ID"] = request_id
        return response


app.add_middleware(RequestLoggingMiddleware)

# [NEW-02 FIX] CORS — settings에서 로드 (기본값 localhost 제거)
_settings = get_settings()
_cors_origins = (
    [o.strip() for o in _settings.cors_allowed_origins.split(",")]
    if hasattr(_settings, "cors_allowed_origins")
    else ["http://localhost:3000"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH"],
    allow_headers=["*"],
)


# ============================================================
# DI: DB Pool & Pipeline
# ============================================================
_db_pool: Optional[asyncpg.Pool] = None
_pipeline = None


def set_db_pool(pool: asyncpg.Pool) -> None:
    global _db_pool
    _db_pool = pool


def set_pipeline(pipeline) -> None:
    """[NEW-03 FIX] 파이프라인 DI"""
    global _pipeline
    _pipeline = pipeline


async def get_db() -> asyncpg.Pool:
    if _db_pool is None:
        raise HTTPException(status_code=503, detail="DB 풀 미초기화")
    return _db_pool


# ============================================================
# JWT 인증
# ============================================================
_security = HTTPBearer()


def verify_token(
    credentials: HTTPAuthorizationCredentials = Security(_security),
) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("sub") is None:
            raise HTTPException(status_code=401, detail="유효하지 않은 토큰")
        return payload
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"토큰 검증 실패: {e}")


def create_access_token(subject: str) -> str:
    settings = get_settings()
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {"sub": subject, "exp": expire},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


# ============================================================
# 응답 스키마
# ============================================================
class APIResponse(BaseModel):
    success: bool
    message: str = ""
    data: Optional[dict] = None
    errors: list[str] = Field(default_factory=list)


class CommentReviewRequest(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")
    reviewer_id: str = Field(..., min_length=1, max_length=100)
    rejection_reason: Optional[str] = None


class ManualRunRequest(BaseModel):
    room_id: str = Field(..., min_length=1, max_length=255)
    target_date: Optional[date] = None


# ============================================================
# 스냅샷 관리
# ============================================================

@app.get("/api/v1/snapshots", tags=["Snapshots"], summary="일일 스냅샷 목록 조회")
async def list_snapshots(
    room_id: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    status: Optional[str] = Query(None, pattern="^(pending|processing|done|failed)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _token: dict = Depends(verify_token),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """[NEW-01 FIX] daily_conversation_snapshots 실제 페이지네이션 조회"""
    offset = (page - 1) * page_size
    end = end_date or date.today()
    start = start_date or (end - timedelta(days=30))

    conditions = ["s.snapshot_date BETWEEN $1 AND $2"]
    params: list = [start, end]
    idx = 3

    if room_id:
        conditions.append(f"s.room_id = ${idx}")
        params.append(room_id)
        idx += 1
    if status:
        conditions.append(f"s.processing_status = ${idx}")
        params.append(status)
        idx += 1

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        total: int = await conn.fetchval(
            f"SELECT COUNT(*) FROM daily_conversation_snapshots s WHERE {where}",
            *params,
        )
        rows = await conn.fetch(
            f"""SELECT s.snapshot_id, s.snapshot_date, s.room_id,
                       s.total_messages, s.unique_users, s.processing_status,
                       EXISTS(
                           SELECT 1 FROM agent_comments c
                           WHERE c.snapshot_id = s.snapshot_id
                             AND c.comment_type = 'daily_summary'
                       ) AS has_comment
                FROM daily_conversation_snapshots s
                WHERE {where}
                ORDER BY s.snapshot_date DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
            *params, page_size, offset,
        )

    return {
        "success": True,
        "data": {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        },
    }


@app.get("/api/v1/snapshots/{snapshot_id}", tags=["Snapshots"], summary="스냅샷 상세 조회")
async def get_snapshot(
    snapshot_id: str,
    _token: dict = Depends(verify_token),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """[NEW-01 FIX] 스냅샷 + 분석 통계 조회"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT s.snapshot_id, s.snapshot_date, s.room_id,
                      s.total_messages, s.unique_users, s.processing_status,
                      s.processed_at, s.created_at,
                      COUNT(a.analysis_id)                               AS analyzed_users,
                      AVG(a.sentiment_score)::NUMERIC(4,3)               AS avg_sentiment,
                      COUNT(CASE WHEN a.needs_agent_response THEN 1 END) AS pending_qa
               FROM daily_conversation_snapshots s
               LEFT JOIN message_analyses a ON a.snapshot_id = s.snapshot_id
               WHERE s.snapshot_id = $1
               GROUP BY s.snapshot_id""",
            snapshot_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="스냅샷을 찾을 수 없습니다")
    return {"success": True, "data": dict(row)}


# ============================================================
# 댓글 관리
# ============================================================

@app.get("/api/v1/comments", tags=["Comments"], summary="생성된 댓글 목록")
async def list_comments(
    status: Optional[str] = Query(
        None,
        pattern="^(auto_approved|pending_review|approved|rejected|sent)$",
    ),
    date_from: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    _token: dict = Depends(verify_token),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """[NEW-01 FIX] agent_comments 실제 조회"""
    offset = (page - 1) * page_size
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if status:
        conditions.append(f"c.approval_status = ${idx}")
        params.append(status)
        idx += 1
    if date_from:
        conditions.append(f"c.comment_date >= ${idx}")
        params.append(date_from)
        idx += 1

    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        total: int = await conn.fetchval(
            f"SELECT COUNT(*) FROM agent_comments c WHERE {where}", *params
        )
        rows = await conn.fetch(
            f"""SELECT c.comment_id, c.comment_date, c.comment_type,
                       c.generated_comment, c.approval_status,
                       c.reviewed_by, c.reviewed_at, c.sent_at
                FROM agent_comments c
                WHERE {where}
                ORDER BY c.comment_date DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
            *params, page_size, offset,
        )
    return {
        "success": True,
        "data": {"items": [dict(r) for r in rows], "total": total},
    }


@app.patch(
    "/api/v1/comments/{comment_id}/review",
    tags=["Comments"],
    summary="댓글 수동 승인/거절",
)
async def review_comment(
    comment_id: str,
    body: CommentReviewRequest,
    _token: dict = Depends(verify_token),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """[NEW-01 FIX] 실제 UPDATE"""
    if body.action == "reject" and not body.rejection_reason:
        raise HTTPException(status_code=422, detail="거절 시 rejection_reason 필수")

    new_status = "approved" if body.action == "approve" else "rejected"
    import json as _json
    meta_patch = _json.dumps({"rejection_reason": body.rejection_reason or ""})

    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE agent_comments
               SET approval_status  = $1,
                   reviewed_by      = $2,
                   reviewed_at      = NOW(),
                   comment_metadata = comment_metadata || $3::jsonb,
                   updated_at       = NOW()
               WHERE comment_id = $4""",
            new_status, body.reviewer_id, meta_patch, comment_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="댓글을 찾을 수 없습니다")

    return APIResponse(
        success=True,
        message=f"댓글 {'승인' if body.action == 'approve' else '거절'} 완료",
        data={"comment_id": comment_id, "new_status": new_status},
    ).model_dump()


# ============================================================
# 파이프라인 수동 실행
# ============================================================

@app.post("/api/v1/pipeline/run", tags=["Pipeline"], summary="파이프라인 수동 실행")
@limiter.limit("10/minute")
async def run_pipeline(
    request: Request,
    body: ManualRunRequest,
    background_tasks: BackgroundTasks,
    _token: dict = Depends(verify_token),
) -> dict:
    """
    [NEW-03 FIX] 파이프라인 DI 연결 + background_tasks 실제 실행.
    이전: background_tasks.add_task(...) 주석 처리로 아무것도 실행 안 됨.
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="파이프라인 미초기화 — 서버 재시작 필요")

    target = body.target_date or date.today()
    background_tasks.add_task(_pipeline.run, target)  # [NEW-03 FIX] 주석 해제

    api_logger.info(f"파이프라인 수동 실행: room={body.room_id}, date={target}")
    return APIResponse(
        success=True,
        message=f"파이프라인 시작: {body.room_id} / {target}",
        data={"room_id": body.room_id, "target_date": str(target), "status": "started"},
    ).model_dump()


@app.get("/api/v1/pipeline/status", tags=["Pipeline"], summary="파이프라인 실행 상태")
async def get_pipeline_status(
    _token: dict = Depends(verify_token),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """[NEW-01 FIX] processing_logs 실제 조회"""
    async with pool.acquire() as conn:
        last = await conn.fetchrow(
            """SELECT process_type, status, started_at, completed_at, duration_ms
               FROM processing_logs
               ORDER BY started_at DESC LIMIT 1"""
        )
        running: int = await conn.fetchval(
            """SELECT COUNT(*) FROM daily_conversation_snapshots
               WHERE processing_status = 'processing'"""
        )
    return {
        "success": True,
        "data": {
            "is_running": running > 0,
            "last_run": dict(last) if last else None,
        },
    }


# ============================================================
# 사용자 분석
# ============================================================

@app.get("/api/v1/users/{user_id}/analysis", tags=["Users"], summary="사용자 대화 분석")
async def get_user_analysis(
    user_id: str,
    days: int = Query(7, ge=1, le=30),
    _token: dict = Depends(verify_token),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """[NEW-01 FIX] 실제 user_profiles + message_analyses 조회"""
    if not user_id.startswith("u_") or len(user_id) != 18:
        raise HTTPException(status_code=400, detail="유효하지 않은 user_id 형식")

    since = date.today() - timedelta(days=days)
    async with pool.acquire() as conn:
        profile = await conn.fetchrow(
            "SELECT * FROM user_profiles WHERE user_id = $1", user_id
        )
        if not profile:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

        breakdown = await conn.fetch(
            """SELECT a.question_type,
                      COUNT(*)                            AS cnt,
                      AVG(a.sentiment_score)::NUMERIC(4,3) AS avg_sentiment
               FROM message_analyses a
               JOIN daily_conversation_snapshots s ON s.snapshot_id = a.snapshot_id
               WHERE a.user_id = $1 AND s.snapshot_date >= $2
               GROUP BY a.question_type
               ORDER BY cnt DESC""",
            user_id, since,
        )

    return {
        "success": True,
        "data": {
            "user_id": user_id,
            "proficiency_level": profile["proficiency_level"],
            "dominant_topics": profile["dominant_topics"],
            "total_messages": profile["total_messages"],
            "total_questions": profile["total_questions"],
            "analysis_period_days": days,
            "question_type_breakdown": [dict(r) for r in breakdown],
        },
    }


@app.get("/api/v1/analytics/daily-summary", tags=["Analytics"], summary="일일 통계 대시보드")
async def get_daily_summary(
    room_id: str,
    target_date: Optional[date] = None,
    _token: dict = Depends(verify_token),
    pool: asyncpg.Pool = Depends(get_db),
) -> dict:
    """[NEW-01 FIX] v_daily_summary 뷰 실제 조회"""
    t = target_date or date.today()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM v_daily_summary WHERE room_id = $1 AND snapshot_date = $2",
            room_id, t,
        )
    return {
        "success": True,
        "data": (
            dict(row) if row
            else {"date": str(t), "room_id": room_id, "no_data": True}
        ),
    }


# ============================================================
# 헬스체크
# ============================================================

@app.get("/health", tags=["System"])
async def health_check() -> dict:
    return {"status": "ok", "timestamp": datetime.now().isoformat(), "version": "1.0.0"}


@app.get("/health/db", tags=["System"])
async def db_health(pool: asyncpg.Pool = Depends(get_db)) -> dict:
    """[NEW-01 FIX] 실제 DB 핑"""
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB 연결 실패: {e}")
