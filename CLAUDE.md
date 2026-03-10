# 카카오톡 대화방 분석 Agent — 오케스트레이터 지침

> **버전**: v1.3 | **설계서**: `kakao_agent_design_v1.3.md`
> **역할**: 6단계 파이프라인 총괄 오케스트레이터

---

## Role

본 CLAUDE.md는 카카오톡 그룹채팅 분석 파이프라인의 **오케스트레이터**다.
STEP 1~2, 4~6은 스킬 스크립트를 직접 호출하고, STEP 3(LLM 분석)만 `analysis-agent`에 위임한다.

---

## Workflow

### 실행 진입점
```bash
python main.py --date YYYY-MM-DD          # 분석만
python main.py --date YYYY-MM-DD --deploy # 분석 + 배포
```

### STEP 1: 파일 로드 & 전처리
- **스킬**: `kakao-parser`
- **스크립트**: `.claude/skills/kakao-parser/scripts/parse_and_clean.py`
- **트리거 조건**: `/input/kakao_YYYY-MM-DD.txt` 존재 확인
- **입력**: `/input/kakao_YYYY-MM-DD.txt`
- **출력**: `/output/step1_preprocessed.json`
- **성공 기준**: 파싱 성공률 ≥ 95%, 멀티라인 병합 건수 로그

### STEP 2: 통계 집계 + 메타정보 집계
- **스킬**: `stats-aggregator` → `question-clusterer`
- **스크립트**: `aggregate_stats.py` → `cluster_questions.py`
- **트리거 조건**: step1_preprocessed.json 존재 확인
- **입력**: step1_preprocessed.json
- **출력**: `step2_stats.json` (meta_stats 포함), `step2_question_candidates.json`
- **성공 기준**: total_users > 0, 질문 후보 1개 이상, meta_stats 집계 완료

### STEP 3: LLM 분석 [analysis-agent 위임]
- **에이전트**: `.claude/agents/analysis-agent/AGENT.md` 참조
- **트리거 조건**: step2 완료 후 자동 호출
- **입력**: step2_question_candidates.json, step2_stats.json (파일 경로 전달)
- **출력**: `step3_analysis.json`
- **멱등성**: 동일 날짜 재실행 시 step3_analysis.json 캐시 재사용

### STEP 4: 결과 검증
- **처리 주체**: 스크립트 (JSON 스키마 검증)
- **검증 항목**:
  - 필수 필드: `date, topics(3개), question_analysis`
  - 주제 요약 각 100자 이상
  - 7개 도메인 분류 유효성
  - meta_stats 섹션 존재
- **실패 처리**: 스키마 오류 → STEP 3 재시도(1회) → 에스컬레이션

### STEP 5: HTML 리포트 생성
- **스킬**: `report-generator`
- **스크립트**: `.claude/skills/report-generator/scripts/generate_report.py`
- **트리거 조건**: STEP 4 검증 통과
- **출력**: `/output/YYYY-MM-DD_report.html`
- **성공 기준**: 6개 섹션 모두 포함, 섹션 5·6 데이터 렌더링 확인

### STEP 6: GitHub Pages 배포 (`--deploy` 플래그 시)
- **스킬**: `deployer`
- **스크립트**: `.claude/skills/deployer/scripts/deploy.sh`
- **처리**: HTML → `/docs/` 복사 → index.html 갱신 → git push origin main
- **성공 기준**: git push exit code 0

---

## Agent Delegation

### analysis-agent 호출 조건
- STEP 2 완료 → `step2_question_candidates.json`, `step2_stats.json` 생성 확인 후 호출
- 동일 날짜 `step3_analysis.json` 이미 존재 → 캐시 재사용, 호출 스킵

### 입출력 명세
```
입력 파일: /output/step2_question_candidates.json
          /output/step2_stats.json
출력 파일: /output/step3_analysis.json
```

### 데이터 전달 방식
- 파일 경로 인라인 전달 (DB 미사용)
- analysis-agent는 파일을 직접 읽고 결과를 파일로 저장

---

## Error Handling

| 단계 | 오류 유형 | 처리 방법 |
|------|-----------|-----------|
| STEP 1 | 파싱 오류 3% 초과 | 에스컬레이션 + error.log |
| STEP 1 | 닉네임 파싱 실패 | 스킵 + 로그 (전체 중단 불가) |
| STEP 2 | 질문 후보 0개 | 스킵 + 로그, 주제 요약만 진행 |
| STEP 2 | 메타 집계 실패 | 스킵 + 로그 (섹션 5·6 '데이터 없음') |
| STEP 3 | LLM 오류 | 자동 재시도 최대 2회 → 에스컬레이션 |
| STEP 3 | 429 Rate Limit | 60초 대기 후 재시도 (최대 3회) |
| STEP 4 | 스키마 오류 | STEP 3 재시도(1회) → 에스컬레이션 |
| STEP 5 | HTML 생성 실패 | 에스컬레이션 |
| STEP 6 | git push 실패 | 스킵 + 로그 (로컬 output/ 보존) |

**에스컬레이션**: `/output/error.log`에 날짜·단계·오류 내용 기록 후 파이프라인 중단.

---

## Token Policy

LLM 전송 전 반드시 아래 순서 준수:

1. **STEP 1에서 가비지 제거 확인** — '사진'·'동영상'·'이모티콘'·URL 메시지 제거 (~3.3%)
2. **STEP 2에서 질문 후보만 추출** — 전체 대화 비전송, 군집 대표 메시지만 전송
3. **청크 크기**: 최대 3,000 토큰/청크
4. **청크 간 딜레이**: `time.sleep(0.5)`
5. **429 재시도**: 60초 대기, 최대 3회
6. **일일 한도**: Groq 무료 티어 500,000 tok/day 준수

토큰 최적화 목표: LLM 전송 토큰 = 전처리 전 대비 **20% 이하**

---

## File Conventions

### 입력 파일
```
/input/kakao_YYYY-MM-DD.txt    # 카카오톡 그룹채팅 내보내기 (UTF-8, CRLF)
```

### 중간 산출물 (gitignore 등록, 원본 데이터 보호)
```
/output/step1_preprocessed.json
/output/step2_stats.json              # meta_stats 섹션 포함
/output/step2_question_candidates.json
/output/step3_analysis.json
/output/YYYY-MM-DD_report.html
/output/error.log
```

### 배포 파일 (GitHub Pages, git push 대상)
```
/docs/index.html                      # 날짜별 리포트 인덱스 (최신순)
/docs/YYYY-MM-DD_report.html          # 집계 통계만 포함된 HTML
```

### 환경설정
```
/.env                                  # gitignore 필수
```

---

## Success Criteria

전체 파이프라인 성공 조건 (모두 충족 시):

- [ ] step1_preprocessed.json — 파싱 성공률 ≥ 95%
- [ ] step2_stats.json — meta_stats 섹션 포함
- [ ] step2_question_candidates.json — 질문 후보 1개 이상
- [ ] step3_analysis.json — 주제 3개 완성, 각 100자 이상, 7개 도메인 유효
- [ ] YYYY-MM-DD_report.html — 6섹션 포함, 섹션 5·6 렌더링 확인
- [ ] (--deploy 시) git push exit code 0

---

## Security Policy

### 메타정보 노출 규칙 ★v1.3
| 규칙 | 금지 | 허용 |
|------|------|------|
| 개인 식별 | 닉네임 + 메타정보 1:1 노출 | 집계 통계만 표시 |
| 집계 최소 단위 | 5명 미만 그룹 별도 항목 | 5명 미만 → OTHER 합산 |
| TOP 10 섹션 | 닉네임과 메타정보 함께 노출 | 닉네임 + 메시지수만 표시 |
| Git 공개 | output/ 폴더 push | docs/ 폴더만 push |

### 필수 체크리스트
- [ ] `.env` → `.gitignore` 등록 확인
- [ ] `output/` → `.gitignore` 등록 (원본 데이터 보호)
- [ ] `docs/`만 GitHub push (집계 통계 HTML만)
- [ ] 섹션 5·6: 개인 닉네임-메타 1:1 매핑 없이 집계 통계만 렌더링 확인
- [ ] 5명 미만 그룹 OTHER 합산 로직 구현 확인

---

## 스킬 참조

| 스킬 | SKILL.md 경로 |
|------|--------------|
| kakao-parser | `.claude/skills/kakao-parser/SKILL.md` |
| stats-aggregator | `.claude/skills/stats-aggregator/SKILL.md` |
| question-clusterer | `.claude/skills/question-clusterer/SKILL.md` |
| report-generator | `.claude/skills/report-generator/SKILL.md` |
| deployer | `.claude/skills/deployer/SKILL.md` |

## 에이전트 참조

| 에이전트 | AGENT.md 경로 |
|----------|--------------|
| analysis-agent | `.claude/agents/analysis-agent/AGENT.md` |
