#!/usr/bin/env bash
# demo-video 스킬 최초 1회 설치 스크립트.
#   1) 러너에 필요한 npm 패키지 설치 (playwright / yaml / ffmpeg-static)
#   2) Playwright 크로미움 브라우저 바이너리 설치
#
# 사용법: bash scripts/setup.sh   (스킬 폴더 어디서 실행해도 됨)
set -euo pipefail
cd "$(dirname "$0")/.."   # 이 스크립트 기준 스킬 루트(SKILL.md 와 같은 위치)로 이동

echo "▶ npm 패키지 설치: playwright, yaml, ffmpeg-static"
npm install playwright yaml ffmpeg-static

echo "▶ Playwright 크로미움 브라우저 설치"
if ! npx --yes playwright install chromium --with-deps; then
  echo "⚠ --with-deps 설치가 실패했습니다(권한 문제일 수 있음). OS 라이브러리 없이 브라우저만 다시 설치합니다."
  npx --yes playwright install chromium
fi

echo "✅ 설치 완료."
echo "   테스트: node scripts/run_scenario.js --scenario scripts/fixture/example-scenario.yaml"
