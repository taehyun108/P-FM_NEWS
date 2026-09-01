---
name: supabase-news-schema
description: Supabase/PostgreSQL 뉴스 아카이브 스키마·인덱스·RLS 정책과 벌크 업서트 패턴. Use when designing Supabase or Postgres tables for a news/article archive, writing RLS policies, choosing indexes for feed queries, using service_role vs anon key safely, or doing bulk upsert from a collector worker.
---

# Supabase 뉴스 아카이브 스키마

## 1. 키 설계 (가장 중요)

| 컬럼 | 제약 | 이유 |
|---|---|---|
| `url_source` | UNIQUE NOT NULL | 신규 판정 키. 네트워크 없이 계산 가능 |
| `url_canonical` | UNIQUE | 리다이렉트 해제 후 원문. 중복 제거·표시용 |
| `url_source_aliases` | `text[]` + GIN | 같은 기사의 다른 소스 URL |

UNIQUE 제약이 **최종 방어선**이다. 애플리케이션 중복 체크만 믿지 않는다.

## 2. 스키마 골격

```sql
create table articles (
  id                uuid primary key default gen_random_uuid(),
  url_source        text not null unique,
  url_source_aliases text[] default '{}',
  url_canonical     text not null unique,
  url_original      text not null,
  title             text not null,
  press_id          uuid references press_outlets(id),
  author            text,
  published_at      timestamptz not null,
  collected_at      timestamptz not null default now(),
  source_type       text not null,          -- 'google_rss'|'naver_api'|'rss'|'manual'
  thumbnail_url     text,
  content_hash      text,
  dedup_group_id    uuid,
  is_representative boolean default true,
  is_backfill       boolean default false,
  importance_score  int default 0,
  status            text default 'active'   -- 'active'|'hidden'|'error'
);

create index on articles (published_at desc);
create index on articles (importance_score desc, published_at desc);
create index on articles using gin (url_source_aliases);
```

동반 테이블: `summaries`, `article_tags`, `press_outlets`, `keyword_sets`, `notifications`, `collection_logs`, `url_ledger`, `run_state`.

### 설정을 DB로 뺀다
검색 키워드(`keyword_sets`), 언론사 매핑(`press_outlets`), 실행 상태(`run_state`)는 **테이블로 관리**한다.
소스·키워드 추가가 코드 수정 없이 가능해야 한다.

## 3. RLS 정책

```sql
alter table articles enable row level security;

-- 공개 읽기: 활성 기사만
create policy "public read active" on articles
  for select to anon using (status = 'active');
```

원칙
- 쓰기는 **service_role 키만** 허용. 이 키는 서버 환경변수로만 두고 **클라이언트 번들에 절대 포함하지 않는다.**
- 공개 읽기 대상: `articles`, `summaries`, `article_tags`, `press_outlets`(승인된 것만).
- 비공개: `notifications`, `collection_logs`, `url_ledger`, `run_state` — 운영 데이터는 anon에 열지 않는다.
- 브라우저에서 쓰는 키는 반드시 `anon` 키. RLS를 끄고 anon 쓰기를 허용하는 조합은 금지.

## 4. 벌크 업서트 (실행당 DB 왕복 최소화)

```sql
insert into articles (url_source, url_canonical, url_original, title, published_at, source_type, importance_score)
select * from unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::timestamptz[], $6::text[], $7::int[])
on conflict (url_source) do nothing
returning id, url_source;
```

- 목표: **실행당 조회 1회 + 저장 1회.**
- `do nothing` + `returning`으로 실제 신규 저장분만 후속 처리(알림 큐 적재)한다.
- alias 누적은 별도 `array_append` 업데이트 1회로 묶는다.

## 5. 조회 성능

- 목록은 뷰 또는 RPC로 만들어 조인을 서버에서 끝낸다(N+1 방지).
- 초기 로드 20건, 커서 기반 페이지네이션(`published_at`, `id` 복합 커서). OFFSET은 뒤로 갈수록 느려진다.
- 필터 조합(그룹사·카테고리·언론사·기간·검색)이 많으면 부분 인덱스를 검토한다.
- 전문 검색이 필요하면 `to_tsvector`에 GIN 인덱스. `LIKE '%..%'`는 인덱스를 못 탄다.

## 6. 검증

- [ ] anon 키로 `insert`/`update` 시도 → 거부된다.
- [ ] service_role 키가 프론트 번들에 포함되지 않는다(빌드 산출물 grep).
- [ ] 동일 `url_source` 2회 입력 → 1행만 존재.
- [ ] 목록 쿼리가 인덱스를 탄다(`explain analyze`로 seq scan 여부 확인).
