"""
STEP 1: 카카오톡 그룹채팅 TXT 파싱 & 전처리
- 실제 그룹채팅 포맷: [닉네임/성별/출생연도/입주연도/도시/비자] [오전 HH:MM] 메시지
- 멀티라인 병합, 닉네임 퍼지 파싱, 가비지 필터링
"""

import re
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── 정규식 ──────────────────────────────────────────────────────────
MSG_PATTERN = re.compile(r'^\[(.+?)\]\s*\[(?:오전|오후)\s*(\d+:\d+)\]\s*(.+)')
DATE_SEP = re.compile(r'^-{5,}')
FILE_HEADER = re.compile(r'님과 카카오톡 대화|저장한 날짜\s*:')
GARBAGE_EXACT = {'사진', '동영상', '이모티콘', '파일', '스티커'}
URL_PATTERN = re.compile(r'^https?://')

# ── 정규화 맵 ───────────────────────────────────────────────────────
CITY_MAP = {
    # 브리즈번
    'bne': 'BNE', '브리즈번': 'BNE', '브즈번': 'BNE', 'brisbane': 'BNE',
    # 시드니
    'syd': 'SYD', '시드니': 'SYD', 'sydney': 'SYD',
    # 멜버른
    'mel': 'MEL', '멜번': 'MEL', '멜버른': 'MEL', 'melbourne': 'MEL',
    # 퍼스
    'per': 'PER', '퍼스': 'PER', 'perth': 'PER',
    # 애들레이드
    'adl': 'ADL', '애들레이드': 'ADL', 'adelaide': 'ADL',
    # 골드코스트
    'gc': 'GC', '골드코스트': 'GC', 'goldcoast': 'GC', 'gold coast': 'GC',
    # 태즈매니아
    'tas': 'TAS', '태즈매니아': 'TAS', '태즈': 'TAS', 'tasmania': 'TAS',
    # 캔버라
    'cbr': 'CBR', '캔버라': 'CBR', 'canberra': 'CBR',
    # 주 코드 (그대로 유지)
    'nsw': 'NSW', 'vic': 'VIC', 'qld': 'QLD', 'wa': 'WA',
    'sa': 'SA', 'nt': 'NT', 'act': 'ACT',
}

VISA_MAP = {
    'p': 'PR', 'pr': 'PR', '영주권': 'PR',
    'c': 'CITIZEN', 'ct': 'CITIZEN', '시민권': 'CITIZEN',
    'citizen': 'CITIZEN',
    '진행': 'APPLYING', '진행중': 'APPLYING', 'applying': 'APPLYING',
    'tr': 'TR', '임시': 'TR',
}

GENDER_MAP = {
    'm': 'M', '남': 'M', 'male': 'M',
    'f': 'F', '여': 'F', 'female': 'F',
}

# 의문문 감지 패턴
QUESTION_PATTERNS = [
    re.compile(r'\?'),
    re.compile(r'(어디|언제|얼마|어떻게|어떤|어느|뭐|왜|몇|누구|무슨)'),
    re.compile(r'(은요|는요|나요|죠|에요|인가요|할까요|될까요|인지요)\s*$'),
]


def normalize_city(raw: str) -> str:
    key = raw.strip().lower()
    return CITY_MAP.get(key, raw.upper() if len(raw) <= 3 else 'OTHER')


def normalize_visa(raw: str) -> str:
    key = raw.strip().lower()
    return VISA_MAP.get(key, 'UNKNOWN')


def normalize_gender(raw: str) -> str:
    key = raw.strip().lower()
    return GENDER_MAP.get(key, 'UNKNOWN')


def normalize_birth_year(raw: str) -> Optional[int]:
    try:
        y = int(raw.strip())
        if y < 100:  # 2자리
            return 2000 + y if y <= 50 else 1900 + y
        return y
    except (ValueError, AttributeError):
        return None


def normalize_arrival_year(raw: str) -> Optional[int]:
    try:
        y = int(raw.strip())
        if y < 100:
            return 2000 + y
        return y
    except (ValueError, AttributeError):
        return None


def parse_nickname(raw: str) -> Tuple[str, dict]:
    """닉네임 원본에서 메타 필드 추출 (슬래시/공백 혼재 구분자 지원)."""
    # 구분자 통일: 슬래시 또는 2개 이상 공백
    parts = re.split(r'[/\s]+', raw.strip())

    meta = {
        'gender': None,
        'birth_year': None,
        'arrival_year': None,
        'city': None,
        'visa_status': None,
    }

    if len(parts) < 2:
        return raw.strip(), meta

    nickname = parts[0]

    # 필드 파싱 (성별/출생/입주/도시/비자 순서)
    candidates = parts[1:]
    field_idx = 0

    for val in candidates:
        if field_idx == 0:  # 성별
            g = normalize_gender(val)
            if g != 'UNKNOWN' or val.upper() in ('M', 'F'):
                meta['gender'] = normalize_gender(val)
                field_idx += 1
                continue
        if field_idx == 1:  # 출생연도
            y = normalize_birth_year(val)
            if y:
                meta['birth_year'] = y
                field_idx += 1
                continue
        if field_idx == 2:  # 입주연도
            y = normalize_arrival_year(val)
            if y:
                meta['arrival_year'] = y
                field_idx += 1
                continue
        if field_idx == 3:  # 도시
            meta['city'] = normalize_city(val)
            field_idx += 1
            continue
        if field_idx == 4:  # 비자
            meta['visa_status'] = normalize_visa(val)
            field_idx += 1
            break

    return nickname, meta


def parse_time(am_pm_indicator: str, time_str: str, date_str: str) -> str:
    """오전/오후 HH:MM → ISO 타임스탬프 반환."""
    h, m = map(int, time_str.split(':'))
    if am_pm_indicator == '오후' and h != 12:
        h += 12
    elif am_pm_indicator == '오전' and h == 12:
        h = 0
    return f"{date_str}T{h:02d}:{m:02d}:00"


def is_question(text: str) -> bool:
    return any(p.search(text) for p in QUESTION_PATTERNS)


def is_garbage(text: str) -> bool:
    return text.strip() in GARBAGE_EXACT or bool(URL_PATTERN.match(text.strip()))


def parse_file(file_path: Path, target_date: str) -> dict:
    messages = []
    total_raw = 0
    garbage_removed = 0
    multiline_merged = 0
    parse_errors = 0

    current_date = None
    last_msg = None
    msg_id = 0

    with open(file_path, encoding='utf-8', errors='replace') as f:
        for raw_line in f:
            total_raw += 1
            line = raw_line.rstrip('\r\n')

            # 빈줄 스킵
            if not line.strip():
                continue
            # 파일 헤더 스킵
            if FILE_HEADER.search(line):
                continue
            # 날짜 구분선
            if DATE_SEP.match(line):
                m = re.search(r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', line)
                if m:
                    current_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                continue

            # 날짜 필터
            if current_date != target_date:
                # 이미 지난 날짜면 early exit 가능하지만, 파일에 날짜 순서 보장 없음 → 스킵만
                continue

            # 메시지 라인 파싱
            match = MSG_PATTERN.match(line)
            if match:
                nick_raw, time_raw, msg_text = match.group(1), match.group(2), match.group(3)

                # 오전/오후 추출 (정규식 재탐색)
                ampm_m = re.search(r'\[(오전|오후)\s*\d+:\d+\]', raw_line)
                ampm = ampm_m.group(1) if ampm_m else '오전'

                # 가비지 체크
                if is_garbage(msg_text):
                    garbage_removed += 1
                    last_msg = None
                    continue

                try:
                    nickname, meta = parse_nickname(nick_raw)
                    timestamp = parse_time(ampm, time_raw, current_date)
                    msg_id += 1
                    last_msg = {
                        'id': f'msg_{msg_id:05d}',
                        'timestamp': timestamp,
                        'user_raw': nick_raw,
                        'user_normalized': nickname,
                        'message': msg_text,
                        'is_question': is_question(msg_text),
                        'meta': meta,
                    }
                    messages.append(last_msg)
                except Exception as e:
                    parse_errors += 1
                    logger.warning(f"파싱 오류: {line[:50]} — {e}")
                    last_msg = None

            else:
                # 멀티라인 이어쓰기
                if last_msg is not None:
                    last_msg['message'] += '\n' + line
                    last_msg['is_question'] = is_question(last_msg['message'])
                    multiline_merged += 1

    total_messages = len(messages)
    parse_success_rate = total_messages / max(total_raw, 1)

    if total_raw > 0 and parse_errors / total_raw > 0.05:
        raise RuntimeError(f"파싱 오류율 {parse_errors/total_raw:.1%} > 5% — 에스컬레이션")

    return {
        'date': target_date,
        'total_raw_lines': total_raw,
        'total_messages': total_messages,
        'garbage_removed': garbage_removed,
        'multiline_merged': multiline_merged,
        'parse_errors': parse_errors,
        'parse_success_rate': round(parse_success_rate, 4),
        'messages': messages,
    }


def run(input_path: str, output_path: str, target_date: str) -> dict:
    file_path = Path(input_path)
    if not file_path.exists():
        raise FileNotFoundError(f"입력 파일 없음: {file_path}")

    logger.info(f"[STEP 1] 파싱 시작: {file_path} / 대상 날짜: {target_date}")
    result = parse_file(file_path, target_date)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(
        f"[STEP 1] 완료 — 메시지: {result['total_messages']}, "
        f"멀티라인: {result['multiline_merged']}, 가비지: {result['garbage_removed']}, "
        f"파싱률: {result['parse_success_rate']:.1%}"
    )
    return result


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--date', required=True)
    args = p.parse_args()
    run(args.input, args.output, args.date)
