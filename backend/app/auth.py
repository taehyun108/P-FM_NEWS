"""웹 로그인·마스터 토큰"""
from __future__ import annotations

from typing import Any
import hashlib
import hmac
import secrets
import time

from .core import (
    recent_hits,
    Context,
    esc,
    log,
    now_utc,
)
from .view import (
    MASTER_TOKEN_TTL,
    _MASTER_TOKENS,
    send_report_email,
)




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
    q = recent_hits(_LOGIN_FAILS.setdefault(key, []), LOGIN_WINDOW_SEC)
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
    q = recent_hits(_MASTER_LOGIN_FAILS.setdefault(key, []), LOGIN_WINDOW_SEC)
    q.append(time.time())
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
