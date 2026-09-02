---
name: env-secret-guard
description: API 키·토큰을 .env 환경변수로만 다루는 시크릿 관리 규약과 점검 절차. Use when handling API keys, tokens, service_role keys, or any credential — creating .env / .env.example, adding .gitignore rules, checking for hardcoded secrets, or when a key might have been committed or exposed in a client bundle.
---

# 시크릿 관리 규약 (.env 전용)

## 1. 절대 규칙

1. **모든 키는 `.env`에서 읽는다.** 코드·설정 파일·주석·문서·커밋 메시지에 키 값을 직접 쓰지 않는다.
2. **`.env`는 절대 커밋하지 않는다.** `.gitignore`에 먼저 넣고 파일을 만든다(순서가 중요).
3. **`.env.example`의 값은 비워 둔다.** 실제 값은 사용자가 직접 채운다. 예시라며 진짜처럼 보이는 값을 넣지 않는다.
4. 키가 없으면 **시작 시점에 명확히 실패**시킨다. 조용히 `None`으로 진행하다 런타임에 터지게 두지 않는다.
5. 로그·에러 메시지·예외 스택에 키를 출력하지 않는다.

## 2. .env.example 형태

```dotenv
# LLM
OPENAI_API_KEY=

# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=     # 서버 전용. 클라이언트 노출 금지
SUPABASE_ANON_KEY=             # 브라우저용

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

- 키 이름·주석·그룹은 채우되 **`=` 뒤는 비운다.**
- 각 키가 어디서 발급되는지 한 줄 주석을 남기면 사용자가 채우기 쉽다.

## 3. .gitignore

```gitignore
.env
.env.*
!.env.example
```

## 4. 코드에서 읽는 방식

```python
import os

def require_env(name: str) -> str:
    """필수 환경변수를 읽는다. 없으면 즉시 중단한다."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"환경변수 {name} 가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return value

OPENAI_API_KEY = require_env("OPENAI_API_KEY")
```

- 필수 키는 **프로그램 시작 직후 한 번에 전부 검증**한다.
- 선택 키는 기본값과 함께 `os.environ.get(name, default)`.
- 키를 함수 인자로 넘겨 다니지 말고 설정 객체 한 곳에서 관리한다.

## 5. 공개/비공개 키 구분

| 종류 | 노출 가능 | 예 |
|---|---|---|
| 서버 전용 | 절대 불가 | service_role 키, LLM API 키, 봇 토큰 |
| 클라이언트 공개 | 가능(RLS 전제) | Supabase anon 키, 공개 URL |

프론트엔드 프레임워크의 `NEXT_PUBLIC_`, `VITE_` 접두사는 **브라우저 번들에 그대로 들어간다.** 서버 전용 키에 이 접두사를 붙이는 것이 가장 흔한 유출 경로다.

## 6. 점검 절차

```bash
# 하드코딩된 키 흔적 탐색
grep -rInE '(sk-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,})' \
  --exclude-dir=.git --exclude-dir=node_modules .

# .env 가 추적되고 있지 않은지 확인
git ls-files | grep -E '^\.env'
```

- [ ] `.env`가 git에 추적되지 않는다.
- [ ] 빌드 산출물에 서버 전용 키가 없다.
- [ ] 키 누락 시 시작 단계에서 명확한 메시지와 함께 종료된다.

## 7. 이미 커밋했다면

파일을 지우는 것만으로는 해결되지 않는다(히스토리에 남는다).
1. **먼저 해당 키를 발급처에서 폐기·재발급한다.** 이것이 1순위.
2. 그 다음 히스토리 정리(`git filter-repo` 등)를 검토한다.
3. 순서를 바꾸지 않는다. 히스토리를 지우는 동안에도 유출된 키는 유효하다.
