"""
STEP 2-B: TF-IDF 기반 유사 질문 군집화
- is_question=true 메시지만 처리
- 코사인 유사도 임계값 0.4으로 군집화
- 군집 대표 질문 선택 → LLM 전송 토큰 최소화
"""

import json
import logging
import re
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

# 의문문 감지 패턴
QUESTION_PATTERNS = [
    re.compile(r'\?'),
    re.compile(r'(어디|언제|얼마|어떻게|어떤|어느|뭐|왜|몇|누구|무슨)'),
    re.compile(r'(은요|는요|나요|죠|에요|인가요|할까요|될까요|인지요)\s*$'),
]

SIMILARITY_THRESHOLD = 0.4  # 운영 후 0.3~0.5 튜닝
MAX_CLUSTERS = 30           # 최대 군집 수 (LLM 토큰 관리)
MAX_SAMPLES_PER_CLUSTER = 3  # 군집당 샘플 메시지 수


def is_question(text: str) -> bool:
    return any(p.search(text) for p in QUESTION_PATTERNS)


def cluster(step1_path: str, output_path: str) -> dict:
    with open(step1_path, encoding='utf-8') as f:
        data = json.load(f)

    target_date = data['date']

    # 질문 메시지 필터링
    questions = [
        msg for msg in data['messages']
        if msg.get('is_question') or is_question(msg.get('message', ''))
    ]

    if not questions:
        logger.warning("[STEP 2-B] 질문 후보 0개 — 스킵")
        result = {'date': target_date, 'total_questions': 0, 'clusters': []}
        _save(result, output_path)
        return result

    texts = [q['message'] for q in questions]

    # TF-IDF 벡터화
    vectorizer = TfidfVectorizer(
        analyzer='char_wb',
        ngram_range=(2, 3),
        max_features=5000,
        min_df=1,
    )
    try:
        tfidf_matrix = vectorizer.fit_transform(texts)
    except Exception as e:
        logger.warning(f"[STEP 2-B] TF-IDF 실패: {e} — 군집화 스킵")
        result = {'date': target_date, 'total_questions': len(questions), 'clusters': []}
        _save(result, output_path)
        return result

    # Union-Find 방식 군집화
    n = len(texts)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    sim_matrix = cosine_similarity(tfidf_matrix)
    for i in range(n):
        for j in range(i + 1, n):
            if sim_matrix[i][j] >= SIMILARITY_THRESHOLD:
                union(i, j)

    # 군집 그룹화
    clusters_map = {}
    for i in range(n):
        root = find(i)
        if root not in clusters_map:
            clusters_map[root] = []
        clusters_map[root].append(i)

    # 군집 정렬 (크기 내림차순) + MAX_CLUSTERS 제한
    sorted_clusters = sorted(clusters_map.values(), key=len, reverse=True)[:MAX_CLUSTERS]

    clusters = []
    for idx, members in enumerate(sorted_clusters):
        # 대표 질문: 군집 내 다른 메시지들과의 평균 유사도가 가장 높은 것
        if len(members) == 1:
            rep_idx = members[0]
        else:
            sub_matrix = sim_matrix[np.ix_(members, members)]
            avg_sims = sub_matrix.mean(axis=1)
            rep_idx = members[int(avg_sims.argmax())]

        rep_msg = questions[rep_idx]
        samples = [questions[i]['message'] for i in members[:MAX_SAMPLES_PER_CLUSTER]
                   if i != rep_idx]

        clusters.append({
            'cluster_id': idx + 1,
            'representative_question': rep_msg['message'],
            'cluster_size': len(members),
            'sample_messages': samples,
            'meta_context': _build_meta_context([questions[i] for i in members]),
        })

    result = {
        'date': target_date,
        'total_questions': len(questions),
        'clusters': clusters,
    }

    _save(result, output_path)
    logger.info(
        f"[STEP 2-B] 군집화 완료 — 질문: {len(questions)}, "
        f"군집 수: {len(clusters)}"
    )
    return result


def _build_meta_context(msgs: list) -> str:
    """닉네임 메타 컨텍스트 요약 (LLM 분류 정확도 향상용)."""
    cities = [m['meta']['city'] for m in msgs if m.get('meta', {}).get('city')]
    visas = [m['meta']['visa_status'] for m in msgs if m.get('meta', {}).get('visa_status')]
    parts = []
    if cities:
        from collections import Counter
        top_city = Counter(cities).most_common(1)[0][0]
        parts.append(f"{top_city} 거주자 多")
    if visas:
        from collections import Counter
        top_visa = Counter(visas).most_common(1)[0][0]
        parts.append(f"{top_visa} 비자 多")
    return ' | '.join(parts) if parts else ''


def _save(result: dict, output_path: str):
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    cluster(args.input, args.output)
