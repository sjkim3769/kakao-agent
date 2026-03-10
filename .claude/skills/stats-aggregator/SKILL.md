# Skill: stats-aggregator

## 역할
step1 데이터 집계: 사용자 통계 + 시간대 밀도 + 메타정보 통계(v1.3).

## 스크립트
`.claude/skills/stats-aggregator/scripts/aggregate_stats.py`

## 입/출력
- 입력: `/output/step1_preprocessed.json`
- 출력: `/output/step2_stats.json`

## 집계 항목
- user_ranking: 사용자별 메시지 수 TOP 10
- hourly_density: 시간대별 메시지 수 (00~23)
- meta_stats: city/visa/age_group/arrival_year 분포, meta_coverage_rate

## 보안 규칙
5명 미만 그룹 → OTHER 합산 (개인 특정 방지)

## 성공 기준
total_users > 0 | meta_stats 섹션 존재 | 메타 집계 실패 시 스킵+로그
