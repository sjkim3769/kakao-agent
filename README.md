# kakao-agent

카카오톡 대화방 자동 분석 및 답변 에이전트 시스템

## 개요
1,000명+ 카카오톡 단톡방 대화를 1일 1회 수집·분석하여
질문 유형별 자동 답변과 일일 주제 요약을 생성합니다.

## 기술 스택
- Python 3.11+, FastAPI, PostgreSQL, Redis, APScheduler
- Anthropic Claude API

## 시작하기
```bash
cp .env.example .env
pip install -r requirements.txt
python -m src.scheduler.scheduler
```

## 문서
- [통합 설계서](CLAUDE.md)
- [아키텍처](docs/ARCHITECTURE.md)
- [배포 체크리스트](docs/ORCHESTRATOR_CHECKLIST.md)
