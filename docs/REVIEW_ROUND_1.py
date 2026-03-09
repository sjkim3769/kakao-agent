"""
============================================================
4인 순차 독립 검토 프로토콜
============================================================

구조:
  1라운드: Facilitator   → 가독성/구조/미구현 탐지
  2라운드: Efficiency    → Facilitator 결과 보고 반박+성능 검토
  3라운드: Quality       → 앞 두 검토자 결과 보고 반박+보안/UX 검토
  4라운드: 품질담당자    → 전체 결과 보고 최종 감사 + 반박
  5라운드: 중재자        → 4명 의견 충돌 시 캐스팅보트 + 배포 최종 결정

규칙:
  - 각 검토자는 이전 결과를 읽은 뒤 "동의 / 반박 / 추가 발견" 명시
  - 한 명이라도 BLOCK 선언 시 중재자가 최종 판단
  - 모든 BLOCK은 수정 코드 없이 승인 불가
"""

# ============================================================
# 현재 코드베이스 기준 4인 순차 검토 실행
# ============================================================


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 1 — Facilitator (코드 구조·가독성·미구현 탐지 전담)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUND_1_FACILITATOR = {
    "reviewer": "Facilitator",
    "focus": "코드 가독성 / 구조 / 미구현 stub / import 정리",
    "verdict": "BLOCK",
    "findings": [
        {
            "id": "F-01",
            "severity": "BLOCK",
            "file": "analyzer.py",
            "line": 474,
            "issue": "함수 내부에서 lazy import 사용 (from pathlib import Path, from src.collector...)",
            "detail": (
                "_load_snapshot_messages() 안에 import가 박혀있다. "
                "이건 매 호출마다 모듈 탐색 비용 발생이고, "
                "순환 참조(circular import) 위험 신호다. "
                "analyzer가 collector를 직접 import하는 것 자체가 "
                "레이어 의존성 위반이다."
            ),
            "fix_required": "최상단 import로 이동 + 의존성 역전 검토",
        },
        {
            "id": "F-02",
            "severity": "BLOCK",
            "file": "collector.py",
            "line": 150,
            "issue": "parse_file()에서 경로 검증 로직이 의미없음",
            "detail": (
                "resolved = file_path.resolve() 후 "
                "'if resolved != file_path.resolve()' 를 비교하는데 "
                "이는 항상 동일 객체 비교라 절대 True가 될 수 없다. "
                "즉 심볼릭 링크 체크가 완전히 무효화된 코드다."
            ),
            "fix_required": "심볼릭 링크 체크: file_path.resolve() != file_path 로 수정",
        },
        {
            "id": "F-03",
            "severity": "WARN",
            "file": "collector.py",
            "line": 196,
            "issue": "parse_file()이 snapshot_date를 date.today()로 고정",
            "detail": (
                "파일 내용에서 파싱된 날짜(current_date)가 있는데 "
                "return 시 snapshot_date=date.today()로 덮어쓴다. "
                "오전 2시 배치가 전날 파일을 처리할 때 날짜 불일치 발생."
            ),
            "fix_required": "파일에서 파싱된 마지막 날짜 또는 target_date를 파라미터로 전달",
        },
        {
            "id": "F-04",
            "severity": "WARN",
            "file": "scheduler.py",
            "line": "전체",
            "issue": "재시도 정책 선언만 있고 구현 없음",
            "detail": (
                "docstring에 '수집 실패 시 30분 후 1회 재시도'라고 명시되어 있으나 "
                "실제 재시도 로직이 코드 어디에도 없다."
            ),
            "fix_required": "tenacity 또는 APScheduler misfire_grace_time으로 재시도 구현",
        },
        {
            "id": "F-05",
            "severity": "INFO",
            "file": "agent.py",
            "line": 31,
            "issue": "asyncio import 누락",
            "detail": "agent.py에서 비동기 타임아웃이 필요할 수 있으나 asyncio import가 없다.",
            "fix_required": "import asyncio 추가 (선제적 조치)",
        },
    ],
    "summary": "F-01(순환참조 위험), F-02(심볼릭링크 체크 무효) 두 건은 즉시 BLOCK. 나머지는 WARN.",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 2 — Efficiency (Facilitator 결과 확인 후 독립 검토)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUND_2_EFFICIENCY = {
    "reviewer": "Efficiency",
    "focus": "성능 / 비용 / 중복연산 / DB 쿼리 최적화",
    "prior_review_read": "Facilitator의 F-01~F-05 확인함",
    "agreements": ["F-01에 동의: lazy import는 성능 문제도 있음"],
    "disputes": [],
    "verdict": "BLOCK",
    "findings": [
        {
            "id": "E-01",
            "severity": "BLOCK",
            "file": "analyzer.py",
            "line": "analyze_snapshot() 전체",
            "issue": "1,000명 × 다수 메시지 → 메모리 전량 로드 위험",
            "detail": (
                "messages_by_user에 모든 사용자의 모든 메시지를 dict로 전량 적재한 뒤 "
                "rule_results, llm_results, analyses 3개 dict를 동시에 유지한다. "
                "1,000명 × 평균 20메시지 = 20,000건이 메모리에 3중으로 존재. "
                "피크 메모리 사용량 예측 불가."
            ),
            "fix_required": "사용자 단위 청크 처리 또는 제너레이터 패턴 적용",
        },
        {
            "id": "E-02",
            "severity": "BLOCK",
            "file": "analyzer.py",
            "line": 417,
            "issue": "동일 메시지 중복 분류 가능성",
            "detail": (
                "rule_results와 llm_results의 key가 (user_id, msg_content) 튜플인데 "
                "서로 다른 사용자가 동일 메시지를 보낸 경우 "
                "LLM은 content 기준 캐싱이지만 key는 user_id를 포함해서 "
                "캐시 히트가 발생해도 llm_results에는 user_id별로 따로 저장된다. "
                "반면 rule_results key 충돌 시 앞 사용자 결과가 뒤 사용자를 덮어쓴다."
            ),
            "fix_required": "key를 content 기반으로 통일하거나 user별 독립 dict 사용",
        },
        {
            "id": "E-03",
            "severity": "WARN",
            "file": "analyzer.py",
            "line": 354,
            "issue": "캐시 저장 시 ClassificationResult.__dict__ 직렬화 불안정",
            "detail": (
                "dataclass의 __dict__는 중첩 객체나 custom type이 있을 때 "
                "json.dumps에서 TypeError 발생 가능. "
                "dataclasses.asdict()를 써야 안전하다."
            ),
            "fix_required": "json.dumps(dataclasses.asdict(result)) 로 변경",
        },
        {
            "id": "E-04",
            "severity": "WARN",
            "file": "collector.py",
            "line": 101,
            "issue": "content_hash 계산 시 전체 메시지 재직렬화",
            "detail": (
                "to_dict()가 _sanitize_content()를 포함하므로 "
                "메시지 20,000건에 대해 정규식 치환 × 3종을 hash 계산할 때마다 재실행. "
                "content_hash는 최초 1회만 계산되므로 큰 문제는 아니나 "
                "LRU 캐시 또는 sanitized 결과 저장으로 최적화 가능."
            ),
            "fix_required": "WARN 수준 — 즉시 필수 아님, 다음 버전 개선 권고",
        },
    ],
    "summary": "E-01(메모리 폭탄), E-02(key 충돌로 인한 분류 오염) BLOCK. 핵심 비즈니스 데이터 오염 가능성 있음.",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 3 — Quality (앞 두 검토자 결과 확인 후 독립 검토)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUND_3_QUALITY = {
    "reviewer": "Quality",
    "focus": "보안 / 개인정보 / 비즈니스 로직 정확성 / UX",
    "prior_review_read": "Facilitator F-01~F-05, Efficiency E-01~E-04 확인함",
    "agreements": [
        "F-02에 강하게 동의: 심볼릭 링크 체크 무효는 보안 결함",
        "F-03에 동의: 날짜 불일치는 비즈니스 로직 오류",
        "E-02에 동의: key 충돌로 사용자 분류 데이터 오염은 Quality 관점서도 심각",
    ],
    "disputes": [
        "E-01에 부분 반박: 1,000명 × 20메시지는 약 20MB 수준으로 "
        "현대 서버에서 즉각적 문제는 아님. 단, 장기적 확장성은 위험."
    ],
    "verdict": "BLOCK",
    "findings": [
        {
            "id": "Q-01",
            "severity": "BLOCK",
            "file": "collector.py",
            "line": "RawMessage.display_name",
            "issue": "원본 이름(display_name)이 메모리에 마스킹 전 상태로 유지",
            "detail": (
                "RawMessage.display_name = name (원본)으로 저장되고 "
                "to_dict()에서만 마스킹된다. "
                "로그 출력, 디버그 print, 예외 메시지에 "
                "원본 이름이 노출될 수 있다. "
                "특히 logger.info(f'파싱 완료: {len(messages)}개') 같은 곳에서 "
                "실수로 display_name을 포함시키면 로그에 개인정보가 남는다. "
                "GDPR/개인정보보호법 위반 리스크."
            ),
            "fix_required": (
                "RawMessage 생성 시점에 즉시 마스킹 적용. "
                "display_name_masked 필드로 분리하거나 "
                "원본은 저장하지 않는 구조로 변경."
            ),
        },
        {
            "id": "Q-02",
            "severity": "BLOCK",
            "file": "agent.py",
            "line": "CommentQualityChecker.check()",
            "issue": "품질 검사가 빈 문자열('')을 통과시킴",
            "detail": (
                "LLM이 빈 응답을 반환하면 len('') = 0 < 50이라 "
                "'댓글이 너무 짧음' 이슈를 추가하지만 "
                "passed = len(issues) == 0 이므로 passed=False가 맞다. "
                "그러나 2회 시도 후 마지막 comment_text가 '' 인 상태로 "
                "GeneratedComment(content='')가 DB에 저장될 수 있다. "
                "빈 댓글이 카카오톡에 전송되는 UX 재앙."
            ),
            "fix_required": (
                "2회 시도 실패 시 content='' 저장 금지. "
                "approval_status='pending_review'로 강제 설정하고 "
                "content에 '[생성실패 - 수동입력필요]' placeholder 삽입."
            ),
        },
        {
            "id": "Q-03",
            "severity": "BLOCK",
            "file": "api.py",
            "line": "전체",
            "issue": "Rate Limiting 없음",
            "detail": (
                "JWT 인증은 적용됐으나 Rate Limit이 없다. "
                "토큰 탈취 시 파이프라인 강제 실행 API를 무제한 호출 가능. "
                "1,000명 방에서 LLM 비용 폭탄 유발 가능."
            ),
            "fix_required": "slowapi 또는 Redis 기반 Rate Limiter 적용 (관리자 API: 10 req/min)",
        },
        {
            "id": "Q-04",
            "severity": "WARN",
            "file": "analyzer.py",
            "line": 417,
            "issue": "fallback 분류 결과가 DB에 아무 표시 없이 저장됨",
            "detail": (
                "LLM 실패로 fallback_result()가 사용됐을 때 "
                "method='fallback'이 ClassificationResult에 있지만 "
                "DB 저장 시 method 컬럼이 없어 신뢰도 낮은 데이터가 "
                "정상 데이터와 구분 불가. 추후 분석 오염."
            ),
            "fix_required": "message_analyses 테이블에 classification_method 컬럼 추가",
        },
    ],
    "summary": (
        "Q-01(개인정보 메모리 노출), Q-02(빈 댓글 전송 가능), Q-03(Rate Limit 부재) BLOCK. "
        "특히 Q-01은 법적 리스크."
    ),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 4 — 품질담당자 (전체 3인 결과 보고 최종 감사)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUND_4_QA_LEAD = {
    "reviewer": "품질담당자 (QA Lead, 20년 경력)",
    "focus": "상용화 수준 안정성 / 전체 결함 최종 확인 / 이전 검토자 검증",
    "prior_review_read": "Facilitator, Efficiency, Quality 전체 결과 확인함",
    "agreements": [
        "F-01(순환참조), F-02(symlink 체크 무효), F-03(날짜 덮어쓰기)",
        "E-01(메모리), E-02(key 충돌)",
        "Q-01(개인정보 메모리 노출) — 이건 내가 이전에 놓쳤다. Quality가 맞다.",
        "Q-02(빈 댓글 저장) — 이것도 내가 놓쳤다.",
        "Q-03(Rate Limiting) — 동의",
    ],
    "disputes": [
        "E-01에 대한 Quality의 반박(20MB는 괜찮다)에 재반박: "
        "메시지 급증일, 이미지/파일 메시지 포함 시 가정이 깨짐. BLOCK 유지.",
    ],
    "verdict": "BLOCK",
    "additional_findings": [
        {
            "id": "QA-01",
            "severity": "BLOCK",
            "file": "collector.py + analyzer.py",
            "issue": "analyze_snapshot()의 processing_status 업데이트가 실패해도 'done'으로 기록",
            "detail": (
                "_save_analyses() 또는 _update_user_profiles() 에서 예외가 발생해도 "
                "_update_snapshot_status(snapshot_id, 'done')이 호출되어 "
                "부분 실패를 성공으로 기록한다. "
                "다음 날 재처리 트리거가 없어 데이터 누락이 영구적으로 숨겨진다."
            ),
            "fix_required": (
                "try/except로 _save_analyses + _update_user_profiles 묶고 "
                "예외 발생 시 status='failed'로 업데이트."
            ),
        },
        {
            "id": "QA-02",
            "severity": "BLOCK",
            "file": "scheduler.py",
            "issue": "파이프라인 실패 알림 없음",
            "detail": (
                "docstring에 '실패 알림 (Slack/이메일 연동 가능)'이라고 썼으나 "
                "실제 알림 코드가 없다. 오전 2시 배치가 조용히 실패해도 "
                "다음날 아침까지 아무도 모른다."
            ),
            "fix_required": "최소한 total_errors > 0 시 logger.critical() + 알림 훅(hook) 인터페이스",
        },
        {
            "id": "QA-03",
            "severity": "WARN",
            "file": "tests/test_all.py",
            "issue": "F-02, E-02, Q-01, Q-02에 대한 테스트 없음",
            "detail": "이번 라운드에서 발견된 결함들에 대한 회귀 테스트가 없다.",
            "fix_required": "각 결함 수정 후 반드시 테스트 케이스 추가",
        },
    ],
    "final_summary": (
        "이전 두 번의 검토(1차 코드, 2차 수정)에서 내가 놓친 결함이 "
        "이번 순차 검토에서 추가로 8건 발견됐다. "
        "구조: F-01, F-02, F-03 / 성능: E-01, E-02 / 보안: Q-01, Q-02, Q-03 / "
        "안정성: QA-01, QA-02. "
        "총 BLOCK 9건, WARN 4건. 전량 수정 전 배포 불가."
    ),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUND 5 — 중재자 (4인 결과 종합 + 충돌 조율 + 최종 결정)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUND_5_ORCHESTRATOR = {
    "reviewer": "중재자 (Orchestrator)",
    "focus": "4인 결과 충돌 조율 + 비즈니스 우선순위 결정 + 최종 배포 판단",
    "prior_review_read": "4인 전원 결과 확인. 충돌 항목 분석 완료.",

    "conflict_resolution": [
        {
            "conflict": "E-01 메모리 문제의 심각도",
            "efficiency_says": "BLOCK — 1,000명 × 메시지 수 메모리 폭탄",
            "quality_says": "현재 규모에서는 괜찮음",
            "orchestrator_decision": (
                "Efficiency 손을 들어줌. "
                "상용화 관점에서 '현재는 괜찮다'는 기준으로 설계하면 안 된다. "
                "지금 설계가 확장 한계를 결정한다. BLOCK 유지."
            ),
        },
    ],

    "orchestrator_own_finding": {
        "id": "O-01",
        "severity": "BLOCK",
        "issue": "4인 모두 놓친 것: 트랜잭션 경계가 collector와 analyzer에 분리되어 있어 원자성 보장 불가",
        "detail": (
            "collector._save_snapshot()은 트랜잭션으로 snapshot 저장. "
            "그러나 analyzer._save_analyses()는 별도 커넥션, 별도 트랜잭션. "
            "snapshot은 있는데 analyses가 없는 반쪽짜리 데이터가 "
            "DB에 남을 수 있다. "
            "agent가 이 snapshot을 읽어 댓글을 생성하면 "
            "데이터 없이 댓글만 나가는 상황 발생."
        ),
        "fix_required": (
            "processing_status='done' 판정 기준을 "
            "analyses 저장 완료 후로 명확히 하고 "
            "agent는 status='done' 인 snapshot만 처리하도록 보장."
        ),
    },

    "final_block_list": [
        "F-01: analyzer.py lazy import + 순환참조",
        "F-02: collector.py 심볼릭 링크 체크 무효",
        "F-03: collector.py snapshot_date 날짜 덮어쓰기",
        "E-01: analyzer.py 전체 메시지 메모리 적재",
        "E-02: analyzer.py rule_results key 충돌",
        "Q-01: collector.py display_name 원본 메모리 노출",
        "Q-02: agent.py 빈 댓글 DB 저장",
        "Q-03: api.py Rate Limiting 없음",
        "QA-01: analyzer.py 부분 실패를 'done'으로 기록",
        "QA-02: scheduler.py 실패 알림 없음",
        "O-01: collector-analyzer 트랜잭션 원자성 부재",
    ],

    "verdict": "REJECT — 수정 후 재심의 필요",
    "required_before_resubmit": (
        "11건 BLOCK 전량 수정 + 각 수정에 대한 테스트 케이스 추가. "
        "재제출 시 본 5라운드 프로토콜 재실행 필수."
    ),
}
