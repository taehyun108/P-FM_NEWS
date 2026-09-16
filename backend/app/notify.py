"""③ 텔레그램·카카오 알림 발송과 봇 루프"""
from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Sequence
import json
import re
import threading
import time
from datetime import timedelta
from urllib.parse import urlencode

from .collect import (
    CATEGORY_RULES,
    _kw_first_hit,
    _kw_hit_any,
    detect_group_companies,
    normalize_group_list,
)
from .core import (
    rate_ok,
    Config,
    Context,
    HttpClient,
    esc,
    esc_attr,
    format_summary_header,
    iso,
    jload,
    log,
    normalize_chip,
    now_local,
    now_utc,
    parse_dt,
)
from .view import (
    build_card,
)

if TYPE_CHECKING:
    from .analyze import (
        analyze_url,
    )




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
    return rate_ok(_bot_chat_calls.setdefault(chat_id, []), BOT_CHAT_MAX_PER_HOUR)




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
    if card.get("perspective_text"):
        lines += ["", f"포스코 관점: {esc(card['perspective_text'])}"]
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
        from .analyze import analyze_url   # 순환 import 회피 — analyze 가 notify 를 쓴다
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
