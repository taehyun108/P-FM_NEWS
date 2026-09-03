"""
P-FM NEWS — 포스코 그룹 뉴스 수집 · 분석 · 아카이브 파이프라인 (파이썬 단일 파일)

PRD.md v0.5 / PLAN.md 기준 구현.

실행 방법
    python backend/main.py initdb     # 스키마 생성 + 시드 데이터 입력 (최초 1회)
    python backend/main.py once       # 파이프라인 1회 실행
    python backend/main.py serve      # API + 프론트엔드 서버
    python backend/main.py run        # 서버 + 1분 주기 수집 루프 (운영 모드)
    python backend/main.py selftest   # 내장 검증 (DB · 외부 API 불필요)

설계 원칙
    1. 네트워크 요청과 LLM 호출은 게이트 G2 · G2.5 를 통과한 항목에만 발생한다. (PRD F1.1)
    2. 모든 키는 .env 에서만 읽는다. 코드에 값을 쓰지 않는다.
    3. 저장소 계층을 분리해 SQLite ↔ Supabase 를 값 하나로 전환한다. (PLAN 0-4)
"""

from __future__ import annotations

import hashlib
import hmac
import html as html_mod
import importlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
import uuid
from abc import ABC, abstractmethod
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ─────────────────────────────────────────────────────────────────────
# 로깅 — 키 값은 절대 출력하지 않는다. (env-secret-guard §1-5)
#
# Windows 콘솔 기본 코드페이지(cp949)로는 로그 파일에 한글이 깨져 저장된다.
# 표준 출력을 UTF-8로 맞춰 리다이렉트한 로그도 그대로 읽히게 한다.
# ─────────────────────────────────────────────────────────────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pfm")

# 라이브러리 잡음 억제 — readability 는 "ruthless removal did not work.",
# httpx 는 요청마다 'HTTP Request: ...' 를 INFO 로 찍는다. 레벨 조정만으로는
# 라이브러리가 다시 켜는 경우가 있어, 루트 핸들러에 필터를 직접 건다.
_NOISE_SUBSTRINGS = ("HTTP Request:", "ruthless removal", "useless removal")


class _NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(s in msg for s in _NOISE_SUBSTRINGS)


def _hush_libraries() -> None:
    for name in ("readability", "readability.readability", "httpx", "httpcore",
                 "openai", "openai._base_client", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, _NoiseFilter) for f in handler.filters):
            handler.addFilter(_NoiseFilter())


_hush_libraries()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT_DIR, "backend")
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")


# =====================================================================
# 1. 설정 (PRD §6 보안 / env-secret-guard)
# =====================================================================

def _clean(value: str | None) -> str:
    """.env 값에서 인라인 주석과 따옴표를 제거한다.

    `POLL_INTERVAL_SEC=60   # 폴링 주기` 처럼 주석이 붙은 줄을 안전하게 다룬다.
    """
    if value is None:
        return ""
    v = value.strip()
    # 따옴표로 감싼 값은 그대로 둔다(주석 기호가 값의 일부일 수 있다).
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    # 공백 뒤의 # 부터는 주석으로 본다.
    v = re.split(r"\s+#", v, maxsplit=1)[0]
    return v.strip()


def load_dotenv_file(path: str) -> None:
    """.env 를 읽어 os.environ 에 채운다.

    python-dotenv 가 없어도 동작하도록 직접 파싱한다. 이미 설정된 환경변수는
    덮어쓰지 않는다(셸에서 준 값이 우선).
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = _clean(val)


def get_env(name: str, default: str = "") -> str:
    value = _clean(os.environ.get(name))
    return value if value else default


def get_env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = get_env(name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        log.warning("환경변수 %s 값이 정수가 아닙니다. 기본값 %s 사용", name, default)
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


@dataclass
class Config:
    # LLM
    openai_api_key: str
    llm_model: str
    embedding_model: str
    # DB
    db_backend: str
    sqlite_path: str
    supabase_url: str
    supabase_service_role_key: str
    # 알림
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_channel_url: str      # 헤더 'Telegram' 버튼이 여는 주소 (채널 초대 링크 등). 없으면 봇 DM
    # 수집 소스 (선택)
    naver_client_id: str
    naver_client_secret: str
    # 운영 설정
    poll_interval_sec: int
    naver_interval_sec: int
    fresh_cutoff_hours: int
    backfill_cutoff_hours: int
    notify_threshold: int
    llm_daily_limit: int
    llm_per_run: int
    api_host: str
    api_port: int
    master_password: str          # 마스터 패널 초기 비밀번호 (변경 시 DB 해시가 우선)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def naver_enabled(self) -> bool:
        return bool(self.naver_client_id and self.naver_client_secret)


def load_config() -> Config:
    """필수 키를 시작 직후 한 번에 전부 검증한다. (env-secret-guard §4)"""
    load_dotenv_file(os.path.join(ROOT_DIR, ".env"))

    backend = get_env("DB_BACKEND", "sqlite").lower()
    if backend not in ("sqlite", "supabase"):
        raise SystemExit(f"DB_BACKEND 값이 잘못되었습니다: {backend!r} (sqlite 또는 supabase)")

    missing: list[str] = []
    openai_key = get_env("OPENAI_API_KEY")
    if not openai_key:
        missing.append("OPENAI_API_KEY")

    supabase_url = get_env("SUPABASE_URL")
    supabase_key = get_env("SUPABASE_SERVICE_ROLE_KEY")
    if backend == "supabase":
        if not supabase_url:
            missing.append("SUPABASE_URL")
        if not supabase_key:
            missing.append("SUPABASE_SERVICE_ROLE_KEY")

    if missing:
        raise SystemExit(
            "다음 환경변수가 비어 있습니다: "
            + ", ".join(missing)
            + "\n프로젝트 루트의 .env 파일에 값을 채운 뒤 다시 실행하세요."
        )

    sqlite_path = get_env("SQLITE_PATH", os.path.join("backend", "pfm_news.db"))
    if not os.path.isabs(sqlite_path):
        sqlite_path = os.path.join(ROOT_DIR, sqlite_path)

    return Config(
        openai_api_key=openai_key,
        llm_model=get_env("LLM_MODEL", "gpt-5.6-luna"),
        embedding_model=get_env("EMBEDDING_MODEL", "text-embedding-3-small"),
        db_backend=backend,
        sqlite_path=sqlite_path,
        supabase_url=supabase_url,
        supabase_service_role_key=supabase_key,
        telegram_bot_token=get_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=get_env("TELEGRAM_CHAT_ID"),
        telegram_channel_url=get_env("TELEGRAM_CHANNEL_URL"),
        naver_client_id=get_env("NAVER_CLIENT_ID"),
        naver_client_secret=get_env("NAVER_CLIENT_SECRET"),
        poll_interval_sec=get_env_int("POLL_INTERVAL_SEC", 300, 30, 600),
        # 네이버 뉴스 검색 하루 25,000회 한도 대응. 300초면 29키워드 기준 하루 약 8,400회.
        naver_interval_sec=get_env_int("NAVER_INTERVAL_SEC", 300, 60),
        fresh_cutoff_hours=get_env_int("FRESH_CUTOFF_HOURS", 6, 1),
        backfill_cutoff_hours=get_env_int("BACKFILL_CUTOFF_HOURS", 72, 1),
        notify_threshold=get_env_int("NOTIFY_THRESHOLD", 50, 0, 100),
        llm_daily_limit=get_env_int("LLM_DAILY_LIMIT", 1500, 0),
        # 1회 실행에서 분석할 최대 건수. 나머지는 analyzed_at=null 로 남아 다음 실행에 처리된다.
        # 60초 주기를 지키려면 작게 유지한다(모델 1회 호출이 5~10초).
        llm_per_run=get_env_int("LLM_PER_RUN", 6, 1),
        api_host=get_env("API_HOST", "127.0.0.1"),
        api_port=get_env_int("API_PORT", 8000, 1, 65535),
        master_password=get_env("MASTER_PASSWORD"),
    )


# =====================================================================
# 2. 공통 유틸
# =====================================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """ISO8601 UTC 문자열. SQLite 에서 문자열 정렬 = 시간 정렬이 되도록 통일한다."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


KST = timezone(timedelta(hours=9))  # 한국 표준시


def parse_feed_datetime(raw: Any, struct: Any = None) -> datetime | None:
    """RSS/뉴스 피드의 발행시각을 UTC 로 파싱한다.

    한국 언론사 RSS 상당수(전기신문·머니투데이 등)가 'YYYY-MM-DD HH:MM:SS' 처럼
    타임존 없이 '한국시간 로컬값'을 준다. 이를 UTC 로 오인하면 9시간 미래가 되어
    목록 최상단에 '방금'으로 붙는다. 그래서 타임존이 없으면 KST 로 간주한다.

    raw    : 피드 원문 문자열(entry['published'] 등). 타임존 표기가 있으면 그것을 신뢰한다.
    struct : feedparser 의 *_parsed struct_time. 원문 파싱 실패 시 최후 수단(GMT 로 간주됨).
    """
    dt: datetime | None = None
    text = str(raw or "").strip()

    if text:
        # 1) RFC822 형식 (예: 'Tue, 02 Sep 2026 08:29:51 +0900' / '... GMT')
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            dt = None
        # 2) ISO 유사 형식 (예: '2026-09-02 08:29:51', '2026-09-02T08:29:51+09:00')
        if dt is None:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(text[:len("2026-09-02 08:29:51")], fmt)
                        break
                    except ValueError:
                        continue

    if dt is None and struct is not None:
        try:
            dt = datetime(*tuple(struct)[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            dt = None

    if dt is None:
        return None

    # 타임존이 없으면 한국시간으로 간주한다.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    dt = dt.astimezone(timezone.utc)

    # 미래 시각은 파싱 오류로 본다(시계 오차 여유 2시간). 발행일 불명으로 되돌린다.
    if dt > now_utc() + timedelta(hours=2):
        return None
    return dt


def new_id() -> str:
    return str(uuid.uuid4())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def jload(value: Any, default: Any) -> Any:
    """SQLite 는 JSON 문자열, Supabase 는 이미 파싱된 값을 돌려준다. 둘 다 받는다."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


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
    def delete_body(self, article_id: str) -> None: ...

    @abstractmethod
    def cleanup_bodies(self, older_than_days: int) -> int: ...

    @abstractmethod
    def purge_stale_drafts(self, older_than_hours: int) -> int: ...

    @abstractmethod
    def purge_stale_embeddings(self, older_than_hours: int) -> int: ...

    @abstractmethod
    def save_summary(self, row: dict) -> None: ...

    @abstractmethod
    def save_swot(self, row: dict) -> None: ...

    @abstractmethod
    def llm_calls_today(self) -> int: ...

    @abstractmethod
    def list_articles(self, limit: int, offset: int, since: datetime | None, query: str) -> list[dict]: ...

    @abstractmethod
    def article_detail(self, article_id: str) -> dict | None: ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def upsert_quote(self, row: dict) -> None: ...

    @abstractmethod
    def all_quotes(self) -> list[dict]: ...

    @abstractmethod
    def get_run_state(self) -> dict: ...

    @abstractmethod
    def set_run_state(self, patch: dict) -> None: ...

    @abstractmethod
    def log_collection(self, row: dict) -> None: ...

    @abstractmethod
    def enabled_keywords(self) -> list[dict]: ...

    @abstractmethod
    def seed_keywords(self, rows: Sequence[tuple[str, str]]) -> None: ...

    @abstractmethod
    def enabled_feeds(self) -> list[dict]: ...

    @abstractmethod
    def seed_feeds(self, rows: Sequence[tuple[str, str, str, bool]]) -> None: ...

    @abstractmethod
    def press_by_domain(self, domain: str) -> dict | None: ...

    @abstractmethod
    def press_tier_by_id(self, press_id: str | None) -> int:
        """언론사 tier(1=주요지 … 3=기타). id 가 없거나 못 찾으면 3."""

    @abstractmethod
    def all_press(self) -> list[dict]:
        """press_outlets 전체 행. fixpress 의 도메인 표기 복원에 쓴다."""

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


class SqliteStorage(Storage):
    """로컬 개발용. 배열·JSON 컬럼은 JSON 문자열로 저장한다."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        # 수집 루프와 API 서버가 다른 스레드에서 접근하므로 스레드별 커넥션을 쓴다.
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("pragma journal_mode = wal")
            conn.execute("pragma foreign_keys = on")
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
        return self._rows(
            "select id, title, published_at, dedup_group_id, is_representative, title_embedding,"
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
        return {
            "total": int(total["n"]) if total else 0,
            "today": int(today_n["n"]) if today_n else 0,
            "last_collected_at": last["t"] if last else None,
            "notify_failed": int(failed["n"]) if failed else 0,
            "analysis_pending": self.unanalyzed_count(),
        }

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
            self._exec(
                "insert into run_state (key,last_success_at,notify_mode,updated_at) values (?,?,?,?)",
                ("pipeline", None, "suppressed", row["updated_at"]),
            )
        return row

    def set_run_state(self, patch: dict) -> None:
        self.get_run_state()
        data = self._encode({**patch, "updated_at": iso(now_utc())})
        sets = ",".join(f"{k}=?" for k in data)
        self._exec(f"update run_state set {sets} where key='pipeline'", list(data.values()))

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

    def press_tier_by_id(self, press_id: str | None) -> int:
        if not press_id:
            return 3
        row = self._one("select tier from press_outlets where id=?", (press_id,))
        return int(row["tier"]) if row and row["tier"] is not None else 3

    def all_press(self) -> list[dict]:
        return self._rows("select * from press_outlets")

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
            " a.importance_score, a.published_at, a.group_companies,"
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


# ── Supabase 구현 ────────────────────────────────────────────────────

class SupabaseStorage(Storage):
    """운영용. SQLite 구현과 완전히 같은 메서드 집합을 제공한다.

    주의: Supabase 키가 채워지기 전까지 실행 검증을 하지 못한 코드다.
    전환 시 반드시 `python backend/main.py once` 로 1회 검증 후 운영에 올린다.
    """

    def __init__(self, url: str, key: str) -> None:
        try:
            from supabase import create_client
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "supabase 패키지가 없습니다. `pip install supabase` 후 다시 실행하세요."
            ) from exc
        self.db = create_client(url, key)

    def _t(self, name: str):
        return self.db.table(name)

    def init_schema(self) -> None:
        raise SystemExit(
            "Supabase 스키마는 자동 생성하지 않습니다.\n"
            "Supabase 대시보드 > SQL Editor 에서 backend/schema.sql 을 실행하세요."
        )

    def seen_url_sources(self, candidates: Sequence[str]) -> set[str]:
        if not candidates:
            return set()
        found: set[str] = set()
        chunk = 400
        for i in range(0, len(candidates), chunk):
            part = list(candidates[i:i + chunk])
            found.update(
                r["url_source"] for r in self._t("articles").select("url_source").in_("url_source", part).execute().data
            )
            found.update(
                r["url_source"] for r in self._t("url_ledger").select("url_source").in_("url_source", part).execute().data
            )
            remaining = [c for c in part if c not in found]
            for alias in remaining:
                hit = self._t("articles").select("id").contains("url_source_aliases", [alias]).limit(1).execute().data
                if hit:
                    found.add(alias)
        return found

    def recent_url_sources(self, hours: int) -> set[str]:
        cutoff = iso(now_utc() - timedelta(hours=hours))
        rows = self._t("articles").select("url_source,url_source_aliases").gte("collected_at", cutoff).execute().data
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
        return (self._t("articles")
                .select("id,title,published_at,dedup_group_id,is_representative,title_embedding,press_id,press_name,content_hash")
                .gte("published_at", iso(since)).eq("status", "active").execute().data)

    def upsert_ledger(self, url_source: str, reason: str) -> None:
        existing = self._t("url_ledger").select("hit_count").eq("url_source", url_source).execute().data
        if existing:
            self._t("url_ledger").update({"hit_count": existing[0]["hit_count"] + 1}).eq("url_source", url_source).execute()
        else:
            self._t("url_ledger").insert(
                {"url_source": url_source, "reason": reason, "first_seen": iso(now_utc()), "hit_count": 1}
            ).execute()

    def bump_ledger(self, url_sources: Sequence[str]) -> None:
        for url in url_sources:
            rows = self._t("url_ledger").select("hit_count").eq("url_source", url).execute().data
            if rows:
                self._t("url_ledger").update({"hit_count": rows[0]["hit_count"] + 1}).eq("url_source", url).execute()

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
        q = (self._t("articles")
             .select(f"{cols}, summaries(summary_text,perspective_text,summary_source),"
                     " swot_analyses(total_score,s_score,w_score,o_score,t_score,s_text,w_text,o_text,t_text)")
             .eq("status", "active").eq("is_representative", True))
        if since is not None:
            q = q.gte("published_at", iso(since))
        if query:
            like = f"%{query}%"
            q = q.or_(f"title.ilike.{like},author.ilike.{like},press_name.ilike.{like}")
        rows = q.order("published_at", desc=True).range(offset, offset + limit - 1).execute().data
        return [self._flatten(r) for r in rows]

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
        return {
            "total": total,
            "today": today_n,
            "last_collected_at": last[0]["collected_at"] if last else None,
            "notify_failed": failed,
            "analysis_pending": self.unanalyzed_count(),
        }

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
        self._t("run_state").insert(row).execute()
        return row

    def set_run_state(self, patch: dict) -> None:
        self.get_run_state()
        self._t("run_state").update({**patch, "updated_at": iso(now_utc())}).eq("key", "pipeline").execute()

    def log_collection(self, row: dict) -> None:
        self._t("collection_logs").insert(row).execute()

    def enabled_keywords(self) -> list[dict]:
        return self._t("keyword_sets").select("*").eq("enabled", True).execute().data

    def seed_keywords(self, rows: Sequence[tuple[str, str]]) -> None:
        payload = [{"category": c, "keyword": k, "enabled": True} for c, k in rows]
        if payload:
            self._t("keyword_sets").upsert(payload, on_conflict="category,keyword").execute()

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

    def press_tier_by_id(self, press_id: str | None) -> int:
        if not press_id:
            return 3
        rows = self._t("press_outlets").select("tier").eq("id", press_id).execute().data
        return int(rows[0]["tier"]) if rows and rows[0].get("tier") is not None else 3

    def all_press(self) -> list[dict]:
        return self._t("press_outlets").select("*").execute().data or []

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
        names = {r["id"]: r["name"] for r in self._t("press_outlets").select("id,name").execute().data}
        rows = (self._t("articles").select("id,press_id,press_name")
                .not_.is_("press_id", "null").execute().data)
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
        rows = (self._t("notifications")
                .select("*, articles(title,url_canonical,url_original,press_name,author,"
                        "importance_score,published_at,group_companies,"
                        "summaries(summary_text,perspective_text))")
                .eq("status", "queued").lt("retry_count", 3)
                .order("created_at").limit(limit).execute().data)
        out = []
        for row in rows:
            article = row.pop("articles", None) or {}
            summary = article.pop("summaries", None)
            if isinstance(summary, list):
                summary = summary[0] if summary else None
            out.append({**row, **article, **(summary or {})})
        out.sort(key=lambda r: r.get("importance_score") or 0, reverse=True)
        return out

    def mark_notification(self, notif_id: str, status: str, error: str | None) -> None:
        rows = self._t("notifications").select("retry_count").eq("id", notif_id).execute().data
        retry = (rows[0]["retry_count"] if rows else 0) + 1
        patch: dict[str, Any] = {"retry_count": retry, "error": error}
        if status != "queued":
            patch["status"] = status
            patch["sent_at"] = iso(now_utc()) if status == "sent" else None
        self._t("notifications").update(patch).eq("id", notif_id).execute()


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
}

# 그룹사 정규 명칭 — LLM 이 만든 그룹사명은 이 목록에 없으면 버린다. (PRD F4.2)
#   키 = 표시명(필터 칩·카드에 이 이름이 나온다). 값 = 본문에서 찾을 별칭(법인격 ㈜/(유) 제외).
#   주요 5개사는 옛 사명·영문명까지, 그 밖 계열사(사용자 제공 51개소 목록, 2026-09-02)는 사명만.
#   '포스코'(상위 개념)는 계열사가 특정되면 normalize_group_list 에서 제거된다.
GROUP_COMPANIES: dict[str, list[str]] = {
    "포스코홀딩스": ["포스코홀딩스", "POSCO홀딩스", "posco holdings"],
    "포스코퓨처엠": ["포스코퓨처엠", "POSCO퓨처엠", "posco future m", "포스코케미칼"],
    "포스코DX": ["포스코DX", "포스코 DX", "posco dx", "포스코ICT"],
    "포스코인터내셔널": ["포스코인터내셔널", "posco international", "포스코대우"],
    "포스코이앤씨": ["포스코이앤씨", "posco e&c", "포스코건설"],
    # ── 그 밖 계열사 ──────────────────────────────────────────────────
    "포스코스틸리온": ["포스코스틸리온"],
    "포스코엠텍": ["포스코엠텍"],
    "포스코휴먼스": ["포스코휴먼스"],
    "피엔알": ["피엔알(PNR)", "피엔알"],
    "엔투비": ["엔투비", "N2B"],
    "포항특수용접봉": ["포항특수용접봉"],
    "포스코피알테크": ["포스코피알테크"],
    "포스코피에스테크": ["포스코피에스테크"],
    "포스코피에이치솔루션": ["포스코피에이치솔루션"],
    "포스코지와이알테크": ["포스코지와이알테크"],
    "포스코지와이에스테크": ["포스코지와이에스테크"],
    "포스코지와이솔루션": ["포스코지와이솔루션"],
    "포항에스알디씨": ["포항에스알디씨"],
    "이스틸포유": ["이스틸포유"],
    "켐가스코리아": ["켐가스코리아"],
    "포스코에스피": ["포스코에스피"],
    "포스코모빌리티솔루션": ["포스코모빌리티솔루션"],
    "탄천이앤이": ["탄천이앤이"],
    "삼척블루파워": ["삼척블루파워"],
    "한국퓨얼셀": ["한국퓨얼셀"],
    "신안그린에너지": ["신안그린에너지"],
    "에코에너지솔루션": ["에코에너지솔루션"],
    "우이신설경전철": ["우이신설경전철"],
    "게일인터내셔널코리아": ["게일인터내셔널코리아"],
    "송도개발피엠씨": ["송도개발피엠씨"],
    "포스코에이앤씨": ["포스코에이앤씨건축사사무소", "포스코에이앤씨", "포스코A&C"],
    "알앤알물류": ["알앤알물류"],
    "포스코엠씨머티리얼즈": ["포스코엠씨머티리얼즈"],
    "퓨처그라프": ["퓨처그라프"],
    "포스코지에스에코머티리얼즈": ["포스코지에스에코머티리얼즈", "포스코GS에코머티리얼즈"],
    "포스코에이치와이클린메탈": ["포스코에이치와이클린메탈", "포스코HY클린메탈"],
    "포스코플로우": ["포스코플로우"],
    "플로우케이": ["플로우케이"],
    "포스코와이드": ["포스코와이드"],
    "포스코경영연구원": ["포스코경영연구원", "포스리", "POSRI"],
    "포스코기술투자": ["포스코기술투자"],
    "에스엔엔씨": ["에스엔엔씨(SNNC)", "에스엔엔씨", "SNNC"],
    "부산이앤이": ["부산이앤이"],
    "포스코인재창조원": ["포스코인재창조원"],
    "포스코아이에이치": ["포스코아이에이치"],
    "포스코필바라리튬솔루션": ["포스코필바라리튬솔루션"],
    "포스코리튬솔루션": ["포스코리튬솔루션"],
    "큐에스원": ["큐에스원"],
    "포스코에어솔루션": ["포스코에어솔루션"],
    "포스코세이프티솔루션": ["포스코세이프티솔루션"],
    # 포스코퓨처엠 합작사·관계사 (2026-09-02)
    "얼티엄캠": ["ultiumcam", "얼티엄캠", "얼티엄 캠", "얼티엄캡"],
    "절강포화": ["절강포화", "저장포화", "浙江浦华", "zhejiang puhua"],
    "절강화포": ["절강화포", "저장화포", "浙江华浦", "zhejiang huapu"],
    "씨앤피신소재테크놀로지": ["씨앤피신소재테크놀로지", "c&p신소재테크놀로지",
                              "씨앤피신소재", "cnp신소재"],
    # 상위 개념 — 계열사가 특정되면 제거됨
    "포스코": ["포스코", "POSCO"],
}

# 그룹사 판정에 쓰는 본문 길이. 기사의 '주체'는 제목과 리드 문단에 드러난다.
#
# 본문 전체를 스캔하면 말미의 스치는 언급이 주체를 가로챈다. 실제 사례:
#   '포스코 직접고용 이행하겠다지만…'(부산일보) 본문 1,963자 중 1,859번째에
#   '포스코홀딩스 장인화 회장' 이 한 번 나오는데, detect_group_companies 의
#   `if not found` 때문에 '포스코홀딩스' 만 붙고 정작 주체인 '포스코' 는 빠졌다.
# 리드 700자로 줄이면 위 기사는 '포스코', 계열사를 실제로 나열하는 공채 기사는
# 4개 계열사가 그대로 잡힌다(계열사명이 0~270자에 등장). detect_categories 가
# 제목+요약만 보는 것과 같은 이유다.
GROUP_LEAD_CHARS = 700
REGROUP_DAYS = 3   # `regroup` 명령이 되돌아볼 기간. 그보다 오래된 카드는 화면에서 밀려난다.

TRADE_CATEGORY = "글로벌 통상환경"
# '특정 국가의 명시적 통상 조치' 만 넣는다. '공급망 재편'·'디리스킹' 같은 일반 트렌드어는
# 노사·실적 기사에도 스치듯 나와 오태깅되므로 제외한다.
TRADE_MEASURE_KW = [
    # 미국
    "IRA", "인플레이션감축법", "OBBBA", "FEOC", "해외우려기관", "무역확장법 232조", "232조 관세",
    "무역법 301조", "301조 관세", "세이프가드", "상호관세", "보편관세",
    # 유럽
    "CBAM", "탄소국경조정", "탄소국경세", "핵심원자재법", "CRMA", "역외보조금 규정",
    "EU 배터리규정", "공급망실사지침", "CSDDD",
    # 중국
    "흑연 수출통제", "갈륨 수출", "게르마늄 수출", "안티모니 수출", "희토류 수출통제",
    "요소 수출제한", "반도체 장비 수출통제", "수출허가 대상",
    # 무역구제 (공식 절차)
    "반덤핑 관세", "반덤핑관세", "반덤핑 조사", "상계관세", "덤핑 판정", "긴급수입제한",
]
# 통상 기사가 '포스코 관련 산업'인지 판정.
TRADE_INDUSTRY_KW = [
    "배터리", "이차전지", "2차전지", "양극재", "음극재", "전구체", "리튬", "니켈", "코발트",
    "흑연", "전기차", "ESS", "핵심광물",
    "철강", "제철", "후판", "열연", "냉연", "선재", "강판", "스테인리스", "합금철", "봉형강",
    "포스코",
]

# 카테고리 태깅 규칙 (PRD F3.1)
#   제목+요약에서만 판정한다. 본문 전체를 스캔하면 부차적 언급까지 걸려
#   거의 모든 기사가 3~4개 카테고리를 달아 필터가 무의미해진다.
#   그래서 '정부'·'정책'·'셀'·'실적' 같은 흔한 단어는 넣지 않고 구체적 용어만 쓴다.
#   '지역'은 폐지했다 — 포스코 키워드가 들어간 기사만 수집하므로 지역 태깅이 무의미하다.
CATEGORY_RULES: dict[str, list[str]] = {
    # 양극재·음극재는 포스코퓨처엠 주력이라 별도 카테고리로 뺀다.
    # detect_categories 는 정의 순서대로 도니 배터리·이차전지 앞에 둬 더 구체적으로 잡는다.
    "양극재": ["양극재", "양극활물질", "하이니켈", "니켈코발트망간", "NCM", "NCA", "NCMA",
               "단결정 양극재", "코발트프리", "전구체", "리튬 전구체", "LFP", "LMFP", "LFP 양극재",
               # 양극재 경쟁사 — 회사명이 곧 사업 분야
               "에코프로비엠", "엘앤에프", "코스모신소재", "당성과기", "룽바이", "화유코발트"],
    "음극재": ["음극재", "음극활물질", "인조흑연", "천연흑연", "구상흑연", "실리콘 음극",
               "실리콘음극재", "SiOx", "리튬메탈 음극",
               # 음극재 경쟁사
               "BTR", "베이터루이", "샨샨", "대주전자재료"],
    "배터리·이차전지": ["이차전지", "2차전지", "배터리", "리튬",
                        "니켈", "코발트", "흑연", "전고체", "ESS", "전기차", "배터리셀"],
    "산업": ["철강", "제철", "제철소", "고로", "용광로", "전기로", "조강", "쇳물", "열연", "냉연",
             "후판", "선재", "강판", "형강", "스테인리스", "합금철", "제련", "정련",
             "수소환원제철", "하이렉스", "HyREX", "조선", "완성차", "설비 증설",
             # 포스코 그룹 사업 전반 — 건설·인프라·로봇·자동화
             "재건축", "재개발", "정비사업", "시공", "수주", "분양", "플랜트", "EPC",
             "로봇", "협동로봇", "휴머노이드", "스마트팩토리", "자동화", "푸드테크"],
    # '정부/정책'과 '법령'을 분리한다. 둘 다 '정부'·'정책' 같은 흔한 단어는 넣지 않고
    # 행정·입법을 각각 가리키는 구체 용어만 쓴다.
    "정부/정책": ["국회", "산업부", "환경부", "기재부", "공정위", "보조금", "특화단지",
                  "IRA", "관세", "국정감사", "예비타당성", "예타", "국정과제", "부처 합동",
                  "정부 지원", "정책 지원", "육성 방안"],
    "법령": ["법안", "법률안", "개정안", "시행령", "시행규칙", "특별법", "입법", "규제",
             "인허가", "과징금", "행정처분", "제재", "고시", "조례"],
    # 미국·유럽·중국 등 주요국의 명시적 통상 조치. 정의는 TRADE_MEASURE_KW 와 맞춘다.
    # detect_categories 가 '제목에 조치명' 조건을 한 번 더 건다.
    "글로벌 통상환경": TRADE_MEASURE_KW,
    "시장/주가": ["주가", "증권", "코스피", "코스닥", "목표주가", "시황", "상한가", "하한가",
                  "거래량", "거래대금", "시가총액", "PER", "PBR", "공매도", "외국인 순매수",
                  "기관 순매수", "배당"],
}

# 중요도 가중치 (PRD F3.2)
SCORE_FUTUREM_TITLE = 50
SCORE_FUTUREM_BODY = 40
SCORE_GROUP = 25
SCORE_POLICY = 20
SCORE_MAJOR_PRESS = 10
SCORE_MARKET_PENALTY = -15

POLICY_KEYWORDS = ["정책", "규제", "법안", "수사", "사고", "화재", "제재", "과징금",
                   "국회", "산업부", "환경부", "보조금", "특화단지", "인허가", "감사", "고발"]
MARKET_ONLY_KEYWORDS = ["목표주가", "투자의견", "코스피", "시황", "주가 전망", "증권가"]


# =====================================================================
# 5. URL 정규화 (PRD F2.1 / news-dedup-normalize)
#    (A) 게이트 이전 — 네트워크 금지. (B) 게이트 통과분 — HTTP 허용.
# =====================================================================

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "igshid", "spm", "ref", "from", "cid", "sid", "oid", "aid"}


def normalize_url(raw: str) -> str:
    """네트워크 없이 가능한 정규화만 수행한다. **리다이렉트를 풀지 않는다.**

    여기서 리다이렉트를 풀면 이미 수집한 기사에도 매 실행 HTTP 요청이 발생해
    PRD F1.1 의 핵심 규칙("재조회 비용 0")이 깨진다.
    """
    if not raw:
        return ""
    u = urlsplit(raw.strip())
    scheme = (u.scheme or "https").lower()
    host = (u.hostname or "").lower()
    port = "" if u.port in (None, 80, 443) else f":{u.port}"
    query = [
        (k, v) for k, v in parse_qsl(u.query, keep_blank_values=False)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_KEYS
    ]
    path = u.path.rstrip("/") or "/"
    return urlunsplit((scheme, host + port, path, urlencode(sorted(query)), ""))


def domain_of(url: str) -> str:
    """서브도메인을 등록 도메인 기준으로 접는다. (news.chosun.com → chosun.com)"""
    host = (urlsplit(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # co.kr / or.kr / go.kr 같은 2단계 국가 도메인 처리
    if len(parts) >= 3 and parts[-2] in ("co", "or", "go", "ne", "re", "pe", "ac") and parts[-1] == "kr":
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


TITLE_PREFIX_RE = re.compile(r"^\s*[\[\(【][^\]\)】]{1,12}[\]\)】]\s*")
TITLE_SUFFIX_RE = re.compile(r"\s*[-–—|]\s*[^-–—|]{1,20}\s*$")


def normalize_title(title: str) -> str:
    """유사도 계산 전처리. 말머리·언론사 꼬리·기호를 제거한다. (F2.2)"""
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title)  # 전각 → 반각
    # [속보] [단독] (종합) (2보) 같은 말머리를 반복 제거
    while True:
        stripped = TITLE_PREFIX_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = TITLE_SUFFIX_RE.sub("", text)          # ' - 언론사' 꼬리 제거
    text = re.sub(r"[^\w가-힣]+", "", text)        # 공백·특수문자 제거
    return text.lower()


def title_similarity(a: str, b: str) -> float:
    """한국어는 어절 토큰만으로 부족하므로 문자 단위로 비교한다. (news-dedup-normalize)"""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def normalize_chip(text: str) -> str:
    """칩 중복 제거용 정규화 키. (PRD F4.2 / F6.2b)"""
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", text or "")).lower()


def dedupe_chips(items: Iterable[str], exclude: Iterable[str] = ()) -> list[str]:
    """정규화 키 기준으로 중복을 제거하고 입력 순서를 유지한다.

    레퍼런스 화면에서 '포스코그룹' 칩이 3회 중복 노출된 문제를 저장·표시 양쪽에서 막는다.
    """
    blocked = {normalize_chip(x) for x in exclude if normalize_chip(x)}
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = (item or "").strip()
        key = normalize_chip(text)
        if not key or key in seen or key in blocked:
            continue
        seen.add(key)
        out.append(text)
    return out


# =====================================================================
# 6. HTTP 클라이언트
#    실행당 외부 요청 수를 센다. PRD §6 의 검증 지표(재조회 비용 0)를
#    측정 가능하게 만드는 것이 목적이다.
# =====================================================================

def _import(module: str, pip_name: str):
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise SystemExit(
            f"{module} 패키지가 없습니다. `pip install {pip_name}` 후 다시 실행하세요."
        ) from exc


USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


class HttpClient:
    def __init__(self, timeout: float = 10.0) -> None:
        requests = _import("requests", "requests")
        self._requests = requests
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"})
        # 본문 페이지를 병렬로 받을 때 커넥션 풀이 부족하지 않게 늘린다.
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.timeout = timeout
        self.count = 0
        self._lock = threading.Lock()

    def get(self, url: str, **kwargs: Any):
        with self._lock:
            self.count += 1
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        with self._lock:
            self.count += 1
        kwargs.setdefault("timeout", self.timeout)
        return self.session.post(url, **kwargs)

    def reset(self) -> None:
        self.count = 0


# =====================================================================
# 7. 수집 (PRD F1.2)
# =====================================================================

@dataclass
class RawItem:
    """소스가 돌려준 원시 항목. 아직 게이트를 통과하지 않았다."""
    url_source: str
    url_original: str
    title: str
    published_at: datetime | None
    source_type: str
    press_hint: str = ""
    snippet: str = ""


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
# NAVER 검색 API는 developers.naver.com 에서 NAVER Cloud Platform 의
# 'NAVER API HUB' 로 통합되었다. 엔드포인트와 헤더가 바뀌었다.
#   구: https://openapi.naver.com/v1/search/news.json  +  X-Naver-Client-Id / -Secret
#   신: 아래 URL  +  X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY
NAVER_NEWS_API = "https://naverapihub.apigw.ntruss.com/search/v1/news"


POLICY_SITE = "site:www.korea.kr"   # '정책' 키워드는 정책브리핑으로만 검색한다


def collect_google_rss(http: HttpClient, keyword_rows: Sequence[dict]) -> list[RawItem]:
    feedparser = _import("feedparser", "feedparser")
    items: list[RawItem] = []
    for row in keyword_rows:
        keyword = row["keyword"]
        query = f"{POLICY_SITE} {keyword}" if row.get("category") == "정책" else keyword
        url = GOOGLE_NEWS_RSS.format(q=urlencode({"q": query})[2:])
        try:
            resp = http.get(url)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Google RSS 조회 실패 (%s): %s", keyword, exc)
            continue
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            link = entry.get("link", "")
            if not link:
                continue
            src_title = (entry.get("source", {}) or {}).get("title", "")
            # 정책브리핑 검색인데 생활정보 블로그(gonggam.korea.kr)면 버린다. (사용자 지정)
            if row.get("category") == "정책" and "gonggam" in f"{src_title} {entry.get('title','')}".lower():
                continue
            published = parse_feed_datetime(entry.get("published") or entry.get("updated"),
                                            entry.get("published_parsed") or entry.get("updated_parsed"))
            items.append(RawItem(
                url_source=normalize_url(link),
                url_original=link,
                title=html_mod.unescape(entry.get("title", "")).strip(),
                published_at=published,
                source_type="google_rss",
                press_hint=(entry.get("source", {}) or {}).get("title", ""),
                snippet=html_mod.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", ""))).strip(),
            ))
    return items


POSCO_MENTION_RE = re.compile(r"포스코|posco", re.IGNORECASE)


def _kw_hit(text: str, keywords: Sequence[str]) -> bool:
    """키워드 목록이 비어 있으면 True(조건 없음), 아니면 하나라도 본문에 있으면 True."""
    return (not keywords) or any(k in text for k in keywords)

# 대한민국 정책브리핑 기사 URL (생활정보 gonggam.korea.kr 은 제외).
KOREA_KR_NEWS_RE = re.compile(r"//(?:www\.)?korea\.kr/news/", re.IGNORECASE)

# 정책 기사가 '포스코 산업에 영향이 있는가' 판정용. 포스코 미언급이어도 이 중 하나면 수집한다.
POLICY_RELEVANCE_KW = [
    "철강", "제철", "이차전지", "배터리", "리튬", "니켈", "양극재",
    "전기요금", "전력", "에너지", "전력수급", "송전", "발전",
    "탄소", "배출권", "탄소중립", "탄소국경", "CBAM", "RE100", "재생에너지", "수소",
    "산업", "제조", "공급망", "핵심광물", "통상", "관세", "무역", "수출",
    "산업단지", "특화단지", "투자", "보조금", "세제", "인센티브", "국가전략기술",
    "반도체", "친환경", "기후", "환경규제", "국토", "건설", "인프라", "SOC",
    "예산", "국정감사", "규제완화", "노동", "고용",
]


POLICY_BRIEF_PRESS = "대한민국 정책브리핑"

def _kw_hit_any(text: str, keywords: Sequence[str]) -> bool:
    """_kw_hit 과 달리 빈 목록이면 False (조건이 반드시 있어야 하는 경우)."""
    return any(k in text for k in keywords)


def is_trade_topic(title: str, extra: str = "") -> bool:
    """글로벌 통상환경 기사인가.

    통상 조치는 헤드라인에 드러나므로 '제목'에 조치명이 있어야 한다.
    (요약·본문에만 스친 언급은 부차 주제 — 오태깅을 막는다.)
    포스코 관련 산업(철강·배터리)이 제목·요약에 함께 있어야 한다.
    """
    return _kw_hit_any(title, TRADE_MEASURE_KW) and _kw_hit_any(f"{title}\n{extra}", TRADE_INDUSTRY_KW)


def is_policy_brief(row: dict) -> bool:
    """정책브리핑(korea.kr) 기사인지. press_name 또는 URL 로 판정한다."""
    if (row.get("press_name") or "") == POLICY_BRIEF_PRESS:
        return True
    return bool(KOREA_KR_NEWS_RE.search(row.get("url_canonical") or row.get("url_original") or ""))


def is_trade_article(row: dict) -> bool:
    """저장된 기사가 통상환경 기사인지 — 카테고리 태그 또는 제목 기준으로 판정."""
    if TRADE_CATEGORY in jload(row.get("categories"), []):
        return True
    return is_trade_topic(row.get("title") or "", row.get("summary_text") or "")


# 포스코퓨처엠 사업(양극재·음극재)에 영향을 주는 배터리 생태계 전반.
# 소재·기술 개발뿐 아니라 셀 업체·전기차 수요·ESS 시장까지 — 전부 전방 수요다.
# 이 범위에 들면 포스코 회사명이 없어도 수집한다. (사용자 지정)
BATTERY_SCOPE_KW = [
    # 소재·차세대 기술
    "전고체", "리튬메탈", "리튬금속 배터리", "황리튬", "황-리튬", "리튬황",
    "나트륨이온", "나트륨 배터리", "소듐이온", "소듐 배터리",
    "하이니켈", "단결정 양극재", "코발트프리", "망간리치", "LMFP", "LFP",
    "실리콘 음극", "실리콘음극재", "SiOx", "리튬메탈 음극", "무음극",
    "고체 전해질", "고분자 전해질", "건식 전극", "드라이 전극", "전해액", "분리막",
    "양극재", "음극재", "전구체", "차세대 배터리", "차세대 이차전지",
    # 전지·셀
    "이차전지", "2차전지", "배터리셀", "배터리 셀", "배터리팩", "배터리 공장", "기가팩토리",
    "배터리 수주", "배터리 합작", "배터리 투자", "배터리 시장", "배터리 수요",
    # 셀·완성차 업체 (전방 수요)
    "LG에너지솔루션", "삼성SDI", "SK온", "CATL", "BYD", "파나소닉",
    "테슬라", "Tesla", "리비안", "루시드", "폭스바겐 배터리", "GM 배터리", "포드 배터리",
    # 소재 경쟁사 (포스코퓨처엠 양극재·음극재·전구체 직접 경쟁) — 회사명 정확 매칭이라 오탐 낮음
    "에코프로", "엘앤에프", "코스모신소재", "대주전자재료", "나노신소재", "한솔케미칼",
    "BTR", "베이터루이", "샨샨", "샨산", "룽바이", "롱바이", "CNGR", "중웨이",
    "화유코발트", "당성과기", "스미토모금속광산", "니치아",
    "LG화학 양극재", "LG화학 첨단소재",
    # 전기차 수요
    "전기차 판매", "전기차 수요", "전기차 보조금", "전기차 캐즘", "EV 수요", "전기차 시장",
    # ESS (에너지저장)
    "ESS", "에너지저장장치", "에너지저장시스템", "전력저장", "계통 안정화", "BESS",
    # 원료
    "리튬 가격", "니켈 가격", "코발트 가격", "탄산리튬", "수산화리튬", "흑연 공급",
]


def is_battery_scope(title: str, extra: str = "") -> bool:
    """포스코퓨처엠 전·후방(소재·셀·전기차·ESS·원료) 기사인가.

    이 범위면 포스코 미언급이어도 수집한다 — 전방 수요·경쟁 동향이 사업에 직결된다.
    """
    return _kw_hit_any(f"{title}\n{extra}", BATTERY_SCOPE_KW)


def extract_ministry(html: str, body: str) -> str:
    """정책브리핑 기사에서 발표 부처명을 뽑는다. 못 찾으면 '정책브리핑'."""
    text = f"{html}\n{body}"
    for pat in (
        r"문의\s*[:：]\s*(?:&lt;|<)?\s*총괄\s*(?:&gt;|>)?\s*([가-힣]{2,12}(?:부|처|청|위원회|실))",
        r"문의\s*[:：]\s*([가-힣]{2,12}(?:부|처|청|위원회))",
        r"자료\s*=\s*([가-힣]{2,12}(?:부|처|청|위원회))",
        r"\(([가-힣]{2,12}(?:부|처|청))\s*제공\)",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return "정책브리핑"


def _naver_item_relevant(title: str, category: str) -> bool:
    """네이버가 느슨하게 매칭한 무관 기사를 거른다.

    '그룹사' 키워드(포스코DX 등) 검색 결과에는 **제목에** '포스코'가 있는 기사만 통과시킨다.
    네이버 description 은 검색어를 그대로 되풀이하므로 본문·요약으로는 걸러지지 않는다.
    이 필터가 없으면 '영진전문대 수시모집' 같은 기사가 포스코DX 태그로 들어온다.
    """
    if category == "그룹사":
        return bool(POSCO_MENTION_RE.search(title or ""))
    return True


def collect_naver(http: HttpClient, cfg: Config, keyword_rows: Sequence[dict]) -> list[RawItem]:
    """NAVER API HUB 뉴스 검색. 직접 크롤링은 약관 위반이므로 하지 않는다. (PRD §7-4)

    무료 한도: 뉴스 검색 하루 25,000회 / 월 775,000회.
    키워드 29개를 1분마다 조회하면 하루 41,760회로 한도를 넘는다.
    → run_once 에서 `NAVER_INTERVAL_SEC`(기본 300초) 간격으로만 호출한다.

    keyword_rows: [{"keyword": ..., "category": ...}, ...]
    """
    if not cfg.naver_enabled:
        return []
    headers = {"X-NCP-APIGW-API-KEY-ID": cfg.naver_client_id,
               "X-NCP-APIGW-API-KEY": cfg.naver_client_secret}
    items: list[RawItem] = []
    fail_count = 0
    dropped = 0
    for row in keyword_rows:
        keyword = row["keyword"] if isinstance(row, dict) else row
        category = row.get("category", "") if isinstance(row, dict) else ""
        try:
            resp = http.get(
                NAVER_NEWS_API,
                # 5분 간격 폴링에는 최신 30건이면 충분하다. 100건을 받으면
                # 대부분 backfill·중복이라 분석 백로그만 부풀린다.
                params={"query": keyword, "display": 30, "sort": "date"},
                headers=headers,
            )
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"{resp.status_code} 인증 실패 — .env 의 NAVER_CLIENT_ID/SECRET 이 "
                    "NAVER API HUB 의 Client ID/Secret 인지 확인하세요."
                )
            if resp.status_code == 429:
                log.warning("Naver API 호출 한도 초과(429). 이번 실행의 네이버 수집을 중단합니다.")
                break
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            fail_count += 1
            log.warning("Naver API 조회 실패 (%s): %s", keyword, exc)
            # 첫 3개 키워드가 연속 실패하면 설정 문제다. 나머지를 시도할 필요가 없다.
            if fail_count >= 3 and not items:
                log.error("Naver API 연속 실패 — 설정을 점검하세요. 이번 실행 네이버 수집 중단.")
                break
            continue
        for entry in data.get("items", []):
            link = entry.get("originallink") or entry.get("link", "")
            if not link:
                continue
            title = html_mod.unescape(re.sub(r"<[^>]+>", "", entry.get("title", ""))).strip()
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", entry.get("description", ""))).strip()
            if not _naver_item_relevant(title, category):
                dropped += 1
                continue
            published = parse_feed_datetime(entry.get("pubDate"))
            items.append(RawItem(
                url_source=normalize_url(link),
                url_original=link,
                title=title,
                published_at=published,
                source_type="naver_api",
                snippet=snippet,
            ))
    if dropped:
        log.info("네이버 무관 기사 %d건 제외 (그룹사 키워드 결과 중 포스코 미언급)", dropped)
    return items


def collect_rss_feeds(http: HttpClient, feeds: Sequence[dict]) -> list[RawItem]:
    """언론사 자체 RSS. DB(feed_sources)로 추가하며 코드 수정이 필요 없다. (§6 확장성)"""
    feedparser = _import("feedparser", "feedparser")
    items: list[RawItem] = []
    for feed_row in feeds:
        url = feed_row.get("url") or ""
        if not url:
            continue
        try:
            resp = http.get(url)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("RSS 조회 실패 (%s): %s", feed_row.get("name"), exc)
            continue
        for entry in feedparser.parse(resp.content).entries:
            link = entry.get("link", "")
            if not link:
                continue
            published = parse_feed_datetime(entry.get("published") or entry.get("updated"),
                                            entry.get("published_parsed") or entry.get("updated_parsed"))
            items.append(RawItem(
                url_source=normalize_url(link),
                url_original=link,
                title=html_mod.unescape(entry.get("title", "")).strip(),
                published_at=published,
                source_type="rss",
                press_hint=feed_row.get("name", ""),
                snippet=html_mod.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", ""))).strip(),
            ))
    return items


# =====================================================================
# 8. 리다이렉트 해제 및 본문 추출 (PRD F2.1-B, F4.5)
#    여기부터 HTTP 요청이 발생한다. 게이트 통과분만 도달해야 한다.
# =====================================================================

GOOGLE_HOSTS = ("news.google.com", "google.com")


def decode_html(resp: Any) -> str:
    """응답 바이트를 올바른 인코딩으로 디코드한다.

    requests 는 charset 헤더가 없으면 ISO-8859-1 로 디코드한다(RFC 2616).
    한국 언론사 상당수가 charset 헤더 없이 본문 meta 로만 utf-8/euc-kr 을 알린다.
    그대로 .text 를 쓰면 기자명·제목이 'ì¡ìë¯¼' 처럼 깨진다.
    """
    raw = resp.content or b""
    # 1) HTML meta 의 charset 을 우선한다.
    m = re.search(rb'charset=["\']?\s*([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    if m:
        enc = m.group(1).decode("ascii", "ignore").lower().replace("euc-kr", "cp949")
        try:
            return raw.decode(enc, "replace")
        except LookupError:
            pass
    # 2) 응답 헤더의 charset (requests 가 채운 encoding)
    enc = (resp.encoding or "").lower()
    if enc and enc not in ("iso-8859-1", "ascii"):
        try:
            return raw.decode(enc.replace("euc-kr", "cp949"), "replace")
        except LookupError:
            pass
    # 3) UTF-8 → CP949 순서로 시도
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def resolve_canonical(http: HttpClient, url: str) -> tuple[str, str]:
    """리다이렉트를 해제해 최종 원문 URL 과 HTML 을 돌려준다.

    반환: (정규화된 canonical URL, HTML 본문). 실패 시 ("", "").
    """
    try:
        # 본문 페이지는 8초 안에 응답 없으면 포기한다. 느린 사이트 하나가
        # 1회 실행 전체를 지연시키지 않게 한다. (PRD §6 실행 시간)
        resp = http.get(url, allow_redirects=True, timeout=8)
        resp.raise_for_status()
    except Exception as exc:
        log.debug("리다이렉트 해제 실패 %s: %s", url, exc)
        return "", ""

    final_url = resp.url
    text = decode_html(resp)

    host = (urlsplit(final_url).hostname or "").lower()
    if any(host.endswith(g) for g in GOOGLE_HOSTS):
        # Google News 는 HTML 안에서 JS 로 이동시키는 경우가 있다. 링크를 직접 찾는다.
        found = _extract_google_target(text)
        if found:
            try:
                resp2 = http.get(found, allow_redirects=True)
                resp2.raise_for_status()
                final_url, text = resp2.url, decode_html(resp2)
            except Exception:
                final_url = found
                text = ""
        else:
            return "", ""

    # canonical(HTML) → 리다이렉트 최종 URL → 최초 요청 URL 순으로 신뢰한다.
    # 루트만 남는 값은 기사 링크가 아니므로 건너뛴다.
    canonical = _canonical_from_html(text)
    if not canonical:
        canonical = final_url if not _is_bare_root(final_url) else url
    return normalize_url(canonical), text


def prefetch_articles(http: HttpClient, urls: Sequence[str], workers: int = 6) -> dict[str, tuple[str, str]]:
    """여러 기사의 리다이렉트 해제 + HTML 을 병렬로 받는다.

    한국 언론사 사이트는 응답이 느려 순차 처리하면 1회 실행이 수 분 걸린다.
    게이트(G2·G2.5)를 이미 통과한 항목만 여기 오므로 재조회 비용 규칙과 무관하다.
    """
    result: dict[str, tuple[str, str]] = {}
    if not urls:
        return result
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
        futures = {pool.submit(resolve_canonical, http, u): u for u in urls}
        for fut in futures:
            url = futures[fut]
            try:
                result[url] = fut.result()
            except Exception as exc:
                log.debug("prefetch 실패 %s: %s", url, exc)
                result[url] = ("", "")
    return result


def _extract_google_target(html: str) -> str:
    """Google News 중간 페이지에서 실제 기사 URL 을 찾는다."""
    for pattern in (r'data-n-au="(https?://[^"]+)"', r'<a[^>]+href="(https?://(?!news\.google)[^"]+)"'):
        m = re.search(pattern, html)
        if m:
            return html_mod.unescape(m.group(1))
    return ""


def _is_bare_root(url: str) -> bool:
    """경로 없는 도메인 루트('http://site.com/')인지. 기사 링크로는 쓸 수 없다."""
    p = urlsplit(url)
    return p.path in ("", "/") and not p.query


def _canonical_from_html(html: str) -> str:
    """<link rel="canonical"> / og:url 이 있으면 채택한다. (news-dedup-normalize)

    일부 구형 뉴스 CMS(예: techholic)는 기사 페이지에서도 canonical 을
    홈페이지 루트로 잘못 지정한다. 루트만 있는 값은 버리고 다음 후보로 넘어간다.
    """
    if not html:
        return ""
    for pattern in (
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pattern, html, re.IGNORECASE)
        if m and m.group(1).startswith("http"):
            cand = html_mod.unescape(m.group(1))
            if not _is_bare_root(cand):
                return cand
    return ""


# 순서 = 신뢰도. JSON-LD > 본문 서명('OOO 기자') > meta[name=author].
# meta author 에 매체명을 넣는 사이트가 많아 가장 뒤에 둔다.
AUTHOR_PATTERNS = [
    re.compile(r'"author"\s*:\s*{[^}]*"name"\s*:\s*"([^"]{2,20})"'),
    re.compile(r'([가-힣]{2,4})\s*기자'),
    re.compile(r'<meta[^>]+(?:name|property)=["\'](?:author|article:author|dable:author)["\'][^>]+content=["\']([^"\']{2,30})["\']', re.I),
]

# 기자명 자리에 매체명이 잡히는 것을 막는다 (예: '중앙이코노미뉴스')
MEDIA_NAME_SUFFIX = (
    "뉴스", "일보", "신문", "미디어", "타임즈", "타임스", "저널", "방송", "닷컴",
    "통신", "데일리", "포스트", "투데이", "경제", "신보", "타임", "프레스", "위키",
)


def decode_unicode_escapes(text: str) -> str:
    r"""HTML/JSON-LD 에서 긁어온 문자열에 남아 있는 '\uXXXX' 리터럴을 실제 글자로 바꾼다.

    JSON-LD 의 "name":"한혜선" 을 정규식으로 캡처하면 역슬래시-u 표기가
    그대로 남아 '한혜선' 대신 '한혜선' 이 저장된다. (한글 음절 U+AC00~U+D7A3)
    """
    if not text or "\\u" not in text:
        return text
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)


def clean_author(name: str) -> str:
    """기자명 표기를 다듬는다. '영남본부=장원규' → '장원규', '송영민 기자' → '송영민'."""
    name = (name or "").strip()
    name = re.sub(r"^[가-힣A-Za-z]{2,10}\s*[=:]\s*", "", name)   # '영남본부=' 같은 소속 머리표
    name = re.sub(r"\s*(기자|팀|특파원|논설위원|편집위원)\s*$", "", name).strip()
    name = re.sub(r"[·,/]\s*(사진|영상|취재)\s*$", "", name).strip()
    return name


def fix_mojibake(text: str) -> str:
    """UTF-8 바이트를 latin-1 로 잘못 디코드한 문자열('ì¡ìë¯¼')을 되살린다.

    본문 HTML 을 잘못된 인코딩으로 읽었을 때 기자명에 깨진 글자가 섞인다.
    latin-1 재인코딩 후 utf-8 디코딩이 성공하고 한글이 나오면 그 값을 채택한다.
    """
    if not text or not re.search(r"[ÂÃÌÍ][\x80-\xBF]|[ìíîï][\x80-\xBF]", text):
        return text
    try:
        restored = text.encode("latin-1").decode("utf-8")
    except (UnicodeError, ValueError):
        return text
    return restored if _has_hangul(restored) else text


def _valid_author(raw: str, press_key: str) -> str:
    """기자명 후보를 정제·검증한다. 부적합하면 빈 문자열."""
    name = clean_author(fix_mojibake(decode_unicode_escapes((raw or "").strip())))
    if not (2 <= len(name) <= 20):
        return ""
    if re.match(r"(https?:|www\.)", name, re.I) or _looks_like_domain(name):
        return ""
    if not name.isascii() and not _has_hangul(name):
        return ""
    if name.endswith(MEDIA_NAME_SUFFIX):
        return ""
    key = normalize_chip(name)
    if press_key and (key == press_key or key in press_key or press_key in key):
        return ""
    if key in NON_AUTHOR_WORDS:
        return ""
    return name


def extract_author(html: str, body: str, press_name: str = "") -> str:
    """기자명 추출. '[언론사, 기자]' 머리표 조립에 쓴다. (PRD F4.1)

    확실하지 않으면 빈 문자열을 돌려주고 '[언론사]' 만 표기하는 편이 낫다.
    """
    press_key = normalize_chip(press_name)

    # 1) JSON-LD author (보통 정확, 단일 매치)
    for source in (html, body):
        if source and (m := AUTHOR_PATTERNS[0].search(source)):
            if name := _valid_author(m.group(1), press_key):
                return name

    # 2) 본문 서명 'OOO 기자' — 서명은 기사에 여러 번 나오므로 최빈값을 택한다.
    #    카테고리 라벨('칼럼기자', '시민기자')은 1회만 나와 자연히 밀린다.
    for source in (html, body):
        if not source:
            continue
        names = [n for n in (_valid_author(g, press_key)
                             for g in AUTHOR_PATTERNS[1].findall(source)) if n]
        if names:
            return Counter(names).most_common(1)[0][0]

    # 3) meta[name=author] (매체명을 넣는 사이트가 많아 최후 순위, 단일 매치)
    for source in (html, body):
        if source and (m := AUTHOR_PATTERNS[2].search(source)):
            if name := _valid_author(m.group(1), press_key):
                return name

    return ""


# 기자명 자리에 자주 잘못 들어오는 값들 (직함·부서·라벨)
NON_AUTHOR_WORDS = {
    "뉴시스", "연합뉴스", "뉴스1", "편집국", "온라인뉴스팀", "산업부", "경제부",
    "취재팀", "디지털뉴스팀", "특별취재팀", "무단전재", "재배포금지",
    "칼럼", "시민", "객원", "명예", "인턴", "수습", "선임", "본지", "특약",
    "한국", "일요", "주말", "사진", "영상", "그래픽", "독자", "논설", "사설",
}


def extract_thumbnail(html: str) -> str:
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html or "", re.I)
    return html_mod.unescape(m.group(1)) if m else ""


def extract_title(html: str) -> str:
    """수동 URL 등록용 — 피드 제목이 없을 때 HTML 에서 제목을 뽑는다."""
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<title[^>]*>([^<]+)</title>',
    ):
        m = re.search(pattern, html or "", re.I)
        if m:
            title = html_mod.unescape(m.group(1)).strip()
            # ' - 언론사' 같은 사이트명 꼬리 정리
            title = re.sub(r'\s*[|·\-–—]\s*[^|·\-–—]{1,25}$', '', title).strip()
            if len(title) >= 5:
                return title
    return ""


PUBLISHED_META_PATTERNS = [
    r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+property=["\']og:published_time["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+name=["\'](?:pubdate|publishdate|date|sailthru\.date)["\'][^>]+content=["\']([^"\']+)["\']',
    r'"datePublished"\s*:\s*"([^"]+)"',
]


def extract_published(html: str) -> datetime | None:
    for pattern in PUBLISHED_META_PATTERNS:
        m = re.search(pattern, html or "", re.I)
        if m:
            dt = parse_dt(m.group(1))
            if dt:
                return dt
    return None


def extract_body(html: str) -> str:
    """Readability 를 1순위로 본문을 뽑는다. 실패해도 예외를 던지지 않는다. (F4.5)"""
    if not html:
        return ""
    try:
        readability = _import("readability", "readability-lxml")
        bs4 = _import("bs4", "beautifulsoup4")
        doc = readability.Document(html)
        soup = bs4.BeautifulSoup(doc.summary(), "html.parser")
        text = soup.get_text("\n", strip=True)
        if len(text) >= 200:
            return text
    except SystemExit:
        raise
    except Exception as exc:
        log.debug("Readability 실패: %s", exc)
    # 폴백: 전체 문서에서 텍스트만 긁는다.
    try:
        bs4 = _import("bs4", "beautifulsoup4")
        soup = bs4.BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)
    except SystemExit:
        raise
    except Exception:
        return ""


# =====================================================================
# 9. 분류 및 중요도 스코어 (PRD F3)
# =====================================================================

def detect_group_companies(text: str) -> list[str]:
    """룰 기반 그룹사 판정. LLM 보다 우선한다. (PRD F4.2)

    '포스코퓨처엠'이 잡히면 상위 개념인 '포스코'는 붙이지 않는다. 칩 중복의 원인이 된다.
    """
    lowered = (text or "").lower()
    found: list[str] = []
    for canonical, aliases in GROUP_COMPANIES.items():
        if canonical == "포스코":
            continue  # 마지막에 따로 판단
        if any(alias.lower() in lowered for alias in aliases):
            found.append(canonical)
    if not found and any(alias.lower() in lowered for alias in GROUP_COMPANIES["포스코"]):
        found.append("포스코")
    return found


def normalize_group_list(groups: Iterable[str]) -> list[str]:
    """그룹사 목록 정리. 구체 계열사가 있으면 상위 개념 '포스코'는 뺀다.

    룰 기반 결과와 LLM 결과를 합칠 때 '포스코홀딩스 · 포스코퓨처엠 · 포스코' 처럼
    상위·하위가 같이 붙는다. 칩이 늘어나기만 하고 정보량은 늘지 않는다.
    """
    cleaned = dedupe_chips(g for g in groups if g in GROUP_COMPANIES)
    if len(cleaned) > 1 and "포스코" in cleaned:
        cleaned = [g for g in cleaned if g != "포스코"]
    return cleaned


def detect_categories(title: str, summary: str = "") -> list[str]:
    """카테고리 태깅. (PRD F3.1)

    제목+요약에서만 판정한다. 본문 전체를 스캔하면 부차적 언급까지 걸려
    거의 모든 기사에 카테고리가 붙어 필터가 변별력을 잃는다.
    """
    lowered = f"{title or ''}\n{summary or ''}".lower()
    found = [name for name, words in CATEGORY_RULES.items()
             if any(w.lower() in lowered for w in words)]
    # 글로벌 통상환경은 조치명이 '제목'에 있을 때만 — 요약에 스친 언급은 부차 주제라 뺀다.
    if TRADE_CATEGORY in found and not _kw_hit_any((title or "").lower(),
                                                  [w.lower() for w in TRADE_MEASURE_KW]):
        found.remove(TRADE_CATEGORY)
    # '그룹사'는 별도의 그룹사 필터가 담당하므로 카테고리에는 넣지 않는다.
    return dedupe_chips(found)


def score_article(title: str, body: str, group_companies: Sequence[str], press_tier: int) -> int:
    """중요도 0~100. (PRD F3.2)"""
    title_l = (title or "").lower()
    full_l = f"{title} {body}".lower()
    score = 0

    futurem = [a.lower() for a in GROUP_COMPANIES["포스코퓨처엠"]]
    if any(a in title_l for a in futurem):
        score += SCORE_FUTUREM_TITLE
    elif any(a in full_l for a in futurem):
        score += SCORE_FUTUREM_BODY

    if any(g != "포스코퓨처엠" for g in group_companies):
        score += SCORE_GROUP

    if any(w.lower() in full_l for w in POLICY_KEYWORDS):
        score += SCORE_POLICY
    if press_tier <= 1:
        score += SCORE_MAJOR_PRESS

    # 단순 시황·주가 기사는 알림 피로를 유발하므로 감점한다.
    if any(w.lower() in title_l for w in MARKET_ONLY_KEYWORDS):
        score += SCORE_MARKET_PENALTY

    return int(clamp(score, 0, 100))


# =====================================================================
# 10. LLM 분석 (PRD F4)
#     요약 · 포스코 관점 · 키워드 · 그룹사 · 감성 · SWOT 을 호출 1회로 받는다.
#     항목별로 나눠 호출하면 과금이 4배가 된다.
# =====================================================================

ANALYSIS_SYSTEM = "당신은 한국어 뉴스 분석 어시스턴트다. 주어진 기사 내용만을 근거로 분석하고 JSON 으로만 답한다."

ANALYSIS_PROMPT = """아래 기사를 분석해 JSON 하나로만 답하라.

[요약 규칙]
- summary: 정확히 3~5문장, 한국어, 각 문장 40자 내외
- 순서: (1) 무슨 일이 (2) 누가·어디서 (3) 수치·규모
- 기사에 없는 사실·배경·전망을 추가하지 않는다
- 의견·평가·추측 표현을 쓰지 않는다
- 원문 문장을 그대로 복사하지 않고 재서술한다
- 언론사명과 기자명은 summary 에 넣지 않는다 (표시할 때 따로 붙인다)
- 기사가 요약하기에 불충분하면 summary 를 ["요약불가"] 로만 채운다

[포스코 관점]
- perspective: 포스코퓨처엠 사업 관점의 시사점 1~2문장
- "검토할 필요가 있습니다" 수준의 확인 요청 톤으로 쓰고 단정하지 않는다
- 관련성이 없으면 빈 문자열

[키워드]
- keywords: 기사 핵심 키워드 최대 6개, 한국어 명사구
- 서로 중복되거나 포함관계인 키워드를 넣지 않는다
- 회사명은 keywords 가 아니라 group_companies 에 넣는다

[관련 그룹사]
- group_companies: 기사 본문에 그 회사 이름이 실제로 등장하는 경우에만 넣는다
  포스코홀딩스, 포스코퓨처엠, 포스코DX, 포스코인터내셔널, 포스코이앤씨, 포스코
- '이차전지·배터리·공급망 뉴스니까 포스코퓨처엠' 같은 추측은 금지한다
- 회사명이 본문에 없으면 관련 산업 기사라도 빈 배열로 둔다
- 목록에 없는 회사명을 만들어내지 않는다

[감성]
- sentiment: "긍정" | "중립" | "부정" 중 하나
- 주가 호재/악재가 아니라 포스코 그룹의 대외협력 대응 필요성 기준으로 판단한다

[SWOT]
- swot: 이 기사의 사안이 포스코 그룹(철강·이차전지소재·인프라 전반)에 주는
  영향을 S/W/O/T 로 평가한다. 각 항목 score(0~100 정수)와 text(1~2줄 근거)
- 기사 본문에서 실제로 읽어낼 수 있는 함의를 적고, 최소한 한 항목은 채운다.
  신사업·수요 확대·우호적 협력 = O / 경쟁 심화·규제·공급과잉·자원 리스크 = T /
  자사 기술력·생산능력·계약·점유율 = S / 비용 부담·생산 차질·구조적 약점 = W
- 정말로 근거를 찾을 수 없는 항목만 score 0, text "해당 없음"

[출력 형식 — 이 구조를 정확히 지킨다]
{{"summary":["문장1","문장2","문장3"],"perspective":"...","keywords":["..."],
"group_companies":["..."],"sentiment":"중립",
"swot":{{"s":{{"score":0,"text":"..."}},"w":{{"score":0,"text":"..."}},
"o":{{"score":0,"text":"..."}},"t":{{"score":0,"text":"..."}}}}}}

제목: {title}
언론사: {press}
본문:
{body}
"""

MAX_BODY_CHARS = 6000  # 토큰 비용 상한. 기사 본문 대부분은 이 안에 들어간다.


@dataclass
class Analysis:
    summary_sentences: list[str] = field(default_factory=list)
    perspective: str = ""
    keywords: list[str] = field(default_factory=list)
    group_companies: list[str] = field(default_factory=list)
    sentiment: str = "중립"
    swot: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)
    ok: bool = False

    @property
    def summary_text(self) -> str:
        return " ".join(s.strip() for s in self.summary_sentences if s.strip())


def _make_openai_client(api_key: str):
    """OpenAI 클라이언트. LANGSMITH_TRACING 이 켜져 있으면 LangSmith 로 감싼다.

    langsmith 미설치·추적 off 면 순수 클라이언트를 그대로 쓴다(오버헤드 0).
    """
    openai = _import("openai", "openai")
    client = openai.OpenAI(api_key=api_key)
    if _clean(os.environ.get("LANGSMITH_TRACING")).lower() in ("1", "true", "yes"):
        try:
            from langsmith.wrappers import wrap_openai
            client = wrap_openai(client)
            log.info("LangSmith 트레이싱 활성화 (project=%s)",
                     os.environ.get("LANGSMITH_PROJECT") or "default")
        except ImportError:
            log.warning("LANGSMITH_TRACING 이 켜졌지만 langsmith 패키지가 없습니다. "
                        "`pip install langsmith` 후 다시 실행하세요.")
    return client


class LLMClient:
    def __init__(self, cfg: Config) -> None:
        _hush_libraries()  # openai/httpx 가 import 시 로깅을 다시 켜는 경우 대비
        self.client = _make_openai_client(cfg.openai_api_key)
        self.model = cfg.llm_model
        self.embedding_model = cfg.embedding_model
        # 모델별로 지원하는 파라미터가 다르다. 첫 호출에서 학습해 이후 재시도를 줄인다.
        self._supports_json_mode = True
        # 중복 판정 4단계의 임베딩 호출을 1회 실행당 이 수로 제한한다.
        # 네이버 수집 시 유사 제목이 대량으로 들어와 임베딩 폭주가 발생할 수 있다.
        self.max_embed_per_run = MAX_EMBED_PER_RUN
        self._embed_calls = 0

    def reset_run(self) -> None:
        self._embed_calls = 0

    def _chat(self, system: str, user: str) -> tuple[str, dict]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self._supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            # response_format 미지원 모델이면 한 번만 빼고 재시도한다.
            if self._supports_json_mode and "response_format" in str(exc):
                log.info("모델이 JSON 모드를 지원하지 않아 일반 모드로 전환합니다.")
                self._supports_json_mode = False
                kwargs.pop("response_format", None)
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise
        usage = {}
        if getattr(resp, "usage", None):
            usage = {"prompt": resp.usage.prompt_tokens, "completion": resp.usage.completion_tokens,
                     "total": resp.usage.total_tokens}
        return (resp.choices[0].message.content or ""), usage

    def analyze(self, title: str, press: str, body: str) -> Analysis:
        prompt = ANALYSIS_PROMPT.format(title=title, press=press or "미상", body=body[:MAX_BODY_CHARS])
        last_error: Exception | None = None
        for attempt in range(3):  # 지수 백오프 3회 (PRD F4.5)
            try:
                content, usage = self._chat(ANALYSIS_SYSTEM, prompt)
                parsed = _parse_json_object(content)
                if parsed is None:
                    raise ValueError("JSON 파싱 실패")
                return _build_analysis(parsed, usage)
            except Exception as exc:
                last_error = exc
                wait = 2 ** attempt
                log.warning("LLM 분석 실패 (%d/3): %s — %ds 후 재시도", attempt + 1, exc, wait)
                if attempt < 2:
                    time.sleep(wait)
        log.error("LLM 분석 최종 실패: %s", last_error)
        return Analysis(ok=False)

    def chat_text(self, system: str, user: str) -> str:
        """일반 텍스트 응답(JSON 강제 없음). 텔레그램 챗봇 질의응답용."""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""

    def embed(self, text: str) -> list[float] | None:
        if self._embed_calls >= self.max_embed_per_run:
            return None  # 이번 실행 임베딩 예산 소진 — 3단계 문자열 유사도까지만 적용된다
        self._embed_calls += 1
        try:
            resp = self.client.embeddings.create(model=self.embedding_model, input=text[:2000])
            return list(resp.data[0].embedding)
        except Exception as exc:
            log.warning("임베딩 생성 실패: %s", exc)
            return None


def _parse_json_object(content: str) -> dict | None:
    """모델이 코드펜스나 설명을 덧붙여도 JSON 객체만 뽑아낸다."""
    text = (content or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _build_analysis(data: dict, usage: dict) -> Analysis:
    sentences = [str(s).strip() for s in (data.get("summary") or []) if str(s).strip()]
    if not sentences or sentences == ["요약불가"]:
        return Analysis(ok=False, token_usage=usage)

    sentiment = str(data.get("sentiment") or "중립").strip()
    if sentiment not in ("긍정", "중립", "부정"):
        sentiment = "중립"

    # LLM 이 만든 그룹사명은 정규 목록에 없으면 버린다. (PRD F4.2)
    raw_groups = [str(g).strip() for g in (data.get("group_companies") or [])]
    groups = [g for g in raw_groups if g in GROUP_COMPANIES]

    swot: dict[str, dict[str, Any]] = {}
    raw_swot = data.get("swot") or {}
    for key in ("s", "w", "o", "t"):
        node = raw_swot.get(key) or {}
        try:
            score = int(clamp(float(node.get("score", 0) or 0), 0, 100))
        except (TypeError, ValueError):
            score = 0
        text = str(node.get("text") or "").strip() or "해당 없음"
        swot[key] = {"score": score, "text": text}

    return Analysis(
        summary_sentences=sentences[:5],
        perspective=str(data.get("perspective") or "").strip(),
        keywords=[str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()],
        group_companies=groups,
        sentiment=sentiment,
        swot=swot,
        token_usage=usage,
        ok=True,
    )


def swot_total(swot: dict[str, dict[str, Any]]) -> int:
    """(S+O)-(W+T) 를 0~100 으로 정규화한다. (PRD F4.4)

    원값 범위는 -200~+200 이므로 (raw + 200) / 4 로 옮긴다.
    """
    if not swot:
        return 0
    s = swot.get("s", {}).get("score", 0)
    w = swot.get("w", {}).get("score", 0)
    o = swot.get("o", {}).get("score", 0)
    t = swot.get("t", {}).get("score", 0)
    raw = (s + o) - (w + t)
    return int(round(clamp((raw + 200) / 4.0, 0, 100)))


def format_summary_header(press: str, author: str) -> str:
    """'[언론사, 기자명]' 머리표를 표시 시점에 조립한다. 저장하지 않는다. (PRD F4.1)"""
    press = (press or "").strip()
    author = (author or "").strip()
    if press and author:
        return f"[{press}, {author}]"
    if press:
        return f"[{press}]"
    return ""


# =====================================================================
# 11. 중복 판정 4단계 (PRD F2.2)
#     통신사 전재로 같은 기사가 10~20건 들어오는 것이 최대 노이즈 요인이다.
#     단계마다 비용이 오르므로 순서를 지킨다.
# =====================================================================

TITLE_SIM_THRESHOLD = 0.9      # 3단계
EMBED_SIM_THRESHOLD = 0.92     # 4단계
EMBED_PREFILTER_MIN = 0.62     # 이 아래는 4단계로 보내지 않는다(비용·지연 절감)
DEDUP_WINDOW_HOURS = 24
MAX_EMBED_PER_RUN = 30         # 1회 실행당 임베딩 호출 상한 (유사 제목 대량 유입 방어)


def find_duplicate(
    storage: Storage,
    llm: LLMClient | None,
    title: str,
    published_at: datetime,
    content_hash: str,
    url_canonical: str,
    candidates: Sequence[dict],
) -> dict | None:
    """중복이면 기존 대표 기사 행을, 아니면 None 을 돌려준다."""
    # 1단계 — 정규화 URL 완전 일치
    if url_canonical:
        hit = storage.find_by_canonical(url_canonical)
        if hit:
            return hit

    # 2단계 — 본문 해시 일치
    if content_hash:
        hit = storage.find_by_content_hash(content_hash)
        if hit:
            return hit

    window = timedelta(hours=DEDUP_WINDOW_HOURS)
    leftovers: list[tuple[dict, float]] = []

    # 3단계 — 제목 유사도 AND 발행 시각 차이. 두 조건의 AND 다.
    # 유사도만 보면 연재·기획 기사가 잘못 묶인다.
    for cand in candidates:
        cand_dt = parse_dt(cand.get("published_at"))
        if cand_dt is None or abs(cand_dt - published_at) > window:
            continue
        sim = title_similarity(title, cand.get("title", ""))
        if sim >= TITLE_SIM_THRESHOLD:
            return cand
        if sim >= EMBED_PREFILTER_MIN:
            leftovers.append((cand, sim))

    # 4단계 — 3단계에서 확정되지 않은 "잔여 후보"만 임베딩으로 본다.
    # 전건에 임베딩을 돌리면 비용이 요약 단계를 넘어선다. (PRD F2.2)
    if not leftovers or llm is None:
        return None
    new_vec = llm.embed(title)
    if not new_vec:
        return None
    for cand, _ in sorted(leftovers, key=lambda x: -x[1])[:10]:
        cand_vec = jload(cand.get("title_embedding"), None)
        if not cand_vec:
            cand_vec = llm.embed(cand.get("title", ""))
            if cand_vec:
                storage.update_article(cand["id"], {"title_embedding": cand_vec})
        if cosine(new_vec, cand_vec) >= EMBED_SIM_THRESHOLD:
            return cand
    return None


# =====================================================================
# 12. 파이프라인 (PRD F1.1 게이트)
# =====================================================================

# 1회 실행에서 G3(HTTP) 이후로 보낼 최대 건수.
# PRD §6 의 "1회 실행 20초 이내" 를 지키기 위한 상한이다. 넘친 항목은 버리는 것이
# 아니라 다음 실행에서 다시 후보가 된다(아직 articles 에 없으므로 G2 를 통과한다).
MAX_PROCESS_PER_RUN = 12


def matches_keywords(text: str, keywords: Sequence[str]) -> bool:
    """언론사 RSS 는 키워드로 질의할 수 없으므로 수집 후 로컬에서 거른다. (F1.3)"""
    lowered = (text or "").lower()
    return any(k.lower() in lowered for k in keywords if k)


def interleave_by_group(fresh: list[tuple[RawItem, bool]], cap: int) -> list[tuple[RawItem, bool]]:
    """그룹사별 큐를 라운드로빈으로 돌며 cap 건을 고른다.

    입력은 이미 중요도 내림차순. 그룹사가 감지 안 되는 항목은 마지막 순번으로 둔다.
    포스코퓨처엠 기사가 아무리 많아도 다른 그룹사 기사가 매 회차 최소 1건은 뽑힌다.
    """
    buckets: dict[str, list[tuple[RawItem, bool]]] = {}
    for pair in fresh:
        item = pair[0]
        groups = detect_group_companies(f"{item.title} {item.snippet}")
        key = groups[0] if groups else "_기타"
        buckets.setdefault(key, []).append(pair)

    # 그룹사 버킷을 먼저, '_기타'를 마지막에
    order = [k for k in buckets if k != "_기타"] + (["_기타"] if "_기타" in buckets else [])
    picked: list[tuple[RawItem, bool]] = []
    while len(picked) < cap and any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                picked.append(buckets[k].pop(0))
                if len(picked) >= cap:
                    break
    return picked


@dataclass
class Context:
    cfg: Config
    storage: Storage
    http: HttpClient
    seen_cache: set[str] = field(default_factory=set)
    last_naver_fetch: float = 0.0
    _llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        """LLM 클라이언트는 실제로 필요한 시점에 만든다.

        initdb·serve 처럼 LLM 을 쓰지 않는 명령이 openai 패키지를 요구하면 안 된다.
        """
        if self._llm is None:
            self._llm = LLMClient(self.cfg)
        return self._llm


def _looks_like_domain(text: str) -> bool:
    return bool(re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", (text or "").strip().lower()))


def _has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))


def prettify_domain(domain: str) -> str:
    """SEED_PRESS 에도 없고 힌트도 없을 때 쓰는 최소 정리.

    예전에는 'bbsi.co.kr' → 'bbsi' 처럼 도막을 냈지만, 뜻 없는 영문 조각이
    화면에 그대로 노출된다. 매핑이 없으면 도메인 전체를 유지한다.
    """
    return (domain or "").strip()


def site_name_from_html(html: str) -> str:
    """HTML 의 og:site_name / <meta name=publisher> 에서 매체명을 뽑는다.

    SEED_PRESS 에 없는 매체도 대부분 이 태그에 한글 매체명을 넣는다.
    영문 사이트명·도메인 형태는 신뢰하지 않는다(한글이 있어야 채택).
    """
    for pat in (r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\'](?:twitter:site|publisher|source)["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(pat, html or "", re.I)
        if m:
            name = html_mod.unescape(m.group(1)).strip()
            if name and _has_hangul(name) and not _looks_like_domain(name):
                return name
    return ""


def resolve_press(storage: Storage, url: str, hint: str, html: str = "") -> tuple[str, str | None, int]:
    """도메인으로 언론사를 식별한다. 미등록이면 pending 으로 적재하고 수집을 막지 않는다. (F2.3)

    우선순위: SEED_PRESS > 본문 og:site_name > 피드 힌트 > 도메인 표기.
    """
    domain = domain_of(url)
    if not domain:
        return (hint if hint and not _looks_like_domain(hint) else ""), None, 3

    row = storage.press_by_domain(domain)
    seed = SEED_PRESS.get(domain)
    og_name = site_name_from_html(html)

    if row is None:
        if seed:
            name, tier, status = seed[0], seed[1], "approved"
        elif og_name:
            name, tier, status = og_name, 3, "approved"
        elif hint and not _looks_like_domain(hint):
            name, tier, status = hint, 3, "pending"
        else:
            name, tier, status = prettify_domain(domain), 3, "pending"
        row = storage.upsert_press(domain, name, tier, status)
    elif seed and row.get("name") != seed[0] and _looks_like_domain(row.get("name", "")):
        # 예전에 도메인 그대로 저장됐던 행을 SEED 정식 이름으로 교체한다.
        storage.update_press_name(domain, seed[0], seed[1])
        row = storage.press_by_domain(domain) or row
    elif not seed and og_name and _looks_like_domain(row.get("name", "")):
        # SEED 에 없고 도메인으로만 저장돼 있던 행을 og:site_name 으로 교체한다.
        storage.update_press_name(domain, og_name, int(row.get("tier") or 3))
        row = storage.press_by_domain(domain) or row

    name = row.get("name") or ""
    if _looks_like_domain(name):
        name = seed[0] if seed else (og_name or prettify_domain(domain))
    return name, row.get("id"), int(row.get("tier") or 3)


def run_once(ctx: Context, max_llm: int | None = None, force_naver: bool = False) -> dict:
    """수집 → 게이트 → 분석 → 저장 → 알림 큐 적재를 1회 수행한다.

    max_llm 을 주면 이번 실행의 LLM 호출을 그 수만큼으로 제한한다(검증용).
    force_naver 가 True면 네이버 호출 간격 제한을 무시한다(수동 1회 실행용).
    """
    started = time.monotonic()
    cfg, storage, http = ctx.cfg, ctx.storage, ctx.http
    http.reset()

    state = storage.get_run_state()
    last_success = parse_dt(state.get("last_success_at"))
    bootstrap_at = parse_dt(state.get("bootstrap_at"))
    if bootstrap_at is None:
        # 최초 실행 — 이 시각 이전에 발행된 기사는 "역사"이므로 절대 알림하지 않는다.
        bootstrap_at = now_utc()
        storage.set_run_state({"bootstrap_at": iso(bootstrap_at)})

    # 최초 실행 또는 30분 이상 중단 후 재개는 억제 모드로 돈다. (PRD F1.1)
    suppressed = (
        state.get("notify_mode") != "active"
        or last_success is None
        or (now_utc() - last_success) > timedelta(minutes=30)
    )
    if suppressed:
        log.info("억제 모드로 실행합니다 (알림 발송 없음).")

    keyword_rows = storage.enabled_keywords()
    keywords = [k["keyword"] for k in keyword_rows]
    if not keywords:
        log.warning("활성 키워드가 없습니다. `python backend/main.py initdb` 를 먼저 실행하세요.")
        return {"fetched": 0, "new": 0}

    feeds = storage.enabled_feeds()
    feed_types = {f["source_type"] for f in feeds}

    # ── 수집 ─────────────────────────────────────────────────────────
    raw: list[RawItem] = []
    if "google_rss" in feed_types:
        raw += collect_google_rss(http, keyword_rows)
    if "naver_api" in feed_types and cfg.naver_enabled:
        # 하루 25,000회 한도 때문에 매 실행이 아니라 일정 간격으로만 호출한다.
        due = force_naver or (time.monotonic() - ctx.last_naver_fetch) >= cfg.naver_interval_sec
        if due:
            raw += collect_naver(http, cfg, keyword_rows)
            ctx.last_naver_fetch = time.monotonic()
    rss_feeds = [f for f in feeds if f["source_type"] == "rss"]
    if rss_feeds:
        # 언론사 RSS 는 전체 기사를 주므로 키워드로 먼저 거른다.
        # 이 필터가 없으면 무관한 기사까지 G3(HTTP)·G6(과금)까지 올라간다.
        raw += [
            item for item in collect_rss_feeds(http, rss_feeds)
            if matches_keywords(f"{item.title} {item.snippet}", keywords)
        ]
    fetched_count = len(raw)

    # ── G0: 실행 내 중복 제거 ────────────────────────────────────────
    unique: dict[str, RawItem] = {}
    for item in raw:
        if item.url_source and item.url_source not in unique:
            unique[item.url_source] = item
    items = list(unique.values())

    # ── G1: seen-set 캐시 (메모리 조회) ──────────────────────────────
    if not ctx.seen_cache:
        ctx.seen_cache = storage.recent_url_sources(72)
    after_g1 = [i for i in items if i.url_source not in ctx.seen_cache]
    skipped_g1 = len(items) - len(after_g1)

    # ── G2: 전체 기간 DB 대조 (판정 권위) ────────────────────────────
    seen = storage.seen_url_sources([i.url_source for i in after_g1])
    after_g2 = [i for i in after_g1 if i.url_source not in seen]
    skipped_g2 = len(after_g1) - len(after_g2)
    ctx.seen_cache.update(seen)
    if seen:
        storage.bump_ledger(list(seen))  # 원장에 있는 항목만 카운트가 오른다

    # ── G2.5: 신선도 컷오프 (네트워크 불필요, 비용 0) ────────────────
    fresh: list[tuple[RawItem, bool]] = []   # (항목, is_backfill)
    now = now_utc()
    for item in after_g2:
        if item.published_at is None:
            storage.upsert_ledger(item.url_source, "no_pubdate")
            continue
        age = now - item.published_at
        if age > timedelta(hours=cfg.backfill_cutoff_hours):
            storage.upsert_ledger(item.url_source, "stale")
            continue
        fresh.append((item, age > timedelta(hours=cfg.fresh_cutoff_hours)))

    log.info(
        "수집 %d건 → G0 %d → G1 통과 %d(-%d) → G2 통과 %d(-%d) → G2.5 통과 %d",
        fetched_count, len(items), len(after_g1), skipped_g1,
        len(after_g2), skipped_g2, len(fresh),
    )

    # 여기까지 외부 HTTP 요청은 소스 조회분뿐이어야 한다. (§6 검증 기준)
    source_requests = http.count

    dedup_candidates = storage.recent_articles_for_dedup(now - timedelta(hours=DEDUP_WINDOW_HOURS + 1))
    ctx.llm.reset_run()  # 이번 실행의 임베딩 호출 카운터 초기화

    daily_left = max(0, cfg.llm_daily_limit - storage.llm_calls_today())
    # max_llm 이 주어지면(수동 실행) 그 값이 1회 상한을 대신한다. 아니면 설정값.
    per_run_cap = max_llm if max_llm is not None else cfg.llm_per_run
    llm_budget = min(daily_left, per_run_cap)
    if max_llm is not None:
        log.info("이번 실행의 LLM 분석을 최대 %d건으로 제한합니다.", llm_budget)
    if daily_left <= 0:
        log.warning("일일 LLM 호출 상한(%d)에 도달했습니다. 저장만 하고 분석은 다음 날 재개합니다.",
                    cfg.llm_daily_limit)

    # 수집 속도가 분석 속도를 앞지르면 백로그가 무한히 는다. 예산에 맞춰 수집을 조인다.
    #   예산 없음  → 수집만 계속(본문 저장), 분석은 다음 날
    #   백로그 있음 → 예산의 절반만 신규에 쓰고 나머지는 백로그 해소
    #   평상시     → 예산만큼만 수집
    pending_now = storage.unanalyzed_count()
    if llm_budget <= 0:
        process_cap = MAX_PROCESS_PER_RUN
    elif pending_now > llm_budget:
        process_cap = max(1, llm_budget // 2)
    else:
        process_cap = min(MAX_PROCESS_PER_RUN, llm_budget)

    fresh_available = len(fresh)   # 절단 전 신규 후보 수 — 안정화 판단에 쓴다

    # 중요도 순 + 그룹사 균형. 중요도만 쓰면 포스코퓨처엠(제목 +50)이 큐를 독점해
    # 포스코DX·이앤씨 기사가 매 회차 뒤로 밀린다. 그룹사별로 번갈아 뽑는다. (PRD F4.6)
    fresh.sort(key=lambda pair: score_article(pair[0].title, pair[0].snippet, [], 3), reverse=True)
    if fresh_available > process_cap:
        picked = interleave_by_group(fresh, process_cap)
        log.info("이번 실행 처리 대상을 %d건으로 제한합니다 (신규 대기 %d · 분석 대기 %d).",
                 len(picked), fresh_available - len(picked), pending_now)
        fresh = picked

    new_count = 0
    dup_count = 0
    # 알림 판정에 필요한 항목만 담는다. {id, score, is_backfill, published_at, priority,
    #  policy: (해당여부, 키워드통과), trade: (해당여부, 키워드통과)}
    saved_for_notify: list[dict] = []
    # 마스터가 지정한 '항상 발송' 키워드 — 본문에 있으면 점수 무관 알림
    always_kws = [k for k in jload(state.get("always_notify_keywords"), []) if k]
    # 특수 주제 알림 키워드 (OR: 하나라도) / 필수 공통 키워드 (AND: 반드시). 둘 다 비면 전부.
    policy_notify_kws = [k for k in jload(state.get("policy_notify_keywords"), []) if k]
    policy_required_kws = [k for k in jload(state.get("policy_required_keywords"), []) if k]
    trade_notify_kws = [k for k in jload(state.get("trade_notify_keywords"), []) if k]
    trade_required_kws = [k for k in jload(state.get("trade_required_keywords"), []) if k]

    # ── G3: 리다이렉트 해제 + HTML 확보 (병렬) ───────────────────────
    prefetched = prefetch_articles(http, [item.url_original for item, _ in fresh])

    for item, is_backfill in fresh:
        canonical, html = prefetched.get(item.url_original, ("", ""))
        if not canonical:
            storage.upsert_ledger(item.url_source, "extract_failed")
            continue

        # ── G4: 본문 추출 (G3 응답을 재사용하므로 추가 요청 없음) ────
        body = extract_body(html)
        summary_source = "fulltext" if len(body) >= 300 else "snippet"
        if summary_source == "snippet":
            body = item.snippet or item.title

        press_name, press_id, press_tier = resolve_press(storage, canonical, item.press_hint, html)
        is_policy = bool(KOREA_KR_NEWS_RE.search(canonical or ""))
        # 정책브리핑 기사는 기자명 대신 발표 부처명을 넣는다. (사용자 지정)
        author = extract_ministry(html, body) if is_policy else extract_author(html, body, press_name)
        content_hash = sha256(body) if summary_source == "fulltext" else ""

        # ── G5: 중복 그룹 판정 ───────────────────────────────────────
        existing = find_duplicate(
            storage, ctx.llm, item.title, item.published_at, content_hash, canonical, dedup_candidates
        )
        if existing:
            # 같은 기사를 가리키는 다른 소스 URL 을 누적한다.
            # 다음 실행부터 G1 에서 탈락하므로 HTTP 요청이 발생하지 않는다.
            storage.append_alias(existing["id"], item.url_source)
            ctx.seen_cache.add(item.url_source)
            dup_count += 1
            # 더 상위 언론사에서 온 중복이면 카드의 표시 정보(제목·링크·언론사·
            # 기자·썸네일)를 그쪽으로 승격한다. 요약·SWOT·키워드 등 분석 결과는
            # 같은 사건이라 그대로 두고, 재정렬을 피하려 발행시각도 유지한다.
            if press_tier < storage.press_tier_by_id(existing.get("press_id")):
                promo = {
                    "title": item.title,
                    "url_canonical": canonical,
                    "url_original": item.url_original,
                    "press_id": press_id,
                    "press_name": press_name,
                    "author": author,
                }
                new_thumb = extract_thumbnail(html)
                if new_thumb:
                    promo["thumbnail_url"] = new_thumb
                storage.update_article(existing["id"], promo)
                log.info("대표 승격: %s → %s (%s)", existing.get("press_name") or "?",
                         press_name, item.title[:40])
            continue

        # 그룹사는 제목+리드까지만 본다 — 본문 말미의 스치는 계열사 언급이
        # 기사 주체를 가로채는 것을 막는다. (GROUP_LEAD_CHARS 주석 참고)
        rule_groups = detect_group_companies(f"{item.title}\n{body[:GROUP_LEAD_CHARS]}")
        relevance_probe = f"{item.title}\n{item.snippet or ''}\n{body}"
        # 글로벌 통상환경 기사: 제목에 통상 조치명 + 포스코 관련 산업어가 함께 있으면
        # 포스코 미언급이어도 수집한다. (사용자 지정)
        is_trade = is_trade_topic(item.title, item.snippet or "")
        if is_policy:
            # 정책브리핑 기사: 포스코 미언급이어도 포스코 산업에 닿는 주제면 수집.
            if not matches_keywords(relevance_probe, POLICY_RELEVANCE_KW):
                storage.upsert_ledger(item.url_source, "off_topic")
                ctx.seen_cache.add(item.url_source)
                continue
        elif is_trade:
            pass  # 통상 신호 + 산업 키워드가 확인됨 — 포스코 미언급 허용
        elif is_battery_scope(item.title, f"{item.snippet or ''}\n{body[:1500]}"):
            pass  # 배터리 생태계(소재·셀·전기차·ESS·원료) — 포스코 미언급 허용 (사용자 지정)
        elif not rule_groups and not POSCO_MENTION_RE.search(relevance_probe):
            # 일반 기사: 포스코·계열사가 어디에도 없으면 무관 기사 — 저장하지 않는다.
            storage.upsert_ledger(item.url_source, "off_topic")
            ctx.seen_cache.add(item.url_source)
            continue
        # 카테고리는 제목+스니펫으로만 잡고, 분석 후 요약으로 다시 계산한다.
        categories = detect_categories(item.title, item.snippet or "")
        if is_policy and "정부/정책" not in categories:
            categories = ["정부/정책"] + categories
        if is_trade and "글로벌 통상환경" not in categories:
            categories = ["글로벌 통상환경"] + categories
        score = score_article(item.title, body, rule_groups, press_tier)

        article_id = new_id()
        row = {
            "id": article_id,
            "url_source": item.url_source,
            "url_source_aliases": [],
            "url_canonical": canonical,
            "url_original": item.url_original,
            "title": item.title,
            "press_id": press_id,
            "press_name": press_name,
            "author": author,
            "published_at": iso(item.published_at),
            "collected_at": iso(now_utc()),
            "source_type": item.source_type,
            "thumbnail_url": extract_thumbnail(html),
            "content_hash": content_hash,
            "dedup_group_id": article_id,
            "is_representative": True,
            "is_backfill": is_backfill,
            "importance_score": score,
            "sentiment": None,
            "keywords": [],
            "group_companies": rule_groups,
            "categories": categories,
            "title_embedding": None,
            "analyzed_at": None,
            "status": "active",
        }
        if not storage.insert_article(row):
            # UNIQUE 위반 = 경합 상황. 최종 방어선이 작동한 것이므로 조용히 넘어간다.
            ctx.seen_cache.add(item.url_source)
            continue

        new_count += 1
        ctx.seen_cache.add(item.url_source)
        dedup_candidates.append({
            "id": article_id, "title": item.title, "published_at": iso(item.published_at),
            "dedup_group_id": article_id, "is_representative": True,
            "title_embedding": None, "press_name": press_name, "content_hash": content_hash,
        })
        # 본문은 분석 전까지만 임시 보관한다 (§7-3). 분석 완료 시 삭제된다.
        storage.save_body(article_id, body, summary_source)

        # ── G6: LLM 분석 (과금 지점) ─────────────────────────────────
        # 이번 실행 예산 안에서만 즉시 분석하고, 나머지는 아래 드레인 단계나
        # 다음 실행에서 처리한다. 60초 주기를 지키기 위한 조치다.
        if llm_budget > 0:
            llm_budget -= 1
            analyzed = analyze_and_save(ctx, article_id, dict(row), body, summary_source)
            if analyzed is not None:
                score = analyzed
        probe = f"{item.title}\n{body}"
        # 우선 알림: 마스터의 '항상 발송 키워드'가 본문에 있으면 중요도 게이트를 우회한다.
        # (포스코퓨처엠 특례는 폐지 — 원하면 키워드로 추가한다. 사용자 지정)
        is_priority = _kw_hit(probe, always_kws)
        # 특수 주제 발송 조건: (OR 키워드 하나 이상) AND (필수 공통 키워드 하나 이상)
        saved_for_notify.append({
            "id": article_id, "score": score, "is_backfill": is_backfill,
            "published_at": item.published_at, "priority": is_priority,
            "policy": (is_policy,
                       _kw_hit(probe, policy_notify_kws) and _kw_hit(probe, policy_required_kws)),
            "trade": (is_trade,
                      _kw_hit(probe, trade_notify_kws) and _kw_hit(probe, trade_required_kws)),
        })

    # ── 분석 백로그 드레인 ───────────────────────────────────────────
    # 이전 실행에서 저장만 되고 분석이 밀린 기사를 예산 안에서 처리한다.
    analyzed_backlog = 0
    if llm_budget > 0:
        for pending in storage.unanalyzed_with_body(llm_budget):
            row = {
                "id": pending["id"], "title": pending["title"],
                "press_id": pending.get("press_id"), "press_name": pending.get("press_name"),
                "importance_score": pending.get("importance_score") or 0,
                "group_companies": jload(pending.get("group_companies"), []),
            }
            if analyze_and_save(ctx, pending["id"], row, pending["body"], pending["summary_source"]) is not None:
                analyzed_backlog += 1
            llm_budget -= 1
    if analyzed_backlog:
        log.info("분석 백로그 %d건 처리", analyzed_backlog)

    # 30일 넘은 임시 본문 정리 (§7-3 보존 기간)
    purged = storage.cleanup_bodies(30)
    if purged:
        log.info("임시 본문 %d건 정리(30일 초과)", purged)
    # 24시간 넘게 등록·취소 안 한 URL 미리보기(draft) 정리
    dropped = storage.purge_stale_drafts(24)
    if dropped:
        log.info("미확정 미리보기 %d건 정리", dropped)
    # 48시간 지난 제목 임베딩 정리 — 중복 판정 창(25시간) 밖이면 다시 안 읽는다.
    # 행당 ~31KB 로 DB 용량의 대부분을 차지하므로 매일 되찾는다.
    cleared = storage.purge_stale_embeddings(48)
    if cleared:
        log.info("오래된 제목 임베딩 %d건 정리", cleared)

    # ── 알림 큐 적재 (PRD F3.3 / F7.1) ───────────────────────────────
    queued = 0
    notify_threshold = effective_threshold(ctx)
    # 특수 주제 기사(정책브리핑·글로벌 통상환경)는 기본적으로 웹에만. 마스터가 켜야 알림. (사용자 지정)
    _on = lambda v: str(v or "0") not in ("0", "False", "false", "")
    notify_policy, notify_trade = _on(state.get("notify_policy")), _on(state.get("notify_trade"))

    def _topic_ok(pair: tuple[bool, bool], enabled: bool) -> bool:
        is_topic, kw_ok = pair
        return (not is_topic) or (enabled and kw_ok)

    for it in saved_for_notify:
        if not cfg.telegram_enabled:
            break
        should_send = (
            (not suppressed)                       # 부트스트랩·복구 억제
            and (not it["is_backfill"])            # 6시간 넘은 기사는 웹에만
            and _topic_ok(it["policy"], notify_policy)
            and _topic_ok(it["trade"], notify_trade)
            and (it["score"] >= notify_threshold or it["priority"])  # 중요도 게이트(우선 기사는 우회)
            and it["published_at"] is not None
            and it["published_at"] >= bootstrap_at  # 파이프라인 가동 이전 기사는 절대 알림 안 함
        )
        status = "queued" if should_send else "skipped"
        if storage.queue_notification(it["id"], cfg.telegram_chat_id, status,
                                      1 if it["priority"] else 0):
            queued += 1 if should_send else 0

    duration_ms = int((time.monotonic() - started) * 1000)
    storage.log_collection({
        "run_at": iso(now_utc()),
        "source_type": "all",
        "fetched_count": fetched_count,
        "new_count": new_count,
        "dup_count": dup_count,
        "skipped_seen_count": skipped_g1 + skipped_g2,
        "http_request_count": http.count,
        "error": None,
        "duration_ms": duration_ms,
    })

    backlog = storage.unanalyzed_count()
    # 부트스트랩/복구 억제는 "한 회"가 아니라 파이프라인이 안정될 때까지 유지한다. (PRD F1.1)
    # 신규 유입과 분석 백로그가 모두 잦아들면 그때 알림을 켠다. 그러지 않으면
    # 초기 수집 몇 시간 동안 밀린 기사가 한꺼번에 알림으로 쏟아진다.
    # 아직 수집 못 한 신규 후보가 많으면(예: 네이버 배치가 막 들어옴) 억제를 유지한다.
    stabilized = fresh_available < 20 and new_count < 10 and backlog < 10
    next_mode = "active" if (not suppressed or stabilized) else "suppressed"
    if suppressed and next_mode == "active":
        log.info("파이프라인이 안정되어 알림을 활성화합니다.")
    storage.set_run_state({"last_success_at": iso(now_utc()), "notify_mode": next_mode})

    log.info(
        "완료: 신규 %d · 중복 %d · 분석대기 %d · 알림큐 %d · HTTP %d회(소스조회 %d) · %.1f초",
        new_count, dup_count, backlog, queued, http.count, source_requests, duration_ms / 1000,
    )
    return {
        "fetched": fetched_count, "new": new_count, "dup": dup_count, "queued": queued,
        "analysis_backlog": backlog,
        "http": http.count, "source_http": source_requests, "duration_ms": duration_ms,
        "suppressed": suppressed,
    }


def analyze_and_save(ctx: Context, article_id: str, row: dict, body: str, summary_source: str) -> int | None:
    """LLM 분석 결과를 저장하고, 갱신된 중요도 점수를 돌려준다.

    analyze() 가 내부적으로 3회 재시도하므로, 여기서 실패하면 재처리해도 같은 결과다.
    임시 본문은 성공·실패와 무관하게 삭제한다(§7-3 최소 보관). 실패 기사는
    본문이 없으므로 백로그 큐에서 빠지고, 링크·제목만 남는다. (PRD F4.5 취지)
    """
    analysis = ctx.llm.analyze(row["title"], row.get("press_name") or "", body)
    if not analysis.ok:
        log.warning("분석 실패 — 링크·제목만 저장합니다: %s", row["title"][:40])
        ctx.storage.delete_body(article_id)
        return None

    # 룰 기반 그룹사가 1순위. (PRD F4.2)
    # LLM 은 '이차전지·공급망 뉴스니까 포스코퓨처엠' 식으로 본문에 없는 계열사를
    # 태깅하는 경향이 강하다. LLM 이 보탠 그룹사는 제목·본문에 회사명(별칭)이
    # 실제로 등장하는 것만 채택한다.
    rule_groups = normalize_group_list(list(row.get("group_companies") or []))
    # LLM 이 보탠 계열사의 근거는 제목 + 리드 + 요약 + 키워드에서만 찾는다.
    # 본문 전체를 근거로 삼으면 말미의 스치는 언급까지 통과해 기사 주체가 뒤바뀐다.
    kw_text = " ".join(analysis.keywords)
    mentioned = set(detect_group_companies(
        f"{row['title']}\n{body[:GROUP_LEAD_CHARS]}\n{analysis.summary_text}\n{kw_text}"))
    llm_verified = [g for g in analysis.group_companies if g in mentioned]
    # LLM 이 group_companies 에 안 넣었어도 키워드에 계열사명이 있으면 채택한다.
    kw_groups = [g for g in detect_group_companies(kw_text) if g != "포스코"]
    groups = normalize_group_list(rule_groups + llm_verified + kw_groups)
    # 그룹사로 표기된 값은 키워드에서 제외한다 — 칩 중복의 근본 원인이다.
    keywords = dedupe_chips(analysis.keywords, exclude=groups)[:6]
    score = score_article(row["title"], body, groups, 3 if not row.get("press_id") else 1)
    score = max(int(row.get("importance_score") or 0), score)

    # 카테고리는 제목 + 요약 + LLM 키워드로 다시 계산한다(수집 때는 스니펫만 봤다).
    # 키워드는 LLM 이 뽑은 핵심어 6개뿐이라, 본문 전체를 스캔할 때 같은 과태깅 없이
    # 요약에서 빠진 주제('인허가', '이차전지 소재' 등)를 잡아 준다.
    categories = detect_categories(row["title"], f"{analysis.summary_text}\n{' '.join(keywords)}")
    if is_policy_brief(row) and "정부/정책" not in categories:
        categories = ["정부/정책"] + categories
    # 통상환경은 detect_categories 가 '제목에 조치명' 조건으로 이미 판정한다 — 강제 추가 안 함.

    ctx.storage.update_article(article_id, {
        "sentiment": analysis.sentiment,
        "keywords": keywords,
        "group_companies": groups,
        "categories": categories,
        "importance_score": score,
        "analyzed_at": iso(now_utc()),
    })
    ctx.storage.save_summary({
        "id": new_id(),
        "article_id": article_id,
        "summary_text": analysis.summary_text,
        "perspective_text": analysis.perspective,
        "summary_source": summary_source,
        "model": ctx.cfg.llm_model,
        "token_usage": analysis.token_usage,
        "created_at": iso(now_utc()),
    })
    # 근거가 부족한 snippet 기반 기사에는 SWOT 을 만들지 않는다. (PRD F4.5)
    if summary_source == "fulltext" and analysis.swot:
        ctx.storage.save_swot({
            "article_id": article_id,
            "s_score": analysis.swot["s"]["score"], "s_text": analysis.swot["s"]["text"],
            "w_score": analysis.swot["w"]["score"], "w_text": analysis.swot["w"]["text"],
            "o_score": analysis.swot["o"]["score"], "o_text": analysis.swot["o"]["text"],
            "t_score": analysis.swot["t"]["score"], "t_text": analysis.swot["t"]["text"],
            "total_score": swot_total(analysis.swot),
            "model": ctx.cfg.llm_model,
            "created_at": iso(now_utc()),
        })
    ctx.storage.delete_body(article_id)  # 분석 끝 — 임시 본문 삭제
    return score


def analyze_url(ctx: Context, raw_url: str, activate: bool = True) -> dict:
    """사용자가 직접 붙여넣은 URL 하나를 포토카드로 만든다. (PRD F8 수동 등록)

    수집 게이트(G1/G2/G2.5 신선도)는 건너뛴다 — 오래된 기사여도 등록할 수 있어야 한다.
    분석 규격(요약·관점·키워드·SWOT)은 파이프라인과 동일하다.
    activate=False 면 status='draft' 로 저장한다 — 사용자가 '등록' 을 눌러야 목록에 뜬다.
    반환: {"ok": bool, "card": {...}, "draft_id": "..."} 또는 {"ok": False, "error": "..."}
    """
    storage, http = ctx.storage, ctx.http
    raw_url = (raw_url or "").strip()
    if not re.match(r"^https?://", raw_url, re.I):
        return {"ok": False, "error": "http/https 로 시작하는 URL 을 입력하세요."}

    url_source = normalize_url(raw_url)

    def _existing_result(row_id: str) -> dict:
        """이미 있는 기사 — active 면 already, draft 면 다시 미리보기로 돌려준다."""
        detail = storage.article_detail(row_id)
        r = {"ok": True, "card": build_card(detail)}
        if (detail or {}).get("status") == "draft":
            r["draft_id"] = row_id
        else:
            r["already"] = True
        return r

    # 이미 등록·미리보기된 기사면 재분석·중복 저장 없이 그대로 돌려준다.
    existing = _find_article_by_any_url(storage, url_source, raw_url)
    if existing:
        return _existing_result(existing["id"])

    # ── G3: 리다이렉트 해제 + HTML ──────────────────────────────────
    canonical, html = resolve_canonical(http, raw_url)
    if not canonical or not html:
        return {"ok": False, "error": "페이지를 가져오지 못했습니다. 링크를 확인해 주세요."}

    existing = _find_article_by_any_url(storage, canonical, "")
    if existing:
        return _existing_result(existing["id"])

    # ── G4: 제목·본문·메타 ─────────────────────────────────────────
    title = extract_title(html)
    body = extract_body(html)
    summary_source = "fulltext" if len(body) >= 300 else "snippet"
    if not title and not body:
        return {"ok": False, "error": "기사 제목·본문을 찾지 못했습니다. 뉴스 기사 URL 이 맞는지 확인해 주세요."}
    if not title:
        title = body[:40].strip() or "제목 없음"

    press_name, press_id, press_tier = resolve_press(storage, canonical, "", html)
    author = extract_author(html, body, press_name)
    published = extract_published(html) or now_utc()
    content_hash = sha256(body) if summary_source == "fulltext" else ""

    # ── G5: 중복 판정 ──────────────────────────────────────────────
    candidates = storage.recent_articles_for_dedup(published - timedelta(hours=DEDUP_WINDOW_HOURS + 1))
    dup = find_duplicate(storage, ctx.llm, title, published, content_hash, canonical, candidates)
    if dup:
        storage.append_alias(dup["id"], url_source)
        return _existing_result(dup["id"])

    groups = detect_group_companies(f"{title}\n{body[:GROUP_LEAD_CHARS]}")
    # 카테고리는 제목 기준 임시값. 아래 analyze_and_save 에서 요약으로 다시 계산된다.
    categories = detect_categories(title)
    score = score_article(title, body, groups, press_tier)

    article_id = new_id()
    row = {
        "id": article_id, "url_source": url_source, "url_source_aliases": [],
        "url_canonical": canonical, "url_original": raw_url, "title": title,
        "press_id": press_id, "press_name": press_name, "author": author,
        "published_at": iso(published), "collected_at": iso(now_utc()),
        "source_type": "manual", "thumbnail_url": extract_thumbnail(html),
        "content_hash": content_hash, "dedup_group_id": article_id,
        "is_representative": True,
        "is_backfill": (now_utc() - published) > timedelta(hours=ctx.cfg.fresh_cutoff_hours),
        "importance_score": score, "sentiment": None, "keywords": [],
        "group_companies": groups, "categories": categories,
        "title_embedding": None, "analyzed_at": None,
        "status": "active" if activate else "draft",
    }
    if not storage.insert_article(row):
        existing = _find_article_by_any_url(storage, url_source, raw_url)
        if existing:
            return {"ok": True, "card": build_card(storage.article_detail(existing["id"])), "already": True}
        return {"ok": False, "error": "저장에 실패했습니다. 잠시 후 다시 시도해 주세요."}

    storage.save_body(article_id, body if summary_source == "fulltext" else (body or title), summary_source)

    # ── G6: LLM 분석 (수동 등록은 일일 상한과 무관하게 바로 분석) ───
    try:
        analyze_and_save(ctx, article_id, dict(row), body or title, summary_source)
    except Exception as exc:
        log.warning("수동 등록 분석 실패: %s", exc)

    detail = storage.article_detail(article_id)
    out = {"ok": True, "card": build_card(detail), "already": False}
    if not activate:
        out["draft_id"] = article_id      # 프런트가 '등록'/'취소' 를 호출할 때 쓴다
    return out


def _find_article_by_any_url(storage: Storage, url_a: str, url_b: str) -> dict | None:
    """url_source / url_canonical / alias 어느 쪽으로든 이미 있는 기사를 찾는다."""
    for u in (url_a, url_b):
        if not u:
            continue
        hit = storage.find_by_canonical(u)
        if hit:
            return hit
        seen = storage.seen_url_sources([u])
        if u in seen:
            # url_source 로는 있는데 canonical 조회로 안 나온 경우 — 목록에서 다시 찾는다
            for row in storage.list_articles(200, 0, None, ""):
                if row.get("url_source") == u or u in jload(row.get("url_source_aliases"), []):
                    return row
    return None


# =====================================================================
# 13. 시세 티커 (PRD F9)
#     무료 공개 소스를 쓰기로 확정했으므로(PLAN 0-3) 실패는 상시 발생한다.
#     따라서 "마지막 성공 값 유지"가 선택이 아니라 필수 동작이다.
# =====================================================================

STOCK_SYMBOLS = [("005490", "포스코홀딩스"), ("003670", "포스코퓨처엠")]
FX_SYMBOLS = [
    ("FX_USDKRW", "USDKRW", "(미국) 원/$"),
    ("FX_CNYKRW", "CNYKRW", "(중국) 원/元"),
    ("FX_JPYKRW", "JPYKRW", "(일본) 원/100¥"),
    ("FX_EURKRW", "EURKRW", "(유럽) 원/€"),
]
QUOTE_STALE_MINUTES = 15   # 이보다 낡으면 '지연' 배지를 붙인다


def _deep_find(node: Any, key: str) -> Any:
    """중첩된 JSON 에서 키를 찾는다. 외부 API 응답 구조가 바뀌어도 잘 견디게 한다."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _deep_find(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _deep_find(value, key)
            if found is not None:
                return found
    return None


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def fetch_quotes(http: HttpClient) -> list[dict]:
    """조회에 성공한 항목만 돌려준다. 실패 항목은 아예 넣지 않는다(마지막 값 유지)."""
    out: list[dict] = []
    headers = {"Referer": "https://m.stock.naver.com/", "Accept": "application/json"}

    for code, label in STOCK_SYMBOLS:
        try:
            resp = http.get(f"https://m.stock.naver.com/api/stock/{code}/basic", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            price = _to_number(_deep_find(data, "closePrice"))
            rate = _to_number(_deep_find(data, "fluctuationsRatio"))
            if price is None:
                raise ValueError("closePrice 없음")
            out.append({"symbol": code, "kind": "stock", "label": label,
                        "price": price, "change_rate": rate, "fetched_at": iso(now_utc())})
        except Exception as exc:
            log.warning("주가 조회 실패 (%s): %s — 마지막 값을 유지합니다", label, exc)

    for reuters_code, symbol, label in FX_SYMBOLS:
        quote = _fetch_fx(http, headers, reuters_code, symbol, label)
        if quote:
            out.append(quote)
        else:
            log.warning("환율 조회 실패 (%s) — 마지막 값을 유지합니다", label)

    return out


def _fetch_fx(http: HttpClient, headers: dict, reuters_code: str, symbol: str, label: str) -> dict | None:
    """환율 1건. 무료 소스는 언제든 형태가 바뀌므로 소스를 2개 두고 순서대로 시도한다."""
    endpoints = [
        ("https://m.stock.naver.com/front-api/marketIndex/prices",
         {"category": "exchange", "reutersCode": reuters_code, "page": 1}),
        (f"https://api.stock.naver.com/marketindex/exchange/{reuters_code}", None),
    ]
    for url, params in endpoints:
        try:
            resp = http.get(url, params=params, headers=headers) if params else http.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            price = _to_number(_deep_find(data, "closePrice"))
            rate = _to_number(_deep_find(data, "fluctuationsRatio"))
            if price is None:
                continue
            return {"symbol": symbol, "kind": "fx", "label": label,
                    "price": price, "change_rate": rate, "fetched_at": iso(now_utc())}
        except Exception as exc:
            log.debug("환율 소스 실패 (%s / %s): %s", label, url, exc)
    return None


def refresh_quotes(ctx: Context) -> int:
    rows = fetch_quotes(ctx.http)
    for row in rows:
        ctx.storage.upsert_quote(row)
    return len(rows)


# =====================================================================
# 14. 텔레그램 발송 (PRD F7)
# =====================================================================

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
NIGHT_START, NIGHT_END = 23, 7          # 야간 모드 23:00–07:00
NIGHT_MIN_SCORE = 80
DIGEST_MIN_COUNT = 3                     # 이 이상이면 다이제스트로 묶는다
RATE_LIMIT_SLEEP = 1.1                   # 동일 채팅방 분당 20건 제한 → 초당 1건 이하


def esc(text: str) -> str:
    """parse_mode=HTML 이므로 <, >, & 를 반드시 이스케이프한다. (PRD F7.2)"""
    return html_mod.escape(text or "", quote=False)


def format_message(row: dict) -> str:
    score = int(row.get("importance_score") or 0)
    emoji = "🔴" if score >= 80 else "🟠"
    groups = normalize_group_list(jload(row.get("group_companies"), []))
    if not groups:
        probe = f"{row.get('title') or ''}\n{row.get('summary_text') or ''}"
        groups = normalize_group_list(detect_group_companies(probe))
    tag = groups[0] if groups else "포스코"
    header = format_summary_header(row.get("press_name") or "", row.get("author") or "")

    lines = [f"{emoji} [{esc(tag)}] {esc(row.get('title') or '')}", ""]
    summary = (row.get("summary_text") or "").strip()
    if summary:
        lines.append(f"{esc(header)} {esc(summary)}".strip())
    perspective = (row.get("perspective_text") or "").strip()
    if perspective:
        lines += ["", f"포스코 관점: {esc(perspective)}"]
    link = row.get("url_canonical") or row.get("url_original") or ""
    if link:
        lines += ["", f'🔗 <a href="{esc(link)}">원문 보기</a>']
    return "\n".join(lines)


def effective_threshold(ctx: Context) -> int:
    """/threshold 로 지정한 값이 있으면 그것을, 없으면 .env 값을 쓴다."""
    override = ctx.storage.get_run_state().get("notify_threshold")
    if override is not None:
        try:
            return int(override)
        except (TypeError, ValueError):
            pass
    return ctx.cfg.notify_threshold


def split_for_digest(pending: list[dict]) -> tuple[list[dict], list[dict], bool]:
    """발송 대기 목록을 (우선 기사, 일반 기사, 일반 기사를 묶을지)로 나눈다.

    우선 기사('항상 발송 키워드' 매칭)는 절대 묶지 않는다. 다이제스트는 제목 한 줄만
    남기므로 요약·그룹사·점수가 사라지고, 키워드를 지정한 의미 자체가 없어진다.
    묶음은 일반 기사가 DIGEST_MIN_COUNT 건 이상일 때만 적용한다. (사용자 지정)
    """
    priority = [p for p in pending if p.get("priority")]
    normal = [p for p in pending if not p.get("priority")]
    return priority, normal, len(normal) >= DIGEST_MIN_COUNT


def send_notifications(ctx: Context, limit: int = 20) -> int:
    cfg = ctx.cfg
    if not cfg.telegram_enabled:
        return 0
    state = ctx.storage.get_run_state()
    if str(state.get("notify_paused") or "0") not in ("0", "False", "false", ""):
        return 0  # /stop 으로 일시중지됨
    pending = ctx.storage.pending_notifications(limit)
    if not pending:
        return 0
    def _is_priority(p: dict) -> bool:
        # 큐 적재 시 run_once 가 '항상 발송 키워드' 매칭으로 판정해 둔 값.
        return bool(p.get("priority"))

    # /threshold 로 조정한 임계값을 큐 단계에서 한 번 더 적용 (큐 적재는 .env 기준으로 됐을 수 있음)
    # 우선 기사(마스터 '항상 발송 키워드' 매칭)는 임계값·야간 게이트를 우회한다.
    th = effective_threshold(ctx)
    pending = [p for p in pending if int(p.get("importance_score") or 0) >= th or _is_priority(p)]
    if not pending:
        return 0

    hour = datetime.now().hour
    is_night = hour >= NIGHT_START or hour < NIGHT_END
    if is_night:
        # 야간에는 중요도 80 이상 + 포스코퓨처엠 기사만 즉시 발송, 나머지는 큐에 남긴다. (PRD F7.3)
        pending = [p for p in pending
                   if int(p.get("importance_score") or 0) >= NIGHT_MIN_SCORE or _is_priority(p)]
        if not pending:
            return 0

    url = TELEGRAM_API.format(token=cfg.telegram_bot_token)

    def _send_one(row: dict) -> bool:
        """개별 카드 발송 — 요약·그룹사·점수가 다 들어간 전체 메시지."""
        ok, err = _telegram_send(ctx, url, format_message(row))
        if ok:
            ctx.storage.mark_notification(row["id"], "sent", None)
        else:
            # 3회까지 재시도. retry_count 가 3이 되면 pending 조회에서 빠진다.
            status = "queued" if int(row.get("retry_count") or 0) < 2 else "failed"
            ctx.storage.mark_notification(row["id"], status, err)
        time.sleep(RATE_LIMIT_SLEEP)
        return ok

    priority, normal, use_digest = split_for_digest(pending)
    if len(priority) >= 10:
        log.warning("우선 기사가 한 회차에 %d건입니다. '항상 발송 키워드'가 너무 넓지 않은지"
                    " 확인하세요(예: '포스코'는 사실상 전체 기사와 매칭됩니다).", len(priority))
    sent = sum(1 for row in priority if _send_one(row))

    if use_digest:
        # 묶음 발송 — 일반 기사가 3건 이상이면 다이제스트 1건으로 보낸다.
        body = [f"📰 신규 기사 {len(normal)}건", ""]
        for row in normal:
            link = row.get("url_canonical") or row.get("url_original") or ""
            score = int(row.get("importance_score") or 0)
            mark = "🔴" if score >= 80 else "🟠"
            body.append(f'{mark} <a href="{esc(link)}">{esc(row.get("title") or "")}</a>')
        ok, err = _telegram_send(ctx, url, "\n".join(body))
        for row in normal:
            ctx.storage.mark_notification(row["id"], "sent" if ok else "queued", err)
        return sent + (len(normal) if ok else 0)

    return sent + sum(1 for row in normal if _send_one(row))


def warn_if_bad_chat_id(chat_id: str) -> None:
    """텔레그램 chat_id 형식을 미리 알려준다.

    봇 이름이나 채널 제목을 그대로 넣는 실수가 잦다. 그 경우 'chat not found' 로
    조용히 실패하며, 원인을 찾기 어렵다.
    """
    if not chat_id:
        return
    valid = chat_id.startswith("@") or chat_id.lstrip("-").isdigit()
    if not valid:
        log.warning(
            "TELEGRAM_CHAT_ID=%r 는 유효한 형식이 아닙니다. "
            "공개 채널은 '@채널아이디', 그 외(개인 DM·비공개 그룹)는 숫자 ID 를 넣어야 합니다. "
            "`python backend/main.py chatid` 로 확인할 수 있습니다.",
            chat_id,
        )


def cmd_chatid(ctx: Context) -> None:
    """봇이 받은 최근 메시지에서 chat_id 를 찾아 보여준다."""
    if not ctx.cfg.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 이 비어 있습니다.")
    url = f"https://api.telegram.org/bot{ctx.cfg.telegram_bot_token}/getUpdates"
    try:
        data = ctx.http.get(url).json()
    except Exception as exc:
        raise SystemExit(f"텔레그램 조회 실패: {exc}") from exc
    if not data.get("ok"):
        raise SystemExit(f"텔레그램 오류: {data.get('description')}")

    found: dict[str, str] = {}
    for update in data.get("result", []):
        for key in ("message", "channel_post", "edited_message", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                found[str(chat["id"])] = f'{chat.get("type", "?")} · {title}'
    if not found:
        print(
            "받은 메시지가 없습니다.\n"
            "  1) 개인 DM 이면: 텔레그램에서 봇에게 아무 메시지나 한 번 보내세요.\n"
            "  2) 그룹/채널이면: 봇을 관리자로 추가하고 메시지를 한 번 보내세요.\n"
            "그 다음 이 명령을 다시 실행하세요."
        )
        return
    print("아래 값을 .env 의 TELEGRAM_CHAT_ID 에 넣으세요.\n")
    for chat_id, desc in found.items():
        print(f"  TELEGRAM_CHAT_ID={chat_id}    ({desc})")


def cmd_sendtest(ctx: Context) -> None:
    """텔레그램 설정이 맞는지 시험 메시지 1건을 보낸다."""
    if not ctx.cfg.telegram_enabled:
        raise SystemExit("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 가 비어 있습니다.")
    url = TELEGRAM_API.format(token=ctx.cfg.telegram_bot_token)
    text = ("✅ <b>P-FM NEWS</b> 텔레그램 연결 확인\n\n"
            f"chat_id: <code>{esc(ctx.cfg.telegram_chat_id)}</code>\n"
            f"시각: {esc(iso(now_utc()))}")
    ok, err = _telegram_send(ctx, url, text)
    if ok:
        log.info("시험 메시지 발송 성공. 텔레그램에서 확인하세요.")
    else:
        raise SystemExit(f"발송 실패: {err}")


def _telegram_send(ctx: Context, url: str, text: str,
                   chat_id: str | None = None, preview: bool = True) -> tuple[bool, str | None]:
    try:
        resp = ctx.http.post(url, json={
            "chat_id": chat_id or ctx.cfg.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        })
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            return True, None
        return False, str(data.get("description") or resp.status_code)
    except Exception as exc:
        return False, str(exc)


# =====================================================================
# 14b. 텔레그램 챗봇 (PRD F7.4 + 자연어 질의응답)
#      run 모드에서 별도 스레드로 getUpdates 롱폴링을 돌린다.
# =====================================================================

TELEGRAM_LOCK = threading.Lock()   # 봇 메시지 처리를 직렬화 (SQLite 쓰기 경합 완화)
_bot_chat_calls: dict[str, list[float]] = {}
BOT_CHAT_MAX_PER_HOUR = 30

BOT_HELP = (
    "<b>P-FM NEWS 봇</b>\n\n"
    "/latest — 최근 기사 5건\n"
    "/today — 오늘 다이제스트\n"
    "/threshold [숫자] — 알림 중요도 임계값 조회·변경\n"
    "/filter [카테고리] — 카테고리별 최근 기사\n"
    "/stop — 알림 일시중지    /start — 알림 재개\n"
    "/help — 도움말\n\n"
    "그 외 메시지는 <b>질문</b>으로 처리합니다.\n"
    "예: <code>포스코DX 최근 소식</code>, <code>이번 주 이차전지 이슈</code>\n"
    "기사 <b>URL</b>을 보내면 즉시 분석해 카드로 만듭니다."
)

TG_QA_SYSTEM = "당신은 포스코 그룹 뉴스 브리핑 어시스턴트다. 아래 제공된 기사 목록만 근거로 한국어로 간결히 답한다."
TG_QA_PROMPT = """사용자 질문: {question}

아래는 최근 수집된 포스코 관련 기사다. 이 목록만 근거로 답하라.
- 관련 기사가 있으면 핵심을 3줄 이내로 요약하고, 참고한 기사 제목과 링크를 최대 3개 붙인다.
- 관련 기사가 없으면 "관련 기사를 찾지 못했습니다"라고만 답한다.
- 목록에 없는 사실을 지어내지 않는다.

[기사 목록]
{articles}
"""


def _bot_rate_ok(chat_id: str) -> bool:
    now = time.time()
    q = [t for t in _bot_chat_calls.get(chat_id, []) if now - t < 3600]
    if len(q) >= BOT_CHAT_MAX_PER_HOUR:
        _bot_chat_calls[chat_id] = q
        return False
    q.append(now)
    _bot_chat_calls[chat_id] = q
    return True


def tg_send(ctx: Context, chat_id: str, text: str, preview: bool = True) -> None:
    url = TELEGRAM_API.format(token=ctx.cfg.telegram_bot_token)
    ok, err = _telegram_send(ctx, url, text[:4000], chat_id=chat_id, preview=preview)
    if not ok:
        log.warning("봇 응답 발송 실패 (%s): %s", chat_id, err)


def _card_line(card: dict) -> str:
    score = int(card.get("importance_score") or 0)
    mark = "🔴" if score >= 80 else "🟠" if score >= 50 else "⚪"
    when = (card.get("published_at") or "")[:10]
    return f'{mark} <a href="{esc(card.get("url") or "")}">{esc(card.get("title") or "")}</a>  <i>{esc(when)}</i>'


def _card_full_text(card: dict, already: bool = False) -> str:
    head = "이미 등록된 기사입니다.\n\n" if already else ""
    score = int(card.get("importance_score") or 0)
    emoji = "🔴" if score >= 80 else "🟠"
    groups = card.get("group_companies") or []
    tag = groups[0] if groups else "포스코"
    lines = [f"{head}{emoji} [{esc(tag)}] {esc(card.get('title') or '')}", ""]
    if card.get("summary_header") or card.get("summary_text"):
        lines.append(f"{esc(card.get('summary_header') or '')} {esc(card.get('summary_text') or '')}".strip())
    if card.get("perspective_text"):
        lines += ["", f"포스코 관점: {esc(card['perspective_text'])}"]
    sw = card.get("swot")
    if sw:
        lines += ["", f"SWOT 종합 {sw['total']} · 감성 {esc(card.get('sentiment') or '-')} · 중요도 {score}"]
    kws = card.get("keywords") or []
    if kws:
        lines.append("키워드: " + esc(", ".join(kws)))
    if card.get("url"):
        lines += ["", f'🔗 <a href="{esc(card["url"])}">원문 보기</a>']
    return "\n".join(lines)


def _bot_recent_cards(ctx: Context, hours: int | None, limit: int) -> list[dict]:
    since = now_utc() - timedelta(hours=hours) if hours else None
    rows = ctx.storage.list_articles(max(limit, 60), 0, since, "")
    return [build_card(r) for r in rows][:limit]


def handle_telegram_update(ctx: Context, update: dict) -> None:
    # 채널 글에는 응답하지 않는다 — 방송 채널은 단방향이라 봇이 끼어들면 안 된다.
    # (channel_post 를 수신은 하되 여기서 무시해 chat_id 탐지·오프셋 정합성만 유지)
    if update.get("channel_post") or update.get("edited_channel_post"):
        return
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return

    # 1) URL 이 포함되면 즉시 분석
    url_m = re.search(r"https?://\S+", text)
    if url_m and not text.startswith("/"):
        tg_send(ctx, chat_id, "🔍 기사를 분석하고 있습니다… (10~20초)")
        res = analyze_url(ctx, url_m.group(0).rstrip(").,"))
        if res.get("ok"):
            tg_send(ctx, chat_id, _card_full_text(res["card"], already=res.get("already")))
        else:
            tg_send(ctx, chat_id, f"❌ {esc(res.get('error') or '분석 실패')}")
        return

    # 2) 슬래시 명령
    if text.startswith("/"):
        cmd, _, arg = text[1:].partition(" ")
        cmd = cmd.split("@")[0].lower()
        _bot_command(ctx, chat_id, cmd, arg.strip())
        return

    # 3) 자연어 질문
    _bot_answer(ctx, chat_id, text)


def _bot_command(ctx: Context, chat_id: str, cmd: str, arg: str) -> None:
    if cmd in ("help", ""):
        tg_send(ctx, chat_id, BOT_HELP)

    elif cmd == "start":
        ctx.storage.set_run_state({"notify_paused": 0})
        tg_send(ctx, chat_id, "알림을 켰습니다. 명령어는 /help 로 확인하세요.\n\n" + BOT_HELP)

    elif cmd == "stop":
        ctx.storage.set_run_state({"notify_paused": 1})
        tg_send(ctx, chat_id, "알림을 일시중지했습니다. /start 로 다시 켤 수 있습니다.")

    elif cmd == "latest":
        cards = _bot_recent_cards(ctx, None, 5)
        if not cards:
            tg_send(ctx, chat_id, "아직 수집된 기사가 없습니다.")
        else:
            tg_send(ctx, chat_id, "<b>최근 기사</b>\n" + "\n".join(_card_line(c) for c in cards), preview=False)

    elif cmd == "today":
        midnight = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        hours = max(1, int((now_utc() - midnight).total_seconds() // 3600) + 1)
        cards = _bot_recent_cards(ctx, hours, 15)
        if not cards:
            tg_send(ctx, chat_id, "오늘 수집된 기사가 없습니다.")
        else:
            tg_send(ctx, chat_id, f"<b>오늘 다이제스트 · {len(cards)}건</b>\n"
                    + "\n".join(_card_line(c) for c in cards), preview=False)

    elif cmd == "threshold":
        if not arg:
            tg_send(ctx, chat_id, f"현재 알림 중요도 임계값: <b>{effective_threshold(ctx)}</b>\n"
                    "변경하려면 <code>/threshold 60</code> 처럼 보내세요 (0~100).")
        elif arg.isdigit() and 0 <= int(arg) <= 100:
            ctx.storage.set_run_state({"notify_threshold": int(arg)})
            tg_send(ctx, chat_id, f"알림 임계값을 <b>{int(arg)}</b> 로 변경했습니다.")
        else:
            tg_send(ctx, chat_id, "0~100 사이 숫자를 보내주세요. 예: <code>/threshold 60</code>")

    elif cmd == "filter":
        cats = list(CATEGORY_RULES.keys()) + ["그룹사"]
        if not arg:
            tg_send(ctx, chat_id, "카테고리를 붙여서 보내세요:\n"
                    + "\n".join(f"<code>/filter {c}</code>" for c in cats))
            return
        want = normalize_chip(arg)
        cards = [c for c in _bot_recent_cards(ctx, 24 * 7, 200)
                 if any(normalize_chip(x) == want or want in normalize_chip(x) for x in c.get("categories") or [])]
        if not cards:
            tg_send(ctx, chat_id, f"'{esc(arg)}' 카테고리의 최근 기사가 없습니다.")
        else:
            tg_send(ctx, chat_id, f"<b>{esc(arg)} · 최근 {min(len(cards), 10)}건</b>\n"
                    + "\n".join(_card_line(c) for c in cards[:10]), preview=False)

    else:
        tg_send(ctx, chat_id, f"모르는 명령입니다: /{esc(cmd)}\n\n" + BOT_HELP)


def _bot_answer(ctx: Context, chat_id: str, question: str) -> None:
    if not _bot_rate_ok(chat_id):
        tg_send(ctx, chat_id, "질문이 많아 잠시 제한합니다. 1시간 뒤 다시 시도해 주세요.")
        return
    rows = ctx.storage.list_articles(40, 0, now_utc() - timedelta(days=7),
                                     question if len(question) <= 20 else "")
    if not rows:
        rows = ctx.storage.list_articles(40, 0, now_utc() - timedelta(days=7), "")
    if not rows:
        tg_send(ctx, chat_id, "아직 수집된 기사가 없습니다.")
        return

    lines = []
    for r in rows[:40]:
        c = build_card(r)
        lines.append(f"- [{(c.get('published_at') or '')[:10]}] {c.get('title')} "
                     f"({c.get('press_name')}) | {' '.join(c.get('group_companies') or [])} "
                     f"| {(c.get('summary_text') or '')[:90]} | {c.get('url')}")
    prompt = TG_QA_PROMPT.format(question=question, articles="\n".join(lines))
    try:
        answer = ctx.llm.chat_text(TG_QA_SYSTEM, prompt).strip()
    except Exception as exc:
        log.warning("봇 질문 응답 실패: %s", exc)
        tg_send(ctx, chat_id, "답변 생성에 실패했습니다. 잠시 후 다시 시도해 주세요.")
        return
    tg_send(ctx, chat_id, answer or "답변을 만들지 못했습니다.", preview=False)


def telegram_bot_loop(ctx: Context, stop: threading.Event) -> None:
    """getUpdates 롱폴링. run 모드에서 별도 스레드로 실행된다."""
    if not ctx.cfg.telegram_enabled:
        return
    base = f"https://api.telegram.org/bot{ctx.cfg.telegram_bot_token}/getUpdates"
    offset = int(ctx.storage.get_run_state().get("tg_offset") or 0)
    log.info("텔레그램 봇 수신 대기 시작")
    while not stop.is_set():
        try:
            resp = ctx.http.get(base, params={
                "offset": offset, "timeout": 25,
                # channel_post 도 받는다(수신만 — handle_telegram_update 가 무시).
                # 안 받으면 텔레그램이 큐에 안 쌓아 나중에 채널 chat_id 탐지가 막힌다.
                "allowed_updates": '["message","channel_post","my_chat_member"]',
            }, timeout=35)
            data = resp.json()
            updates = data.get("result", []) if data.get("ok") else []
            for upd in updates:
                offset = max(offset, int(upd.get("update_id", 0)) + 1)
                try:
                    with TELEGRAM_LOCK:
                        handle_telegram_update(ctx, upd)
                except Exception as exc:
                    log.exception("봇 업데이트 처리 오류: %s", exc)
            if updates:
                ctx.storage.set_run_state({"tg_offset": offset})
        except Exception as exc:
            log.debug("텔레그램 폴링 오류: %s", exc)
            stop.wait(5)


# =====================================================================
# 15. API 서버 (PRD F6)
#     프론트엔드는 외부 API 와 DB 에 직접 접근하지 않고 여기만 호출한다.
# =====================================================================

PERIOD_HOURS = {"today": 24, "7d": 24 * 7, "30d": 24 * 30, "all": 0}
MAX_SCAN_ROWS = 3000   # 배열 필터는 애플리케이션에서 처리하므로 스캔 범위를 제한한다

# ── 마스터 패널 인증 ─────────────────────────────────────────────────
MASTER_TOKEN_TTL = timedelta(hours=24)      # 로그인 유지 (사용자 지정)
RECOMMENDED_MIN_SCORE = 40                  # 관련도 하한 권장값 (마스터는 0~100 자유)
_MASTER_TOKENS: dict[str, datetime] = {}    # token -> 만료시각 (서버 메모리, 재시작 시 소멸)


def hash_password(pw: str) -> str:
    """pbkdf2-sha256. 결과는 'pbkdf2_sha256$반복수$salt$hash' 한 줄."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def _issue_master_token() -> str:
    now = now_utc()
    for tok, exp in list(_MASTER_TOKENS.items()):
        if exp <= now:
            _MASTER_TOKENS.pop(tok, None)
    tok = secrets.token_urlsafe(24)
    _MASTER_TOKENS[tok] = now + MASTER_TOKEN_TTL
    return tok


def _valid_master_token(tok: str) -> bool:
    exp = _MASTER_TOKENS.get(tok or "")
    return bool(exp and exp > now_utc())


def check_master_password(ctx: Context, pw: str) -> bool:
    """DB 에 변경된 해시가 있으면 그것을, 없으면 .env 의 MASTER_PASSWORD 를 쓴다."""
    if not pw:
        return False
    stored = ctx.storage.get_run_state().get("master_pw_hash")
    if stored:
        return verify_password(pw, stored)
    env_pw = ctx.cfg.master_password
    return bool(env_pw) and hmac.compare_digest(pw, env_pw)


def card_tags(row: dict) -> tuple[list[str], list[str], str]:
    """카드의 필터 대상 태그 (그룹사, 카테고리, 언론사명). build_card 와 같은 폴백을 쓴다.

    목록·필터 응답에서 전체 행을 build_card 하지 않고 필터만 걸 때 쓴다.
    """
    groups = normalize_group_list(jload(row.get("group_companies"), []))
    if not groups:
        probe = " ".join([
            row.get("title") or "", row.get("summary_text") or "",
            " ".join(jload(row.get("keywords"), [])),
        ])
        groups = normalize_group_list(detect_group_companies(probe))
        # 정책브리핑·통상환경·배터리기술 기사는 포스코 미언급이 정상 — '포스코' 폴백 안 씌운다.
        # 일반 기사는 수집 게이트에서 포스코 관련이 확인됐으므로 최소 '포스코'.
        if (not groups and not is_policy_brief(row)
                and TRADE_CATEGORY not in jload(row.get("categories"), [])
                and not is_battery_scope(row.get("title") or "", row.get("summary_text") or "")):
            groups = ["포스코"]
    categories = dedupe_chips(jload(row.get("categories"), []), exclude=groups)
    return groups, categories, row.get("press_name") or ""


def build_card(row: dict) -> dict:
    """카드 1건의 표시용 형태를 만든다. 칩 중복 제거를 여기서 한 번 더 한다. (PRD F6.2b)"""
    groups, categories, _ = card_tags(row)
    keywords = dedupe_chips(jload(row.get("keywords"), []), exclude=groups + categories)

    swot = None
    if row.get("swot_total") is not None:
        s_sc = int(row.get("s_score") or 0)
        w_sc = int(row.get("w_score") or 0)
        o_sc = int(row.get("o_score") or 0)
        t_sc = int(row.get("t_score") or 0)
        # S/W/O/T 가 모두 0 이면 LLM 이 근거를 찾지 못한 것이다.
        # 이때 종합점수는 공식상 50(중립)이 나오는데, 배지에 '50'을 띄우면
        # 실제로 평가된 것처럼 오해된다. 그래서 배지 자체를 노출하지 않는다.
        if s_sc or w_sc or o_sc or t_sc:
            swot = {
                "total": int(row.get("swot_total") or 0),
                "s": {"score": s_sc, "text": row.get("s_text") or "해당 없음"},
                "w": {"score": w_sc, "text": row.get("w_text") or "해당 없음"},
                "o": {"score": o_sc, "text": row.get("o_text") or "해당 없음"},
                "t": {"score": t_sc, "text": row.get("t_text") or "해당 없음"},
            }

    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "url": row.get("url_canonical") or row.get("url_original") or "",
        "press_name": row.get("press_name") or "",
        "author": row.get("author") or "",
        "summary_header": format_summary_header(row.get("press_name") or "", row.get("author") or ""),
        "summary_text": row.get("summary_text") or "",
        "perspective_text": row.get("perspective_text") or "",
        "summary_source": row.get("summary_source") or "",
        "published_at": row.get("published_at"),
        "thumbnail_url": row.get("thumbnail_url") or "",
        "importance_score": int(row.get("importance_score") or 0),
        "sentiment": row.get("sentiment") or "",
        "keywords": keywords,
        "group_companies": groups,
        "categories": categories,
        "is_backfill": bool(row.get("is_backfill")),
        "swot": swot,
    }


def _split_multi(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def apply_filters(rows: list[dict], groups: list[str], cats: list[str], presses: list[str]) -> list[dict]:
    """행 목록을 필터한다. 같은 그룹 안은 OR, 다른 그룹 사이는 AND. (PRD F6.1a)

    카드로 변환하지 않고 card_tags 만 계산해 필터한다 — 전체 스캔 비용을 줄인다.
    """
    g_keys = {normalize_chip(x) for x in groups}
    c_keys = {normalize_chip(x) for x in cats}
    p_keys = {normalize_chip(x) for x in presses}
    if not (g_keys or c_keys or p_keys):
        return list(rows)

    def matches(row: dict) -> bool:
        rg, rc, rp = card_tags(row)
        if g_keys and not (g_keys & {normalize_chip(x) for x in rg}):
            return False
        if c_keys and not (c_keys & {normalize_chip(x) for x in rc}):
            return False
        if p_keys and normalize_chip(rp) not in p_keys:
            return False
        return True
    return [r for r in rows if matches(r)]


def create_app(ctx: Context):
    fastapi = _import("fastapi", "fastapi")
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = fastapi.FastAPI(title="P-FM NEWS API", docs_url="/api/docs")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"]
    )

    @app.middleware("http")
    async def _no_cache_frontend(request, call_next):
        """프런트 정적 파일(HTML·JS·CSS)은 캐시하지 않는다.

        수정 후에도 브라우저·중간 프록시가 옛 파일을 계속 내주는 문제를 막는다.
        """
        resp = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".js", ".css", ".html")):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp
    # 수동 URL 등록이 겹치지 않게 직렬화한다(SQLite 쓰기 경합 방지).
    _manual_lock = threading.Lock()

    def _scan_rows(period: str, query: str) -> list[dict]:
        hours = PERIOD_HOURS.get(period, 0)
        since = now_utc() - timedelta(hours=hours) if hours else None
        return ctx.storage.list_articles(MAX_SCAN_ROWS, 0, since, query)

    @app.get("/api/articles")
    def api_articles(group: str = "", cat: str = "", press: str = "",
                     period: str = "all", q: str = "", page: int = 1, size: int = 20):
        page = max(1, page)
        size = int(clamp(size, 1, 100))
        matched = apply_filters(_scan_rows(period, q),
                                _split_multi(group), _split_multi(cat), _split_multi(press))
        start = (page - 1) * size
        # 필터를 통과한 행만 세고, 화면에 보일 페이지 분량만 카드로 만든다.
        return JSONResponse({
            "total": len(matched),
            "page": page,
            "size": size,
            "items": [build_card(r) for r in matched[start:start + size]],
        })

    # 필터 칩 고정 순서 — 목록에 없는 값은 뒤에 원래 순서로 붙는다.
    GROUP_ORDER = ["포스코퓨처엠", "포스코홀딩스", "포스코", "포스코DX", "포스코이앤씨"]
    CATEGORY_ORDER = ["양극재", "음극재", "배터리·이차전지", "산업", "시장/주가",
                      "정부/정책", "법령", "글로벌 통상환경"]

    def _ordered(values: list[str], priority: list[str]) -> list[str]:
        uniq = dedupe_chips(values)
        return [p for p in priority if p in uniq] + [v for v in uniq if v not in priority]

    # 필터 목록은 전체 스캔이 필요한데 수집 주기(60초)에 한 번만 바뀐다.
    # 페이지를 열 때마다 /api/articles 와 같은 스캔을 두 번 하지 않도록 짧게 캐시한다.
    _filters_cache: dict[str, Any] = {"at": 0.0, "data": None}
    FILTERS_TTL_SEC = 20

    @app.get("/api/filters")
    def api_filters():
        """현재 데이터에 실제로 존재하는 필터 값만 돌려준다."""
        now = time.monotonic()
        if _filters_cache["data"] and now - _filters_cache["at"] < FILTERS_TTL_SEC:
            return JSONResponse(_filters_cache["data"])

        groups: list[str] = []
        cats: list[str] = []
        presses: list[str] = []
        for row in _scan_rows("all", ""):
            g, c, pname = card_tags(row)
            groups += g
            cats += c
            if pname:
                presses.append(pname)
        data = {
            "groups": _ordered(groups, GROUP_ORDER),
            "categories": _ordered(cats, CATEGORY_ORDER),
            "presses": [p for p, _ in Counter(presses).most_common()],  # 기사 많은 순
            "periods": [{"key": "today", "label": "오늘"}, {"key": "7d", "label": "7일"},
                        {"key": "30d", "label": "30일"}, {"key": "all", "label": "전체"}],
        }
        _filters_cache.update(at=now, data=data)
        return JSONResponse(data)

    @app.get("/api/quotes")
    def api_quotes():
        rows = ctx.storage.all_quotes()
        out = []
        now = now_utc()
        for row in rows:
            fetched = parse_dt(row.get("fetched_at"))
            stale = fetched is None or (now - fetched) > timedelta(minutes=QUOTE_STALE_MINUTES)
            out.append({
                "symbol": row.get("symbol"), "kind": row.get("kind"), "label": row.get("label"),
                "price": float(row.get("price") or 0),
                "change_rate": float(row["change_rate"]) if row.get("change_rate") is not None else None,
                "fetched_at": row.get("fetched_at"), "stale": stale,
            })
        order = {s: i for i, (s, _) in enumerate(STOCK_SYMBOLS)}
        order.update({s: 10 + i for i, (_, s, _) in enumerate(FX_SYMBOLS)})
        out.sort(key=lambda r: order.get(r["symbol"], 99))
        return JSONResponse({"items": out, "server_time": iso(now)})

    @app.get("/api/stats")
    def api_stats():
        return JSONResponse(ctx.storage.stats())

    @app.get("/api/articles/{article_id}")
    def api_article(article_id: str):
        row = ctx.storage.article_detail(article_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(build_card(row))

    @app.post("/api/articles/{article_id}/telegram")
    async def api_share_telegram(article_id: str):
        """카드의 전송 버튼 — 이 기사 요약을 설정된 텔레그램 채팅으로 보낸다."""
        if not ctx.cfg.telegram_enabled:
            return JSONResponse({"ok": False, "error": "텔레그램이 설정되지 않았습니다."},
                                status_code=400)
        row = ctx.storage.article_detail(article_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "기사를 찾을 수 없습니다."}, status_code=404)
        import anyio
        api_url = TELEGRAM_API.format(token=ctx.cfg.telegram_bot_token)

        def _work():
            return _telegram_send(ctx, api_url, format_message(row))

        ok, err = await anyio.to_thread.run_sync(_work)
        if ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": err or "발송에 실패했습니다."}, status_code=502)

    _tg_link_cache: dict[str, str] = {}

    @app.get("/api/telegram-link")
    def api_telegram_link():
        """헤더 Telegram 버튼용 주소.

        TELEGRAM_CHANNEL_URL 이 있으면 그 값(채널 초대 링크 등)을,
        없으면 봇 대화방(t.me/<봇아이디>)을 돌려준다.
        """
        if ctx.cfg.telegram_channel_url:
            return JSONResponse({"ok": True, "url": ctx.cfg.telegram_channel_url, "kind": "channel"})
        if not ctx.cfg.telegram_enabled:
            return JSONResponse({"ok": False, "error": "텔레그램이 설정되지 않았습니다."})
        url = _tg_link_cache.get("url")
        if not url:
            try:
                resp = ctx.http.get(
                    f"https://api.telegram.org/bot{ctx.cfg.telegram_bot_token}/getMe", timeout=8)
                uname = ((resp.json() or {}).get("result") or {}).get("username")
                if uname:
                    url = f"https://t.me/{uname}"
                    _tg_link_cache["url"] = url
            except Exception as exc:
                log.debug("텔레그램 봇 주소 조회 실패: %s", exc)
        if not url:
            return JSONResponse({"ok": False, "error": "봇 주소를 확인하지 못했습니다."})
        return JSONResponse({"ok": True, "url": url, "kind": "bot"})

    # ── 마스터 패널 (PRD 추가) ──────────────────────────────────────
    def _master_guard(token: str) -> JSONResponse | None:
        if not _valid_master_token(token or ""):
            return JSONResponse({"ok": False, "error": "마스터 인증이 필요합니다."}, status_code=401)
        return None

    @app.post("/api/master/login")
    async def api_master_login(payload: dict):
        pw = (payload or {}).get("password", "")
        if not isinstance(pw, str) or not check_master_password(ctx, pw):
            return JSONResponse({"ok": False, "error": "비밀번호가 올바르지 않습니다."},
                                status_code=401)
        return JSONResponse({"ok": True, "token": _issue_master_token(),
                             "ttl_hours": int(MASTER_TOKEN_TTL.total_seconds() // 3600)})

    @app.get("/api/master/settings")
    async def api_master_settings_get(x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        st = ctx.storage.get_run_state()
        return JSONResponse({
            "ok": True,
            "threshold": effective_threshold(ctx),
            "recommended_min": RECOMMENDED_MIN_SCORE,
            "keywords": jload(st.get("always_notify_keywords"), []),
            "web_password": st.get("web_password") or "",
            "notify_policy": str(st.get("notify_policy") or "0") not in ("0", "False", "false", ""),
            "policy_keywords": jload(st.get("policy_notify_keywords"), []),
            "policy_required": jload(st.get("policy_required_keywords"), []),
            "notify_trade": str(st.get("notify_trade") or "0") not in ("0", "False", "false", ""),
            "trade_keywords": jload(st.get("trade_notify_keywords"), []),
            "trade_required": jload(st.get("trade_required_keywords"), []),
        })

    @app.post("/api/master/settings")
    async def api_master_settings_post(payload: dict, x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        patch: dict[str, Any] = {}
        if "threshold" in (payload or {}):
            try:
                patch["notify_threshold"] = int(clamp(int(payload["threshold"]), 0, 100))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "임계값은 0~100 숫자여야 합니다."},
                                    status_code=400)
        # 키워드 목록 필드 — 같은 방식으로 정리(중복 제거, 30개 상한)
        for field, col in (("keywords", "always_notify_keywords"),
                           ("policy_keywords", "policy_notify_keywords"),
                           ("policy_required", "policy_required_keywords"),
                           ("trade_keywords", "trade_notify_keywords"),
                           ("trade_required", "trade_required_keywords")):
            if field in (payload or {}):
                kws = payload[field]
                if not isinstance(kws, list):
                    return JSONResponse({"ok": False, "error": "키워드는 목록이어야 합니다."},
                                        status_code=400)
                patch[col] = jdump(dedupe_chips(
                    str(k).strip() for k in kws if str(k).strip())[:30])
        for field, col in (("notify_policy", "notify_policy"), ("notify_trade", "notify_trade")):
            if field in (payload or {}):
                patch[col] = 1 if payload[field] else 0
        if patch:
            ctx.storage.set_run_state(patch)
        return JSONResponse({"ok": True})

    @app.post("/api/master/password")
    async def api_master_password(payload: dict, x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        target = (payload or {}).get("target", "")
        new_pw = (payload or {}).get("new_password", "")
        if target not in ("master", "web"):
            return JSONResponse({"ok": False, "error": "target 은 master 또는 web 이어야 합니다."},
                                status_code=400)
        if not isinstance(new_pw, str) or len(new_pw) < 4:
            return JSONResponse({"ok": False, "error": "새 비밀번호는 4자 이상이어야 합니다."},
                                status_code=400)
        if target == "master":
            cur = (payload or {}).get("current_password", "")
            if not check_master_password(ctx, cur):
                return JSONResponse({"ok": False, "error": "현재 비밀번호가 올바르지 않습니다."},
                                    status_code=401)
            ctx.storage.set_run_state({"master_pw_hash": hash_password(new_pw)})
        else:
            # 웹페이지 비밀번호는 마스터가 팀에 공유하는 값이라 확인이 가능해야 한다.
            # 해시와 평문을 함께 보관하고, 평문은 마스터 인증 뒤에서만 노출한다.
            ctx.storage.set_run_state({"web_pw_hash": hash_password(new_pw),
                                       "web_password": new_pw})
        return JSONResponse({"ok": True})

    @app.post("/api/analyze-url")
    async def api_analyze_url(payload: dict):
        """URL 하나를 분석해 미리보기 카드를 만든다. status='draft' 로만 저장하고,
        사용자가 /confirm 을 호출해야 목록에 노출된다. (PRD F8 수동 등록)"""
        url = (payload or {}).get("url", "")
        if not isinstance(url, str) or len(url) > 2000:
            return JSONResponse({"ok": False, "error": "URL 형식이 올바르지 않습니다."}, status_code=400)
        import anyio

        def _work():
            with _manual_lock:
                return analyze_url(ctx, url, activate=False)

        result = await anyio.to_thread.run_sync(_work)
        code = 200 if result.get("ok") else 422
        return JSONResponse(result, status_code=code)

    @app.post("/api/articles/{article_id}/confirm")
    def api_confirm_draft(article_id: str):
        """미리보기(draft) 기사를 목록에 등록한다."""
        row = ctx.storage.article_detail(article_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "기사를 찾을 수 없습니다."}, status_code=404)
        if row.get("status") == "draft":
            ctx.storage.update_article(article_id, {"status": "active"})
        return JSONResponse({"ok": True})

    @app.post("/api/articles/{article_id}/discard")
    def api_discard_draft(article_id: str):
        """미리보기(draft) 기사를 등록하지 않고 버린다(보관 처리)."""
        row = ctx.storage.article_detail(article_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "기사를 찾을 수 없습니다."}, status_code=404)
        if row.get("status") == "draft":
            ctx.storage.update_article(article_id, {"status": "archived"})
        return JSONResponse({"ok": True})

    if os.path.isdir(FRONTEND_DIR):
        from fastapi.responses import HTMLResponse

        _index_cache: dict[str, Any] = {"sig": None, "html": ""}
        _index_files = [os.path.join(FRONTEND_DIR, n)
                        for n in ("index.html", "style.css", "app.js")]

        def _render_index() -> str:
            # JS·CSS 를 HTML 에 인라인해서 내보낸다. 별도 정적 요청이 없으므로
            # 쿼리스트링을 무시하는 프록시가 있어도 옛 파일을 내줄 수 없다.
            html, css, js = (open(p, "r", encoding="utf-8").read() for p in _index_files)
            # 리터럴 치환만 한다(re.sub 은 repl 의 \s 등을 이스케이프로 해석해 깨진다).
            html = html.replace('<link rel="stylesheet" href="./style.css">',
                                f"<style>\n{css}\n</style>")
            return html.replace('<script src="./app.js"></script>',
                                f"<script>\n{js}\n</script>")

        @app.get("/")
        def index():
            # 세 파일의 수정시각이 그대로면 조립 결과를 재사용한다(매 요청 디스크 3회 읽기·치환 방지).
            sig = tuple(os.path.getmtime(p) for p in _index_files)
            if _index_cache["sig"] != sig:
                _index_cache.update(sig=sig, html=_render_index())
            return HTMLResponse(_index_cache["html"])

        # 직접 접근(디버그)용으로 파일도 계속 서빙한다.
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


# =====================================================================
# 16. CLI
# =====================================================================

def cmd_fixpress(ctx: Context) -> None:
    """SEED_PRESS 의 최신 이름을 press_outlets 에 반영하고, 기사에도 다시 맞춘다.

    도메인 그대로(예: 'ajunews.com') 저장돼 있던 언론사명을 정식 이름으로 교체한다.
    """
    renamed = 0
    for domain, (name, tier) in SEED_PRESS.items():
        row = ctx.storage.press_by_domain(domain)
        if row is None:
            ctx.storage.upsert_press(domain, name, tier, "approved")
        elif row.get("name") != name:
            ctx.storage.update_press_name(domain, name, tier)
            renamed += 1

    # SEED_PRESS 에 없어 도메인·조각으로 남은 매체는 대표 기사 1건을 받아
    # og:site_name 에서 한글 매체명을 시도한다. 실패하면 도메인 전체로 둔다.
    ogfix = 0
    tried: set[str] = set()
    for r in ctx.storage.list_articles(5000, 0, None, ""):
        name = (r.get("press_name") or "").strip()
        if _has_hangul(name):
            continue
        target = r.get("url_original") or r.get("url_canonical") or ""
        domain = domain_of(target)
        if not domain or domain in SEED_PRESS or domain in tried:
            continue
        tried.add(domain)
        og = ""
        try:
            _, html = resolve_canonical(ctx.http, target)
            og = site_name_from_html(html)
        except Exception as exc:
            log.debug("og:site_name 조회 실패 %s: %s", target, exc)
        ctx.storage.update_press_name(domain, og or domain, 3)
        if og:
            ogfix += 1

    synced = ctx.storage.sync_article_press_names()
    log.info("언론사명 정리: %d개 이름 교체 · %d개 og:site_name 복원 · 기사 %d건 반영",
             renamed, ogfix, synced)


def cmd_fixauthors(ctx: Context) -> None:
    r"""깨진 기자명을 복구한다 (일회성).

    - JSON-LD 유니코드 이스케이프('\uXXXX')가 리터럴로 저장된 값
    - UTF-8 을 latin-1 로 잘못 디코드한 모지바케('ì¡ìë¯¼')
    - URL·도메인·매체명이 기자명 자리에 들어간 값('www.etnews.com', '중앙이코노미뉴스')
      → 원문을 한 번 더 받아 본문 서명('OOO 기자')에서 재추출, 실패하면 비운다
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    fixed = recovered = cleared = 0
    for r in rows:
        author = (r.get("author") or "").strip()
        if not author:
            continue
        restored = clean_author(fix_mojibake(decode_unicode_escapes(author)))
        press = (r.get("press_name") or "").strip()
        bad = (re.match(r"(https?:|www\.)", restored, re.I) or _looks_like_domain(restored)
               or (not restored.isascii() and not _has_hangul(restored))
               or not (2 <= len(restored) <= 20)
               or restored.endswith(MEDIA_NAME_SUFFIX)
               or normalize_chip(restored) in NON_AUTHOR_WORDS
               or (press and normalize_chip(restored) == normalize_chip(press)))
        if bad:
            new_author = ""
            target = r.get("url_canonical") or r.get("url_original") or ""
            if target:
                try:
                    _, html = resolve_canonical(ctx.http, target)
                    cand = clean_author(extract_author(html, "", press))
                    if 2 <= len(cand) <= 20:
                        new_author = cand
                except Exception as exc:
                    log.debug("기자명 재추출 실패 %s: %s", target, exc)
            ctx.storage.update_article(r["id"], {"author": new_author})
            if new_author:
                recovered += 1
            else:
                cleared += 1
        elif restored != author:
            ctx.storage.update_article(r["id"], {"author": restored})
            fixed += 1
    log.info("기자명 복구: %d건 정리 · %d건 재추출 · %d건 제거", fixed, recovered, cleared)


def cmd_fixcategories(ctx: Context) -> None:
    """카테고리를 제목+요약 기준으로 재태깅한다 (일회성).

    과거엔 본문 2000자를 스캔하고 규칙에 '정부'·'정책' 같은 흔한 단어가 있어
    거의 모든 기사에 3~4개 카테고리가 붙어 필터가 변별력을 잃었다.
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    fixed = 0
    for r in rows:
        cur = dedupe_chips(jload(r.get("categories"), []))
        probe = f"{r.get('summary_text') or ''}\n{' '.join(jload(r.get('keywords'), []))}"
        new = detect_categories(r.get("title") or "", probe)
        if is_policy_brief(r) and "정부/정책" not in new:
            new = ["정부/정책"] + new
        if new != cur:
            ctx.storage.update_article(r["id"], {"categories": jdump(new)})
            fixed += 1
    log.info("카테고리 재태깅: %d건", fixed)


def cmd_fixgroups(ctx: Context) -> None:
    """제목·요약·키워드에 계열사명이 있는데 빠진 그룹사 태그를 채운다 (일회성).

    기존 태그는 지우지 않는다(수집 시 이미 본문 검증을 거쳤다).
    카드·필터에 계열사가 더 잘 드러나도록 '추가'만 한다.
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    fixed = 0
    for r in rows:
        cur = normalize_group_list(jload(r.get("group_companies"), []))
        probe = "\n".join([
            r.get("title") or "",
            r.get("summary_text") or "",
            " ".join(jload(r.get("keywords"), [])),
        ])
        new = normalize_group_list(list(cur) + detect_group_companies(probe))
        if new != cur:
            ctx.storage.update_article(r["id"], {"group_companies": jdump(new)})
            fixed += 1
    log.info("그룹사 태그 보강: %d건", fixed)


def cmd_regroup(ctx: Context) -> None:
    """그룹사 태그를 새 기준(제목 + 리드 GROUP_LEAD_CHARS)으로 다시 매긴다 (일회성).

    본문 말미의 스치는 계열사 언급 때문에 엉뚱한 계열사가 붙은 기사를 바로잡는다.
    예) '포스코 직접고용…'(부산일보)은 마지막 문단의 '포스코홀딩스 장인화 회장'
        한 번 때문에 '포스코홀딩스'로 태깅됐다.

    원문을 다시 받아 리드만 보고 판정하므로 LLM 비용은 0이다. 본문을 못 받으면
    되돌릴 근거가 없으므로 기존 태그를 그대로 둔다(잘못 지우는 것보다 낫다).
    """
    cutoff = iso(now_utc() - timedelta(days=REGROUP_DAYS))
    rows = [r for r in ctx.storage.list_articles(5000, 0, None, "")
            if (r.get("collected_at") or "") >= cutoff]
    log.info("최근 %d일 기사 %d건 — 원문 리드로 그룹사를 재판정합니다.", REGROUP_DAYS, len(rows))

    urls = [r.get("url_original") or r.get("url_canonical") or "" for r in rows]
    prefetched = prefetch_articles(ctx.http, [u for u in urls if u])
    changed = unchanged = unchecked = 0
    for r, u in zip(rows, urls):
        _, html = prefetched.get(u, ("", ""))
        body = extract_body(html) if html else ""
        if not body:
            unchecked += 1
            continue  # 원문 확인 불가 — 기존 태그 유지
        probe = "\n".join([
            r.get("title") or "", body[:GROUP_LEAD_CHARS],
            r.get("summary_text") or "", " ".join(jload(r.get("keywords"), [])),
        ])
        new = normalize_group_list(detect_group_companies(probe))
        cur = normalize_group_list(jload(r.get("group_companies"), []))
        if not new or new == cur:
            unchanged += 1
            continue
        ctx.storage.update_article(r["id"], {"group_companies": jdump(new)})
        log.info("  %s → %s | %s", cur or ["-"], new, (r.get("title") or "")[:36])
        changed += 1
    log.info("그룹사 재판정: 변경 %d건 · 유지 %d건 · 원문 확인 불가 %d건",
             changed, unchanged, unchecked)


def cmd_fixlinks(ctx: Context) -> None:
    """홈페이지 루트로 잘못 저장된 url_canonical 을 바로잡는다 (일회성).

    구형 CMS 가 기사 페이지에서도 canonical 을 루트로 지정한 경우다.
    카드 링크가 홈으로 연결된다. 원문 URL 로 되돌리고, 기자명이 비어 있으면
    같은 페이지를 한 번 받아 다시 추출한다.
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    fixed_url = fixed_author = 0
    for r in rows:
        canon = r.get("url_canonical") or ""
        if not canon or not _is_bare_root(canon):
            continue
        target = r.get("url_original") or r.get("url_source") or ""
        if not target or _is_bare_root(target):
            continue
        patch: dict[str, Any] = {"url_canonical": normalize_url(target)}
        if not (r.get("author") or "").strip():
            try:
                _, html = resolve_canonical(ctx.http, target)
                name = clean_author(extract_author(html, "", r.get("press_name") or ""))
                if 2 <= len(name) <= 20:
                    patch["author"] = name
                    fixed_author += 1
            except Exception as exc:
                log.debug("링크 보정 중 기자명 재추출 실패 %s: %s", target, exc)
        try:
            ctx.storage.update_article(r["id"], patch)
            fixed_url += 1
        except Exception as exc:
            log.warning("링크 보정 실패 %s: %s", r.get("id"), exc)
    log.info("링크 보정: %d건 · 기자명 추가 %d건", fixed_url, fixed_author)


def cmd_fixofftopic(ctx: Context) -> None:
    """포스코·계열사가 어디에도 없는 기존 기사를 보관 처리한다 (일회성).

    제목·요약·키워드로 먼저 거르고, 남은 후보만 원문을 병렬로 다시 받아
    본문에 포스코 언급이 있으면 유지한다. 확인 불가(요청 실패)면 유지한다.
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    cands = []
    for r in rows:
        if (is_policy_brief(r) or is_trade_article(r)
                or is_battery_scope(r.get("title") or "", r.get("summary_text") or "")):
            continue  # 정책·통상·배터리기술 기사는 포스코 미언급이어도 유지 (사용자 지정)
        text = "\n".join([
            r.get("title") or "", r.get("summary_text") or "",
            " ".join(jload(r.get("keywords"), [])),
            " ".join(jload(r.get("group_companies"), [])),
        ])
        if not (detect_group_companies(text) or POSCO_MENTION_RE.search(text)):
            cands.append(r)
    log.info("1차 무관 후보 %d건 — 원문 재확인 중...", len(cands))

    urls = [r.get("url_original") or r.get("url_canonical") or "" for r in cands]
    prefetched = prefetch_articles(ctx.http, [u for u in urls if u])
    archived = kept = unchecked = 0
    for r, u in zip(cands, urls):
        _, html = prefetched.get(u, ("", ""))
        body = extract_body(html) if html else ""
        if not body:
            unchecked += 1
            continue  # 확인 불가 → 유지
        probe = f"{r.get('title') or ''}\n{body}"
        if detect_group_companies(probe) or POSCO_MENTION_RE.search(probe):
            kept += 1
        else:
            ctx.storage.update_article(r["id"], {"status": "archived"})
            archived += 1
    log.info("무관 기사 정리: 보관 %d건 · 본문에 포스코 있어 유지 %d건 · 확인 불가 유지 %d건",
             archived, kept, unchecked)


def cmd_reanalyze(ctx: Context) -> None:
    """분석이 끊긴 기사를 원문에서 되살린다 (일회성).

    분석에 한 번 실패하면 임시 본문이 지워지므로(§7-3) 백로그 큐에서 빠져
    요약·키워드가 영영 비어 있게 된다. 원문을 다시 받아 재분석한다.
    """
    rows = [r for r in ctx.storage.list_articles(5000, 0, None, "") if not r.get("analyzed_at")]
    log.info("미분석 %d건 — 원문에서 본문을 다시 받아 분석합니다.", len(rows))
    done = failed = 0
    for r in rows:
        target = r.get("url_canonical") or r.get("url_original") or ""
        if not target:
            failed += 1
            continue
        try:
            _, html = resolve_canonical(ctx.http, target)
            body = extract_body(html)
            source = "fulltext" if len(body) >= 300 else "snippet"
            if source == "snippet":
                body = r.get("summary_text") or r.get("title") or ""
            ctx.storage.save_body(r["id"], body, source)
            row = {
                "id": r["id"], "title": r.get("title") or "",
                "press_id": r.get("press_id"), "press_name": r.get("press_name") or "",
                "importance_score": r.get("importance_score") or 0,
                "group_companies": jload(r.get("group_companies"), []),
            }
            if analyze_and_save(ctx, r["id"], row, body, source) is not None:
                done += 1
            else:
                failed += 1
        except Exception as exc:
            log.debug("재분석 실패 %s: %s", target, exc)
            failed += 1
    log.info("재분석: 성공 %d건 · 실패 %d건", done, failed)


def cmd_reswot(ctx: Context) -> None:
    """SWOT 가 전부 0(카드에서 숨겨짐)인 fulltext 기사를 재분석한다 (일회성).

    프롬프트를 적극화한 뒤, 예전에 전 항목 0으로 저장된 기사를 다시 돌린다.
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    targets = []
    for r in rows:
        if not r.get("analyzed_at") or r.get("summary_source") == "snippet":
            continue
        scores = [int(r.get(k) or 0) for k in ("s_score", "w_score", "o_score", "t_score")]
        if r.get("swot_total") is None or not any(scores):
            targets.append(r)
    log.info("SWOT 재분석 대상 %d건", len(targets))
    done = failed = 0
    for r in targets:
        target = r.get("url_canonical") or r.get("url_original") or ""
        try:
            _, html = resolve_canonical(ctx.http, target)
            body = extract_body(html)
            if len(body) < 300:
                failed += 1
                continue
            ctx.storage.save_body(r["id"], body, "fulltext")
            row = {
                "id": r["id"], "title": r.get("title") or "",
                "press_id": r.get("press_id"), "press_name": r.get("press_name") or "",
                "importance_score": r.get("importance_score") or 0,
                "group_companies": jload(r.get("group_companies"), []),
            }
            if analyze_and_save(ctx, r["id"], row, body, "fulltext") is not None:
                done += 1
            else:
                failed += 1
        except Exception as exc:
            log.debug("SWOT 재분석 실패 %s: %s", target, exc)
            failed += 1
    log.info("SWOT 재분석: 성공 %d건 · 실패 %d건", done, failed)


def cmd_fixdates(ctx: Context) -> None:
    """타임존 오파싱으로 미래에 저장된 발행시각을 보정한다 (일회성).

    과거 버전은 한국 언론사 RSS('YYYY-MM-DD HH:MM:SS', 타임존 없음)를 UTC 로 오인해
    published_at 이 실제보다 9시간 미래가 됐다. 목록에서 '방금'으로 뜨고 최상단을 차지한다.
    published_at 이 collected_at 보다 2시간 넘게 미래면 9시간(KST→UTC) 뺀다.
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    fixed = 0
    for r in rows:
        pub = parse_dt(r.get("published_at"))
        col = parse_dt(r.get("collected_at"))
        if not pub or not col:
            continue
        if pub > col + timedelta(hours=2):
            ctx.storage.update_article(r["id"], {"published_at": iso(pub - timedelta(hours=9))})
            fixed += 1
    log.info("발행시각 보정: %d건 (미래 저장분 KST→UTC 보정)", fixed)


def cmd_initdb(ctx: Context) -> None:
    ctx.storage.init_schema()
    ctx.storage.seed_keywords(SEED_KEYWORDS)
    ctx.storage.seed_feeds(SEED_FEEDS)
    for domain, (name, tier) in SEED_PRESS.items():
        ctx.storage.upsert_press(domain, name, tier, "approved")
    log.info("스키마 생성 완료. 키워드 %d개 · 언론사 %d개 시드 입력됨.",
             len(SEED_KEYWORDS), len(SEED_PRESS))


def cmd_once(ctx: Context, max_llm: int | None) -> None:
    refresh_quotes(ctx)
    # 수동 1회 실행에서는 네이버 간격 제한을 무시한다(바로 확인하려는 것이므로).
    result = run_once(ctx, max_llm=max_llm, force_naver=True)
    sent = send_notifications(ctx)
    log.info("텔레그램 발송 %d건", sent)
    print(json.dumps(result, ensure_ascii=False, indent=2))


QUOTE_REFRESH_SEC = 60   # 시세는 수집 주기와 무관하게 항상 60초로 갱신한다. (PRD F9.2)


def pipeline_loop(ctx: Context, stop: threading.Event) -> None:
    """수집·분석·알림 루프. 시세는 quote_loop 가 따로 돈다."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            run_once(ctx)
            send_notifications(ctx)
        except Exception as exc:
            log.exception("파이프라인 실행 중 오류: %s", exc)

        elapsed = time.monotonic() - started
        if elapsed > ctx.cfg.poll_interval_sec:
            log.warning("실행이 %.1f초 걸렸습니다(주기 %d초). 다음 회차가 밀릴 수 있습니다.",
                        elapsed, ctx.cfg.poll_interval_sec)
        stop.wait(max(1.0, ctx.cfg.poll_interval_sec - elapsed))


def quote_loop(ctx: Context, stop: threading.Event) -> None:
    """시세 갱신 전용 루프 — 수집 주기(60~300초)와 독립적으로 60초마다."""
    while not stop.is_set():
        try:
            refresh_quotes(ctx)
        except Exception as exc:
            log.debug("시세 갱신 실패: %s", exc)
        stop.wait(QUOTE_REFRESH_SEC)


def cmd_serve(ctx: Context, with_pipeline: bool) -> None:
    uvicorn = _import("uvicorn", "uvicorn")
    app = create_app(ctx)
    stop = threading.Event()
    threads: list[threading.Thread] = []
    if with_pipeline:
        threads.append(threading.Thread(target=pipeline_loop, args=(ctx, stop), daemon=True))
        threads.append(threading.Thread(target=quote_loop, args=(ctx, stop), daemon=True))
        log.info("수집 루프 시작 (%d초 주기) · 시세 갱신 %d초", ctx.cfg.poll_interval_sec, QUOTE_REFRESH_SEC)
        if ctx.cfg.telegram_enabled:
            threads.append(threading.Thread(target=telegram_bot_loop, args=(ctx, stop), daemon=True))
    for t in threads:
        t.start()
    log.info("서버: http://%s:%d", ctx.cfg.api_host, ctx.cfg.api_port)
    try:
        uvicorn.run(app, host=ctx.cfg.api_host, port=ctx.cfg.api_port, log_level="warning")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)


# ── 내장 검증 (DB · 네트워크 · API 키 불필요) ────────────────────────

def cmd_selftest() -> int:
    """순수 함수들을 검증한다. CLAUDE.md 의 '검증 3회 이상' 규약을 자동화한 것."""
    failures: list[str] = []

    def check(name: str, got: Any, want: Any) -> None:
        if got != want:
            failures.append(f"  ✗ {name}\n      기대: {want!r}\n      실제: {got!r}")
        else:
            print(f"  ✓ {name}")

    print("\n[1] URL 정규화 (PRD F2.1)")
    check("추적 파라미터 제거",
          normalize_url("https://News.Example.com/a/?utm_source=x&id=3&fbclid=z"),
          "https://news.example.com/a?id=3")
    check("후행 슬래시·기본 포트 정리",
          normalize_url("HTTPS://Example.com:443/path/"), "https://example.com/path")
    check("프래그먼트 제거", normalize_url("https://a.com/b#top"), "https://a.com/b")
    check("빈 입력", normalize_url(""), "")

    print("\n[2] 도메인 접기 (PRD F2.3)")
    check("서브도메인", domain_of("https://news.chosun.com/a"), "chosun.com")
    check("co.kr 2단계", domain_of("https://biz.hankyung.co.kr/a"), "hankyung.co.kr")
    check("www 제거", domain_of("https://www.yna.co.kr/x"), "yna.co.kr")

    print("\n[2-1] canonical URL 검증 (홈 루트 오설정 방어)")
    check("루트 canonical 무시",
          _canonical_from_html('<link rel="canonical" href="http://www.techholic.co.kr">'
                               '<meta property="og:url" content="http://www.techholic.co.kr/news/articleView.html?idxno=222703"/>'),
          "http://www.techholic.co.kr/news/articleView.html?idxno=222703")
    check("정상 canonical 채택",
          _canonical_from_html('<link rel="canonical" href="https://a.com/news/1">'), "https://a.com/news/1")
    check("루트 URL 판정", _is_bare_root("http://a.com/"), True)
    check("기사 URL 은 루트 아님", _is_bare_root("http://a.com/news?idxno=1"), False)

    print("\n[3] 제목 정규화·유사도 (PRD F2.2)")
    check("말머리 제거", normalize_title("[속보] 포스코 신규 투자"), "포스코신규투자")
    check("중첩 말머리", normalize_title("[단독](종합) 포스코"), "포스코")
    check("언론사 꼬리 제거", normalize_title("포스코이앤씨 수익성 - FETV"), "포스코이앤씨수익성")
    sim = title_similarity("[속보] 포스코퓨처엠 양극재 증설", "포스코퓨처엠 양극재 증설 - 한국경제")
    check("전재 기사 유사도 ≥ 0.9", sim >= 0.9, True)
    check("다른 기사 유사도 < 0.9", title_similarity("포스코 실적 발표", "리튬 가격 급락") < 0.9, True)

    print("\n[4] 칩 중복 제거 (PRD F4.2 / F6.2b)")
    check("중복 칩 제거",
          dedupe_chips(["포스코그룹", "포스코 그룹", "포스코그룹", "양극재"]),
          ["포스코그룹", "양극재"])
    check("그룹사와 겹치는 키워드 제외",
          dedupe_chips(["포스코홀딩스", "양극재"], exclude=["포스코홀딩스"]), ["양극재"])
    check("빈 값 무시", dedupe_chips(["", "  ", "리튬"]), ["리튬"])
    # 순수 한글 칩의 키가 비면 dedupe 단계에서 통째로 버려진다.
    # (프런트 chipKey 가 JS \W 를 쓰다가 한글을 전부 지운 회귀가 있었다)
    check("순수 한글 칩 키 유지", normalize_chip("배터리·이차전지"), "배터리이차전지")
    check("한글 칩 살아남음", dedupe_chips(["신재생에너지", "전력망 투자"]),
          ["신재생에너지", "전력망 투자"])
    check("한글+영문 혼합", normalize_chip("ESS 시장 확대"), "ess시장확대")
    _front = os.path.join(FRONTEND_DIR, "app.js")
    if os.path.exists(_front):
        _js = open(_front, encoding="utf-8").read()
        check("프런트 chipKey 가 유니코드 클래스를 쓴다",
              "[^\\p{L}\\p{N}]+" in _js and "[\\s\\W_]+" not in _js, True)

    print("\n[5] 그룹사 판정 (PRD F4.2)")
    check("퓨처엠 단독", detect_group_companies("포스코퓨처엠이 증설한다"), ["포스코퓨처엠"])
    check("상위 개념 미중복", "포스코" in detect_group_companies("포스코퓨처엠 소식"), False)
    check("포스코만", detect_group_companies("포스코가 발표했다"), ["포스코"])
    check("무관 기사", detect_group_companies("삼성전자 실적"), [])
    check("소규모 계열사도 판정", detect_group_companies("포스코모빌리티솔루션 신규 라인"), ["포스코모빌리티솔루션"])
    check("계열사 잡히면 '포스코' 미부착",
          "포스코" in normalize_group_list(detect_group_companies("삼척블루파워 발전소")), False)

    # 본문 말미의 스치는 계열사 언급이 기사 주체를 가로채지 않아야 한다.
    # 실제 오탐 사례: 부산일보 '포스코 직접고용…' 본문 1,859번째의 '포스코홀딩스 장인화 회장'
    _lead = "포스코 직접고용 이행하겠다지만 부담 비용 눈덩이\n포스코가 사내하청 노동자 불법파견 소송에서 항소를 포기했다."
    _tail = "ㅁ" * 1200 + "일각에서는 포스코홀딩스 장인화 회장 체제에서 혼란을 가중한 것 아니냐는 지적이 나온다."
    check("본문 전체를 보면 주체가 뒤바뀐다(회귀 재현)",
          detect_group_companies(_lead + "\n" + _tail), ["포스코홀딩스"])
    check("리드까지만 보면 주체가 유지된다",
          detect_group_companies((_lead + "\n" + _tail)[:GROUP_LEAD_CHARS]), ["포스코"])
    # 계열사를 실제로 나열하는 기사는 리드 안에 다 들어와 그대로 잡힌다.
    _hire = ("포스코그룹, 6개 계열사 하반기 공채\n포스코홀딩스 미래기술연구원과"
             " 포스코인터내셔널, 포스코이앤씨, 포스코DX 등 6개사가 신입사원을 모집한다.")
    check("계열사 나열 기사는 리드에서 전부 잡힘",
          sorted(detect_group_companies(_hire[:GROUP_LEAD_CHARS])),
          sorted(["포스코홀딩스", "포스코DX", "포스코인터내셔널", "포스코이앤씨"]))
    check("카테고리에 '그룹사' 없음", "그룹사" in detect_categories("포스코퓨처엠 양극재 증설"), False)
    check("병합 시 상위 개념 제거",
          normalize_group_list(["포스코홀딩스", "포스코", "포스코퓨처엠"]),
          ["포스코홀딩스", "포스코퓨처엠"])
    check("'포스코' 단독일 때는 유지", normalize_group_list(["포스코"]), ["포스코"])
    check("목록 밖 회사명 폐기", normalize_group_list(["포스코", "삼성전자"]), ["포스코"])

    print("\n[6] 중요도 스코어 (PRD F3.2)")
    check("제목 퓨처엠 + 정책",
          score_article("포스코퓨처엠 보조금 규제 대응", "본문", ["포스코퓨처엠"], 3), 70)
    check("0~100 범위 클램프",
          0 <= score_article("포스코퓨처엠 화재 사고 포항 규제", "포스코홀딩스", ["포스코퓨처엠", "포스코홀딩스"], 1) <= 100,
          True)
    check("시황 기사 감점 반영",
          score_article("코스피 시황 목표주가", "단순 시황", [], 3) == 0, True)

    print("\n[7] SWOT 정규화 (PRD F4.4)")
    check("전부 0 → 50", swot_total({"s": {"score": 0}, "w": {"score": 0},
                                     "o": {"score": 0}, "t": {"score": 0}}), 50)
    check("S·O 최대 → 100", swot_total({"s": {"score": 100}, "w": {"score": 0},
                                        "o": {"score": 100}, "t": {"score": 0}}), 100)
    check("W·T 최대 → 0", swot_total({"s": {"score": 0}, "w": {"score": 100},
                                      "o": {"score": 0}, "t": {"score": 100}}), 0)
    check("빈 입력", swot_total({}), 0)

    print("\n[7-1] 발행시각 파싱 (타임존 없으면 KST 로 간주)")
    check("타임존 없는 KST 로컬 → UTC -9h",
          iso(parse_feed_datetime("2026-09-02 08:29:51")), "2026-09-01T23:29:51Z")
    check("RFC822 +0900 존중",
          iso(parse_feed_datetime("Tue, 02 Sep 2026 08:29:51 +0900")), "2026-09-01T23:29:51Z")
    check("RFC822 GMT 존중",
          iso(parse_feed_datetime("Mon, 01 Sep 2026 23:29:51 GMT")), "2026-09-01T23:29:51Z")
    check("ISO +09:00 존중",
          iso(parse_feed_datetime("2026-09-01T23:00:00+09:00")), "2026-09-01T14:00:00Z")
    check("먼 미래는 파싱 오류로 폐기", parse_feed_datetime("2099-01-01 00:00:00"), None)
    check("빈 값 → None", parse_feed_datetime(""), None)
    check("struct_time 폴백(GMT)",
          iso(parse_feed_datetime("", (2026, 9, 1, 23, 0, 0, 0, 0, 0))), "2026-09-01T23:00:00Z")

    print("\n[8] 요약 머리표 조립 (PRD F4.1)")
    check("언론사+기자", format_summary_header("FETV", "임요한"), "[FETV, 임요한]")
    check("기자 없음", format_summary_header("국제신문", ""), "[국제신문]")
    check("둘 다 없음", format_summary_header("", ""), "")
    check("정상 기자명 추출", extract_author("", "이 기사는 홍길동 기자가 작성", "매일경제"), "홍길동")
    check("언론사명은 기자명이 아님", extract_author("", "뉴시스 기자", "뉴시스"), "")
    check("편집국 등 비인명 제외", extract_author("", "편집국 기자", "한국경제"), "")
    check("JSON-LD 유니코드 이스케이프 디코딩",
          decode_unicode_escapes("\\ud55c\\ud61c\\uc120"), "한혜선")
    check("이스케이프 없는 문자열은 그대로", decode_unicode_escapes("김소영"), "김소영")
    check("JSON-LD author 이스케이프 → 정상 기자명",
          extract_author('{"author":{"name":"\\ud64d\\uae38\\ub3d9"}}', "", "벤처스퀘어"), "홍길동")
    check("모지바케(UTF-8→latin-1) 복구",
          fix_mojibake("김소영".encode("utf-8").decode("latin-1")), "김소영")
    check("정상 한글은 모지바케 처리 안 함", fix_mojibake("김소영"), "김소영")
    check("www 도메인은 기자명이 아님",
          extract_author('<meta name="author" content="www.etnews.com">', "", "전자신문"), "")
    check("소속 머리표 제거", clean_author("영남본부=장원규"), "장원규")
    check("'기자' 접미어 제거", clean_author("송영민 기자"), "송영민")
    check("사진 꼬리 제거", clean_author("김철수·사진"), "김철수")
    check("카테고리 라벨('칼럼기자')보다 반복된 서명 우선",
          extract_author("칼럼기자 코너. 조택영 기자. 조택영 기자. 조택영 기자.", "", "프라임경제"), "조택영")
    check("'시민기자' 라벨 제외하고 실제 서명 채택",
          extract_author("시민기자 게시판. 단정민 기자 단정민 기자 단정민 기자", "", "경북매일"), "단정민")
    check("매체명이 meta author 에 있으면 본문 서명 사용",
          extract_author('<meta name="author" content="중앙이코노미뉴스"/>'
                         '<p>이 기사는 조용우 기자가 작성했다</p>', "", "중앙이코노미뉴스"), "조용우")
    check("매체명 단독이면 기자명 아님",
          extract_author('<meta name="author" content="중앙이코노미뉴스"/>', "", "중앙뉴스"), "")

    print("\n[8-0] 언론사 도메인 폴백")
    check("매핑 없는 도메인은 전체 유지", prettify_domain("bbsi.co.kr"), "bbsi.co.kr")
    check("SEED_PRESS 신규 매핑 반영", SEED_PRESS.get("venturesquare.net", ("", 0))[0], "벤처스퀘어")
    check("사용자 지정 매핑(포쓰저널)", SEED_PRESS.get("4th.kr", ("", 0))[0], "포쓰저널")
    check("og:site_name 한글 추출",
          site_name_from_html('<meta property="og:site_name" content="비즈니스포스트"/>'), "비즈니스포스트")
    check("영문 og:site_name 은 무시",
          site_name_from_html('<meta property="og:site_name" content="BusinessPost"/>'), "")

    print("\n[8-1] 카테고리 태깅 (PRD F3.1)")
    check("'지역' 카테고리는 폐지됨", "지역" in detect_categories("포항 공장에서 사고"), False)
    check("양극재는 '양극재' 카테고리", "양극재" in detect_categories("포스코퓨처엠 양극재 증설"), True)
    check("음극재는 '음극재' 카테고리", "음극재" in detect_categories("인조흑연 음극재 공장"), True)
    check("양극재는 배터리·이차전지 아님(별도)",
          "배터리·이차전지" in detect_categories("양극재 증설"), False)
    check("LFP 는 양극재 카테고리", "양극재" in detect_categories("LFP 양극재 라인 전환"), True)
    check("LFP 는 배터리·이차전지 아님", "배터리·이차전지" in detect_categories("LFP 생산"), False)
    check("셀·전기차는 배터리·이차전지", "배터리·이차전지" in detect_categories("전기차 배터리셀 계약"), True)
    check("'정부' 단독은 정책 태그 아님",
          "정부/정책" in detect_categories("정부 관계자 만난 수출입은행"), False)
    check("행정 용어는 '정부/정책'", "정부/정책" in detect_categories("특화단지 지정, 국회 통과"), True)
    check("입법 용어는 '법령'", "법령" in detect_categories("이차전지 특별법 개정안 발의"), True)
    check("'정부/정책'과 '법령'은 별개",
          "정부/정책" in detect_categories("이차전지 특별법 개정안 발의"), False)
    check("철강 공정어는 '산업'", "산업" in detect_categories("포스코 포항 3고로 개수 완료"), True)
    check("'실적' 단독은 시장/주가 태그 아님",
          "시장/주가" in detect_categories("포스코 2분기 실적 발표"), False)
    check("주가 특화어는 시장/주가 태그",
          "시장/주가" in detect_categories("포스코 주가 코스피 상한가"), True)
    check("본문 언급은 카테고리에 안 잡힌다(제목·요약만 스캔)",
          detect_categories("장인화 회장 호주 방문", "핵심광물 공급망 협력을 제안했다"), [])

    print("\n[8-2] 네이버 관련성 필터 (제목 기준, 그룹사 쏠림 방지)")
    check("포스코 무관 기사(수집 게이트)",
          bool(detect_group_companies("해병대 포병대대 포항 소외계층 무료급식 봉사")
               or POSCO_MENTION_RE.search("해병대 포병대대 포항 소외계층 무료급식 봉사")), False)
    check("포스코 언급 기사(수집 게이트)",
          bool(POSCO_MENTION_RE.search("포스코 포항제철소 3고로 개수 완료")), True)
    check("계열사만 언급된 기사(수집 게이트)",
          bool(detect_group_companies("삼척블루파워 석탄화력 준공")), True)
    check("그룹사 키워드: 제목에 포스코 없음 → 제외",
          _naver_item_relevant("영진전문대 수시모집 2309명 선발", "그룹사"), False)
    check("그룹사 키워드: 제목에 포스코DX 있음 → 통과",
          _naver_item_relevant("대덕SW고 학생, 포스코DX AI 유스챌린지 대상", "그룹사"), True)
    check("산업 키워드는 필터 안 함",
          _naver_item_relevant("LG엔솔 리튬 계약", "산업"), True)

    print("\n[8-2b] 정책브리핑(korea.kr) 수집")
    check("정책브리핑 URL 판정",
          bool(KOREA_KR_NEWS_RE.search("https://www.korea.kr/news/policyNewsView.do?newsId=1")), True)
    check("생활정보(gonggam) 는 정책브리핑 아님",
          bool(KOREA_KR_NEWS_RE.search("https://gonggam.korea.kr/newsView.do?newsId=1")), False)
    check("포스코 산업 관련 정책이면 수집",
          matches_keywords("내년 전기차 보조금 역대 최대 첨단산업 전력 용수 공급 1.9조", POLICY_RELEVANCE_KW), True)
    check("농업·복지 정책은 제외",
          matches_keywords("쌀 직불금 인상 농가 소득 안정", POLICY_RELEVANCE_KW), False)
    check("부처명 추출 (문의 총괄)",
          extract_ministry("", "문의 : <총괄>기후에너지환경부 기획재정담당관(044-201-6337)"), "기후에너지환경부")
    check("부처명 없으면 정책브리핑",
          extract_ministry("", "본문에 부처 언급 없음"), "정책브리핑")
    check("press_name 으로 정책브리핑 판정",
          is_policy_brief({"press_name": "대한민국 정책브리핑"}), True)
    # 정책 알림 키워드: 비면 전부 통과, 있으면 본문에 있는 것만
    _pk = ["전기요금", "탄소중립"]
    check("정책 키워드 비면 통과", (lambda kw: (not kw) or any(k in "아무 본문" for k in kw))([]), True)
    check("정책 키워드 매칭 시 통과",
          any(k in "전기요금 개편안 발표" for k in _pk), True)
    check("정책 키워드 불일치 시 제외",
          any(k in "쌀값 안정 대책" for k in _pk), False)

    print("\n[8-2c] 글로벌 통상환경 — 제목에 조치명 + 산업어일 때만")
    check("제목에 CBAM + 철강 → 통상환경",
          is_trade_topic("EU CBAM 시행에 철강업계 비상"), True)
    check("제목에 흑연 수출통제 + 이차전지 → 통상환경",
          is_trade_topic("중국 흑연 수출통제 확대…이차전지 타격"), True)
    check("조치명이 제목 아닌 요약에만 있으면 제외",
          is_trade_topic("포스코 노사, 타협점 찾아야", "철강업계는 반덤핑 관세와 중국산 저가재로 삼중고"), False)
    check("일반 무역 트렌드어(공급망 재편)는 통상환경 아님",
          is_trade_topic("종합상사 부활…공급망 재편 수혜"), False)
    check("조치명만 있고 산업어 없으면 아님", is_trade_topic("美, 對中 반도체 301조 관세"), False)
    check("산업어만 있고 조치명 없으면 아님", is_trade_topic("포스코 철강 신제품 출시"), False)
    check("통상환경 카테고리 태그로 판정",
          is_trade_article({"categories": ["글로벌 통상환경"]}), True)
    check("detect_categories: 제목에 조치명 있을 때만 태깅",
          "글로벌 통상환경" in detect_categories("美 무역확장법 232조 철강 관세 부과"), True)
    check("detect_categories: 요약에만 조치명이면 태깅 안 함",
          "글로벌 통상환경" in detect_categories("포스코 실적 회복세", "CBAM 대응 비용이 변수"), False)

    print("\n[8-2d] 배터리 생태계 기사 (포스코 미언급 허용)")
    check("전고체 배터리 개발 → 수집",
          is_battery_scope("전고체 배터리 상온 구동 성공…에너지밀도 2배"), True)
    check("황-리튬 배터리 연구 → 수집",
          is_battery_scope("황 원소 활용한 황-리튬 배터리, 2천배 빠른 충방전"), True)
    check("실리콘 음극재 신기술 → 수집",
          is_battery_scope("실리콘 음극재 팽창 잡는 바인더 개발"), True)
    check("테슬라 인도량(전방 수요) → 수집",
          is_battery_scope("테슬라 3분기 인도량 사상 최대"), True)
    check("ESS 시장 기사 → 수집",
          is_battery_scope("ESS 배터리 화재로 공장 가동 중단"), True)
    check("전기차 캐즘 → 수집", is_battery_scope("전기차 캐즘 장기화…배터리 수요 둔화"), True)
    check("리튬 가격 → 수집", is_battery_scope("탄산리튬 가격 반등"), True)
    check("소재 경쟁사(에코프로비엠) → 수집", is_battery_scope("에코프로비엠, 3분기 영업손실 확대"), True)
    check("소재 경쟁사(엘앤에프) → 수집", is_battery_scope("엘앤에프 유상증자 5천억 조달"), True)
    check("중국 음극재 1위(BTR) → 수집", is_battery_scope("BTR, 인도네시아 흑연 공장 착공"), True)
    check("에코프로비엠 → 양극재 카테고리", "양극재" in detect_categories("에코프로비엠 신규 수주"), True)
    check("BTR → 음극재 카테고리", "음극재" in detect_categories("BTR 증설 발표"), True)
    check("무관 기사는 여전히 제외", is_battery_scope("아파트 분양가 상승세"), False)
    check("반도체 기사도 제외", is_battery_scope("삼성전자 HBM 신제품 공개"), False)

    print("\n[8-3] 그룹사 균형 인터리브 (포스코퓨처엠 독점 방지)")
    _ri = lambda t: (RawItem(url_source=t, url_original=t, title=t, published_at=now_utc(),
                             source_type="naver_api"), False)
    _pool = ([_ri("포스코퓨처엠 소식 %d" % i) for i in range(10)]
             + [_ri("포스코DX 소식"), _ri("포스코이앤씨 소식")])
    _picked = interleave_by_group(_pool, 4)
    _titles = {p[0].title.split()[0] for p in _picked}
    check("4건 중 3개 이상 그룹사가 대표됨", len(_titles) >= 3, True)
    check("포스코DX 포함", any("포스코DX" in p[0].title for p in _picked), True)
    check("포스코이앤씨 포함", any("포스코이앤씨" in p[0].title for p in _picked), True)

    print("\n[9] LLM 응답 파싱 (PRD F4)")
    check("코드펜스 제거", _parse_json_object('```json\n{"a":1}\n```'), {"a": 1})
    check("설명 섞인 응답", _parse_json_object('결과입니다: {"a":2} 끝'), {"a": 2})
    check("파싱 불가", _parse_json_object("그냥 문장"), None)
    bad = _build_analysis({"summary": ["요약불가"]}, {})
    check("'요약불가' → 실패 처리", bad.ok, False)
    good = _build_analysis({
        "summary": ["문장1", "문장2", "문장3"], "perspective": "검토 필요",
        "keywords": ["양극재"], "group_companies": ["포스코퓨처엠", "없는회사"],
        "sentiment": "이상한값", "swot": {"s": {"score": 200, "text": "x"}},
    }, {})
    check("정규 목록 밖 그룹사 폐기", good.group_companies, ["포스코퓨처엠"])
    check("잘못된 감성값 → 중립", good.sentiment, "중립")
    check("SWOT 점수 0~100 클램프", good.swot["s"]["score"], 100)
    check("누락 SWOT 항목 기본값", good.swot["t"], {"score": 0, "text": "해당 없음"})

    print("\n[10] 다중 선택 필터 결합 (PRD F6.1a)")
    cards = [
        {"group_companies": ["포스코퓨처엠"], "categories": ["배터리·이차전지"], "press_name": "FETV"},
        {"group_companies": ["포스코홀딩스"], "categories": ["시장/주가"], "press_name": "한국경제"},
        {"group_companies": ["포스코퓨처엠"], "categories": ["시장/주가"], "press_name": "한국경제"},
    ]
    check("같은 그룹 안 OR", len(apply_filters(cards, ["포스코퓨처엠", "포스코홀딩스"], [], [])), 3)
    check("다른 그룹 사이 AND", len(apply_filters(cards, ["포스코퓨처엠"], ["시장/주가"], [])), 1)
    check("선택 없으면 미적용", len(apply_filters(cards, [], [], [])), 3)
    check("언론사 필터", len(apply_filters(cards, [], [], ["한국경제"])), 2)
    # '포스코'와 '포스코퓨처엠'은 부분일치가 아닌 별개 키로 취급한다
    check("상위어는 하위 계열사에 매칭 안 됨",
          len(apply_filters([{"group_companies": ["포스코"], "categories": [], "press_name": "x"}],
                            ["포스코퓨처엠"], [], [])), 0)

    print("\n[10-1] 그룹사 태그 본문 검증 (LLM 과잉 태깅 방지)")
    check("본문에 회사명 있으면 유지",
          [g for g in ["포스코퓨처엠"] if g in set(detect_group_companies("포스코퓨처엠이 양극재를 증설한다"))],
          ["포스코퓨처엠"])
    check("본문에 회사명 없으면 탈락",
          [g for g in ["포스코퓨처엠"] if g in set(detect_group_companies("청주시가 이차전지 국책사업을 유치했다"))],
          [])
    check("키워드에만 있는 계열사도 태그로 추가",
          normalize_group_list(detect_group_companies("에너지 스타트업 투자상담 포스코모빌리티솔루션")),
          ["포스코모빌리티솔루션"])

    print("\n[11] 코사인 유사도 (PRD F2.2 4단계)")
    check("동일 벡터", round(cosine([1, 0, 1], [1, 0, 1]), 6), 1.0)
    check("직교 벡터", cosine([1, 0], [0, 1]), 0.0)
    check("길이 불일치 방어", cosine([1, 2], [1, 2, 3]), 0.0)
    check("None 방어", cosine(None, [1]), 0.0)

    print("\n[11-1] 대표 승격 — 언론사 tier 조회 (일시 DB)")
    import shutil
    import tempfile
    _dbdir = tempfile.mkdtemp()
    _tmp = SqliteStorage(os.path.join(_dbdir, "selftest.db"))
    _tmp.init_schema()
    _tmp.upsert_press("major.co.kr", "주요지", 1, "approved")
    _pid = (_tmp.press_by_domain("major.co.kr") or {}).get("id")
    check("등록된 언론사 tier", _tmp.press_tier_by_id(_pid), 1)
    check("id 없으면 기타(3)", _tmp.press_tier_by_id(None), 3)
    check("모르는 id 는 기타(3)", _tmp.press_tier_by_id("nope"), 3)
    check("상위 tier 로만 승격", 1 < _tmp.press_tier_by_id(None), True)   # tier1 < 3
    check("동급이면 승격 안 함", 3 < _tmp.press_tier_by_id(None), False)  # tier3 !< 3

    print("\n[11-2] 오래된 제목 임베딩 정리 (purge_stale_embeddings)")
    _old = iso(now_utc() - timedelta(hours=100))
    _new = iso(now_utc() - timedelta(hours=1))
    for _eid, _pub in (("emb-old", _old), ("emb-new", _new)):
        _tmp._exec(
            "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
            " collected_at, source_type, title_embedding) values (?,?,?,?,?,?,?,?,?)",
            (_eid, f"http://x/{_eid}", f"http://x/{_eid}", f"http://x/{_eid}", "제목",
             _pub, _pub, "search", "[0.1,0.2]"))
    check("48시간 밖 임베딩만 비움 → 1건", _tmp.purge_stale_embeddings(48), 1)
    check("오래된 행 임베딩 제거됨",
          _tmp._one("select title_embedding from articles where id='emb-old'")["title_embedding"], None)
    check("최근 행 임베딩 보존됨",
          _tmp._one("select title_embedding from articles where id='emb-new'")["title_embedding"] is not None, True)
    check("두 번째 호출은 대상 없음 → 0건", _tmp.purge_stale_embeddings(48), 0)

    shutil.rmtree(_dbdir, ignore_errors=True)

    print("\n[12] .env 인라인 주석 처리")
    check("주석 제거", _clean("60          # 폴링 주기"), "60")
    check("따옴표 값 보존", _clean('"a # b"'), "a # b")
    check("None 방어", _clean(None), "")

    print("\n[13] HTML 이스케이프 (PRD F7.2)")
    check("꺾쇠·앰퍼샌드", esc("<b>A&B</b>"), "&lt;b&gt;A&amp;B&lt;/b&gt;")

    print("\n[13-1] 텔레그램 챗봇 (PRD F7.4)")
    check("카드 한 줄 포맷 (중요도 이모지)",
          _card_line({"importance_score": 85, "title": "테스트<&>", "url": "http://a.com",
                      "published_at": "2026-09-02T01:00:00Z"}).startswith("🔴"),
          True)
    check("카드 한 줄: HTML 이스케이프",
          "테스트&lt;&amp;&gt;" in _card_line({"importance_score": 10, "title": "테스트<&>",
                                              "url": "http://a.com", "published_at": ""}),
          True)
    _bot_chat_calls.clear()
    check("자연어 질문 rate limit: 30회까지 허용",
          all(_bot_rate_ok("c1") for _ in range(30)), True)
    check("자연어 질문 rate limit: 31회째 차단", _bot_rate_ok("c1"), False)
    check("다른 chat 은 별도 카운트", _bot_rate_ok("c2"), True)
    _bot_chat_calls.clear()

    print("\n[13-2] 알림 메시지 포맷 (PRD F7)")
    msg = format_message({"importance_score": 60, "title": "포스코퓨처엠 주가 하락",
                          "press_name": "중앙이코노미뉴스", "author": "조용우",
                          "group_companies": '["포스코", "포스코퓨처엠"]',
                          "summary_text": "주가가 내렸다."})
    check("대표 그룹사는 계열사 우선('포스코' 아님)", msg.splitlines()[0].startswith("🟠 [포스코퓨처엠]"), True)
    check("머리표에 언론사+기자", "[중앙이코노미뉴스, 조용우]" in msg, True)
    msg2 = format_message({"importance_score": 60, "title": "포스코퓨처엠 신제품", "group_companies": "[]",
                           "summary_text": "포스코퓨처엠이 발표했다."})
    check("그룹사 비면 제목·요약에서 폴백", msg2.splitlines()[0].startswith("🟠 [포스코퓨처엠]"), True)
    # 우선 알림은 '항상 발송 키워드' 매칭으로만 판정한다 (포스코퓨처엠 자동 특례 폐지)
    check("항상 발송 키워드 매칭 → 우선", _kw_hit("포스코퓨처엠 양극재 증설", ["포스코퓨처엠"]), True)
    check("키워드 목록 비면 조건 없음(True)", _kw_hit("아무 본문", []), True)
    check("키워드 불일치 → 우선 아님", _kw_hit("삼성전자 실적", ["포스코퓨처엠"]), False)
    # 정책 알림: OR 키워드 AND 필수 공통 키워드
    check("정책: OR 매칭 + 필수 매칭 → 발송",
          _kw_hit("산업용 전기요금 인하", ["전기요금"]) and _kw_hit("산업용 전기요금 인하", ["산업"]), True)
    check("정책: OR 매칭 but 필수 불일치 → 제외",
          _kw_hit("가정용 전기요금 인하", ["전기요금"]) and _kw_hit("가정용 전기요금 인하", ["산업"]), False)

    print("\n[13-2a] 다이제스트 분리 — 우선 기사는 묶지 않는다")
    _p, _n, _d = split_for_digest([{"priority": 1}, {"priority": 0}, {"priority": 0}, {"priority": 0}])
    check("우선 기사는 개별 발송으로 분리", len(_p), 1)
    check("남은 일반 기사 3건 → 묶음", (len(_n), _d), (3, True))
    _p, _n, _d = split_for_digest([{"priority": 1}, {"priority": 1}, {"priority": 1}, {"priority": 0}])
    check("우선 3건이어도 묶지 않음", (len(_p), _d), (3, False))
    _p, _n, _d = split_for_digest([{"priority": 0}, {"priority": 0}])
    check("일반 2건은 묶음 기준 미달 → 개별 발송", _d, False)
    check("빈 목록 방어", split_for_digest([]), ([], [], False))

    print("\n[13-3] 마스터 비밀번호 (pbkdf2)")
    _h = hash_password("s3cret!")
    check("정상 비번 검증", verify_password("s3cret!", _h), True)
    check("틀린 비번 거부", verify_password("nope", _h), False)
    check("손상된 해시 거부", verify_password("s3cret!", "garbage"), False)
    check("빈 문자열 거부", verify_password("", _h), False)
    _kw = ["수소환원제철", "장인화"]
    check("키워드 본문 매칭", any(k in "포스코가 장인화 회장 주재로 회의" for k in _kw), True)
    check("키워드 미포함", any(k in "삼성전자 실적 발표" for k in _kw), False)

    print("\n[14] 수동 URL 등록 (PRD F8)")
    check("제목 추출 + 사이트명 꼬리 제거",
          extract_title('<meta property="og:title" content="포스코퓨처엠 양극재 증설 - 한국경제">'),
          "포스코퓨처엠 양극재 증설")
    check("<title> 폴백", extract_title("<title>포스코이앤씨 신규 수주 소식</title>"),
          "포스코이앤씨 신규 수주 소식")
    check("발행일 메타 파싱",
          extract_published('<meta property="article:published_time" content="2026-09-01T08:30:00Z">') is not None,
          True)
    check("발행일 없음 → None", extract_published("<html>본문</html>"), None)

    print()
    if failures:
        print(f"실패 {len(failures)}건:\n" + "\n".join(failures))
        return 1
    print("전체 통과.")
    return 0


USAGE = """사용법: python backend/main.py <명령>

  initdb     스키마 생성 + 시드 데이터 입력 (최초 1회)
  fixpress   언론사명 정리 (도메인으로 저장된 매체명을 정식 이름으로 교체)
  fixauthors 깨진 기자명 복구 (JSON-LD 유니코드 이스케이프 — 일회성)
  fixgroups  LLM 과잉 태깅된 그룹사 재검증 (필터 정확도 — 일회성)
  regroup    원문 리드 기준으로 그룹사 태그 재판정 (오탐 정정 — 일회성, LLM 비용 0)
  fixcategories 카테고리를 제목+요약 기준으로 재태깅 (필터 변별력 — 일회성)
  fixofftopic 포스코 언급 없는 기존 기사를 원문 재확인 후 보관 (일회성)
  reanalyze  분석이 끊긴 기사를 원문에서 다시 받아 재분석 (일회성)
  reswot     SWOT 가 전부 0인 기사를 재분석 (일회성)
  fixlinks   홈으로 잘못 연결된 카드 링크(url_canonical) 보정 (일회성)
  fixdates   미래로 저장된 발행시각 보정 (타임존 오파싱 복구 — 일회성)
  once [N]   파이프라인 1회 실행 (N 을 주면 LLM 호출을 N건으로 제한 — 검증용)
  serve      API + 프론트엔드 서버만 실행
  run        서버 + 수집 루프 + 텔레그램 챗봇 (운영 모드)
  quotes     시세만 1회 갱신
  notify     대기 중인 텔레그램 알림만 발송
  chatid     텔레그램 chat_id 확인 (봇에게 메시지를 한 번 보낸 뒤 실행)
  sendtest   텔레그램 시험 메시지 1건 발송 (연결 확인용)
  selftest   내장 검증 (DB · 네트워크 · API 키 불필요)
"""


def main(argv: Sequence[str]) -> int:
    command = argv[1] if len(argv) > 1 else "help"

    if command in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    if command == "selftest":
        return cmd_selftest()

    cfg = load_config()
    storage = make_storage(cfg)
    ctx = Context(cfg=cfg, storage=storage, http=HttpClient())
    warn_if_bad_chat_id(cfg.telegram_chat_id)

    if command == "initdb":
        cmd_initdb(ctx)
    elif command == "fixpress":
        cmd_fixpress(ctx)
    elif command == "fixauthors":
        cmd_fixauthors(ctx)
    elif command == "fixgroups":
        cmd_fixgroups(ctx)
    elif command == "regroup":
        cmd_regroup(ctx)
    elif command == "fixcategories":
        cmd_fixcategories(ctx)
    elif command == "fixofftopic":
        cmd_fixofftopic(ctx)
    elif command == "reanalyze":
        cmd_reanalyze(ctx)
    elif command == "reswot":
        cmd_reswot(ctx)
    elif command == "fixlinks":
        cmd_fixlinks(ctx)
    elif command == "fixdates":
        cmd_fixdates(ctx)
    elif command == "once":
        max_llm = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
        cmd_once(ctx, max_llm)
    elif command == "quotes":
        log.info("시세 %d건 갱신", refresh_quotes(ctx))
    elif command == "notify":
        log.info("텔레그램 발송 %d건", send_notifications(ctx))
    elif command == "chatid":
        cmd_chatid(ctx)
    elif command == "sendtest":
        cmd_sendtest(ctx)
    elif command == "serve":
        cmd_serve(ctx, with_pipeline=False)
    elif command == "run":
        cmd_serve(ctx, with_pipeline=True)
    else:
        print(f"알 수 없는 명령: {command}\n")
        print(USAGE)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
