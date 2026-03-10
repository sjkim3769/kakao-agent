# Skill: report-generator

## 역할
step2_stats.json + step3_analysis.json → Chart.js 포함 단일 HTML 리포트 (6섹션).

## 스크립트
`.claude/skills/report-generator/scripts/generate_report.py`

## 입/출력
- 입력: `/output/step2_stats.json`, `/output/step3_analysis.json`
- 출력: `/output/YYYY-MM-DD_report.html`

## 6섹션 구성
1. 핵심 주제 3개 카드 UI
2. 도메인별 질문·답변 아코디언
3. 활발한 사용자 TOP 10 수평 바차트 (닉네임+메시지수만, 메타 미노출)
4. 시간대별 대화 밀도 라인차트
5. 커뮤니티 지역 현황 도넛차트 (집계 통계만)
6. 비자상태·연령대 분포 파이+바차트 (집계 통계만)

## 성공 기준
6섹션 모두 포함 | 섹션5·6 데이터 렌더링 확인 | 5명 미만 OTHER 합산 확인
