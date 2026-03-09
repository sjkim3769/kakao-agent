"""
agent.py - 카카오톡 대화방 Agent 댓글 생성 모듈

[품질담당자] 검증 항목:
  - 1일 1회 댓글 생성 보장 (DB 유니크 제약)
  - 생성된 댓글 품질 자동 검증 (길이, 금지어, 감정 톤)
  - 수동 검토 워크플로우 지원

[Efficiency] 최적화:
  - 분석 결과 캐시 활용 (LLM 재호출 최소화)
  - 템플릿 우선 사용, LLM은 템플릿 없는 경우만

[Quality] UX:
  - 댓글 톤: 따뜻하고 전문적
  - 사용자 숙련도별 맞춤 응답
  - 금지어/부적절 표현 필터

[Facilitator] 구조:
  - CommentGenerator: 댓글 생성
  - ResponseBuilder: Q&A 응답 생성
  - KakaoAgent: 통합 오케스트레이터
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 도메인 모델
# ============================================================

@dataclass
class DailyCommentContext:
    """댓글 생성을 위한 컨텍스트 데이터"""
    snapshot_id: str
    target_date: date
    room_id: str
    total_messages: int
    unique_users: int
    top_topics: list[str]
    question_count: int
    avg_sentiment: float
    # 유형별 분류
    technical_questions: list[str]   # 미응답 기술 질문들
    popular_topics: list[str]        # 많이 언급된 토픽
    notable_discussions: list[str]   # 주목할 토론 주제


@dataclass
class GeneratedComment:
    """생성된 댓글"""
    content: str
    comment_type: str
    estimated_tone: str      # "warm" | "informative" | "encouraging"
    word_count: int
    passed_quality_check: bool
    quality_issues: list[str]


# ============================================================
# 댓글 품질 검사기
# ============================================================

class CommentQualityChecker:
    """
    [품질담당자] 생성된 댓글 품질 자동 검증
    5가지 품질 게이트 통과 필수
    """

    MIN_LENGTH = 50
    MAX_LENGTH = 500
    
    # [M-3 FIX] '죽' 단독 패턴은 오탐 과다 (죽음밥, 나물죽 등 차단)
    # 실제 유해 표현만 정확히 타게팅하도록 수정
    _PROHIBITED_PATTERNS = re.compile(
        r'(자살|자해|죽고\s*싶|죽이고\s*싶|혐오|비하|개새끼|씨발|병신|지랄)',
        re.IGNORECASE
    )
    _SPAM_PATTERNS = re.compile(
        r'(클릭|광고|홍보|구매|할인|이벤트)',
        re.IGNORECASE
    )

    def check(self, comment: str) -> tuple[bool, list[str]]:
        """
        Returns: (passed, issues)
        [품질담당자] 모든 게이트 통과해야 auto_approved
        """
        issues = []

        # Gate 1: 길이 검사
        if len(comment) < self.MIN_LENGTH:
            issues.append(f"댓글이 너무 짧음 ({len(comment)}자 < {self.MIN_LENGTH}자)")
        if len(comment) > self.MAX_LENGTH:
            issues.append(f"댓글이 너무 김 ({len(comment)}자 > {self.MAX_LENGTH}자)")

        # Gate 2: 금지어 검사
        if self._PROHIBITED_PATTERNS.search(comment):
            issues.append("금지어 포함")

        # Gate 3: 스팸 패턴 검사
        if self._SPAM_PATTERNS.search(comment):
            issues.append("광고성 표현 포함")

        # Gate 4: 의미 있는 내용 여부
        if len(set(comment.split())) < 5:
            issues.append("어휘 다양성 부족 (의미없는 반복 가능성)")

        # Gate 5: 카카오톡 형식 적합성
        if comment.count('\n') > 10:
            issues.append("줄바꿈 과다 (카카오톡 가독성 저하)")

        passed = len(issues) == 0
        return passed, issues


# ============================================================
# 일일 댓글 생성기
# ============================================================

class DailyCommentGenerator:
    """
    [Facilitator] 단일 책임: 일일 요약 댓글만 생성
    """

    _SYSTEM_PROMPT = """당신은 1,000명 이상이 참여하는 카카오톡 오픈채팅방의 AI 운영자입니다.
매일 한 번, 당일 대화 내용을 분석하여 따뜻하고 유익한 댓글을 작성합니다.

댓글 작성 원칙:
1. 따뜻하고 격려하는 톤 유지
2. 구체적인 내용 언급 (오늘 나온 주요 주제, 좋은 질문들)
3. 커뮤니티 참여 독려
4. 내일을 기대하게 하는 마무리
5. 카카오톡 특성상 짧고 읽기 쉽게 (200자 내외)
6. 이모지 1-2개 적절히 사용
7. 특정 사용자 지목 금지 (개인정보 보호)

절대 하지 말 것:
- 특정인 비판 또는 과도한 칭찬
- 광고성 내용
- 정치적/종교적 발언
- 개인정보 언급"""

    def __init__(self, llm_client, model: str = "claude-sonnet-4-20250514"):
        self.llm = llm_client
        self.model = model  # [IMPACT-03 FIX] 모델명 단일 관리
        self.quality_checker = CommentQualityChecker()

    async def generate(self, context: DailyCommentContext) -> GeneratedComment:
        """
        [품질담당자] 생성 → 품질검사 → 재생성(1회) 파이프라인
        """
        for attempt in range(2):  # 최대 2회 시도
            comment_text = await self._call_llm(context, attempt)
            passed, issues = self.quality_checker.check(comment_text)
            
            if passed:
                logger.info(f"댓글 생성 성공 (시도 {attempt + 1}): {len(comment_text)}자")
                return GeneratedComment(
                    content=comment_text,
                    comment_type="daily_summary",
                    estimated_tone=self._estimate_tone(comment_text),
                    word_count=len(comment_text),
                    passed_quality_check=True,
                    quality_issues=[],
                )
            
            logger.warning(f"품질 검사 실패 (시도 {attempt + 1}): {issues}")

        # [Q-02 FIX] 2회 시도 모두 실패 → 빈 댓글 저장 금지, 수동 입력 요청 placeholder
        logger.error("댓글 품질 검사 2회 실패 → 수동 검토 필요")
        placeholder = "[자동 생성 실패 — 관리자 수동 입력 필요]"
        return GeneratedComment(
            content=placeholder,
            comment_type="daily_summary",
            estimated_tone="unknown",
            word_count=len(placeholder),
            passed_quality_check=False,
            quality_issues=issues,
        )

    async def _call_llm(self, context: DailyCommentContext, attempt: int) -> str:
        user_prompt = self._build_prompt(context, attempt)
        # [COST-03 FIX] max_tokens=500 → 300 (시스템 프롬프트 '200자 내외'와 일치)
        # [IMPACT-03] model은 외부 주입 가능하도록 클래스 속성화
        response = await self.llm.messages.create(
            model=self.model,
            max_tokens=300,
            system=self._SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()

    def _build_prompt(self, ctx: DailyCommentContext, attempt: int) -> str:
        retry_note = "\n⚠️ 이전 댓글이 품질 기준 미달. 더 자연스럽고 간결하게 재작성." if attempt > 0 else ""
        
        topics_str = ", ".join(ctx.top_topics[:5]) if ctx.top_topics else "다양한 주제"
        
        return f"""오늘({ctx.target_date}) 대화방 요약:
- 총 메시지: {ctx.total_messages}개
- 참여자: {ctx.unique_users}명
- 주요 주제: {topics_str}
- 기술 질문: {ctx.question_count}개
- 분위기: {'긍정적' if ctx.avg_sentiment > 0.1 else '중립적' if ctx.avg_sentiment > -0.1 else '다소 어려움'}

오늘의 일일 댓글을 작성해주세요.{retry_note}"""

    @staticmethod
    def _estimate_tone(comment: str) -> str:
        if any(w in comment for w in ["수고", "감사", "응원", "💪", "🎉"]):
            return "encouraging"
        if any(w in comment for w in ["주제", "질문", "오늘", "내용"]):
            return "informative"
        return "warm"


# ============================================================
# Q&A 응답 생성기
# ============================================================

class QAResponseBuilder:
    """
    [Facilitator] 유형별 질문에 대한 맞춤 응답 생성
    템플릿 우선, LLM 보완 전략
    """

    _SYSTEM_PROMPT = """당신은 카카오톡 채팅방 AI 어시스턴트입니다.
사용자의 질문에 해당 사용자의 숙련도 수준에 맞게 답변합니다.

답변 원칙:
1. 사용자 숙련도 고려 (beginner: 쉽게, expert: 심화)
2. 핵심만 간결하게
3. 코드가 필요하면 포함 (카카오톡 코드블록 형식)
4. 참고 자료 있으면 공식 문서 우선 링크
5. 모르면 솔직하게 "확인 후 답변드릴게요" 라고 답변"""

    def __init__(self, db_pool, llm_client, model: str = "claude-sonnet-4-20250514"):
        self.db = db_pool
        self.llm = llm_client
        self.model = model  # [IMPACT-03 FIX]

    async def build_response(
        self,
        question: str,
        question_type: str,
        topic_tags: list[str],
        user_proficiency: str,
    ) -> str:
        """
        템플릿 조회 → LLM 생성 순서
        [M-7 FIX] 템플릿이 있어도 치환 결과가 비어있으면 LLM으로 폴백
        """
        template = await self._find_template(question_type, topic_tags, user_proficiency)
        if template:
            filled = self._fill_template(template, question, topic_tags)
            if filled:  # [M-7 FIX] 빈 문자열이면 LLM으로 폴백
                logger.debug(f"템플릿 히트: {question_type} / {user_proficiency}")
                return filled

        return await self._generate_with_llm(question, question_type, topic_tags, user_proficiency)

    async def _find_template(
        self, question_type: str, topics: list[str], proficiency: str
    ) -> Optional[str]:
        """
        [NEW-06 FIX] SELECT + UPDATE를 동일 conn 블록 내 단일 트랜잭션으로 처리.
        이전 코드: with 블록 종료 후 conn.execute → asyncpg InterfaceError 위험.
        """
        async with self.db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT template_id, template_content
                       FROM response_templates
                       WHERE question_type = $1
                         AND (topic_tag = ANY($2) OR topic_tag IS NULL)
                         AND (proficiency_level = $3 OR proficiency_level IS NULL)
                         AND is_active = TRUE
                       ORDER BY
                         CASE WHEN topic_tag = ANY($2) THEN 0 ELSE 1 END,
                         CASE WHEN proficiency_level = $3 THEN 0 ELSE 1 END
                       LIMIT 1""",
                    question_type, topics or [], proficiency
                )
                if row:
                    await conn.execute(
                        "UPDATE response_templates SET usage_count = usage_count + 1"
                        " WHERE template_id = $1",
                        row["template_id"]
                    )
                    return row["template_content"]
        return None

    @staticmethod
    def _fill_template(template: str, question: str, topics: list[str]) -> str:
        """
        [M-7 FIX] 템플릿 변수 치환. {answer} 같은 미치환 변수가 남으면
        빈 문자열 대신 명시적 경고를 남기고 None 반환 → LLM 폴백 유도.
        """
        topic_str = topics[0] if topics else "해당 주제"
        filled = (template
                  .replace("{topic}", topic_str)
                  .replace("{question}", question[:100])
                  .replace("{answer}", "")       # answer는 LLM이 채워야 할 부분
                  .replace("{user_name}", "")
                  .replace("{reference}", ""))

        # 미치환 변수 감지
        remaining = re.findall(r'\{[^}]+\}', filled)
        if remaining:
            logger.warning(f"템플릿 미치환 변수 발견: {remaining} → LLM으로 폴백")
            return ""  # 빈 문자열 반환 시 build_response가 LLM 호출로 이어짐

        return filled.strip()

    async def _generate_with_llm(
        self, question: str, q_type: str, topics: list[str], proficiency: str
    ) -> str:
        prompt = f"""질문 유형: {q_type}
사용자 숙련도: {proficiency}
주제 태그: {', '.join(topics)}
질문: {question}

위 질문에 대한 답변을 300자 이내로 작성해주세요."""

        # [COST-04 FIX] max_tokens=800 → 400 (카카오톡 1,000자 제한 기준 충분)
        # [IMPACT-03 FIX] model 속성 사용
        response = await self.llm.messages.create(
            model=self.model,
            max_tokens=400,
            system=self._SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()


# ============================================================
# 통합 Agent
# ============================================================

class KakaoAgent:
    """
    [Facilitator] 전체 Agent 워크플로우 오케스트레이터
    
    일일 실행 순서:
    1. 분석 완료 스냅샷 조회
    2. 일일 요약 댓글 생성
    3. 미응답 질문 처리
    4. 결과 저장 + 승인 대기
    """

    def __init__(self, db_pool, llm_client, cache_client=None):
        self.db = db_pool
        self.comment_gen = DailyCommentGenerator(llm_client)
        self.qa_builder = QAResponseBuilder(db_pool, llm_client)

    async def run_daily(self, room_id: str, target_date: date) -> dict:
        """
        [품질담당자] 전체 일일 Agent 실행
        각 단계 실패가 전체 실패로 이어지지 않도록 개별 예외 처리
        """
        results = {
            "room_id": room_id,
            "date": str(target_date),
            "daily_comment": None,
            "qa_responses": [],
            "errors": [],
        }

        # Step 1: 오늘 이미 댓글 달았는지 확인
        if await self._already_commented(room_id, target_date):
            logger.info(f"오늘 이미 댓글 완료: {room_id} / {target_date}")
            results["skipped"] = True
            return results

        # Step 2: 컨텍스트 조회
        context = await self._load_context(room_id, target_date)
        if not context:
            results["errors"].append("분석된 스냅샷 없음")
            return results

        # Step 3: 일일 댓글 생성
        try:
            comment = await self.comment_gen.generate(context)
            await self._save_comment(context.snapshot_id, target_date, comment)
            results["daily_comment"] = {
                "content": comment.content,
                "passed_qc": comment.passed_quality_check,
                "issues": comment.quality_issues,
            }
            logger.info(f"일일 댓글 생성 완료: {target_date}")
        except Exception as e:
            logger.exception("일일 댓글 생성 실패")
            results["errors"].append(f"댓글 생성 오류: {e}")

        # Step 4: 미응답 질문 처리 (상위 5개)
        try:
            unanswered = await self._load_unanswered_questions(context.snapshot_id, limit=5)
            # [PROD-02 FIX] N+1 제거: 개별 SELECT → IN절 한 번에 조회
            user_ids = list({q["user_id"] for q in unanswered})
            proficiency_map = await self._get_users_proficiency_batch(user_ids)

            for q in unanswered:
                proficiency = proficiency_map.get(q["user_id"], "intermediate")
                response = await self.qa_builder.build_response(
                    question=q["context_summary"],
                    question_type=q["question_type"],
                    topic_tags=q["topic_tags"],
                    user_proficiency=proficiency,
                )
                results["qa_responses"].append({
                    "question_type": q["question_type"],
                    "response_preview": response[:100] + "...",
                })
            logger.info(f"Q&A 응답 {len(results['qa_responses'])}건 생성")
        except Exception as e:
            logger.exception("Q&A 응답 생성 실패")
            results["errors"].append(f"Q&A 오류: {e}")

        return results

    async def _already_commented(self, room_id: str, target_date: date) -> bool:
        """[품질담당자] 1일 1댓글 보장 체크"""
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT c.comment_id
                   FROM agent_comments c
                   JOIN daily_conversation_snapshots s ON s.snapshot_id = c.snapshot_id
                   WHERE s.room_id = $1 AND c.comment_date = $2
                     AND c.comment_type = 'daily_summary'
                     AND c.approval_status != 'rejected'""",
                room_id, target_date
            )
            return row is not None

    async def _load_context(self, room_id: str, target_date: date) -> Optional[DailyCommentContext]:
        """
        [O-01 FIX] processing_status='done'인 스냅샷만 처리.
        'done'은 analyses 저장 완료 후에만 설정되므로 원자성 보장.
        """
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT s.snapshot_id, s.total_messages, s.unique_users,
                          s.question_count, s.top_topics,
                          AVG(a.sentiment_score) as avg_sentiment
                   FROM daily_conversation_snapshots s
                   LEFT JOIN message_analyses a ON a.snapshot_id = s.snapshot_id
                   WHERE s.room_id = $1 AND s.snapshot_date = $2
                     AND s.processing_status = 'done'
                   GROUP BY s.snapshot_id""",
                room_id, target_date
            )
            
            if not row:
                return None

            return DailyCommentContext(
                snapshot_id=str(row["snapshot_id"]),
                target_date=target_date,
                room_id=room_id,
                total_messages=row["total_messages"] or 0,
                unique_users=row["unique_users"] or 0,
                top_topics=row["top_topics"] or [],
                question_count=row["question_count"] or 0,
                avg_sentiment=float(row["avg_sentiment"] or 0),
                technical_questions=[],
                popular_topics=row["top_topics"] or [],
                notable_discussions=[],
            )

    async def _save_comment(
        self, snapshot_id: str, comment_date: date, comment: GeneratedComment
    ) -> None:
        """[Quality] 품질 통과 여부에 따라 자동/수동 승인 구분"""
        approval_status = "auto_approved" if comment.passed_quality_check else "pending_review"
        
        async with self.db.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_comments
                   (snapshot_id, comment_date, comment_type, generated_comment,
                    comment_metadata, approval_status)
                   VALUES ($1, $2, 'daily_summary', $3, $4, $5)
                   ON CONFLICT (comment_date, snapshot_id) DO NOTHING""",
                snapshot_id, comment_date, comment.content,
                json.dumps({
                    "tone": comment.estimated_tone,
                    "word_count": comment.word_count,
                    "quality_issues": comment.quality_issues,
                }),
                approval_status,
            )

    async def _load_unanswered_questions(self, snapshot_id: str, limit: int = 5) -> list[dict]:
        """urgency_score DESC 순으로 미응답 질문 조회"""
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                """SELECT user_id, question_type, topic_tags, context_summary, urgency_score
                   FROM message_analyses
                   WHERE snapshot_id = $1
                     AND needs_agent_response = TRUE
                     AND question_type != 'off_topic'
                   ORDER BY urgency_score DESC, complexity_score DESC
                   LIMIT $2""",
                snapshot_id, limit
            )
            return [dict(r) for r in rows]

    async def _get_users_proficiency_batch(self, user_ids: list[str]) -> dict[str, str]:
        """[PROD-02 FIX] 다수 사용자 숙련도 단일 IN절 조회 — N+1 제거"""
        if not user_ids:
            return {}
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, proficiency_level FROM user_profiles"
                " WHERE user_id = ANY($1)",
                user_ids,
            )
        return {r["user_id"]: r["proficiency_level"] for r in rows}

    async def _get_user_proficiency(self, user_id: str) -> str:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT proficiency_level FROM user_profiles WHERE user_id = $1",
                user_id
            )
            return row["proficiency_level"] if row else "intermediate"
