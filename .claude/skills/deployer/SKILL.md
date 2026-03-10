# Skill: deployer

## 역할
HTML 리포트를 /docs/에 복사하고 git push하여 GitHub Pages 배포.

## 스크립트
`.claude/skills/deployer/scripts/deploy.sh`

## 처리 순서
1. /output/YYYY-MM-DD_report.html → /docs/ 복사
2. docs/index.html 날짜별 링크 갱신 (최신순)
3. git add docs/
4. git commit -m "report: YYYY-MM-DD"
5. git push origin main

## 성공 기준
git push exit code 0 | docs/ 파일 존재 확인

## 실패 처리
push 실패 → 스킵+로그 (로컬 /output/ 항상 보존)
