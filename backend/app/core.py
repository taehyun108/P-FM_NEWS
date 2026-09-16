"""설정·로깅·공용 유틸·HTTP 클라이언트·Context"""
from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Iterable
from typing import Sequence
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import datetime
from dataclasses import field
import hashlib
import html as html_mod
import importlib
import json
import logging
import os
from urllib.parse import parse_qsl
import re
import sys
import threading
from datetime import timedelta
from datetime import timezone
import unicodedata
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
import uuid


if TYPE_CHECKING:
    from .analyze import (
        LLMClient,
    )
    from .storage import (
        Storage,
    )

try:   # 대외협력(대관) 모듈 — 없거나 깨져도 기존 수집·API 는 그대로 동작한다
    import external_affairs as ea_mod
except Exception:   # pragma: no cover
    ea_mod = None



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



# app/core.py 기준 — backend/app -> backend -> 레포 루트 (3단계)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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




def format_summary_header(press: str, author: str) -> str:
    """'[언론사, 기자명]' 머리표를 표시 시점에 조립한다. 저장하지 않는다. (PRD F4.1)"""
    press = (press or "").strip()
    author = (author or "").strip()
    if press and author:
        return f"[{press}, {author}]"
    if press:
        return f"[{press}]"
    return ""




@dataclass
class Context:
    cfg: Config
    storage: Storage
    http: HttpClient
    seen_cache: set[str] = field(default_factory=set)
    last_naver_fetch: float = 0.0
    naver_cycle: int = 0        # 키워드 교대 조회 회차 (select_naver_keywords)
    _llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        """LLM 클라이언트는 실제로 필요한 시점에 만든다.

        initdb·serve 처럼 LLM 을 쓰지 않는 명령이 openai 패키지를 요구하면 안 된다.
        """
        if self._llm is None:
            from .analyze import LLMClient   # 순환 import 회피 — analyze 가 core 를 쓴다
            self._llm = LLMClient(self.cfg)
        return self._llm




def _looks_like_domain(text: str) -> bool:
    return bool(re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", (text or "").strip().lower()))




def _has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))




def esc(text: str) -> str:
    """parse_mode=HTML 이므로 <, >, & 를 반드시 이스케이프한다. (PRD F7.2)"""
    return html_mod.escape(text or "", quote=False)




def esc_attr(text: str) -> str:
    """href="..." 안에 들어가는 값. 큰따옴표까지 이스케이프해야 태그가 안 깨진다.

    esc() 는 quote=False 라 " 를 그대로 둔다. 주소에 " 가 섞이면 <a href> 가 끊겨
    'can't parse entities' 로 영구 실패한다.
    """
    return html_mod.escape(text or "", quote=True)
