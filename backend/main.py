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
import socket
import sqlite3
import sys
import tempfile
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
from functools import lru_cache
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
    # 대체 공급자 (선택) — OpenAI 호출이 실패하면 이 키가 있을 때만 시도한다.
    # NVIDIA NIM 은 OpenAI 호환 엔드포인트라 openai 라이브러리를 base_url 만 바꿔 그대로 쓴다.
    nvidia_embed_api_key: str
    nvidia_embed_model: str
    nvidia_llm_api_key: str
    nvidia_llm_model: str
    # 채팅 대체 순서: OpenAI → Gemini → Grok → NVIDIA. 임베딩 API 가 없는
    # 공급자(Gemini·xAI)는 채팅(분석·요약·주간레포트·챗봇)에만 쓴다.
    gemini_api_key: str
    gemini_llm_model: str
    xai_api_key: str
    xai_llm_model: str
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
    master_pw_recovery_to: list[str]  # 마스터 로그인 3회 이상 실패 시 복구 메일 받을 주소
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
        # 선택 항목 — 비어 있으면 대체 없이 기존처럼 OpenAI 실패를 그냥 넘어간다.
        nvidia_embed_api_key=get_env("NVIDIA_EMBED_API_KEY", ""),
        nvidia_embed_model=get_env("NVIDIA_EMBED_MODEL", "nvidia/nemotron-3-embed-1b"),
        nvidia_llm_api_key=get_env("NVIDIA_LLM_API_KEY", ""),
        nvidia_llm_model=get_env("NVIDIA_LLM_MODEL", "google/gemma-4-31b-it"),
        gemini_api_key=get_env("GEMINI_API_KEY", ""),
        gemini_llm_model=get_env("GEMINI_LLM_MODEL", "gemini-2.5-flash-lite"),
        xai_api_key=get_env("XAI_API_KEY", ""),
        xai_llm_model=get_env("XAI_LLM_MODEL", "grok-4-fast"),
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
        master_pw_recovery_to=[e.strip() for e in get_env(
            "MASTER_PW_RECOVERY_EMAILS",
            "taehyun931008@naver.com,taehyun108@poscofuturem.com",
        ).replace(";", ",").split(",") if e.strip()],
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
        """본문 저장 (§7-3). 분석 후에도 '본문' 검색용으로 30일 보관하고 cleanup_bodies 가 지운다.
        분석 실패 시에만 delete_body 로 즉시 지워 백로그 큐에서 뺀다."""

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
    def clear_ledger(self, reasons: Sequence[str], since_days: int) -> int:
        """최근 since_days 일 안에 원장에 오른 항목 중 사유가 reasons 인 것을 지운다(다음 수집 때 다시 판정).
        반환: 지운 건수. 수집 규칙을 넓힌 뒤 예전에 '무관'으로 걸러진 기사를 되살릴 때 쓴다."""

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
    def set_perspective(self, article_id: str, text: str) -> None:
        """summaries.perspective_text 만 바꾼다(요약·생성시각은 그대로 — 일일 LLM 호출 집계가 늘지 않는다)."""

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
    def pfm_articles(self, since: str, with_excerpt: bool = True) -> list[dict]:
        """since 이후 발행된 활성·대표·분석완료 기사 중 그룹사 태그에 포스코퓨처엠이 있는 것.
        언론사 탭 집계·논조 백필용 경량 컬럼만. 집계는 발췌 본문이 필요 없어 with_excerpt=False."""

    @abstractmethod
    def body_of(self, article_id: str) -> str | None:
        """보관 중인 본문(최근 30일). 없으면 None."""

    @abstractmethod
    def body_match_ids(self, term: str, limit: int = 5000) -> set[str]:
        """보관 중인 본문(최근 30일)에 term 이 들어 있는 기사 id. 대소문자 무시."""

    @abstractmethod
    def article_press_rows(self) -> list[dict]:
        """모든 기사의 id·press_id·press_name·URL 만 가볍게 읽는다 (언론사명 정비용)."""

    @abstractmethod
    def queue_notification(self, article_id: str, chat_id: str, status: str, priority: int = 0,
                           channel: str = "telegram") -> bool: ...

    @abstractmethod
    def pending_notifications(self, limit: int, channel: str = "telegram") -> list[dict]:
        """발송 대기 큐. channel='telegram' = 부서용(기본), 'telegram_public' = 일반용 — 서로 섞이지 않는다."""

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
    "pfm_excerpt", "pfm_tone", "pfm_tone_reason",
))

# 목록 스캔(필터·집계)에만 필요한 컬럼. 카드 렌더 전용 컬럼(SWOT 근거문 4개·포스코 관점·
# summary_source·URL 원본·해시·dedup 키)은 뺀다 — 수천 행에서 그게 비용의 대부분이다.
# card_tags 가 그룹사 폴백에 summary_text 를 쓰므로 그것만 남긴다.
ARTICLE_SCAN_COLS = ", ".join(f"a.{c}" for c in (
    "id", "url_canonical", "url_original", "title", "press_name", "author",
    "published_at", "source_type", "thumbnail_url", "is_backfill",
    "importance_score", "sentiment", "keywords", "group_companies", "categories",
    "analyzed_at",
    # 포스코퓨처엠 발췌(상세 검색 '본문' 대체 대상) + 논조(언론사 탭 집계) — 해당 기사만 값이 있다
    "pfm_excerpt", "pfm_tone",
))


# 언론사 탭 집계·논조 백필 전용 컬럼 (포스코퓨처엠 태그 기사만 읽는다).
PFM_ARTICLE_COLS = ("id, title, url_canonical, url_original, press_name, author, published_at,"
                    " pfm_tone, pfm_tone_reason")


def _pfm_cols(with_excerpt: bool) -> str:
    return PFM_ARTICLE_COLS + (", pfm_excerpt" if with_excerpt else "")


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
            # 포스코퓨처엠 발췌·논조 (2026-09-29) — 카드 발췌 표시 + 언론사 탭 집계
            "alter table articles add column pfm_excerpt TEXT",
            "alter table articles add column pfm_tone TEXT",
            "alter table articles add column pfm_tone_reason TEXT",
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

    def clear_ledger(self, reasons: Sequence[str], since_days: int) -> int:
        if not reasons:
            return 0
        cutoff = iso(now_utc() - timedelta(days=since_days))
        marks = ",".join("?" * len(reasons))
        cur = self._exec(f"delete from url_ledger where reason in ({marks}) and first_seen >= ?",
                         (*reasons, cutoff))
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

    def set_perspective(self, article_id: str, text: str) -> None:
        self._exec("update summaries set perspective_text=? where article_id=?", (text, article_id))

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

    def pfm_articles(self, since: str, with_excerpt: bool = True) -> list[dict]:
        return self._rows(
            f"select {_pfm_cols(with_excerpt)} from articles"
            " where status='active' and is_representative=1 and analyzed_at is not null"
            " and published_at >= ? and group_companies like ?"
            " order by published_at desc",
            (since, '%"포스코퓨처엠"%'))

    def body_of(self, article_id: str) -> str | None:
        row = self._one("select body from article_bodies where article_id=?", (article_id,))
        return row["body"] if row else None

    def body_match_ids(self, term: str, limit: int = 5000) -> set[str]:
        if not term:
            return set()
        rows = self._rows("select article_id from article_bodies where body like ? limit ?",
                          (f"%{term}%", limit))
        return {r["article_id"] for r in rows}

    def article_press_rows(self) -> list[dict]:
        return self._rows("select id, press_id, press_name, url_canonical, url_original,"
                          " url_source from articles")

    def sync_article_press_names(self) -> int:
        cur = self._exec(
            "update articles set press_name = (select name from press_outlets where id = articles.press_id)"
            " where press_id is not null"
            "   and press_name is not (select name from press_outlets where id = articles.press_id)"
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def queue_notification(self, article_id: str, chat_id: str, status: str, priority: int = 0,
                           channel: str = "telegram") -> bool:
        try:
            self._exec(
                "insert into notifications (id,article_id,channel,chat_id,status,priority,retry_count,created_at)"
                " values (?,?,?,?,?,?,0,?)",
                (new_id(), article_id, channel, chat_id, status, int(priority), iso(now_utc())),
            )
            return True
        except sqlite3.IntegrityError:
            # 이미 큐에 있음 = 중복 발송 방지가 작동한 것. (§6 정합성)
            return False

    def pending_notifications(self, limit: int, channel: str = "telegram") -> list[dict]:
        return self._rows(
            "select n.*, a.title, a.url_canonical, a.url_original, a.press_name, a.author,"
            " a.importance_score, a.published_at, a.group_companies, a.source_type, a.pfm_excerpt,"
            " s.summary_text"
            " from notifications n"
            " join articles a on a.id = n.article_id"
            " left join summaries s on s.article_id = a.id"
            " where n.status='queued' and n.retry_count < 3 and n.channel = ?"
            " order by a.importance_score desc, n.created_at asc limit ?",
            (channel, limit),
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
        # 본문을 30일 보관하므로(분석 끝난 것 포함) 서버에서 미분석·활성만 거른다 —
        # 안 거르면 매 사이클 보관 중인 본문 수천 건을 통째로 내려받는다.
        rows = (self._t("article_bodies")
                .select("body,summary_source,articles!inner(id,title,press_id,press_name,importance_score,group_companies,analyzed_at,status)")
                .is_("articles.analyzed_at", "null").eq("articles.status", "active")
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
        bodies = {r["article_id"] for r in self._page(lambda: (
            self._t("article_bodies").select("article_id,articles!inner(analyzed_at,status)")
            .is_("articles.analyzed_at", "null").eq("articles.status", "active")), cap=20000)}
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

    def clear_ledger(self, reasons: Sequence[str], since_days: int) -> int:
        if not reasons:
            return 0
        cutoff = iso(now_utc() - timedelta(days=since_days))
        res = (self._t("url_ledger").delete().in_("reason", list(reasons))
               .gte("first_seen", cutoff).execute())
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

    def set_perspective(self, article_id: str, text: str) -> None:
        self._t("summaries").update({"perspective_text": text}).eq("article_id", article_id).execute()

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

    def pfm_articles(self, since: str, with_excerpt: bool = True) -> list[dict]:
        # group_companies 는 jsonb 배열 — cs(포함) 연산자에 JSON 배열 문자열을 넘긴다.
        cols = _pfm_cols(with_excerpt).replace(" ", "")
        return self._page(lambda: (
            self._t("articles").select(cols)
            .eq("status", "active").eq("is_representative", True)
            .not_.is_("analyzed_at", "null").gte("published_at", since)
            .filter("group_companies", "cs", jdump(["포스코퓨처엠"]))
            .order("published_at", desc=True)), cap=50000)

    def body_of(self, article_id: str) -> str | None:
        rows = (self._t("article_bodies").select("body")
                .eq("article_id", article_id).execute().data) or []
        return rows[0]["body"] if rows else None

    def body_match_ids(self, term: str, limit: int = 5000) -> set[str]:
        if not term:
            return set()
        return {r["article_id"] for r in self._page(lambda: (
            self._t("article_bodies").select("article_id").ilike("body", f"%{term}%")), cap=limit)}

    def article_press_rows(self) -> list[dict]:
        return self._page(lambda: self._t("articles").select(
            "id,press_id,press_name,url_canonical,url_original,url_source"), cap=200000)

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

    def queue_notification(self, article_id: str, chat_id: str, status: str, priority: int = 0,
                           channel: str = "telegram") -> bool:
        try:
            self._t("notifications").insert({
                "article_id": article_id, "channel": channel, "chat_id": chat_id,
                "status": status, "priority": int(priority), "retry_count": 0,
                "created_at": iso(now_utc()),
            }).execute()
            return True
        except Exception as exc:
            if "duplicate" in str(exc).lower() or "23505" in str(exc):
                return False
            raise

    def pending_notifications(self, limit: int, channel: str = "telegram") -> list[dict]:
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
                    "importance_score,published_at,group_companies,source_type,pfm_excerpt,"
                    "summaries(summary_text))")
            .eq("status", "queued").lt("retry_count", 3).eq("channel", channel)
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
        bodies = (self._t("article_bodies")
                  .select("article_id,fetched_at,summary_source,articles!inner(analyzed_at,status)")
                  .is_("articles.analyzed_at", "null").eq("articles.status", "active")
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
     ["포스코", "포스코홀딩스", "포스코퓨처엠", "포스코DX", "포스코인터내셔널", "포스코이앤씨", "POSCO",
      "배터리협회", "한국배터리산업협회", "KBIA"]]
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
        "이차전지 공급망 재편", "배터리 디리스킹", "철강 통상마찰", "글로벌 관세 전쟁",
        # 한미 관세협상·대미투자 (2026-09-30)
        "트럼프 관세", "대미투자", "한미 관세협상", "상호관세", "자동차 관세", "미국 무역협상"]]
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
    "andongmbc.co.kr": ("안동MBC", 3), "itworld.co.kr": ("ITWorld Korea", 3), "msn.com": ("MSN", 3),
    "busan.com": ("부산일보", 2), "tjb.co.kr": ("TJB 대전방송", 3), "vop.co.kr": ("민중의소리", 3),
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
    # 포스코 계열사는 아니지만 함께 추적하는 기관 (사용자 지정 2026-09-30) — 필터 '그룹사' 칩에 같이 나온다.
    "배터리협회": ["한국배터리산업협회", "배터리산업협회", "한국전지산업협회", "배터리협회", "KBIA"],
    # 상위 개념 — 계열사가 특정되면 제거됨
    "포스코": ["포스코", "POSCO"],
}

# '그룹사' 칩에는 나오지만 포스코 계열이 아닌 항목 — 이것만 잡혔다고 해서 상위 개념 '포스코'
# 태그를 빼거나(normalize_group_list) 붙이지 않는(detect_group_companies) 일이 없어야 한다.
NON_POSCO_GROUPS = frozenset({"배터리협회"})

# 별칭 소문자 사전계산 — detect_group_companies·score_article 이 호출마다
# `[a.lower() for a in aliases]` 를 다시 만들던 것을 제거한다(파이프라인·API 공통 경로).
def alias_variants(alias: str) -> list[str]:
    """별칭의 띄어쓰기 변형 — '포스코퓨처엠' → ['포스코퓨처엠', '포스코 퓨처엠'].

    기사에는 '포스코 퓨처엠'·'POSCO 퓨처엠'처럼 띄어 쓴 표기가 흔한데 예전엔 인식하지 못해, 제목에 이름이 있는
    기사도 '언급 없음'으로 판정됐다(2026-10-01 fixpfm 이 포항남부서-포스코 퓨처엠 기사 태그를 잘못 뗌).
    상위 개념 '포스코'(3글자) 자체는 변형을 만들지 않는다.
    """
    a = alias.lower()
    out = [a]
    for prefix in ("포스코", "posco"):
        if a.startswith(prefix) and len(a) > len(prefix) and a[len(prefix)] != " ":
            out.append(f"{prefix} {a[len(prefix):]}")
    return out


_GROUP_ALIASES_LOWER: dict[str, list[str]] = {
    canonical: list(dict.fromkeys(v for a in aliases for v in alias_variants(a)))
    for canonical, aliases in GROUP_COMPANIES.items()
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
# 700자 고정컷 밖에서도 예외적으로 살리는 문장: '~을 비롯해 A, 포스코이앤씨, B 등이
# 참여/시공/수주한다' 처럼 참여사·제안사·수상사 등을 정식으로 나열하는 문장
# (실제 사례 ①: 대보건설 신안산선 기사, 본문 835자 중 737번째 '포스코이앤씨' —
#  700자 컷에 37자 차이로 빠짐.
#  실제 사례 ②: QuINSA 총회 기사, "IQM, 포스코홀딩스, SDT, 노르마, 연세대 등이 …
#  표준화 과제를 제안했다" — 동사 화이트리스트에 '제안'이 없어서 빠짐. 이런
#  나열문 동사는 제안·발표·수상·선정·체결 등 사실상 무한해 동사 조건을 뺐었다.)
#
# 처음엔 "기사가 짧으면 통째로 본다"로 고쳤다가 회귀를 냈다: '포스코 노사 파업'
# 기사에서 인용문 "이제 공은 포스코홀딩스 장인화 회장에게 넘어갔다"의
# '포스코홀딩스' 가 detect_group_companies 의 `if not found` 때문에 진짜 주체인
# '포스코' 를 밀어냈다 — 원래 700자 컷이 막던 바로 그 실패 모드다. '쉼표 3개
# 이상'만으로 막았더니(②를 고치며 동사 조건을 뺀 뒤) 다시 회귀가 났다: 같은
# 파업 기사의 "2022년 출범한 지주회사인 포스코홀딩스로 배당금, 브랜드사용료
# 등이 상당액 들어가면서, 포스코 영업이익이 …" 처럼 쉼표 3개짜리 '서술 문장'이
# '회사 나열 문장'으로 오인됐다.
# 두 문장의 진짜 차이는 '항목이 짧은 명사 그 자체로 끝나는가' 다 — 진짜 나열은
# 'A, B, C 등이' 처럼 각 항목이 조사·어미 없이 뚝뚝 끊기고 짧다. 그래서 마지막
# 항목(서술부가 붙는 게 정상)을 뺀 나머지 항목이 전부 15자 이내이고 조사·연결
# 어미로 끝나지 않아야 '나열 문장' 으로 인정한다.
_LIST_ITEM_MAX_CHARS = 15
_NOT_LIST_ITEM_END_RE = re.compile(
    r"(?:은|는|이|가|을|를|의|에|와|과|로|으로|에서|에게|한테|보다|처럼|만큼|"
    r"까지|부터|면서|하며|이며|다가|해서|이라|라서|고|며|서|면|데|니|자)$")


def _is_company_list_sentence(sentence: str) -> bool:
    """'A, B, C 등이 …' 처럼 명사를 나열하는 문장인지 — 서술 문장 속 쉼표 3개와
    구분한다(위 group_lead_text 주석의 실패 사례 참고).

    첫 항목은 '공사에는 대보건설을 비롯해 포스코이앤씨' 처럼 도입구가 붙어 길어질
    수 있어 길이는 안 재고 조사/어미로 안 끝나는지만 본다. 중간 항목(있다면)은
    도입구·서술부 둘 다 없는 '진짜 나열 한복판'이라 짧고 깨끗해야 한다. 마지막
    항목은 서술부('~등이 참여했다')가 붙는 게 정상이라 아예 안 본다.
    """
    parts = [p.strip() for p in re.split(r"[,·]", sentence) if p.strip()]
    if len(parts) < 3:
        return False
    if _NOT_LIST_ITEM_END_RE.search(parts[0]):
        return False
    for item in parts[1:-1]:
        if len(item) > _LIST_ITEM_MAX_CHARS or _NOT_LIST_ITEM_END_RE.search(item):
            return False
    # '후원(하고 있는) A, B, C가 참여' 처럼 스포츠·공연 등 제3자 행사의 후원사를
    # 나열하는 문장은 실제 사업 소식이 아니라 단순 협찬 언급이라 그룹사로 치지
    # 않는다. (실사례: e스포츠 대회 후원사 명단에 '포스코'가 있어 무관 기사가
    # 걸림 — LCK 결승전 기사, 2026-09-16)
    if re.search(r"후원(하[고는]|사)", sentence):
        return False
    return True


def group_lead_text(body: str) -> str:
    """그룹사 판정용 리드 텍스트 — 기본 리드 + 참여사 나열 문장(리드 밖이어도)."""
    lead = body[:GROUP_LEAD_CHARS]
    tail = body[GROUP_LEAD_CHARS:]
    if not tail:
        return lead
    extra: list[str] = []
    for sentence in re.split(r"(?<=[.\n])\s*", tail):
        if not sentence or not _is_company_list_sentence(sentence):
            continue
        if any(alias in sentence.lower() for aliases in _GROUP_ALIASES_LOWER.values() for alias in aliases):
            extra.append(sentence)
    return lead + "\n" + "\n".join(extra) if extra else lead


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
    # 유럽 — 산업·탈탄소 입법 (2025~2026). 조치명이 명시적이라 오탐 낮다.
    "산업가속화법", "산업 가속화법", "탄소중립산업법", "넷제로산업법", "NZIA",
    "청정산업딜", "클린산업딜", "그린딜 산업계획", "외국보조금규정", "외국보조금 규정",
    "역내산 요건", "역내 조달 요건", "저탄소 제품 기준", "저탄소 조달",
    # 중국
    "흑연 수출통제", "갈륨 수출", "게르마늄 수출", "안티모니 수출", "희토류 수출통제",
    "요소 수출제한", "반도체 장비 수출통제", "수출허가 대상",
    # 무역구제 (공식 절차)
    "반덤핑 관세", "반덤핑관세", "반덤핑 조사", "상계관세", "덤핑 판정", "긴급수입제한",
    # 한미 관세협상·대미투자 (2026-09-30 누락 사례: '트럼프 대미투자' 기사를 일일이 수동 등록)
    "트럼프 관세", "미국 관세", "관세협상", "관세 협상", "관세 합의", "품목관세", "품목별 관세",
    "자동차 관세", "철강 관세", "대미투자", "대미 투자", "한미 통상", "통상협상", "통상 협상",
    "무역합의", "무역 합의",
]
# 이 낱말이 제목에 있으면 철강·배터리 같은 산업어가 없어도 통상환경 기사로 본다.
# 국가 단위 관세·투자 협상은 제목에 업종이 안 나오지만(예: '트럼프 대미투자 압박') 철강·배터리·자동차
# 수출 전반에 영향을 주기 때문이다. 301조처럼 업종이 특정돼야 의미가 있는 조치어는 넣지 않는다.
TRADE_MACRO_KW = [
    "트럼프 관세", "미국 관세", "상호관세", "보편관세", "관세협상", "관세 협상", "관세 합의",
    "품목관세", "품목별 관세", "자동차 관세", "철강 관세", "대미투자", "대미 투자", "한미 통상",
    "통상협상", "통상 협상", "무역합의", "무역 합의",
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

# 기본 중요도 항목 — 마스터 패널이 점수·사용여부를 그대로 편집한다(단일 출처).
# 판정 로직(is_battery_scope 등)은 코드에 있어 항목 자체를 지울 순 없지만,
# enabled=false 로 두면 점수 기여가 0이 되어 사실상 삭제와 같은 효과를 낸다.
SCORE_RULE_DEFS: dict[str, tuple[int, str]] = {
    "futurem_title": (SCORE_FUTUREM_TITLE, "포스코퓨처엠이 제목에"),
    "futurem_body": (SCORE_FUTUREM_BODY, "포스코퓨처엠이 본문에만"),
    "group": (SCORE_GROUP, "다른 계열사(홀딩스·DX·인터내셔널·이앤씨 등)"),
    "battery_title": (SCORE_BATTERY_TITLE, "배터리 생태계(소재·셀·전기차·ESS·원료)가 제목에"),
    "battery_body": (SCORE_BATTERY_BODY, "배터리 생태계가 본문에만"),
    "trade": (SCORE_TRADE, "해외 통상 조치(IRA·CBAM·반덤핑 등) 신호"),
    "policy": (SCORE_POLICY, "정책 키워드(전기요금·배출권·특화단지 등)"),
    "major_press": (SCORE_MAJOR_PRESS, "주요 언론사(연합·전자신문·머니투데이 등)"),
    "market_penalty": (SCORE_MARKET_PENALTY, "단순 시황·주가 기사(목표주가·코스피·투자의견)"),
}
SCORE_CUSTOM_MAX = 30       # 사용자 추가 항목 상한
SCORE_RULES_CACHE_SEC = 20  # run_state 재조회 간격 — 기사마다 DB 를 다시 묻지 않는다

_score_rules_cache: dict[str, Any] = {"at": 0.0, "overrides": {}, "customs": []}


def get_score_rules(storage: "Storage") -> tuple[dict[str, dict], list[dict]]:
    """마스터 패널에서 고친 중요도 규칙(기본 항목 재정의 + 사용자 추가 항목).

    호출마다 DB 를 묻지 않도록 짧게 캐시한다(파이프라인 한 회차 안에서 기사마다
    다시 조회하면 Supabase 왕복이 기사 수만큼 늘어난다)."""
    now = time.monotonic()
    if now - _score_rules_cache["at"] > SCORE_RULES_CACHE_SEC:
        state = storage.get_run_state()
        _score_rules_cache["overrides"] = jload(state.get("score_overrides"), {}) or {}
        _score_rules_cache["customs"] = jload(state.get("score_custom_rules"), []) or []
        _score_rules_cache["at"] = now
    return _score_rules_cache["overrides"], _score_rules_cache["customs"]
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
    # 중국 도메인도 co.kr 처럼 2단계 SLD 를 쓴다(예: news.xinhuanet.com.cn). 이걸
    # 못 접으면 뒤 두 조각만 남아 'com.cn'으로 뭉개져 매체를 특정할 수 없게 된다.
    if len(parts) >= 3 and parts[-2] in ("com", "net", "org", "gov", "edu") and parts[-1] == "cn":
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

@lru_cache(maxsize=None)
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

    def _is_dropped_connection(self, exc: Exception) -> bool:
        """재사용하려던 연결이 이미 끊겨 있어서 난 실패인가.

        'Connection aborted' / RemoteDisconnected / 'Connection reset' 이 해당한다.
        SSL·프록시·연결 시간초과·읽기 시간초과는 재시도로 나아지지 않으니 제외한다.
        """
        exc_mod = self._requests.exceptions
        if isinstance(exc, (exc_mod.SSLError, exc_mod.ProxyError, exc_mod.ConnectTimeout)):
            return False
        text = str(exc)
        return ("Connection aborted" in text or "RemoteDisconnected" in text
                or "Connection reset" in text)

    def get(self, url: str, **kwargs: Any):
        with self._lock:
            self.count += 1
        kwargs.setdefault("timeout", self.timeout)
        try:
            return self.session.get(url, **kwargs)
        except self._requests.exceptions.ConnectionError as exc:
            if not self._is_dropped_connection(exc):
                raise
            # 서버·중간 장비가 유휴 연결을 조용히 끊었는데 그 연결을 재사용하다 실패한 경우다.
            # 실사례(2026-09-30): 연합뉴스 RSS 가 5분 수집 회차마다 번갈아 실패했다. 죽은 연결은
            # 이미 풀에서 버려졌으니 새 연결로 딱 1회만 다시 시도한다. (GET 전용 — 멱등이라 안전.
            # 기사 페이지 조회가 이렇게 한 번 실패하면 URL 원장에 영구 제외로 남았다.)
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


# ── 다음(Daum) 뉴스 수집 (사용자 지정 2026-10-06) ───────────────────────────────
# 다음에는 공식 뉴스 검색 API 가 없고 직접 크롤링은 약관상 하지 않는다(PRD §7-4).
# 그래서 이미 쓰는 Google 뉴스 RSS 에 'site:v.daum.net 키워드' 로 검색해 다음에 실린 기사만 가져온다.
# 호출량 때문에 '그룹사' 키워드만 쓰고, .env 의 DAUM_ENABLED=true 일 때만 켠다(기본 꺼짐).
# 같은 기사가 언론사 원문에도 있으면 기존 중복 판정(URL·본문 해시·제목 유사도)이 하나로 합친다.
DAUM_SITE = "site:v.daum.net"
DAUM_CATEGORY = "다음"      # 내부 표시용 분류(마스터 키워드 분류가 아니다 — 저장하지 않는다)


def daum_enabled() -> bool:
    """호출 시점에 환경변수를 읽는다(실행 방식에 따라 동작이 갈리지 않게)."""
    return get_env("DAUM_ENABLED", "").strip().lower() in ("1", "true", "yes")


def daum_keyword_rows(keyword_rows: Sequence[dict]) -> list[dict]:
    """다음 검색용 키워드 — 활성 '그룹사' 키워드만(중복 제거). 산업·정책·통상은 호출량 때문에 뺀다."""
    out, seen = [], set()
    for r in keyword_rows:
        kw = (r.get("keyword") or "").strip() if isinstance(r, dict) else ""
        if kw and r.get("category") == NAVER_ALWAYS_CATEGORY and kw not in seen:
            seen.add(kw)
            out.append({"keyword": kw, "category": DAUM_CATEGORY})
    return out


GOOGLE_FETCH_WORKERS = 4   # Google News RSS 동시 요청 수 — 네이버(4)와 같은 이유로 낮게 둔다


def collect_google_rss(http: HttpClient, keyword_rows: Sequence[dict]) -> list[RawItem]:
    """키워드별 Google News RSS. 키워드마다 독립 호출이라 병렬로 받는다.

    예전엔 순차였다 — 키워드 120여 개 × 응답 1초 안팎 = 회차마다 2분 가까이 이 단계만 기다렸다.
    결과는 입력 키워드 순서대로 이어 붙여(pool.map) 병렬이어도 항목 순서가 이전과 같다."""
    feedparser = _import("feedparser", "feedparser")

    def _fetch_one(row: dict) -> list[RawItem]:
        keyword = row["keyword"]
        category = row.get("category")
        query = (f"{POLICY_SITE} {keyword}" if category == "정책"
                 else f"{DAUM_SITE} {keyword}" if category == DAUM_CATEGORY else keyword)
        url = GOOGLE_NEWS_RSS.format(q=urlencode({"q": query})[2:])
        try:
            resp = http.get(url)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Google RSS 조회 실패 (%s): %s", keyword, exc)
            return []
        out: list[RawItem] = []
        for entry in feedparser.parse(resp.content).entries:
            link = entry.get("link", "")
            if not link:
                continue
            src_title = (entry.get("source", {}) or {}).get("title", "")
            # 정책브리핑 검색인데 생활정보 블로그(gonggam.korea.kr)면 버린다. (사용자 지정)
            if row.get("category") == "정책" and "gonggam" in f"{src_title} {entry.get('title','')}".lower():
                continue
            published = parse_feed_datetime(entry.get("published") or entry.get("updated"),
                                            entry.get("published_parsed") or entry.get("updated_parsed"))
            out.append(RawItem(
                url_source=normalize_url(link),
                url_original=link,
                title=html_mod.unescape(entry.get("title", "")).strip(),
                published_at=published,
                source_type="google_rss",
                press_hint=src_title,
                snippet=html_mod.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", ""))).strip(),
            ))
        return out

    rows = list(keyword_rows)
    if not rows:
        return []
    items: list[RawItem] = []
    with ThreadPoolExecutor(max_workers=min(GOOGLE_FETCH_WORKERS, len(rows))) as pool:
        for part in pool.map(_fetch_one, rows):
            items.extend(part)
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
    "전기요금", "전력", "전력수급", "송전", "발전",
    # 'ㅇㅇㅇ에너지기술평가원'·'에너지경제신문'처럼 기관명·매체명에 흔히 들어가는
    # 부분 문자열이라 '에너지' 단독은 뺐다(실제 오탐: 포항시 공공기관 유치 기사가
    # '한국에너지기술평가원' 언급만으로 정책 관련 기사로 잘못 수집됐다). 의미가
    # 뚜렷한 복합어로만 인정한다.
    "에너지 정책", "에너지 안보", "에너지 전환", "에너지 위기", "청정에너지",
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
    if _kw_hit_any(title, TRADE_MACRO_KW):
        return True
    return _kw_hit_any(title, TRADE_MEASURE_KW) and _kw_hit_any(f"{title}\n{extra}", TRADE_INDUSTRY_KW)


_MARKET_LIST_TITLE_RE = re.compile(
    r"(?i)closing\s+price\s+list|opening\s+price\s+list|코스피\s*200\s*(?:종가|시가)\s*목록|종가\s*목록")


def is_market_list_title(title: str) -> bool:
    """종가표·시세 목록 형태의 제목인가(기사가 아니라 데이터 나열)."""
    return bool(_MARKET_LIST_TITLE_RE.search(title or ""))


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
    # 국내 배터리 생태계 (셀·소재·부품·리사이클) — 제목에 '배터리'가 없어도 이 회사명이면 관련 기사다.
    # 지명과 겹치는 짧은 이름(금양·천보)은 여기 두지 않고 BATTERY_COMPANY_KW + 가드로만 본다.
    "에코프로비엠", "에코프로머티리얼즈", "성일하이텍", "새빗켐", "탑머티리얼",
    "SK아이이테크놀로지", "더블유씨피", "동화일렉트로라이트", "엔켐", "솔루스첨단소재",
    # 전기차 수요
    "전기차 판매", "전기차 수요", "전기차 보조금", "전기차 캐즘", "EV 수요", "전기차 시장",
    # ESS (에너지저장)
    "ESS", "에너지저장장치", "에너지저장시스템", "전력저장", "계통 안정화", "BESS",
    # 원료
    "리튬 가격", "니켈 가격", "코발트 가격", "탄산리튬", "수산화리튬", "흑연 공급",
]


# 제목에 이 말이 있으면 포스코 미언급이어도 수집한다 — 전기차·배터리·이차전지 기사는 결국 포스코퓨처엠(양극재·음극재)의
# 전방 수요라 전부 포토카드 대상이다(사용자 지정 2026-10-06). '전기차' '배터리' 처럼 흔한 낱말이라
# 본문 앞부분에 스치는 것까지 받으면 잡음이 크므로 **제목**에 있을 때만 인정한다.
BATTERY_TITLE_KW = ["전기차", "전기 자동차", "전기자동차", "배터리", "이차전지", "2차전지", "전기차충전", "EV "]


def is_battery_scope(title: str, extra: str = "") -> bool:
    """포스코퓨처엠 전·후방(소재·셀·전기차·ESS·원료) 기사인가.

    이 범위면 포스코 미언급이어도 수집한다 — 전방 수요·경쟁 동향이 사업에 직결된다.
    지명과 겹치는 짧은 회사명은 battery_company_hit 이 가드와 함께 판정한다.
    """
    probe = f"{title}\n{extra}"
    return (_kw_hit_any(probe, BATTERY_SCOPE_KW) or _kw_hit_any(title or "", BATTERY_TITLE_KW)
            or battery_company_hit(probe))


# 배터리 생태계 '회사명'만 추린 고정밀 신호.
# 일반어('배터리'·'전기차')와 달리 회사명은 검색 블러브에 우연히 끼어들지 않으므로,
# 제목이 아닌 **리드(스니펫)** 에서 발견돼도 관련 기사로 인정한다.
#   실제 누락 사례: "금양, 기장공장을 데이터센터로 물적분할 추진…상폐 돌파구"
#   — 제목에 '배터리'가 없어 제목 전용 필터에서 통째로 버려졌다(본문은 배터리 사업 얘기).
BATTERY_COMPANY_KW = [
    "LG에너지솔루션", "삼성SDI", "SK온", "CATL", "BYD", "파나소닉",
    "에코프로", "에코프로비엠", "에코프로머티리얼즈", "엘앤에프", "코스모신소재",
    "대주전자재료", "나노신소재", "한솔케미칼", "금양", "성일하이텍", "새빗켐",
    "탑머티리얼", "SK아이이테크놀로지", "더블유씨피", "천보", "동화일렉트로라이트",
    "엔켐", "솔루스첨단소재",
    "BTR", "베이터루이", "샨샨", "룽바이", "CNGR", "중웨이", "화유코발트", "당성과기",
]

# 짧은 회사명은 지명·학교명과 글자가 겹친다. 그 형태로만 나오면 회사 언급이 아니다.
#   예: '부산 기장군 금양읍 도시계획' → '금양'(배터리 셀 업체)이 아니다.
BATTERY_COMPANY_FALSE_HINTS: dict[str, list[str]] = {
    "금양": ["금양읍", "금양면", "금양리", "금양동", "금양초", "금양중", "금양고", "금양역"],
    "천보": ["천보산", "천보사", "천보루"],
}


def battery_company_hit(text: str) -> bool:
    """배터리 생태계 회사명이 실제로 언급됐는가 (지명·학교명 오탐 제외)."""
    t = text or ""
    for kw in BATTERY_COMPANY_KW:
        if kw not in t:
            continue
        hints = BATTERY_COMPANY_FALSE_HINTS.get(kw)
        if hints:
            stripped = t
            for h in hints:
                stripped = stripped.replace(h, "")
            if kw not in stripped:
                continue   # 지명·기관명 형태로만 나왔다 — 회사 언급 아님
        return True
    return False


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
    r"|무단\s*전재|\d{4}/\d{2}/\d{2}\s*\d{2}:\d{2}\s*송고).*$", re.S)
# 줄글 기사 맨 앞의 '(서울=연합뉴스) 홍길동 기자 =' — 꼬리표 규칙이 본문 전체를 지우지 않게 먼저 뗀다
_NOTICE_DATELINE_RE = re.compile(r"^\s*\(\S+=\s*연합뉴스\)\s*(?:[가-힣]{2,4}\s*기자\s*=)?\s*")


# 이름으로 오인하기 쉬운 흔한 직함 — 항목 끝 토큰이 이 단어면 이름이 아니라 직함이다
# (예: "정치부 부장" 처럼 이름 없이 직함만 있는 항목이 "부장"을 이름으로 잘못 떼어가지 않게).
_TITLE_SUFFIXES = frozenset({
    "부장", "차장", "과장", "국장", "실장", "이사", "사장", "회장", "대표", "단장", "팀장",
    "본부장", "센터장", "원장", "총장", "학장", "청장", "처장", "장관", "차관", "수석", "특보",
})
_PERSON_NAME_RE = re.compile(r"^(.*\S)\s+([가-힣]{2,4})$")


def _split_person_item(item: str) -> tuple[str, str]:
    """'정치사회부/생활산업부장 김경태' → ('정치사회부/생활산업부장', '김경태').

    끝 토큰이 이름처럼 안 생겼거나(흔한 직함) 애초에 못 나누면 ('', 원문) 을 돌려준다.
    """
    m = _PERSON_NAME_RE.match(item)
    if m and m.group(2) not in _TITLE_SUFFIXES:
        return m.group(1).strip(), m.group(2)
    return "", ""


# 인사 기사 표기 — 헤더는 ◇◆□■, 항목은 ▲△▶▷ 로 시작한다. 연합뉴스 [인사] 법무부처럼
# 기사마다 △ 를 쓰기도 해서(2026-09-29 사용자 지적: 검사 전보 내용 누락) ▲ 만 보면 항목이 통째로 사라진다.
PEOPLE_RULE_MODEL = "rule"     # 규칙 기반으로 정리했다는 표시(LLM 을 쓰지 않았다)
PEOPLE_BULK_ITEMS = 6          # 이 인원을 넘으면 LLM 을 건너뛰고 한 줄씩 규칙 정리한다


_PEOPLE_TOKEN_RE = re.compile(r"([◇◈◆◎□■])|([▲△▶▷])")
_PEOPLE_HEAD_LEVEL = {"◇": 1, "◈": 1, "◆": 2, "◎": 2, "□": 2, "■": 2}


def _clean_people_header(text: str) -> str:
    """'승진<신규 임원>' → '승진 · 신규 임원'. 머리말 끝의 구분 기호를 정리한다."""
    t = re.sub(r"\s*<([^<>]*)>\s*", r" · \1 ", text or "")
    t = re.sub(r"\(\s*총?\s*\d+\s*명\s*\)", " ", t)       # '(총 3명)' — 인원 표기는 줄 수로 알 수 있다
    return re.sub(r"\s+", " ", t).strip(" ·,;:")


_COMMON_SURNAMES = frozenset("김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민나진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부가복태목")
_PEOPLE_NAME_ONLY_RE = re.compile(r"^[가-힣]{2,4}(?:\([^()]*\))?$")


def _split_item_people(item: str) -> list[str]:
    """'김민수, 박인찬, 이경재' → 세 사람. '직책 이름, 직책 이름' 도 사람별로 나눈다.

    쉼표·가운뎃점으로 이은 토막이 전부 '이름(또는 직책+이름)' 모양일 때만 나눈다 —
    '서울, 부산 지검' 같은 지명 나열을 사람으로 잘못 가르지 않기 위해서다.
    """
    parts = [x.strip() for x in re.split(r"\s*[,、，]\s*|\s+·\s+", item) if x.strip()]
    if len(parts) < 2:
        # '한화M&S 박대주 배상현 유상현' — 소속 뒤에 이름 세 개 이상이 공백으로만 이어진 경우
        toks = item.split()
        tail: list[str] = []
        for tk in reversed(toks):
            if len(tk) == 3 and tk[0] in _COMMON_SURNAMES and _PEOPLE_NAME_ONLY_RE.match(tk) \
                    and tk not in _TITLE_SUFFIXES:
                tail.insert(0, tk)
            else:
                break
        if len(tail) >= 3 and len(tail) < len(toks):
            org = " ".join(toks[:len(toks) - len(tail)])
            return [f"{org} {n}" for n in tail]
        return [item]
    def kind_of(x: str) -> str:
        """'이름' | '직책 이름' | ''(사람 아님)"""
        if _PEOPLE_NAME_ONLY_RE.match(x) and x[0] in _COMMON_SURNAMES:
            return "name"
        pos, name = _split_person_item(x)
        return "posname" if name and name[0] in _COMMON_SURNAMES else ""
    kinds = {kind_of(x) for x in parts}
    # 전부 같은 모양일 때만 사람 나열로 본다 — '서울, 부산 지검 검사장 김철수' 는 지명+직책이라 섞여 있어 나누지 않는다
    return parts if len(kinds) == 1 and "" not in kinds else [item]


def _split_people_sections(text: str) -> list[tuple[str, str]]:
    """'◇ 기관 ◆ 구분 ▲ 항목 △ 항목 …' → [(머리말, 항목), …].

    머리말은 위계를 잇는다 — 상위(◇·◈ 기관)와 하위(◆·◎·■·□ 구분)를 ' · ' 로 이어 항목마다 붙인다.
    예) '◈한화M&S ◎승진<신규 임원> ▷박대주 ▷배상현' → ('한화M&S · 승진 · 신규 임원', '박대주'), …
    연합뉴스처럼 ◇ 하나뿐이면 그 머리말이 곧 구분이다. 새 상위 머리말이 나오면 하위는 비운다.
    '▷신규 임원 △김민수 △박인찬' 처럼 ▷·▶ 바로 뒤에 △·▲ 항목이 이어지면 ▷ 글은 사람이 아니라 소제목이다.
    """
    pairs: list[tuple[str, str]] = []
    parts = _PEOPLE_TOKEN_RE.split(text)       # [앞글, 머리표, 항목표, 글, 머리표, 항목표, 글, …]
    l1 = l2 = l3 = ""
    for k in range(1, len(parts) - 2, 3):
        head_mark, item_mark, seg = parts[k], parts[k + 1], parts[k + 2]
        if head_mark:
            l3 = ""
            if _PEOPLE_HEAD_LEVEL[head_mark] == 1:
                l1, l2 = _clean_people_header(seg), ""
            else:
                l2 = _clean_people_header(seg)
        elif item_mark:
            item = (seg or "").strip(" ·,;")
            if not item:
                continue
            nxt = parts[k + 3] or parts[k + 4] if k + 4 < len(parts) else ""
            if item_mark in "▷▶" and nxt and nxt in "▲△":
                l3 = _clean_people_header(item)
                continue
            if item_mark in "▷▶":
                l3 = ""
            head = " · ".join(x for x in (l1, l2, l3) if x)
            for one in _split_item_people(item):
                pairs.append((head, one))
    return pairs


def _join_capped(blocks: list[str], sep: str, limit: int) -> str:
    """블록 단위로 이어 붙이되, 한도를 넘기면 통째로 빼고 만다.

    문자 수로 그냥 잘라 버리면 마지막 블록의 마지막 줄(대개 연락처)이 중간에서
    잘려 나간다(사용자 지적 2026-09-28 — 부고 연락처가 잘려 보임). 블록 하나를
    통째로 빼거나 통째로 넣거나만 하므로, 남는 블록은 완전한 채로 남는다.
    """
    out: list[str] = []
    total = 0
    for b in blocks:
        add = (len(sep) if out else 0) + len(b)
        if out and total + add > limit:
            break
        out.append(b)
        total += add
    if not out and blocks:
        out = [blocks[0]]   # 첫 블록마저 넘으면 그거라도 안 잘리게 통째로 준다
    return sep.join(out)


# 부고 본문에서 이 단어가 그대로 나오면 그 뒤 텍스트를 해당 항목 값으로 본다.
# 라벨이 없는 선두 텍스트(대개 장례식장 안내)는 '빈소'로 취급한다.
_OBIT_FIELD_RE = re.compile(r"(상주|빈소|발인|장지)\s*[:：=]?\s*")


def _parse_obituary_block(raw_block: str) -> str:
    """'김철수(향년 80)씨 별세, 김영희씨 부친상 = 서울대병원, 발인 10일 ☎ 02-1234-5678'을

    '대상명/관계 → 상주 → 빈소 → 발인 → 연락처' 순서로 정리한다(사용자 지정
    2026-09-28). 라벨(상주·빈소·발인·장지)이 원문에 그대로 있으면 그 문장을
    해당 항목에 담고, 없으면 빈 항목은 건너뛴다 — 지어내지 않는다.
    연락처(☎ 뒤)는 끝까지 통째로 담아 절대 잘리지 않게 한다(전화번호가
    두 개 이상 나열돼도 마찬가지).
    """
    block = raw_block.strip(" ▲")
    contact = ""
    m = re.search(r"☎\s*(.+)$", block)
    if m:
        contact = m.group(1).strip()
        block = block[:m.start()].strip()

    head, sep, rest = block.partition("=")
    head = head.strip().rstrip(",")
    rest = rest.strip()
    # 흔한 패턴 "OOO씨 별세, XXX씨 YY상" — 마지막 쉼표로 대상명과 관계를 갈라
    # LLM 구조화 결과와 같은 '대상 / 관계' 모양으로 맞춘다.
    subj, csep, rel = head.rpartition(",")
    head_line = f"{subj.strip()} / {rel.strip()}" if csep else head

    fields = {"상주": "", "빈소": "", "발인": "", "장지": ""}
    if rest:
        markers = list(_OBIT_FIELD_RE.finditer(rest))
        prefix = rest[:markers[0].start()].strip(" ,.、") if markers else rest.strip(" ,.、")
        if prefix:
            fields["빈소"] = prefix
        for i, mk in enumerate(markers):
            end = markers[i + 1].start() if i + 1 < len(markers) else len(rest)
            val = rest[mk.end():end].strip(" ,.、")
            if val:
                key = mk.group(1)
                fields[key] = f"{fields[key]}, {val}" if fields[key] else val

    lines = [f"ㆍ{head_line}" if head_line else "ㆍ부고"]
    if fields["상주"]:
        lines.append(f"   · 상주: {fields['상주']}")
    if fields["빈소"]:
        lines.append(f"   · 빈소: {fields['빈소']}")
    fb = " / ".join(x for x in (
        f"발인 {fields['발인']}" if fields["발인"] else "",
        f"장지 {fields['장지']}" if fields["장지"] else "") if x)
    if fb:
        lines.append(f"   · {fb}")
    if contact:
        lines.append(f"   · ☎ {contact}")
    return "\n".join(lines)


def format_people_notice(body: str, kind: str, title: str = "") -> str:
    """인사·부고 공지에서 핵심 블록만 남긴다. LLM 을 쓰지 않는다.

    LLM 예산이 바닥나면(PEOPLE_LLM_PER_RUN) 이 함수가 그대로 카드 요약이 되므로,
    원문을 잘라 붙이기만 하면 '요약'이 아니라 '원문'으로 보인다(사용자 지적
    2026-09-28 — [인사] 프라임경제 기사가 원문 그대로 노출됨). 그래서 사람별로
    한 줄씩 나눠 LLM 구조화 결과(format_people_llm)와 비슷한 모양으로 만든다.

    부고:  '▲ 별세·상주 … = 빈소, 발인, 장지 ☎ 전화' — 여러 명이면 ▲ 단위로 나눠
           각각 '대상명/관계 → 상주 → 빈소 → 발인 → 연락처' 순서로 정리한다
           (사용자 지정 2026-09-28). 연락처는 ▲ 단위로만 잘라 절대 안 잘린다.
    인사:  '◇ 부서 ▲ 직책 이름 …' — (직책, 이름)을 갈라 'ㆍ직책 이름 (헤더)' 로 만든다.
           원문에 적힌 대로만 옮긴다(해석·보강 없음, 사용자 지적 2026-10-01 — AI 가 업무·이력을 지어냈다).
           명단 표시가 없는 줄글 기사는 원문 문장을 한 줄씩 그대로 옮기되, 제목과 관련 없는 본문
           (추출이 엉뚱한 칼럼을 잡은 경우)이면 빈 문자열을 돌려준다 — 호출부가 안내 문구를 쓴다.
    """
    text = _clean_notice_text(body)
    if kind == "obituary":
        raw_blocks = [b.strip() for b in text.split("▲") if b.strip()]
        if not raw_blocks:
            return text[:800]
        blocks = [_parse_obituary_block(b) for b in raw_blocks]
        return _join_capped(blocks, "\n\n", 1600)
    pairs = _personnel_pairs(text)
    if not pairs:
        return _personnel_prose_lines(text, title)
    lines = []
    for header, item in pairs:
        pos, name = _split_person_item(item)
        # 이름을 맨 앞에 둔다(가독성) — 'ㆍ장은익 · 성과경영실장 (1급 승진)'
        line = f"ㆍ{name} · {pos}" if name else f"ㆍ{item}"
        if header:
            line += f" ({header})"
        lines.append(line)
    # 인사는 사람 수가 수백 명일 수 있다(검사 인사 등). 뒤쪽(전보 등)이 잘려 나가면 안 되므로
    # 사실상 상한 없이 전부 담는다 — 카드는 화면에서 접어 보여 준다.
    return _join_capped(lines, "\n", PEOPLE_LINES_MAX_CHARS)


PEOPLE_LINES_MAX_CHARS = 60000


def _clean_notice_text(body: str) -> str:
    """공백을 정리하고 머리(구독 안내)·꼬리(저작권 문구)를 떼어낸 인사·부고 공지 본문."""
    text = re.sub(r"\s+", " ", (body or "").strip())
    text = _NOTICE_HEAD_RE.sub("", text, count=1)
    # 줄글 기사는 맨 앞에 '(서울=연합뉴스) 홍길동 기자 =' 가 붙는다 — 꼬리표 규칙이 이걸 보고 본문 전체를
    # 지우지 않도록 머리의 것은 먼저 떼어 낸다.
    text = _NOTICE_DATELINE_RE.sub("", text)
    return _NOTICE_TAIL_RE.sub("", text).strip()


def _personnel_pairs(text: str) -> list[tuple[str, str]]:
    """정리된 공지 본문에서 (헤더, 항목) 목록을 뽑는다. 항목이 없으면 빈 목록.

    명단 앞에 '<승진>' 처럼 꺾쇠로 묶인 유형 표시가 따로 있으면 모든 항목의 머리말 앞에 붙인다(조선비즈식).
    """
    m = re.search(r"[◇◈◆◎□■▲△▶▷].*", text)
    if not m:
        return []
    pairs = _split_people_sections(m.group(0))
    lead = re.findall(r"<([^<>]{1,20})>", text[:m.start()])
    if lead:
        tag = lead[-1].strip()
        pairs = [(f"{tag} · {h}" if h else tag, it) for h, it in pairs]
    return pairs


# 제목에서 '이 기사가 무엇에 관한 것인지' 알려 주는 낱말을 고를 때 뺄 일반어
_PEOPLE_GENERIC_WORDS = frozenset({
    "인사", "승진", "보직", "임원", "사장단", "발령", "신임", "신규", "임용", "정기", "단행", "동정",
    "부고", "별세", "프로필", "취임", "선임", "발탁", "전보", "이동", "대표이사", "사장", "부사장",
})
PEOPLE_NO_LIST_MSG = "원문에서 인사 명단을 확인하지 못했습니다. 원문 링크에서 확인해 주세요."
PEOPLE_PROSE_LINES = 8     # 명단 표시가 없는 줄글 기사에서 옮기는 문장 수 상한


def _title_keywords(title: str) -> list[str]:
    """제목에서 기사 주제를 가리키는 낱말(회사·기관명 등)을 뽑는다. 말머리([인사])·일반어는 뺀다."""
    t = re.sub(r"\[[^\]]*\]|【[^】]*】", " ", title or "")
    words = [w.replace("㈜", "").lower() for w in re.findall(r"[가-힣A-Za-z0-9&㈜]{2,}", t)]
    return [w for w in words if len(w) >= 2 and w not in _PEOPLE_GENERIC_WORDS]


_PEOPLE_ACTION_RE = re.compile(r"승진|선임|내정|임명|발탁|보임|위촉|영입|전보|취임|선출|발령|임용|부임")
# '사장단'·'임원진'처럼 사람 이름이 아닌 집합명은 직책으로 보지 않는다
_PEOPLE_ROLE_RE = re.compile(
    r"(?:대표이사|부사장|사장|대표|회장|부회장|본부장|센터장|총괄|부문장|실장|국장|부장|팀장|이사|전무|상무|상임|원장|청장|처장|장관|차관)(?!단|진)")


def _personnel_prose_lines(text: str, title: str) -> str:
    """명단 표시(◇▲)가 없는 줄글 인사 기사 — 사람이 등장하는 인사 문장만 원문 그대로 한 줄씩 옮긴다(해석 없음).

    '사장단 승진 및 보직 인사를 단행했다' 같은 배경 문장이나 인물 소개(경력 서술)는 빼고,
    '인사 동작(승진·선임…)'과 '직책'이 함께 있는 문장만 남긴다(2026-10-02 에코프로 사례: 승진자만 보이게).
    제목의 주제어가 본문에 하나도 없으면(본문 추출이 사이드바의 엉뚱한 칼럼을 잡은 경우) 빈 문자열.
    """
    if not text:
        return ""
    kws = _title_keywords(title)
    if kws and not any(k in text.lower() for k in kws):
        return ""
    sents = [x for x in _sentences(text) if len(x) >= 8]
    picked = [x for x in sents if _PEOPLE_ACTION_RE.search(x) and _PEOPLE_ROLE_RE.search(x)]
    if not picked:       # 직책을 못 잡았으면 동작 문장이라도 — 아무것도 없을 때만 안내 문구로 간다
        picked = [x for x in sents if _PEOPLE_ACTION_RE.search(x)][:3]
    return "\n".join("ㆍ" + _cut_at_word(x, 220) for x in picked[:PEOPLE_PROSE_LINES])


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
    # 문자 수로 그냥 자르면 마지막 사람의 마지막 줄(대개 연락처)이 중간에서
    # 잘려 나간다(사용자 지적 2026-09-28). 사람 단위로만 자른다.
    return _join_capped(blocks, "\n\n", 1600)


# ── 정부 부처 인사 — 이력(출생연도·학교·시험·경력) 인터넷 검색 보강 (사용자 지정 2026-10-02) ─────────────
# 기사 본문에 이력이 없으면 인터넷 기사(프로필)에서 찾는다. 틀린 정보가 들어가면 안 되므로 AI 가 지어내지 않고,
# '△1970년 △부산 △행시41회 △고려대 경제학과 △…' 같은 프로필 표기를 규칙으로만 읽는다.
#  · 검색 결과 페이지에 '이름 + 소속/직책'이 같이 나와야 같은 사람으로 본다(동명이인 방지).
#  · 출생연도·학교·시험은 서로 다른 사이트 2곳 이상에서 같으면 '교차확인', 1곳이면 '단일 출처'로 표시한다.
#    사이트끼리 값이 다르면 그 항목은 아예 빼 버린다.
#  · 항상 출처 주소를 같이 적어 화면에서 링크로 열 수 있게 한다.
PEOPLE_CAREER_MAX = get_env_int("PEOPLE_CAREER_MAX", 4, 0)          # 기사 1건당 이력을 찾는 사람 수(0 이면 끔)
PEOPLE_CAREER_PER_HOUR = get_env_int("PEOPLE_CAREER_PER_HOUR", 40, 0)   # 시간당 검색 인원 상한(비용·속도 보호)
_career_times: list[float] = []
_GOV_GRADE_RE = re.compile(r"과장급|국장급|실장급|부이사관|서기관|고위공무원|고공단|사무관|차관|청장|처장|장관|"
                           r"본부장급|임용|보직\s*인사|인사\s*발령")
_RANK_ORDER = ("장관", "차관", "청장", "처장", "위원장", "실장", "본부장", "국장", "관", "부이사관", "단장",
               "과장", "팀장", "서기관")
_AGENCY_IN_TITLE_RE = re.compile(r"([가-힣]{2,12}(?:부|처|청|위원회|공사|공단|연구원|진흥원|원))(?![가-힣])")
_BLOCK_MARK_RE = re.compile(r"[△▲▷◆◇●○ㆍ]")
_EXAM_RE = re.compile(r"(행시|행정고시|기술고시|입법고시|외무고시|외시|사법시험|사시)\s*(\d{1,3})\s*회")
_SCHOOL_RE = re.compile(r"([가-힣A-Za-z]{2,12}(?:대학교|대학원|대))(?![가-힣])(?:\s*([가-힣]{2,12}(?:학과|과|학부)))?")
_BIRTH4_RE = re.compile(r"(?<!\d)(19[4-9]\d|20[0-2]\d)\s*년(?:생)?(?![가-힣]*[월일])")
_BIRTH2_RE = re.compile(r"(?<!\d)(\d{2})\s*년생")


def is_gov_personnel(title: str, text: str) -> bool:
    """정부 부처 인사 공지인가 — 직급 표현(과장급·국장급·부이사관 …)이 있으면 그렇다고 본다."""
    return bool(_GOV_GRADE_RE.search(f"{title or ''}\n{(text or '')[:1500]}"))


def _career_allow() -> bool:
    now = time.monotonic()
    while _career_times and now - _career_times[0] > 3600:
        _career_times.pop(0)
    if len(_career_times) >= PEOPLE_CAREER_PER_HOUR:
        return False
    _career_times.append(now)
    return True


def _rank_of(pos: str) -> int:
    """직책 문자열의 높낮이(작을수록 높음). 이력을 찾을 사람을 높은 직책부터 고르는 데 쓴다."""
    for i, w in enumerate(_RANK_ORDER):
        if w in (pos or ""):
            return i
    return len(_RANK_ORDER)


def extract_profile_blocks(text: str, name: str) -> list[dict]:
    """페이지 글에서 '이름 … △1970년 △서울 △행시41회 △○○대 ○○학과 △경력…' 형태의 프로필 블록을 읽는다.

    반환: [{head, birth, exam, school, career[]}] — head 는 이름 앞뒤 글(소속·직책 확인용)이다.
    """
    out: list[dict] = []
    for m in re.finditer(re.escape(name), text or ""):
        win = text[m.start(): m.start() + 520]
        head = text[max(0, m.start() - 60): m.start() + 80]
        marks = len(_BLOCK_MARK_RE.findall(win[:300]))
        if marks < 2:       # 프로필 표기(△ 나열)가 아니면 본문 속 단순 언급이다
            continue
        nxt = re.search(r"\n\s*\n|(?<=[가-힣])\s[가-힣]{2,4}\s+[가-힣]{2,10}(?:장|관|원장|처장|청장)\s*[△▲]", win[len(name):])
        if nxt:
            win = win[: len(name) + nxt.start()]
        rest = win[len(name):]
        first_mark = _BLOCK_MARK_RE.search(rest)
        preface = rest[:first_mark.start()] if first_mark else rest     # '국세청 차장' · '부산지방국세청장 70년생'
        tokens = [t.strip(" ,·\n") for t in _BLOCK_MARK_RE.split(rest[first_mark.start():] if first_mark else "")
                  if t.strip(" ,·\n")]
        block: dict[str, Any] = {"head": head, "birth": "", "exam": "", "school": "", "career": []}
        pb2, pb4 = _BIRTH2_RE.search(preface), _BIRTH4_RE.search(preface)
        if pb4:
            block["birth"] = pb4.group(1)
        elif pb2:
            yy = int(pb2.group(1))
            block["birth"] = str((1900 if yy >= 30 else 2000) + yy)
        for tk in tokens:
            if not block["birth"]:
                b4 = _BIRTH4_RE.search(tk)
                b2 = _BIRTH2_RE.search(tk)
                if b4 and len(tk) <= 12:
                    block["birth"] = b4.group(1)
                    continue
                if b2 and len(tk) <= 12:
                    yy = int(b2.group(1))
                    block["birth"] = str((1900 if yy >= 30 else 2000) + yy)
                    continue
            ex = _EXAM_RE.search(tk)
            if ex and not block["exam"] and len(tk) <= 14:
                kind = {"행정고시": "행시", "외무고시": "외시", "사법시험": "사시"}.get(ex.group(1), ex.group(1))
                block["exam"] = f"{kind}{ex.group(2)}회"
                continue
            sc = _SCHOOL_RE.search(tk)
            if sc and not block["school"] and len(tk) <= 40:
                block["school"] = (sc.group(1) + (" " + sc.group(2) if sc.group(2) else "")).strip()
                continue
            if len(tk) >= 3 and len(block["career"]) < 5 and not re.fullmatch(r"[가-힣]{2,4}", tk):
                block["career"].append(tk[:40])
        if block["birth"] or block["school"] or block["exam"]:
            out.append(block)
    return out


def _search_profile_pages(ctx: Any, query: str, limit: int = 5) -> list[str]:
    """프로필 기사 후보 URL. 네이버 검색 API 가 있으면 그것을, 없으면 Google 뉴스 RSS 를 쓴다."""
    urls: list[str] = []
    http = ctx.http
    cfg = ctx.cfg
    try:
        if getattr(cfg, "naver_enabled", False):
            resp = http.get(NAVER_NEWS_API, params={"query": query, "display": limit, "sort": "sim"},
                            headers={"X-NCP-APIGW-API-KEY-ID": cfg.naver_client_id,
                                     "X-NCP-APIGW-API-KEY": cfg.naver_client_secret})
            resp.raise_for_status()
            for it in (resp.json().get("items") or []):
                u = it.get("originallink") or it.get("link") or ""
                if u:
                    urls.append(u)
        else:
            feedparser = _import("feedparser", "feedparser")
            resp = http.get(GOOGLE_NEWS_RSS.format(q=urlencode({"q": query})[2:]))
            resp.raise_for_status()
            for e in feedparser.parse(resp.content).entries[:limit]:
                if e.get("link"):
                    urls.append(e["link"])
    except Exception as exc:
        log.debug("인물 이력 검색 실패 (%s): %s", query, exc)
    return urls[:limit]


def lookup_person_career(ctx: Any, name: str, agency: str, pos: str,
                         search=None, fetch=None) -> dict:
    """사람 1명의 이력을 인터넷 기사에서 찾아 검증한다. 못 찾으면 {}.

    search(query) → URL 목록, fetch(url) → (최종URL, 본문글) 은 시험을 위해 바꿔 끼울 수 있다.
    """
    pos_core = next((w for w in _RANK_ORDER if w in (pos or "")), (pos or "").split()[-1] if pos else "")
    query = " ".join(x for x in (name, agency, pos_core, "프로필") if x)
    urls = (search or (lambda q: _search_profile_pages(ctx, q)))(query)
    if fetch is None:
        def fetch(u: str) -> tuple[str, str]:
            final, html = resolve_canonical(ctx.http, u)
            return final or u, (extract_body(html) if html else "")
    agency_keys = [k for k in {agency, agency.replace("부", "") if agency.endswith("부") else agency} if k]
    sources: list[dict] = []
    seen_hosts: set[str] = set()
    for u in urls:
        if len(sources) >= 3:
            break
        try:
            final, text = fetch(u)
        except Exception as exc:
            log.debug("프로필 페이지 조회 실패 %s: %s", u, exc)
            continue
        host = (urlsplit(final).hostname or "").lower()
        if not text or host in seen_hosts:
            continue
        for blk in extract_profile_blocks(text, name):
            head = blk["head"]
            # 같은 사람 확인 — 이름 앞뒤에 소속(부처)이나 직책이 나와야 한다
            if (agency_keys and any(k in head for k in agency_keys)) or (pos_core and pos_core in head):
                seen_hosts.add(host)
                sources.append({"url": final, "host": host, **blk})
                break
    if not sources:
        return {}
    info: dict[str, Any] = {"sources": [x["url"] for x in sources], "facts": {}}
    for key in ("birth", "school", "exam"):
        vals = [(x[key], x["url"]) for x in sources if x.get(key)]
        distinct = {v for v, _ in vals}
        if len(distinct) == 1:
            info["facts"][key] = (vals[0][0], len(vals))
        # 값이 서로 다르면(동명이인·오기 가능성) 빼 버린다
    # 경력은 가장 많이 적힌 출처 하나의 것을 그대로 옮긴다(여러 사이트를 섞지 않는다)
    best = max(sources, key=lambda x: len(x["career"]))
    info["career"] = best["career"]
    if not info["facts"] and not info["career"]:
        return {}
    return info


def format_career_line(info: dict) -> str:
    """lookup_person_career 결과 → 카드의 '   · 이력: …' 한 줄. 출처 주소를 끝에 붙인다(화면에서 링크가 된다)."""
    if not info:
        return ""
    f = info["facts"]
    bits = []
    if "birth" in f:
        bits.append(f"{f['birth'][0]}년생")
    if "school" in f:
        bits.append(f["school"][0])
    if "exam" in f:
        bits.append(f["exam"][0])
    if info.get("career"):
        bits.append("경력: " + " → ".join(info["career"]))
    confirmed = [k for k, (_, n) in f.items() if n >= 2]
    tag = f"교차확인 {len(info['sources'])}곳" if confirmed and len(confirmed) == len(f) \
        else "단일 출처 — 원문 확인 필요" if len(info["sources"]) == 1 else f"출처 {len(info['sources'])}곳"
    return "   · 이력: " + " · ".join(bits) + f" ({tag}) " + " ".join(info["sources"][:3])


def enrich_people_careers(ctx: Any, text: str, title: str, lookup=None) -> str:
    """정부 부처 인사 명단 텍스트의 높은 직책 사람 아래에 '이력' 줄을 끼워 넣는다. 못 찾으면 원문 그대로."""
    if not text or PEOPLE_CAREER_MAX <= 0 or not is_gov_personnel(title, text):
        return text
    lines = text.splitlines()
    people: list[tuple[int, int, str, str]] = []
    for i, ln in enumerate(lines):
        m = re.match(r"^ㆍ([가-힣]{2,4}) · (.+?)(?: \(|$)", ln)
        if m:
            people.append((_rank_of(m.group(2)), i, m.group(1), m.group(2)))
    people.sort(key=lambda t: (t[0], t[1]))
    am = _AGENCY_IN_TITLE_RE.search(re.sub(r"\[[^\]]*\]", "", title or ""))
    agency = am.group(1) if am else ""
    found: dict[int, str] = {}
    for _rank, i, name, pos in people[:PEOPLE_CAREER_MAX]:
        if lookup is None and not _career_allow():
            break
        try:
            info = (lookup or (lambda n, a, p: lookup_person_career(ctx, n, a, p)))(name, agency, pos)
        except Exception as exc:
            log.debug("이력 검색 실패 %s: %s", name, exc)
            continue
        line = format_career_line(info)
        if line:
            found[i] = line
    if not found:
        return text
    out: list[str] = []
    for i, ln in enumerate(lines):
        out.append(ln)
        if i in found:
            out.append(found[i])
    return "\n".join(out)


def people_summary(ctx: Context, kind: str, title: str, press: str, body: str,
                   use_llm: bool, html: str = "") -> tuple[str, str, dict]:
    """인사·부고 요약 텍스트를 만든다. 반환: (요약, 사용 모델, 토큰 usage).

    use_llm 이면 구조화를 시도하고, 실패하거나 use_llm 이 아니면 규칙 기반으로 대체한다.
    공지가 짧아 본문 추출이 푸터를 잡은 경우 og:description·<article> 텍스트로 보강한다.
    """
    notice = _notice_text(html, body, title)
    # 인사는 AI 를 쓰지 않고 원문에 적힌 대로 한 줄씩 옮긴다. 예전엔 AI 가 '업무·이력·학력'을 보태거나
    # 20명에서 끊어 뒤 섹션(전보)이 빠졌고, 사이드바의 엉뚱한 글을 요약하기도 했다(2026-10-01).
    # 명단을 못 찾으면 지어내지 않고 그 사실을 알린다. LLM 비용도 0이다.
    if kind == "personnel":
        text = format_people_notice(notice, kind, title)
        if text and hasattr(ctx, "http"):
            text = enrich_people_careers(ctx, text, title)     # 정부 부처 인사면 이력 보강(검증·출처 포함)
        return (text or PEOPLE_NO_LIST_MSG), PEOPLE_RULE_MODEL, {}
    if use_llm:
        parsed, usage = ctx.llm.people_notice(kind, title, press, notice)
        text = format_people_llm(parsed, kind) if parsed else ""
        if text:
            # format_people_llm 이 사람 단위로 이미 1600자 안에 담아 왔다(_join_capped).
            # 여기서 문자 수로 다시 자르면 마지막 사람의 연락처가 잘릴 수 있어 안 자른다.
            return text, ctx.llm.model, usage
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
                         "무역장벽", "무역분쟁", "무역전쟁", "통상", "FTA", "덤핑", "무역확장법",
                         "대미투자", "대미 투자", "관세협상", "상호관세"]


def _naver_item_relevant(title: str, category: str, keyword: str = "",
                         snippet: str = "") -> bool:
    """네이버가 느슨하게 매칭한 무관 기사를 거른다.

    네이버 description 은 검색어를 그대로 되풀이하는 블러브(기사 요약 아님)라
    **일반어**('배터리'·'통상' 등)로는 걸러지지 않는다. 그래서 느슨한 신호는
    **제목**에 있어야 통과시킨다. 이 필터가 없으면 "니켈" 검색의 원자재 시황,
    "국정감사" 검색의 정치 기사가 배터리·포스코 태그를 달고 대거 유입된다.

    다만 **회사명·명시적 조치명**은 블러브에 우연히 끼어들지 않는 고정밀 신호라
    제목이 아닌 **리드(스니펫)** 에서 발견돼도 인정한다 — 제목이 사업을 안 드러내는
    기사('금양, 기장공장을 데이터센터로…')가 통째로 버려지던 문제를 막는다.

    · 포스코·계열사·배터리 회사명·통상 조치명이 제목/리드에 있으면 통과 (고정밀)
    · '그룹사' 검색은 포스코가 없으면 탈락
    · 검색어가 제목에 그대로 들어 있으면 통과(정밀 매칭)
    · 그 밖에는 카테고리별 느슨한 신호가 **제목**에 있어야 통과
    """
    t = title or ""
    lead = f"{t}\n{snippet or ''}"
    # ── 고정밀 신호: 제목이 아니라 리드에 있어도 인정 ──────────────────
    if POSCO_MENTION_RE.search(lead) or detect_group_companies(lead):
        return True
    if category != "그룹사" and (battery_company_hit(lead)
                                 or _kw_hit_any(lead, TRADE_MEASURE_KW)):
        return True
    # ── 느슨한 신호: 제목에만 있어야 인정 ──────────────────────────────
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


SOURCE_FETCH_WORKERS = 10   # 키워드·피드별 조회 동시 실행 수 (2026-09-14, 아래 참고)

# 네이버 무료 한도(하루 25,000회) 대응 — 키워드 전부를 매 회차 조회하면 한도를 넘는다.
# 실측(2026-09-15): 활성 키워드 118개 × 하루 288회차 = 33,984회 → 한도의 1.4배.
# 한도를 넘기면 그날 남은 시간 내내 429 로 네이버 수집이 통째로 막혀 기사를 놓친다.
# 그래서 '그룹사'(포스코 계열사명) 키워드만 매 회차 조회하고, 나머지(산업·정책·통상)는
# 아래 수만큼 나눠 교대로 조회한다. 그룹사 7 + 나머지 111/2 ≈ 63개/회차 → 하루 약 18,100회.
NAVER_ALWAYS_CATEGORY = "그룹사"
NAVER_ROTATE_SLOTS_DEFAULT = 2
# 마스터 패널 '수집 키워드 관리'에서 고를 수 있는 분류. _naver_item_relevant 가
# 이 값으로 느슨한 신호 종류를 나누므로(그룹사=고정밀만, 정책/통상=전용 키워드,
# 그 외=산업으로 취급) 목록에 없는 값은 만들지 않는다.
KEYWORD_CATEGORIES = [NAVER_ALWAYS_CATEGORY, "산업", "정책", "통상"]


def naver_rotate_slots() -> int:
    """회차 분할 수. **호출 시점에** 환경변수를 읽는다.

    모듈 상단 상수로 두면 안 된다 — .env 는 load_config() 안에서 읽는데 그건
    모듈 import 보다 나중이라, 도커(--env-file 로 진짜 환경변수)에서는 반영되고
    로컬 실행에서는 무시되는 식으로 **실행 방식에 따라 동작이 갈린다.**
    """
    return get_env_int("NAVER_ROTATE_SLOTS", NAVER_ROTATE_SLOTS_DEFAULT, 1, 12)


def select_naver_keywords(keyword_rows: Sequence[dict], cycle: int) -> list[dict]:
    """이번 회차에 조회할 키워드만 고른다. (일일 한도 대응 — 위 상수 설명 참고)

    '그룹사'는 매 회차 전부, 나머지는 cycle 을 슬롯 수로 나눈 몫의 것만 낸다.
    슬롯이 1이면 기존처럼 전부 조회한다(끄고 싶을 때 .env 로 조절).
    """
    slots = naver_rotate_slots()
    always = [r for r in keyword_rows
              if (r.get("category") if isinstance(r, dict) else "") == NAVER_ALWAYS_CATEGORY]
    rotating = [r for r in keyword_rows
                if (r.get("category") if isinstance(r, dict) else "") != NAVER_ALWAYS_CATEGORY]
    if slots <= 1 or not rotating:
        return list(keyword_rows)
    slot = cycle % slots
    return always + [r for i, r in enumerate(rotating) if i % slots == slot]


NAVER_FETCH_WORKERS = 4      # 네이버 API 전용 동시 요청 수 — 10개로 보내면 초당 한도(429)에 걸렸다
NAVER_429_RETRIES = 2        # 429 를 받으면 쉬었다가 이만큼 다시 시도한 뒤에야 라운드를 중단한다
NAVER_429_BACKOFF_SEC = 1.5  # 첫 대기(다음은 2배·3배)


def collect_naver(http: HttpClient, cfg: Config, keyword_rows: Sequence[dict]) -> list[RawItem]:
    """NAVER API HUB 뉴스 검색. 직접 크롤링은 약관 위반이므로 하지 않는다. (PRD §7-4)

    무료 한도: 뉴스 검색 하루 25,000회 / 월 775,000회.
    키워드 29개를 1분마다 조회하면 하루 41,760회로 한도를 넘는다.
    → run_once 에서 `NAVER_INTERVAL_SEC`(기본 300초) 간격으로만 호출한다.

    키워드마다 독립적인 API 호출이라 병렬로 보낸다(2026-09-14) — 활성 키워드가
    100개를 넘어가면서 순차 처리 시 이 단계만으로 사이클 시간의 대부분(실측
    118개 키워드 기준 100초 이상)을 써버리는 게 확인됐다. HttpClient 는 이미
    커넥션 풀 16개로 병렬 사용을 전제해 뒀다(§ prefetch_articles).
    401/403(인증 실패)·429(한도 초과)는 여전히 감지하되, 병렬 실행에서는
    '연속 3회 실패 시 즉시 중단' 같은 순차 전용 최적화는 의미가 없어 빠졌다
    — 대신 전체 실패율로 설정 문제를 사후 판단한다.

    keyword_rows: [{"keyword": ..., "category": ...}, ...]
    """
    if not cfg.naver_enabled:
        return []
    headers = {"X-NCP-APIGW-API-KEY-ID": cfg.naver_client_id,
               "X-NCP-APIGW-API-KEY": cfg.naver_client_secret}
    stop_event = threading.Event()   # 재시도까지 해도 429 면 아직 안 나간 요청은 건너뛴다
    pause_until = [0.0]              # 429 를 본 순간 모든 작업자가 이 시각까지 함께 쉰다(순간 폭주 완화)

    def _fetch_one(row: Any) -> tuple[list[RawItem], int, bool]:
        if stop_event.is_set():
            return [], 0, False
        keyword = row["keyword"] if isinstance(row, dict) else row
        category = row.get("category", "") if isinstance(row, dict) else ""
        try:
            resp = None
            for attempt in range(NAVER_429_RETRIES + 1):
                wait = pause_until[0] - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                if stop_event.is_set():
                    return [], 0, False
                resp = http.get(
                    NAVER_NEWS_API,
                    # 5분 간격 폴링에는 최신 30건이면 충분하다. 100건을 받으면
                    # 대부분 backfill·중복이라 분석 백로그만 부풀린다.
                    params={"query": keyword, "display": 30, "sort": "date"},
                    headers=headers,
                )
                if resp.status_code != 429:
                    break
                # 429 는 하루 한도 소진이 아니라 '1초에 너무 많이' 인 경우가 대부분이다
                # (2026-09-30 로그: 라운드 시작 1초 뒤 동시 2~4건, 나머지 회차는 정상).
                # 예전엔 즉시 라운드 전체를 중단해 그 회차 키워드 조회가 통째로 사라졌다.
                pause_until[0] = max(pause_until[0], time.monotonic() + NAVER_429_BACKOFF_SEC * (attempt + 1))
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"{resp.status_code} 인증 실패 — .env 의 NAVER_CLIENT_ID/SECRET 이 "
                    "NAVER API HUB 의 Client ID/Secret 인지 확인하세요."
                )
            if resp.status_code == 429:
                stop_event.set()
                log.warning("Naver API 호출 한도 초과(429) — %d회 재시도 후에도 계속됩니다. "
                            "이번 실행의 네이버 수집을 중단합니다.", NAVER_429_RETRIES)
                return [], 0, True
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Naver API 조회 실패 (%s): %s", keyword, exc)
            return [], 0, True

        local_items: list[RawItem] = []
        local_dropped = 0
        for entry in data.get("items", []):
            link = entry.get("originallink") or entry.get("link", "")
            if not link:
                continue
            title = html_mod.unescape(re.sub(r"<[^>]+>", "", entry.get("title", ""))).strip()
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", entry.get("description", ""))).strip()
            if not _naver_item_relevant(title, category, keyword, snippet):
                local_dropped += 1
                continue
            published = parse_feed_datetime(entry.get("pubDate"))
            local_items.append(RawItem(
                url_source=normalize_url(link), url_original=link, title=title,
                published_at=published, source_type="naver_api", snippet=snippet,
            ))
        return local_items, local_dropped, False

    items: list[RawItem] = []
    dropped = 0
    fail_count = 0
    if keyword_rows:
        with ThreadPoolExecutor(max_workers=min(NAVER_FETCH_WORKERS, len(keyword_rows))) as pool:
            for its, drp, failed in pool.map(_fetch_one, keyword_rows):
                items.extend(its)
                dropped += drp
                if failed:
                    fail_count += 1
    if fail_count >= 3 and not items:
        log.error("Naver API 다수 실패(%d/%d건) — 설정을 점검하세요. 이번 실행 네이버 수집분 없음.",
                  fail_count, len(keyword_rows))
    if dropped:
        log.info("네이버 무관 기사 %d건 제외 (제목에 관련성 신호 없음)", dropped)
    return items


def collect_rss_feeds(http: HttpClient, feeds: Sequence[dict]) -> list[RawItem]:
    """언론사 자체 RSS. DB(feed_sources)로 추가하며 코드 수정이 필요 없다. (§6 확장성)

    피드마다 독립적인 요청이라 병렬로 받는다(2026-09-14) — 느린·응답 없는 피드
    하나가 나머지 전체를 붙잡지 않는다(§ collect_naver 와 같은 근거).
    """
    feedparser = _import("feedparser", "feedparser")

    def _fetch_one(feed_row: dict) -> list[RawItem]:
        url = feed_row.get("url") or ""
        if not url:
            return []
        try:
            resp = http.get(url)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("RSS 조회 실패 (%s): %s", feed_row.get("name"), exc)
            return []
        local_items: list[RawItem] = []
        for entry in feedparser.parse(resp.content).entries:
            link = entry.get("link", "")
            if not link:
                continue
            published = parse_feed_datetime(entry.get("published") or entry.get("updated"),
                                            entry.get("published_parsed") or entry.get("updated_parsed"))
            local_items.append(RawItem(
                url_source=normalize_url(link),
                url_original=link,
                title=html_mod.unescape(entry.get("title", "")).strip(),
                published_at=published,
                source_type="rss",
                press_hint=feed_row.get("name", ""),
                snippet=html_mod.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", ""))).strip(),
            ))
        return local_items

    items: list[RawItem] = []
    if feeds:
        with ThreadPoolExecutor(max_workers=min(SOURCE_FETCH_WORKERS, len(feeds))) as pool:
            for its in pool.map(_fetch_one, feeds):
                items.extend(its)
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


FETCH_FAIL_LIMIT = 3   # 원문 접속에 연속 이만큼 실패해야 '접속 실패'로 영구 제외한다


def fetch_fallback_url(item: Any) -> str:
    """원문 접속에 실패했지만 제목·리드만으로 이어갈 수 있으면 그 기사 URL, 아니면 ''.

    구글뉴스 중간 링크(news.google.com)는 실제 기사 주소가 아니라 카드 링크로 쓸 수 없으므로
    제외한다. 제목이 없거나 http 주소가 아니어도 이어갈 수 없다.
    """
    url = (item.url_original or "").strip()
    if not (item.title or "").strip() or not url.lower().startswith(("http://", "https://")):
        return ""
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("news.google.com") or _is_bare_root(url):
        return ""
    return normalize_url(url)


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
    # '이름 기자' 뿐 아니라 '이름 인턴기자'·'이름 수습기자' 처럼 이름과 '기자' 사이에
    # 직함 수식어가 붙는 경우도 이름을 잡는다. 수식어 없이 '인턴기자'만 있으면(이름이
    # 없는 경우) 이 수식어 자체가 캡처되는데, _valid_author 의 NON_AUTHOR_WORDS 가
    # 그런 수식어를 걸러낸다.
    re.compile(r'([가-힣]{2,4})\s*(?:칼럼|시민|객원|명예|인턴|수습|선임|특약)?기자'),
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


# 사람 이름이 아닌 바이라인 — 부서·데스크·통신사명 ('English News Desk', '편집국', '뉴스룸').
# 영문은 낱말 단위로, 한글은 끝말(부서 접미)로 본다. 사람 이름에는 거의 안 나오는 표현만 넣는다.
_NON_PERSON_BYLINE_RE = re.compile(
    r"(?i)\b(?:desk|newsroom|news|staff|team|editorial|editors?|bureau|agency|wire|online|digital|admin|yonhap|newsis)\b"
    r"|(?:데스크|뉴스룸|편집부|편집국|편집팀|취재팀|뉴스팀|기획팀|제작팀|사진부|영상팀|콘텐츠팀|산업부|경제부|사회부"
    r"|정치부|국제부|증권부|온라인팀|특파원단|속보팀|디지털팀|관리자)$")


def is_non_person_byline(name: str) -> bool:
    """기자 이름이 아니라 부서·데스크·통신사명인가."""
    nm = (name or "").strip()
    return bool(_NON_PERSON_BYLINE_RE.search(nm)) or normalize_chip(nm) in _WIRE_AND_LABEL_WORDS


# 통신사·바이라인 라벨 — 사람이 아니다. ('Reuters', 'AFP', '사진', '영상' …)
_WIRE_AND_LABEL_WORDS = frozenset({
    "reuters", "afp", "ap", "epa", "bloomberg", "xinhua", "kyodo", "tass", "cnn", "bbc",
    "사진", "영상", "그래픽", "촬영", "취재", "글", "정리", "제공", "자료", "편집", "기획",
})


@lru_cache(maxsize=1)
def _press_name_keys() -> frozenset:
    """매핑표의 언론사명(정규화) — 바이라인 앞에 붙는 소속 매체('이데일리 김철수')를 떼는 데 쓴다."""
    return frozenset(normalize_chip(n) for n, _ in SEED_PRESS.values())


def _is_affiliation_token(tok: str) -> bool:
    """바이라인의 한 토큰이 사람이 아니라 소속(매체명·부서·통신사·라벨)인가."""
    key = normalize_chip(tok)
    return (not key or key in _press_name_keys() or key in NON_AUTHOR_WORDS
            or is_non_person_byline(tok) or tok.endswith(MEDIA_NAME_SUFFIX))


def author_display(author: str) -> str:
    """카드 머리표에 쓸 기자명 — 소속·직함·이메일을 뗀 사람 이름만 '·' 로 잇는다.
    사람 이름이 하나도 안 남으면(부서·데스크·통신사뿐) 빈 문자열 → '[언론사]' 만 표기."""
    raw = (author or "").strip()
    if not raw:
        return ""
    names = split_authors(raw)
    return "·".join(names) if names else ""


def _valid_author(raw: str, press_key: str) -> str:
    """기자명 후보를 정제·검증한다. 부적합하면 빈 문자열."""
    raw_text = fix_mojibake(decode_unicode_escapes((raw or "").strip()))
    if is_non_person_byline(raw_text):   # clean_author 가 '취재팀'의 '팀'을 떼기 전에 먼저 거른다
        return ""
    name = clean_author(raw_text)
    # '김철수 선임기자'·'산업부 김철수'·'이데일리 김철수' 처럼 직함·소속·이메일이 붙은 값은 이름만 남긴다.
    names = split_authors(name)
    if not names:
        return ""
    name = "·".join(names)
    if not (2 <= len(name) <= 20):
        return ""
    if is_non_person_byline(name):
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
    # 주석(<!-- … -->) 안에 죽은 '관련기사' 위젯 마크업을 그대로 남겨 두는 사이트가
    # 있다(실사례: PPSS — 화면에 안 보이는 주석 속 다른 기사 3건의 '권미나 기자'가
    # 이 기사의 실제 기자 '이승민 인턴기자'보다 더 많이 잡혀 최빈값으로 오채택됐다).
    # 정규식은 HTML 구조를 모르므로 주석을 먼저 지워야 한다.
    html = re.sub(r"<!--.*?-->", "", html or "", flags=re.S)

    # 1) JSON-LD author (보통 정확, 단일 매치)
    for source in (html, body):
        if source and (m := AUTHOR_PATTERNS[0].search(source)):
            if name := _valid_author(m.group(1), press_key):
                return name

    # 2) 본문 서명 'OOO 기자' — 서명은 기사에 여러 번 나오므로 최빈값을 택한다.
    #    카테고리 라벨('칼럼기자', '시민기자')은 1회만 나와 자연히 밀린다.
    #    body(추출된 본문)를 html(사이드바·관련기사 위젯 포함 전체)보다 먼저 본다 —
    #    본문에 서명이 있으면 그걸로 충분하고, 전체 html 까지 보면 이 기사와 무관한
    #    다른 기사의 서명이 최빈값을 오염시킬 수 있다.
    for source in (body, html):
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
    # 화면 요소·기사 구분 낱말이 기자명으로 잡힌 사례('날씨'·'보도' 등, 2026-10-01)
    "날씨", "보도", "제보", "속보", "단독", "종합", "인사", "부고", "동정", "포토", "기획", "연재",
    "오피니언", "알림", "공지", "광고", "홍보", "보도자료", "기사", "출처", "관리자", "운영자", "admin",
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


_JSON_BODY_RE = re.compile(r'"type"\s*:\s*"text"\s*,\s*"content"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _json_script_body(html: str) -> str:
    """SPA(Next.js 등)가 본문을 <script> 안 JSON 으로 넣는 사이트 대응.

    news1.kr 처럼 문단을 {"type":"text","content":"..."} 배열로 담는 구조를 읽는다.
    이런 페이지는 <script> 를 걷어내는 readability·BeautifulSoup 폴백이 내비게이션만
    긁어 와서 본문이 통째로 비고, '포스코'가 본문에만 있는 기사가 무관 처리됐다.
    """
    parts: list[str] = []
    for mo in _JSON_BODY_RE.finditer(html or ""):
        try:
            txt = json.loads(f'"{mo.group(1)}"')
        except ValueError:
            continue
        txt = txt.strip()
        if txt:
            parts.append(txt)
    return "\n".join(parts)


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
        readable = text
    except SystemExit:
        raise
    except Exception as exc:
        log.debug("Readability 실패: %s", exc)
        readable = ""
    # SPA(Next.js 등)는 본문이 <script> JSON 안에 있어 readability 가 내비게이션만 긁는다.
    # JSON 문단 쪽이 더 길면 그것을 본문으로 쓴다. (문자열 포함 검사라 대부분 비용 0)
    if '"type":"text"' in (html or ""):
        json_body = _json_script_body(html)
        if len(json_body) > len(readable):
            return json_body
    if len(readable) >= 200:
        return readable
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


_PEOPLE_MARK_RE = re.compile(r"[◇◈◆◎□■▲△▶▷]")
_NOTICE_STRONG_FOOTER = ("사업자등록번호", "Copyright", "All rights reserved")


def _notice_text(html: str, body: str, title: str = "") -> str:
    """인사·부고 공지 텍스트를 최대한 알차게 뽑는다.

    이런 공지는 몇 줄로 짧아서 Readability 가 본문 대신 <푸터(회사 정보)>를 잡는 일이 잦다.
    body · og:description · meta description · <article> 컨테이너 텍스트를 후보로 모은다.
    · 저작권·무단전재 같은 꼬리 문구는 **후보를 버리지 않고 그 앞까지만 쓴다** — 연합뉴스는 명단 바로 뒤에
      '<저작권자(c) 연합뉴스, 무단 전재…>' 가 붙어 있어, 예전엔 그 때문에 통째로 버려지고 og:description
      (머리말 한 줄 '◇ 신규 임원 승진')만 남아 명단이 사라졌다(2026-10-01 한화저축은행 사례).
    · 회사정보 푸터(사업자등록번호 등)가 남은 후보는 버린다.
    · 고르는 기준: 명단 표시(◇▲)가 있고 → 제목 주제어가 들어 있고 → 더 긴 것.
      (예전엔 가장 긴 것을 골라, 사이드바의 긴 칼럼이 진짜 공지보다 먼저 뽑혔다.)
    """
    cands = [body or ""]
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(pat, html or "", re.I)
        if m:
            cands.append(html_mod.unescape(m.group(1)))
    for m in list(re.finditer(r"<article[^>]*>(.*?)</article>", html or "", re.S | re.I))[:5]:
        seg = re.sub(r"<script.*?</script>", " ", m.group(1), flags=re.S | re.I)
        seg = re.sub(r"<br\s*/?>", "\n", seg, flags=re.I)
        seg = html_mod.unescape(re.sub(r"<[^>]+>", " ", seg))
        seg = re.sub(r"[ \t]+", " ", seg).strip()
        if seg:
            cands.append(seg)
    good = []
    for c in cands:
        c = c.strip()
        if _PEOPLE_MARK_RE.search(c):
            # 연합뉴스 명단 뒤에는 '(서울=연합뉴스) / 제보는 카카오톡 / <저작권자…>' 가 줄바꿈을 끼고 붙는다.
            # 꼬리 규칙이 줄바꿈을 넘어 끝까지 자르도록 DOTALL 이다(2026-10-02 예금보험공사 사례:
            # 줄바꿈 때문에 꼬리가 안 잘려 본문 후보가 통째로 버려지고 머리말 한 줄만 남았다).
            c = _NOTICE_TAIL_RE.sub("", _NOTICE_DATELINE_RE.sub("", c)).strip()
        if c and not any(h in c for h in _NOTICE_STRONG_FOOTER + _NOTICE_FOOTER_HINT):
            good.append(c)
    if not good:
        return body or ""
    kws = _title_keywords(title)
    return max(good, key=lambda c: (bool(_PEOPLE_MARK_RE.search(c)),
                                    bool(kws) and any(k in c.lower() for k in kws), len(c)))


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
    if (not [g for g in found if g not in NON_POSCO_GROUPS]
            and any(alias in lowered for alias in _GROUP_ALIASES_LOWER["포스코"])):
        found.append("포스코")
    return found


# ── 포스코퓨처엠 언급 발췌 (사용자 지정 2026-09-29, 흐름 개선 2026-10-01) ─────────
# 카드의 '포스코 관점'(LLM 생성문)을 대신해, 본문에서 포스코퓨처엠이 실제로 나온 대목을 기사 주제(도입부)와
# 함께 문단 단위로 그대로 발췌한다. 이 발췌문은 언론사 탭의 논조 판정(LLM) 입력으로도 재사용하므로
# DB 에 저장한다. 영어 기사는 한글로 번역해 저장한다(translate_to_korean).
PFM_EXCERPT_MAX = 700      # 발췌 전체 상한(글자)
PFM_PARA_MAX = 380         # 언급 문단 하나에서 가져오는 상한 — 넘으면 언급 문장 앞뒤를 문장 단위로만 자른다
PFM_MENTION_PARAS = 3      # 언급 문단을 최대 몇 개까지 담을지
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。…])\s+|(?<=다\.)(?=\S)")


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in _SENT_SPLIT_RE.split(text or "") if x and x.strip()]


def _cut_at_word(text: str, limit: int) -> str:
    """limit 를 넘으면 단어 경계에서 잘라 … 를 붙인다(문장이 한 개뿐인 극단적인 경우에만 쓴다)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return (cut[:cut.rfind(" ")] if " " in cut else cut).rstrip() + "…"


def _paragraph_window(par: str, aliases: Sequence[str], limit: int) -> str:
    """문단이 limit 이하면 통째로, 길면 첫 언급 문장 앞뒤를 **문장 단위로** 붙여 limit 안에서 자른다."""
    if len(par) <= limit:
        return par
    sents = _sentences(par)
    hit = next((i for i, x in enumerate(sents) if any(a in x.lower() for a in aliases)), 0)
    lo = hi = hit
    total = len(sents[hit])
    while True:   # 뒤(이어지는 내용) → 앞(배경) 순으로 번갈아, 문장이 통째로 들어갈 때만 붙인다
        grew = False
        if hi + 1 < len(sents) and total + len(sents[hi + 1]) + 1 <= limit:
            hi += 1
            total += len(sents[hi]) + 1
            grew = True
        if lo - 1 >= 0 and total + len(sents[lo - 1]) + 1 <= limit:
            lo -= 1
            total += len(sents[lo]) + 1
            grew = True
        if not grew:
            break
    return _cut_at_word(" ".join(sents[lo:hi + 1]), limit)


# 문단 맨 앞에 붙는 작성 표기 — '(서울=연합뉴스) 홍길동 기자 =', '(뉴스메이커=이영수 기자)', '[잡포스트] 전진홍 기자', '홍길동 기자 ='
_BYLINE_PREFIX_RES = (
    re.compile(r"^\s*[\(\[（【][^\)\]）】]{0,24}[=＝][^\)\]）】]{0,24}[\)\]）】]\s*"
               r"(?:[가-힣]{2,4}\s*(?:선임|수석|전문|인턴|수습)?\s*기자\s*(?:[=＝·:\-]\s*)?)?"),
    re.compile(r"^\s*[\[【][^\]】]{1,14}[\]】]\s*(?:[가-힣]{2,4}\s*(?:선임|수석|전문|인턴|수습)?\s*기자\s*)?(?:[·=＝:\-]\s*)?"),
    re.compile(r"^\s*[가-힣]{2,4}\s*(?:선임|수석|전문|인턴|수습)?\s*기자\s*[=＝·:]\s*"),
)


def _strip_byline_prefix(par: str, aliases: Sequence[str]) -> str:
    """문단 앞의 작성 표기(기자명·매체 말머리)를 뗀다. 회사명이 들어 있는 괄호이거나 뗀 뒤 글이 너무 짧으면 그대로 둔다."""
    for rx in _BYLINE_PREFIX_RES:
        m = rx.match(par)
        if m and len(par) - m.end() >= 10 and not any(a in m.group(0).lower() for a in aliases):
            par = par[m.end():]
    return par.strip()


def _headline_like(par: str) -> bool:
    """소제목·헤드라인 같은 줄인가 — 짧고 문장 끝 부호가 없다. 맥락 문장으로는 쓰지 않는다."""
    p = par.strip()
    return len(p) <= 70 and not p.endswith((".", "!", "?", "。", "…", "”", "\"", "’", "'", "다", "요"))


def _tail_sentences(par: str, limit: int = 170) -> str:
    """문단의 마지막 문장들을 limit 안에서 — 언급 문단 바로 앞의 맥락."""
    out: list[str] = []
    total = 0
    for sent in reversed(_sentences(par)[-3:]):
        if out and total + len(sent) + 1 > limit:
            break
        if not out and len(sent) > limit:
            return _cut_at_word(sent, limit)
        out.insert(0, sent)
        total += len(sent) + 1
    return " ".join(out)


def _head_sentences(par: str, limit: int = 170) -> str:
    """문단의 첫 문장들을 limit 안에서 — 언급 문단 바로 뒤에 이어지는 내용."""
    out: list[str] = []
    total = 0
    for sent in _sentences(par)[:3]:
        if out and total + len(sent) + 1 > limit:
            break
        if not out and len(sent) > limit:
            return _cut_at_word(sent, limit)
        out.append(sent)
        total += len(sent) + 1
    return " ".join(out)


# 증권 기사 본문에는 종목명 바로 아래 줄에 시세 위젯 '(203,500원 ▲16,600 +8.88%)' 이 끼어 있다(머니투데이 등).
# 그러면 '포스코퓨처엠' 한 줄·시세 한 줄·'·' 한 줄로 쪼개져, 15자 미만 줄을 버리는 발췌 규칙이 종목명을 통째로 버렸다
# (2026-10-06 머니투데이 '이차전지주 장중 강세…' 사례). 시세 위젯 줄과 그 앞의 종목명 줄을 앞뒤 글에 이어 붙인다.
_QUOTE_WIDGET_RE = re.compile(r"\n([^\n]{1,20})\n(\([\d,]+원[^)\n]*\))[ \t]*(?:\n|$)")


def _reflow_quote_widgets(text: str) -> str:
    """'종목명 \\n (시세) \\n 은 …' 처럼 쪼개진 줄을 한 문장으로 잇는다. 시세 위젯이 없는 글은 그대로 돌려준다."""
    if "원 " not in text or "(" not in text:
        return text
    prev = None
    while prev != text:                       # 한 번에 하나씩 이어 붙이므로 더 줄지 않을 때까지 반복
        prev = text
        text = _QUOTE_WIDGET_RE.sub(r" \1 \2 ", text)
    text = re.sub(r"[ \t]*\n·[ \t]*\n", " · ", text)          # 종목 사이 가운뎃점만 있는 줄
    return re.sub(r"[ \t]{2,}", " ", text)


def extract_pfm_excerpt(body: str) -> str:
    """본문에서 포스코퓨처엠(옛 사명·영문·띄어쓴 표기 포함) 언급을 **원문 순서 그대로 이어지게** 발췌한다.

    ① 언급이 나온 문단 — 문단째(길면 언급 문장 앞뒤를 문장 단위로)
    ② 그 문단 바로 앞 문장(맥락)과 바로 뒤 문장(이어지는 내용) — 원문에서 붙어 있던 글만 가져온다
    언급 문단은 최대 3개, 전체는 700자 이내. 문장은 중간에서 자르지 않고, 기사의 문장을 그대로 옮길 뿐
    재서술하지 않는다.

    예전엔 '기사 주제(첫 문단)'를 따로 앞에 붙여 서로 안 이어지는 두 토막이 됐고, 카드의 접힌 줄 아래로 정작
    언급 부분이 밀려났다(2026-10-01 지적). 이제는 한 덩어리의 이어진 글이고, 앞의 '(서울=연합뉴스) 홍길동 기자 ='
    같은 작성 표기와 헤드라인성 줄은 걷어 낸다. 언급이 없으면 빈 문자열(카드는 이 영역을 그리지 않는다).
    """
    # 줄바꿈 아닌 공백류(NBSP·전각 공백 등)는 한 칸으로, 폭 없는 문자(ZWSP·BOM 등)는 지운다 —
    # '포스코\xa0퓨처엠'·'포스코​퓨처엠' 처럼 눈에 안 보이는 문자가 끼면 별칭 매칭이 빗나가 언급이 통째로 빠졌다.
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]", "", body or "")
    text = re.sub(r"[ \t\r\f\v\u00a0\u3000\u2002-\u200a\u202f]+", " ", text).strip()
    if not text:
        return ""
    text = _reflow_quote_widgets(text)
    aliases = _GROUP_ALIASES_LOWER.get("포스코퓨처엠", [])
    paras = [_strip_byline_prefix(p.strip(), aliases) for p in text.split("\n") if len(p.strip()) >= 15] or [text]
    hits = [i for i, p in enumerate(paras) if any(a in p.lower() for a in aliases)]
    if not hits:
        return ""
    hit_set = set(hits)

    segs: list[tuple[int, str]] = []
    used_idx: set[int] = set()
    used = 0
    for idx in hits[:PFM_MENTION_PARAS]:
        if idx in used_idx:
            continue
        window: list[tuple[int, str]] = []
        prev_i, next_i = idx - 1, idx + 1
        if prev_i >= 0 and prev_i not in hit_set and prev_i not in used_idx and not _headline_like(paras[prev_i]):
            ctx_before = _tail_sentences(paras[prev_i])
            if ctx_before:
                window.append((prev_i, ctx_before))
        window.append((idx, _paragraph_window(paras[idx], aliases, PFM_PARA_MAX)))
        if next_i < len(paras) and next_i not in hit_set and next_i not in used_idx and not _headline_like(paras[next_i]):
            ctx_after = _head_sentences(paras[next_i])
            if ctx_after:
                window.append((next_i, ctx_after))
        if used + sum(len(t) + 1 for _, t in window) > PFM_EXCERPT_MAX:
            window = [w for w in window if w[0] == idx]      # 넘치면 앞뒤 맥락부터 뺀다
            if used + len(window[0][1]) + 1 > PFM_EXCERPT_MAX:
                break
        for i, t in window:
            segs.append((i, t))
            used_idx.add(i)
            used += len(t) + 1
    segs.sort(key=lambda x: x[0])
    return "\n".join(t for _, t in segs)


def excerpt_for_message(text: str, limit: int = 450) -> str:
    """텔레그램 등 메시지용 발췌 — 줄바꿈을 공백으로 잇고, 길면 문장 단위로만 자른다(문장 중간에서 안 끊는다)."""
    one = re.sub(r"\s*\n\s*", " ", (text or "").strip())
    if len(one) <= limit:
        return one
    out = ""
    for sent in _sentences(one):
        if len(out) + len(sent) + 1 > limit:
            break
        out = f"{out} {sent}".strip()
    return out or _cut_at_word(one, limit)


def looks_english(text: str) -> bool:
    """글자의 대부분이 영문인가(한글이 거의 없다) — 영어 기사 판별용. 너무 짧으면 판단하지 않는다."""
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < 12:
        return False
    hangul = sum(1 for c in letters if "가" <= c <= "힣")
    latin = sum(1 for c in letters if c.isascii())
    return hangul / len(letters) < 0.1 and latin / len(letters) > 0.8


def normalize_group_list(groups: Iterable[str]) -> list[str]:
    """그룹사 목록 정리. 구체 계열사가 있으면 상위 개념 '포스코'는 뺀다.

    룰 기반 결과와 LLM 결과를 합칠 때 '포스코홀딩스 · 포스코퓨처엠 · 포스코' 처럼
    상위·하위가 같이 붙는다. 칩이 늘어나기만 하고 정보량은 늘지 않는다.
    """
    cleaned = dedupe_chips(g for g in groups if g in GROUP_COMPANIES)
    if "포스코" in cleaned and any(g not in NON_POSCO_GROUPS and g != "포스코" for g in cleaned):
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


def score_breakdown(title: str, body: str, group_companies: Sequence[str], press_tier: int,
                    overrides: dict[str, dict] | None = None,
                    custom_rules: Sequence[dict] | None = None) -> list[tuple[str, int]]:
    """중요도 점수를 이루는 항목 내역 [(항목 설명, 가감 점수)]. 0점 항목은 뺀다.

    overrides: 마스터 패널에서 고친 기본 항목 {키: {"points": int, "enabled": bool}}.
    custom_rules: 마스터 패널에서 추가한 키워드 기반 항목
                  [{"keywords": [...], "scope": "title"|"title_or_body", "points": int}, ...].
    둘 다 없으면(선택 인자) 기존 하드코딩 상수 그대로 동작한다.
    카드의 '긍정 · 22' 툴팁이 이 내역을 그대로 보여 준다 — 점수 계산과 설명이 갈라지지 않도록
    score_article 도 이 함수의 합계를 쓴다."""
    title_l = (title or "").lower()
    full_l = f"{title} {body}".lower()
    items: list[tuple[str, int]] = []

    def pts(key: str) -> int:
        default, _label = SCORE_RULE_DEFS[key]
        rule = (overrides or {}).get(key)
        if not rule:
            return default
        if rule.get("enabled") is False:
            return 0
        try:
            return int(rule.get("points", default))
        except (TypeError, ValueError):
            return default

    def add(key: str) -> None:
        p = pts(key)
        if p:
            items.append((SCORE_RULE_DEFS[key][1], p))

    futurem = _GROUP_ALIASES_LOWER["포스코퓨처엠"]
    if any(a in title_l for a in futurem):
        add("futurem_title")
    elif any(a in full_l for a in futurem):
        add("futurem_body")

    if any(g != "포스코퓨처엠" for g in group_companies):
        add("group")

    # 배터리 생태계 기사(포스코 미언급 허용 대상)는 그룹사 언급이 없어도
    # 전방 수요·경쟁 동향이라 최소 중요도를 준다. 예전엔 0점이라 큐에서 굶었다.
    if is_battery_scope(title_l, ""):
        add("battery_title")
    elif is_battery_scope("", body[:1500]):
        add("battery_body")
    if is_trade_topic(title):
        add("trade")

    if any(w in full_l for w in _POLICY_KEYWORDS_LOWER):
        add("policy")
    if press_tier <= 1:
        add("major_press")

    # 단순 시황·주가 기사는 알림 피로를 유발하므로 감점한다.
    if any(w in title_l for w in _MARKET_ONLY_KEYWORDS_LOWER):
        add("market_penalty")

    # 사용자가 마스터 패널에서 추가한 키워드 항목 — 제목(또는 제목+본문)에 키워드 중
    # 하나라도 있으면 점수를 더하거나(양수) 뺀다(음수).
    for rule in (custom_rules or []):
        kws = [str(k).lower() for k in (rule.get("keywords") or []) if str(k).strip()]
        if not kws:
            continue
        hay = title_l if rule.get("scope") == "title" else full_l
        if any(k in hay for k in kws):
            try:
                p = int(rule.get("points") or 0)
            except (TypeError, ValueError):
                p = 0
            if p:
                items.append((f"직접 추가한 항목({', '.join(str(k) for k in (rule.get('keywords') or [])[:3])})", p))
    return items


def score_article(title: str, body: str, group_companies: Sequence[str], press_tier: int,
                  overrides: dict[str, dict] | None = None,
                  custom_rules: Sequence[dict] | None = None) -> int:
    """중요도 0~100. (PRD F3.2) 항목 내역은 score_breakdown 참고."""
    score = sum(p for _, p in score_breakdown(title, body, group_companies, press_tier,
                                              overrides, custom_rules))
    return int(clamp(score, 0, 100))


# =====================================================================
# 10. LLM 분석 (PRD F4)
#     요약 · 키워드 · 그룹사 · 감성 · SWOT 을 호출 1회로 받는다.
#     '포스코 관점'은 2026-09-29 생성 중단 — 본문 발췌(extract_pfm_excerpt)로 대체했다.
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

[키워드]
- keywords: 기사 핵심 키워드 최대 6개, 한국어 명사구
- 서로 중복되거나 포함관계인 키워드를 넣지 않는다
- 회사명은 keywords 가 아니라 group_companies 에 넣는다

[관련 그룹사]
- group_companies: 기사 본문에 그 회사 이름이 실제로 등장하는 경우에만 넣는다
  포스코홀딩스, 포스코퓨처엠, 포스코DX, 포스코인터내셔널, 포스코이앤씨, 포스코,
  배터리협회(한국배터리산업협회·KBIA — 포스코 계열은 아니지만 함께 추적한다)
- 여러 소식을 묶은 브리핑·모음 기사는 본문 전체(주요 항목이 아닌 다른 항목
  포함)를 끝까지 확인한다 — 요약 문장에 담기지 않은 항목이라도 본문에 회사
  이름이 있으면 반드시 group_companies 에 넣는다
- '이차전지·배터리·공급망 뉴스니까 포스코퓨처엠' 같은 추측은 금지한다
- 회사명이 본문에 없으면 관련 산업 기사라도 빈 배열로 둔다
- 목록에 없는 회사명을 만들어내지 않는다

[감성]
- sentiment: "긍정" | "중립" | "부정" 중 하나
- 주가 호재/악재가 아니라 포스코 그룹의 대외협력 대응 필요성 기준으로 판단한다
- sentiment_reason: 그렇게 판단한 근거를 본문 속 표현 기준으로 한 줄(60자 이내)

[SWOT]
- swot: 이 기사의 사안이 포스코 그룹(철강·이차전지소재·인프라 전반)에 주는
  영향을 S/W/O/T 로 평가한다. 각 항목 score(0~100 정수)와 text(1~2줄 근거)
- 기사 본문에서 실제로 읽어낼 수 있는 함의를 적고, 최소한 한 항목은 채운다.
  신사업·수요 확대·우호적 협력 = O / 경쟁 심화·규제·공급과잉·자원 리스크 = T /
  자사 기술력·생산능력·계약·점유율 = S / 비용 부담·생산 차질·구조적 약점 = W
- 정말로 근거를 찾을 수 없는 항목만 score 0, text "해당 없음"
- score 는 아래 기준으로 매기고, text 첫머리에 근거가 된 사실(수치·계약·규제 등)을 적는다
  0        본문에 근거 없음
  1~30     언급은 있으나 간접적이거나 전망·가능성 수준
  31~60    사실이지만 영향이 제한적(일부 사업·지역·제품) 이거나 수치 없이 방향만 언급
  61~80    구체적 사실(수치·계약·투자 규모·일정)이 본문에 있고 그룹 사업에 직접 영향
  81~100   그룹 핵심 사업 전반에 영향을 주는 결정적 사안이며 본문이 수치로 뒷받침
- 같은 사안을 두 항목에 중복해서 높게 매기지 않는다. 본문에 없는 사실로 점수를 올리지 않는다

[출력 형식 — 이 구조를 정확히 지킨다]
{{"summary":["문장1","문장2","문장3"],"keywords":["..."],
"group_companies":["..."],"sentiment":"중립","sentiment_reason":"...",
"swot":{{"s":{{"score":0,"text":"..."}},"w":{{"score":0,"text":"..."}},
"o":{{"score":0,"text":"..."}},"t":{{"score":0,"text":"..."}}}}}}

제목: {title}
언론사: {press}
본문:
{body}
"""

MAX_BODY_CHARS = 6000  # 토큰 비용 상한. 기사 본문 대부분은 이 안에 들어간다.

# ── 포스코퓨처엠 논조 판정 (사용자 지정 2026-09-29) ─────────────────────
# 입력은 본문 전체가 아니라 extract_pfm_excerpt 발췌문뿐이다. 다른 회사 평가는 반영하지 않는다.
PFM_TONES = ("긍정", "중립", "부정")
PFM_TONE_SYSTEM = ("당신은 기업 홍보팀의 언론 모니터링 담당자다. 주어진 발췌문만 근거로 "
                   "판정하고 JSON 으로만 답한다.")
PFM_TONE_PROMPT = """[판정 대상] 포스코퓨처엠
[기사 본문 발췌 — 포스코퓨처엠 언급 부분]
{excerpt}

이 기사가 '포스코퓨처엠'에 대해 어떤 논조인지 판정하라.
- 긍정: 포스코퓨처엠의 실적·수주·기술·투자·평판 등을 호의적으로 다룸
- 부정: 포스코퓨처엠의 손실·사고·리스크·비판·실적 악화 등을 다룸
- 중립: 단순 사실 전달, 여러 회사 나열 속 언급, 평가가 드러나지 않음
- 다른 회사(경쟁사·고객사·모회사 등)에 대한 평가는 판정에 반영하지 않는다
- 발췌문에 없는 내용을 추측하지 않는다
- reason: 판정 근거가 된 발췌문 속 표현을 한 줄(60자 이내)로

형식: {{"tone":"긍정|중립|부정","reason":"..."}}"""


# ── 감성(긍정·중립·부정) 근거 사후 생성 — 예전 기사는 근거를 저장하기 전에 분석돼 비어 있다(2026-10-02) ──
SENTI_REASON_SYSTEM = ("당신은 기업 홍보팀의 언론 모니터링 담당자다. 이미 정해진 감성 판정의 근거를 "
                       "기사 속 표현으로 설명하고 JSON 으로만 답한다.")
SENTI_REASON_PROMPT = """[기사 제목] {title}
[기사 내용]
{text}

이 기사는 포스코 그룹의 대외협력 대응 필요성 기준으로 '{sentiment}'(긍정=우호적 보도, 부정=대응이 필요한 비판·리스크,
중립=단순 사실 전달)으로 분류돼 있다. 그렇게 분류된 근거를 기사 속 표현을 인용해 한 줄(70자 이내)로 쓰라.
- 판정을 바꾸지 말고, 기사에 없는 내용을 지어내지 않는다.
형식: {{"reason":"..."}}"""


# ── 언론사 탭: 언론사가 포스코퓨처엠을 전반적으로 어떻게 평가하는지 한 줄 (2026-10-02) ──
PRESS_OVERVIEW_SYSTEM = ("당신은 기업 홍보팀의 언론 모니터링 담당자다. 한 언론사의 포스코퓨처엠 기사 제목과 논조 근거만 보고 "
                         "그 언론사의 전반적 시각을 한 줄로 요약하며, JSON 으로만 답한다.")
PRESS_OVERVIEW_PROMPT = """[언론사] {press}
[논조 분포] {dist}
[최근 기사 — 제목 | 논조 | 판정 근거]
{lines}

이 언론사가 포스코퓨처엠을 전반적으로 어떻게 평가·보도하는지 **한 줄(80자 이내)** 로 써라.
- 위 제목·근거에 실제로 나타난 내용만 쓴다. 기사에 없는 평가를 지어내지 않는다.
- 논조가 갈리면 갈린다고 쓰고(예: 실적은 호의적, 투자 부담은 비판적), 주로 다루는 주제를 한두 개 넣는다.
- 기사가 1~2건뿐이면 '(기사 N건 기준)' 을 덧붙인다.
형식: {{"overview":"..."}}"""


TRANSLATE_SYSTEM = ("당신은 영한 뉴스 번역가다. 원문의 뜻을 바꾸거나 덧붙이지 않고 자연스러운 한국어 "
                    "뉴스 문체로 옮기며, JSON 으로만 답한다.")
TRANSLATE_PROMPT = """아래 영어 기사의 제목과 발췌를 한국어로 번역하라.
- 원문에 없는 내용을 추가하거나 빼지 않는다. 숫자·단위·날짜·인용은 그대로 옮긴다.
- 회사명은 한국에서 쓰는 표기를 쓴다: POSCO Future M → 포스코퓨처엠, POSCO Holdings → 포스코홀딩스,
  POSCO → 포스코, LG Energy Solution → LG에너지솔루션, Samsung SDI → 삼성SDI, SK On → SK온.
  그 밖의 고유명사는 통용 표기가 있으면 그것을, 없으면 원어를 그대로 둔다.
- 문장은 "~했다"체 뉴스 문장으로. 발췌에 줄바꿈이 있으면 같은 줄바꿈을 유지한다.
- 비어 있는 항목은 빈 문자열로 둔다.

[제목]
{title}

[발췌]
{excerpt}

형식: {{"title":"한국어 제목","excerpt":"한국어 발췌"}}"""


@dataclass
class Analysis:
    summary_sentences: list[str] = field(default_factory=list)
    perspective: str = ""
    keywords: list[str] = field(default_factory=list)
    group_companies: list[str] = field(default_factory=list)
    sentiment: str = "중립"
    sentiment_reason: str = ""
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


NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
XAI_BASE_URL = "https://api.x.ai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _make_nvidia_client(api_key: str):
    """NVIDIA NIM 클라이언트. OpenAI 호환 엔드포인트라 openai 라이브러리를 그대로 쓴다."""
    openai = _import("openai", "openai")
    return openai.OpenAI(api_key=api_key, base_url=NVIDIA_NIM_BASE_URL)


def _make_xai_client(api_key: str):
    """xAI(Grok) 클라이언트. 마찬가지로 OpenAI 호환 엔드포인트다."""
    openai = _import("openai", "openai")
    return openai.OpenAI(api_key=api_key, base_url=XAI_BASE_URL)


def _make_gemini_client(api_key: str):
    """Gemini 클라이언트. 구글이 제공하는 OpenAI 호환 엔드포인트를 그대로 쓴다."""
    openai = _import("openai", "openai")
    return openai.OpenAI(api_key=api_key, base_url=GEMINI_BASE_URL)


class LLMClient:
    def __init__(self, cfg: Config) -> None:
        _hush_libraries()  # openai/httpx 가 import 시 로깅을 다시 켜는 경우 대비
        self.client = _make_openai_client(cfg.openai_api_key)
        self.model = cfg.llm_model
        self.embedding_model = cfg.embedding_model
        # OpenAI 임베딩이 실패할 때만 쓰는 대체 경로. 키가 없으면 그대로 None — 기존처럼
        # 실패를 삼키고 넘어간다(4단계 임베딩 유사도 판정 생략, 3단계까지만 적용).
        # 서로 다른 임베딩 모델 벡터는 차원·벡터공간이 달라 직접 비교할 수 없지만,
        # cosine() 이 차원이 다르면 0.0 을 돌려주도록 이미 방어돼 있어 오작동 없이
        # '유사하지 않음'으로만 처리된다 — 대체가 걸린 동안 중복 탐지 정확도만 낮아진다.
        self.nvidia_embed_client = (
            _make_nvidia_client(cfg.nvidia_embed_api_key) if cfg.nvidia_embed_api_key else None)
        self.nvidia_embed_model = cfg.nvidia_embed_model
        # 채팅(분석·요약·주간레포트·챗봇 응답)이 실패할 때만 쓰는 대체 경로들.
        # 순서: OpenAI(기본) → Gemini → Grok(xAI) → NVIDIA — 키가 없는 단계는 건너뛴다.
        # 마찬가지로 전부 실패하면 OpenAI 실패가 곧 최종 실패가 되는 기존 동작 유지.
        self.gemini_llm_client = (
            _make_gemini_client(cfg.gemini_api_key) if cfg.gemini_api_key else None)
        self.gemini_llm_model = cfg.gemini_llm_model
        self.xai_llm_client = (
            _make_xai_client(cfg.xai_api_key) if cfg.xai_api_key else None)
        self.xai_llm_model = cfg.xai_llm_model
        self.nvidia_llm_client = (
            _make_nvidia_client(cfg.nvidia_llm_api_key) if cfg.nvidia_llm_api_key else None)
        self.nvidia_llm_model = cfg.nvidia_llm_model
        # 모델별로 지원하는 파라미터가 다르다. 첫 호출에서 학습해 이후 재시도를 줄인다.
        # 공급자마다 서로 다른 모델이라 지원 여부도 따로 학습해야 한다.
        self._supports_json_mode = True
        self._gemini_supports_json_mode = True
        self._xai_supports_json_mode = True
        self._nvidia_supports_json_mode = True
        # 중복 판정 4단계의 임베딩 호출을 1회 실행당 이 수로 제한한다.
        # 네이버 수집 시 유사 제목이 대량으로 들어와 임베딩 폭주가 발생할 수 있다.
        self.max_embed_per_run = MAX_EMBED_PER_RUN
        self._embed_calls = 0

    def reset_run(self) -> None:
        self._embed_calls = 0

    def _chat_once(self, client: Any, model: str, supports_json_attr: str,
                    system: str, user: str) -> tuple[str, dict]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if getattr(self, supports_json_attr):
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            # response_format 미지원 모델이면 한 번만 빼고 재시도한다.
            if getattr(self, supports_json_attr) and "response_format" in str(exc):
                log.info("모델이 JSON 모드를 지원하지 않아 일반 모드로 전환합니다.")
                setattr(self, supports_json_attr, False)
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            else:
                raise
        usage = {}
        if getattr(resp, "usage", None):
            usage = {"prompt": resp.usage.prompt_tokens, "completion": resp.usage.completion_tokens,
                     "total": resp.usage.total_tokens}
        return (resp.choices[0].message.content or ""), usage

    def _chat_chain(self) -> list[tuple[Any, str, str, str]]:
        """채팅 대체 순서: OpenAI(기본) → Gemini → Grok(xAI) → NVIDIA. 키 없는 단계는 뺀다."""
        chain = [(self.client, self.model, "_supports_json_mode", "OpenAI")]
        if self.gemini_llm_client is not None:
            chain.append((self.gemini_llm_client, self.gemini_llm_model,
                          "_gemini_supports_json_mode", f"Gemini({self.gemini_llm_model})"))
        if self.xai_llm_client is not None:
            chain.append((self.xai_llm_client, self.xai_llm_model,
                          "_xai_supports_json_mode", f"Grok({self.xai_llm_model})"))
        if self.nvidia_llm_client is not None:
            chain.append((self.nvidia_llm_client, self.nvidia_llm_model,
                          "_nvidia_supports_json_mode", f"NVIDIA({self.nvidia_llm_model})"))
        return chain

    def _chat(self, system: str, user: str) -> tuple[str, dict]:
        chain = self._chat_chain()
        last_exc: Exception | None = None
        for i, (client, model, flag, label) in enumerate(chain):
            try:
                return self._chat_once(client, model, flag, system, user)
            except Exception as exc:
                last_exc = exc
                if i + 1 < len(chain):
                    log.warning("%s 채팅 호출 실패, %s로 대체: %s", label, chain[i + 1][3], exc)
        assert last_exc is not None
        raise last_exc

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

    def translate_to_korean(self, title: str, excerpt: str) -> tuple[str, str]:
        """영어 기사의 제목·포스코퓨처엠 언급 발췌를 한국어로 번역한다(AI 1회 호출).

        반환: (한국어 제목, 한국어 발췌). 번역에 실패하면(또는 결과에 한글이 없으면) 해당 항목은 ''
        — 호출부는 원문을 그대로 둔다. 번역할 내용이 없으면 호출하지 않는다.
        """
        if not (title or "").strip() and not (excerpt or "").strip():
            return "", ""
        prompt = TRANSLATE_PROMPT.format(title=(title or "").strip()[:300], excerpt=(excerpt or "").strip()[:1500])
        for attempt in range(2):
            try:
                content, _ = self._chat(TRANSLATE_SYSTEM, prompt)
                data = _parse_json_object(content) or {}
                t_ko = _pp(data.get("title"))
                e_ko = re.sub(r"[ \t]+", " ", str(data.get("excerpt") or "")).strip()
                # 번역 결과에 한글이 있어야 한다(영어를 그대로 돌려주면 번역 실패로 본다)
                t_ko = t_ko if _has_hangul(t_ko) else ""
                e_ko = e_ko if _has_hangul(e_ko) else ""
                if t_ko or e_ko:
                    return t_ko, e_ko
                raise ValueError("한글 번역 결과 없음")
            except Exception as exc:
                if attempt == 0:
                    time.sleep(1)
                else:
                    log.warning("영어 기사 번역 실패(원문 그대로 둠): %s", exc)
        return "", ""

    def pfm_tone(self, excerpt: str) -> tuple[str, str]:
        """포스코퓨처엠 언급 발췌문만 보고 이 기사의 포스코퓨처엠 논조를 판정한다.

        반환: (긍정|중립|부정, 근거 한 줄). 실패하면 ("", "") — 호출부는 미판정으로 둔다.
        수집 때 1회만 부르고 결과를 DB 에 저장한다(언론사 탭은 저장값만 집계).
        """
        if not (excerpt or "").strip():
            return "", ""
        prompt = PFM_TONE_PROMPT.format(excerpt=excerpt.strip()[:1200])
        for attempt in range(2):
            try:
                content, _ = self._chat(PFM_TONE_SYSTEM, prompt)
                data = _parse_json_object(content) or {}
                tone = str(data.get("tone") or "").strip()
                if tone in PFM_TONES:
                    return tone, _pp(data.get("reason"))[:120]
                raise ValueError(f"알 수 없는 논조 값: {tone!r}")
            except Exception as exc:
                if attempt == 0:
                    time.sleep(1)
                else:
                    log.warning("포스코퓨처엠 논조 판정 실패(미판정으로 둠): %s", exc)
        return "", ""

    def sentiment_reason(self, title: str, text: str, sentiment: str) -> str:
        """이미 정해진 감성(긍정|중립|부정)의 근거 한 줄을 만든다. 실패하면 ''."""
        if not (text or "").strip() or sentiment not in PFM_TONES:
            return ""
        prompt = SENTI_REASON_PROMPT.format(title=(title or "")[:200], text=text.strip()[:1500],
                                            sentiment=sentiment)
        for attempt in range(2):
            try:
                content, _ = self._chat(SENTI_REASON_SYSTEM, prompt)
                data = _parse_json_object(content) or {}
                reason = _pp(data.get("reason"))[:120]
                if reason:
                    return reason
                raise ValueError("근거가 비어 있음")
            except Exception as exc:
                if attempt == 0:
                    time.sleep(1)
                else:
                    log.warning("감성 근거 생성 실패: %s", exc)
        return ""

    def press_overview(self, press: str, articles: list[dict], tone: dict) -> str:
        """한 언론사의 포스코퓨처엠 보도를 한 줄로 요약한다. 실패하면 ''."""
        if not articles:
            return ""
        lines = "\n".join(f"- {(a.get('title') or '')[:80]} | {a.get('tone') or '미판정'} | "
                          f"{(a.get('tone_reason') or '')[:70]}" for a in articles[:25])
        dist = " · ".join(f"{k} {tone.get(k, 0)}" for k in ("긍정", "중립", "부정", "미판정"))
        prompt = PRESS_OVERVIEW_PROMPT.format(press=press, dist=dist, lines=lines)
        for attempt in range(2):
            try:
                content, _ = self._chat(PRESS_OVERVIEW_SYSTEM, prompt)
                text = _pp((_parse_json_object(content) or {}).get("overview"))[:140]
                if text:
                    return text
                raise ValueError("빈 응답")
            except Exception as exc:
                if attempt == 0:
                    time.sleep(1)
                else:
                    log.warning("언론사 한줄 평가 생성 실패 (%s): %s", press, exc)
        return ""

    def chat_text(self, system: str, user: str) -> str:
        """일반 텍스트 응답(JSON 강제 없음). 텔레그램 챗봇 질의응답용."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        chain = self._chat_chain()
        last_exc: Exception | None = None
        for i, (client, model, _flag, label) in enumerate(chain):
            try:
                resp = client.chat.completions.create(model=model, messages=messages)
                return resp.choices[0].message.content or ""
            except Exception as exc:
                last_exc = exc
                if i + 1 < len(chain):
                    log.warning("%s 채팅 호출 실패, %s로 대체: %s", label, chain[i + 1][3], exc)
        assert last_exc is not None
        raise last_exc

    def weekly_brief(self, kind: str, name: str, articles: list[dict]) -> dict:
        """주간 레포트 섹션 1건을 합성한다.

        kind='swot'  → {"s","w","o","t"} 각 2~3문장 (그룹사 섹션)
        kind='impact'→ {"impact": 3~4문장,                       (정부/정책·글로벌 통상 섹션)
                        "pfm": {"tone","text"},                  주간 포스코퓨처엠 영향(긍정·중립·부정)
                        "items": [{"tone","text"} …기사 순서대로]}  기사별 포스코퓨처엠 영향
        기사가 없으면 빈 dict. 실패해도 레포트 전체가 죽지 않도록 예외를 삼킨다.
        """
        if not articles:
            return {}
        digest = "\n".join(
            f"[{i}] ({a.get('published_at','')[:10]}) {a.get('title','')}\n  {a.get('summary_text') or ''}"
            for i, a in enumerate(articles, 1))
        if kind == "swot":
            system = ("당신은 포스코 그룹 전략 담당 애널리스트다. 아래 한 주간 기사만 근거로 "
                      "해당 계열사 관점의 주간 SWOT 를 한국어로 작성하고 JSON 으로만 답한다.")
            user = (f"[대상] {name}\n[이번 주 주요 기사]\n{digest}\n\n"
                    "각 항목 2~3문장, 기사에서 실제로 읽어낼 수 있는 내용만. 근거가 없으면 "
                    '"이번 주 해당 신호 없음". 형식: '
                    '{"s":"...","w":"...","o":"...","t":"..."}')
        else:
            system = ("당신은 포스코 그룹 대외전략 담당이다. 아래 한 주간 기사만 근거로 "
                      "이 이슈들이 포스코 그룹(철강·이차전지소재·인프라)과 포스코퓨처엠에 미치는 영향을 "
                      "한국어로 정리하고 JSON 으로만 답한다.")
            n = len(articles)
            user = (f"[주제] {name}\n[이번 주 주요 기사]\n{digest}\n\n"
                    "포스코퓨처엠은 이차전지 소재(양극재·음극재·전구체)와 원료(리튬·니켈 등 핵심광물) 조달, "
                    "전기차·배터리 수요, 관세·수출통제·공급망·보조금 규제에 영향을 받는 회사다.\n"
                    "[작성 규칙]\n"
                    "- impact: 포스코 그룹 전체에 미치는 영향과 대응 관점 3~4문장. 단정하지 말고 검토 필요 톤.\n"
                    "- pfm_tone / pfm_impact: 이번 주 이 주제가 **포스코퓨처엠에** 미치는 영향 종합. "
                    "pfm_tone 은 \"긍정\"(매출·원가·규제 면에서 도움) | \"부정\"(비용 증가·규제·수출 제약 등 불리) | "
                    "\"중립\"(영향이 불분명하거나 직접 관련 없음) 중 하나. pfm_impact 는 2~3문장, 왜 그렇게 봤는지 기사 내용으로 설명.\n"
                    f"- items: 기사 {n}건 각각에 대해 [번호] 순서대로 n(번호)·tone·impact(1~2문장, 쉬운 말). "
                    "그 기사가 포스코퓨처엠에 미치는 영향만 쓴다.\n"
                    "- 기사에 없는 사실은 추측하지 않는다. 포스코퓨처엠과 직접 관련이 없으면 tone 은 \"중립\", "
                    'impact 는 "포스코퓨처엠에 직접 영향은 확인되지 않음"과 이유 한 줄로 쓴다.\n'
                    '형식: {"impact":"...","pfm_tone":"긍정|중립|부정","pfm_impact":"...",'
                    '"items":[{"n":1,"tone":"중립","impact":"..."}]}')
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
            log.warning("OpenAI 임베딩 생성 실패: %s", exc)
            if self.nvidia_embed_client is None:
                return None
            try:
                resp = self.nvidia_embed_client.embeddings.create(
                    model=self.nvidia_embed_model, input=text[:2000])
                log.info("NVIDIA 임베딩으로 대체했습니다 (model=%s)", self.nvidia_embed_model)
                return list(resp.data[0].embedding)
            except Exception as exc2:
                log.warning("NVIDIA 임베딩도 실패: %s", exc2)
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
        sentiment_reason=re.sub(r"\s+", " ", str(data.get("sentiment_reason") or "")).strip()[:120],
        swot=swot,
        token_usage=usage,
        ok=True,
    )


SENTIMENT_REASON_TAG = "[감성근거] "
TITLE_ORIGINAL_TAG = "[원제] "


def _perspective_field(row: dict, tag: str) -> str:
    """summaries.perspective_text 는 줄마다 '[머리말] 값' 으로 여러 값을 담는다. tag 줄의 값, 없으면 ''."""
    for line in (row.get("perspective_text") or "").splitlines():
        line = line.strip()
        if line.startswith(tag):
            return line[len(tag):].strip()
    return ""


def sentiment_reason_of(row: dict) -> str:
    """summaries.perspective_text 에 머리말과 함께 저장된 감성 판단 근거. 없으면 ''."""
    return _perspective_field(row, SENTIMENT_REASON_TAG)


def ensure_sentiment_reason(ctx: "Context", row: dict) -> str:
    """감성 근거가 없는 옛 기사에 근거를 만들어 저장하고 돌려준다. 만들 수 없으면 ''.

    근거는 본문(30일 보관) → 없으면 요약문으로 만든다. 저장은 perspective_text 의 [감성근거] 줄이라
    DB 칸을 더하지 않는다. 원제([원제]) 줄은 그대로 보존한다.
    """
    have = sentiment_reason_of(row)
    if have:
        return have
    senti = row.get("sentiment") or ""
    if senti not in PFM_TONES:
        return ""
    aid = row.get("id") or ""
    basis = (ctx.storage.body_of(aid) or "") if aid else ""
    basis = basis or (row.get("summary_text") or "")
    reason = ctx.llm.sentiment_reason(row.get("title") or "", basis, senti)
    if reason and aid:
        ctx.storage.set_perspective(aid, pack_perspective(
            reason, title_original_of(row), row.get("perspective_text") or ""))
    return reason


def title_original_of(row: dict) -> str:
    """영어 기사를 한글로 번역해 저장했을 때의 영어 원제. 번역하지 않았으면 ''."""
    return _perspective_field(row, TITLE_ORIGINAL_TAG)


def pack_perspective(reason: str = "", original_title: str = "", fallback: str = "") -> str:
    """감성 근거·원제를 perspective_text 한 칸에 줄 단위로 담는다(컬럼 추가 없이)."""
    lines = []
    if reason:
        lines.append(f"{SENTIMENT_REASON_TAG}{reason}")
    if original_title:
        one_line = re.sub(r"\s+", " ", original_title).strip()   # 줄바꿈이 있으면 머리말 구조가 깨진다
        lines.append(TITLE_ORIGINAL_TAG + one_line)
    return "\n".join(lines) if lines else fallback


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
    exclude_id: str = "",
) -> dict | None:
    """중복이면 기존 대표 기사 행을, 아니면 None 을 돌려준다.

    exclude_id: 이미 DB에 존재하는 자기 자신의 id(예: _drain_deferred 가 재검증하는
    deferred 기사 — url_canonical 이 아직 자기 자신의 url_source 그대로다). 없으면
    무시하지만, 있으면 1·2단계에서 '자기 자신'과의 매칭은 건너뛰고 다음 단계로
    넘어간다 — 안 그러면 캐노니컬이 자기 자신으로 남아 있는 한 1단계가 항상
    자기 자신을 찾아 반환해버려 정작 다른 진짜 중복 기사(2단계 본문 해시 등)까지는
    확인이 안 된다(2026-09-14, 병렬화 검증 중 발견 — 순차 실행에서도 있던 버그).
    """
    # 1단계 — 정규화 URL 완전 일치
    if url_canonical:
        hit = storage.find_by_canonical(url_canonical)
        if hit and hit["id"] != exclude_id:
            return hit

    # 2단계 — 본문 해시 일치
    if content_hash:
        hit = storage.find_by_content_hash(content_hash)
        if hit and hit["id"] != exclude_id:
            return hit

    window = timedelta(hours=DEDUP_WINDOW_HOURS)
    leftovers: list[tuple[dict, float]] = []

    # 3단계 — 제목 유사도 AND 발행 시각 차이. 두 조건의 AND 다.
    # 유사도만 보면 연재·기획 기사가 잘못 묶인다.
    for cand in candidates:
        if cand.get("id") == exclude_id:
            continue
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
# 분석 백로그 드레인의 LLM 호출 동시 실행 수 (2026-09-14). 항목끼리 서로
# 참조하지 않는 독립 호출이라 prefetch_articles 와 같은 이유로 병렬화한다 —
# 순차 처리하면 20건에 100초 넘게 걸려 PRD §6(1회 실행 20초 목표)를 크게 넘긴다.
LLM_ANALYZE_WORKERS = 5
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
    그래서 배터리·통상 **일반어**는 제목에서만 인정하고, 그룹사·포스코 언급과
    **회사명·명시적 조치명**은 스니펫도 함께 본다(고정밀 신호는 블러브에 우연히
    나오기 어렵다). 최종 관련성은 _drain_deferred 가 본문으로 다시 판정하므로
    여기서 조금 놓쳐도 복구된다."""
    probe = f"{title}\n{snippet or ''}"
    groups = normalize_group_list(detect_group_companies(probe))
    keep = bool(
        groups
        or POSCO_MENTION_RE.search(probe)
        or battery_company_hit(probe)                 # 회사명은 리드에 있어도 인정
        or _kw_hit_any(probe, TRADE_MEASURE_KW)       # 조치명도 마찬가지
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
    score_overrides, score_customs = get_score_rules(storage)
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
            "importance_score": SCORE_PEOPLE_NEWS if pk else score_article(
                item.title, "", groups, 3, score_overrides, score_customs),
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
    score_overrides, score_customs = get_score_rules(storage)
    done = 0
    # 중복 판정을 통과한 항목만 모아 뒀다가 LLM 분석만 병렬로 보낸다(아래 참고).
    to_analyze: list[tuple[str, dict, str]] = []
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
                                                    art.get("press_name") or "", html, http)
            storage.update_article(aid, {
                "url_canonical": canonical or art["url_canonical"],
                "press_id": press_id, "press_name": press_name,
                "categories": [PEOPLE_NEWS_CATEGORY], "importance_score": SCORE_PEOPLE_NEWS,
                "analyzed_at": iso(now_utc()),
            })
            summary, model, usage = people_summary(ctx, pk, art["title"], press_name, body,
                                                   use_llm=people_llm > 0, html=html)
            if model and model != PEOPLE_RULE_MODEL:
                people_llm -= 1
            storage.save_summary({
                "id": new_id(), "article_id": aid,
                "summary_text": summary, "perspective_text": "",
                "summary_source": "notice", "model": model, "token_usage": usage or None,
                "created_at": iso(now_utc()),
            })
            done += 1
            continue

        rule_groups = detect_group_companies(f"{art['title']}\n{group_lead_text(body)}")
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
                             content_hash, canonical or art["url_canonical"], dedup_candidates,
                             exclude_id=aid)
        if dup and dup["id"] != aid:
            storage.append_alias(dup["id"], art["url_source"])
            storage.update_article(aid, {"status": "archived"})
            continue

        press_name, press_id, press_tier = resolve_press(storage, canonical or target,
                                                         art.get("press_name") or "", html, http)
        storage.update_article(aid, {
            "url_canonical": canonical or art["url_canonical"],
            "press_id": press_id, "press_name": press_name,
            "author": extract_author(html, body, press_name),
            "content_hash": content_hash, "thumbnail_url": extract_thumbnail(html),
            "importance_score": score_article(art["title"], body, rule_groups, press_tier,
                                              score_overrides, score_customs),
        })
        row = {"id": aid, "title": art["title"], "press_id": press_id, "press_name": press_name,
               "importance_score": score_article(art["title"], body, rule_groups, press_tier,
                                                 score_overrides, score_customs),
               "group_companies": rule_groups}
        # 중복 판정(find_duplicate)까지는 반드시 여기서 순차로 끝낸다 — 같은 배치 안의
        # 두 기사가 서로 중복이면, 먼저 처리된 쪽이 저장한 content_hash·canonical URL을
        # DB에서 바로 조회해 뒤 항목이 중복으로 잡아낸다. 이 시점부터는 서로 독립적인
        # LLM 호출만 남으므로(2026-09-14) 병렬로 보낸다.
        # 신규 수집 경로처럼 본문을 저장해 둔다 — '본문' 상세 검색(30일 보관) 대상.
        storage.save_body(aid, body, "fulltext")
        to_analyze.append((aid, row, body))

    if to_analyze:
        def _analyze_one(item: tuple[str, dict, str]) -> tuple[str, bool]:
            aid, row, body = item
            try:
                ok = analyze_and_save(ctx, aid, row, body, "fulltext") is not None
            except Exception as exc:
                log.warning("드레인 분석 실패(개별 항목, 나머지는 계속): %s — %s", aid, exc)
                ok = False
            return aid, ok

        with ThreadPoolExecutor(max_workers=min(LLM_ANALYZE_WORKERS, len(to_analyze))) as pool:
            for aid, ok in pool.map(_analyze_one, to_analyze):
                if ok:
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
    return bool(model) and model != PEOPLE_RULE_MODEL   # 규칙 정리는 LLM 예산을 쓰지 않는다


SENTI_LAZY_PER_HOUR = get_env_int("SENTI_LAZY_PER_HOUR", 40, 0)
OVERVIEW_PER_HOUR = get_env_int("PRESS_OVERVIEW_PER_HOUR", 60, 0)   # 언론사 한줄 평가 AI 호출 시간당 상한
_senti_lazy_times: list[float] = []


def _senti_lazy_allow() -> bool:
    """화면에서 감성 근거를 즉석 생성하는 횟수를 시간당 SENTI_LAZY_PER_HOUR 로 제한한다(AI 비용 보호)."""
    now = time.monotonic()
    while _senti_lazy_times and now - _senti_lazy_times[0] > 3600:
        _senti_lazy_times.pop(0)
    if len(_senti_lazy_times) >= SENTI_LAZY_PER_HOUR:
        return False
    _senti_lazy_times.append(now)
    return True


@dataclass
class Context:
    cfg: Config
    storage: Storage
    http: HttpClient
    seen_cache: set[str] = field(default_factory=set)
    last_naver_fetch: float = 0.0
    naver_cycle: int = 0        # 키워드 교대 조회 회차 (select_naver_keywords)
    fetch_fail: dict[str, int] = field(default_factory=dict)   # 원문 접속 연속 실패 횟수(프로세스 메모리)
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
        # 쉼표는 넣지 않는다 — '중국, 리튬 배터리…' 같은 기사 제목에서 첫 쉼표 앞
        # 조각이 매체명으로 오인된다(홈페이지 title 전용 처리는 _homepage_site_name 참고).
        name = re.split(r"\s*[|\-–]\s*", name, maxsplit=1)[0].strip()
        # 끝에 붙은 '(English…)' 영문 병기 제거 (한글 병기 '(주간)' 등은 남긴다).
        name = re.sub(r"\s*\([A-Za-z0-9 .,'&/\-]+\)\s*$", "", name).strip()
    return name


def site_name_from_html(html: str, domain: str = "") -> str:
    """HTML 의 og:site_name / <meta name=publisher> 에서 매체명을 뽑는다.

    SEED_PRESS 에 없는 매체도 대부분 이 태그에 한글 매체명을 넣는다.
    영문 사이트명·도메인 형태는 신뢰하지 않는다(한글이 있어야 채택).
    <title> 은 여기서 보지 않는다 — 개별 기사 페이지의 <title>은 '차세대 배터리
    기술 한눈에'처럼 기사 제목 그 자체인 경우가 흔해서, 짧다는 것만으로는 매체명과
    구분이 안 된다. <title> 기반 추정은 홈페이지에서만 신뢰할 수 있다
    (_homepage_site_name — 홈페이지 title은 '기사 제목'이 될 수 없다).
    """
    for pat in (r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\'](?:twitter:site|publisher|source)["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(pat, html or "", re.I)
        if m:
            name = clean_site_name(m.group(1), domain)
            if name and _has_hangul(name) and not _looks_like_domain(name):
                return name
    return ""


def _title_media_name(title_raw: str) -> str:
    """<title> 에서 매체명 후보를 뽑는다.

    '매체명 - 슬로건'(스카이데일리) · '매체명, 슬로건, …'(한국무역신문) 은 첫 조각이
    매체명이지만, '슬로건 - 매체명'(조선비즈가 만드는 프리미엄 경제 주간지 - 이코노미조선)
    처럼 매체명이 맨 뒤에 오는 사이트도 있다. 첫 조각이 실패하면 마지막 조각도 본다.
    """
    raw = html_mod.unescape(title_raw).strip()
    parts = [p.strip() for p in re.split(r"\s*[|\-–,]\s*", raw) if p.strip()]
    for cand in ((parts[0], parts[-1]) if parts else ()):
        if cand and _has_hangul(cand) and not _looks_like_domain(cand) and len(cand) <= 20:
            return cand
    return ""


def _homepage_site_name(http: "HttpClient", domain: str, source_host: str = "") -> str:
    """새 언론사를 처음 만났을 때 1회, 기사 페이지에서 매체명을 못 찾으면
    홈페이지에서 다시 시도한다 — 홈페이지 title 은 거의 항상 매체명을 담고,
    (기사 제목과 달리) 짧아도 매체명으로 신뢰할 수 있다.

    domain_of() 가 news.tvchosun.com 같은 서브도메인을 등록 도메인(tvchosun.com)
    으로 접는데, 그 등록 도메인 자체는 리다이렉트 스텁만 있는 경우가 있다(실사례:
    tvchosun.com·dizzo.com 은 각각 55·77바이트짜리 빈 페이지만 준다). 기사가 실제로
    있던 호스트를 먼저 시도하고, 안 되면 등록 도메인 · www. 붙인 도메인 순으로 넘어간다.
    호스트마다 https 를 먼저, 안 되면 http 로도 시도한다(실사례: areyou.co.kr 은
    인증서가 호스트명과 안 맞아 https 전체가 SSLError 로 막혀 있었다).
    """
    candidates = [source_host, domain]
    if not domain.startswith("www."):
        candidates.append(f"www.{domain}")
    hosts: list[str] = []
    for h in candidates:
        if h and h not in hosts:
            hosts.append(h)

    for host in hosts:
        html = ""
        for scheme in ("https", "http"):
            try:
                resp = http.get(f"{scheme}://{host}/", timeout=6)
                resp.raise_for_status()
                html = decode_html(resp)
                break
            except Exception as exc:
                log.debug("홈페이지 매체명 조회 실패 %s://%s: %s", scheme, host, exc)
        if not html:
            continue
        name = site_name_from_html(html, domain)
        if name:
            return name
        m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        if m:
            name = _title_media_name(m.group(1))
            if name:
                return name
    return ""


# ── 언론사 조회 캐시 ────────────────────────────────────────────────
# 기사 1건마다 press_by_domain 을, 중복 1건마다 press_tier_by_id 를 부른다.
# Supabase 에서는 이게 그대로 HTTP 왕복이라 수집 1사이클에 수십~수백 번 나가는데,
# 언론사는 몇백 개뿐이라 같은 값을 계속 다시 받아오고 있었다.
#
# ⚠ 이름이 아직 도메인 그대로인 행(예: 'mdilbo.com')은 캐시하지 않는다 —
#   resolve_press 가 매번 다시 시도해서 정식 매체명으로 고쳐야 하기 때문이다.
#   (캐시하면 '언론사명이 도메인 그대로 굳어버리는' 예전 버그가 되살아난다.)
# ⚠ 캐시는 저장소 인스턴스마다 따로 둔다 — 셀프테스트가 임시 DB 를 계속 새로 만든다.
PRESS_MEMO_MAX = 5000


def _press_memo(storage: Storage) -> dict:
    memo = getattr(storage, "_press_memo_map", None)
    if memo is None:
        memo = {"by_domain": {}, "tier_by_id": {}}
        storage._press_memo_map = memo   # type: ignore[attr-defined]
    return memo


def press_row_cached(storage: Storage, domain: str) -> dict | None:
    """press_by_domain 의 캐시판. 이름이 확정된 행만 기억한다."""
    memo = _press_memo(storage)
    row = memo["by_domain"].get(domain)
    if row is not None:
        return row
    row = storage.press_by_domain(domain)
    if row and not _looks_like_domain(row.get("name", "")):
        if len(memo["by_domain"]) >= PRESS_MEMO_MAX:
            memo["by_domain"].clear()
        memo["by_domain"][domain] = row
        if row.get("id"):
            memo["tier_by_id"][row["id"]] = int(row.get("tier") or 3)
    return row


def press_memo_forget(storage: Storage, domain: str, press_id: str | None = None) -> None:
    """이름·tier 를 고쳐 쓴 직후 호출한다. 다음 조회가 DB 를 다시 읽는다."""
    memo = _press_memo(storage)
    dropped = memo["by_domain"].pop(domain, None)
    for pid in (press_id, (dropped or {}).get("id")):
        if pid:
            memo["tier_by_id"].pop(pid, None)


def press_tier_cached(storage: Storage, press_id: str | None) -> int:
    """press_tier_by_id 의 캐시판. tier 는 거의 바뀌지 않는다."""
    if not press_id:
        return 3
    memo = _press_memo(storage)["tier_by_id"]
    tier = memo.get(press_id)
    if tier is None:
        tier = storage.press_tier_by_id(press_id)
        if len(memo) >= PRESS_MEMO_MAX:
            memo.clear()
        memo[press_id] = tier
    return tier


def resolve_press(storage: Storage, url: str, hint: str, html: str = "",
                  http: "HttpClient | None" = None) -> tuple[str, str | None, int]:
    """도메인으로 언론사를 식별한다. 미등록이면 pending 으로 적재하고 수집을 막지 않는다. (F2.3)

    우선순위: SEED_PRESS > 본문 og:site_name/<title> > 홈페이지 재조회 > 피드 힌트 > 도메인 표기.
    """
    domain = press_domain_of(url)
    if not domain:
        return (hint if hint and not _looks_like_domain(hint) else ""), None, 3

    row = press_row_cached(storage, domain)
    seed = SEED_PRESS.get(domain)
    og_name = site_name_from_html(html, domain)
    # 이 매체를 처음 보는데(row is None) 기사 페이지에서 이름을 못 찾았으면, 홈페이지를
    # 한 번 더 본다. 기사 페이지는 SEO 상 기사 제목만 <title>에 넣는 경우가 많아
    # 실패하기 쉬운데, 홈페이지는 거의 항상 매체명을 담는다.
    # 기존에 등록된 매체라도 이름이 아직 도메인 그대로('mdilbo.com')면 계속 재시도
    # 한다 — 예전엔 row is None 일 때 딱 1회만 시도해서, 그 1회가 실패하면 화면
    # 필터 칩에 도메인이 영구히 그대로 노출됐다(2026-09-16 지적: 실제 다수 발견).
    if not og_name and http is not None and not seed and (
            row is None or _looks_like_domain(row.get("name", ""))) and (
            not hint or _looks_like_domain(hint)):
        og_name = _homepage_site_name(http, domain, urlsplit(url).hostname or "")

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
        press_memo_forget(storage, domain, row.get("id"))
        row = press_row_cached(storage, domain) or row
    elif not seed and og_name and _looks_like_domain(row.get("name", "")):
        # SEED 에 없고 도메인으로만 저장돼 있던 행을 og:site_name 으로 교체한다.
        storage.update_press_name(domain, og_name, int(row.get("tier") or 3))
        press_memo_forget(storage, domain, row.get("id"))
        row = press_row_cached(storage, domain) or row

    name = row.get("name") or ""
    if _looks_like_domain(name):
        name = seed[0] if seed else (og_name or prettify_domain(domain))
    if _looks_like_domain(name):
        _log_unmapped_press(domain)
    return name, row.get("id"), int(row.get("tier") or 3)


# 매핑표(SEED_PRESS)에 없어 도메인 그대로 저장되는 매체 — 같은 도메인은 프로세스당 한 번만 경고한다.
_UNMAPPED_PRESS_LOGGED: set[str] = set()


def press_domain_of(url: str) -> str:
    """언론사 식별용 도메인. domain_of 는 서브도메인을 등록 도메인으로 접는데, SEED_PRESS 에는
    'biz.chosun.com'(조선비즈)·'biz.sbs.co.kr'(SBS Biz) 처럼 서브도메인이 **다른 매체**인 항목이 있다.
    접어 버리면 그 항목은 영영 안 맞아 조선비즈 기사가 '조선일보'로 표시되던 문제(2026-10-01 점검)를 막으려고,
    서브도메인 키가 SEED_PRESS 에 있으면 그 호스트부터 먼저 맞춰 본다."""
    host = (urlsplit(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    for key in _SUBDOMAIN_PRESS_KEYS:
        if host == key or host.endswith("." + key):
            return key
    return domain_of(url)


# SEED_PRESS 중 서브도메인이 별개 매체인 키 — 긴 것(더 구체적인 것)부터 맞춘다.
_SUBDOMAIN_PRESS_KEYS = tuple(sorted(
    (d for d in SEED_PRESS if domain_of(f"http://{d}") != d), key=len, reverse=True))


def _log_unmapped_press(domain: str) -> None:
    if domain and domain not in _UNMAPPED_PRESS_LOGGED:
        _UNMAPPED_PRESS_LOGGED.add(domain)
        log.warning("언론사 매핑 없음 — 도메인 그대로 표시됨: %s (SEED_PRESS 에 추가 필요)", domain)


def press_display_name(name: str, url: str = "") -> str:
    """언론사명이 도메인 모양('mstoday.co.kr')이면 매핑표(SEED_PRESS)의 정식 이름으로 바꾼다.

    articles.press_name 은 복사본이라, 매핑을 추가해도 과거 기사는 `fixpress` 전까지
    도메인 그대로 남는다(2026-09-29 사용자 지적). 화면·집계는 이 함수를 거쳐 즉시 바로잡는다.
    """
    nm = (name or "").strip()
    # 서브도메인이 별개 매체인 경우(biz.chosun.com)는 저장된 이름이 모회사('조선일보')여도 바로잡는다.
    sub = press_domain_of(url) if url else ""
    if sub in _SUBDOMAIN_PRESS_KEYS and sub in SEED_PRESS:
        return SEED_PRESS[sub][0]
    if nm and not _looks_like_domain(nm):
        return nm
    for cand in (press_domain_of(url) if url else "", domain_of(f"http://{nm}") if nm else ""):
        if cand and cand in SEED_PRESS:
            return SEED_PRESS[cand][0]
    return nm


def run_once(ctx: Context, max_llm: int | None = None, force_naver: bool = False) -> dict:
    """수집 → 게이트 → 분석 → 저장 → 알림 큐 적재를 1회 수행한다.

    max_llm 을 주면 이번 실행의 LLM 호출을 그 수만큼으로 제한한다(검증용).
    force_naver 가 True면 네이버 호출 간격 제한을 무시한다(수동 1회 실행용).
    """
    started = time.monotonic()
    cfg, storage, http = ctx.cfg, ctx.storage, ctx.http
    http.reset()

    # 다중 인스턴스 오배포 안전장치 — 실행권을 못 얻으면 이번 회차는 건너뛴다.
    # 컬럼이 아직 없는 기존 DB(Supabase 수동 마이그레이션 전)에서도 파이프라인이
    # 멈추면 안 되므로, 락 자체가 실패하면(예외) '락 없이 진행'으로 폴백한다 —
    # 이건 필수 기능이 아니라 사고를 줄이는 안전장치일 뿐이다.
    try:
        stale_after = max(60, cfg.poll_interval_sec * PIPELINE_LOCK_STALE_MULT)
        if not storage.try_acquire_pipeline_lock(_INSTANCE_ID, stale_after):
            # 예전엔 여기서 '내' id 를 찍어 놓고 남의 것처럼 표시해 원인 추적이 어려웠다.
            # 실제 락 주인을 DB 에서 읽어 함께 남긴다(충돌 회차에만 1회 조회).
            owner = ""
            try:
                owner = str(storage.get_run_state().get("pipeline_lock_owner") or "")
            except Exception:
                pass
            log.warning(
                "다른 인스턴스(%s)가 파이프라인을 실행 중입니다 — 이번 회차는 건너뜁니다."
                " (내 id=%s) 기사 중복은 이 락이 막지만 네이버·LLM 같은 외부 API 의"
                " 일일 한도는 인스턴스끼리 공유되므로, 안 쓰는 쪽은 꼭 내려 주세요.",
                owner or "알 수 없음", _INSTANCE_ID)
            return {"fetched": 0, "new": 0, "skipped_locked": True}
    except Exception as exc:
        log.debug("파이프라인 락 확인 실패(무시하고 진행): %s", exc)

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

    # 마스터 패널에서 고친 중요도 규칙 — state 를 이미 읽었으니 여기서 같이 반영한다
    # (get_score_rules 캐시도 이걸로 갱신돼 이후 _defer_overflow/_drain_deferred 호출에서
    # 같은 회차 안엔 재조회하지 않는다).
    score_overrides = jload(state.get("score_overrides"), {}) or {}
    score_customs = jload(state.get("score_custom_rules"), []) or []
    _score_rules_cache.update(overrides=score_overrides, customs=score_customs, at=time.monotonic())

    feeds = storage.enabled_feeds()
    feed_types = {f["source_type"] for f in feeds}

    # ── 수집 ─────────────────────────────────────────────────────────
    raw: list[RawItem] = []
    if "google_rss" in feed_types:
        raw += collect_google_rss(http, keyword_rows)
        if daum_enabled():
            raw += collect_google_rss(http, daum_keyword_rows(keyword_rows))
    if "naver_api" in feed_types and cfg.naver_enabled:
        # 하루 25,000회 한도 때문에 매 실행이 아니라 일정 간격으로만 호출한다.
        due = force_naver or (time.monotonic() - ctx.last_naver_fetch) >= cfg.naver_interval_sec
        if due:
            # 일일 무료 한도(25,000회)를 넘지 않도록 이번 회차 몫만 고른다.
            picked = select_naver_keywords(keyword_rows, ctx.naver_cycle)
            ctx.naver_cycle += 1
            if len(picked) < len(keyword_rows):
                log.info("네이버 키워드 교대 조회: %d/%d개 (회차 %d, 일일 한도 대응)",
                         len(picked), len(keyword_rows), ctx.naver_cycle)
            raw += collect_naver(http, cfg, picked)
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
        if is_market_list_title(item.title):
            # 종가표·시세 목록('KOSPI 200 Closing Price List')은 구성 종목에 포스코퓨처엠이 끼어 있을 뿐
            # 기사가 아니다 — 언론사 탭에 잡히던 잡음(2026-10-01). 원문도 받지 않고 제외한다.
            storage.upsert_ledger(item.url_source, "off_topic")
            ctx.seen_cache.add(item.url_source)
            continue
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
    fresh.sort(key=lambda pair: score_article(pair[0].title, pair[0].snippet, [], 3,
                                              score_overrides, score_customs), reverse=True)
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
            # 원문 접속 실패. 예전엔 1회 실패로 180일 영구 제외해서, 일시 장애든 우리 서버 IP 를
            # 막은 사이트든 관련 기사가 통째로 사라졌다(2026-09-30 진단: 3일간 143건).
            # ① 제목·리드(스니펫)가 있으면 그것만으로 계속 진행한다 — 아래 G4 가 snippet 요약으로
            #    처리하고 관련성 판정도 똑같이 적용한다.
            # ② 그럴 수 없으면 연속 3회 실패해야 제외한다(그 전까지는 다음 회차에 다시 시도).
            # 인사·부고는 본문 명단이 곧 내용이라 제목만으로는 의미가 없다 — 재시도 쪽으로 보낸다.
            fallback = ("" if people_news_kind(item.url_original, item.title)
                        else fetch_fallback_url(item))
            if fallback:
                canonical, html = fallback, ""
                ctx.fetch_fail.pop(item.url_source, None)
                log.info("원문 접속 실패 → 제목·요약만으로 진행: %s", item.title[:44])
            else:
                strikes = ctx.fetch_fail.get(item.url_source, 0) + 1
                if strikes >= FETCH_FAIL_LIMIT:
                    storage.upsert_ledger(item.url_source, "extract_failed")
                    ctx.fetch_fail.pop(item.url_source, None)
                else:
                    ctx.fetch_fail[item.url_source] = strikes
                continue

        # ── G4: 본문 추출 (G3 응답을 재사용하므로 추가 요청 없음) ────
        body = extract_body(html)
        # 네이버가 '...' 로 자른 제목을 원문 제목으로 되돌린다 (이후 모든 단계가 이 제목을 쓴다)
        item.title = repair_truncated_title(item.title, html)
        summary_source = "fulltext" if len(body) >= 300 else "snippet"
        if summary_source == "snippet":
            body = item.snippet or item.title

        # 원문을 못 받은(html 없음) 기사는 언론사 홈페이지도 막혀 있을 가능성이 커서 조회하지 않는다
        # (막힌 사이트마다 수십 초씩 회차가 늘어지는 것을 막는다).
        press_name, press_id, press_tier = resolve_press(
            storage, canonical, item.press_hint, html, http if html else None)
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
            if press_tier < press_tier_cached(storage, existing.get("press_id")):
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
        rule_groups = detect_group_companies(f"{item.title}\n{group_lead_text(body)}")
        relevance_probe = f"{item.title}\n{item.snippet or ''}\n{body}"
        # 글로벌 통상환경 기사: 제목에 통상 조치명 + 포스코 관련 산업어가 함께 있으면
        # 포스코 미언급이어도 수집한다. (사용자 지정)
        # 조치명은 여전히 **제목**에서만 인정하되(is_trade_topic 내부), 산업어는 본문까지 본다 —
        # "산업장관, EU 산업가속화법 우려 전달" 처럼 조치명만 제목에 있고 철강·배터리는
        # 본문에서 설명되는 기사가 통째로 버려지던 문제를 막는다.
        is_trade = is_trade_topic(item.title, f"{item.snippet or ''}\n{body[:1500]}")
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
        score = score_article(item.title, body, rule_groups, press_tier, score_overrides, score_customs)

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

    # ── 분석 백로그 드레인 (병렬, 2026-09-14) ────────────────────────
    # 이전 실행에서 본문까지 저장되고 분석만 밀린 기사를 예산 안에서 처리한다.
    # 항목끼리 서로 참조하지 않는 독립적인 LLM 호출이라 병렬로 돌려도 안전하다
    # (dedup 판정이 껴 있는 _drain_deferred 와 달리 여긴 순서 의존이 없다).
    analyzed_backlog = 0
    if llm_budget > 0:
        pendings = storage.unanalyzed_with_body(llm_budget)

        def _analyze_pending(pending: dict) -> bool:
            row = {
                "id": pending["id"], "title": pending["title"],
                "press_id": pending.get("press_id"), "press_name": pending.get("press_name"),
                "importance_score": pending.get("importance_score") or 0,
                "group_companies": jload(pending.get("group_companies"), []),
            }
            try:
                return analyze_and_save(
                    ctx, pending["id"], row, pending["body"], pending["summary_source"]) is not None
            except Exception as exc:
                log.warning("백로그 분석 실패(개별 항목, 나머지는 계속): %s — %s",
                           pending.get("id"), exc)
                return False

        if pendings:
            with ThreadPoolExecutor(max_workers=min(LLM_ANALYZE_WORKERS, len(pendings))) as pool:
                analyzed_backlog = sum(1 for ok in pool.map(_analyze_pending, pendings) if ok)
            llm_budget -= len(pendings)
    if analyzed_backlog:
        log.info("분석 백로그 %d건 처리(병렬)", analyzed_backlog)

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
        f"{row['title']}\n{group_lead_text(body)}\n{analysis.summary_text}\n{kw_text}"))
    # 요약·키워드는 LLM 이 쓴 글이라 거기 나온 회사명만으로는 근거가 못 된다(스스로 만든 말로 스스로를
    # 검증하는 셈 — 언론사 탭에서 '포스코퓨처엠 언급이 전혀 없는 기사'가 잡힌 원인 후보, 2026-09-30).
    # LLM 이 더한 계열사는 제목·원문 어딘가에 실제 이름(별칭)이 있어야 인정한다.
    in_source = set(detect_group_companies(f"{row['title']}\n{body}"))
    llm_verified = [g for g in analysis.group_companies if g in mentioned and g in in_source]
    # LLM 이 group_companies 에 안 넣었어도 키워드에 계열사명이 있으면 채택한다(원문에도 있어야 한다).
    kw_groups = [g for g in detect_group_companies(kw_text) if g != "포스코" and g in in_source]
    groups = normalize_group_list(rule_groups + llm_verified + kw_groups)
    # 그룹사로 표기된 값은 키워드에서 제외한다 — 칩 중복의 근본 원인이다.
    keywords = dedupe_chips(analysis.keywords, exclude=groups)[:6]
    score_overrides, score_customs = get_score_rules(ctx.storage)
    score = score_article(row["title"], body, groups, 3 if not row.get("press_id") else 1,
                          score_overrides, score_customs)
    score = max(int(row.get("importance_score") or 0), score)

    # 카테고리는 제목 + 요약 + LLM 키워드로 다시 계산한다(수집 때는 스니펫만 봤다).
    # 키워드는 LLM 이 뽑은 핵심어 6개뿐이라, 본문 전체를 스캔할 때 같은 과태깅 없이
    # 요약에서 빠진 주제('인허가', '이차전지 소재' 등)를 잡아 준다.
    categories = detect_categories(row["title"], f"{analysis.summary_text}\n{' '.join(keywords)}")
    if is_policy_brief(row) and "정부/정책" not in categories:
        categories = ["정부/정책"] + categories
    # 통상환경은 detect_categories 가 '제목에 조치명' 조건으로 이미 판정한다 — 강제 추가 안 함.

    # 포스코퓨처엠 언급 발췌 — 카드 표시 + 논조 판정 입력. 언급이 없으면 빈 값(영역 미표시).
    excerpt = extract_pfm_excerpt(body)
    # 영어 기사는 제목과 발췌를 한글로 번역해 저장한다(요약·키워드·SWOT 근거는 이미 한국어로 만든다).
    # 번역이 실패하면 원문을 그대로 둔다. 영어 원제는 카드에 작게 보이도록 따로 남긴다.
    orig_title = ""
    en_title, en_excerpt = looks_english(row["title"]), looks_english(excerpt)
    translated_title = ""
    if en_title or en_excerpt:
        t_ko, e_ko = ctx.llm.translate_to_korean(row["title"] if en_title else "", excerpt if en_excerpt else "")
        if t_ko:
            translated_title, orig_title = t_ko, row["title"]
        if e_ko:
            excerpt = e_ko
    patch = {
        "sentiment": analysis.sentiment,
        "keywords": keywords,
        "group_companies": groups,
        "categories": categories,
        "importance_score": score,
        "analyzed_at": iso(now_utc()),
        "pfm_excerpt": excerpt,
    }
    if translated_title:
        patch["title"] = translated_title
    if excerpt:
        tone, reason = ctx.llm.pfm_tone(excerpt)
        if tone:
            patch.update(pfm_tone=tone, pfm_tone_reason=reason)
    ctx.storage.update_article(article_id, patch)
    ctx.storage.save_summary({
        "id": new_id(),
        "article_id": article_id,
        "summary_text": analysis.summary_text,
        # '포스코 관점'은 생성을 중단해 이 칸이 비어 있다 — 감성 판단 근거를 표시 머리말과 함께 담는다
        # (컬럼 추가 없이 쓰려는 것. 읽을 때는 sentiment_reason_of 가 머리말로 구분한다).
        "perspective_text": pack_perspective(analysis.sentiment_reason, orig_title, analysis.perspective),
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
    # 본문은 지우지 않고 30일 보관한다 — '본문' 상세 검색용(사용자 지정 2026-09-29).
    # 30일이 지나면 run_once 의 cleanup_bodies(30) 가 지운다(PRD §7 보존 기간 제안값).
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


# analyze_url() 의 본문 추출(Readability)은 '기사 문단'을 전제로 한다. 동영상 페이지는
# <article> 이 없고 플레이어·JSON 데이터뿐이라 본문이 사실상 비어 있게 되는데, 그때
# og:title 은 대개 성공해 "제목·본문 둘 다 없음" 체크를 통과해 버린다 — 에러 없이
# 텅 빈 카드가 만들어지는 게 실제 증상이었다(2026-09 유튜브 링크 문의).
_UNSUPPORTED_MEDIA_HOSTS = (
    "youtube.com", "youtu.be", "m.youtube.com",
    "vimeo.com", "twitch.tv", "tiktok.com",
)


def _unsupported_media_host(raw_url: str) -> str | None:
    """뉴스 기사가 아닌, 지원 대상 밖 동영상 호스트면 호스트명을 돌려준다."""
    host = (urlsplit(raw_url).hostname or "").lower()
    for bad in _UNSUPPORTED_MEDIA_HOSTS:
        if host == bad or host.endswith("." + bad):
            return host
    return None


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

    # 이 기능은 '기사 본문'을 Readability 로 뽑는 방식이라 영상 페이지는 원천적으로
    # 지원 대상이 아니다. 그런데 예전에는 여기서 걸러내지 않아, 유튜브 링크를 넣으면
    # 제목만 (og:title 로) 뽑히고 본문은 비다시피 한 채로 "성공"해 버렸다 — 에러도
    # 없이 텅 빈 요약 카드가 그대로 만들어지는 게 가장 혼란스러운 실패 형태였다.
    # URL 형식만 보고 미리 걸러 원인을 바로 알 수 있게 한다.
    unsupported_host = _unsupported_media_host(raw_url)
    if unsupported_host:
        return {"ok": False, "error": (
            f"이 기능은 뉴스 기사 링크 전용입니다. '{unsupported_host}' 은(는) "
            "동영상 페이지라 본문을 추출할 수 없습니다. 뉴스 기사 URL을 입력해 주세요."
        )}

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

    press_name, press_id, press_tier = resolve_press(storage, canonical, "", html, http)
    author = extract_author(html, body, press_name)
    published = extract_published(html) or now_utc()
    content_hash = sha256(body) if summary_source == "fulltext" else ""

    # ── G5: 중복 판정 ──────────────────────────────────────────────
    candidates = storage.recent_articles_for_dedup(published - timedelta(hours=DEDUP_WINDOW_HOURS + 1))
    dup = find_duplicate(storage, ctx.llm, title, published, content_hash, canonical, candidates)
    if dup:
        storage.append_alias(dup["id"], url_source)
        return _existing_result(dup["id"])

    groups = detect_group_companies(f"{title}\n{group_lead_text(body)}")
    # 카테고리는 제목 기준 임시값. 아래 analyze_and_save 에서 요약으로 다시 계산된다.
    categories = detect_categories(title)
    score_overrides, score_customs = get_score_rules(storage)
    score = score_article(title, body, groups, press_tier, score_overrides, score_customs)

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
    press = press_display_name(row.get("press_name") or "",
                               row.get("url_canonical") or row.get("url_original") or "")
    header = format_summary_header(press, row.get("author") or "")

    lines = [f"{emoji} [{esc(tag)}] {esc(row.get('title') or '')}", ""]
    summary = (row.get("summary_text") or "").strip()
    if summary:
        lines.append(f"{esc(header)} {esc(summary)}".strip())
    excerpt = excerpt_for_message(row.get("pfm_excerpt") or "")
    if excerpt:
        lines += ["", f"포스코퓨처엠 언급: {esc(excerpt)}"]
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


# ── 일반용 텔레그램 (모든 직원) — 부서용(승인자)과 별개의 봇·채널 (사용자 지정 2026-10-06) ─────────────
# · 포스코퓨처엠 기사 중 포스코퓨처엠 논조(언론사 탭의 긍정/중립/부정)가 '긍정' 또는 '중립'인 것만 보낸다.
#   부정·논조 미판정 기사는 절대 보내지 않는다(판정이 끝난 뒤에 대상이 된다).
# · 부서용과 같은 규칙 — 6시간 이내 기사만·'제외 키워드' 제외·/stop 일시중지·야간 억제·플러드 보류.
# · 메시지에는 중요도·SWOT·대응 필요성 같은 내부용 내용을 넣지 않는다.
# · 큐는 같은 notifications 표를 쓰되 channel='telegram_public' 으로 구분한다(부서용 발송과 섞이지 않는다).
# · 켜는 법: .env(AWS pfm-news-env)에 TELEGRAM_PUBLIC_BOT_TOKEN · TELEGRAM_PUBLIC_CHAT_ID 를 넣는다.
PUBLIC_CHANNEL = "telegram_public"
PUBLIC_TONES = ("긍정", "중립")
PUBLIC_SEND_PER_CYCLE = 6
_PUBLIC_SEEN: set[tuple[str, str]] = set()   # 이미 큐에 올린 (기사 id, 대화방) — 회차마다 DB 중복 삽입 시도를 막는다


def public_bot_token() -> str:
    return get_env("TELEGRAM_PUBLIC_BOT_TOKEN")


def public_chat_id() -> str:
    return get_env("TELEGRAM_PUBLIC_CHAT_ID")


def public_enabled() -> bool:
    """호출 시점에 환경변수를 읽는다 — 토큰과 채널 번호가 둘 다 있어야 켜진다."""
    return bool(public_bot_token() and public_chat_id())


# ── 텔레그램 바로가기 주소 ─────────────────────────────────────────────
# · 헤더 '✈ Telegram 채널' 버튼 = 일반용(모든 직원) 채널 — 누구나 볼 수 있다.
# · 부서용(승인자 전용) 채널 주소는 마스터 패널 '텔레그램 연동' 에서만 보인다 — 일반 사용자에게 내보내지 않는다.
_TG_LINK_CACHE: dict[str, str] = {}


def _tg_get(ctx: Context, token: str, method: str, **params: Any) -> dict:
    resp = ctx.http.get(f"https://api.telegram.org/bot{token}/{method}", params=params or None, timeout=8)
    return ((resp.json() or {}).get("result")) or {}


def dept_telegram_link(ctx: Context) -> tuple[str, str]:
    """부서용 채널 주소 (url, kind). TELEGRAM_CHANNEL_URL 이 있으면 그 값, 없으면 부서용 봇 대화방. 없으면 ('', '')."""
    if ctx.cfg.telegram_channel_url:
        return ctx.cfg.telegram_channel_url, "channel"
    if not ctx.cfg.telegram_enabled:
        return "", ""
    url = _TG_LINK_CACHE.get("dept")
    if not url:
        try:
            uname = _tg_get(ctx, ctx.cfg.telegram_bot_token, "getMe").get("username")
            if uname:
                url = _TG_LINK_CACHE["dept"] = f"https://t.me/{uname}"
        except Exception as exc:
            log.debug("부서용 봇 주소 조회 실패: %s", exc)
    return (url, "bot") if url else ("", "")


def public_telegram_link(ctx: Context) -> tuple[str, str]:
    """일반용 채널 주소 (url, kind). TELEGRAM_PUBLIC_CHANNEL_URL(초대 링크) 이 있으면 그 값.
    없으면 일반용 봇이 채널 정보를 알려 주는 대로(공개 채널이면 t.me/<채널이름>, 비공개면 초대 링크). 못 찾으면 ('', '').
    부서용 주소로는 절대 대체하지 않는다."""
    url = get_env("TELEGRAM_PUBLIC_CHANNEL_URL")
    if url:
        return url, "channel"
    if not public_enabled():
        return "", ""
    url = _TG_LINK_CACHE.get("public")
    if not url:
        try:
            chat = _tg_get(ctx, public_bot_token(), "getChat", chat_id=public_chat_id())
            if chat.get("username"):
                url = f"https://t.me/{chat['username']}"
            elif chat.get("invite_link"):
                url = chat["invite_link"]
            if url:
                _TG_LINK_CACHE["public"] = url
        except Exception as exc:
            log.debug("일반용 채널 주소 조회 실패: %s", exc)
    return (url, "channel") if url else ("", "")


# 일반용(모든 직원) 채널은 '포스코퓨처엠이 주제인 기사'만 — 시세표·순위표에 이름만 스친 기사는 뺀다 (사용자 지정 2026-10-07)
#   · 제목에 포스코퓨처엠(띄어 쓴 표기·영문 포함)이 있어야 한다.
#   · 시세·순위·주가 기사(브랜드평판·시황·특징주·목표주가 등)는 제목에 있으면 뺀다 — 투자 권유처럼 보이거나 회사 소식이 아니다.
PUBLIC_TITLE_EXCLUDE = (
    "브랜드평판", "빅데이터 분석", "코스피", "코스닥", "시황", "마감", "종가", "특징주", "목표주가", "투자의견",
    "리포트", "주가", "상한가", "하한가", "공매도", "순매수", "순매도", "급등", "급락",
)


def public_title_ok(title: str) -> bool:
    """일반용 채널에 올릴 제목인가 — 제목에 포스코퓨처엠이 있고, 시세·순위·주가 기사 표시가 없다."""
    t = (title or "").lower()
    if not any(a in t for a in _GROUP_ALIASES_LOWER["포스코퓨처엠"]):
        return False
    return not any(x.lower() in t for x in PUBLIC_TITLE_EXCLUDE)


def is_public_candidate(row: dict) -> bool:
    """일반용에 올릴 기사인가 — 포스코퓨처엠 논조가 긍정·중립이고, 제목이 포스코퓨처엠 기사(시세·순위 기사 제외)다."""
    return row.get("pfm_tone") in PUBLIC_TONES and public_title_ok(row.get("title") or "")


# 같은 사건을 여러 언론사가 다룬 기사(예: 삼성SDI 6조 LFP 계약 20건)가 일반 채널에 줄줄이 올라가면 직원들에게 스팸이다.
# 제목의 '핵심어'(회사 이름·'계약' 같은 흔한 말 제외)가 2개 이상 겹치면 같은 사건으로 보고, 먼저 나온 기사 1건만 올린다.
_EVENT_STOP = frozenset({
    "포스코퓨처엠", "포스코", "posco", "퓨처엠", "계약", "체결", "공급", "확대", "맞손", "협력", "장기", "규모", "소식",
    "속보", "단독", "종합", "그룹", "기업", "관련", "이상", "올해", "내년", "오늘", "발표", "추진", "나선다", "본격",
    # 사건이 아니라 '주제'를 가리키는 말 — 이것만 겹쳐서는 같은 사건이 아니다(SK온 LFP 계약 ≠ 삼성SDI LFP 계약)
    "lfp", "양극재", "음극재", "배터리", "이차전지", "2차전지", "전기차", "전고체", "소재", "공급계약", "투자", "증설",
})
_JOSA_RE = re.compile(r"(?:으로|에서|까지|부터|과|와|에|의|은|는|이|가|을|를|도|로|만)$")
PUBLIC_EVENT_SHARED = 2          # 이만큼 핵심어가 겹치면 같은 사건
PUBLIC_MAX_PER_HOUR = 5          # 일반 채널에 시간당 최대 발송 건수(넘치면 다음 시간에)
_PUBLIC_SENT_AT: list[float] = []


def event_tokens(title: str) -> set[str]:
    """제목에서 사건을 가리키는 핵심어만 뽑는다 — 조사를 떼고, 회사 이름·흔한 말은 뺀다."""
    out: set[str] = set()
    for t in re.findall(r"[A-Za-z0-9가-힣]{2,}", unicodedata.normalize("NFKC", (title or "").replace("兆", "조"))):
        t = t.lower()
        base = _JOSA_RE.sub("", t) if len(t) > 2 else t
        base = base if len(base) >= 2 else t
        if base in _EVENT_STOP or t in _EVENT_STOP:
            continue
        out.add(base)
    return out


def same_event(a: str, b: str) -> bool:
    """두 기사 제목이 같은 사건을 다루는가 — 핵심어가 PUBLIC_EVENT_SHARED 개 이상 겹치면 True('6조'≈'6조원' 처럼 앞부분이 같으면 같은 말)."""
    ta, tb = event_tokens(a), event_tokens(b)
    shared = 0
    for x in ta:
        if any(x == y or (min(len(x), len(y)) >= 2 and (x.startswith(y) or y.startswith(x))) for y in tb):
            shared += 1
    return shared >= PUBLIC_EVENT_SHARED


def queue_public_notifications(ctx: Context) -> int:
    """최근 N시간 포스코퓨처엠 기사 중 일반용 대상(긍정·중립)을 큐에 올린다. 반환: 새로 올린 건수.

    같은 기사는 (기사, 채널, 대화방) 유일 제약이 막아 두 번 올라가지 않는다."""
    if not public_enabled():
        return 0
    since = now_utc() - timedelta(hours=max(1, int(ctx.cfg.fresh_cutoff_hours or 6)))
    excl = [k for k in jload(ctx.storage.get_run_state().get("exclude_notify_keywords"), []) if k]
    queued = 0
    # 같은 사건 판정은 최근 24시간 후보와 비교한다(6시간 창 밖으로 밀려난 첫 기사 때문에 같은 사건이 다시 올라가지 않게).
    pool = [r for r in ctx.storage.pfm_articles(iso(now_utc() - timedelta(hours=24)), with_excerpt=True)
            if is_public_candidate(r) and parse_dt(r.get("published_at")) is not None]
    pool.sort(key=lambda r: parse_dt(r.get("published_at")))
    for r in pool:
        pub = parse_dt(r.get("published_at"))
        if pub < since:
            continue
        if _kw_hit_any(r.get("title") or "", excl):
            continue      # 제목에 '제외' 키워드 → 웹에만
        if any(o["id"] != r["id"] and parse_dt(o.get("published_at")) <= pub and same_event(o.get("title") or "", r.get("title") or "")
               for o in pool if o is not r and (parse_dt(o.get("published_at")), o["id"]) < (pub, r["id"])):
            continue      # 같은 사건을 먼저 다룬 기사가 있다 — 그 기사 1건만 올린다
        # 이미 큐에 올린 기사는 5분마다 DB 에 중복 삽입을 시도하지 않는다(Supabase 는 삽입 실패도 HTTP 1회다).
        key = (r["id"], public_chat_id())
        if key in _PUBLIC_SEEN:
            continue
        queue_notification_ok = ctx.storage.queue_notification(r["id"], public_chat_id(), "queued", 0,
                                                              channel=PUBLIC_CHANNEL)
        _PUBLIC_SEEN.add(key)            # 새로 올렸든 이미 있든 — 다시 시도할 필요가 없다
        if queue_notification_ok:
            queued += 1
    if len(_PUBLIC_SEEN) > 5000:         # 메모리 상한 — 넘으면 비운다(중복은 DB 제약이 어차피 막는다)
        _PUBLIC_SEEN.clear()
    return queued


def format_public_message(row: dict) -> str:
    """일반용 메시지 — 제목·언론사/기자·요약·포스코퓨처엠 언급·원문 링크만(내부용 정보 없음)."""
    press = press_display_name(row.get("press_name") or "",
                               row.get("url_canonical") or row.get("url_original") or "")
    header = format_summary_header(press, row.get("author") or "")
    lines = [f"📰 [포스코퓨처엠] {esc(row.get('title') or '')}", ""]
    summary = (row.get("summary_text") or "").strip()
    if summary:
        lines.append(f"{esc(header)} {esc(summary)}".strip())
    excerpt = excerpt_for_message(row.get("pfm_excerpt") or "")
    if excerpt:
        lines += ["", f"포스코퓨처엠 언급: {esc(excerpt)}"]
    link = row.get("url_canonical") or row.get("url_original") or ""
    if link:
        lines += ["", f'🔗 <a href="{esc_attr(link)}">원문 보기</a>']
    return "\n".join(lines)


def send_public_notifications(ctx: Context, limit: int = PUBLIC_SEND_PER_CYCLE) -> int:
    """일반용 큐 적재 + 발송. 부서용 발송과 같은 락을 써서 겹치지 않는다. 반환: 보낸 건수."""
    if not public_enabled():
        return 0
    with _SEND_LOCK:
        try:
            queue_public_notifications(ctx)
            return _send_public_notifications(ctx, limit)
        except Exception as exc:   # 일반용 실패가 부서용 파이프라인을 멈추면 안 된다
            log.warning("일반용 텔레그램 처리 실패: %s", exc)
            return 0


def _send_public_notifications(ctx: Context, limit: int) -> int:
    state = ctx.storage.get_run_state()
    if str(state.get("notify_paused") or "0") not in ("0", "False", "false", ""):
        return 0       # 봇 /stop 으로 일시중지됨 — 일반용도 함께 멈춘다
    if _flood_remaining() > 0:
        return 0
    pending = ctx.storage.pending_notifications(max(limit, 1) * 4, channel=PUBLIC_CHANNEL)
    if not pending:
        return 0
    # 큐에 오른 뒤 마스터가 '제외 키워드'를 추가했을 수 있다 — 발송 직전에 한 번 더 거른다.
    excl = [k for k in jload(state.get("exclude_notify_keywords"), []) if k]
    if excl:
        pending = [p for p in pending if not _kw_hit_any(p.get("title") or "", excl)]
    # 야간 억제 — 부서용과 같은 규칙(밤에는 중요도 n_min 이상만)
    n_start, n_end, n_min = effective_night(ctx, state)
    if _in_night_window(now_local().hour, n_start, n_end):
        pending = [p for p in pending if int(p.get("importance_score") or 0) >= n_min]
    if not pending:
        return 0
    url = TELEGRAM_API.format(token=public_bot_token())
    sent = 0
    nowm = time.monotonic()
    _PUBLIC_SENT_AT[:] = [t for t in _PUBLIC_SENT_AT if nowm - t < 3600]
    room = max(0, PUBLIC_MAX_PER_HOUR - len(_PUBLIC_SENT_AT))     # 이번 시간에 더 보낼 수 있는 건수
    for row in pending[:min(limit, room)]:
        if _flood_remaining() > 0:
            break
        # 발송 직전 재확인 — 보관 처리됐거나 논조가 바뀐 기사는 보내지 않는다.
        detail = ctx.storage.article_detail(row["article_id"]) or {}
        if (detail.get("status") != "active" or detail.get("pfm_tone") not in PUBLIC_TONES
                or not public_title_ok(detail.get("title") or "")):
            ctx.storage.mark_notification(row["id"], "skipped", "일반용 조건 불충족(논조·상태·제목)")
            continue
        _rate_gate()
        ok, err = _telegram_send(ctx, url, clamp_message(format_public_message(row)),
                                 chat_id=public_chat_id(),
                                 kind=f"일반용 · 포스코퓨처엠 논조 {detail.get('pfm_tone')}",
                                 article_id=row.get("article_id"))
        if ok:
            ctx.storage.mark_notification(row["id"], "sent", None)
            sent += 1
            _PUBLIC_SENT_AT.append(time.monotonic())
        elif _notify_too_old(row):
            ctx.storage.mark_notification(row["id"], "failed", err)
        elif _is_transient_tg_error(err):
            ctx.storage.touch_notification(row["id"], err)
        else:
            status = "queued" if int(row.get("retry_count") or 0) < 2 else "failed"
            ctx.storage.mark_notification(row["id"], status, err)
        time.sleep(RATE_LIMIT_SLEEP)
    return sent


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


def cmd_chatid(ctx: Context, public: bool = False) -> None:
    """봇이 받은 최근 메시지에서 chat_id 를 찾아 보여준다. public=True 면 일반용 봇(TELEGRAM_PUBLIC_BOT_TOKEN)."""
    token = public_bot_token() if public else ctx.cfg.telegram_bot_token
    if not token:
        raise SystemExit(("TELEGRAM_PUBLIC_BOT_TOKEN" if public else "TELEGRAM_BOT_TOKEN") + " 이 비어 있습니다.")
    url = f"https://api.telegram.org/bot{token}/getUpdates"
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
    name = "TELEGRAM_PUBLIC_CHAT_ID" if public else "TELEGRAM_CHAT_ID"
    print(f"아래 값을 .env 의 {name} 에 넣으세요.\n")
    for chat_id, desc in found.items():
        print(f"  {name}={chat_id}    ({desc})")


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


def cmd_public_test(ctx: Context) -> None:
    """일반용 텔레그램 설정이 맞는지 시험 메시지 1건을 일반용 채널로 보낸다."""
    if not public_enabled():
        raise SystemExit("TELEGRAM_PUBLIC_BOT_TOKEN 또는 TELEGRAM_PUBLIC_CHAT_ID 가 비어 있습니다.")
    url = TELEGRAM_API.format(token=public_bot_token())
    text = ("✅ <b>P-FM NEWS 일반용</b> 연결 확인\n\n"
            "이 채널에는 포스코퓨처엠 관련 긍정·중립 기사만 올라옵니다.\n"
            f"시각: {esc(iso(now_utc()))}")
    ok, err = _telegram_send(ctx, url, text, chat_id=public_chat_id(), kind="일반용 연결 테스트")
    if ok:
        log.info("일반용 시험 메시지 발송 성공. 일반용 채널에서 확인하세요.")
    else:
        raise SystemExit(f"발송 실패: {err}")


def cmd_public_check(ctx: Context) -> None:
    """일반용으로 올라갈 기사를 미리 본다(큐 적재·발송 없음). 최근 N시간 포스코퓨처엠 기사를 논조별로 센다."""
    since = now_utc() - timedelta(hours=max(1, int(ctx.cfg.fresh_cutoff_hours or 6)))
    rows = ctx.storage.pfm_articles(iso(since), with_excerpt=True)
    cnt = Counter((r.get("pfm_tone") or "미판정") for r in rows)
    cands = [r for r in rows if is_public_candidate(r)]
    log.info("일반용 점검 — 설정 %s · 최근 %d시간 포스코퓨처엠 기사 %d건 (긍정 %d · 중립 %d · 부정 %d · 미판정 %d)",
             "켜짐" if public_enabled() else "꺼짐(토큰/채널 번호 없음)", int(ctx.cfg.fresh_cutoff_hours or 6), len(rows),
             cnt["긍정"], cnt["중립"], cnt["부정"], cnt["미판정"])
    log.info("일반용 발송 대상(긍정·중립) %d건 — 부정·미판정은 보내지 않습니다", len(cands))
    for r in cands[:15]:
        log.info("  · [%s] %s | %s", r.get("pfm_tone"), (r.get("press_name") or "")[:10], (r.get("title") or "")[:50])


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
    hdr = format_summary_header(press_display_name(row.get("press_name") or "",
                                                   row.get("url_canonical") or row.get("url_original") or ""),
                                row.get("author") or "")
    summary = (row.get("summary_text") or "").strip()
    excerpt = excerpt_for_message(row.get("pfm_excerpt") or "")

    seg = f"{hdr} {summary}".strip() if (hdr and summary) else summary
    if seg and KAKAO_TEXT_MAX - len(body) > 14:
        body += "\n\n" + _clip(seg, KAKAO_TEXT_MAX - len(body) - 2)
    if excerpt and KAKAO_TEXT_MAX - len(body) > 20:
        body += "\n\n포스코퓨처엠 언급: " + _clip(excerpt, KAKAO_TEXT_MAX - len(body) - 13)

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
    return f'{mark} <a href="{esc_attr(card.get("url") or "")}">{esc(card.get("title") or "")}</a>  <i>{esc(when)}</i>'


def _card_full_text(card: dict, already: bool = False) -> str:
    head = "이미 등록된 기사입니다.\n\n" if already else ""
    score = int(card.get("importance_score") or 0)
    emoji = "🔴" if score >= 80 else "🟠"
    groups = card.get("group_companies") or []
    tag = groups[0] if groups else "포스코"
    lines = [f"{head}{emoji} [{esc(tag)}] {esc(card.get('title') or '')}", ""]
    if card.get("summary_header") or card.get("summary_text"):
        lines.append(f"{esc(card.get('summary_header') or '')} {esc(card.get('summary_text') or '')}".strip())
    if card.get("pfm_excerpt"):
        lines += ["", f"포스코퓨처엠 언급: {esc(excerpt_for_message(card['pfm_excerpt']))}"]
    sw = card.get("swot")
    if sw:
        lines += ["", f"SWOT 종합 {sw['total']} · 감성 {esc(card.get('sentiment') or '-')} · 중요도 {score}"]
    kws = card.get("keywords") or []
    if kws:
        lines.append("키워드: " + esc(", ".join(kws)))
    if card.get("url"):
        lines += ["", f'🔗 <a href="{esc_attr(card["url"])}">원문 보기</a>']
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


# ── 언론사 탭 — 포스코퓨처엠 보도 언론사별 집계 (사용자 지정 2026-09-29) ─────
# 주간동향과 같은 '발송(조회) 시점 기준 최근 N일' 롤링 구간을 쓴다.
PRESS_STATS_WINDOWS = (("week", 7), ("month", 30), ("year", 365))
PRESS_STATS_ARTICLES_MAX = 200   # 언론사 1곳당 펼쳐 보여줄 기사 수 상한
PRESS_STATS_TTL_SEC = 300        # 탭을 열 때마다 DB 를 치지 않게 5분 캐시
_AUTHOR_SPLIT_RE = re.compile(r"\s*(?:[,·/、&]|\s및\s)\s*|\s{2,}")
_AUTHOR_NOISE_RE = re.compile(r"\S+@\S+|\([^)]*\)|\[[^\]]*\]|^[가-힣A-Za-z]{2,10}\s*[=:]\s*")
_AUTHOR_TITLE_RE = re.compile(
    r"\s*(?:선임기자|수석기자|전문기자|인턴기자|수습기자|객원기자|기자|특파원|논설위원|편집위원"
    r"|인턴|수습|객원|선임|에디터|PD|앵커)$", re.I)


def split_authors(author: str) -> list[str]:
    return list(_split_authors_cached(author or ""))


@lru_cache(maxsize=30000)
def _split_authors_cached(author: str) -> tuple[str, ...]:
    """기자명 필드를 사람 이름 목록으로 정규화한다.

    '김철수 기자, 이영희 인턴기자' → ['김철수', '이영희'], '김철수 (kcs@x.com)' → ['김철수'].
    상세 검색 '기자' 칸과 언론사 탭 기자별 건수가 **같은 규칙**으로 이름을 비교·집계한다.
    """
    names: list[str] = []
    for part in _AUTHOR_SPLIT_RE.split(author or ""):
        part = _AUTHOR_NOISE_RE.sub("", part).strip()
        if is_non_person_byline(part):   # 'English News Desk' 같은 부서·데스크는 기자가 아니다
            continue
        nm = _AUTHOR_TITLE_RE.sub("", part).strip()
        if len(nm) < 2:      # '김기자'처럼 떼고 나면 이름이 안 남으면 원문 그대로 둔다
            nm = part
        # 소속이 앞에 붙은 바이라인('이데일리 김철수', '산업부 김철수', '뉴스1 김철수')은 소속을 떼고
        # 사람 이름만 남긴다. 직함('기자')은 위에서 이미 뗐으므로 중간 토큰도 직함·라벨이면 버린다.
        pieces = [t for t in nm.split() if not _is_affiliation_token(t)
                  and not _AUTHOR_TITLE_RE.fullmatch(t)]
        if not pieces:
            continue
        # '김철수 이영희' 처럼 한글 이름만 공백으로 나열된 공동 바이라인은 사람별로 나눈다.
        if len(pieces) > 1 and all(re.fullmatch(r"[가-힣]{2,4}", x) for x in pieces):
            cands = pieces
        else:
            cands = [" ".join(pieces)]
        for c in cands:
            if c and c not in names:
                names.append(c)
    return tuple(names)


def author_matches(author: str, query: str) -> bool:
    """상세 검색 '기자' 칸 — 이름이 **정확히** 같은 기자만 찾는다.

    예전엔 부분 일치라 '김민'을 치면 김민수·김민지가, 입력 도중 '김철'이 먼저 검색되면
    김철민이 섞여 나왔다(2026-09-29 사용자 지적). '기자'·직함·이메일은 떼고 비교한다.
    정규화하면 아무것도 안 남는 검색어(예: 이메일만 입력)일 때만 부분 일치로 찾는다.
    """
    want = [q.lower() for q in split_authors(query)]
    if not want:
        q = (query or "").strip().lower()
        if is_non_person_byline(q):
            # 부서·데스크 바이라인('온라인뉴스팀')은 기자 집계에서는 빠지지만, 검색은 이름 전체가
            # 같을 때만 찾는다('뉴스팀' 으로 '온라인뉴스팀' 이 걸리면 안 된다).
            return q in {p.strip().lower() for p in re.split(r"\s*[,·/、&]\s*", author or "")}
        return q in (author or "").lower()
    have = {n.lower() for n in split_authors(author)}
    return all(w in have for w in want)


def pfm_mention_supported(row: dict) -> bool:
    """'포스코퓨처엠' 태그 기사에 실제 언급 근거가 있는가.

    · 제목에 이름(별칭)이 있거나 · 본문 발췌문(pfm_excerpt)이 있으면 True
    · 발췌가 빈 값('')이면 분석 때 본문에서 언급을 못 찾았다는 뜻 → 근거 없음
    · 발췌가 NULL(아직 백필 전)이면 모른다 → 일단 True
    """
    excerpt = row.get("pfm_excerpt")
    if excerpt is None or str(excerpt).strip():
        return True
    title = (row.get("title") or "").lower()
    return any(a in title for a in _GROUP_ALIASES_LOWER["포스코퓨처엠"])


def aggregate_press_stats(rows: Iterable[dict], now: datetime | None = None) -> list[dict]:
    """포스코퓨처엠 태그 기사를 언론사별로 묶어 주간·월간·연간 건수, 논조, 기자별 건수를 센다.

    언론사명은 press_display_name 을 거친다 — 도메인으로 저장된 이름('mstoday.co.kr')도
    매핑표 이름('MS투데이')으로 합쳐 집계한다. 논조는 수집 때 저장한 값만 센다(재판정 없음).
    """
    now = now or now_utc()
    cuts = {k: now - timedelta(days=d) for k, d in PRESS_STATS_WINDOWS}
    by: dict[str, dict] = {}
    for r in rows:
        pub = parse_dt(r.get("published_at"))
        if pub is None or pub < cuts["year"]:
            continue
        if not pfm_mention_supported(r):
            continue   # 태그는 있는데 제목·본문 어디에도 포스코퓨처엠 언급이 없는 기사
        url = r.get("url_canonical") or r.get("url_original") or ""
        press = press_display_name(r.get("press_name") or "", url) or "(언론사 미상)"
        e = by.setdefault(press, {
            "press": press, "week": 0, "month": 0, "year": 0,
            "tone": {"긍정": 0, "중립": 0, "부정": 0, "미판정": 0},
            "_rep": Counter(), "_rep_tone": {}, "articles": [],
        })
        for key, _ in PRESS_STATS_WINDOWS:
            if pub >= cuts[key]:
                e[key] += 1
        tone = r.get("pfm_tone") if r.get("pfm_tone") in PFM_TONES else "미판정"
        e["tone"][tone] += 1
        authors = split_authors(r.get("author") or "")
        for nm in authors:
            e["_rep"][nm] += 1
            e["_rep_tone"].setdefault(nm, {"긍정": 0, "중립": 0, "부정": 0, "미판정": 0})[tone] += 1
        if len(e["articles"]) < PRESS_STATS_ARTICLES_MAX:
            e["articles"].append({
                "id": r.get("id"), "title": r.get("title") or "", "url": url,
                "published_at": r.get("published_at"), "author": r.get("author") or "",
                "authors": authors,
                "tone": r.get("pfm_tone") or "", "tone_reason": r.get("pfm_tone_reason") or "",
            })
    out = []
    for e in by.values():
        tones = e.pop("_rep_tone")
        e["reporters"] = [{"name": n, "count": c, "tone": tones[n], "color": reporter_tone(tones[n])}
                          for n, c in e.pop("_rep").most_common()]
        e["articles"].sort(key=lambda a: a.get("published_at") or "", reverse=True)
        e["color"] = press_tone_color(e["tone"])   # 언론사 이름 색(긍정=파랑·부정=빨강·그 외 검정, 판정 없음=회색)
        out.append(e)
    out.sort(key=lambda e: (-e["year"], -e["month"], -e["week"], e["press"]))
    for i, e in enumerate(out, 1):
        e["rank"] = i          # 언급 순위 — 연간 → 월간 → 주간 건수 순
    return out


def press_overview_fallback(entry: dict) -> str:
    """AI 를 못 쓸 때의 한 줄 — 논조 분포만으로 만든다(기사 내용은 읽지 않았다는 점을 문장에 밝힌다)."""
    t = entry.get("tone") or {}
    rated = sum(t.get(k, 0) for k in PFM_TONES)
    if not rated:
        return f"논조가 판정된 기사가 아직 없습니다(전체 {entry.get('year', 0)}건)."
    top = reporter_tone(t) or "중립"
    return (f"판정된 {rated}건 중 {top} 보도가 가장 많습니다"
            f"(긍정 {t.get('긍정', 0)} · 중립 {t.get('중립', 0)} · 부정 {t.get('부정', 0)}) — 논조 분포만 반영한 요약입니다.")


PRESS_COLOR_GAP = 0.6   # 언론사 이름 색 — 긍정·부정 차이가 (긍정+부정)의 60% 를 넘어야 파랑·빨강


def press_tone_color(tone: dict[str, int]) -> str:
    """언론사 이름 색을 정하는 논조 — '긍정' | '중립' | '부정' | ''(판정 기사 없음 → 회색).

    긍정과 부정의 차이가 (긍정+부정)의 60% 를 **넘을 때만** 많은 쪽 색(파랑·빨강)이다.
    예) 긍정 9·부정 1 → 차이 8 ÷ 합 10 = 80% → 파랑 / 긍정 3·부정 2 → 20% → 검정.
    긍정·부정이 하나도 없고 중립만 있으면 검정, 판정된 기사가 아예 없으면 회색('').
    (기자 이름 색은 '가장 많은 쪽' 규칙 reporter_tone 을 그대로 쓴다.)
    """
    pos, neg, neu = tone.get("긍정", 0), tone.get("부정", 0), tone.get("중립", 0)
    if pos + neg + neu <= 0:
        return ""
    if pos + neg > 0:
        gap = (pos - neg) / (pos + neg)
        if gap > PRESS_COLOR_GAP:
            return "긍정"
        if gap < -PRESS_COLOR_GAP:
            return "부정"
    return "중립"


def reporter_tone(tone: dict[str, int]) -> str:
    """기자의 대표 논조 — 긍정·중립·부정 중 가장 많은 쪽(동률이면 중립). 판정된 기사가 없으면 ''.

    화면 색: 긍정=파랑, 중립=검정, 부정=빨강.
    """
    top = max(tone.get(k, 0) for k in PFM_TONES)
    if top <= 0:
        return ""
    best = [k for k in PFM_TONES if tone.get(k, 0) == top]
    return best[0] if len(best) == 1 else "중립"


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
    return [r for r in _weekly_hits(rows, kind, key, tags)[:WEEKLY_ARTICLES_PER_SECTION]]


def _weekly_hits(rows: list[dict], kind: str, key: str,
                 tags: dict[str, tuple] | None = None) -> list[dict]:
    """섹션에 해당하는 기사 전부를 '제목에 회사명 → 중요도 → 최신' 순으로 정렬해 돌려준다."""
    aliases = _GROUP_ALIASES_LOWER.get(key, [key.lower()]) if kind == "group" else []
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
    return [r for _, r in hits]


def weekly_group_score(hits: list[dict]) -> dict:
    """그룹사 한 곳의 주간 점수(0~100)와 그 이유. 규칙 계산이라 AI 를 쓰지 않고, 이유에 계산 내역을 그대로 적는다.

    점수 = 최고 기사 중요도×0.5 + 상위 5건 평균 중요도×0.3 + min(기사 수, 10)×2
      · 가장 큰 사건 하나가 점수의 절반을 정하고, 꾸준한 보도량(최대 20점)이 나머지를 보탠다.
      · 기사 중요도는 카드의 '긍정 · 22' 와 같은 점수(제목·본문·언론사 규칙으로 계산)다.
    """
    n = len(hits)
    if not n:
        return {"value": 0, "count": 0, "reason": "이번 주 해당 기사가 없습니다."}
    scores = sorted((int(r.get("importance_score") or 0) for r in hits), reverse=True)
    top = scores[0]
    top5 = scores[:5]
    avg5 = sum(top5) / len(top5)
    vol = min(n, 10) * 2
    value = int(clamp(round(top * 0.5 + avg5 * 0.3 + vol), 0, 100))
    tone = {t: sum(1 for r in hits if (r.get("sentiment") or "중립") == t) for t in PFM_TONES}
    top_row = max(hits, key=lambda r: int(r.get("importance_score") or 0))
    reason = (f"기사 {n}건(긍정 {tone['긍정']} · 중립 {tone['중립']} · 부정 {tone['부정']}). "
              f"가장 높은 기사 {top}점 → {top * 0.5:.0f}점 + 상위 {len(top5)}건 평균 {avg5:.0f}점 → {avg5 * 0.3:.0f}점 "
              f"+ 보도량 {n}건 → {vol}점 = {value}점. 최고 기사: {(top_row.get('title') or '')[:40]}")
    if n and tone["부정"] / n >= 0.3:
        reason += f" · 부정 보도가 {tone['부정']}건({tone['부정'] * 100 // n}%)이라 대응 검토가 필요합니다."
    return {"value": value, "count": n, "tone": tone, "reason": reason}


def normalize_pfm_impact(brief: dict, n: int) -> tuple[dict | None, list[dict | None]]:
    """LLM 이 준 주간 포스코퓨처엠 영향을 안전한 형태로 다듬는다.

    반환: (섹션 종합 {"tone","text"} 또는 None, 기사별 [{"tone","text"} 또는 None] × n)
    · tone 이 긍정·중립·부정이 아니면 '중립' 으로 둔다(임의 값이 화면에 나가지 않게).
    · 본문이 빈 값이면 None — 근거 없는 칸을 만들어 내지 않는다.
    · 기사 번호(n)가 범위를 벗어나거나 중복이면 무시한다.
    """
    def _one(tone: Any, text: Any) -> dict | None:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not text:
            return None
        tone = str(tone or "").strip()
        return {"tone": tone if tone in PFM_TONES else "중립", "text": text[:400]}

    brief = brief or {}
    overall = _one(brief.get("pfm_tone"), brief.get("pfm_impact"))
    items: list[dict | None] = [None] * n
    for it in (brief.get("items") or []):
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("n")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n and items[idx] is None:
            items[idx] = _one(it.get("tone"), it.get("impact"))
    return overall, items


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
        hits = _weekly_hits(rows, kind, key, tags)
        picked = hits[:WEEKLY_ARTICLES_PER_SECTION]
        brief = ctx.llm.weekly_brief(
            "swot" if kind == "group" else "impact", label, picked) if picked else {}
        pfm_all, pfm_items = (normalize_pfm_impact(brief, len(picked))
                              if kind == "topic" else (None, [None] * len(picked)))
        views = [_weekly_article_view(r) for r in picked]
        for v, pi in zip(views, pfm_items):
            if pi:
                v["pfm"] = pi          # 기사별 포스코퓨처엠 영향(긍정·중립·부정)
        sections.append({
            "label": label,
            "kind": kind,
            "articles": views,
            "pfm": pfm_all,            # 주간 포스코퓨처엠 영향 종합(정부/정책·통상 섹션만)
            "swot": {k: brief.get(k, "") for k in ("s", "w", "o", "t")} if kind == "group" else None,
            "impact": brief.get("impact", "") if kind == "topic" else None,
            "score": weekly_group_score(hits) if kind == "group" else None,
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


# 포스코퓨처엠 영향 톤 색 — 긍정=파랑, 중립=회색, 부정=빨강 (카드·언론사 탭과 같은 약속)
_PFM_TONE_STYLE = {
    "긍정": ("#e8f0ff", "#1a56db"),
    "중립": ("#eef1f6", "#344054"),
    "부정": ("#fdecea", "#d92d20"),
}


def _pfm_tone_badge(tone: str) -> str:
    bg, fg = _PFM_TONE_STYLE.get(tone, _PFM_TONE_STYLE["중립"])
    return (f'<span style="display:inline-block;background:{bg};color:{fg};border-radius:10px;'
            f'padding:0 8px;font-size:11.5px;font-weight:700;margin-right:6px;">{esc(tone)}</span>')


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
        sc = sec.get("score")
        if sc and sc.get("count"):
            # 그룹사 이름 옆 주간 점수 + 계산 이유 (사용자 지정 2026-10-02)
            out[-1] = out[-1].replace(
                '</h2>',
                f'<span style="float:right;background:#eef2fb;color:#16337A;border-radius:14px;'
                f'padding:2px 12px;font-size:13px;font-weight:700;">주간 {sc["value"]}점</span></h2>')
            out.append(f'<p class="wr-score-why" style="margin:0 0 10px;color:#667085;font-size:12px;'
                       f'line-height:1.55;">{esc(sc.get("reason") or "")}</p>')
        if not arts:
            out.append('<p style="color:#98a2b3;font-size:13px;">이번 주 해당 기사가 없습니다.</p>')
            continue
        out.append('<ol style="margin:0 0 14px;padding-left:20px;font-size:14px;">')
        for a in arts:
            d = (a.get("published_at") or "")[:10]
            out.append(
                f'<li style="margin-bottom:8px;">'
                f'<a href="{esc_attr(a["url"])}" style="color:#16337A;text-decoration:none;font-weight:600;">'
                f'{esc(a["title"])}</a>'
                f'<br><span style="color:#98a2b3;font-size:12px;">{esc(a["press"])} · {esc(d)} · 중요도 {a["score"]}</span>'
                f'<br><span style="color:#475467;font-size:13px;">{esc(a["summary"])}</span>'
                + (f'<br><span class="wr-pfm-item" style="display:block;margin-top:4px;color:#2b3a55;font-size:12.5px;'
                   f'background:#f6f8fc;border-radius:6px;padding:5px 8px;">'
                   f'<b style="color:#16337A;">포스코퓨처엠 영향</b> {_pfm_tone_badge(a["pfm"]["tone"])}'
                   f'{esc(a["pfm"]["text"])}</span>' if a.get("pfm") else '')
                + '</li>')
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
        if sec.get("kind") == "topic" and sec.get("impact"):
            out.append(
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'style="margin:10px 0 6px;">'
                '<tr><td style="background:#16337A;padding:9px 16px;border-radius:8px 8px 0 0;">'
                '<span style="color:#fff;font-weight:700;font-size:13px;">'
                '🎯 이 이슈가 포스코 그룹에 미치는 영향</span></td></tr>'
                '<tr><td style="background:#eef2fb;padding:14px 16px;border-radius:0 0 8px 8px;'
                'color:#2b3a55;font-size:13.5px;line-height:1.75;">'
                f'{esc(sec["impact"])}</td></tr></table>')
        if sec.get("kind") == "topic" and sec.get("pfm"):
            pf = sec["pfm"]
            bg, fg = _PFM_TONE_STYLE.get(pf["tone"], _PFM_TONE_STYLE["중립"])
            out.append(
                '<table class="wr-pfm-week" role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'style="margin:10px 0 6px;">'
                f'<tr><td style="background:{fg};padding:9px 16px;border-radius:8px 8px 0 0;">'
                '<span style="color:#fff;font-weight:700;font-size:13px;">'
                f'🔋 이번 주 포스코퓨처엠에 미치는 영향 · {esc(pf["tone"])}</span></td></tr>'
                f'<tr><td style="background:{bg};padding:14px 16px;border-radius:0 0 8px 8px;'
                'color:#2b3a55;font-size:13.5px;line-height:1.75;">'
                f'{esc(pf["text"])}</td></tr></table>')
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


# ── 로그인 무차별 대입 방지 ───────────────────────────────────────────
# /api/web/login·/api/master/login 은 시도 횟수 제한이 없어서, 비밀번호가
# 짧으면(마스터는 8자 이상만 요구) 무제한 시도로 뚫릴 수 있었다. IP 별로
# 5분 안에 5회 실패하면 5분간 잠근다(서버 메모리, 재시작 시 초기화 —
# _MASTER_TOKENS 와 같은 성격이라 무거운 저장소를 새로 안 둔다).
_LOGIN_FAILS: dict[str, list[float]] = {}
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_SEC = 300.0


def _client_ip(request: Any) -> str:
    """리버스 프록시 뒤에 있을 수 있어 X-Forwarded-For 를 우선한다(첫 번째 값 —
    가장 왼쪽이 원 클라이언트). 없으면 소켓 주소로 돌아간다."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_locked(key: str) -> bool:
    now = time.time()
    q = [t for t in _LOGIN_FAILS.get(key, []) if now - t < LOGIN_WINDOW_SEC]
    _LOGIN_FAILS[key] = q
    return len(q) >= LOGIN_MAX_FAILS


def _login_fail(key: str) -> None:
    _LOGIN_FAILS.setdefault(key, []).append(time.time())


def _login_reset(key: str) -> None:
    _LOGIN_FAILS.pop(key, None)


# ── 마스터 비밀번호 복구 (2026-09-15) ────────────────────────────────
# 마스터 비밀번호는 바뀐 뒤엔 해시로만 저장되어 원문을 알 수 없다. 그래서
# "잘못 바꾼 뒤 잊어버림"에서 복구하는 유일한 방법은, DB에 저장된 변경
# 이력(해시)을 지워 .env 의 원래 값으로 되돌리는 것뿐이다 — 같은 IP에서
# 마스터 로그인이 연속 3회 이상 실패하면 자동으로 이렇게 되돌리고, 그
# 결과(.env 값)를 정해둔 이메일로 보낸다.
_MASTER_LOGIN_FAILS: dict[str, list[float]] = {}
MASTER_LOGIN_FAIL_THRESHOLD = 3


def _master_login_fail(key: str) -> int:
    now = time.time()
    q = [t for t in _MASTER_LOGIN_FAILS.get(key, []) if now - t < LOGIN_WINDOW_SEC]
    q.append(now)
    _MASTER_LOGIN_FAILS[key] = q
    return len(q)


def _master_login_reset(key: str) -> None:
    _MASTER_LOGIN_FAILS.pop(key, None)


def _recover_master_password(ctx: Context) -> None:
    """마스터 로그인 3회 이상 연속 실패 시 호출된다.

    DB에 저장된 master_pw_hash(변경 이력)를 지워 .env MASTER_PASSWORD 로
    되돌리고, 그 값을 복구 메일 수신자에게 보낸다. .env 값 자체가 바뀐
    적이 없다면(해시가 원래 없었다면) 이 함수는 사실상 아무 것도 바꾸지
    않고 안내 메일만 다시 보내는 셈이 된다.
    """
    try:
        ctx.storage.set_run_state({"master_pw_hash": ""})
    except Exception as exc:
        log.warning("마스터 비밀번호 복구(초기화) 실패: %s", exc)
        return
    to_list = ctx.cfg.master_pw_recovery_to
    pw = ctx.cfg.master_password
    if not to_list or not pw:
        log.warning("마스터 비밀번호 복구: 수신자 또는 .env 비밀번호가 없어 메일 생략")
        return
    if not ctx.cfg.smtp_configured:
        log.warning("마스터 비밀번호 복구: SMTP 미설정이라 메일 생략 (비밀번호는 초기화됨)")
        return
    html = (
        "<p>마스터 비밀번호를 3회 이상 잘못 입력해서, 안전을 위해 "
        ".env 에 저장된 기본 비밀번호로 되돌렸습니다.</p>"
        f"<p>새로 로그인할 마스터 비밀번호: <b>{esc(pw)}</b></p>"
        "<p>본인이 시도한 게 아니라면 즉시 서버 관리자에게 알려주세요.</p>"
    )
    ok, err = send_report_email(ctx.cfg, "[P-FM NEWS] 마스터 비밀번호 복구 안내", html, to_list)
    if not ok:
        log.warning("마스터 비밀번호 복구 메일 발송 실패: %s", err)


# ── 사이트 전체 잠금 (배포용) ────────────────────────────────────────
# 마스터 토큰은 관리 기능만 지킨다. 배포하면 URL 만 알아도 기사 목록·URL 등록·
# 텔레그램 직접 전송 API 를 누구나 쓸 수 있으므로, 그 앞에 세션 관문을 하나 둔다.
WEB_COOKIE = "pfm_web"
WEB_SESSION_DAYS = 30
# 잠금 대상에서 빼는 경로 — 로그인 자체, 헬스체크, 카카오 OAuth 착지점.
# /api/master/login 도 여기 넣어야 한다 — 안 그러면 '웹 접속 비밀번호를 잊어버려
# 사이트 전체가 잠긴' 상황에서 마스터 비밀번호를 알아도 복구 API 자체에 도달할
# 수 없어 영영 못 들어가는 사고가 난다(2026-09-15, 실제로 발생). 마스터 로그인은
# 시도 횟수 제한(_login_locked)과 비밀번호 자체 검증이 그대로 지켜주므로 공개해도
# 안전하다.
WEB_PUBLIC_PATHS = frozenset({"/api/web/login", "/api/web/logout", "/api/web/status",
                              "/api/master/login",
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
    """DB 에 변경된 해시가 있으면 그것을, 없으면 .env 의 MASTER_PASSWORD 를 쓴다.

    master_lock_enabled 를 명시적으로 꺼뒀으면(마스터 패널 "마스터 비밀번호 사용
    안 함") 아무 값이나(빈 값 포함) 통과시킨다 — 위험을 알고 켠 선택이므로
    존중한다(2026-09-16).
    """
    st = ctx.storage.get_run_state()
    flag = st.get("master_lock_enabled")
    if flag is not None and str(flag) in ("0", "False", "false"):
        return True
    if not pw:
        return False
    stored = st.get("master_pw_hash")
    if stored:
        return verify_password(pw, stored)
    env_pw = ctx.cfg.master_password
    # compare_digest 는 비-ASCII 문자열을 못 받는다(TypeError) — 한글 등을 쳐 넣으면
    # 그냥 '틀렸다'로 처리돼야지 서버 오류가 나면 안 되므로 바이트로 비교한다.
    return bool(env_pw) and hmac.compare_digest(pw.encode("utf-8"), env_pw.encode("utf-8"))


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
    else:
        # 저장된 그룹사가 있어도, 제목·요약에 협회 이름이 나오면 '배터리협회' 칩을 덧붙인다 —
        # 이 칩은 나중에 추가돼 과거 기사에는 저장값이 없다(2026-09-30).
        probe = f"{row.get('title') or ''} {row.get('summary_text') or ''}".lower()
        for extra in NON_POSCO_GROUPS:
            if extra not in groups and any(a in probe for a in _GROUP_ALIASES_LOWER[extra]):
                groups = groups + [extra]
    categories = dedupe_chips(cats, exclude=groups)
    press = press_display_name(row.get("press_name") or "",
                               row.get("url_canonical") or row.get("url_original") or "")
    return groups, categories, press


def build_card(row: dict) -> dict:
    """카드 1건의 표시용 형태를 만든다. 칩 중복 제거를 여기서 한 번 더 한다. (PRD F6.2b)"""
    groups, categories, press = card_tags(row)
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
        "press_name": press,
        "author": author_display(row.get("author") or ""),
        "summary_header": format_summary_header(press, author_display(row.get("author") or "")),
        "summary_text": row.get("summary_text") or "",
        "pfm_excerpt": row.get("pfm_excerpt") or "",
        "pfm_tone": row.get("pfm_tone") or "",
        "summary_source": row.get("summary_source") or "",
        "published_at": row.get("published_at"),
        "thumbnail_url": row.get("thumbnail_url") or "",
        "importance_score": int(row.get("importance_score") or 0),
        "sentiment": row.get("sentiment") or "",
        "sentiment_reason": sentiment_reason_of(row),
        "title_original": title_original_of(row),
        "keywords": keywords,
        "group_companies": groups,
        "categories": categories,
        "is_backfill": bool(row.get("is_backfill")),
        "manual": (row.get("source_type") == "manual"),   # 사용자가 URL 로 직접 등록한 기사
        "swot": swot,
    }


def field_search(tagged: list[dict], fields: dict[str, str],
                 body_ids: set[str] | None = None) -> list[dict]:
    """상세 검색 — 제목·본문·언론사·기자 칸 중 입력된 칸끼리 AND, 각 칸은 부분 일치.

    fields 키: title / body / press / author (빈 값은 조건 없음).
    '본문'은 30일 보관 본문(body_ids = 저장소에서 찾은 기사 id)에 더해, 본문이 이미
    지워진 오래된 기사도 찾을 수 있도록 요약문과 포스코퓨처엠 발췌문까지 본다.
    언론사는 화면에 보이는 이름(도메인 → 매핑 반영된 t["p"])으로 비교한다.
    """
    want = {k: (v or "").strip().lower() for k, v in fields.items() if (v or "").strip()}
    if not want:
        return tagged
    out = []
    for t in tagged:
        r = t["row"]
        if "title" in want and want["title"] not in (r.get("title") or "").lower():
            continue
        if "press" in want and want["press"] not in (t.get("p") or "").lower():
            continue
        if "author" in want and not author_matches(r.get("author") or "", want["author"]):
            continue
        if "body" in want:
            term = want["body"]
            hit = (r.get("id") in (body_ids or set())
                   or term in (r.get("summary_text") or "").lower()
                   or term in (r.get("pfm_excerpt") or "").lower())
            if not hit:
                continue
        out.append(t)
    return out


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
# 태그가 붙은 행을 메모리에 두고, '바뀐 것만' 델타로 갱신한다.
#
# ⚠ 프로세스 경계 주의 (serve + worker 분리 배포, 2026-09-11~):
#   이 스토어는 **프로세스마다 따로** 존재한다. worker 가 새 기사를 넣어도 serve 의
#   메모리에는 안 들어온다 — serve 는 _scan_tagged 가 요청 때마다 호출하는
#   refresh_scan_store 로 **자기 스토어를 스스로** 델타 갱신한다(그래서 동작한다).
#   새 전역 캐시를 추가할 땐 "누가 쓰고 누가 읽는가"를 반드시 따져라. 한쪽 프로세스만
#   쓰고 다른 쪽이 읽는 구조면 배포 후에 조용히 깨진다.
#
# ⚠ 정렬 불변식 (2026-09-15 실장애):
#   by_id 는 dict 라 **삽입 순서**가 곧 반환 순서인데, 델타로 새로 들어온 기사는
#   항상 맨 뒤에 붙는다. api_articles 의 'recent' 정렬은 이 반환 순서를 그대로
#   믿고 쓰므로, refresh_scan_store 는 **반환 직전에 반드시 발행일 최신순으로
#   재정렬**한다. 이걸 빼면 신규 기사가 마지막 페이지로 밀려 화면에서 사라진다.
#   (회귀 방지: selftest [11-2e] "델타로 들어온 최신 기사가 정렬 후 맨 앞에 온다")
_SCAN_STORE: dict[str, Any] = {"by_id": {}, "cursor": "", "full_at": 0.0, "delta_at": 0.0,
                               "sorted": None}
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
    """태그가 붙은 활성·분석완료 행 목록. 최초/하루 1회만 전체, 그 외엔 델타만 읽는다.

    반환은 항상 발행일 최신순으로 정렬해서 준다 — dict 삽입 순서에 기대면 안 된다.
    델타로 새로 들어온(기존에 없던 id) 기사는 파이썬 dict 특성상 삽입 순서상
    맨 뒤에 붙는데, api_articles 의 'recent' 정렬은 이 반환 순서를 그대로 믿고
    쓰기 때문에, 정렬을 안 하면 신규 기사가 항상 마지막 페이지로 밀려 화면에
    '최신 기사가 안 보이는' 것처럼 보인다(2026-09-15, 실사용 중 발견 — serve
    컨테이너가 델타로 계속 갱신은 하고 있었지만 순서가 틀어져 있었다).
    """
    now = time.monotonic()
    with _SCAN_STORE_LOCK:
        st = _SCAN_STORE
        changed = False
        if full or not st["by_id"] or (now - st["full_at"]) > SCAN_FULL_RELOAD_SEC:
            rows = storage.scan_articles(SCAN_STORE_CAP, None, "")
            st["by_id"] = {r["id"]: tag_row(r) for r in rows}
            st["cursor"] = max((_row_ts(r) for r in rows), default="")
            st["full_at"] = now
            st["delta_at"] = now
            changed = True
        elif st["cursor"] and now - st["delta_at"] >= SCAN_DELTA_MIN_SEC:
            st["delta_at"] = now
            fresh = storage.changed_articles_since(st["cursor"])
            for r in fresh:
                _store_put(st, r)
            if fresh:
                st["cursor"] = max([st["cursor"]] + [_row_ts(r) for r in fresh])
                changed = True
            if len(st["by_id"]) > SCAN_STORE_CAP * 1.15:
                keep = sorted(st["by_id"].values(),
                              key=lambda t: t["row"].get("published_at") or "", reverse=True)
                st["by_id"] = {t["row"]["id"]: t for t in keep[:SCAN_STORE_CAP]}
                changed = True
        # 정렬 결과를 들고 있다가 스토어가 바뀐 경우에만 다시 만든다. 델타는 60초에
        # 한 번뿐이라 대부분의 요청은 이 캐시를 그대로 쓴다(최대 4만 건 재정렬 회피).
        if changed or st["sorted"] is None:
            st["sorted"] = sorted(st["by_id"].values(),
                                  key=lambda t: t["row"].get("published_at") or "", reverse=True)
        return st["sorted"]   # 공유 리스트다 — 받는 쪽에서 절대 제자리 수정하지 말 것


def scan_store_upsert(storage: "Storage", article_id: str) -> None:
    """수동 등록처럼 즉시 반영이 필요한 한 건만 스토어에 넣거나 뺀다."""
    row = storage.article_detail(article_id)
    with _SCAN_STORE_LOCK:
        if row:
            _store_put(_SCAN_STORE, row)
        else:
            _SCAN_STORE["by_id"].pop(article_id, None)
        _SCAN_STORE["sorted"] = None   # by_id 를 직접 건드렸으니 정렬 캐시를 버린다


def create_app(ctx: Context):
    fastapi = _import("fastapi", "fastapi")
    # 이 파일은 from __future__ import annotations 를 쓰므로 타입 주석이 실행되지
    # 않고 문자열로 남는다. FastAPI 는 라우트 함수의 __globals__(이 모듈의 전역)
    # 에서 그 문자열을 typing.get_type_hints 로 다시 풀어야 하는데, 'fastapi' 는
    # 이 함수의 지역 변수라 전역에 없어서 'fastapi.Request' 타입 힌트를 못 풀고
    # 그냥 쿼리 파라미터로 오인한다(로그인 무차별 대입 방지에 Request 를 주입받다
    # 겪은 실패). 아래 한 줄로 전역에도 등록해 해결한다 — create_app 은 서버
    # 시작 시 한 번만 호출되므로 부작용이 없다.
    globals()["fastapi"] = fastapi
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
            flag = st.get("web_lock_enabled")
            if flag is not None and str(flag) in ("0", "False", "false"):
                # 마스터 패널에서 "웹 접속 비밀번호 사용 안 함"을 명시적으로 골랐다.
                # 저장된 비밀번호 값은 나중에 다시 켤 때 쓰려고 그대로 둔다(2026-09-16).
                _WEB_PW_CACHE.update(at=now, pw="")
                return ""
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
        # compare_digest 는 비-ASCII 문자열을 못 받는다 — 바이트로 비교해 한글
        # 비밀번호를 입력해도 서버 오류 없이 '틀렸다'로 처리되게 한다.
        return hmac.compare_digest(pw.encode("utf-8"), secret.encode("utf-8"))

    def _web_session_ok(token: str) -> bool:
        secret = _web_secret()
        return bool(token) and bool(secret) and hmac.compare_digest(
            token, web_session_token(secret))

    _WEB_LOGIN_SCRIPT = (
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
    )

    def _csp_hash(script_text: str) -> str:
        """CSP script-src 에 넣을 'sha256-...' 해시 토큰. 인라인 스크립트 원문 그대로 넣어야 한다."""
        import base64
        digest = hashlib.sha256(script_text.encode("utf-8")).digest()
        return "'sha256-" + base64.b64encode(digest).decode() + "'"

    # CSP 가 허용할 인라인 스크립트 해시 목록. [0]은 로그인 페이지(고정 문자열)용,
    # 그 뒤는 _index_html() 이 index.html/app.js 를 합칠 때마다 다시 채운다.
    _csp_script_hashes: list[str] = [_csp_hash(_WEB_LOGIN_SCRIPT)]

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
            "<div class='e' id='e'></div></form>"
            f"<script>{_WEB_LOGIN_SCRIPT}</script></body></html>"
        )

    @app.middleware("http")
    async def _security_headers(request, call_next):
        """모든 응답에 기본 보안 헤더를 붙인다 (배포 전 점검, 2026-09-11).

        지금까지 이런 헤더가 하나도 없었다 — 특히 clickjacking(다른 사이트가
        이 화면을 투명 iframe 으로 씌워 '텔레그램 전송'·'URL 등록' 같은 버튼을
        몰래 클릭시키는 공격)에 무방비였다. CSP 는 이 프런트가 쓰는 리소스만
        허용한다. app.js 는 캐싱 프록시 대응으로 index.html 에 인라인되므로
        (아래 _render_index) 'unsafe-inline' 대신 정확한 스크립트 해시만 허용해
        외부에서 주입된 스크립트는 여전히 막는다.
        """
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        script_src = "script-src 'self' " + " ".join(_csp_script_hashes)
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; {script_src}; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https: http:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return resp

    @app.middleware("http")
    async def _web_gate(request, call_next):
        """사이트 전체 잠금 — WEB_PASSWORD(또는 마스터 패널의 웹 비밀번호)가 있을 때만 동작.

        비밀번호가 비어 있으면(로컬 개발) 아무 것도 막지 않는다. 설정돼 있으면
        쿠키에 든 세션 토큰이 맞아야 화면·API 를 내준다 — 배포 후 URL 만 알면
        누구나 기사·발송 API 를 쓸 수 있는 상태를 막는 것이 목적이다.

        유효한 마스터 토큰(X-Master-Token)이 있으면 이 잠금을 통과시킨다 — 마스터
        권한이 웹 접속 잠금보다 상위이기 때문. 이게 없으면 '웹 접속 비밀번호를
        잊어 사이트가 잠긴' 상황에서 마스터 비밀번호를 알아도 마스터 패널
        API(/api/master/settings 등)에 도달할 수 없어 웹 잠금을 끄러 들어갈
        방법조차 없어진다(2026-09-16). 마스터 토큰은 자체 로그인·시도 횟수
        제한으로 보호되므로 안전하다.
        """
        if _web_password() and request.url.path not in WEB_PUBLIC_PATHS:
            if not _web_session_ok(request.cookies.get(WEB_COOKIE, "")) and not _valid_master_token(
                    request.headers.get("x-master-token", "")):
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

    # '본문' 상세 검색 결과(기사 id 집합)를 잠깐 캐시한다 — 입력 중 매 타자마다 DB 를 치지 않게.
    _body_cache: dict[str, dict] = {}

    def _body_ids(term: str) -> set[str]:
        now = time.monotonic()
        ent = _body_cache.get(term)
        if ent and now - ent["at"] < SCAN_TTL_SEC:
            return ent["ids"]
        ids = ctx.storage.body_match_ids(term)
        _body_cache[term] = {"at": now, "ids": ids}
        if len(_body_cache) > 24:
            for k in sorted(_body_cache, key=lambda k: _body_cache[k]["at"])[:12]:
                _body_cache.pop(k, None)
        return ids

    @app.get("/api/articles")
    def api_articles(group: str = "", cat: str = "", press: str = "",
                     period: str = "all", q: str = "", page: int = 1, size: int = 20,
                     sort: str = "recent", s_title: str = "", s_body: str = "",
                     s_press: str = "", s_author: str = ""):
        page = max(1, page)
        size = int(clamp(size, 1, 100))
        tagged = _scan_tagged(period, q)
        # 상세 검색(제목·본문·언론사·기자) — 통합 검색 q 와도 AND 로 겹쳐진다.
        fields = {"title": s_title, "body": s_body, "press": s_press, "author": s_author}
        if any((v or "").strip() for v in fields.values()):
            body_term = (s_body or "").strip()
            tagged = field_search(tagged, fields, _body_ids(body_term) if body_term else None)
        # 같은 그룹 안 OR, 다른 그룹 사이 AND. (PRD F6.1a — filter_tagged 가 유일한 구현)
        matched = filter_tagged(tagged,
                                {normalize_chip(x) for x in _split_multi(group)},
                                {normalize_chip(x) for x in _split_multi(cat)},
                                {normalize_chip(x) for x in _split_multi(press)})
        # 정렬 — 기본(recent)은 refresh_scan_store 가 발행일 최신순으로 정렬해 준
        # 순서를 그대로 쓴다(그 함수의 '정렬 불변식' 주석 참고. 예전엔 dict 삽입
        # 순서에 기대다가 신규 기사가 마지막 페이지로 밀리는 장애가 있었다).
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
            # 포스코 계열이 아닌 '배터리협회'는 기사가 아직 없어도 칩이 사라지지 않게 항상 맨 끝에 둔다.
            "groups": [g for g in _ordered(groups, GROUP_ORDER) if g not in NON_POSCO_GROUPS]
                      + sorted(NON_POSCO_GROUPS),
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
    async def api_share_telegram(article_id: str, x_master_token: str = fastapi.Header(default="")):
        """카드의 전송 버튼 — 이 기사 요약을 설정된 텔레그램 채팅으로 보낸다."""
        if (err := _master_guard(x_master_token)):
            return err
        if not ctx.cfg.telegram_enabled:
            return JSONResponse({"ok": False, "error": "텔레그램이 설정되지 않았습니다."},
                                status_code=400)
        row = ctx.storage.article_detail(article_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "기사를 찾을 수 없습니다."}, status_code=404)
        import anyio
        api_url = TELEGRAM_API.format(token=ctx.cfg.telegram_bot_token)

        def _work():
            # 카드의 ↗ 버튼도 알림 큐와 '같은 채널'로 나가므로 분당 한도를 함께 지킨다.
            # (이 경로만 _rate_gate 를 안 거쳐서, 큐 발송이 몰릴 때 겹치면 429 를 맞았다)
            _rate_gate()
            return _telegram_send(ctx, api_url, clamp_message(format_message(row)),
                                  kind="직접 전송", article_id=article_id)

        ok, err = await anyio.to_thread.run_sync(_work)
        if ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "error": err or "발송에 실패했습니다."}, status_code=502)

    @app.get("/api/telegram-link")
    def api_telegram_link():
        """헤더 Telegram 버튼용 주소 — **일반용(모든 직원) 채널**. 부서용 주소는 여기서 내보내지 않는다."""
        url, kind = public_telegram_link(ctx)
        if not url:
            return JSONResponse({"ok": False, "error": "일반용 텔레그램 채널이 설정되지 않았습니다."})
        return JSONResponse({"ok": True, "url": url, "kind": kind})

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
    async def api_web_login(payload: dict, request: fastapi.Request,
                            x_forwarded_proto: str = fastapi.Header(default="")):
        """접속 비밀번호 확인 후 세션 쿠키를 심는다. 로그인 페이지가 fetch 로 호출한다."""
        secret = _web_secret()
        if not secret:
            return JSONResponse({"ok": True, "locked": False})   # 잠금이 꺼져 있음
        ip = _client_ip(request)
        if _login_locked(ip):
            return JSONResponse(
                {"ok": False, "error": "너무 많이 실패했습니다. 5분 후 다시 시도하세요."},
                status_code=429)
        pw = (payload or {}).get("password", "")
        if not _web_verify(pw):
            _login_fail(ip)
            return JSONResponse({"ok": False, "error": "비밀번호가 올바르지 않습니다."},
                                status_code=401)
        _login_reset(ip)
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
    async def api_master_login(payload: dict, request: fastapi.Request):
        ip = _client_ip(request)
        if _login_locked(ip):
            return JSONResponse(
                {"ok": False, "error": "너무 많이 실패했습니다. 5분 후 다시 시도하세요."},
                status_code=429)
        pw = (payload or {}).get("password", "")
        if not isinstance(pw, str) or not check_master_password(ctx, pw):
            _login_fail(ip)
            if _master_login_fail(ip) >= MASTER_LOGIN_FAIL_THRESHOLD:
                _recover_master_password(ctx)
                _master_login_reset(ip)
            return JSONResponse({"ok": False, "error": "비밀번호가 올바르지 않습니다."},
                                status_code=401)
        _login_reset(ip)
        _master_login_reset(ip)
        return JSONResponse({"ok": True, "token": _issue_master_token(),
                             "ttl_hours": int(MASTER_TOKEN_TTL.total_seconds() // 3600)})

    @app.get("/api/master/settings")
    async def api_master_settings_get(x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        st = ctx.storage.get_run_state()
        n_start, n_end, n_min = effective_night(ctx, st)
        _overrides = jload(st.get("score_overrides"), {}) or {}
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
            # 잠금 사용 여부 — 마스터 패널 토글용. NULL(미지정)이면 '비밀번호 값이
            # 있으면 켜짐'으로 해석해 보여준다(레거시 호환). (2026-09-16)
            "web_lock_enabled": bool(_web_password()),
            "master_lock_enabled": str(st.get("master_lock_enabled", 1) or 0)
                                   not in ("0", "False", "false"),
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
            # 중요도 점수 산정 규칙 — 마스터 패널에서 그대로 편집한다(단일 출처).
            # items: 기본 항목(코드 로직에 묶여 있어 점수 수정·사용안함만 가능).
            # custom_items: 사용자가 추가한 키워드 기반 항목(추가·수정·완전 삭제 가능).
            "score_rules": {
                "night": {"start": n_start, "end": n_end, "min_score": n_min,
                          "tz": f"UTC{ctx.cfg.tz_offset_hours:+d}"},
                "items": [
                    {"key": key, "label": label,
                     "points": int(_overrides.get(key, {}).get("points", default))
                               if isinstance(_overrides.get(key), dict) else default,
                     "enabled": _overrides.get(key, {}).get("enabled", True)
                                if isinstance(_overrides.get(key), dict) else True}
                    for key, (default, label) in SCORE_RULE_DEFS.items()
                ],
                "custom_items": jload(st.get("score_custom_rules"), []) or [],
                "custom_max": SCORE_CUSTOM_MAX,
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
        if "web_lock_enabled" in (payload or {}):
            # 마스터 비밀번호는 여기서 안 받는다 — 껐다 켜도 마스터 패널
            # 자체는 항상 마스터 비밀번호로 보호되므로 별도 확인 없이 바꿀 수
            # 있다(위험도가 낮다: 사이트 화면·API 노출 여부만 바뀜).
            patch["web_lock_enabled"] = 1 if payload["web_lock_enabled"] else 0
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
        # 중요도 기본 항목 — 점수 수정·사용안함 토글만 가능(판정 로직은 코드에 있다).
        if "score_items" in (payload or {}):
            raw = payload["score_items"]
            if not isinstance(raw, list):
                return JSONResponse({"ok": False, "error": "중요도 항목은 목록이어야 합니다."},
                                    status_code=400)
            overrides = jload(ctx.storage.get_run_state().get("score_overrides"), {}) or {}
            for it in raw:
                key = str((it or {}).get("key") or "")
                if key not in SCORE_RULE_DEFS:
                    return JSONResponse({"ok": False, "error": f"알 수 없는 중요도 항목: {key}"},
                                        status_code=400)
                try:
                    points = int(clamp(int(it.get("points")), -100, 100))
                except (TypeError, ValueError):
                    return JSONResponse({"ok": False, "error": f"{key} 점수는 -100~100 숫자여야 합니다."},
                                        status_code=400)
                overrides[key] = {"points": points, "enabled": bool(it.get("enabled", True))}
            patch["score_overrides"] = jdump(overrides)
        # 중요도 사용자 추가 항목 — 목록 전체를 통째로 교체한다(추가·수정·삭제 모두 이 형태).
        if "score_custom_items" in (payload or {}):
            raw = payload["score_custom_items"]
            if not isinstance(raw, list):
                return JSONResponse({"ok": False, "error": "추가 항목은 목록이어야 합니다."},
                                    status_code=400)
            if len(raw) > SCORE_CUSTOM_MAX:
                return JSONResponse(
                    {"ok": False, "error": f"추가 항목은 최대 {SCORE_CUSTOM_MAX}개까지입니다."},
                    status_code=400)
            customs: list[dict] = []
            for it in raw:
                label = str((it or {}).get("label") or "").strip()[:60]
                kws = dedupe_chips([str(k).strip() for k in (it.get("keywords") or []) if str(k).strip()])[:10]
                if not label or not kws:
                    return JSONResponse(
                        {"ok": False, "error": "추가 항목은 이름과 키워드가 최소 1개 필요합니다."},
                        status_code=400)
                scope = it.get("scope") if it.get("scope") in ("title", "title_or_body") else "title_or_body"
                try:
                    points = int(clamp(int(it.get("points")), -100, 100))
                except (TypeError, ValueError):
                    return JSONResponse({"ok": False, "error": f"'{label}' 점수는 -100~100 숫자여야 합니다."},
                                        status_code=400)
                customs.append({"id": str(it.get("id") or new_id()), "label": label,
                                "keywords": kws, "scope": scope, "points": points})
            patch["score_custom_rules"] = jdump(customs)
        if patch:
            try:
                ctx.storage.set_run_state(patch)
                if "score_overrides" in patch or "score_custom_rules" in patch:
                    _score_rules_cache["at"] = 0.0   # 다음 조회에서 바로 새 값을 반영
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
                         "  add column if not exists trade_exclude_keywords text default '[]',\n"
                         "  add column if not exists score_overrides text default '{}',\n"
                         "  add column if not exists score_custom_rules text default '[]',\n"
                         "  add column if not exists web_lock_enabled boolean,\n"
                         "  add column if not exists master_lock_enabled boolean;"},
                        status_code=500)
                raise
        if "web_lock_enabled" in patch:
            _WEB_PW_CACHE.update(at=0.0, pw="")   # 다음 요청부터 바로 새 값 반영
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
        if not isinstance(new_pw, str) or len(new_pw) < 8:
            return JSONResponse({"ok": False, "error": "새 비밀번호는 8자 이상이어야 합니다."},
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

    @app.post("/api/master/lock")
    async def api_master_lock(payload: dict, x_master_token: str = fastapi.Header(default="")):
        """마스터 비밀번호 자체를 쓸지 말지 켜고 끈다 (2026-09-16).

        끄면 마스터 패널이 비밀번호 없이 열린다 — 모든 마스터 전용 API(텔레그램
        발송·키워드 관리 등)의 유일한 보호막이 사라지는 것이므로, 현재 마스터
        비밀번호를 다시 입력해 확인해야만 끌 수 있다(잠금 켜는 건 확인 없이 가능).
        """
        if (err := _master_guard(x_master_token)):
            return err
        enabled = bool((payload or {}).get("enabled", True))
        if not enabled:
            cur = (payload or {}).get("current_password", "")
            if not check_master_password(ctx, cur):
                return JSONResponse({"ok": False, "error": "현재 비밀번호가 올바르지 않습니다."},
                                    status_code=401)
        try:
            ctx.storage.set_run_state({"master_lock_enabled": 1 if enabled else 0})
        except Exception as exc:
            msg = str(exc)
            if "column" in msg.lower() or "PGRST204" in msg or "schema cache" in msg.lower():
                return JSONResponse(
                    {"ok": False, "error": "저장소(run_state)에 master_lock_enabled 컬럼이 아직 "
                     "없습니다. Supabase SQL Editor 에서 아래를 1회 실행하세요:\n"
                     "alter table run_state add column if not exists master_lock_enabled boolean;"},
                    status_code=500)
            raise
        return JSONResponse({"ok": True})

    # ── 수집 키워드 관리 (마스터 패널, 2026-09-15) ───────────────────────
    # keyword_sets 는 collect_naver/collect_google_rss 가 매 회차 읽는 표라,
    # 여기서 켜고 끄거나 추가·삭제하면 다음 수집 사이클부터 바로 반영된다.
    @app.get("/api/master/telegram")
    def api_master_telegram(x_master_token: str = fastapi.Header(default="")):
        """마스터 패널 '텔레그램 연동' — 부서용(승인자 전용)·일반용(모든 직원) 채널 상태와 바로가기 주소."""
        if (err := _master_guard(x_master_token)):
            return err
        d_url, d_kind = dept_telegram_link(ctx)
        p_url, p_kind = public_telegram_link(ctx)
        return JSONResponse({"ok": True,
                             "dept": {"enabled": bool(ctx.cfg.telegram_enabled), "url": d_url, "kind": d_kind},
                             "public": {"enabled": public_enabled(), "url": p_url, "kind": p_kind}})

    @app.get("/api/master/keywords")
    async def api_master_keywords_get(x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        items = ctx.storage.all_keywords()
        return JSONResponse({
            "ok": True,
            "categories": KEYWORD_CATEGORIES,
            "always_category": NAVER_ALWAYS_CATEGORY,
            "items": [{
                "id": r["id"], "category": r["category"], "keyword": r["keyword"],
                "enabled": bool(r.get("enabled")),
            } for r in items],
        })

    @app.post("/api/master/keywords")
    async def api_master_keywords_add(payload: dict, x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        category = str((payload or {}).get("category") or "").strip()
        keyword = str((payload or {}).get("keyword") or "").strip()
        if category not in KEYWORD_CATEGORIES:
            return JSONResponse(
                {"ok": False, "error": f"분류는 {'/'.join(KEYWORD_CATEGORIES)} 중 하나여야 합니다."},
                status_code=400)
        if not keyword or len(keyword) > 50:
            return JSONResponse({"ok": False, "error": "키워드는 1~50자여야 합니다."}, status_code=400)
        row = ctx.storage.add_keyword(category, keyword)
        if row is None:
            return JSONResponse({"ok": False, "error": "이미 같은 분류에 같은 키워드가 있습니다."},
                                status_code=409)
        return JSONResponse({"ok": True, "item": {
            "id": row["id"], "category": row["category"], "keyword": row["keyword"], "enabled": True,
        }})

    @app.post("/api/master/keywords/{keyword_id}/toggle")
    async def api_master_keywords_toggle(keyword_id: str, payload: dict,
                                         x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        enabled = bool((payload or {}).get("enabled", True))
        ctx.storage.set_keyword_enabled(keyword_id, enabled)
        return JSONResponse({"ok": True})

    @app.delete("/api/master/keywords/{keyword_id}")
    async def api_master_keywords_delete(keyword_id: str,
                                         x_master_token: str = fastapi.Header(default="")):
        if (err := _master_guard(x_master_token)):
            return err
        ctx.storage.delete_keyword(keyword_id)
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
    def api_confirm_draft(article_id: str, x_master_token: str = fastapi.Header(default="")):
        """미리보기(draft) 기사를 목록에 등록한다."""
        if (err := _master_guard(x_master_token)):
            return err
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
    def api_discard_draft(article_id: str, x_master_token: str = fastapi.Header(default="")):
        """미리보기(draft) 기사를 등록하지 않고 버린다(보관 처리)."""
        if (err := _master_guard(x_master_token)):
            return err
        row = ctx.storage.article_detail(article_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "기사를 찾을 수 없습니다."}, status_code=404)
        if row.get("status") == "draft":
            ctx.storage.update_article(article_id, {"status": "archived"})
        return JSONResponse({"ok": True})

    @app.get("/api/articles/{article_id}/score-detail")
    def api_score_detail(article_id: str):
        """카드의 '긍정 · 22' 툴팁 — 감성 판단 근거와 중요도 점수 내역을 돌려준다.

        점수 내역은 지금의 규칙·본문으로 다시 계산한 값이다. 저장된 점수는 수집 때와 분석 때
        두 번 계산한 값 중 큰 쪽이라 다를 수 있어, 다르면 그 사실을 note 로 알려 준다.
        """
        row = ctx.storage.article_detail(article_id)
        if row is None:
            return JSONResponse({"ok": False, "error": "기사를 찾을 수 없습니다."}, status_code=404)
        # 옛 기사는 감성 근거가 비어 있다 — 처음 열릴 때 한 번 만들어 저장한다(시간당 상한으로 비용 관리).
        reason_now = sentiment_reason_of(row)
        if not reason_now and _senti_lazy_allow():
            reason_now = ensure_sentiment_reason(ctx, row)
        body = ctx.storage.body_of(article_id) or ""
        basis = body or (row.get("summary_text") or "")
        groups = normalize_group_list(jload(row.get("group_companies"), []))
        overrides, customs = get_score_rules(ctx.storage)
        # analyze_and_save 와 같은 조건(언론사가 식별돼 있으면 주요 언론사 기준 1)으로 계산한다.
        items = score_breakdown(row.get("title") or "", basis, groups,
                                1 if row.get("press_id") else 3, overrides, customs)
        computed = int(clamp(sum(p for _, p in items), 0, 100))
        stored = int(row.get("importance_score") or 0)
        notes = []
        if not body:
            notes.append("원문 본문(30일 보관)이 없어 요약문으로 계산했습니다.")
        if computed != stored:
            notes.append(f"저장된 점수 {stored}점은 수집·분석 두 시점의 계산 중 높은 값이라 위 합계와 다를 수 있습니다.")
        return JSONResponse({
            "ok": True,
            "sentiment": row.get("sentiment") or "중립",
            "sentiment_reason": reason_now,
            "score": stored,
            "computed": computed,
            "items": [{"label": label, "points": p} for label, p in items],
            "notes": notes,
        })

    _press_stats_cache: dict[str, Any] = {"at": 0.0, "data": None}

    @app.get("/api/press-stats")
    def api_press_stats():
        """언론사 탭 — 포스코퓨처엠 기사 언론사별 집계(최근 1년). 5분 캐시."""
        mono = time.monotonic()
        if _press_stats_cache["data"] and mono - _press_stats_cache["at"] < PRESS_STATS_TTL_SEC:
            return JSONResponse(_press_stats_cache["data"])
        now = now_utc()
        rows = ctx.storage.pfm_articles(iso(now - timedelta(days=365)), with_excerpt=True)
        items = aggregate_press_stats(rows, now)
        data = {
            "generated_at": iso(now),
            "windows": {k: d for k, d in PRESS_STATS_WINDOWS},
            "article_count": sum(e["year"] for e in items),
            "items": items,
        }
        _press_stats_cache.update(at=mono, data=data)
        return JSONResponse(data)

    _overview_cache: dict[str, tuple[str, str, str]] = {}      # 언론사 → (기사 서명, 문장, 출처)
    _overview_times: list[float] = []

    @app.get("/api/press-stats/overview")
    def api_press_overview(press: str = ""):
        """언론사 한 곳이 포스코퓨처엠을 전반적으로 어떻게 평가하는지 한 줄. 기사 구성이 바뀔 때만 다시 만든다(AI 비용 보호)."""
        data = _press_stats_cache["data"]
        if not data or time.monotonic() - _press_stats_cache["at"] >= PRESS_STATS_TTL_SEC:
            api_press_stats()
            data = _press_stats_cache["data"]
        entry = next((e for e in data["items"] if e["press"] == press), None)
        if entry is None:
            return JSONResponse({"ok": False, "error": "언론사를 찾을 수 없습니다."}, status_code=404)
        arts = entry["articles"][:25]
        sig = hashlib.sha1("|".join(f"{a['id']}:{a['tone']}" for a in arts).encode("utf-8")).hexdigest()
        hit = _overview_cache.get(press)
        if hit and hit[0] == sig:
            return JSONResponse({"ok": True, "press": press, "overview": hit[1], "source": hit[2], "articles": len(arts)})
        text, source = "", "rule"
        now_m = time.monotonic()
        while _overview_times and now_m - _overview_times[0] > 3600:
            _overview_times.pop(0)
        if len(_overview_times) < OVERVIEW_PER_HOUR:
            _overview_times.append(now_m)
            text = ctx.llm.press_overview(press, arts, entry["tone"])
            source = "ai"
        if not text:
            text, source = press_overview_fallback(entry), "rule"
        else:
            _overview_cache[press] = (sig, text, source)     # AI 결과만 저장 — 규칙 문장은 다음에 AI 를 다시 시도한다
        return JSONResponse({"ok": True, "press": press, "overview": text, "source": source, "articles": len(arts)})

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

        def _render_index() -> tuple[str, list[str]]:
            # JS·CSS 를 HTML 에 인라인해서 내보낸다. 별도 정적 요청이 없으므로
            # 쿼리스트링을 무시하는 프록시가 있어도 옛 파일을 내줄 수 없다.
            html, css, js, ea_css, ea_js = (
                open(p, "r", encoding="utf-8").read() for p in _index_files)
            # 리터럴 치환만 한다(re.sub 은 repl 의 \s 등을 이스케이프로 해석해 깨진다).
            html = html.replace('<link rel="stylesheet" href="./style.css">',
                                f"<style>\n{css}\n{ea_css}\n</style>")
            html = html.replace('<script src="./app.js"></script>',
                                f"<script>\n{js}\n</script>\n<script>\n{ea_js}\n</script>")
            # CSP script-src 는 'unsafe-inline' 을 안 쓰므로, 방금 인라인한 두 스크립트의
            # 해시를 매번 다시 계산해 허용 목록에 넣어야 브라우저가 실행을 막지 않는다.
            hashes = [_csp_hash(f"\n{js}\n"), _csp_hash(f"\n{ea_js}\n")]
            return html, hashes

        def _index_html() -> str:
            sig = tuple(os.path.getmtime(p) for p in _index_files)
            if _index_cache["sig"] != sig:
                html, hashes = _render_index()
                _index_cache.update(sig=sig, html=html)
                _csp_script_hashes[1:] = hashes
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

def fix_domain_press_names(ctx: Context, dry: bool) -> tuple[int, dict[str, int]]:
    """기사 테이블에서 언론사명이 도메인 모양이거나 비어 있는 행을 매핑표로 바로잡는다.

    sync_article_press_names 는 press_id 로 조인하므로, press_id 가 비어 있는 기사
    (메타만 먼저 저장된 기사 등)는 영영 고쳐지지 않았다. 여기서는 기사 URL 의 도메인으로
    직접 찾는다. 반환: (고친 건수, 매핑이 없어 못 고친 도메인별 기사 수).
    """
    press_by_domain = {r["domain"]: r for r in ctx.storage.all_press() if r.get("domain")}
    fixed = 0
    unmapped: dict[str, int] = {}
    for r in ctx.storage.article_press_rows():
        cur = (r.get("press_name") or "").strip()
        if cur and not _looks_like_domain(cur):
            continue
        url = r.get("url_canonical") or r.get("url_original") or r.get("url_source") or ""
        domain = press_domain_of(url) or (domain_of(f"http://{cur}") if cur else "")
        want = press_display_name(cur, url)
        prow = press_by_domain.get(domain)
        if (not want or _looks_like_domain(want)) and prow and not _looks_like_domain(prow.get("name") or ""):
            want = prow["name"]
        if not want or _looks_like_domain(want):
            if domain:
                unmapped[domain] = unmapped.get(domain, 0) + 1
            continue
        if want == cur:
            continue
        fixed += 1
        if not dry:
            patch: dict[str, Any] = {"press_name": want}
            if not r.get("press_id") and prow:
                patch["press_id"] = prow["id"]
            ctx.storage.update_article(r["id"], patch)
    return fixed, unmapped


def cmd_fixpress(ctx: Context, dry: bool = False) -> None:
    """SEED_PRESS 의 최신 이름을 press_outlets 에 반영하고, 기사에도 다시 맞춘다.

    도메인 그대로(예: 'ajunews.com') 저장돼 있던 언론사명을 정식 이름으로 교체한다.
    dry=True(`fixpress --dry`) 이면 아무것도 쓰지 않고 고칠 대상만 보고한다.
    """
    if dry:
        seed_todo = [d for d, (name, _) in SEED_PRESS.items()
                     if (ctx.storage.press_by_domain(d) or {}).get("name") != name]
        fixed, unmapped = fix_domain_press_names(ctx, dry=True)
        log.info("[미리보기] 매핑표 반영 대상 언론사 %d곳 · 도메인 이름 기사 %d건 교정 예정",
                 len(seed_todo), fixed)
        for d, n in sorted(unmapped.items(), key=lambda x: -x[1]):
            log.warning("[미리보기] 매핑 없음 — %s (기사 %d건). SEED_PRESS 에 추가 필요", d, n)
        log.info("[미리보기] 실제로 고치려면 --dry 없이 다시 실행하세요.")
        return
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
    # og:site_name/<title> 에서 한글 매체명을 시도한다. 기사 페이지는 SEO 상
    # 기사 제목만 <title>에 넣는 경우가 많아 실패하기 쉬우니, 안 되면 홈페이지도
    # 한 번 더 본다(_homepage_site_name). 그래도 실패하면 도메인 전체로 둔다.
    ogfix = 0
    tried: set[str] = set()
    for r in ctx.storage.article_press_rows():   # 최근 5000건이 아니라 전체 기사를 본다
        name = (r.get("press_name") or "").strip()
        if _has_hangul(name):
            continue
        target = r.get("url_original") or r.get("url_canonical") or ""
        domain = press_domain_of(target)
        if not domain or domain in SEED_PRESS or domain in tried:
            continue
        tried.add(domain)
        og = ""
        try:
            _, html = resolve_canonical(ctx.http, target)
            og = site_name_from_html(html, domain)
        except Exception as exc:
            log.debug("og:site_name 조회 실패 %s: %s", target, exc)
        if not og:
            og = _homepage_site_name(ctx.http, domain, urlsplit(target).hostname or "")
        ctx.storage.update_press_name(domain, og or domain, 3)
        if og:
            ogfix += 1

    synced = ctx.storage.sync_article_press_names()
    fixed, unmapped = fix_domain_press_names(ctx, dry=False)
    log.info("언론사명 정리: %d개 SEED 교체 · %d개 부제 제거 · %d개 og:site_name 복원 · "
             "기사 %d건 반영 · 도메인 이름 기사 %d건 교정",
             renamed, cleaned, ogfix, synced, fixed)
    for d, n in sorted(unmapped.items(), key=lambda x: -x[1]):
        log.warning("매핑 없음 — %s (기사 %d건). SEED_PRESS 에 추가 필요", d, n)


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
               or is_non_person_byline(restored) or not split_authors(restored)
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
        else:
            # 소속·직함·이메일이 붙은 값('연합뉴스 김철수 기자')은 사람 이름만 남긴다.
            norm = "·".join(split_authors(restored)) or restored
            if norm != author:
                ctx.storage.update_article(r["id"], {"author": norm})
                fixed += 1
    log.info("기자명 복구: %d건 정리 · %d건 재추출 · %d건 제거", fixed, recovered, cleared)


def cmd_fixcategories(ctx: Context) -> None:
    """카테고리를 제목+요약 기준으로 재태깅한다 (일회성).

    과거엔 본문 2000자를 스캔하고 규칙에 '정부'·'정책' 같은 흔한 단어가 있어
    거의 모든 기사에 3~4개 카테고리가 붙어 필터가 변별력을 잃었다.

    인사·부고는 detect_categories 가 전혀 모르는 별도 판정 경로(people_news_kind)로
    붙는 태그라, 예전엔 이 함수가 지나갈 때마다 조용히 지워졌다(2026-09-10 발견 —
    인사·부고 기사 104건이 이 명령 실행 시점에 태그를 잃고 탭에서 사라짐). 그래서
    인사·부고 기사는 재태깅 대상에서 제외하고 태그를 그대로 둔다.
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    fixed = 0
    for r in rows:
        if people_news_kind(r.get("url_canonical") or r.get("url_source") or "", r.get("title") or ""):
            continue
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
            r.get("title") or "", group_lead_text(body),
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
    global PEOPLE_CAREER_PER_HOUR
    PEOPLE_CAREER_PER_HOUR = max(PEOPLE_CAREER_PER_HOUR, 400)   # 일회성 재정리는 시간당 검색 상한을 넉넉히
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
            if len(_notice_text(html, body, r.get("title") or "")) < 20:
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


def cmd_reexcerpt(ctx: Context, dry: bool = False) -> None:
    """보관 중인 본문(30일)으로 포스코퓨처엠 언급 발췌를 새 방식(문단 단위·기사 주제 포함)으로 다시 만든다.

    LLM 을 쓰지 않는다(논조는 그대로 둔다). 영어 본문은 건너뛴다 — 새로 뽑으면 영어 원문으로 되돌아가므로
    `retranslate` 로 번역한다. 본문이 30일 보관 기간을 지난 기사는 원문이 없어 옛 발췌를 그대로 둔다.
    """
    rows = ctx.storage.pfm_articles(iso(now_utc() - timedelta(days=30)), with_excerpt=True)
    changed = skipped = nobody = 0
    for r in rows:
        body = ctx.storage.body_of(r["id"]) or ""
        if not body:
            nobody += 1
            continue
        if looks_english(body[:600]):
            skipped += 1
            continue
        new = extract_pfm_excerpt(body)
        if new and new != (r.get("pfm_excerpt") or ""):
            changed += 1
            if not dry:
                ctx.storage.update_article(r["id"], {"pfm_excerpt": new})
    log.info("발췌 재생성%s: %d건 새 방식으로 교체 · 영어 본문 %d건 건너뜀 · 보관 본문 없음 %d건 (대상 %d건)",
             " [미리보기, 쓰지 않음]" if dry else "", changed, skipped, nobody, len(rows))


def cmd_retranslate(ctx: Context, limit: int = 50, dry: bool = False) -> None:
    """이미 저장된 영어 기사(제목·포스코퓨처엠 발췌)를 한국어로 번역한다(AI 호출 최대 limit 회).

    영어 원제는 카드에 작게 남기도록 summaries.perspective_text 에 함께 저장한다. 이미 번역된 기사는 건너뛴다.
    """
    rows = ctx.storage.list_articles(5000, 0, None, "")
    todo = []
    for r in rows:
        en_title = looks_english(r.get("title") or "") and not title_original_of(r)
        en_excerpt = looks_english(r.get("pfm_excerpt") or "")
        if en_title or en_excerpt:
            todo.append((r, en_title, en_excerpt))
    log.info("영어 기사 번역 대상 %d건 (최대 %d건 처리)%s", len(todo), limit, " [미리보기, 쓰지 않음]" if dry else "")
    if dry:
        for r, _, _ in todo[:10]:
            log.info("  - %s | %s", (r.get("press_name") or "")[:10], (r.get("title") or "")[:60])
        return
    done = failed = 0
    for r, en_title, en_excerpt in todo[:limit]:
        t_ko, e_ko = ctx.llm.translate_to_korean(r.get("title") if en_title else "",
                                                 r.get("pfm_excerpt") if en_excerpt else "")
        patch: dict[str, Any] = {}
        if t_ko:
            patch["title"] = t_ko
        if e_ko:
            patch["pfm_excerpt"] = e_ko
        if not patch:
            failed += 1
            continue
        ctx.storage.update_article(r["id"], patch)
        if t_ko:
            ctx.storage.set_perspective(r["id"], pack_perspective(
                sentiment_reason_of(r), r.get("title") or "", r.get("perspective_text") or ""))
        done += 1
        log.info("  ✓ %s", (t_ko or r.get("title") or "")[:50])
    log.info("영어 기사 번역 완료: 성공 %d · 실패 %d · 남은 대상 %d건", done, failed, max(0, len(todo) - limit))


def cmd_fixpfm(ctx: Context, dry: bool = False) -> None:
    """'포스코퓨처엠' 태그가 붙었지만 제목·본문 어디에도 언급이 없는 기사에서 그 태그를 뗀다(일회성).

    발췌문(pfm_excerpt)이 빈 값('')인 기사 = 분석 때 본문에서 언급을 못 찾은 기사다.
    (발췌가 NULL 인 기사는 아직 확인 전이라 건드리지 않는다 — 먼저 `pfmtone` 으로 채운다.)
    dry=True 면 대상만 보여 주고 쓰지 않는다.
    """
    rows = ctx.storage.pfm_articles(iso(now_utc() - timedelta(days=365)), with_excerpt=True)
    bad = [r for r in rows if not pfm_mention_supported(r)]
    log.info("포스코퓨처엠 언급 근거 없는 태그 기사 %d건 (전체 %d건)%s", len(bad), len(rows),
             " — 미리보기, 쓰지 않음" if dry else "")
    if dry:
        for r in bad[:15]:
            log.info("  - %s | %s", (r.get("press_name") or "")[:10], (r.get("title") or "")[:50])
        return
    for r in bad:
        detail = ctx.storage.article_detail(r["id"]) or {}
        groups = [g for g in jload(detail.get("group_companies"), []) if g != "포스코퓨처엠"]
        ctx.storage.update_article(r["id"], {"group_companies": groups})
        log.info("  - 태그 제거: %s", (r.get("title") or "")[:50])
    log.info("포스코퓨처엠 태그 정리 완료: %d건 (언론사 탭·필터는 최대 몇 분 뒤 반영)", len(bad))


def cmd_reopen(ctx: Context, days: int = 3, dry: bool = False) -> None:
    """수집 규칙을 넓힌 뒤, 예전에 '무관'·'접속 실패'로 제외된 최근 기사를 다시 판정 대상으로 되돌린다.

    url_ledger 의 off_topic·extract_failed 기록은 180일간 영구 제외라, 규칙을 고쳐도 최근 기사는
    계속 걸러진 채 남는다(2026-09-30 통상 기사 누락 사례). 최근 days 일 분량의 그 기록만 지운다 —
    stale(오래됨)·no_pubdate 는 규칙과 무관하므로 건드리지 않는다. 지운 뒤 worker 를 재시작해야
    메모리 캐시(seen_cache)에서도 빠진다.
    """
    days = max(1, min(days, 10))   # 신선도 창(72시간)보다 오래된 기사는 어차피 다시 안 받는다
    reasons = ("off_topic", "extract_failed")
    if dry:
        log.info("[미리보기] 최근 %d일의 %s 기록을 지울 예정입니다(실제로는 지우지 않음).",
                 days, " · ".join(reasons))
        return
    n = ctx.storage.clear_ledger(reasons, days)
    log.info("제외 기록 %d건을 지웠습니다(최근 %d일, %s). worker 를 재시작하면 다음 수집 회차에 다시 판정합니다.",
             n, days, " · ".join(reasons))


def cmd_tagassoc(ctx: Context, dry: bool = False) -> None:
    """이미 저장된 기사에 빠진 그룹사 태그를 뒤늦게 붙인다(일회성) — '배터리협회'와 '포스코퓨처엠'.

    제목·보관 본문(30일)에 별칭(띄어쓴 표기 포함)이 있으면 태그를 더한다. 협회는 요약까지 보지만,
    포스코퓨처엠은 AI 가 쓴 요약을 근거로 인정하지 않으므로 제목·본문만 본다.
    포스코퓨처엠 태그를 새로 붙이는 기사는 발췌를 비워 두어(NULL) `pfmtone` 이 발췌·논조를 다시 채운다.
    (이름 주의: `regroup` 은 원문 리드 기준 그룹사 재판정이라는 별개의 기존 명령이다.)
    dry=True 면 대상 건수만 보여 주고 쓰지 않는다.
    """
    targets: dict[tuple[str, str], dict] = {}
    rows = ctx.storage.list_articles(20000, 0, None, "")
    for group in ("포스코퓨처엠", *sorted(NON_POSCO_GROUPS)):
        aliases = _GROUP_ALIASES_LOWER[group]
        ids: set[str] = set()
        for alias in aliases:
            ids |= ctx.storage.body_match_ids(alias)
        for r in rows:
            if group in jload(r.get("group_companies"), []):
                continue
            probe = (r.get("title") or "").lower()
            if group in NON_POSCO_GROUPS:
                probe += " " + (r.get("summary_text") or "").lower()
            if r.get("id") in ids or any(a in probe for a in aliases):
                targets[(r["id"], group)] = {"row": r, "group": group}
    log.info("그룹사 태그 보충 대상 %d건%s", len(targets), " (미리보기 — 쓰지 않음)" if dry else "")
    if dry:
        return
    for (aid, group), t in targets.items():
        r = t["row"]
        groups = normalize_group_list(jload(r.get("group_companies"), []) + [group])
        patch: dict[str, Any] = {"group_companies": groups}
        if group == "포스코퓨처엠" and (r.get("pfm_excerpt") or "") == "":
            patch["pfm_excerpt"] = None   # pfmtone 이 다시 채운다
        ctx.storage.update_article(aid, patch)
        log.info("  + %s → %s", (r.get("title") or "")[:40], group)
    log.info("그룹사 태그 보충 완료: %d건 (화면 필터는 최대 몇 분 뒤 반영 — 포스코퓨처엠은 이어서 pfmtone 실행)",
             len(targets))


def cmd_addkw(ctx: Context, category: str, words: Sequence[str]) -> None:
    """수집 키워드를 서버에서 바로 추가한다(마스터 패널 화면 없이). 이미 있으면 건너뛴다."""
    if category not in KEYWORD_CATEGORIES:
        log.error("분류는 %s 중 하나여야 합니다. 예: addkw 그룹사 배터리협회 KBIA",
                  " · ".join(KEYWORD_CATEGORIES))
        return
    added = skipped = 0
    for w in words:
        kw = (w or "").strip()
        if not kw:
            continue
        if ctx.storage.add_keyword(category, kw):
            added += 1
            log.info("  + 추가: [%s] %s", category, kw)
        else:
            skipped += 1
            log.info("  = 이미 있음: [%s] %s", category, kw)
    log.info("수집 키워드 추가 완료: 새로 %d개 · 이미 있음 %d개 (다음 수집 회차부터 조회됩니다)", added, skipped)


def _title_tone_basis(ctx: Context, r: dict) -> str:
    """본문에서 언급을 못 찾은 기사의 논조 판정 근거 — 제목에 포스코퓨처엠이 있을 때만 '제목 + 요약의 언급 문장'.
    제목에도 없으면 ''(근거 없음)."""
    title = (r.get("title") or "").strip()
    if not any(a in title.lower() for a in _GROUP_ALIASES_LOWER["포스코퓨처엠"]):
        return ""
    detail = ctx.storage.article_detail(r["id"]) or {}
    from_summary = extract_pfm_excerpt(detail.get("summary_text") or "")
    return f"{title}\n{from_summary}".strip()


def cmd_unrated(ctx: Context, limit: int = 40) -> None:
    """언론사 탭에서 '미판정'으로 남은 포스코퓨처엠 기사를 원인별로 나눠 보여 준다(점검용, 쓰지 않음)."""
    rows = ctx.storage.pfm_articles(iso(now_utc() - timedelta(days=365)), with_excerpt=True)
    unrated = [r for r in rows if pfm_mention_supported(r) and not r.get("pfm_tone")]
    kinds = {"발췌 미생성(본문 확보 못함)": [], "본문엔 언급 없고 제목에만 있음": [], "발췌는 있는데 AI 판정 실패": []}
    for r in unrated:
        ex = r.get("pfm_excerpt")
        kinds["발췌 미생성(본문 확보 못함)" if ex is None else
              "본문엔 언급 없고 제목에만 있음" if ex == "" else "발췌는 있는데 AI 판정 실패"].append(r)
    log.info("미판정 %d건 (집계 대상 %d건 중)", len(unrated), len([r for r in rows if pfm_mention_supported(r)]))
    for name, lst in kinds.items():
        log.info("  · %s: %d건", name, len(lst))
        for r in lst[:limit]:
            log.info("      - %s | %s | %s", (r.get("press_name") or "")[:10], (r.get("title") or "")[:46],
                     (r.get("url_canonical") or r.get("url_original") or "")[:70])


def cmd_sentireason(ctx: Context, limit: int = 100, dry: bool = False, days: int = 90) -> None:
    """감성 근거([감성근거])가 비어 있는 옛 기사에 근거를 채운다(일회성, 2026-10-02).

    카드의 '긍정 · 22' 툴팁이 '개별 근거가 저장되기 전에 분석되어…' 로만 나오던 문제를 없앤다.
    최근 days 일 기사 중 감성이 있고 근거가 없는 것만 대상이며, limit 건까지 AI 를 부른다(비용 관리).
    """
    since = now_utc() - timedelta(days=days)
    rows = ctx.storage.list_articles(5000, 0, since, "")
    todo = [r for r in rows if (r.get("sentiment") in PFM_TONES) and not sentiment_reason_of(r)
            and (r.get("summary_text") or "").strip()]
    log.info("감성 근거 없는 기사 %d건 (최근 %d일 %d건 중)%s", len(todo), days, len(rows),
             " — 미리보기, 쓰지 않음" if dry else "")
    if dry:
        log.info("[미리보기] 이번 실행 AI 호출은 최대 %d건입니다. 실제로 채우려면 --dry 없이 다시 실행하세요.",
                 min(len(todo), limit))
        return
    done = failed = 0
    for r in todo[:limit]:
        if ensure_sentiment_reason(ctx, r):
            done += 1
        else:
            failed += 1
    log.info("감성 근거 생성 완료: 성공 %d · 실패 %d · 남은 대상 %d건", done, failed, max(0, len(todo) - limit))


def cmd_pfmtone(ctx: Context, limit: int = 200, dry: bool = False, days: int = 365) -> None:
    """기존 포스코퓨처엠 기사에 발췌문·논조를 채운다(백필, 사용자 지정 2026-09-29).

    대상: 최근 days 일 포스코퓨처엠 태그 기사 중 발췌문이 아직 없거나(NULL),
    발췌는 있는데 논조가 비어 있는 것. 이미 판정된 기사는 건너뛴다(재판정 없음).
    본문은 30일 보관분을 먼저 쓰고, 없으면 원문 페이지를 다시 받는다.
    limit = 이번 실행의 LLM 호출 상한(비용 관리). dry=True 면 대상 수만 보고하고 아무것도
    쓰지 않으며 네트워크·LLM 도 쓰지 않는다.
    """
    rows = ctx.storage.pfm_articles(iso(now_utc() - timedelta(days=days)))
    need_excerpt = [r for r in rows if r.get("pfm_excerpt") is None]
    need_tone = [r for r in rows if r.get("pfm_excerpt") and not r.get("pfm_tone")]
    # 요약문으로 이미 논조를 판정한 기사(발췌는 NULL 인 채)는 다시 보지 않는다.
    need_excerpt = [r for r in need_excerpt if not r.get("pfm_tone")]
    # 발췌가 빈 값('')인 기사 중 제목에 이름이 있는 것 — 본문에선 언급을 못 찾았어도 제목(+요약의 언급 문장)으로
    # 논조를 판정한다. 안 그러면 언론사 탭에 계속 '미판정'으로 남는다(2026-10-01).
    need_title = [r for r in rows if r.get("pfm_excerpt") == "" and not r.get("pfm_tone")
                  and pfm_mention_supported(r)]
    todo = need_tone + need_excerpt + need_title   # 발췌가 이미 있는 쪽이 싸다(원문 재수집 불필요) — 먼저
    log.info("논조 백필 대상 %d건 — 발췌부터 필요 %d · 논조만 필요 %d · 제목 기준 %d (최근 %d일 포스코퓨처엠 기사 %d건)",
             len(todo), len(need_excerpt), len(need_tone), len(need_title), days, len(rows))
    if dry:
        log.info("[미리보기] 이번 실행 LLM 호출은 최대 %d건입니다(limit=%d). "
                 "발췌가 필요한 기사는 원문을 다시 받아 언급이 없으면 호출하지 않습니다.",
                 min(len(todo), limit), limit)
        log.info("[미리보기] 실제로 채우려면 --dry 없이 다시 실행하세요.")
        return
    excerpted = no_mention = toned = failed = llm_used = visited = 0
    for r in todo:
        if llm_used >= limit:
            break
        visited += 1
        excerpt = r.get("pfm_excerpt")
        if excerpt == "":
            basis = _title_tone_basis(ctx, r)
            if not basis:
                failed += 1
                continue
            tone, reason = ctx.llm.pfm_tone(basis)
            llm_used += 1
            if tone:
                ctx.storage.update_article(r["id"], {"pfm_tone": tone, "pfm_tone_reason": reason})
                toned += 1
            else:
                failed += 1
            continue
        if excerpt is None:
            body = ctx.storage.body_of(r["id"]) or ""
            if not body:
                target = r.get("url_canonical") or r.get("url_original") or ""
                try:
                    _, html = resolve_canonical(ctx.http, target)
                    body = extract_body(html) if html else ""
                except Exception as exc:
                    log.debug("원문 재수집 실패 %s: %s", target, exc)
            if len(body) < 50:
                # 본문이 없다(30일 보관 이전 기사이거나 원문 접속 실패) — 저장된 요약문에서
                # 포스코퓨처엠 언급 문장을 찾아 그것으로 논조만 판정한다. 발췌는 NULL 로 둔다
                # (요약은 발췌가 아니므로 카드의 '포스코퓨처엠 언급' 칸에 넣지 않는다).
                detail = ctx.storage.article_detail(r["id"]) or {}
                from_summary = extract_pfm_excerpt(detail.get("summary_text") or "") \
                    or _title_tone_basis(ctx, r)      # 요약에도 없으면 제목에 이름이 있을 때 제목으로
                if not from_summary:
                    failed += 1    # 제목·요약 어디에도 언급이 없다 — NULL 로 두어 다음 실행에서 다시 시도
                    continue
                tone, reason = ctx.llm.pfm_tone(from_summary)
                llm_used += 1
                if tone:
                    ctx.storage.update_article(
                        r["id"], {"pfm_tone": tone, "pfm_tone_reason": reason})
                    toned += 1
                else:
                    failed += 1
                continue
            excerpt = extract_pfm_excerpt(body)
            if looks_english(excerpt):        # 영어 기사는 한글로 번역해 저장한다(실패하면 원문 그대로)
                _, e_ko = ctx.llm.translate_to_korean("", excerpt)
                excerpt = e_ko or excerpt
            ctx.storage.update_article(r["id"], {"pfm_excerpt": excerpt})
            excerpted += 1
            if not excerpt:
                no_mention += 1    # 태그는 있지만 본문에 언급 없음 — '' 로 저장돼 다시 안 본다
                continue
        tone, reason = ctx.llm.pfm_tone(excerpt)
        llm_used += 1
        if tone:
            ctx.storage.update_article(r["id"], {"pfm_tone": tone, "pfm_tone_reason": reason})
            toned += 1
        else:
            failed += 1
    log.info("논조 백필 완료: 발췌 %d건(언급 없음 %d) · 논조 판정 %d건 · 실패 %d건 · LLM %d회 · "
             "이번에 못 본 대상 %d건(실패분과 함께 다음 실행에서 계속)",
             excerpted, no_mention, toned, failed, llm_used, len(todo) - visited)


def cmd_fixexcerpt(ctx: Context, press: str = "", limit: int = 60, dry: bool = False) -> None:
    """'포스코퓨처엠' 기사인데 카드에 언급 발췌가 비어 있는 것을 점검·복구한다(2026-10-06 머니투데이 사례).

    대상: 발췌가 NULL(아직 못 만듦) 또는 ''(분석 때 본문에서 언급을 못 찾음)인 포스코퓨처엠 기사.
    press 를 주면 그 언론사(부분 일치)만. dry 면 원인별 건수만 보여 주고 아무것도 쓰지 않는다.
    복구: 보관 본문(30일)이 있으면 그것으로, 없거나 언급이 안 잡히면 원문 페이지를 다시 받아
    발췌를 만든다. 논조가 비어 있으면 함께 채운다(AI 최대 limit 회). 원문에도 언급이 없으면 그대로 둔다.
    """
    rows = ctx.storage.pfm_articles(iso(now_utc() - timedelta(days=365)), with_excerpt=True)
    todo = []
    for r in rows:
        if r.get("pfm_excerpt") not in (None, ""):
            continue
        url = r.get("url_canonical") or r.get("url_original") or ""
        name = press_display_name(r.get("press_name") or "", url) or "(언론사 미상)"
        if press and press not in name:
            continue
        todo.append((name, r))
    by_press = Counter(n for n, _ in todo)
    log.info("발췌 비어 있는 포스코퓨처엠 기사 %d건%s — 언론사별: %s", len(todo),
             f" (언론사 '{press}')" if press else "",
             ", ".join(f"{n} {c}" for n, c in by_press.most_common(8)) or "없음")
    if dry:
        for name, r in todo[:15]:
            body = ctx.storage.body_of(r["id"]) or ""
            has = bool(extract_pfm_excerpt(body)) if body else False      # 실제 발췌 규칙으로 판정
            state = "NULL" if r.get("pfm_excerpt") is None else "''"
            log.info("  - [%s] %s | 발췌 %s · 보관본문 %d자 · 발췌 가능 %s | %s", name[:8], (r.get("title") or "")[:40],
                     state, len(body), "있음" if has else "없음", (r.get("url_canonical") or r.get("url_original") or "")[:60])
        log.info("[미리보기] 실제로 복구하려면 --dry 없이 다시 실행하세요(이번 AI 호출은 최대 %d회).", limit)
        return
    fixed = still_none = nobody = toned = llm_used = 0
    for name, r in todo:
        body = ctx.storage.body_of(r["id"]) or ""
        excerpt = extract_pfm_excerpt(body) if body else ""
        if not excerpt:
            target = r.get("url_canonical") or r.get("url_original") or ""
            try:
                _, html = resolve_canonical(ctx.http, target)
                fresh = extract_body(html) if html else ""
            except Exception as exc:
                log.debug("원문 재수집 실패 %s: %s", target, exc)
                fresh = ""
            if len(fresh) < 50 and not body:
                nobody += 1
                log.info("  ✗ 본문 확보 못함: [%s] %s", name[:8], (r.get("title") or "")[:40])
                continue
            excerpt = extract_pfm_excerpt(fresh)
        if not excerpt:
            still_none += 1
            ctx.storage.update_article(r["id"], {"pfm_excerpt": ""})
            log.info("  · 원문에도 언급 없음: [%s] %s", name[:8], (r.get("title") or "")[:40])
            continue
        if looks_english(excerpt):
            _, e_ko = ctx.llm.translate_to_korean("", excerpt)
            excerpt = e_ko or excerpt
        patch: dict[str, Any] = {"pfm_excerpt": excerpt}
        if not r.get("pfm_tone") and llm_used < limit:
            tone, reason = ctx.llm.pfm_tone(excerpt)
            llm_used += 1
            if tone:
                patch.update(pfm_tone=tone, pfm_tone_reason=reason)
                toned += 1
        ctx.storage.update_article(r["id"], patch)
        fixed += 1
        log.info("  ✓ 발췌 복구: [%s] %s", name[:8], (r.get("title") or "")[:40])
    log.info("발췌 복구 완료: 복구 %d건(논조 %d건) · 원문에도 언급 없음 %d건 · 본문 확보 못함 %d건 · AI %d회",
             fixed, toned, still_none, nobody, llm_used)


def cmd_daum_test(ctx: Context, show: int = 12) -> None:
    """다음 수집을 미리 점검한다(저장·분석 없음). 그룹사 키워드로 'site:v.daum.net' 검색 결과를 받아
    몇 건이 나오는지, 그중 제목이 이미 DB 에 있는 기사(중복 추정)와 새 후보가 몇 건인지 보여 준다."""
    rows = ctx.storage.enabled_keywords()
    drows = daum_keyword_rows(rows)
    log.info("다음 점검 — 그룹사 키워드 %d개: %s", len(drows), ", ".join(r["keyword"] for r in drows))
    items = collect_google_rss(ctx.http, drows)
    uniq: dict[str, RawItem] = {}
    for it in items:
        uniq.setdefault(it.url_source, it)
    cands = ctx.storage.recent_articles_for_dedup(now_utc() - timedelta(days=7))
    known = {normalize_title(c.get("title") or "") for c in cands}
    dup, fresh = [], []
    for it in uniq.values():
        (dup if normalize_title(it.title) in known else fresh).append(it)
    log.info("다음 검색 결과 %d건(중복 제거 후 %d건) → 제목이 이미 있는 기사 %d건 · 새 후보 %d건 (최근 7일 기준)",
             len(items), len(uniq), len(dup), len(fresh))
    for it in fresh[:show]:
        log.info("  + [%s] %s | %s", (it.press_hint or "")[:12], it.title[:60],
                 it.published_at.strftime("%m-%d %H:%M") if it.published_at else "발행시각 없음")
    if not items:
        log.warning("결과가 0건입니다 — 구글 RSS 가 site: 검색을 막았거나 키워드가 없습니다. 켜지 않는 편이 좋습니다.")
    log.info("켜려면 서버 .env 에 DAUM_ENABLED=true 를 넣고 컨테이너를 다시 시작하세요(켠 뒤 첫 회차부터 수집).")


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
    if public_enabled():
        log.info("일반용 텔레그램 발송 %d건", send_public_notifications(ctx))
    print(json.dumps(result, ensure_ascii=False, indent=2))


QUOTE_REFRESH_SEC = 60   # 시세는 수집 주기와 무관하게 항상 60초로 갱신한다. (PRD F9.2)


# worker(웹서버 없이 수집만 도는 모드)는 /healthz 가 없다. AWS ECS 등 컨테이너
# 플랫폼이 "살아있는지"를 물을 방법이 필요해서, 수집 루프가 사이클마다 로컬
# 파일에 시각을 남기고 `healthcheck` 명령이 그 최신성으로 판정한다.
_HEARTBEAT_PATH = os.path.join(tempfile.gettempdir(), "pfm_news_heartbeat")

# 이 프로세스를 run_state.pipeline_lock_owner 에 식별하는 값. 정상 운영은 항상
# 인스턴스 1개지만, AWS 등에서 desiredCount 를 잘못 설정하거나 배포 중 잠깐
# 신·구 인스턴스가 겹치면 수집·알림이 중복된다 — run_once() 가 매 회차 이
# 값으로 실행권(락)을 얻으려 시도해 그런 사고를 막는다.
_INSTANCE_ID = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"
PIPELINE_LOCK_STALE_MULT = 3   # 락 유효시간 = poll_interval_sec 의 이 배수 (죽은 소유자 회수용)


def _touch_heartbeat() -> None:
    try:
        with open(_HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            f.write(iso(now_utc()))
    except OSError as exc:   # 헬스체크 실패는 치명적이지 않다 — 다음 사이클에 재시도
        log.debug("heartbeat 기록 실패: %s", exc)


def cmd_healthcheck(max_age_sec: int) -> int:
    """worker 컨테이너용 헬스체크. Docker/ECS 헬스체크 CMD 로 등록한다:
    `python backend/main.py healthcheck` (기본 상한 poll_interval_sec 의 3배).
    DB·API 키가 필요 없어 컨테이너가 자주 불러도 가볍다.
    """
    try:
        with open(_HEARTBEAT_PATH, encoding="utf-8") as f:
            ts = parse_dt(f.read().strip())
    except OSError:
        print("unhealthy: heartbeat 파일이 없습니다(첫 사이클 전이거나 worker 가 아닙니다).")
        return 1
    if ts is None:
        print("unhealthy: heartbeat 파일을 읽을 수 없습니다.")
        return 1
    age = (now_utc() - ts).total_seconds()
    if age > max_age_sec:
        print(f"unhealthy: 마지막 수집이 {age:.0f}초 전 (기준 {max_age_sec}초).")
        return 1
    print(f"healthy: 마지막 수집 {age:.0f}초 전.")
    return 0


def pipeline_loop(ctx: Context, stop: threading.Event) -> None:
    """수집·분석·알림 루프. 시세는 quote_loop 가 따로 돈다."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            run_once(ctx)
            # 한 회차에 12건까지만 — 억제 해제 등으로 큐가 밀려도 분당 한도를 넘기지 않는다.
            send_notifications(ctx, limit=SEND_BATCH_PER_CYCLE)
            send_public_notifications(ctx)      # 일반용(모든 직원) — 긍정·중립만, 설정이 없으면 아무 일도 안 한다
            maybe_run_weekly(ctx)
            # 목록 스캔 스토어를 델타로 갱신해 둔다(웹 요청 시 DB 재조회 없음).
            refresh_scan_store(ctx.storage)
        except Exception as exc:
            log.exception("파이프라인 실행 중 오류: %s", exc)
        # 예외가 나도 사이클이 '끝나긴' 했다는 신호로 찍는다 — worker(웹서버 없음)는
        # /healthz 가 없어 컨테이너 헬스체크가 이걸로 대신 판단한다(cmd_healthcheck).
        # 사이클이 진짜로 멎으면(네트워크 요청이 행 걸리는 등) 여기까지 못 와서
        # heartbeat 가 오래된 채로 남고, 헬스체크가 정확히 그걸 감지한다.
        _touch_heartbeat()

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


def _start_pipeline_threads(ctx: Context, stop: threading.Event) -> list[threading.Thread]:
    """수집·시세·텔레그램봇·대외협력 스케줄러 스레드를 만들어 시작한다.

    serve(--with-pipeline)와 worker 가 이 목록을 공유한다 — 두 곳에 따로
    적어두면 한쪽만 고쳤을 때 조용히 갈라진다(PRD F6.1a filter_tagged 와
    같은 이유의 '유일한 구현' 원칙).
    """
    threads: list[threading.Thread] = [
        threading.Thread(target=pipeline_loop, args=(ctx, stop), daemon=True),
        threading.Thread(target=quote_loop, args=(ctx, stop), daemon=True),
    ]
    log.info("수집 루프 시작 (%d초 주기) · 시세 갱신 %d초", ctx.cfg.poll_interval_sec, QUOTE_REFRESH_SEC)
    if ctx.cfg.telegram_enabled:
        threads.append(threading.Thread(target=telegram_bot_loop, args=(ctx, stop), daemon=True))
    if ea_mod is not None and ea_mod.ea_enabled():
        threads.append(threading.Thread(target=ea_mod.scheduler_loop, args=(ctx, stop), daemon=True))
    for t in threads:
        t.start()
    return threads


def cmd_serve(ctx: Context, with_pipeline: bool) -> None:
    uvicorn = _import("uvicorn", "uvicorn")
    # 시드는 idempotent — 새로 추가된 RSS 피드·키워드를 기동 시 반영한다.
    try:
        ctx.storage.seed_feeds(SEED_FEEDS)
    except Exception as exc:   # pragma: no cover
        log.warning("피드 시드 스킵: %s", exc)
    app = create_app(ctx)
    stop = threading.Event()
    threads = _start_pipeline_threads(ctx, stop) if with_pipeline else []
    log.info("서버: http://%s:%d", ctx.cfg.api_host, ctx.cfg.api_port)
    try:
        uvicorn.run(app, host=ctx.cfg.api_host, port=ctx.cfg.api_port, log_level="warning")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)


def cmd_worker(ctx: Context) -> None:
    """웹서버 없이 수집·시세·텔레그램봇·대외협력 스케줄러만 돌린다 (다중 프로세스 배포용).

    지금까지는 `run` 하나가 웹서버·수집·봇을 전부 한 프로세스에 담아서, 코드
    수정 뒤 재시작하면(수집 로직만 고쳤어도) 웹 접속도 같이 끊겼고, 어느
    한쪽에서 처리되지 않은 예외로 프로세스 전체가 죽으면 나머지도 같이
    멎었다(2026-09-11 구조 점검, 사용자 지정). `serve`(웹만)와 이 명령을
    각각 별도 OS 프로세스로 띄우면 서로 DB 로만 통신하니 한쪽이 죽거나
    재시작해도 다른 쪽은 계속 돈다.

        프로세스 A: python backend/main.py serve
        프로세스 B: python backend/main.py worker
    """
    try:
        ctx.storage.seed_feeds(SEED_FEEDS)
    except Exception as exc:   # pragma: no cover
        log.warning("피드 시드 스킵: %s", exc)
    stop = threading.Event()
    threads = _start_pipeline_threads(ctx, stop)
    log.info("worker 시작 (웹서버 없음) — Ctrl+C 로 종료")
    try:
        while not stop.is_set():
            stop.wait(1.0)
    except KeyboardInterrupt:
        log.info("worker 종료 요청을 받았습니다.")
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

    # 실제 오탐 사례(2026-09-10 ①): 대보건설 신안산선 기사, 본문 835자 중 737번째
    # 참여사 나열 문장('포스코이앤씨' 등)이 700자 리드 컷에 37자 차이로 빠졌다.
    # 회사명이 쉼표로 3개 이상 나열된 '정식 disclosure' 문장은 리드 밖이어도 살린다.
    _list_body = ("ㅁ" * 730 + " 공사에는 대보건설을 비롯해 포스코이앤씨, 롯데건설, 서희건설 등이 참여하고 있다.")
    check("본문 리드컷 밖 — 700자 컷만이면 빠짐(회귀 재현)",
          "포스코이앤씨" in detect_group_companies(_list_body[:GROUP_LEAD_CHARS]), False)
    check("group_lead_text — 참여사 3개 이상 나열 문장은 리드 밖이어도 살아남음",
          "포스코이앤씨" in detect_group_companies(group_lead_text(_list_body)), True)

    # 실제 오탐 사례(2026-09-10 ②, 위 ①의 1차 수정이 냈던 회귀): '포스코 노사 파업'
    # 기사(본문 1,030자)의 873번째 인용문 "이제 공은 포스코홀딩스 장인화 회장에게
    # 넘어갔다"— 단발 언급(나열 아님)이라 detect_group_companies 의 `if not found` 로
    # 주체 '포스코' 가 밀려났다. '기사가 짧으면 통째로 본다'로 고쳤다가 이 사례가
    # 다시 터졌다 — 그래서 나열 패턴(쉼표 3개 이상)만 골라 살리는 방식으로 바꿨다.
    _quote_body = ("포스코 노사가 임금협상 이견을 좁히지 못했다. " + "ㅁ" * 800
                  + " 노조 위원장은 이제 공은 포스코홀딩스 장인화 회장에게 넘어갔다고 말했다.")
    check("group_lead_text — 인용문 속 단발 언급은 안 살아남음(회귀 방지)",
          detect_group_companies(group_lead_text(_quote_body)), ["포스코"])

    # 실제 오탐 사례(2026-09-10 ③): QuINSA 총회 기사, 본문 1,715자 중 800번째
    # "IQM, 포스코홀딩스, SDT, 노르마, 연세대 등이 … 표준화 과제를 제안했다" 가
    # 예전엔 동사 화이트리스트에 '제안'이 없어서 빠졌다. 나열문에 쓰이는 동사는
    # 제안·발표·수상·선정·체결 등 사실상 무한해 동사 조건 자체를 뺐다 — 쉼표 3개
    # 이상 나열이라는 구조 신호만으로 충분히 안전하다(②가 이미 이걸 증명한다).
    _propose_body = ("ㅁ" * 800 + " 양자컴퓨팅 분야에서는 IQM, 포스코홀딩스, SDT, 노르마,"
                     " 연세대 등이 신규 표준화 과제를 제안했다.")
    check("group_lead_text — '제안했다'처럼 화이트리스트 밖 동사도 나열 구조면 살아남음",
          "포스코홀딩스" in detect_group_companies(group_lead_text(_propose_body)), True)

    # 실제 오탐 사례(2026-09-10 ④, ③의 수정이 냈던 회귀): 동사 조건을 뺀 순수
    # '쉼표 3개' 조건만으로는 서술 문장도 걸린다. 같은 '포스코 파업' 기사의
    # "2022년 출범한 지주회사인 포스코홀딩스로 배당금, 브랜드사용료 등이 상당액
    # 들어가면서, 포스코 영업이익이 …" 가 쉼표 3개짜리 문장이라 나열문으로
    # 오인돼 주체가 다시 '포스코홀딩스'로 뒤바뀌었다. 항목이 짧고 조사·어미 없이
    # 끝나야 '진짜 나열' 이라는 조건(_is_company_list_sentence)으로 막는다.
    _narrative_comma_body = (
        "포스코 노사가 임금협상 이견을 좁히지 못했다. " + "ㅁ" * 700
        + " 2022년 출범한 지주회사인 포스코홀딩스로 배당금, 브랜드사용료 등이"
          " 상당액 들어가면서, 포스코 영업이익이 계속 줄어드는 구조적 문제가 크다.")
    check("group_lead_text — 서술문 속 쉼표 3개는 나열 아님(회귀 방지)",
          detect_group_companies(group_lead_text(_narrative_comma_body)), ["포스코"])
    check("group_lead_text — 긴 기사(원래 사례)는 여전히 700자로 잘라 주체 유지",
          detect_group_companies(group_lead_text(_lead + "\n" + _tail)), ["포스코"])

    # 실제 오탐 사례(2026-09-16): e스포츠 대회 후원사 명단에 '포스코'가 있어
    # 무관 기사(LCK 결승전 소식)가 그룹사로 걸렸다. "후원(하고 있는) A, B, C가
    # 참여" 형태는 나열문 조건은 만족하지만 실제 사업 소식이 아니므로 뺀다.
    _sponsor_body = (
        "ㅁ" * 700 + " 현장에는 LCK를 후원하고 있는 우리은행, 치지직, 업비트, 포스코,"
        " 카스, JW중외제약, 골든듀, 로지텍G와 국가보훈부가 참여해 다양한 이벤트를 펼친다.")
    check("group_lead_text — 제3자 행사 후원사 나열은 그룹사로 안 침",
          detect_group_companies(group_lead_text(_sponsor_body)), [])

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

    print("\n[6-1] 마스터 패널 중요도 규칙 재정의 — overrides · custom_rules")
    check("override 없으면 기존과 동일",
          score_article("포스코퓨처엠 실적", "", [], 3),
          score_article("포스코퓨처엠 실적", "", [], 3, {}, []))
    check("override 로 점수 수정 반영",
          score_article("포스코퓨처엠 실적", "", [], 3, {"futurem_title": {"points": 5, "enabled": True}}, []),
          5)
    check("override enabled=False 는 0점(사실상 삭제)",
          score_article("포스코퓨처엠 실적", "", [], 3, {"futurem_title": {"points": 999, "enabled": False}}, []),
          0)
    _custom = [{"keywords": ["청약"], "scope": "title", "points": 30}]
    check("사용자 추가 항목 — 제목에 키워드 있으면 가점",
          score_article("아파트 청약 경쟁률", "분양시장", [], 3, {}, _custom), 30)
    check("사용자 추가 항목 — 키워드 없으면 미적용",
          score_article("아파트 매매 동향", "분양시장", [], 3, {}, _custom), 0)
    _custom_body = [{"keywords": ["단독"], "scope": "title_or_body", "points": -10}]
    check("사용자 추가 항목 — title_or_body 는 본문도 검사(음수 감점)",
          score_article("포스코퓨처엠 실적", "단독 취재", ["포스코퓨처엠"], 3, {}, _custom_body),
          50 - 10)

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
    check("'이름 인턴기자'도 이름을 잡는다(수식어가 이름과 '기자' 사이에 붙는 경우)",
          extract_author("", "(PPSS 이승민 인턴기자) 현대엔지니어링이...", "PPSS"), "이승민")
    check("'이름 수습기자'도 마찬가지", extract_author("", "김철수 수습기자가 보도했다", "한국일보"), "김철수")
    # 실사례(2026-09-10, PPSS): 화면에 안 보이는 HTML 주석 속 '관련기사' 위젯에 이
    # 기사와 무관한 다른 기사 3건의 '권미나 기자' 서명이 남아 있어, 실제 기자
    # '이승민 인턴기자'(본문 1회)보다 더 많이 잡혀 최빈값으로 잘못 채택됐다.
    check("HTML 주석 속 무관한 기자명은 무시(회귀 방지)",
          extract_author(
              "<!-- (PPSS 권미나 기자) 관련기사1 --><!-- (PPSS 권미나 기자) 관련기사2 -->"
              "<!-- (PPSS 권미나 기자) 관련기사3 -->",
              "(PPSS 이승민 인턴기자) 현대엔지니어링이 발전소를 수주했다.", "PPSS"),
          "이승민")

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
    check("쉼표는 구분자 아님 — 기사 제목 오인 방지('중국, 리튬…')",
          clean_site_name("중국, 리튬 배터리 소식"), "중국, 리튬 배터리 소식")
    check("SEED_PRESS 신규 매핑(KPI뉴스)", SEED_PRESS.get("kpinews.kr", ("", 0))[0], "KPI뉴스")

    # site_name_from_html 은 <title> 을 절대 안 본다 (2026-09-10) — 개별 기사 페이지의
    # <title>이 '차세대 배터리 기술 한눈에'처럼 그냥 기사 제목인 경우가 흔해서, 한때
    # <title> 폴백을 넣었다가 이런 기사 제목을 매체명으로 오인하는 회귀를 냈다.
    # <title> 기반 추정은 _homepage_site_name(홈페이지 전용)에서만 한다.
    check("og:site_name 없으면 <title> 이 있어도 그냥 빈 문자열",
          site_name_from_html("<title>스카이데일리 - 뉴 패러다임을 선도하는 종합일간지</title>"), "")
    check("기사 제목이 짧아도 <title> 은 안 봄(회귀 재현 방지)",
          site_name_from_html("<title>차세대 배터리 기술 한눈에</title>"), "")
    check("og:site_name 은 여전히 정상 추출", site_name_from_html(
        '<meta property="og:site_name" content="정식매체명"/><title>딴 내용</title>'), "정식매체명")
    check("SEED_PRESS 신규 매핑(여성소비자신문)", SEED_PRESS.get("wsobi.com", ("", 0))[0], "여성소비자신문")
    check("SEED_PRESS ppss.kr 은 PPSS(초성 'ㅍㅍㅅㅅ' 아님, 사용자 지정)",
          SEED_PRESS.get("ppss.kr", ("", 0))[0], "PPSS")

    # resolve_press — 신규 매체는 기사 페이지에 매체명이 없으면 홈페이지를 한 번 더 본다
    # (2026-09-10, 필터 칩에 도메인 그대로 노출되던 문제의 실제 원인)
    class _FakeResp:
        def __init__(self, body: str):
            self.content = body.encode("utf-8")
            self.encoding = "utf-8"

        def raise_for_status(self):
            pass

    class _FakeHttp:
        def get(self, url, **kw):
            return _FakeResp("<title>스카이데일리 - 뉴 패러다임을 선도하는 종합일간지</title>")

    import tempfile as _tf1
    import shutil as _sh1
    _pdir = _tf1.mkdtemp()
    _pstore = SqliteStorage(os.path.join(_pdir, "press.db"))
    _pstore.init_schema()
    _name, _pid, _tier = resolve_press(_pstore, "https://skyedaily.com/news_view.html?ID=1",
                                       "", "<title>차세대 배터리 기술 한눈에</title>", _FakeHttp())
    check("신규 매체 — 기사 페이지에 매체명 없으면 홈페이지 재조회로 복구", _name, "스카이데일리")
    check("두 번째 호출은 이미 저장된 이름을 그대로 씀(재조회 안 함)",
          resolve_press(_pstore, "https://skyedaily.com/news_view.html?ID=2", "", "", None)[0],
          "스카이데일리")

    # ── 언론사 조회 캐시 (2026-09-22) ────────────────────────────────
    # 기사 1건마다 press_by_domain 을 부르는데 Supabase 에선 그대로 HTTP 왕복이다.
    # 다만 이름이 아직 도메인 그대로인 행을 캐시해 버리면 '언론사명이 도메인
    # 그대로 굳어버리는' 예전 버그가 되살아난다 — 그래서 확정된 이름만 기억한다.
    _pbd_hits = {"n": 0}
    _real_pbd = _pstore.press_by_domain

    def _counting_pbd(domain, _f=_real_pbd):
        _pbd_hits["n"] += 1
        return _f(domain)

    _pstore.press_by_domain = _counting_pbd
    _pbd_hits["n"] = 0
    resolve_press(_pstore, "https://skyedaily.com/news_view.html?ID=3", "", "", None)
    check("이름이 확정된 매체는 DB 를 다시 읽지 않는다", _pbd_hits["n"], 0)

    _pstore.press_by_domain = _real_pbd          # 셋업 중 내부 호출은 세지 않는다
    _pstore.upsert_press("testpress.kr", "testpress.kr", 3, "pending")
    _pstore.press_by_domain = _counting_pbd
    _pbd_hits["n"] = 0
    resolve_press(_pstore, "https://testpress.kr/a", "", "", None)
    resolve_press(_pstore, "https://testpress.kr/b", "", "", None)
    check("이름이 도메인 그대로면 캐시하지 않고 매번 다시 읽는다", _pbd_hits["n"], 2)

    _pstore.press_by_domain = _real_pbd
    _pstore.update_press_name("testpress.kr", "테스트신문", 3)
    press_memo_forget(_pstore, "testpress.kr")
    _pstore.press_by_domain = _counting_pbd
    _pbd_hits["n"] = 0
    check("이름 정정 후 한 번만 다시 읽고 그 뒤로는 캐시",
          (resolve_press(_pstore, "https://testpress.kr/c", "", "", None)[0],
           resolve_press(_pstore, "https://testpress.kr/d", "", "", None)[0],
           _pbd_hits["n"]), ("테스트신문", "테스트신문", 1))
    _pstore.press_by_domain = _real_pbd

    # tier 캐시 — 같은 press_id 를 두 번 물어도 DB 는 한 번만 읽는다.
    _tier_hits = {"n": 0}
    _real_tier = _pstore.press_tier_by_id

    def _counting_tier(pid, _f=_real_tier):
        _tier_hits["n"] += 1
        return _f(pid)

    _tp_id = (_pstore.press_by_domain("testpress.kr") or {}).get("id")
    _pstore.press_tier_by_id = _counting_tier
    check("행을 캐시할 때 tier 도 같이 담아 둔다 — tier 조회는 DB 를 안 친다",
          (press_tier_cached(_pstore, _tp_id), _tier_hits["n"]), (3, 0))
    press_memo_forget(_pstore, "testpress.kr", _tp_id)
    _tier_hits["n"] = 0
    check("캐시를 버린 뒤엔 첫 조회만 DB, 두 번째는 캐시",
          (press_tier_cached(_pstore, _tp_id), press_tier_cached(_pstore, _tp_id),
           _tier_hits["n"]), (3, 3, 1))
    check("press_tier_cached — id 가 없으면 DB 안 치고 기본 tier 3",
          (press_tier_cached(_pstore, None), _tier_hits["n"]), (3, 1))
    _pstore.press_tier_by_id = _real_tier

    # ── 도메인 모양 언론사명 교정 (2026-09-29 사용자 지적: 'mstoday.co.kr') ──
    check("표시 단계 매핑 — 도메인 이름이면 매핑표 이름으로",
          press_display_name("mstoday.co.kr"), "MS투데이")
    check("표시 단계 매핑 — www·서브도메인도 접어서 찾는다",
          press_display_name("", "https://www.mstoday.co.kr/news/1"), "MS투데이")
    check("표시 단계 매핑 — 이미 한글 이름이면 그대로",
          press_display_name("한국경제", "https://www.mstoday.co.kr/x"), "한국경제")
    check("표시 단계 매핑 — 매핑 없는 도메인은 그대로(지어내지 않음)",
          press_display_name("unknown-press.kr"), "unknown-press.kr")
    for _aid, _pn, _url in (("fp-1", "mstoday.co.kr", "https://www.mstoday.co.kr/a"),
                            ("fp-2", "", "https://mstoday.co.kr/b"),
                            ("fp-3", "unknown-press.kr", "https://unknown-press.kr/c"),
                            ("fp-4", "한국경제", "https://hankyung.com/d")):
        _pstore._exec(
            "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
            " collected_at, source_type, press_name, status, is_representative)"
            " values (?,?,?,?,?,?,?,?,?,?,?)",
            (_aid, _url, _url, _url, "t", iso(now_utc()), iso(now_utc()), "rss", _pn, "active", 1))
    _fpctx = type("C", (), {"storage": _pstore})()
    _fx, _um = fix_domain_press_names(_fpctx, dry=True)
    check("fixpress --dry — 고칠 대상만 세고(2건) 매핑 없는 도메인을 보고한다",
          (_fx, _um), (2, {"unknown-press.kr": 1}))
    check("fixpress --dry — 실제로는 아무것도 안 바뀐다",
          _pstore._one("select press_name from articles where id='fp-1'")["press_name"], "mstoday.co.kr")
    fix_domain_press_names(_fpctx, dry=False)
    check("fixpress — press_id 없는 기사도 도메인으로 찾아 고친다",
          [_pstore._one("select press_name from articles where id=?", (i,))["press_name"]
           for i in ("fp-1", "fp-2", "fp-3", "fp-4")],
          ["MS투데이", "MS투데이", "unknown-press.kr", "한국경제"])

    class _FakeHttpComma:
        """홈페이지 title 이 '매체명 - 슬로건' 도 아니고 og:site_name 도 없어서
        _homepage_site_name 의 쉼표 최후 수단까지 가는 실사례(weeklytrade.co.kr)."""
        def get(self, url, **kw):
            return _FakeResp("<title>한국무역신문, 주간무역, 한국무역의 길잡이 한국무역신문</title>")

    _name2, _, _ = resolve_press(_pstore, "https://weeklytrade.co.kr/news/view.html?no=1",
                                 "", "<title>중국, 리튬 배터리 소식 :: 한국무역신문</title>", _FakeHttpComma())
    check("홈페이지 title 이 쉼표로 슬로건을 늘어놓아도 첫 조각(매체명) 복구", _name2, "한국무역신문")

    # _title_media_name — 매체명이 맨 앞(스카이데일리류)과 맨 뒤(이코노미조선류) 둘 다
    # 실사례가 있다(2026-09-10). 실패하면 빈 문자열.
    check("매체명이 맨 앞", _title_media_name("스카이데일리 - 뉴 패러다임을 선도하는 종합일간지"),
          "스카이데일리")
    check("매체명이 맨 뒤(조선비즈가 만드는... - 이코노미조선)",
          _title_media_name("조선비즈가 만드는 프리미엄 경제 주간지 - 이코노미조선"), "이코노미조선")
    check("양쪽 다 실패하면 빈 문자열",
          _title_media_name("영문뿐인 사이트 이름은 아니지만 앞뒤 다 너무 길게 늘어진 슬로건" * 2), "")

    class _FakeHttpHostOrder:
        """등록 도메인(tvchosun.com)은 리다이렉트 스텁만 주고, 기사가 실제로 있던
        서브도메인(news.tvchosun.com)에 진짜 매체명이 있는 실사례."""
        def get(self, url, **kw):
            if "news.tvchosun.com" in url:
                return _FakeResp("<title>TV조선뉴스</title>")
            return _FakeResp("")  # 등록 도메인은 빈 페이지(리다이렉트 스텁)

    check("서브도메인이 접힌 등록 도메인이 스텁이어도, 기사가 있던 호스트를 먼저 시도",
          _homepage_site_name(_FakeHttpHostOrder(), "tvchosun.com", "news.tvchosun.com"),
          "TV조선뉴스")

    class _FakeHttpHttpsFail:
        """https 전체가 SSLError 로 막혀 있고 http 로만 되는 실사례(areyou.co.kr)."""
        def get(self, url, **kw):
            if url.startswith("https://"):
                raise Exception("SSLError: certificate verify failed")
            return _FakeResp("<title>AU경제</title>")

    check("https 가 SSLError 면 http 로 재시도",
          _homepage_site_name(_FakeHttpHttpsFail(), "areyou.co.kr"), "AU경제")
    _sh1.rmtree(_pdir, ignore_errors=True)

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
    # 회귀 방지(2026-09-28, 사용자 지정): 부고는 항상
    # '대상명/관계 → 상주 → 빈소 → 발인 → 연락처' 순서로 정리하고, 연락처는
    # (전화번호가 두 개 이상이어도) 절대 잘리지 않는다.
    _ob = ("김 기자 구독 구독중 이전 다음 ▲ 김철수(향년 80세)씨 별세, 김영희씨 부친상 "
           "= 8일 오전, 서울대병원, 발인 10일. ☎ 02-1234-5678 (서울=연합뉴스) 무단 전재 금지")
    check("부고 요약 — 대상/관계 → 빈소 → 발인 → 연락처 순으로 정리",
          format_people_notice(_ob, "obituary"),
          "ㆍ김철수(향년 80세)씨 별세 / 김영희씨 부친상\n"
          "   · 빈소: 8일 오전, 서울대병원\n"
          "   · 발인 10일\n"
          "   · ☎ 02-1234-5678")
    _ob_full = ("▲ 홍길동(향년 85세)씨 별세, 아들 홍민수씨 부친상 = 상주 홍민수, "
                "빈소 서울성모병원 3호실, 발인 12일 오전 7시, 장지 하늘공원 "
                "☎ 010-1234-5678, 010-8765-4321")
    check("부고 요약 — 상주까지 있으면 5개 항목 전부, 연락처 2개 다 안 잘림",
          format_people_notice(_ob_full, "obituary"),
          "ㆍ홍길동(향년 85세)씨 별세 / 아들 홍민수씨 부친상\n"
          "   · 상주: 홍민수\n"
          "   · 빈소: 서울성모병원 3호실\n"
          "   · 발인 12일 오전 7시 / 장지 하늘공원\n"
          "   · ☎ 010-1234-5678, 010-8765-4321")
    # 회귀 방지(2026-09-28): 한 기사에 여러 부고가 있으면 첫 번째 전화번호에서
    # 끊기지 않고 전부 남는다 — 예전엔 두 번째 이후가 통째로 사라졌다.
    _ob2 = ("김 기자 구독 구독중 이전 다음 ▲ 김철수씨 별세 = 발인 10일 ☎ 02-1111-2222 "
            "▲ 이영희씨 별세 = 발인 11일 ☎ 02-3333-4444 (서울=연합뉴스) 무단 전재 금지")
    check("부고 2건 — 첫 번째에서 끊기지 않고 둘 다 남는다",
          format_people_notice(_ob2, "obituary"),
          "ㆍ김철수씨 별세\n   · 발인 10일\n   · ☎ 02-1111-2222\n\n"
          "ㆍ이영희씨 별세\n   · 발인 11일\n   · ☎ 02-3333-4444")
    # _join_capped — 한도를 넘으면 블록을 통째로 빼지, 블록 중간(=대개 연락처)에서
    # 자르지 않는다. 블록 3개(각 624자)를 1600자 한도에 넣으면 2개만 들어간다.
    _ob3 = ["대상" + c * 600 + f"\n   · ☎ 010-{n}-{n}"
            for c, n in (("X", "1111"), ("Y", "2222"), ("Z", "3333"))]
    _capped3 = _join_capped(_ob3, "\n\n", 1600)
    check("_join_capped — 넘치기 직전까지만 통째로 담고 마지막 줄이 안 잘린다",
          _capped3.endswith("☎ 010-2222-2222"), True)
    check("_join_capped — 한도를 넘는 블록은 아예 통째로 뺀다(중간에서 안 자름)",
          "3333" in _capped3, False)
    check("인사 요약 — 직책·이름을 갈라 'ㆍ직책 이름 (부서)' 로 정리",
          format_people_notice("기자 구독 구독중 이전 다음 ◇ 편집국 ▲ 산업본부장 류준형 (서울=연합뉴스)", "personnel"),
          "ㆍ류준형 · 산업본부장 (편집국)")
    # 회귀 방지(2026-09-28, 사용자 지적): LLM 예산이 없어 이 규칙 기반으로 빠지면
    # 원문을 그대로 노출해 '요약이 안 된다'는 지적이 있었다 — [인사] 프라임경제 실사례.
    _personnel_dump = (
        "◇ 부장 승진 ▲ 정치사회부/생활산업부장 김경태 ▲ 건설산업부장 전훈식 "
        "▲ 미래산업부장 노병우 ◇ 차장 승진 ▲ 생활산업부 차장 이인영 "
        "▲ 미래산업부 차장 박지혜 ▲ 자본시장부 차장 장민태")
    check("인사 다건(실사례) — 사람마다 한 줄, 헤더는 괄호로",
          format_people_notice(_personnel_dump, "personnel"),
          "ㆍ김경태 · 정치사회부/생활산업부장 (부장 승진)\n"
          "ㆍ전훈식 · 건설산업부장 (부장 승진)\n"
          "ㆍ노병우 · 미래산업부장 (부장 승진)\n"
          "ㆍ이인영 · 생활산업부 차장 (차장 승진)\n"
          "ㆍ박지혜 · 미래산업부 차장 (차장 승진)\n"
          "ㆍ장민태 · 자본시장부 차장 (차장 승진)")
    # 회귀 방지(2026-09-29, 사용자 지적): [인사] 법무부 — 신규 보임 뒤 '전보' 명단이 누락됐다.
    # 원인: LLM 20명 상한 · △ 항목 표시 미인식 · 1600자 상한. 실제 연합뉴스 기사 형식.
    _moj_new = ["법무연수원 기획부장 박진성", "대검찰청 마약·조직범죄부장 홍완희", "대검찰청 공판송무부장 안성희",
                "대검찰청 과학수사부장 장혜영", "대전고검 차장검사 정광수", "대구고검 차장검사 조아라",
                "전주지검 검사장 이정렬"]
    _moj_move = [f"서울{i}지검 검사장 김철{'수영민준서진호'[i % 6]}" for i in range(25)]
    _moj_text = ("◇ 대검검사급 신규 보임 " + " ".join("▲ " + x for x in _moj_new)
                 + " ◇ 대검검사급 전보 " + " ".join("▲ " + x for x in _moj_move) + " (서울=연합뉴스)")
    _moj_out = format_people_notice(_moj_text, "personnel").splitlines()
    check("인사 대량(검사 인사) — 신규 보임 7 + 전보 25 = 32줄, 뒤 섹션까지 전부",
          (len(_moj_out), _moj_out[0], _moj_out[-1].endswith("(대검검사급 전보)")),
          (32, "ㆍ박진성 · 법무연수원 기획부장 (대검검사급 신규 보임)", True))
    check("인사 대량 — 꼬리표 '(서울=연합뉴스)' 는 이름에 안 섞인다",
          any("연합뉴스" in ln for ln in _moj_out), False)
    _moj_tri = ("◇ 공소청 검사급 검사 신규 보임 △ 법무부 법무실장 김남훈 △ 법무연수원 기획부장 구태연 "
                "◇ 공소청 검사급 검사 전보 △ 법무부 형사사법국장 박지호 △ 서울고검 차장검사 이도윤")
    check("인사 — △ 표시 기사도 한 줄씩(전보 포함)",
          format_people_notice(_moj_tri, "personnel").splitlines(),
          ["ㆍ김남훈 · 법무부 법무실장 (공소청 검사급 검사 신규 보임)",
           "ㆍ구태연 · 법무연수원 기획부장 (공소청 검사급 검사 신규 보임)",
           "ㆍ박지호 · 법무부 형사사법국장 (공소청 검사급 검사 전보)",
           "ㆍ이도윤 · 서울고검 차장검사 (공소청 검사급 검사 전보)"])
    check("인사 — ◆ 헤더도 인식", format_people_notice("◆ 신규 ▲ 서울지검 검사장 김철수", "personnel"),
          "ㆍ김철수 · 서울지검 검사장 (신규)")

    class _NoLLM:
        model = "x"

        def people_notice(self, *a, **k):
            raise AssertionError("대량 인사는 LLM 을 부르면 안 된다")

    _bulk_ctx = type("C", (), {"llm": _NoLLM()})()
    _bs, _bm, _bu = people_summary(_bulk_ctx, "personnel", "[인사] 법무부", "연합뉴스", _moj_text, True)
    check("인사 대량 — LLM 없이 규칙 정리(모델 표시 rule), 32줄 전부",
          (_bm, len(_bs.splitlines())), (PEOPLE_RULE_MODEL, 32))
    # 배터리협회 — 계열사는 아니지만 그룹사 칩·수집 대상 (2026-09-30)
    check("배터리협회 — 정식 명칭·약칭·옛 명칭 모두 잡는다",
          [detect_group_companies(t) for t in ("한국배터리산업협회가 발표했다", "KBIA 총회", "배터리협회 세미나",
                                                "한국전지산업협회 시절")],
          [["배터리협회"]] * 4)
    # 카드 '긍정 · 22' 툴팁 — 점수 내역과 감성 근거 (2026-09-30)
    _bd = score_breakdown("포스코퓨처엠 양극재 증설", "포스코퓨처엠이 광양에 공장을 짓는다.", ["포스코퓨처엠"], 1)
    check("점수 내역 — 합계가 score_article 과 같다(설명과 계산이 갈라지지 않는다)",
          (sum(p for _, p in _bd),
           score_article("포스코퓨처엠 양극재 증설", "포스코퓨처엠이 광양에 공장을 짓는다.", ["포스코퓨처엠"], 1)),
          (sum(p for _, p in _bd), int(clamp(sum(p for _, p in _bd), 0, 100))))
    check("점수 내역 — 항목 설명과 가산점이 함께 나온다",
          any("포스코퓨처엠이 제목에" in label and p > 0 for label, p in _bd), True)
    check("점수 내역 — 꺼진 항목(0점)은 내역에서 빠진다",
          any("제목에" in label for label, _ in score_breakdown(
              "포스코퓨처엠 소식", "", [], 3, {"futurem_title": {"enabled": False}})), False)
    check("감성 근거 — 머리말로 저장하고 읽을 때 꺼낸다",
          (sentiment_reason_of({"perspective_text": SENTIMENT_REASON_TAG + "증설 수주를 호의적으로 보도"}),
           sentiment_reason_of({"perspective_text": "옛 포스코 관점 문장"}), sentiment_reason_of({})),
          ("증설 수주를 호의적으로 보도", "", ""))
    check("감성 근거 — 분석 JSON 의 sentiment_reason 을 읽는다",
          _build_analysis({"summary": ["a"], "sentiment": "긍정", "sentiment_reason": "  수주  확대 ",
                           "swot": {}}, {}).sentiment_reason, "수주 확대")
    check("과거 기사(저장된 그룹사 있음)도 제목·요약에 협회가 나오면 배터리협회 칩이 붙는다",
          card_tags({"title": "포스코퓨처엠 양극재 증설", "summary_text": "한국배터리산업협회는 환영했다",
                     "group_companies": '["포스코퓨처엠"]', "categories": "[]", "keywords": "[]"})[0],
          ["포스코퓨처엠", "배터리협회"])
    check("배터리협회 + 포스코 — 포스코 태그가 사라지지 않는다",
          (detect_group_companies("포스코와 배터리협회가 협약"), normalize_group_list(["배터리협회", "포스코"])),
          (["배터리협회", "포스코"], ["배터리협회", "포스코"]))
    check("배터리협회 + 포스코퓨처엠 — 상위 '포스코' 는 여전히 빠진다",
          normalize_group_list(["배터리협회", "포스코퓨처엠", "포스코"]), ["배터리협회", "포스코퓨처엠"])
    check("네이버 그룹사 키워드 — 협회 기사는 포스코 언급이 없어도 통과",
          _naver_item_relevant("배터리협회, 배터리 안전 세미나 개최", "그룹사", "배터리협회", ""), True)
    check("직함만 있고 이름이 없으면(끝 토큰이 흔한 직함) 나누지 않는다",
          format_people_notice("◇ 인사 ▲ 정치부 부장", "personnel"),
          "ㆍ정치부 부장 (인사)")
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
    # 고정밀 신호(회사명·조치명)는 제목이 아니라 리드(스니펫)에 있어도 인정한다.
    #   실제 누락 사례: 제목이 사업을 안 드러내는 기사가 통째로 버려졌다.
    _t모호 = "기장공장 부지 활용 방안 이사회 의결…상폐 돌파구"
    check("제목·리드 모두 신호 없으면 탈락",
          _naver_item_relevant(_t모호, "산업", ""), False)
    check("리드에 배터리 회사명 있으면 통과",
          _naver_item_relevant(_t모호, "산업", "", "금양이 배터리 사업 부진으로 상장폐지 기로에"), True)
    check("제목에 배터리 회사명이 있으면 리드 없이도 통과",
          _naver_item_relevant("금양, 기장공장을 데이터센터로 물적분할 추진", "산업", ""), True)
    check("리드에 통상 조치명 있으면 통과",
          _naver_item_relevant("산업장관, 통상현안 간담회", "통상", "",
                               "EU 산업가속화법의 역내산 요건을 논의했다"), True)
    check("리드가 있어도 '그룹사' 검색은 포스코 없으면 탈락",
          _naver_item_relevant("금양 신공장", "그룹사", "", "금양이 배터리 공장을"), False)
    # 짧은 회사명의 지명·학교명 오탐 가드
    check("battery_company_hit — 회사 언급", battery_company_hit("금양이 배터리 신공장을 짓는다"), True)
    check("battery_company_hit — 지명만이면 아님", battery_company_hit("금양읍 도시계획 변경 고시"), False)
    check("battery_company_hit — 지명+회사 함께면 인정",
          battery_company_hit("금양읍에 금양이 공장을 짓는다"), True)
    check("battery_company_hit — 천보산은 회사 아님", battery_company_hit("천보산 등산로 정비"), False)
    check("지명 오탐은 본문 게이트도 통과 못 함",
          is_battery_scope("부산 기장군 금양읍 도시계획", "기장군은 금양읍 일대를"), False)

    print("\n[8-2b] 정책브리핑(korea.kr) 수집")
    check("정책브리핑 URL 판정",
          bool(KOREA_KR_NEWS_RE.search("https://www.korea.kr/news/policyNewsView.do?newsId=1")), True)
    check("생활정보(gonggam) 는 정책브리핑 아님",
          bool(KOREA_KR_NEWS_RE.search("https://gonggam.korea.kr/newsView.do?newsId=1")), False)
    check("포스코 산업 관련 정책이면 수집",
          matches_keywords("내년 전기차 보조금 역대 최대 첨단산업 전력 용수 공급 1.9조", POLICY_RELEVANCE_KW), True)
    check("농업·복지 정책은 제외",
          matches_keywords("쌀 직불금 인상 농가 소득 안정", POLICY_RELEVANCE_KW), False)
    # 실제 오탐 사례(2026-09-10): 포항시가 '한국에너지기술평가원' 유치전에 나선
    # 기사가 '에너지' 단독 키워드에 걸려 정책 관련 기사로 잘못 수집됐다. 기관명·
    # 매체명에 흔히 섞이는 '에너지' 단독은 빼고 뚜렷한 복합어만 남겼다.
    check("기관명에 섞인 '에너지'는 정책 관련성 아님(회귀 방지)",
          matches_keywords("포항시, 2차 공공기관 이전 앞두고 한국에너지기술평가원 등 유치전 본격화",
                           POLICY_RELEVANCE_KW), False)
    check("'에너지 정책' 처럼 뚜렷한 복합어는 여전히 인정",
          matches_keywords("정부, 에너지 정책 방향 새로 발표", POLICY_RELEVANCE_KW), True)
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
    # EU 산업·탈탄소 입법 (2026 누락 사례) — 조치명은 제목, 산업어는 본문에 있어도 인정
    check("EU 산업가속화법 + 본문 철강 → 통상환경",
          is_trade_topic("산업장관, EU 산업가속화법 우려 전달",
                         "역내산 요건과 저탄소 기준이 한국 철강·배터리 기업에"), True)
    check("탄소중립산업법도 조치명으로 인정",
          is_trade_topic("EU 탄소중립산업법 시행", "배터리 업계 대응 분주"), True)
    check("조치명이 본문에만 있으면 여전히 아님",
          is_trade_topic("정부, 통상 현안 점검회의", "EU 산업가속화법과 철강 영향을 논의"), False)
    check("통상환경 카테고리 태그로 판정",
          is_trade_article({"categories": ["글로벌 통상환경"]}), True)
    check("detect_categories: 제목에 조치명 있을 때만 태깅",
          "글로벌 통상환경" in detect_categories("美 무역확장법 232조 철강 관세 부과"), True)
    check("detect_categories: 요약에만 조치명이면 태깅 안 함",
          "글로벌 통상환경" in detect_categories("포스코 실적 회복세", "CBAM 대응 비용이 변수"), False)

    # 2026-09-30: '트럼프 대미투자' 같은 한미 관세협상·투자 기사가 수집되지 않아 일일이 수동 등록했다
    print("\n[8-2c2] 한미 관세협상·대미투자 — 업종이 제목에 없어도 통상환경")
    for _t in ("트럼프, 대미투자 압박 강화…韓 3500억달러 이행 촉구", "한미 관세협상 타결 임박", "트럼프 관세 25%로 인상 예고",
               "자동차 관세 인하 합의", "상호관세 재협상 돌입"):
        check(f"통상환경(업종 없이 통과): {_t[:16]}", is_trade_topic(_t), True)
    check("거시 통상어 기사는 카테고리도 글로벌 통상환경",
          "글로벌 통상환경" in detect_categories("트럼프, 대미투자 압박 강화"), True)
    check("301조처럼 업종이 필요한 조치어는 여전히 산업어가 있어야 한다",
          is_trade_topic("美, 對中 반도체 301조 관세"), False)
    check("통상 키워드 검색 결과 — 제목에 '대미투자'만 있어도 통과",
          _naver_item_relevant("트럼프 '韓 대미투자 약속 이행하라'", "통상", "트럼프 관세"), True)
    check("무관 기사(코스피)는 여전히 제외", is_trade_topic("코스피 7100선 돌파"), False)
    check("통상 키워드 시드에 한미 관세협상 계열이 있다",
          {"대미투자", "트럼프 관세", "한미 관세협상"} <= {k for c, k in SEED_KEYWORDS if c == "통상"}, True)

    # 2026-09-30: 연합뉴스 'English News Desk' 가 기자로 집계되고, 언급 없는 기사가 포스코퓨처엠으로 잡혔다
    print("\n[8-2c4] 기자 아닌 바이라인 · 포스코퓨처엠 태그 근거 확인")
    check("바이라인 — 데스크·부서·통신사명은 사람이 아니다",
          [is_non_person_byline(x) for x in ("English News Desk", "편집국", "뉴스룸", "취재팀", "Yonhap News Agency")],
          [True] * 5)
    check("바이라인 — 사람 이름은 그대로(영문 이름 포함)",
          [is_non_person_byline(x) for x in ("김철수", "김철수 기자", "John Smith", "Kim Min-su")], [False] * 4)
    check("기자 집계 — English News Desk 는 기자에서 빠진다",
          (split_authors("English News Desk"), split_authors("김철수 기자, English News Desk"),
           split_authors("John Smith")), ([], ["김철수"], ["John Smith"]))
    check("카드 머리표 — 부서·데스크뿐이면 비우고, 사람이 있으면 이름만 남긴다",
          (author_display("English News Desk"), author_display("김철수 기자"),
           author_display("김철수, 편집국")), ("", "김철수", "김철수"))
    # 2026-10-01 점검 — 소속·직함·이메일·라벨이 붙은 바이라인, 서브도메인 매체
    check("기자 정규화 — 소속(매체·부서)이 앞에 붙어도 사람 이름만",
          [split_authors(x) for x in ("연합뉴스 김철수 기자", "서울=연합뉴스 김철수 기자", "이데일리 김철수",
                                      "뉴스1 김철수 기자", "산업부 김철수 기자", "김철수 (서울=연합뉴스)")],
          [["김철수"]] * 6)
    check("기자 정규화 — 통신사·라벨·괄호 매체명은 기자가 아니다",
          [split_authors(x) for x in ("Reuters", "AFP=연합뉴스", "(주)한국경제", "취재팀", "특별취재팀")], [[]] * 5)
    check("기자 정규화 — 공동 바이라인의 '사진' 라벨은 사람이 아니다",
          split_authors("김철수 기자·사진 이영희"), ["김철수", "이영희"])
    check("기자 정규화 — 직함 붙은 값·이메일도 이름만(추출 단계)",
          [_valid_author(x, normalize_chip("연합뉴스")) for x in ("김철수 선임기자", "산업부 김철수", "취재팀",
                                                              "김철수 기자 chulsoo@x.com")],
          ["김철수", "김철수", "", "김철수"])
    check("서브도메인 매체 — biz.chosun.com 은 조선비즈, www.chosun.com 은 조선일보",
          (press_domain_of("https://biz.chosun.com/a"), press_domain_of("https://news.chosun.com/a"),
           press_display_name("조선일보", "https://biz.chosun.com/a"),
           press_display_name("조선일보", "https://www.chosun.com/a")),
          ("biz.chosun.com", "chosun.com", "조선비즈", "조선일보"))
    check("서브도메인 매체 — SBS Biz·뉴데일리경제도 모회사와 구분",
          (press_display_name("SBS", "https://biz.sbs.co.kr/x"), press_display_name("SBS", "https://news.sbs.co.kr/x"),
           press_display_name("", "https://biz.newdaily.co.kr/x")), ("SBS Biz", "SBS", "뉴데일리경제"))
    check("기자 추출 — JSON-LD author 가 데스크명이면 채택하지 않는다",
          extract_author('<script type="application/ld+json">{"author":{"@type":"Person","name":"English News Desk"}}</script>',
                         "", "연합뉴스"), "")
    check("언급 근거 — 발췌 있음/제목에 이름/발췌 NULL 은 인정",
          [pfm_mention_supported(r) for r in (
              {"pfm_excerpt": "포스코퓨처엠이 증설한다.", "title": "배터리"},
              {"pfm_excerpt": "", "title": "포스코퓨처엠 신공장"},
              {"pfm_excerpt": None, "title": "배터리"})], [True, True, True])
    check("언급 근거 — 발췌가 빈 값이고 제목에도 없으면 근거 없음",
          pfm_mention_supported({"pfm_excerpt": "", "title": "코스피 상승 마감"}), False)
    _pn = now_utc()
    _sup = aggregate_press_stats([
        {"id": "s1", "title": "코스피 상승 마감", "press_name": "연합뉴스", "pfm_excerpt": "",
         "url_canonical": "https://yna.co.kr/1", "author": "English News Desk",
         "published_at": iso(_pn - timedelta(days=1))},
        {"id": "s2", "title": "포스코퓨처엠 증설", "press_name": "연합뉴스", "pfm_excerpt": "포스코퓨처엠이 증설한다.",
         "url_canonical": "https://yna.co.kr/2", "author": "English News Desk, 김철수 기자",
         "published_at": iso(_pn - timedelta(days=2))}], _pn)
    check("언론사 탭 — 언급 근거 없는 기사는 집계에서 빠지고, 데스크는 기자가 아니다",
          (_sup[0]["year"], [x["name"] for x in _sup[0]["reporters"]]), (1, ["김철수"]))

    class _GroupStubLLM:
        """LLM 이 본문에 없는 '포스코퓨처엠'을 요약·키워드·그룹사에 지어낸 상황."""
        def analyze(self, title: str, press: str, body: str) -> Analysis:
            return Analysis(summary_sentences=["포스코퓨처엠이 양극재를 늘린다."], perspective="",
                            keywords=["포스코퓨처엠", "양극재"], group_companies=["포스코퓨처엠"],
                            sentiment="중립", ok=True)

        def pfm_tone(self, excerpt: str) -> tuple[str, str]:
            return "중립", "x"

    _gt = SqliteStorage(os.path.join(__import__("tempfile").mkdtemp(), "g.db"))
    _gt.init_schema()
    _gt._exec("insert into articles (id,url_source,url_canonical,url_original,title,published_at,collected_at,"
              "source_type,importance_score,group_companies,categories,press_name,analyzed_at,status,"
              "is_representative) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("g1", "u/g1", "u/g1", "u/g1", "배터리 업계 동향", iso(_pn), iso(_pn), "rss", 10, "[]", "[]",
               "연합뉴스", None, "active", 1))
    _gctx = Context(cfg=Config(**{**{f: "" for f in Config.__dataclass_fields__}, "openai_api_key": "x",
                                  "llm_model": "m", "embedding_model": "e", "nvidia_embed_model": "n",
                                  "nvidia_llm_model": "nvm"}), storage=_gt, http=HttpClient())
    _gctx._llm = _GroupStubLLM()
    analyze_and_save(_gctx, "g1", {"id": "g1", "title": "배터리 업계 동향", "press_id": None, "press_name": "연합뉴스",
                                   "importance_score": 10, "group_companies": []},
                     "국내 배터리 업계가 전기차 수요 둔화에 대응하고 있다. " * 10, "fulltext")
    _g1 = _gt._one("select group_companies, pfm_excerpt from articles where id='g1'")
    check("LLM 이 지어낸 포스코퓨처엠 — 원문에 없으면 태그가 붙지 않는다",
          (jload(_g1["group_companies"], []), _g1["pfm_excerpt"]), ([], ""))
    _gt._exec("update articles set group_companies='[]', analyzed_at=null where id='g1'")
    analyze_and_save(_gctx, "g1", {"id": "g1", "title": "배터리 업계 동향", "press_id": None, "press_name": "연합뉴스",
                                   "importance_score": 10, "group_companies": []},
                     "포스코퓨처엠은 양극재 증설을 발표했다. 국내 배터리 업계가 주목한다. " * 10, "fulltext")
    check("원문에 실제로 있으면 정상적으로 붙는다",
          jload(_gt._one("select group_companies from articles where id='g1'")["group_companies"], []), ["포스코퓨처엠"])

    # 2026-10-01: 포스코퓨처엠 언급 발췌가 맥락 없이 끊겨 읽힌다는 지적, 영어 기사 한글 번역
    print("\n[8-2c5] 포스코퓨처엠 언급 발췌 — 맥락 · 영어 기사 번역")
    _mp = ("정부가 이차전지 지원책을 발표했다. 업계는 대체로 환영했다.\n"
           "시장은 전반적으로 긍정적인 반응을 보였다.\n"
           "포스코퓨처엠은 광양 양극재 공장 증설을 서두르기로 했다. 회사 관계자는 내년 가동을 목표로 한다고 밝혔다.\n"
           "증권가는 실적 개선을 전망했다.\n"
           "한편 삼성SDI는 미국 공장을 늘린다.")
    _mx = extract_pfm_excerpt(_mp)
    check("발췌 맥락 — 언급 문단 바로 앞 문장이 맥락으로 앞에 붙고, 멀리 있는 첫 문단은 안 붙는다(흐름이 한 덩어리)",
          (_mx.splitlines()[0], "정부가 이차전지" in _mx), ("시장은 전반적으로 긍정적인 반응을 보였다.", False))
    check("발췌 맥락 — 언급 문단 바로 뒤에 이어지는 문장도 원문 순서대로 붙는다",
          _mx.splitlines()[-1], "증권가는 실적 개선을 전망했다.")
    check("발췌 맥락 — 언급 문단이 문단째(두 문장 모두) 들어간다",
          "광양 양극재 공장 증설을 서두르기로 했다. 회사 관계자는 내년 가동을 목표로 한다고 밝혔다." in _mx, True)
    check("발췌 맥락 — 그 뒤 문단(삼성SDI)까지는 안 붙는다", "삼성SDI" in _mx, False)
    _mh = ("광양만권, 국가첨단전략산업 특화단지로 최종 지정\n포스코퓨처엠 등 관련 선도기업을 중심으로 기업 간 연계를 강화한다.\n"
           "(뉴스메이커=이영수 기자) 다음 문단이 이어진다. 후속 내용이다.")
    check("발췌 — 헤드라인성 줄과 작성 표기((매체=기자))는 걷어 낸다",
          ("특화단지로 최종 지정" in extract_pfm_excerpt(_mh), "이영수" in extract_pfm_excerpt(_mh)), (False, False))
    _mq = ("국내 배터리 업계가 증설 경쟁을 벌이고 있다.\n원료 수급이 가장 큰 변수로 꼽힌다. 가격 변동성도 크다.\n"
           "\"포스코퓨처엠은 계획대로 간다\"고 말했다.")
    check("발췌 맥락 — 언급 문단이 짧은 인용이면 바로 앞 문장까지 붙는다", "가격 변동성도 크다." in extract_pfm_excerpt(_mq), True)
    _many = "\n".join(f"포스코퓨처엠 관련 {i}번째 소식이다. " + ("배경 설명이 이어진다. " * 8) for i in range(6))
    _me = extract_pfm_excerpt(_many)
    check("발췌 맥락 — 언급 문단은 최대 3개, 전체는 상한 이하",
          (_me.count("번째 소식") <= PFM_MENTION_PARAS, len(_me) <= PFM_EXCERPT_MAX), (True, True))
    _bigpar = "도입 문장이다. " + "".join(f"{i}번째 배경 문장이 이어진다. " for i in range(40)) \
        + "포스코퓨처엠은 증설을 발표했다. " + "".join(f"{i}번째 후속 문장이다. " for i in range(40))
    _mb = extract_pfm_excerpt(_bigpar)
    check("발췌 맥락 — 긴 문단도 문장 중간에서 자르지 않는다(모든 줄이 문장 끝으로 끝남)",
          all(ln.rstrip().endswith((".", "다.")) for ln in _mb.splitlines()), True)
    check("발췌 맥락 — 긴 문단에서도 언급 문장이 들어 있다", "포스코퓨처엠은 증설을 발표했다." in _mb, True)

    check("영어 판별 — 영문 기사/한글 기사/짧은 글",
          (looks_english("POSCO Future M to expand cathode plant in Gwangyang"),
           looks_english("포스코퓨처엠이 광양 공장을 증설한다"), looks_english("EV"),
           looks_english("POSCO퓨처엠 Q3 실적 발표")), (True, False, False, False))
    check("원제 머리말 — 감성 근거와 함께 한 칸에 담고 각각 꺼낸다",
          (sentiment_reason_of({"perspective_text": pack_perspective("수주 호재", "POSCO Future M expands")}),
           title_original_of({"perspective_text": pack_perspective("수주 호재", "POSCO Future M expands")}),
           title_original_of({"perspective_text": "옛 포스코 관점 문장"})),
          ("수주 호재", "POSCO Future M expands", ""))

    class _EnLLM:
        """영어 기사: 요약은 한국어, 번역은 제목·발췌. fail=True 면 번역 실패."""
        fail = False

        def analyze(self, title: str, press: str, body: str) -> Analysis:
            return Analysis(summary_sentences=["포스코퓨처엠이 양극재 공장을 증설한다."], perspective="",
                            keywords=["양극재"], group_companies=["포스코퓨처엠"], sentiment="긍정",
                            sentiment_reason="증설 보도", ok=True)

        def translate_to_korean(self, title: str, excerpt: str) -> tuple[str, str]:
            if _EnLLM.fail:
                return "", ""
            return ("포스코퓨처엠, 광양 양극재 공장 증설" if title else "",
                    "포스코퓨처엠은 광양 양극재 공장을 증설한다." if excerpt else "")

        def pfm_tone(self, excerpt: str) -> tuple[str, str]:
            return ("긍정", "x") if excerpt else ("", "")

        calls_reason = 0

        def sentiment_reason(self, title: str, text: str, sentiment: str) -> str:
            _EnLLM.calls_reason += 1
            return "공장 증설을 호의적으로 보도" if text else ""

    _en_body = ("POSCO Future M will expand its cathode plant in Gwangyang, the company said on Tuesday. "
                "The expansion is expected to be completed next year. ") * 4
    _et = SqliteStorage(os.path.join(__import__("tempfile").mkdtemp(), "en.db"))
    _et.init_schema()
    _ectx = Context(cfg=Config(**{**{f: "" for f in Config.__dataclass_fields__}, "openai_api_key": "x",
                                  "llm_model": "m", "embedding_model": "e", "nvidia_embed_model": "n",
                                  "nvidia_llm_model": "nvm"}), storage=_et, http=HttpClient())
    _ectx._llm = _EnLLM()
    for _eid, _etitle in (("e1", "POSCO Future M to expand cathode plant"), ("e2", "POSCO Future M to expand plant too")):
        _et._exec("insert into articles (id,url_source,url_canonical,url_original,title,published_at,collected_at,"
                  "source_type,importance_score,group_companies,categories,press_name,analyzed_at,status,"
                  "is_representative) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (_eid, "u/" + _eid, "u/" + _eid, "u/" + _eid, _etitle, iso(_pn), iso(_pn), "rss", 10, "[]", "[]",
                   "Yonhap", None, "active", 1))
    analyze_and_save(_ectx, "e1", {"id": "e1", "title": "POSCO Future M to expand cathode plant", "press_id": None,
                                   "press_name": "Yonhap", "importance_score": 10, "group_companies": []},
                     _en_body, "fulltext")
    _e1 = _et.article_detail("e1")
    _c1 = build_card(_e1)
    check("영어 기사 — 제목·발췌가 한글로 저장된다", (_e1["title"], _e1["pfm_excerpt"]),
          ("포스코퓨처엠, 광양 양극재 공장 증설", "포스코퓨처엠은 광양 양극재 공장을 증설한다."))
    check("영어 기사 — 영어 원제는 카드에 따로 실린다(감성 근거도 유지)",
          (_c1["title_original"], _c1["sentiment_reason"]), ("POSCO Future M to expand cathode plant", "증설 보도"))
    check("영어 기사 — 논조는 번역된 발췌로 판정", _e1["pfm_tone"], "긍정")
    _EnLLM.fail = True
    analyze_and_save(_ectx, "e2", {"id": "e2", "title": "POSCO Future M to expand plant too", "press_id": None,
                                   "press_name": "Yonhap", "importance_score": 10, "group_companies": []},
                     _en_body, "fulltext")
    _e2 = _et.article_detail("e2")
    check("번역 실패 — 원문 제목을 그대로 두고 원제 칸은 비운다",
          (_e2["title"], build_card(_e2)["title_original"]), ("POSCO Future M to expand plant too", ""))
    _EnLLM.fail = False
    cmd_retranslate(_ectx, 10, dry=True)
    check("retranslate --dry — 아무것도 쓰지 않는다", _et.article_detail("e2")["title"], "POSCO Future M to expand plant too")
    cmd_retranslate(_ectx, 10)
    _e2b = _et.article_detail("e2")
    check("retranslate — 영어로 남은 기사를 번역하고 원제를 남긴다",
          (_e2b["title"], title_original_of(_e2b)), ("포스코퓨처엠, 광양 양극재 공장 증설", "POSCO Future M to expand plant too"))
    # 감성 근거가 빈 옛 기사 — 화면에서 열면 한 번 만들어 저장하고, 원제 줄은 보존한다(2026-10-02)
    _et.set_perspective("e2", TITLE_ORIGINAL_TAG + "POSCO Future M to expand plant too")
    _EnLLM.calls_reason = 0
    cmd_sentireason(_ectx, 10, dry=True)
    check("sentireason --dry — AI 를 부르지 않고 아무것도 쓰지 않는다",
          (_EnLLM.calls_reason, sentiment_reason_of(_et.article_detail("e2"))), (0, ""))
    _r_now = ensure_sentiment_reason(_ectx, _et.article_detail("e2"))
    _e2c = _et.article_detail("e2")
    check("감성 근거 사후 생성 — 저장되고 원제 줄도 그대로",
          (_r_now, sentiment_reason_of(_e2c), title_original_of(_e2c)),
          ("공장 증설을 호의적으로 보도", "공장 증설을 호의적으로 보도", "POSCO Future M to expand plant too"))
    _EnLLM.calls_reason = 0
    ensure_sentiment_reason(_ectx, _e2c)
    check("이미 근거가 있으면 AI 를 다시 부르지 않는다", _EnLLM.calls_reason, 0)
    _et.set_perspective("e2", "")
    cmd_sentireason(_ectx, 10)
    check("sentireason — 근거가 빈 기사를 채운다", sentiment_reason_of(_et.article_detail("e2")), "공장 증설을 호의적으로 보도")
    check("retranslate — 번역한 기사의 감성 근거는 지워지지 않는다(e1 재실행 대상 아님)",
          sentiment_reason_of(_et.article_detail("e1")), "증설 보도")
    cmd_retranslate(_ectx, 10)
    check("retranslate — 이미 번역된 기사는 다시 하지 않는다", _et.article_detail("e1")["title"], "포스코퓨처엠, 광양 양극재 공장 증설")

    # reexcerpt — 보관 본문으로 새 방식 발췌 재생성(LLM 0)
    _et._exec("update articles set pfm_excerpt='옛 토막', group_companies='[\"포스코퓨처엠\"]' where id='e1'")
    _et.save_body("e1", _mp, "fulltext")
    cmd_reexcerpt(_ectx, dry=True)
    check("reexcerpt --dry — 쓰지 않는다", _et.article_detail("e1")["pfm_excerpt"], "옛 토막")
    cmd_reexcerpt(_ectx)
    check("reexcerpt — 보관 본문으로 문단 단위 발췌를 다시 만든다",
          ("포스코퓨처엠은 광양 양극재 공장 증설을" in _et.article_detail("e1")["pfm_excerpt"],
           "정부가 이차전지" in _et.article_detail("e1")["pfm_excerpt"]), (True, False))

    # fixexcerpt — 발췌가 비어 있는 포스코퓨처엠 기사를 점검·복구 (머니투데이 사례, 2026-10-06)
    _et._exec("insert into articles (id,url_source,url_canonical,url_original,title,published_at,collected_at,"
              "source_type,importance_score,group_companies,categories,press_name,analyzed_at,status,"
              "is_representative) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("e3", "u/e3", "https://www.mt.co.kr/x/e3", "https://www.mt.co.kr/x/e3", "포스코퓨처엠 계약", iso(_pn), iso(_pn),
               "rss", 10, '["포스코퓨처엠"]', "[]", "머니투데이", iso(_pn), "active", 1))
    _et._exec("update articles set pfm_excerpt='' where id='e3'")
    _et.save_body("e3", _mp, "fulltext")
    cmd_fixexcerpt(_ectx, "머니투데이", 10, dry=True)
    check("fixexcerpt --dry — 쓰지 않는다", _et.article_detail("e3")["pfm_excerpt"], "")
    cmd_fixexcerpt(_ectx, "없는언론사", 10)
    check("fixexcerpt — 다른 언론사 지정이면 건드리지 않는다", _et.article_detail("e3")["pfm_excerpt"], "")
    cmd_fixexcerpt(_ectx, "머니투데이", 10)
    _e3 = _et.article_detail("e3")
    check("fixexcerpt — 보관 본문으로 발췌·논조를 채운다",
          ("포스코퓨처엠은 광양 양극재 공장 증설을" in _e3["pfm_excerpt"], _e3["pfm_tone"]), (True, "긍정"))

    # 일반용 텔레그램 — 긍정·중립만, 부서용 큐와 분리 (2026-10-06)
    class _FakeTGResp:
        status_code = 200
        def json(self) -> dict:
            return {"ok": True}

    class _FakeTG:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str, str]] = []
        def post(self, url: str, **kw: Any) -> Any:
            j = kw.get("json") or {}
            self.sent.append((url, str(j.get("chat_id")), str(j.get("text"))))
            return _FakeTGResp()

    _pub_env = {k: os.environ.get(k) for k in ("TELEGRAM_PUBLIC_BOT_TOKEN", "TELEGRAM_PUBLIC_CHAT_ID")}
    for _k in _pub_env:
        os.environ.pop(_k, None)
    # 텔레그램 바로가기 — 헤더 버튼=일반용, 부서용은 마스터에서만 (2026-10-06)
    class _LinkResp:
        def __init__(self, result: dict) -> None:
            self._r = result
        def json(self) -> dict:
            return {"ok": True, "result": self._r}

    class _LinkHttp:
        def __init__(self, result: dict) -> None:
            self.result, self.urls = result, []
        def get(self, url: str, **kw: Any) -> Any:
            self.urls.append(url)
            return _LinkResp(self.result)

    _lcfg = replace(_ectx.cfg, telegram_bot_token="DEPT:T", telegram_chat_id="-1", telegram_channel_url="https://t.me/+DEPTINVITE")
    _TG_LINK_CACHE.clear()
    _lk_env = {k: os.environ.pop(k, None) for k in ("TELEGRAM_PUBLIC_BOT_TOKEN", "TELEGRAM_PUBLIC_CHAT_ID", "TELEGRAM_PUBLIC_CHANNEL_URL")}
    _lctx = Context(cfg=_lcfg, storage=_et, http=_LinkHttp({"username": "pfm_public"}))
    check("부서용 주소 — TELEGRAM_CHANNEL_URL 이 있으면 그것", dept_telegram_link(_lctx), ("https://t.me/+DEPTINVITE", "channel"))
    check("일반용 주소 — 설정이 없으면 없음(부서용 주소로 대체하지 않는다)", public_telegram_link(_lctx), ("", ""))
    os.environ["TELEGRAM_PUBLIC_BOT_TOKEN"], os.environ["TELEGRAM_PUBLIC_CHAT_ID"] = "PUB:T", "-100999"
    check("일반용 주소 — 공개 채널이면 봇이 알려 준 t.me/<채널이름>", public_telegram_link(_lctx), ("https://t.me/pfm_public", "channel"))
    check("일반용 주소 — 일반용 봇 토큰으로 조회한다(부서용 토큰 아님)", "botPUB:T/getChat" in _lctx.http.urls[0], True)
    _TG_LINK_CACHE.clear()
    _lctx2 = Context(cfg=_lcfg, storage=_et, http=_LinkHttp({"invite_link": "https://t.me/+PUBINVITE"}))
    check("일반용 주소 — 비공개 채널이면 초대 링크", public_telegram_link(_lctx2), ("https://t.me/+PUBINVITE", "channel"))
    os.environ["TELEGRAM_PUBLIC_CHANNEL_URL"] = "https://t.me/+FROMENV"
    check("일반용 주소 — TELEGRAM_PUBLIC_CHANNEL_URL 이 있으면 조회 없이 그 값", public_telegram_link(_lctx), ("https://t.me/+FROMENV", "channel"))
    for _k, _v in _lk_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    _TG_LINK_CACHE.clear()
    check("일반용 — 토큰·채널 번호가 없으면 꺼짐", public_enabled(), False)
    os.environ["TELEGRAM_PUBLIC_BOT_TOKEN"] = "PUB:TOKEN"
    check("일반용 — 토큰만 있으면 아직 꺼짐", public_enabled(), False)
    os.environ["TELEGRAM_PUBLIC_CHAT_ID"] = "-100999"
    check("일반용 — 둘 다 있으면 켜짐", public_enabled(), True)
    check("일반용 대상 — 긍정·중립 + 제목에 포스코퓨처엠이 있는 기사만",
          [is_public_candidate({"pfm_tone": t, "title": "포스코퓨처엠, 양극재 증설"}) for t in ("긍정", "중립", "부정", "", None)],
          [True, True, False, False, False])
    _pt = {"긍정": "포스코퓨처엠, 삼성SDI에 LFP 6조 공급", "띄어쓴": "포스코 퓨처엠 광양 공장 증설", "영문": "POSCO Future M to expand plant"}
    check("일반용 제목 — 포스코퓨처엠(띄어쓴 표기·영문 포함)이 제목에 있으면 통과", [public_title_ok(t) for t in _pt.values()], [True, True, True])
    _bad = ["CBC뉴스 | 화학 상장기업 브랜드평판 2026년 10월 빅데이터 분석결과…1위 에코프로·2위 LG화학",
            "에코프로, 화학 상장기업 브랜드평판 1위…LG화학·포스코퓨처엠 順",
            "코스피 하락 출발 뒤 6880선… 포스코퓨처엠 약세", "[특징주] 포스코퓨처엠, 삼성SDI 계약에 급등",
            "포스코퓨처엠 목표주가 상향…증권사 리포트", "美 중간선거 앞두고 친환경주 꿈틀…신재생·이차전지 정책 기대",
            "SK온, 포드 'Q1 어워드' 수상…최고 등급 품질 인증 획득"]
    check("일반용 제목 — 시세·순위·주가 기사와 제목에 포스코퓨처엠이 없는 기사는 제외",
          [public_title_ok(t) for t in _bad], [False] * len(_bad))
    check("일반용 대상 — 논조가 중립이어도 시세·순위 기사면 제외",
          is_public_candidate({"pfm_tone": "중립", "title": "에코프로, 화학 상장기업 브랜드평판 1위…LG화학·포스코퓨처엠 順"}), False)
    _pm = format_public_message({"title": "포스코퓨처엠 <신공장>", "summary_text": "요약문", "press_name": "머니투데이",
                                 "author": "홍길동", "importance_score": 90, "pfm_excerpt": "포스코퓨처엠은 증설한다.",
                                 "url_canonical": "https://x.test/a", "group_companies": '["포스코퓨처엠"]'})
    check("일반용 메시지 — 제목·요약·언급·링크 포함, 중요도·SWOT 같은 내부 정보 없음",
          ("&lt;신공장&gt;" in _pm, "요약문" in _pm, "포스코퓨처엠 언급" in _pm, "원문 보기" in _pm,
           "🔴" in _pm or "🟠" in _pm, "SWOT" in _pm, "중요도" in _pm), (True, True, True, True, False, False, False))
    _pn2 = now_utc()
    for _pid, _ptone, _hrs, _pex in (("pu1", "긍정", 1, "포스코퓨처엠은 LFP 양극재를 공급한다."), ("pu2", "부정", 1, "포스코퓨처엠은 적자를 냈다."),
                                     ("pu3", None, 1, "포스코퓨처엠은 투자한다."), ("pu4", "중립", 1, "포스코퓨처엠은 협약했다."),
                                     ("pu5", "긍정", 12, "포스코퓨처엠은 오래된 기사다.")):
        _et._exec("insert into articles (id,url_source,url_canonical,url_original,title,published_at,collected_at,"
                  "source_type,importance_score,group_companies,categories,press_name,analyzed_at,status,"
                  "is_representative,pfm_excerpt,pfm_tone) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (_pid, "u/" + _pid, "https://x.test/" + _pid, "https://x.test/" + _pid, "포스코퓨처엠 기사 " + _pid,
                   iso(_pn2 - timedelta(hours=_hrs)), iso(_pn2), "rss", 10, '["포스코퓨처엠"]', "[]", "연합뉴스",
                   iso(_pn2), "active", 1, _pex, _ptone))
    _PUBLIC_SEEN.clear()
    globals()["_FLOOD_UNTIL"] = 0.0      # 앞선 테스트(429)가 건 플러드 보류가 남아 있으면 발송이 막혀 결과가 흔들린다
    _SEND_TIMES.clear()
    _tg = _FakeTG()
    _pctx = Context(cfg=_ectx.cfg, storage=_et, http=_tg)
    _orig_now_local, _orig_sleep = now_local, RATE_LIMIT_SLEEP
    globals()["now_local"] = lambda: datetime(2026, 10, 6, 2, 0)      # 새벽 2시 — 야간 억제 구간
    globals()["RATE_LIMIT_SLEEP"] = 0
    _n_night = send_public_notifications(_pctx)
    check("일반용 — 야간(중요도 낮음)에는 큐에만 쌓고 보내지 않는다", (_n_night, len(_tg.sent)), (0, 0))
    globals()["now_local"] = lambda: datetime(2026, 10, 6, 12, 0)
    _n_day = send_public_notifications(_pctx)
    _texts = " ".join(t for _, _, t in _tg.sent)
    check("일반용 — 낮에는 긍정·중립만 발송(앞선 테스트의 e1·e2는 제목이 같은 사건이라 1건), 부정·미판정·오래된 기사는 제외",
          (_n_day, "pu1" in _texts, "pu4" in _texts, "pu2" in _texts, "pu3" in _texts, "pu5" in _texts),
          (4, True, True, False, False, False))
    check("일반용 — 일반용 봇 토큰·채널 번호로 나간다",
          {(u, c) for u, c, _ in _tg.sent}, {(TELEGRAM_API.format(token="PUB:TOKEN"), "-100999")})
    _n_again = send_public_notifications(_pctx)
    check("일반용 — 이미 보낸 기사는 다시 보내지 않는다(중복 방지)", (_n_again, len(_tg.sent)), (0, 4))
    _ins_calls: list[str] = []
    _orig_q = _et.queue_notification
    _et.queue_notification = lambda *a, **k: (_ins_calls.append(a[0]), _orig_q(*a, **k))[1]   # type: ignore[method-assign]
    queue_public_notifications(_pctx)
    _et.queue_notification = _orig_q   # type: ignore[method-assign]
    check("일반용 — 이미 올린 기사는 DB 중복 삽입을 다시 시도하지 않는다(캐시)", _ins_calls, [])
    _et.queue_notification("e1", "-1", "queued", 0)
    check("부서용 큐와 일반용 큐는 섞이지 않는다",
          ([r["chat_id"] for r in _et.pending_notifications(10)], [r["chat_id"] for r in _et.pending_notifications(10, channel=PUBLIC_CHANNEL)]),
          (["-1"], []))
    _et._exec("update notifications set status='sent' where chat_id='-1'")
    # 같은 사건(삼성SDI 6조 LFP 계약)을 여러 언론사가 쓴 기사 → 먼저 나온 1건만, 다른 사건(SK온)은 따로 (2026-10-06)
    _EV = ["포스코퓨처엠·삼성SDI … LFP 양극재 6조원 계약", "포스코퓨처엠, 삼성SDI와 '6조' LFP 공급계약…협력 확대",
           "LFP 6兆 '빅딜' 포스코퓨처엠, 삼성SDI 핵심 공급사로 도약", "포스코퓨처엠, SK온에 1조 LFP 양극재 공급"]
    for _i, (_eid2, _eti) in enumerate(zip(("ev1", "ev2", "ev3", "ev4"), _EV)):
        _et._exec("insert into articles (id,url_source,url_canonical,url_original,title,published_at,collected_at,"
                  "source_type,importance_score,group_companies,categories,press_name,analyzed_at,status,"
                  "is_representative,pfm_excerpt,pfm_tone) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (_eid2, "u/" + _eid2, "https://x.test/" + _eid2, "https://x.test/" + _eid2, _eti,
                   iso(_pn2 - timedelta(minutes=50 - _i * 10)), iso(_pn2), "rss", 10, '["포스코퓨처엠"]', "[]", "연합뉴스",
                   iso(_pn2), "active", 1, "포스코퓨처엠은 계약했다.", "긍정"))
    _tg.sent.clear()
    _PUBLIC_SENT_AT.clear()
    _n_ev = send_public_notifications(_pctx)
    _etx = " ".join(t for _, _, t in _tg.sent)
    check("일반용 — 같은 사건 기사는 먼저 나온 1건만(ev1), 다른 사건(ev4)은 따로 보낸다",
          (_n_ev, "6조원 계약" in _etx, "6조' LFP" in _etx, "6兆" in _etx, "SK온" in _etx), (2, True, False, False, True))
    check("일반용 — 같은 사건 판정: 삼성SDI 6조 계약 기사끼리는 같은 사건, SK온·다른 주제는 아님",
          (same_event(_EV[0], _EV[1]), same_event(_EV[0], _EV[2]), same_event(_EV[0], _EV[3]),
           same_event(_EV[0], "포스코퓨처엠 광양 음극재 공장 증설")), (True, True, False, False))
    # 시간당 상한 — 한도에 닿으면 다음 시간으로 미룬다
    _old_cap = PUBLIC_MAX_PER_HOUR
    globals()["PUBLIC_MAX_PER_HOUR"] = 1
    _et._exec("insert into articles (id,url_source,url_canonical,url_original,title,published_at,collected_at,"
              "source_type,importance_score,group_companies,categories,press_name,analyzed_at,status,"
              "is_representative,pfm_excerpt,pfm_tone) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("cap1", "u/cap1", "https://x.test/cap1", "https://x.test/cap1", "포스코퓨처엠 포항 전구체 신공장 착공식", iso(_pn2),
               iso(_pn2), "rss", 10, '["포스코퓨처엠"]', "[]", "연합뉴스", iso(_pn2), "active", 1, "포스코퓨처엠은 증설한다.", "긍정"))
    _tg.sent.clear()
    _PUBLIC_SENT_AT[:] = [time.monotonic()]            # 방금 1건 보냈다 → 한도 1건 소진
    check("일반용 — 시간당 상한에 닿으면 이번엔 보내지 않는다", (send_public_notifications(_pctx), len(_tg.sent)), (0, 0))
    _PUBLIC_SENT_AT.clear()
    _r_cap = (send_public_notifications(_pctx), len(_tg.sent))
    check("일반용 — 상한이 풀리면(1시간 지나면) 다시 보낸다", _r_cap, (1, 1))
    globals()["PUBLIC_MAX_PER_HOUR"] = _old_cap
    globals()["now_local"] = _orig_now_local
    globals()["RATE_LIMIT_SLEEP"] = _orig_sleep
    for _k, _v in _pub_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

    print("\n[8-2c6] 띄어쓴 회사명 인식 · 태그 복구 (2026-10-01)")
    check("띄어쓴 표기도 그룹사로 인식 — 포스코 퓨처엠·POSCO 퓨처엠·포스코 홀딩스·포스코 인터내셔널",
          [detect_group_companies(t) for t in ("포스코 퓨처엠, 태양열 지원", "POSCO 퓨처엠 신공장",
                                              "포스코 홀딩스 회장", "포스코 인터내셔널 수주")],
          [["포스코퓨처엠"], ["포스코퓨처엠"], ["포스코홀딩스"], ["포스코인터내셔널"]])
    check("띄어쓴 표기는 언급 근거·발췌에서도 인정",
          (pfm_mention_supported({"pfm_excerpt": "", "title": "포항남부서-포스코 퓨처엠, 1인 소상공인 지원"}),
           "포스코 퓨처엠" in extract_pfm_excerpt("지역 소식이다.\n포스코 퓨처엠은 태양열 설비를 지원했다.")),
          (True, True))
    check("상위 개념 '포스코' 는 변형을 만들지 않고, 띄어쓴 일반 문구는 오탐하지 않는다",
          (alias_variants("포스코"), detect_group_companies("포스코 협력사 대금 조기 지급")), (["포스코"], ["포스코"]))
    _tg = SqliteStorage(os.path.join(__import__("tempfile").mkdtemp(), "tg.db"))
    _tg.init_schema()
    for _gid, _gt2, _gg, _gx in (("x1", "포항남부서-포스코 퓨처엠, 소상공인 지원", "[]", ""),
                                 ("x2", "무관한 기사", "[]", ""),
                                 ("x3", "포스코퓨처엠 증설", '["포스코퓨처엠"]', "있는 발췌")):
        _tg._exec("insert into articles (id,url_source,url_canonical,url_original,title,published_at,collected_at,"
                  "source_type,importance_score,group_companies,categories,press_name,analyzed_at,status,"
                  "is_representative,pfm_excerpt) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (_gid, "u/" + _gid, "u/" + _gid, "u/" + _gid, _gt2, iso(_pn), iso(_pn), "rss", 10, _gg, "[]",
                   "서울신문", iso(_pn), "active", 1, _gx))
        _tg.save_summary({"id": new_id(), "article_id": _gid, "summary_text": "요약", "perspective_text": "",
                          "summary_source": "fulltext", "model": "m", "token_usage": None, "created_at": iso(_pn)})
    _tgctx = type("C", (), {"storage": _tg})()
    cmd_tagassoc(_tgctx, dry=True)
    check("tagassoc --dry — 쓰지 않는다", jload(_tg._one("select group_companies from articles where id='x1'")["group_companies"], []), [])
    cmd_tagassoc(_tgctx)
    _x1 = _tg._one("select group_companies, pfm_excerpt from articles where id='x1'")
    check("tagassoc — 제목에 띄어쓴 이름이 있는 기사에 포스코퓨처엠 태그를 되돌리고 발췌는 비워 pfmtone 이 채우게 한다",
          (jload(_x1["group_companies"], []), _x1["pfm_excerpt"]), (["포스코퓨처엠"], None))
    check("tagassoc — 무관한 기사·이미 태그된 기사는 그대로",
          (jload(_tg._one("select group_companies from articles where id='x2'")["group_companies"], []),
           _tg._one("select pfm_excerpt from articles where id='x3'")["pfm_excerpt"]), ([], "있는 발췌"))

    check("종가표 목록 제목은 수집 제외 대상, 일반 기사는 아니다",
          ([is_market_list_title(t) for t in ("KOSPI 200 Closing Price List-2", "코스피 200 종가 목록-3",
                                              "포스코퓨처엠 양극재 증설", "[Closing Market] Micron Sparks KOSPI Reversal")]),
          [True, True, False, False])

    print("\n[8-2c7] 인사 — 원문 그대로 한 줄씩 (2026-10-01 실제 기사 형식)")
    _hw = "◇ 신규 임원 승진 ▲ 강건희 (서울=연합뉴스) 신규 임원 과반이 1980년대생 (서울=연합뉴스) 인사를 단행했다."
    check("연합 한화저축은행 — 이름만 있는 항목도 한 줄, 뒤의 기사 문장은 안 섞인다",
          format_people_notice(_hw, "personnel"), "ㆍ강건희 (신규 임원 승진)")
    _hw_html = ('<meta property="og:description" content="◇ 신규 임원 승진"/><article><p>◇ 신규 임원 승진</p>'
                '<p>▲ 강건희 (서울=연합뉴스)</p><p>&lt;저작권자(c) 연합뉴스, 무단 전재-재배포, AI 학습 및 활용 금지&gt;</p></article>')
    _hw_body = "◇ 신규 임원 승진 ▲ 강건희 <저작권자(c) 연합뉴스, 무단 전재-재배포, AI 학습 및 활용 금지>"
    check("꼬리 문구(저작권·무단전재)가 붙어도 명단을 버리지 않는다(예전엔 머리말 한 줄만 남았다)",
          "강건희" in _notice_text(_hw_html, _hw_body, "[인사] 한화저축은행"), True)
    _hk = ("◈한화그룹 M&S부문◎승진<신규 임원>▷한화M&S 박대주 배상현 유상현 "
           "◈한화자산운용◎승진<임원>▷김동영▷신정호")
    check("한국경제식 표기(◈ 기관 ◎ 구분 <유형> ▷ 항목) — 위계 머리말을 이어 붙여 한 줄씩",
          format_people_notice(_hk, "personnel").splitlines(),
          ["ㆍ박대주 · 한화M&S (한화그룹 M&S부문 · 승진 · 신규 임원)",
           "ㆍ배상현 · 한화M&S (한화그룹 M&S부문 · 승진 · 신규 임원)",
           "ㆍ유상현 · 한화M&S (한화그룹 M&S부문 · 승진 · 신규 임원)",
           "ㆍ김동영 (한화자산운용 · 승진 · 임원)", "ㆍ신정호 (한화자산운용 · 승진 · 임원)"])
    check("연합식(◇ 하나) 머리말은 그대로 — 위계 변경이 기존 형식을 바꾸지 않는다",
          format_people_notice("◇ 대검검사급 전보 ▲ 법무부 기획조정실장 차범준", "personnel"),
          "ㆍ차범준 · 법무부 기획조정실장 (대검검사급 전보)")
    check("◇ 아래 ◆ 하위 구분도 이어 붙인다",
          format_people_notice("◇ ㈜한화 ◆ 신규 임원 승진 ▲ 강창수 ▲ 곽원석", "personnel").splitlines(),
          ["ㆍ강창수 (㈜한화 · 신규 임원 승진)", "ㆍ곽원석 (㈜한화 · 신규 임원 승진)"])
    _col = "칼럼 윤문원의 쪽팔리게 살지마 마음의 시력을 가져야 쪽팔리지 않는다. 눈으로만 세상을 보면 사실만 보이지만, 마음의 시력으로 바라보면 감정과 사정까지 보이게 된다."
    check("엉뚱한 본문(제목과 무관한 칼럼)은 옮기지 않는다 — 안내 문구로 대체",
          (format_people_notice(_col, "personnel", "에코프로, 사장단 승진·보직 인사"),
           people_summary(type("C", (), {})(), "personnel", "에코프로, 사장단 승진·보직 인사", "충청매일", _col, False)[0]),
          ("", PEOPLE_NO_LIST_MSG))
    _prose = "에코프로는 1일 사장단 승진·보직 인사를 단행했다. 이재영 부사장이 사장으로 승진했다. 김철수 상무는 전무로 승진했다."
    check("줄글 인사 기사는 제목과 관련 있을 때 원문 문장을 한 줄씩 그대로(재서술 없음)",
          format_people_notice(_prose, "personnel", "에코프로, 사장단 승진·보직 인사").splitlines(),
          ["ㆍ이재영 부사장이 사장으로 승진했다.", "ㆍ김철수 상무는 전무로 승진했다."])   # 배경 문장(사장단 인사를 단행했다)은 뺀다
    check("줄글 맨 앞의 연합 dateline 이 본문을 통째로 지우지 않는다",
          "이재영" in format_people_notice("(서울=연합뉴스) 홍길동 기자 = " + _prose, "personnel", "에코프로 인사"), True)
    check("인사는 AI 를 부르지 않고 규칙(rule)으로 처리한다",
          people_summary(type("C", (), {})(), "personnel", "[인사] 법무부", "연합", _hw, True)[1], PEOPLE_RULE_MODEL)
    check("기자명 — '날씨'·'보도' 같은 화면 낱말은 기자가 아니다",
          ([_valid_author(x, "연합뉴스") for x in ("날씨", "보도", "제보", "속보")],
           [split_authors(x) for x in ("날씨", "보도")]), (["", "", "", ""], [[], []]))

    # 회귀 방지(2026-10-02, 사용자 지적): 한화에너지는 세 사람이 한 줄, 예금보험공사·산업통상부는
    # 명단이 있는데 '확인 못 함', 에코프로는 승진자만이 아니라 기사 문장 전체. 실제 기사 형식으로 검증한다.
    _viva = ("◇한화에너지 (총 3명) ▲김민수, 박인찬, 이경재 ◇한화토탈에너지스 (총 1명) ▲임한빈 "
             "◇한화파워 (총 2명) ▲박성순, 서재호")
    check("한화에너지 — 쉼표로 이은 세 사람은 한 줄씩, '(총 3명)' 은 머리말에서 뺀다",
          format_people_notice(_viva, "personnel", "[인사] 한화에너지").splitlines(),
          ["ㆍ김민수 (한화에너지)", "ㆍ박인찬 (한화에너지)", "ㆍ이경재 (한화에너지)", "ㆍ임한빈 (한화토탈에너지스)",
           "ㆍ박성순 (한화파워)", "ㆍ서재호 (한화파워)"])
    check("한화에너지(조선비즈식) — ▲ 이어붙임도 한 줄씩",
          format_people_notice("<승진> ◇임원 ▲김민수 ▲박인찬 ▲이경재", "personnel", "[인사] 한화에너지").splitlines(),
          ["ㆍ김민수 (승진 · 임원)", "ㆍ박인찬 (승진 · 임원)", "ㆍ이경재 (승진 · 임원)"])
    check("뉴데일리식 — '▷신규 임원 △이름' 의 ▷ 글은 사람이 아니라 소제목",
          format_people_notice("◆한화에너지 ▷신규 임원 △김민수 △박인찬 ◆한화파워 ▷신규 임원 △박성순", "personnel").splitlines(),
          ["ㆍ김민수 (한화에너지 · 신규 임원)", "ㆍ박인찬 (한화에너지 · 신규 임원)", "ㆍ박성순 (한화파워 · 신규 임원)"])
    check("지명 나열은 사람으로 가르지 않는다",
          format_people_notice("◇ 전보 ▲ 서울, 부산 지검 검사장 김철수", "personnel"),
          "ㆍ김철수 · 서울, 부산 지검 검사장 (전보)")
    _kdic_html = ('<meta property="og:description" content="◇ 1급 승진"/><article><p>◇ 1급 승진</p>'
                  '<p>▲ 성과경영실장 장은익 ▲ 금투리스크관리부장 황우진</p><p>◇ 부서장 전보 발령</p>'
                  '<p>▲ 기획조정부장 진호정 ▲ 은행리스크관리부장\n이지현 (서울=연합뉴스)\n제보는 카카오톡 okjebo\n'
                  '<저작권자(c) 연합뉴스,\n무단 전재-재배포, AI 학습 및 활용 금지>\n2026/07/23 09:44 송고</p></article>')
    _kdic_body = ("◇ 1급 승진\n▲ 성과경영실장 장은익 ▲ 금투리스크관리부장 황우진\n◇ 부서장 전보 발령\n"
                  "▲ 기획조정부장 진호정 ▲ 은행리스크관리부장\n이지현 (서울=연합뉴스)\n제보는 카카오톡 okjebo\n"
                  "<저작권자(c) 연합뉴스,\n무단 전재-재배포, AI 학습 및 활용 금지>\n2026/07/23 09:44 송고")
    _kdic_out = format_people_notice(_notice_text(_kdic_html, _kdic_body, "[인사] 예금보험공사"), "personnel", "[인사] 예금보험공사")
    check("예금보험공사 — 줄바꿈을 낀 저작권 꼬리가 있어도 명단 전부(전보 포함)",
          (len(_kdic_out.splitlines()), "ㆍ진호정 · 기획조정부장 (부서장 전보 발령)" in _kdic_out,
           "ㆍ이지현 · 은행리스크관리부장 (부서장 전보 발령)" in _kdic_out),
          (4, True, True))
    _eco = ("에코프로는 지난달 30일 사장단 승진 및 보직 인사를 단행했다고 1일 밝혔다. "
            "이번 인사에서 최상운 에코프로 경영지원본부장과 박종환 에코프로이엠 대표가 각각 사장으로 승진했다. "
            "최 사장은 에코프로 대표이사로, 박 사장은 에코프로에이치엔 대표이사로 각각 내정됐다. "
            "최 신임 사장은 그동안 회사의 핵심 기반 업무를 총괄하며 조직 안정과 지속가능한 성장을 이끌었다.")
    _eco_out = format_people_notice(_eco, "personnel", "에코프로, 사장단 인사 조기 단행…최상운·박종환 사장 승진").splitlines()
    check("에코프로 — 승진·내정 문장만(배경 설명·인물 칭찬 문장은 뺀다)",
          (len(_eco_out), "최상운" in _eco_out[0], any("조직 안정" in x for x in _eco_out),
           any("단행했다고" in x for x in _eco_out)), (2, True, False, False))

    # 정부 부처 인사 이력 보강(2026-10-02) — 프로필 기사 표기(△1970년 △부산 △행시41회 △○○대 …)를 규칙으로만 읽고,
    # 같은 사람인지(이름+소속·직책)·사이트 간 일치 여부를 검증한 뒤 출처 링크와 함께 쓴다.
    _pf1 = ("고위공무원 가급(4명) 이성진 국세청 차장 △1970년 △부산 △행시41회 △해운대고, 고려대 경제학과 "
            "△목포세무서장 △미국, Nelson mullins(교육훈련) △국세청 법인납세국장\n\n다른 문단")
    _pf2 = "이성진 국세청 차장 70년생 △부산 △해운대고 △고려대 경제학과 △행시41회 △목포세무서장 △국세청 법인납세국장"
    _pf3 = "이성진 국세청 차장 71년생 △부산 △행시41회 △고려대 경제학과"
    _pfb = extract_profile_blocks(_pf1, "이성진")[0]
    check("프로필 표기 읽기 — 출생·시험·학교·경력(소속 직책 머리말은 경력에 안 섞인다)",
          (_pfb["birth"], _pfb["exam"], _pfb["school"], _pfb["career"][0]),
          ("1970", "행시41회", "고려대 경제학과", "목포세무서장"))
    check("프로필 표기 읽기 — '70년생' 두 자리 연도는 1970 으로",
          extract_profile_blocks("이동운 부산지방국세청장 70년생 △서울 △서울대 경영학과 △행시37회", "이동운")[0]["birth"], "1970")
    check("본문 속 단순 언급(△ 나열 아님)은 프로필로 안 읽는다",
          extract_profile_blocks("이성진 차장은 어제 기자간담회를 열었다.", "이성진"), [])
    _pages = {"https://a.example/p": _pf1, "https://b.example/p": _pf2}
    _info = lookup_person_career(None, "이성진", "국세청", "차장", search=lambda q: list(_pages),
                                 fetch=lambda u: (u, _pages[u]))
    check("이력 검증 — 두 사이트가 같으면 교차확인, 출처 둘",
          (_info["facts"]["birth"], _info["facts"]["school"], len(_info["sources"])),
          (("1970", 2), ("고려대 경제학과", 2), 2))
    _line = format_career_line(_info)
    check("이력 줄 — 교차확인 표시 + 출처 주소 + 경력", ("교차확인 2곳" in _line, "https://a.example/p" in _line, "목포세무서장" in _line),
          (True, True, True))
    _pages2 = {"https://a.example/p": _pf1, "https://c.example/p": _pf3}
    _info2 = lookup_person_career(None, "이성진", "국세청", "차장", search=lambda q: list(_pages2),
                                  fetch=lambda u: (u, _pages2[u]))
    check("이력 검증 — 사이트끼리 출생연도가 다르면 그 항목은 뺀다(나머지는 일치하면 유지)",
          ("birth" in _info2["facts"], _info2["facts"]["school"][0]), (False, "고려대 경제학과"))
    _other = {"https://d.example/p": "이성진 △1980년 △서울 △행시50회 △서울대 법학과 △법무법인 대표"}
    check("같은 이름이라도 소속·직책이 안 나오면 다른 사람으로 보고 버린다",
          lookup_person_career(None, "이성진", "국세청", "차장", search=lambda q: list(_other),
                               fetch=lambda u: (u, _other[u])), {})
    check("검색이 아무것도 못 찾으면 빈 결과(지어내지 않는다)",
          lookup_person_career(None, "이성진", "국세청", "차장", search=lambda q: [], fetch=lambda u: (u, "")), {})
    _gov_txt = "ㆍ김태현 · 첨단민군협력과장 (과장급 승진)\nㆍ이도윤 · 서울고검 차장검사 (전보)"
    _enr = enrich_people_careers(None, _gov_txt, "[인사] 산업통상부 과장급 전보",
                                 lookup=lambda n, a, p: {"facts": {"birth": ("1975", 2)}, "sources": ["https://x/1", "https://y/1"],
                                                         "career": ["산업정책과장"]} if n == "김태현" else {})
    check("이력 보강 — 해당자 줄 바로 아래에 이력 줄, 못 찾은 사람은 그대로",
          _enr.splitlines()[:3], ["ㆍ김태현 · 첨단민군협력과장 (과장급 승진)",
                                  "   · 이력: 1975년생 · 경력: 산업정책과장 (교차확인 2곳) https://x/1 https://y/1",
                                  "ㆍ이도윤 · 서울고검 차장검사 (전보)"])
    check("이력 보강 — 정부 직급 표현이 없는 기업 인사는 건드리지 않는다",
          enrich_people_careers(None, "ㆍ김민수 (한화에너지)", "[인사] 한화에너지", lookup=lambda *a: {"facts": {}, "sources": ["u"], "career": ["x"]}),
          "ㆍ김민수 (한화에너지)")

    # 언론사 탭 — 언론사가 포스코퓨처엠을 전반적으로 어떻게 평가하는지 한 줄(2026-10-02)
    _po = object.__new__(LLMClient)
    _po_seen: list[str] = []

    def _po_chat(system, prompt):
        _po_seen.append(prompt)
        return '{"overview":"실적·증설은 호의적으로, 투자 부담은 비판적으로 다룸"}', {}
    _po._chat = _po_chat
    _po_tone = {"긍정": 2, "중립": 1, "부정": 1, "미판정": 0}
    _po_arts = [{"title": "포스코퓨처엠 양극재 증설", "tone": "긍정", "tone_reason": "증설을 호의적으로 보도"},
                {"title": "포스코퓨처엠 적자 전환", "tone": "부정", "tone_reason": "실적 악화"}]
    check("언론사 한줄 평가 — 제목·논조·근거를 AI 에 주고 한 줄을 받는다",
          (_po.press_overview("머니투데이", _po_arts, _po_tone), "양극재 증설" in _po_seen[0], "실적 악화" in _po_seen[0],
           "긍정 2" in _po_seen[0]),
          ("실적·증설은 호의적으로, 투자 부담은 비판적으로 다룸", True, True, True))
    check("언론사 한줄 평가 — 기사가 없으면 AI 를 부르지 않는다", (_po.press_overview("x", [], _po_tone), len(_po_seen)), ("", 1))
    check("언론사 한줄 평가 — AI 불가 시 논조 분포만 반영한 문장(그렇다고 밝힌다)",
          ("긍정 보도가 가장 많습니다" in press_overview_fallback({"tone": {"긍정": 3, "중립": 1, "부정": 0, "미판정": 0}, "year": 4}),
           "논조 분포만 반영" in press_overview_fallback({"tone": {"긍정": 3, "중립": 1, "부정": 0, "미판정": 0}, "year": 4}),
           "판정된 기사가 아직 없습니다" in press_overview_fallback({"tone": {"긍정": 0, "중립": 0, "부정": 0, "미판정": 2}, "year": 2})),
          (True, True, True))

    print("\n[8-2c3] 원장 되살리기(reopen) · Google RSS 병렬 수집")
    _lt = SqliteStorage(os.path.join(__import__("tempfile").mkdtemp(), "ledger.db"))
    _lt.init_schema()
    _lnow = now_utc()
    for _u, _r, _d in (("u/off-new", "off_topic", 1), ("u/fail-new", "extract_failed", 2),
                       ("u/stale-new", "stale", 1), ("u/off-old", "off_topic", 20)):
        _lt._exec("insert into url_ledger (url_source, reason, first_seen) values (?,?,?)",
                   (_u, _r, iso(_lnow - timedelta(days=_d))))
    _lctx = type("C", (), {"storage": _lt})()
    cmd_reopen(_lctx, 3, dry=True)
    check("reopen --dry — 아무것도 지우지 않는다", _lt._one("select count(*) as n from url_ledger")["n"], 4)
    cmd_reopen(_lctx, 3)
    check("reopen — 최근 무관·접속실패만 지운다(stale·오래된 기록은 그대로)",
          sorted(r["url_source"] for r in _lt._rows("select url_source from url_ledger")),
          ["u/off-old", "u/stale-new"])
    _lt._exec("delete from url_ledger")

    class _GResp:
        def __init__(self, kw: str) -> None:
            self.content = (
                '<?xml version="1.0"?><rss version="2.0"><channel><item>'
                f"<title>{kw} 기사</title><link>https://g.test/{kw}</link>"
                "<pubDate>Mon, 14 Sep 2026 00:00:00 +0900</pubDate><description>d</description>"
                "</item></channel></rss>").encode()
        def raise_for_status(self) -> None:
            pass

    class _GHttp:
        def __init__(self) -> None:
            self.calls = 0
            self._lk = threading.Lock()
        def get(self, url: str, **kw: Any) -> Any:
            with self._lk:
                self.calls += 1
            if "BAD" in url:
                raise RuntimeError("조회 실패 시뮬레이션")
            import urllib.parse as _up
            return _GResp(_up.parse_qs(_up.urlparse(url).query)["q"][0])
    _grows = [{"keyword": f"k{i}", "category": "산업"} for i in range(9)] + [{"keyword": "BAD", "category": "산업"}]
    _gh = _GHttp()
    _gitems = collect_google_rss(_gh, _grows)
    check("Google RSS 병렬 — 전 키워드 조회·실패 1건은 건너뜀", (_gh.calls, len(_gitems)), (10, 9))
    check("Google RSS 병렬 — 결과 순서가 키워드 순서와 같다",
          [i.url_original for i in _gitems], [f"https://g.test/k{i}" for i in range(9)])

    # 다음(Daum) 수집 — Google RSS 'site:v.daum.net' (2026-10-06)
    _drows = daum_keyword_rows([
        {"keyword": "포스코퓨처엠", "category": "그룹사"}, {"keyword": "포스코퓨처엠", "category": "그룹사"},
        {"keyword": "포스코DX", "category": "그룹사"}, {"keyword": "정부", "category": "정책"},
        {"keyword": "양극재", "category": "산업"}, {"keyword": "관세", "category": "통상"}])
    check("다음 키워드 — 그룹사만·중복 제거", [(r["keyword"], r["category"]) for r in _drows],
          [("포스코퓨처엠", "다음"), ("포스코DX", "다음")])
    _queries: list[str] = []
    class _QHttp(_GHttp):
        def get(self, url: str, **kw: Any) -> Any:
            import urllib.parse as _up
            _queries.append(_up.parse_qs(_up.urlparse(url).query)["q"][0])
            return super().get(url, **kw)
    collect_google_rss(_QHttp(), _drows + [{"keyword": "양극재", "category": "산업"},
                                           {"keyword": "정부", "category": "정책"}])
    check("다음 검색어 — site:v.daum.net 은 다음 분류만, 정책은 korea.kr, 나머지는 키워드 그대로",
          sorted(_queries), sorted(["site:v.daum.net 포스코퓨처엠", "site:v.daum.net 포스코DX",
                                    "양극재", f"{POLICY_SITE} 정부"]))
    _old_daum = os.environ.pop("DAUM_ENABLED", None)
    check("DAUM_ENABLED 기본은 꺼짐", daum_enabled(), False)
    os.environ["DAUM_ENABLED"] = "true"
    check("DAUM_ENABLED=true 면 켜짐", daum_enabled(), True)
    os.environ["DAUM_ENABLED"] = "no"
    check("DAUM_ENABLED=no 면 꺼짐", daum_enabled(), False)
    if _old_daum is None:
        os.environ.pop("DAUM_ENABLED", None)
    else:
        os.environ["DAUM_ENABLED"] = _old_daum
    # 눈에 안 보이는 문자가 끼어도 포스코퓨처엠 언급 발췌가 빠지지 않는다 (머니투데이 사례 점검)
    _inv = ("이번 계약은 양극재 시장에 의미가 크다는 평가다. 업계는 주목하고 있다.\n"
            "포스코\u00a0퓨처엠은 SK온과 1조원 규모 공급계약을 체결했다고 22일 밝혔다. 계약기간은 3년이다.\n"
            "포스코\u200b퓨처엠 관계자는 \"고객 포트폴리오를 확대하겠다\"고 설명했다. 다른 설명은 없었다.")
    _ex_inv = extract_pfm_excerpt(_inv)
    # 머니투데이 증권 기사 — 종목명·시세 위젯·'·' 가 줄마다 쪼개져 15자 미만 줄이 버려지던 문제 (2026-10-06)
    _mt = ("[특징주]\n국내 이차전지주가 6일 장중 강세다. 미국 테슬라의 인도량이 시장 예상을 뛰어넘었다.\n"
           "이날 오후 한국거래소에서 KRX 2차전지 TOP10 지수는 4.70% 오른 3793.31로 산출됐다.\n지수 구성종목 가운데\n에코프로비엠\n(128,300원 ▲12,600 +10.89%)\n"
           "은 1만800원 오른 12만6500원,\n삼성SDI\n(574,500원 ▲45,500 +8.6%)\n는 4만1000원 오른 57만원이다.\n"
           "포스코퓨처엠\n(203,500원 ▲16,600 +8.88%)\n·\nLG화학\n(276,500원 ▲14,000 +5.33%)\n은 5%대,\n"
           "POSCO홀딩스\n(317,000원 ▲6,000 +1.93%)\n는 1%대 강세다.\n지난 2일 테슬라는 차량 48만6천532대를 인도했다고 발표했다.")
    _mt_ex = extract_pfm_excerpt(_mt)
    check("발췌 — 시세 위젯으로 쪼개진 종목명도 언급으로 잡는다(머니투데이 증권 기사)",
          ("포스코퓨처엠 (203,500원 ▲16,600 +8.88%)" in _mt_ex, "은 5%대" in _mt_ex), (True, True))
    check("시세 위젯 이음 — 조사가 아닌 일반 문단(이날 …)은 앞 문장에 붙이지 않는다",
          "다.\n이날" in _reflow_quote_widgets(_mt) or "다.이날" not in _reflow_quote_widgets(_mt), True)
    check("시세 위젯 이음 — 위젯이 없는 글은 그대로", _reflow_quote_widgets("가나다\n라마바"), "가나다\n라마바")
    check("발췌 — NBSP·폭 없는 문자가 끼어도 언급을 찾는다", ("SK온과 1조원" in _ex_inv, "포스코 퓨처엠 관계자" in _ex_inv or "포스코퓨처엠 관계자" in _ex_inv), (True, True))

    print("\n[8-2d] 배터리 생태계 기사 (포스코 미언급 허용)")
    check("제목에 '전기차' 만 있어도 수집(포스코 미언급)", is_battery_scope("전기차 신차 출시 앞두고 가격 인하 경쟁"), True)
    check("제목에 '배터리' 만 있어도 수집", is_battery_scope("배터리 화재 예방 위한 점검 확대"), True)
    check("제목에 '이차전지' 만 있어도 수집", is_battery_scope("이차전지 업종 강세"), True)
    check("본문 앞부분에 '전기차' 가 스치기만 하면 수집하지 않음(제목 기준)",
          is_battery_scope("서울 아파트 매매 동향", "단지 내 전기차 충전기 설치"), False)
    check("제목에 무관 낱말만 있으면 수집 안 함", is_battery_scope("코스피 상승 마감"), False)
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
        _SCAN_STORE.update(by_id={}, cursor="", full_at=0.0, delta_at=0.0, sorted=None)
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
    # 회귀 방지(2026-09-15): 델타로 새로 들어온 기사가 dict 삽입 순서상 맨
    # 뒤에 붙어도, 반환은 발행일 최신순으로 재정렬돼 있어야 한다 — 안 그러면
    # api_articles 의 'recent' 정렬이 신규 기사를 마지막 페이지로 밀어버린다.
    check("델타로 들어온 최신 기사가 정렬 후 맨 앞에 온다", rows[0]["row"]["id"], "st-c")
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
    # 정렬 캐시(2026-09-22) — 스토어가 안 바뀌면 최대 4만 건을 다시 정렬하지 않는다.
    _SCAN_STORE["delta_at"] = time.monotonic()
    check("스토어가 그대로면 정렬 결과를 재사용한다",
          refresh_scan_store(_tmp) is refresh_scan_store(_tmp), True)
    # 한 건만 직접 반영해도(수동 등록 경로) 정렬 캐시는 버려야 한다.
    _before = refresh_scan_store(_tmp)
    scan_store_upsert(_tmp, "st-b")
    _SCAN_STORE["delta_at"] = time.monotonic()
    check("scan_store_upsert 뒤에는 정렬을 다시 한다",
          refresh_scan_store(_tmp) is not _before, True)
    check("정렬 캐시를 써도 발행일 최신순은 그대로",
          [t["row"]["id"] for t in refresh_scan_store(_tmp)][0], "st-c")
    _reset_store()

    print("\n[11-2e1] 포스코퓨처엠 언급 발췌 (2026-09-29)")
    _pb = ("정부가 이차전지 지원책을 발표했다. 업계는 대체로 환영했다. "
           "포스코퓨처엠은 광양 양극재 공장 증설을 서두르기로 했다. "
           "회사 관계자는 내년 가동을 목표로 한다고 밝혔다. 증권가는 실적 개선을 전망했다. "
           "한편 삼성SDI는 미국 공장을 늘린다.")
    _ex = extract_pfm_excerpt(_pb)
    check("발췌 — 언급 문장이 들어 있다", "포스코퓨처엠은 광양 양극재" in _ex, True)
    check("발췌 — 뒤 문맥(다음 문장)도 붙는다", "내년 가동" in _ex, True)
    check("발췌 — 상한 이하", 0 < len(_ex) <= PFM_EXCERPT_MAX, True)
    check("발췌 — 언급 없으면 빈 문자열(영역 미표시)",
          extract_pfm_excerpt("삼성SDI가 공장을 늘린다. 업계가 주목한다."), "")
    check("발췌 — 옛 사명(포스코케미칼)도 언급으로 본다",
          "포스코케미칼" in extract_pfm_excerpt("과거 포스코케미칼 시절 투자다."), True)
    check("발췌 — 빈 본문", extract_pfm_excerpt(""), "")
    _long = "포스코퓨처엠이 " + "아주 긴 문장을 " * 80 + "끝낸다."
    check("발췌 — 너무 긴 문장은 상한에서 자르고 … 표시",
          (len(extract_pfm_excerpt(_long)) <= PFM_EXCERPT_MAX + 1,
           extract_pfm_excerpt(_long).endswith("…")), (True, True))

    print("\n[11-2e2] 상세 검색 — 제목·본문·언론사·기자 AND (2026-09-29)")
    _fs_rows = [tag_row(r) for r in (
        {"id": "fs-1", "title": "포스코퓨처엠 양극재 증설", "author": "김기자",
         "press_name": "mstoday.co.kr", "url_canonical": "https://www.mstoday.co.kr/1",
         "summary_text": "광양 공장", "pfm_excerpt": ""},
        {"id": "fs-2", "title": "포스코퓨처엠 음극재 수주", "author": "이기자",
         "press_name": "한국경제", "url_canonical": "https://hankyung.com/2",
         "summary_text": "요약", "pfm_excerpt": "리튬 가격 하락"},
        {"id": "fs-3", "title": "삼성SDI 실적", "author": "김기자",
         "press_name": "한국경제", "url_canonical": "https://hankyung.com/3",
         "summary_text": "요약", "pfm_excerpt": ""},
    )]
    _ids = lambda ts: [t["row"]["id"] for t in ts]
    check("빈 칸만 있으면 조건 없음", _ids(field_search(_fs_rows, {"title": "", "body": " "})),
          ["fs-1", "fs-2", "fs-3"])
    check("제목 칸", _ids(field_search(_fs_rows, {"title": "포스코퓨처엠"})), ["fs-1", "fs-2"])
    # 회귀 방지(2026-09-29 사용자 지적: 기자명을 검색했는데 다른 기자가 나옴)
    check("기자 칸 — 정확히 같은 이름만 (김철 ≠ 김철수·김철민)",
          [author_matches(a, "김철") for a in ("김철수", "김철민", "김철")], [False, False, True])
    check("기자 칸 — '기자'·직함·이메일 떼고 비교",
          [author_matches(a, "김철수") for a in ("김철수 기자", "김철수 인턴기자", "김철수 (kcs@x.com)",
                                                 "영남본부=김철수", "김철수민")],
          [True, True, True, True, False])
    check("기자 칸 — 공동 바이라인 속 한 명도 찾는다",
          [author_matches(a, "이영희") for a in ("김철수·이영희", "김철수, 이영희 기자", "김철수 이영희")],
          [True, True, True])
    check("기자 칸 — 검색어에 '기자'를 붙여도 같다", author_matches("김철수", "김철수 기자"), True)
    check("기자 칸 — 팀 바이라인도 이름 전체가 같으면 찾는다",
          (author_matches("온라인뉴스팀", "온라인뉴스팀"), author_matches("온라인뉴스팀", "뉴스팀")),
          (True, False))
    check("기자 분리 — 직함·이메일 정리 후 중복 제거",
          split_authors("김철수 선임기자, 김철수 (kcs@x.com) / 이영희 특파원"), ["김철수", "이영희"])
    check("여러 칸은 AND (제목 + 기자)",
          _ids(field_search(_fs_rows, {"title": "포스코퓨처엠", "author": "김기자"})), ["fs-1"])
    check("언론사 칸은 화면 이름(도메인 → 매핑)으로 비교",
          _ids(field_search(_fs_rows, {"press": "MS투데이"})), ["fs-1"])
    check("본문 칸 — 보관 본문 id 로 찾음",
          _ids(field_search(_fs_rows, {"body": "배터리"}, {"fs-3"})), ["fs-3"])
    check("본문 칸 — 본문이 지워진 기사도 요약·발췌문으로 찾음",
          _ids(field_search(_fs_rows, {"body": "리튬"}, set())), ["fs-2"])
    _tmp._exec("delete from article_bodies")
    _tmp.save_body("st-b", "원문에만 있는 전고체 이야기", "fulltext")
    check("body_match_ids — 보관 본문에서 찾는다", _tmp.body_match_ids("전고체"), {"st-b"})
    check("body_match_ids — 빈 검색어는 빈 집합", _tmp.body_match_ids(""), set())
    # 본문 30일 보관: 분석 끝난 기사의 본문이 남아 있어도 '분석 대기'로 잡히면 안 된다.
    _tmp._exec("update articles set analyzed_at=? where id='st-b'", (iso(now_utc()),))
    check("분석 끝난 기사의 보관 본문은 분석 대기 큐에 안 들어간다",
          "st-b" in {r["id"] for r in _tmp.unanalyzed_with_body(50)}, False)
    _tmp._exec("delete from article_bodies")

    print("\n[11-2e4] HTTP 끊어진 연결 재사용 실패 — 1회 재시도 (2026-09-30)")
    # 실사례: 연합뉴스 RSS 가 5분 회차마다 번갈아 'Connection aborted / RemoteDisconnected' 로 실패했다.
    # 로컬 서버가 같은 연결의 두 번째 요청에 응답 없이 연결을 끊는 상황을 재현한다.
    import http.server as _hs
    import socketserver as _ss

    class _DropSecondHandler(_hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"   # keep-alive
        _hits = 0

        def do_GET(self):
            self._hits += 1
            if self._hits >= 2:         # 같은 연결의 2번째 요청 → 무응답 종료(유휴 연결이 죽은 상황)
                self.close_connection = True
                self.connection.close()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    class _DropServer(_ss.ThreadingMixIn, _hs.HTTPServer):
        daemon_threads = True

    _srv = _DropServer(("127.0.0.1", 0), _DropSecondHandler)
    threading.Thread(target=_srv.serve_forever, daemon=True).start()
    _drop_url = f"http://127.0.0.1:{_srv.server_address[1]}/x"
    _hc = HttpClient()
    _hc.session.trust_env = False       # 환경 프록시가 로컬 주소를 가로채지 않게
    _codes = []
    for _ in range(4):                  # 요청마다 '죽은 연결 재사용' 이 번갈아 일어난다
        _codes.append(_hc.get(_drop_url).status_code)
    check("끊어진 연결을 재사용해 실패해도 새 연결로 재시도해 성공한다", _codes, [200, 200, 200, 200])
    # 재시도 없는 세션 그대로면 실제로 실패한다 — 위 테스트가 의미 있는지 확인
    _plain = HttpClient()
    _plain.session.trust_env = False
    _plain.session.get(_drop_url)
    try:
        _plain.session.get(_drop_url)
        _plain_failed = False
    except Exception:
        _plain_failed = True
    check("(대조) 재시도 없는 세션은 같은 상황에서 실제로 실패한다", _plain_failed, True)
    _srv.shutdown()

    _calls = {"n": 0}
    _hc2 = HttpClient()
    _ex = _hc2._requests.exceptions

    def _raise_ssl(url, **kw):
        _calls["n"] += 1
        raise _ex.SSLError("certificate verify failed")

    _hc2.session.get = _raise_ssl
    try:
        _hc2.get("http://x.test/")
    except _ex.SSLError:
        pass
    check("SSL 오류는 재시도하지 않는다(1번만 시도)", _calls["n"], 1)

    def _raise_connect_timeout(url, **kw):
        _calls["n"] += 1
        raise _ex.ConnectTimeout("Connection to x timed out")

    _calls["n"] = 0
    _hc2.session.get = _raise_connect_timeout
    try:
        _hc2.get("http://x.test/")
    except _ex.ConnectTimeout:
        pass
    check("연결 시간초과는 재시도하지 않는다(응답 없는 서버를 두 배로 기다리지 않게)", _calls["n"], 1)

    def _raise_read_timeout(url, **kw):
        _calls["n"] += 1
        raise _ex.ReadTimeout("Read timed out")

    _calls["n"] = 0
    _hc2.session.get = _raise_read_timeout
    try:
        _hc2.get("http://x.test/")
    except _ex.ReadTimeout:
        pass
    check("읽기 시간초과도 재시도하지 않는다", _calls["n"], 1)

    def _always_dropped(url, **kw):
        _calls["n"] += 1
        raise _ex.ConnectionError("('Connection aborted.', RemoteDisconnected('x'))")

    _calls["n"] = 0
    _hc2.session.get = _always_dropped
    try:
        _hc2.get("http://x.test/")
    except _ex.ConnectionError:
        pass
    check("계속 끊기는 서버는 딱 2번(원래 1 + 재시도 1)만 시도하고 포기한다", _calls["n"], 2)

    print("\n[11-2e3] 언론사 탭 — 포스코퓨처엠 언론사별 집계 · 논조 백필 (2026-09-29)")
    check("기자 분리 — 공동 바이라인·'기자' 꼬리 제거",
          split_authors("김철수 기자, 이영희 기자"), ["김철수", "이영희"])
    check("기자 분리 — 빈 값", split_authors(""), [])
    check("기자 분리 — 붙여 쓴 '기자'도 떼되 이름이 안 남으면 그대로",
          (split_authors("김철수기자"), split_authors("김기자")), (["김철수"], ["김기자"]))
    _pnow = now_utc()
    _prows = [
        {"id": "p1", "title": "A", "press_name": "mstoday.co.kr", "url_canonical": "https://mstoday.co.kr/1",
         "author": "김기자", "published_at": iso(_pnow - timedelta(days=1)), "pfm_tone": "긍정"},
        {"id": "p2", "title": "B", "press_name": "MS투데이", "url_canonical": "https://mstoday.co.kr/2",
         "author": "김기자, 박기자", "published_at": iso(_pnow - timedelta(days=20)), "pfm_tone": "부정"},
        {"id": "p3", "title": "C", "press_name": "한국경제", "url_canonical": "https://hankyung.com/3",
         "author": "이기자", "published_at": iso(_pnow - timedelta(days=200)), "pfm_tone": None},
        {"id": "p4", "title": "D", "press_name": "한국경제", "url_canonical": "https://hankyung.com/4",
         "author": "", "published_at": iso(_pnow - timedelta(days=400)), "pfm_tone": "중립"},
    ]
    _agg = {e["press"]: e for e in aggregate_press_stats(_prows, _pnow)}
    check("집계 — 도메인 이름과 정식 이름이 한 언론사로 합쳐진다",
          sorted(_agg), ["MS투데이", "한국경제"])
    check("집계 — 주간·월간·연간 롤링 건수",
          (_agg["MS투데이"]["week"], _agg["MS투데이"]["month"], _agg["MS투데이"]["year"]), (1, 2, 2))
    check("집계 — 1년 넘은 기사는 제외", _agg["한국경제"]["year"], 1)
    check("집계 — 논조 건수(저장값만, 미판정 따로)",
          (_agg["MS투데이"]["tone"]["긍정"], _agg["MS투데이"]["tone"]["부정"],
           _agg["한국경제"]["tone"]["미판정"]), (1, 1, 1))
    check("집계 — 기자별 건수(많은 순)",
          [(x["name"], x["count"]) for x in _agg["MS투데이"]["reporters"]], [("김기자", 2), ("박기자", 1)])
    check("집계 — 기자 대표 논조(긍1·부1 동률→중립, 부정 1건→부정, 미판정만→'')",
          [x["color"] for x in _agg["MS투데이"]["reporters"]] + [x["color"] for x in _agg["한국경제"]["reporters"]],
          ["중립", "부정", ""])
    check("집계 — 기자 색 규칙(다수결·판정 없음)",
          (reporter_tone({"긍정": 2, "부정": 1}), reporter_tone({"중립": 3, "부정": 1}),
           reporter_tone({"미판정": 4})), ("긍정", "중립", ""))
    check("집계 — 기사에 기자 목록이 실려 화면이 기자별로 거른다",
          [a["authors"] for a in _agg["MS투데이"]["articles"]], [["김기자"], ["김기자", "박기자"]])
    check("집계 — 언급 순위(1위부터)",
          [(e["press"], e["rank"]) for e in aggregate_press_stats(_prows, _pnow)], [("MS투데이", 1), ("한국경제", 2)])
    check("집계 — 언론사 행 클릭 시 펼칠 기사 목록(최신순)",
          [a["id"] for a in _agg["MS투데이"]["articles"]], ["p1", "p2"])
    check("집계 — 연간 건수 많은 언론사가 위", aggregate_press_stats(_prows, _pnow)[0]["press"], "MS투데이")

    _tmp._exec("delete from articles where id like 'pt-%'")
    for _aid, _grp, _ex, _tone in (("pt-1", '["포스코퓨처엠"]', None, None),
                                   ("pt-2", '["포스코퓨처엠"]', "포스코퓨처엠이 증설한다.", None),
                                   ("pt-3", '["포스코퓨처엠"]', "", None),
                                   ("pt-4", '["포스코이앤씨"]', None, None),
                                   ("pt-5", '["포스코퓨처엠"]', "포스코퓨처엠 호조.", "긍정")):
        _tmp._exec(
            "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
            " collected_at, source_type, group_companies, press_name, analyzed_at, status,"
            " is_representative, pfm_excerpt, pfm_tone) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_aid, f"u/{_aid}", f"u/{_aid}", f"u/{_aid}", "t", iso(_pnow), iso(_pnow), "rss",
             _grp, "한경", iso(_pnow), "active", 1, _ex, _tone))
    check("pfm_articles — 포스코퓨처엠 태그 기사만",
          sorted(r["id"] for r in _tmp.pfm_articles(iso(_pnow - timedelta(days=1)))
                 if r["id"].startswith("pt-")), ["pt-1", "pt-2", "pt-3", "pt-5"])
    _tmp.save_body("pt-1", "업계 소식이다. 포스코퓨처엠이 광양 공장의 양극재 생산능력을 늘린다. "
                           "시장은 대체로 이 결정을 반긴다는 분위기다.", "fulltext")

    class _ToneLLM:
        calls = 0

        def pfm_tone(self, excerpt: str) -> tuple[str, str]:
            _ToneLLM.calls += 1
            return "중립", "사실 전달"

    _tctx = type("C", (), {"storage": _tmp, "llm": _ToneLLM(), "http": None})()
    cmd_pfmtone(_tctx, limit=10, dry=True)
    check("백필 --dry — LLM 도 안 부르고 아무것도 안 쓴다",
          (_ToneLLM.calls, _tmp._one("select pfm_excerpt from articles where id='pt-1'")["pfm_excerpt"]),
          (0, None))
    cmd_pfmtone(_tctx, limit=10)
    _pt = {r["id"]: r for r in _tmp._rows(
        "select id, pfm_excerpt, pfm_tone, pfm_tone_reason from articles where id like 'pt-%'")}
    check("백필 — 보관 본문으로 발췌 후 논조 판정", (bool(_pt["pt-1"]["pfm_excerpt"]), _pt["pt-1"]["pfm_tone"]),
          (True, "중립"))
    check("백필 — 발췌만 있고 논조 없던 기사는 논조만 채운다",
          (_pt["pt-2"]["pfm_tone"], _pt["pt-2"]["pfm_tone_reason"]), ("중립", "사실 전달"))
    check("백필 — 언급 없음('')·이미 판정된 기사는 다시 안 부른다",
          (_pt["pt-3"]["pfm_tone"], _pt["pt-5"]["pfm_tone"]), (None, "긍정"))
    # 제목에 이름이 있고 발췌가 ''(본문엔 없음)인 기사는 제목+요약 언급 문장으로 논조를 판정한다(미판정 방지)
    _tmp._exec("insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
               " collected_at, source_type, group_companies, press_name, analyzed_at, status,"
               " is_representative, pfm_excerpt) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("pt-7", "u/pt-7", "u/pt-7", "u/pt-7", "포스코퓨처엠 신공장 착공", iso(_pnow), iso(_pnow), "rss",
                '["포스코퓨처엠"]', "한경", iso(_pnow), "active", 1, ""))
    _tmp.save_summary({"id": new_id(), "article_id": "pt-7", "summary_text": "포스코퓨처엠이 신공장 착공식을 열었다.",
                       "perspective_text": "", "summary_source": "fulltext", "model": "m",
                       "token_usage": None, "created_at": iso(_pnow)})
    cmd_pfmtone(_tctx, limit=10)
    check("백필 — 제목에 이름이 있는 '본문 언급 없음' 기사도 제목 기준으로 논조를 채운다",
          _tmp._one("select pfm_tone from articles where id='pt-7'")["pfm_tone"], "중립")
    # 본문이 없고 원문 재접속도 안 되는 기사 — 저장된 요약문의 언급 문장으로 논조만 판정한다
    _tmp._exec(
        "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
        " collected_at, source_type, group_companies, press_name, analyzed_at, status,"
        " is_representative) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pt-6", "u/pt-6", "u/pt-6", "u/pt-6", "t", iso(_pnow), iso(_pnow), "rss",
         '["포스코퓨처엠"]', "한경", iso(_pnow), "active", 1))
    _tmp.save_summary({"id": new_id(), "article_id": "pt-6",
                       "summary_text": "포스코퓨처엠이 양극재 증설을 발표했다. 시장은 환영했다.",
                       "perspective_text": "", "summary_source": "fulltext", "model": "m",
                       "token_usage": None, "created_at": iso(_pnow)})
    _before = _ToneLLM.calls
    cmd_pfmtone(_tctx, limit=10)
    _p6 = _tmp._one("select pfm_excerpt, pfm_tone from articles where id='pt-6'")
    check("백필 — 본문이 없으면 요약문으로 논조만 판정(발췌는 NULL 유지)",
          (_p6["pfm_tone"], _p6["pfm_excerpt"], _ToneLLM.calls - _before), ("중립", None, 1))
    cmd_pfmtone(_tctx, limit=10)
    check("백필 — 요약으로 판정된 기사는 다음 실행에서 다시 안 부른다",
          _ToneLLM.calls - _before, 1)
    _tmp._exec("delete from summaries where article_id='pt-6'")
    _tmp._exec("delete from articles where id like 'pt-%'")
    _tmp._exec("delete from article_bodies")

    print("\n[11-2f] 대체 공급자(Gemini·Grok·NVIDIA) — OpenAI 실패시 전환 (배포 전 점검, 2026-09-11/15)")
    _llm_blank = {f: "" for f in Config.__dataclass_fields__}
    _lcfg_with_fallback = Config(**{**_llm_blank, "openai_api_key": "sk-test",
                                     "llm_model": "m", "embedding_model": "e",
                                     "nvidia_embed_api_key": "nv-test", "nvidia_embed_model": "n",
                                     "nvidia_llm_api_key": "nv-llm-test", "nvidia_llm_model": "nvm",
                                     "gemini_api_key": "gm-test", "gemini_llm_model": "gemini-test",
                                     "xai_api_key": "xai-test", "xai_llm_model": "grok-test"})
    _lcfg_no_fallback = Config(**{**_llm_blank, "openai_api_key": "sk-test",
                                   "llm_model": "m", "embedding_model": "e",
                                   "nvidia_embed_api_key": "", "nvidia_embed_model": "n",
                                   "nvidia_llm_api_key": "", "nvidia_llm_model": "nvm",
                                   "gemini_api_key": "", "gemini_llm_model": "gemini-test",
                                   "xai_api_key": "", "xai_llm_model": "grok-test"})

    class _FakeEmbData:
        def __init__(self, vec: list[float]) -> None:
            self.embedding = vec

    class _FakeEmbResp:
        def __init__(self, vec: list[float]) -> None:
            self.data = [_FakeEmbData(vec)]

    class _FakeMsg:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeChoice:
        def __init__(self, content: str) -> None:
            self.message = _FakeMsg(content)

    class _FakeChatResp:
        def __init__(self, content: str) -> None:
            self.choices = [_FakeChoice(content)]
            self.usage = None

    def _openai_down(**_kw: Any) -> Any:
        raise RuntimeError("OpenAI 인증 실패(시뮬레이션)")

    _llm1 = LLMClient(_lcfg_with_fallback)
    _llm1.client.embeddings.create = _openai_down
    _llm1.nvidia_embed_client.embeddings.create = lambda **_kw: _FakeEmbResp([0.4, 0.5, 0.6])
    check("OpenAI 임베딩 실패 → NVIDIA 로 대체", _llm1.embed("테스트"), [0.4, 0.5, 0.6])
    check("채팅 대체 순서는 OpenAI→Gemini→Grok→NVIDIA",
          [c[3] for c in _llm1._chat_chain()],
          ["OpenAI", "Gemini(gemini-test)", "Grok(grok-test)", "NVIDIA(nvm)"])
    _llm1.client.chat.completions.create = _openai_down
    _llm1.gemini_llm_client.chat.completions.create = lambda **_kw: _FakeChatResp('{"ok":"gemini"}')
    check("OpenAI 채팅 실패 → Gemini 로 대체 (_chat)",
          _llm1._chat("s", "u"), ('{"ok":"gemini"}', {}))
    check("chat_text 도 동일하게 Gemini 로 대체", _llm1.chat_text("s", "u"), '{"ok":"gemini"}')
    _llm1.gemini_llm_client.chat.completions.create = _openai_down
    _llm1.xai_llm_client.chat.completions.create = lambda **_kw: _FakeChatResp('{"ok":"grok"}')
    check("OpenAI·Gemini 둘 다 실패 → Grok 로 대체",
          _llm1._chat("s", "u"), ('{"ok":"grok"}', {}))
    _llm1.xai_llm_client.chat.completions.create = _openai_down
    _llm1.nvidia_llm_client.chat.completions.create = lambda **_kw: _FakeChatResp('{"ok":"nvidia"}')
    check("OpenAI·Gemini·Grok 셋 다 실패 → NVIDIA 로 대체(4단계 체인 끝까지)",
          _llm1._chat("s", "u"), ('{"ok":"nvidia"}', {}))

    _llm2 = LLMClient(_lcfg_no_fallback)
    check("NVIDIA 키 없으면 임베딩 대체 클라이언트도 없다", _llm2.nvidia_embed_client, None)
    check("NVIDIA 키 없으면 채팅 대체 클라이언트도 없다", _llm2.nvidia_llm_client, None)
    check("xAI 키 없으면 Grok 대체 클라이언트도 없다", _llm2.xai_llm_client, None)
    check("Gemini 키 없으면 대체 클라이언트도 없다", _llm2.gemini_llm_client, None)
    _llm2.client.embeddings.create = _openai_down
    check("대체 키 없이 OpenAI 도 실패하면 None (기존과 동일)", _llm2.embed("테스트"), None)
    _llm2.client.chat.completions.create = _openai_down
    try:
        _llm2._chat("s", "u")
        _raised = False
    except RuntimeError:
        _raised = True
    check("채팅도 대체 없으면 예외가 그대로 올라온다(기존과 동일)", _raised, True)

    _llm3 = LLMClient(_lcfg_with_fallback)
    _llm3.client.embeddings.create = _openai_down
    _llm3.nvidia_embed_client.embeddings.create = _openai_down
    check("OpenAI·NVIDIA 둘 다 실패하면 None", _llm3.embed("테스트"), None)
    _llm3.client.chat.completions.create = _openai_down
    _llm3.gemini_llm_client.chat.completions.create = _openai_down
    _llm3.xai_llm_client.chat.completions.create = _openai_down
    _llm3.nvidia_llm_client.chat.completions.create = _openai_down
    try:
        _llm3._chat("s", "u")
        _raised = False
    except RuntimeError:
        _raised = True
    check("채팅 4곳(OpenAI·Gemini·Grok·NVIDIA) 전부 실패하면 예외가 올라온다", _raised, True)

    print("\n[11-2g] 분석 백로그 드레인 병렬화 — 동시 실행 정합성 (2026-09-14)")
    _tmp._exec("delete from articles"); _tmp._exec("delete from article_bodies")
    _tmp._exec("delete from summaries")
    _par_ids = [f"par-{i}" for i in range(8)]
    for i, pid in enumerate(_par_ids):
        _tmp._exec(
            "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
            " collected_at, source_type, importance_score, group_companies, categories, press_name,"
            " analyzed_at, status, is_representative) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, f"h/{pid}", f"h/{pid}", f"h/{pid}", f"제목-{i}", iso(now_utc()), iso(now_utc()),
             "search", 50, "[]", "[]", "한경", None, "active", 1))
        _tmp.save_body(pid, f"본문 내용 {i}", "fulltext")

    class _StubAnalyzeLLM:
        def analyze(self, title: str, press: str, body: str) -> Analysis:
            return Analysis(summary_sentences=[f"요약:{title}"], perspective="", keywords=[],
                            group_companies=[], sentiment="중립", ok=True)

        def pfm_tone(self, excerpt: str) -> tuple[str, str]:
            return "긍정", "테스트 근거"

    _pcfg = Config(**{**_llm_blank, "openai_api_key": "x", "llm_model": "m", "embedding_model": "e",
                      "nvidia_embed_api_key": "", "nvidia_embed_model": "n",
                      "nvidia_llm_api_key": "", "nvidia_llm_model": "nvm"})
    _pctx = Context(cfg=_pcfg, storage=_tmp, http=HttpClient())
    _pctx._llm = _StubAnalyzeLLM()

    _par_pendings = _tmp.unanalyzed_with_body(20)
    check("병렬 테스트 대상 8건 조회", len(_par_pendings), 8)

    def _analyze_pending_test(pending: dict) -> bool:
        row = {"id": pending["id"], "title": pending["title"], "press_id": pending.get("press_id"),
               "press_name": pending.get("press_name"),
               "importance_score": pending.get("importance_score") or 0,
               "group_companies": jload(pending.get("group_companies"), [])}
        return analyze_and_save(
            _pctx, pending["id"], row, pending["body"], pending["summary_source"]) is not None

    with ThreadPoolExecutor(max_workers=5) as pool:
        _par_results = list(pool.map(_analyze_pending_test, _par_pendings))
    check("8건 모두 성공(병렬 실행)", _par_results, [True] * 8)
    check("동시 실행 후 분석 대기 0건(전부 처리됨)", len(_tmp.unanalyzed_with_body(20)), 0)
    _par_summaries = {r["title"]: r.get("summary_text") for r in _tmp._rows(
        "select a.title, s.summary_text from articles a"
        " join summaries s on s.article_id=a.id where a.id like 'par-%'")}
    check("기사마다 자기 제목에 맞는 요약을 받았다(교차오염 없음)",
          len(_par_summaries) == 8 and all(v == f"요약:{k}" for k, v in _par_summaries.items()), True)

    print("\n[11-2h] _drain_deferred 병렬화 — 중복판정은 순차 유지 검증 (2026-09-14)")
    # LLM 호출만 병렬화하고 find_duplicate 는 여전히 한 항목씩 순서대로 돈다는 것을
    # 확인한다 — 같은 배치 안의 두 '중복' 기사 중 하나만 분석되고 나머지는 archived.
    _tmp._exec("delete from articles"); _tmp._exec("delete from article_bodies")
    _tmp._exec("delete from summaries")
    _now_iso = iso(now_utc())
    _dup_html = ("<html><body><article><p>" + ("포스코퓨처엠 중복 테스트 본문입니다. " * 12)
                + "</p></article></body></html>")
    _uniq_html = ("<html><body><article><p>" + ("포스코퓨처엠 단독 테스트 본문입니다. " * 12)
                 + "</p></article></body></html>")
    _pages = {
        "http://x.test/dup-a": _dup_html,
        "http://x.test/dup-b": _dup_html,
        "http://x.test/uniq-c": _uniq_html,
    }

    class _FakeDrainResp:
        def __init__(self, url: str, html: str) -> None:
            self.url = url
            self.content = html.encode("utf-8")
            self.encoding = "utf-8"
        def raise_for_status(self) -> None:
            pass

    class _FakeHttpDrain:
        def __init__(self, pages: dict[str, str]) -> None:
            self.pages = pages
        def get(self, url: str, allow_redirects: bool = True, timeout: float = 8) -> Any:
            return _FakeDrainResp(url, self.pages.get(url, "<html><body></body></html>"))

    for did, durl, dtitle in [
        ("dd-a", "http://x.test/dup-a", "포스코퓨처엠 중복기사A"),
        ("dd-b", "http://x.test/dup-b", "포스코퓨처엠 중복기사B"),
        ("dd-c", "http://x.test/uniq-c", "포스코퓨처엠 단독기사C"),
    ]:
        _tmp._exec(
            "insert into articles (id, url_source, url_canonical, url_original, title, published_at,"
            " collected_at, source_type, importance_score, group_companies, categories, press_name,"
            " analyzed_at, status, is_representative) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, durl, durl, durl, dtitle, _now_iso, _now_iso, "search", 50, "[]", "[]", "",
             None, "active", 1))

    _dctx = Context(cfg=_pcfg, storage=_tmp, http=_FakeHttpDrain(_pages))
    _dctx._llm = _StubAnalyzeLLM()
    _drained = _drain_deferred(_dctx, 10, [])
    check("드레인 처리 건수 — 중복 1건은 걸러지고 2건만 분석", _drained, 2)
    _dc = _tmp._one("select pfm_excerpt, pfm_tone, pfm_tone_reason from articles where id='dd-c'")
    check("분석 시 포스코퓨처엠 발췌·논조·근거가 함께 저장된다",
          (bool(_dc["pfm_excerpt"]), _dc["pfm_tone"], _dc["pfm_tone_reason"]),
          (True, "긍정", "테스트 근거"))
    check("분석 후에도 본문은 30일 보관된다(본문 검색용)",
          _tmp._one("select count(*) as n from article_bodies where article_id='dd-c'")["n"], 1)
    _final_status = {r["id"]: r["status"] for r in
                     _tmp._rows("select id, status from articles where id like 'dd-%'")}
    check("중복 쌍 중 하나는 archived, 나머지는 active 그대로",
          sorted(_final_status.values()) == ["active", "active", "archived"], True)
    _analyzed_ids = {r["id"] for r in _tmp._rows(
        "select id from articles where id like 'dd-%' and analyzed_at is not null")}
    check("실제로 분석 완료된 건 정확히 2건(중복 제외, 병렬 실행에도 안전)",
          len(_analyzed_ids), 2)

    print("\n[7-3] 네이버 키워드 교대 조회 — 일일 무료 한도 대응 (2026-09-15)")
    # 활성 키워드 118개를 매 회차 전부 조회하면 하루 33,984회로 무료 한도(25,000)를
    # 1.4배 초과해, 매일 중간부터 429 로 네이버 수집이 통째로 막혔다.
    _rot_rows = ([{"keyword": f"g{i}", "category": "그룹사"} for i in range(7)]
                 + [{"keyword": f"a{i}", "category": "산업"} for i in range(48)]
                 + [{"keyword": f"p{i}", "category": "정책"} for i in range(38)]
                 + [{"keyword": f"t{i}", "category": "통상"} for i in range(25)])
    check("테스트 입력은 실제와 같은 118개", len(_rot_rows), 118)
    _slot0 = select_naver_keywords(_rot_rows, 0)
    _slot1 = select_naver_keywords(_rot_rows, 1)
    check("한 회차 조회량이 절반 수준으로 준다", len(_slot0) < 70 and len(_slot1) < 70, True)
    check("그룹사 7개는 매 회차 전부 조회한다",
          (sum(1 for r in _slot0 if r["category"] == "그룹사"),
           sum(1 for r in _slot1 if r["category"] == "그룹사")), (7, 7))
    # 한 바퀴(NAVER_ROTATE_SLOTS 회차) 돌면 모든 키워드가 빠짐없이 조회돼야 한다
    _seen = set()
    for _c in range(naver_rotate_slots()):
        _seen |= {r["keyword"] for r in select_naver_keywords(_rot_rows, _c)}
    check("한 바퀴 돌면 모든 키워드가 빠짐없이 조회된다",
          _seen, {r["keyword"] for r in _rot_rows})
    check("회차가 한 바퀴 넘어가면 처음 슬롯으로 돌아온다",
          [r["keyword"] for r in select_naver_keywords(_rot_rows, naver_rotate_slots())],
          [r["keyword"] for r in _slot0])
    check("슬롯이 1이면 기존처럼 전부 조회(끄기 옵션)",
          len(select_naver_keywords(_rot_rows, 0)) if naver_rotate_slots() > 1 else 118,
          len(_slot0))
    check("키워드가 그룹사뿐이면 그대로 전부",
          len(select_naver_keywords([{"keyword": "g", "category": "그룹사"}], 3)), 1)

    print("\n[7-4] 수집 키워드 관리 CRUD (마스터 패널, 2026-09-15)")
    _tmp._exec("delete from keyword_sets")
    _kw_added = _tmp.add_keyword("산업", "테스트키워드")
    check("추가 성공 — 분류·키워드 반환", (_kw_added or {}).get("keyword"), "테스트키워드")
    check("추가 직후 켜짐(enabled) 상태", bool((_kw_added or {}).get("enabled")), True)
    check("같은 분류+키워드 중복 추가는 None(중복 방지)",
          _tmp.add_keyword("산업", "테스트키워드"), None)
    check("다른 분류면 같은 키워드도 별개로 추가된다",
          (_tmp.add_keyword("정책", "테스트키워드") or {}).get("keyword"), "테스트키워드")
    check("all_keywords 는 켜짐·꺼짐 상관없이 전부(방금 2건)", len(_tmp.all_keywords()), 2)
    _tmp.set_keyword_enabled(_kw_added["id"], False)
    check("끄면 enabled_keywords 에서 빠진다(1건만 남음)", len(_tmp.enabled_keywords()), 1)
    check("all_keywords 에는 꺼진 채로 계속 남는다", len(_tmp.all_keywords()), 2)
    _tmp.set_keyword_enabled(_kw_added["id"], True)
    check("다시 켜면 enabled_keywords 에 복귀한다", len(_tmp.enabled_keywords()), 2)
    _tmp.delete_keyword(_kw_added["id"])
    check("삭제하면 all_keywords 에서도 완전히 빠진다", len(_tmp.all_keywords()), 1)
    _tmp._exec("delete from keyword_sets")

    print("\n[7-2] 수집 소스 조회 병렬화 — collect_naver · collect_rss_feeds (2026-09-14)")
    # 활성 키워드가 100개를 넘어가며 순차 조회가 사이클 시간의 대부분을 차지하는
    # 것을 실측으로 확인했다 — 키워드·피드마다 독립 호출이라 병렬로 바꿨다.
    class _FakeNaverResp:
        def __init__(self, status_code: int, items: list[dict]) -> None:
            self.status_code = status_code
            self._items = items
        def raise_for_status(self) -> None:
            pass
        def json(self) -> dict:
            return {"items": self._items}

    class _FakeNaverHttp:
        def __init__(self) -> None:
            self.calls: list[str] = []
        def get(self, url: str, params: dict | None = None, headers: dict | None = None,
               **kw: Any) -> Any:
            kw_ = (params or {}).get("query", "")
            self.calls.append(kw_)
            return _FakeNaverResp(200, [{
                "originallink": f"http://n.test/{kw_}", "title": f"포스코퓨처엠 {kw_} 소식",
                "description": "설명", "pubDate": "Mon, 14 Sep 2026 00:00:00 +0900",
            }])

    _nblank = {f: "" for f in Config.__dataclass_fields__}
    _ncfg2 = Config(**{**_nblank, "naver_client_id": "id", "naver_client_secret": "sec"})
    _nhttp = _FakeNaverHttp()
    _nkw_rows = [{"keyword": f"kw{i}", "category": "산업"} for i in range(12)]
    _nitems = collect_naver(_nhttp, _ncfg2, _nkw_rows)
    check("12개 키워드 전부 조회됨(병렬 실행)", len(_nhttp.calls), 12)
    check("키워드마다 결과 1건씩 총 12건 수집", len(_nitems), 12)
    check("결과가 키워드별로 정확히 대응(교차오염 없음)",
          sorted(it.url_original for it in _nitems)
          == sorted(f"http://n.test/kw{i}" for i in range(12)), True)

    class _FakeNaver429Http:
        def get(self, url: str, params: dict | None = None, headers: dict | None = None,
               **kw: Any) -> Any:
            return _FakeNaverResp(429, [])
    check("429 응답이 계속되면 예외 없이 빈 결과", collect_naver(_FakeNaver429Http(), _ncfg2, _nkw_rows), [])

    # 순간 폭주로 처음 몇 건만 429 → 쉬었다 재시도해서 라운드 전체가 살아야 한다(2026-09-30).
    class _FakeNaverBurstHttp(_FakeNaverHttp):
        def __init__(self) -> None:
            super().__init__()
            self.first = 3
            self._lk = threading.Lock()
        def get(self, url: str, params: dict | None = None, headers: dict | None = None,
               **kw: Any) -> Any:
            with self._lk:
                if self.first > 0:
                    self.first -= 1
                    return _FakeNaverResp(429, [])
            return super().get(url, params=params, headers=headers, **kw)
    _burst = collect_naver(_FakeNaverBurstHttp(), _ncfg2, _nkw_rows)
    check("일시적 429(초반 3건) — 재시도해서 12개 키워드 결과를 모두 받는다", len(_burst), 12)
    check("네이버 조회 동시 작업 수는 10보다 낮다(순간 폭주 방지)", NAVER_FETCH_WORKERS < SOURCE_FETCH_WORKERS, True)

    print("\n[7-3] 원문 접속 실패 처리 — 기사가 조용히 사라지지 않는다 (2026-09-30)")
    _fb = RawItem(url_source="https://www.busan.com/a/1", url_original="https://www.busan.com/a/1?x=1",
                  title="포스코퓨처엠, 증설", published_at=None, source_type="naver_api", snippet="증설한다")
    check("접속 실패 시 제목·리드로 계속 — 기사 URL 을 그대로 쓴다",
          fetch_fallback_url(_fb), normalize_url("https://www.busan.com/a/1?x=1"))
    check("구글뉴스 중간 링크는 진행 불가(카드 링크로 못 씀)",
          fetch_fallback_url(RawItem("u", "https://news.google.com/rss/articles/CBM", "제목", None, "google_rss")), "")
    check("제목이 없거나 도메인 루트면 진행 불가",
          (fetch_fallback_url(RawItem("u", "https://a.com/x", "  ", None, "rss")),
           fetch_fallback_url(RawItem("u", "https://a.com/", "제목", None, "rss"))), ("", ""))
    check("영구 제외는 연속 3회 실패부터", FETCH_FAIL_LIMIT, 3)

    def _rss_xml(title: str, link: str) -> str:
        return (
            '<?xml version="1.0"?><rss version="2.0"><channel><item>'
            f"<title>{title}</title><link>{link}</link>"
            "<pubDate>Mon, 14 Sep 2026 00:00:00 +0900</pubDate>"
            "<description>desc</description></item></channel></rss>"
        )

    class _FakeRssResp:
        def __init__(self, content: bytes) -> None:
            self.content = content
        def raise_for_status(self) -> None:
            pass

    class _FakeRssHttp:
        def __init__(self) -> None:
            self.calls: list[str] = []
        def get(self, url: str, **kw: Any) -> Any:
            self.calls.append(url)
            idx = url.rsplit("/", 1)[-1]
            xml = _rss_xml(f"포스코퓨처엠 피드{idx}", f"http://r.test/{idx}")
            return _FakeRssResp(xml.encode("utf-8"))

    _feed_rows = [{"name": f"feed{i}", "url": f"http://f.test/{i}"} for i in range(6)]
    _rhttp = _FakeRssHttp()
    _ritems = collect_rss_feeds(_rhttp, _feed_rows)
    check("6개 피드 전부 조회됨(병렬 실행)", len(_rhttp.calls), 6)
    check("피드마다 결과 1건씩 총 6건 수집", len(_ritems), 6)

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

    # SPA(Next.js) 본문 추출 — <script> JSON 문단을 읽는다 (news1.kr 등)
    _spa = ('<html><body><nav>메뉴 메뉴 메뉴</nav>'
            '<script>{"body":[{"type":"text","content":"\uc0c1\uc7a5\ubc95\uc778 \uc2dc\uac00\ucd1d\uc561\uc774 \ub298\uc5c8\ub2e4."},'
            '{"type":"image","content":"x.jpg"},'
            '{"type":"text","content":"\ubc95\uc778\ubcc4\ub85c\ub294 \ud3ec\uc2a4\ucf54\ud4e8\ucc98\uc5e0 \uc21c\uc774\ub2e4."}]}</script>'
            '</body></html>')
    _jb = _json_script_body(_spa)
    check("JSON 문단 2개를 이어붙임", _jb.count("\n"), 1)
    check("JSON 본문에 포스코퓨처엠 포함", "포스코퓨처엠" in _jb, True)
    check("image 타입은 본문에서 제외", "x.jpg" in _jb, False)
    check("extract_body 가 JSON 본문을 택함", "포스코퓨처엠" in extract_body(_spa), True)
    check("JSON 마커 없으면 기존 경로 유지",
          _json_script_body("<html><body><p>일반 기사</p></body></html>"), "")
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

    print("\n[11-5b] 파이프라인 락 — 다중 인스턴스 오배포 방지 (AWS 배포 전 점검, 2026-09-11)")
    check("아무도 없으면 얻는다", _tmp.try_acquire_pipeline_lock("me", 300), True)
    check("내가 이미 쥐고 있으면 다시 얻는다(재갱신)", _tmp.try_acquire_pipeline_lock("me", 300), True)
    check("남이 쥐고 있고 신선하면 못 얻는다", _tmp.try_acquire_pipeline_lock("other", 300), False)
    # iso() 는 초 단위까지만 기록해서(SQLite 문자열 정렬용) 같은 초 안의 연속
    # 호출은 lock_at 이 다 똑같다 — stale_after_sec 를 음수로 줘서 '만료 기준
    # 시각'을 미래로 만들면, 방금 갱신한 락도 확실히 '오래된 것'으로 취급된다.
    check("남이 쥐고 있어도 오래됐으면(죽은 소유자) 얻는다",
          _tmp.try_acquire_pipeline_lock("other", -60), True)

    print("\n[11-6] cmd_fixcategories — 인사·부고 태그는 재태깅에서 보호 (회귀 방지)")
    # 2026-09-10 발견: detect_categories 는 인사·부고를 모르는데 cmd_fixcategories 가
    # 전체 기사의 categories 를 그걸로 덮어써, 실행 시점마다 인사·부고 태그가 지워지고
    # 탭에서 기사가 사라졌다(104건 피해). people_news_kind 대상은 건너뛰도록 고쳤다.
    _tmp._exec("delete from articles")
    _tmp._exec(
        "insert into articles (id, url_source, url_canonical, url_original, title,"
        " published_at, collected_at, source_type, categories, status) values (?,?,?,?,?,?,?,?,?,?)",
        ("art-people", "http://r/people", "http://r/people", "http://r/people",
         "[부고] 김철수(전 삼성전자 부사장)씨 별세", _d030, _d030, "rss", '["인사·부고"]', "active"))
    _tmp._exec(
        "insert into articles (id, url_source, url_canonical, url_original, title,"
        " published_at, collected_at, source_type, categories, status) values (?,?,?,?,?,?,?,?,?,?)",
        ("art-normal", "http://r/normal", "http://r/normal", "http://r/normal",
         "포스코 주가 코스피 상한가", _d030, _d030, "rss", "[]", "active"))
    from types import SimpleNamespace
    cmd_fixcategories(SimpleNamespace(storage=_tmp))
    check("인사·부고 기사는 태그 유지",
          jload(_tmp._one("select categories from articles where id='art-people'")["categories"], []),
          ["인사·부고"])
    check("일반 기사는 그대로 재태깅됨(회귀 아님)",
          "시장/주가" in jload(_tmp._one("select categories from articles where id='art-normal'")["categories"], []),
          True)

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

    print("\n[19] 로그인 무차별 대입 방지")
    from types import SimpleNamespace
    # /api/web/login·/api/master/login 에 시도 횟수 제한이 없어서 비밀번호를
    # 무제한으로 시도할 수 있었다(2026-09-11 지적). IP 별 5분 슬라이딩 윈도.
    for _ in range(LOGIN_MAX_FAILS):
        _login_fail("1.2.3.4")
    check("5회 실패하면 잠김", _login_locked("1.2.3.4"), True)
    check("다른 IP 는 영향 없음", _login_locked("5.6.7.8"), False)
    _login_reset("1.2.3.4")
    check("성공하면(reset) 다시 시도 가능", _login_locked("1.2.3.4"), False)
    check("X-Forwarded-For 첫 값을 클라이언트 IP 로 씀",
          _client_ip(SimpleNamespace(
              headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"},
              client=SimpleNamespace(host="127.0.0.1"))),
          "9.9.9.9")
    check("X-Forwarded-For 없으면 소켓 주소로",
          _client_ip(SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))),
          "127.0.0.1")
    _LOGIN_FAILS.clear()

    print("\n[19-2] 마스터 비밀번호 자동 복구 (2026-09-15)")
    check("/api/master/login 은 사이트 잠금과 무관하게 항상 열려 있어야 함",
          "/api/master/login" in WEB_PUBLIC_PATHS, True)
    _MASTER_LOGIN_FAILS.clear()
    for _i in range(MASTER_LOGIN_FAIL_THRESHOLD - 1):
        check(f"{_i + 1}회째는 아직 임계치 미만",
              _master_login_fail("9.9.9.9") >= MASTER_LOGIN_FAIL_THRESHOLD, False)
    check("임계치(3회)째 도달", _master_login_fail("9.9.9.9") >= MASTER_LOGIN_FAIL_THRESHOLD, True)
    _master_login_reset("9.9.9.9")
    check("reset 후 다시 0부터", _master_login_fail("9.9.9.9"), 1)
    _MASTER_LOGIN_FAILS.clear()

    _rec_cfg = replace(_pcfg, master_password="testpw123", master_pw_recovery_to=["r@x.com"],
                       smtp_user="", smtp_app_password="")
    _rec_ctx = Context(cfg=_rec_cfg, storage=_tmp, http=HttpClient())
    _tmp.set_run_state({"master_pw_hash": "이전에-바뀐-해시"})
    check("복구 전: DB 해시가 남아 있음", bool(_tmp.get_run_state().get("master_pw_hash")), True)
    _recover_master_password(_rec_ctx)
    check("복구 후: DB 해시가 지워져 .env 값으로 돌아감",
          bool(_tmp.get_run_state().get("master_pw_hash")), False)
    check("check_master_password: 복구 후 .env 값으로 로그인 성공",
          check_master_password(_rec_ctx, "testpw123"), True)
    _tmp.set_run_state({"master_pw_hash": ""})

    print("\n[19-3] 웹/마스터 잠금 사용 여부 선택 (2026-09-16)")
    _tmp.set_run_state({"web_lock_enabled": None, "web_password": "abcd1234", "web_pw_hash": ""})
    check("web_lock_enabled 미지정 + 비밀번호 있음 → 레거시대로 잠금 켜짐",
          bool(_rec_ctx.storage.get_run_state().get("web_password")), True)
    _tmp.set_run_state({"web_lock_enabled": 0})
    check("web_lock_enabled=0 이면 비밀번호가 있어도 명시적으로 꺼짐",
          str(_tmp.get_run_state().get("web_lock_enabled")) in ("0", "False", "false"), True)
    _tmp.set_run_state({"web_lock_enabled": 1})
    check("web_lock_enabled=1 로 다시 켤 수 있음",
          str(_tmp.get_run_state().get("web_lock_enabled")) not in ("0", "False", "false"), True)
    _tmp.set_run_state({"web_lock_enabled": None, "web_password": "", "web_pw_hash": ""})

    check("master_lock_enabled 끄기 전에는 빈 값이 여전히 거부됨",
          check_master_password(_rec_ctx, ""), False)
    _tmp.set_run_state({"master_lock_enabled": 0})
    check("master_lock_enabled=0 이면 빈 값도 통과(잠금 해제)",
          check_master_password(_rec_ctx, ""), True)
    check("master_lock_enabled=0 이면 아무 문자열이나 통과",
          check_master_password(_rec_ctx, "아무거나"), True)
    _tmp.set_run_state({"master_lock_enabled": 1})
    check("master_lock_enabled=1 로 되돌리면 다시 검증함",
          check_master_password(_rec_ctx, "틀린값"), False)
    _tmp.set_run_state({"master_lock_enabled": None})

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
           "summary_text": "짧은 요약 문장.", "pfm_excerpt": "포스코퓨처엠이 증설한다.",
           "url_canonical": "https://x.test/a"}
    _ft = _kakao_text_from_row(_fr, "https://x.test/a")
    check("row text — object_type=text", _ft["object_type"], "text")
    check("row text — 점수 85 는 🔴", _ft["text"].startswith("🔴"), True)
    check("row text — 태그가 머리줄에", "[포스코퓨처엠]" in _ft["text"], True)
    check("row text — 언론사·기자 머리표", "[뉴스1, 김기자]" in _ft["text"], True)
    check("row text — 포스코 관점은 더 이상 안 나간다", "포스코 관점" in _ft["text"], False)
    check("row text — 포스코퓨처엠 언급 발췌 포함", "포스코퓨처엠 언급:" in _ft["text"], True)
    check("row text — 링크는 본문 아닌 link 필드", "http" not in _ft["text"], True)
    check("row text — 원문 버튼", _ft["button_title"], "원문 보기")
    check("row text — 긴 요약도 200자 이하",
          len(_kakao_text_from_row({**_fr, "summary_text": "요" * 500,
                                    "pfm_excerpt": "관" * 500},
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
    # 실제 취약점(2026-09-11 배포 전 점검): href 속성에 esc()(따옴표 미이스케이프)를
    # 써서, 큰따옴표가 든 URL이 속성 밖으로 튀어나가 이벤트 핸들러를 주입할 수
    # 있었다(canonical 링크는 수동 등록 시 공격자가 통제하는 페이지에서 올 수 있다).
    # esc_attr() 로 고쳤다 — href 는 항상 이걸 써야 한다.
    _xss_url = 'https://evil.com/x" onmouseover="alert(1)'
    _xss_html = render_weekly_html({
        "period_start": "2026-08-31", "period_end": "2026-09-07", "article_count": 1,
        "sections": [{"label": "테스트", "articles": [
            {"title": "제목", "url": _xss_url, "press": "언론사",
             "published_at": "2026-09-01", "score": 50, "summary": "요약"}]}]})
    check("href 속성의 큰따옴표는 이스케이프됨(XSS 방지)",
          'onmouseover="alert' not in _xss_html and "&quot;" in _xss_html, True)
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
    # 그룹사 이름 옆 주간 점수 + 이유 (2026-10-02)
    _hits = _weekly_hits(_rows, "group", "포스코퓨처엠")
    _sc = weekly_group_score(_hits)
    # 최고 80×0.5=40 + 상위(80,30)평균 55×0.3=16.5→(반올림 합산) + 보도량 2건×2=4 → 60.5 → 60 또는 61(반올림)
    check("그룹사 주간 점수 — 계산식(최고×0.5+상위5평균×0.3+보도량)", (_sc["value"], _sc["count"]), (60, 2))
    check("그룹사 주간 점수 — 이유에 계산 내역이 그대로 있다",
          all(x in _sc["reason"] for x in ("기사 2건", "80점 → 40점", "평균 55점", "보도량 2건 → 4점")), True)
    check("그룹사 주간 점수 — 기사 없으면 0점·안내", weekly_group_score([]), {"value": 0, "count": 0, "reason": "이번 주 해당 기사가 없습니다."})
    check("그룹사 주간 점수 — 부정 30% 이상이면 대응 검토 문구",
          "대응 검토" in weekly_group_score([{"importance_score": 50, "sentiment": "부정", "title": "a"},
                                           {"importance_score": 40, "sentiment": "중립", "title": "b"}])["reason"], True)
    _sh = render_weekly_html({"period_start": "2026-08-31", "period_end": "2026-09-07", "article_count": 2, "sections": [
        {"label": "포스코퓨처엠", "kind": "group", "score": _sc, "swot": None, "impact": None,
         "articles": [{"title": "t", "url": "http://a", "press": "p", "published_at": "2026-09-05", "score": 70, "summary": "s"}]}]})
    check("주간 HTML — 그룹사 이름 옆 점수와 이유", ("주간 60점" in _sh, "wr-score-why" in _sh), (True, True))
    # 정부/정책·통상 섹션 — 기사별·주간 포스코퓨처엠 영향(긍정·중립·부정) (2026-10-06)
    _ov, _its = normalize_pfm_impact({
        "pfm_tone": "긍정", "pfm_impact": "  핵심광물 비축 지원으로\n원료 조달 부담이 줄 수 있다. ",
        "items": [{"n": 1, "tone": "부정", "impact": "관세 인상으로 수출 부담"},
                  {"n": 2, "tone": "엉터리", "impact": "직접 영향 없음"},
                  {"n": 2, "tone": "긍정", "impact": "중복 번호는 무시"},
                  {"n": 9, "tone": "긍정", "impact": "범위 밖 번호는 무시"},
                  {"n": "x", "tone": "긍정", "impact": "숫자 아님"},
                  "문자열", {"n": 3, "tone": "긍정", "impact": "   "}]}, 3)
    check("언론사 색 — 긍정 9·부정 1(차이 80%) → 긍정", press_tone_color({"긍정": 9, "부정": 1, "중립": 5}), "긍정")
    check("언론사 색 — 부정 8·긍정 2(차이 60%, 넘지 않음) → 중립", press_tone_color({"긍정": 2, "부정": 8}), "중립")
    check("언론사 색 — 부정 9·긍정 1(차이 80%) → 부정", press_tone_color({"긍정": 1, "부정": 9}), "부정")
    check("언론사 색 — 긍정 3·부정 2(차이 20%) → 중립", press_tone_color({"긍정": 3, "부정": 2}), "중립")
    check("언론사 색 — 긍정 1건만 → 긍정(차이 100%)", press_tone_color({"긍정": 1}), "긍정")
    check("언론사 색 — 중립만 → 중립, 판정 없음 → ''", (press_tone_color({"중립": 4}), press_tone_color({"미판정": 3})), ("중립", ""))
    check("주간 영향 — 종합 톤·공백 정리", (_ov["tone"], "\n" in _ov["text"], _ov["text"].startswith("핵심광물")), ("긍정", False, True))
    check("주간 영향 — 기사별 톤", [i and i["tone"] for i in _its], ["부정", "중립", None])
    check("주간 영향 — 중복·범위 밖·빈 본문은 버린다", (_its[1]["text"], _its[2]), ("직접 영향 없음", None))
    check("주간 영향 — 빈 응답이면 전부 None", normalize_pfm_impact({}, 2), (None, [None, None]))
    check("주간 영향 — 잘못된 톤은 중립", normalize_pfm_impact({"pfm_tone": "good", "pfm_impact": "x"}, 0)[0]["tone"], "중립")
    _ph = render_weekly_html({"period_start": "2026-08-31", "period_end": "2026-09-07", "article_count": 1, "sections": [
        {"label": "정부/정책", "kind": "topic", "swot": None, "score": None, "impact": "그룹 영향 문단",
         "pfm": {"tone": "부정", "text": "수출 부담 <b>커짐</b>"},
         "articles": [{"title": "t", "url": "http://a", "press": "p", "published_at": "2026-09-05", "score": 70,
                       "summary": "s", "pfm": {"tone": "긍정", "text": "원료 조달 숨통"}}]}]})
    check("주간 HTML — 기사별 영향 줄(긍정 배지)", ("wr-pfm-item" in _ph, "원료 조달 숨통" in _ph, "긍정" in _ph), (True, True, True))
    check("주간 HTML — 주간 포스코퓨처엠 영향 박스(부정) + 이스케이프",
          ("wr-pfm-week" in _ph, "이번 주 포스코퓨처엠에 미치는 영향 · 부정" in _ph, "&lt;b&gt;커짐" in _ph), (True, True, True))
    check("주간 HTML — 기존 그룹 영향 문단도 유지", "그룹 영향 문단" in _ph, True)
    _po = render_weekly_html({"period_start": "2026-08-31", "period_end": "2026-09-07", "article_count": 1, "sections": [
        {"label": "정부/정책", "kind": "topic", "swot": None, "score": None, "impact": "",
         "articles": [{"title": "t", "url": "http://a", "press": "p", "published_at": "2026-09-05", "score": 70, "summary": "s"}]}]})
    check("주간 HTML — 예전 레포트(영향 필드 없음)도 렌더", ("wr-pfm-item" in _po, "wr-pfm-week" in _po), (False, False))

    class _FakeWeeklyLLM:
        def weekly_brief(self, kind, name, arts):
            return {"impact": "그룹 영향", "pfm_tone": "중립", "pfm_impact": "종합",
                    "items": [{"n": i, "tone": "긍정", "impact": f"영향{i}"} for i in range(1, len(arts) + 1)]}
    _fk = _FakeWeeklyLLM()
    _fb = _fk.weekly_brief("impact", "정부/정책", _rows[:2])
    _fo, _fi = normalize_pfm_impact(_fb, 2)
    check("주간 영향 — LLM 응답 형식 → 정규화 연결", (_fo["tone"], [i["text"] for i in _fi]), ("중립", ["영향1", "영향2"]))

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
        # 카테고리는 정책 동향·국회 의안 2개(2026-10-02 개편) + item_type 매핑(옛 키는 호환용으로 남김)
        check("카테고리 6개", [c["key"] for c in ea_mod.EA_CATEGORIES], ["policy", "notice", "bill", "trade", "grant", "calendar"])
        check("정책 동향 → policy_press", ea_mod.EA_CATEGORY_TYPES["policy"], ["policy_press"])
        check("부처별 동향 → ministry_news", ea_mod.EA_CATEGORY_TYPES["ministry"], ["ministry_news"])
        check("통상 환경 → trade_webzine(월간 통상)", ea_mod.EA_CATEGORY_TYPES["trade"], ["trade_webzine"])
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

        print("\n[16-2] 대외협력 - Supabase/SQLite 공용 저장소 계층")
        # EaDB(SQLite)·EaSupabaseDB 양쪽이 이 함수 하나로 정렬해 백엔드가 바뀌어도
        # 화면에 보이는 순서가 달라지지 않는다. (2026-09-10 EA -> Supabase 이관)
        _er = [
            {"id": "a", "notice_end": "2026-09-20", "collected_at": "2026-09-01T00:00:00Z",
             "impact_level": "low"},
            {"id": "b", "notice_end": None, "collected_at": "2026-09-05T00:00:00Z",
             "impact_level": "high"},
            {"id": "c", "notice_end": "2026-09-10", "collected_at": "2026-09-02T00:00:00Z",
             "impact_level": "medium"},
        ]
        check("deadline 정렬 - 마감일 있는 항목 먼저, 오름차순",
              [r["id"] for r in ea_mod._ea_sort_rows(_er, "deadline")], ["c", "a", "b"])
        check("recent 정렬 - notice_end 무관, notice_start/collected_at 내림차순",
              [r["id"] for r in ea_mod._ea_sort_rows(_er, "recent")], ["b", "c", "a"])
        check("impact 정렬 - 영향도 등급 내림차순(high>medium>low)",
              [r["id"] for r in ea_mod._ea_sort_rows(_er, "impact")], ["b", "c", "a"])
        _ev = {"title": "산업가속화법 시행령 개정안", "category": "통상", "agency": "산업통상부",
              "notice_start": "2026-09-01", "notice_end": "2026-09-20", "d_day": "D-10",
              "status": "예고중", "group_companies": ["포스코"], "summary": "요약문",
              "impact_level": "high", "impact_rationale": "제3조 인용",
              "suggested_action": "의견서 제출 검토", "url": "https://x.test/a"}
        _msg = ea_mod._ea_format_message(_ev)
        check("텔레그램 메시지에 제목·기관·요약·영향도·원문 전부 포함",
              all(s in _msg for s in ("산업가속화법", "산업통상부", "요약문", "높음", "x.test/a")), True)

    if ea_mod is not None:
        print("\n[16-3] 대외협력 개편 — 정책브리핑 보도자료 · 국회의안 발의자 · 우선순위(긴급/중요/관심/일반)")
        import ea_crawl as _eac
        _list_html = (
            '<ul><li><a href="https://www.korea.kr/briefing/pressReleaseView.do?newsId=156784008&amp;pageIndex=1">'
            '<span class="text"><strong class="is-truncated"></strong>'
            '<span class="lead is-truncated">K-원전 팀코리아, 미국 진출 시동건다 - 원전수출기획위 개최 산업통상부는 …공급망···</span></span>'
            '<span class="info"><span>2026.10.01</span><span>산업통상부</span></span></a></li>'
            '<li><a href="/briefing/pressReleaseView.do?newsId=156784003">'
            '<span class="text"><strong>(참고자료)배터리 재활용 지원 확대</strong>'
            '<span class="lead">기후에너지환경부는 전지 재활용…</span></span>'
            '<span class="info"><span>2026.09.30</span><span>기후에너지환경부</span></span></a></li></ul>')
        _pr = _eac.parse_korea_press_list(_list_html)
        check("보도자료 목록 — 링크·날짜·소관 부처 추출",
              [(r["news_id"], r["date"], r["agency"]) for r in _pr],
              [("156784008", "2026-10-01", "산업통상부"), ("156784003", "2026-09-30", "기후에너지환경부")])
        check("보도자료 목록 — 제목이 비면 빈 값(상세에서 채운다), 있으면 그대로",
              [r["title"] for r in _pr], ["", "(참고자료)배터리 재활용 지원 확대"])
        check("보도자료 목록 — 본문 앞부분은 말줄임표를 떼고 담는다", _pr[0]["lead"].endswith("공급망"), True)
        check("보도자료 상세 — h1 제목에서 (참고자료) 말머리를 뗀다",
              _eac.parse_korea_press_title("<html><h1>(참고자료, 1(목) 16시엠바고)원전수출진흥과 팀코리아 진출</h1></html>"),
              "원전수출진흥과 팀코리아 진출")
        check("제목이 비면 본문 첫머리에서 임시 제목",
              _eac._first_phrase("K-원전 팀코리아, 미국 진출 시동건다 - 원전수출기획위 개최"), "K-원전 팀코리아, 미국 진출 시동건다")

        def _pv(**kw):
            base = {"title": "", "summary": "", "impact_rationale": "", "law_name": "", "status": "",
                    "group_companies": [], "impact_level": "", "d_day": None, "category": ""}
            base.update(kw)
            return base
        check("우선순위 긴급 — 퓨처엠 직접(배터리) + 시행 단계",
              ea_mod.ea_priority(_pv(title="이차전지 핵심광물 지원 확대", summary="내년 1월부터 시행한다"))[0], "긴급")
        check("우선순위 중요 — 직접 영향이지만 임박 단계 없음",
              ea_mod.ea_priority(_pv(title="양극재 기술 로드맵 논의", summary="간담회를 열었다"))[0], "중요")
        check("우선순위 긴급 — 마감 D-7 이내도 임박",
              ea_mod.ea_priority(_pv(title="배터리 안전 기준 행정예고", d_day=5))[0], "긴급")
        check("우선순위 관심 — 다른 그룹사 사업만",
              ea_mod.ea_priority(_pv(title="건설산업 규제 개선", group_companies=["포스코이앤씨"]))[0], "관심")
        check("우선순위 일반 — 연결 없음", ea_mod.ea_priority(_pv(title="청소년 쉼터 운영 안내"))[0], "일반")
        check("우선순위 — 이유 문장이 함께 나온다(기준이 보이게)",
              "배터리" in ea_mod.ea_priority(_pv(title="배터리 규제", summary="공포"))[1], True)

        check("발의자 이름 분리 — '의원'·'외 N인' 꼬리 제거",
              ea_mod._split_names("김철수의원 외 12인, 이영희 의원, 박민수"), ["김철수", "이영희", "박민수"])
        _mem_html = ("<table><tr><th>이름</th><th>정당</th></tr>"
                     "<tr><td>김철수</td><td>더불어민주당</td></tr><tr><td>이영희</td><td>국민의힘</td></tr></table>")
        check("발의자 명단 페이지 — 이름·정당 파싱",
              ea_mod.parse_member_list_html(_mem_html),
              [{"name": "김철수", "party": "더불어민주당"}, {"name": "이영희", "party": "국민의힘"}])
        _real_roster = ea_mod._party_roster
        ea_mod._party_roster = lambda: {"김철수": "더불어민주당", "이영희": "국민의힘", "박민수": ""}
        check("의안 발의자 — 대표 먼저, 공동발의자 전원, 정당은 표에 있는 만큼(동명이인은 비움)",
              ea_mod.resolve_bill_proposers({"_rst": "김철수", "_pub": "이영희, 박민수"}),
              [{"name": "김철수", "party": "더불어민주당", "role": "대표"},
               {"name": "이영희", "party": "국민의힘", "role": "공동"},
               {"name": "박민수", "party": "", "role": "공동"}])
        ea_mod._party_roster = _real_roster

        _edb = ea_mod.EaDB(os.path.join(__import__("tempfile").mkdtemp(), "ea.db"))
        _edb.exec("create table if not exists ea_policy_items (id TEXT primary key, url_source TEXT unique,"
                  " url_canonical TEXT, item_type TEXT, title TEXT)")
        _edb._ensure_cols()
        check("sqlite — proposers 컬럼이 없던 DB 도 자동으로 보강한다",
              "proposers" in {r["name"] for r in _edb.rows("pragma table_info(ea_policy_items)")}, True)

        class _FakeT:
            def __init__(self, store, fail):
                self.store, self.fail = store, fail

            def insert(self, row):
                self.row = row
                return self

            def execute(self):
                if self.fail and "proposers" in self.row:
                    raise RuntimeError("Could not find the 'proposers' column of 'ea_policy_items' (PGRST204)")
                self.store.append(self.row)

        _store: list = []
        _sdb = object.__new__(ea_mod.EaSupabaseDB)
        _sdb._t = lambda name: _FakeT(_store, True)
        check("Supabase — proposers 칼럼이 아직 없으면 그 칸만 빼고 저장(수집이 멈추지 않는다)",
              (_sdb.insert_item({"id": "x", "proposers": "[]"}), _store), (True, [{"id": "x"}]))
        _ts_home = ('<nav><ul><li><a href="https://tongsangnews.kr/webzine/202609/2026090180044.html" class="on">글로벌 통상 뉴스</a></li>'
                    '<li><a href="https://tongsangnews.kr/webzine/202609/2026090180116.html">통상 트렌드</a></li></ul></nav>'
                    '<div><a href="https://tongsangnews.kr/webzine/202609/2026090180044.html"><span class="img_enlarg"><img src="x.png"></span>'
                    '<div class="text"><em>글로벌 통상 뉴스</em><strong>한국 기업 반사이익</strong></div></a></div>')
        _tsl = _eac.parse_tongsang_index(_ts_home)
        check("월간 통상 목록 — 링크·분류·날짜(기사 id 앞 8자리)·중복 제거",
              [(r["id"], r["category"], r["date"]) for r in _tsl],
              [("2026090180044", "글로벌 통상 뉴스", "2026-09-01"), ("2026090180116", "통상 트렌드", "2026-09-01")])
        _ts_art = ('<html><meta name="title" content="한국 기업, 반사이익 기대 속 산업부 긴급 대응 나서"/>'
                   '<meta property="og:image" content="https://tongsangnews.kr/site/data/img/2026/09/2026090180044_0.jpg"/>'
                   '<div class="nav-guide"><div class="cat_name">이달의뉴스 <svg></svg> 글로벌 통상 뉴스</div></div>'
                   '<div class="contents-tit"><span class="sub">폴리실리콘에 232조 관세 15%</span>'
                   '<strong class="main">한국 기업, 반사이익 기대 속 산업부 긴급 대응 나서</strong></div>'
                   '<div class="editor-template"><div class="par"><p>미국 정부가 폴리실리콘에 관세를 도입했다.</p>'
                   '<p>산업통상부는 긴급 대책 회의를 열었다.</p></div></div></html>')
        _tsa = _eac.parse_tongsang_article(_ts_art)
        check("월간 통상 기사 — 제목·분류·썸네일·본문 문단",
              (_tsa["title"], _tsa["category"], _tsa["thumb"].endswith("2026090180044_0.jpg"),
               _tsa["body"].count("\n"), _tsa["sub"]),
              ("한국 기업, 반사이익 기대 속 산업부 긴급 대응 나서", "글로벌 통상 뉴스", True, 1, "폴리실리콘에 232조 관세 15%"))
        _tv = ea_mod._item_view({"id": "t1", "title": "관세 대응", "item_type": "trade_webzine",
                                 "attachment_urls": '["https://tongsangnews.kr/a.jpg"]', "agency_raw": "산업통상부"})
        check("월간 통상 뷰 — 썸네일은 thumbnail 로, 소관은 산업통상부", (_tv["thumbnail"], _tv["agency"]),
              ("https://tongsangnews.kr/a.jpg", "산업통상부"))
        _gt = ea_mod.Gates.__new__(ea_mod.Gates)
        _gt.db = type("D", (), {"known_url_sources": lambda self, u: set(), "upsert_ledger": lambda *a: None})()
        _gt.seen, _gt.agency_names, _gt.extra_terms = set(), set(), []
        _gt.counts = {"fetched": 0, "g0": 0, "g1": 0, "g2": 0, "g2_5": 0, "off_topic": 0}
        check("월간 통상은 관련성 검사 없이 통과(출처가 통상 자료)",
              len(_gt.filter([{"url_source": "u1", "title": "분류명", "_trusted": True},
                              {"url_source": "u2", "title": "청소년 쉼터 안내"}])), 1)
        try:
            from fastapi import FastAPI as _FA
            from fastapi.testclient import TestClient as _TC
        except Exception:
            _TC = None
        if _TC is not None:
            _cdir = __import__("tempfile").mkdtemp()
            _cpath = os.path.join(_cdir, "cal.db")
            _cc = sqlite3.connect(_cpath)
            _cc.executescript(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_sqlite.sql"),
                                   encoding="utf-8").read())
            _cc.commit()
            _cc.close()
            _cctx = type("C", (), {"cfg": type("G", (), {"db_backend": "sqlite", "sqlite_path": _cpath})()})()
            _capp = _FA()
            ea_mod.register_api(_capp, _cctx)
            _cdb = ea_mod.EaDB(_cpath)
            _ct = datetime.now(KST).date()
            for _i, (_ty, _tt, _off) in enumerate([("legislation", "산업안전보건법 시행령 입법예고", 3),
                                                   ("admin_notice", "배터리 안전 행정예고", 20),
                                                   ("legislation", "지난 예고", -400)]):
                _cdb.insert_item({"id": f"c{_i}", "url_source": f"cu{_i}", "url_canonical": f"cu{_i}", "item_type": _ty,
                                  "title": _tt, "notice_end": (_ct + timedelta(days=_off)).isoformat(),
                                  "collected_at": "2026-10-01T00:00:00Z"})
            _cl = _TC(_capp)
            _cal = _cl.get("/api/ea/calendar", params={"month": (_ct + timedelta(days=3)).strftime("%Y-%m")}).json()
            check("일정 API — 마감일이 있는 항목을 달별로, 종류 라벨·D-day 포함",
                  (any(e["kind"] == "입법예고 마감" and e["d_day"] == 3 for e in _cal["events"]),
                   any(e["title"] == "지난 예고" for e in _cal["events"])), (True, False))
            check("일정 API — 오늘 이후 마감만 '다가오는 마감'(가까운 순)",
                  [e["title"] for e in _cal["upcoming"]], ["산업안전보건법 시행령 입법예고", "배터리 안전 행정예고"])
            check("일정 API — 달 형식이 틀리면 이번 달로", _cl.get("/api/ea/calendar", params={"month": "xx"}).json()["month"],
                  _ct.strftime("%Y-%m"))
            check("입법·행정예고 목록 — 두 유형을 마감 임박순으로",
                  [i["title"] for i in _cl.get("/api/ea/items", params={"item_type": "legislation,admin_notice",
                                                                         "sort": "deadline"}).json()["items"]][:2],
                  ["지난 예고", "산업안전보건법 시행령 입법예고"])
        # 공모·수요조사 공고(산업부 사업공고 · 기후부 공지·공고 · IRIS) — 서비스키 없이 공개 페이지만 읽는다
        _mt = ('<table><tbody><tr><td data-cell-header="공고번호">2026-613</td><td class="ta-l" data-cell-header="제목">'
               '<div class="board-link"><a href="https://www.motir.go.kr/kor/article/ATCL2826a2625/71333/view?mno=&amp;pageIndex=1">'
               '<i>2026년도 소재부품기술개발사업(4차) 신규지원 대상과제 공고</i></a></div></td>'
               '<td data-cell-header="담당부서">산업공급망정책과</td><td data-cell-header="등록일">2026-09-22</td>'
               '<td data-cell-header="조회수">4,815</td><td data-cell-header="첨부파일"><a href="https://www.motir.go.kr/attach/down/x1">f</a></td></tr></tbody></table>')
        _mtr = _eac.parse_motir_notice_list(_mt)
        check("산업부 사업공고 목록 — 공고번호·제목·담당부서·등록일·첨부(주소의 쿼리는 뗀다)",
              (_mtr[0]["no"], _mtr[0]["title"], _mtr[0]["dept"], _mtr[0]["date"], _mtr[0]["url"], len(_mtr[0]["attachments"])),
              ("2026-613", "2026년도 소재부품기술개발사업(4차) 신규지원 대상과제 공고", "산업공급망정책과", "2026-09-22",
               "https://www.motir.go.kr/kor/article/ATCL2826a2625/71333/view", 1))
        _mc = ('<table><tr><td>11880</td><td><a href="/home/web/board/read.do;jsessionid=AB.mehome1?pagerOffset=0&amp;menuId=10524'
               '&amp;boardId=1894540&amp;boardMasterId=39">2026년도 3차 「전력정보화 및 정책지원사업」 신규지원 대상과제 공고</a></td>'
               '<td>재생에너지<br>정책과</td><td>고진희</td><td>2026-09-29</td><td>1,011</td></tr></table>')
        _mcr = _eac.parse_mcee_notice_list(_mc)
        check("기후부 공지·공고 목록 — 세션 주소를 뗀 깨끗한 상세 주소·부서·등록일",
              (_mcr[0]["id"], _mcr[0]["url"], _mcr[0]["dept"], _mcr[0]["date"]),
              ("1894540", "https://www.mcee.go.kr/home/web/board/read.do?menuId=10524&boardId=1894540&boardMasterId=39",
               "재생에너지 정책과", "2026-09-29"))
        check("공고문에서 접수 마감일 — 기간이면 뒤쪽, 공고일·시행일은 마감으로 오인하지 않는다",
              (_eac.deadline_from_text("접수기간 2026. 9. 22.(화) ~ 2026. 10. 21.(수) 18:00까지"),
               _eac.deadline_from_text("신청기한: ~ 2026년 10월 21일까지"),
               _eac.deadline_from_text("공고일 2026.9.1 시행일 2026.12.1")), ("2026-10-21", "2026-10-21", None))
        check("IRIS 상세 — 접수기간 칸의 끝 날짜가 마감",
              _eac.parse_iris_detail("<div><li>공고번호 제2026-613호</li><li>접수기간 2026-09-22 ~ 2026-10-21</li>"
                                     "<li>사업담당자 홍길동</li><p>■ 공고문 소재부품 신규과제를 공고합니다</p></div>")["period_end"],
              "2026-10-21")
        check("IRIS 날짜 — 하이픈·붙은 숫자·점 표기 모두 ISO 로, 비면 None",
              [_eac._iris_date(x) for x in ("2026-10-21", "20261021", "2026.10.21 18:00", "", None, "미정")],
              ["2026-10-21", "2026-10-21", "2026-10-21", None, None, None])
        _fdb = ea_mod.EaDB(os.path.join(__import__("tempfile").mkdtemp(), "fill.db"))
        _fdb.exec("create table if not exists ea_policy_items (id TEXT primary key, url_source TEXT unique,"
                  " url_canonical TEXT, item_type TEXT, title TEXT, notice_end TEXT)")
        _fdb.exec("insert into ea_policy_items (id,url_source,url_canonical,item_type,title,notice_end) values "
                  "('a','u-null','u','grant_notice','t',null),('b','u-has','u','grant_notice','t','2026-01-01')")
        check("저장된 공고의 빈 마감일만 채우고 이미 있는 값은 덮어쓰지 않는다",
              (_fdb.fill_notice_end("u-null", "2026-10-21"), _fdb.fill_notice_end("u-has", "2026-12-31"),
               _fdb.one("select notice_end from ea_policy_items where id='a'")["notice_end"],
               _fdb.one("select notice_end from ea_policy_items where id='b'")["notice_end"]),
              (1, 0, "2026-10-21", "2026-01-01"))
        # 정책 동향 '원문을 확보하지 못했습니다' (2026-10-05) — 본문을 저장하지 않는 유형은 분석 때 본문을 다시 받아야 한다
        _pb = ('<html><meta name="description" content="K-원전 팀코리아, 미국 진출 시동건다 - 한-미 원전 프레임워크 체결의 후속 조치로 '
               '미국내 대형원전 8기 건설 추진방안을 논의했다. 팀코리아 주도 노형 건설 방안과 공급망 참여를 다뤘다."></html>')
        check("보도자료 본문 — 페이지 머리 description 에서 제목 접두를 떼고 본문 첫머리를 얻는다",
              _eac.parse_press_body(_pb, "K-원전 팀코리아, 미국 진출 시동건다").startswith("한-미 원전 프레임워크"), True)
        _real_pb = _eac.fetch_press_body
        _eac.fetch_press_body = lambda url, title="": "본문 첫머리 " * 20
        check("본문 재확보 — 정책브리핑은 상세 description, 월간 통상·공모는 각자 규칙, KOTRA 입찰은 목록 정보",
              (len(ea_mod.fetch_body_for_item({"item_type": "policy_press", "url_source": "u", "title": "t"})) > 40,
               "입찰방법 제한경쟁" in ea_mod.fetch_body_for_item({"item_type": "grant_notice", "title": "대행",
                                                          "url_source": "https://www.kotra.or.kr/x", "law_name": "제한경쟁(총액)"})),
              (True, True))
        _eac.fetch_press_body = _real_pb
        _rdb = ea_mod.EaDB(os.path.join(__import__("tempfile").mkdtemp(), "re.db"))
        _rc = sqlite3.connect(_rdb.path)
        _rc.executescript(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_sqlite.sql"),
                               encoding="utf-8").read())
        _rc.commit()
        _rc.close()
        for _i, _sm in enumerate(("원문을 확보하지 못했습니다 — 원문 링크로 확인하세요", "정상 요약")):
            _rdb.insert_item({"id": f"r{_i}", "url_source": f"ru{_i}", "url_canonical": f"ru{_i}", "item_type": "policy_press",
                              "title": "t", "collected_at": "2026-10-01T00:00:00Z"})
            _rdb.save_analysis({"id": f"ra{_i}", "policy_item_id": f"r{_i}", "summary": _sm, "impact_level": "none", "model": "m"})
        check("재분석 대상 — 요약이 '원문을 확보하지 못했습니다'인 항목만",
              [x["id"] for x in _rdb.placeholder_items(10)], ["r0"])
        _rdb.delete_analysis("r0")
        check("재분석 — 분석을 지우면 미분석 목록으로 돌아가 다음 수집이 다시 분석한다",
              [x["id"] for x in _rdb.unanalyzed_items(10)], ["r0"])
        _gg = ea_mod.Gates.__new__(ea_mod.Gates)
        _gg.db = type("D", (), {"known_url_sources": lambda self, u: set(), "upsert_ledger": lambda *a: None})()
        _gg.seen, _gg.agency_names, _gg.extra_terms = set(), set(), []
        _gg.counts = {"fetched": 0, "g0": 0, "g1": 0, "g2": 0, "g2_5": 0, "off_topic": 0}
        check("공모 공고 — 소재부품·수요조사 같은 낱말이 제목에 있으면 관련으로 통과(일반 공고는 제외)",
              [x["url_source"] for x in _gg.filter([
                  {"url_source": "g1", "title": "2026년도 소재부품기술개발사업(4차) 신규지원 대상과제 공고", "_grant": True},
                  {"url_source": "g2", "title": "비영리법인 설립허가 공고 (사단법인 주한덴마크상공회의소)", "_grant": True},
                  {"url_source": "g3", "title": "2027년 사전 수요조사 공고", "_grant": True},
                  {"url_source": "g4", "title": "2026년 경제안보품목 수입처 다변화 지원사업 공고", "_grant": True}])],
              ["g1", "g3", "g4"])
        check("공모 공고 마감일이 있으면 달력·D-day 에 올라간다(항목 뷰)",
              ea_mod._item_view({"id": "g", "title": "소재부품 공고", "item_type": "grant_notice",
                                 "notice_end": (datetime.now(KST).date() + timedelta(days=5)).isoformat()})["d_day"], 5)
        # KOTRA 입찰공고 — 화면이 동적으로 그려지는 사이트(브라우저로 읽는다). 파서는 항상, 브라우저 읽기는 설치돼 있을 때만 시험한다.
        _kb = ('<table><tbody><tr onclick="fnDetail(\'7301\')"><td>2026년 경제안보품목 수입처 다변화 지원사업 운영 대행</td><td>작성일 : 2026-09-20</td>'
               '<td>마감일 : <br> <br> 2026. 10. 12. (월) 18:00</td><td>입찰방법 : 제한경쟁(총액)</td><td>기타 : 협상에 의한 계약</td></tr>'
               '<tr><td>제목 없는 번호 행</td><td>작성일 : 2026-09-01</td><td>마감일 : 2026. 9. 9.</td></tr></tbody></table>')
        _kbr = _eac.parse_kotra_bids(_kb)
        check("KOTRA 입찰공고 파서 — 제목·작성일·마감일(ISO)·입찰방법·계약방식·상세 번호",
              (_kbr[0]["title"], _kbr[0]["date"], _kbr[0]["deadline"], _kbr[0]["method"], _kbr[0]["extra"], _kbr[0]["nttseq"]),
              ("2026년 경제안보품목 수입처 다변화 지원사업 운영 대행", "2026-09-20", "2026-10-12", "제한경쟁(총액)",
               "협상에 의한 계약", "7301"))
        check("KOTRA 입찰공고 파서 — 상세 번호가 없으면 제목+작성일로 고유값을 만든다",
              _kbr[1]["nttseq"].startswith("h") and len(_kbr[1]["nttseq"]) == 11, True)
        _pw_exe = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
        try:
            import playwright  # noqa: F401
            _has_pw = os.path.exists(os.environ.get("EA_CHROMIUM_PATH") or _pw_exe)
        except ImportError:
            _has_pw = False
        if _has_pw:
            _site = os.path.join(__import__("tempfile").mkdtemp(), "kotra.html")
            open(_site, "w", encoding="utf-8").write(
                '<html><body><div id="detail_area"></div><script>const P={1:[["일반 용역 입찰","2026-09-21","2099. 11. 3.","일반경쟁","x"]],'
                '2:[["경제안보품목 수입처 다변화 대행","2026-09-20","2099. 10. 12.","제한경쟁","y"],["지난 입찰","2026-01-01","2026. 1. 5.","일반","z"]]};'
                'function draw(n){document.getElementById("detail_area").innerHTML="<table><tbody>"+P[n].map((r,i)=>"<tr onclick=\\"f("+(5000+n*10+i)+")\\">"'
                '+r.map((c,j)=>"<td>"+["","작성일 : ","마감일 : ","입찰방법 : ","기타 : "][j]+c+"</td>").join("")+"</tr>").join("")+"</tbody></table>"'
                '+"<div class=paging><a href=# onclick=\\"draw(1);return false\\">1</a><a href=# onclick=\\"draw(2);return false\\">2</a></div>";}'
                'setTimeout(()=>draw(1),500);</script></body></html>')
            _old_url, _old_exe = _eac.KOTRA_BID_URL, os.environ.get("EA_CHROMIUM_PATH")
            _eac.KOTRA_BID_URL = "file://" + _site
            os.environ["EA_CHROMIUM_PATH"] = _old_exe or _pw_exe
            try:
                _kc = _eac.crawl_kotra_bids(pages=2)
            finally:
                _eac.KOTRA_BID_URL = _old_url
                if _old_exe is None:
                    os.environ.pop("EA_CHROMIUM_PATH", None)
                else:
                    os.environ["EA_CHROMIUM_PATH"] = _old_exe
            check("KOTRA 입찰공고 — 브라우저로 늦게 그려지는 목록을 읽고 2쪽까지 넘기며, 마감 지난 건은 뺀다",
                  [x["title"] for x in _kc], ["일반 용역 입찰", "경제안보품목 수입처 다변화 대행"])
        else:
            print("  - (브라우저 읽기 시험은 playwright·Chromium 이 있을 때만 — 이 환경에서는 건너뜀)")
        _v = ea_mod._item_view({"id": "i1", "title": "이차전지 지원", "item_type": "policy_press",
                                "proposers": '[{"name":"김철수","party":"무소속","role":"대표"}]',
                                "summary": "시행한다", "group_companies": '["포스코퓨처엠"]'})
        check("항목 뷰 — 우선순위·발의자 포함", (_v["priority"], _v["proposers"][0]["name"]), ("긴급", "김철수"))

    if failures:
        print(f"실패 {len(failures)}건:\n" + "\n".join(failures))
        return 1
    print("전체 통과.")
    return 0


USAGE = """사용법: python backend/main.py <명령>

  initdb     스키마 생성 + 시드 데이터 입력 (최초 1회)
  reexcerpt [--dry]  보관 본문(30일)으로 포스코퓨처엠 언급 발췌를 새 방식(문단 단위·맥락 포함)으로 다시 만들기 (LLM 0)
  retranslate [N] [--dry]  저장된 영어 기사(제목·발췌)를 한국어로 번역 (AI 호출 최대 N회, 기본 50)
  fixpfm [--dry]  포스코퓨처엠 태그가 붙었지만 제목·본문에 언급이 없는 기사에서 태그 제거 (먼저 pfmtone 으로 발췌 채우기)
  reopen [일수] [--dry]  규칙을 넓힌 뒤 최근 며칠(기본 3일)의 '무관'·'접속 실패' 제외 기록을 지워 다시 판정(실행 후 worker 재시작)
  tagassoc [--dry]  이미 저장된 기사에 빠진 그룹사 태그 보충('배터리협회'·'포스코퓨처엠', 띄어쓴 표기 포함, 일회성)
  addkw <분류> <키워드>...  수집 키워드 추가 (예: addkw 그룹사 배터리협회 KBIA — 이미 있으면 건너뜀.
                    분류는 그룹사·산업·정책·통상. 마스터 패널 '수집 키워드 관리'와 같은 동작)
  unrated [N]  언론사 탭 '미판정' 기사를 원인별로 목록 보기 (점검용, 쓰지 않음)
  sentireason [N] [--dry]  감성(긍정·중립·부정) 근거가 빈 옛 기사에 AI 근거를 채움(기본 100건)
  pfmtone [N] [--dry]  기존 포스코퓨처엠 기사에 발췌문·논조(LLM) 채우기 (백필, 기본 LLM 200회,
                      --dry 는 대상 수만 보여주고 쓰지 않음)
  fixpress [--dry]  언론사명 정리 (도메인으로 저장된 매체명을 정식 이름으로 교체,
                    --dry 는 고칠 대상·매핑 없는 도메인만 보여주고 쓰지 않음)
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
  worker     웹서버 없이 수집 루프 + 텔레그램 챗봇만 실행 (serve 와 별도 프로세스로
             띄우면 한쪽이 죽거나 재시작해도 다른 쪽은 안 끊긴다)
  healthcheck [N]  worker 컨테이너 헬스체크 — 마지막 수집이 N초(기본 poll 주기의 3배)
             이내면 healthy. DB 연결 없이 즉시 끝난다(Docker/ECS 헬스체크 CMD 용)
  run        서버 + 수집 루프 + 텔레그램 챗봇 (한 프로세스, 기존 운영 모드)
  quotes     시세만 1회 갱신
  notify     대기 중인 텔레그램 알림만 발송
  retryfailed  발송 실패로 막힌 알림을 다시 큐로 되돌림 (rate limit 등 일시 장애 복구용)
  chatid     텔레그램 chat_id 확인 (봇에게 메시지를 한 번 보낸 뒤 실행)
  sendtest   텔레그램 시험 메시지 1건 발송 (연결 확인용)
  kakao-auth   카카오 '나에게 보내기' 최초 토큰 발급 (브라우저 동의 → code 붙여넣기, 1회)
  kakao-test   카카오 '나에게 보내기' 시험 발송 1건
  ea-collect      대외협력(입법·행정예고·국회) 즉시 1회 수집·분석
  ea-reanalyze [N] [--dry]  '원문을 확보하지 못했습니다'로 저장된 대외협력 요약을 본문을 다시 받아 재분석(기본 40건, AI 일일 상한 안에서)
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
    if command == "healthcheck":
        # DB 연결 없이 가볍게 — 컨테이너 플랫폼이 자주(예: 60초마다) 호출한다.
        default_max_age = load_config().poll_interval_sec * 3
        max_age = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else default_max_age
        return cmd_healthcheck(max_age)

    cfg = load_config()
    storage = make_storage(cfg)
    ctx = Context(cfg=cfg, storage=storage, http=HttpClient())
    warn_if_bad_chat_id(cfg.telegram_chat_id)

    if command == "initdb":
        cmd_initdb(ctx)
    elif command == "fixpress":
        cmd_fixpress(ctx, dry="--dry" in argv[2:])
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
    elif command == "reexcerpt":
        cmd_reexcerpt(ctx, dry="--dry" in argv[2:])
    elif command == "retranslate":
        nums = [int(a) for a in argv[2:] if a.isdigit()]
        cmd_retranslate(ctx, nums[0] if nums else 50, dry="--dry" in argv[2:])
    elif command == "fixpfm":
        cmd_fixpfm(ctx, dry="--dry" in argv[2:])
    elif command == "reopen":
        nums = [int(a) for a in argv[2:] if a.isdigit()]
        cmd_reopen(ctx, nums[0] if nums else 3, dry="--dry" in argv[2:])
    elif command == "tagassoc":
        cmd_tagassoc(ctx, dry="--dry" in argv[2:])
    elif command == "addkw":
        cmd_addkw(ctx, argv[2] if len(argv) > 2 else "", argv[3:])
    elif command == "sentireason":
        rest = argv[2:]
        nums = [int(a) for a in rest if a.isdigit()]
        cmd_sentireason(ctx, nums[0] if nums else 100, dry="--dry" in rest)
    elif command == "unrated":
        nums = [int(a) for a in argv[2:] if a.isdigit()]
        cmd_unrated(ctx, nums[0] if nums else 40)
    elif command == "pfmtone":
        rest = argv[2:]
        nums = [int(a) for a in rest if a.isdigit()]
        cmd_pfmtone(ctx, nums[0] if nums else 200, dry="--dry" in rest)
    elif command == "fixexcerpt":
        rest = argv[2:]
        nums = [int(a) for a in rest if a.isdigit()]
        names = [a for a in rest if not a.isdigit() and not a.startswith("--")]
        cmd_fixexcerpt(ctx, names[0] if names else "", nums[0] if nums else 60, dry="--dry" in rest)
    elif command == "daum-test":
        nums = [int(a) for a in argv[2:] if a.isdigit()]
        cmd_daum_test(ctx, nums[0] if nums else 12)
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
        cmd_chatid(ctx, public="public" in argv[2:])
    elif command == "public-test":
        cmd_public_test(ctx)
    elif command == "public-check":
        cmd_public_check(ctx)
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
        ea_mod.collect_once(ctx, ea_mod.make_ea_db(ctx))
    elif command == "ea-reanalyze":
        if ea_mod is None:
            raise SystemExit("external_affairs 모듈을 불러오지 못했습니다.")
        _rest = argv[2:]
        _nums = [int(a) for a in _rest if a.isdigit()]
        _edb = ea_mod.make_ea_db(ctx)
        _edb.ensure_ready()
        ea_mod.reanalyze_placeholders(ctx, _edb, _nums[0] if _nums else 40, dry="--dry" in _rest)
    elif command == "migrate":
        cmd_migrate(ctx)
    elif command == "serve":
        cmd_serve(ctx, with_pipeline=False)
    elif command == "run":
        cmd_serve(ctx, with_pipeline=True)
    elif command == "worker":
        cmd_worker(ctx)
    else:
        print(f"알 수 없는 명령: {command}\n")
        print(USAGE)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
