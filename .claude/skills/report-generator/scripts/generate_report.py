"""
STEP 5: HTML 리포트 생성 (Chart.js 6섹션, v1.3)
- step2_stats.json + step3_analysis.json → YYYY-MM-DD_report.html
- 섹션5·6: 집계 통계만 표시 (개인 식별 불가)
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).parent.parent / 'references' / 'report_template.html'

# 섹션 존재 확인용 ID
REQUIRED_SECTIONS = [
    'section-topics', 'section-qa', 'section-top10',
    'section-hourly', 'section-city', 'section-visa-age'
]


def _escape(text: str) -> str:
    """HTML 특수문자 이스케이프."""
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def _to_js_array(lst) -> str:
    """Python 리스트 → JS 배열 문자열."""
    if not lst:
        return '[]'
    if isinstance(lst[0], str):
        return '[' + ','.join(f'"{_escape(s)}"' for s in lst) + ']'
    return '[' + ','.join(str(v) for v in lst) + ']'


def _build_topics_html(topics: list) -> str:
    parts = []
    for t in topics:
        rank = t.get('rank', '')
        title = _escape(t.get('title', ''))
        summary = _escape(t.get('summary', ''))
        parts.append(f'''
<div class="card">
  <div style="display:flex;align-items:center;margin-bottom:8px;">
    <span class="topic-rank">{rank}</span>
    <span class="topic-title">{title}</span>
  </div>
  <div class="topic-summary">{summary}</div>
</div>''')
    return '\n'.join(parts)


def _build_qa_html(question_analysis: list) -> str:
    parts = []
    for i, qa in enumerate(question_analysis):
        domain = _escape(qa.get('domain', '기타'))
        question = _escape(qa.get('representative_question', ''))
        answer = _escape(qa.get('answer', ''))
        confidence = qa.get('confidence', 'medium')
        cluster_size = qa.get('cluster_size', 0)
        user_ctx = _escape(qa.get('user_context', ''))

        conf_text = {'high': '높음', 'medium': '보통', 'low': '낮음'}.get(confidence, '보통')
        ctx_html = f'<div style="font-size:0.8rem;color:#64748B;margin-top:4px;">{user_ctx}</div>' if user_ctx else ''

        parts.append(f'''
<div class="accordion">
  <div class="accordion-header">
    <span><span class="domain-badge">{domain}</span>&nbsp; {question} <span style="color:#94A3B8;font-size:0.8rem;">({cluster_size}건)</span></span>
    <span class="toggle-icon">▼</span>
  </div>
  <div class="accordion-body">
    <div class="question-text">Q. {question}</div>
    <div class="answer-text">{answer}</div>
    {ctx_html}
    <div class="confidence {confidence}">신뢰도: {conf_text}</div>
  </div>
</div>''')
    return '\n'.join(parts)


def generate(stats_path: str, analysis_path: str, output_path: str) -> str:
    with open(stats_path, encoding='utf-8') as f:
        stats = json.load(f)
    with open(analysis_path, encoding='utf-8') as f:
        analysis = json.load(f)

    target_date = stats['date']
    template = TEMPLATE_PATH.read_text(encoding='utf-8')

    # ── 헤더 데이터 ──────────────────────────────────────
    total_users = stats.get('total_users', 0)
    total_messages = stats.get('total_messages', 0)

    # ── 섹션 1: 핵심 주제 ────────────────────────────────
    topics_html = _build_topics_html(analysis.get('topics', []))

    # ── 섹션 2: Q&A 아코디언 ─────────────────────────────
    qa_html = _build_qa_html(analysis.get('question_analysis', []))

    # ── 섹션 3: TOP 10 ────────────────────────────────────
    ranking = stats.get('user_ranking', [])
    top10_labels = [r['user'] for r in ranking]
    top10_data = [r['message_count'] for r in ranking]

    # ── 섹션 4: 시간대 밀도 ───────────────────────────────
    hourly = stats.get('hourly_density', {})
    hourly_labels = [f'{h:02d}시' for h in range(24)]
    hourly_data = [hourly.get(f'{h:02d}', 0) for h in range(24)]

    # ── 섹션 5·6: 메타 통계 ──────────────────────────────
    meta = stats.get('meta_stats', {})

    city_dist = meta.get('city_distribution', {})
    city_labels = list(city_dist.keys()) or ['데이터 없음']
    city_data = list(city_dist.values()) or [1]

    visa_dist = meta.get('visa_distribution', {})
    visa_labels = list(visa_dist.keys()) or ['데이터 없음']
    visa_data = list(visa_dist.values()) or [1]

    age_dist = meta.get('age_group_distribution', {})
    age_order = ['10s', '20s', '30s', '40s', '50s+', 'OTHER']
    age_labels = [a for a in age_order if a in age_dist] or list(age_dist.keys()) or ['데이터 없음']
    age_data = [age_dist.get(a, 0) for a in age_labels] or [1]

    # ── 템플릿 치환 ────────────────────────────────────────
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    html = (template
            .replace('{{date}}', target_date)
            .replace('{{total_users}}', str(total_users))
            .replace('{{total_messages}}', str(total_messages))
            .replace('{{topics_html}}', topics_html)
            .replace('{{qa_html}}', qa_html)
            .replace('{{top10_labels}}', _to_js_array(top10_labels))
            .replace('{{top10_data}}', _to_js_array(top10_data))
            .replace('{{hourly_labels}}', _to_js_array(hourly_labels))
            .replace('{{hourly_data}}', _to_js_array(hourly_data))
            .replace('{{city_labels}}', _to_js_array(city_labels))
            .replace('{{city_data}}', _to_js_array(city_data))
            .replace('{{visa_labels}}', _to_js_array(visa_labels))
            .replace('{{visa_data}}', _to_js_array(visa_data))
            .replace('{{age_labels}}', _to_js_array(age_labels))
            .replace('{{age_data}}', _to_js_array(age_data))
            .replace('{{generated_at}}', generated_at))

    # ── 섹션 검증 ──────────────────────────────────────────
    missing = [s for s in REQUIRED_SECTIONS if f'id="{s}"' not in html]
    if missing:
        raise RuntimeError(f"리포트 섹션 누락: {missing}")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding='utf-8')

    logger.info(f"[STEP 5] 리포트 생성 완료: {out}")
    return str(out)


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--stats', required=True)
    p.add_argument('--analysis', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    generate(args.stats, args.analysis, args.output)
