# PRD — POSCO Future M News Intelligence

**실시간 포스코 그룹·이차전지 뉴스 수집 / 요약 / 아카이브 / 텔레그램 알림 시스템**

| 항목 | 내용 |
|---|---|
| 문서 버전 | v0.4 (초안 — 전체 기간 재조회 차단, 신선도 게이트 G2.5 및 제외 원장 추가) |
| 작성일 | 2026-09-01 |
| 상태 | Draft — 검토 및 스코프 확정 필요 |
| 참고 자료 | `자동_뉴스_및_정보_수집_다이어그램.drawio`, 디자인 레퍼런스 `posco-future-m-intelligence.taehyun108484171.chatgpt.site` |

---

## 1. 배경 및 문제 정의

포스코퓨처엠 대외협력 업무에서는 그룹사·업계·지역 뉴스를 실시간으로 파악해야 하지만, 현재는 다음과 같은 문제가 있다.

- 포털·언론사를 수동으로 확인해야 하며, 확인 주기가 사람마다 다르다.
- 기사를 놓치면 대응이 늦어진다. 특히 지역 이슈·규제 이슈는 초동 대응 시점이 중요하다.
- 과거 기사를 다시 찾으려면 검색을 반복해야 하고, 링크가 만료되거나 유실된다.
- 기사 전문을 읽는 데 시간이 걸려, 실제로 봐야 할 기사와 그렇지 않은 기사의 선별 비용이 높다.

### 해결 방향

수집 → 중복 제거 → 분류 → LLM 요약 → 영구 저장 → (웹 아카이브 + 텔레그램 푸시)의 단방향 파이프라인을 자동화한다. 사람은 "확인"이 아니라 "판단"에만 시간을 쓴다.

---

## 2. 목표 / 비목표

### 2.1 목표 (In Scope)

| ID | 목표 |
|---|---|
| G1 | 포스코 그룹사 관련 신규 기사를 **1분 주기**로 탐지 |
| G2 | 기사별 **3~5줄 LLM 요약**을 생성하고 Supabase에 영구 저장 |
| G3 | 포토카드 형태의 웹 아카이브 제공 (필터·검색 포함) |
| G4 | 신규 기사 등록 시 **텔레그램으로 링크 + 요약 자동 발송** |
| G5 | 저작권을 침해하지 않는 범위(제목·출처·요약·링크)만 공개 |

### 2.2 비목표 (Out of Scope — v1 기준)

- 기사 본문 전문의 공개 게시 (저작권 리스크)
- 여론 분석·감성 분석 대시보드 (v2 후보)
- SWOT / 대응책 자동 생성 (v2 후보)
- 카카오톡 채널 배포 (v2 후보 — 비즈니스 채널 심사 필요)
- 다국어(영문) 요약

---

## 3. 사용자

| 페르소나 | 니즈 | 주 사용 채널 |
|---|---|---|
| 대외협력 담당자 (주 사용자) | 이슈 발생 즉시 인지, 원문 즉시 접근 | 텔레그램 |
| 팀 동료 / 유관부서 | 오늘·이번 주 이슈 훑어보기 | 웹 |
| 본인 (운영자) | 수집 상태·실패 건 모니터링 | 웹 (운영 섹션) |

---

## 4. 시스템 아키텍처

### 4.1 파이프라인 (drawio 워크플로우 반영)

```
[입력]                    [처리]                                      [출력]
┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ Google News  │   │ 1분 주기     │   │ 정규화 ·     │   │ LLM 요약     │   │ Supabase     │
│ RSS          │──▶│ Cron Trigger │──▶│ 중복제거 ·   │──▶│ (3~5줄)      │──▶│ (영구 저장)  │
│ Naver 뉴스 API│   │ (Worker)     │   │ 분류·스코어  │   │              │   │              │
└──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘   └──────┬───────┘
                                                                                   │
                                                          ┌────────────────────────┴───────┐
                                                          ▼                                ▼
                                                  ┌──────────────┐              ┌──────────────┐
                                                  │ 웹 포토카드  │              │ Telegram Bot │
                                                  │ 아카이브     │              │ 링크 + 요약  │
                                                  └──────────────┘              └──────────────┘
```

### 4.2 컴포넌트

| 컴포넌트 | 역할 | 기술(제안) |
|---|---|---|
| Collector | RSS/API 폴링, 원시 항목 적재 | Cloudflare Workers + Cron Triggers |
| Normalizer | URL 정규화, 중복 판정, 언론사 식별 | Worker 내 모듈 |
| Classifier | 그룹사/카테고리 태깅, 중요도 스코어 | 룰 기반 + LLM 보조 |
| Summarizer | 본문 추출 후 3~5줄 요약 | Claude API (Haiku/Sonnet) |
| Store | 기사·요약·발송 이력 | Supabase (Postgres) |
| Web | 포토카드 아카이브 | Next.js + Tailwind, Vercel |
| Notifier | 텔레그램 발송 큐 | Worker + Telegram Bot API |

> **아키텍처 선택 근거**: 1분 주기 스케줄이 핵심 제약이다. Vercel Cron은 플랜에 따라 분 단위 실행이 제한되고, GitHub Actions는 최소 5분에 실제 지연도 크다. Cloudflare Workers Cron Triggers는 `* * * * *`(1분) 실행이 가능하고 무료 티어에서도 동작한다. 대안으로 Supabase `pg_cron` + Edge Function 조합도 가능하며, 이 경우 인프라를 Supabase로 단일화할 수 있다.

---

## 5. 기능 요구사항

### F1. 뉴스 수집

**F1.1 수집 주기 및 "최신 기사" 판정**

*폴링 주기*
- 기본 60초. 환경변수로 조정 가능(30~300초).
- 각 실행은 등록된 모든 키워드 셋에 대해 소스 목록을 조회한다.

*신규 판정 — 커서를 쓰지 않는다*

Google News RSS·네이버 API 모두 "마지막 조회 이후 것만"을 요청하는 파라미터가 없다. 항상 최근 목록 전체를 반환한다. 따라서 발행 시각 커서(`published_at > last_cursor`) 방식은 사용하지 않는다. 다음 이유로 기사를 영구 누락시키기 때문이다.

- RSS 캐시 지연으로 기사가 목록에 뒤늦게 나타나지만, 그 기사의 `published_at`은 최초 발행 시각이다. 커서가 이미 그 시점을 지났으면 영원히 수집되지 않는다.
- 언론사 수정 기사, 분 단위 정보가 없는 소스 등으로 발행 시각 자체가 부정확하다.

대신 **저장소 대조(store comparison)** 로 판정하되, **비용이 낮은 단계를 먼저 통과시키는 순서**를 반드시 지킨다.

*게이트 단계 — 순서가 요구사항이다*

| 단계 | 처리 | 비용 | 이미 본 기사가 여기까지 오면 |
|---|---|---|---|
| G0 | 실행 내 중복 제거 (in-memory Set) | 0 | — |
| G1 | `url_source` 대조 (seen-set 캐시) | 메모리 조회 | 최근분은 여기서 탈락 |
| G2 | `url_source` 배치 DB 조회 (**전체 기간**) | 쿼리 1회 | **여기서 반드시 전부 탈락한다** |
| G2.5 | 신선도 컷오프 판정 (RSS `pubDate` 사용, 네트워크 불필요) | 0 | — |
| G3 | 리다이렉트 해제 → `url_canonical` 생성 | **HTTP 요청** | 도달하면 안 됨 |
| G4 | 본문 추출 | **HTTP 요청** | 도달하면 안 됨 |
| G5 | 중복 그룹 판정 (F2.2) | 쿼리 | |
| G6 | LLM 요약 (F4) | **API 호출·과금** | 도달하면 안 됨 |

**핵심 규칙: 네트워크 요청과 LLM 호출은 G2·G2.5를 통과한 항목에 대해서만 발생한다.** 한 번이라도 수집한 기사는 수집 시점이 언제였든 G1 또는 G2에서 탈락하므로, 재조회 비용은 0이다.

*판정 범위 — 전체 기간이며 만료되지 않는다*

"이미 가져온 기사인가"의 판정 권위는 **`articles` 테이블 전체**다. 보관 기간 제한이 없다.

| 계층 | 범위 | 역할 |
|---|---|---|
| G1 seen-set 캐시 | 최근 72시간 | 성능 최적화. 대부분의 항목을 메모리에서 즉시 탈락 |
| G2 DB 배치 조회 | **전체 히스토리 (무기한)** | 판정 권위. 캐시를 빠져나간 항목을 여기서 전부 차단 |
| `url_source` UNIQUE 제약 | 전체 히스토리 | 최종 방어선. 경합·재시도 시에도 중복 저장 불가 |

- 캐시 윈도를 72시간으로 잡은 이유는 검색 결과에 뜨는 기사 대부분이 최근분이라 적중률이 높기 때문이지, 그 이전 기사를 다시 수집한다는 뜻이 아니다.
- 캐시를 통째로 비워도 동작은 정확하다. 느려질 뿐이다.

*G2.5 신선도 컷오프와 영구 제외 원장*

컷오프를 넘겨 저장하지 않기로 한 기사는 `articles`에 남지 않으므로, 조치가 없으면 매 실행 G3까지 도달한다. 이를 막기 위해 다음을 둔다.

- 컷오프 판정은 RSS/API가 제공하는 발행 시각으로 수행한다. 네트워크가 필요 없으므로 G2.5는 비용 0이다.
- 발행 시각을 얻을 수 없거나, 본문 추출·요약이 영구 실패한 항목은 `url_ledger`에 `excluded`로 기록하고, 다음 실행부터 G2 조회 대상에 포함시켜 차단한다.
- `url_ledger`는 URL과 사유만 담는 경량 테이블이며 만료시키지 않는다.

*이중 키 구조 — 리다이렉트 해제를 게이트 뒤로 미룬다*

Google News는 원문 URL이 아니라 자체 리다이렉트 URL을 반환한다. 이 URL을 풀어야만 원문 URL을 알 수 있으므로, 원문 URL을 기준으로 신규 여부를 판정하면 **매 실행마다 모든 항목의 리다이렉트를 다시 풀어야 한다.** 이를 피하기 위해 키를 둘로 분리한다.

| 컬럼 | 값 | 용도 |
|---|---|---|
| `url_source` | 소스가 반환한 URL 원본 (Google News 리다이렉트 URL 포함), 추적 파라미터만 제거 | **신규 판정 키.** 네트워크 없이 계산 가능 |
| `url_canonical` | 리다이렉트 해제 후의 최종 원문 URL | 중복 제거·표시·링크용 |

- 두 컬럼 모두 UNIQUE 인덱스를 건다.
- `url_source`는 동일 기사에 대해 소스가 안정적으로 같은 값을 반환하므로 판정 키로 유효하다.
- 서로 다른 `url_source`가 같은 `url_canonical`로 해제되는 경우(키워드별 URL 상이 등)는 G5 중복 판정에서 걸러지고, 이때 두 `url_source`를 모두 `url_source_aliases`에 누적해 다음 실행부터는 G1에서 탈락시킨다.

*seen-set 캐시 (성능 계층)*
- 최근 72시간 내 수집분의 `url_source` 및 alias 집합을 Worker KV(또는 인메모리 + 주기 갱신)에 유지한다.
- 실행 시작 시 1회 로드, 신규 저장 시 즉시 추가한다.
- **캐시는 정확성에 관여하지 않는다.** 캐시 미스는 G2에서 전체 기간 대조로 처리되므로, 72시간 이전 기사가 재수집되는 일은 없다.

*배치 조회 (판정 권위)*
- 남은 항목은 개별 조회하지 않고 단일 쿼리로 처리한다. 조회 대상은 `articles` 전체 기간과 `url_ledger`다.

```sql
select url_source from articles       where url_source = any($1)
union all
select url_source from articles, unnest(url_source_aliases) as url_source
                                      where url_source = any($1)
union all
select url_source from url_ledger     where url_source = any($1);
```

- 실행당 DB 왕복은 조회 1회 + 저장 1회(bulk upsert)를 목표로 한다.

*수정 기사(리비전) 처리 — v1 범위 밖*
- 동일 URL의 기사가 나중에 수정되어도 v1에서는 재처리하지 않는다. 재처리하려면 매 실행마다 본문을 다시 받아 해시를 비교해야 하고, 이는 이 절의 목적(재조회 비용 제거)과 정면으로 충돌한다.
- 필요 시 v2에서 중요도 80 이상 기사에 한해 24시간 동안 1시간 간격으로만 리비전 확인하는 별도 저빈도 잡으로 분리한다. (§12 참조)

*검증 기준*
- 정상 운영 중 1회 실행에서 발생하는 **외부 HTTP 요청 수 = 소스 조회 수 + 신규 기사 수**여야 한다. 신규 기사가 0건인 실행의 추가 요청은 0이어야 한다.
- **회귀 테스트**: 임의의 과거 기사(수집 후 7일·30일·180일 경과)를 소스 응답에 강제 주입했을 때, 세 건 모두 G2에서 탈락하고 HTTP 요청이 0회여야 한다.
- `collection_logs`에 `skipped_seen_count`를 기록해 이 값을 상시 검증한다.
- `url_ledger.hit_count` 상위 항목을 주기 점검해, 반복 재등장하는 URL이 게이트를 통과하고 있지 않은지 확인한다.

*신선도 컷오프 — "최신"의 정의*

| 발행 경과 | 저장 | 텔레그램 알림 |
|---|---|---|
| 6시간 이내 | O | O (중요도 임계값 충족 시) |
| 6~72시간 | O (`is_backfill = true`) | X — 웹에만 노출 |
| 72시간 초과 | X (스킵) | X |

- 컷오프 값(6h / 72h)은 운영 설정으로 조정 가능하다.
- 뒤늦게 인덱싱된 기사가 "속보"처럼 알림되는 것을 막고, 동시에 웹 아카이브에서는 누락되지 않게 한다.

*최초 실행 및 장애 복구*
- 최초 부트스트랩은 **알림 억제 모드**로 동작한다. 과거 72시간치를 수집·저장하되 텔레그램 발송은 전건 `status = 'skipped'` 처리한다.
- 파이프라인이 30분 이상 중단된 뒤 재개될 때도 동일하게 억제 모드로 1회 실행한 후 정상 모드로 전환한다. 재개 직후 수십 건이 한꺼번에 발송되는 것을 방지한다.
- 모드 상태는 `run_state` 테이블로 관리한다.

**F1.2 수집 소스**

| 소스 | 방식 | 비고 |
|---|---|---|
| Google News RSS | `news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko` | 무인증. 단, 캐시로 인해 실제 반영에 수 분 지연 가능 |
| Naver 검색 API (뉴스) | 공식 Open API | **권장**. 직접 크롤링은 약관 위반 소지 |
| 언론사 자체 RSS | 개별 등록 | 지역지·업계지 커버 보강용 |

**F1.3 검색 키워드 셋** (DB `keyword_sets` 테이블로 관리, 코드 하드코딩 금지)

- **그룹사**: 포스코, 포스코홀딩스, 포스코퓨처엠, 포스코DX, 포스코인터내셔널, 포스코이앤씨, POSCO
- **지역/사업장**: 포항 포스코, 광양 포스코, 세종 포스코퓨처엠, 구미 양극재, 포항 이차전지, 광양 양극재
- **산업**: 이차전지, 배터리 소재, 양극재, 음극재, 전구체, 리튬, 니켈, 흑연, 전고체 배터리, 나트륨 배터리, LFP
- **정책/규제**: 이차전지 특화단지, IRA 배터리, 배터리 보조금, 핵심광물, 탄소중립 철강

> 지역 키워드는 사업장 위치 확정 후 확정한다(§11 미결정 사항 참조).

**F1.4 수집 실패 처리**
- 소스별 3회 지수 백오프 재시도 후 `collection_logs`에 실패 기록.
- 연속 10회 실패 시 운영자에게 텔레그램 경보 1회 발송(중복 경보 억제).

---

### F2. 정규화 및 중복 제거

**F2.1 URL 정규화**
- 추적 파라미터 제거(`utm_*` 등), 프로토콜·호스트 소문자화, 후행 슬래시 정리.
- **`url_source` 정규화(G1 이전)**: 네트워크 없이 가능한 처리만 한다. 추적 파라미터 제거와 소문자화까지. 리다이렉트를 풀지 않는다.
- **`url_canonical` 생성(G3, 게이트 통과분만)**: Google News 리다이렉트를 해제해 최종 원문 URL을 얻고, 여기에 동일한 정규화를 적용한다.
- 순서를 뒤집으면 이미 수집한 기사에 대해 매 실행 리다이렉트 해제 요청이 발생한다. F1.1 게이트 규칙을 위반하는 구현이다.

**F2.2 중복 판정 (3단계)**
1. 정규화 URL 완전 일치 → 중복
2. 본문 해시(SHA-256) 일치 → 중복
3. 제목 정규화 후 유사도 ≥ 0.9 **AND** 발행 시각 차이 ≤ 24h → 중복 후보로 묶고 대표 기사 1건만 노출

> 통신사(연합뉴스 등) 기사의 매체 전재로 동일 내용이 10~20건 중복 수집되는 것이 이 시스템의 최대 노이즈 요인이다. 3단계 판정은 필수 요구사항이다.

**F2.3 언론사 식별**
- 도메인 → 언론사명 매핑 테이블(`press_outlets`) 관리. 미등록 도메인은 `pending`으로 적재 후 운영 화면에서 승인.

---

### F3. 분류 및 중요도 스코어링

**F3.1 카테고리 태깅** (복수 태그 허용)
- `그룹사` (하위: 홀딩스 / 퓨처엠 / DX / 인터내셔널 / 이앤씨 / 기타)
- `배터리·이차전지`
- `지역`
- `정부/정책·법령`
- `시장/주가`

**F3.2 중요도 스코어 (0~100)**

| 요소 | 가중치 | 설명 |
|---|---|---|
| 포스코퓨처엠 직접 언급 | +40 | 제목 언급 시 +50 |
| 그룹사 언급 | +25 | |
| 정책·규제·수사·사고 키워드 | +20 | 대응 필요 이슈 |
| 지역 사업장 언급 | +15 | |
| 주요 언론사 여부 | +10 | |
| 단순 시황·주가 기사 | −15 | 알림 피로 방지 |

**F3.3 스코어 활용**
- **웹**: 전체 저장·노출 (필터로 조절)
- **텔레그램**: 임계값(기본 50) 이상만 발송. 임계값은 운영 설정으로 변경 가능.

> 1분마다 전부 밀어 넣으면 알림이 사실상 무시된다. 스코어 기반 게이팅은 알림 채널의 생존 조건이다.

---

### F4. LLM 요약

**F4.1 요약 규격**
- 3~5문장, 한국어, 각 문장 40자 내외.
- 구성: ① 무슨 일이 / ② 누가·어디서 / ③ 수치·규모 / ④ 포스코퓨처엠 관점 시사점(해당 시).
- 추측·의견 금지. 기사에 없는 사실 생성 금지.

**F4.2 처리 방식**
- 본문 추출 실패 시 RSS description + 제목으로 요약하되 `summary_source = 'snippet'`으로 표시.
- 중복 그룹 내 대표 기사 1건만 요약 호출 → LLM 비용 최적화.
- 요약 실패 시 3회 재시도, 최종 실패 시 요약 없이 링크만 저장하고 재처리 큐에 적재.

**F4.3 비용 관리**
- 일 예상 호출량 및 상한(예: 일 500건)을 설정하고, 초과 시 스코어 상위 건만 요약.
- 모델은 요약 품질/비용 기준으로 선택(초안: Claude Haiku, 고스코어 기사만 Sonnet).

---

### F5. 데이터 모델 (Supabase / PostgreSQL)

```sql
-- 기사 원본
create table articles (
  id              uuid primary key default gen_random_uuid(),
  url_source      text not null unique,    -- 신규 판정 키 (G1/G2). 리다이렉트 미해제
  url_source_aliases text[] default '{}',  -- 같은 기사를 가리키는 다른 소스 URL
  url_canonical   text not null unique,    -- 리다이렉트 해제 후 최종 원문 URL
  url_original    text not null,
  title           text not null,
  press_id        uuid references press_outlets(id),
  author          text,
  published_at    timestamptz not null,
  collected_at    timestamptz not null default now(),
  source_type     text not null,           -- 'google_rss' | 'naver_api' | 'rss' | 'manual'
  thumbnail_url   text,
  content_hash    text,
  dedup_group_id  uuid,                    -- 중복 묶음 대표 키
  is_representative boolean default true,
  is_backfill     boolean default false,   -- 신선도 컷오프 초과분(웹 노출 O, 알림 X)
  importance_score int default 0,
  status          text default 'active'    -- 'active' | 'hidden' | 'error'
);

-- 영구 제외 원장 (저장하지 않기로 확정된 URL — 재조회 방지용)
create table url_ledger (
  url_source text primary key,
  reason     text not null,              -- 'stale' | 'no_pubdate' | 'extract_failed' | 'blocked'
  first_seen timestamptz default now(),
  hit_count  int default 1               -- 이후 재등장 횟수(모니터링용)
);

-- 파이프라인 실행 상태 (부트스트랩/복구 모드 판정용)
create table run_state (
  key            text primary key,         -- 'pipeline'
  last_success_at timestamptz,
  notify_mode    text not null default 'suppressed',  -- 'suppressed' | 'active'
  updated_at     timestamptz default now()
);

-- LLM 요약
create table summaries (
  id             uuid primary key default gen_random_uuid(),
  article_id     uuid not null references articles(id) on delete cascade,
  summary_text   text not null,            -- 3~5줄
  summary_source text not null,            -- 'fulltext' | 'snippet'
  model          text not null,
  token_usage    jsonb,
  created_at     timestamptz default now()
);

-- 카테고리 태그
create table article_tags (
  article_id uuid references articles(id) on delete cascade,
  tag        text not null,
  confidence numeric,
  primary key (article_id, tag)
);

-- 언론사
create table press_outlets (
  id       uuid primary key default gen_random_uuid(),
  domain   text not null unique,
  name     text not null,
  tier     int default 3,
  status   text default 'pending'          -- 'approved' | 'pending' | 'blocked'
);

-- 검색 키워드
create table keyword_sets (
  id       uuid primary key default gen_random_uuid(),
  category text not null,
  keyword  text not null,
  enabled  boolean default true
);

-- 텔레그램 발송 큐/이력
create table notifications (
  id           uuid primary key default gen_random_uuid(),
  article_id   uuid references articles(id),
  channel      text not null,              -- 'telegram'
  chat_id      text not null,
  status       text not null,              -- 'queued' | 'sent' | 'failed' | 'skipped'
  error        text,
  retry_count  int default 0,
  sent_at      timestamptz,
  created_at   timestamptz default now()
);

-- 수집 로그
create table collection_logs (
  id            bigserial primary key,
  run_at        timestamptz default now(),
  source_type   text,
  fetched_count int,
  new_count     int,
  dup_count     int,
  skipped_seen_count int,                  -- G1/G2에서 탈락한 기수집 건수
  http_request_count int,                  -- 실행당 외부 요청 수 (검증 지표)
  error         text,
  duration_ms   int
);

create index on articles (published_at desc);
create index on articles (importance_score desc, published_at desc);
create index on articles using gin (url_source_aliases);   -- G1/G2 alias 조회
create index on notifications (status, created_at);
```

**RLS 정책**
- 공개 읽기: `articles`, `summaries`, `article_tags`, `press_outlets` (status = approved)
- 쓰기: service role key만 허용 (Worker에서만 사용, 클라이언트 노출 금지)

---

### F6. 웹 애플리케이션

**F6.1 화면 구성** (레퍼런스 사이트 구조 계승)

1. **헤더**: 브랜딩(딥네이비 `#16337A` 계열), 상단 라벨 스트립(`SUPABASE · GOOGLE RSS · POSCO SIGNAL`)
2. **요약 통계 바**: 전체 기사 수 / 오늘 업데이트 수 / 최근 수집 시각
3. **필터 영역**
   - 그룹사 필터 (홀딩스·퓨처엠·DX·인터내셔널·이앤씨)
   - 카테고리 필터 (배터리·이차전지 / 정부·정책 / 법령 / 지역 / 시장)
   - 언론사 필터
   - 기간 필터 (오늘 / 7일 / 30일 / 전체)
   - 키워드 검색
4. **포토카드 그리드** (핵심)
5. **운영 상태 섹션**: 수집 성공률, 대기 큐, 실패 건수, 텔레그램 구독자 수
6. **푸터**: 저작권 및 투자권유 아님 고지

**F6.2 포토카드 사양**

| 요소 | 내용 |
|---|---|
| 썸네일 | og:image, 없으면 카테고리별 기본 그래픽 |
| 배지 | 그룹사 태그 / 카테고리 / 중요도(높음일 때만) |
| 제목 | 2줄 말줄임 |
| 요약 | 3~5줄 전문 표시 |
| 메타 | 언론사 · 발행시각(상대시간) |
| 액션 | 원문 열기(새 탭) / 링크 복사 |

- 반응형: 데스크톱 3열 / 태블릿 2열 / 모바일 1열
- 무한 스크롤 또는 페이지네이션(20건 단위)
- 신규 기사 도착 시 상단에 "새 기사 N건" 배너 → 클릭 시 갱신

**F6.3 성능**
- LCP 2.5초 이내, 초기 로드 20건.
- 목록 조회는 Supabase 뷰 또는 RPC로 조인 최소화.

---

### F7. 텔레그램 봇

**F7.1 발송 트리거**
- 기사가 DB에 신규 저장되고 요약이 완료된 시점(웹 등록 시점과 동일 트리거).

**F7.2 메시지 포맷**

```
🔴 [포스코퓨처엠] 제목이 여기에 들어갑니다

한국경제 · 3분 전

• 요약 첫 문장
• 요약 두 번째 문장
• 요약 세 번째 문장

🔗 원문 보기
```

- 배지 이모지는 중요도에 따라 🔴(80+) / 🟠(50–79) 구분
- `parse_mode: HTML`, `disable_web_page_preview: false`

**F7.3 발송 정책 (중요)**
- **묶음 발송**: 1분 사이클에서 대상 기사가 3건 이상이면 하나의 다이제스트 메시지로 묶는다.
- **Rate limit 준수**: 동일 채팅방 분당 20건 제한. 큐에서 초당 1건 이하로 배출.
- **야간 모드**: 23:00–07:00에는 중요도 80 이상만 즉시 발송, 나머지는 07:00 다이제스트로 이월. (설정 가능)
- **실패 처리**: 재시도 3회, 최종 실패 시 `notifications.status = 'failed'` 및 운영 화면 표시.

**F7.4 봇 명령어**

| 명령 | 동작 |
|---|---|
| `/start` | 구독 등록 |
| `/stop` | 구독 해제 |
| `/latest` | 최근 5건 즉시 조회 |
| `/today` | 오늘 다이제스트 |
| `/filter` | 카테고리별 구독 설정 |
| `/threshold [n]` | 알림 중요도 임계값 조정 |

---

### F8. 운영 및 모니터링

- 대시보드에 최근 24시간 수집 성공률, 소스별 수집량, LLM 실패 건수, 발송 실패 건수 노출.
- 수집이 10분간 0건이면 운영자 경보.
- 관리자 화면: 기사 숨김, 언론사 승인/차단, 키워드 추가/비활성, 수동 URL 등록 후 즉시 분석.

---

## 6. 비기능 요구사항

| 구분 | 요구사항 |
|---|---|
| 지연 | 기사 발행 → 텔레그램 도달 목표 5분 이내 (P90). RSS 캐시 지연 포함 |
| 실행 시간 | 1회 수집 실행이 20초 이내 종료. 다음 주기와 겹치면 해당 회차는 스킵하고 경보 |
| 재조회 비용 | 이미 수집한 기사에 대한 외부 HTTP 요청 및 LLM 호출은 0건 (F1.1 게이트) |
| 가용성 | 월 99% 이상. 파이프라인 중단 시 재시작만으로 커서부터 복구 |
| 보안 | Service role key·API key는 서버 환경변수로만 관리. 클라이언트 번들 포함 금지 |
| 확장성 | 소스 추가가 설정으로 가능해야 함(코드 수정 없이 `keyword_sets`·RSS 등록) |
| 정합성 | 동일 기사 중복 발송 0건 (`notifications` 유니크 제약으로 보장) |

---

## 7. 법적 / 저작권 준수

이 항목은 협상 대상이 아니다. 사내 활용 시스템이라도 동일하게 적용한다.

1. **본문 전문은 공개 화면에 게시하지 않는다.** 공개 요소는 제목·언론사·발행시각·요약·원문 링크로 제한한다.
2. 요약은 원문 표현을 그대로 옮기지 않고 재서술한다.
3. 본문 원문은 요약 생성 목적의 임시 처리만 하고, 저장이 필요하면 비공개 영역에 보관하며 보존 기간을 정한다(제안: 30일).
4. 네이버는 공식 검색 API를 사용하고 직접 크롤링하지 않는다. 각 언론사 `robots.txt`를 준수한다.
5. 접근은 사내 사용자로 제한하는 것을 검토한다(공개 웹이면 리스크가 커진다).
6. 푸터에 저작권 귀속 및 투자권유 아님 고지를 상시 표시한다.

---

## 8. 기술 스택 (제안)

| 레이어 | 선택 | 비고 |
|---|---|---|
| 스케줄러/백엔드 | Cloudflare Workers + Cron Triggers | 1분 주기 실행 가능 |
| DB | Supabase (PostgreSQL) | 요구사항 명시 |
| LLM | Claude API | 요약 및 분류 보조 |
| 프론트엔드 | Next.js (App Router) + Tailwind CSS | Vercel 배포 |
| 알림 | Telegram Bot API | |
| 본문 추출 | Readability 계열 파서 (실패 시 폴백) | |

---

## 9. 개발 마일스톤

| 단계 | 범위 | 완료 기준 |
|---|---|---|
| **M0. 기반** | Supabase 스키마, 환경 구성, 키워드 세트 등록 | 스키마 배포 및 시드 데이터 입력 완료 |
| **M1. 수집 파이프라인** | Google RSS 수집 + 게이트 + 정규화 + 중복 제거 + 신선도 컷오프 | 1분 주기 실행, 중복 저장 0건, **신규 0건 실행의 추가 HTTP 요청 0회**, 실행 시간 20초 이내 |
| **M2. 요약** | 본문 추출 + LLM 3~5줄 요약 저장 | 요약 성공률 90% 이상 |
| **M3. 웹 아카이브** | 포토카드 그리드 + 필터 + 검색 | 레퍼런스 디자인 수준 구현, 모바일 대응 |
| **M4. 텔레그램 봇** | 신규 등록 트리거 발송 + 명령어 | 중복 발송 0건, 5분 내 도달 |
| **M5. 운영 강화** | 네이버 API 소스 추가, 모니터링, 관리자 화면 | 24시간 무인 운영 성공 |
| **v2 후보** | SWOT·대응책 분석, 카카오톡 채널, URL 즉시 분석기 | — |

---

## 10. 성공 지표

| 지표 | 목표 |
|---|---|
| 기사 발행 → 알림 도달 (P90) | 5분 이내 |
| 중복 발송 건수 | 0건 |
| 요약 생성 성공률 | 95% 이상 |
| 중요 기사 누락률 | 5% 이하 (주간 수동 샘플 검증) |
| 알림 유효율(열람으로 이어진 비율) | 30% 이상 |
| 일 LLM 비용 | 설정 상한 이내 |

---

## 11. 리스크 및 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| Google News RSS 캐시 지연 | "1분 수집"이 실제로는 수 분 지연 | 네이버 API 병행, 목표를 "1분 폴링 / 5분 도달"로 정의 |
| 발행 시각 기반 커서로 인한 영구 누락 | 기사 유실 (탐지도 어려움) | 커서 미사용. F1.1 저장소 대조 방식 채택 |
| 기수집 기사 재조회로 실행 시간 폭증 | 1분 주기 내 완료 실패, 비용 증가 | F1.1 게이트 단계 순서 준수. `url_source` 선판정으로 리다이렉트 해제를 게이트 뒤로 이동 |
| 재기동 시 과거 기사 일괄 발송 | 알림 폭탄 | 억제 모드 부트스트랩(F1.1) |
| 통신사 전재로 인한 대량 중복 | 알림 스팸 | F2.2 3단계 중복 판정 필수 구현 |
| 텔레그램 rate limit | 발송 누락 | 큐 기반 배출 + 다이제스트 묶음 |
| LLM 비용 증가 | 운영 지속성 | 대표 기사만 요약, 일일 상한, 모델 티어링 |
| 저작권 이슈 | 법적 리스크 | §7 준수, 본문 비공개 |
| 알림 피로 | 시스템 무시 | 중요도 게이팅 + 야간 모드 + 사용자별 임계값 |

---

## 12. 미결정 사항 (확정 필요)

1. **웹 공개 범위** — 공개 웹 / 사내 한정(로그인) 중 무엇인가? 저작권·보안 방침이 여기에 달려 있다.
2. **텔레그램 대상** — 개인 DM인가, 팀 그룹방인가? 그룹방이면 알림 임계값을 더 보수적으로 잡아야 한다.
3. **지역 키워드 확정** — 포스코퓨처엠 사업장 소재지(포항·광양·세종·구미 등) 중 모니터링 대상 확정 필요.
4. **네이버 API 사용 가능 여부** — Open API 키 발급 및 일 호출 한도 확인.
5. **기존 자산 연계** — 기존 `taehyun108.github.io/KTH_01/news/` 아카이브와 통합할 것인가, 신규 분리할 것인가?
6. **본문 보관 기간** — 재요약·재분석을 위해 원문을 얼마나 보관할 것인가.
7. **LLM 일일 예산 상한** — 구체 금액 확정 필요.
8. **수정 기사 재처리 여부** — "업데이트 되는 기사"에 *내용이 수정된 기존 기사*까지 포함할 것인가? v1은 미포함으로 설계했다(F1.1). 포함하려면 중요도 상위 기사에 한한 별도 저빈도 잡이 필요하며, 1분 파이프라인에는 넣지 않는다.

---

## 부록 A. 워크플로우 원본 대비 매핑

| drawio 노드 | PRD 대응 |
|---|---|
| 구글 RSS 및 Naver 기사 최신기사 크롤링 | F1.2 수집 소스 |
| 1분마다 최신기사 크롤링 | F1.1 수집 주기 |
| LLM에게 기사 요약 3~5줄로 요청 | F4 LLM 요약 |
| 웹페이지에 포토카드 형태로 URL링크 및 요약 표기 | F6 웹 애플리케이션 |
| Chatbot에 URL 링크 및 요약내용 송부 | F7 텔레그램 봇 |

원본 다이어그램에는 없지만 실제 운영에 필수적이어서 추가한 단계: **중복 제거(F2)**, **중요도 스코어링(F3)**, **발송 큐 및 rate limit 제어(F7.3)**.
