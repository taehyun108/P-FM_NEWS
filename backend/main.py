"""
P-FM NEWS — 포스코 그룹 뉴스 수집 · 분석 · 아카이브 파이프라인

PRD.md v0.5 / PLAN.md 기준 구현.
실제 구현은 backend/app/ 패키지에 있고, 이 파일은 진입점 역할만 한다.

실행 방법
    python backend/main.py initdb     # 스키마 생성 + 시드 데이터 입력 (최초 1회)
    python backend/main.py once       # 파이프라인 1회 실행
    python backend/main.py serve      # API + 프론트엔드 서버
    python backend/main.py run        # 서버 + 1분 주기 수집 루프 (운영 모드)
    python backend/main.py selftest   # 내장 검증 (DB · 외부 API 불필요)

설계 원칙
    1. 네트워크 요청과 LLM 호출은 게이트 G2 · G2.5 를 통과한 항목에만 발생한다. (PRD F1.1)
    2. 모든 키는 .env 에서만 읽는다. 코드에 값을 쓰지 않는다.
    3. 저장소 계층을 분리해 SQLite ↔ Supabase 를 값 하나로 전환한다. (PLAN 0-4)

모듈 구성 (의존 방향: 위에서 아래로만)
    app/core.py      설정 · 로깅 · 공용 유틸 · HTTP 클라이언트 · Context
    app/storage.py   저장소 (SQLite / Supabase)
    app/collect.py   ① 뉴스 수집 · 본문 파싱 · 분류/점수 기준
    app/analyze.py   ② 필터링 · 중복판정 · LLM 분석 · 저장 파이프라인
    app/notify.py    ③ 텔레그램 · 카카오 알림 발송과 봇 루프
    app/view.py      카드 · 필터 · 주간 리포트 등 표시용 가공
    app/auth.py      웹 로그인 · 마스터 토큰
    app/web.py       FastAPI 라우트
    app/cli.py       운영 커맨드와 백그라운드 루프
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
