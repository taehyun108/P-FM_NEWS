-- =====================================================================
-- P-FM NEWS — Supabase / PostgreSQL 스키마 (PRD F5)
--
-- 사용법: Supabase 대시보드 > SQL Editor 에 붙여넣고 실행한다.
-- 주의: backend/schema_sqlite.sql 과 "같은 논리 스키마"를 유지해야 한다.
--       한쪽만 고치면 DB 전환 시점에 반드시 깨진다. (PLAN 0-4)
--
-- 임베딩은 pgvector 대신 jsonb 배열로 저장한다. 중복 판정 4단계는
-- 후보 건수가 적어 애플리케이션에서 코사인을 계산해도 충분하다. (PLAN 0-5)
-- =====================================================================

-- ── 언론사 (articles 가 참조하므로 먼저 만든다) ────────────────────
create table if not exists press_outlets (
  id       uuid primary key default gen_random_uuid(),
  domain   text not null unique,
  name     text not null,
  tier     int  default 3,                  -- 1=주요지, 2=업계·경제지, 3=기타
  status   text default 'pending'           -- 'approved' | 'pending' | 'blocked'
);

-- ── 기사 원본 ──────────────────────────────────────────────────────
create table if not exists articles (
  id                 uuid primary key default gen_random_uuid(),
  url_source         text not null unique,   -- 신규 판정 키 (G1/G2). 리다이렉트 미해제
  url_source_aliases text[] default '{}',    -- 같은 기사를 가리키는 다른 소스 URL
  url_canonical      text unique,            -- 리다이렉트 해제 후 최종 원문 URL
  url_original       text not null,
  title              text not null,
  press_id           uuid references press_outlets(id),
  press_name         text,                   -- 표시용 비정규화 (조인 제거, F6.3)
  author             text,                   -- 기자명. '[언론사, 기자]' 머리표 조립용 (F4.1)
  published_at       timestamptz not null,
  collected_at       timestamptz not null default now(),
  source_type        text not null,          -- 'google_rss' | 'naver_api' | 'rss' | 'manual'
  thumbnail_url      text,
  content_hash       text,                   -- 본문 SHA-256 (중복 판정 2단계)
  dedup_group_id     uuid,                   -- 중복 묶음 대표 키
  is_representative  boolean default true,
  is_backfill        boolean default false,  -- 신선도 컷오프 초과분(웹 노출 O, 알림 X)
  importance_score   int default 0,
  sentiment          text,                   -- '긍정' | '중립' | '부정' (F4.3)
  keywords           jsonb default '[]',     -- 정규화·중복 제거된 키워드 최대 6개 (F4.2)
  group_companies    jsonb default '[]',     -- 관련 포스코 그룹사 (F4.2)
  categories         jsonb default '[]',     -- 카테고리 태그 (F3.1)
  title_embedding    jsonb,                  -- 중복 판정 4단계용 캐시 (F2.2)
  analyzed_at        timestamptz,            -- LLM 분석 완료 시각. null 이면 재처리 큐 대상
  status             text default 'active'   -- 'active' | 'hidden' | 'error'
);

-- ── 영구 제외 원장 (저장하지 않기로 확정된 URL — 재조회 방지) ──────
create table if not exists url_ledger (
  url_source text primary key,
  reason     text not null,                  -- 'stale' | 'no_pubdate' | 'extract_failed' | 'blocked'
  first_seen timestamptz default now(),
  hit_count  int default 1                   -- 이후 재등장 횟수(모니터링용)
);

-- ── 파이프라인 실행 상태 (부트스트랩/복구 모드 판정용) ─────────────
create table if not exists run_state (
  key             text primary key,          -- 'pipeline'
  last_success_at timestamptz,
  bootstrap_at    timestamptz,               -- 최초 실행 시각. 이 이전 발행 기사는 알림하지 않는다
  notify_mode     text not null default 'suppressed',  -- 'suppressed' | 'active'
  notify_paused   boolean not null default false,      -- 텔레그램 /stop 로 알림 일시중지
  notify_threshold int,                     -- /threshold 로 지정한 임계값 (null 이면 .env 값)
  tg_offset       bigint not null default 0,           -- 텔레그램 getUpdates offset
  always_notify_keywords text default '[]', -- 이 단어가 본문에 있으면 점수 무관 알림 (마스터 설정)
  master_pw_hash  text,                     -- 마스터 비밀번호 (pbkdf2, null 이면 .env 값 사용)
  web_pw_hash     text,                     -- 웹페이지 비밀번호 해시 (게이트는 추후)
  web_password    text,                     -- 웹페이지 비밀번호 평문 (마스터 패널에서 확인용)
  updated_at      timestamptz default now()
);

-- ── LLM 요약 ───────────────────────────────────────────────────────
create table if not exists summaries (
  id               uuid primary key default gen_random_uuid(),
  article_id       uuid not null unique references articles(id) on delete cascade,
  summary_text     text not null,            -- 3~5줄 (머리표 제외, 본문만)
  perspective_text text,                     -- '포스코 관점:' 단락 (F4.1)
  summary_source   text not null,            -- 'fulltext' | 'snippet'
  model            text not null,
  token_usage      jsonb,
  created_at       timestamptz default now()
);

-- ── 본문 임시 보관 (§7-3) ─────────────────────────────────────────
-- 분석 대기 중인 기사의 본문. 분석 완료 시 즉시 삭제하고, 미완료분은 30일 후 정리한다.
-- 공개 화면에는 절대 노출하지 않는다(RLS 미적용 = service role 전용).
create table if not exists article_bodies (
  article_id     uuid primary key references articles(id) on delete cascade,
  body           text not null,
  summary_source text not null,
  fetched_at     timestamptz not null default now()
);

-- ── SWOT 분석 (F4.4) — 기사 1건당 1행 ──────────────────────────────
create table if not exists swot_analyses (
  article_id  uuid primary key references articles(id) on delete cascade,
  s_score     int not null default 0,        -- 각 0~100
  w_score     int not null default 0,
  o_score     int not null default 0,
  t_score     int not null default 0,
  total_score int not null default 0,        -- (S+O)-(W+T) 정규화 0~100. 카드 노출값
  s_text      text,                          -- 1~2줄 근거. 툴팁 표시용
  w_text      text,
  o_text      text,
  t_text      text,
  model       text not null,
  created_at  timestamptz default now()
);

-- ── 검색 키워드 셋 (코드 하드코딩 금지, F1.3) ──────────────────────
create table if not exists keyword_sets (
  id       uuid primary key default gen_random_uuid(),
  category text not null,
  keyword  text not null,
  enabled  boolean default true,
  unique (category, keyword)
);

-- ── 수집 소스 (코드 수정 없이 추가 가능해야 함, §6 확장성) ─────────
create table if not exists feed_sources (
  id          uuid primary key default gen_random_uuid(),
  source_type text not null,                 -- 'google_rss' | 'naver_api' | 'rss'
  name        text not null,
  url         text,                          -- 'rss' 일 때 피드 주소
  enabled     boolean default true
);

-- ── 텔레그램 발송 큐/이력 ──────────────────────────────────────────
create table if not exists notifications (
  id          uuid primary key default gen_random_uuid(),
  article_id  uuid references articles(id) on delete cascade,
  channel     text not null,                 -- 'telegram'
  chat_id     text not null,
  status      text not null,                 -- 'queued' | 'sent' | 'failed' | 'skipped'
  error       text,
  retry_count int default 0,
  priority    int not null default 0,        -- 1이면 임계값·야간 게이트를 우회해 발송
  sent_at     timestamptz,
  created_at  timestamptz default now(),
  -- 중복 발송 0건을 DB 제약으로 보장한다 (§6 정합성)
  unique (article_id, channel, chat_id)
);

-- ── 수집 로그 ──────────────────────────────────────────────────────
create table if not exists collection_logs (
  id                 bigserial primary key,
  run_at             timestamptz default now(),
  source_type        text,
  fetched_count      int,
  new_count          int,
  dup_count          int,
  skipped_seen_count int,                    -- G1/G2에서 탈락한 기수집 건수
  http_request_count int,                    -- 실행당 외부 요청 수 (검증 지표)
  error              text,
  duration_ms        int
);

-- ── 시세 캐시 (F9) ─────────────────────────────────────────────────
create table if not exists market_quotes (
  symbol      text primary key,              -- '005490' | 'USDKRW' ...
  kind        text not null,                 -- 'stock' | 'fx'
  label       text not null,                 -- '포스코홀딩스' | '달러' ...
  price       numeric not null,
  change_rate numeric,                       -- 전일 대비 등락률(%)
  fetched_at  timestamptz not null default now()
);

-- ── 인덱스 ─────────────────────────────────────────────────────────
create index if not exists idx_articles_published  on articles (published_at desc);
create index if not exists idx_articles_score      on articles (importance_score desc, published_at desc);
create index if not exists idx_articles_aliases    on articles using gin (url_source_aliases);
create index if not exists idx_articles_keywords   on articles using gin (keywords);
create index if not exists idx_articles_groups     on articles using gin (group_companies);
create index if not exists idx_articles_categories on articles using gin (categories);
create index if not exists idx_articles_dedup      on articles (dedup_group_id);
create index if not exists idx_notifications_state on notifications (status, created_at);

-- ── RLS (F5) ───────────────────────────────────────────────────────
-- 공개 읽기만 허용한다. 쓰기는 service role key 로만 가능하며,
-- service role key 는 backend 밖으로 나가지 않는다. (§6 보안)
alter table articles       enable row level security;
alter table summaries      enable row level security;
alter table swot_analyses  enable row level security;
alter table market_quotes  enable row level security;
alter table press_outlets  enable row level security;

create policy "public read articles"  on articles      for select using (status = 'active');
create policy "public read summaries" on summaries     for select using (true);
create policy "public read swot"      on swot_analyses for select using (true);
create policy "public read quotes"    on market_quotes for select using (true);
create policy "public read press"     on press_outlets for select using (status = 'approved');
