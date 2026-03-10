# Skill: question-clusterer

## 역할
TF-IDF + 코사인 유사도로 유사 질문 군집화 → LLM 전송 토큰 최소화.

## 스크립트
`.claude/skills/question-clusterer/scripts/cluster_questions.py`

## 의존성
`scikit-learn` (TF-IDF + cosine_similarity)

## 입/출력
- 입력: `/output/step1_preprocessed.json` (is_question=true 필터)
- 출력: `/output/step2_question_candidates.json`

## 의문문 감지 패턴
물음표(?) | 어디/언제/얼마/어떻게/뭐/왜/몇/어떤 | ~은요/는요/나요/죠/에요?

## 군집화 파라미터
코사인 유사도 임계값: 0.4 (운영 후 0.3~0.5 튜닝)

## 성공 기준
질문 후보 1개 이상 | 0개면 스킵+로그
