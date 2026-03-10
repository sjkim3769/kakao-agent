# Skill: kakao-parser

## 역할
카카오톡 그룹채팅 TXT → 구조화 JSON 변환. 멀티라인 병합, 닉네임 퍼지 파싱, 가비지 제거.

## 스크립트
`.claude/skills/kakao-parser/scripts/parse_and_clean.py`

## 입/출력
- 입력: `/input/kakao_YYYY-MM-DD.txt`
- 출력: `/output/step1_preprocessed.json`

## 메시지 정규식
`r'^\[(.+?)\]\s*\[(?:오전|오후)\s*(\d+:\d+)\]\s*(.+)'`

## 닉네임 구조
슬래시 또는 공백 구분자: `[닉네임/성별/출생연도/입주연도/도시/비자]`

## 도시 정규화 맵 (20+ 종)
BNE/bne/브리즈번→BNE | SYD/syd/시드니→SYD | MEL/mel/멜번→MEL | PER/per/퍼스→PER
ADL/애들레이드→ADL | GC/골드코스트→GC | TAS/태즈매니아→TAS | CBR/캔버라→CBR
NSW/VIC/QLD/WA/SA/NT→그대로 | 미확인→OTHER

## 비자 정규화
P/PR/pr→PR | C/CT/ct/시민권→CITIZEN | 진행중/진행→APPLYING | 미확인→UNKNOWN

## 연도 변환
- 출생 2자리: ≤50 → 2000+, >50 → 1900+
- 입주 2자리: 항상 2000+

## 가비지 제거
정확 일치: 사진, 동영상, 이모티콘, 파일 | http(s):// 시작 메시지

## 성공 기준
파싱 성공률 ≥ 95% | 멀티라인 병합/가비지 제거 건수 로그
