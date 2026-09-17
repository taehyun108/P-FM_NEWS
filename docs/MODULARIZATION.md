# 백엔드 모듈 구조

```
backend/
  main.py              진입점만 (40줄) — Procfile·배포 스크립트 호환
  app/
    core.py            설정 · 로깅 · 공용 유틸 · HttpClient · Context
    storage.py         저장소 (SQLite / Supabase)
    collect.py         ① 뉴스 수집 · 본문 파싱 · 분류/점수 기준
    analyze.py         ② 필터링 · 중복판정 · LLM 분석 · 저장 파이프라인
    notify.py          ③ 텔레그램 · 카카오 발송과 봇 루프
    view.py            카드 · 필터 · 주간 리포트
    auth.py            웹 로그인 · 마스터 토큰
    web.py             FastAPI 라우트
    cli.py             운영 커맨드와 백그라운드 루프
  tests/
    test_selftest.py   내장 검증 (구 cmd_selftest)
```

| 파일 | 줄 수 | 역할 |
|---|---:|---|
| `storage.py` | 2,038 | SQLite / Supabase 구현체 |
| `analyze.py` | 2,010 | 파이프라인(`run_once`) · LLM · 중복판정 |
| `collect.py` | 1,744 | 수집 · 파싱 · 판정 규칙 |
| `web.py` | 1,183 | 라우트 44개 |
| `notify.py` | 1,101 | 알림 발송 · 봇 |
| `cli.py` | 981 | 운영 커맨드 18개 · 루프 · `main()` |
| `core.py` | 862 | 설정 · 유틸 · Context |
| `view.py` | 602 | 표시용 가공 · 주간 리포트 |
| `auth.py` | 245 | 로그인 · 토큰 |
| `tests/test_selftest.py` | 1,941 | 검증 542개 |

## 의존 방향 (위에서 아래로만 import)

```
core → storage → collect → view → notify → analyze → auth → web → cli
```

실제 파일의 import 문으로 확인한 관계:

| 모듈 | 가져다 쓰는 모듈 |
|---|---|
| `storage` | core |
| `collect` | core |
| `view` | collect, core |
| `notify` | collect, core, view |
| `analyze` | collect, core, notify, storage, view |
| `auth` | core, view |
| `web` | analyze, auth, collect, core, notify, view |
| `cli` | analyze, collect, core, notify, storage, view, web |

런타임 import 순환 **0건**.
