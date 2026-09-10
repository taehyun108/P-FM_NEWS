# 전체 로직 다이어그램

`backend/main.py` 한 파일의 동작을 그림으로. 상세 설명은
[ARCHITECTURE.html](ARCHITECTURE.html), 운영은 [RUNBOOK.md](RUNBOOK.md).

> GitHub 에서 이 파일을 열면 다이어그램이 렌더된다.

---

## 1. 시스템 구성 — 프로세스 1개, 스레드 4개

```mermaid
flowchart TB
    subgraph PC["로컬 PC · python backend/main.py run (프로세스 1개)"]
        API["FastAPI / uvicorn :8000\n· 대시보드 HTML+JS+CSS (/)\n· /api/* JSON\n· /healthz · /api/web/* · /kakao/callback"]
        TA["스레드 A · 수집 루프 (300초)\nrun_once → 알림 큐 → 주간레포트 확인"]
        TB["스레드 B · 시세 갱신 (60초)"]
        TC["스레드 C · 텔레그램 봇 (getUpdates 롱폴링)"]
        TD["스레드 D · 대외협력 수집 (하루 2회 · 9·15시 KST)"]
    end

    API & TA & TB & TC & TD -->|읽기·쓰기| SB[("Supabase\nPostgres · 유일한 상태")]

    TA -->|수집| GN["Google News RSS"]
    TA -->|수집| NV["NAVER 검색 API"]
    TA -->|요약·SWOT·분류| OAI["OpenAI\nchat + embeddings"]
    TA -->|알림| TG["Telegram Bot API"]
    TB -->|시세| MKT["네이버 증시"]
    TC <-->|명령·질의| TG
    TA -->|월요일 07시| SMTP["Gmail SMTP\n주간 레포트"]
    TD -->|입법·행정예고·의안| GOV["lawmaking.go.kr\nassembly.go.kr\ndata.go.kr(KOTRA)"]
    TD -->|영향도 분석| OAI

    BR["브라우저 / 폰\n(Tailscale 등)"] -->|HTTPS| API

    classDef ext fill:#eef2f7,stroke:#9bb
    class GN,NV,OAI,TG,MKT,SMTP,GOV ext
```

**핵심:** 저장소는 SQLite(로컬 개발·사내 배포) 또는 Supabase(현재), `DB_BACKEND` 로 전환.
어느 쪽이든 `Storage` ABC + 두 구현이 **동일 스키마**를 유지한다
(`schema_sqlite.sql` + `init_schema()` 의 `alter table` 리스트 ↔ `schema.sql`).
PC 프로세스는 재시작해도 잃을 게 없다. → 인스턴스는 **반드시 1개** (2개면 알림 중복).

---

## 2. 수집 파이프라인 — `run_once()` (300초마다)

```mermaid
flowchart TB
    START(["수집 시작"]) --> FETCH["소스 조회\nGoogle News RSS + NAVER 검색"]
    FETCH --> G0["G0 · 실행 내 중복 제거"]
    G0 --> G1["G1 · seen-set 캐시 (메모리)"]
    G1 --> G2["G2 · 전체 기간 DB 대조\n(articles + alias + url_ledger)"]
    G2 --> G25["G2.5 · 신선도 컷오프\n(발행 72h 초과 = 저장 안 함, 비용 0)"]
    G25 --> CAP{"이번 회차\n처리 한도 24건?"}
    CAP -->|초과분| DEFER["메타만 저장 (deferred)\n비용 0 · 다음 회차 본문 확보"]
    CAP -->|24건 이내| G3["G3 · 리다이렉트 해제 + HTML 확보 (병렬)"]
    G3 --> G4["G4 · 제목·본문·메타 추출\n(G3 응답 재사용, 추가 요청 0)"]
    G4 --> RELV{"관련성\n포스코/그룹사/배터리 생태계?"}
    RELV -->|무관| DROP["폐기"]
    RELV -->|통과| G5["G5 · 중복 판정 4단계\n① 정규화 URL ② 본문 해시\n③ 제목 문자열 유사도 ④ 임베딩 의미 유사도"]
    G5 -->|중복| MERGE["기존 대표기사에 병합"]
    G5 -->|신규| SAVE["저장 + 규칙 기반 중요도 점수\n(score_article — LLM 아님)"]
    SAVE --> QUEUE["알림 큐 적재 판정 → 3절"]
    SAVE --> G6["G6 · LLM 분석 (과금 지점)\nanalyze() : 요약·관점·키워드·그룹사·감성·SWOT\n일일 상한 LLM_DAILY_LIMIT"]
    G6 --> ENRICH["summaries · swot_analyses 저장\n중요도 점수 상향 가능 (max)"]

    DRAIN["_drain_deferred (회차당 16건)\ndeferred → 본문 확보 → 관련성 재검 → G6"] -.-> G6

    classDef gate fill:#e8f0fe,stroke:#5a7
    class G0,G1,G2,G25,G3,G4,G5,G6 gate
```

**분석(G6)이 밀리거나 예산이 소진돼도** 수집·저장·알림은 계속된다. 요약·SWOT 만
빠진 채로 저장되고, 나중에 소급 분석된다. 본문은 **분석이 끝나면 즉시 삭제**.

---

## 3. 알림 판정 — 저장과 발송은 별개

### 3-1. 큐 적재 (`run_once` 안에서, 저장 직후)

```mermaid
flowchart TB
    A(["저장된 기사"]) --> X{"제목에 '제외할 키워드'?\n(마스터 4번)"}
    X -->|있음| SKIP0["무조건 웹에만 (skipped) · 최우선"]
    X -->|없음| B{"억제 모드 아님?\n(부트스트랩·복구)"}
    B -->|아니오| SKIP1["queued 안 함 (skipped)"]
    B -->|예| C{"발행 6시간 이내?\n(is_backfill 아님)"}
    C -->|아니오| SKIP2["웹에만 노출"]
    C -->|예| D{"주제 토글 통과?\n(정책·통상은 마스터가 켜야)"}
    D -->|아니오| SKIP3["웹에만"]
    D -->|예| E{"중요도 ≥ 임계값(기본 50)\nOR 우선 기사?"}
    E -->|아니오| SKIP4["큐에 skipped 로 남음"]
    E -->|예| F{"발행시각 ≥ 부트스트랩 시각?"}
    F -->|아니오| SKIP5["과거 기사 — 절대 발송 안 함"]
    F -->|예| Q["알림 큐 queued"]

    classDef q fill:#dcfce7,stroke:#4a4
    class Q q
```

- `우선 기사` = 마스터의 **무조건 받을 키워드**가 제목·판정 그룹사에 있음
  → 임계값을 우회. 야간 우회는 **'야간에도 즉시 발송' 체크**(`always_kw_bypass_night`, 기본 켜짐)가
  켜져 있을 때만 — 끄면 우선 기사도 야간엔 아침까지 대기
- `제외할 키워드` 는 임계값·우선 여부를 **모두 이긴다** — 제목에 있으면 무조건 웹에만.
  `run_once` 와 `_send_notifications` 양쪽에서 검사
- **정책·통상 주제**: ① 관심 키워드와 ② 필수 공통 키워드가 **둘 다** 매칭돼야 하고
  (한쪽이라도 비면 발송 안 함), 제목에 그 주제의 ✕ 제외 키워드가 없어야 한다

### 3-2. 실제 발송 (`_send_notifications` — 파이프라인 회차마다)

```mermaid
flowchart TB
    P(["큐의 queued 기사\n(중요도 내림차순)"]) --> PAUSE{"텔레그램 발송 토글 ON?\n(notify_paused)"}
    PAUSE -->|OFF| HOLD1["큐에 그대로 (다시 켜면 발송)"]
    PAUSE -->|ON| EXCL{"제목에 '제외할 키워드'?\n(큐 적재 후 추가됐을 수 있음)"}
    EXCL -->|있음| DROP2["이번 회차 제외"]
    EXCL -->|없음| FLOOD{"텔레그램 flood 대기 중?\n(429 retry_after)"}
    FLOOD -->|대기| HOLD2["이번 회차 건너뜀"]
    FLOOD -->|아니오| TH{"중요도 ≥ 현재 임계값\nOR 우선?"}
    TH -->|아니오| HOLD3["큐에 남음"]
    TH -->|예| NIGHT{"지금 야간?\n_in_night_window(서울시간)"}
    NIGHT -->|예| NMIN{"중요도 ≥ 야간 최소 점수(기본 80)\nOR (우선 AND '야간에도 즉시' 체크)?"}
    NMIN -->|아니오| MORNING["아침까지 큐 대기"]
    NMIN -->|예| SEND
    NIGHT -->|아니오| SEND["개별 카드 발송\n요약·관점·SWOT·링크\n(3.5초 간격·분당 17·회차당 12)"]
    SEND --> LOG["telegram_log 기록\n(발송 이유 포함)"]

    classDef send fill:#dcfce7,stroke:#4a4
    class SEND,LOG send
```

야간 창·최소 점수는 **마스터 패널 3번** 에서 조정 (`run_state`, 없으면 `.env NIGHT_*`).
최소 점수 **101** = 야간 전면 차단(우선 기사만).

---

## 4. LLM 호출 지도 — 어떤 함수가 어떤 모델을 쓰나

```mermaid
flowchart LR
    subgraph CHAT["채팅 모델 · LLM_MODEL (gpt-5.6-luna)"]
        AN["analyze()\n기사 분석 (G6)"]
        PN["people_notice()\n인사·부고 구조화"]
        EA["_ea_chat()\n대외협력 영향도"]
        WB["weekly_brief()\n주간 레포트 섹션"]
        CT["chat_text()\n텔레그램 봇 Q&A"]
    end
    subgraph EMB["임베딩 모델 · EMBEDDING_MODEL (text-embedding-3-small)"]
        EM["embed()\n중복 판정 G5 4단계"]
    end

    KEY["OPENAI_API_KEY\n(유일한 과금 키)"] --> CHAT
    KEY --> EMB

    AN -.->|~335건/일 · 비용 93%| COST["월 ≈ $14"]
    PN -.->|~14건/일| COST
    EA -.->|~10건/일| COST
    WB -.->|~10건/주| COST
    CT -.->|0건| COST
    EM -.->|~220건/일 · 월 ~$0.01| COST

    classDef free fill:#eef7ee,stroke:#4a4
    class EMB,EM free
```

사내 키로 대체 시: 채팅은 `LLM_BASE_URL` 지원 추가로 라우팅 가능(코드 ~20줄),
임베딩은 별도 판단 — [RUNBOOK §3](RUNBOOK.md#3-사내-api-키로-교체-비용-절감).

---

## 5. 데이터 수명주기

```mermaid
flowchart LR
    NEW["신규 기사"] --> BODY["article_bodies\n(본문 임시)"]
    BODY -->|G6 분석 완료| DEL1["본문 즉시 삭제\n(저작권·최소보관)"]
    NEW --> EMB2["title_embedding\n(중복 판정용)"]
    EMB2 -->|48시간 경과| DEL2["임베딩 비움\n(용량 회수)"]
    NEW --> ROW["articles 행\n(제목·요약·점수·태그)"]
    ROW -->|핵심 기사\n알림대상·고중요도·그룹사| KEEP550["550일 보관\n(≈18개월)"]
    ROW -->|잡음 기사| KEEP90["90일 보관"]
    LEDGER["url_ledger"] -->|180일| DELL["삭제"]
    LOGS["collection_logs · telegram_log"] -->|30~90일| DELG["삭제"]
```

Supabase 무료 500MB 한도 대비. `ARTICLE_RETENTION_DAYS` 로 조정 (늘리면 용량도 비례).

---

## 6. 배포 형태별 (참고)

```mermaid
flowchart TB
    subgraph NOW["현재 · 로컬 PC"]
        L["python main.py run\n+ Tailscale (폰 접속)"]
    end
    subgraph ALT["대안"]
        K["Koyeb 무료\n(Dockerfile 자동, 256MB 주의)"]
        V["소형 VM\n(Lightsail/Oracle, systemd)"]
    end
    NOW -.->|"PC 못 켤 때"| ALT
    L & K & V --> SB[("Supabase (공통)")]
```

Vercel·serverless 는 **불가** — 상주 스레드(수집·봇)를 못 돌린다.
자세히는 [DEPLOY.md](DEPLOY.md).
