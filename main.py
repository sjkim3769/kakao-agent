"""
카카오톡 대화방 분석 파이프라인 진입점 (v1.3)

사용법:
  python main.py --date 2026-03-10
  python main.py --date 2026-03-10 --deploy
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── 경로 설정 ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
INPUT_DIR = BASE_DIR / 'input'
OUTPUT_DIR = BASE_DIR / 'output'
SKILLS_DIR = BASE_DIR / '.claude' / 'skills'
DEPLOY_SCRIPT = SKILLS_DIR / 'deployer' / 'scripts' / 'deploy.sh'

# sys.path에 스킬 스크립트 디렉토리 추가
for skill in ['kakao-parser', 'stats-aggregator', 'question-clusterer', 'report-generator']:
    sys.path.insert(0, str(SKILLS_DIR / skill / 'scripts'))

import parse_and_clean
import aggregate_stats
import cluster_questions
import generate_report

# ── 로깅 설정 ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

# ── Groq 설정 ─────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')
GROQ_MODEL = 'llama-3.3-70b-versatile'
MAX_TOKENS_PER_CHUNK = 3000
CHUNK_DELAY = 0.5       # 청크 간 딜레이 (초)
RATE_LIMIT_WAIT = 60    # 429 오류 시 대기 (초)
MAX_RETRIES = 3


# ── 유틸 ──────────────────────────────────────────────────────────
def error_log(step: str, msg: str):
    """error.log에 기록."""
    log_path = OUTPUT_DIR / 'error.log'
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"[{timestamp}] [{step}] {msg}\n")
    logger.error(f"[{step}] {msg}")


def paths(date: str) -> dict:
    return {
        'input': INPUT_DIR / f'kakao_{date}.txt',
        'step1': OUTPUT_DIR / 'step1_preprocessed.json',
        'step2_stats': OUTPUT_DIR / 'step2_stats.json',
        'step2_questions': OUTPUT_DIR / 'step2_question_candidates.json',
        'step3': OUTPUT_DIR / f'step3_analysis.json',
        'report': OUTPUT_DIR / f'{date}_report.html',
    }


# ── STEP 3: Groq API 분석 ────────────────────────────────────────
def groq_call(client, messages: list, step_name: str) -> dict:
    """Groq API 호출 (429 자동 재시도 포함)."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                response_format={'type': 'json_object'},
                max_tokens=2048,
            )
            content = resp.choices[0].message.content
            return json.loads(content)
        except Exception as e:
            err_str = str(e)
            if '429' in err_str or 'rate_limit' in err_str.lower():
                logger.warning(f"[{step_name}] Rate Limit — {RATE_LIMIT_WAIT}초 대기 ({attempt+1}/{MAX_RETRIES})")
                time.sleep(RATE_LIMIT_WAIT)
            else:
                if attempt == MAX_RETRIES - 1:
                    raise
                logger.warning(f"[{step_name}] 오류 재시도 ({attempt+1}/{MAX_RETRIES}): {e}")
                time.sleep(2)
    raise RuntimeError(f"[{step_name}] 최대 재시도 초과")


def build_conversation_sample(step2_stats: dict, step1_path: Path, max_messages: int = 50) -> str:
    """대화 샘플 구성 (LLM 컨텍스트용)."""
    try:
        with open(step1_path, encoding='utf-8') as f:
            step1 = json.load(f)
        msgs = step1.get('messages', [])[:max_messages]
        return '\n'.join(
            f"[{m['user_normalized']}] {m['message'][:100]}"
            for m in msgs
        )
    except Exception:
        return ''


def meta_summary(step2_stats: dict) -> str:
    """메타 통계 요약 문자열 (LLM 컨텍스트)."""
    meta = step2_stats.get('meta_stats', {})
    parts = []
    city_dist = meta.get('city_distribution', {})
    if city_dist:
        top_cities = ', '.join(f"{k}({v}명)" for k, v in list(city_dist.items())[:5])
        parts.append(f"거주 지역: {top_cities}")
    visa_dist = meta.get('visa_distribution', {})
    if visa_dist:
        top_visa = ', '.join(f"{k}({v}명)" for k, v in list(visa_dist.items())[:3])
        parts.append(f"비자 상태: {top_visa}")
    total_users = step2_stats.get('total_users', 0)
    parts.append(f"총 참여자: {total_users}명")
    return ' | '.join(parts)


def run_step3(p: dict, target_date: str) -> dict:
    """STEP 3: Groq API 분석."""
    step3_path = p['step3']

    # 멱등성: 이미 존재하면 캐시 재사용
    if step3_path.exists():
        logger.info("[STEP 3] 캐시 재사용 (step3_analysis.json 이미 존재)")
        with open(step3_path, encoding='utf-8') as f:
            return json.load(f)

    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY 미설정 — .env 파일 확인")

    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY)

    with open(p['step2_stats'], encoding='utf-8') as f:
        stats = json.load(f)
    with open(p['step2_questions'], encoding='utf-8') as f:
        questions = json.load(f)

    meta_ctx = meta_summary(stats)
    conv_sample = build_conversation_sample(stats, p['step1'])

    # ── 3-A: 핵심 주제 추출 ───────────────────────────────
    logger.info("[STEP 3-A] 핵심 주제 추출")
    topic_prompt = f"""날짜: {target_date}
참여자 메타: {meta_ctx}

대화 샘플:
{conv_sample}

오늘의 핵심 주제 3개를 추출하세요. 각 요약은 100자 이상.
JSON 응답:
{{"topics": [{{"rank": 1, "title": "주제 제목", "summary": "3줄 요약\\n두번째줄\\n세번째줄"}}]}}"""

    topics_result = groq_call(client, [
        {'role': 'system', 'content': '호주 한인 커뮤니티 채팅 분석 전문가. 반드시 JSON으로만 응답.'},
        {'role': 'user', 'content': topic_prompt},
    ], 'STEP 3-A')

    time.sleep(CHUNK_DELAY)

    # ── 3-B: 질문 분류 및 답변 (청크 단위) ───────────────
    clusters = questions.get('clusters', [])
    question_analysis = []

    DOMAINS = '통신·요금 / 비자·이민 / 부동산·세금 / 생활정보 / 시사·뉴스반응 / 잡담 / 기타'

    if clusters:
        logger.info(f"[STEP 3-B] 질문 분류 — 군집 {len(clusters)}개")
        # 청크 분할 (대략 MAX_TOKENS_PER_CHUNK 기준으로 군집 묶기)
        chunk_size = max(1, MAX_TOKENS_PER_CHUNK // 200)  # 군집당 ~200 토큰 가정

        for chunk_start in range(0, len(clusters), chunk_size):
            chunk = clusters[chunk_start:chunk_start + chunk_size]
            cluster_text = '\n'.join(
                f"{i+1}. [{c.get('meta_context','')}] {c['representative_question']} (유사 질문 {c['cluster_size']}건)"
                for i, c in enumerate(chunk)
            )
            qa_prompt = f"""도메인 목록: {DOMAINS}

질문 군집:
{cluster_text}

각 질문을 도메인 분류하고 호주 실정에 맞는 답변을 생성하세요.
JSON:
{{"question_analysis": [{{"domain": "도메인명", "representative_question": "질문", "cluster_size": 숫자, "answer": "답변(50자 이상)", "confidence": "high|medium|low", "user_context": ""}}]}}"""

            result = groq_call(client, [
                {'role': 'system', 'content': '호주 한인 커뮤니티 채팅 분석 전문가. 반드시 JSON으로만 응답.'},
                {'role': 'user', 'content': qa_prompt},
            ], f'STEP 3-B 청크{chunk_start}')

            for qa, orig in zip(result.get('question_analysis', []), chunk):
                qa['cluster_size'] = orig.get('cluster_size', qa.get('cluster_size', 0))
            question_analysis.extend(result.get('question_analysis', []))
            time.sleep(CHUNK_DELAY)

    # ── 3-C: 자기검증 ──────────────────────────────────────
    logger.info("[STEP 3-C] 자기검증")
    val_result = {'overall': 'pass', 'topics_valid': True, 'domains_valid': True}
    try:
        val_prompt = f"""분석 결과를 검증하세요:
주제 수: {len(topics_result.get('topics', []))}
질문 분류 수: {len(question_analysis)}
도메인 목록 준수 여부 확인: {DOMAINS}

JSON:
{{"validation": {{"topics_valid": true, "domains_valid": true, "overall": "pass", "issues": []}}}}"""

        val_raw = groq_call(client, [
            {'role': 'system', 'content': '반드시 JSON으로만 응답.'},
            {'role': 'user', 'content': val_prompt},
        ], 'STEP 3-C')
        val_result = val_raw.get('validation', val_result)
    except Exception as e:
        logger.warning(f"[STEP 3-C] 자기검증 실패 (스킵): {e}")

    # ── 최종 결과 저장 ────────────────────────────────────
    result = {
        'date': target_date,
        'topics': topics_result.get('topics', []),
        'question_analysis': question_analysis,
        'validation': val_result,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(step3_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"[STEP 3] 완료 — 주제: {len(result['topics'])}개, 답변: {len(question_analysis)}개")
    return result


# ── STEP 4: 결과 검증 ─────────────────────────────────────────────
def validate_step3(analysis: dict) -> bool:
    issues = []
    topics = analysis.get('topics', [])
    if len(topics) < 3:
        issues.append(f"주제 수 부족: {len(topics)} < 3")
    for t in topics:
        if len(t.get('summary', '')) < 100:
            issues.append(f"주제 요약 길이 부족: {t.get('title','')}")

    qa = analysis.get('question_analysis', [])
    valid_domains = {'통신·요금', '비자·이민', '부동산·세금', '생활정보', '시사·뉴스반응', '잡담', '기타'}
    for item in qa:
        if item.get('domain') not in valid_domains:
            issues.append(f"유효하지 않은 도메인: {item.get('domain')}")

    if issues:
        logger.warning(f"[STEP 4] 검증 이슈: {issues}")
        return False

    logger.info("[STEP 4] 검증 통과")
    return True


# ── 메인 파이프라인 ───────────────────────────────────────────────
def run_pipeline(target_date: str, deploy: bool = False):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    p = paths(target_date)

    logger.info(f"{'='*50}")
    logger.info(f"파이프라인 시작: {target_date}")
    logger.info(f"{'='*50}")

    # ── STEP 1 ────────────────────────────────────────────
    try:
        logger.info("[STEP 1] 파일 로드 & 전처리")
        if not p['input'].exists():
            raise FileNotFoundError(f"입력 파일 없음: {p['input']}")
        parse_and_clean.run(str(p['input']), str(p['step1']), target_date)
    except Exception as e:
        error_log('STEP1', str(e))
        sys.exit(1)

    # ── STEP 2-A: 통계 집계 ───────────────────────────────
    try:
        logger.info("[STEP 2-A] 통계 집계")
        aggregate_stats.aggregate(str(p['step1']), str(p['step2_stats']))
    except Exception as e:
        error_log('STEP2-A', str(e))
        sys.exit(1)

    # ── STEP 2-B: 질문 군집화 ─────────────────────────────
    try:
        logger.info("[STEP 2-B] 질문 군집화")
        cluster_questions.cluster(str(p['step1']), str(p['step2_questions']))
    except Exception as e:
        error_log('STEP2-B', str(e))
        # 질문 군집화 실패는 치명적이지 않음 — 빈 파일로 계속
        logger.warning("[STEP 2-B] 군집화 실패 — 빈 결과로 진행")
        empty = {'date': target_date, 'total_questions': 0, 'clusters': []}
        with open(p['step2_questions'], 'w', encoding='utf-8') as f:
            json.dump(empty, f)

    # ── STEP 3: LLM 분석 ──────────────────────────────────
    try:
        logger.info("[STEP 3] LLM 분석 (analysis-agent)")
        analysis = run_step3(p, target_date)
    except Exception as e:
        error_log('STEP3', str(e))
        sys.exit(1)

    # ── STEP 4: 검증 ─────────────────────────────────────
    logger.info("[STEP 4] 결과 검증")
    if not validate_step3(analysis):
        # 1회 재시도: 캐시 삭제 후 재실행
        logger.warning("[STEP 4] 검증 실패 — STEP 3 재시도")
        if p['step3'].exists():
            p['step3'].unlink()
        try:
            analysis = run_step3(p, target_date)
            if not validate_step3(analysis):
                error_log('STEP4', '2회 검증 실패 — 에스컬레이션')
                sys.exit(1)
        except Exception as e:
            error_log('STEP4', str(e))
            sys.exit(1)

    # ── STEP 5: HTML 리포트 생성 ──────────────────────────
    try:
        logger.info("[STEP 5] HTML 리포트 생성")
        generate_report.generate(
            str(p['step2_stats']),
            str(p['step3']),
            str(p['report']),
        )
    except Exception as e:
        error_log('STEP5', str(e))
        sys.exit(1)

    # ── STEP 6: 배포 ─────────────────────────────────────
    if deploy:
        logger.info("[STEP 6] GitHub Pages 배포")
        try:
            result = subprocess.run(
                ['bash', str(DEPLOY_SCRIPT), target_date, str(BASE_DIR)],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                error_log('STEP6', f"deploy.sh 실패: {result.stderr}")
                logger.warning("[STEP 6] 배포 실패 — 로컬 파일은 보존됨")
            else:
                logger.info("[STEP 6] 배포 완료")
        except Exception as e:
            error_log('STEP6', str(e))
            logger.warning("[STEP 6] 배포 오류 — 로컬 파일은 보존됨")

    logger.info(f"{'='*50}")
    logger.info(f"파이프라인 완료: {target_date}")
    logger.info(f"리포트: {p['report']}")
    logger.info(f"{'='*50}")


# ── 진입점 ───────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='카카오톡 채팅 분석 파이프라인')
    parser.add_argument('--date', required=True, help='분석 날짜 (YYYY-MM-DD)')
    parser.add_argument('--deploy', action='store_true', help='GitHub Pages 배포 포함')
    args = parser.parse_args()

    run_pipeline(args.date, args.deploy)
