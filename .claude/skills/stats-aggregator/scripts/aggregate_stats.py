"""
STEP 2-A: 통계 집계 + 메타정보 집계 (v1.3)
- 사용자별 메시지 수, 시간대 밀도
- meta_stats: 도시/비자/연령대/입주연도 분포
- 보안: 5명 미만 그룹 → OTHER 합산
"""

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MIN_GROUP_SIZE = 5  # 이 미만 그룹은 OTHER 합산


def _merge_small_groups(dist: dict) -> dict:
    """5명 미만 그룹을 OTHER로 합산."""
    result = {}
    other = 0
    for k, v in dist.items():
        if k == 'OTHER':
            other += v
        elif v < MIN_GROUP_SIZE:
            other += v
        else:
            result[k] = v
    if other:
        result['OTHER'] = result.get('OTHER', 0) + other
    return dict(sorted(result.items(), key=lambda x: -x[1]))


def _age_group(birth_year: Optional[int]) -> Optional[str]:
    if birth_year is None:
        return None
    current_year = 2026
    age = current_year - birth_year
    if age < 20:
        return '10s'
    elif age < 30:
        return '20s'
    elif age < 40:
        return '30s'
    elif age < 50:
        return '40s'
    else:
        return '50s+'


def _arrival_group(arrival_year: Optional[int]) -> Optional[str]:
    if arrival_year is None:
        return None
    if arrival_year <= 2015:
        return '2015이전'
    elif arrival_year <= 2020:
        return '2016-2020'
    else:
        return '2021이후'


def aggregate(step1_path: str, output_path: str) -> dict:
    with open(step1_path, encoding='utf-8') as f:
        data = json.load(f)

    messages = data['messages']
    target_date = data['date']

    # ── 사용자별 카운트 ───────────────────────────────
    user_counts = defaultdict(int)
    hourly = defaultdict(int)

    # ── 메타 집계용 (사용자별 첫 등장 메타만 사용) ────
    user_meta = {}

    for msg in messages:
        user = msg['user_normalized']
        user_counts[user] += 1

        # 시간대
        try:
            hour = msg['timestamp'][11:13]
            hourly[hour] += 1
        except Exception:
            pass

        # 메타 — 사용자별 첫 번째 메타 기록
        if user not in user_meta and msg.get('meta'):
            user_meta[user] = msg['meta']

    # ── user_ranking TOP 10 ───────────────────────────
    ranking = sorted(user_counts.items(), key=lambda x: -x[1])
    user_ranking = [
        {'rank': i + 1, 'user': u, 'message_count': c}
        for i, (u, c) in enumerate(ranking[:10])
    ]

    # ── hourly_density ────────────────────────────────
    hourly_density = {f'{h:02d}': hourly.get(f'{h:02d}', 0) for h in range(24)}

    # ── meta_stats 집계 ───────────────────────────────
    meta_stats = {}
    try:
        city_raw = defaultdict(int)
        visa_raw = defaultdict(int)
        age_raw = defaultdict(int)
        arrival_raw = defaultdict(int)
        meta_parsed = 0

        for meta in user_meta.values():
            has_data = False
            if meta.get('city'):
                city_raw[meta['city']] += 1
                has_data = True
            if meta.get('visa_status'):
                visa_raw[meta['visa_status']] += 1
                has_data = True
            ag = _age_group(meta.get('birth_year'))
            if ag:
                age_raw[ag] += 1
                has_data = True
            arr = _arrival_group(meta.get('arrival_year'))
            if arr:
                arrival_raw[arr] += 1
                has_data = True
            if has_data:
                meta_parsed += 1

        total_users = len(user_meta)
        coverage = meta_parsed / total_users if total_users > 0 else 0.0

        meta_stats = {
            'city_distribution': _merge_small_groups(dict(city_raw)),
            'visa_distribution': _merge_small_groups(dict(visa_raw)),
            'age_group_distribution': _merge_small_groups(dict(age_raw)),
            'arrival_year_distribution': _merge_small_groups(dict(arrival_raw)),
            'meta_coverage_rate': round(coverage, 4),
        }
        logger.info(f"[STEP 2] meta_stats 집계 완료 — coverage: {coverage:.1%}")

    except Exception as e:
        logger.warning(f"[STEP 2] meta_stats 집계 실패 (스킵): {e}")
        meta_stats = {}

    result = {
        'date': target_date,
        'total_users': len(user_counts),
        'total_messages': data['total_messages'],
        'user_ranking': user_ranking,
        'hourly_density': hourly_density,
        'meta_stats': meta_stats,
    }

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(
        f"[STEP 2] 통계 완료 — 사용자: {result['total_users']}, "
        f"메시지: {result['total_messages']}"
    )
    return result


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    aggregate(args.input, args.output)
