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
# 목록에서 잘려 들어온 꼬리('… 행', '… 입법예', '… 행정예고')를 정리
_STATUS_TAIL = re.compile(r"\s*(입법예고|행정예고|입법예|행정예|입법|행정|입|행)\s*$")


def _strip_status(title: str) -> str:
    t = _clean(title)
    for _ in range(2):
        t = _STATUS_PREFIX.sub("", t)
    t = _STATUS_TAIL.sub("", t).strip()
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


# ── 상세 본문 (개정이유·주요내용) ──────────────────────────────────
_TITLE_QUOTED = re.compile(r"[「『]([^」』]{4,80})[」』]")
_BILL_TITLE = re.compile(r"\[(\d{6,8})\]\s*([^(\n]{4,80})")
# 본문 앞 상용 문구를 잘라낼 지점 — '1. 개정이유' 또는 '제안이유 및 주요내용' 헤더
_REASON_HEAD = re.compile(
    r"(?:\d+\s*\.\s*)?(?:개정이유|제정이유|제안이유|개정 이유|제안 이유)"
    r"(?:\s*및\s*주요\s*내용)?")
_NOISE = re.compile(
    r"(메인페이지|화면크기|로그아웃|로그인|"
    r"바로가기|누리집|의견목록|의견등록|인쇄하기|"
    r"카카오|페이스북|네이버 블로그|URL 복사|창닫기|HOME|"
    r"첨부파일이 없습니다|목록 보기|본문 보기)")


def fetch_detail(item: dict) -> dict:
    """G3·G4 — 예고 상세에서 개정이유·주요내용 텍스트와 정식 제명을 뽑는다.

    반환 {"body": str, "title": str|None}. 세 사이트 모두 상세가 서버렌더다
    (입법·행정예고 .detailContent / 국회 .desc).
    """
    url = item.get("opinion_url") or item.get("url_canonical") or item.get("url_source") or ""
    if not url:
        return {"body": "", "title": None}
    try:
        html = _get(url)
    except Exception as exc:
        log.debug("예고 상세 실패 %s: %s", url, exc)
        return {"body": "", "title": None}
    soup = _soup(html)
    kind = item.get("item_type")
    title = None

    if kind == "bill":
        # 국회 의안 — pal.assembly.go.kr : .desc 에 제안이유·주요내용
        cand = [e for e in soup.find_all(class_=re.compile(r"desc|card-wrap|item"))
                if "제안이유" in e.get_text()]
        node = min(cand, key=lambda e: len(e.get_text()), default=None)
        body = _clean(node.get_text(" ")) if node else ""
        full = soup.get_text(" ", strip=True)
        tm = _BILL_TITLE.search(full)
        if tm:
            title = _clean(tm.group(2))
    else:
        info = soup.find(class_="detailInfo")
        cont = soup.find(class_="detailContent") or soup.find(id="content") or soup.find("main")
        parts = []
        if info:
            parts.append(_clean(info.get_text(" ")))
        if cont:
            parts.append(_clean(cont.get_text(" ")))
        body = " ".join(parts)
        if cont:
            qm = _TITLE_QUOTED.search(cont.get_text(" "))
            if qm:
                title = _clean(qm.group(1))

    body = _clean(re.sub(_NOISE, " ", body))
    # 공고 상용문구(⊙OO부공고 제N호 …「법령명」을 개정함에 있어 …)를 개정이유 직전까지 잘라낸다
    hm = _REASON_HEAD.search(body)
    if hm and hm.start() > 30:
        body = "[개정이유·주요내용] " + body[hm.end():].lstrip(" .:")
    return {"body": body[:8000], "title": title}


# ── S4 부처 정책뉴스 (표준 정부 홈페이지 CMS — POST 방식 RSS, 본문 포함) ──
# 부처마다 홈페이지 CMS 가 달라 일괄 적용이 안 된다. 본문까지 서버가 주는 곳만
# 넣는다. 새 부처는 그 부처 '정책뉴스' 게시판의 ATCL id 를 확인해 아래에 추가한다.
#   확인법: https://<부처>/kor/article/ATCL.../ 목록 페이지에서 게시판 링크의 ATCL id
MINISTRY_NEWS_FEEDS: list[tuple[str, str]] = [
    ("산업통상부", "https://www.motir.go.kr/kor/article/ATCLb41cda0c5/rss"),
]

import html as _html_mod


def _rss_items(xml: str) -> list[dict]:
    """<item> 목록을 {title, link, pubDate, description(평문)} 로 뽑는다."""
    out: list[dict] = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def _f(tag: str) -> str:
            m = re.search(rf"<{tag}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>", block, re.S)
            return (m.group(1).strip() if m else "")
        desc = _html_mod.unescape(_f("description"))
        desc = _clean(re.sub(r"<[^>]+>", " ", desc))
        out.append({"title": _clean(_html_mod.unescape(_f("title"))),
                    "link": _f("link"), "pubDate": _f("pubDate"), "description": desc})
    return out


def crawl_ministry_news(max_per_feed: int = 20) -> list[dict]:
    """부처 정책뉴스 — POST 방식 RSS 로 제목·링크·발행일·본문을 한 번에 받는다."""
    import requests
    items: list[dict] = []
    for agency, feed_url in MINISTRY_NEWS_FEEDS:
        gap = _REQ_GAP - (time.monotonic() - _last_req[0])
        if gap > 0:
            time.sleep(gap)
        try:
            resp = requests.post(feed_url, data=b"", timeout=20,
                                 headers={"User-Agent": _UA, "Accept-Language": "ko",
                                          "Content-Type": "application/x-www-form-urlencoded"})
            _last_req[0] = time.monotonic()
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
            rows = _rss_items(resp.text)
        except Exception as exc:
            log.warning("%s 정책뉴스 RSS 실패: %s", agency, exc)
            continue
        for r in rows[:max_per_feed]:
            if not r["link"] or not r["title"]:
                continue
            start, _ = parse_period(r["pubDate"])
            items.append({
                "url_source": r["link"], "url_canonical": r["link"],
                "item_type": "ministry_news", "title": r["title"],
                "law_name": "", "agency": agency,
                "notice_start": start, "notice_end": None,
                "status": "발표", "opinion_url": None,
                "attachment_urls": [], "published_at": start,
                "_body": r["description"],   # RSS 가 본문을 줬으므로 상세 HTTP 불필요
            })
    log.info("부처 정책뉴스 크롤링 %d건", len(items))
    return items


# ── S5 KOTRA 해외시장뉴스 (data.go.kr 오픈API — 서비스키 필요) ─────────
# 서비스: data.go.kr "KOTRA 해외시장뉴스" (기관: 대한무역투자진흥공사).
# 응답은 data.go.kr 표준(XML: <response><body><items><item>…). 필드명이 서비스마다
# 조금씩 달라, 흔한 이름을 후보로 두고 첫 성공 응답의 키를 로그로 남긴다.
KOTRA_API_URL_DEFAULT = "https://apis.data.go.kr/B410001/ovseaMrktNews/ovseaMrktNewsList"
_kotra_keys_logged = [False]
_KOTRA_FIELDS = {
    "title": ("newsTitl", "cntntsSj", "titl", "title", "newsTitle"),
    "body":  ("newsCn", "cntntsCn", "cn", "newsCntnts", "contents"),
    "date":  ("newsWrtDt", "regDt", "frstRegistDt", "newsRegDt", "wrtDt"),
    "url":   ("newsOrgnlUrl", "newsUrl", "orgnlUrl", "url", "newsLink"),
    "id":    ("newsId", "cntntsId", "id", "newsSn"),
    "nation": ("newsNatArea", "natN", "nationNm", "cntryNm", "area"),
}


def _kotra_pick(row: dict, key: str) -> str:
    for n in _KOTRA_FIELDS[key]:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def crawl_kotra_news(service_key: str, rows: int = 60) -> list[dict]:
    """KOTRA 해외시장뉴스. service_key 가 없으면 빈 목록(비활성)."""
    if not service_key:
        return []
    import os
    import requests
    from xml.etree import ElementTree as ET

    url = os.environ.get("EA_KOTRA_API_URL", KOTRA_API_URL_DEFAULT).strip() or KOTRA_API_URL_DEFAULT
    gap = _REQ_GAP - (time.monotonic() - _last_req[0])
    if gap > 0:
        time.sleep(gap)
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": _UA},
                            params={"serviceKey": service_key, "numOfRows": rows,
                                    "pageNo": 1, "type": "json"})
        _last_req[0] = time.monotonic()
        resp.raise_for_status()
    except Exception as exc:
        log.warning("KOTRA 해외시장뉴스 조회 실패: %s", exc)
        return []

    records: list[dict] = []
    text = resp.text.strip()
    try:
        data = resp.json()
        # data.go.kr JSON: response.body.items.item (list 또는 dict)
        node = data
        for k in ("response", "body", "items"):
            if isinstance(node, dict) and k in node:
                node = node[k]
        item = node.get("item") if isinstance(node, dict) else node
        records = item if isinstance(item, list) else ([item] if isinstance(item, dict) else [])
    except ValueError:
        try:
            root = ET.fromstring(text)
            records = [{c.tag: (c.text or "").strip() for c in it}
                       for it in root.iter("item")]
        except ET.ParseError:
            log.warning("KOTRA 응답 형식 불명 (앞 200자): %s", text[:200])
            return []

    if records and not _kotra_keys_logged[0]:
        log.info("KOTRA 응답 필드: %s", sorted(records[0].keys()))
        _kotra_keys_logged[0] = True

    out: list[dict] = []
    for r in records:
        title = _kotra_pick(r, "title")
        link = _kotra_pick(r, "url") or (
            f"https://dream.kotra.or.kr/kotranews/cms/news/actionKotraBoardDetail.do?"
            f"pageNo=1&pRttSrchKeyword=&pNttSn={_kotra_pick(r, 'id')}" if _kotra_pick(r, "id") else "")
        if not title or not link:
            continue
        start, _ = parse_period(_kotra_pick(r, "date"))
        nation = _kotra_pick(r, "nation")
        body = _clean(re.sub(r"<[^>]+>", " ", _html_mod.unescape(_kotra_pick(r, "body"))))
        out.append({
            "url_source": link, "url_canonical": link,
            "item_type": "trade_news",
            "title": f"[{nation}] {title}" if nation and nation not in title else title,
            "law_name": "", "agency": "KOTRA",
            "notice_start": start, "notice_end": None,
            "status": "발표", "opinion_url": None,
            "attachment_urls": [], "published_at": start,
            "_body": body,
        })
    log.info("KOTRA 해외시장뉴스 크롤링 %d건", len(out))
    return out


def _law_name(title: str) -> str:
    t = re.sub(r"\s*\d{7}\b.*$", "", title or "")
    t = re.sub(r"\s*(일부개정|전부개정|제정)?(법률안|령안|규칙안|안)?\s*"
               r"(입법예고|행정예고)?\s*(일부)?\s*$", "", t).strip()
    return t or (title or "").strip()
