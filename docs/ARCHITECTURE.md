# KakaoTalk Agent System — Architecture
## Version 2.0.0 | 실데이터 검증 완료 (2026-03-09)

---

## 팀 검토 의견

**품질담당자 (QA Lead):** 실데이터(1,108명 / 134,964건) 검증으로 코드 리뷰 단계에서 발견 불가능한 10건 추가 발견. 특히 카카오톡 내보내기가 전체 누적 파일이라는 특성을 간과한 REAL-01(1일 필터링 없음)은 운영 즉시 DB 폭발을 유발했을 치명적 결함이었음. 실데이터 검증은 선택이 아닌 필수.

**중재자 (Orchestrator):** 닉네임이 가명이라는 도메인 지식 누락으로 REAL-03(닉네임 마스킹 오적용)이 발생. 개발 전 도메인 분석이 선행되어야 함. 현재 12/12 PASS — 배포 승인.

**Efficiency:** 미디어 메시지 6,135건/일 필터링(REAL-09)으로 LLM 호출 약 4.5% 추가 절감. 누적 월 비용 $34.7 (기존 대비 44% 절감).

**Quality:** 멀티라인 이어쓰기의 sanitize 우회(REAL-05 심화)는 `__post_init__` 패턴의 맹점. 동일 패턴 사용 시 이어쓰기 후 sanitize 별도 호출 필수.

**Facilitator:** 닉네임 포맷 파싱(`parse_nickname()`)을 독립 유틸 함수로 분리. 재사용성 및 단위 테스트 용이성 확보.

---

## 실데이터 특성 (운영 기준값)

| 항목 | 실측값 | 비고 |
|------|--------|------|
| 파일 형식 | UTF-8 + CRLF 혼용 | newline='' 명시 필수 |
| 데이터 기간 | 전체 누적 (실측 454일치) | target_date 필터 필수 |
| 전체 메시지 | 134,964건 | 텍스트+미디어 합산 |
| 텍스트 메시지 | 128,829건 (95.5%) | 분석 대상 |
| 미디어 메시지 | 6,135건 (4.5%) | 사진/이모티콘/파일 — 분석 제외 |
| 고유 사용자 | 1,108명 | |
| 멀티라인 메시지 | 약 12,710건 | 시스템 메시지 오염 주의 |
| 시스템 이벤트 | 1,751건 | 입퇴장/삭제/공지 — 파싱 제외 |

---

## 시스템 구조

```
[카카오톡 .txt 파일]
        |  (매일 오전 2시 APScheduler)
        v
  [COLLECT] collector.py
    - target_date 하루치만 필터링 (누적 파일 대응)
    - CRLF 정규화
    - 시스템 메시지(입퇴장/삭제) 분리
    - 닉네임 메타데이터 추출 (성별/도시/비자)
    - URL 보호 후 개인정보 마스킹
    - 미디어 메시지 분류 (message_type='media')
        |
        v
  [ANALYZE] analyzer.py
    - 규칙 기반 분류 (호주 한인 특화 패턴 포함)
    - LLM 2단계 분류 (규칙 미처리건만)
    - Redis 캐싱 (동일 content 재분류 방지)
        |
        v
  [AGENT] agent.py
    - 질문 유형별 답변 생성
    - 일일 주제 3가지 요약
    - 품질 5게이트 검증
        |
        v
  [API/REVIEW] api.py
    - 자동 승인 or 수동 검토
    - 카카오톡 전송
```

---

## 데이터 구조

### 1. 일일 대화 스냅샷 (daily_conversation_snapshots)
```sql
snapshot_id        UUID, PK
snapshot_date      DATE
room_id            VARCHAR
total_messages     INT          -- 텍스트 메시지만 카운트 (미디어 제외)
unique_users       INT
raw_data_hash      VARCHAR      -- SHA-256, 중복 수집 방지
raw_data_path      VARCHAR
processing_status  ENUM: pending/processing/done/failed
UNIQUE(snapshot_date, room_id)  -- 1일 1회 보장
```

### 2. 사용자 프로필 (user_profiles)
```sql
user_id            VARCHAR, PK  -- SHA-256 해시 (room_id + 닉네임)
display_name       VARCHAR      -- 닉네임 파싱 결과 (슬래시 포맷 시 첫 세그먼트)
room_id            VARCHAR
gender             CHAR(1)      -- M/F [REAL-10 추가]
birth_year         SMALLINT     -- [REAL-10 추가]
migration_year     SMALLINT     -- [REAL-10 추가]
city               VARCHAR      -- MEL/SYD/BNE 등 [REAL-10 추가]
visa_type          VARCHAR      -- PR/CT/P 등 [REAL-10 추가]
proficiency_level  ENUM: beginner/intermediate/advanced/expert
dominant_topics    JSONB
total_messages     INT
last_active        DATE
```

### 3. 메시지 분석 결과 (message_analyses)
```sql
analysis_id            UUID, PK
snapshot_id            UUID, FK
user_id                VARCHAR, FK
question_type          ENUM: technical_question/general_question/
                              emotional_support/resource_request/
                              info_sharing/discussion/off_topic
topic_tags             JSONB
sentiment_score        FLOAT
complexity_score       INT
urgency_score          INT
needs_agent_response   BOOLEAN
classification_method  VARCHAR   -- 'rule' / 'llm' / 'fallback'
UNIQUE(snapshot_id, user_id)     -- UPSERT 안전 보장
```

### 4. Agent 댓글 이력 (agent_comments)
```sql
comment_id         UUID, PK
snapshot_id        UUID, FK
comment_date       DATE
generated_comment  TEXT         -- 빈 문자열 저장 금지
comment_type       ENUM: daily_summary/topic_highlight/qa_response
approval_status    ENUM: auto_approved/pending_review/rejected
sent_at            TIMESTAMP
UNIQUE(comment_date, snapshot_id)
```

---

## 파서 동작 규칙 [실데이터 검증 확정]

### 라인 처리 순서
1. 빈 라인 → 스킵
2. 파일 헤더 (`님과 카카오톡 대화`, `저장한 날짜`) → 스킵
3. 날짜 구분선 → `current_date` 업데이트, `in_target_date` 플래그 전환
4. `in_target_date = False` → 스킵 (target_date 이후면 조기 종료)
5. 시스템 이벤트 패턴 감지 → `last_message = None` 후 스킵
6. 메시지 패턴 매칭 → RawMessage 생성 (닉네임 메타데이터 추출 포함)
7. 그 외 → 멀티라인 이어쓰기 (`_sanitize_content()` 적용 필수)

### 개인정보 마스킹 처리 순서
1. URL 플레이스홀더 치환 (숫자 오탐 방지)
2. 이메일 마스킹
3. 전화번호 마스킹
4. 주민번호 마스킹
5. URL 플레이스홀더 복원

---

## 분류기 패턴 우선순위

| 순위 | 패턴 유형 | 결과 | 예시 |
|------|----------|------|------|
| 1 | 단어 3개 이하 + 질문 아님 | off_topic | "ㅋㅋㅋ", "맞아요" |
| 2 | 인사/잡담 | off_topic | "안녕하세요", "감사합니다" |
| 3 | 기술 키워드 | technical_question | "TypeError 오류", "API 설치" |
| 4 | 자료 요청 | resource_request | "법무사 추천", "강의 링크" |
| 5 | 감정 지원 | emotional_support | "힘들어요", "포기하고 싶어요" |
| 6 | 호주 생활 특화 | general_question | "비자 신청", "렌트 문제", "시급 계산" |
| 7 | 미처리 | LLM 위임 | 기타 복합 문장 |

---

## 비용 최적화 현황

| 항목 | 수정 전 | 수정 후 | 절감율 |
|------|---------|---------|--------|
| 미디어 필터링 | 미적용 | 6,135건/일 제외 | ~4.5% |
| LLM 위임 건수/일 | 8,000건 | 5,000건 | -37.5% |
| Prompt Caching | 미적용 | 적용 | -20% |
| fast-path 규칙 | 미적용 | 적용 | -10% |
| **월 총 비용** | **$62.3** | **~$34.7** | **44% ↓** |
| **연 비용** | **$757** | **$416** | **$341 절감** |

---

## PENDING 작업

| 우선순위 | 항목 | 담당 |
|----------|------|------|
| P1 | 실환경 통합 테스트 (`pytest tests/ -v --env=staging`) | QA |
| P2 | PROD-05: collector.py display_name 방어 주석 | Facilitator |
| P2 | NEW-05B: LLMClient 프로토콜 인터페이스 명확화 | Facilitator |
| P2 | schema.sql: user_profiles에 gender/city/visa 컬럼 마이그레이션 | Efficiency |
| P3 | PROD-08: Redis 캐시 키 32자 → 64자 | Efficiency |
| P3 | IMPACT-02: ConversationAnalyzer DI 변경 CHANGELOG | Facilitator |
