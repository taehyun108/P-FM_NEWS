-- =====================================================================
-- P-FM NEWS — 로컬 개발용 SQLite 스키마 (PLAN 0-4)
--
-- backend/schema.sql (Supabase/PostgreSQL) 과 "같은 논리 스키마"다.
-- 한쪽만 고치면 DB 전환 시점에 반드시 깨지므로 항상 함께 수정한다.
--
-- 타입 대응
--   uuid        -> TEXT   (파이썬에서 uuid4 문자열 생성)
--   timestamptz -> TEXT   (ISO8601 UTC 문자열. 문자열 정렬 = 시간 정렬)
--   text[]      -> TEXT   (JSON 배열 문자열)
--   jsonb       -> TEXT   (JSON 문자열)
--   boolean     -> INTEGER(0/1)
--   numeric     -> REAL
-- =====================================================================

pragma journal_mode = wal;   -- 수집 루프와 API 서버가 동시에 접근한다
pragma foreign_keys = on;

create table if not exists press_outlets (
  id     TEXT primary key,
  domain TEXT not null unique,
  name   TEXT not null,
  tier   INTEGER default 3,
  status TEXT default 'pending'
);

create table if not exists articles (
  id                 TEXT primary key,
  url_source         TEXT not null unique,
  url_source_aliases TEXT default '[]',
  url_canonical      TEXT unique,
  url_original       TEXT not null,
  title              TEXT not null,
  press_id           TEXT references press_outlets(id),
  press_name         TEXT,
  author             TEXT,
  published_at       TEXT not null,
  collected_at       TEXT not null,
  source_type        TEXT not null,
  thumbnail_url      TEXT,
  content_hash       TEXT,
  dedup_group_id     TEXT,
  is_representative  INTEGER default 1,
  is_backfill        INTEGER default 0,
  importance_score   INTEGER default 0,
  sentiment          TEXT,
  keywords           TEXT default '[]',
  group_companies    TEXT default '[]',
  categories         TEXT default '[]',
  title_embedding    TEXT,
  analyzed_at        TEXT,
  status             TEXT default 'active'
);

create table if not exists url_ledger (
  url_source TEXT primary key,
  reason     TEXT not null,
  first_seen TEXT not null,
  hit_count  INTEGER default 1
);

create table if not exists run_state (
  key              TEXT primary key,
  last_success_at  TEXT,
  bootstrap_at     TEXT,                   -- 최초 실행 시각. 이 이전 발행 기사는 알림하지 않는다
  notify_mode      TEXT not null default 'suppressed',
  notify_paused    INTEGER not null default 0,   -- 텔레그램 /stop 로 알림 일시중지
  notify_threshold INTEGER,                -- /threshold 로 지정한 임계값 (null 이면 .env 값)
  tg_offset        INTEGER not null default 0,   -- 텔레그램 getUpdates offset
  always_notify_keywords TEXT default '[]', -- 이 단어가 본문에 있으면 점수 무관 알림 (마스터 설정)
  master_pw_hash   TEXT,                    -- 마스터 비밀번호 (pbkdf2, null 이면 .env 값 사용)
  web_pw_hash      TEXT,                    -- 웹페이지 비밀번호 해시 (게이트는 추후)
  web_password     TEXT,                    -- 웹페이지 비밀번호 평문 (마스터 패널에서 확인용)
  notify_policy    INTEGER not null default 0, -- 정책브리핑 기사도 텔레그램 알림 (마스터 설정)
  policy_notify_keywords TEXT default '[]', -- 정책 알림 키워드 (마스터 설정, 비면 모든 정책기사)
  updated_at       TEXT not null
);

create table if not exists summaries (
  id               TEXT primary key,
  article_id       TEXT not null unique references articles(id) on delete cascade,
  summary_text     TEXT not null,
  perspective_text TEXT,
  summary_source   TEXT not null,
  model            TEXT not null,
  token_usage      TEXT,
  created_at       TEXT not null
);

-- 본문 임시 보관 (§7-3) — 분석 대기 중인 기사의 본문. 분석 완료 시 삭제, 미완료분은 30일 후 정리
create table if not exists article_bodies (
  article_id     TEXT primary key references articles(id) on delete cascade,
  body           TEXT not null,
  summary_source TEXT not null,
  fetched_at     TEXT not null
);

create table if not exists swot_analyses (
  article_id  TEXT primary key references articles(id) on delete cascade,
  s_score     INTEGER not null default 0,
  w_score     INTEGER not null default 0,
  o_score     INTEGER not null default 0,
  t_score     INTEGER not null default 0,
  total_score INTEGER not null default 0,
  s_text      TEXT,
  w_text      TEXT,
  o_text      TEXT,
  t_text      TEXT,
  model       TEXT not null,
  created_at  TEXT not null
);

create table if not exists keyword_sets (
  id       TEXT primary key,
  category TEXT not null,
  keyword  TEXT not null,
  enabled  INTEGER default 1,
  unique (category, keyword)
);

create table if not exists feed_sources (
  id          TEXT primary key,
  source_type TEXT not null,
  name        TEXT not null,
  url         TEXT,
  enabled     INTEGER default 1
);

create table if not exists notifications (
  id          TEXT primary key,
  article_id  TEXT references articles(id) on delete cascade,
  channel     TEXT not null,
  chat_id     TEXT not null,
  status      TEXT not null,
  error       TEXT,
  retry_count INTEGER default 0,
  priority    INTEGER not null default 0,   -- 1이면 임계값·야간 게이트를 우회해 발송
  sent_at     TEXT,
  created_at  TEXT not null,
  -- 중복 발송 0건을 DB 제약으로 보장한다 (§6 정합성)
  unique (article_id, channel, chat_id)
);

create table if not exists collection_logs (
  id                 INTEGER primary key autoincrement,
  run_at             TEXT not null,
  source_type        TEXT,
  fetched_count      INTEGER,
  new_count          INTEGER,
  dup_count          INTEGER,
  skipped_seen_count INTEGER,
  http_request_count INTEGER,
  error              TEXT,
  duration_ms        INTEGER
);

create table if not exists market_quotes (
  symbol      TEXT primary key,
  kind        TEXT not null,
  label       TEXT not null,
  price       REAL not null,
  change_rate REAL,
  fetched_at  TEXT not null
);

-- 인덱스 (SQLite 에는 GIN 이 없다. 배열 필터는 애플리케이션에서 처리한다)
create index if not exists idx_articles_published  on articles (published_at desc);
create index if not exists idx_articles_score      on articles (importance_score desc, published_at desc);
create index if not exists idx_articles_dedup      on articles (dedup_group_id);
create index if not exists idx_articles_canonical  on articles (url_canonical);
create index if not exists idx_notifications_state on notifications (status, created_at);
