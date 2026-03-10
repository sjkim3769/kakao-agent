#!/bin/bash
# STEP 6: GitHub Pages 배포
# 사용법: bash deploy.sh YYYY-MM-DD [/path/to/repo]
set -euo pipefail

DATE="${1:?날짜 인수 필요: YYYY-MM-DD}"
REPO_DIR="${2:-$(cd "$(dirname "$0")/../../../../" && pwd)}"

OUTPUT_FILE="$REPO_DIR/output/${DATE}_report.html"
DOCS_DIR="$REPO_DIR/docs"
INDEX_FILE="$DOCS_DIR/index.html"

echo "[STEP 6] 배포 시작: $DATE"

# 1. 출력 파일 존재 확인
if [ ! -f "$OUTPUT_FILE" ]; then
  echo "ERROR: 리포트 파일 없음: $OUTPUT_FILE" >&2
  exit 1
fi

# 2. docs 디렉토리 생성 및 복사
mkdir -p "$DOCS_DIR"
cp "$OUTPUT_FILE" "$DOCS_DIR/"
echo "  복사 완료: $OUTPUT_FILE → $DOCS_DIR/"

# 3. index.html 갱신 (docs/ 내 HTML 파일 최신순 목록)
PYTHON="${PYTHON:-/c/Users/capri/AppData/Local/Programs/Python/Python38-32/python.exe}"
# Git Bash /c/... 경로 → Python용 Windows 경로 변환
DOCS_DIR_WIN=$(cygpath -w "$DOCS_DIR")
INDEX_FILE_WIN=$(cygpath -w "$INDEX_FILE")

"$PYTHON" - "$DOCS_DIR_WIN" "$INDEX_FILE_WIN" <<'PYEOF'
import sys, re
from pathlib import Path

docs_dir = Path(sys.argv[1])
index_file = Path(sys.argv[2])

reports = sorted(docs_dir.glob("*_report.html"), reverse=True)

items = ""
for r in reports:
    date_match = re.match(r'(\d{4}-\d{2}-\d{2})_report', r.name)
    date_str = date_match.group(1) if date_match else r.name
    items += f'<li><a href="{r.name}">{date_str} 리포트</a></li>\n    '

html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>카카오 채팅 분석 리포트 목록</title>
  <style>
    body {{ font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
           max-width: 600px; margin: 60px auto; padding: 0 16px; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 24px; }}
    ul {{ list-style: none; padding: 0; }}
    li {{ padding: 12px 0; border-bottom: 1px solid #E2E8F0; }}
    a {{ color: #3B82F6; text-decoration: none; font-weight: 600; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>카카오 채팅 분석 리포트</h1>
  <ul>
    {items}
  </ul>
</body>
</html>"""

index_file.write_text(html, encoding='utf-8')
print("  index.html 갱신 완료")
PYEOF

# 4. git push
cd "$REPO_DIR"
git add docs/
if git diff --cached --quiet; then
  echo "  변경사항 없음 — 이미 최신 상태"
else
  git commit -m "report: $DATE"
  git push origin main
fi

echo "[STEP 6] 배포 완료"
