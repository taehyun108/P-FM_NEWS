# backend

수집 · 분석 · API · 시세를 담당한다. **외부 API 호출과 Supabase 쓰기는 전부 이 계층에서만 일어난다.**

## 담당 범위 (PRD 대응)

| 파일 | 역할 | PRD |
|---|---|---|
| `main.py` | 전체 파이프라인 + API 서버 (파이썬 단일 파일, CLAUDE.md 규약) | F1~F5, F7~F9 |
| `schema.sql` | Supabase 테이블 · 인덱스 · RLS 정의 | F5 |
| `.env` (루트) | 모든 키. 커밋 금지 | §6 보안 |

## 원칙

1. `OPENAI_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `TELEGRAM_BOT_TOKEN`은 **backend 밖으로 나가지 않는다.** 프론트엔드 번들에 절대 포함하지 않는다.
2. 외부 HTTP 요청은 수집 게이트(F1.1 G2·G2.5)를 통과한 항목에만 발생시킨다.
3. 새 파이썬 라이브러리는 사용 전 사용자에게 확인한다 (CLAUDE.md 규약).

> 코드는 PLAN.md 검증 통과 후 작성한다. 현재는 구조만 잡힌 상태다.
