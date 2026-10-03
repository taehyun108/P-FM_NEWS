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
# 서비스: data.go.kr/15034831 "대한무역투자진흥공사_해외시장뉴스".
#   GET https://apis.data.go.kr/B410001/kotra_overseasMarketNews/ovseaMrktNews/ovseaMrktNews
#        (오퍼레이션명이 base 경로 끝에 한 번 더 붙는다 — data.go.kr GW 규칙)
#   필수 파라미터: serviceKey, pageNo, numOfRows. 응답: response.body.itemList.item (최신순).
#   본문 필드는 없다(제목 자체가 완결된 문장이라 그것을 요약으로 쓴다).
#   필드: newsTitl 제목 · kotraNewsUrl 링크 · othbcDt 게시일 · natn 국가 · indstCl 산업(콤마)
#        · infoCl 분류(트렌드/경제·무역/통상·규제/…) · bbstxSn 번호 · ovrofInfo 무역관
KOTRA_API_URL_DEFAULT = ("https://apis.data.go.kr/B410001"
                         "/kotra_overseasMarketNews/ovseaMrktNews/ovseaMrktNews")
# 포스코 그룹에 닿는 산업분류·정보분류만 남긴다(해외시장뉴스는 소비재·농식품이 대부분).
_KOTRA_INDUST_KEEP = ("철강", "금속", "자동차", "수송기기", "화학", "광물", "에너지",
                      "조선", "기계", "전자", "전기", "건설", "인프라", "플랜트",
                      "그리드", "전력망", "환경")
_KOTRA_INFO_KEEP = ("통상·규제", "국별 주요산업", "글로벌 공급망")
_KOTRA_TITLE_KW = ("철강", "제철", "배터리", "이차전지", "2차전지", "양극재", "음극재",
                   "리튬", "니켈", "코발트", "흑연", "전기차", "EV", "ESS", "수소",
                   "핵심광물", "희토류", "공급망", "관세", "반덤핑", "상계관세",
                   "세이프가드", "수출통제", "IRA", "CBAM", "무역장벽")
_kotra_keys_logged = [False]


def _kotra_relevant(r: dict) -> bool:
    ind = r.get("indstCl") or ""
    info = (r.get("infoCl") or "").strip()
    title = _html_mod.unescape(r.get("newsTitl") or "")
    return (any(k in ind for k in _KOTRA_INDUST_KEEP)
            or info in _KOTRA_INFO_KEEP
            or any(k in title for k in _KOTRA_TITLE_KW))


def crawl_kotra_news(service_key: str, rows: int = 100, pages: int = 2) -> list[dict]:
    """KOTRA 해외시장뉴스. service_key 가 없으면 빈 목록(비활성).

    data.go.kr 서비스키는 인코딩본으로 배포되므로 unquote 후 requests 가 한 번만
    인코딩하게 한다(그대로 넘기면 %2F→%252F 로 이중 인코딩돼 400 이 난다).
    """
    if not service_key:
        return []
    import os
    from urllib.parse import unquote
    import requests

    url = (os.environ.get("EA_KOTRA_API_URL", "").strip() or KOTRA_API_URL_DEFAULT)
    key = unquote(service_key)   # 이미 디코딩돼 있으면 그대로

    records: list[dict] = []
    for page in range(1, pages + 1):
        gap = _REQ_GAP - (time.monotonic() - _last_req[0])
        if gap > 0:
            time.sleep(gap)
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": _UA},
                                params={"serviceKey": key, "numOfRows": rows,
                                        "pageNo": page, "returnType": "json"})
            _last_req[0] = time.monotonic()
            resp.raise_for_status()
            data = resp.json()
        except ValueError:
            m = re.search(r"<(?:errMsg|returnReasonCode)>([^<]+)</", resp.text)
            log.warning("KOTRA API 오류: %s (서비스키 승인·URL 확인)",
                        m.group(1) if m else resp.text[:120])
            break
        except Exception as exc:
            log.warning("KOTRA 해외시장뉴스 조회 실패: %s", exc)
            break
        hdr = (data.get("response") or {}).get("header") or {}
        if str(hdr.get("resultCode")) not in ("0", "00"):
            log.warning("KOTRA API 응답 코드 %s: %s", hdr.get("resultCode"), hdr.get("resultMsg"))
            break
        body = (data.get("response") or {}).get("body") or {}
        il = body.get("itemList")
        got = il.get("item") if isinstance(il, dict) else il
        got = got if isinstance(got, list) else ([got] if isinstance(got, dict) else [])
        records.extend(g for g in got if isinstance(g, dict))
        if len(got) < rows:
            break

    if records and not _kotra_keys_logged[0]:
        log.info("KOTRA 응답 필드: %s", sorted(records[0].keys()))
        _kotra_keys_logged[0] = True

    out: list[dict] = []
    for r in records:
        if not _kotra_relevant(r):
            continue
        title = _clean(_html_mod.unescape(r.get("newsTitl") or ""))
        rid = str(r.get("bbstxSn") or "").strip()
        link = (r.get("kotraNewsUrl") or "").strip() or (
            f"https://dream.kotra.or.kr/user/extra/kotranews/bbs/linkView/jsp/Page.do?dataIdx={rid}"
            if rid else "")
        if not title or not link:
            continue
        nation = (r.get("natn") or "").strip()
        ind = (r.get("indstCl") or "").strip()
        info = (r.get("infoCl") or "").strip()
        start, _ = parse_period(r.get("othbcDt") or "")
        # 본문이 없으므로 제목 + 메타를 요약 재료로 넘긴다(제목이 완결된 문장이다).
        synth = " ".join(x for x in (
            title, f"({nation} · {ind})" if nation or ind else "",
            f"KOTRA {r.get('ovrofInfo') or ''} · {info}".strip(" ·"),
        ) if x)
        out.append({
            "url_source": link, "url_canonical": link,
            "item_type": "trade_news",
            "title": f"[{nation}] {title}" if nation and nation not in title else title,
            "law_name": "", "agency": "KOTRA",
            "notice_start": start, "notice_end": None,
            "status": "발표", "opinion_url": None,
            "attachment_urls": [], "published_at": start,
            "_body": synth,
        })
    log.info("KOTRA 해외시장뉴스 크롤링 %d건", len(out))
    return out


# ── S6 · 정책브리핑(korea.kr) 보도자료 ─────────────────────────────────
#   브리핑룸 > 보도자료 목록: https://www.korea.kr/briefing/pressReleaseList.do
#   목록 한 줄(li > a) 에 링크(newsId)·본문 앞부분(span.lead)·날짜·소관 부처가 들어 있다.
#   제목(strong)은 비어 있는 경우가 있어, 관련 항목으로 확정된 것만 상세(h1)에서 따로 읽는다.
KOREA_KR = "https://www.korea.kr"
_KOREA_PRESS_ID = re.compile(r"pressReleaseView\.do\?newsId=(\d+)")
_KOREA_DATE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})$")
_PRESS_TAG_WORDS = ("참고", "보도", "해명", "설명", "사진", "보충", "브리핑", "정정", "연합")
_PRESS_TAG_OPEN = {"(": ")", "[": "]", "（": "）", "【": "】"}


def _strip_press_tag(title: str) -> str:
    """'(참고자료, 1(목) 16시엠바고)제목' → '제목'. 말머리 안에 괄호가 또 있어도 짝을 맞춰 뗀다."""
    t = (title or "").strip()
    while t and t[0] in _PRESS_TAG_OPEN and t[1:1 + 2] and any(t[1:].startswith(w) for w in _PRESS_TAG_WORDS):
        close, depth = _PRESS_TAG_OPEN[t[0]], 0
        end = -1
        for i, ch in enumerate(t):
            if ch in _PRESS_TAG_OPEN:
                depth += 1
            elif ch in (")", "]", "）", "】"):
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            break
        t = t[end + 1:].strip()
    return t


def _press_url(news_id: str) -> str:
    return f"{KOREA_KR}/briefing/pressReleaseView.do?newsId={news_id}"


def _first_phrase(lead: str, limit: int = 60) -> str:
    """제목이 비어 있을 때 쓰는 임시 제목 — 본문 첫머리에서 문장·'- ' 구분 앞까지."""
    t = _clean(lead)
    t = re.split(r"\s-\s|[.。]\s|□|ㅇ", t, maxsplit=1)[0].strip(" -·")
    return (t[:limit] + "…") if len(t) > limit else t


def parse_korea_press_list(html: str) -> list[dict]:
    """보도자료 목록 HTML → [{news_id, url, title, lead, date(ISO), agency}]. 순서는 화면 그대로(최신순)."""
    soup = _soup(html)
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _KOREA_PRESS_ID.search(a["href"])
        if not m or m.group(1) in seen:
            continue
        nid = m.group(1)
        seen.add(nid)
        strong = a.find("strong")
        lead_el = a.find("span", class_="lead")
        lead = _clean(lead_el.get_text(" ")) if lead_el else ""
        lead = re.sub(r"[·…]{2,}\s*$", "", lead).strip()
        # 날짜 span 과 그 다음 span(소관 부처)을 찾는다 — '2026.10.01' 다음의 글자가 부처명
        texts = [_clean(x.get_text(" ")) for x in a.find_all(["span", "em", "b"])]
        date_iso, agency = None, ""
        for i, t in enumerate(texts):
            dm = _KOREA_DATE.match(t)
            if dm:
                date_iso = _iso(*dm.groups())
                for nxt in texts[i + 1:]:
                    if nxt and not _KOREA_DATE.match(nxt) and "2026" not in nxt[:4] and len(nxt) <= 20:
                        agency = nxt
                        break
                break
        if not date_iso:        # span 구조가 달라도 글 끝의 '날짜 부처' 로 읽는다
            full = _clean(a.get_text(" "))
            tm = re.search(r"(\d{4})\.(\d{2})\.(\d{2})\s*([가-힣A-Za-z·]{2,20})\s*$", full)
            if tm:
                date_iso, agency = _iso(*tm.groups()[:3]), tm.group(4)
        out.append({"news_id": nid, "url": _press_url(nid),
                    "title": _clean(strong.get_text(" ")) if strong else "",
                    "lead": lead, "date": date_iso, "agency": agency})
    return out


def parse_korea_press_title(html: str) -> str:
    """보도자료 상세 HTML 에서 제목(h1 → og:title). '(참고자료)' 같은 말머리는 뗀다."""
    soup = _soup(html)
    h1 = soup.find("h1")
    title = _clean(h1.get_text(" ")) if h1 else ""
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = _clean(og.get("content", "")) if og else ""
    return _strip_press_tag(title)


def fetch_press_title(url: str) -> str:
    """상세 페이지 1건 요청. 실패하면 빈 문자열."""
    try:
        return parse_korea_press_title(_get(url))
    except Exception as exc:
        log.debug("보도자료 제목 확보 실패 %s: %s", url, exc)
        return ""


def crawl_korea_press(days: int = 3, max_pages: int = 5) -> list[dict]:
    """최근 days 일 보도자료를 최신순으로 훑는다(페이지당 약 20건, 페이지 수 상한 있음).

    한 페이지가 통째로 기간 밖이면 거기서 멈춘다. 제목이 비어 있으면 _title_missing 로 표시해 둔다
    (관련 항목으로 통과한 것만 collect_once 가 상세에서 제목을 채운다).
    """
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            html = _get(f"{KOREA_KR}/briefing/pressReleaseList.do",
                        {"pageIndex": page, "startDate": start, "endDate": end})
        except Exception as exc:
            log.warning("S6 정책브리핑 보도자료 목록 실패 (page=%d): %s", page, exc)
            break
        rows = parse_korea_press_list(html)
        if not rows:
            break
        for r in rows:
            title = _strip_press_tag(r["title"])
            out.append({
                "url_source": r["url"], "url_canonical": r["url"], "item_type": "policy_press",
                "title": title or _first_phrase(r["lead"]), "_title_missing": not title,
                "law_name": "", "agency": r["agency"] or "정책브리핑",
                "notice_start": r["date"], "notice_end": None, "status": "정책 발표",
                "opinion_url": "", "attachment_urls": [], "published_at": r["date"],
                "_body": r["lead"],
            })
        if all((r["date"] or "9999") < start for r in rows):
            break
    log.info("S6 정책브리핑 보도자료 수집 %d건", len(out))
    return out


# ── S7 · 월간 통상(tongsangnews.kr) — 산업통상부가 발행하는 통상 웹진 ──────────────
#   기자명이 없는 정부 발행물이라 소관은 '산업통상부'로 적는다. 메인 페이지 메뉴에 이번 호 기사 링크
#   (/webzine/YYYYMM/YYYYMMDDnnnnn.html)와 분류명이 전부 들어 있다. 기사 id 앞 8자리가 날짜다.
TONGSANG = "https://tongsangnews.kr"
_TS_LINK = re.compile(r"/webzine/(\d{6})/(\d{8})(\d{5})\.html")
TONGSANG_MIN_BODY = 150     # 인포그래픽·캘린더처럼 글이 거의 없는 꼭지는 카드로 만들지 않는다


def parse_tongsang_index(html: str) -> list[dict]:
    """메인 페이지 → [{id, url, category, date}] (중복 제거, 등장 순서)."""
    soup = _soup(html)
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        m = _TS_LINK.search(a["href"])
        if not m:
            continue
        aid = m.group(2) + m.group(3)
        label = _clean(a.get_text(" "))
        em = a.find("em")
        if em:
            label = _clean(em.get_text(" "))
        cur = found.get(aid)
        if cur is None:
            d = m.group(2)
            found[aid] = {"id": aid, "url": f"{TONGSANG}/webzine/{m.group(1)}/{aid}.html",
                          "category": label if len(label) <= 24 else "", "date": f"{d[:4]}-{d[4:6]}-{d[6:]}"}
        elif not cur["category"] and label and len(label) <= 24:
            cur["category"] = label
    return list(found.values())


def parse_tongsang_article(html: str) -> dict:
    """기사 페이지 → {title, sub, category, thumb, body}. 본문은 문단 글만 이어 붙인다(광고·메뉴 제외)."""
    soup = _soup(html)
    main = soup.select_one(".contents-tit .main")
    title = _clean(main.get_text(" ")) if main else ""
    if not title:
        mt = soup.find("meta", attrs={"name": "title"}) or soup.find("meta", attrs={"property": "og:title"})
        title = _clean(mt.get("content", "")) if mt else ""
    sub = soup.select_one(".contents-tit .sub")
    cat = soup.select_one(".nav-guide .cat_name")
    category = " ".join(_clean(cat.get_text(" ")).split(" ")[1:]) if cat else ""    # '이달의뉴스 > 글로벌 통상 뉴스' → 뒤쪽
    og = soup.find("meta", attrs={"property": "og:image"})
    paras = [_clean(p.get_text(" ")) for p in soup.select(".editor-template .par p")]
    body = "\n".join(x for x in paras if x)
    if not body:
        md = soup.find("meta", attrs={"name": "description"})
        body = _clean(md.get("content", "")) if md else ""
    return {"title": title, "sub": _clean(sub.get_text(" ")) if sub else "", "category": category,
            "thumb": og.get("content", "") if og else "", "body": body}


def fetch_tongsang_detail(url: str) -> dict:
    try:
        return parse_tongsang_article(_get(url))
    except Exception as exc:
        log.debug("월간 통상 기사 조회 실패 %s: %s", url, exc)
        return {}


def crawl_tongsang(max_items: int = 60) -> list[dict]:
    """이번 호 기사 목록(제목은 상세에서 채운다 — collect_once 가 새 항목만 읽는다)."""
    try:
        rows = parse_tongsang_index(_get(TONGSANG + "/"))
    except Exception as exc:
        log.warning("S7 월간 통상 목록 실패: %s", exc)
        return []
    out = [{"url_source": r["url"], "url_canonical": r["url"], "item_type": "trade_webzine",
            "title": r["category"] or "월간 통상", "_need_detail": True, "_trusted": True,
            "_cat_label": r["category"], "law_name": "", "agency": "산업통상부",
            "notice_start": r["date"], "notice_end": None, "status": "월간 통상",
            "opinion_url": "", "attachment_urls": [], "published_at": r["date"]}
           for r in rows[:max_items]]
    log.info("S7 월간 통상 목록 %d건", len(out))
    return out


# ── S8 · 공모·수요조사·지원사업 공고 — 산업부 사업공고 · 기후부 공지·공고 · IRIS(범부처통합연구지원시스템) ────
#   서비스키 없이 공개 페이지만 읽는다. 목록은 최근 몇 쪽만 본다(요청 간 1초).
#   마감일(접수 마감)은 IRIS 상세의 '접수기간' 칸이나 공고문 글 속 '접수기간 … ~ …' 에서만 읽고, 못 읽으면 비워 둔다
#   (정부 부처 공고문은 본문이 짧고 기간은 첨부(hwp·pdf)에 있는 일이 많다 — 첨부는 열지 않는다).
MOTIR_NOTICE_LIST = "https://www.motir.go.kr/kor/article/ATCL2826a2625"       # 산업통상부 알림·뉴스 > 사업공고
MCEE = "https://www.mcee.go.kr"
MCEE_NOTICE_LIST = MCEE + "/home/web/board/list.do"                              # 기후에너지환경부 공지·공고(boardMasterId=39)
IRIS = "https://www.iris.go.kr"
_DATE_ANY = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
_DEADLINE_KEY = re.compile(r"(접수|신청|제출|공모|모집|마감)\s*(기간|기한|일시|일정|마감)?")


def deadline_from_text(text: str) -> str | None:
    """공고문 글에서 접수 마감일(ISO)을 찾는다. '접수기간 2026.9.22 ~ 2026.10.21' · '~ 2026. 10. 21.(수) 18:00까지' 형태.

    '접수·신청·제출·마감' 같은 낱말 뒤 120자 안의 날짜만 본다 — 공고일·시행일을 마감으로 오인하지 않으려는 것이다.
    기간이면 뒤쪽 날짜를, 날짜 하나뿐이면 그것을 쓴다. 못 찾으면 None.
    """
    t = re.sub(r"\s+", " ", text or "")
    best: str | None = None
    for m in _DEADLINE_KEY.finditer(t):
        win = t[m.end(): m.end() + 120]
        dates = [_iso(*d) for d in _DATE_ANY.findall(win)]
        dates = [d for d in dates if d]
        if not dates:
            continue
        cand = dates[1] if ("~" in win[:win.find(_DATE_ANY.findall(win)[0][2]) + 40] and len(dates) > 1) else dates[-1] \
            if "~" in win[:80] and len(dates) > 1 else dates[0]
        if best is None or cand > best:
            best = cand
    return best


def parse_motir_notice_list(html: str) -> list[dict]:
    """산업부 사업공고 목록 → [{no, title, url, dept, date, attachments[]}] (표의 data-cell-header 로 읽는다)."""
    soup = _soup(html)
    out: list[dict] = []
    for tr in soup.select("table tbody tr"):
        cell = {td.get("data-cell-header", ""): td for td in tr.find_all("td")}
        a = cell.get("제목").find("a", href=True) if cell.get("제목") else None
        if not a:
            continue
        date = _clean(cell["등록일"].get_text(" ")) if cell.get("등록일") else ""
        att = [x["href"] for x in (cell["첨부파일"].find_all("a", href=True) if cell.get("첨부파일") else [])]
        out.append({"no": _clean(cell["공고번호"].get_text(" ")) if cell.get("공고번호") else "",
                    "title": _clean(a.get_text(" ")), "url": a["href"].split("?")[0],
                    "dept": _clean(cell["담당부서"].get_text(" ")) if cell.get("담당부서") else "",
                    "date": date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None, "attachments": att})
    return out


def parse_mcee_notice_list(html: str) -> list[dict]:
    """기후부 공지·공고 목록 → [{id, title, url, dept, date}]. jsessionid 는 주소에서 뗀다."""
    soup = _soup(html)
    out: list[dict] = []
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(r"board/read\.do"))
        if not a:
            continue
        m = re.search(r"boardId=(\d+)", a["href"])
        if not m:
            continue
        cells = [_clean(td.get_text(" ")) for td in tr.find_all("td")]
        date = next((c for c in cells if re.fullmatch(r"\d{4}-\d{2}-\d{2}", c)), None)
        dept = cells[2] if len(cells) > 2 and not re.fullmatch(r"[\d,]+", cells[2]) else ""
        out.append({"id": m.group(1), "title": _clean(a.get_text(" ")) or a.get("title", ""),
                    "url": f"{MCEE}/home/web/board/read.do?menuId=10524&boardId={m.group(1)}&boardMasterId=39",
                    "dept": dept, "date": date})
    return out


def parse_iris_detail(html: str) -> dict:
    """IRIS 공고 상세 → {period_end(ISO)|None, body}. '접수기간' 칸에서 마감일을 읽는다."""
    soup = _soup(html)
    text = _clean(soup.get_text(" "))
    i = text.find("접수기간")
    seg = text[i: i + 160] if i >= 0 else ""
    dates = [_iso(*d) for d in _DATE_ANY.findall(seg)]
    dates = [d for d in dates if d]
    body_i = text.find("■ 공고문")
    return {"period_end": dates[-1] if dates else deadline_from_text(text),
            "body": text[body_i: body_i + 4000] if body_i >= 0 else text[:2000]}


def fetch_notice_detail(url: str, kind: str = "") -> dict:
    """공고 상세 1건 → {deadline, body}. 실패하면 {}."""
    try:
        html = _get(url)
        if kind == "iris":
            d = parse_iris_detail(html)
            return {"deadline": d["period_end"], "body": d["body"]}
        soup = _soup(html)
        node = soup.select_one(".board-detail, .board_view, .bbs_view, .view_cont, #contents") or soup
        body = _clean(node.get_text(" "))
        return {"deadline": deadline_from_text(body), "body": body[:4000]}
    except Exception as exc:
        log.debug("공고 상세 조회 실패 %s: %s", url, exc)
        return {}


def crawl_motir_notices(max_pages: int = 2) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            rows = parse_motir_notice_list(_get(MOTIR_NOTICE_LIST, {"pageIndex": page}))
        except Exception as exc:
            log.warning("S8 산업부 사업공고 목록 실패 (page=%d): %s", page, exc)
            break
        if not rows:
            break
        for r in rows:
            out.append({"url_source": r["url"], "url_canonical": r["url"], "item_type": "grant_notice",
                        "title": r["title"], "_grant": True, "_detail_kind": "motir", "law_name": r["dept"],
                        "agency": "산업통상부", "notice_start": r["date"], "notice_end": None,
                        "status": "공고", "opinion_url": "", "attachment_urls": r["attachments"][:3],
                        "published_at": r["date"]})
    return out


def crawl_mcee_notices(max_pages: int = 3) -> list[dict]:
    out: list[dict] = []
    for page in range(max_pages):
        try:
            rows = parse_mcee_notice_list(_get(MCEE_NOTICE_LIST, {
                "menuId": 10524, "boardMasterId": 39, "maxPageItems": 10, "pagerOffset": page * 10}))
        except Exception as exc:
            log.warning("S8 기후부 공지·공고 목록 실패 (page=%d): %s", page + 1, exc)
            break
        if not rows:
            break
        for r in rows:
            out.append({"url_source": r["url"], "url_canonical": r["url"], "item_type": "grant_notice",
                        "title": r["title"], "_grant": True, "_detail_kind": "mcee", "law_name": r["dept"],
                        "agency": "기후에너지환경부", "notice_start": r["date"], "notice_end": None,
                        "status": "공고", "opinion_url": "", "attachment_urls": [], "published_at": r["date"]})
    return out


_iris_logged = [False]


def _iris_date(v) -> str | None:
    """IRIS 날짜 값 → ISO. '2026-10-21'·'20261021'·'2026.10.21 18:00' 모두 읽는다. 못 읽으면 None."""
    t = str(v or "").strip()
    if not t:
        return None
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", t)
    if m:
        return _iso(*m.groups())
    d = _DATE_ANY.search(t)
    return _iso(*d.groups()) if d else None


def crawl_iris_notices(max_pages: int = 2) -> list[dict]:
    """IRIS 사업공고(접수중). 목록은 화면이 부르는 JSON(POST) 을 그대로 쓴다.

    요청 항목명은 화면 소스에서 읽은 값이다. 응답이 예상과 다르면 첫 응답의 키를 로그에 남기고 빈 목록을 돌려준다.
    """
    import requests
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        gap = _REQ_GAP - (time.monotonic() - _last_req[0])
        if gap > 0:
            time.sleep(gap)
        try:
            resp = requests.post(IRIS + "/contents/retrieveBsnsAncmBtinSituList.do", timeout=20,
                                 headers={"User-Agent": _UA, "X-Requested-With": "XMLHttpRequest",
                                          "Referer": IRIS + "/contents/retrieveBsnsAncmBtinSituListView.do"},
                                 data={"pageIndex": page, "ancmPrg": "ancmIng", "bsnsTl": "", "blngGovdSeArr": "",
                                       "sorgnIdArr": "", "techFildArr": "", "ancmSttArr": "", "pbofrTpArr": "",
                                       "qualCndtArr": ""})
            _last_req[0] = time.monotonic()
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("S8 IRIS 사업공고 목록 실패 (page=%d): %s", page, exc)
            break
        rows = data.get("listBsnsAncmBtinSitu") or []
        if not _iris_logged[0]:
            log.info("S8 IRIS 응답 키: %s · 첫 행 키: %s", sorted(data.keys()), sorted(rows[0].keys()) if rows else [])
            _iris_logged[0] = True
        if not rows:
            break
        for r in rows:
            aid = str(r.get("ancmId") or "")
            if not aid:
                continue
            url = f"{IRIS}/contents/retrieveBsnsAncmView.do?ancmId={aid}&ancmPrg=ancmIng"
            ad = str(r.get("ancmDe") or "")[:10]
            # 2026-10-02 서버 로그로 확인: 목록 응답에 접수 시작·마감일(rcveStrDe·rcveEndDe)이 이미 들어 있다 — 상세를 열 필요가 없다
            end_iso = _iris_date(r.get("rcveEndDe"))
            str_iso = _iris_date(r.get("rcveStrDe")) or (ad or None)
            title = _clean(str(r.get("ancmTl") or ""))
            sorgn = _clean(str(r.get("sorgnNm") or ""))
            out.append({"url_source": url, "url_canonical": url, "item_type": "grant_notice",
                        "title": title, "_grant": True,
                        "_detail_kind": "none" if end_iso else "iris",
                        "_body": f"{title} · 전문기관 {sorgn} · 공모유형 {_clean(str(r.get('pbofrTpSeNmLst') or ''))}"
                                 f" · 접수 {str_iso or '?'} ~ {end_iso or '?'}",
                        "law_name": sorgn,
                        "agency": _clean(str(r.get("blngGovdSeNm") or "")) or "범부처",
                        "notice_start": str_iso, "notice_end": end_iso,
                        "status": "접수중", "opinion_url": "", "attachment_urls": [],
                        "published_at": ad or None, "_ancm_no": str(r.get("ancmNo") or "")})
        pg = data.get("paginationInfo") or {}
        if page >= int(pg.get("totalPageCount") or page):
            break
    log.info("S8 IRIS 사업공고 목록 %d건", len(out))
    return out


# ── 화면이 자바스크립트로 그려지는 사이트(KOTRA 등) — 브라우저(Playwright·Chromium)로 읽기 ─────────────
#   일반 요청으로는 껍데기만 오는 사이트용이다(2026-10-02 승인된 라이브러리: playwright).
#   · 설치돼 있지 않으면 경고 한 번만 남기고 건너뛴다 — 다른 소스 수집은 영향이 없다.
#   · 하루 두 번뿐인 대외협력 수집에서만 쓰고, 사이트당 브라우저를 한 번 띄워 쪽을 넘기며 읽는다.
#   · 사이트를 더 늘리려면 RENDERED_SITES 에 (이름·주소·기다릴 요소·읽는 함수)만 추가한다.
_render_warned = [False]


def render_snapshots(url: str, wait_selector: str, pages: int = 1, next_page=None,
                     timeout_ms: int = 30000) -> list[str]:
    """브라우저로 url 을 열고 wait_selector 가 나타나면 HTML 을 한 장 찍는다. pages>1 이면 next_page(page, n) 로 쪽을 넘겨 더 찍는다.

    실패(미설치·타임아웃 등)는 지금까지 찍은 것만 돌려준다(없으면 빈 목록).
    """
    import os
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if not _render_warned[0]:
            log.warning("playwright 미설치 — 화면이 동적으로 그려지는 사이트(KOTRA 등)는 건너뜁니다")
            _render_warned[0] = True
        return []
    snaps: list[str] = []
    try:
        with sync_playwright() as pw:
            exe = os.environ.get("EA_CHROMIUM_PATH", "").strip() or None
            browser = pw.chromium.launch(
                executable_path=exe,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                page = browser.new_page(user_agent=_UA, locale="ko-KR")
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
                snaps.append(page.content())
                for n in range(2, pages + 1):
                    if not next_page or not next_page(page, n):
                        break
                    page.wait_for_timeout(1500)
                    snaps.append(page.content())
            finally:
                browser.close()
    except Exception as exc:
        log.warning("브라우저 읽기 실패 (%s): %s", url, exc)
    return snaps


KOTRA_BID_URL = "https://www.kotra.or.kr/subList/20000005958?tabid=1092"      # 알림·홍보 > 입찰공고


def parse_kotra_bids(html: str) -> list[dict]:
    """KOTRA 입찰공고 목록(다 그려진 HTML) → [{title, date, deadline, method, extra, nttseq}]. 마감일은 ISO."""
    import hashlib
    soup = _soup(html)
    out: list[dict] = []
    for tr in soup.select("table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        cells = [_clean(td.get_text(" ")) for td in tds]
        title = re.sub(r"^제목\s*:\s*", "", cells[0])
        if not title:
            continue
        dates = [_iso(*d) for d in _DATE_ANY.findall(cells[1])]
        date = next((d for d in dates if d), None)
        dl = [_iso(*d) for d in _DATE_ANY.findall(cells[2])]
        deadline = next((d for d in dl if d), None)
        method = re.sub(r"^입찰방법\s*:\s*", "", cells[3]) if len(cells) > 3 else ""
        extra = re.sub(r"^기타\s*:\s*", "", cells[4]) if len(cells) > 4 else ""
        # 상세 주소가 있으면 쓰고(onclick 의 번호), 없으면 제목+작성일로 만든 고유값을 붙인다
        ntt = ""
        for tag in [tr] + tr.find_all(True):
            blob = " ".join(str(tag.get(k, "")) for k in ("onclick", "href", "data-nttseq", "data-ntt-seq"))
            m = re.search(r"(\d{3,})", blob)
            if m:
                ntt = m.group(1)
                break
        if not ntt:
            ntt = "h" + hashlib.sha1((title + (date or "")).encode("utf-8")).hexdigest()[:10]
        out.append({"title": title, "date": date, "deadline": deadline, "method": method, "extra": extra,
                    "nttseq": ntt})
    return out


def _kotra_next_page(page, n: int) -> bool:
    """입찰공고 목록 하단 쪽 번호 n 을 눌러 이동한다. 없으면 False."""
    try:
        link = page.locator("#detail_area .paging a, #detail_area .pagination a, #detail_area [class*=page] a").filter(
            has_text=re.compile(rf"^\s*{n}\s*$")).first
        if link.count() == 0:
            return False
        link.click(timeout=5000)
        return True
    except Exception:
        return False


def crawl_kotra_bids(pages: int = 2) -> list[dict]:
    """KOTRA 입찰공고 — 아직 마감 전인 것만. 상세 페이지는 열지 않고 목록 정보(입찰방법·계약방식)로 본문을 만든다."""
    snaps = render_snapshots(KOTRA_BID_URL, "table tbody tr td", pages=pages, next_page=_kotra_next_page)
    today = datetime.now(timezone.utc).date().isoformat()
    out: list[dict] = []
    seen: set[str] = set()
    for html in snaps:
        for r in parse_kotra_bids(html):
            if r["nttseq"] in seen or (r["deadline"] and r["deadline"] < today):
                continue
            seen.add(r["nttseq"])
            url = f"{KOTRA_BID_URL}&nttSeq={r['nttseq']}"
            out.append({"url_source": url, "url_canonical": KOTRA_BID_URL, "item_type": "grant_notice",
                        "title": r["title"], "_grant": True, "_detail_kind": "none",
                        "_body": f"{r['title']} · 입찰방법 {r['method']} · {r['extra']} · 마감 {r['deadline'] or '미정'}",
                        "law_name": r["method"], "agency": "KOTRA", "notice_start": r["date"],
                        "notice_end": r["deadline"], "status": "입찰 접수중", "opinion_url": "",
                        "attachment_urls": [], "published_at": r["date"]})
    log.info("S8 KOTRA 입찰공고 %d건(마감 전)", len(out))
    return out


# 더 늘리려면 여기에 한 줄 — 화면이 동적으로 그려지는 사이트(이름, 수집 함수)
RENDERED_SITES = [("KOTRA 입찰공고", crawl_kotra_bids)]


def _law_name(title: str) -> str:
    t = re.sub(r"\s*\d{7}\b.*$", "", title or "")
    t = re.sub(r"\s*(일부개정|전부개정|제정)?(법률안|령안|규칙안|안)?\s*"
               r"(입법예고|행정예고)?\s*(일부)?\s*$", "", t).strip()
    return t or (title or "").strip()
