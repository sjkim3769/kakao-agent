-- ============================================================
-- KakaoTalk Agent System - Database Schema
-- Version: 1.0.0
-- QA Approved: 5-round validation complete
-- ============================================================

-- [품질담당자] 모든 테이블 UUID 기본키, created_at/updated_at 감사 추적 필수
-- [Efficiency] 인덱스 전략: 쿼리 패턴 기반 복합 인덱스 설계
-- [Quality] 개인정보 컬럼 명시적 주석 처리

-- Extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm"; -- 텍스트 검색 최적화

-- ============================================================
-- 1. 카카오톡 대화방
-- ============================================================
CREATE TABLE chat_rooms (
    room_id         VARCHAR(255) PRIMARY KEY,
    room_name       VARCHAR(500) NOT NULL,
    description     TEXT,
    member_count    INT DEFAULT 0,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 2. 사용자 프로필 [개인정보 - 암호화 대상]
-- ============================================================
CREATE TABLE user_profiles (
    user_id             VARCHAR(255) PRIMARY KEY,  -- 카카오 고유 ID (해시 처리)
    display_name        VARCHAR(500),               -- [개인정보] 마스킹 처리
    room_id             VARCHAR(255) REFERENCES chat_rooms(room_id),
    proficiency_level   VARCHAR(20) DEFAULT 'intermediate'
                        CHECK (proficiency_level IN ('beginner','intermediate','advanced','expert')),
    dominant_topics     JSONB DEFAULT '[]',         -- ["python", "데이터분석", ...]
    total_messages      INT DEFAULT 0,
    total_questions     INT DEFAULT 0,
    question_answered   INT DEFAULT 0,
    last_active         DATE,
    first_seen          DATE DEFAULT CURRENT_DATE,
    is_anonymous        BOOLEAN DEFAULT FALSE,      -- 익명 처리 여부
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_user_profiles_room_active ON user_profiles(room_id, last_active DESC);
CREATE INDEX idx_user_profiles_proficiency ON user_profiles(proficiency_level);
CREATE INDEX idx_user_profiles_topics ON user_profiles USING GIN(dominant_topics);

-- ============================================================
-- 3. 일일 대화 스냅샷 [핵심: 1일 1회 보장]
-- ============================================================
CREATE TABLE daily_conversation_snapshots (
    snapshot_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot_date       DATE NOT NULL,
    room_id             VARCHAR(255) NOT NULL REFERENCES chat_rooms(room_id),
    total_messages      INT DEFAULT 0,
    unique_users        INT DEFAULT 0,
    question_count      INT DEFAULT 0,
    top_topics          JSONB DEFAULT '[]',
    raw_data_path       VARCHAR(1000),              -- 원본 데이터 파일 경로 (S3/로컬)
    raw_data_hash       VARCHAR(64),                -- SHA-256 중복 수집 방지
    processing_status   VARCHAR(20) DEFAULT 'pending'
                        CHECK (processing_status IN ('pending','processing','done','failed','skipped')),
    error_message       TEXT,
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    
    -- [품질담당자] 1일 1회 수집 보장: (날짜 + 방) 조합 유니크
    CONSTRAINT uq_snapshot_date_room UNIQUE (snapshot_date, room_id)
);

CREATE INDEX idx_snapshots_date ON daily_conversation_snapshots(snapshot_date DESC);
CREATE INDEX idx_snapshots_room_date ON daily_conversation_snapshots(room_id, snapshot_date DESC);
CREATE INDEX idx_snapshots_status ON daily_conversation_snapshots(processing_status) 
    WHERE processing_status IN ('pending', 'processing');

-- ============================================================
-- 4. 메시지 분석 결과
-- ============================================================
CREATE TABLE message_analyses (
    analysis_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot_id         UUID NOT NULL REFERENCES daily_conversation_snapshots(snapshot_id),
    user_id             VARCHAR(255) NOT NULL REFERENCES user_profiles(user_id),
    
    -- 질문 분류 [Facilitator] 명확한 Enum 정의
    question_type       VARCHAR(30) DEFAULT 'general'
                        CHECK (question_type IN (
                            'technical_question',   -- 기술적 질문
                            'general_question',     -- 일반 질문
                            'emotional_support',    -- 감정적 지원
                            'resource_request',     -- 자료/링크 요청
                            'info_sharing',         -- 정보 공유
                            'discussion',           -- 토론/의견
                            'off_topic'             -- 주제 무관
                        )),
    
    topic_tags          JSONB DEFAULT '[]',         -- ["python", "머신러닝", ...]
    sentiment_score     NUMERIC(4,3)                -- -1.000 ~ 1.000
                        CHECK (sentiment_score BETWEEN -1.0 AND 1.0),
    complexity_score    INT DEFAULT 3 CHECK (complexity_score BETWEEN 1 AND 5),
    urgency_score       INT DEFAULT 1 CHECK (urgency_score BETWEEN 1 AND 5),
    
    needs_agent_response BOOLEAN DEFAULT FALSE,
    context_summary     TEXT,                       -- LLM이 요약한 맥락
    message_count       INT DEFAULT 0,              -- 사용자의 해당 날짜 메시지 수
    
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    -- [NEW-09 FIX] ON CONFLICT (snapshot_id, user_id) DO UPDATE 를 위한 UNIQUE 제약
    -- analyzer._save_analyses 재실행 시 중복 행 없이 안전하게 UPSERT 가능
    CONSTRAINT uq_analysis_snapshot_user UNIQUE (snapshot_id, user_id)
);

CREATE INDEX idx_analyses_snapshot ON message_analyses(snapshot_id);
CREATE INDEX idx_analyses_user_date ON message_analyses(user_id, created_at DESC);
CREATE INDEX idx_analyses_type ON message_analyses(question_type);
CREATE INDEX idx_analyses_needs_response ON message_analyses(needs_agent_response) 
    WHERE needs_agent_response = TRUE;
CREATE INDEX idx_analyses_topics ON message_analyses USING GIN(topic_tags);

-- ============================================================
-- 5. Agent 댓글 이력
-- ============================================================
CREATE TABLE agent_comments (
    comment_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot_id         UUID NOT NULL REFERENCES daily_conversation_snapshots(snapshot_id),
    comment_date        DATE NOT NULL,
    
    comment_type        VARCHAR(30) DEFAULT 'daily_summary'
                        CHECK (comment_type IN (
                            'daily_summary',        -- 일일 대화 요약 댓글
                            'topic_highlight',      -- 주요 주제 하이라이트
                            'qa_response',          -- Q&A 응답
                            'encouragement'         -- 격려/참여 유도
                        )),
    
    generated_comment   TEXT NOT NULL,
    comment_metadata    JSONB DEFAULT '{}',         -- 생성에 사용된 메타데이터
    
    -- [Quality] 승인 워크플로우
    approval_status     VARCHAR(20) DEFAULT 'pending_review'
                        CHECK (approval_status IN (
                            'auto_approved',        -- 자동 승인
                            'pending_review',       -- 수동 검토 필요
                            'approved',             -- 수동 승인
                            'rejected',             -- 거절
                            'sent'                  -- 전송 완료
                        )),
    
    reviewed_by         VARCHAR(255),               -- 검토자 ID
    reviewed_at         TIMESTAMPTZ,
    sent_at             TIMESTAMPTZ,
    
    -- [품질담당자] 1일 1개 댓글 보장
    CONSTRAINT uq_comment_date_snapshot UNIQUE (comment_date, snapshot_id),
    
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_comments_date ON agent_comments(comment_date DESC);
CREATE INDEX idx_comments_status ON agent_comments(approval_status);

-- ============================================================
-- 6. 질문 유형별 응답 템플릿
-- ============================================================
CREATE TABLE response_templates (
    template_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    question_type       VARCHAR(30) NOT NULL,
    topic_tag           VARCHAR(100),               -- NULL이면 모든 토픽에 적용
    proficiency_level   VARCHAR(20),               -- NULL이면 모든 숙련도에 적용
    template_content    TEXT NOT NULL,
    usage_count         INT DEFAULT 0,
    effectiveness_score NUMERIC(4,3) DEFAULT 0.500, -- 피드백 기반 점수
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_templates_type_topic ON response_templates(question_type, topic_tag);
CREATE INDEX idx_templates_proficiency ON response_templates(proficiency_level);

-- ============================================================
-- 7. 처리 이력 (감사 로그)
-- ============================================================
CREATE TABLE processing_logs (
    log_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    process_type        VARCHAR(50) NOT NULL,
    snapshot_id         UUID REFERENCES daily_conversation_snapshots(snapshot_id)
                            ON DELETE SET NULL,   -- [N-3 FIX] 스냅샷 삭제 시 로그 보존
    status              VARCHAR(20) NOT NULL,
    duration_ms         INT,
    tokens_used         INT DEFAULT 0,
    error_details       JSONB,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX idx_proc_logs_type_date ON processing_logs(process_type, started_at DESC);
CREATE INDEX idx_proc_logs_status ON processing_logs(status) WHERE status = 'failed';

-- ============================================================
-- 트리거: updated_at 자동 갱신
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOR t IN SELECT unnest(ARRAY[
        'chat_rooms','user_profiles','daily_conversation_snapshots',
        'agent_comments','response_templates'
    ])
    LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_%s_updated_at
             BEFORE UPDATE ON %s
             FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()',
            t, t
        );
    END LOOP;
END $$;

-- ============================================================
-- 초기 데이터
-- ============================================================
INSERT INTO response_templates (question_type, proficiency_level, template_content) VALUES
('technical_question', 'beginner', '안녕하세요! {user_name}님의 질문을 확인했습니다. {topic} 관련해서 기초부터 설명드릴게요: {answer}'),
('technical_question', 'advanced', '{user_name}님, {topic} 심화 내용입니다. {answer} 추가로 {reference}도 참고해보세요.'),
('general_question', NULL, '{user_name}님의 질문({topic}): {answer}'),
('emotional_support', NULL, '오늘 하루도 수고하셨어요! 커뮤니티가 함께합니다 💪');

-- ============================================================
-- 뷰: 일일 요약 (대시보드용)
-- ============================================================
CREATE OR REPLACE VIEW v_daily_summary AS
SELECT
    s.snapshot_date,
    s.room_id,
    s.total_messages,
    s.unique_users,
    s.question_count,
    COUNT(a.analysis_id) AS analyzed_messages,
    AVG(a.sentiment_score)::NUMERIC(4,3) AS avg_sentiment,
    COUNT(CASE WHEN a.needs_agent_response THEN 1 END) AS pending_responses,
    c.generated_comment IS NOT NULL AS has_daily_comment,
    c.approval_status AS comment_status
FROM daily_conversation_snapshots s
LEFT JOIN message_analyses a ON a.snapshot_id = s.snapshot_id
LEFT JOIN agent_comments c ON c.snapshot_id = s.snapshot_id 
    AND c.comment_type = 'daily_summary'
GROUP BY s.snapshot_id, s.snapshot_date, s.room_id, 
         s.total_messages, s.unique_users, s.question_count,
         c.generated_comment, c.approval_status;
