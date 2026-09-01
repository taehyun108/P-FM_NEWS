#!/usr/bin/env bash
# PRD 기반 스킬을 전역(~/.claude/skills)에 설치한다.
# 사용법:  bash skills/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${HOME}/.claude/skills"

mkdir -p "$DEST"

for dir in "$SRC"/*/; do
  name="$(basename "$dir")"
  [ -f "$dir/SKILL.md" ] || continue          # SKILL.md 없는 디렉터리는 건너뛴다
  rm -rf "${DEST:?}/${name}"                  # 기존 설치본 제거 후 갱신
  cp -R "$dir" "$DEST/$name"
  echo "installed: $DEST/$name"
done

echo
echo "완료. 새 Claude Code 세션부터 스킬이 인식됩니다."
