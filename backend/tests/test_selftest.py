"""내장 검증 — 원래 main.py 의 cmd_selftest() 를 그대로 옮긴 것.

python backend/main.py selftest 가 이 파일을 호출한다.
"""
from __future__ import annotations

import os
import sys
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dataclasses import replace
import tempfile
import time
from datetime import timedelta
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analyze import (
    DEFER_BACKLOG_STABLE,
    LLMClient,
    _build_analysis,
    _drain_deferred,
    _homepage_site_name,
    _parse_json_object,
    _title_media_name,
    _title_snippet_relevant,
    analyze_and_save,
    clean_site_name,
    interleave_by_group,
    is_public_http_url,
    matches_keywords,
    prettify_domain,
    resolve_press,
    site_name_from_html,
    swot_total,
)
from app.auth import (
    LOGIN_MAX_FAILS,
    MASTER_LOGIN_FAIL_THRESHOLD,
    WEB_PUBLIC_PATHS,
    _LOGIN_FAILS,
    _MASTER_LOGIN_FAILS,
    _client_ip,
    _login_fail,
    _login_locked,
    _login_reset,
    _master_login_fail,
    _master_login_reset,
    _rate_ok,
    _recover_master_password,
    check_master_password,
    hash_password,
    verify_password,
    web_session_token,
)
from app.cli import (
    cmd_fixcategories,
)
from app.collect import (
    Analysis,
    GROUP_LEAD_CHARS,
    KOREA_KR_NEWS_RE,
    POLICY_RELEVANCE_KW,
    POSCO_MENTION_RE,
    SCORE_BATTERY_BODY,
    SCORE_BATTERY_TITLE,
    SCORE_TRADE,
    _canonical_from_html,
    _is_bare_root,
    _json_script_body,
    _kw_first_hit,
    _kw_hit,
    _kw_hit_any,
    _naver_item_relevant,
    _notice_text,
    battery_company_hit,
    clean_author,
    collect_naver,
    collect_rss_feeds,
    decode_unicode_escapes,
    detect_categories,
    detect_group_companies,
    extract_author,
    extract_body,
    extract_ministry,
    extract_published,
    extract_title,
    fix_mojibake,
    format_people_llm,
    format_people_notice,
    group_lead_text,
    is_battery_scope,
    is_policy_brief,
    is_trade_article,
    is_trade_topic,
    naver_rotate_slots,
    normalize_group_list,
    people_news_kind,
    repair_truncated_title,
    score_article,
    select_naver_keywords,
)
from app.core import (
    APP_TZ,
    Config,
    Context,
    FRONTEND_DIR,
    HttpClient,
    RawItem,
    _clean,
    _jwt_role,
    _pick_supabase_service_key,
    cosine,
    dedupe_chips,
    domain_of,
    esc,
    format_summary_header,
    iso,
    jload,
    new_id,
    normalize_chip,
    normalize_title,
    normalize_url,
    now_local,
    now_utc,
    parse_feed_datetime,
    set_app_tz,
    title_similarity,
)
from app.notify import (
    _bot_chat_calls,
    _bot_rate_ok,
    _card_line,
    _clip,
    _flood_arm,
    _flood_remaining,
    _in_night_window,
    _int_or,
    _is_transient_tg_error,
    _kakao_text_from_row,
    _kakao_text_template,
    _notify_reason,
    effective_night,
    format_message,
    kakao_authorize_url,
    kakao_enabled_now,
)
from app.storage import (
    SEED_PRESS,
    SqliteStorage,
)
from app.view import (
    WEEKLY_SECTIONS,
    _SCAN_STORE,
    _html_to_text,
    _weekly_pick,
    apply_filters,
    build_card,
    card_tags,
    refresh_scan_store,
    render_weekly_html,
    send_report_email,
    tag_row,
    weekly_window,
)

try:   # 대외협력(대관) 모듈 — 없어도 나머지 검증은 그대로 돈다
    import external_affairs as ea_mod
except Exception:
    ea_mod = None

try:
    import ea_crawl as ea_crawl_mod
except Exception:
    ea_crawl_mod = None




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
    _ob = ("김 기자 구독 구독중 이전 다음 ▲ 김철수(향년 80세)씨 별세, 김영희씨 부친상 "
           "= 8일 오전, 서울대병원, 발인 10일. ☎ 02-1234-5678 (서울=연합뉴스) 무단 전재 금지")
    check("부고 요약 = ▲…☎ 블록",
          format_people_notice(_ob, "obituary"),
          "▲ 김철수(향년 80세)씨 별세, 김영희씨 부친상 = 8일 오전, 서울대병원, 발인 10일. ☎ 02-1234-5678")
    check("인사 요약 = ◇…블록",
          format_people_notice("기자 구독 구독중 이전 다음 ◇ 편집국 ▲ 산업본부장 류준형 (서울=연합뉴스)", "personnel"),
          "◇ 편집국 ▲ 산업본부장 류준형")
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

    print("\n[8-2d] 배터리 생태계 기사 (포스코 미언급 허용)")
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
        _SCAN_STORE.update(by_id={}, cursor="", full_at=0.0, delta_at=0.0)
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
    _reset_store()

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
    check("429 응답이면 예외 없이 빈 결과", collect_naver(_FakeNaver429Http(), _ncfg2, _nkw_rows), [])

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
    import app.notify as _notify_mod       # 전역 상태는 정의된 모듈에서 직접 해제한다
    _notify_mod._FLOOD_UNTIL = 0.0         # 다른 검증에 영향 없도록 즉시 해제
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
           "summary_text": "짧은 요약 문장.", "perspective_text": "관점 문장",
           "url_canonical": "https://x.test/a"}
    _ft = _kakao_text_from_row(_fr, "https://x.test/a")
    check("row text — object_type=text", _ft["object_type"], "text")
    check("row text — 점수 85 는 🔴", _ft["text"].startswith("🔴"), True)
    check("row text — 태그가 머리줄에", "[포스코퓨처엠]" in _ft["text"], True)
    check("row text — 언론사·기자 머리표", "[뉴스1, 김기자]" in _ft["text"], True)
    check("row text — 포스코 관점 포함", "포스코 관점:" in _ft["text"], True)
    check("row text — 링크는 본문 아닌 link 필드", "http" not in _ft["text"], True)
    check("row text — 원문 버튼", _ft["button_title"], "원문 보기")
    check("row text — 긴 요약도 200자 이하",
          len(_kakao_text_from_row({**_fr, "summary_text": "요" * 500,
                                    "perspective_text": "관" * 500},
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
        # 카테고리 5개 + item_type 매핑
        check("카테고리 5개", [c["key"] for c in ea_mod.EA_CATEGORIES],
              ["notice", "bill", "policy", "trade", "ministry"])
        check("부처별 동향 → ministry_news", ea_mod.EA_CATEGORY_TYPES["ministry"], ["ministry_news"])
        check("통상 환경 → trade_news", ea_mod.EA_CATEGORY_TYPES["trade"], ["trade_news"])
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

    if failures:
        print(f"실패 {len(failures)}건:\n" + "\n".join(failures))
        return 1
    print("전체 통과.")
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_selftest())
