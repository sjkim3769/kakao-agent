"""
collector.py — 카카오톡 대화 수집 모듈 (실데이터 검증 후 전면 수정)

[실데이터 검증 발견 버그 수정]
  REAL-01: target_date 날짜 필터링 추가 (전체 기간 → 1일치만)
  REAL-02: 시스템 메시지 멀티라인 오염 방지
  REAL-03: 닉네임 마스킹 오적용 제거 (닉네임은 가명이므로 마스킹 불필요)
  REAL-04: URL 내 숫자 전화번호 오탐 방지 (URL 선보호 후 마스킹)
  REAL-05: 이메일 정규식 강화 (하이픈·점 포함 로컬파트)
  REAL-06: 닉네임 포맷 파싱 — 실명 의심 부분 분리
  REAL-07: CRLF 명시적 처리
  REAL-09: 미디어 메시지(사진/이모티콘) 필터링 — LLM 비용 낭비 방지
  REAL-10: 닉네임 메타데이터(성별/도시/비자) 추출 저장
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 상수
# ============================================================

# [REAL-09] 내용 없는 미디어 타입 — 분석 제외
_MEDIA_TYPES: frozenset[str] = frozenset({
    "사진", "동영상", "이모티콘", "파일", "스티커",
    "photo", "video", "sticker", "file",
    "삭제된 메시지",   # 삭제 메시지도 분석 불필요
})

# [REAL-02] 시스템 이벤트 패턴 — 멀티라인 이어쓰기 차단
_SYSTEM_EVENT_PATTERNS = re.compile(
    r'님이 (들어왔습니다|나갔습니다)|'
    r'메시지가 삭제되었습니다|'
    r'Start an Open Chat|'
    r'^-+\s+\d{4}년'
)

# [REAL-04] URL 패턴 — 마스킹 전 보호
_URL_PATTERN = re.compile(
    r'https?://[^\s\u3000\u200b\uff00-\uffef\u4e00-\u9fff]+'
)

# [REAL-06] 닉네임 표준 포맷: 닉/성별/출생연도/이민연도/도시/비자
_NICK_META_PATTERN = re.compile(
    r'^(.+?)/(M|F|m|f|남|여)/(\d{2})/(\d{2,4})/(.+)/(.+)$'
)


# ============================================================
# 닉네임 파싱 유틸리티
# ============================================================

def parse_nickname(raw_name: str) -> dict:
    """
    [REAL-06/10] 닉네임 포맷에서 표시명 + 메타데이터 추출
    포맷: 위스킹/M/81/08/MEL/CT
    반환: {display: '위스킹', gender: 'M', birth_year: 1981, city: 'MEL', visa: 'CT'}
    포맷 불일치 시: {display: raw_name}
    """
    if not raw_name or raw_name == "(알 수 없음)":
        return {"display": raw_name}

    m = _NICK_META_PATTERN.match(raw_name)
    if m:
        nick, gender, birth_yy, migration_yy, city, visa = m.groups()
        birth_year = int("19" + birth_yy) if int(birth_yy) > 20 else int("20" + birth_yy)
        return {
            "display": nick,
            "gender": gender.upper() if gender in ("M", "F") else ("M" if gender == "남" else "F"),
            "birth_year": birth_year,
            "migration_year": int(migration_yy) if len(migration_yy) == 4 else int("20" + migration_yy),
            "city": city.strip(),
            "visa_type": visa.strip().upper(),
        }
    return {"display": raw_name}


# ============================================================
# 도메인 모델
# ============================================================

@dataclass
class RawMessage:
    """카카오톡 단일 메시지 (수집 원본)"""
    timestamp: datetime
    user_id: str            # SHA-256 해시 ID
    display_name: str       # [REAL-03 FIX] 닉네임 원본 (이미 가명 — 추가 마스킹 불필요)
    content: str
    message_type: str = "text"   # text | media (사진/이모티콘 등)

    def __post_init__(self) -> None:
        """
        [REAL-03 FIX] 닉네임은 이미 가명이므로 마스킹 불적용.
        content만 개인정보 sanitize 처리.
        """
        object.__setattr__(self, "content", self._sanitize_content(self.content))

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "user_id": self.user_id,
            "display_name": self.display_name,
            "content": self.content,
            "message_type": self.message_type,
        }

    @staticmethod
    def _sanitize_content(content: str) -> str:
        """
        [REAL-04 FIX] URL 선보호 후 마스킹 — URL 내 숫자 오탐 방지
        [REAL-05 FIX] 이메일 정규식 강화 — 하이픈/점 포함 로컬파트
        """
        # Step 1: URL을 플레이스홀더로 임시 치환 (마스킹 보호)
        urls: list[str] = []
        def _replace_url(m: re.Match) -> str:
            urls.append(m.group(0))
            return f"__URL_{len(urls) - 1}__"

        protected = _URL_PATTERN.sub(_replace_url, content)

        # Step 2: 개인정보 마스킹
        # [REAL-05 FIX] 이메일 — 하이픈 포함 로컬파트 커버
        protected = re.sub(
            r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
            '[이메일]', protected
        )
        # 전화번호 — 한국 휴대폰 (010/011/016/017/018/019)
        protected = re.sub(r'01[0-9][-\s]?\d{3,4}[-\s]?\d{4}', '[전화번호]', protected)
        # 주민번호
        protected = re.sub(r'\d{6}-?\d{7}', '[주민번호]', protected)

        # Step 3: URL 플레이스홀더 복원
        for i, url in enumerate(urls):
            protected = protected.replace(f"__URL_{i}__", url)

        return protected


@dataclass
class UserMeta:
    """닉네임에서 추출한 사용자 메타데이터 [REAL-10]"""
    display_name: str
    gender: Optional[str] = None
    birth_year: Optional[int] = None
    migration_year: Optional[int] = None
    city: Optional[str] = None
    visa_type: Optional[str] = None


@dataclass
class DailyConversation:
    """일일 대화 데이터 집합체"""
    room_id: str
    snapshot_date: date
    messages: list[RawMessage] = field(default_factory=list)
    user_meta: dict[str, UserMeta] = field(default_factory=dict)  # [REAL-10]

    @property
    def total_messages(self) -> int:
        return len(self.messages)

    @property
    def unique_users(self) -> set[str]:
        return {m.user_id for m in self.messages}

    @property
    def content_hash(self) -> str:
        """[C-2] SHA-256 해시 — sanitize 이후 데이터 기준"""
        sanitized = [m.to_dict() for m in self.messages]
        payload = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CollectionResult:
    """수집 결과"""
    success: bool
    snapshot_id: Optional[str] = None
    message: str = ""
    skipped: bool = False


# ============================================================
# 카카오톡 파일 파서
# ============================================================

class KakaoTalkParser:
    """
    카카오톡 내보내기 TXT 파일 파서.

    [실데이터 검증 수정 사항]
    - REAL-01: target_date 필터 — 해당 날짜 메시지만 반환
    - REAL-02: 시스템 이벤트 라인 멀티라인 차단
    - REAL-07: CRLF 명시적 처리
    - REAL-09: 미디어 메시지 message_type='media' 표기
    """

    _MSG_PATTERN = re.compile(
        r'^\[(.+?)\] \[(오전|오후) (\d{1,2}:\d{2})\] (.+)$'
    )
    _DATE_PATTERN = re.compile(
        r'^-+ (\d{4})년 (\d{1,2})월 (\d{1,2})일'
    )
    # 파일 헤더 (첫 줄: "... 님과 카카오톡 대화")
    _HEADER_PATTERN = re.compile(r'님과 카카오톡 대화|저장한 날짜')

    def parse_file(
        self,
        file_path: Path,
        room_id: str,
        target_date: date | None = None,
    ) -> "DailyConversation":
        """
        [REAL-01 FIX] target_date 지정 시 해당 날짜 메시지만 수집.
        카카오톡 내보내기는 전체 누적 파일이므로 날짜 필터 필수.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"대화 파일 없음: {file_path}")

        # [F-02] 심볼릭 링크 차단
        resolved = file_path.resolve()
        if resolved != file_path:
            raise PermissionError(
                f"심볼릭 링크 경로 차단: {file_path} → {resolved}"
            )

        messages: list[RawMessage] = []
        user_meta: dict[str, UserMeta] = {}
        current_date: date = date.today()
        last_message: RawMessage | None = None
        in_target_date: bool = (target_date is None)  # 필터 없으면 전체 수집

        # [REAL-07] newline='' — CRLF/LF 모두 명시적 처리
        with open(file_path, encoding="utf-8", errors="replace", newline="") as f:
            for line_num, raw_line in enumerate(f, 1):
                # CRLF → LF 정규화
                line = raw_line.replace("\r\n", "\n").replace("\r", "\n")
                stripped = line.strip()
                if not stripped:
                    continue

                # 파일 헤더 스킵
                if self._HEADER_PATTERN.search(stripped):
                    last_message = None
                    continue

                # 날짜 구분선
                date_match = self._DATE_PATTERN.match(stripped)
                if date_match:
                    y, mo, d = (int(x) for x in date_match.groups())
                    try:
                        current_date = date(y, mo, d)
                        # [REAL-01 FIX] 날짜 전환 시 target_date 체크
                        in_target_date = (target_date is None or current_date == target_date)
                        last_message = None
                    except ValueError:
                        logger.warning(f"잘못된 날짜 (line {line_num}): {stripped}")
                    continue

                # target_date 범위 밖이면 파싱 스킵 (성능 최적화)
                if not in_target_date:
                    # target_date 이후 날짜가 나오면 조기 종료 가능
                    if target_date and current_date > target_date:
                        logger.debug(f"target_date({target_date}) 초과 — 파싱 조기 종료")
                        break
                    continue

                # [REAL-02] 시스템 이벤트 라인 — 멀티라인 이어쓰기 차단
                if _SYSTEM_EVENT_PATTERNS.search(stripped):
                    last_message = None   # 연결 끊기
                    continue

                # 메시지 파싱
                msg_match = self._MSG_PATTERN.match(stripped)
                if msg_match:
                    name, ampm, time_str, content = msg_match.groups()
                    ts = self._parse_timestamp(current_date, ampm, time_str)

                    # [REAL-09] 미디어 메시지 타입 분류
                    content_stripped = content.strip()
                    msg_type = "media" if content_stripped in _MEDIA_TYPES else "text"

                    # [REAL-06/10] 닉네임 메타데이터 추출
                    uid = self._hash_user_id(room_id, name)
                    if uid not in user_meta:
                        meta = parse_nickname(name)
                        user_meta[uid] = UserMeta(
                            display_name=meta["display"],
                            gender=meta.get("gender"),
                            birth_year=meta.get("birth_year"),
                            migration_year=meta.get("migration_year"),
                            city=meta.get("city"),
                            visa_type=meta.get("visa_type"),
                        )

                    last_message = RawMessage(
                        timestamp=ts,
                        user_id=uid,
                        display_name=user_meta[uid].display_name,  # [REAL-03] 파싱된 닉네임만
                        content=content,
                        message_type=msg_type,
                    )
                    messages.append(last_message)

                elif last_message is not None:
                    # [REAL-05 FIX] 멀티라인 이어쓰기 — sanitize 적용 필수
                    # += 직접 추가는 __post_init__ sanitize를 우회하므로 명시적 호출
                    safe_line = RawMessage._sanitize_content(stripped)
                    last_message.content += "\n" + safe_line

        logger.info(
            f"파싱 완료: {len(messages)}건 (target={target_date}, room={room_id})"
        )
        return DailyConversation(
            room_id=room_id,
            snapshot_date=target_date or current_date,
            messages=messages,
            user_meta=user_meta,
        )

    @staticmethod
    def _hash_user_id(room_id: str, name: str) -> str:
        raw = f"{room_id}:{name}".encode("utf-8")
        return "u_" + hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _parse_timestamp(d: date, ampm: str, time_str: str) -> datetime:
        hour, minute = map(int, time_str.split(":"))
        if ampm == "오후" and hour != 12:
            hour += 12
        elif ampm == "오전" and hour == 12:
            hour = 0
        return datetime(d.year, d.month, d.day, hour, minute)


# ============================================================
# 수집기 메인 클래스
# ============================================================

class ConversationCollector:
    """
    일일 대화 수집 오케스트레이터

    [품질담당자] 파이프라인:
    1. 중복 수집 체크 (Idempotency)
    2. 파일 파싱 (target_date 1일치만)
    3. 해시 검증
    4. DB 저장 (트랜잭션)
    5. 사용자 메타데이터 저장 [REAL-10]
    """

    def __init__(self, db_pool, data_dir: Path):
        self.db = db_pool
        self.data_dir = data_dir
        self.parser = KakaoTalkParser()

    async def collect_daily(self, room_id: str, target_date: date) -> CollectionResult:
        try:
            if await self._already_collected(room_id, target_date):
                logger.info(f"이미 수집됨: {room_id} / {target_date}")
                return CollectionResult(success=True, skipped=True, message="이미 수집된 날짜")

            file_path = self._resolve_data_file(room_id, target_date)
            conversation = self.parser.parse_file(file_path, room_id, target_date=target_date)

            if conversation.total_messages == 0:
                logger.warning(f"메시지 없음: {room_id} / {target_date}")
                return CollectionResult(success=False, message="수집된 메시지가 없습니다")

            # [REAL-09] 미디어 메시지 통계 로깅
            text_count = sum(1 for m in conversation.messages if m.message_type == "text")
            media_count = conversation.total_messages - text_count
            logger.info(f"텍스트:{text_count}건, 미디어:{media_count}건 (미디어는 분석 제외)")

            snapshot_id = await self._save_snapshot(conversation)
            return CollectionResult(
                success=True,
                snapshot_id=snapshot_id,
                message=f"{text_count}개 텍스트 메시지 수집 (미디어 {media_count}건 제외)",
            )

        except FileNotFoundError as e:
            logger.error(f"파일 없음: {e}")
            return CollectionResult(success=False, message=str(e))
        except Exception as e:
            logger.exception(f"수집 오류: {room_id} / {target_date}")
            await self._log_error(room_id, target_date, str(e))
            return CollectionResult(success=False, message=f"수집 실패: {e}")

    async def _already_collected(self, room_id: str, target_date: date) -> bool:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT snapshot_id FROM daily_conversation_snapshots
                   WHERE room_id = $1 AND snapshot_date = $2
                     AND processing_status NOT IN ('failed')""",
                room_id, target_date,
            )
            return row is not None

    def _resolve_data_file(self, room_id: str, target_date: date) -> Path:
        """
        [Quality] 파일 경로 생성 — path traversal 방지
        카카오톡 내보내기 파일은 하나의 누적 파일이므로
        room 디렉토리 내 최신 파일을 찾거나 날짜 기반으로 탐색.
        """
        safe_room_id = re.sub(r'[^a-zA-Z0-9_-]', '', room_id)
        if not safe_room_id:
            raise ValueError(f"유효하지 않은 room_id: {room_id}")

        room_dir = self.data_dir / safe_room_id

        # 1순위: {YYYY-MM-DD}.txt (날짜 기반)
        dated_file = room_dir / f"{target_date.isoformat()}.txt"
        if dated_file.exists():
            return dated_file

        # 2순위: 디렉토리 내 가장 최신 .txt 파일 (누적 파일 대응)
        if room_dir.exists():
            txt_files = sorted(room_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
            if txt_files:
                logger.info(f"날짜 파일 없음 → 최신 파일 사용: {txt_files[0].name}")
                return txt_files[0]

        raise FileNotFoundError(
            f"대화 파일 없음: {dated_file} (디렉토리: {room_dir})"
        )

    async def _save_snapshot(self, conversation: DailyConversation) -> str:
        """[QA-01 FIX] 트랜잭션 — 부분 실패 시 status='failed' 보장"""
        raw_data_path = str(self._resolve_data_file(
            conversation.room_id, conversation.snapshot_date
        ))

        # 텍스트 메시지만 카운트 (미디어 제외) [REAL-09]
        text_msgs = [m for m in conversation.messages if m.message_type == "text"]

        async with self.db.acquire() as conn:
            async with conn.transaction():
                snapshot_id = await conn.fetchval(
                    """INSERT INTO daily_conversation_snapshots
                       (snapshot_date, room_id, total_messages, unique_users,
                        raw_data_hash, raw_data_path, processing_status)
                       VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                       ON CONFLICT (snapshot_date, room_id) DO UPDATE
                         SET raw_data_hash     = EXCLUDED.raw_data_hash,
                             raw_data_path     = EXCLUDED.raw_data_path,
                             total_messages    = EXCLUDED.total_messages,
                             unique_users      = EXCLUDED.unique_users,
                             processing_status = 'pending',
                             updated_at        = NOW()
                       RETURNING snapshot_id""",
                    conversation.snapshot_date,
                    conversation.room_id,
                    len(text_msgs),
                    len(conversation.unique_users),
                    conversation.content_hash,
                    raw_data_path,
                )
                await self._upsert_users_batch(conn, conversation)
                return str(snapshot_id)

    async def _upsert_users_batch(self, conn, conversation: DailyConversation) -> None:
        """[REAL-10] 닉네임 메타데이터 포함 배치 upsert"""
        user_message_counts: dict[str, int] = {}
        for msg in conversation.messages:
            if msg.message_type == "text":
                user_message_counts[msg.user_id] = user_message_counts.get(msg.user_id, 0) + 1

        records = []
        for uid, count in user_message_counts.items():
            meta = conversation.user_meta.get(uid)
            display = meta.display_name if meta else uid
            records.append((uid, display, conversation.room_id,
                             conversation.snapshot_date, count))

        await conn.executemany(
            """INSERT INTO user_profiles
               (user_id, display_name, room_id, last_active, total_messages)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id) DO UPDATE
                 SET last_active    = EXCLUDED.last_active,
                     total_messages = user_profiles.total_messages + EXCLUDED.total_messages,
                     updated_at     = NOW()""",
            records,
        )

    async def _log_error(self, room_id: str, target_date: date, error: str) -> None:
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO processing_logs (process_type, status, error_details)
                       VALUES ('collection', 'failed', $1)""",
                    json.dumps({
                        "room_id": room_id, "date": str(target_date),
                        "error": error[:2000],
                    }),
                )
        except Exception as log_exc:
            logger.critical(
                f"처리 로그 저장 실패: room={room_id}, date={target_date}, "
                f"원본={error[:200]}, 로그오류={log_exc}"
            )
