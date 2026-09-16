"""카드·필터·주간 리포트 등 표시용 가공"""
from __future__ import annotations

from typing import Any
from typing import Sequence
from datetime import datetime
import html as html_mod
import re
import threading
import time
from datetime import timedelta

from .collect import (
    GROUP_COMPANIES,
    detect_group_companies,
    normalize_group_list,
)
from .core import (
    Config,
    Context,
    dedupe_chips,
    esc,
    esc_attr,
    format_summary_header,
    iso,
    jload,
    log,
    new_id,
    normalize_chip,
    now_local,
    now_utc,
    parse_dt,
)




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
                f'<a href="{esc_attr(a["url"])}" style="color:#16337A;text-decoration:none;font-weight:600;">'
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
        return sorted(st["by_id"].values(),
                      key=lambda t: t["row"].get("published_at") or "", reverse=True)




def scan_store_upsert(storage: "Storage", article_id: str) -> None:
    """수동 등록처럼 즉시 반영이 필요한 한 건만 스토어에 넣거나 뺀다."""
    row = storage.article_detail(article_id)
    with _SCAN_STORE_LOCK:
        if row:
            _store_put(_SCAN_STORE, row)
        else:
            _SCAN_STORE["by_id"].pop(article_id, None)
