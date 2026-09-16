"""운영 커맨드와 백그라운드 루프"""
from __future__ import annotations

from typing import Any
from typing import Sequence
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

from .analyze import (
    _homepage_site_name,
    analyze_and_save,
    clean_site_name,
    people_summary,
    refresh_quotes,
    run_once,
    site_name_from_html,
)
from .collect import (
    MEDIA_NAME_SUFFIX,
    NON_AUTHOR_WORDS,
    PEOPLE_NEWS_CATEGORY,
    POLICY_CATEGORY,
    POSCO_MENTION_RE,
    REGROUP_DAYS,
    TRADE_CATEGORY,
    _is_bare_root,
    _notice_text,
    clean_author,
    decode_unicode_escapes,
    detect_categories,
    detect_group_companies,
    extract_author,
    extract_body,
    fix_mojibake,
    group_lead_text,
    is_battery_scope,
    is_policy_brief,
    is_trade_article,
    normalize_group_list,
    people_news_kind,
    prefetch_articles,
    resolve_canonical,
)
from .core import (
    ea_mod,
    Context,
    HttpClient,
    _has_hangul,
    _import,
    _looks_like_domain,
    dedupe_chips,
    domain_of,
    iso,
    jdump,
    jload,
    load_config,
    log,
    new_id,
    normalize_chip,
    normalize_url,
    now_utc,
    parse_dt,
)
from .notify import (
    SEND_BATCH_PER_CYCLE,
    cmd_chatid,
    cmd_kakao_auth,
    cmd_kakao_test,
    cmd_sendtest,
    send_notifications,
    telegram_bot_loop,
    warn_if_bad_chat_id,
)
from .storage import (
    SEED_FEEDS,
    SEED_KEYWORDS,
    SEED_PRESS,
    make_storage,
)
from .view import (
    maybe_run_weekly,
    refresh_scan_store,
    run_weekly_report,
)
from .web import (
    create_app,
)









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
    # og:site_name/<title> 에서 한글 매체명을 시도한다. 기사 페이지는 SEO 상
    # 기사 제목만 <title>에 넣는 경우가 많아 실패하기 쉬우니, 안 되면 홈페이지도
    # 한 번 더 본다(_homepage_site_name). 그래도 실패하면 도메인 전체로 둔다.
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
        if not og:
            og = _homepage_site_name(ctx.http, domain, urlsplit(target).hostname or "")
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




def cmd_reperspective(ctx: Context, limit: int = 500) -> None:
    """계열사 태그·배터리 카테고리는 있는데 '포스코 관점'이 빈 기사를 재분석한다
    (일회성).

    2026-09-10 이전 프롬프트는 perspective 를 '포스코퓨처엠 사업 관점'으로만
    좁혀 물어봤다. 그래서 포스코·포스코이앤씨·홀딩스 등 다른 계열사만 다루고
    이차전지와 무관한 기사(노사 이슈·아파트 분양 등)는 관점이 통째로 비었다.
    프롬프트를 '포스코 그룹 전체 관점'으로 넓힌 뒤, 이어서 '계열사명이 없어도
    배터리·이차전지 기사는 포스코퓨처엠 관점을 쓴다'로 다시 넓혔다(실사례:
    현대차·LG엔솔 UBESS 실증 기사 — 포스코 계열사 미언급이라 관점이 비었었다).
    이미 group_companies 가 붙어 있거나 배터리 계열 카테고리인데 관점이 빈
    기존 카드를 다시 돌려 채운다.
    """
    _battery_cats = {"배터리·이차전지", "양극재", "음극재"}
    rows = ctx.storage.list_articles(8000, 0, None, "")
    targets = [r for r in rows
               if r.get("analyzed_at") and r.get("summary_source") == "fulltext"
               and (jload(r.get("group_companies"), []) or _battery_cats & set(jload(r.get("categories"), [])))
               and not (r.get("perspective_text") or "").strip()][:limit]
    log.info("포스코 관점 재분석 대상 %d건", len(targets))
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
            log.debug("포스코 관점 재분석 실패 %s: %s", target, exc)
            failed += 1
    log.info("포스코 관점 재분석: 성공 %d건 · 실패 %d건", done, failed)




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




# worker(웹서버 없이 수집만 도는 모드)는 /healthz 가 없다. AWS ECS 등 컨테이너
# 플랫폼이 "살아있는지"를 물을 방법이 필요해서, 수집 루프가 사이클마다 로컬
# 파일에 시각을 남기고 `healthcheck` 명령이 그 최신성으로 판정한다.
_HEARTBEAT_PATH = os.path.join(tempfile.gettempdir(), "pfm_news_heartbeat")




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
  reperspective [N]  계열사 태그는 있는데 포스코 관점이 빈 기사를 재분석 (일회성, 기본 500건)
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
        from tests.test_selftest import cmd_selftest   # 검증 코드는 tests/ 에 있다
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
    elif command == "reperspective":
        _lim = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 500
        cmd_reperspective(ctx, _lim)
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
        ea_mod.collect_once(ctx, ea_mod.make_ea_db(ctx))
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
