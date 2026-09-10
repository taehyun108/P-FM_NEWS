# 운영 런북 — 사내 로컬 PC

이 문서는 **① 사내 PC로 처음 옮길 때** 와 **② 코드를 업데이트할 때마다** 무엇을
확인해야 하는지 정리한 것이다. 클라우드 배포는 [DEPLOY.md](DEPLOY.md) 를 본다.

> 한 줄 요약: 이 시스템은 **`python backend/main.py run` 프로세스 1개**가
> API 서버 + 수집·시세·텔레그램봇·대외협력 스레드를 다 돌린다.
> 저장소는 `DB_BACKEND` 로 고른다 — **`sqlite`**(사내 배포·오프라인) 또는 **`supabase`**(현재).
> 로컬 PC는 상태를 갖지 않는다(SQLite면 `backend/pfm_news.db` 파일이 상태).

## 저장소 — SQLite 와 Supabase 동시 유지

`Storage` ABC + `SqliteStorage` / `SupabaseStorage` 두 구현이 **같은 스키마**를 유지한다.
스키마를 바꾸는 커밋은 **항상 세 곳을 같이** 고친다:

| 파일 | 역할 |
|---|---|
| `backend/schema_sqlite.sql` | SQLite `create table` (신규 DB) |
| `backend/main.py` `SqliteStorage.init_schema()` 의 `migrations` 리스트 | 기존 SQLite DB 에 `alter table` (자동 실행) |
| `backend/schema.sql` | Supabase `create table` + 기존 프로젝트용 `alter table` 안내 |

**사내 배포에서 `DB_BACKEND=sqlite` 를 쓸 때** 스키마 변경 반영:
```bash
python backend/main.py initdb      # 스키마 갱신 (idempotent — 여러 번 돌려도 안전)
```
`initdb` 가 `create table if not exists` + `alter table … (있으면 무시)` + 시드 upsert 를
수행한다. 데이터는 그대로. Supabase 쪽은 `alter table` 을 SQL Editor 에서 1회 (§2-3).

---

## 1. 최초 1회 — 사내 PC 세팅

### 1-1. 준비물

| 항목 | 값 / 확인 |
|---|---|
| Python | **3.12.x** (`python --version`) |
| 저장소 | `git clone` (private → deploy key 또는 PAT 필요) |
| 사내망 → 외부 도달 | 아래 호스트에 HTTPS 가능해야 함 |

**앱이 나가는 외부 호스트** — 사내 방화벽/프록시 화이트리스트에 필요:

```
*.supabase.co              # DB (필수)
api.openai.com             # LLM (또는 사내 대체 엔드포인트)
api.telegram.org           # 알림
openapi.naver.com          # 뉴스 검색
news.google.com            # 뉴스 수집 (RSS)
smtp.gmail.com:587         # 주간 레포트 메일
api.smith.langchain.com    # LangSmith 추적 (선택)
apis.data.go.kr            # KOTRA (대외협력, 선택)
*.lawmaking.go.kr / *.assembly.go.kr   # 대외협력 크롤링 (선택)
```

사내 프록시가 있으면 `.env` 또는 시스템 환경변수에:
```
HTTPS_PROXY=http://proxy.내회사:8080
HTTP_PROXY=http://proxy.내회사:8080
NO_PROXY=localhost,127.0.0.1,.supabase.co   # 필요 시 조정
```
`requests` 와 `openai`(httpx) 둘 다 이 표준 변수를 읽는다.

### 1-2. 설치

```bash
cd P-FM_NEWS
python -m venv .venv
.venv\Scripts\activate            # (PowerShell: .venv\Scripts\Activate.ps1)
pip install -r backend/requirements.txt
```

### 1-3. `.env` 만들기

```bash
copy .env.example .env
```
그리고 `.env` 를 채운다. **값이 필요한 키 목록과 역할**은 [§4 키 카탈로그](#4-api-키-카탈로그) 참고.
최소 필수: `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_ANON_KEY`, `DB_BACKEND=supabase`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`MASTER_PASSWORD`, `APP_TZ_OFFSET=9`.

> `.env` 는 **절대 커밋하지 않는다** (`.gitignore` 에 있음). 사내 PC에서도 마찬가지.

### 1-4. 검증 → 실행

```bash
python backend/main.py selftest        # 399건 전부 통과해야 함 (DB·키 불필요)
python backend/main.py once 3          # 파이프라인 1회, LLM 3건만 (연결 확인)
python backend/main.py run             # 운영 모드
```
`run` 후 로그에 `저장소: Supabase` → `수집 루프 시작` → `서버: http://127.0.0.1:8000`
이 뜨고, 5분 안에 `완료: 신규 N …` 이 찍히면 정상.

### 1-5. 상시 가동 (PC를 켜 둘 때)

1. **절전·최대 절전 끄기** — 제어판 → 전원 옵션 → "절대 안 함". 잠들면 수집이 멈춘다.
2. **자동 시작** — 작업 스케줄러에 "시스템 시작 시" 트리거로
   `<경로>\.venv\Scripts\python.exe backend\main.py run` 등록. 또는 NSSM 으로 서비스화.
   (크래시·재부팅 자동 복구)
3. **Windows Update 활성 시간** 을 업무 시간으로 설정 → 새벽 자동 재시작 방지.
4. 폰에서 대시보드를 보려면 **Tailscale**(무료) 을 PC·폰에 설치 → `http://<PC이름>:8000`.

---

## 2. ⭐ 업데이트할 때마다 체크리스트 (`git pull` 후)

> 이 순서대로만 하면 된다. 대부분은 1·5·6만 해당된다.

| # | 확인 | 명령 / 방법 |
|---|---|---|
| 1 | 코드 받기 | `git pull` |
| 2 | **의존성 변경?** `backend/requirements.txt` 가 diff 에 있으면 | `pip install -r backend/requirements.txt` |
| 3 | **스키마 변경?** `backend/schema*.sql` 가 diff 에 있으면 | **SQLite**: `python backend/main.py initdb` (idempotent). **Supabase**: 바뀐 `alter table` 을 SQL Editor 에서 1회. 최근: `run_state` 에 `night_start_hour·night_end_hour·night_min_score·exclude_notify_keywords` 추가 |
| 4 | **새 환경변수?** `.env.example` 이 diff 에 있으면 | 새로 생긴 줄을 `.env` 에도 추가 (값이 필요하면 채움) |
| 5 | **검증** | `python backend/main.py selftest` → `전체 통과` 확인 |
| 6 | **서버 재시작** | 기존 `run` 프로세스 종료 후 다시 `python backend/main.py run` |
| 7 | **1사이클 로그 확인** | 5분 안에 `완료: …` 이 뜨고 `[ERROR]` 가 없는지 |

### 커밋 메시지에서 3·4 를 놓치지 않으려면

`git log --oneline -- backend/schema.sql .env.example` 로 마지막으로 본 시점 이후
변경이 있었는지 빠르게 확인. `schema.sql` 이나 `.env.example` 이 바뀐 커밋은
메시지에 보통 `⚠️ Supabase … alter table` 또는 `.env` 안내가 들어 있다.

---

## 3. 사내 API 키로 교체 (비용 절감)

**비용이 드는 키는 `OPENAI_API_KEY` 하나뿐이다.** (자세한 근거는 §4)
실측 월 비용 ≈ **$14** (하루 ~350건 분석 × $0.00133).

### 3-1. 지금 상태

코드는 `openai.OpenAI(api_key=...)` 만 호출한다 — **`base_url` 설정이 없어서
무조건 `api.openai.com` 으로 간다.** 사내 엔드포인트로 돌리려면 코드 수정이 필요하다:

- `_make_openai_client()` 에 `base_url` 인자 추가
- `Config` 에 `LLM_BASE_URL` (채팅용), `EMBEDDING_BASE_URL` + `EMBEDDING_API_KEY` (임베딩 별도)
- `.env.example` 반영

→ 필요 시 이 변경을 요청할 것. (약 20줄)

### 3-2. 교체 시 판단표

| 사내 게이트웨이 형태 | 채팅 | 임베딩 |
|---|---|---|
| **OpenAI 호환** (사내 Azure OpenAI 프록시 등) | `LLM_BASE_URL` + 사내 키 + `LLM_MODEL` 을 사내 모델명으로. 5개 함수 그대로 동작 | 사내가 임베딩도 주면 `EMBEDDING_BASE_URL` 추가. **차원이 1536 이 아니면** 기존 저장 임베딩 무효화(하루면 재구축, 크래시 없음) |
| **비호환 / 임베딩 미제공** | OpenAI 호환 어댑터가 있으면 위와 동일 | **작은 OpenAI 키를 임베딩 전용으로 유지**(월 ~$0.01) 하거나 4단계 중복 판정 포기 |

### 3-3. 그대로 두는 키

`NAVER_*`(검색 API·무료), `LANGSMITH_API_KEY`(관측·무료), `SMTP_*`(메일·무료),
`SUPABASE_*`(고정 요금), `TELEGRAM_*`(무료), `EA_KOTRA_SERVICE_KEY`(공개 데이터·무료).

---

## 4. API 키 카탈로그

### 💰 비용 발생 + 모델 사용 — `OPENAI_API_KEY`

**채팅 모델** = `LLM_MODEL` (현재 `gpt-5.6-luna`, $0.20 / $1.20 per 1M)

| 함수 | 역할 | 최근 7일 실측 |
|---|---|---|
| `analyze()` | 기사 요약·포스코 관점·키워드·그룹사 판정·감성·SWOT (파이프라인 G6 + URL 수동 등록) | ~335건/일 · **비용의 93%** |
| `people_notice()` | 인사·부고를 사람별 구조로 (부처·고시회차·이력·업무·학력) | ~14건/일 |
| `_ea_chat()` | 대외협력 입법·행정예고 포스코 영향도 분석 | ~10건/일 (상한 50) |
| `weekly_brief()` | 주간 레포트 섹션 합성 (그룹사 SWOT·정책/통상 영향) | ~10건/주 |
| `chat_text()` | 텔레그램 봇 자연어 질의응답 | 0건 (미사용) |

**임베딩 모델** = `EMBEDDING_MODEL` (현재 `text-embedding-3-small`, $0.02 per 1M)

| 함수 | 역할 | 실측 |
|---|---|---|
| `embed()` | 중복 판정 G5 4단계 — 제목 의미 유사도 | ~220건/일 · **월 ~$0.01 (사실상 공짜)** |

비용 통제: `.env LLM_DAILY_LIMIT`(하루 분석 건수 상한, 기본 1500) +
OpenAI 대시보드 월 하드 리밋. `$X/월` 로 묶으려면 `LLM_DAILY_LIMIT ≈ (X ÷ 30 ÷ 0.00133)`.
큐가 중요도 내림차순이라 예산은 **중요한 기사부터** 쓰인다. 한도 초과해도 크래시 없이
"제목+링크만" 알림으로 저하되고, 다음 날/다음 달 소급 분석된다.

### 🆓 비용 없음 / 모델 없음 — 그대로 유지

| 키 | 역할 | 비용 |
|---|---|---|
| `SUPABASE_URL`·`SUPABASE_SERVICE_ROLE_KEY`·`SUPABASE_ANON_KEY` | 기사·설정·로그 저장(DB) | 고정 요금제 |
| `TELEGRAM_BOT_TOKEN`·`TELEGRAM_CHAT_ID` | 알림 발송 + 봇 수신 | 무료 |
| `NAVER_CLIENT_ID`·`NAVER_CLIENT_SECRET` | 뉴스 검색 API (수집 보강) | 무료 25,000회/일 |
| `LANGSMITH_API_KEY` | LLM 호출 추적·관측 | 무료 티어 |
| `SMTP_USER`·`SMTP_APP_PASSWORD` | 주간 레포트 메일 발송 | 무료 (Gmail 앱 비밀번호) |
| `EA_KOTRA_SERVICE_KEY` | KOTRA 해외시장뉴스 (대외협력 통상 탭) | 무료 (공개 데이터) |
| `MASTER_PASSWORD` | 마스터 패널 인증 | — |
| `WEB_PASSWORD` | 사이트 접속 잠금 (비우면 잠금 없음) | — |
| `KAKAO_*` | **비활성** (`KAKAO_ENABLED=false`) | — |

---

## 5. 자주 쓰는 운영 명령

| 명령 | 용도 |
|---|---|
| `python backend/main.py run` | 운영 모드 (서버 + 모든 스레드) |
| `python backend/main.py selftest` | 순수 함수 검증 (DB·키 불필요) |
| `python backend/main.py once [N]` | 파이프라인 1회 (N = LLM 호출 상한, 검증용) |
| `python backend/main.py notify` | 대기 중인 알림만 발송 |
| `python backend/main.py retryfailed` | 발송 실패로 막힌 알림을 큐로 되돌림 |
| `python backend/main.py weekly --dry` | 주간 레포트 생성만 (메일 없음) |
| `python backend/main.py sendtest` | 텔레그램 연결 확인 |
| `python backend/main.py ea-collect` | 대외협력 즉시 수집·분석 |

일회성 정리(규칙 바꿨을 때 소급 적용): `fixcategories` · `regroup` · `repeople [N]` ·
`reanalyze` · `reswot` · `fixpress` 등 — `--help` 참고.

---

## 6. 문제 생겼을 때

| 증상 | 원인 / 조치 |
|---|---|
| 야간(23~7시)에 알림이 옴 | 정상 — 중요도 `NIGHT_MIN_SCORE`(기본 80) 이상·우선 기사는 야간에도 발송. **완전 무음**은 마스터 패널 3번에서 최소 점수를 **101** 로 |
| 야간 시간이 안 맞음 | `APP_TZ_OFFSET=9` 확인. 마스터 패널 3번에서 시각 조정 (서울시간 기준) |
| 특정 주제(채용·부고 등)가 계속 알림됨 | 마스터 패널 **4번 '제외할 키워드'** 에 그 단어 추가 (제목 기준). 임계값·'무조건 받을 키워드'보다 우선 |
| 마스터 패널 저장 시 `이 설정용 컬럼이 아직 없습니다` | Supabase 스키마 미반영 → §2-3 의 `alter table` 실행 (SQLite 는 자동) |
| 알림에 요약·SWOT 이 없고 제목만 옴 | OpenAI 한도 소진 or 키 오류. `[ERROR] LLM 분석` 로그 확인. 한도 리셋되면 소급 분석됨 |
| 발송 실패 다수 (`Too Many Requests`) | 텔레그램 rate limit. `retryfailed` 로 복구. 자동 회복도 됨 |
| `SystemExit: 환경변수가 비어 있습니다` | `.env` 필수 키 누락 |
| `PGRST … column … does not exist` | Supabase 스키마 미반영 → §2-3 |
| 대시보드가 옛날 기사만 | Supabase 연결 실패 or service_role 키 오류. 로그 확인 |
| 서버는 뜨는데 폰에서 안 보임 | 사내망 방화벽 / Tailscale 미연결 |

---

## 7. 절대 하지 말 것

- **`.env` 커밋** — 키가 git 이력에 영구 잔존한다.
- **인스턴스 2개 실행** — 수집·LLM·알림이 두 벌 나간다. `run` 은 PC당 1개만.
- **`DB_BACKEND=sqlite` 로 되돌리기** (운영 중) — Supabase 데이터와 분리된다.
- **한글 소스 파일을 PowerShell 로 편집** — 인코딩 깨짐. 에디터로만.
- **커밋/푸시를 요청 없이** — 작업 브랜치는 `claude/prd-skills-setup-yqehgi`.
