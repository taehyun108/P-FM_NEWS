"""대외협력 — API 키 없이 크롤링으로 예고 정보를 가져온다.

세 사이트 모두 robots.txt = 'Allow: /' 이고 서버렌더(HTML 에 표가 그대로 들어옴)이며,
게시 자료는 공공누리(KOGL) 공공저작물이다. 그래도 예의상 다음을 지킨다.
  · 요청 간 1초 간격, 페이지 수 상한(기본 3), 데스크톱 UA
  · 목록 → 상세 순으로만 접근. 검색·다운로드 폼은 건드리지 않는다.

수집 대상
  ogLmPp   입법예고    https://opinion.lawmaking.go.kr/gcom/ogLmPp?pageIndex=N
  admpp    행정예고    https://opinion.lawmaking.go.kr/gcom/admpp?pageIndex=N
  napal    국회 입법예고 https://pal.assembly.go.kr/napal/lgsltpa/lgsltpaOngoing/list.do

REST(OC 키) 가 설정돼 있으면 external_affairs 가 그쪽을 먼저 쓰고, 이 모듈은 폴백이다.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timezone

log = logging.getLogger("pfm.ea.crawl")

KST = timezone.__class__(__import__("datetime").timedelta(hours=9)) if False else None  # noqa
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) P-FM-NEWS/EA"
_REQ_GAP = 1.0            # 요청 간 최소 간격(초)
_last_req = [0.0]

LAWMAKING = "https://opinion.lawmaking.go.kr"
PAL = "https://pal.assembly.go.kr"


def _get(url: str, params: dict | None = None) -> str:
    import requests
    gap = _REQ_GAP - (time.monotonic() - _last_req[0])
    if gap > 0:
        time.sleep(gap)
    resp = requests.get(url, params=params or {}, timeout=20,
                        headers={"User-Agent": _UA, "Accept-Language": "ko"})
    _last_req[0] = time.monotonic()
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
    return resp.text


def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


# ── 날짜 파싱 ────────────────────────────────────────────────────────
_PERIOD_RE = re.compile(
    r"(\d{4})[.\-/\s]+(\d{1,2})[.\-/\s]+(\d{1,2})\.?\s*~\s*"
    r"(\d{4})[.\-/\s]+(\d{1,2})[.\-/\s]+(\d{1,2})")
_ONE_DATE_RE = re.compile(r"(\d{4})[.\-/\s]+(\d{1,2})[.\-/\s]+(\d{1,2})")


def _iso(y: str, m: str, d: str) -> str | None:
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def parse_period(text: str) -> tuple[str | None, str | None]:
    """'2026. 9. 4. ~ 2026. 9. 11.' → ('2026-09-04', '2026-09-11')"""
    t = (text or "").strip()
    m = _PERIOD_RE.search(t)
    if m:
        return _iso(*m.group(1, 2, 3)), _iso(*m.group(4, 5, 6))
    dates = _ONE_DATE_RE.findall(t)
    if len(dates) >= 2:
        return _iso(*dates[0]), _iso(*dates[1])
    if len(dates) == 1:
        return _iso(*dates[0]), None
    return None, None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


_STATUS_PREFIX = re.compile(r"^(진행|마감|예정|종료|접수중|D-\d+)\s+")


def _strip_status(title: str) -> str:
    t = _clean(title)
    for _ in range(2):
        t = _STATUS_PREFIX.sub("", t)
    return t


def _status_from_end(end_iso: str | None) -> str:
    if not end_iso:
        return "예고중"
    try:
        left = (date.fromisoformat(end_iso) - datetime.now().date()).days
        return "예고중" if left >= 0 else "종료"
    except ValueError:
        return "예고중"


# ── 목록 표 파싱 공통 ────────────────────────────────────────────────
def _rows_from_table(soup, link_pat: re.Pattern) -> list[tuple[str, str, list[str]]]:
    """(상세href, 제목, 셀텍스트리스트) 목록. 첫 tbody 의 tr 을 훑는다."""
    out: list[tuple[str, str, list[str]]] = []
    for tbody in soup.find_all("tbody"):
        trs = tbody.find_all("tr")
        if len(trs) < 2:
            continue
        for tr in trs:
            a = None
            for cand in tr.find_all("a", href=True):
                if link_pat.search(cand["href"]):
                    a = cand
                    break
            if a is None:
                continue
            cells = [_clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            title = _strip_status(a.get_text(" ")) or _strip_status(cells[1] if len(cells) > 1 else "")
            out.append((a["href"], title, cells))
        if out:
            break
    return out


# ── S1 입법예고 (정부) ──────────────────────────────────────────────
_OGLMPP_LINK = re.compile(r"/gcom/ogLmPp/(\d+)")


def crawl_legislation_notices(max_pages: int = 2) -> list[dict]:
    items: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            html = _get(f"{LAWMAKING}/gcom/ogLmPp", {"pageIndex": page})
        except Exception as exc:
            log.warning("입법예고 목록 %d페이지 실패: %s", page, exc)
            break
        rows = _rows_from_table(_soup(html), _OGLMPP_LINK)
        if not rows:
            break
        for href, title, cells in rows:
            seq_m = _OGLMPP_LINK.search(href)
            if not seq_m:
                continue
            seq = seq_m.group(1)
            # 컬럼: [번호, 제목, 소관부처(법령종류), 예고구분, 예고기간, 남은일수, 의견수, 조회수]
            agency = _clean(re.sub(r"\s*\([^)]*\)\s*$", "", cells[2])) if len(cells) > 2 else ""
            period = cells[4] if len(cells) > 4 else " ".join(cells)
            start, end = parse_period(period)
            url = f"{LAWMAKING}/gcom/ogLmPp/{seq}"
            items.append({
                "url_source": url, "url_canonical": url, "item_type": "legislation",
                "title": title, "law_name": _law_name(title), "agency": agency,
                "notice_start": start, "notice_end": end,
                "status": _status_from_end(end), "opinion_url": url,
                "attachment_urls": [], "published_at": start,
            })
        if len(rows) < 10:
            break
    log.info("입법예고 크롤링 %d건", len(items))
    return items


# ── S2 행정예고 ────────────────────────────────────────────────────
_ADMPP_LINK = re.compile(r"/gcom/admpp/(\d+)")


def crawl_admin_notices(max_pages: int = 2) -> list[dict]:
    items: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            html = _get(f"{LAWMAKING}/gcom/admpp", {"pageIndex": page})
        except Exception as exc:
            log.warning("행정예고 목록 %d페이지 실패: %s", page, exc)
            break
        rows = _rows_from_table(_soup(html), _ADMPP_LINK)
        if not rows:
            break
        for href, title, cells in rows:
            seq_m = _ADMPP_LINK.search(href)
            if not seq_m:
                continue
            # 컬럼: [번호, 제목, 예고구분, 소관부처(고시번호), 예고기간, 남은일수]
            agency = _clean(re.sub(r"\s*\([^)]*\)\s*$", "", cells[3])) if len(cells) > 3 else ""
            period = cells[4] if len(cells) > 4 else " ".join(cells)
            start, end = parse_period(period)
            path = href if href.startswith("http") else LAWMAKING + href
            url = path.split("?")[0]
            items.append({
                "url_source": url, "url_canonical": url, "item_type": "admin_notice",
                "title": title, "law_name": _law_name(title), "agency": agency,
                "notice_start": start, "notice_end": end,
                "status": _status_from_end(end), "opinion_url": path,
                "attachment_urls": [], "published_at": start,
            })
        if len(rows) < 10:
            break
    log.info("행정예고 크롤링 %d건", len(items))
    return items


# ── S3 국회 입법예고 ───────────────────────────────────────────────
_NAPAL_LINK = re.compile(r"lgsltPaId=([A-Za-z0-9_]+)")


def crawl_assembly_notices(max_pages: int = 2) -> list[dict]:
    items: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            html = _get(f"{PAL}/napal/lgsltpa/lgsltpaOngoing/list.do",
                        {"pageIndex": page} if page > 1 else None)
        except Exception as exc:
            log.warning("국회 입법예고 목록 %d페이지 실패: %s", page, exc)
            break
        soup = _soup(html)
        rows = _rows_from_table(soup, _NAPAL_LINK)
        if not rows:
            # 링크가 onclick 에만 있는 경우 tr 원문에서 직접 추출
            rows = []
            for tbody in soup.find_all("tbody"):
                for tr in tbody.find_all("tr"):
                    m = _NAPAL_LINK.search(str(tr))
                    if not m:
                        continue
                    cells = [_clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
                    title = _strip_status(cells[1] if len(cells) > 1 else "")
                    rows.append((f"?lgsltPaId={m.group(1)}", title, cells))
        if not rows:
            break
        for href, title, cells in rows:
            m = _NAPAL_LINK.search(href)
            if not m:
                continue
            pa_id = m.group(1)
            url = f"{PAL}/napal/lgsltpa/lgsltpaOngoing/view.do?lgsltPaId={pa_id}"
            committee = cells[3] if len(cells) > 3 else ""
            # 목록엔 예고기간이 없다 — 상세에서 보강한다. 국회 입법예고는 통상 10일.
            start = end = None
            for c in cells:
                s, e = parse_period(c)
                if s:
                    start, end = s, e
                    break
            items.append({
                "url_source": url, "url_canonical": url, "item_type": "bill",
                "title": re.sub(r"\s*\d{7}.*$", "", title).strip() or title,
                "law_name": _law_name(title), "agency": committee or "국회",
                "notice_start": start, "notice_end": end,
                "status": _status_from_end(end) if end else "국회심의",
                "opinion_url": url, "attachment_urls": [], "published_at": start,
                "_need_detail_period": end is None,
            })
        if len(rows) < 8:
            break
    log.info("국회 입법예고 크롤링 %d건", len(items))
    return items


_PERIOD_LABEL_RE = re.compile(r"예고\s*기간[^0-9]{0,15}(.{0,40})")


def enrich_assembly_period(item: dict) -> dict:
    """국회 입법예고 상세에서 예고기간을 읽어 notice_end 를 채운다. (목록엔 없다)"""
    if not item.get("_need_detail_period"):
        return item
    try:
        html = _get(item["url_source"])
    except Exception as exc:
        log.debug("국회 입법예고 상세 실패 %s: %s", item.get("title", "")[:20], exc)
        return item
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ",
                  html, flags=re.S)))
    m = _PERIOD_LABEL_RE.search(text)
    seg = m.group(1) if m else text
    start, end = parse_period(seg)
    if not end:   # 라벨 근처에서 못 찾으면 전체에서 첫 기간
        start, end = parse_period(text)
    if end:
        item["notice_start"] = start or item.get("notice_start")
        item["notice_end"] = end
        item["status"] = _status_from_end(end)
        item["published_at"] = item.get("published_at") or start
    item.pop("_need_detail_period", None)
    return item


def _law_name(title: str) -> str:
    t = re.sub(r"\s*\d{7}\b.*$", "", title or "")
    t = re.sub(r"\s*(일부개정|전부개정|제정)?(법률안|령안|규칙안|안)?\s*"
               r"(입법예고|행정예고)?\s*(일부)?\s*$", "", t).strip()
    return t or (title or "").strip()
