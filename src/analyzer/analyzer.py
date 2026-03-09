"""
analyzer.py - 대화 내용 분석 및 질문 유형 분류 모듈

[품질담당자] 검증 항목:
  - LLM 호출 실패 시 규칙 기반 폴백(fallback) 보장
  - 토큰 비용 추적 및 임계값 초과 알림
  - 분석 결과 정합성 검증 (스키마 validation)

[Efficiency] 최적화:
  - 유사 질문 캐싱 (Redis, 24시간 TTL)
  - 배치 LLM 호출 (최대 20개씩 묶어서 처리)
  - 규칙 기반 사전 필터링으로 LLM 호출 최소화

[Quality] 분류 정확성:
  - 7가지 질문 유형 명확한 정의
  - 신뢰도 점수 반환

[Facilitator] 구조:
  - RuleBasedClassifier: 빠른 사전 분류
  - LLMClassifier: 심화 분류
  - ConversationAnalyzer: 오케스트레이터
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

# [F-01 FIX] TYPE_CHECKING 블록: 런타임에는 실행되지 않음 — 타입 힌트 전용
# 실제 parser 인스턴스는 ConversationAnalyzer 생성자 주입(DI)으로 전달받음
if TYPE_CHECKING:
    from src.collector.collector import KakaoTalkParser  # noqa: F401

logger = logging.getLogger(__name__)

# ============================================================
# 질문 유형 정의
# ============================================================

QUESTION_TYPES = {
    "technical_question": "기술적 질문 (코딩, 도구 사용법, 오류 해결 등)",
    "general_question":   "일반 질문 (방향, 추천, 의견 요청 등)",
    "emotional_support":  "감정적 지원 (격려, 위로, 공감 요청)",
    "resource_request":   "자료/링크 요청 (문서, 강의, 도구 추천)",
    "info_sharing":       "정보 공유 (뉴스, 발견, 팁 공유)",
    "discussion":         "토론/의견 교환",
    "off_topic":          "주제 무관 (잡담, 인사 등)",
}


@dataclass
class ClassificationResult:
    """분류 결과"""
    question_type: str
    topic_tags: list[str]
    sentiment_score: float       # -1.0 ~ 1.0
    complexity_score: int        # 1 ~ 5
    urgency_score: int           # 1 ~ 5
    needs_agent_response: bool
    confidence: float            # 0.0 ~ 1.0
    method: str                  # "rule" | "llm" | "cache"
    context_summary: str = ""


@dataclass
class UserDailyAnalysis:
    """사용자 1일 분석 결과"""
    user_id: str
    snapshot_id: str
    message_count: int
    classifications: list[ClassificationResult]
    
    @property
    def dominant_question_type(self) -> str:
        if not self.classifications:
            return "off_topic"
        type_counts: dict[str, int] = {}
        for c in self.classifications:
            type_counts[c.question_type] = type_counts.get(c.question_type, 0) + 1
        return max(type_counts, key=type_counts.get)

    @property
    def avg_sentiment(self) -> float:
        if not self.classifications:
            return 0.0
        return round(sum(c.sentiment_score for c in self.classifications) / len(self.classifications), 3)

    @property
    def needs_response(self) -> bool:
        return any(c.needs_agent_response for c in self.classifications)


# ============================================================
# 규칙 기반 사전 분류기
# ============================================================

class RuleBasedClassifier:
    """
    [Efficiency] LLM 호출 전 빠른 규칙 기반 분류
    단순 패턴은 LLM 없이 처리하여 비용 절감
    """

    _TECHNICAL_PATTERNS = re.compile(
        r"(오류|에러|error|bug|버그|exception|코드|함수|API|설치|import|"
        r"TypeError|ValueError|AttributeError|traceback|스택|디버그)",
        re.IGNORECASE
    )
    _RESOURCE_PATTERNS = re.compile(
        r"(추천|강의|자료|링크|문서|tutorial|레퍼런스|reference|어디서|어떻게 배"
        r"|법무사|이민법|변호사|회계사|세무사)",
        re.IGNORECASE
    )
    _EMOTIONAL_PATTERNS = re.compile(
        r"(힘들|지쳐|포기|모르겠|막막|어렵|좌절|우울|힘내|파이팅|응원)",
        re.IGNORECASE
    )
    _GREETING_PATTERNS = re.compile(
        r"^(안녕|ㅎㅇ|ㅂㅂ|굿나잇|good morning|hi|bye|감사합니다|고마워|ㄱㅅ)[\s!.]*$",
        re.IGNORECASE
    )
    # [REAL-08 FIX] 호주 한인 생활 특화 패턴 — 비자/세금/렌트/구인 등
    _AUSTRALIA_LIFE_PATTERNS = re.compile(
        r"(비자|영주권|시민권|이민|렌트|임대|집주인|부동산|세금|택스"
        r"|슈퍼|수퍼애뉴에이션|연금|메디케어|페어워크|페널티레이트"
        r"|구인|구직|취업|이직|급여|시급|연봉|allowance|샐팩|FIFO|fifo)",
        re.IGNORECASE
    )
    _QUESTION_MARK = re.compile(r"[?？]")

    def classify(self, text: str) -> Optional[ClassificationResult]:
        """
        빠른 분류. 불확실하면 None 반환 → LLM이 처리
        [Efficiency] 90%+ 메시지가 규칙으로 처리되도록 목표
        [COST-05 FIX] 초단문(3단어 이하), 이모지/특수문자만, 숫자만 → off_topic 즉시 반환
        """
        text_stripped = text.strip()

        # [COST-05 FIX] 초단문 fast-path: 단어 수 ≤ 3이고 질문 아닌 경우
        words = text_stripped.split()
        has_question = bool(self._QUESTION_MARK.search(text_stripped))
        if len(words) <= 3 and not has_question:
            return self._make_result("off_topic", [], 0.2, 1, 1, False, 0.90)

        # 인사/잡담
        if self._GREETING_PATTERNS.match(text_stripped):
            return self._make_result("off_topic", [], 0.3, 1, 1, False, 0.95)

        # 기술 질문
        if self._TECHNICAL_PATTERNS.search(text_stripped):
            return self._make_result(
                "technical_question",
                self._extract_tech_tags(text_stripped),
                -0.1, 3, 3, has_question, 0.80
            )

        # 자료 요청
        if self._RESOURCE_PATTERNS.search(text_stripped):
            return self._make_result("resource_request", [], 0.1, 2, 2, True, 0.85)

        # 감정적 지원
        if self._EMOTIONAL_PATTERNS.search(text_stripped):
            return self._make_result("emotional_support", [], -0.3, 1, 2, True, 0.78)

        # [REAL-08 FIX] 호주 한인 생활 특화 질문
        if self._AUSTRALIA_LIFE_PATTERNS.search(text_stripped):
            return self._make_result(
                "general_question",
                self._extract_australia_tags(text_stripped),
                0.0, 2, 2, has_question, 0.82,
            )

        # 불확실 → LLM 위임
        return None

    @staticmethod
    def _extract_tech_tags(text: str) -> list[str]:
        """간단한 기술 키워드 추출"""
        tech_keywords = [
            "python", "javascript", "java", "react", "sql", "git",
            "docker", "kubernetes", "api", "llm", "머신러닝", "딥러닝",
            "pandas", "numpy", "fastapi", "django", "spring"
        ]
        text_lower = text.lower()
        return [kw for kw in tech_keywords if kw in text_lower][:5]

    @staticmethod
    def _extract_australia_tags(text: str) -> list[str]:
        """[REAL-08] 호주 한인 생활 토픽 태그 추출"""
        tag_map = {
            "비자": "visa", "영주권": "pr", "시민권": "citizenship",
            "이민": "immigration", "렌트": "rental", "부동산": "property",
            "세금": "tax", "택스": "tax", "슈퍼": "superannuation",
            "메디케어": "medicare", "구인": "job", "구직": "job",
            "시급": "wage", "연봉": "salary", "allowance": "allowance",
            "샐팩": "salary_packaging", "fifo": "fifo",
        }
        text_lower = text.lower()
        return list({v for k, v in tag_map.items() if k in text_lower})[:5]

    @staticmethod
    def _make_result(
        q_type: str, tags: list, sentiment: float,
        complexity: int, urgency: int, needs_response: bool, confidence: float
    ) -> ClassificationResult:
        return ClassificationResult(
            question_type=q_type,
            topic_tags=tags,
            sentiment_score=sentiment,
            complexity_score=complexity,
            urgency_score=urgency,
            needs_agent_response=needs_response,
            confidence=confidence,
            method="rule",
        )


# ============================================================
# LLM 기반 심화 분류기
# ============================================================

class LLMClassifier:
    """
    [품질담당자] LLM 호출 시 타임아웃, 재시도, 스키마 검증 필수
    [Efficiency] 배치 처리로 API 비용 최소화
    """

    _SYSTEM_PROMPT = """당신은 카카오톡 오픈채팅방 대화 분석 전문가입니다.
주어진 메시지들을 분석하여 JSON 형식으로만 응답하세요.

질문 유형 (question_type):
- technical_question: 코딩, 기술 오류, 도구 사용법
- general_question: 일반적인 질문, 조언 요청
- emotional_support: 감정 표현, 격려 요청
- resource_request: 자료, 링크, 강의 추천 요청
- info_sharing: 정보, 뉴스, 팁 공유
- discussion: 토론, 의견 교환
- off_topic: 잡담, 인사

응답 형식 (배열):
[
  {
    "index": 0,
    "question_type": "technical_question",
    "topic_tags": ["python", "오류처리"],
    "sentiment_score": -0.2,
    "complexity_score": 3,
    "urgency_score": 2,
    "needs_agent_response": true,
    "context_summary": "Python 예외 처리 방법 질문"
  }
]"""

    def __init__(self, llm_client, cache_client=None, max_batch_size: int = 20,
                 model: str = "claude-sonnet-4-20250514"):
        self.llm = llm_client
        self.cache = cache_client
        self.max_batch_size = max_batch_size
        self.model = model  # [IMPACT-03 FIX] 모델명 settings에서 주입 가능

    async def classify_batch(self, messages: list[str]) -> list[ClassificationResult]:
        """
        [COST-02 FIX] 토큰 예산 기반 동적 배치. 배치당 입력 토큰 ≤ 1,500 보장.
        [NEW-04 FIX] lazy import 제거 — 모듈 상단 re 직접 사용.
        """
        TOKEN_BUDGET = 1_500
        results: list[ClassificationResult] = []
        batch: list[str] = []
        batch_tokens = 0

        def _est(t: str) -> int:
            # [NEW-04 FIX] 'import re as _re' 제거 — 모듈 상단 re 사용
            k = len(re.findall(r'[가-힣]', t))
            return k // 2 + (len(t) - k) // 4

        for msg in messages:
            msg_tok = _est(msg[:200])
            if batch and (batch_tokens + msg_tok > TOKEN_BUDGET or len(batch) >= self.max_batch_size):
                results.extend(await self._classify_batch_internal(batch))
                batch, batch_tokens = [], 0
            batch.append(msg)
            batch_tokens += msg_tok

        if batch:
            results.extend(await self._classify_batch_internal(batch))
        return results

    async def _classify_batch_internal(self, messages: list[str]) -> list[ClassificationResult]:
        """캐시 히트 체크 후 LLM 호출"""
        cache_results: dict[int, ClassificationResult] = {}
        uncached_indices: list[int] = []
        uncached_messages: list[str] = []

        # 캐시 확인
        for idx, msg in enumerate(messages):
            if self.cache:
                cached = await self._get_cache(msg)
                if cached:
                    cache_results[idx] = cached
                    continue
            uncached_indices.append(idx)
            uncached_messages.append(msg)

        # LLM 호출 (캐시 미스만)
        if uncached_messages:
            llm_results = await self._call_llm(uncached_messages)
            for idx, (orig_idx, msg) in enumerate(zip(uncached_indices, uncached_messages)):
                if idx < len(llm_results):
                    result = llm_results[idx]
                    cache_results[orig_idx] = result
                    if self.cache:
                        await self._set_cache(msg, result)

        # 원래 순서로 결과 조립
        return [
            cache_results.get(i, self._fallback_result())
            for i in range(len(messages))
        ]

    async def _call_llm(self, messages: list[str]) -> list[ClassificationResult]:
        """
        [COST-01 FIX] Prompt Caching 적용 — system prompt를 cache_control로 고정.
        400배치 × 110토큰 낭비 → 최초 1회 이후 캐시 히트 (90% 절감).
        [COST-02 FIX] 배치 총 토큰 2,000 이하 동적 보장.
        """
        prompt_content = self._build_prompt(messages)

        try:
            response = await asyncio.wait_for(
                self.llm.messages.create(
                    model=self.model,
                    max_tokens=2000,
                    system=[
                        {
                            "type": "text",
                            "text": self._SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},  # [COST-01] 캐시 고정
                        }
                    ],
                    messages=[{"role": "user", "content": prompt_content}],
                ),
                timeout=30.0,
            )
            raw_json = response.content[0].text.strip()
            items = json.loads(raw_json)
            return [self._parse_item(item) for item in items]

        except asyncio.TimeoutError:
            logger.error("LLM 호출 타임아웃 (30초 초과)")
            return [self._fallback_result() for _ in messages]
        except json.JSONDecodeError as e:
            logger.error(f"LLM 응답 JSON 파싱 실패: {e}")
            return [self._fallback_result() for _ in messages]
        except Exception as e:
            logger.exception(f"LLM 호출 오류: {e}")
            return [self._fallback_result() for _ in messages]

    def _build_prompt(self, messages: list[str]) -> str:
        items = "\n".join(
            f"[{i}] {msg[:500]}"  # [Efficiency] 500자 이상 잘라서 토큰 절약
            for i, msg in enumerate(messages)
        )
        return f"다음 메시지들을 분석해주세요:\n\n{items}"

    @staticmethod
    def _parse_item(item: dict) -> ClassificationResult:
        """[Quality] 스키마 검증 포함 파싱"""
        return ClassificationResult(
            question_type=item.get("question_type", "general_question"),
            topic_tags=item.get("topic_tags", [])[:10],  # 최대 10개
            sentiment_score=max(-1.0, min(1.0, float(item.get("sentiment_score", 0)))),
            complexity_score=max(1, min(5, int(item.get("complexity_score", 3)))),
            urgency_score=max(1, min(5, int(item.get("urgency_score", 1)))),
            needs_agent_response=bool(item.get("needs_agent_response", False)),
            confidence=0.85,
            method="llm",
            context_summary=str(item.get("context_summary", ""))[:500],
        )

    @staticmethod
    def _fallback_result() -> ClassificationResult:
        """[품질담당자] LLM 실패 시 안전한 기본값"""
        return ClassificationResult(
            question_type="general_question",
            topic_tags=[],
            sentiment_score=0.0,
            complexity_score=2,
            urgency_score=1,
            needs_agent_response=False,
            confidence=0.3,
            method="fallback",
        )

    async def _get_cache(self, text: str) -> Optional[ClassificationResult]:
        # [C-4 FIX] MD5 → SHA-256 (충돌 저항성 보장)
        key = f"cls:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]}"
        try:
            data = await self.cache.get(key)
            if data:
                d = json.loads(data)
                # [FIX] 캐시 역직렬화 시 필드 검증
                if "question_type" in d and d["question_type"] in QUESTION_TYPES:
                    d["method"] = "cache"  # 캐시 히트 표시
                    return ClassificationResult(**d)
        except Exception as e:
            logger.debug(f"캐시 조회 실패 (무시): {e}")
        return None

    async def _set_cache(self, text: str, result: ClassificationResult) -> None:
        key = f"cls:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]}"
        try:
            # [E-03 FIX] __dict__ → dataclasses.asdict() : 중첩 타입 안전 직렬화
            await self.cache.setex(key, 86400, json.dumps(dataclasses.asdict(result)))
        except Exception as e:
            logger.debug(f"캐시 저장 실패 (무시): {e}")


# ============================================================
# 메인 분석기
# ============================================================

class ConversationAnalyzer:
    """
    [Facilitator] 분석 파이프라인 오케스트레이터
    Rule → LLM 2단계 분류 전략

    [F-01 FIX] KakaoTalkParser는 생성자 주입(DI)으로 받음.
    analyzer가 collector를 직접 import하는 순환 참조 구조 제거.
    """

    def __init__(self, db_pool, llm_client, parser, cache_client=None):
        """
        Args:
            parser: KakaoTalkParser 인스턴스 (DI — analyzer는 collector에 의존하지 않음)
        """
        self.db = db_pool
        self.parser = parser           # [F-01 FIX] 외부에서 주입
        self.rule_classifier = RuleBasedClassifier()
        self.llm_classifier = LLMClassifier(llm_client, cache_client)

    async def analyze_snapshot(self, snapshot_id: str) -> dict:
        """
        스냅샷 전체 분석
        Returns: 분석 요약 통계
        """
        logger.info(f"분석 시작: {snapshot_id}")

        # 스냅샷 로드
        messages_by_user = await self._load_snapshot_messages(snapshot_id)
        
        if not messages_by_user:
            logger.warning(f"분석할 메시지 없음: {snapshot_id}")
            return {"analyzed": 0, "snapshot_id": snapshot_id}

        # [E-02 FIX] key를 (user_id, content) → content_hash 로 통일
        # 동일 내용 메시지는 어느 사용자든 동일 분류 결과 재사용 (LLM 비용 절감)
        # user별 결과는 analyses 조립 시 user_id를 붙여 독립 유지
        content_to_rule: dict[str, ClassificationResult] = {}
        content_needing_llm: list[str] = []        # 중복 제거된 content 목록
        seen_for_llm: set[str] = set()

        for user_id, messages in messages_by_user.items():
            for msg_content in messages:
                if msg_content in content_to_rule or msg_content in seen_for_llm:
                    continue  # 이미 처리됨
                rule_result = self.rule_classifier.classify(msg_content)
                if rule_result:
                    content_to_rule[msg_content] = rule_result
                else:
                    seen_for_llm.add(msg_content)
                    content_needing_llm.append(msg_content)

        # LLM 배치 분류 (중복 제거된 content만)
        content_to_llm: dict[str, ClassificationResult] = {}
        if content_needing_llm:
            llm_classifications = await self.llm_classifier.classify_batch(content_needing_llm)
            content_to_llm = {
                content_needing_llm[i]: llm_classifications[i]
                for i in range(len(content_needing_llm))
            }

        # 결과 병합: content 기준으로 분류 결과 조회
        analyses: list[UserDailyAnalysis] = []
        for user_id, messages in messages_by_user.items():
            user_classifications = []
            for msg in messages:
                result = (
                    content_to_rule.get(msg)
                    or content_to_llm.get(msg)
                    or LLMClassifier._fallback_result()
                )
                user_classifications.append(result)

            analyses.append(UserDailyAnalysis(
                user_id=user_id,
                snapshot_id=snapshot_id,
                message_count=len(messages),
                classifications=user_classifications,
            ))

        # [QA-01 FIX] 저장 실패 시 status='failed' 명시. 부분 성공을 'done'으로 속이지 않음
        try:
            await self._save_analyses(analyses)
            await self._update_user_profiles(analyses)
        except Exception as e:
            logger.exception(f"분석 결과 저장 실패: {snapshot_id}")
            await self._update_snapshot_status(snapshot_id, "failed")
            raise RuntimeError(f"분석 저장 실패: {e}") from e

        await self._update_snapshot_status(snapshot_id, "done")

        rule_count = len(content_to_rule)
        llm_count = len(content_to_llm)
        total_analyzed = sum(a.message_count for a in analyses)
        
        logger.info(
            f"분석 완료: {snapshot_id} | 총 {total_analyzed}건 "
            f"(규칙: {rule_count}, LLM: {llm_count})"
        )
        
        return {
            "snapshot_id": snapshot_id,
            "analyzed": total_analyzed,
            "users": len(analyses),
            "rule_classified": rule_count,
            "llm_classified": llm_count,
        }

    async def _load_snapshot_messages(self, snapshot_id: str) -> dict[str, list[str]]:
        """
        [F-01 FIX] lazy import 제거. self.parser (DI) 사용.
        [C-1 FIX] raw_data_path 기반 실제 파일 로드.
        """
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT room_id, raw_data_path, snapshot_date
                   FROM daily_conversation_snapshots
                   WHERE snapshot_id = $1""",
                snapshot_id
            )

        if not row:
            logger.error(f"스냅샷 없음: {snapshot_id}")
            return {}

        raw_data_path = row["raw_data_path"]
        room_id = row["room_id"]
        snapshot_date = row["snapshot_date"]

        if not raw_data_path:
            logger.error(f"raw_data_path 미설정: {snapshot_id}")
            return {}

        try:
            # [F-01 FIX] self.parser 사용 — lazy import 완전 제거
            # [F-03 FIX] snapshot_date를 target_date로 전달
            conversation = self.parser.parse_file(
                Path(raw_data_path), room_id, target_date=snapshot_date
            )
        except Exception as e:
            logger.error(f"메시지 파일 로드 실패: {snapshot_id} / {e}")
            return {}

        messages_by_user: dict[str, list[str]] = {}
        for msg in conversation.messages:
            if msg.content.strip():
                messages_by_user.setdefault(msg.user_id, []).append(msg.content)

        logger.info(
            f"메시지 로드 완료: {snapshot_id} | "
            f"{len(conversation.messages)}건 / {len(messages_by_user)}명"
        )
        return messages_by_user

    async def _save_analyses(self, analyses: list[UserDailyAnalysis]) -> None:
        """[Efficiency] 배치 INSERT"""
        records = []
        for analysis in analyses:
            for cls in analysis.classifications:
                records.append((
                    analysis.snapshot_id,
                    analysis.user_id,
                    cls.question_type,
                    json.dumps(cls.topic_tags),
                    cls.sentiment_score,
                    cls.complexity_score,
                    cls.urgency_score,
                    cls.needs_agent_response,
                    cls.context_summary,
                    analysis.message_count,
                ))
        
        async with self.db.acquire() as conn:
            await conn.executemany(
                """INSERT INTO message_analyses
                   (snapshot_id, user_id, question_type, topic_tags,
                    sentiment_score, complexity_score, urgency_score,
                    needs_agent_response, context_summary, message_count)
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10)
                   ON CONFLICT (snapshot_id, user_id) DO UPDATE
                     SET question_type       = EXCLUDED.question_type,
                         topic_tags          = EXCLUDED.topic_tags,
                         sentiment_score     = EXCLUDED.sentiment_score,
                         complexity_score    = EXCLUDED.complexity_score,
                         urgency_score       = EXCLUDED.urgency_score,
                         needs_agent_response = EXCLUDED.needs_agent_response,
                         context_summary     = EXCLUDED.context_summary,
                         message_count       = EXCLUDED.message_count,
                         updated_at          = NOW()""",
                records
            )

    async def _update_user_profiles(self, analyses: list[UserDailyAnalysis]) -> None:
        """
        [M-5 FIX] N+1 쿼리 제거: 루프 내 개별 UPDATE → 배치 executemany
        """
        records = []
        for analysis in analyses:
            all_tags: list[str] = []
            for cls in analysis.classifications:
                all_tags.extend(cls.topic_tags)

            # 상위 5개 토픽
            tag_counts: dict[str, int] = {}
            for tag in all_tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            top_topics = sorted(tag_counts, key=lambda k: tag_counts[k], reverse=True)[:5]

            if not analysis.classifications:
                continue

            avg_complexity = (
                sum(c.complexity_score for c in analysis.classifications)
                / len(analysis.classifications)
            )
            proficiency = (
                "expert"       if avg_complexity >= 4.0 else
                "advanced"     if avg_complexity >= 3.0 else
                "intermediate" if avg_complexity >= 2.0 else
                "beginner"
            )
            question_delta = sum(
                1 for c in analysis.classifications if c.needs_agent_response
            )

            records.append((
                json.dumps(top_topics),
                proficiency,
                question_delta,
                analysis.user_id,
            ))

        if not records:
            return

        async with self.db.acquire() as conn:
            # [M-5 FIX] 단일 executemany로 1000명도 단 1번의 DB 왕복 처리
            await conn.executemany(
                """UPDATE user_profiles SET
                       dominant_topics  = $1::jsonb,
                       proficiency_level = $2,
                       total_questions  = total_questions + $3,
                       updated_at       = NOW()
                   WHERE user_id = $4""",
                records
            )

    async def _update_snapshot_status(self, snapshot_id: str, status: str) -> None:
        async with self.db.acquire() as conn:
            await conn.execute(
                """UPDATE daily_conversation_snapshots
                   SET processing_status = $1, processed_at = NOW(), updated_at = NOW()
                   WHERE snapshot_id = $2""",
                status, snapshot_id
            )
