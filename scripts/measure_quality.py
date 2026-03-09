"""
품질담당자 정량 측정 스크립트
- AST 기반 정적 분석 (외부 도구 불필요)
- 5개 측정 영역: 구조/안정성/보안/성능/유지보수성
- 각 항목 수치 산출 후 100점 만점 환산
"""

import ast
import re
import os
import sys
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

SRC_DIR = Path("/home/claude/kakao-agent/src")
FILES = {
    "collector": SRC_DIR / "collector/collector.py",
    "analyzer":  SRC_DIR / "analyzer/analyzer.py",
    "agent":     SRC_DIR / "agent/agent.py",
    "api":       SRC_DIR / "api/api.py",
    "scheduler": SRC_DIR / "scheduler/scheduler.py",
}

# ── 색상 ──
R = "\033[91m"; Y = "\033[93m"; G = "\033[92m"; B = "\033[94m"; W = "\033[0m"; BOLD = "\033[1m"

@dataclass
class Finding:
    file: str
    line: int
    code: str
    severity: str   # CRITICAL / HIGH / MEDIUM / LOW
    message: str
    category: str   # STRUCTURE / STABILITY / SECURITY / PERFORMANCE / MAINTAINABILITY

findings: list[Finding] = []

def add(file, line, code, severity, message, category):
    findings.append(Finding(file, line, code, severity, message, category))

# ══════════════════════════════════════════════════════════════
# 측정 1: 구조 (Structure) — 함수 복잡도, 클래스 설계
# ══════════════════════════════════════════════════════════════

def measure_structure(name: str, tree: ast.Module, source: str):
    lines = source.splitlines()

    for node in ast.walk(tree):
        # 1-A. 함수 길이 (>50줄 경고, >80줄 위험)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_lines = node.end_lineno - node.lineno
            if fn_lines > 80:
                add(name, node.lineno, "S001", "HIGH",
                    f"함수 '{node.name}' 길이 {fn_lines}줄 (기준: ≤80)", "STRUCTURE")
            elif fn_lines > 50:
                add(name, node.lineno, "S001", "MEDIUM",
                    f"함수 '{node.name}' 길이 {fn_lines}줄 (권고: ≤50)", "STRUCTURE")

            # 1-B. 인자 수 (>5개 경고)
            args = node.args
            total_args = len(args.args) + len(args.kwonlyargs)
            if total_args > 5:
                add(name, node.lineno, "S002", "MEDIUM",
                    f"함수 '{node.name}' 인자 {total_args}개 (기준: ≤5)", "STRUCTURE")

            # 1-C. 중첩 깊이 측정 (>4 경고)
            depth = _max_nesting(node)
            if depth > 4:
                add(name, node.lineno, "S003", "HIGH",
                    f"함수 '{node.name}' 최대 중첩 깊이 {depth} (기준: ≤4)", "STRUCTURE")

        # 1-D. 클래스당 메서드 수 (>15개 경고)
        if isinstance(node, ast.ClassDef):
            methods = [n for n in ast.walk(node)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if len(methods) > 15:
                add(name, node.lineno, "S004", "MEDIUM",
                    f"클래스 '{node.name}' 메서드 {len(methods)}개 (기준: ≤15)", "STRUCTURE")

    # 1-E. 함수 내 lazy import
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    add(name, child.lineno, "S005", "HIGH",
                        f"함수 '{node.name}' 내 lazy import: {ast.dump(child)[:60]}",
                        "STRUCTURE")


def _max_nesting(node) -> int:
    """재귀적으로 최대 중첩 깊이 계산"""
    max_depth = [0]
    def walk(n, depth):
        max_depth[0] = max(max_depth[0], depth)
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.With,
                                   ast.Try, ast.ExceptHandler)):
                walk(child, depth + 1)
            else:
                walk(child, depth)
    walk(node, 0)
    return max_depth[0]


# ══════════════════════════════════════════════════════════════
# 측정 2: 안정성 (Stability) — 예외처리, 타입힌트, None 안전
# ══════════════════════════════════════════════════════════════

def measure_stability(name: str, tree: ast.Module, source: str):

    for node in ast.walk(tree):
        # 2-A. 빈 except (bare except: 또는 except Exception: pass)
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                add(name, node.lineno, "ST001", "CRITICAL",
                    "bare except: — 모든 예외를 무차별 삼킴", "STABILITY")
            elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                add(name, node.lineno, "ST002", "HIGH",
                    "except + pass: 예외를 완전히 무시함", "STABILITY")

        # 2-B. 타입힌트 없는 public 함수
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):  # public만
                missing = []
                if node.returns is None:
                    missing.append("반환타입")
                for arg in node.args.args:
                    if arg.annotation is None and arg.arg != "self":
                        missing.append(f"파라미터 '{arg.arg}'")
                if missing:
                    add(name, node.lineno, "ST003", "MEDIUM",
                        f"함수 '{node.name}' 타입힌트 없음: {', '.join(missing[:3])}",
                        "STABILITY")

        # 2-C. assert 문 사용 (운영 코드에서 -O 플래그로 비활성화됨)
        if isinstance(node, ast.Assert):
            add(name, node.lineno, "ST004", "MEDIUM",
                "assert 문: python -O 실행 시 비활성화됨 — 명시적 raise로 교체", "STABILITY")

    # 2-D. TODO/FIXME/HACK 주석 수
    for i, line in enumerate(source.splitlines(), 1):
        if re.search(r'\b(TODO|FIXME|HACK|XXX)\b', line, re.IGNORECASE):
            add(name, i, "ST005", "LOW",
                f"미완성 주석: {line.strip()[:60]}", "STABILITY")


# ══════════════════════════════════════════════════════════════
# 측정 3: 보안 (Security)
# ══════════════════════════════════════════════════════════════

def measure_security(name: str, tree: ast.Module, source: str):
    lines = source.splitlines()

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # 3-A. 하드코딩 시크릿 패턴
        if re.search(
            r'(password|secret|api_key|token)\s*=\s*["\'][^"\']{6,}["\']',
            stripped, re.IGNORECASE
        ):
            add(name, i, "SEC001", "CRITICAL",
                f"하드코딩 시크릿 의심: {stripped[:60]}", "SECURITY")

        # 3-B. SQL 문자열 직접 포매팅 (f-string/% 포매팅)
        if re.search(r'(SELECT|INSERT|UPDATE|DELETE).*(f["\']|%\s)', stripped, re.IGNORECASE):
            add(name, i, "SEC002", "HIGH",
                f"SQL 인젝션 위험 — f-string/% 포매팅 SQL: {stripped[:60]}", "SECURITY")

        # 3-C. shell=True
        if re.search(r'subprocess.*shell\s*=\s*True', stripped):
            add(name, i, "SEC003", "CRITICAL",
                "subprocess shell=True: 명령어 인젝션 위험", "SECURITY")

        # 3-D. 개인정보 로그 출력 패턴
        if re.search(r'logger\.(info|debug|warning).*display_name', stripped):
            add(name, i, "SEC004", "HIGH",
                f"display_name 로그 출력 — 개인정보 노출 위험: {stripped[:60]}", "SECURITY")

        # 3-E. eval/exec 사용
        if re.search(r'\b(eval|exec)\s*\(', stripped):
            add(name, i, "SEC005", "CRITICAL",
                f"eval/exec 사용: 코드 인젝션 위험", "SECURITY")

    # 3-F. 입력값 검증 없이 Path() 직접 사용
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "Path":
                # 인자가 외부 입력처럼 보이는 경우 (변수 또는 복잡한 표현식)
                for arg in node.args:
                    if isinstance(arg, (ast.Name, ast.Subscript)):
                        add(name, node.lineno, "SEC006", "MEDIUM",
                            f"Path() 에 검증되지 않은 변수 전달 가능성 — L{node.lineno}",
                            "SECURITY")
                        break


# ══════════════════════════════════════════════════════════════
# 측정 4: 성능 (Performance)
# ══════════════════════════════════════════════════════════════

def measure_performance(name: str, tree: ast.Module, source: str):

    # 4-A. 루프 내 DB 호출 패턴 (N+1)
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            for child in ast.walk(node):
                if isinstance(child, ast.Await):
                    if isinstance(child.value, ast.Call):
                        call_str = ast.dump(child.value)
                        if any(kw in call_str for kw in ["execute", "fetchrow", "fetch("]):
                            add(name, child.lineno, "P001", "HIGH",
                                f"루프 내 DB 비동기 호출 — N+1 쿼리 의심 (L{child.lineno})",
                                "PERFORMANCE")

    # 4-B. 전체 결과 로드 후 Python에서 필터링 (LIMIT 없는 fetch)
    for i, line in enumerate(source.splitlines(), 1):
        if re.search(r'await.*\.fetch\(', line) and "LIMIT" not in line.upper():
            add(name, i, "P002", "MEDIUM",
                f"LIMIT 없는 fetch — 대용량 데이터 전체 로드 위험: {line.strip()[:60]}",
                "PERFORMANCE")

    # 4-C. 동기 sleep in async context
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_str = ast.dump(node.func) if hasattr(node, 'func') else ''
            if 'time' in call_str and 'sleep' in call_str:
                add(name, node.lineno, "P003", "HIGH",
                    "비동기 컨텍스트에서 time.sleep() — asyncio.sleep() 사용 필요",
                    "PERFORMANCE")


# ══════════════════════════════════════════════════════════════
# 측정 5: 유지보수성 (Maintainability)
# ══════════════════════════════════════════════════════════════

def measure_maintainability(name: str, tree: ast.Module, source: str):
    lines = source.splitlines()

    # 5-A. 매직 넘버 (의미없는 리터럴 숫자)
    magic_numbers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            val = node.value
            if val not in {0, 1, -1, 2, True, False} and abs(val) > 1:
                # 설정값처럼 보이는 숫자들
                if val in {86400, 3600, 1440, 500, 100, 50, 20, 30, 10, 5}:
                    magic_numbers.add((node.lineno, val))
    for line_no, val in sorted(magic_numbers)[:5]:
        add(name, line_no, "M001", "LOW",
            f"매직 넘버 {val} — 상수로 추출 권고", "MAINTAINABILITY")

    # 5-B. 주석 비율
    comment_lines = sum(1 for l in lines if l.strip().startswith("#"))
    code_lines = sum(1 for l in lines if l.strip() and not l.strip().startswith("#"))
    if code_lines > 0:
        ratio = comment_lines / code_lines
        if ratio < 0.05:
            add(name, 0, "M002", "MEDIUM",
                f"주석 비율 {ratio:.1%} (기준: ≥5%) — 문서화 부족", "MAINTAINABILITY")

    # 5-C. 라인 길이 (>120자)
    long_lines = [(i+1, len(l)) for i, l in enumerate(lines) if len(l) > 120]
    if long_lines:
        add(name, long_lines[0][0], "M003", "LOW",
            f"120자 초과 라인 {len(long_lines)}개 (첫 번째: L{long_lines[0][0]}, {long_lines[0][1]}자)",
            "MAINTAINABILITY")

    # 5-D. 중복 문자열 리터럴 (동일 문자열 3회 이상)
    str_counts = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) > 10:
                str_counts[node.value].append(node.lineno)
    for val, locs in str_counts.items():
        if len(locs) >= 3:
            add(name, locs[0], "M004", "LOW",
                f"문자열 '{val[:30]}' {len(locs)}회 중복 — 상수 추출 권고",
                "MAINTAINABILITY")


# ══════════════════════════════════════════════════════════════
# 점수 계산
# ══════════════════════════════════════════════════════════════

SEVERITY_WEIGHTS = {
    "CRITICAL": 20,
    "HIGH":     10,
    "MEDIUM":    5,
    "LOW":       1,
}

CATEGORY_WEIGHTS = {
    "STRUCTURE":       15,
    "STABILITY":       25,
    "SECURITY":        30,
    "PERFORMANCE":     20,
    "MAINTAINABILITY": 10,
}

def calc_score(file_findings: list[Finding]) -> dict:
    deductions = defaultdict(int)
    for f in file_findings:
        deductions[f.category] += SEVERITY_WEIGHTS[f.severity]

    category_scores = {}
    for cat, weight in CATEGORY_WEIGHTS.items():
        raw_deduction = deductions[cat]
        # 카테고리 내 최대 감점 = weight (100점 기준)
        deducted = min(raw_deduction, weight * 10) / 10
        score = max(0, weight - deducted)
        category_scores[cat] = round(score, 1)

    total = round(sum(category_scores.values()), 1)
    return {"total": total, "categories": category_scores}


# ══════════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n{BOLD}{'='*64}{W}")
    print(f"{BOLD}  🔍 품질담당자 정량 품질 측정 보고서{W}")
    print(f"{BOLD}{'='*64}{W}\n")

    all_scores = {}

    for name, path in FILES.items():
        source = path.read_text(encoding="utf-8")
        tree   = ast.parse(source, filename=str(path))
        file_findings = []

        def add_local(file, line, code, severity, message, category):
            f = Finding(file, line, code, severity, message, category)
            findings.append(f)
            file_findings.append(f)

        _orig_add = add
        import builtins

        # 측정 실행
        measure_structure(name, tree, source)
        measure_stability(name, tree, source)
        measure_security(name, tree, source)
        measure_performance(name, tree, source)
        measure_maintainability(name, tree, source)

        file_findings = [f for f in findings if f.file == name]
        score_result = calc_score(file_findings)
        all_scores[name] = score_result

        # 파일별 출력
        sev_counts = defaultdict(int)
        for f in file_findings:
            sev_counts[f.severity] += 1

        total_score = score_result["total"]
        grade = ("S" if total_score >= 90 else "A" if total_score >= 80
                 else "B" if total_score >= 70 else "C" if total_score >= 60 else "D")
        color = G if total_score >= 80 else Y if total_score >= 60 else R

        print(f"{BOLD}── {name}.py{W}  {color}{BOLD}{total_score}/100 ({grade}){W}")
        print(f"   발견: {R}CRITICAL {sev_counts['CRITICAL']}{W}  "
              f"{R}HIGH {sev_counts['HIGH']}{W}  "
              f"{Y}MEDIUM {sev_counts['MEDIUM']}{W}  "
              f"LOW {sev_counts['LOW']}")

        for cat, cat_score in score_result["categories"].items():
            bar_len = int(cat_score / CATEGORY_WEIGHTS[cat] * 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)
            cat_color = G if cat_score >= CATEGORY_WEIGHTS[cat]*0.8 else Y if cat_score >= CATEGORY_WEIGHTS[cat]*0.6 else R
            print(f"   {cat:<20} {cat_color}{bar}{W} {cat_score:4.1f}/{CATEGORY_WEIGHTS[cat]}")

        # 심각한 발견만 출력
        serious = [f for f in file_findings if f.severity in ("CRITICAL", "HIGH")]
        for f in serious[:8]:
            sev_color = R if f.severity == "CRITICAL" else Y
            print(f"   {sev_color}[{f.severity:8}]{W} L{f.line:4d} [{f.code}] {f.message[:72]}")
        if len(serious) > 8:
            print(f"   ... 외 {len(serious)-8}건")
        print()

    # ── 전체 종합 ──
    print(f"{BOLD}{'='*64}{W}")
    print(f"{BOLD}  종합 품질 측정 결과{W}")
    print(f"{BOLD}{'='*64}{W}")

    overall_total = round(sum(s["total"] for s in all_scores.values()) / len(all_scores), 1)
    overall_color = G if overall_total >= 80 else Y if overall_total >= 60 else R

    for name, score in all_scores.items():
        t = score["total"]
        grade = ("S" if t >= 90 else "A" if t >= 80 else "B" if t >= 70
                 else "C" if t >= 60 else "D")
        bar_len = int(t / 100 * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        c = G if t >= 80 else Y if t >= 60 else R
        print(f"  {name:<12} {c}{bar}{W} {t:5.1f}/100 ({grade})")

    print(f"\n  {'전체 평균':<12} {'─'*20} {overall_color}{BOLD}{overall_total}/100{W}")

    # 카테고리별 전체 평균
    print(f"\n{BOLD}  카테고리별 전체 평균{W}")
    for cat, weight in CATEGORY_WEIGHTS.items():
        cat_avg = sum(s["categories"][cat] for s in all_scores.values()) / len(all_scores)
        c = G if cat_avg >= weight*0.8 else Y if cat_avg >= weight*0.6 else R
        print(f"  {cat:<20} {c}{cat_avg:4.1f}/{weight}{W}")

    # 전체 결함 통계
    all_sev = defaultdict(int)
    for f in findings:
        all_sev[f.severity] += 1
    print(f"\n  총 발견 결함:")
    print(f"    {R}CRITICAL {all_sev['CRITICAL']}건{W}  {R}HIGH {all_sev['HIGH']}건{W}  "
          f"{Y}MEDIUM {all_sev['MEDIUM']}건{W}  LOW {all_sev['LOW']}건  "
          f"(합계 {sum(all_sev.values())}건)")

    # 배포 판정
    critical_count = all_sev["CRITICAL"]
    high_count = all_sev["HIGH"]
    print(f"\n{BOLD}{'='*64}{W}")
    if critical_count == 0 and overall_total >= 75:
        print(f"  {G}{BOLD}✅ 배포 판정: APPROVE ({overall_total}/100){W}")
    elif critical_count == 0:
        print(f"  {Y}{BOLD}⚠️  배포 판정: CONDITIONAL ({overall_total}/100) — HIGH 결함 수정 권고{W}")
    else:
        print(f"  {R}{BOLD}❌ 배포 판정: REJECT — CRITICAL {critical_count}건 수정 필수{W}")
    print(f"{BOLD}{'='*64}{W}\n")
