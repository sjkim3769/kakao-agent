# Agent: analysis-agent

## 역할
전처리된 질문 후보 + 대화 샘플을 Groq API로 분석하여
핵심 주제 요약, 질문 도메인 분류(7개), 답변 생성을 수행한다.

## 트리거 조건
STEP 2 완료 후 오케스트레이터(CLAUDE.md)로부터 호출.
동일 날짜 step3_analysis.json 이미 존재 → 캐시 재사용, 호출 스킵.

## 입력 파일
- `/output/step2_question_candidates.json`
- `/output/step2_stats.json`

## 출력 파일
- `/output/step3_analysis.json`

## LLM 설정
- API: Groq API
- 모델: `llama-3.3-70b-versatile`
- 출력 형식: JSON mode 강제 (`response_format={"type": "json_object"}`)

## Rate Limit 전략
| 항목 | 기준 | 대응 |
|------|------|------|
| 분당 요청 | 30 req/min | 청크 간 time.sleep(0.5) |
| 분당 토큰 | 6,000 tok/min | 청크 최대 3,000 토큰 |
| 429 오류 | Rate Limit 초과 | 60초 대기 후 재시도 (최대 3회) |

## 처리 순서
1. step2_question_candidates.json 로드 → 군집 대표 질문 추출
2. step2_stats.json 로드 → 대화 샘플 + 메타 컨텍스트 구성
3. Groq API 호출 — 핵심 주제 3개 추출
4. Groq API 호출 — 질문 도메인 분류 + 답변 생성 (청크 단위)
5. 자기검증 프롬프트 실행
6. step3_analysis.json 저장

## 자기검증 기준
- 주제 요약 각 100자 이상
- 7개 도메인 분류 유효
- 답변 신뢰도 medium 이상

## 실패 처리
- LLM 오류 → 자동 재시도 최대 2회
- 2회 실패 → error.log 기록 + 에스컬레이션

## 프롬프트 템플릿
`references/prompt_templates.md` 참조

## 출력 스키마 (step3_analysis.json)
```json
{
  "date": "YYYY-MM-DD",
  "topics": [
    {
      "rank": 1,
      "title": "주제 제목",
      "summary": "3줄 요약 (100자 이상)"
    }
  ],
  "question_analysis": [
    {
      "domain": "통신·요금",
      "representative_question": "...",
      "cluster_size": 18,
      "answer": "...",
      "confidence": "high",
      "user_context": "WA·PER 거주자 질문 비중 높음"
    }
  ],
  "validation": {
    "topics_valid": true,
    "domains_valid": true,
    "overall": "pass"
  }
}
```
