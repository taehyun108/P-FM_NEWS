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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:   # 대외협력(대관) 모듈 — 없거나 깨져도 기존 수집·API 는 그대로 동작한다
    import external_affairs as ea_mod
except Exception:   # pragma: no cover
    ea_mod = None

try:
    import ea_crawl as ea_crawl_mod
except Exception:   # pragma: no cover
    ea_crawl_mod = None

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
    # 카카오톡 '나에게 보내기' (선택 · 기본 비활성) — 텔레그램과 병행. refresh_token 은 run_state 에 저장
    #   text 템플릿 200자 한도 때문에 텔레그램 대비 정보량이 적어 기본은 꺼 둔다.
    #   KAKAO_ENABLED=true 로 명시해야 켜진다. 코드·라우트·selftest 는 그대로 유지.
    kakao_feature_enabled: bool
    kakao_rest_api_key: str
    kakao_client_secret: str
    kakao_redirect_uri: str
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
    article_retention_days: int    # 핵심 기사 보존일 (Supabase 무료 500MB 한도 대비)
    api_host: str
    api_port: int
    master_password: str          # 마스터 패널 초기 비밀번호 (변경 시 DB 해시가 우선)
    web_password: str             # 사이트 접속 비밀번호. 비우면 잠금 없음(로컬 개발)
    tz_offset_hours: int          # 운영 기준 시간대 UTC 오프셋 (한국 = 9)
    # 야간 억제 — 모두 '운영 기준 시간대'(APP_TZ_OFFSET, 기본 KST) 기준 시각이다.
    night_start_hour: int         # 이 시각부터 (기본 23)
    night_end_hour: int           # 이 시각 전까지 억제 (기본 7)
    night_min_score: int          # 야간엔 이 점수 이상(또는 우선 기사)만 발송. 101 = 전면 차단
    # 주간 레포트 (월요일 아침 이메일)
    weekly_enabled: bool
    weekly_to: list[str]          # 수신자 이메일 (쉼표로 여러 명)
    weekly_hour: int              # 월요일 발송 시각 (0~23, 서버 로컬시간)
    smtp_host: str
    smtp_port: int
    smtp_user: str                # 발신 Gmail 주소
    smtp_app_password: str        # Gmail 앱 비밀번호 16자리

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def kakao_configured(self) -> bool:
        """카카오 '나에게 보내기' 기능이 활성이고 앱 키까지 갖춰졌는가.

        기본은 비활성이다. KAKAO_ENABLED=true 로 켜고 + REST 키·redirect 가 있어야
        하며, 실제 발송은 refresh_token(run_state)까지 있어야 한다(kakao_enabled_now).
        """
        return bool(self.kakao_feature_enabled
                    and self.kakao_rest_api_key and self.kakao_redirect_uri)

    @property
    def smtp_configured(self) -> bool:
        """SMTP 자격 증명이 갖춰졌는가. 수신자는 .env 또는 마스터 패널에서 따로 정한다."""
        return bool(self.smtp_user and self.smtp_app_password)

    @property
    def naver_enabled(self) -> bool:
        return bool(self.naver_client_id and self.naver_client_secret)


def _jwt_role(token: str) -> str:
    """Supabase 키(JWT)의 role 클레임을 읽는다. 실패하면 빈 문자열."""
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role", "")
    except Exception:
        return ""


def _pick_supabase_service_key(k1: str, k2: str) -> str:
    """두 Supabase 키 중 role=service_role 인 것을 고른다(변수를 바꿔 넣어도 동작).
    role 을 못 읽으면 첫 번째(SUPABASE_SERVICE_ROLE_KEY) 를 그대로 쓴다."""
    for k in (k1, k2):
        if k and _jwt_role(k) == "service_role":
            return k
    return k1 or k2


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
    # 서비스 롤 키를 쓴다(RLS 우회). 두 키 변수를 서로 바꿔 넣는 실수가 잦아
    # JWT 의 role 클레임을 보고 실제 service_role 키를 고른다.
    supabase_key = _pick_supabase_service_key(
        get_env("SUPABASE_SERVICE_ROLE_KEY"), get_env("SUPABASE_ANON_KEY"))
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

    # 야간 억제·주간 레포트·대외협력 스케줄이 쓰는 기준 시간대를 여기서 확정한다.
    # 클라우드 서버는 UTC 로 도는 경우가 대부분이라 기본값을 KST(+9)로 둔다.
    tz_off = get_env_int("APP_TZ_OFFSET", 9, -12, 14)
    set_app_tz(tz_off)

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
        # 기본 비활성. 텔레그램 대비 정보량이 적어(text 200자) 명시적으로 켤 때만 동작.
        kakao_feature_enabled=get_env("KAKAO_ENABLED", "").strip().lower() in ("1", "true", "yes"),
        kakao_rest_api_key=get_env("KAKAO_REST_API_KEY"),
        kakao_client_secret=get_env("KAKAO_CLIENT_SECRET"),
        kakao_redirect_uri=get_env("KAKAO_REDIRECT_URI"),
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
        llm_per_run=get_env_int("LLM_PER_RUN", 20, 1),
        # 알림 대상이었거나·중요도 높거나·그룹사 태그가 있는 '핵심' 기사의 보존일.
        # 그 외 잡음 기사는 RETENTION_SOFT_DAYS(90일)만 보관한다. 약 550일 = 18개월.
        article_retention_days=get_env_int("ARTICLE_RETENTION_DAYS", 550, 30),
        # 컨테이너·클라우드에서는 0.0.0.0 으로 열어야 밖에서 붙는다. PORT 가 주입돼
        # 있으면(= 클라우드) 기본값을 0.0.0.0 으로 바꾼다. 로컬은 그대로 127.0.0.1.
        api_host=get_env("API_HOST", "0.0.0.0" if get_env("PORT") else "127.0.0.1"),
        # 클라우드(Render·Koyeb·Fly·Railway 등)는 리슨 포트를 PORT 로 주입한다.
        # PORT 가 있으면 그것을 우선한다 — 없으면 기존 API_PORT.
        api_port=get_env_int("PORT", 0, 0, 65535) or get_env_int("API_PORT", 8000, 1, 65535),
        master_password=get_env("MASTER_PASSWORD"),
        web_password=get_env("WEB_PASSWORD"),
        tz_offset_hours=tz_off,
        # 야간 억제 창 — APP_TZ_OFFSET(기본 KST) 기준 시각. 0~23.
        night_start_hour=get_env_int("NIGHT_START_HOUR", 23, 0, 23),
        night_end_hour=get_env_int("NIGHT_END_HOUR", 7, 0, 23),
        night_min_score=get_env_int("NIGHT_MIN_SCORE", 80, 0, 101),
        weekly_enabled=get_env("WEEKLY_REPORT_ENABLED", "").strip().lower() in ("1", "true", "yes"),
        weekly_to=[e.strip() for e in get_env("WEEKLY_REPORT_TO", "").replace(";", ",").split(",")
                   if e.strip()],
        weekly_hour=get_env_int("WEEKLY_REPORT_HOUR", 7, 0, 23),
        smtp_host=get_env("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=get_env_int("SMTP_PORT", 587, 1, 65535),
        smtp_user=get_env("SMTP_USER"),
        smtp_app_password=get_env("SMTP_APP_PASSWORD").replace(" ", ""),
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
APP_TZ = KST   # 운영 기준 시간대. load_config() 가 APP_TZ_OFFSET 으로 덮어쓴다.


def set_app_tz(offset_hours: float) -> None:
    """운영 기준 시간대를 UTC 오프셋(시)으로 지정한다. 한국은 서머타임이 없어
    고정 오프셋으로 충분하다(zoneinfo·tzdata 의존성을 만들지 않는다)."""
    global APP_TZ
    APP_TZ = timezone(timedelta(hours=max(-12.0, min(14.0, offset_hours))))


def now_local() -> datetime:
    """운영 기준 지역시간(기본 KST = UTC+9).

    야간 억제(23–07시)·주간 레포트 발송 시각·대외협력 수집 시각은 모두
    '한국 시간' 기준이어야 한다. 클라우드 서버는 대개 UTC 로 도는데
    datetime.now() 를 그대로 쓰면 9시간 어긋나 야간 억제가 대낮에 걸린다.
    """
    return datetime.now(APP_TZ)


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
        bodies = {r["article_id"] for r in
                  self._t("article_bodies").select("article_id").execute().data or []}
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
                        "importance_score,published_at,group_companies,source_type,"
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
    "ppss.kr": ("ㅍㅍㅅㅅ", 3), "ktv.go.kr": ("KTV 국민방송", 2),
    "kpinews.kr": ("KPI뉴스", 3), "kjdaily.com": ("광주매일신문", 3),
    "kgnews.co.kr": ("경기신문", 3), "gosiweek.com": ("피앤피뉴스", 3),
    "unn.net": ("한국대학신문", 3), "ttlnews.com": ("퍼블릭뉴스통신", 3),
    "the-stock.kr": ("더스탁", 3), "apnews.kr": ("AP신문", 3),
}

# 다음·네이버 뉴스 래퍼 도메인 — 그 자체가 언론사가 아니다.
# 이런 페이지의 og:site_name 은 'Daum | 뉴스1' 처럼 '래퍼 | 실제출처' 형태라
# 마지막 구분자 뒤(실제 출처)를 매체명으로 쓴다.
NEWS_AGGREGATORS = frozenset({"daum.net", "naver.com"})

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

# 별칭 소문자 사전계산 — detect_group_companies·score_article 이 호출마다
# `[a.lower() for a in aliases]` 를 다시 만들던 것을 제거한다(파이프라인·API 공통 경로).
_GROUP_ALIASES_LOWER: dict[str, list[str]] = {
    canonical: [a.lower() for a in aliases] for canonical, aliases in GROUP_COMPANIES.items()
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
             "로봇", "협동로봇", "휴머노이드", "스마트팩토리"],
    # 정부/정책 = 한국 정부의 국내 산업·에너지 정책·입법·행정. (외국 통상조치는 '글로벌 통상환경')
    #   detect_categories 가 '핵심어가 제목에 있을 때만' 태깅한다(TITLE_ANCHORED_CATEGORIES).
    #   '보조금'·'정부 지원'·'관세'·'IRA' 처럼 관용적으로 쓰이는 낱말은 넣지 않는다 —
    #   전기차 판매 기사·실적 기사까지 정책으로 태깅된다(보조금 한 낱말로 7/14건 오태깅).
    "정부/정책": ["산업부", "환경부", "기재부", "국토부", "중기부", "과기정통부", "공정위",
                  "국정감사", "예비타당성", "예비타당성조사", "예타 면제", "국정과제",
                  "부처 합동", "범부처", "특화단지", "국가첨단전략산업", "소부장 특별법",
                  "규제 완화", "규제 혁신", "규제 샌드박스", "세액공제", "국비 지원",
                  "추가경정예산", "추경 편성", "예산안", "육성 방안", "종합대책",
                  "개정안", "제정안", "시행령", "특별법", "입법예고", "본회의 통과",
                  "국회 통과", "법안 발의", "중대재해처벌법", "노란봉투법"],
    # 미국·유럽·중국 등 주요국의 명시적 통상 조치. 정의는 TRADE_MEASURE_KW 와 맞춘다.
    # detect_categories 가 '제목에 조치명' 조건을 한 번 더 건다.
    "글로벌 통상환경": TRADE_MEASURE_KW,
    "시장/주가": ["주가", "증권", "코스피", "코스닥", "목표주가", "시황", "상한가", "하한가",
                  "거래량", "거래대금", "시가총액", "PER", "PBR", "공매도", "외국인 순매수",
                  "기관 순매수", "배당"],
}

POLICY_CATEGORY = "정부/정책"
# 이 카테고리들은 '핵심어가 제목에 있을 때만' 태깅한다. 요약·본문에 스친 언급은
# 부차 주제라, 넣으면 필터가 변별력을 잃는다(통상·정책 모두 실제로 그랬다).
TITLE_ANCHORED_CATEGORIES = (TRADE_CATEGORY, POLICY_CATEGORY)

# 카테고리 규칙 소문자 사전계산 — detect_categories 가 호출마다 각 단어를
# 소문자로 바꾸던 것을 제거한다(API 필터 스캔에서 행 수 × 카테고리 수만큼 반복됐다).
_CATEGORY_RULES_LOWER: dict[str, list[str]] = {
    name: [w.lower() for w in words] for name, words in CATEGORY_RULES.items()
}

# 중요도 가중치 (PRD F3.2)
SCORE_FUTUREM_TITLE = 50
SCORE_FUTUREM_BODY = 40
SCORE_GROUP = 25
SCORE_BATTERY_TITLE = 25   # 배터리 생태계(소재·셀·전기차·ESS·원료) 키워드가 제목에
SCORE_BATTERY_BODY = 12    # 본문에만
SCORE_TRADE = 15           # 글로벌 통상환경 신호
SCORE_POLICY = 20
SCORE_MAJOR_PRESS = 10
SCORE_MARKET_PENALTY = -15
SCORE_PEOPLE_NEWS = 12     # 인사·부고 — 웹 전용(임계값 미만), 알림 안 나감

POLICY_KEYWORDS = ["정책", "규제", "법안", "수사", "사고", "화재", "제재", "과징금",
                   "국회", "산업부", "환경부", "보조금", "특화단지", "인허가", "감사", "고발"]
MARKET_ONLY_KEYWORDS = ["목표주가", "투자의견", "코스피", "시황", "주가 전망", "증권가"]
# score_article 이 호출마다 소문자 변환하던 것을 사전계산한다.
_POLICY_KEYWORDS_LOWER = [w.lower() for w in POLICY_KEYWORDS]
_MARKET_ONLY_KEYWORDS_LOWER = [w.lower() for w in MARKET_ONLY_KEYWORDS]


# =====================================================================
# 5. URL 정규화 (PRD F2.1 / news-dedup-normalize)
#    (A) 게이트 이전 — 네트워크 금지. (B) 게이트 통과분 — HTTP 허용.
# =====================================================================

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "igshid", "spm", "ref", "from", "cid", "sid", "oid", "aid"}

# 같은 기사를 여러 경로로 서비스하는 사이트 — 기사 ID 파라미터 하나로 접는다.
# (도메인: (표준 경로, ID 파라미터)).  예) thebell 은 /free/content/ArticleView.asp
# (로그인/유료 안내 페이지)와 /front/newsview.asp(실제 기사)를 같은 key 로 서비스한다.
SITE_CANONICAL: dict[str, tuple[str, str]] = {
    "thebell.co.kr": ("/front/newsview.asp", "key"),
}


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

    # 사이트별 표준화 — 같은 기사 ID 면 경로·나머지 쿼리와 무관하게 한 URL 로 접는다
    rule = SITE_CANONICAL.get(domain_of(f"{scheme}://{host}"))
    if rule:
        canon_path, id_key = rule
        id_val = next((v for k, v in query if k.lower() == id_key.lower()), "")
        if id_val:
            reg = domain_of(f"{scheme}://{host}")
            return urlunsplit(("https", f"www.{reg}", canon_path,
                               urlencode([(id_key, id_val)]), ""))

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


def _kw_first_hit(text: str, keywords: Sequence[str]) -> str | None:
    """본문에 처음 걸린 키워드를 돌려준다. 없으면 None. (발송 이유 표시용)"""
    for k in keywords:
        if k and k in text:
            return k
    return None


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


# ── 인사·부고 (포스코 관점 없이 공지 원문만 요약) ───────────────────────
PEOPLE_NEWS_CATEGORY = "인사·부고"
_OBIT_TITLE_RE = re.compile(r"^\s*(?:\[|【)\s*(?:부고|訃告|부음)\s*(?:\]|】)")
_OBIT_URL_RE = re.compile(r"obituary", re.I)
_PERSONNEL_TITLE_RE = re.compile(r"^\s*(?:\[|【)\s*(?:인사|人事|승진|신임|취임|프로필)\s*(?:\]|】)")
# 말머리가 없는 정부부처·기관 인사 공지. '인사발령'·'보직인사'는 사실상 인사 공지에서만
# 쓰는 표현이라 제목에 있으면 personnel 로 본다. (motir.go.kr 등 부처 사이트 대응)
_GOV_PERSONNEL_RE = re.compile(r"인사\s*발령|보직\s*인사|전보\s*발령|승진\s*임용")


def people_news_kind(url: str, title: str) -> str:
    """인사·부고 기사면 종류('obituary' | 'personnel'), 아니면 ''.

    제목 말머리([부고]·[인사]·[승진]…)나 연합뉴스 부고 섹션 URL 로 판정한다.
    말머리가 없어도 '인사발령'·'보직인사' 같은 정부부처 인사 공지 표현이 있으면 personnel.
    '동정'(장관 활동 등)은 포함하지 않는다 — 일반 기사로 흐르게 둔다.
    """
    t = title or ""
    if _OBIT_TITLE_RE.search(t) or _OBIT_URL_RE.search(url or ""):
        return "obituary"
    if _PERSONNEL_TITLE_RE.search(t) or _GOV_PERSONNEL_RE.search(t):
        return "personnel"
    return ""


_NOTICE_HEAD_RE = re.compile(
    r"^.*?(?:구독\s*구독중\s*이전\s*다음|이미지\s*확대.*?자료사진\s*\])\s*")
_NOTICE_TAIL_RE = re.compile(
    r"\s*(?:\(\S+=\s*연합뉴스\)|※\s*부고\s*요청|&lt;저작권자|<저작권자|제보는\s*카카오톡"
    r"|무단\s*전재|\d{4}/\d{2}/\d{2}\s*\d{2}:\d{2}\s*송고).*$")


def format_people_notice(body: str, kind: str) -> str:
    """인사·부고 공지에서 핵심 블록만 남긴다. LLM 을 쓰지 않는다.

    부고:  '▲ 별세·상주 … = 빈소, 발인, 장지 ☎ 전화'
    인사:  '◇ 부서 ▲ 직책 이름 …'
    """
    text = re.sub(r"\s+", " ", (body or "").strip())
    text = _NOTICE_HEAD_RE.sub("", text, count=1)
    text = _NOTICE_TAIL_RE.sub("", text).strip()
    if kind == "obituary":
        m = re.search(r"▲.*?☎[\s\d\-()]+", text) or re.search(r"▲.*", text)
        if m:
            text = m.group(0).strip()
    else:
        m = re.search(r"[◇▲■].*", text)
        if m:
            text = m.group(0).strip()
    return text[:800]


# ── 인사·부고 LLM 구조화 (사용자 지정 2026-09-09) ──────────────────────
# 소스 기사(연합·뉴시스 인사 RSS)는 이름·소속만 나열한 덤프라 읽기 어렵다.
# LLM 으로 사람별 항목(소속 국·과 / 직급 / 승진·전보 / 고시·회차 / 이력 / 업무 / 학력)을
# 뽑되, **기사에 실제로 적힌 것만** 채운다. 외부 검색으로 보강하지 않는다.
PEOPLE_NOTICE_SYSTEM = (
    "당신은 정부·공공기관 인사·부고 공지를 정리하는 편집자다. "
    "주어진 기사 본문에 실제로 적힌 사실만 사용하고, 없는 항목은 빈 값으로 둔다. "
    "고시 회차·이력·학력 등은 기사에 없으면 절대 추측하지 않는다. JSON 으로만 답한다."
)
PEOPLE_NOTICE_PROMPT = """[구분] {kind}
[제목] {title}
[언론사] {press}
[본문]
{body}

위 공지를 사람별로 정리해 JSON 하나로만 답하라.

인사(승진·전보·임용·취임 등)이면 각 사람마다:
  name       이름
  org        소속 (부처 + 국/과, 기사에 있는 만큼)
  position   직급/직위 (예: 부이사관, 국장)
  change     인사 종류 (승진 | 전보 | 신규임용 | 취임 | 연임 | 퇴직 등)
  exam       임용 경로 (예: "행정고시 45회", "기술고시 30회") — 없으면 ""
  career     주요 이력 배열 (역임 보직과 기간, 예: "기재부 재정성과평가과장(23.5~25.5)") — 없으면 []
  duty       담당·역점 업무 — 없으면 ""
  education  출신 학교 — 없으면 ""
→ {{"kind":"personnel","people":[{{"name":"...","org":"...","position":"...","change":"...","exam":"","career":[],"duty":"","education":""}}]}}

부고면 각 대상마다:
  subject   별세자 (직함 포함)
  relation  부고 주체와의 관계 (예: "OOO 부장 부친상") — 없으면 ""
  mourners  상주 — 없으면 ""
  wake      빈소 — 없으면 ""
  funeral   발인 일시 — 없으면 ""
  burial    장지 — 없으면 ""
  contact   연락처 — 없으면 ""
→ {{"kind":"obituary","deaths":[{{"subject":"...","relation":"","mourners":"","wake":"","funeral":"","burial":"","contact":""}}]}}

규칙:
- 기사에 없는 항목은 빈 문자열/빈 배열로 둔다. 지어내지 말 것.
- 사람이 여러 명이면 모두 담되, 최대 20명.
- 본문이 이름 나열뿐이면 name·org·position·change 만 채우면 된다."""


def _pp(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def format_people_llm(parsed: dict, kind: str) -> str:
    """people_notice() 의 JSON 을 카드·텔레그램에 넣을 사람별 텍스트 블록으로 만든다."""
    if not isinstance(parsed, dict):
        return ""
    blocks: list[str] = []
    if kind == "obituary" or parsed.get("kind") == "obituary":
        for d in (parsed.get("deaths") or [])[:20]:
            if not isinstance(d, dict):
                continue
            head = _pp(d.get("subject")) or "부고"
            rel = _pp(d.get("relation"))
            lines = [f"ㆍ{head}" + (f" / {rel}" if rel else "")]
            if _pp(d.get("mourners")):
                lines.append(f"   · 상주: {_pp(d.get('mourners'))}")
            if _pp(d.get("wake")):
                lines.append(f"   · 빈소: {_pp(d.get('wake'))}")
            fb = " / ".join(x for x in (
                f"발인 {_pp(d.get('funeral'))}" if _pp(d.get("funeral")) else "",
                f"장지 {_pp(d.get('burial'))}" if _pp(d.get("burial")) else "") if x)
            if fb:
                lines.append(f"   · {fb}")
            if _pp(d.get("contact")):
                lines.append(f"   · ☎ {_pp(d.get('contact'))}")
            blocks.append("\n".join(lines))
    else:
        for p in (parsed.get("people") or [])[:20]:
            if not isinstance(p, dict):
                continue
            name = _pp(p.get("name"))
            if not name:
                continue
            head_bits = [_pp(p.get("org")), name,
                         " ".join(x for x in (_pp(p.get("position")), _pp(p.get("change"))) if x)]
            lines = ["ㆍ" + " ".join(b for b in head_bits if b).strip()]
            if _pp(p.get("exam")):
                lines.append(f"   · {_pp(p.get('exam'))}")
            career = [_pp(c) for c in (p.get("career") or []) if _pp(c)]
            if career:
                joined = " ".join(f"{i}) {c}" for i, c in enumerate(career, 1))
                lines.append(f"   · 이력: {joined}")
            if _pp(p.get("duty")):
                lines.append(f"   · 업무: {_pp(p.get('duty'))}")
            if _pp(p.get("education")):
                lines.append(f"   · 학력: {_pp(p.get('education'))}")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip()


def people_summary(ctx: Context, kind: str, title: str, press: str, body: str,
                   use_llm: bool, html: str = "") -> tuple[str, str, dict]:
    """인사·부고 요약 텍스트를 만든다. 반환: (요약, 사용 모델, 토큰 usage).

    use_llm 이면 구조화를 시도하고, 실패하거나 use_llm 이 아니면 규칙 기반으로 대체한다.
    공지가 짧아 본문 추출이 푸터를 잡은 경우 og:description·<article> 텍스트로 보강한다.
    """
    notice = _notice_text(html, body)
    if use_llm:
        parsed, usage = ctx.llm.people_notice(kind, title, press, notice)
        text = format_people_llm(parsed, kind) if parsed else ""
        if text:
            return text[:1600], ctx.llm.model, usage
    return format_people_notice(notice, kind), "", {}


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


# is_battery_scope / is_trade_topic 가 놓치는 흔한 제목어. 검색 카테고리가 이미
# 배터리·통상이라 이 단어가 제목에 있으면 관련 기사로 본다(범용 명사는 넣지 않는다).
_NAVER_BATTERY_TITLE_KW = ["배터리", "이차전지", "2차전지", "전기차", "EV", "충전소", "배터리팩"]
_NAVER_TRADE_TITLE_KW = ["관세", "반덤핑", "상계관세", "세이프가드", "수출규제", "수출통제",
                         "무역장벽", "무역분쟁", "무역전쟁", "통상", "FTA", "덤핑", "무역확장법"]


def _naver_item_relevant(title: str, category: str, keyword: str = "") -> bool:
    """네이버가 느슨하게 매칭한 무관 기사를 거른다. **제목** 기준으로만 판정한다.

    네이버 description 은 검색어를 그대로 되풀이하는 블러브(기사 요약 아님)라
    본문·요약으로는 걸러지지 않는다. 그래서 관련성 신호가 **제목**에 있어야 통과시킨다.
    이 필터가 없으면 "니켈" 검색의 원자재 시황, "국정감사" 검색의 정치 기사,
    "SK온" 검색의 SK 시황이 배터리·포스코 태그를 달고 대거 유입된다.

    · 포스코·계열사가 제목에 있으면 카테고리 불문 통과
    · '그룹사' 검색은 포스코가 제목에 없으면 탈락
    · 검색어가 제목에 그대로 들어 있으면 통과(정밀 매칭)
    · 그 밖에는 카테고리별 신호(배터리 생태계 / 정책어 / 통상 조치어)가 제목에 있어야 통과
    """
    t = title or ""
    if POSCO_MENTION_RE.search(t) or detect_group_companies(t):
        return True
    if category == "그룹사":
        return False
    if keyword and keyword in t:
        return True
    if category == "정책":
        return _kw_hit_any(t, POLICY_RELEVANCE_KW)
    if category == "통상":
        return (is_trade_topic(t, "") or _kw_hit_any(t, TRADE_MEASURE_KW)
                or _kw_hit_any(t, _NAVER_TRADE_TITLE_KW))
    # '산업'·기타 — 배터리 생태계 신호가 제목에 있어야 한다
    return is_battery_scope(t, "") or _kw_hit_any(t, _NAVER_BATTERY_TITLE_KW)


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
            if not _naver_item_relevant(title, category, keyword):
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
        log.info("네이버 무관 기사 %d건 제외 (제목에 관련성 신호 없음)", dropped)
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


def repair_truncated_title(feed_title: str, html: str) -> str:
    """네이버 검색 API 는 제목을 '...' 로 잘라 준다. 원문 HTML 의 온전한 제목으로 되돌린다.

    - feed 제목이 '...'·'…' 로 끝나고,
    - HTML 제목이 더 길며 feed 제목의 앞부분과 시작이 같으면
    그 HTML 제목을 쓴다. 아니면 원래 제목을 그대로 둔다(오교체 방지).
    """
    t = (feed_title or "").strip()
    if not (t.endswith("...") or t.endswith("…") or t.endswith("···")):
        return feed_title
    full = extract_title(html)
    if not full or len(full) <= len(t):
        return feed_title
    head = t.rstrip(".·…").strip()[:10]
    return full if head and head[:8] in full else feed_title


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


_NOTICE_FOOTER_HINT = ("무단 전재", "사업자등록번호", "Copyright", "저작권자", "All rights reserved")


def _notice_text(html: str, body: str) -> str:
    """인사·부고 공지 텍스트를 최대한 알차게 뽑는다.

    이런 공지는 몇 줄로 짧아서 Readability 가 본문 대신 <푸터(회사 정보)>를 잡는 일이 잦다.
    body · og:description · meta description · <article> 컨테이너 텍스트 중
    푸터가 아닌 가장 긴 것을 고른다.
    """
    cands = [body or ""]
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(pat, html or "", re.I)
        if m:
            cands.append(html_mod.unescape(m.group(1)))
    m = re.search(r"<article[^>]*>(.*?)</article>", html or "", re.S | re.I)
    if m:
        seg = re.sub(r"<script.*?</script>", " ", m.group(1), flags=re.S | re.I)
        seg = re.sub(r"<br\s*/?>", "\n", seg, flags=re.I)
        seg = html_mod.unescape(re.sub(r"<[^>]+>", " ", seg))
        seg = re.sub(r"[ \t]+", " ", seg).strip()
        if seg:
            cands.append(seg)
    good = [c.strip() for c in cands
            if c.strip() and not any(h in c for h in _NOTICE_FOOTER_HINT)]
    return max(good, key=len) if good else (body or "")


# =====================================================================
# 9. 분류 및 중요도 스코어 (PRD F3)
# =====================================================================

def detect_group_companies(text: str) -> list[str]:
    """룰 기반 그룹사 판정. LLM 보다 우선한다. (PRD F4.2)

    '포스코퓨처엠'이 잡히면 상위 개념인 '포스코'는 붙이지 않는다. 칩 중복의 원인이 된다.
    """
    lowered = (text or "").lower()
    found: list[str] = []
    for canonical, aliases in _GROUP_ALIASES_LOWER.items():
        if canonical == "포스코":
            continue  # 마지막에 따로 판단
        if any(alias in lowered for alias in aliases):
            found.append(canonical)
    if not found and any(alias in lowered for alias in _GROUP_ALIASES_LOWER["포스코"]):
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
    title_l = (title or "").lower()
    found = [name for name, words in _CATEGORY_RULES_LOWER.items()
             if any(w in lowered for w in words)]
    # 제목 고정 카테고리(글로벌 통상환경·정부/정책): 핵심어가 제목에 없으면 부차 주제라 뺀다.
    for cat in TITLE_ANCHORED_CATEGORIES:
        if cat in found and not _kw_hit_any(title_l, _CATEGORY_RULES_LOWER[cat]):
            found.remove(cat)
    # '그룹사'는 별도의 그룹사 필터가 담당하므로 카테고리에는 넣지 않는다.
    return dedupe_chips(found)


def score_article(title: str, body: str, group_companies: Sequence[str], press_tier: int) -> int:
    """중요도 0~100. (PRD F3.2)"""
    title_l = (title or "").lower()
    full_l = f"{title} {body}".lower()
    score = 0

    futurem = _GROUP_ALIASES_LOWER["포스코퓨처엠"]
    if any(a in title_l for a in futurem):
        score += SCORE_FUTUREM_TITLE
    elif any(a in full_l for a in futurem):
        score += SCORE_FUTUREM_BODY

    if any(g != "포스코퓨처엠" for g in group_companies):
        score += SCORE_GROUP

    # 배터리 생태계 기사(포스코 미언급 허용 대상)는 그룹사 언급이 없어도
    # 전방 수요·경쟁 동향이라 최소 중요도를 준다. 예전엔 0점이라 큐에서 굶었다.
    if is_battery_scope(title_l, ""):
        score += SCORE_BATTERY_TITLE
    elif is_battery_scope("", body[:1500]):
        score += SCORE_BATTERY_BODY
    if is_trade_topic(title):
        score += SCORE_TRADE

    if any(w in full_l for w in _POLICY_KEYWORDS_LOWER):
        score += SCORE_POLICY
    if press_tier <= 1:
        score += SCORE_MAJOR_PRESS

    # 단순 시황·주가 기사는 알림 피로를 유발하므로 감점한다.
    if any(w in title_l for w in _MARKET_ONLY_KEYWORDS_LOWER):
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

    def people_notice(self, kind: str, title: str, press: str, body: str) -> tuple[dict | None, dict]:
        """인사·부고 공지를 사람별 구조로 뽑는다. 실패하면 (None, {}).

        기사에 실제로 적힌 내용만 채운다 — 고시·이력·학력이 없으면 빈 값이다.
        """
        prompt = PEOPLE_NOTICE_PROMPT.format(
            kind=("부고" if kind == "obituary" else "인사"),
            title=title, press=press or "미상", body=(body or "")[:MAX_BODY_CHARS])
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                content, usage = self._chat(PEOPLE_NOTICE_SYSTEM, prompt)
                parsed = _parse_json_object(content)
                if parsed is None:
                    raise ValueError("JSON 파싱 실패")
                return parsed, usage
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        log.warning("인사·부고 구조화 실패(규칙 기반으로 대체): %s", last_error)
        return None, {}

    def chat_text(self, system: str, user: str) -> str:
        """일반 텍스트 응답(JSON 강제 없음). 텔레그램 챗봇 질의응답용."""
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""

    def weekly_brief(self, kind: str, name: str, articles: list[dict]) -> dict:
        """주간 레포트 섹션 1건을 합성한다.

        kind='swot'  → {"s","w","o","t"} 각 2~3문장 (그룹사 섹션)
        kind='impact'→ {"impact": 3~4문장}          (정부/정책·글로벌 통상 섹션)
        기사가 없으면 빈 dict. 실패해도 레포트 전체가 죽지 않도록 예외를 삼킨다.
        """
        if not articles:
            return {}
        digest = "\n".join(
            f"- ({a.get('published_at','')[:10]}) {a.get('title','')}\n  {a.get('summary_text') or ''}"
            for a in articles)
        if kind == "swot":
            system = ("당신은 포스코 그룹 전략 담당 애널리스트다. 아래 한 주간 기사만 근거로 "
                      "해당 계열사 관점의 주간 SWOT 를 한국어로 작성하고 JSON 으로만 답한다.")
            user = (f"[대상] {name}\n[이번 주 주요 기사]\n{digest}\n\n"
                    "각 항목 2~3문장, 기사에서 실제로 읽어낼 수 있는 내용만. 근거가 없으면 "
                    '"이번 주 해당 신호 없음". 형식: '
                    '{"s":"...","w":"...","o":"...","t":"..."}')
        else:
            system = ("당신은 포스코 그룹 대외전략 담당이다. 아래 한 주간 기사만 근거로 "
                      "이 이슈들이 포스코 그룹(철강·이차전지소재·인프라)에 미치는 영향을 "
                      "한국어로 정리하고 JSON 으로만 답한다.")
            user = (f"[주제] {name}\n[이번 주 주요 기사]\n{digest}\n\n"
                    "3~4문장으로 영향과 대응 관점을 정리한다. 단정하지 말고 검토 필요 톤. "
                    '형식: {"impact":"..."}')
        try:
            content, _ = self._chat(system, user)
            return _parse_json_object(content) or {}
        except Exception as exc:
            log.warning("주간 브리핑 생성 실패 (%s/%s): %s", kind, name, exc)
            return {}

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
    finalists = [c for c, _ in sorted(leftovers, key=lambda x: -x[1])[:10]]
    # 후보 목록에는 임베딩이 실려 오지 않는다(행당 ~31KB라 사이클마다 수 MB가 된다).
    # 여기 도달한 소수 후보의 것만 지금 읽는다.
    cached = storage.embeddings_for([c["id"] for c in finalists])
    for cand in finalists:
        cand_vec = jload(cached.get(cand["id"]), None)
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

# 1회 실행에서 본문 확보(G3/G4)·중복판정·저장까지 갈 최대 건수. 이 안에 든 것은
# 즉시 저장되고, 예산이 남으면 바로 분석된다. 나머지는 _defer_overflow 가 메타만
# 저장하므로(비용 0) 이 값을 올릴 이유가 없다 — 올리면 '본문 있는데 미분석' 큐만 커진다.
MAX_PROCESS_PER_RUN = 24
# 상한을 넘어 이번 회차에 못 다룬 신선 후보 — 버리지 않고 이만큼은 메타데이터만
# 저장해 둔다(본문·LLM 없이). 못 담은 나머지는 다음 회차에 다시 후보가 된다.
DEFER_PER_RUN = 40
# 메타만 저장된 기사(deferred)를 회차당 이만큼 본문 확보 + 분석한다.
# 이 드레인은 안전망이지 우선순위가 아니다 — 신규 분석이 예산을 먼저 쓴다.
# deferred 는 이제 화면에 안 뜨므로(분석 완료분만 노출) 조금 더 공격적으로 빼도 된다.
DEFER_DRAIN_PER_RUN = 16
# deferred 상태로 이 시간을 넘기면(본문을 계속 못 받음) 보관 처리한다.
DEFER_MAX_AGE_HOURS = 24
# 1회 실행에서 처리할 인사·부고 최대 건수 (점수 경쟁 없이 항상 처리)
PEOPLE_PER_RUN = 30
# 그 중 사람별 구조 요약(LLM)에 쓸 수 있는 최대 호출 수. 나머지는 규칙 기반으로 저장되고
# 나중에 `repeople` 로 채울 수 있다. 일일 상한(LLM_DAILY_LIMIT)도 함께 적용된다.
PEOPLE_LLM_PER_RUN = get_env_int("PEOPLE_LLM_PER_RUN", 12, 0)
# 인사·부고는 '기록·레퍼런스' 성격이라 일반 신선도 컷오프(72h)로 버리면 안 된다.
# 부처 인사는 발행 후 며칠 지나 인지되는 경우가 많다. 이 창 안이면 수집한다(알림은
# is_backfill=6h 규칙이 그대로 막으므로 오래된 인사가 텔레그램으로 가지는 않는다).
PEOPLE_BACKFILL_CUTOFF_HOURS = 24 * 30
# 메타만 저장분(deferred)이 이 수 미만이면 파이프라인을 '안정'으로 본다. 본문 대기 큐(30)와
# 달리 느슨하게 둔다 — deferred 는 알림 후보가 아니라서 억제를 유지할 이유가 약하다.
DEFER_BACKLOG_STABLE = 600

# ── 보존 정책 (Supabase 무료 500MB 한도 대비) ─────────────────────────
# 핵심 기사(알림 대상이었음/중요도 임계값 이상/그룹사 태그 O)는 cfg.article_retention_days
# (기본 550일 ≈ 18개월) 동안 보관하고, 그 외 잡음·주제이탈 기사는 아래 기간만 보관한다.
# 자식 테이블(요약·SWOT·본문·알림)은 on delete cascade 로 함께 삭제된다.
RETENTION_SOFT_DAYS   = 90    # 일반·archived 기사 보관일
RETENTION_LOG_DAYS    = 90    # collection_logs 보관일 (하루 약 288행 쌓임)
RETENTION_LEDGER_DAYS = 180   # url_ledger 보관일 (이후 재수집돼도 게이트가 다시 거른다)
RETENTION_TGLOG_DAYS  = 30    # telegram_log(봇 발신 로그) 보관일
RETENTION_INTERVAL_HOURS = 24 # 보존 정리 실행 간격 (매 수집 사이클마다 하면 과하다)
RETENTION_VACUUM_DAYS = 7     # SQLite VACUUM(파일 축소) 최소 간격
_RETENTION_LAST: "datetime | None" = None
_VACUUM_LAST: "datetime | None" = None


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


def _title_snippet_relevant(title: str, snippet: str) -> tuple[bool, list[str]]:
    """본문 없이 관련성을 저비용 판정한다. (keep, groups) 반환.

    스니펫(특히 네이버 description)은 검색어를 되풀이하는 블러브라 신뢰도가 낮다.
    그래서 배터리·통상 신호는 **제목**에서만 인정하고, 그룹사·포스코 언급은
    스니펫도 함께 본다(회사명은 블러브에 우연히 나오기 어렵다). 최종 관련성은
    _drain_deferred 가 본문으로 다시 판정하므로 여기서 조금 놓쳐도 복구된다."""
    probe = f"{title}\n{snippet or ''}"
    groups = normalize_group_list(detect_group_companies(probe))
    keep = bool(
        groups
        or POSCO_MENTION_RE.search(probe)
        or is_battery_scope(title, "")
        or is_trade_topic(title, "")
        or people_news_kind("", title)
    )
    return keep, groups


def _defer_overflow(ctx: Context, overflow: list[tuple[RawItem, bool]],
                    dedup_candidates: list[dict]) -> int:
    """상한을 넘어 이번 회차에 못 다룬 신선 후보를 메타데이터만 저장한다.

    본문·HTTP·LLM 을 쓰지 않으므로 비용이 0이다. 관련성·카테고리·점수는 **제목** 기준으로만
    임시 판정하고(스니펫 블러브 오염 방지), 본문 재검증·최종 태깅은 _drain_deferred 가 한다.
    통과분은 analyzed_at=NULL 로 넣어 두되 화면에는 노출하지 않는다(분석 완료분만 노출).
    """
    storage = ctx.storage
    known = {c.get("url_canonical") or c.get("url_source") for c in dedup_candidates}
    saved = 0
    for item, is_backfill in overflow[:DEFER_PER_RUN]:
        if item.url_source in known or item.url_original in known:
            continue
        keep, groups = _title_snippet_relevant(item.title, item.snippet or "")
        if not keep:
            continue   # 원장에 넣지 않는다 — 다음 회차 상한이 커지면 정식 처리될 수 있다
        pk = people_news_kind(item.url_source, item.title)
        cats = [PEOPLE_NEWS_CATEGORY] if pk else detect_categories(item.title, "")
        aid = new_id()
        row = {
            "id": aid, "url_source": item.url_source, "url_source_aliases": [],
            "url_canonical": item.url_source, "url_original": item.url_original,
            "title": item.title, "press_id": None,
            "press_name": item.press_hint or "", "author": "",
            "published_at": iso(item.published_at), "collected_at": iso(now_utc()),
            "source_type": item.source_type, "thumbnail_url": "",
            "content_hash": "", "dedup_group_id": aid, "is_representative": True,
            "is_backfill": is_backfill,
            "importance_score": SCORE_PEOPLE_NEWS if pk else score_article(item.title, "", groups, 3),
            "sentiment": None, "keywords": [], "group_companies": groups,
            "categories": cats,
            "title_embedding": None, "analyzed_at": None, "status": "active",
        }
        if storage.insert_article(row):
            saved += 1
            known.add(item.url_source)
            ctx.seen_cache.add(item.url_source)
            dedup_candidates.append({
                "id": aid, "title": item.title, "published_at": iso(item.published_at),
                "dedup_group_id": aid, "is_representative": True, "title_embedding": None,
                "press_name": row["press_name"], "content_hash": "",
                "url_canonical": item.url_source, "url_source": item.url_source,
            })
    return saved


def _drain_deferred(ctx: Context, limit: int, dedup_candidates: list[dict],
                    people_llm: int = 0) -> int:
    """메타만 저장된(deferred) 기사를 본문 확보 → 관련성 재검 → 분석한다.

    본문을 계속 못 받으면 DEFER_MAX_AGE_HOURS 후 보관 처리한다.
    본문 기준 관련성에서 탈락하면(제목만 그럴듯했던 경우) 역시 보관한다.
    people_llm = 이 드레인에서 인사·부고 구조화에 쓸 수 있는 LLM 호출 수.
    """
    storage, http = ctx.storage, ctx.http
    done = 0
    pending = storage.deferred_articles(limit)
    # 본문 확보는 신규 수집과 동일하게 병렬로 받는다. 순차로 받으면 느린 언론사 한 곳이
    # 회차 전체를 붙잡아 사이클 시간이 수십 초씩 늘어난다.
    targets = {a["id"]: (a.get("url_original") or a.get("url_canonical") or a.get("url_source"))
               for a in pending}
    fetched = prefetch_articles(http, [u for u in targets.values() if u])
    for art in pending:
        aid = art["id"]
        target = targets.get(aid)
        canonical, html = fetched.get(target, ("", "")) if target else ("", "")
        body = extract_body(html) if html else ""
        if len(body) < 200:
            collected = parse_dt(art.get("collected_at"))
            if collected and (now_utc() - collected) > timedelta(hours=DEFER_MAX_AGE_HOURS):
                storage.update_article(aid, {"status": "archived"})
            continue   # 다음 회차 재시도

        # 인사·부고면 사람별 구조로 정리한다 (예산 남으면 LLM, 아니면 규칙 기반)
        pk = people_news_kind(canonical or art["url_source"], art["title"])
        if pk:
            press_name, press_id, _ = resolve_press(storage, canonical or target,
                                                    art.get("press_name") or "", html)
            storage.update_article(aid, {
                "url_canonical": canonical or art["url_canonical"],
                "press_id": press_id, "press_name": press_name,
                "categories": [PEOPLE_NEWS_CATEGORY], "importance_score": SCORE_PEOPLE_NEWS,
                "analyzed_at": iso(now_utc()),
            })
            summary, model, usage = people_summary(ctx, pk, art["title"], press_name, body,
                                                   use_llm=people_llm > 0, html=html)
            if model:
                people_llm -= 1
            storage.save_summary({
                "id": new_id(), "article_id": aid,
                "summary_text": summary, "perspective_text": "",
                "summary_source": "notice", "model": model, "token_usage": usage or None,
                "created_at": iso(now_utc()),
            })
            done += 1
            continue

        rule_groups = detect_group_companies(f"{art['title']}\n{body[:GROUP_LEAD_CHARS]}")
        probe = f"{art['title']}\n{body}"
        if not (rule_groups or POSCO_MENTION_RE.search(probe)
                or is_battery_scope(art["title"], body[:1500])
                or is_trade_topic(art["title"], body[:1500])
                or bool(KOREA_KR_NEWS_RE.search(canonical or ""))):
            storage.update_article(aid, {"status": "archived"})
            continue

        # 본문 도착 후 중복 재판정 — 그새 정식 수집된 기사와 겹치면 흡수한다
        content_hash = sha256(body)
        dup = find_duplicate(storage, ctx.llm, art["title"], parse_dt(art["published_at"]),
                             content_hash, canonical or art["url_canonical"], dedup_candidates)
        if dup and dup["id"] != aid:
            storage.append_alias(dup["id"], art["url_source"])
            storage.update_article(aid, {"status": "archived"})
            continue

        press_name, press_id, press_tier = resolve_press(storage, canonical or target,
                                                         art.get("press_name") or "", html)
        storage.update_article(aid, {
            "url_canonical": canonical or art["url_canonical"],
            "press_id": press_id, "press_name": press_name,
            "author": extract_author(html, body, press_name),
            "content_hash": content_hash, "thumbnail_url": extract_thumbnail(html),
            "importance_score": score_article(art["title"], body, rule_groups, press_tier),
        })
        row = {"id": aid, "title": art["title"], "press_id": press_id, "press_name": press_name,
               "importance_score": score_article(art["title"], body, rule_groups, press_tier),
               "group_companies": rule_groups}
        if analyze_and_save(ctx, aid, row, body, "fulltext") is not None:
            done += 1
        else:
            # 분석 실패(3회 재시도해도 동일) — 링크·제목 카드로 확정해 deferred 큐에서 뺀다.
            storage.update_article(aid, {"analyzed_at": iso(now_utc())})
    return done


def _save_people_news(ctx: Context, item: RawItem, canonical: str, html: str, body: str,
                      kind: str, press_name: str, press_id: str | None,
                      dedup_candidates: list[dict], is_backfill: bool,
                      use_llm: bool = False) -> bool:
    """인사·부고 기사를 저장한다. 포스코 관점·SWOT 은 없다.

    use_llm 이면 사람별 구조 요약을 시도한다. 반환: LLM 을 실제로 썼으면 True.
    """
    storage = ctx.storage
    aid = new_id()
    row = {
        "id": aid, "url_source": item.url_source, "url_source_aliases": [],
        "url_canonical": canonical or item.url_source, "url_original": item.url_original,
        "title": item.title, "press_id": press_id, "press_name": press_name,
        "author": extract_author(html, body, press_name),
        "published_at": iso(item.published_at), "collected_at": iso(now_utc()),
        "source_type": item.source_type, "thumbnail_url": extract_thumbnail(html),
        "content_hash": sha256(body), "dedup_group_id": aid, "is_representative": True,
        "is_backfill": is_backfill, "importance_score": SCORE_PEOPLE_NEWS,
        "sentiment": "중립", "keywords": [], "group_companies": [],
        "categories": [PEOPLE_NEWS_CATEGORY], "title_embedding": None,
        "analyzed_at": iso(now_utc()), "status": "active",
    }
    if not storage.insert_article(row):
        return False
    ctx.seen_cache.add(item.url_source)
    dedup_candidates.append({
        "id": aid, "title": item.title, "published_at": iso(item.published_at),
        "dedup_group_id": aid, "is_representative": True, "title_embedding": None,
        "press_name": press_name, "content_hash": row["content_hash"],
        "url_canonical": row["url_canonical"], "url_source": item.url_source,
    })
    summary, model, usage = people_summary(ctx, kind, item.title, press_name, body, use_llm, html=html)
    storage.save_summary({
        "id": new_id(), "article_id": aid,
        "summary_text": summary, "perspective_text": "",
        "summary_source": "notice", "model": model, "token_usage": usage or None,
        "created_at": iso(now_utc()),
    })
    return bool(model)


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


def clean_site_name(name: str, domain: str = "") -> str:
    """매체명에서 부제·영문 병기·래퍼 접두를 떼어낸다.

    예) '경기신문 - 기본에 충실한 …'      → '경기신문'
        'AP신문 |  온라인뉴스미디어  …'    → 'AP신문'
        '더스탁(The Stock)'              → '더스탁'
        'Daum | 뉴스1'   (daum.net)     → '뉴스1'   (래퍼 도메인은 뒤쪽이 실제 출처)
    """
    name = re.sub(r"\s+", " ", html_mod.unescape(name or "")).strip()
    if not name:
        return ""
    # domain 은 domain_of 로 이미 접힌 값(v.daum.net → daum.net)이 들어온다.
    if domain in NEWS_AGGREGATORS:
        # '래퍼 | 실제출처' — 마지막 구분자 뒤(한글 조각)를 취한다.
        parts = re.split(r"\s*[|\-–·]\s*", name)
        name = next((p for p in reversed(parts) if _has_hangul(p)), parts[-1]).strip()
    else:
        # 첫 구분자 앞이 매체명. 뒤는 대개 슬로건·부제다.
        name = re.split(r"\s*[|\-–]\s*", name, maxsplit=1)[0].strip()
        # 끝에 붙은 '(English…)' 영문 병기 제거 (한글 병기 '(주간)' 등은 남긴다).
        name = re.sub(r"\s*\([A-Za-z0-9 .,'&/\-]+\)\s*$", "", name).strip()
    return name


def site_name_from_html(html: str, domain: str = "") -> str:
    """HTML 의 og:site_name / <meta name=publisher> 에서 매체명을 뽑는다.

    SEED_PRESS 에 없는 매체도 대부분 이 태그에 한글 매체명을 넣는다.
    영문 사이트명·도메인 형태는 신뢰하지 않는다(한글이 있어야 채택).
    """
    for pat in (r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\'](?:twitter:site|publisher|source)["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(pat, html or "", re.I)
        if m:
            name = clean_site_name(m.group(1), domain)
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
    og_name = site_name_from_html(html, domain)

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
        # [부고]·[인사]·[승진] 말머리 기사는 키워드와 무관하게 통과시킨다. (사용자 지정)
        raw += [
            item for item in collect_rss_feeds(http, rss_feeds)
            if matches_keywords(f"{item.title} {item.snippet}", keywords)
            or people_news_kind(item.url_source, item.title)
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
        # 인사·부고는 훨씬 긴 창을 쓴다 — 며칠 지나 올라온 부처 인사도 놓치지 않는다.
        cutoff = (PEOPLE_BACKFILL_CUTOFF_HOURS
                  if people_news_kind(item.url_source, item.title)
                  else cfg.backfill_cutoff_hours)
        if age > timedelta(hours=cutoff):
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

    # 메타만 저장된(deferred) 기사가 있으면 예산의 일부를 그 드레인에 예약한다(안 그러면
    # 신규 분석이 매 회차 예산을 다 써 deferred 가 안 빠진다). 다만 예약은 작게 —
    # 신규 기사 분석이 우선이고, 남는 예산은 아래에서 deferred 드레인이 더 쓴다.
    defer_pending = storage.deferred_count()
    defer_budget = min(6, defer_pending, max(1, llm_budget // 4)) if defer_pending else 0
    llm_budget = max(0, llm_budget - defer_budget)

    # 인사·부고는 점수 경쟁에서 빼고 항상 처리한다 — 제목 점수가 0이라 일반 큐에 두면
    # 영원히 상한에 밀린다. (사용자 지정) 사람별 구조 요약은 LLM 을 쓰되, 이번 회차·오늘
    # 남은 예산 안에서만 한다. 예산 밖은 규칙 기반으로 저장되고 `repeople` 로 채운다.
    people = [p for p in fresh if people_news_kind(p[0].url_source, p[0].title)][:PEOPLE_PER_RUN]
    people_urls = {p[0].url_source for p in people}
    fresh = [p for p in fresh if p[0].url_source not in people_urls]
    people_llm_left = min(PEOPLE_LLM_PER_RUN, max(0, daily_left - llm_budget - defer_budget))

    # 저장 상한은 LLM 예산과 분리한다. 저장(본문 확보+중복판정)은 비용이 작고,
    # 여기서 조이면 관련 기사가 큐에서 굶어 72시간 뒤 stale 로 사라진다.
    # 넘친 후보는 _defer_overflow 가 메타만 저장해 두므로 '수집'은 무엇도 잃지 않는다.
    pending_now = storage.unanalyzed_count() + storage.deferred_count()
    fresh_available = len(fresh)   # 절단 전 신규 후보 수 — 안정화 판단에 쓴다
    process_cap = MAX_PROCESS_PER_RUN

    # 중요도 순 + 그룹사 균형. 중요도만 쓰면 포스코퓨처엠(제목 +50)이 큐를 독점해
    # 포스코DX·이앤씨 기사가 매 회차 뒤로 밀린다. 그룹사별로 번갈아 뽑는다. (PRD F4.6)
    fresh.sort(key=lambda pair: score_article(pair[0].title, pair[0].snippet, [], 3), reverse=True)
    overflow: list[tuple[RawItem, bool]] = []
    if fresh_available > process_cap:
        picked = interleave_by_group(fresh, process_cap)
        picked_urls = {p[0].url_source for p in picked}
        overflow = [pair for pair in fresh if pair[0].url_source not in picked_urls]
        log.info("이번 실행 즉시 처리 %d건 · 메타 저장 대기 %d건 · 분석 대기 %d건",
                 len(picked), len(overflow), pending_now)
        fresh = picked

    # 인사·부고를 같은 처리 루프 앞에 붙인다 (본문만 받아 _save_people_news 로 확정)
    fresh = people + fresh
    if people:
        log.info("인사·부고 %d건 처리", len(people))

    new_count = 0
    dup_count = 0
    # 알림 판정에 필요한 항목만 담는다. {id, score, is_backfill, published_at, priority,
    #  policy: (해당여부, 키워드통과), trade: (해당여부, 키워드통과)}
    saved_for_notify: list[dict] = []
    # '무조건 받을' 키워드 — 제목·계열사에 있으면 임계값 무관 무조건 알림(우선 기사)
    always_kws = [k for k in jload(state.get("always_notify_keywords"), []) if k]
    # '제외' 키워드 — 제목에 있으면 임계값·무조건 받을 키워드보다 우선해서 알림하지 않는다(최우선).
    exclude_kws = [k for k in jload(state.get("exclude_notify_keywords"), []) if k]
    # (하위호환) 무조건 발송 점수 — UI 제거됨, 컬럼·기본 0. 설정돼 있으면 우선 기사로 취급.
    try:
        hard_score = int(state.get("hard_notify_score") or 0)
    except (TypeError, ValueError):
        hard_score = 0
    # 특수 주제(정책·통상) — 관심 키워드 하나 AND 필수 공통 키워드 하나 (둘 다 필수).
    # 제목에 제외 키워드가 있으면 이 주제 알림에서 뺀다.
    policy_notify_kws = [k for k in jload(state.get("policy_notify_keywords"), []) if k]
    policy_required_kws = [k for k in jload(state.get("policy_required_keywords"), []) if k]
    policy_exclude_kws = [k for k in jload(state.get("policy_exclude_keywords"), []) if k]
    trade_notify_kws = [k for k in jload(state.get("trade_notify_keywords"), []) if k]
    trade_required_kws = [k for k in jload(state.get("trade_required_keywords"), []) if k]
    trade_exclude_kws = [k for k in jload(state.get("trade_exclude_keywords"), []) if k]

    # ── G3: 리다이렉트 해제 + HTML 확보 (병렬) ───────────────────────
    prefetched = prefetch_articles(http, [item.url_original for item, _ in fresh])

    for item, is_backfill in fresh:
        canonical, html = prefetched.get(item.url_original, ("", ""))
        if not canonical:
            storage.upsert_ledger(item.url_source, "extract_failed")
            continue

        # ── G4: 본문 추출 (G3 응답을 재사용하므로 추가 요청 없음) ────
        body = extract_body(html)
        # 네이버가 '...' 로 자른 제목을 원문 제목으로 되돌린다 (이후 모든 단계가 이 제목을 쓴다)
        item.title = repair_truncated_title(item.title, html)
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

        # ── 인사·부고 — 포스코 관점·SWOT 없이 사람별 구조 요약만 담는다 (사용자 지정) ──
        pk = people_news_kind(canonical or item.url_source, item.title)
        if pk:
            used = _save_people_news(ctx, item, canonical, html, body, pk, press_name,
                                     press_id, dedup_candidates, is_backfill,
                                     use_llm=people_llm_left > 0)
            if used:
                people_llm_left -= 1
            new_count += 1
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
        # 우선 알림: '항상 발송 키워드' 매칭 또는 '무조건 발송 점수' 이상이면
        # 임계값·야간 게이트를 우회한다. (포스코퓨처엠 특례는 폐지 — 키워드로 추가)
        # '항상 발송 키워드' 는 **제목 + 판정된 주체 그룹사**로만 본다 — 본문 전체를 훑으면
        # 포스코 그룹 기사 대부분에 '포스코홀딩스'·'포스코퓨처엠'이 스치듯 등장해
        # 사실상 모든 그룹 기사가 야간에도 발송된다(파업 기사 오발송 사례, 2026-09-08).
        priority_probe = f"{item.title}\n{' '.join(rule_groups)}"
        excluded = _kw_hit_any(item.title, exclude_kws)   # 제목에 제외 키워드 → 무조건 알림 안 함
        is_priority = (not excluded) and (
            _kw_hit_any(priority_probe, always_kws)
            or (hard_score > 0 and score >= hard_score))
        # 특수 주제 발송 조건: 관심 키워드 하나 AND 필수 공통 키워드 하나 (둘 다 필수, 비면 발송 안 함).
        # 제목에 그 주제의 제외 키워드가 있으면 뺀다.
        policy_ok = (_kw_hit_any(probe, policy_notify_kws)
                     and _kw_hit_any(probe, policy_required_kws)
                     and not _kw_hit_any(item.title, policy_exclude_kws))
        trade_ok = (_kw_hit_any(probe, trade_notify_kws)
                    and _kw_hit_any(probe, trade_required_kws)
                    and not _kw_hit_any(item.title, trade_exclude_kws))
        saved_for_notify.append({
            "id": article_id, "score": score, "is_backfill": is_backfill,
            "published_at": item.published_at, "priority": is_priority, "excluded": excluded,
            "policy": (is_policy, policy_ok),
            "trade": (is_trade, trade_ok),
        })

    # ── 넘친 신선 후보를 메타데이터만 저장한다 (본문·LLM 없음, 비용 0) ──
    # '수집' 단계에서 관련 기사를 절대 잃지 않기 위한 장치. 아래 _drain_deferred 가
    # 다음 회차부터 본문을 받아 정식 분석한다.
    if overflow:
        deferred = _defer_overflow(ctx, overflow, dedup_candidates)
        if deferred:
            log.info("메타데이터만 저장 %d건 (다음 회차부터 본문 확보·분석)", deferred)

    # ── 분석 백로그 드레인 ───────────────────────────────────────────
    # 이전 실행에서 본문까지 저장되고 분석만 밀린 기사를 예산 안에서 처리한다.
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

    # ── 메타만 저장된(deferred) 기사 드레인 — 본문 확보 → 관련성 재검 → 분석 ──
    # 예약해 둔 defer_budget + 앞 단계에서 남은 예산을 함께 쓴다.
    drain_budget = defer_budget + max(0, llm_budget)
    drained = _drain_deferred(ctx, min(drain_budget, DEFER_DRAIN_PER_RUN), dedup_candidates,
                              people_llm=people_llm_left) if drain_budget else 0
    if drained:
        log.info("메타 저장분 분석 %d건 (예약 예산 %d)", drained, defer_budget)

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

    # ── 보존 정책: 하루 1회만 (Supabase 무료 500MB 한도 대비) ─────────
    # 핵심 기사는 cfg.article_retention_days(≈18개월), 잡음은 RETENTION_SOFT_DAYS(90일).
    global _RETENTION_LAST, _VACUUM_LAST
    _n = now_utc()
    if _RETENTION_LAST is None or (_n - _RETENTION_LAST) >= timedelta(hours=RETENTION_INTERVAL_HOURS):
        _RETENTION_LAST = _n
        keep_score = effective_threshold(ctx)
        gone = storage.purge_old_articles(cfg.article_retention_days, RETENTION_SOFT_DAYS, keep_score)
        logs_gone = storage.prune_collection_logs(RETENTION_LOG_DAYS)
        ledger_gone = storage.prune_url_ledger(RETENTION_LEDGER_DAYS)
        tglog_gone = storage.prune_telegram_log(RETENTION_TGLOG_DAYS)
        if gone or logs_gone or ledger_gone or tglog_gone:
            log.info("보존 정리: 기사 %d · 수집로그 %d · URL원장 %d · 발송로그 %d 삭제 (핵심 %d일·잡음 %d일 보관)",
                     gone, logs_gone, ledger_gone, tglog_gone,
                     cfg.article_retention_days, RETENTION_SOFT_DAYS)
        if gone:
            # 대량 삭제분을 스캔 스토어(델타는 삭제를 못 봄)에 즉시 반영한다.
            try:
                refresh_scan_store(storage, full=True)
            except Exception as e:   # pragma: no cover
                log.debug("스캔 스토어 재적재 실패(무시): %s", e)
        # SQLite 는 삭제해도 파일이 안 줄어들어 가끔 VACUUM 으로 회수한다(락 위험 있어 드물게).
        if gone and cfg.db_backend == "sqlite" and (
                _VACUUM_LAST is None or (_n - _VACUUM_LAST) >= timedelta(days=RETENTION_VACUUM_DAYS)):
            _VACUUM_LAST = _n
            try:
                storage.vacuum()
                log.info("VACUUM 완료 — 삭제분 디스크 회수")
            except Exception as e:
                log.warning("VACUUM 실패(무시): %s", e)

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
        # 정책·통상 주제 기사가 ①관심 + ②필수공통 키워드를 모두 통과하고 토글이 켜져 있으면
        # → 일반 중요도 임계값을 우회해 발송한다(정책·통상 기사는 포스코 미언급이라 점수가 낮다).
        #   ②(필수공통) 가 비어 있으면 kw_ok=False → 여전히 발송 안 됨.
        topic_hit = ((it["policy"][0] and it["policy"][1] and notify_policy)
                     or (it["trade"][0] and it["trade"][1] and notify_trade))
        should_send = (
            (not suppressed)                       # 부트스트랩·복구 억제
            and (not it.get("excluded"))           # 제목에 '제외' 키워드 → 무조건 웹에만
            and (not it["is_backfill"])            # 6시간 넘은 기사는 웹에만
            and _topic_ok(it["policy"], notify_policy)
            and _topic_ok(it["trade"], notify_trade)
            and (it["score"] >= notify_threshold or it["priority"] or topic_hit)  # 중요도 게이트(우선·주제매칭은 우회)
            and it["published_at"] is not None
            and it["published_at"] >= bootstrap_at  # 파이프라인 가동 이전 기사는 절대 알림 안 함
        )
        status = "queued" if should_send else "skipped"
        # priority: 1 = '무조건 받을 키워드'(야간도 우회 가능) · 2 = 정책·통상 주제 매칭(임계값만 우회)
        prio_val = 1 if it["priority"] else (2 if topic_hit else 0)
        if storage.queue_notification(it["id"], cfg.telegram_chat_id, status, prio_val):
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

    body_backlog = storage.unanalyzed_count()      # 본문까지 확보돼 곧 분석·알림될 큐 — 폭주 위험
    defer_backlog = storage.deferred_count()        # 메타만 저장분 — 본문·분석 없이 배경에서 천천히 드레인
    backlog = body_backlog + defer_backlog
    # 부트스트랩/복구 억제는 "한 회"가 아니라 파이프라인이 안정될 때까지 유지한다. (PRD F1.1)
    # 초기 수집 몇 시간 동안 밀린 기사가 한꺼번에 알림으로 쏟아지는 것을 막는 장치다.
    # 안정화 판정은 '알림 폭주 위험'(본문 대기 큐)만 본다. 메타만 저장분(deferred)은
    # 분석·본문이 없어 알림 후보가 아니고, 대부분 관련성 재검에서 보관 처리되며 느리게
    # 빠지므로, 여기에 세면 배경 큐가 조금만 쌓여도 알림이 영구히 억제된다. (조사 2026-09-08)
    stabilized = (fresh_available < 20 and new_count < 10
                  and body_backlog < 30 and defer_backlog < DEFER_BACKLOG_STABLE)
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


def is_public_http_url(raw_url: str) -> bool:
    """공인 인터넷 주소인가 — 사설망·루프백·링크로컬이면 False.

    수동 URL 등록은 서버가 그 주소를 대신 가져온다. 막지 않으면 사내망 주소나
    클라우드 메타데이터(169.254.169.254)를 대신 긁게 만들 수 있다(SSRF).
    """
    import ipaddress
    import socket
    host = (urlsplit(raw_url).hostname or "").strip("[]")
    if not host:
        return False
    if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal")):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False   # 이름을 못 풀면 가져올 수도 없다
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


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
    if not is_public_http_url(raw_url):
        # 사설망·로컬 주소를 넣어 서버가 내부망을 대신 긁게 만드는 SSRF 를 막는다.
        return {"ok": False, "error": "외부에 공개된 뉴스 주소만 등록할 수 있습니다."}

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
    else:
        # 바로 목록에 뜬 수동 등록(봇 DM 등) — 임계값을 넘으면 채널에도 발송한다.
        try:
            if queue_manual_notify(ctx, article_id):
                out["notified"] = True
        except Exception as exc:   # pragma: no cover
            log.warning("수동 등록 알림 처리 실패: %s", exc)
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


def _in_night_window(hour: int, start: int, end: int) -> bool:
    """hour(운영 기준 시간대 시각)가 야간 억제 창(start~end)에 드는가.

    start>end 면 자정을 넘는 창(예: 23→7 = 23·0·1·…·6시). start==end 면 창 없음.
    실제 start·end·min_score 는 Config(NIGHT_START_HOUR 등)에서 온다.
    """
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


RATE_LIMIT_SLEEP = 3.5                   # 채널 분당 ~20건 한도 → 건당 3초 이상 간격(여유 포함)
PRIORITY_FLOOD_WARN = 10                 # 한 회차 우선 기사가 이 수 이상이면 경고 로그
SEND_BATCH_PER_CYCLE = 12               # 파이프라인 한 회차에 발송할 최대 건수(백로그 폭주 완충)
TG_MAX_PER_MIN = 17                      # 최근 60초 발송이 이 수에 닿으면 대기 (텔레그램 한도 아래)
FLOOD_PAD_SEC = 3                        # 텔레그램이 준 retry_after 에 이만큼 더 얹어 기다린다
NOTIFY_MAX_AGE_HOURS = 24               # 이보다 오래 큐에 남은 건은 일시 오류라도 failed 로 확정
# 파이프라인 루프와 수동 등록(API·봇)이 동시에 send_notifications 를 부르면 같은 큐 행을
# 두 번 보낼 수 있다. 발송 구간을 직렬화해 중복을 막는다.
_SEND_LOCK = threading.Lock()

# ── 텔레그램 rate limit / flood 상태 (모듈 전역) ──────────────────────
# 429 "Too Many Requests: retry after N" 을 받으면 그 N초 동안 봇 전체가 발송 금지다.
# 이걸 무시하고 계속 두드리면 텔레그램이 금지 시간을 오히려 늘린다(2026-09-08 사고).
_FLOOD_UNTIL = 0.0                       # time.monotonic() 기준. 이 시각까지 발송 보류
_FLOOD_LOCK = threading.Lock()
_SEND_TIMES: list[float] = []            # 최근 발송 시각(monotonic). 분당 한도 계산용
_RATE_LOCK = threading.Lock()


def _flood_arm(retry_after: float) -> None:
    """텔레그램이 준 대기 시간만큼(+여유) 발송을 보류하도록 설정."""
    global _FLOOD_UNTIL
    with _FLOOD_LOCK:
        _FLOOD_UNTIL = max(_FLOOD_UNTIL,
                           time.monotonic() + max(1.0, retry_after) + FLOOD_PAD_SEC)


def _flood_remaining() -> float:
    """flood 대기가 남았으면 남은 초, 아니면 0."""
    with _FLOOD_LOCK:
        return max(0.0, _FLOOD_UNTIL - time.monotonic())


def _rate_gate() -> None:
    """채널 분당 한도 아래로 유지한다. 한도에 닿으면 가장 오래된 발송이 빠질 때까지 잔다.
    (락은 sleep 전에 반드시 놓아 다른 스레드를 막지 않는다.)"""
    while True:
        with _RATE_LOCK:
            now = time.monotonic()
            _SEND_TIMES[:] = [t for t in _SEND_TIMES if now - t < 60.0]
            if len(_SEND_TIMES) < TG_MAX_PER_MIN:
                _SEND_TIMES.append(now)
                return
            wait = 60.0 - (now - _SEND_TIMES[0]) + 0.3
        log.info("텔레그램 분당 발송 한도 근접 — %.1f초 대기", wait)
        time.sleep(min(max(wait, 0.1), 60.0))


_TRANSIENT_TG_HINTS = (
    "too many requests", "retry after", "429", "bad gateway", "502",
    "503", "gateway time-out", "gateway timeout", "internal server error",
    "timed out", "timeout", "connection", "temporarily",
)


def _is_transient_tg_error(err: str | None) -> bool:
    """rate limit·서버 오류·네트워크 오류처럼 <기다리면 풀리는> 실패인가.
    이런 실패는 재시도 횟수를 깎지 않는다(일시 장애로 영구 실패 처리 방지)."""
    low = (err or "").lower()
    return any(h in low for h in _TRANSIENT_TG_HINTS)


def _notify_too_old(row: dict) -> bool:
    """큐에 들어온 지 NOTIFY_MAX_AGE_HOURS 를 넘겼는가.
    일시 오류라도 이 정도 오래됐으면 포기하고 failed 로 확정한다(무한 재시도 방지)."""
    created = parse_dt(row.get("created_at"))
    if created is None:
        return False
    return (now_utc() - created) > timedelta(hours=NOTIFY_MAX_AGE_HOURS)


def esc(text: str) -> str:
    """parse_mode=HTML 이므로 <, >, & 를 반드시 이스케이프한다. (PRD F7.2)"""
    return html_mod.escape(text or "", quote=False)


def esc_attr(text: str) -> str:
    """href="..." 안에 들어가는 값. 큰따옴표까지 이스케이프해야 태그가 안 깨진다.

    esc() 는 quote=False 라 " 를 그대로 둔다. 주소에 " 가 섞이면 <a href> 가 끊겨
    'can't parse entities' 로 영구 실패한다.
    """
    return html_mod.escape(text or "", quote=True)


# 텔레그램 메시지 상한은 4096자. 넘으면 'message is too long' 으로 <재시도해도 계속>
# 실패한다. 요약·관점이 긴 기사가 조용히 실패로 쌓이던 원인이라 보낼 때 잘라 준다.
TELEGRAM_MAX_CHARS = 4096


def clamp_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    tail = "\n\n…(길어서 줄였습니다)"
    cut = text[: limit - len(tail)]
    # 태그 한가운데서 자르면 HTML 이 깨지므로 마지막으로 닫힌 지점까지 물러난다
    if cut.count("<") > cut.count(">"):
        cut = cut[: cut.rfind("<")]
    return cut + tail


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
        lines += ["", f'🔗 <a href="{esc_attr(link)}">원문 보기</a>']
    return "\n".join(lines)


def _notify_reason(row: dict, always_kws: Sequence[str] = (),
                   threshold: int = 0, hard_score: int = 0) -> str:
    """알림 큐에서 나가는 기사의 '발송 이유' — 대시보드 '발송 로그' 에 표시된다.

    발송 시점에 <제목 + 판정된 그룹사> 와 점수로 다시 따져, 셋 중 무엇 때문에
    나가는지 사람이 읽을 문구로 만든다.
      · 항상발송 키워드 '…'        — 마스터가 지정한 키워드 매칭 (임계값·야간 우회)
      · 무조건 발송 점수 66 ≥ 65   — hard_notify_score 이상
      · 중요도 55 ≥ 임계값 30      — 평범한 임계값 초과
    수동 URL 등록 기사는 앞에 'URL 등록 · ' 를 붙인다.
    always_kws/threshold 없이 불린 경우(구버전 경로·테스트)는 짧은 라벨로 폴백한다.
    """
    manual = row.get("source_type") == "manual"
    prefix = "URL 등록 · " if manual else ""
    score = int(row.get("importance_score") or 0)
    probe = f"{row.get('title') or ''}\n{' '.join(jload(row.get('group_companies'), []))}"
    kw = _kw_first_hit(probe, [k for k in always_kws if k])
    if kw:
        return f"{prefix}무조건 받을 키워드 '{kw}'"
    if int(row.get("priority") or 0) == 2:
        return f"{prefix}정책·통상 주제 키워드 매칭"
    if hard_score > 0 and score >= hard_score:
        return f"{prefix}무조건 발송 점수 {score} ≥ {hard_score}"
    if threshold > 0 and score >= threshold:
        return f"{prefix}중요도 {score} ≥ 임계값 {threshold}"
    # 판정 근거 없이 불린 경우 — 대략적 분류로만
    if manual:
        return "URL 등록"
    return "우선 발송" if row.get("priority") else "자동 알림"


def effective_threshold(ctx: Context) -> int:
    """/threshold 로 지정한 값이 있으면 그것을, 없으면 .env 값을 쓴다."""
    override = ctx.storage.get_run_state().get("notify_threshold")
    if override is not None:
        try:
            return int(override)
        except (TypeError, ValueError):
            pass
    return ctx.cfg.notify_threshold


def _int_or(value: Any, fallback: int) -> int:
    try:
        return int(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback


def effective_night(ctx: Context, state: dict | None = None) -> tuple[int, int, int]:
    """야간 억제 (시작시각, 종료시각, 최소점수). 마스터 패널(run_state)이 우선,
    없으면 .env(Config). 시각은 모두 운영 기준 시간대(APP_TZ_OFFSET, 기본 KST)."""
    st = state if state is not None else ctx.storage.get_run_state()
    return (
        _int_or(st.get("night_start_hour"), _int_or(ctx.cfg.night_start_hour, 23)),
        _int_or(st.get("night_end_hour"), _int_or(ctx.cfg.night_end_hour, 7)),
        _int_or(st.get("night_min_score"), _int_or(ctx.cfg.night_min_score, 80)),
    )


def queue_manual_notify(ctx: Context, article_id: str) -> bool:
    """수동 등록(URL) 기사를 알림 큐에 올리고 즉시 발송을 시도한다.

    자동 수집과 달리 사람이 직접 고른 등록이므로 is_backfill(6시간 경과)·
    부트스트랩 억제(suppressed)는 무시한다. 다만 점수 임계값 미만이면 보내지
    않는다(사용자 지정 2026-09-08). telegram_enabled·봇 /stop·야간 모드는
    send_notifications 가 그대로 적용한다(야간 저점수 기사는 큐에 남아 아침에 발송).
    반환: 큐에 새로 적재했으면 True.
    """
    cfg = ctx.cfg
    if not cfg.telegram_enabled or not cfg.telegram_chat_id:
        return False
    detail = ctx.storage.article_detail(article_id)
    if not detail or detail.get("status") != "active":
        return False
    state = ctx.storage.get_run_state()
    if str(state.get("notify_paused") or "0") not in ("0", "False", "false", ""):
        return False  # 봇 /stop 으로 일시중지됨

    score = int(detail.get("importance_score") or 0)
    always_kws = [k for k in jload(state.get("always_notify_keywords"), []) if k]
    try:
        hard_score = int(state.get("hard_notify_score") or 0)
    except (TypeError, ValueError):
        hard_score = 0
    # '항상 발송 키워드'는 제목 + 판정된 그룹사로만 본다(요약에 스친 언급 제외 — run_once 와 동일).
    probe = f"{detail.get('title', '')}\n{' '.join(jload(detail.get('group_companies'), []))}"
    excl_kws = [k for k in jload(state.get("exclude_notify_keywords"), []) if k]
    if _kw_hit_any(detail.get("title", ""), excl_kws):
        log.info("수동 등록 기사 제목에 '제외' 키워드 → 웹에만 노출: %s",
                 (detail.get("title") or "")[:40])
        return False
    is_priority = _kw_hit_any(probe, always_kws) or (hard_score > 0 and score >= hard_score)
    # 임계값도 위에서 읽어 둔 state 로 판정한다(effective_threshold 를 부르면 run_state 를 또 조회한다).
    try:
        threshold = int(state["notify_threshold"]) if state.get("notify_threshold") is not None \
            else cfg.notify_threshold
    except (TypeError, ValueError):
        threshold = cfg.notify_threshold
    if not (score >= threshold or is_priority):
        log.info("수동 등록 기사(점수 %d)가 임계값 미만이라 웹에만 노출: %s",
                 score, (detail.get("title") or "")[:40])
        return False

    if not ctx.storage.queue_notification(article_id, cfg.telegram_chat_id, "queued",
                                          1 if is_priority else 0):
        return False  # 이미 큐에 있음 — 중복 발송 방지
    # 즉시 발송은 다른 발송이 진행 중이 아닐 때만 시도한다. 락을 기다리며 API 응답을
    # 붙잡고 있으면(발송 배치가 수십 초 걸릴 수 있음) 등록 요청이 타임아웃된다.
    # 지금 못 보내도 큐에 남아 다음 파이프라인 주기에 나간다.
    if _SEND_LOCK.acquire(blocking=False):
        try:
            _send_notifications(ctx, limit=3)
        except Exception as exc:   # pragma: no cover
            log.warning("수동 등록 알림 즉시 발송 실패(큐에는 남아 다음 주기에 재시도): %s", exc)
        finally:
            _SEND_LOCK.release()
    else:
        log.info("다른 발송이 진행 중 — 수동 등록 기사는 큐에 두고 다음 주기에 발송합니다.")
    return True


def send_notifications(ctx: Context, limit: int = 20) -> int:
    # 파이프라인 루프·수동 등록이 겹쳐도 같은 큐 행을 두 번 보내지 않도록 직렬화한다.
    with _SEND_LOCK:
        return _send_notifications(ctx, limit)


def _send_notifications(ctx: Context, limit: int = 20) -> int:
    cfg = ctx.cfg
    if not cfg.telegram_enabled:
        return 0
    state = ctx.storage.get_run_state()
    if str(state.get("notify_paused") or "0") not in ("0", "False", "false", ""):
        return 0  # /stop 으로 일시중지됨

    # 텔레그램이 429 로 "N초 기다려라" 한 상태면 이번 회차는 통째로 건너뛴다.
    # 페널티 창 안에서 계속 두드리면 금지 시간이 오히려 늘어난다(2026-09-08 사고).
    wait = _flood_remaining()
    if wait > 0:
        log.warning("텔레그램 flood 대기 중 — %.0f초 후 재개 (이번 회차 발송 건너뜀)", wait)
        return 0

    pending = ctx.storage.pending_notifications(limit)
    if not pending:
        return 0
    def _is_priority(p: dict) -> bool:
        # priority 1(무조건 받을 키워드)·2(정책·통상 주제) 둘 다 임계값을 우회한다.
        return bool(p.get("priority"))

    def _kw_priority(p: dict) -> bool:
        # 야간 우회는 '무조건 받을 키워드'(priority 1)만 — 정책·통상(2)은 야간 억제를 지킨다.
        return int(p.get("priority") or 0) == 1

    # 큐 적재 후 마스터가 '제외 키워드'를 추가했을 수 있으니 발송 직전에 한 번 더 거른다.
    excl_kws = [k for k in jload(state.get("exclude_notify_keywords"), []) if k]
    if excl_kws:
        pending = [p for p in pending if not _kw_hit_any(p.get("title") or "", excl_kws)]

    # /threshold 로 조정한 임계값을 큐 단계에서 한 번 더 적용 (큐 적재는 .env 기준으로 됐을 수 있음)
    # 우선 기사(마스터 '항상 발송 키워드' 매칭)는 임계값·야간 게이트를 우회한다.
    th = effective_threshold(ctx)
    pending = [p for p in pending if int(p.get("importance_score") or 0) >= th or _is_priority(p)]
    if not pending:
        return 0

    # 야간 억제 — 마스터 패널(run_state) 값이 우선, 없으면 .env. 시각은 운영 기준(APP_TZ_OFFSET · 기본 KST).
    n_start, n_end, n_min = effective_night(ctx, state)
    # '무조건 받을 키워드' 기사를 야간에도 즉시 보낼지 (마스터 체크, 기본 켜짐)
    bypass_night = str(state.get("always_kw_bypass_night", 1) or 0) not in ("0", "False", "false", "")
    hour = now_local().hour
    if _in_night_window(hour, n_start, n_end):
        # 야간엔 중요도 n_min 이상만 즉시 발송. '무조건 받을 키워드' 기사는 위 체크가 켜져 있을 때만
        # 야간 우회(정책·통상 주제 매칭 기사는 야간엔 아침까지 대기). n_min=101 이면 사실상 전면 억제.
        pending = [p for p in pending
                   if int(p.get("importance_score") or 0) >= n_min
                   or (_kw_priority(p) and bypass_night)]
        if not pending:
            return 0

    url = TELEGRAM_API.format(token=cfg.telegram_bot_token)
    # 발송 이유(대시보드 '발송 로그') 재판정에 쓸 현재 설정값 — 한 번만 읽는다.
    always_kws = [k for k in jload(state.get("always_notify_keywords"), []) if k]
    try:
        hard_score = int(state.get("hard_notify_score") or 0)
    except (TypeError, ValueError):
        hard_score = 0
    kakao_on = kakao_enabled_now(ctx)   # 카카오 '나에게 보내기' 병행 여부

    def _send_one(row: dict) -> bool:
        """개별 카드 발송 — 요약·그룹사·점수가 다 들어간 전체 메시지."""
        _rate_gate()   # 채널 분당 한도 아래로 유지 (알림 큐 발송에만 적용, 봇 응답은 제외)
        reason = _notify_reason(row, always_kws, th, hard_score)
        ok, err = _telegram_send(ctx, url, clamp_message(format_message(row)),
                                 kind=reason, article_id=row.get("article_id"))
        if kakao_on:
            # 카카오도 텔레그램과 같은 요약·관점 카드로. 텔레그램 성패와 무관하게 시도한다.
            link = row.get("url_canonical") or row.get("url_original") or ""
            if link:
                _kakao_send(ctx, (row.get("title") or "")[:120], link,
                            article_id=row.get("article_id"), kind=reason, row=row)
        if ok:
            ctx.storage.mark_notification(row["id"], "sent", None)
        elif _notify_too_old(row):
            # 24시간 넘게 큐에 있었다 — 원인이 무엇이든 포기하고 failed 로 확정한다.
            ctx.storage.mark_notification(row["id"], "failed", err)
        elif _is_transient_tg_error(err):
            # rate limit·서버 오류·네트워크 오류 — 기다리면 풀린다. 재시도 횟수를 쓰지 않고
            # 큐에 그대로 두어 다음 회차(또는 flood 해제 후)에 다시 시도한다.
            ctx.storage.touch_notification(row["id"], err)
        else:
            # 진짜 실패(파싱 오류·chat not found·메시지 초과).
            # 3회까지 재시도. retry_count 가 3이 되면 pending 조회에서 빠진다.
            status = "queued" if int(row.get("retry_count") or 0) < 2 else "failed"
            ctx.storage.mark_notification(row["id"], status, err)
        time.sleep(RATE_LIMIT_SLEEP)
        return ok

    # 모든 기사를 개별 카드로 보낸다 — 다이제스트 묶음은 쓰지 않는다. (사용자 지정)
    # pending 은 pending_notifications 가 이미 중요도 높은 순으로 정렬해 준 상태다.
    prio_count = sum(1 for p in pending if p.get("priority"))
    if prio_count >= PRIORITY_FLOOD_WARN:
        log.warning("우선 기사가 한 회차에 %d건입니다. '항상 발송 키워드'가 너무 넓지 않은지"
                    " 확인하세요(예: '포스코'는 사실상 전체 기사와 매칭됩니다).", prio_count)

    sent = 0
    for row in pending:
        if _flood_remaining() > 0:
            # 방금 발송에서 429 를 받았다 — 남은 건은 큐에 두고 이번 회차를 끝낸다.
            log.warning("flood 진입 — 이번 회차 남은 %d건은 큐에 유지",
                        len(pending) - sent)
            break
        if _send_one(row):
            sent += 1
    return sent


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
    ok, err = _telegram_send(ctx, url, text, kind="연결 테스트")
    if ok:
        log.info("시험 메시지 발송 성공. 텔레그램에서 확인하세요.")
    else:
        raise SystemExit(f"발송 실패: {err}")


def _telegram_send(ctx: Context, url: str, text: str,
                   chat_id: str | None = None, preview: bool = True,
                   kind: str = "기타", article_id: str | None = None) -> tuple[bool, str | None]:
    """봇으로 메시지 1건을 보낸다. 성공·실패 모두 telegram_log 에 전문을 남긴다.

    kind = 발송 이유. 대시보드 '발송 로그' 에 그대로 표시된다. 큐 알림은
    발송 시점에 판정한 문구가 들어온다(예: "항상발송 키워드 '포스코퓨처엠'",
    "중요도 55 ≥ 임계값 30", "무조건 발송 점수 66 ≥ 65", "URL 등록 · …").
    그 외: '직접 전송'(카드 ↗) · '봇 응답' · '연결 테스트' · '기타'.
    """
    target = chat_id or ctx.cfg.telegram_chat_id
    ok, err = False, None
    try:
        resp = ctx.http.post(url, json={
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        })
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            ok = True
        else:
            err = str(data.get("description") or resp.status_code)
            if resp.status_code == 429:
                # 텔레그램이 준 대기 시간만큼 봇 전체 발송을 보류한다.
                retry_after = 0
                try:
                    retry_after = int((data.get("parameters") or {}).get("retry_after") or 0)
                except (TypeError, ValueError):
                    retry_after = 0
                if retry_after <= 0:
                    m = re.search(r"retry after (\d+)", err or "", re.IGNORECASE)
                    retry_after = int(m.group(1)) if m else 30
                _flood_arm(retry_after)
                log.warning("텔레그램 429 — %d초 발송 보류 (retry after)", retry_after)
                err = f"Too Many Requests: retry after {retry_after}"
    except Exception as exc:
        err = str(exc)
    # 로그 기록은 발송 성패에 영향을 주면 안 된다 — 어떤 예외도 삼킨다.
    try:
        ctx.storage.log_telegram({"chat_id": target, "kind": kind, "article_id": article_id,
                                  "text": text, "ok": ok, "error": err})
    except Exception as exc:   # pragma: no cover
        log.debug("telegram_log 기록 실패(무시): %s", exc)
    return ok, err


# =====================================================================
# 14a. 카카오톡 '나에게 보내기' (선택) — 텔레그램과 병행 발송
#      봇 토큰이 없고 사용자 OAuth 토큰이 필요하다. refresh_token 은 run_state 에
#      저장하고, access token 은 만료(약 12h) 시 자동 갱신한다.
# =====================================================================

KAKAO_AUTHORIZE = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN = "https://kauth.kakao.com/oauth/token"
KAKAO_MEMO_SEND = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_SCOPE = "talk_message"
_KAKAO_LOCK = threading.Lock()   # 토큰 갱신·발송 직렬화


def kakao_authorize_url(cfg: Config) -> str:
    q = urlencode({
        "client_id": cfg.kakao_rest_api_key,
        "redirect_uri": cfg.kakao_redirect_uri,
        "response_type": "code",
        "scope": KAKAO_SCOPE,
    })
    return f"{KAKAO_AUTHORIZE}?{q}"


def kakao_enabled_now(ctx: Context) -> bool:
    """앱 키 + refresh_token + 마스터 토글이 모두 켜져 있는가."""
    if not ctx.cfg.kakao_configured:
        return False
    st = ctx.storage.get_run_state()
    if not st.get("kakao_refresh_token"):
        return False
    return str(st.get("kakao_enabled") if st.get("kakao_enabled") is not None else 1) \
        not in ("0", "False", "false", "")


def _kakao_token_request(cfg: Config, http: HttpClient, data: dict) -> dict:
    if cfg.kakao_client_secret:
        data["client_secret"] = cfg.kakao_client_secret
    resp = http.post(KAKAO_TOKEN, data=data,
                     headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    body = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"{body.get('error')}: {body.get('error_description') or resp.status_code}")
    return body


def kakao_exchange_code(ctx: Context, code: str) -> dict:
    """인가 코드를 토큰으로 교환하고 refresh_token 을 저장한다. (kakao-auth CLI)"""
    body = _kakao_token_request(ctx.cfg, ctx.http, {
        "grant_type": "authorization_code",
        "client_id": ctx.cfg.kakao_rest_api_key,
        "redirect_uri": ctx.cfg.kakao_redirect_uri,
        "code": code.strip(),
    })
    exp = iso(now_utc() + timedelta(seconds=int(body.get("expires_in") or 0) - 60))
    ctx.storage.set_run_state({
        "kakao_refresh_token": body["refresh_token"],
        "kakao_access_token": body["access_token"],
        "kakao_token_expires_at": exp,
    })
    return body


def _kakao_access_token(ctx: Context) -> str | None:
    """유효한 access token 을 돌려준다. 만료됐으면 refresh 로 갱신한다."""
    st = ctx.storage.get_run_state()
    refresh = st.get("kakao_refresh_token")
    if not refresh:
        return None
    tok = st.get("kakao_access_token")
    exp = parse_dt(st.get("kakao_token_expires_at"))
    if tok and exp and exp > now_utc():
        return tok
    # 갱신
    body = _kakao_token_request(ctx.cfg, ctx.http, {
        "grant_type": "refresh_token",
        "client_id": ctx.cfg.kakao_rest_api_key,
        "refresh_token": refresh,
    })
    patch: dict[str, Any] = {
        "kakao_access_token": body["access_token"],
        "kakao_token_expires_at": iso(now_utc() + timedelta(seconds=int(body.get("expires_in") or 0) - 60)),
    }
    if body.get("refresh_token"):   # 남은 유효기간이 1개월 미만일 때만 새로 온다
        patch["kakao_refresh_token"] = body["refresh_token"]
    ctx.storage.set_run_state(patch)
    return body["access_token"]


KAKAO_TEXT_MAX = 195   # text 템플릿 200자 한도 (이모지·여유분 감안)


def _clip(s: str, n: int) -> str:
    """n 자로 자르고 잘렸으면 끝에 '…' 를 붙인다."""
    s = (s or "").strip()
    if n <= 0:
        return ""
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def _kakao_text_template(title: str, link: str) -> dict:
    """키·링크만 있을 때(연결 테스트·콜백)의 단순 text 템플릿."""
    return {
        "object_type": "text",
        "text": f"{title}\n{link}".strip()[:KAKAO_TEXT_MAX],
        "link": {"web_url": link, "mobile_web_url": link},
        "button_title": "원문 보기",
    }


def _kakao_text_from_row(row: dict, link: str) -> dict:
    """텔레그램 카드와 같은 서식(태그·제목·요약·포스코 관점)을 카카오 text 템플릿에
    담는다. text 는 200자 한도 — 링크는 본문이 아니라 link 필드·버튼으로 뺀다.
    format_message() 의 카카오판(200자 압축)."""
    score = int(row.get("importance_score") or 0)
    emoji = "🔴" if score >= 80 else "🟠"
    groups = normalize_group_list(jload(row.get("group_companies"), []))
    if not groups:
        probe = f"{row.get('title') or ''}\n{row.get('summary_text') or ''}"
        groups = normalize_group_list(detect_group_companies(probe))
    tag = groups[0] if groups else "포스코"

    body = f"{emoji} [{tag}] {(row.get('title') or '').strip()}"
    hdr = format_summary_header(row.get("press_name") or "", row.get("author") or "")
    summary = (row.get("summary_text") or "").strip()
    perspective = (row.get("perspective_text") or "").strip()

    seg = f"{hdr} {summary}".strip() if (hdr and summary) else summary
    if seg and KAKAO_TEXT_MAX - len(body) > 14:
        body += "\n\n" + _clip(seg, KAKAO_TEXT_MAX - len(body) - 2)
    if perspective and KAKAO_TEXT_MAX - len(body) > 20:
        body += "\n\n포스코 관점: " + _clip(perspective, KAKAO_TEXT_MAX - len(body) - 10)

    return {
        "object_type": "text",
        "text": body[:200],
        "link": {"web_url": link, "mobile_web_url": link},
        "button_title": "원문 보기",
    }


def _kakao_send(ctx: Context, title: str, link: str,
                article_id: str | None = None, kind: str = "카카오",
                row: dict | None = None) -> tuple[bool, str | None]:
    """카카오톡 '나와의 채팅'으로 기사를 보낸다. telegram_log 에 같이 기록한다.

    row 가 있으면 텔레그램과 같은 서식(태그·제목·요약·포스코 관점)을 text 템플릿에
    200자로 압축해 보낸다 — PC 카카오톡에서도 일반 메시지처럼 펼쳐 보인다.
    없으면(테스트·콜백) 제목+링크만 담는다.
    """
    template_obj = _kakao_text_from_row(row, link) if row else _kakao_text_template(title, link)
    log_text = (template_obj.get("text") or f"{title}\n{link}").strip()[:190]   # 발송 로그 표시용
    ok, err = False, None
    try:
        with _KAKAO_LOCK:
            access = _kakao_access_token(ctx)
            if not access:
                return False, "refresh_token 없음 (kakao-auth 필요)"
            resp = ctx.http.post(
                KAKAO_MEMO_SEND,
                data={"template_object": json.dumps(template_obj, ensure_ascii=False)},
                headers={"Authorization": f"Bearer {access}",
                         "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
            data = resp.json()
            if resp.status_code == 200 and data.get("result_code") == 0:
                ok = True
            else:
                err = f"{data.get('code')}: {data.get('msg') or resp.status_code}"
    except Exception as exc:
        err = str(exc)
    try:
        ctx.storage.log_telegram({"chat_id": "kakao:me", "kind": f"{kind}(카카오)",
                                  "article_id": article_id, "text": log_text, "ok": ok, "error": err})
    except Exception as exc:   # pragma: no cover
        log.debug("kakao 로그 기록 실패(무시): %s", exc)
    return ok, err


def cmd_kakao_auth(ctx: Context) -> None:
    """카카오 '나에게 보내기' 최초 토큰 발급 (1회)."""
    if not ctx.cfg.kakao_feature_enabled:
        raise SystemExit("카카오 기능이 비활성 상태입니다. .env 에 KAKAO_ENABLED=true 를 설정하세요.")
    if not ctx.cfg.kakao_configured:
        raise SystemExit("KAKAO_REST_API_KEY 또는 KAKAO_REDIRECT_URI 가 비어 있습니다.")
    redirect = ctx.cfg.kakao_redirect_uri
    auto = "/kakao/callback" in redirect or redirect.rstrip("/").endswith("/kakao")
    print("\n1) 아래 URL 을 브라우저에서 열고 카카오 로그인 + '카카오톡 메시지 전송' 동의:\n")
    print("   " + kakao_authorize_url(ctx.cfg))
    if auto:
        print(f"\n2) 동의하면 카카오가 {redirect} 로 보내고,"
              "\n   실행 중인 서버(run)가 code 를 자동으로 받아 토큰을 저장합니다."
              "\n   → 서버를 켜 둔 상태라면 이 명령은 더 실행할 필요가 없습니다."
              "\n   → 브라우저에 '카카오 연결 완료' 가 뜨면 끝. (kakao-test 로 확인)\n")
        print("   서버를 켜지 않았다면, 착지 페이지 주소창의 code= 값을 아래에 붙여넣으세요.")
    else:
        print(f"\n2) 동의 후 {redirect}?code=... 로 이동합니다 (에러 페이지여도 정상)."
              "\n   주소창의 code= 값을 복사해 아래에 붙여넣으세요.\n")
    code = input("   code = ").strip()
    if not code:
        raise SystemExit("code 가 비었습니다. (서버가 자동 처리했다면 정상입니다)")
    body = kakao_exchange_code(ctx, code)
    print(f"\n완료. refresh_token 저장됨 (유효 {int(body.get('refresh_token_expires_in', 0)) // 86400}일)."
          "  python backend/main.py kakao-test 로 시험 발송하세요.")


def cmd_kakao_test(ctx: Context) -> None:
    """카카오 '나에게 보내기' 시험 발송 1건."""
    ok, err = _kakao_send(
        ctx, "P-FM NEWS 카카오 연결 확인", "https://developers.kakao.com", kind="연결 테스트")
    if ok:
        log.info("시험 메시지 발송 성공. 카카오톡 '나와의 채팅'을 확인하세요.")
    else:
        raise SystemExit(f"발송 실패: {err}")


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
    ok, err = _telegram_send(ctx, url, text[:4000], chat_id=chat_id, preview=preview, kind="봇 응답")
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
# 14c. 주간 레포트 (월요일 아침 이메일)
#      최근 7일 기사를 섹션별로 모아 LLM 이 주간 SWOT·영향분석을 합성하고,
#      HTML 로 렌더해 Gmail SMTP 로 보낸다. 같은 HTML 을 웹 '주간동향' 탭이 재사용한다.
# =====================================================================

WEEKLY_PERIOD_DAYS = 7
WEEKLY_ARTICLES_PER_SECTION = 5

# (제목, 종류, 매칭기준)  종류: 'group'=그룹사 태그 / 'topic'=카테고리 태그
#   group  → LLM 주간 종합 SWOT
#   topic  → LLM '포스코 그룹 영향' 분석
WEEKLY_SECTIONS: list[tuple[str, str, str]] = [
    ("포스코퓨처엠", "group", "포스코퓨처엠"),
    ("포스코홀딩스", "group", "포스코홀딩스"),
    ("포스코", "group", "포스코"),
    ("포스코이앤씨", "group", "포스코이앤씨"),
    ("포스코인터내셔널", "group", "포스코인터내셔널"),
    ("정부/정책", "topic", "정부/정책"),
    ("글로벌 통상환경", "topic", "글로벌 통상환경"),
]


def weekly_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """집계 구간 — 발송 시점 기준 최근 7일. (사용자 지정)"""
    end = now or now_utc()
    return end - timedelta(days=WEEKLY_PERIOD_DAYS), end


def _weekly_pick(rows: list[dict], kind: str, key: str,
                 tags: dict[str, tuple] | None = None) -> list[dict]:
    """섹션 대상 기사를 최대 5건 고른다. rows 는 이미 기간·활성·대표만.

    그룹사 섹션은 '제목에 회사명이 있는 기사'를 먼저 올린다. 태그만 붙은
    시황·기관수급 기사(제목은 다른 종목)가 상위를 차지하는 것을 막는다.
    tags = 기사별 card_tags 결과(섹션 7개가 공유한다. 없으면 그때그때 계산).
    """
    aliases = [a.lower() for a in GROUP_COMPANIES.get(key, [key])] if kind == "group" else []
    hits = []
    for r in rows:
        cached = tags.get(r.get("id")) if tags else None
        groups, cats, _ = cached if cached else card_tags(r)
        if not ((key in groups) if kind == "group" else (key in cats)):
            continue
        title_hit = any(a in (r.get("title") or "").lower() for a in aliases)
        # 제목에 회사명이 없는데 카테고리가 시황뿐이면 = 코스피 종가표 같은 잡음. 건너뛴다.
        if kind == "group" and not title_hit and cats and set(cats) <= {"시장/주가"}:
            continue
        hits.append((title_hit, r))
    hits.sort(key=lambda t: (t[0], int(t[1].get("importance_score") or 0),
                             t[1].get("published_at") or ""), reverse=True)
    return [r for _, r in hits[:WEEKLY_ARTICLES_PER_SECTION]]


def _weekly_article_view(r: dict) -> dict:
    """레포트 payload 에 담을 기사 1건의 표시용 형태."""
    return {
        "title": r.get("title") or "",
        "url": r.get("url_canonical") or r.get("url_original") or r.get("url_source") or "",
        "press": r.get("press_name") or "",
        "published_at": r.get("published_at") or "",
        "score": int(r.get("importance_score") or 0),
        "summary": r.get("summary_text") or "",
    }


def build_weekly_report(ctx: Context) -> dict:
    """주간 레포트 payload 를 만든다. LLM 을 섹션당 최대 1회 호출한다(총 7회)."""
    start, end = weekly_window()
    rows = ctx.storage.list_articles(3000, 0, start, "")
    end_iso = iso(end)
    # 분석 완료(요약 있음) 기사만 — 요약 없는 카드는 레포트에서 빈 줄로 보인다.
    rows = [r for r in rows
            if (r.get("published_at") or "") <= end_iso and (r.get("summary_text") or "").strip()]

    # 태그는 기사당 한 번만 판정한다 — 섹션 7개가 각자 돌리면 같은 계산을 7배 한다.
    tags = {r["id"]: card_tags(r) for r in rows if r.get("id")}

    sections = []
    for label, kind, key in WEEKLY_SECTIONS:
        picked = _weekly_pick(rows, kind, key, tags)
        brief = ctx.llm.weekly_brief(
            "swot" if kind == "group" else "impact", label, picked) if picked else {}
        sections.append({
            "label": label,
            "kind": kind,
            "articles": [_weekly_article_view(r) for r in picked],
            "swot": {k: brief.get(k, "") for k in ("s", "w", "o", "t")} if kind == "group" else None,
            "impact": brief.get("impact", "") if kind == "topic" else None,
        })
    return {
        "period_start": iso(start),
        "period_end": iso(end),
        "generated_at": iso(now_utc()),
        "article_count": len(rows),
        "sections": sections,
    }


# SWOT 4분면 색상 — 이메일 클라이언트 호환을 위해 배경·글자색을 직접 지정한다.
_SWOT_QUADRANTS = {
    "s": ("강점", "Strengths",  "#0E6E4A", "#e7f4ee", "#20402f"),
    "w": ("약점", "Weaknesses", "#B4610C", "#fbf0e3", "#4a3520"),
    "o": ("기회", "Opportunities", "#1D63C4", "#e8f1fc", "#20344f"),
    "t": ("위협", "Threats",    "#C0392B", "#fbeae8", "#4a2723"),
}


def render_weekly_html(payload: dict) -> str:
    """이메일·웹 공용 HTML. 이메일 클라이언트 호환을 위해 인라인 스타일·표 레이아웃만 쓴다."""
    ps = (payload.get("period_start") or "")[:10]
    pe = (payload.get("period_end") or "")[:10]
    # 헤더(제목·기간)는 .wr-head 로 감싼다 — 웹 '주간동향' 탭은 CSS 로 숨기고
    # 자체 헤더를 쓴다(제목 중복 방지). 이메일에서는 그대로 보인다.
    out = [
        '<div class="weekly-report" style="max-width:760px;margin:0 auto;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Malgun Gothic\',\'맑은 고딕\',sans-serif;'
        'color:#1a1a1a;line-height:1.6;">',
        '<div class="wr-head">',
        f'<h1 style="font-size:20px;margin:0 0 4px;">📈 포스코 그룹 주간동향</h1>',
        f'<p style="color:#667085;font-size:13px;margin:0 0 24px;">집계 기간 {esc(ps)} ~ {esc(pe)}'
        f' · 대상 기사 {payload.get("article_count", 0)}건</p>',
        '</div>',
    ]
    for i, sec in enumerate(payload.get("sections", []), 1):
        out.append(
            f'<h2 style="font-size:16px;margin:30px 0 10px;padding-bottom:6px;'
            f'border-bottom:2px solid #16337A;color:#16337A;">'
            f'<span style="background:#16337A;color:#fff;border-radius:5px;'
            f'padding:1px 8px;font-size:13px;margin-right:7px;">{i}</span>{esc(sec["label"])}</h2>')
        arts = sec.get("articles") or []
        if not arts:
            out.append('<p style="color:#98a2b3;font-size:13px;">이번 주 해당 기사가 없습니다.</p>')
            continue
        out.append('<ol style="margin:0 0 14px;padding-left:20px;font-size:14px;">')
        for a in arts:
            d = (a.get("published_at") or "")[:10]
            out.append(
                f'<li style="margin-bottom:8px;">'
                f'<a href="{esc(a["url"])}" style="color:#16337A;text-decoration:none;font-weight:600;">'
                f'{esc(a["title"])}</a>'
                f'<br><span style="color:#98a2b3;font-size:12px;">{esc(a["press"])} · {esc(d)} · 중요도 {a["score"]}</span>'
                f'<br><span style="color:#475467;font-size:13px;">{esc(a["summary"])}</span></li>')
        out.append('</ol>')

        if sec.get("kind") == "group" and sec.get("swot"):
            sw = sec["swot"]
            out.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="8" '
                       'style="margin:10px 0 6px;">')
            for row_keys in (("s", "w"), ("o", "t")):
                out.append('<tr>')
                for k in row_keys:
                    name, en, accent, bg, ink = _SWOT_QUADRANTS[k]
                    out.append(
                        f'<td width="50%" valign="top" style="background:{bg};border-radius:8px;'
                        f'padding:12px 14px;">'
                        f'<div style="font-weight:700;color:{accent};font-size:12px;'
                        f'letter-spacing:.3px;margin-bottom:5px;">'
                        f'<span style="font-size:9px;vertical-align:2px;">●</span> {name} '
                        f'<span style="opacity:.55;font-weight:600;">{k.upper()} · {en}</span></div>'
                        f'<div style="color:{ink};font-size:13px;line-height:1.6;">'
                        f'{esc(sw.get(k) or "이번 주 해당 신호 없음")}</div></td>')
                out.append('</tr>')
            out.append('</table>')
        elif sec.get("kind") == "topic" and sec.get("impact"):
            out.append(
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'style="margin:10px 0 6px;">'
                '<tr><td style="background:#16337A;padding:9px 16px;border-radius:8px 8px 0 0;">'
                '<span style="color:#fff;font-weight:700;font-size:13px;">'
                '🎯 이 이슈가 포스코 그룹에 미치는 영향</span></td></tr>'
                '<tr><td style="background:#eef2fb;padding:14px 16px;border-radius:0 0 8px 8px;'
                'color:#2b3a55;font-size:13.5px;line-height:1.75;">'
                f'{esc(sec["impact"])}</td></tr></table>')
    out.append('<p style="color:#98a2b3;font-size:11px;margin-top:32px;border-top:1px solid #e4e7ec;'
               'padding-top:12px;">이 레포트는 수집·요약된 공개 기사와 AI 분석을 기반으로 자동 생성되었습니다. '
               '원문 저작권은 각 언론사에 있습니다.</p>')
    out.append('</div>')
    return "\n".join(out)


def _html_to_text(html: str) -> str:
    """이메일 plain 대체본 — 태그를 지운 최소 텍스트."""
    text = re.sub(r"<(br|/p|/h[12]|/li|/tr)[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return html_mod.unescape(text).strip()


def send_report_email(cfg: Config, subject: str, html_body: str,
                      to_list: Sequence[str]) -> tuple[bool, str | None]:
    """Gmail SMTP(STARTTLS)로 HTML 메일 1건을 보낸다. 표준 라이브러리만 사용."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formatdate, make_msgid

    to_list = [e for e in (to_list or []) if e]
    if not cfg.smtp_configured:
        return False, "SMTP 미설정 (.env 의 SMTP_USER · SMTP_APP_PASSWORD 확인)"
    if not to_list:
        return False, "수신자가 없습니다 (마스터 패널 또는 .env WEEKLY_REPORT_TO)"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_user
    msg["To"] = ", ".join(to_list)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.attach(MIMEText(_html_to_text(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.login(cfg.smtp_user, cfg.smtp_app_password)
            s.sendmail(cfg.smtp_user, to_list, msg.as_string())
        return True, None
    except Exception as exc:
        return False, str(exc)


def weekly_recipients(ctx: Context) -> list[str]:
    """수신자 = 마스터 패널 목록이 있으면 그것, 없으면 .env WEEKLY_REPORT_TO."""
    db = [e.strip() for e in jload(ctx.storage.get_run_state().get("weekly_report_to"), [])
          if str(e).strip()]
    return db or list(ctx.cfg.weekly_to)


def run_weekly_report(ctx: Context, send: bool = True) -> dict:
    """레포트를 생성·저장하고, send 면 이메일까지 보낸다. 저장된 레포트 dict 를 돌려준다."""
    log.info("주간 레포트 생성 시작 (최근 %d일)", WEEKLY_PERIOD_DAYS)
    payload = build_weekly_report(ctx)
    html = render_weekly_html(payload)
    report_id = new_id()
    pe = (payload["period_end"] or "")[:10]
    row = {
        "id": report_id,
        "period_start": payload["period_start"],
        "period_end": payload["period_end"],
        "generated_at": payload["generated_at"],
        "sent_at": None,
        "send_error": None,
        "payload": payload,
        "html": html,
    }
    ctx.storage.save_weekly_report(row)

    if send:
        to_list = weekly_recipients(ctx)
        ok, err = send_report_email(ctx.cfg, f"[P-FM] 포스코 그룹 주간동향 ({pe})", html, to_list)
        if ok:
            sent_at = iso(now_utc())
            ctx.storage.mark_weekly_sent(report_id, sent_at, None)
            row["sent_at"] = sent_at
            log.info("주간 레포트 발송 완료 → %s", ", ".join(to_list))
        else:
            ctx.storage.mark_weekly_sent(report_id, None, err)
            row["send_error"] = err
            log.warning("주간 레포트 발송 실패: %s", err)
    ctx.storage.set_run_state({"last_weekly_report_at": iso(now_utc())})
    return row


def maybe_run_weekly(ctx: Context) -> None:
    """파이프라인 매 회차 호출 — 월요일 지정 시각이 지났고 이번 주 미발송이면 실행."""
    cfg = ctx.cfg
    if not cfg.weekly_enabled:
        return
    cur = now_local()   # 서버 TZ 가 아니라 운영 기준(KST) 시각으로 판단
    if cur.weekday() != 0 or cur.hour < cfg.weekly_hour:
        return
    last = parse_dt(ctx.storage.get_run_state().get("last_weekly_report_at"))
    if last is not None and (now_utc() - last) < timedelta(days=6):
        return  # 이번 주 이미 처리됨
    try:
        run_weekly_report(ctx, send=cfg.smtp_configured)
    except Exception as exc:
        log.exception("주간 레포트 처리 중 오류: %s", exc)


# =====================================================================
# 15. API 서버 (PRD F6)
#     프론트엔드는 외부 API 와 DB 에 직접 접근하지 않고 여기만 호출한다.
# =====================================================================

PERIOD_HOURS = {"today": 24, "7d": 24 * 7, "30d": 24 * 30, "all": 0}
# 배열 필터(그룹사·카테고리)는 SQL 인덱스로 못 걸어 애플리케이션에서 처리한다.
# 스캔 대상 행수 상한은 SCAN_STORE_CAP(스토어 정의부 참조).

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


# ── 사이트 전체 잠금 (배포용) ────────────────────────────────────────
# 마스터 토큰은 관리 기능만 지킨다. 배포하면 URL 만 알아도 기사 목록·URL 등록·
# 텔레그램 직접 전송 API 를 누구나 쓸 수 있으므로, 그 앞에 세션 관문을 하나 둔다.
WEB_COOKIE = "pfm_web"
WEB_SESSION_DAYS = 30
# 잠금 대상에서 빼는 경로 — 로그인 자체, 헬스체크, 카카오 OAuth 착지점.
WEB_PUBLIC_PATHS = frozenset({"/api/web/login", "/api/web/logout", "/api/web/status",
                              "/healthz", "/kakao/callback", "/kakao"})
_WEB_PW_CACHE: dict[str, Any] = {"at": 0.0, "pw": ""}
WEB_PW_TTL_SEC = 60.0   # run_state 조회가 요청마다 DB 를 때리지 않게


def web_session_token(pw: str) -> str:
    """비밀번호에서 결정적으로 파생한 세션 토큰.

    서버에 세션을 저장하지 않아 재시작해도 로그인이 풀리지 않고,
    비밀번호를 바꾸면 기존 쿠키가 자동으로 무효가 된다.
    """
    return hmac.new(pw.encode("utf-8"), b"pfm-web-session-v1", hashlib.sha256).hexdigest()


# URL 수동 등록은 요청 1건 = LLM 호출 1건(과금)이라 시간당 상한을 둔다.
_analyze_calls: list[float] = []
ANALYZE_MAX_PER_HOUR = 30   # 정상 사용(하루 몇 건)에는 걸리지 않는 넉넉한 상한


def _rate_ok(bucket: list[float], limit: int, window: float = 3600.0) -> bool:
    """window 초 안에서 limit 회까지 허용. 통과하면 호출 시각을 기록한다."""
    now = time.time()
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


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
    cats = jload(row.get("categories"), [])
    if not groups:
        # 제목·요약·키워드에서 계열사를 찾는다. 없으면 그룹사 태그를 붙이지 않는다 —
        # 예전에는 '포스코' 를 폴백으로 씌웠지만, 본문에 포스코가 시황·티커 목록으로
        # 스치듯 나온 기사(홍콩증시·충북경제 등)까지 포스코 태그를 달아 오해를 샀다.
        # (사용자 지적 2회) 카테고리 태그로 충분하고, 로고 썸네일은 프런트가 알아서 채운다.
        probe = " ".join([
            row.get("title") or "", row.get("summary_text") or "",
            " ".join(jload(row.get("keywords"), [])),
        ])
        groups = normalize_group_list(detect_group_companies(probe))
    categories = dedupe_chips(cats, exclude=groups)
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
        "manual": (row.get("source_type") == "manual"),   # 사용자가 URL 로 직접 등록한 기사
        "swot": swot,
    }


def _split_multi(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


# 태그 계산 결과 메모 — 같은 기사를 매 스캔마다 다시 판정하지 않는다.
# 키에 analyzed_at·press_name 을 넣어 재분석·언론사명 정정이 반영되게 한다.
# (그룹사가 빈 기사 46%는 card_tags 의 폴백 판정을 매번 다시 돌리고 있었다.)
_TAG_MEMO: dict[str, tuple] = {}
TAG_MEMO_MAX = 60000


def tag_row(row: dict) -> dict:
    """행 + 사전계산된 필터 태그(표시용 g/c/p, 정규화 키 gk/ck/pk).

    카드로 변환하지 않고 card_tags 만 계산한다 — 전체 스캔 비용을 줄인다.
    /api/articles 의 스캔 캐시와 apply_filters 가 같은 모양을 쓴다.
    """
    # id 가 없는 행(테스트 픽스처·임시 dict)은 메모하지 않는다 — 키가 겹쳐 남의 태그를 받는다.
    aid = row.get("id")
    key = f"{aid}\x00{row.get('analyzed_at')}\x00{row.get('press_name')}" if aid else None
    memo = _TAG_MEMO.get(key) if key else None
    if memo is None:
        g, c, pname = card_tags(row)
        memo = (g, c, pname, {normalize_chip(x) for x in g},
                {normalize_chip(x) for x in c}, normalize_chip(pname) if pname else "")
        if key:
            if len(_TAG_MEMO) >= TAG_MEMO_MAX:
                _TAG_MEMO.clear()   # 단순 비우기 — 재계산 비용이 낮고 캐시는 금방 다시 찬다
            _TAG_MEMO[key] = memo
    g, c, pname, gk, ck, pk = memo
    return {"row": row, "g": g, "c": c, "p": pname, "gk": gk, "ck": ck, "pk": pk}


def filter_tagged(tagged: list[dict], g_keys: set[str], c_keys: set[str],
                  p_keys: set[str]) -> list[dict]:
    """같은 그룹 안은 OR, 다른 그룹 사이는 AND. (PRD F6.1a)

    필터 규칙의 **유일한 구현**이다. /api/articles 도 apply_filters 도 이것만 부른다
    — 두 벌로 두면 한쪽만 고쳤을 때 화면과 테스트가 조용히 갈라진다.
    """
    if not (g_keys or c_keys or p_keys):
        return list(tagged)
    return [t for t in tagged
            if (not g_keys or (g_keys & t["gk"]))
            and (not c_keys or (c_keys & t["ck"]))
            and (not p_keys or t["pk"] in p_keys)]


def apply_filters(rows: list[dict], groups: list[str], cats: list[str], presses: list[str]) -> list[dict]:
    """행 목록을 필터해 행 목록으로 돌려준다. 규칙은 filter_tagged 하나만 쓴다."""
    def keys(values: Sequence[str]) -> set[str]:
        return {normalize_chip(x) for x in values}
    tagged = filter_tagged([tag_row(r) for r in rows], keys(groups), keys(cats), keys(presses))
    return [t["row"] for t in tagged]


# ── 목록 스캔 스토어 ────────────────────────────────────────────────
# /api/articles·/api/filters 는 활성·분석완료 기사 수천~수만 행을 훑어 필터한다.
# 요청마다 DB 를 다시 읽으면 Supabase 이관 후 무료 대역폭(5GB/월)을 금방 넘긴다.
# 태그가 붙은 행을 메모리에 두고, 파이프라인이 사이클마다 '바뀐 것만' 델타로 갱신한다.
_SCAN_STORE: dict[str, Any] = {"by_id": {}, "cursor": "", "full_at": 0.0, "delta_at": 0.0}
_SCAN_STORE_LOCK = threading.Lock()
SCAN_STORE_CAP = 40000            # 메모리 상한(≈100일치). 넘으면 발행일 오래된 것부터 버린다.
SCAN_FULL_RELOAD_SEC = 20 * 3600  # 삭제·상태변경 반영: 하루 1회 전체 재적재
SCAN_DELTA_MIN_SEC = 60           # 델타 조회 최소 간격 — API 요청마다 DB 치지 않는다
                                 # (파이프라인이 사이클마다 갱신하므로 이 정도 지연은 무해)


def _row_ts(r: dict) -> str:
    return max(r.get("collected_at") or "", r.get("analyzed_at") or "")


def _store_put(st: dict, r: dict) -> None:
    if r.get("status") in (None, "active") and r.get("analyzed_at"):
        st["by_id"][r["id"]] = tag_row(r)
    else:   # 보관·삭제·미분석으로 바뀐 행은 스토어에서 뺀다
        st["by_id"].pop(r["id"], None)


def refresh_scan_store(storage: "Storage", full: bool = False) -> list[dict]:
    """태그가 붙은 활성·분석완료 행 목록. 최초/하루 1회만 전체, 그 외엔 델타만 읽는다."""
    now = time.monotonic()
    with _SCAN_STORE_LOCK:
        st = _SCAN_STORE
        if full or not st["by_id"] or (now - st["full_at"]) > SCAN_FULL_RELOAD_SEC:
            rows = storage.scan_articles(SCAN_STORE_CAP, None, "")
            st["by_id"] = {r["id"]: tag_row(r) for r in rows}
            st["cursor"] = max((_row_ts(r) for r in rows), default="")
            st["full_at"] = now
            st["delta_at"] = now
        elif st["cursor"] and now - st["delta_at"] >= SCAN_DELTA_MIN_SEC:
            st["delta_at"] = now
            fresh = storage.changed_articles_since(st["cursor"])
            for r in fresh:
                _store_put(st, r)
            if fresh:
                st["cursor"] = max([st["cursor"]] + [_row_ts(r) for r in fresh])
            if len(st["by_id"]) > SCAN_STORE_CAP * 1.15:
                keep = sorted(st["by_id"].values(),
                              key=lambda t: t["row"].get("published_at") or "", reverse=True)
                st["by_id"] = {t["row"]["id"]: t for t in keep[:SCAN_STORE_CAP]}
        return list(st["by_id"].values())


def scan_store_upsert(storage: "Storage", article_id: str) -> None:
    """수동 등록처럼 즉시 반영이 필요한 한 건만 스토어에 넣거나 뺀다."""
    row = storage.article_detail(article_id)
    with _SCAN_STORE_LOCK:
        if row:
            _store_put(_SCAN_STORE, row)
        else:
            _SCAN_STORE["by_id"].pop(article_id, None)


def create_app(ctx: Context):
    fastapi = _import("fastapi", "fastapi")
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = fastapi.FastAPI(title="P-FM NEWS API", docs_url="/api/docs")
    # 프런트는 같은 오리진에서 서빙되므로 CORS 가 필요 없다. 다른 사이트에서
    # 이 API 를 부르게 두면 세션을 가진 이용자를 통해 발송·분석이 트리거될 수 있다.
    # ALLOWED_ORIGINS 에 콤마로 적은 주소만 허용하고, 비우면 교차 오리진을 막는다.
    _origins = [o.strip() for o in get_env("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware, allow_origins=_origins, allow_credentials=True,
        allow_methods=["GET", "POST"], allow_headers=["*"],
    )

    def _web_secret() -> str:
        """잠금 기준이 되는 비밀 재료. 빈 문자열이면 잠금이 꺼진다(로컬 개발).

        잠금은 '명시적으로 설정했을 때'만 켠다 — .env WEB_PASSWORD 가 있거나,
        마스터 패널에서 웹 비밀번호를 지정(run_state.web_password)했을 때.
        예전에 남아 있을 수 있는 web_pw_hash 만으로는 켜지 않는다. 기억 못 하는
        비밀번호로 화면이 잠겨 버리는 사고를 막기 위해서다.

        검증 재료로는 해시(web_pw_hash)가 있으면 그것을 쓴다. 해시 문자열은
        비밀번호가 바뀌면 함께 바뀌므로 세션 토큰도 저절로 무효가 된다.
        """
        now = time.monotonic()
        if now - float(_WEB_PW_CACHE["at"]) < WEB_PW_TTL_SEC:
            return str(_WEB_PW_CACHE["pw"])
        secret = ""
        try:
            st = ctx.storage.get_run_state()
            if (st.get("web_password") or "").strip():        # 패널에서 지정함 = 잠금 on
                secret = (st.get("web_pw_hash") or "").strip() or st["web_password"].strip()
        except Exception as exc:   # DB 장애 때 사이트를 통째로 잠가 버리지 않는다
            log.debug("웹 비밀번호 조회 실패(.env 값 사용): %s", exc)
        if not secret and ctx.cfg.web_password:               # .env 로 지정함 = 잠금 on
            secret = ctx.cfg.web_password
        _WEB_PW_CACHE.update(at=now, pw=secret)
        return secret

    def _web_password() -> str:
        """잠금이 켜져 있는가(빈 문자열이면 꺼짐). 이름은 게이트 가독성 때문에 유지."""
        return _web_secret()

    def _web_verify(pw: str) -> bool:
        """입력한 비밀번호가 맞는가. 해시가 있으면 해시로, 없으면 평문과 비교."""
        secret = _web_secret()
        if not secret or not isinstance(pw, str) or not pw:
            return False
        if secret.count("$") >= 3:      # pbkdf2$iters$salt$hash 형태 = 저장된 해시
            return verify_password(pw, secret)
        return hmac.compare_digest(pw, secret)

    def _web_session_ok(token: str) -> bool:
        secret = _web_secret()
        return bool(token) and bool(secret) and hmac.compare_digest(
            token, web_session_token(secret))

    def _web_login_page() -> str:
        """잠금 상태에서 화면 대신 내주는 로그인 페이지. 외부 파일 없이 자립한다."""
        return (
            "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>P-FM NEWS</title><style>"
            "body{font-family:system-ui,'Malgun Gothic',sans-serif;background:#0E2841;margin:0;"
            "display:flex;min-height:100vh;align-items:center;justify-content:center}"
            "form{background:#fff;padding:36px 40px;border-radius:14px;width:300px;"
            "box-shadow:0 8px 32px rgba(0,0,0,.25)}"
            "h1{margin:0 0 6px;font-size:19px;color:#0E2841}"
            "p{margin:0 0 18px;font-size:13px;color:#667}"
            "input{width:100%;box-sizing:border-box;padding:11px;font-size:14px;"
            "border:1px solid #ccd;border-radius:7px}"
            "button{width:100%;margin-top:12px;padding:11px;font-size:14px;font-weight:600;"
            "background:#156082;color:#fff;border:0;border-radius:7px;cursor:pointer}"
            "button:disabled{opacity:.6;cursor:default}"
            ".e{margin-top:10px;font-size:12px;color:#c0392b;min-height:16px}"
            "</style></head><body><form id='f' autocomplete='on'>"
            "<h1>P-FM NEWS</h1><p>사내 뉴스 인텔리전스 · 접속 비밀번호를 입력하세요.</p>"
            "<input id='pw' type='password' name='password' placeholder='비밀번호' "
            "autocomplete='current-password' autofocus required>"
            "<button id='b' type='submit'>들어가기</button>"
            "<div class='e' id='e'></div></form><script>"
            "var f=document.getElementById('f'),b=document.getElementById('b'),"
            "e=document.getElementById('e');"
            "f.addEventListener('submit',async function(ev){ev.preventDefault();"
            "b.disabled=true;e.textContent='';try{"
            "var r=await fetch('/api/web/login',{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({password:document.getElementById('pw').value})});"
            "var d=await r.json();"
            "if(d.ok){location.replace('/');return;}"
            "e.textContent=d.error||'로그인에 실패했습니다.';}"
            "catch(err){e.textContent='서버에 연결하지 못했습니다.';}"
            "b.disabled=false;});"
            "</script></body></html>"
        )

    @app.middleware("http")
    async def _web_gate(request, call_next):
        """사이트 전체 잠금 — WEB_PASSWORD(또는 마스터 패널의 웹 비밀번호)가 있을 때만 동작.

        비밀번호가 비어 있으면(로컬 개발) 아무 것도 막지 않는다. 설정돼 있으면
        쿠키에 든 세션 토큰이 맞아야 화면·API 를 내준다 — 배포 후 URL 만 알면
        누구나 기사·발송 API 를 쓸 수 있는 상태를 막는 것이 목적이다.
        """
        if _web_password() and request.url.path not in WEB_PUBLIC_PATHS:
            if not _web_session_ok(request.cookies.get(WEB_COOKIE, "")):
                accept = request.headers.get("accept", "")
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"ok": False, "error": "로그인이 필요합니다."},
                                        status_code=401)
                if "text/html" in accept or accept in ("", "*/*"):
                    return HTMLResponse(_web_login_page(), status_code=401)
                return JSONResponse({"ok": False, "error": "로그인이 필요합니다."}, status_code=401)
        return await call_next(request)

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

    # ── 스캔 스토어 + 결과 캐시 ────────────────────────────────────
    # 태그가 붙은 행은 모듈 전역 _SCAN_STORE 에 있고 파이프라인이 델타로 갱신한다.
    # 여기서는 (기간, 검색어)로 걸러낸 결과만 짧게 캐시해 재필터 CPU 를 줄인다.
    _scan_cache: dict[str, dict] = {}
    _filters_cache: dict[str, Any] = {"at": 0.0, "data": None}
    SCAN_TTL_SEC = 20
    FILTERS_TTL_SEC = 30

    def _scan_tagged(period: str, query: str) -> list[dict]:
        """스토어에서 (기간·검색어) 조건에 맞는 태그된 행만 골라 돌려준다."""
        key = f"{period}\x00{query}"
        now = time.monotonic()
        ent = _scan_cache.get(key)
        if ent and now - ent["at"] < SCAN_TTL_SEC:
            return ent["data"]
        rows = refresh_scan_store(ctx.storage)
        hours = PERIOD_HOURS.get(period, 0)
        cut = iso(now_utc() - timedelta(hours=hours)) if hours else ""
        ql = query.lower().strip()
        data = [
            t for t in rows
            if (not cut or (t["row"].get("published_at") or "") >= cut)
            and (not ql or ql in " ".join((
                t["row"].get("title") or "", t["row"].get("summary_text") or "",
                t["row"].get("author") or "", t["row"].get("press_name") or "")).lower())
        ]
        _scan_cache[key] = {"at": now, "data": data}
        if len(_scan_cache) > 24:
            for k in sorted(_scan_cache, key=lambda k: _scan_cache[k]["at"])[:12]:
                _scan_cache.pop(k, None)
        return data

    def _bust_scan_cache(article_id: str | None = None) -> None:
        """수동 등록·draft 확정 등 즉시 반영이 필요할 때. 결과 캐시를 비우고
        해당 기사만 스토어에 반영(id 없으면 다음 요청에서 전체 재적재)."""
        _scan_cache.clear()
        _filters_cache.update(at=0.0, data=None)
        if article_id:
            scan_store_upsert(ctx.storage, article_id)
        else:
            _SCAN_STORE["full_at"] = 0.0

    @app.get("/api/articles")
    def api_articles(group: str = "", cat: str = "", press: str = "",
                     period: str = "all", q: str = "", page: int = 1, size: int = 20,
                     sort: str = "recent"):
        page = max(1, page)
        size = int(clamp(size, 1, 100))
        tagged = _scan_tagged(period, q)
        # 같은 그룹 안 OR, 다른 그룹 사이 AND. (PRD F6.1a — filter_tagged 가 유일한 구현)
        matched = filter_tagged(tagged,
                                {normalize_chip(x) for x in _split_multi(group)},
                                {normalize_chip(x) for x in _split_multi(cat)},
                                {normalize_chip(x) for x in _split_multi(press)})
        # 정렬 — 기본(recent)은 SQL 이 이미 발행일 최신순으로 준 순서를 그대로 쓴다.
        # 'score' 는 사용자가 직접 등록한 기사·중요도 높은 기사를 위로 올린다.
        if sort == "score":
            matched = sorted(matched, key=lambda t: (
                1 if t["row"].get("source_type") == "manual" else 0,
                int(t["row"].get("importance_score") or 0),
                t["row"].get("published_at") or "",
            ), reverse=True)
        start = (page - 1) * size
        # 스캔은 경량 컬럼만 읽었으므로, 화면에 보일 한 페이지만 카드용 전체 컬럼으로
        # 다시 읽어 만든다(요약·관점·SWOT 근거문). 스캔 결과가 없으면 조회도 없다.
        page_ids = [t["row"]["id"] for t in matched[start:start + size]]
        return JSONResponse({
            "total": len(matched),
            "page": page,
            "size": size,
            "items": [build_card(r) for r in ctx.storage.card_details(page_ids)],
        })

    # 필터 칩 고정 순서 — 목록에 없는 값은 뒤에 원래 순서로 붙는다.
    GROUP_ORDER = ["포스코퓨처엠", "포스코홀딩스", "포스코", "포스코DX", "포스코이앤씨"]
    CATEGORY_ORDER = ["양극재", "음극재", "배터리·이차전지", "산업", "시장/주가",
                      "정부/정책", "글로벌 통상환경", PEOPLE_NEWS_CATEGORY]

    def _ordered(values: list[str], priority: list[str]) -> list[str]:
        uniq = dedupe_chips(values)
        return [p for p in priority if p in uniq] + [v for v in uniq if v not in priority]

    @app.get("/api/filters")
    def api_filters():
        """현재 데이터에 실제로 존재하는 필터 값만 돌려준다."""
        now = time.monotonic()
        if _filters_cache["data"] and now - _filters_cache["at"] < FILTERS_TTL_SEC:
            return JSONResponse(_filters_cache["data"])

        groups: list[str] = []
        cats: list[str] = []
        presses: list[str] = []
        for t in _scan_tagged("all", ""):   # /api/articles 와 스캔·태그 캐시를 공유한다
            groups += t["g"]
            cats += t["c"]
            if t["p"]:
                presses.append(t["p"])
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

    def _detail_card(article_id: str | None, fallback_title: str, fallback_url: str) -> dict:
        """상세 모달용 카드. 기사가 있으면 목록과 똑같은 포토카드, 없으면 최소 정보만."""
        row = ctx.storage.article_detail(article_id) if article_id else None
        if row:
            return build_card(row)
        return {"id": article_id, "title": fallback_title, "url": fallback_url or "",
                "summary_text": "", "keywords": [], "group_companies": [], "categories": [],
                "importance_score": 0, "swot": None, "published_at": None, "thumbnail_url": ""}

    @app.get("/api/stats/notify-failed")
    def api_stats_notify_failed(limit: int = 50):
        """대시보드 '발송 실패' 카드를 눌렀을 때 — 실패한 기사 포토카드 + 실패 원인."""
        rows = ctx.storage.failed_notifications(max(1, min(limit, 200)))
        items = []
        for r in rows:
            card = _detail_card(r.get("article_id"), r.get("title") or "(기사 정보 없음)",
                                r.get("url_canonical") or r.get("url_original") or "")
            card["fail"] = {
                "error": r.get("error") or "(원인이 기록되지 않았습니다)",
                "retry_count": r.get("retry_count"),
                "created_at": r.get("created_at"),
                "channel": r.get("channel"),
                # queued 인데 재시도 한도를 넘긴 건 = 실패로 세어지지도, 재시도되지도 않던 것
                "stuck": r.get("status") == "queued",
            }
            items.append(card)
        return JSONResponse({"items": items})

    @app.get("/api/stats/analysis-pending")
    def api_stats_analysis_pending(limit: int = 50):
        """대시보드 '분석 대기' 카드를 눌렀을 때 — 본문은 받았는데 분석이 안 끝난 기사."""
        rows = ctx.storage.unanalyzed_articles(max(1, min(limit, 200)))
        items = []
        for r in rows:
            card = _detail_card(r.get("article_id"), r.get("title") or "(제목 없음)",
                                r.get("url_canonical") or r.get("url_original") or "")
            card["pending"] = {
                "collected_at": r.get("collected_at"),
                "fetched_at": r.get("fetched_at"),
                "summary_source": r.get("summary_source"),
                "body_len": r.get("body_len"),
            }
            items.append(card)
        return JSONResponse({"items": items})

    @app.get("/api/stats/telegram-log")
    def api_stats_telegram_log(limit: int = 100):
        """대시보드 '발송 로그' 카드 — 봇으로 실제 나간 메시지 전문(알림·봇응답·테스트)."""
        rows = ctx.storage.recent_telegram_logs(max(1, min(limit, 300)))
        return JSONResponse({"items": [{
            "created_at": r.get("created_at"),
            "chat_id": r.get("chat_id"),
            "kind": r.get("kind") or "기타",
            "ok": bool(r.get("ok")),
            "error": r.get("error"),
            "text": r.get("text") or "",
            "article_id": r.get("article_id"),
            "url": r.get("url_canonical") or r.get("url_original"),
        } for r in rows]})

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
            return _telegram_send(ctx, api_url, clamp_message(format_message(row)),
                                  kind="직접 전송", article_id=article_id)

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

    def _kakao_cb_page(title: str, body: str, ok: bool):
        color = "#156082" if ok else "#c0392b"
        html = (
            "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>"
            "body{font-family:system-ui,'Malgun Gothic',sans-serif;background:#f4f6f8;"
            "margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}"
            ".box{background:#fff;max-width:520px;padding:36px 40px;border-radius:14px;"
            "box-shadow:0 6px 24px rgba(0,0,0,.08);line-height:1.6}"
            f".box h1{{margin:0 0 14px;font-size:20px;color:{color}}}"
            ".box p{margin:8px 0;color:#333;font-size:14px}"
            ".box code{background:#eef2f5;padding:2px 6px;border-radius:4px;font-size:13px}"
            "</style></head><body><div class='box'>"
            f"<h1>{title}</h1>{body}</div></body></html>"
        )
        return HTMLResponse(html, status_code=200 if ok else 400)

    async def _kakao_callback(code: str = "", error: str = "",
                              error_description: str = ""):
        """카카오 OAuth 리다이렉트 착지점.

        authorize 동의 후 카카오가 ?code= 를 붙여 이리로 보낸다.
        서버가 그 code 를 토큰으로 교환하고 refresh_token 을 run_state 에 저장한다.
        (기존 수동 복사·붙여넣기 kakao-auth 를 대체한다.)
        """
        if error:
            return _kakao_cb_page(
                "카카오 연결 실패",
                f"<p>카카오가 오류를 반환했습니다.</p><p><code>{error}</code>"
                f"<br>{error_description or ''}</p>"
                "<p>동의 창에서 <b>카카오톡 메시지 전송</b> 항목에 동의했는지 확인하세요.</p>",
                ok=False)
        if not code:
            return _kakao_cb_page(
                "잘못된 접근",
                "<p>이 주소는 카카오 로그인 동의 후 자동으로 이동되는 착지점입니다.</p>"
                "<p>먼저 <code>python backend/main.py kakao-auth</code> 가 안내하는 "
                "URL 을 브라우저에서 여세요.</p>",
                ok=False)
        import anyio
        try:
            body = await anyio.to_thread.run_sync(lambda: kakao_exchange_code(ctx, code))
        except KeyError as exc:   # 토큰 응답에 refresh_token/access_token 이 없음
            return _kakao_cb_page(
                "카카오 연결 실패",
                f"<p>토큰 응답에 필요한 값이 없습니다: <code>{exc}</code></p>"
                "<p>인가 코드가 만료되었을 수 있습니다(10분). 처음부터 다시 시도하세요.</p>",
                ok=False)
        except Exception as exc:
            msg = str(exc)
            hint = ""
            if "kakao_refresh_token" in msg or "column" in msg.lower() \
                    or "PGRST" in msg or "schema cache" in msg.lower():
                hint = ("<p><b>원인:</b> 저장소(run_state)에 카카오 컬럼이 아직 없습니다."
                        " Supabase SQL Editor 에서 아래를 1회 실행하세요.</p>"
                        "<p><code>alter table run_state "
                        "add column if not exists kakao_enabled boolean not null default true, "
                        "add column if not exists kakao_refresh_token text, "
                        "add column if not exists kakao_access_token text, "
                        "add column if not exists kakao_token_expires_at timestamptz;</code></p>")
            return _kakao_cb_page(
                "카카오 연결 실패",
                f"<p>토큰 교환 중 오류: <code>{msg}</code></p>{hint}",
                ok=False)
        days = int(body.get("refresh_token_expires_in", 0)) // 86400
        return _kakao_cb_page(
            "카카오 연결 완료",
            f"<p>refresh_token 을 저장했습니다. (유효 약 <b>{days}일</b>)</p>"
            "<p>이제 텔레그램 알림이 나갈 때 카카오톡 <b>나와의 채팅</b>으로도 "
            "기사 제목·링크가 전송됩니다.</p>"
            "<p>시험 발송: <code>python backend/main.py kakao-test</code></p>"
            "<p>이 창은 닫아도 됩니다.</p>",
            ok=True)

    app.add_api_route("/kakao/callback", _kakao_callback, methods=["GET"])
    app.add_api_route("/kakao", _kakao_callback, methods=["GET"])  # 구 리다이렉트 URI 호환

    # ── 사이트 접속 로그인 (WEB_PASSWORD 가 설정됐을 때만 의미 있음) ──
    @app.get("/healthz")
    def healthz():
        """플랫폼 프로브·업타임 핑용. DB 를 건드리지 않는 가벼운 응답."""
        return JSONResponse({"ok": True, "service": "pfm-news"})

    # 주의: 이 파일은 from __future__ import annotations 를 쓰므로 타입 주석이 문자열이다.
    # create_app 지역의 fastapi 는 모듈 전역이 아니라 FastAPI 가 주석을 풀지 못한다.
    # 그래서 Request 를 주입받지 않고 Cookie/Header 기본값으로만 값을 받는다.
    @app.get("/api/web/status")
    def api_web_status(pfm_web: str = fastapi.Cookie(default="")):
        """잠금이 켜져 있는지 / 지금 세션이 유효한지."""
        return JSONResponse({"ok": True, "locked": bool(_web_password()),
                             "authed": _web_session_ok(pfm_web)})

    @app.post("/api/web/login")
    async def api_web_login(payload: dict,
                            x_forwarded_proto: str = fastapi.Header(default="")):
        """접속 비밀번호 확인 후 세션 쿠키를 심는다. 로그인 페이지가 fetch 로 호출한다."""
        pw = (payload or {}).get("password", "")
        secret = _web_secret()
        if not secret:
            return JSONResponse({"ok": True, "locked": False})   # 잠금이 꺼져 있음
        if not _web_verify(pw):
            return JSONResponse({"ok": False, "error": "비밀번호가 올바르지 않습니다."},
                                status_code=401)
        resp = JSONResponse({"ok": True})
        # 프록시가 "https" 또는 "https,http" 처럼 넘기므로 부분 일치로 본다.
        # HttpOnly + SameSite=Lax 로 자바스크립트 탈취와 교차 사이트 POST 를 막는다.
        resp.set_cookie(WEB_COOKIE, web_session_token(secret),
                        max_age=WEB_SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure="https" in x_forwarded_proto.lower(), path="/")
        return resp

    @app.post("/api/web/logout")
    def api_web_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(WEB_COOKIE, path="/")
        return resp

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
        n_start, n_end, n_min = effective_night(ctx, st)
        return JSONResponse({
            "ok": True,
            "telegram_enabled": str(st.get("notify_paused") or "0") in ("0", "False", "false", ""),
            # 카카오 발송은 기본 비활성 — 마스터 패널 UI 에서는 노출하지 않는다.
            # 다시 쓰려면 .env KAKAO_ENABLED=true + kakao-auth (ARCHITECTURE 04절).
            "threshold": effective_threshold(ctx),
            "night_start": n_start, "night_end": n_end, "night_min_score": n_min,
            "night_tz": f"UTC{ctx.cfg.tz_offset_hours:+d}",
            "recommended_min": RECOMMENDED_MIN_SCORE,
            "keywords": jload(st.get("always_notify_keywords"), []),
            "exclude_keywords": jload(st.get("exclude_notify_keywords"), []),
            "always_kw_bypass_night": str(st.get("always_kw_bypass_night", 1) or 0)
                                      not in ("0", "False", "false", ""),
            "web_password": st.get("web_password") or "",
            "notify_policy": str(st.get("notify_policy") or "0") not in ("0", "False", "false", ""),
            "policy_keywords": jload(st.get("policy_notify_keywords"), []),
            "policy_required": jload(st.get("policy_required_keywords"), []),
            "policy_exclude": jload(st.get("policy_exclude_keywords"), []),
            "notify_trade": str(st.get("notify_trade") or "0") not in ("0", "False", "false", ""),
            "trade_keywords": jload(st.get("trade_notify_keywords"), []),
            "trade_required": jload(st.get("trade_required_keywords"), []),
            "trade_exclude": jload(st.get("trade_exclude_keywords"), []),
            "weekly_to": weekly_recipients(ctx),
            "weekly_env_to": list(ctx.cfg.weekly_to),
            "weekly_smtp_ready": ctx.cfg.smtp_configured,
            # 중요도 점수 산정 규칙 — 마스터 패널에 그대로 표시한다(단일 출처).
            "score_rules": {
                "night": {"start": n_start, "end": n_end, "min_score": n_min,
                          "tz": f"UTC{ctx.cfg.tz_offset_hours:+d}"},
                "items": [
                    {"label": "포스코퓨처엠이 제목에", "points": SCORE_FUTUREM_TITLE},
                    {"label": "포스코퓨처엠이 본문에만", "points": SCORE_FUTUREM_BODY},
                    {"label": "다른 계열사(홀딩스·DX·인터내셔널·이앤씨 등)", "points": SCORE_GROUP},
                    {"label": "배터리 생태계(소재·셀·전기차·ESS·원료)가 제목에", "points": SCORE_BATTERY_TITLE},
                    {"label": "배터리 생태계가 본문에만", "points": SCORE_BATTERY_BODY},
                    {"label": "해외 통상 조치(IRA·CBAM·반덤핑 등) 신호", "points": SCORE_TRADE},
                    {"label": "정책 키워드(전기요금·배출권·특화단지 등)", "points": SCORE_POLICY},
                    {"label": "주요 언론사(연합·전자신문·머니투데이 등)", "points": SCORE_MAJOR_PRESS},
                    {"label": "단순 시황·주가 기사(목표주가·코스피·투자의견)", "points": SCORE_MARKET_PENALTY},
                ],
            },
        })

    @app.post("/api/master/settings")
    async def api_master_settings_post(payload: dict, x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        patch: dict[str, Any] = {}
        if "telegram_enabled" in (payload or {}):
            patch["notify_paused"] = 0 if payload["telegram_enabled"] else 1
        if "threshold" in (payload or {}):
            try:
                patch["notify_threshold"] = int(clamp(int(payload["threshold"]), 0, 100))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "임계값은 0~100 숫자여야 합니다."},
                                    status_code=400)
        # 야간 억제 — 시각은 0~23(운영 기준 시간대), 점수는 0~101(101=전면 차단)
        for key, col, lo, hi, label in (
            ("night_start", "night_start_hour", 0, 23, "야간 시작 시각"),
            ("night_end", "night_end_hour", 0, 23, "야간 종료 시각"),
            ("night_min_score", "night_min_score", 0, 101, "야간 최소 점수"),
        ):
            if key in (payload or {}):
                try:
                    patch[col] = int(clamp(int(payload[key]), lo, hi))
                except (TypeError, ValueError):
                    return JSONResponse({"ok": False, "error": f"{label}은 {lo}~{hi} 숫자여야 합니다."},
                                        status_code=400)
        # 키워드 목록 필드 — 같은 방식으로 정리(중복 제거, 30개 상한)
        for field, col in (("keywords", "always_notify_keywords"),
                           ("exclude_keywords", "exclude_notify_keywords"),
                           ("policy_keywords", "policy_notify_keywords"),
                           ("policy_required", "policy_required_keywords"),
                           ("policy_exclude", "policy_exclude_keywords"),
                           ("trade_keywords", "trade_notify_keywords"),
                           ("trade_required", "trade_required_keywords"),
                           ("trade_exclude", "trade_exclude_keywords")):
            if field in (payload or {}):
                kws = payload[field]
                if not isinstance(kws, list):
                    return JSONResponse({"ok": False, "error": "키워드는 목록이어야 합니다."},
                                        status_code=400)
                patch[col] = jdump(dedupe_chips(
                    str(k).strip() for k in kws if str(k).strip())[:30])
        for field, col in (("notify_policy", "notify_policy"), ("notify_trade", "notify_trade"),
                           ("always_kw_bypass_night", "always_kw_bypass_night")):
            if field in (payload or {}):
                patch[col] = 1 if payload[field] else 0
        if "weekly_to" in (payload or {}):
            raw = payload["weekly_to"]
            if not isinstance(raw, list):
                return JSONResponse({"ok": False, "error": "수신자는 목록이어야 합니다."}, status_code=400)
            emails: list[str] = []
            for e in raw:
                e = str(e).strip()
                if not e or e.lower() in [x.lower() for x in emails]:
                    continue
                if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
                    return JSONResponse({"ok": False, "error": f"이메일 형식이 아닙니다: {e}"},
                                        status_code=400)
                emails.append(e)
            patch["weekly_report_to"] = jdump(emails[:30])
        if patch:
            try:
                ctx.storage.set_run_state(patch)
            except Exception as exc:
                msg = str(exc)
                if ("column" in msg.lower() or "PGRST204" in msg
                        or "schema cache" in msg.lower()):
                    return JSONResponse(
                        {"ok": False, "error": "저장소(run_state)에 이 설정용 컬럼이 아직 없습니다. "
                         "Supabase SQL Editor 에서 아래를 1회 실행하세요:\n"
                         "alter table run_state\n"
                         "  add column if not exists night_start_hour int,\n"
                         "  add column if not exists night_end_hour int,\n"
                         "  add column if not exists night_min_score int,\n"
                         "  add column if not exists exclude_notify_keywords text default '[]',\n"
                         "  add column if not exists always_kw_bypass_night boolean not null default true,\n"
                         "  add column if not exists policy_exclude_keywords text default '[]',\n"
                         "  add column if not exists trade_exclude_keywords text default '[]';"},
                        status_code=500)
                raise
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
            # 세션 토큰은 비밀번호에서 파생되므로 바꾸는 즉시 기존 쿠키가 무효가 된다.
            # 캐시를 비워 다음 요청부터 새 값을 쓰게 한다.
            _WEB_PW_CACHE.update(at=0.0, pw="")
        return JSONResponse({"ok": True})

    @app.post("/api/analyze-url")
    async def api_analyze_url(payload: dict):
        """URL 하나를 분석해 미리보기 카드를 만든다. status='draft' 로만 저장하고,
        사용자가 /confirm 을 호출해야 목록에 노출된다. (PRD F8 수동 등록)"""
        url = (payload or {}).get("url", "")
        if not isinstance(url, str) or len(url) > 2000:
            return JSONResponse({"ok": False, "error": "URL 형식이 올바르지 않습니다."}, status_code=400)
        # 이 경로는 요청 1건이 곧 LLM 호출 1건(과금)이다. 실수·악용으로 비용이
        # 새지 않게 시간당 상한을 둔다. 정상 사용(하루 몇 건)에는 걸리지 않는다.
        if not _rate_ok(_analyze_calls, ANALYZE_MAX_PER_HOUR):
            return JSONResponse(
                {"ok": False, "error": f"URL 등록은 시간당 {ANALYZE_MAX_PER_HOUR}건까지입니다."
                                       " 잠시 후 다시 시도해 주세요."}, status_code=429)
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
        notified = False
        if row.get("status") == "draft":
            ctx.storage.update_article(article_id, {"status": "active"})
            _bust_scan_cache(article_id)
            # 등록 확정 시 임계값을 넘으면 텔레그램 채널에도 발송한다. (사용자 지정 2026-09-08)
            try:
                notified = queue_manual_notify(ctx, article_id)
            except Exception as exc:
                log.warning("수동 등록 알림 처리 실패: %s", exc)
        return JSONResponse({"ok": True, "notified": notified})

    @app.post("/api/articles/{article_id}/discard")
    def api_discard_draft(article_id: str):
        """미리보기(draft) 기사를 등록하지 않고 버린다(보관 처리)."""
        row = ctx.storage.article_detail(article_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "기사를 찾을 수 없습니다."}, status_code=404)
        if row.get("status") == "draft":
            ctx.storage.update_article(article_id, {"status": "archived"})
        return JSONResponse({"ok": True})

    @app.get("/api/weekly")
    def api_weekly(id: str = ""):
        """주간 레포트 1건 (기본: 최신). payload + html 포함."""
        report = ctx.storage.get_weekly_report(id or None)
        meta = {
            "ok": True,
            "enabled": ctx.cfg.weekly_enabled,
            "smtp_ready": ctx.cfg.smtp_configured,
            "recipients": weekly_recipients(ctx),
        }
        if report is None:
            return JSONResponse({**meta, "report": None})
        return JSONResponse({
            **meta,
            "report": {
                "id": report.get("id"),
                "period_start": report.get("period_start"),
                "period_end": report.get("period_end"),
                "generated_at": report.get("generated_at"),
                "sent_at": report.get("sent_at"),
                "send_error": report.get("send_error"),
                "payload": report.get("payload"),
                "html": report.get("html"),
            },
        })

    @app.get("/api/weekly/history")
    def api_weekly_history():
        rows = ctx.storage.list_weekly_reports(30)
        return JSONResponse({"items": [{
            "id": r.get("id"),
            "period_start": r.get("period_start"),
            "period_end": r.get("period_end"),
            "generated_at": r.get("generated_at"),
            "sent_at": r.get("sent_at"),
        } for r in rows]})

    _weekly_lock = threading.Lock()

    @app.post("/api/weekly/generate")
    def api_weekly_generate(payload: dict | None = None,
                            x_master_token: str = fastapi.Header(default="")):
        """지금 새로 생성. 마스터 전용 · LLM 7회 과금.
        payload {"send": true} 면 생성 직후 이메일까지 보낸다(기본 false)."""
        if not _valid_master_token(x_master_token):
            return JSONResponse({"ok": False, "error": "마스터 인증이 필요합니다."}, status_code=401)
        if not _weekly_lock.acquire(blocking=False):
            return JSONResponse({"ok": False, "error": "이미 생성 중입니다."}, status_code=409)
        want_send = bool((payload or {}).get("send"))
        try:
            report = run_weekly_report(ctx, send=want_send and ctx.cfg.smtp_configured)
        finally:
            _weekly_lock.release()
        return JSONResponse({"ok": True, "id": report["id"], "sent_at": report.get("sent_at"),
                             "send_error": report.get("send_error")})

    @app.post("/api/weekly/{report_id}/send")
    def api_weekly_send(report_id: str, x_master_token: str = fastapi.Header(default="")):
        """이미 생성된 레포트를 지금 이메일로 보낸다. 마스터 전용."""
        if not _valid_master_token(x_master_token):
            return JSONResponse({"ok": False, "error": "마스터 인증이 필요합니다."}, status_code=401)
        report = ctx.storage.get_weekly_report(report_id)
        if report is None:
            return JSONResponse({"ok": False, "error": "레포트를 찾을 수 없습니다."}, status_code=404)
        pe = (report.get("period_end") or "")[:10]
        to_list = weekly_recipients(ctx)
        ok, err = send_report_email(
            ctx.cfg, f"[P-FM] 포스코 그룹 주간동향 ({pe})", report.get("html") or "", to_list)
        ctx.storage.mark_weekly_sent(report_id, iso(now_utc()) if ok else None, err)
        return JSONResponse({"ok": ok, "error": err,
                             "sent_to": to_list if ok else []},
                            status_code=200 if ok else 400)

    if ea_mod is not None:
        ea_mod.register_api(app, ctx)

    if os.path.isdir(FRONTEND_DIR):
        _index_cache: dict[str, Any] = {"sig": None, "html": ""}
        _index_files = [os.path.join(FRONTEND_DIR, n)
                        for n in ("index.html", "style.css", "app.js",
                                  "external_affairs.css", "external_affairs.js")]

        def _render_index() -> str:
            # JS·CSS 를 HTML 에 인라인해서 내보낸다. 별도 정적 요청이 없으므로
            # 쿼리스트링을 무시하는 프록시가 있어도 옛 파일을 내줄 수 없다.
            html, css, js, ea_css, ea_js = (
                open(p, "r", encoding="utf-8").read() for p in _index_files)
            # 리터럴 치환만 한다(re.sub 은 repl 의 \s 등을 이스케이프로 해석해 깨진다).
            html = html.replace('<link rel="stylesheet" href="./style.css">',
                                f"<style>\n{css}\n{ea_css}\n</style>")
            return html.replace('<script src="./app.js"></script>',
                                f"<script>\n{js}\n</script>\n<script>\n{ea_js}\n</script>")

        def _index_html() -> str:
            sig = tuple(os.path.getmtime(p) for p in _index_files)
            if _index_cache["sig"] != sig:
                _index_cache.update(sig=sig, html=_render_index())
            return _index_cache["html"]

        @app.get("/")
        def index():
            # 세 파일의 수정시각이 그대로면 조립 결과를 재사용한다(매 요청 디스크 3회 읽기·치환 방지).
            return HTMLResponse(_index_html())

        @app.get("/ea")
        def ea_page():
            # 대외협력 독립 주소. 같은 SPA 를 내보내고, external_affairs.js 가
            # location.pathname 을 보고 대외협력 화면으로 열어 준다.
            return HTMLResponse(_index_html())

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

    # 이미 저장된 이름에서 부제·영문 병기·래퍼 접두를 뒤늦게 떼어낸다.
    # (SEED_PRESS 로 안 잡히지만 'Daum | 뉴스1' 처럼 정리 여지가 있는 것)
    cleaned = 0
    for row in ctx.storage.all_press():
        cur = (row.get("name") or "").strip()
        nxt = clean_site_name(cur, row.get("domain") or "")
        if nxt and nxt != cur and _has_hangul(nxt) and not _looks_like_domain(nxt):
            ctx.storage.update_press_name(row["domain"], nxt, int(row.get("tier") or 3))
            cleaned += 1

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
            og = site_name_from_html(html, domain)
        except Exception as exc:
            log.debug("og:site_name 조회 실패 %s: %s", target, exc)
        ctx.storage.update_press_name(domain, og or domain, 3)
        if og:
            ogfix += 1

    synced = ctx.storage.sync_article_press_names()
    log.info("언론사명 정리: %d개 SEED 교체 · %d개 부제 제거 · %d개 og:site_name 복원 · 기사 %d건 반영",
             renamed, cleaned, ogfix, synced)


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


def cmd_repeople(ctx: Context, limit: int = 60, force: bool = False) -> None:
    """기존 인사·부고 기사를 사람별 구조 요약(LLM)으로 다시 만든다.

    규칙 기반 덤프로 저장된 카드를 원문에서 본문을 다시 받아 재정리한다.
    이미 구조화된(요약이 'ㆍ'로 시작) 카드는 건너뛴다 — `repeople all` 이면 전부 다시.
    limit 로 LLM 호출 수를 제한한다(비용 관리). `repeople 20` 처럼 쓴다.
    """
    rows = [r for r in ctx.storage.list_articles(8000, 0, None, "")
            if PEOPLE_NEWS_CATEGORY in jload(r.get("categories"), [])]
    if not force:
        rows = [r for r in rows if not (r.get("summary_text") or "").lstrip().startswith("ㆍ")]
    log.info("인사·부고 재정리 대상 %d건 (최대 %d건 처리%s)",
             len(rows), limit, ", 전체 강제" if force else "")
    done = failed = skipped = 0
    for r in rows:
        if done >= limit:
            break
        kind = people_news_kind(r.get("url_canonical") or "", r.get("title") or "") or "personnel"
        target = r.get("url_canonical") or r.get("url_original") or r.get("url_source") or ""
        if not target:
            skipped += 1
            continue
        try:
            _, html = resolve_canonical(ctx.http, target)
            body = extract_body(html)
            if len(_notice_text(html, body)) < 20:
                skipped += 1
                continue
            summary, model, usage = people_summary(
                ctx, kind, r.get("title") or "", r.get("press_name") or "", body,
                use_llm=True, html=html)
            if not model:
                failed += 1
                continue
            ctx.storage.save_summary({
                "id": new_id(), "article_id": r["id"],
                "summary_text": summary, "perspective_text": "",
                "summary_source": "notice", "model": model, "token_usage": usage or None,
                "created_at": iso(now_utc()),
            })
            done += 1
            log.info("  ✓ %s", (r.get("title") or "")[:44])
        except Exception as exc:
            failed += 1
            log.warning("  ✗ %s — %s", (r.get("title") or "")[:44], exc)
    log.info("재정리 완료: 성공 %d · 실패 %d · 건너뜀 %d", done, failed, skipped)


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
    # 이 카테고리가 붙은 기사는 포스코 미언급이 정상 — 보관 대상에서 제외한다.
    _KEEP_CATS = {TRADE_CATEGORY, POLICY_CATEGORY, PEOPLE_NEWS_CATEGORY,
                  "배터리·이차전지", "양극재", "음극재"}
    rows = ctx.storage.list_articles(5000, 0, None, "")
    cands = []
    for r in rows:
        cats = set(jload(r.get("categories"), []))
        if (is_policy_brief(r) or is_trade_article(r) or (cats & _KEEP_CATS)
                or jload(r.get("group_companies"), [])
                or is_battery_scope(r.get("title") or "", r.get("summary_text") or "")):
            continue  # 정책·통상·배터리·인사·부고·그룹사 확정 기사는 유지 (사용자 지정)
        text = "\n".join([
            r.get("title") or "", r.get("summary_text") or "",
            " ".join(jload(r.get("keywords"), [])),
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


# sqlite → supabase 로 옮길 테이블(의존성 순서: 부모 먼저).
_MIGRATE_TABLES = [
    "press_outlets", "feed_sources", "keyword_sets", "run_state", "url_ledger",
    "articles", "article_bodies", "summaries", "swot_analyses", "notifications",
    "collection_logs", "market_quotes", "weekly_reports", "telegram_log",
    "ea_agencies", "ea_policy_items", "ea_analyses", "ea_url_ledger", "ea_run_state",
]


def cmd_migrate(ctx: Context) -> None:
    """SQLite → Supabase 전체 복사 (일회성).

    선행: ① Supabase SQL Editor 에서 backend/schema.sql 실행
          ② .env  DB_BACKEND=supabase  로 전환 (그래야 ctx.storage 가 Supabase)
    JSON 문자열('[...]'/'{...}')·불리언(0/1)은 자동 변환한다. 배치 upsert.
    """
    if ctx.cfg.db_backend != "supabase":
        raise SystemExit("먼저 .env 의 DB_BACKEND=supabase 로 바꾸고 다시 실행하세요.")
    src = sqlite3.connect(ctx.cfg.sqlite_path)
    src.row_factory = sqlite3.Row
    tgt = ctx.storage.db   # supabase client

    def _coerce(v: Any) -> Any:
        if isinstance(v, str) and v[:1] in ("[", "{"):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        return v

    # id 가 bigserial(자동 증가)인 테이블은 id 를 옮기지 않는다 — 명시 id 로 넣으면
    # Postgres 시퀀스가 안 움직여, 이후 로그를 쓸 때 id=1 부터 충돌한다.
    _SERIAL_ID_TABLES = {"collection_logs"}

    total = 0
    for table in _MIGRATE_TABLES:
        try:
            rows = [dict(r) for r in src.execute(f"select * from {table}")]
        except sqlite3.OperationalError:
            log.info("  %s: 원본에 없음 — 건너뜀", table)
            continue
        if not rows:
            log.info("  %s: 0행", table)
            continue
        drop_id = table in _SERIAL_ID_TABLES
        payload = [{k: _coerce(v) for k, v in r.items() if not (drop_id and k == "id")}
                   for r in rows]
        done = 0
        for i in range(0, len(payload), 500):
            chunk = payload[i:i + 500]
            tgt.table(table).insert(chunk).execute() if drop_id else \
                tgt.table(table).upsert(chunk).execute()
            done += len(chunk)
        total += done
        log.info("  %s: %d행 이관", table, done)
    src.close()
    log.info("이관 완료: 총 %d행. 이제 서버를 재시작하면 Supabase 로 동작합니다.", total)


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
            # 한 회차에 12건까지만 — 억제 해제 등으로 큐가 밀려도 분당 한도를 넘기지 않는다.
            send_notifications(ctx, limit=SEND_BATCH_PER_CYCLE)
            maybe_run_weekly(ctx)
            # 목록 스캔 스토어를 델타로 갱신해 둔다(웹 요청 시 DB 재조회 없음).
            refresh_scan_store(ctx.storage)
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
    # 시드는 idempotent — 새로 추가된 RSS 피드·키워드를 기동 시 반영한다.
    try:
        ctx.storage.seed_feeds(SEED_FEEDS)
    except Exception as exc:   # pragma: no cover
        log.warning("피드 시드 스킵: %s", exc)
    app = create_app(ctx)
    stop = threading.Event()
    threads: list[threading.Thread] = []
    if with_pipeline:
        threads.append(threading.Thread(target=pipeline_loop, args=(ctx, stop), daemon=True))
        threads.append(threading.Thread(target=quote_loop, args=(ctx, stop), daemon=True))
        log.info("수집 루프 시작 (%d초 주기) · 시세 갱신 %d초", ctx.cfg.poll_interval_sec, QUOTE_REFRESH_SEC)
        if ctx.cfg.telegram_enabled:
            threads.append(threading.Thread(target=telegram_bot_loop, args=(ctx, stop), daemon=True))
        if ea_mod is not None and ea_mod.ea_enabled():
            threads.append(threading.Thread(target=ea_mod.scheduler_loop, args=(ctx, stop), daemon=True))
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
    # 같은 기사의 여러 경로를 기사 ID 로 접는다 (thebell: 유료안내 vs 실제 기사)
    _tb = "https://www.thebell.co.kr/front/newsview.asp?key=202609021458518280106019"
    check("thebell 유료안내 경로 → 표준 경로",
          normalize_url("https://www.thebell.co.kr/free/content/ArticleView.asp?key=202609021458518280106019"), _tb)
    check("thebell click 파라미터 제거",
          normalize_url("http://thebell.co.kr/front/newsview.asp?click=F&key=202609021458518280106019&page=1"), _tb)
    check("규칙 없는 사이트는 경로 유지",
          normalize_url("https://www.yna.co.kr/view/AKR20260904067500083?utm_source=x"),
          "https://www.yna.co.kr/view/AKR20260904067500083")

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
    check("배터리 생태계(그룹사 미언급)는 0점 아님",
          score_article("BYD 전기차 판매량 급감", "", [], 3), SCORE_BATTERY_TITLE)
    check("배터리 키워드가 본문에만 → 소폭",
          score_article("증시 주도주 순환매", "2차전지 관련주가 반등", [], 3), SCORE_BATTERY_BODY)
    check("통상 신호(조치명+산업어) → 가점",
          score_article("CBAM 시행에 철강업계 비상", "", [], 3) >= SCORE_TRADE, True)
    check("무관 기사는 여전히 0", score_article("아파트 청약 경쟁률", "분양시장", [], 3), 0)

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
    # 부제·영문 병기 제거 (clean_site_name)
    check("' - 부제' 제거", clean_site_name("경기신문 - 기본에 충실한 경기·인천 지역 바른 신문"), "경기신문")
    check("' | 부제' 제거", clean_site_name("AP신문 |  온라인뉴스미디어  에이피신문"), "AP신문")
    check("'(English)' 병기 제거", clean_site_name("더스탁(The Stock)"), "더스탁")
    check("한글 병기 괄호는 유지", clean_site_name("주간동아(주간)"), "주간동아(주간)")
    check("래퍼 도메인은 뒤쪽(실제 출처)", clean_site_name("Daum | 뉴스1", "daum.net"), "뉴스1")
    check("일반 도메인은 앞쪽(매체명)", clean_site_name("AP신문 | 부제", "apnews.kr"), "AP신문")
    check("SEED_PRESS 신규 매핑(KPI뉴스)", SEED_PRESS.get("kpinews.kr", ("", 0))[0], "KPI뉴스")
    check("SEED_PRESS 신규 매핑(여성소비자신문)", SEED_PRESS.get("wsobi.com", ("", 0))[0], "여성소비자신문")

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
    check("행정 용어는 '정부/정책'", "정부/정책" in detect_categories("특화단지 지정, 산업부 발표"), True)
    check("입법 용어도 '정부/정책'", "정부/정책" in detect_categories("이차전지 특별법 개정안 발의"), True)
    check("'법령' 카테고리는 폐지됨", "법령" in detect_categories("공정위 과징금 부과 개정안"), False)
    # 정부/정책: '관세'·'IRA' 제거 — 통상 조치는 '글로벌 통상환경'이 담당
    check("'관세' 스친 실적 기사는 정책 아님",
          "정부/정책" in detect_categories("美 철강 관세에 포스코 현대제철 공동전선"), False)
    check("'규제' 한 낱말로는 정책 태깅 안 함",
          "정부/정책" in detect_categories("탄소 규제에 짓눌린 철강 삼중고"), False)
    # 정부/정책도 제목 고정 — 요약에 '보조금' 스친 전기차 판매·시황 기사는 제외
    check("'보조금'은 더 이상 정책 키워드 아님",
          "정부/정책" in detect_categories("BYD 돌핀 판매 상위…9월도 전기차 보조금 지원",
                                          "테슬라도 월 1만대 판매"), False)
    check("요약에만 산업부 언급 → 정책 아님(제목 고정)",
          "정부/정책" in detect_categories("포스코 2분기 실적 개선", "산업부가 지원책을 검토 중이다"), False)
    check("제목에 산업부 장관 → 정책",
          "정부/정책" in detect_categories("김정관 산업부 장관, G20 참석"), True)
    check("철강 공정어는 '산업'", "산업" in detect_categories("포스코 포항 3고로 개수 완료"), True)

    print("\n[8-5] 인사·부고 (제목 판정 + 규칙 요약 + LLM 구조화)")
    check("[부고] 말머리 → obituary",
          people_news_kind("", "[부고] 홍길동(전 삼성 부사장)씨 모친상"), "obituary")
    check("연합 부고 섹션 URL → obituary",
          people_news_kind("https://www.yna.co.kr/view/AKR1?section=people/obituary-notice", "제목"),
          "obituary")
    check("[인사]·[승진] → personnel",
          people_news_kind("", "[승진] 포스코홀딩스 임원 인사"), "personnel")
    check("[동정]은 대상 아님", people_news_kind("", "[동정] 장관 현장방문"), "")
    check("일반 기사는 대상 아님", people_news_kind("", "포스코퓨처엠 양극재 증설"), "")
    # 말머리 없는 정부부처 인사 공지 (motir.go.kr 등)
    check("'인사발령(…)' 제목 → personnel",
          people_news_kind("https://www.motir.go.kr/kor/article/x", "인사발령(실장급 승진)"), "personnel")
    check("'보직인사' 제목 → personnel",
          people_news_kind("", "산업통상부 4월 정기 보직인사 단행"), "personnel")
    check("부처명만 있고 인사 표현 없으면 대상 아님",
          people_news_kind("", "산업통상부, 반도체 국장급 회의 소집"), "")
    _ob = ("김 기자 구독 구독중 이전 다음 ▲ 김철수(향년 80세)씨 별세, 김영희씨 부친상 "
           "= 8일 오전, 서울대병원, 발인 10일. ☎ 02-1234-5678 (서울=연합뉴스) 무단 전재 금지")
    check("부고 요약 = ▲…☎ 블록",
          format_people_notice(_ob, "obituary"),
          "▲ 김철수(향년 80세)씨 별세, 김영희씨 부친상 = 8일 오전, 서울대병원, 발인 10일. ☎ 02-1234-5678")
    check("인사 요약 = ◇…블록",
          format_people_notice("기자 구독 구독중 이전 다음 ◇ 편집국 ▲ 산업본부장 류준형 (서울=연합뉴스)", "personnel"),
          "◇ 편집국 ▲ 산업본부장 류준형")
    # LLM 구조화 결과 렌더 (format_people_llm)
    _pl = {"kind": "personnel", "people": [{
        "name": "이지원", "org": "기획예산처 재정관리국", "position": "부이사관", "change": "승진",
        "exam": "행정고시 45회",
        "career": ["기획재정부 재정성과평가과장(23.5~25.5)", "기획재정부 재정관리총괄과장"],
        "duty": "부담금 관리체계 전면 정비", "education": ""}]}
    _out = format_people_llm(_pl, "personnel")
    check("인사 구조화 — 머리줄", _out.splitlines()[0], "ㆍ기획예산처 재정관리국 이지원 부이사관 승진")
    check("인사 구조화 — 고시 줄", "· 행정고시 45회" in _out, True)
    check("인사 구조화 — 이력 번호",
          "· 이력: 1) 기획재정부 재정성과평가과장(23.5~25.5) 2) 기획재정부 재정관리총괄과장" in _out, True)
    check("인사 구조화 — 업무 줄", "· 업무: 부담금 관리체계 전면 정비" in _out, True)
    check("인사 구조화 — 빈 학력은 생략", "학력" in _out, False)
    check("이름 없는 항목은 건너뜀",
          format_people_llm({"people": [{"org": "A"}, {"name": "김", "org": "B"}]}, "personnel"),
          "ㆍB 김")
    _po = {"kind": "obituary", "deaths": [{
        "subject": "홍길동 전 삼성전자 부사장", "relation": "김철수 부장 부친상",
        "mourners": "홍자녀", "wake": "서울아산병원 3호실",
        "funeral": "9월 11일", "burial": "OO추모공원", "contact": "02-3010-2000"}]}
    _ov = format_people_llm(_po, "obituary")
    check("부고 구조화 — 머리줄",
          _ov.splitlines()[0], "ㆍ홍길동 전 삼성전자 부사장 / 김철수 부장 부친상")
    check("부고 구조화 — 발인·장지 한 줄", "· 발인 9월 11일 / 장지 OO추모공원" in _ov, True)
    check("부고 구조화 — 연락처", "· ☎ 02-3010-2000" in _ov, True)
    check("빈 파싱 결과 → 빈 문자열", format_people_llm({}, "personnel"), "")
    # _notice_text — 본문 추출이 푸터를 잡으면 og:description·<article> 로 보강
    _foot = "대표이사 홍길동 사업자등록번호 102-81-36588 무단 전재ㆍ복사 금지 Copyright"
    _html = ('<meta property="og:description" content="[세종=뉴시스] ◇부이사관 승진 '
             '▲기획예산처 이지원 ▲기획예산처 조규산"/>'
             '<article>[세종=뉴시스] ◇부이사관 승진<br/>▲기획예산처 이지원 '
             '▲기획예산처 조규산<br/><script>x()</script></article>')
    _nt = _notice_text(_html, _foot)
    check("_notice_text — 푸터 대신 실제 공지", "부이사관 승진" in _nt and "무단 전재" not in _nt, True)
    check("_notice_text — 본문이 알차면 그대로",
          _notice_text("<article>짧은거</article>", "이미 충분히 긴 정상 본문입니다 " * 3).startswith("이미"), True)
    check("'실적' 단독은 시장/주가 태그 아님",
          "시장/주가" in detect_categories("포스코 2분기 실적 발표"), False)
    check("주가 특화어는 시장/주가 태그",
          "시장/주가" in detect_categories("포스코 주가 코스피 상한가"), True)
    check("본문 언급은 카테고리에 안 잡힌다(제목·요약만 스캔)",
          detect_categories("장인화 회장 호주 방문", "핵심광물 공급망 협력을 제안했다"), [])

    print("\n[8-2] 네이버 관련성 필터 (제목 기준 — 모든 카테고리)")
    check("포스코 무관 기사(수집 게이트)",
          bool(detect_group_companies("해병대 포병대대 포항 소외계층 무료급식 봉사")
               or POSCO_MENTION_RE.search("해병대 포병대대 포항 소외계층 무료급식 봉사")), False)
    check("포스코 언급 기사(수집 게이트)",
          bool(POSCO_MENTION_RE.search("포스코 포항제철소 3고로 개수 완료")), True)
    check("계열사만 언급된 기사(수집 게이트)",
          bool(detect_group_companies("삼척블루파워 석탄화력 준공")), True)
    check("그룹사 키워드: 제목에 포스코 없음 → 제외",
          _naver_item_relevant("영진전문대 수시모집 2309명 선발", "그룹사", "포스코DX"), False)
    check("그룹사 키워드: 제목에 포스코DX 있음 → 통과",
          _naver_item_relevant("대덕SW고 학생, 포스코DX AI 유스챌린지 대상", "그룹사", "포스코DX"), True)
    check("산업: 제목에 배터리 신호 있으면 통과",
          _naver_item_relevant("SK온, 美 ESS 1.5조 수주", "산업", "SK온"), True)
    check("산업: 제목에 신호 없는 시황 기사 → 제외",
          _naver_item_relevant("삼성전기, 3%대 약세…140만원선 사수하나?", "산업", "삼성SDI"), False)
    check("산업: 검색어가 제목에 그대로 있으면 통과",
          _naver_item_relevant("니켈 가격 3개월래 최고", "산업", "니켈 가격"), True)
    check("정책: 제목에 정책어 없으면 제외",
          _naver_item_relevant("내일 서울대 총장후보 4명 압축", "정책", "국정감사"), False)
    check("정책: 제목에 정책어 있으면 통과",
          _naver_item_relevant("전기차 보조금 확대 국정감사 쟁점", "정책", "국정감사"), True)
    check("통상: 제목에 통상 조치어 없으면 제외",
          _naver_item_relevant("코스피 7100선 돌파", "통상", "관세"), False)
    check("통상: '관세'가 제목에 있으면 통과",
          _naver_item_relevant("트럼프 관세에 車업계 초비상", "통상", "글로벌 관세 전쟁"), True)
    check("산업: '배터리'가 제목에 있으면 통과(스코프어 아니어도)",
          _naver_item_relevant("K-배터리 소재 국산화 시동…정부 2조 투입", "산업", "양극재"), True)
    check("포스코가 제목에 있으면 카테고리 불문 통과",
          _naver_item_relevant("포스코홀딩스 주가 강세", "통상", "관세"), True)

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

    print("\n[8-4] 넘친 신선 후보 저비용 관련성 판정 (_title_snippet_relevant)")
    check("그룹사 제목 → 저장",
          _title_snippet_relevant("포스코퓨처엠 광양 양극재 증설", "")[0], True)
    check("배터리 생태계(BYD) → 저장",
          _title_snippet_relevant("BYD 전기차 판매량 급감", "테슬라는 증가")[0], True)
    check("포스코 언급 스니펫 → 저장",
          _title_snippet_relevant("증시 주도주 분석", "포스코홀딩스 주가 상승")[0], True)
    check("무관 일반 경제 → 저장 안 함",
          _title_snippet_relevant("아파트 청약 경쟁률 상승", "수도권 분양시장")[0], False)
    check("반도체 기사 → 저장 안 함",
          _title_snippet_relevant("삼성전자 HBM4 양산 준비", "")[0], False)

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

    print("\n[11-2d] 목록 스캔 경량 조회 (scan_articles · card_details · embeddings_for)")
    _tmp._exec("delete from articles")
    for _sid, _an in (("sc-done", iso(now_utc())), ("sc-pending", None)):
        _tmp._exec(
            "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
            " collected_at, source_type, importance_score, group_companies, categories, press_name,"
            " analyzed_at, title_embedding, status, is_representative)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_sid, f"http://s/{_sid}", f"http://s/{_sid}", f"http://s/{_sid}", "포스코퓨처엠 증설",
             iso(now_utc()), iso(now_utc()), "search", 70, '["포스코퓨처엠"]', '["양극재"]',
             "한국경제", _an, "[0.1,0.2]", "active", 1))
    _tmp.save_summary({"id": new_id(), "article_id": "sc-done", "summary_text": "요약본문",
                       "perspective_text": "관점", "summary_source": "fulltext",
                       "model": "m", "token_usage": None, "created_at": iso(now_utc())})
    _scan = _tmp.scan_articles(100, None, "")
    check("분석 끝난 기사만 스캔에 나온다", [r["id"] for r in _scan], ["sc-done"])
    check("스캔은 SWOT 근거문을 싣지 않는다", "s_text" in _scan[0], False)
    check("스캔에도 요약은 있다(그룹사 폴백용)", _scan[0].get("summary_text"), "요약본문")
    check("스캔은 임베딩을 싣지 않는다", "title_embedding" in _scan[0], False)
    _cd = _tmp.card_details(["sc-done"])
    check("card_details 는 관점까지 준다", _cd[0].get("perspective_text"), "관점")
    check("card_details 는 없는 id 를 조용히 건너뛴다", _tmp.card_details(["nope"]), [])
    check("card_details 는 입력 순서를 지킨다",
          [r["id"] for r in _tmp.card_details(["sc-pending", "sc-done"])], ["sc-pending", "sc-done"])
    check("embeddings_for 는 요청한 id 만", sorted(_tmp.embeddings_for(["sc-done"])), ["sc-done"])
    check("embeddings_for 빈 입력 → 빈 dict", _tmp.embeddings_for([]), {})
    # 태그 메모: id 가 같아도 언론사명이 바뀌면 다시 판정한다
    _r1 = {"id": "t1", "analyzed_at": "x", "press_name": "A",
           "group_companies": ["포스코퓨처엠"], "categories": [], "title": "", "keywords": []}
    _r2 = dict(_r1, press_name="B")
    check("태그 메모 — 언론사명이 바뀌면 갱신", (tag_row(_r1)["p"], tag_row(_r2)["p"]), ("A", "B"))
    check("태그 메모 — id 없는 행은 캐시하지 않는다",
          tag_row({"group_companies": ["포스코"], "categories": [], "press_name": "Z"})["g"], ["포스코"])

    print("\n[11-2e] 스캔 스토어 델타 갱신 (changed_articles_since · refresh_scan_store)")
    def _reset_store():
        _SCAN_STORE.update(by_id={}, cursor="", full_at=0.0, delta_at=0.0)
    _reset_store()
    _tmp._exec("delete from articles"); _tmp._exec("delete from summaries")
    _t0 = iso(now_utc() - timedelta(hours=2))
    for _sid, _pub in (("st-a", _t0), ("st-b", _t0)):
        _tmp._exec(
            "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
            " collected_at, source_type, importance_score, group_companies, categories, press_name,"
            " analyzed_at, status, is_representative) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_sid, f"h/{_sid}", f"h/{_sid}", f"h/{_sid}", "포스코퓨처엠 " + _sid, _pub, _pub,
             "search", 70, '["포스코퓨처엠"]', '["양극재"]', "한경", _pub, "active", 1))
    rows = refresh_scan_store(_tmp, full=True)
    check("전체 적재 — 2건", sorted(t["row"]["id"] for t in rows), ["st-a", "st-b"])
    # 새 기사 1건 추가 → 델타로만 반영
    _later = iso(now_utc() - timedelta(minutes=1))
    _tmp._exec(
        "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
        " collected_at, source_type, importance_score, group_companies, categories, press_name,"
        " analyzed_at, status, is_representative) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("st-c", "h/c", "h/c", "h/c", "포스코퓨처엠 c", _later, _later, "search", 70,
         '["포스코퓨처엠"]', '["양극재"]', "한경", _later, "active", 1))
    _SCAN_STORE["delta_at"] = 0.0   # 델타 조회 최소 간격(60초) 우회 — 테스트라 바로 확인
    rows = refresh_scan_store(_tmp)
    check("델타 — 신규 1건 반영", "st-c" in {t["row"]["id"] for t in rows}, True)
    # st-a 를 보관 처리 → 델타가 스토어에서 제거
    _tmp._exec("update articles set status='archived', collected_at=? where id='st-a'",
               (iso(now_utc()),))
    _SCAN_STORE["delta_at"] = 0.0
    rows = refresh_scan_store(_tmp)
    check("델타 — 보관된 기사는 스토어에서 빠진다", "st-a" in {t["row"]["id"] for t in rows}, False)
    check("changed_articles_since 는 상태 무관하게 준다",
          "st-a" in {r["id"] for r in _tmp.changed_articles_since(iso(now_utc() - timedelta(minutes=5)))}, True)
    check("델타 조회 최소 간격 — 방금 갱신했으면 DB 안 침",
          (_SCAN_STORE.update(delta_at=time.monotonic()),
           _tmp._exec("update articles set title='X' where id='st-b'"),
           refresh_scan_store(_tmp),
           _SCAN_STORE["by_id"].get("st-b", {}).get("row", {}).get("title"))[-1] != "X", True)
    _reset_store()

    print("\n[11-2b] 보존 정책 — 오래된 기사·로그·원장 정리")
    _tmp._exec("delete from articles")
    _d600 = iso(now_utc() - timedelta(days=600))
    _d120 = iso(now_utc() - timedelta(days=120))
    _d030 = iso(now_utc() - timedelta(days=30))
    # (id, published_at, score, group_companies, status)
    _seed = [
        ("art-core-old", _d600, 80, "[]", "active"),       # 핵심·550일 초과 → 삭제
        ("art-core-mid", _d120, 80, "[]", "active"),       # 핵심·기간 내 → 유지
        ("art-noise-old", _d120, 10, "[]", "active"),      # 잡음·90일 초과 → 삭제
        ("art-noise-fresh", _d030, 10, "[]", "active"),    # 잡음·90일 내 → 유지
        ("art-noise-notified", _d120, 10, "[]", "active"), # 알림 이력 있음 → 유지
        ("art-noise-grouped", _d120, 10, '["포스코퓨처엠"]', "active"),  # 그룹사 태그 → 유지
        ("art-arch-old", _d120, 10, "[]", "archived"),     # 주제이탈·90일 초과 → 삭제
        ("art-draft-old", _d600, 10, "[]", "draft"),       # draft 는 제외 → 유지
    ]
    for _id, _pub, _sc, _grp, _st in _seed:
        _tmp._exec(
            "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
            " collected_at, source_type, importance_score, group_companies, status)"
            " values (?,?,?,?,?,?,?,?,?,?,?)",
            (_id, f"http://r/{_id}", f"http://r/{_id}", f"http://r/{_id}", "제목",
             _pub, _pub, "search", _sc, _grp, _st))
    _tmp._exec(
        "insert into notifications (id, article_id, channel, chat_id, status, created_at)"
        " values (?,?,?,?,?,?)",
        ("ntf-1", "art-noise-notified", "telegram", "c", "sent", _d120))
    check("오래된 핵심+잡음+archived 3건 삭제", _tmp.purge_old_articles(550, 90, 50), 3)
    _left = {r["id"] for r in _tmp._rows("select id from articles")}
    check("핵심(기간 내)·그룹사·알림이력·최근잡음·draft 는 유지",
          _left, {"art-core-mid", "art-noise-fresh", "art-noise-notified",
                  "art-noise-grouped", "art-draft-old"})
    check("유지된 기사의 알림은 그대로(과잉 삭제 아님)",
          _tmp._one("select count(*) as n from notifications")["n"], 1)
    check("두 번째 호출은 삭제 대상 없음 → 0건", _tmp.purge_old_articles(550, 90, 50), 0)

    for _lid, _ts in (("old", _d120), ("new", _d030)):
        _tmp._exec("insert into collection_logs (run_at, source_type) values (?,?)", (_ts, _lid))
        _tmp._exec("insert into url_ledger (url_source, reason, first_seen) values (?,?,?)",
                   (f"http://led/{_lid}", "seen", _ts))
    check("90일 넘은 수집로그만 정리 → 1건", _tmp.prune_collection_logs(90), 1)
    check("180일 기준 URL원장 정리 대상 없음 → 0건", _tmp.prune_url_ledger(180), 0)
    check("30일 기준이면 오래된 원장 1건 정리", _tmp.prune_url_ledger(90), 1)

    print("\n[11-2c] 텔레그램 발송 로그 (telegram_log)")
    # 발송 이유 판정 — 판정 근거(키워드·임계값·무조건점수) 없이 부르면 짧은 라벨로 폴백
    check("수동 등록 → 'URL 등록'", _notify_reason({"source_type": "manual"}), "URL 등록")
    check("우선 기사 → '우선 발송'", _notify_reason({"priority": 1}), "우선 발송")
    check("그 외 → '자동 알림'", _notify_reason({"source_type": "naver_api"}), "자동 알림")
    # 발송 이유 판정 — 근거를 주면 사람이 읽을 문구로
    _kwrow = {"title": "포스코퓨처엠 양극재 증설", "group_companies": ["포스코퓨처엠"],
              "importance_score": 40}
    check("무조건 받을 키워드 매칭 → 키워드 명시",
          _notify_reason(_kwrow, ["포스코퓨처엠"], 30, 0), "무조건 받을 키워드 '포스코퓨처엠'")
    check("priority 2 → 정책·통상 주제 매칭",
          _notify_reason({"title": "x", "importance_score": 20, "priority": 2}, [], 60, 0),
          "정책·통상 주제 키워드 매칭")
    check("무조건 발송 점수 초과(하위호환)",
          _notify_reason({"title": "x", "importance_score": 66}, [], 30, 65),
          "무조건 발송 점수 66 ≥ 65")
    check("중요도 임계값 초과",
          _notify_reason({"title": "x", "importance_score": 55}, [], 30, 0),
          "중요도 55 ≥ 임계값 30")
    check("수동 등록 + 임계값 초과 → 접두",
          _notify_reason({"source_type": "manual", "title": "x", "importance_score": 55}, [], 30, 0),
          "URL 등록 · 중요도 55 ≥ 임계값 30")
    check("키워드가 점수보다 우선 표기",
          _notify_reason({"title": "포스코 파업", "group_companies": ["포스코"],
                          "importance_score": 90}, ["포스코"], 30, 50),
          "무조건 받을 키워드 '포스코'")
    check("_kw_first_hit — 첫 매칭 키워드", _kw_first_hit("리튬 니켈 가격", ["코발트", "니켈"]), "니켈")
    check("_kw_first_hit — 없으면 None", _kw_first_hit("철강 수출", ["니켈"]), None)
    # rate limit / flood 헬퍼
    check("일시 오류 판정 — 429", _is_transient_tg_error("Too Many Requests: retry after 5"), True)
    check("일시 오류 판정 — chat not found 는 진짜 실패",
          _is_transient_tg_error("Bad Request: chat not found"), False)
    _flood_arm(2)
    check("_flood_arm 후 남은 대기 > 0", _flood_remaining() > 0, True)
    _FLOOD_GLOBALS = globals()
    _FLOOD_GLOBALS["_FLOOD_UNTIL"] = 0.0   # 다른 검증에 영향 없도록 즉시 해제
    check("_flood 해제 후 0", _flood_remaining(), 0.0)
    # 막힌 발송 실패 되돌리기
    _tmp._exec("insert into articles (id, url_source, url_canonical, url_original, title,"
               " published_at, collected_at, source_type, status) values"
               " (?,?,?,?,?,?,?,?,?)",
               ("art-rf", "u-rf", "u-rf", "u-rf", "제목", _d030, _d030, "search", "active"))
    _tmp._exec("insert into notifications (id, article_id, channel, chat_id, status, retry_count,"
               " created_at) values (?,?,?,?,?,?,?)",
               ("ntf-rf1", "art-rf", "telegram", "c", "failed", 3, _d030))
    _tmp._exec("insert into notifications (id, article_id, channel, chat_id, status, retry_count,"
               " created_at) values (?,?,?,?,?,?,?)",
               ("ntf-rf2", "art-rf", "telegram", "c2", "queued", 5, _d030))
    check("실패·소진 2건을 큐로 되돌림", _tmp.requeue_failed_notifications(), 2)
    check("되돌린 뒤 재시도 카운트 0",
          _tmp._one("select max(retry_count) as m from notifications where article_id='art-rf'")["m"], 0)
    _tmp.log_telegram({"chat_id": "-100", "kind": "자동 알림", "article_id": "art-core-mid",
                       "text": "🔴 [포스코퓨처엠] 시험 알림", "ok": True, "error": None})
    _tmp.log_telegram({"chat_id": "-100", "kind": "직접 전송", "article_id": None,
                       "text": "안녕하세요", "ok": False, "error": "Too Many Requests: retry after 5"})
    # 같은 초에 기록돼 정렬 타이가 나므로 시각을 명시적으로 벌린다(실사용은 1초 이상 간격).
    _tmp._exec("update telegram_log set created_at=? where kind='자동 알림'",
               (iso(now_utc() - timedelta(minutes=2)),))
    _logs = _tmp.recent_telegram_logs(10)
    check("2건 기록됨", len(_logs), 2)
    check("최신순 정렬 — 나중 기록이 맨 앞", _logs[0]["kind"], "직접 전송")
    check("성공 로그의 기사 제목 조인", _logs[1].get("title"), "제목")
    check("실패 로그에 원인 보존", "Too Many Requests" in (_logs[0]["error"] or ""), True)
    check("ok 플래그 정수 저장", (_logs[0]["ok"], _logs[1]["ok"]), (0, 1))
    # created_at 을 과거로 돌려 정리 대상으로 만든다
    _tmp._exec("update telegram_log set created_at=? where kind='직접 전송'", (_d120,))
    check("30일 넘은 발송 로그 1건 정리", _tmp.prune_telegram_log(30), 1)
    check("최근 로그는 남는다", len(_tmp.recent_telegram_logs(10)), 1)

    shutil.rmtree(_dbdir, ignore_errors=True)

    print("\n[11-3] build_card 직접 등록(manual) 플래그 + 중요도순 정렬")
    check("manual 등록 → manual=True",
          build_card({"source_type": "manual", "importance_score": 0})["manual"], True)
    check("자동 수집 → manual=False",
          build_card({"source_type": "naver_api", "importance_score": 0})["manual"], False)
    check("source_type 없음 → manual=False", build_card({})["manual"], False)
    # 중요도순 정렬 키 — 직접 등록 우선, 그다음 점수, 그다음 발행일
    _sk = lambda r: (1 if r.get("source_type") == "manual" else 0,
                     int(r.get("importance_score") or 0), r.get("published_at") or "")
    _rows_sorted = sorted([
        {"source_type": "rss", "importance_score": 90, "published_at": "2026-09-01"},
        {"source_type": "manual", "importance_score": 10, "published_at": "2026-09-02"},
        {"source_type": "rss", "importance_score": 95, "published_at": "2026-09-03"},
    ], key=_sk, reverse=True)
    check("중요도순: 직접 등록 기사가 최상단",
          _rows_sorted[0]["source_type"], "manual")
    check("중요도순: 나머지는 점수 내림차순",
          [r["importance_score"] for r in _rows_sorted[1:]], [95, 90])
    # card_tags — 제목·요약에서 실제로 계열사가 잡힐 때만 그룹사 태그. '포스코' 폴백 폐지.
    _now = iso(now_utc())
    check("제목·요약에 계열사 없으면 그룹사 태그 없음",
          card_tags({"title": "새 공장 착공식 개최", "summary_text": "회사가 착공식을 열었다.",
                     "group_companies": "[]", "categories": "[]", "analyzed_at": _now})[0], [])
    check("요약에 포스코 스친 언급만 있어도 태깅 안 함(시황 티커 방지)",
          card_tags({"title": "홍콩 증시 속락", "summary_text": "지수가 하락했다.",
                     "group_companies": "[]", "categories": "[]", "analyzed_at": _now})[0], [])
    check("저장된 group_companies 는 그대로 존중",
          card_tags({"title": "새 공장", "group_companies": '["포스코퓨처엠"]',
                     "categories": "[]"})[0], ["포스코퓨처엠"])
    check("제목에 계열사 있으면 그대로 태깅",
          card_tags({"title": "포스코퓨처엠 양극재 증설", "group_companies": "[]",
                     "categories": "[]", "analyzed_at": _now})[0], ["포스코퓨처엠"])

    # Supabase 키 자동 선별 — 두 키를 바꿔 넣어도 service_role 을 고른다
    import base64 as _b64
    _anon = ("x." + _b64.urlsafe_b64encode(b'{"role":"anon"}').decode().rstrip("=") + ".y")
    _svc = ("x." + _b64.urlsafe_b64encode(b'{"role":"service_role"}').decode().rstrip("=") + ".y")
    check("JWT role 파싱", (_jwt_role(_anon), _jwt_role(_svc)), ("anon", "service_role"))
    check("키 순서 정상이면 첫 키", _pick_supabase_service_key(_svc, _anon), _svc)
    check("키를 바꿔 넣어도 service_role 선택", _pick_supabase_service_key(_anon, _svc), _svc)
    check("role 못 읽으면 첫 키 폴백", _pick_supabase_service_key("garbage", "also-bad"), "garbage")

    # 우선 알림('항상 발송 키워드')은 제목 + 판정 그룹사로만 본다 — 본문 스친 언급 제외
    _akw = ["포스코홀딩스", "포스코퓨처엠"]
    def _prio_probe(title, groups):
        return _kw_hit_any(f"{title}\n{' '.join(groups)}", _akw)
    check("제목에 항상발송 키워드 → 우선",
          _prio_probe("포스코퓨처엠 3분기 실적", ["포스코퓨처엠"]), True)
    check("판정 그룹사에 있으면 → 우선",
          _prio_probe("퓨처엠, 광양 공장 증설", ["포스코퓨처엠"]), True)
    check("본문에만 스친 언급(제목·그룹사에 없음) → 우선 아님",
          _prio_probe("포스코 노조, 48시간 부분파업 D-1", ["포스코"]), False)
    check("항상발송 키워드 비면 우선 아님 (_kw_hit_any)",
          _kw_hit_any("포스코퓨처엠 양극재 증설", []), False)

    # 제외 키워드 — 제목에 있으면 임계값·항상발송 키워드보다 우선해서 알림 차단
    def _notify_decision(title, score, threshold, always, exclude):
        excluded = _kw_hit_any(title, [k for k in exclude if k])
        is_priority = (not excluded) and _kw_hit_any(title, [k for k in always if k])
        return (not excluded) and (score >= threshold or is_priority)
    check("제외 키워드 없음 + 임계값 초과 → 발송",
          _notify_decision("포스코퓨처엠 양극재 증설", 70, 50, [], []), True)
    check("제외 키워드 매칭 → 임계값 넘어도 차단",
          _notify_decision("포스코퓨처엠 신입 채용 공고", 70, 50, [], ["채용"]), False)
    check("제외가 항상발송 키워드를 이긴다",
          _notify_decision("포스코퓨처엠 경력 채용", 30, 50, ["포스코퓨처엠"], ["채용"]), False)
    check("제외 키워드가 제목에 없으면 정상 발송",
          _notify_decision("포스코퓨처엠 3분기 실적 발표", 30, 50, ["포스코퓨처엠"], ["채용"]), True)
    check("제외 목록이 비면 아무 영향 없음",
          _notify_decision("채용 관련 없는 기사", 60, 50, [], []), True)

    # 야간 우회 체크 — '무조건 받을 키워드' 우선 기사가 야간에 나갈지
    def _night_pass(score, n_min, is_priority, bypass):
        return score >= n_min or (is_priority and bypass)
    check("야간: 우선 기사 + 체크 켜짐 → 발송", _night_pass(40, 80, True, True), True)
    check("야간: 우선 기사 + 체크 꺼짐 → 아침 대기", _night_pass(40, 80, True, False), False)
    check("야간: 점수 높으면 체크와 무관하게 발송", _night_pass(90, 80, False, False), True)
    check("야간: n_min=101 + 우선 + 체크 → 발송", _night_pass(100, 101, True, True), True)
    check("야간: n_min=101 + 우선 + 체크 꺼짐 → 전면 억제", _night_pass(100, 101, True, False), False)

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
    # 정책·통상 주제: 관심 키워드 하나 AND 필수 공통 키워드 하나 (둘 다 필수) · 제목 제외 키워드
    def _topic_match(text, notify, required, exclude=()):
        return (_kw_hit_any(text, [k for k in notify if k])
                and _kw_hit_any(text, [k for k in required if k])
                and not _kw_hit_any(text, [k for k in exclude if k]))
    check("정책: 관심 매칭 + 필수 매칭 → 발송",
          _topic_match("산업용 전기요금 인하", ["전기요금"], ["산업"]), True)
    check("정책: 관심 매칭 but 필수 불일치 → 제외",
          _topic_match("가정용 전기요금 인하", ["전기요금"], ["산업"]), False)
    check("정책: 관심 목록 비면 발송 안 함(둘 다 필수)",
          _topic_match("산업용 전기요금 인하", [], ["산업"]), False)
    check("정책: 필수 목록 비면 발송 안 함",
          _topic_match("산업용 전기요금 인하", ["전기요금"], []), False)
    check("정책: 조건 맞아도 제목에 제외 키워드 → 제외",
          _topic_match("산업용 전기요금 인하 공청회 채용 공고", ["전기요금"], ["산업"], ["채용"]), False)
    # 주제 매칭이면 일반 임계값을 우회한다 (정책·통상 기사는 포스코 미언급이라 점수가 낮다)
    def _should_send(score, threshold, priority, topic_hit):
        return score >= threshold or priority or topic_hit
    check("정책 주제 매칭 → 점수 낮아도 발송", _should_send(20, 60, False, True), True)
    check("정책 주제 매칭 안 되고 점수도 낮으면 → 웹에만", _should_send(20, 60, False, False), False)
    check("주제 무관 기사는 여전히 임계값 필요", _should_send(70, 60, False, False), True)
    # 야간: 주제 매칭(priority 2)은 야간 우회 안 함 — priority 1만 (체크 시)
    def _night_ok(score, n_min, prio_val, bypass):
        return score >= n_min or (prio_val == 1 and bypass)
    check("야간: 정책 주제(prio 2) → 아침 대기", _night_ok(20, 80, 2, True), False)
    check("야간: 무조건 받을 키워드(prio 1) + 체크 → 발송", _night_ok(20, 80, 1, True), True)

    print("\n[13-2c] 카카오 '나에게 보내기'")
    _blank = {f: "" for f in Config.__dataclass_fields__}
    _blank.update(smtp_port=587, api_port=8000, poll_interval_sec=300, naver_interval_sec=300,
                  fresh_cutoff_hours=6, backfill_cutoff_hours=72, notify_threshold=50,
                  llm_daily_limit=1500, llm_per_run=6, weekly_enabled=False, weekly_to=[],
                  weekly_hour=7, article_retention_days=550, tz_offset_hours=9,
                  night_start_hour=23, night_end_hour=7, night_min_score=80)
    _kc = Config(**{**_blank, "kakao_feature_enabled": True, "kakao_rest_api_key": "K",
                    "kakao_client_secret": "S", "kakao_redirect_uri": "https://localhost:3000/kakao"})
    check("kakao_configured — 켜짐 + 키·redirect 있으면 True", _kc.kakao_configured, True)
    check("kakao_configured — 키 없으면 False", Config(**_blank).kakao_configured, False)
    check("kakao_configured — KAKAO_ENABLED 없으면(기본) False",
          Config(**{**_blank, "kakao_rest_api_key": "K",
                    "kakao_redirect_uri": "https://localhost:3000/kakao"}).kakao_configured, False)
    _au = kakao_authorize_url(_kc)
    check("authorize URL — scope=talk_message", "scope=talk_message" in _au, True)
    check("authorize URL — response_type=code", "response_type=code" in _au, True)
    check("authorize URL — redirect_uri 인코딩", "redirect_uri=https%3A%2F%2Flocalhost" in _au, True)

    class _KCtx:
        def __init__(self, cfg, st):
            self.cfg = cfg
            self.storage = type("S", (), {"get_run_state": lambda s: st})()
    check("kakao_enabled_now — refresh_token 없으면 False",
          kakao_enabled_now(_KCtx(_kc, {"kakao_enabled": 1})), False)
    check("kakao_enabled_now — 토큰 있고 토글 켜짐 → True",
          kakao_enabled_now(_KCtx(_kc, {"kakao_enabled": 1, "kakao_refresh_token": "r"})), True)
    check("kakao_enabled_now — 토큰 있어도 토글 꺼짐 → False",
          kakao_enabled_now(_KCtx(_kc, {"kakao_enabled": 0, "kakao_refresh_token": "r"})), False)
    check("kakao_enabled_now — 앱 키 없으면 False",
          kakao_enabled_now(_KCtx(Config(**_blank), {"kakao_refresh_token": "r"})), False)

    _tt = _kakao_text_template("제목", "https://x.test/a")
    check("text 템플릿 — object_type=text", _tt["object_type"], "text")
    check("_clip — 안 넘치면 그대로", _clip("가나다", 5), "가나다")
    check("_clip — 넘치면 … 부착", _clip("가나다라마바", 4), "가나다…")
    _fr = {"title": "포스코퓨처엠 양극재 증설", "importance_score": 85,
           "group_companies": '["포스코퓨처엠"]', "press_name": "뉴스1", "author": "김기자",
           "summary_text": "짧은 요약 문장.", "perspective_text": "관점 문장",
           "url_canonical": "https://x.test/a"}
    _ft = _kakao_text_from_row(_fr, "https://x.test/a")
    check("row text — object_type=text", _ft["object_type"], "text")
    check("row text — 점수 85 는 🔴", _ft["text"].startswith("🔴"), True)
    check("row text — 태그가 머리줄에", "[포스코퓨처엠]" in _ft["text"], True)
    check("row text — 언론사·기자 머리표", "[뉴스1, 김기자]" in _ft["text"], True)
    check("row text — 포스코 관점 포함", "포스코 관점:" in _ft["text"], True)
    check("row text — 링크는 본문 아닌 link 필드", "http" not in _ft["text"], True)
    check("row text — 원문 버튼", _ft["button_title"], "원문 보기")
    check("row text — 긴 요약도 200자 이하",
          len(_kakao_text_from_row({**_fr, "summary_text": "요" * 500,
                                    "perspective_text": "관" * 500},
                                   "https://x.test/a")["text"]) <= 200, True)

    print("\n[13-2d] 배포 안전장치 — 시간대 · 세션 · SSRF · 호출 상한")
    # 시간대: 클라우드는 UTC 로 돌기 때문에 야간 억제·주간 레포트가 어긋난다.
    _tz_before = APP_TZ
    set_app_tz(9)
    check("APP_TZ +9 로 설정", now_local().utcoffset(), timedelta(hours=9))
    set_app_tz(0)
    check("APP_TZ 0 (UTC) 로 설정", now_local().utcoffset(), timedelta(hours=0))
    check("같은 순간이라도 TZ 에 따라 시(hour)가 다르다",
          (now_local().hour - datetime.now(timezone(timedelta(hours=9))).hour) % 24, 15)
    set_app_tz(99)   # 범위를 벗어난 값은 잘라 낸다
    check("APP_TZ 상한 +14 로 제한", now_local().utcoffset(), timedelta(hours=14))
    globals()["APP_TZ"] = _tz_before   # 다른 검증에 영향 없도록 복원

    # 야간 억제 창 — 자정을 넘는 창(23→7)과 낮 창(9→18) 양쪽
    check("야간 23→7: 새벽 2시는 야간", _in_night_window(2, 23, 7), True)
    check("야간 23→7: 23시는 야간", _in_night_window(23, 23, 7), True)
    check("야간 23→7: 7시는 야간 아님(경계 제외)", _in_night_window(7, 23, 7), False)
    check("야간 23→7: 낮 12시는 야간 아님", _in_night_window(12, 23, 7), False)
    check("야간 22→6 으로 바꾸면 22시가 야간", _in_night_window(22, 22, 6), True)
    check("창이 낮(9→18)이면 자정 넘지 않게 처리", _in_night_window(3, 9, 18), False)
    check("start==end 면 창 없음", _in_night_window(5, 0, 0), False)

    # effective_night — run_state(마스터 패널) 우선, 없으면 .env(Config)
    _ncfg = Config(**{**_blank, "night_start_hour": 23, "night_end_hour": 7, "night_min_score": 80})
    _NCtx = type("C", (), {})()
    _NCtx.cfg = _ncfg
    check("effective_night — run_state 비면 .env 값", effective_night(_NCtx, {}), (23, 7, 80))
    check("effective_night — run_state 가 .env 를 덮어씀",
          effective_night(_NCtx, {"night_start_hour": 22, "night_end_hour": 6, "night_min_score": 101}),
          (22, 6, 101))
    check("effective_night — 일부만 지정되면 나머지는 .env",
          effective_night(_NCtx, {"night_min_score": 50}), (23, 7, 50))
    check("_int_or — None 이면 fallback", _int_or(None, 9), 9)
    check("_int_or — 빈 문자열도 fallback", _int_or("", 9), 9)
    check("_int_or — 숫자면 그 값", _int_or("22", 9), 22)

    # 웹 세션 토큰 — 비밀번호에서 파생되므로 재시작에 안전하고 변경 시 자동 무효화
    check("같은 비밀번호 → 같은 토큰",
          web_session_token("pw1") == web_session_token("pw1"), True)
    check("비밀번호가 바뀌면 토큰도 바뀜",
          web_session_token("pw1") == web_session_token("pw2"), False)
    check("토큰은 비밀번호를 노출하지 않음", "pw1" in web_session_token("pw1"), False)

    # SSRF — 서버가 사내망·메타데이터 주소를 대신 긁지 않게 막는다 (IP 리터럴이라 DNS 불필요)
    check("루프백 차단", is_public_http_url("http://127.0.0.1/a"), False)
    check("사설망 10.x 차단", is_public_http_url("http://10.0.0.5/a"), False)
    check("사설망 192.168.x 차단", is_public_http_url("http://192.168.0.1/a"), False)
    check("클라우드 메타데이터 169.254 차단",
          is_public_http_url("http://169.254.169.254/latest/meta-data/"), False)
    check("localhost 이름 차단", is_public_http_url("http://localhost:8000/a"), False)
    check("공인 IP 는 통과", is_public_http_url("http://8.8.8.8/a"), True)
    check("호스트 없으면 차단", is_public_http_url("http:///a"), False)

    # URL 등록 시간당 상한 — 요청 1건이 LLM 호출 1건(과금)이라 비용을 묶어 둔다
    _rl: list[float] = []
    check("상한까지는 허용", all(_rate_ok(_rl, 3) for _ in range(3)), True)
    check("상한 초과는 차단", _rate_ok(_rl, 3), False)
    check("창(window)이 지나면 다시 허용", _rate_ok(_rl, 3, window=0.0), True)

    print("\n[13-2b] 무조건 발송 점수 (hard_notify_score) — 우선 판정")
    # run_once 와 같은 판정: 우선 = 항상발송키워드 매칭 OR (hard>0 AND score>=hard)
    # 우선 기사는 발송 단계에서 임계값·야간 게이트를 우회한다. (다이제스트는 폐지됨)
    def _is_prio(score, hard, kw_hit=False):
        return kw_hit or (hard > 0 and score >= hard)
    check("hard=80, 점수 85 → 우선(야간 우회)", _is_prio(85, 80), True)
    check("hard=80, 점수 70 → 우선 아님", _is_prio(70, 80), False)
    check("hard=0(미사용) → 점수 100 이어도 우선 아님", _is_prio(100, 0), False)
    check("hard 미달이어도 키워드 매칭이면 우선", _is_prio(10, 80, kw_hit=True), True)

    # 안정화 게이트: 메타만 저장분(deferred)은 억제 유지 사유가 아니다. (조사 2026-09-08)
    def _stabilized(fresh_avail, new_cnt, body_backlog, defer_backlog):
        return (fresh_avail < 20 and new_cnt < 10
                and body_backlog < 30 and defer_backlog < DEFER_BACKLOG_STABLE)
    check("본문 대기 큐만 잦아들면 deferred 200건이어도 안정",
          _stabilized(5, 3, 10, 200), True)
    check("본문 대기 큐가 크면 억제 유지", _stabilized(5, 3, 120, 0), False)
    check("deferred 가 상한을 넘으면 억제 유지", _stabilized(5, 3, 10, DEFER_BACKLOG_STABLE + 1), False)

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
    _html_full = '<meta property="og:title" content="부지·보조금만으로 투자 안해 전문가들 미싱링크 연결로 산업 경쟁력 키워야">'
    check("네이버가 자른 제목을 원문 제목으로 복원",
          repair_truncated_title("부지·보조금만으로 투자 안해… 전문가들, 미싱링크 연결로 산업...", _html_full),
          "부지·보조금만으로 투자 안해 전문가들 미싱링크 연결로 산업 경쟁력 키워야")
    check("안 잘린 제목은 그대로 둔다",
          repair_truncated_title("포스코퓨처엠 양극재 증설", _html_full), "포스코퓨처엠 양극재 증설")
    check("앞부분이 다르면 오교체하지 않는다",
          repair_truncated_title("전혀 다른 기사 제목입니다...", _html_full), "전혀 다른 기사 제목입니다...")
    # 수동 등록 발송 판정: 점수 임계값 이상 또는 우선일 때만 (사용자 지정 2026-09-08).
    # is_backfill·suppressed 는 무시하지만 임계값·야간·telegram_enabled 는 존중한다.
    def _manual_send_ok(score, threshold, is_priority):
        return score >= threshold or is_priority
    check("수동 등록: 점수 70 ≥ 임계값 50 → 발송", _manual_send_ok(70, 50, False), True)
    check("수동 등록: 점수 20 < 임계값 50, 우선 아님 → 웹에만", _manual_send_ok(20, 50, False), False)
    check("수동 등록: 점수 낮아도 우선이면 발송", _manual_send_ok(10, 50, True), True)

    print("\n[15] 주간 레포트 (월요일 이메일)")
    _ws, _we = weekly_window(datetime(2026, 9, 7, 7, 0, tzinfo=timezone.utc))
    check("집계 구간은 7일", (_we - _ws).days, 7)
    check("섹션 7개", len(WEEKLY_SECTIONS), 7)
    check("그룹사 5 + 주제 2",
          (sum(1 for _, k, _ in WEEKLY_SECTIONS if k == "group"),
           sum(1 for _, k, _ in WEEKLY_SECTIONS if k == "topic")), (5, 2))
    _rows = [
        {"title": "포스코퓨처엠 양극재 증설", "importance_score": 80,
         "group_companies": '["포스코퓨처엠"]', "categories": "[]", "published_at": "2026-09-05T00:00:00Z"},
        {"title": "포스코퓨처엠 소식 2", "importance_score": 30,
         "group_companies": '["포스코퓨처엠"]', "categories": "[]", "published_at": "2026-09-04T00:00:00Z"},
        {"title": "무관 기사", "importance_score": 90,
         "group_companies": "[]", "categories": '["산업"]', "published_at": "2026-09-05T00:00:00Z"},
    ]
    _picked = _weekly_pick(_rows, "group", "포스코퓨처엠")
    check("섹션 매칭 + 중요도 정렬", [a["title"] for a in _picked],
          ["포스코퓨처엠 양극재 증설", "포스코퓨처엠 소식 2"])
    check("무관 기사는 제외", any("무관" in a["title"] for a in _picked), False)
    _html = render_weekly_html({
        "period_start": "2026-08-31T00:00:00Z", "period_end": "2026-09-07T00:00:00Z",
        "article_count": 12, "sections": [
            {"label": "포스코퓨처엠", "kind": "group",
             "articles": [{"title": "제목<&>", "url": "http://a", "press": "머니투데이",
                           "published_at": "2026-09-05", "score": 70, "summary": "요약"}],
             "swot": {"s": "강점", "w": "약점", "o": "기회", "t": "위협"}, "impact": None},
            {"label": "정부/정책", "kind": "topic", "articles": [], "swot": None, "impact": ""},
        ]})
    check("HTML 렌더 + 이스케이프", "제목&lt;&amp;&gt;" in _html and "강점" in _html, True)
    check("기사 없는 섹션 안내", "이번 주 해당 기사가 없습니다" in _html, True)
    check("plain 대체본은 태그 제거", "<" not in _html_to_text("<p>가<b>나</b>다</p>"), True)
    _cfg_blank = Config(**{**{f: "" for f in Config.__dataclass_fields__},
                           "smtp_port": 587, "api_port": 8000, "poll_interval_sec": 300,
                           "naver_interval_sec": 300, "fresh_cutoff_hours": 6, "backfill_cutoff_hours": 72,
                           "notify_threshold": 50, "llm_daily_limit": 1500, "llm_per_run": 6,
                           "weekly_enabled": False, "weekly_to": [], "weekly_hour": 7})
    _ok, _err = send_report_email(_cfg_blank, "제목", "<p>본문</p>", ["a@b.com"])
    check("SMTP 미설정이면 발송 스킵", (_ok, "SMTP 미설정" in (_err or "")), (False, True))
    _cfg_creds = replace(_cfg_blank, smtp_user="x@gmail.com", smtp_app_password="pw")
    _ok2, _err2 = send_report_email(_cfg_creds, "제목", "<p>본문</p>", [])
    check("수신자 없으면 발송 스킵", (_ok2, "수신자" in (_err2 or "")), (False, True))
    check("주간 HTML 헤더는 .wr-head 로 감싼다", 'class="wr-head"' in _html, True)

    print()
    if ea_mod is not None:
        print("\n[16] 대외협력 — 관련성·영향도 판정 규칙")
        # 부처명만으로는 통과시키지 않는다 (관심 부처의 일반 행정·조세 개정안 차단)
        check("산업 키워드 있으면 통과",
              ea_mod.is_relevant("이차전지 특화단지 지정 고시", ""), True)
        check("부처명만 있으면 탈락",
              ea_mod.is_relevant("국토교통부와 그 소속기관 직제 일부개정령안",
                                 "국토교통부와 그 소속기관 직제"), False)
        # 부처명이 차단되는 것은 keyword_sets 를 빌려 쓸 때뿐이다.
        # 호출자가 extra_terms 로 직접 지정하면 그 의도는 존중한다.
        check("extra_terms 로 명시하면 그 단어는 통과",
              ea_mod.is_relevant("국토교통부와 그 소속기관 직제 일부개정령안", "",
                                 extra_terms=["국토교통부"]), True)
        # medium 이상은 조문 인용 또는 원문 직접 인용이 있어야 한다
        check("조문 번호 인용 인정", bool(ea_mod._EA_CITE.search("안 제6조의2 신설")), True)
        check("항·호 인용 인정", bool(ea_mod._EA_CITE.search("제2항을 삭제한다")), True)
        check("원문 직접 인용 인정",
              bool(ea_mod._EA_CITE.search('원문은 "종합건설업체가 자본력과 수주 우위"로 적었다')), True)
        check("근거 없는 문장은 불인정",
              bool(ea_mod._EA_CITE.search("전반적으로 부담이 커질 수 있다")), False)
        check("빈 근거는 불인정", bool(ea_mod._EA_CITE.search("")), False)
        # 그룹사는 규칙으로만 판정한다 (LLM 미사용)
        check("철강 → 포스코", ea_mod.detect_ea_groups("탄소중립 설비 고시"), ["포스코"])
        check("건설 → 포스코이앤씨",
              ea_mod.detect_ea_groups("건설산업기본법 일부개정법률안"), ["포스코이앤씨"])
        check("무관 제목은 그룹사 없음",
              ea_mod.detect_ea_groups("국토교통부와 그 소속기관 직제"), [])
        # 카테고리 5개 + item_type 매핑
        check("카테고리 5개", [c["key"] for c in ea_mod.EA_CATEGORIES],
              ["notice", "bill", "policy", "trade", "ministry"])
        check("부처별 동향 → ministry_news", ea_mod.EA_CATEGORY_TYPES["ministry"], ["ministry_news"])
        check("통상 환경 → trade_news", ea_mod.EA_CATEGORY_TYPES["trade"], ["trade_news"])
        # 발표 기관 추출 — author(정책브리핑) 우선, 없으면 제목 첫머리 약칭
        check("author 가 부처면 기관으로", ea_mod._article_agency({"author": "산업통상부", "title": "x"}),
              "산업통상부")
        check("author 약칭은 정식명으로", ea_mod._article_agency({"author": "산업부", "title": "x"}),
              "산업통상부")
        check("제목 첫머리 부처 약칭 인식",
              ea_mod._article_agency({"author": "김기자", "title": "국토부, 주택공급 확대 방안 발표"}),
              "국토교통부")
        check("기자명뿐이고 제목에도 부처 없으면 빈값",
              ea_mod._article_agency({"author": "홍길동", "title": "포스코퓨처엠 양극재 증설"}), "")
        # 부처 정책뉴스 RSS 파서 — <item> 에서 제목·링크·본문(태그 제거)
        _rss = ('<rss><channel><item><title><![CDATA[제1회 협의회 개최]]></title>'
                '<link><![CDATA[https://x/1/view]]></link><pubDate><![CDATA[2026-09-08]]></pubDate>'
                '<description><![CDATA[<p>차관은 회의에 참석해 논의하였다.</p>]]></description>'
                '</item></channel></rss>')
        _parsed = ea_crawl_mod._rss_items(_rss) if ea_crawl_mod else []
        check("RSS item 제목·본문 파싱",
              (_parsed[0]["title"], "차관은 회의에" in _parsed[0]["description"]) if _parsed else None,
              ("제1회 협의회 개최", True))

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
  repeople [N|all]  인사·부고 기사를 사람별 구조 요약으로 다시 만듦 (구조화 안 된 것만, all 이면 전부)
  fixlinks   홈으로 잘못 연결된 카드 링크(url_canonical) 보정 (일회성)
  fixdates   미래로 저장된 발행시각 보정 (타임존 오파싱 복구 — 일회성)
  once [N]   파이프라인 1회 실행 (N 을 주면 LLM 호출을 N건으로 제한 — 검증용)
  serve      API + 프론트엔드 서버만 실행
  run        서버 + 수집 루프 + 텔레그램 챗봇 (운영 모드)
  quotes     시세만 1회 갱신
  notify     대기 중인 텔레그램 알림만 발송
  retryfailed  발송 실패로 막힌 알림을 다시 큐로 되돌림 (rate limit 등 일시 장애 복구용)
  chatid     텔레그램 chat_id 확인 (봇에게 메시지를 한 번 보낸 뒤 실행)
  sendtest   텔레그램 시험 메시지 1건 발송 (연결 확인용)
  kakao-auth   카카오 '나에게 보내기' 최초 토큰 발급 (브라우저 동의 → code 붙여넣기, 1회)
  kakao-test   카카오 '나에게 보내기' 시험 발송 1건
  ea-collect      대외협력(입법·행정예고·국회) 즉시 1회 수집·분석
  migrate    SQLite → Supabase 전체 이관 (schema.sql 배포 + DB_BACKEND=supabase 후, 일회성)
  weekly [--dry]  주간 레포트 즉시 생성·발송 (--dry 면 생성·저장만, 이메일 없음)
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
    elif command == "repeople":
        rest = argv[2:]
        force = "all" in rest
        nums = [int(a) for a in rest if a.isdigit()]
        cmd_repeople(ctx, nums[0] if nums else (200 if force else 60), force=force)
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
    elif command == "retryfailed":
        n = ctx.storage.requeue_failed_notifications()
        log.info("발송 실패 %d건을 큐로 되돌렸습니다. 다음 발송 주기(또는 notify 명령)에 재시도됩니다.", n)
    elif command == "chatid":
        cmd_chatid(ctx)
    elif command == "sendtest":
        cmd_sendtest(ctx)
    elif command == "kakao-auth":
        cmd_kakao_auth(ctx)
    elif command == "kakao-test":
        cmd_kakao_test(ctx)
    elif command == "weekly":
        dry = "--dry" in argv[2:]
        rep = run_weekly_report(ctx, send=not dry)
        if dry:
            log.info("주간 레포트 생성·저장 완료 (id=%s, 발송 안 함)", rep["id"])
        elif rep.get("send_error"):
            raise SystemExit(f"발송 실패: {rep['send_error']}")
    elif command == "ea-collect":
        if ea_mod is None:
            raise SystemExit("external_affairs 모듈을 불러오지 못했습니다.")
        ea_mod.collect_once(ctx, ea_mod.EaDB(ctx.cfg.sqlite_path))
    elif command == "migrate":
        cmd_migrate(ctx)
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
