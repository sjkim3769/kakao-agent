# KakaoTalk Agent System — 통합 설계서

> Claude Code 구현 시 참조할 계획서  
> 최종 업데이트: 2026-03-09 | 실데이터 검증(1,108명 / 134,964건) 완료

---

## 1. 작업 컨텍스트

### 배경 및 목적
호주 영주권자·시민권자 1,000명+ 카카오톡 단톡방의 대화를 1일 1회 자동 수집·분석하여, 질문 유형별 맥락 기반 답변과 당일 주제 요약을 제공하는 에이전트 시스템.

### 범위
- 수집: 카카오톡 내보내기 TXT 파일 → 1일치 파싱
- 분석: 질문 유형 분류 (규칙 + LLM 2단계)
- 생성: 유형별 답변 댓글 + 일일 주제 3가지 요약
- 검토: 자동 승인 or 수동 검토 후 전송

### 입출력 정의
| 구분 | 내용 |
|------|------|
| 입력 | 카카오톡 TXT 내보내기 파일 (UTF-8 CRLF, 전체 누적) |
| 출력 | 일일 댓글 (주제 요약 3개 + 질문 답변), DB 저장 분석 결과 |

### 제약조건
- 실행 환경: VS Code, GitHub
- 기술 스택: Python (핵심), Node.js (API 선택), PostgreSQL, Redis
- 1일 1회 배치 처리 (오전 2시 APScheduler)
- 카카오톡 내보내기 파일은 전체 누적 파일 → target_date 필터 필수

### 용어 정의
| 용어 | 정의 |
|------|------|
| 스냅샷 | 특정 날짜 하루치 대화 데이터 집합 |
| 닉네임 포맷 | `닉네임/성별/출생연도/이민연도/도시/비자` (예: `위스킹/M/81/08/MEL/CT`) |
| 미디어 메시지 | '사진', '동영상', '이모티콘', '파일', '스티커' — 분석 제외 |
| fast-path | 규칙으로 즉시 분류 가능한 메시지 (LLM 미사용) |

---

## 2. 워크플로우 정의

### 전체 흐름도

```
[카카오톡 .txt 파일]
        |
        v
  STEP 1: COLLECT (스크립트)
    - target_date 하루치 필터링
    - CRLF 정규화 / 시스템 메시지 분리
    - 닉네임 메타데이터 추출
    - 개인정보 마스킹 (URL 선보호 후)
    - 미디어 메시지 분류
        |
        v
  STEP 2: ANALYZE (스크립트 + LLM)
    - 규칙 기반 분류 (fast-path ~70%)
    - LLM 분류 (나머지 ~30%)
    - Redis 캐싱 (중복 분류 방지)
        |
        v
  STEP 3: GENERATE (LLM)
    - 질문 유형별 맥락 이해 및 답변
    - 당일 주제 3가지 요약
    - 품질 5게이트 검증
        |
        v
  STEP 4: REVIEW (사람 or 자동)
    - 자동 승인 (품질 기준 충족 시)
    - 수동 검토 (미달 시 pending_review)
    - 카카오톡 전송
```

### LLM 판단 영역 vs 코드 처리 영역

| LLM이 직접 수행 | 스크립트로 처리 |
|----------------|----------------|
| 질문 유형 분류 (규칙 미처리건) | 파일 파싱, 날짜 필터링 |
| 맥락 기반 답변 생성 | 개인정보 마스킹 |
| 당일 주제 3가지 요약 | DB CRUD, 캐시 관리 |
| 댓글 품질 자기 검증 | 스케줄링, 로깅 |

### 단계별 성공 기준 및 검증

#### STEP 1: COLLECT
- **성공 기준**: target_date 해당일 메시지만 추출, 개인정보 0건 노출, content_hash 생성
- **검증 방법**: 스키마 검증 (필수 필드), 규칙 기반 (이메일·전화번호 패턴 잔존 여부)
- **실패 시**: 자동 재시도 1회 → status='failed' 기록 + 알림

#### STEP 2: ANALYZE
- **성공 기준**: 전체 텍스트 메시지 100% 분류 완료, fallback 사용 시 method='fallback' 표기
- **검증 방법**: 스키마 검증 (question_type 필드), 규칙 기반 (분류 누락 건수 0)
- **실패 시**: LLM 오류 → fallback_result() 적용 후 스킵 + 로그

#### STEP 3: GENERATE
- **성공 기준**: 댓글 50~500자, 금지어 없음, 빈 문자열 금지, 주제 요약 정확히 3개
- **검증 방법**: 규칙 기반 (길이·금지어), LLM 자기 검증 (톤·누락 여부)
- **실패 시**: 자동 재시도 2회 → approval_status='pending_review' + placeholder 삽입

#### STEP 4: REVIEW
- **성공 기준**: approval_status 확정, 전송 완료 기록
- **검증 방법**: 사람 검토 (pending_review 건), 스키마 검증 (sent_at 필드)
- **실패 시**: 에스컬레이션 (관리자 알림)

---

## 3. 구현 스펙

### 폴더 구조

```
/kakao-agent
  ├── CLAUDE.md                          # 메인 에이전트 지침 (이 파일)
  ├── .claude/
  │   ├── skills/
  │   │   ├── kakao-parser/
  │   │   │   ├── SKILL.md
  │   │   │   ├── scripts/               # collector.py
  │   │   │   └── references/            # 닉네임 포맷, 파일 형식 가이드
  │   │   ├── message-classifier/
  │   │   │   ├── SKILL.md
  │   │   │   └── scripts/               # analyzer.py, rule patterns
  │   │   ├── privacy-masker/
  │   │   │   ├── SKILL.md
  │   │   │   └── scripts/               # sanitize 유틸
  │   │   └── comment-quality-checker/
  │   │       ├── SKILL.md
  │   │       └── scripts/               # 5게이트 검증 로직
  │   └── agents/
  │       ├── collector-agent/
  │       │   └── AGENT.md
  │       ├── analyzer-agent/
  │       │   └── AGENT.md
  │       └── comment-agent/
  │           └── AGENT.md
  ├── src/
  │   ├── collector/collector.py
  │   ├── analyzer/analyzer.py
  │   ├── agent/agent.py
  │   ├── api/api.py
  │   ├── scheduler/scheduler.py
  │   └── db/schema.sql
  ├── tests/test_all.py
  ├── config/settings.py
  ├── output/                            # 중간 산출물
  └── docs/
      ├── ARCHITECTURE.md
      └── ORCHESTRATOR_CHECKLIST.md
```

### CLAUDE.md 핵심 섹션 목록
1. 작업 컨텍스트 (이 문서)
2. 워크플로우 정의
3. 구현 스펙 (폴더·스킬·에이전트)
4. 카카오톡 파일 파싱 규칙 (도메인 지식)
5. 개인정보 처리 원칙
6. 배포 판정 기준

### 에이전트 구조 (서브에이전트 분리)

메인 CLAUDE.md가 오케스트레이터. 서브에이전트 간 직접 호출 금지.

```
CLAUDE.md (Orchestrator)
  ├── collector-agent   ← STEP 1 전담
  ├── analyzer-agent    ← STEP 2 전담
  └── comment-agent     ← STEP 3 전담
```

**분리 이유**: 각 단계가 독립적인 도메인 지식을 필요로 하며 (파싱 규칙 / 분류 패턴 / 자연어 생성), 컨텍스트 윈도우 최적화를 위해 필요한 시점에만 해당 지침 로드.

### 스킬 목록

| 스킬명 | 역할 | 트리거 조건 |
|--------|------|------------|
| `kakao-parser` | TXT 파일 → RawMessage 변환 | collector-agent 실행 시 |
| `message-classifier` | 규칙 기반 질문 유형 분류 | analyzer-agent 실행 시 |
| `privacy-masker` | 전화번호·이메일·주민번호 마스킹 | 파싱 직후 항상 |
| `comment-quality-checker` | 댓글 5게이트 품질 검증 | comment-agent 생성 직후 |

### 서브에이전트 상세

#### collector-agent
- **역할**: 카카오톡 파일 파싱, 1일치 필터링, DB 저장
- **입력**: `{room_id, target_date, file_path}`
- **출력**: `/output/step1_snapshot.json` (snapshot_id, 메시지 수, 사용자 수)
- **참조 스킬**: `kakao-parser`, `privacy-masker`
- **데이터 전달**: 파일 기반 (`/output/step1_snapshot.json`)

#### analyzer-agent
- **역할**: 메시지 질문 유형 분류, DB 저장
- **입력**: `/output/step1_snapshot.json`
- **출력**: `/output/step2_analyses.json` (분류 결과, 통계)
- **참조 스킬**: `message-classifier`
- **데이터 전달**: 파일 기반 (`/output/step2_analyses.json`)

#### comment-agent
- **역할**: 일일 댓글 생성, 품질 검증, 승인 상태 설정
- **입력**: `/output/step2_analyses.json`
- **출력**: `/output/step3_comment.json` (댓글 내용, approval_status)
- **참조 스킬**: `comment-quality-checker`
- **데이터 전달**: 파일 기반 (`/output/step3_comment.json`)

### 주요 산출물 파일 형식

```json
// /output/step1_snapshot.json
{
  "snapshot_id": "uuid",
  "snapshot_date": "2026-03-09",
  "room_id": "room_test",
  "total_messages": 128829,
  "unique_users": 1108,
  "media_excluded": 6135
}

// /output/step2_analyses.json
{
  "snapshot_id": "uuid",
  "total_classified": 128829,
  "by_type": {"general_question": 450, "off_topic": 95000, ...},
  "needs_response": [{"user_id": "u_xxx", "question_type": "...", "content": "..."}]
}

// /output/step3_comment.json
{
  "snapshot_id": "uuid",
  "comment": "오늘 대화 주제 요약...",
  "approval_status": "auto_approved",
  "topics": ["비자/이민", "생활정보", "음식/맛집"]
}
```

---

## 4. 카카오톡 파일 파싱 규칙 [실데이터 검증 확정]

### 파일 특성
- UTF-8 with CRLF 혼용 → `newline=''` 명시 필수
- 전체 누적 파일 (실측 454일치) → `target_date` 필터 필수
- 닉네임 포맷: `닉네임/성별/출생연도/이민연도/도시/비자`
- 닉네임은 가명 → **추가 마스킹 불필요**

### 라인 처리 순서
1. 빈 라인 → 스킵
2. 파일 헤더 (`님과 카카오톡 대화`, `저장한 날짜`) → 스킵
3. 날짜 구분선 → current_date 업데이트 + in_target_date 플래그 전환
4. `in_target_date = False` → 스킵 (target_date 이후 → 조기 종료)
5. 시스템 이벤트 (`님이 들어왔습니다` 등) → `last_message = None` 후 스킵
6. 메시지 패턴 → RawMessage 생성
7. 그 외 → 멀티라인 이어쓰기 (`_sanitize_content()` 적용 필수)

---

## 5. 개인정보 처리 원칙

| 항목 | 처리 방식 |
|------|----------|
| 닉네임 | 원본 보존 (가명) |
| 전화번호 | `[전화번호]` 치환 — URL 선보호 후 마스킹 |
| 이메일 | `[이메일]` 치환 — 멀티라인 이어쓰기 시에도 적용 |
| 주민번호 | `[주민번호]` 치환 |
| user_id | `u_` + SHA-256(room_id + 닉네임) 16자 |

**주의**: `content +=` 이어쓰기 후 반드시 `_sanitize_content()` 재호출.

---

## 6. 환경 설정

```env
DATABASE_URL=postgresql://user:pass@localhost/kakao_agent
GROQ_API_KEY=gsk_...
JWT_SECRET_KEY=<random-256bit>
REDIS_URL=redis://localhost:6379
CORS_ALLOWED_ORIGINS=https://your-domain.com
LLM_MODEL=llama-3.3-70b-versatile
```

---

## 7. 배포 판정 기준

| 조건 | 판정 |
|------|------|
| CRITICAL 0건 + 실데이터 테스트 PASS + 평균 ≥ 75점 | ✅ APPROVE |
| CRITICAL 0건 + 평균 < 75점 | ⚠️ CONDITIONAL |
| CRITICAL 1건 이상 | ❌ REJECT |

현재 상태: **✅ APPROVE** (실데이터 검증 12/12 PASS, 2026-03-09)
