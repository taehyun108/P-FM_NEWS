"""저장소 (SQLite / Supabase)"""
from __future__ import annotations

from abc import ABC
from typing import Any
from typing import Sequence
from abc import abstractmethod
from datetime import datetime
import os
import sqlite3
import threading
import time
from datetime import timedelta

from .core import (
    BACKEND_DIR,
    Config,
    ROOT_DIR,
    iso,
    jdump,
    jload,
    log,
    new_id,
    now_utc,
)




# =====================================================================
# 3. 저장소 계층 (PLAN 0-4)
#    파이프라인은 SQL 을 직접 쓰지 않고 이 인터페이스만 호출한다.
#    덕분에 SQLite ↔ Supabase 전환이 .env 값 하나로 끝난다.
# =====================================================================

class Storage(ABC):
    @abstractmethod
    def init_schema(self) -> None: ...

    @abstractmethod
    def seen_url_sources(self, candidates: Sequence[str]) -> set[str]:
        """G2 — 전체 기간 대조. articles.url_source + alias + url_ledger 를 한 번에 조회."""

    @abstractmethod
    def recent_url_sources(self, hours: int) -> set[str]:
        """G1 seen-set 캐시 로드. 성능 계층일 뿐 정확성에 관여하지 않는다."""

    @abstractmethod
    def insert_article(self, row: dict) -> bool: ...

    @abstractmethod
    def update_article(self, article_id: str, patch: dict) -> None: ...

    @abstractmethod
    def append_alias(self, article_id: str, url_source: str) -> None: ...

    @abstractmethod
    def find_by_content_hash(self, content_hash: str) -> dict | None: ...

    @abstractmethod
    def find_by_canonical(self, url_canonical: str) -> dict | None: ...

    @abstractmethod
    def recent_articles_for_dedup(self, since: datetime) -> list[dict]: ...

    @abstractmethod
    def upsert_ledger(self, url_source: str, reason: str) -> None: ...

    @abstractmethod
    def bump_ledger(self, url_sources: Sequence[str]) -> None: ...

    @abstractmethod
    def save_body(self, article_id: str, body: str, summary_source: str) -> None:
        """분석 대기용 본문 임시 저장 (§7-3). 분석 완료 시 delete_body 로 지운다."""

    @abstractmethod
    def unanalyzed_with_body(self, limit: int) -> list[dict]:
        """analyzed_at 이 null 이고 본문이 남아 있는 기사. 중요도·최신 순."""

    @abstractmethod
    def unanalyzed_count(self) -> int: ...

    @abstractmethod
    def deferred_articles(self, limit: int) -> list[dict]:
        """메타만 저장돼(본문 없음) 아직 분석 안 된 기사. 발행 오래된 순."""

    @abstractmethod
    def deferred_count(self) -> int: ...

    @abstractmethod
    def delete_body(self, article_id: str) -> None: ...

    @abstractmethod
    def cleanup_bodies(self, older_than_days: int) -> int: ...

    @abstractmethod
    def purge_stale_drafts(self, older_than_hours: int) -> int: ...

    @abstractmethod
    def purge_stale_embeddings(self, older_than_hours: int) -> int: ...

    @abstractmethod
    def purge_old_articles(self, hard_days: int, soft_days: int, keep_score: int) -> int:
        """오래된 기사 삭제(자식 테이블은 cascade). 핵심 기사는 hard_days, 잡음은 soft_days."""

    @abstractmethod
    def prune_collection_logs(self, older_than_days: int) -> int: ...

    @abstractmethod
    def prune_url_ledger(self, older_than_days: int) -> int: ...

    @abstractmethod
    def log_telegram(self, entry: dict) -> None:
        """봇으로 나간 메시지 1건을 telegram_log 에 기록. 실패해도 발송에 영향 없어야 한다."""

    @abstractmethod
    def recent_telegram_logs(self, limit: int) -> list[dict]: ...

    @abstractmethod
    def prune_telegram_log(self, older_than_days: int) -> int: ...

    @abstractmethod
    def vacuum(self) -> None:
        """저장 공간 회수(SQLite VACUUM). Supabase 는 autovacuum 이 처리하므로 no-op."""

    @abstractmethod
    def save_summary(self, row: dict) -> None: ...

    @abstractmethod
    def save_swot(self, row: dict) -> None: ...

    @abstractmethod
    def llm_calls_today(self) -> int: ...

    @abstractmethod
    def list_articles(self, limit: int, offset: int, since: datetime | None, query: str) -> list[dict]: ...

    @abstractmethod
    def scan_articles(self, limit: int, since: datetime | None, query: str) -> list[dict]:
        """목록 필터·집계 전용 경량 조회 — 카드 렌더에만 쓰는 긴 텍스트를 빼고 읽는다.

        /api/articles 는 수천 행을 훑어 필터를 걸고 그중 20건만 카드로 만든다.
        SWOT 근거문 4개·포스코 관점 같은 긴 컬럼을 전 행에서 읽으면 그게 비용의 대부분이다.
        분석이 끝난(analyzed_at) 행만 돌려준다.
        """

    @abstractmethod
    def changed_articles_since(self, since: str, limit: int = 5000) -> list[dict]:
        """마지막 스캔 이후 수집·재분석된 행 (상태 무관). scan 스토어 델타 갱신용.

        Supabase 이관 후 요청마다 활성 테이블 전체를 읽으면 무료 대역폭을 넘긴다.
        메모리 스토어를 두고 이 델타(회당 수십 건)만 읽어 반영한다.
        보관·삭제·미분석으로 바뀐 행은 status/analyzed_at 을 보고 스토어에서 뺀다.
        """

    @abstractmethod
    def card_details(self, ids: Sequence[str]) -> list[dict]:
        """화면에 보일 소수 기사만 카드용 전체 컬럼으로 읽는다. 입력 순서를 유지한다."""

    @abstractmethod
    def embeddings_for(self, ids: Sequence[str]) -> dict[str, list[float] | None]:
        """중복 판정 4단계에 실제로 필요한 소수 후보의 제목 임베딩만 읽는다."""

    @abstractmethod
    def article_detail(self, article_id: str) -> dict | None: ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def save_weekly_report(self, row: dict) -> None: ...

    @abstractmethod
    def list_weekly_reports(self, limit: int) -> list[dict]:
        """이력 목록 — payload·html 제외, 메타만."""

    @abstractmethod
    def get_weekly_report(self, report_id: str | None) -> dict | None:
        """report_id 가 None 이면 가장 최근 레포트."""

    @abstractmethod
    def mark_weekly_sent(self, report_id: str, sent_at: str | None, error: str | None) -> None: ...

    @abstractmethod
    def upsert_quote(self, row: dict) -> None: ...

    @abstractmethod
    def all_quotes(self) -> list[dict]: ...

    @abstractmethod
    def get_run_state(self) -> dict: ...

    @abstractmethod
    def set_run_state(self, patch: dict) -> None: ...

    @abstractmethod
    def try_acquire_pipeline_lock(self, owner: str, stale_after_sec: float) -> bool:
        """파이프라인 실행권을 얻으면 True. AWS 등에서 실수로 인스턴스가 2개
        떠도(정상 운영은 항상 1개) 수집·알림이 중복되지 않게 하는 안전장치다.
        락 소유자가 없거나, 소유자가 자신이거나(같은 프로세스의 다음 회차),
        마지막 갱신이 stale_after_sec 보다 오래됐으면(죽은 소유자로 간주) 얻는다."""

    @abstractmethod
    def log_collection(self, row: dict) -> None: ...

    @abstractmethod
    def enabled_keywords(self) -> list[dict]: ...

    @abstractmethod
    def seed_keywords(self, rows: Sequence[tuple[str, str]]) -> None: ...

    @abstractmethod
    def all_keywords(self) -> list[dict]:
        """마스터 패널 '수집 키워드 관리'용 — 켜짐·꺼짐 전부 돌려준다."""

    @abstractmethod
    def add_keyword(self, category: str, keyword: str) -> dict | None:
        """새 수집 키워드를 추가한다. 같은 (분류, 키워드)가 이미 있으면 None."""

    @abstractmethod
    def set_keyword_enabled(self, keyword_id: str, enabled: bool) -> None: ...

    @abstractmethod
    def delete_keyword(self, keyword_id: str) -> None: ...

    @abstractmethod
    def enabled_feeds(self) -> list[dict]: ...

    @abstractmethod
    def seed_feeds(self, rows: Sequence[tuple[str, str, str, bool]]) -> None: ...

    @abstractmethod
    def press_by_domain(self, domain: str) -> dict | None: ...

    @abstractmethod
    def all_press(self) -> list[dict]:
        """press_outlets 전체 행. fixpress 의 이름 재정리에 쓴다."""

    @abstractmethod
    def press_tier_by_id(self, press_id: str | None) -> int:
        """언론사 tier(1=주요지 … 3=기타). id 가 없거나 못 찾으면 3."""

    @abstractmethod
    def upsert_press(self, domain: str, name: str, tier: int, status: str) -> dict: ...

    @abstractmethod
    def update_press_name(self, domain: str, name: str, tier: int) -> None: ...

    @abstractmethod
    def sync_article_press_names(self) -> int:
        """articles.press_name 을 press_outlets 의 현재 이름으로 다시 맞춘다. 갱신 건수 반환."""

    @abstractmethod
    def queue_notification(self, article_id: str, chat_id: str, status: str, priority: int = 0) -> bool: ...

    @abstractmethod
    def pending_notifications(self, limit: int) -> list[dict]: ...

    @abstractmethod
    def mark_notification(self, notif_id: str, status: str, error: str | None) -> None: ...

    @abstractmethod
    def touch_notification(self, notif_id: str, error: str | None) -> None:
        """일시적 오류 — 재시도 횟수는 건드리지 않고 error 문구만 갱신한다."""

    @abstractmethod
    def requeue_failed_notifications(self) -> int:
        """막힌 발송 실패 건(failed · 재시도 소진된 queued)을 다시 큐로 되돌린다."""

    @abstractmethod
    def failed_notifications(self, limit: int) -> list[dict]: ...

    @abstractmethod
    def unanalyzed_articles(self, limit: int) -> list[dict]: ...




# ── SQLite 구현 ──────────────────────────────────────────────────────

ARTICLE_JSON_FIELDS = ("url_source_aliases", "keywords", "group_companies", "categories", "title_embedding")



# 카드 표시에 필요한 articles 컬럼. title_embedding(행당 ~31KB)·url_source_aliases 는
# 목록 조회에 쓰이지 않으므로 제외한다 — a.* 로 읽으면 목록 API 가 10배 느려진다.
ARTICLE_CARD_COLS = ", ".join(f"a.{c}" for c in (
    "id", "url_source", "url_canonical", "url_original", "title",
    "press_id", "press_name", "author", "published_at", "collected_at",
    "source_type", "thumbnail_url", "content_hash", "dedup_group_id",
    "is_representative", "is_backfill", "importance_score", "sentiment",
    "keywords", "group_companies", "categories", "analyzed_at", "status",
))



# 목록 스캔(필터·집계)에만 필요한 컬럼. 카드 렌더 전용 컬럼(SWOT 근거문 4개·포스코 관점·
# summary_source·URL 원본·해시·dedup 키)은 뺀다 — 수천 행에서 그게 비용의 대부분이다.
# card_tags 가 그룹사 폴백에 summary_text 를 쓰므로 그것만 남긴다.
ARTICLE_SCAN_COLS = ", ".join(f"a.{c}" for c in (
    "id", "url_canonical", "url_original", "title", "press_name", "author",
    "published_at", "source_type", "thumbnail_url", "is_backfill",
    "importance_score", "sentiment", "keywords", "group_companies", "categories",
    "analyzed_at",
))




class SqliteStorage(Storage):
    """로컬 개발용. 배열·JSON 컬럼은 JSON 문자열로 저장한다."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._local = threading.local()
        # 기존 DB 에도 telegram_log 가 없으면 만들어 둔다 — initdb 재실행 없이 '발송 로그'가 동작하도록.
        # (나머지 스키마 변경은 여전히 init_schema 가 담당)
        try:
            with sqlite3.connect(path) as _c:
                _c.execute(
                    "create table if not exists telegram_log ("
                    " id TEXT primary key, created_at TEXT not null, chat_id TEXT, kind TEXT,"
                    " article_id TEXT, text TEXT not null, ok INTEGER not null, error TEXT)")
                _c.execute("create index if not exists idx_telegram_log_time"
                           " on telegram_log (created_at desc)")
        except sqlite3.Error as exc:   # pragma: no cover
            log.warning("telegram_log 부트스트랩 실패(무시): %s", exc)

    def _conn(self) -> sqlite3.Connection:
        # 수집 루프와 API 서버가 다른 스레드에서 접근하므로 스레드별 커넥션을 쓴다.
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("pragma journal_mode = wal")
            conn.execute("pragma foreign_keys = on")
            # WAL 모드에서 synchronous=normal 은 앱 크래시에 안전하다(OS·전원 장애 시
            # 마지막 트랜잭션만 위험). 파이프라인이 문장마다 커밋하므로 fsync 부담이 큰데,
            # 이 설정으로 커밋 지연이 크게 줄고 WAL 비대화도 완화된다.
            conn.execute("pragma synchronous = normal")
            conn.execute("pragma busy_timeout = 5000")   # 쓰기 경합 시 즉시 실패 대신 5초 대기
            conn.execute("pragma temp_store = memory")    # 정렬·임시 인덱스를 메모리에
            conn.execute("pragma cache_size = -16000")    # 페이지 캐시 약 16MB (기본 2MB)
            self._local.conn = conn
        return conn

    def _rows(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        cur = self._conn().execute(sql, tuple(args))
        return [self._decode(dict(r)) for r in cur.fetchall()]

    def _one(self, sql: str, args: Sequence[Any] = ()) -> dict | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        conn = self._conn()
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        return cur

    @staticmethod
    def _decode(row: dict) -> dict:
        for field_name in ARTICLE_JSON_FIELDS:
            if field_name in row:
                default = None if field_name == "title_embedding" else []
                row[field_name] = jload(row[field_name], default)
        if "token_usage" in row:
            row["token_usage"] = jload(row["token_usage"], {})
        for bool_field in ("is_representative", "is_backfill", "enabled"):
            if bool_field in row and row[bool_field] is not None:
                row[bool_field] = bool(row[bool_field])
        return row

    @staticmethod
    def _encode(row: dict) -> dict:
        out = dict(row)
        for key, value in list(out.items()):
            if isinstance(value, (list, dict)):
                out[key] = jdump(value)
            elif isinstance(value, bool):
                out[key] = 1 if value else 0
            elif isinstance(value, datetime):
                out[key] = iso(value)
        return out

    def init_schema(self) -> None:
        conn = self._conn()
        with open(os.path.join(BACKEND_DIR, "schema_sqlite.sql"), "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        # 기존 DB 에 나중에 추가된 컬럼 채우기 (create table if not exists 로는 안 됨)
        migrations = [
            "alter table run_state add column bootstrap_at TEXT",
            "alter table run_state add column notify_paused INTEGER not null default 0",
            "alter table run_state add column notify_threshold INTEGER",
            "alter table run_state add column tg_offset INTEGER not null default 0",
            "alter table run_state add column always_notify_keywords TEXT default '[]'",
            "alter table run_state add column master_pw_hash TEXT",
            "alter table run_state add column web_pw_hash TEXT",
            "alter table run_state add column web_password TEXT",
            "alter table run_state add column notify_policy INTEGER not null default 0",
            "alter table run_state add column policy_notify_keywords TEXT default '[]'",
            "alter table run_state add column policy_required_keywords TEXT default '[]'",
            "alter table run_state add column notify_trade INTEGER not null default 0",
            "alter table run_state add column trade_notify_keywords TEXT default '[]'",
            "alter table run_state add column trade_required_keywords TEXT default '[]'",
            "alter table articles add column categories TEXT default '[]'",
            "alter table notifications add column priority INTEGER not null default 0",
            "alter table run_state add column last_weekly_report_at TEXT",
            "alter table run_state add column weekly_report_to TEXT default '[]'",
            "alter table run_state add column hard_notify_score INTEGER",
            "alter table run_state add column kakao_enabled INTEGER not null default 1",
            "alter table run_state add column kakao_refresh_token TEXT",
            "alter table run_state add column kakao_access_token TEXT",
            "alter table run_state add column kakao_token_expires_at TEXT",
            "alter table run_state add column night_start_hour INTEGER",
            "alter table run_state add column night_end_hour INTEGER",
            "alter table run_state add column night_min_score INTEGER",
            "alter table run_state add column exclude_notify_keywords TEXT default '[]'",
            "alter table run_state add column always_kw_bypass_night INTEGER not null default 1",
            "alter table run_state add column policy_exclude_keywords TEXT default '[]'",
            "alter table run_state add column trade_exclude_keywords TEXT default '[]'",
            "alter table run_state add column score_overrides TEXT default '{}'",
            "alter table run_state add column score_custom_rules TEXT default '[]'",
            "alter table run_state add column pipeline_lock_owner TEXT",
            "alter table run_state add column pipeline_lock_at TEXT",
            # 잠금 사용 여부 (2026-09-16) — NULL = 기존 방식(비밀번호 값이 있으면
            # 잠금, 없으면 해제)을 그대로 따름. 0/1 이면 그 값을 명시적으로 따름.
            "alter table run_state add column web_lock_enabled INTEGER",
            "alter table run_state add column master_lock_enabled INTEGER",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # 이미 존재
        conn.commit()

    def seen_url_sources(self, candidates: Sequence[str]) -> set[str]:
        """전체 기간 대조. 조회 1회로 끝내되 SQLite 변수 상한(999)을 넘지 않게 나눈다."""
        if not candidates:
            return set()
        found: set[str] = set()
        chunk = 400
        for i in range(0, len(candidates), chunk):
            part = list(candidates[i:i + chunk])
            marks = ",".join("?" * len(part))
            for sql in (
                f"select url_source from articles where url_source in ({marks})",
                f"select url_source from url_ledger where url_source in ({marks})",
            ):
                found.update(r["url_source"] for r in self._rows(sql, part))
            # alias 는 JSON 배열이라 IN 을 못 쓴다. 후보 수가 적으므로 스캔한다.
            remaining = [c for c in part if c not in found]
            if remaining:
                target = set(remaining)
                for row in self._rows(
                    "select url_source_aliases from articles where url_source_aliases != '[]'"
                ):
                    for alias in row["url_source_aliases"]:
                        if alias in target:
                            found.add(alias)
        return found

    def recent_url_sources(self, hours: int) -> set[str]:
        cutoff = iso(now_utc() - timedelta(hours=hours))
        out: set[str] = set()
        for row in self._rows(
            "select url_source, url_source_aliases from articles where collected_at >= ?", (cutoff,)
        ):
            out.add(row["url_source"])
            out.update(row["url_source_aliases"])
        return out

    def insert_article(self, row: dict) -> bool:
        data = self._encode(row)
        cols = ",".join(data)
        marks = ",".join("?" * len(data))
        try:
            self._exec(f"insert into articles ({cols}) values ({marks})", list(data.values()))
            return True
        except sqlite3.IntegrityError:
            # UNIQUE 위반 = 경합·재시도 상황의 최종 방어선. 정상 동작이다. (PRD F1.1)
            return False

    def update_article(self, article_id: str, patch: dict) -> None:
        if not patch:
            return
        data = self._encode(patch)
        sets = ",".join(f"{k}=?" for k in data)
        self._exec(f"update articles set {sets} where id=?", list(data.values()) + [article_id])

    def append_alias(self, article_id: str, url_source: str) -> None:
        row = self._one("select url_source_aliases from articles where id=?", (article_id,))
        if row is None:
            return
        aliases = list(row["url_source_aliases"])
        if url_source in aliases:
            return
        aliases.append(url_source)
        self._exec("update articles set url_source_aliases=? where id=?", (jdump(aliases), article_id))

    def find_by_content_hash(self, content_hash: str) -> dict | None:
        return self._one(
            "select * from articles where content_hash=? and is_representative=1 limit 1", (content_hash,)
        )

    def find_by_canonical(self, url_canonical: str) -> dict | None:
        return self._one("select * from articles where url_canonical=? limit 1", (url_canonical,))

    def recent_articles_for_dedup(self, since: datetime) -> list[dict]:
        # title_embedding 은 행당 ~31KB 이고 실제 비교는 4단계 잔여 후보 ≤10건뿐이다.
        # 여기서 다 읽으면 사이클마다 수 MB 를 헛돌린다 → embeddings_for 로 지연 조회한다.
        return self._rows(
            "select id, title, published_at, dedup_group_id, is_representative,"
            " press_id, press_name, content_hash from articles"
            " where published_at >= ? and status='active'",
            (iso(since),),
        )

    def upsert_ledger(self, url_source: str, reason: str) -> None:
        self._exec(
            "insert into url_ledger (url_source, reason, first_seen, hit_count) values (?,?,?,1)"
            " on conflict(url_source) do update set hit_count = hit_count + 1",
            (url_source, reason, iso(now_utc())),
        )

    def bump_ledger(self, url_sources: Sequence[str]) -> None:
        if not url_sources:
            return
        chunk = 400
        for i in range(0, len(url_sources), chunk):
            part = list(url_sources[i:i + chunk])
            marks = ",".join("?" * len(part))
            self._exec(
                f"update url_ledger set hit_count = hit_count + 1 where url_source in ({marks})", part
            )

    def save_body(self, article_id: str, body: str, summary_source: str) -> None:
        self._exec(
            "insert into article_bodies (article_id, body, summary_source, fetched_at)"
            " values (?,?,?,?) on conflict(article_id) do update set"
            " body=excluded.body, summary_source=excluded.summary_source, fetched_at=excluded.fetched_at",
            (article_id, body, summary_source, iso(now_utc())),
        )

    def unanalyzed_with_body(self, limit: int) -> list[dict]:
        return self._rows(
            "select a.id, a.title, a.press_id, a.press_name, a.importance_score,"
            " a.group_companies, b.body, b.summary_source"
            " from articles a join article_bodies b on b.article_id = a.id"
            " where a.analyzed_at is null and a.status='active'"
            " order by a.importance_score desc, a.collected_at asc limit ?",
            (limit,),
        )

    def unanalyzed_count(self) -> int:
        row = self._one(
            "select count(*) as n from articles a join article_bodies b on b.article_id = a.id"
            " where a.analyzed_at is null and a.status='active'"
        )
        return int(row["n"]) if row else 0

    def deferred_articles(self, limit: int) -> list[dict]:
        return self._rows(
            "select a.id, a.title, a.url_source, a.url_canonical, a.url_original,"
            " a.press_name, a.published_at, a.collected_at, a.group_companies"
            " from articles a"
            " where a.analyzed_at is null and a.status='active'"
            " and not exists (select 1 from article_bodies b where b.article_id = a.id)"
            " order by a.published_at asc limit ?",
            (limit,),
        )

    def deferred_count(self) -> int:
        row = self._one(
            "select count(*) as n from articles a"
            " where a.analyzed_at is null and a.status='active'"
            " and not exists (select 1 from article_bodies b where b.article_id = a.id)"
        )
        return int(row["n"]) if row else 0

    def delete_body(self, article_id: str) -> None:
        self._exec("delete from article_bodies where article_id=?", (article_id,))

    def cleanup_bodies(self, older_than_days: int) -> int:
        cutoff = iso(now_utc() - timedelta(days=older_than_days))
        cur = self._exec("delete from article_bodies where fetched_at < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def purge_stale_drafts(self, older_than_hours: int) -> int:
        """등록도 취소도 안 한 미리보기(draft) 기사를 지운다."""
        cutoff = iso(now_utc() - timedelta(hours=older_than_hours))
        cur = self._exec("delete from articles where status='draft' and collected_at < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def purge_stale_embeddings(self, older_than_hours: int) -> int:
        """오래된 title_embedding 을 비운다 — 중복 판정 창(약 25시간) 밖이면 다시 안 읽는다.

        임베딩은 행당 ~31KB 로 DB 용량의 대부분을 차지하는데, 캐시일 뿐이라 지워도
        무방하다(필요하면 재계산). 저장 공간을 되찾는 게 목적이다.
        """
        cutoff = iso(now_utc() - timedelta(hours=older_than_hours))
        cur = self._exec(
            "update articles set title_embedding=null"
            " where title_embedding is not null"
            " and coalesce(published_at, collected_at) < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def purge_old_articles(self, hard_days: int, soft_days: int, keep_score: int) -> int:
        """오래된 기사 삭제. 자식(요약·SWOT·본문·알림)은 on delete cascade 로 함께 삭제된다.

        · 핵심 기사(알림 대상이었음 · 중요도 keep_score 이상 · 그룹사 태그 있음)
          → hard_days 초과 시 삭제
        · 그 외 일반·주제이탈(archived) 기사 → soft_days 초과 시 삭제
        draft 는 purge_stale_drafts 가 따로 처리하므로 제외한다.
        """
        hard_cut = iso(now_utc() - timedelta(days=hard_days))
        soft_cut = iso(now_utc() - timedelta(days=soft_days))
        cur = self._exec(
            "delete from articles"
            " where status <> 'draft'"
            "   and coalesce(published_at, collected_at) < ?"           # 최소 soft_days 경과
            "   and (coalesce(published_at, collected_at) < ?"          # hard_days 넘으면 무조건
            "        or (importance_score < ?"                          # 아니면 저가치만
            "            and coalesce(group_companies, '[]') in ('[]', '')"
            "            and id not in (select article_id from notifications"
            "                           where article_id is not null and status <> 'skipped')))",
            (soft_cut, hard_cut, keep_score))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def prune_collection_logs(self, older_than_days: int) -> int:
        cutoff = iso(now_utc() - timedelta(days=older_than_days))
        cur = self._exec("delete from collection_logs where run_at < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def prune_url_ledger(self, older_than_days: int) -> int:
        cutoff = iso(now_utc() - timedelta(days=older_than_days))
        cur = self._exec("delete from url_ledger where first_seen < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def log_telegram(self, entry: dict) -> None:
        self._exec(
            "insert into telegram_log (id, created_at, chat_id, kind, article_id, text, ok, error)"
            " values (?,?,?,?,?,?,?,?)",
            (new_id(), iso(now_utc()), entry.get("chat_id"), entry.get("kind") or "기타",
             entry.get("article_id"), entry.get("text") or "",
             1 if entry.get("ok") else 0, entry.get("error")))

    def recent_telegram_logs(self, limit: int) -> list[dict]:
        return self._rows(
            "select l.id, l.created_at, l.chat_id, l.kind, l.article_id, l.text, l.ok, l.error,"
            " a.title, a.url_canonical, a.url_original"
            " from telegram_log l left join articles a on a.id = l.article_id"
            " order by l.created_at desc limit ?", (limit,))

    def prune_telegram_log(self, older_than_days: int) -> int:
        cutoff = iso(now_utc() - timedelta(days=older_than_days))
        cur = self._exec("delete from telegram_log where created_at < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def vacuum(self) -> None:
        # VACUUM 은 트랜잭션 밖에서만 실행된다. _exec 는 매 문장 커밋하므로 안전.
        self._conn().execute("vacuum")

    def save_summary(self, row: dict) -> None:
        data = self._encode(row)
        cols = ",".join(data)
        marks = ",".join("?" * len(data))
        updates = ",".join(f"{k}=excluded.{k}" for k in data if k not in ("id", "article_id"))
        self._exec(
            f"insert into summaries ({cols}) values ({marks})"
            f" on conflict(article_id) do update set {updates}",
            list(data.values()),
        )

    def save_swot(self, row: dict) -> None:
        data = self._encode(row)
        cols = ",".join(data)
        marks = ",".join("?" * len(data))
        updates = ",".join(f"{k}=excluded.{k}" for k in data if k != "article_id")
        self._exec(
            f"insert into swot_analyses ({cols}) values ({marks})"
            f" on conflict(article_id) do update set {updates}",
            list(data.values()),
        )

    def llm_calls_today(self) -> int:
        start = iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
        row = self._one("select count(*) as n from summaries where created_at >= ?", (start,))
        return int(row["n"]) if row else 0

    def list_articles(self, limit: int, offset: int, since: datetime | None, query: str) -> list[dict]:
        # a.* 를 쓰지 않는다 — title_embedding(행당 ~31KB)까지 읽어 목록 조회가 10배 느려진다.
        # 카드에 필요한 컬럼만 고른다. (임베딩은 recent_articles_for_dedup 이 따로 읽는다)
        sql = (
            f"select {ARTICLE_CARD_COLS},"
            " s.summary_text, s.perspective_text, s.summary_source,"
            " w.total_score as swot_total, w.s_score, w.w_score, w.o_score, w.t_score,"
            " w.s_text, w.w_text, w.o_text, w.t_text"
            " from articles a"
            " left join summaries s on s.article_id = a.id"
            " left join swot_analyses w on w.article_id = a.id"
            " where a.status='active' and a.is_representative=1"
        )
        args: list[Any] = []
        if since is not None:
            sql += " and a.published_at >= ?"
            args.append(iso(since))
        if query:
            sql += (" and (a.title like ? or s.summary_text like ?"
                    " or a.author like ? or a.press_name like ?)")
            args += [f"%{query}%"] * 4
        sql += " order by a.published_at desc limit ? offset ?"
        args += [limit, offset]
        return self._rows(sql, args)

    def scan_articles(self, limit: int, since: datetime | None, query: str) -> list[dict]:
        sql = (f"select {ARTICLE_SCAN_COLS}, s.summary_text"
               " from articles a"
               " left join summaries s on s.article_id = a.id"
               " where a.status='active' and a.is_representative=1"
               " and a.analyzed_at is not null")
        args: list[Any] = []
        if since is not None:
            sql += " and a.published_at >= ?"
            args.append(iso(since))
        if query:
            sql += (" and (a.title like ? or s.summary_text like ?"
                    " or a.author like ? or a.press_name like ?)")
            args += [f"%{query}%"] * 4
        sql += " order by a.published_at desc limit ?"
        args.append(limit)
        return self._rows(sql, args)

    def changed_articles_since(self, since: str, limit: int = 5000) -> list[dict]:
        return self._rows(
            f"select {ARTICLE_SCAN_COLS}, a.status, s.summary_text"
            " from articles a"
            " left join summaries s on s.article_id = a.id"
            " where a.is_representative=1"
            " and (a.collected_at >= ? or coalesce(a.analyzed_at,'') >= ?)"
            " order by a.published_at desc limit ?",
            (since, since, limit),
        )

    def card_details(self, ids: Sequence[str]) -> list[dict]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._rows(
            f"select {ARTICLE_CARD_COLS},"
            " s.summary_text, s.perspective_text, s.summary_source,"
            " w.total_score as swot_total, w.s_score, w.w_score, w.o_score, w.t_score,"
            " w.s_text, w.w_text, w.o_text, w.t_text"
            " from articles a"
            " left join summaries s on s.article_id = a.id"
            " left join swot_analyses w on w.article_id = a.id"
            f" where a.id in ({marks})",
            list(ids),
        )
        by_id = {r["id"]: r for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def embeddings_for(self, ids: Sequence[str]) -> dict[str, list[float] | None]:
        if not ids:
            return {}
        out: dict[str, list[float] | None] = {}
        chunk = 400   # SQLite 변수 상한(999) 안에서
        for i in range(0, len(ids), chunk):
            part = list(ids[i:i + chunk])
            marks = ",".join("?" * len(part))
            for r in self._rows(
                    f"select id, title_embedding from articles where id in ({marks})", part):
                out[r["id"]] = r.get("title_embedding")
        return out

    def article_detail(self, article_id: str) -> dict | None:
        rows = self._rows(
            "select a.*, s.summary_text, s.perspective_text, s.summary_source,"
            " w.total_score as swot_total, w.s_score, w.w_score, w.o_score, w.t_score,"
            " w.s_text, w.w_text, w.o_text, w.t_text"
            " from articles a"
            " left join summaries s on s.article_id = a.id"
            " left join swot_analyses w on w.article_id = a.id"
            " where a.id = ?",
            (article_id,),
        )
        return rows[0] if rows else None

    def stats(self) -> dict:
        today = iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
        total = self._one("select count(*) as n from articles where status='active'")
        today_n = self._one(
            "select count(*) as n from articles where status='active' and collected_at >= ?", (today,)
        )
        last = self._one("select max(collected_at) as t from articles")
        failed = self._one("select count(*) as n from notifications where status='failed'")
        tglog = self._one("select count(*) as n from telegram_log")
        return {
            "total": int(total["n"]) if total else 0,
            "today": int(today_n["n"]) if today_n else 0,
            "last_collected_at": last["t"] if last else None,
            "notify_failed": int(failed["n"]) if failed else 0,
            "analysis_pending": self.unanalyzed_count(),
            "telegram_log_total": int(tglog["n"]) if tglog else 0,
        }

    # ── 주간 레포트 ──────────────────────────────────────────────────
    def save_weekly_report(self, row: dict) -> None:
        self._exec(
            "insert into weekly_reports"
            " (id, period_start, period_end, generated_at, sent_at, send_error, payload, html)"
            " values (?,?,?,?,?,?,?,?)",
            (row["id"], row["period_start"], row["period_end"], row["generated_at"],
             row.get("sent_at"), row.get("send_error"),
             jdump(row["payload"]) if not isinstance(row["payload"], str) else row["payload"],
             row["html"]),
        )

    def list_weekly_reports(self, limit: int) -> list[dict]:
        return self._rows(
            "select id, period_start, period_end, generated_at, sent_at, send_error"
            " from weekly_reports order by generated_at desc limit ?", (limit,))

    def get_weekly_report(self, report_id: str | None) -> dict | None:
        if report_id:
            rows = self._rows("select * from weekly_reports where id=?", (report_id,))
        else:
            rows = self._rows(
                "select * from weekly_reports order by generated_at desc limit 1")
        if not rows:
            return None
        row = rows[0]
        row["payload"] = jload(row.get("payload"), {})
        return row

    def mark_weekly_sent(self, report_id: str, sent_at: str | None, error: str | None) -> None:
        self._exec("update weekly_reports set sent_at=?, send_error=? where id=?",
                   (sent_at, error, report_id))

    def upsert_quote(self, row: dict) -> None:
        data = self._encode(row)
        cols = ",".join(data)
        marks = ",".join("?" * len(data))
        updates = ",".join(f"{k}=excluded.{k}" for k in data if k != "symbol")
        self._exec(
            f"insert into market_quotes ({cols}) values ({marks})"
            f" on conflict(symbol) do update set {updates}",
            list(data.values()),
        )

    def all_quotes(self) -> list[dict]:
        return self._rows("select * from market_quotes")

    def get_run_state(self) -> dict:
        row = self._one("select * from run_state where key='pipeline'")
        if row is None:
            row = {"key": "pipeline", "last_success_at": None, "notify_mode": "suppressed",
                   "updated_at": iso(now_utc())}
            try:
                self._exec(
                    "insert into run_state (key,last_success_at,notify_mode,updated_at) values (?,?,?,?)",
                    ("pipeline", None, "suppressed", row["updated_at"]),
                )
            except sqlite3.IntegrityError:
                # 병렬 분석(2026-09-14)으로 여러 스레드가 동시에 처음 조회하면
                # 먼저 끝난 쪽이 이미 만들었을 수 있다 — 그 값을 그대로 쓴다.
                row = self._one("select * from run_state where key='pipeline'") or row
        return row

    def set_run_state(self, patch: dict) -> None:
        self.get_run_state()
        data = self._encode({**patch, "updated_at": iso(now_utc())})
        sets = ",".join(f"{k}=?" for k in data)
        self._exec(f"update run_state set {sets} where key='pipeline'", list(data.values()))

    def try_acquire_pipeline_lock(self, owner: str, stale_after_sec: float) -> bool:
        self.get_run_state()
        stale_before = iso(now_utc() - timedelta(seconds=stale_after_sec))
        now = iso(now_utc())
        cur = self._exec(
            "update run_state set pipeline_lock_owner=?, pipeline_lock_at=?"
            " where key='pipeline' and ("
            "  pipeline_lock_owner is null or pipeline_lock_owner=?"
            "  or pipeline_lock_at is null or pipeline_lock_at < ?)",
            (owner, now, owner, stale_before),
        )
        return bool(cur.rowcount and cur.rowcount > 0)

    def log_collection(self, row: dict) -> None:
        data = self._encode(row)
        cols = ",".join(data)
        marks = ",".join("?" * len(data))
        self._exec(f"insert into collection_logs ({cols}) values ({marks})", list(data.values()))

    def enabled_keywords(self) -> list[dict]:
        return self._rows("select * from keyword_sets where enabled=1 order by category, keyword")

    def seed_keywords(self, rows: Sequence[tuple[str, str]]) -> None:
        for category, keyword in rows:
            self._exec(
                "insert or ignore into keyword_sets (id,category,keyword,enabled) values (?,?,?,1)",
                (new_id(), category, keyword),
            )

    def all_keywords(self) -> list[dict]:
        return self._rows("select * from keyword_sets order by category, keyword")

    def add_keyword(self, category: str, keyword: str) -> dict | None:
        existing = self._one(
            "select id from keyword_sets where category=? and keyword=?", (category, keyword))
        if existing:
            return None
        kid = new_id()
        self._exec(
            "insert into keyword_sets (id,category,keyword,enabled) values (?,?,?,1)",
            (kid, category, keyword))
        return {"id": kid, "category": category, "keyword": keyword, "enabled": 1}

    def set_keyword_enabled(self, keyword_id: str, enabled: bool) -> None:
        self._exec("update keyword_sets set enabled=? where id=?", (1 if enabled else 0, keyword_id))

    def delete_keyword(self, keyword_id: str) -> None:
        self._exec("delete from keyword_sets where id=?", (keyword_id,))

    def enabled_feeds(self) -> list[dict]:
        return self._rows("select * from feed_sources where enabled=1")

    def seed_feeds(self, rows: Sequence[tuple[str, str, str, bool]]) -> None:
        """시드는 여러 번 실행해도 안전하다. url·enabled 는 시드 값으로 맞춘다."""
        for source_type, name, url, enabled in rows:
            exists = self._one(
                "select id from feed_sources where source_type=? and name=?", (source_type, name)
            )
            if exists is None:
                self._exec(
                    "insert into feed_sources (id,source_type,name,url,enabled) values (?,?,?,?,?)",
                    (new_id(), source_type, name, url, 1 if enabled else 0),
                )
            else:
                self._exec(
                    "update feed_sources set url=?, enabled=? where id=?",
                    (url, 1 if enabled else 0, exists["id"]),
                )

    def press_by_domain(self, domain: str) -> dict | None:
        return self._one("select * from press_outlets where domain=?", (domain,))

    def all_press(self) -> list[dict]:
        return self._rows("select * from press_outlets")

    def press_tier_by_id(self, press_id: str | None) -> int:
        if not press_id:
            return 3
        row = self._one("select tier from press_outlets where id=?", (press_id,))
        return int(row["tier"]) if row and row["tier"] is not None else 3

    def upsert_press(self, domain: str, name: str, tier: int, status: str) -> dict:
        existing = self.press_by_domain(domain)
        if existing:
            return existing
        row = {"id": new_id(), "domain": domain, "name": name, "tier": tier, "status": status}
        try:
            self._exec(
                "insert into press_outlets (id,domain,name,tier,status) values (?,?,?,?,?)",
                (row["id"], domain, name, tier, status),
            )
        except sqlite3.IntegrityError:
            return self.press_by_domain(domain) or row
        return row

    def update_press_name(self, domain: str, name: str, tier: int) -> None:
        self._exec(
            "update press_outlets set name=?, tier=?, status='approved' where domain=?",
            (name, tier, domain),
        )

    def sync_article_press_names(self) -> int:
        cur = self._exec(
            "update articles set press_name = (select name from press_outlets where id = articles.press_id)"
            " where press_id is not null"
            "   and press_name is not (select name from press_outlets where id = articles.press_id)"
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def queue_notification(self, article_id: str, chat_id: str, status: str, priority: int = 0) -> bool:
        try:
            self._exec(
                "insert into notifications (id,article_id,channel,chat_id,status,priority,retry_count,created_at)"
                " values (?,?,?,?,?,?,0,?)",
                (new_id(), article_id, "telegram", chat_id, status, int(priority), iso(now_utc())),
            )
            return True
        except sqlite3.IntegrityError:
            # 이미 큐에 있음 = 중복 발송 방지가 작동한 것. (§6 정합성)
            return False

    def pending_notifications(self, limit: int) -> list[dict]:
        return self._rows(
            "select n.*, a.title, a.url_canonical, a.url_original, a.press_name, a.author,"
            " a.importance_score, a.published_at, a.group_companies, a.source_type,"
            " s.summary_text, s.perspective_text"
            " from notifications n"
            " join articles a on a.id = n.article_id"
            " left join summaries s on s.article_id = a.id"
            " where n.status='queued' and n.retry_count < 3"
            " order by a.importance_score desc, n.created_at asc limit ?",
            (limit,),
        )

    def mark_notification(self, notif_id: str, status: str, error: str | None) -> None:
        sent_at = iso(now_utc()) if status == "sent" else None
        if status == "queued":
            self._exec(
                "update notifications set retry_count = retry_count + 1, error=? where id=?",
                (error, notif_id),
            )
        else:
            self._exec(
                "update notifications set status=?, error=?, sent_at=?,"
                " retry_count = retry_count + 1 where id=?",
                (status, error, sent_at, notif_id),
            )

    def touch_notification(self, notif_id: str, error: str | None) -> None:
        self._exec("update notifications set error=? where id=?", (error, notif_id))

    def requeue_failed_notifications(self) -> int:
        cur = self._exec(
            "update notifications set status='queued', retry_count=0, error=null, sent_at=null"
            " where status='failed' or (status='queued' and retry_count >= 3)")
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def failed_notifications(self, limit: int) -> list[dict]:
        """대시보드의 '발송 실패'를 눌렀을 때 보여 줄 목록.

        status='failed' 뿐 아니라, queued 로 남았지만 재시도 한도를 넘겨
        <다시 시도되지도 않고 실패로 세어지지도 않는> 것까지 함께 보여 준다.
        후자는 화면 어디에도 안 나와서 조용히 사라지던 건들이다.
        """
        return self._rows(
            "select n.id, n.status, n.error, n.retry_count, n.created_at, n.channel, n.chat_id,"
            " a.id as article_id, a.title, a.press_name, a.published_at,"
            " a.url_canonical, a.url_original, a.importance_score"
            " from notifications n"
            " left join articles a on a.id = n.article_id"
            " where n.status='failed' or (n.status='queued' and n.retry_count >= 3)"
            " order by n.created_at desc limit ?",
            (limit,),
        )

    def unanalyzed_articles(self, limit: int) -> list[dict]:
        """'분석 대기'를 눌렀을 때 보여 줄 목록 — 본문은 있는데 분석이 안 끝난 기사."""
        return self._rows(
            "select a.id as article_id, a.title, a.press_name, a.published_at, a.collected_at,"
            " a.url_canonical, a.url_original, a.importance_score,"
            " length(b.body) as body_len, b.fetched_at, b.summary_source"
            " from articles a join article_bodies b on b.article_id = a.id"
            " where a.analyzed_at is null and a.status='active'"
            " order by a.collected_at desc limit ?",
            (limit,),
        )




# ── Supabase 구현 ────────────────────────────────────────────────────

_POSTGREST_PATCHED = False




def _patch_postgrest_retry() -> None:
    """postgrest 빌더의 execute() 를 감싸 연결 끊김(Server disconnected) 시 1회 재시도한다.

    Supabase 는 유휴 연결을 조용히 끊는다 — HTTP/1.1 이라도 가끔 RemoteProtocolError 가
    난다. 모든 호출부(60여 곳)를 고치는 대신 execute 한 곳만 감싼다. 재시도는 같은
    httpx 클라이언트로 하되, 실패한 keep-alive 소켓은 풀에서 빠지고 새 연결이 쓰인다.
    """
    global _POSTGREST_PATCHED
    if _POSTGREST_PATCHED:
        return
    try:
        import httpx
        from postgrest._sync import request_builder as _rb
    except Exception:   # pragma: no cover
        return
    _transient = (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError,
                  httpx.WriteError, httpx.PoolTimeout, httpx.ConnectTimeout)
    for _name in ("SyncQueryRequestBuilder", "SyncSingleRequestBuilder",
                  "SyncMaybeSingleRequestBuilder"):
        _cls = getattr(_rb, _name, None)
        if _cls is None or "execute" not in _cls.__dict__:
            continue
        _orig = _cls.execute

        def _wrapped(self, *a, __orig=_orig, **kw):
            try:
                return __orig(self, *a, **kw)
            except _transient as exc:
                log.warning("Supabase 연결 끊김 — 재시도: %s", exc)
                time.sleep(0.5)
                return __orig(self, *a, **kw)

        _cls.execute = _wrapped
    _POSTGREST_PATCHED = True




class SupabaseStorage(Storage):
    """운영용. SQLite 구현과 완전히 같은 메서드 집합을 제공한다.

    주의: Supabase 키가 채워지기 전까지 실행 검증을 하지 못한 코드다.
    전환 시 반드시 `python backend/main.py once` 로 1회 검증 후 운영에 올린다.
    """

    def __init__(self, url: str, key: str) -> None:
        try:
            from supabase import ClientOptions, create_client
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "supabase 패키지가 없습니다. `pip install supabase` 후 다시 실행하세요."
            ) from exc
        import httpx
        _patch_postgrest_retry()
        # HTTP/2 지속 연결이 Supabase 엣지에서 조용히 끊기면 다음 요청이
        # RemoteProtocolError("Server disconnected") 로 죽는다. HTTP/1.1 + 커넥션 재시도로
        # 대부분 흡수한다(끊긴 keep-alive 소켓을 httpx 가 새 연결로 다시 시도).
        http = httpx.Client(http2=False, timeout=httpx.Timeout(30.0),
                            transport=httpx.HTTPTransport(retries=2))
        self.db = create_client(url, key, options=ClientOptions(httpx_client=http))

    def _t(self, name: str):
        return self.db.table(name)

    @staticmethod
    def _in_batches(values: Sequence[str], budget: int = 4000):
        """.in_(...) 는 값을 URL 쿼리스트링에 전부 넣는다. 구글뉴스 리다이렉트 URL 처럼
        긴 값을 수백 개 넣으면 요청 URL 이 서버 한도(약 8KB)를 넘어 400 이 난다.
        누적 길이가 budget 을 넘지 않게 나눈다(항상 최소 1개는 보낸다)."""
        batch: list[str] = []
        acc = 0
        for v in values:
            n = len(str(v)) * 3 + 1   # URL 인코딩 여유
            if batch and acc + n > budget:
                yield batch
                batch, acc = [], 0
            batch.append(v)
            acc += n
        if batch:
            yield batch

    @staticmethod
    def _page(make_query, cap: int, page: int = 1000) -> list[dict]:
        """PostgREST 는 응답을 1000행으로 자른다(Supabase max-rows). Range 로 이어 받는다.

        make_query() 는 매번 새 쿼리 빌더를 돌려주는 팩토리다(.order 까지 포함, .execute 제외).
        """
        out: list[dict] = []
        start = 0
        while start < cap:
            end = min(start + page, cap) - 1
            rows = make_query().range(start, end).execute().data or []
            out.extend(rows)
            if len(rows) < page:
                break
            start += page
        return out

    def init_schema(self) -> None:
        raise SystemExit(
            "Supabase 스키마는 자동 생성하지 않습니다.\n"
            "Supabase 대시보드 > SQL Editor 에서 backend/schema.sql 을 실행하세요."
        )

    def seen_url_sources(self, candidates: Sequence[str]) -> set[str]:
        if not candidates:
            return set()
        found: set[str] = set()
        for part in self._in_batches(list(candidates)):
            found.update(
                r["url_source"] for r in self._t("articles").select("url_source").in_("url_source", part).execute().data
            )
            found.update(
                r["url_source"] for r in self._t("url_ledger").select("url_source").in_("url_source", part).execute().data
            )
        remaining = {c for c in candidates if c not in found}
        if remaining:
            # alias 로 이미 아는 URL 인지 — 예전엔 URL 마다 contains 쿼리를 순차로 쳤다(수백 회).
            # alias 가 붙은 기사는 극소수이므로 그 컬럼만 통째로 훑는 게 훨씬 싸다.
            for r in self._page(lambda: self._t("articles")
                                .select("url_source_aliases").neq("url_source_aliases", "{}")
                                .order("collected_at", desc=True), cap=20000):
                for a in (r.get("url_source_aliases") or []):
                    if a in remaining:
                        found.add(a)
        return found

    def recent_url_sources(self, hours: int) -> set[str]:
        cutoff = iso(now_utc() - timedelta(hours=hours))
        rows = self._page(lambda: (
            self._t("articles").select("url_source,url_source_aliases")
            .gte("collected_at", cutoff).order("collected_at", desc=True)), cap=20000)
        out: set[str] = set()
        for row in rows:
            out.add(row["url_source"])
            out.update(row.get("url_source_aliases") or [])
        return out

    def insert_article(self, row: dict) -> bool:
        try:
            self._t("articles").insert(row).execute()
            return True
        except Exception as exc:  # UNIQUE 위반 = 최종 방어선 작동
            if "duplicate" in str(exc).lower() or "23505" in str(exc):
                return False
            raise

    def update_article(self, article_id: str, patch: dict) -> None:
        if patch:
            self._t("articles").update(patch).eq("id", article_id).execute()

    def append_alias(self, article_id: str, url_source: str) -> None:
        rows = self._t("articles").select("url_source_aliases").eq("id", article_id).execute().data
        if not rows:
            return
        aliases = list(rows[0].get("url_source_aliases") or [])
        if url_source in aliases:
            return
        aliases.append(url_source)
        self._t("articles").update({"url_source_aliases": aliases}).eq("id", article_id).execute()

    def find_by_content_hash(self, content_hash: str) -> dict | None:
        rows = (self._t("articles").select("*").eq("content_hash", content_hash)
                .eq("is_representative", True).limit(1).execute().data)
        return rows[0] if rows else None

    def find_by_canonical(self, url_canonical: str) -> dict | None:
        rows = self._t("articles").select("*").eq("url_canonical", url_canonical).limit(1).execute().data
        return rows[0] if rows else None

    def recent_articles_for_dedup(self, since: datetime) -> list[dict]:
        # title_embedding 은 빼고 읽는다 — 4단계 잔여 후보에만 필요하므로 embeddings_for 로 지연 조회.
        return self._page(lambda: (
            self._t("articles")
            .select("id,title,published_at,dedup_group_id,is_representative,press_id,press_name,content_hash")
            .gte("published_at", iso(since)).eq("status", "active")
            .order("published_at", desc=True)), cap=20000)

    def upsert_ledger(self, url_source: str, reason: str) -> None:
        # 요청 1회. 이미 있으면 덮어쓴다(hit_count 는 1 로). 반복 재등장 카운트는
        # bump_ledger 가 맡는다 — upsert_ledger 는 '처음 제외' 경로에서만 불린다.
        self._t("url_ledger").upsert(
            {"url_source": url_source, "reason": reason,
             "first_seen": iso(now_utc()), "hit_count": 1},
            on_conflict="url_source", ignore_duplicates=True,
        ).execute()

    def bump_ledger(self, url_sources: Sequence[str]) -> None:
        # url_sources 는 이번 회차에 다시 본 URL 전체(수백 개) — 대부분 이미 수집된
        # 기사라 원장에 없다. 원장에 있는 것만 골라(배치 조회) 한 번에 올린다.
        # 예전엔 URL 마다 select+update 를 순차로 쳐 HTTP/2 연결이 끊겼다.
        if not url_sources:
            return
        rows: list[dict] = []
        for part in self._in_batches(list(url_sources)):
            rows += (self._t("url_ledger")
                     .select("url_source,reason,first_seen,hit_count")
                     .in_("url_source", part).execute().data or [])
        if not rows:
            return
        payload = [{**r, "hit_count": (r.get("hit_count") or 0) + 1} for r in rows]
        for i in range(0, len(payload), 500):
            self._t("url_ledger").upsert(payload[i:i + 500], on_conflict="url_source").execute()

    def save_body(self, article_id: str, body: str, summary_source: str) -> None:
        self._t("article_bodies").upsert({
            "article_id": article_id, "body": body,
            "summary_source": summary_source, "fetched_at": iso(now_utc()),
        }, on_conflict="article_id").execute()

    def unanalyzed_with_body(self, limit: int) -> list[dict]:
        rows = (self._t("article_bodies")
                .select("body,summary_source,articles!inner(id,title,press_id,press_name,importance_score,group_companies,analyzed_at,status)")
                .execute().data)
        out = []
        for row in rows:
            art = row.get("articles") or {}
            if art.get("analyzed_at") is not None or art.get("status") != "active":
                continue
            out.append({
                "id": art.get("id"), "title": art.get("title"),
                "press_id": art.get("press_id"), "press_name": art.get("press_name"),
                "importance_score": art.get("importance_score"),
                "group_companies": art.get("group_companies") or [],
                "body": row.get("body"), "summary_source": row.get("summary_source"),
            })
        out.sort(key=lambda r: r.get("importance_score") or 0, reverse=True)
        return out[:limit]

    def unanalyzed_count(self) -> int:
        res = (self._t("article_bodies")
               .select("article_id,articles!inner(analyzed_at,status)", count="exact")
               .is_("articles.analyzed_at", "null").eq("articles.status", "active").execute())
        return res.count or 0

    def deferred_articles(self, limit: int) -> list[dict]:
        # 본문이 없는 미분석 활성 기사 — article_bodies 에 있는 id 를 빼고 조회한다.
        # 1,000건 넘게 쌓이면(디퍼드 백로그가 불어날 때) 페이지네이션 없이는
        # 일부만 '본문 있음'으로 잡혀, 실제로는 본문이 있는 기사가 여기 잘못
        # 섞여 들어간다. _page 로 전부 본다.
        bodies = {r["article_id"] for r in
                  self._page(lambda: self._t("article_bodies").select("article_id"), cap=20000)}
        rows = (self._t("articles")
                .select("id,title,url_source,url_canonical,url_original,press_name,"
                        "published_at,collected_at,group_companies")
                .is_("analyzed_at", "null").eq("status", "active")
                .order("published_at", desc=False).limit(limit + len(bodies)).execute()).data or []
        return [r for r in rows if r["id"] not in bodies][:limit]

    def deferred_count(self) -> int:
        # 미분석·활성 전체에서 '본문은 있는(unanalyzed_count)' 것을 뺀다.
        # 행을 다 받아 세면 max-rows(1000) 에 걸려 과소 집계된다.
        total = (self._t("articles").select("id", count="exact")
                 .is_("analyzed_at", "null").eq("status", "active").execute().count or 0)
        return max(0, total - self.unanalyzed_count())

    def delete_body(self, article_id: str) -> None:
        self._t("article_bodies").delete().eq("article_id", article_id).execute()

    def cleanup_bodies(self, older_than_days: int) -> int:
        cutoff = iso(now_utc() - timedelta(days=older_than_days))
        res = self._t("article_bodies").delete().lt("fetched_at", cutoff).execute()
        return len(res.data or [])

    def purge_stale_drafts(self, older_than_hours: int) -> int:
        cutoff = iso(now_utc() - timedelta(hours=older_than_hours))
        res = (self._t("articles").delete()
               .eq("status", "draft").lt("collected_at", cutoff).execute())
        return len(res.data or [])

    def purge_stale_embeddings(self, older_than_hours: int) -> int:
        cutoff = iso(now_utc() - timedelta(hours=older_than_hours))
        res = (self._t("articles").update({"title_embedding": None})
               .not_.is_("title_embedding", "null").lt("published_at", cutoff).execute())
        return len(res.data or [])

    def purge_old_articles(self, hard_days: int, soft_days: int, keep_score: int) -> int:
        """SqliteStorage.purge_old_articles 와 동일한 정책. PostgREST 는 서브쿼리가 안 되므로
        ① hard_days 초과분 일괄 삭제 ② soft~hard 구간은 저가치 후보를 뽑아 알림 이력과 대조 후 삭제."""
        hard_cut = iso(now_utc() - timedelta(days=hard_days))
        soft_cut = iso(now_utc() - timedelta(days=soft_days))
        total = 0
        res = (self._t("articles").delete()
               .neq("status", "draft").lt("published_at", hard_cut).execute())
        total += len(res.data or [])
        cand = self._page(lambda: (
            self._t("articles").select("id,group_companies,importance_score")
            .neq("status", "draft").lt("published_at", soft_cut)
            .lt("importance_score", keep_score).order("published_at")), cap=50000)
        ids = [r["id"] for r in cand if not (r.get("group_companies") or [])]
        if ids:
            # 알림된 적 있는지는 '후보 id 에 대해서만' 조회한다. 전체 notifications 를
            # 상한 걸어 읽으면(예전 limit 100000) 알림 이력이 그 수를 넘긴 뒤부터
            # 알림됐던 오래된 기사도 잘못 삭제된다.
            notified: set[str] = set()
            for part in self._in_batches(ids):
                notified |= {r["article_id"] for r in
                             (self._t("notifications").select("article_id")
                              .in_("article_id", part).neq("status", "skipped")
                              .execute()).data or []}
            drop = [i for i in ids if i not in notified]
            for part in self._in_batches(drop):
                self._t("articles").delete().in_("id", part).execute()
                total += len(part)
        return total

    def prune_collection_logs(self, older_than_days: int) -> int:
        cutoff = iso(now_utc() - timedelta(days=older_than_days))
        res = self._t("collection_logs").delete().lt("run_at", cutoff).execute()
        return len(res.data or [])

    def prune_url_ledger(self, older_than_days: int) -> int:
        cutoff = iso(now_utc() - timedelta(days=older_than_days))
        res = self._t("url_ledger").delete().lt("first_seen", cutoff).execute()
        return len(res.data or [])

    def log_telegram(self, entry: dict) -> None:
        self._t("telegram_log").insert({
            "chat_id": entry.get("chat_id"), "kind": entry.get("kind") or "기타",
            "article_id": entry.get("article_id"), "text": entry.get("text") or "",
            "ok": bool(entry.get("ok")), "error": entry.get("error"),
        }).execute()

    def recent_telegram_logs(self, limit: int) -> list[dict]:
        rows = (self._t("telegram_log")
                .select("id,created_at,chat_id,kind,article_id,text,ok,error")
                .order("created_at", desc=True).limit(limit).execute().data) or []
        ids = [r["article_id"] for r in rows if r.get("article_id")]
        arts = {}
        for part in self._in_batches(ids):
            for a in (self._t("articles")
                      .select("id,title,url_canonical,url_original")
                      .in_("id", part).execute().data or []):
                arts[a["id"]] = a
        for r in rows:
            a = arts.get(r.get("article_id"), {})
            r["title"] = a.get("title")
            r["url_canonical"] = a.get("url_canonical")
            r["url_original"] = a.get("url_original")
        return rows

    def prune_telegram_log(self, older_than_days: int) -> int:
        cutoff = iso(now_utc() - timedelta(days=older_than_days))
        res = self._t("telegram_log").delete().lt("created_at", cutoff).execute()
        return len(res.data or [])

    def vacuum(self) -> None:
        pass  # Postgres 는 autovacuum 이 처리한다.

    def save_summary(self, row: dict) -> None:
        self._t("summaries").upsert(row, on_conflict="article_id").execute()

    def save_swot(self, row: dict) -> None:
        self._t("swot_analyses").upsert(row, on_conflict="article_id").execute()

    def llm_calls_today(self) -> int:
        start = iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
        res = self._t("summaries").select("id", count="exact").gte("created_at", start).execute()
        return res.count or 0

    def list_articles(self, limit: int, offset: int, since: datetime | None, query: str) -> list[dict]:
        # '*' 를 쓰지 않는다 — title_embedding(행당 ~31KB)까지 실어와 목록 조회가 크게 느려진다.
        cols = ARTICLE_CARD_COLS.replace("a.", "")

        def q():
            b = (self._t("articles")
                 .select(f"{cols}, summaries(summary_text,perspective_text,summary_source),"
                         " swot_analyses(total_score,s_score,w_score,o_score,t_score,s_text,w_text,o_text,t_text)")
                 .eq("status", "active").eq("is_representative", True))
            if since is not None:
                b = b.gte("published_at", iso(since))
            if query:
                like = f"%{query}%"
                b = b.or_(f"title.ilike.{like},author.ilike.{like},press_name.ilike.{like}")
            return b.order("published_at", desc=True)

        if offset:   # 페이지네이션 호출 — 한 페이지만
            rows = q().range(offset, offset + limit - 1).execute().data or []
        else:        # 대량 조회(주간레포트 등) — max-rows(1000) 넘게 이어 받는다
            rows = self._page(q, cap=limit)
        return [self._flatten(r) for r in rows]

    def scan_articles(self, limit: int, since: datetime | None, query: str) -> list[dict]:
        cols = ARTICLE_SCAN_COLS.replace("a.", "")

        def q():
            b = (self._t("articles")
                 .select(f"{cols}, summaries(summary_text)")
                 .eq("status", "active").eq("is_representative", True)
                 .not_.is_("analyzed_at", "null"))
            if since is not None:
                b = b.gte("published_at", iso(since))
            if query:
                like = f"%{query}%"
                b = b.or_(f"title.ilike.{like},author.ilike.{like},press_name.ilike.{like}")
            return b.order("published_at", desc=True)

        return [self._flatten(r) for r in self._page(q, cap=limit)]

    def changed_articles_since(self, since: str, limit: int = 5000) -> list[dict]:
        cols = ARTICLE_SCAN_COLS.replace("a.", "")
        return [self._flatten(r) for r in self._page(lambda: (
            self._t("articles")
            .select(f"{cols}, status, summaries(summary_text)")
            .eq("is_representative", True)
            .or_(f"collected_at.gte.{since},analyzed_at.gte.{since}")
            .order("published_at", desc=True)), cap=limit)]

    def card_details(self, ids: Sequence[str]) -> list[dict]:
        if not ids:
            return []
        cols = ARTICLE_CARD_COLS.replace("a.", "")
        by_id: dict[str, dict] = {}
        for part in self._in_batches(list(ids)):
            for r in (self._t("articles")
                      .select(f"{cols}, summaries(summary_text,perspective_text,summary_source),"
                              " swot_analyses(total_score,s_score,w_score,o_score,t_score,s_text,w_text,o_text,t_text)")
                      .in_("id", part).execute().data) or []:
                by_id[r["id"]] = self._flatten(r)
        return [by_id[i] for i in ids if i in by_id]

    def embeddings_for(self, ids: Sequence[str]) -> dict[str, list[float] | None]:
        out: dict[str, list[float] | None] = {}
        for part in self._in_batches(list(ids)):
            for r in (self._t("articles").select("id,title_embedding")
                      .in_("id", part).execute().data) or []:
                out[r["id"]] = r.get("title_embedding")
        return out

    @staticmethod
    def _flatten(row: dict) -> dict:
        """중첩 조인 결과를 SQLite 구현과 같은 평평한 형태로 맞춘다."""
        out = dict(row)
        summary = out.pop("summaries", None)
        swot = out.pop("swot_analyses", None)
        if isinstance(summary, list):
            summary = summary[0] if summary else None
        if isinstance(swot, list):
            swot = swot[0] if swot else None
        out.update(summary or {})
        if swot:
            out["swot_total"] = swot.get("total_score")
            for k in ("s_score", "w_score", "o_score", "t_score", "s_text", "w_text", "o_text", "t_text"):
                out[k] = swot.get(k)
        return out

    def article_detail(self, article_id: str) -> dict | None:
        rows = (self._t("articles")
                .select("*, summaries(summary_text,perspective_text,summary_source),"
                        " swot_analyses(total_score,s_score,w_score,o_score,t_score,s_text,w_text,o_text,t_text)")
                .eq("id", article_id).execute().data)
        return self._flatten(rows[0]) if rows else None

    def stats(self) -> dict:
        today = iso(now_utc().replace(hour=0, minute=0, second=0, microsecond=0))
        total = self._t("articles").select("id", count="exact").eq("status", "active").execute().count or 0
        today_n = (self._t("articles").select("id", count="exact")
                   .eq("status", "active").gte("collected_at", today).execute().count or 0)
        last = (self._t("articles").select("collected_at")
                .order("collected_at", desc=True).limit(1).execute().data)
        failed = self._t("notifications").select("id", count="exact").eq("status", "failed").execute().count or 0
        tglog = self._t("telegram_log").select("id", count="exact").execute().count or 0
        return {
            "total": total,
            "today": today_n,
            "last_collected_at": last[0]["collected_at"] if last else None,
            "notify_failed": failed,
            "analysis_pending": self.unanalyzed_count(),
            "telegram_log_total": tglog,
        }

    def save_weekly_report(self, row: dict) -> None:
        payload = row["payload"]
        self._t("weekly_reports").insert({
            "id": row["id"], "period_start": row["period_start"], "period_end": row["period_end"],
            "generated_at": row["generated_at"], "sent_at": row.get("sent_at"),
            "send_error": row.get("send_error"),
            "payload": jload(payload, {}) if isinstance(payload, str) else payload,
            "html": row["html"],
        }).execute()

    def list_weekly_reports(self, limit: int) -> list[dict]:
        return (self._t("weekly_reports")
                .select("id, period_start, period_end, generated_at, sent_at, send_error")
                .order("generated_at", desc=True).limit(limit).execute().data)

    def get_weekly_report(self, report_id: str | None) -> dict | None:
        q = self._t("weekly_reports").select("*")
        if report_id:
            q = q.eq("id", report_id)
        else:
            q = q.order("generated_at", desc=True).limit(1)
        rows = q.execute().data
        if not rows:
            return None
        row = rows[0]
        row["payload"] = jload(row.get("payload"), {})
        return row

    def mark_weekly_sent(self, report_id: str, sent_at: str | None, error: str | None) -> None:
        (self._t("weekly_reports").update({"sent_at": sent_at, "send_error": error})
         .eq("id", report_id).execute())

    def upsert_quote(self, row: dict) -> None:
        self._t("market_quotes").upsert(row, on_conflict="symbol").execute()

    def all_quotes(self) -> list[dict]:
        return self._t("market_quotes").select("*").execute().data

    def get_run_state(self) -> dict:
        rows = self._t("run_state").select("*").eq("key", "pipeline").execute().data
        if rows:
            return rows[0]
        row = {"key": "pipeline", "last_success_at": None, "notify_mode": "suppressed",
               "updated_at": iso(now_utc())}
        try:
            self._t("run_state").insert(row).execute()
        except Exception:
            # 병렬 분석(2026-09-14)으로 여러 스레드가 동시에 처음 조회하면
            # 먼저 끝난 쪽이 이미 만들었을 수 있다 — 그 값을 다시 읽어 쓴다.
            rows = self._t("run_state").select("*").eq("key", "pipeline").execute().data
            if rows:
                return rows[0]
            raise
        return row

    def set_run_state(self, patch: dict) -> None:
        self.get_run_state()
        self._t("run_state").update({**patch, "updated_at": iso(now_utc())}).eq("key", "pipeline").execute()

    def try_acquire_pipeline_lock(self, owner: str, stale_after_sec: float) -> bool:
        self.get_run_state()
        stale_before = iso(now_utc() - timedelta(seconds=stale_after_sec))
        now = iso(now_utc())
        # UPDATE 는 Postgres 행 잠금으로 원자적이다 — 두 프로세스가 동시에 보내도
        # 하나가 먼저 커밋되면 owner 가 바뀌어 다른 하나의 WHERE 조건이 깨진다.
        rows = (self._t("run_state")
                .update({"pipeline_lock_owner": owner, "pipeline_lock_at": now})
                .eq("key", "pipeline")
                .or_(f"pipeline_lock_owner.is.null,pipeline_lock_owner.eq.{owner},"
                     f"pipeline_lock_at.is.null,pipeline_lock_at.lt.{stale_before}")
                .execute().data) or []
        return len(rows) > 0

    def log_collection(self, row: dict) -> None:
        self._t("collection_logs").insert(row).execute()

    def enabled_keywords(self) -> list[dict]:
        return self._t("keyword_sets").select("*").eq("enabled", True).execute().data

    def seed_keywords(self, rows: Sequence[tuple[str, str]]) -> None:
        payload = [{"category": c, "keyword": k, "enabled": True} for c, k in rows]
        if payload:
            self._t("keyword_sets").upsert(payload, on_conflict="category,keyword").execute()

    def all_keywords(self) -> list[dict]:
        return self._t("keyword_sets").select("*").order("category").order("keyword").execute().data

    def add_keyword(self, category: str, keyword: str) -> dict | None:
        existing = (self._t("keyword_sets").select("id")
                    .eq("category", category).eq("keyword", keyword).execute().data)
        if existing:
            return None
        row = {"category": category, "keyword": keyword, "enabled": True}
        res = self._t("keyword_sets").insert(row).execute().data
        return res[0] if res else row

    def set_keyword_enabled(self, keyword_id: str, enabled: bool) -> None:
        self._t("keyword_sets").update({"enabled": enabled}).eq("id", keyword_id).execute()

    def delete_keyword(self, keyword_id: str) -> None:
        self._t("keyword_sets").delete().eq("id", keyword_id).execute()

    def enabled_feeds(self) -> list[dict]:
        return self._t("feed_sources").select("*").eq("enabled", True).execute().data

    def seed_feeds(self, rows: Sequence[tuple[str, str, str, bool]]) -> None:
        existing = {(r["source_type"], r["name"]): r["id"]
                    for r in self._t("feed_sources").select("id,source_type,name").execute().data}
        for source_type, name, url, enabled in rows:
            key = (source_type, name)
            if key in existing:
                self._t("feed_sources").update({"url": url, "enabled": enabled}).eq("id", existing[key]).execute()
            else:
                self._t("feed_sources").insert(
                    {"source_type": source_type, "name": name, "url": url, "enabled": enabled}
                ).execute()

    def press_by_domain(self, domain: str) -> dict | None:
        rows = self._t("press_outlets").select("*").eq("domain", domain).execute().data
        return rows[0] if rows else None

    def all_press(self) -> list[dict]:
        return self._page(lambda: self._t("press_outlets").select("*").order("domain"), cap=20000)

    def press_tier_by_id(self, press_id: str | None) -> int:
        if not press_id:
            return 3
        rows = self._t("press_outlets").select("tier").eq("id", press_id).execute().data
        return int(rows[0]["tier"]) if rows and rows[0].get("tier") is not None else 3

    def upsert_press(self, domain: str, name: str, tier: int, status: str) -> dict:
        existing = self.press_by_domain(domain)
        if existing:
            return existing
        try:
            self._t("press_outlets").insert(
                {"domain": domain, "name": name, "tier": tier, "status": status}
            ).execute()
        except Exception:
            pass
        return self.press_by_domain(domain) or {"domain": domain, "name": name, "tier": tier, "status": status}

    def update_press_name(self, domain: str, name: str, tier: int) -> None:
        self._t("press_outlets").update(
            {"name": name, "tier": tier, "status": "approved"}
        ).eq("domain", domain).execute()

    def sync_article_press_names(self) -> int:
        # PostgREST 는 응답을 1000행으로 자른다. .execute() 를 그냥 쓰면 기사가 1000건
        # 넘을 때 나머지는 검사조차 안 돼 언론사명 정정이 조용히 반쪽만 반영된다
        # (실제 사례: cmd_fixpress 가 16개 매체명을 고쳤는데 기사는 1건만 동기화됨 —
        # 3600여 건 중 앞쪽 1000건 안에 그 매체 기사가 거의 없었다). _page 로 전부 본다.
        names = {r["id"]: r["name"] for r in self._page(
            lambda: self._t("press_outlets").select("id,name"), cap=20000)}
        rows = self._page(lambda: (
            self._t("articles").select("id,press_id,press_name").not_.is_("press_id", "null")
        ), cap=100000)
        changed = 0
        for row in rows:
            want = names.get(row["press_id"])
            if want and want != row.get("press_name"):
                self._t("articles").update({"press_name": want}).eq("id", row["id"]).execute()
                changed += 1
        return changed

    def queue_notification(self, article_id: str, chat_id: str, status: str, priority: int = 0) -> bool:
        try:
            self._t("notifications").insert({
                "article_id": article_id, "channel": "telegram", "chat_id": chat_id,
                "status": status, "priority": int(priority), "retry_count": 0,
                "created_at": iso(now_utc()),
            }).execute()
            return True
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "23505" in str(exc):
                return False
            raise

    def pending_notifications(self, limit: int) -> list[dict]:
        # PostgREST 는 notifications->articles 처럼 다대일로 임베드된 테이블의 컬럼으로
        # 부모 행을 정렬하지 못한다(foreign_table 정렬은 1:N 임베드 배열 내부 정렬용).
        # 그래서 importance_score 내림차순 정렬은 파이썬에서 한다 — 단, .limit(limit) 을
        # 먼저 걸어버리면 created_at 오름차순으로 잘린 뒤(가장 오래된 것부터) 그 안에서만
        # 재정렬하게 되어, 대기 건수가 많을 때(예: 야간 억제 후 한꺼번에 풀릴 때) 정작
        # 중요도가 높은 기사가 뒤로 밀리는 문제가 있었다. 실제 하루 대기량이 넘지 않을
        # 만큼 넉넉한 상한(2000)까지 모두 가져온 뒤 정렬하고, 그다음에 limit 만큼 자른다.
        rows = self._page(lambda: (
            self._t("notifications")
            .select("*, articles(title,url_canonical,url_original,press_name,author,"
                    "importance_score,published_at,group_companies,source_type,"
                    "summaries(summary_text,perspective_text))")
            .eq("status", "queued").lt("retry_count", 3)
            .order("created_at")
        ), cap=2000)
        out = []
        for row in rows:
            article = row.pop("articles", None) or {}
            summary = article.pop("summaries", None)
            if isinstance(summary, list):
                summary = summary[0] if summary else None
            out.append({**row, **article, **(summary or {})})
        out.sort(key=lambda r: r.get("importance_score") or 0, reverse=True)
        return out[:limit]

    def mark_notification(self, notif_id: str, status: str, error: str | None) -> None:
        rows = self._t("notifications").select("retry_count").eq("id", notif_id).execute().data
        retry = (rows[0]["retry_count"] if rows else 0) + 1
        patch: dict[str, Any] = {"retry_count": retry, "error": error}
        if status != "queued":
            patch["status"] = status
            patch["sent_at"] = iso(now_utc()) if status == "sent" else None
        self._t("notifications").update(patch).eq("id", notif_id).execute()

    def touch_notification(self, notif_id: str, error: str | None) -> None:
        self._t("notifications").update({"error": error}).eq("id", notif_id).execute()

    def requeue_failed_notifications(self) -> int:
        patch = {"status": "queued", "retry_count": 0, "error": None, "sent_at": None}
        done = (self._t("notifications").update(patch)
                .eq("status", "failed").execute().data) or []
        stuck = (self._t("notifications").update(patch)
                 .eq("status", "queued").gte("retry_count", 3).execute().data) or []
        return len(done) + len(stuck)

    def failed_notifications(self, limit: int) -> list[dict]:
        # PostgREST 는 or() 안에서 and() 를 중첩할 수 있다.
        rows = (self._t("notifications")
                .select("id,status,error,retry_count,created_at,channel,chat_id,article_id")
                .or_("status.eq.failed,and(status.eq.queued,retry_count.gte.3)")
                .order("created_at", desc=True).limit(limit).execute().data) or []
        ids = [r["article_id"] for r in rows if r.get("article_id")]
        arts = {}
        for part in self._in_batches(ids):
            for a in (self._t("articles").select(
                    "id,title,press_name,published_at,url_canonical,url_original,importance_score")
                    .in_("id", part).execute().data or []):
                arts[a["id"]] = a
        out = []
        for r in rows:
            a = arts.get(r.get("article_id"), {})
            out.append({**r, "title": a.get("title"), "press_name": a.get("press_name"),
                        "published_at": a.get("published_at"),
                        "url_canonical": a.get("url_canonical"),
                        "url_original": a.get("url_original"),
                        "importance_score": a.get("importance_score")})
        return out

    def unanalyzed_articles(self, limit: int) -> list[dict]:
        bodies = (self._t("article_bodies").select("article_id,fetched_at,summary_source")
                  .order("fetched_at", desc=True).limit(1000).execute().data) or []
        by_id = {b["article_id"]: b for b in bodies}
        if not by_id:
            return []
        rows: list[dict] = []
        for part in self._in_batches(list(by_id)):
            rows += (self._t("articles").select(
                     "id,title,press_name,published_at,collected_at,url_canonical,url_original,importance_score")
                     .is_("analyzed_at", "null").eq("status", "active")
                     .in_("id", part).order("collected_at", desc=True).execute().data) or []
        rows.sort(key=lambda r: r.get("collected_at") or "", reverse=True)
        return [{**r, "article_id": r["id"],
                 "fetched_at": by_id.get(r["id"], {}).get("fetched_at"),
                 "summary_source": by_id.get(r["id"], {}).get("summary_source"),
                 "body_len": None} for r in rows[:limit]]




def make_storage(cfg: Config) -> Storage:
    if cfg.db_backend == "supabase":
        log.info("저장소: Supabase")
        return SupabaseStorage(cfg.supabase_url, cfg.supabase_service_role_key)
    log.info("저장소: SQLite (%s)", os.path.relpath(cfg.sqlite_path, ROOT_DIR))
    return SqliteStorage(cfg.sqlite_path)




# =====================================================================
# 4. 시드 데이터 (PRD F1.3 — 코드 하드코딩 금지, DB 로 관리)
#    아래 목록은 "최초 1회 DB 에 넣는 초기값"이며, 이후 수정은 DB 에서 한다.
# =====================================================================

SEED_KEYWORDS: list[tuple[str, str]] = (
    [("그룹사", k) for k in
     ["포스코", "포스코홀딩스", "포스코퓨처엠", "포스코DX", "포스코인터내셔널", "포스코이앤씨", "POSCO"]]
    + [("산업", k) for k in
       ["이차전지", "배터리 소재", "양극재", "음극재", "전구체", "리튬", "니켈", "흑연",
        "전고체 배터리", "나트륨 배터리", "LFP",
        # 양극재·음극재·차세대 배터리 개발·연구 (포스코퓨처엠 사업 직결)
        "전고체 전해질", "리튬메탈 배터리", "황리튬 배터리", "실리콘 음극재", "하이니켈 양극재",
        "단결정 양극재", "건식 전극", "차세대 배터리", "배터리 소재 개발", "이차전지 신소재",
        "배터리 연구", "양극재 신기술", "음극재 신기술",
        # 전방 수요 — 셀 업체·전기차·ESS (사용자 지정)
        "배터리 수주", "배터리 공장 투자", "전기차 판매", "전기차 캐즘", "전기차 보조금",
        "테슬라 배터리", "CATL 배터리", "BYD 배터리", "LG에너지솔루션", "삼성SDI", "SK온",
        "ESS 시장", "에너지저장장치", "ESS 수주", "탄산리튬 가격", "니켈 가격",
        # 소재 경쟁사 — 포스코퓨처엠 양극재·음극재·전구체 직접 경쟁 (사용자 지정)
        "에코프로비엠", "엘앤에프", "에코프로머티리얼즈", "코스모신소재",
        "대주전자재료", "나노신소재", "중국 양극재", "일본 양극재"]]
    # '정책' 카테고리는 Google 검색 시 site:www.korea.kr 로 한정된다 (대한민국 정책브리핑).
    # 포스코 산업(철강·이차전지·에너지·통상·환경규제·인프라)에 영향이 있는 부처 발표를 폭넓게 수집.
    + [("정책", k) for k in
       ["철강", "이차전지", "배터리", "리튬", "전기요금", "전력수급", "에너지",
        "탄소중립", "배출권거래제", "탄소국경조정제도", "RE100", "수소경제",
        "공급망", "핵심광물", "통상", "관세", "무역협정", "산업단지", "특화단지",
        "제조업 지원", "투자 인센티브", "국가전략기술",
        "산업통상자원부", "기후에너지환경부", "환경부", "기획재정부",
        "국토교통부", "고용노동부", "과학기술정보통신부", "중소벤처기업부",
        "기획재정부 예산", "국회 예산", "예산결산특별위원회", "국정감사"]]
    # '통상' — 미국·유럽·중국·베트남·일본 등 주요국의 포스코 관련 산업(배터리·철강) 통상 조치.
    + [("통상", k) for k in
       ["IRA 배터리", "FEOC 배터리", "OBBBA 배터리", "미국 배터리 보조금", "미국 IRA 세부지침",
        "CBAM 철강", "EU 탄소국경조정 철강", "EU 핵심원자재법", "EU 배터리 규정",
        "중국 흑연 수출통제", "중국 배터리 소재 수출", "중국 요소 수출제한", "중국 갈륨 게르마늄",
        "미국 철강 관세", "무역확장법 232조 철강", "철강 세이프가드", "US 상호관세 철강",
        "철강 반덤핑", "베트남 철강 반덤핑", "일본 소재 수출규제", "일본 철강 통상",
        "이차전지 공급망 재편", "배터리 디리스킹", "철강 통상마찰", "글로벌 관세 전쟁"]]
)



# 수집 소스 시드 — (source_type, name, url, enabled)
#
# google_rss 를 기본 비활성으로 둔 이유:
#   Google News RSS 의 링크는 news.google.com/rss/articles/CBMi... 형태의 토큰이며,
#   원문 URL 이 JS 로만 해제된다. HTML 어디에도 원문 주소가 없어 공식적인 방법으로는
#   풀 수 없다. 우회 해제는 PRD §7-4(약관 준수)에 어긋나므로 하지 않는다.
#   → 제목만 얻고 본문·링크를 못 얻어 매 실행 수백 건의 무용한 HTTP 요청만 발생한다.
#   네이버 검색 API 키를 넣거나(권장), 아래 언론사 RSS 로 커버한다.
#
# naver_api 는 키가 있을 때만 실제로 동작한다(originallink 가 원문 URL 이라 해제 불필요).
SEED_FEEDS: list[tuple[str, str, str, bool]] = [
    ("google_rss", "Google News", "", False),
    ("naver_api", "Naver 뉴스 검색", "", True),
    # 언론사 자체 RSS — 원문 URL 을 직접 주므로 리다이렉트 해제가 필요 없다.
    # 키워드 필터는 수집 후 로컬에서 적용한다(F1.3 keyword_sets 기준).
    ("rss", "연합뉴스 경제", "https://www.yna.co.kr/rss/economy.xml", True),
    ("rss", "연합뉴스 산업", "https://www.yna.co.kr/rss/industry.xml", True),
    ("rss", "전자신문", "https://rss.etnews.com/Section901.xml", True),
    ("rss", "매일경제 경제", "https://www.mk.co.kr/rss/30100041/", True),
    ("rss", "매일경제 기업", "https://www.mk.co.kr/rss/50100032/", True),
    ("rss", "머니투데이", "https://rss.mt.co.kr/mt_news.xml", True),
    ("rss", "아시아경제", "https://www.asiae.co.kr/rss/stock.htm", True),
    ("rss", "뉴시스 경제", "https://newsis.com/RSS/economy.xml", True),
    ("rss", "전기신문", "https://www.electimes.com/rss/allArticle.xml", True),
    # 인사·부고 전용 스트림 (다른 매체의 [부고]·[인사] 는 위 피드·네이버에서 제목으로 잡힌다)
    ("rss", "연합뉴스 인물", "https://www.yna.co.kr/rss/people.xml", True),
]



# 도메인 → (언론사명, tier). tier 1=주요지, 2=업계·경제지, 3=기타 (PRD F2.3)
SEED_PRESS: dict[str, tuple[str, int]] = {
    "chosun.com": ("조선일보", 1), "donga.com": ("동아일보", 1), "joongang.co.kr": ("중앙일보", 1),
    "hani.co.kr": ("한겨레", 1), "khan.co.kr": ("경향신문", 1), "yna.co.kr": ("연합뉴스", 1),
    "news1.kr": ("뉴스1", 1), "newsis.com": ("뉴시스", 1), "kbs.co.kr": ("KBS", 1),
    "imbc.com": ("MBC", 1), "sbs.co.kr": ("SBS", 1), "ytn.co.kr": ("YTN", 1),
    "hankyung.com": ("한국경제", 2), "mk.co.kr": ("매일경제", 2), "sedaily.com": ("서울경제", 2),
    "fnnews.com": ("파이낸셜뉴스", 2), "edaily.co.kr": ("이데일리", 2), "mt.co.kr": ("머니투데이", 2),
    "etnews.com": ("전자신문", 2), "thelec.kr": ("전자부품 전문 미디어", 2),
    "econovill.com": ("이코노믹리뷰", 2), "ebn.co.kr": ("EBN", 2), "fetv.co.kr": ("FETV", 2),
    "kookje.co.kr": ("국제신문", 2), "asiae.co.kr": ("아시아경제", 2),
    "electimes.com": ("전기신문", 2), "theguru.co.kr": ("더구루", 3),
    "economist.co.kr": ("이코노미스트", 2), "biz.chosun.com": ("조선비즈", 2),
    "kyongbuk.co.kr": ("경북일보", 3), "kwnews.co.kr": ("강원일보", 3),
    "kmaeil.com": ("경기매일", 3), "idomin.com": ("경남도민일보", 3),
    # 네이버 검색으로 들어오는 매체 보강
    "ajunews.com": ("아주경제", 2), "heraldcorp.com": ("헤럴드경제", 2),
    "segye.com": ("세계일보", 1), "imaeil.com": ("매일신문", 2),
    "kbmaeil.com": ("경북매일신문", 3), "namdonews.com": ("남도일보", 3),
    "g-enews.com": ("글로벌이코노믹", 2), "gukjenews.com": ("국제뉴스", 3),
    "dnews.co.kr": ("대한경제", 2), "dealsitetv.com": ("딜사이트경제TV", 2),
    "metroseoul.co.kr": ("메트로신문", 3), "newstown.co.kr": ("뉴스타운", 3),
    "eroun.net": ("이로운넷", 3), "job-post.co.kr": ("잡포스트", 3),
    "pinpointnews.co.kr": ("핀포인트뉴스", 3), "weeklytoday.com": ("위클리오늘", 3),
    "ziksir.com": ("직썰", 3), "asiatoday.co.kr": ("아시아투데이", 2),
    "moneys.co.kr": ("머니S", 2), "newsprime.co.kr": ("프라임경제", 3),
    "biz.heraldcorp.com": ("헤럴드경제", 2), "it.chosun.com": ("IT조선", 2),
    "dt.co.kr": ("디지털타임스", 2), "inews24.com": ("아이뉴스24", 2),
    "zdnet.co.kr": ("지디넷코리아", 2), "wowtv.co.kr": ("한국경제TV", 2),
    "mbn.co.kr": ("MBN", 1), "jtbc.co.kr": ("JTBC", 1), "hankookilbo.com": ("한국일보", 1),
    "munhwa.com": ("문화일보", 1), "seoul.co.kr": ("서울신문", 1),
    "kmib.co.kr": ("국민일보", 1), "hellodd.com": ("헬로디디", 3),
    "greened.kr": ("녹색경제신문", 3), "e2news.com": ("이투뉴스", 3),
    "ekn.kr": ("에너지경제", 2), "energy-news.co.kr": ("에너지뉴스", 3),
    "gasnews.com": ("가스신문", 3), "todayenergy.kr": ("투데이에너지", 3),
    "cnews.co.kr": ("건설경제", 2), "ceoscoredaily.com": ("CEO스코어데일리", 3),
    "biz.newdaily.co.kr": ("뉴데일리경제", 3), "newdaily.co.kr": ("뉴데일리", 3),
    "sisajournal.com": ("시사저널", 2), "sison.co.kr": ("시사온", 3),
    "wikitree.co.kr": ("위키트리", 3), "nspna.com": ("NSP통신", 3),
    "the-pr.co.kr": ("더피알", 3), "b-economy.co.kr": ("비즈니스경제", 3),
    "fntoday.co.kr": ("파이낸셜투데이", 3), "goodkyung.com": ("굿모닝경제", 3),
    "kukinews.com": ("쿠키뉴스", 2), "m-i.kr": ("매일일보", 3), "newstree.kr": ("뉴스트리", 3),
    "biz.sbs.co.kr": ("SBS Biz", 2), "sbsbiz.co.kr": ("SBS Biz", 2),
    "digitaltoday.co.kr": ("디지털투데이", 3), "getnews.co.kr": ("글로벌경제신문", 3),
    "polinews.co.kr": ("폴리뉴스", 3), "newsway.co.kr": ("뉴스웨이", 3),
    "biztribune.co.kr": ("비즈트리뷴", 3), "sisaweek.com": ("시사위크", 3),
    "widedaily.com": ("와이드경제", 3), "bizwnews.com": ("비즈월드", 3),
    "ferrotimes.com": ("페로타임즈", 3), "snmnews.com": ("철강금속신문", 2),
    "steeldaily.co.kr": ("스틸데일리", 2), "e-mj.co.kr": ("월간 리싸이클링", 3),
    "tf.co.kr": ("더팩트", 3), "newspim.com": ("뉴스핌", 2),
    "shinailbo.co.kr": ("신아일보", 3), "viva100.com": ("브릿지경제", 3),
    "youngnak.net": ("영남일보", 3), "yeongnam.com": ("영남일보", 3),
    "dailian.co.kr": ("데일리안", 3), "ohmynews.com": ("오마이뉴스", 2),
    "pressian.com": ("프레시안", 2), "mediapen.com": ("미디어펜", 3),
    "kihoilbo.co.kr": ("기호일보", 3), "joongboo.com": ("중부일보", 3),
    # 네이버·RSS 로 들어오는 매체 2차 보강 (도메인 표기 제거)
    "4th.kr": ("포쓰저널", 3), "autodaily.co.kr": ("엠투데이", 3),
    "banronbodo.com": ("반론보도닷컴", 3), "venturesquare.net": ("벤처스퀘어", 3),
    "etoday.co.kr": ("이투데이", 2), "e-platform.net": ("이플랫폼", 3),
    "efnews.co.kr": ("이에프뉴스", 3), "enewstoday.co.kr": ("이뉴스투데이", 3),
    "ggilbo.com": ("금강일보", 3), "hidomin.com": ("경북도민일보", 3),
    "impacton.net": ("임팩트온", 3), "ksmnews.co.kr": ("경상매일신문", 3),
    "lawissue.co.kr": ("로이슈", 3), "newstomato.com": ("뉴스토마토", 2),
    "nocutnews.co.kr": ("노컷뉴스", 2), "pointdaily.co.kr": ("포인트데일리", 3),
    "thebell.co.kr": ("더벨", 2), "thebigdata.co.kr": ("빅데이터뉴스", 3),
    "bbsi.co.kr": ("BBS", 2), "biotimes.co.kr": ("바이오타임즈", 3),
    "bizwatch.co.kr": ("비즈워치", 2), "bloter.net": ("블로터", 2),
    "breaknews.com": ("브레이크뉴스", 3), "chungnamilbo.co.kr": ("충남일보", 3),
    "cnbnews.com": ("CNB뉴스", 3), "cnbizm.com": ("CNB저널", 3),
    "consumernews.co.kr": ("소비자가만드는신문", 3), "cstimes.com": ("컨슈머타임스", 3),
    "dailypop.kr": ("데일리팝", 3), "ddaily.co.kr": ("디지털데일리", 2),
    "dkilbo.com": ("대경일보", 3), "einfomax.co.kr": ("연합인포맥스", 2),
    "esgeconomy.com": ("ESG경제", 3), "finomy.com": ("현대경제신문", 3),
    "fntimes.com": ("한국금융신문", 2), "goodmorningcc.com": ("굿모닝충청", 3),
    "hankooki.com": ("한국일보", 1), "hellot.net": ("헬로티", 3),
    "idaegu.co.kr": ("대구신문", 3), "idaegu.com": ("대구신문", 3),
    "jeollailbo.com": ("전라일보", 3), "jeonmin.co.kr": ("전민일보", 3),
    "joseilbo.com": ("조세일보", 3), "kbsm.net": ("경북신문", 3),
    "korea.kr": ("대한민국 정책브리핑", 2), "koreaherald.com": ("코리아헤럴드", 2),
    "koreaittimes.com": ("코리아IT타임스", 3), "koreajoongangdaily.com": ("코리아중앙데일리", 2),
    "ksilbo.co.kr": ("경상일보", 3), "kyeonggi.com": ("경기일보", 3),
    "laborplus.co.kr": ("참여와혁신", 3), "megaeconomy.co.kr": ("메가경제", 3),
    "mydaily.co.kr": ("마이데일리", 3), "naeil.com": ("내일신문", 2),
    "news2day.co.kr": ("뉴스투데이", 3), "newscj.com": ("천지일보", 3),
    "newsmaker.or.kr": ("뉴스메이커", 3), "newsroad.co.kr": ("뉴스로드", 3),
    "newsworks.co.kr": ("뉴스웍스", 3), "popcornnews.net": ("팝콘뉴스", 3),
    "seoulfn.com": ("서울파이낸스", 2), "sisafocus.co.kr": ("시사포커스", 3),
    "startuptoday.co.kr": ("스타트업투데이", 3), "suhyupnews.co.kr": ("수산경제신문", 3),
    "techholic.co.kr": ("테크홀릭", 3), "thefairnews.co.kr": ("공정뉴스", 3),
    "tournews21.com": ("투어뉴스21", 3), "whitepaper.co.kr": ("화이트페이퍼", 3),
    "womaneconomy.co.kr": ("여성경제신문", 3), "newslock.co.kr": ("뉴스락", 3),
    "sentv.co.kr": ("서울경제TV", 2), "thepublic.kr": ("더퍼블릭", 3),
    "topstarnews.net": ("톱스타뉴스", 3), "jeonmae.co.kr": ("전매신문", 3),
    "chosunbiz.com": ("조선비즈", 2), "sisain.co.kr": ("시사IN", 2),
    "segyebiz.com": ("세계비즈", 2), "sisajournal-e.com": ("시사저널이코노미", 3),
    # 사용자 확인 매체 (2026-09-02)
    "iminju.net": ("민주신문", 3), "mfgkr.com": ("MFG", 3),
    "cbci.co.kr": ("CBC뉴스", 3), "ccdn.co.kr": ("충청매일", 3),
    "dailyt.co.kr": ("데일리환경", 3), "dizzotv.com": ("디지틀조선TV", 3),
    "enetnews.co.kr": ("이넷뉴스", 3), "handmk.com": ("핸드메이커", 3),
    "jjn.co.kr": ("전북중앙", 3), "jin.co.kr": ("전북중앙", 3),
    "joongangenews.com": ("중앙이코노미뉴스", 3), "joongangnews.com": ("중앙뉴스", 3),
    "mtnews.net": ("기계신문", 3), "the-today.com": ("더투데이", 3),
    "thefirstmedia.net": ("더퍼스트미디어", 3), "thepowernews.co.kr": ("더파워", 3),
    "theviewers.co.kr": ("뷰어스", 3), "bizwork.co.kr": ("비즈워크", 3),
    "energydaily.co.kr": ("에너지데일리", 3), "thevaluenews.co.kr": ("더밸류뉴스", 3),
    "ftoday.co.kr": ("파이낸셜투데이", 3), "s-journal.co.kr": ("S저널", 3),
    "financialreview.co.kr": ("파이낸셜리뷰", 3), "dealsite.co.kr": ("딜사이트", 2),
    "autotimes.co.kr": ("오토타임즈", 3), "businesskorea.co.kr": ("비즈니스코리아", 3),
    "businesspost.co.kr": ("비즈니스포스트", 2), "dailycar.co.kr": ("데일리카", 3),
    "dongascience.com": ("동아사이언스", 2), "hansbiz.co.kr": ("한스경제", 3),
    "industrynews.co.kr": ("인더스트리뉴스", 3), "mediawatch.kr": ("미디어워치", 3),
    "newsworker.co.kr": ("뉴스워커", 3), "the-biz.co.kr": ("더비즈온", 3),
    "srtimes.kr": ("SR타임스", 3), "ulsanpress.net": ("울산신문", 3),
    "iusm.co.kr": ("울산매일신문", 3), "koreatimes.co.kr": ("코리아타임스", 2),
    "sateconomy.co.kr": ("토요경제", 3), "socialvalue.kr": ("소셜밸류", 3),
    "inthenews.co.kr": ("인더뉴스", 3), "press9.kr": ("프레스나인", 3),
    "mtn.co.kr": ("머니투데이방송", 2),
    # 사용자 확인 매체 (2026-09-08) — 도메인·부제 그대로 노출되던 것 정정
    "wsobi.com": ("여성소비자신문", 3), "tbc.co.kr": ("TBC", 2),
    "ppss.kr": ("PPSS", 3), "ktv.go.kr": ("KTV 국민방송", 2),
    # 홈페이지 og:site_name·<title> 이 영문뿐이라(예: "JTV", "CatchNews") 자동 복구가
    # 안 되는 매체 — 한글 정식명을 수동으로 등록한다. (2026-09-10)
    "jtv.co.kr": ("전주방송", 3), "catchnews.kr": ("캐치뉴스", 3),
    "kpinews.kr": ("KPI뉴스", 3), "kjdaily.com": ("광주매일신문", 3),
    "kgnews.co.kr": ("경기신문", 3), "gosiweek.com": ("피앤피뉴스", 3),
    "unn.net": ("한국대학신문", 3), "ttlnews.com": ("퍼블릭뉴스통신", 3),
    "the-stock.kr": ("더스탁", 3), "apnews.kr": ("AP신문", 3),
    # 홈페이지가 영문/도메인만 노출해 자동 복구가 안 되던 매체 (2026-09-16)
    "osen.co.kr": ("OSEN", 2), "spotvnews.co.kr": ("스포티비뉴스", 2),
    "medigatenews.com": ("메디게이트뉴스", 3), "ilyosisa.co.kr": ("일요시사", 3),
    "journalist.or.kr": ("기자협회보", 3), "bntnews.co.kr": ("bnt뉴스", 3),
    "fashionbiz.co.kr": ("패션비즈", 3), "apparelnews.co.kr": ("어패럴뉴스", 3),
    "elle.co.kr": ("엘르", 3), "wkorea.com": ("더블유코리아", 3),
    "kwangju.co.kr": ("광주일보", 2), "cjb.co.kr": ("CJB청주방송", 3),
    "mbcgn.kr": ("MBC경남", 3), "yakup.com": ("약업신문", 3),
    "besteleven.com": ("베스트일레븐", 3), "ddanzi.com": ("딴지일보", 3),
    "voakorea.com": ("VOA 한국어", 3), "g1tv.co.kr": ("G1방송", 3),
    "gamefocus.co.kr": ("게임포커스", 3), "mstoday.co.kr": ("MS투데이", 3),
    "swtvnews.com": ("SWTV", 3),
}
