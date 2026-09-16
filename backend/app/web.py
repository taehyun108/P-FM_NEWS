"""FastAPI 라우트"""
from __future__ import annotations

from typing import Any
from collections import Counter
from dataclasses import field
import hashlib
import hmac
import os
import re
import threading
import time
from datetime import timedelta

from .analyze import (
    FX_SYMBOLS,
    QUOTE_STALE_MINUTES,
    STOCK_SYMBOLS,
    analyze_url,
)
from .auth import (
    ANALYZE_MAX_PER_HOUR,
    MASTER_LOGIN_FAIL_THRESHOLD,
    WEB_COOKIE,
    WEB_PUBLIC_PATHS,
    WEB_PW_TTL_SEC,
    WEB_SESSION_DAYS,
    _WEB_PW_CACHE,
    _analyze_calls,
    _client_ip,
    _issue_master_token,
    _login_fail,
    _login_locked,
    _login_reset,
    _master_login_fail,
    _master_login_reset,
    _rate_ok,
    _recover_master_password,
    _valid_master_token,
    check_master_password,
    hash_password,
    verify_password,
    web_session_token,
)
from .collect import (
    KEYWORD_CATEGORIES,
    NAVER_ALWAYS_CATEGORY,
    PEOPLE_NEWS_CATEGORY,
    SCORE_CUSTOM_MAX,
    SCORE_RULE_DEFS,
    _score_rules_cache,
)
from .core import (
    ea_mod,
    Context,
    FRONTEND_DIR,
    _import,
    clamp,
    dedupe_chips,
    get_env,
    iso,
    jdump,
    jload,
    log,
    new_id,
    normalize_chip,
    now_utc,
    parse_dt,
)
from .notify import (
    TELEGRAM_API,
    _rate_gate,
    _telegram_send,
    clamp_message,
    effective_night,
    effective_threshold,
    format_message,
    kakao_exchange_code,
    queue_manual_notify,
)
from .view import (
    MASTER_TOKEN_TTL,
    PERIOD_HOURS,
    RECOMMENDED_MIN_SCORE,
    _SCAN_STORE,
    _split_multi,
    build_card,
    filter_tagged,
    refresh_scan_store,
    run_weekly_report,
    scan_store_upsert,
    send_report_email,
    weekly_recipients,
)




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
