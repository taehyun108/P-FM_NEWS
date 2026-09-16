"""① 뉴스 수집·본문 파싱·분류/점수 기준"""
from __future__ import annotations

from typing import Any
from collections import Counter
from typing import Iterable
from typing import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from dataclasses import field
import html as html_mod
import json
import re
import threading
import time
from urllib.parse import urlencode
from urllib.parse import urlsplit

from .core import (
    Config,
    HttpClient,
    RawItem,
    _has_hangul,
    _import,
    _looks_like_domain,
    clamp,
    dedupe_chips,
    get_env_int,
    jload,
    log,
    normalize_chip,
    normalize_url,
    parse_dt,
    parse_feed_datetime,
)



# 다음·네이버 뉴스 래퍼 도메인 — 그 자체가 언론사가 아니다.
# 이런 페이지의 og:site_name 은 'Daum | 뉴스1' 처럼 '래퍼 | 실제출처' 형태라
# 마지막 구분자 뒤(실제 출처)를 매체명으로 쓴다.
NEWS_AGGREGATORS = frozenset({"daum.net", "naver.com"})



# 그룹사 정규 명칭 — LLM 이 만든 그룹사명은 이 목록에 없으면 버린다. (PRD F4.2)
#   키 = 표시명(필터 칩·카드에 이 이름이 나온다). 값 = 본문에서 찾을 별칭(법인격 ㈜/(유) 제외).
#   주요 5개사는 옛 사명·영문명까지, 그 밖 계열사(사용자 제공 51개소 목록, 2026-09-02)는 사명만.
#   '포스코'(상위 개념)는 계열사가 특정되면 normalize_group_list 에서 제거된다.
GROUP_COMPANIES: dict[str, list[str]] = {
    "포스코홀딩스": ["포스코홀딩스", "POSCO홀딩스", "posco holdings"],
    "포스코퓨처엠": ["포스코퓨처엠", "POSCO퓨처엠", "posco future m", "포스코케미칼"],
    "포스코DX": ["포스코DX", "포스코 DX", "posco dx", "포스코ICT"],
    "포스코인터내셔널": ["포스코인터내셔널", "posco international", "포스코대우"],
    "포스코이앤씨": ["포스코이앤씨", "posco e&c", "포스코건설"],
    # ── 그 밖 계열사 ──────────────────────────────────────────────────
    "포스코스틸리온": ["포스코스틸리온"],
    "포스코엠텍": ["포스코엠텍"],
    "포스코휴먼스": ["포스코휴먼스"],
    "피엔알": ["피엔알(PNR)", "피엔알"],
    "엔투비": ["엔투비", "N2B"],
    "포항특수용접봉": ["포항특수용접봉"],
    "포스코피알테크": ["포스코피알테크"],
    "포스코피에스테크": ["포스코피에스테크"],
    "포스코피에이치솔루션": ["포스코피에이치솔루션"],
    "포스코지와이알테크": ["포스코지와이알테크"],
    "포스코지와이에스테크": ["포스코지와이에스테크"],
    "포스코지와이솔루션": ["포스코지와이솔루션"],
    "포항에스알디씨": ["포항에스알디씨"],
    "이스틸포유": ["이스틸포유"],
    "켐가스코리아": ["켐가스코리아"],
    "포스코에스피": ["포스코에스피"],
    "포스코모빌리티솔루션": ["포스코모빌리티솔루션"],
    "탄천이앤이": ["탄천이앤이"],
    "삼척블루파워": ["삼척블루파워"],
    "한국퓨얼셀": ["한국퓨얼셀"],
    "신안그린에너지": ["신안그린에너지"],
    "에코에너지솔루션": ["에코에너지솔루션"],
    "우이신설경전철": ["우이신설경전철"],
    "게일인터내셔널코리아": ["게일인터내셔널코리아"],
    "송도개발피엠씨": ["송도개발피엠씨"],
    "포스코에이앤씨": ["포스코에이앤씨건축사사무소", "포스코에이앤씨", "포스코A&C"],
    "알앤알물류": ["알앤알물류"],
    "포스코엠씨머티리얼즈": ["포스코엠씨머티리얼즈"],
    "퓨처그라프": ["퓨처그라프"],
    "포스코지에스에코머티리얼즈": ["포스코지에스에코머티리얼즈", "포스코GS에코머티리얼즈"],
    "포스코에이치와이클린메탈": ["포스코에이치와이클린메탈", "포스코HY클린메탈"],
    "포스코플로우": ["포스코플로우"],
    "플로우케이": ["플로우케이"],
    "포스코와이드": ["포스코와이드"],
    "포스코경영연구원": ["포스코경영연구원", "포스리", "POSRI"],
    "포스코기술투자": ["포스코기술투자"],
    "에스엔엔씨": ["에스엔엔씨(SNNC)", "에스엔엔씨", "SNNC"],
    "부산이앤이": ["부산이앤이"],
    "포스코인재창조원": ["포스코인재창조원"],
    "포스코아이에이치": ["포스코아이에이치"],
    "포스코필바라리튬솔루션": ["포스코필바라리튬솔루션"],
    "포스코리튬솔루션": ["포스코리튬솔루션"],
    "큐에스원": ["큐에스원"],
    "포스코에어솔루션": ["포스코에어솔루션"],
    "포스코세이프티솔루션": ["포스코세이프티솔루션"],
    # 포스코퓨처엠 합작사·관계사 (2026-09-02)
    "얼티엄캠": ["ultiumcam", "얼티엄캠", "얼티엄 캠", "얼티엄캡"],
    "절강포화": ["절강포화", "저장포화", "浙江浦华", "zhejiang puhua"],
    "절강화포": ["절강화포", "저장화포", "浙江华浦", "zhejiang huapu"],
    "씨앤피신소재테크놀로지": ["씨앤피신소재테크놀로지", "c&p신소재테크놀로지",
                              "씨앤피신소재", "cnp신소재"],
    # 상위 개념 — 계열사가 특정되면 제거됨
    "포스코": ["포스코", "POSCO"],
}



# 별칭 소문자 사전계산 — detect_group_companies·score_article 이 호출마다
# `[a.lower() for a in aliases]` 를 다시 만들던 것을 제거한다(파이프라인·API 공통 경로).
_GROUP_ALIASES_LOWER: dict[str, list[str]] = {
    canonical: [a.lower() for a in aliases] for canonical, aliases in GROUP_COMPANIES.items()
}



# 그룹사 판정에 쓰는 본문 길이. 기사의 '주체'는 제목과 리드 문단에 드러난다.
#
# 본문 전체를 스캔하면 말미의 스치는 언급이 주체를 가로챈다. 실제 사례:
#   '포스코 직접고용 이행하겠다지만…'(부산일보) 본문 1,963자 중 1,859번째에
#   '포스코홀딩스 장인화 회장' 이 한 번 나오는데, detect_group_companies 의
#   `if not found` 때문에 '포스코홀딩스' 만 붙고 정작 주체인 '포스코' 는 빠졌다.
# 리드 700자로 줄이면 위 기사는 '포스코', 계열사를 실제로 나열하는 공채 기사는
# 4개 계열사가 그대로 잡힌다(계열사명이 0~270자에 등장). detect_categories 가
# 제목+요약만 보는 것과 같은 이유다.
GROUP_LEAD_CHARS = 700


# 700자 고정컷 밖에서도 예외적으로 살리는 문장: '~을 비롯해 A, 포스코이앤씨, B 등이
# 참여/시공/수주한다' 처럼 참여사·제안사·수상사 등을 정식으로 나열하는 문장
# (실제 사례 ①: 대보건설 신안산선 기사, 본문 835자 중 737번째 '포스코이앤씨' —
#  700자 컷에 37자 차이로 빠짐.
#  실제 사례 ②: QuINSA 총회 기사, "IQM, 포스코홀딩스, SDT, 노르마, 연세대 등이 …
#  표준화 과제를 제안했다" — 동사 화이트리스트에 '제안'이 없어서 빠짐. 이런
#  나열문 동사는 제안·발표·수상·선정·체결 등 사실상 무한해 동사 조건을 뺐었다.)
#
# 처음엔 "기사가 짧으면 통째로 본다"로 고쳤다가 회귀를 냈다: '포스코 노사 파업'
# 기사에서 인용문 "이제 공은 포스코홀딩스 장인화 회장에게 넘어갔다"의
# '포스코홀딩스' 가 detect_group_companies 의 `if not found` 때문에 진짜 주체인
# '포스코' 를 밀어냈다 — 원래 700자 컷이 막던 바로 그 실패 모드다. '쉼표 3개
# 이상'만으로 막았더니(②를 고치며 동사 조건을 뺀 뒤) 다시 회귀가 났다: 같은
# 파업 기사의 "2022년 출범한 지주회사인 포스코홀딩스로 배당금, 브랜드사용료
# 등이 상당액 들어가면서, 포스코 영업이익이 …" 처럼 쉼표 3개짜리 '서술 문장'이
# '회사 나열 문장'으로 오인됐다.
# 두 문장의 진짜 차이는 '항목이 짧은 명사 그 자체로 끝나는가' 다 — 진짜 나열은
# 'A, B, C 등이' 처럼 각 항목이 조사·어미 없이 뚝뚝 끊기고 짧다. 그래서 마지막
# 항목(서술부가 붙는 게 정상)을 뺀 나머지 항목이 전부 15자 이내이고 조사·연결
# 어미로 끝나지 않아야 '나열 문장' 으로 인정한다.
_LIST_ITEM_MAX_CHARS = 15


_NOT_LIST_ITEM_END_RE = re.compile(
    r"(?:은|는|이|가|을|를|의|에|와|과|로|으로|에서|에게|한테|보다|처럼|만큼|"
    r"까지|부터|면서|하며|이며|다가|해서|이라|라서|고|며|서|면|데|니|자)$")




def _is_company_list_sentence(sentence: str) -> bool:
    """'A, B, C 등이 …' 처럼 명사를 나열하는 문장인지 — 서술 문장 속 쉼표 3개와
    구분한다(위 group_lead_text 주석의 실패 사례 참고).

    첫 항목은 '공사에는 대보건설을 비롯해 포스코이앤씨' 처럼 도입구가 붙어 길어질
    수 있어 길이는 안 재고 조사/어미로 안 끝나는지만 본다. 중간 항목(있다면)은
    도입구·서술부 둘 다 없는 '진짜 나열 한복판'이라 짧고 깨끗해야 한다. 마지막
    항목은 서술부('~등이 참여했다')가 붙는 게 정상이라 아예 안 본다.
    """
    parts = [p.strip() for p in re.split(r"[,·]", sentence) if p.strip()]
    if len(parts) < 3:
        return False
    if _NOT_LIST_ITEM_END_RE.search(parts[0]):
        return False
    for item in parts[1:-1]:
        if len(item) > _LIST_ITEM_MAX_CHARS or _NOT_LIST_ITEM_END_RE.search(item):
            return False
    # '후원(하고 있는) A, B, C가 참여' 처럼 스포츠·공연 등 제3자 행사의 후원사를
    # 나열하는 문장은 실제 사업 소식이 아니라 단순 협찬 언급이라 그룹사로 치지
    # 않는다. (실사례: e스포츠 대회 후원사 명단에 '포스코'가 있어 무관 기사가
    # 걸림 — LCK 결승전 기사, 2026-09-16)
    if re.search(r"후원(하[고는]|사)", sentence):
        return False
    return True




def group_lead_text(body: str) -> str:
    """그룹사 판정용 리드 텍스트 — 기본 리드 + 참여사 나열 문장(리드 밖이어도)."""
    lead = body[:GROUP_LEAD_CHARS]
    tail = body[GROUP_LEAD_CHARS:]
    if not tail:
        return lead
    extra: list[str] = []
    for sentence in re.split(r"(?<=[.\n])\s*", tail):
        if not sentence or not _is_company_list_sentence(sentence):
            continue
        if any(alias in sentence.lower() for aliases in _GROUP_ALIASES_LOWER.values() for alias in aliases):
            extra.append(sentence)
    return lead + "\n" + "\n".join(extra) if extra else lead




REGROUP_DAYS = 3   # `regroup` 명령이 되돌아볼 기간. 그보다 오래된 카드는 화면에서 밀려난다.



TRADE_CATEGORY = "글로벌 통상환경"


# '특정 국가의 명시적 통상 조치' 만 넣는다. '공급망 재편'·'디리스킹' 같은 일반 트렌드어는
# 노사·실적 기사에도 스치듯 나와 오태깅되므로 제외한다.
TRADE_MEASURE_KW = [
    # 미국
    "IRA", "인플레이션감축법", "OBBBA", "FEOC", "해외우려기관", "무역확장법 232조", "232조 관세",
    "무역법 301조", "301조 관세", "세이프가드", "상호관세", "보편관세",
    # 유럽
    "CBAM", "탄소국경조정", "탄소국경세", "핵심원자재법", "CRMA", "역외보조금 규정",
    "EU 배터리규정", "공급망실사지침", "CSDDD",
    # 유럽 — 산업·탈탄소 입법 (2025~2026). 조치명이 명시적이라 오탐 낮다.
    "산업가속화법", "산업 가속화법", "탄소중립산업법", "넷제로산업법", "NZIA",
    "청정산업딜", "클린산업딜", "그린딜 산업계획", "외국보조금규정", "외국보조금 규정",
    "역내산 요건", "역내 조달 요건", "저탄소 제품 기준", "저탄소 조달",
    # 중국
    "흑연 수출통제", "갈륨 수출", "게르마늄 수출", "안티모니 수출", "희토류 수출통제",
    "요소 수출제한", "반도체 장비 수출통제", "수출허가 대상",
    # 무역구제 (공식 절차)
    "반덤핑 관세", "반덤핑관세", "반덤핑 조사", "상계관세", "덤핑 판정", "긴급수입제한",
]


# 통상 기사가 '포스코 관련 산업'인지 판정.
TRADE_INDUSTRY_KW = [
    "배터리", "이차전지", "2차전지", "양극재", "음극재", "전구체", "리튬", "니켈", "코발트",
    "흑연", "전기차", "ESS", "핵심광물",
    "철강", "제철", "후판", "열연", "냉연", "선재", "강판", "스테인리스", "합금철", "봉형강",
    "포스코",
]



# 카테고리 태깅 규칙 (PRD F3.1)
#   제목+요약에서만 판정한다. 본문 전체를 스캔하면 부차적 언급까지 걸려
#   거의 모든 기사가 3~4개 카테고리를 달아 필터가 무의미해진다.
#   그래서 '정부'·'정책'·'셀'·'실적' 같은 흔한 단어는 넣지 않고 구체적 용어만 쓴다.
#   '지역'은 폐지했다 — 포스코 키워드가 들어간 기사만 수집하므로 지역 태깅이 무의미하다.
CATEGORY_RULES: dict[str, list[str]] = {
    # 양극재·음극재는 포스코퓨처엠 주력이라 별도 카테고리로 뺀다.
    # detect_categories 는 정의 순서대로 도니 배터리·이차전지 앞에 둬 더 구체적으로 잡는다.
    "양극재": ["양극재", "양극활물질", "하이니켈", "니켈코발트망간", "NCM", "NCA", "NCMA",
               "단결정 양극재", "코발트프리", "전구체", "리튬 전구체", "LFP", "LMFP", "LFP 양극재",
               # 양극재 경쟁사 — 회사명이 곧 사업 분야
               "에코프로비엠", "엘앤에프", "코스모신소재", "당성과기", "룽바이", "화유코발트"],
    "음극재": ["음극재", "음극활물질", "인조흑연", "천연흑연", "구상흑연", "실리콘 음극",
               "실리콘음극재", "SiOx", "리튬메탈 음극",
               # 음극재 경쟁사
               "BTR", "베이터루이", "샨샨", "대주전자재료"],
    "배터리·이차전지": ["이차전지", "2차전지", "배터리", "리튬",
                        "니켈", "코발트", "흑연", "전고체", "ESS", "전기차", "배터리셀"],
    "산업": ["철강", "제철", "제철소", "고로", "용광로", "전기로", "조강", "쇳물", "열연", "냉연",
             "후판", "선재", "강판", "형강", "스테인리스", "합금철", "제련", "정련",
             "수소환원제철", "하이렉스", "HyREX", "조선", "완성차", "설비 증설",
             # 포스코 그룹 사업 전반 — 건설·인프라·로봇·자동화
             "재건축", "재개발", "정비사업", "시공", "수주", "분양", "플랜트", "EPC",
             "로봇", "협동로봇", "휴머노이드", "스마트팩토리"],
    # 정부/정책 = 한국 정부의 국내 산업·에너지 정책·입법·행정. (외국 통상조치는 '글로벌 통상환경')
    #   detect_categories 가 '핵심어가 제목에 있을 때만' 태깅한다(TITLE_ANCHORED_CATEGORIES).
    #   '보조금'·'정부 지원'·'관세'·'IRA' 처럼 관용적으로 쓰이는 낱말은 넣지 않는다 —
    #   전기차 판매 기사·실적 기사까지 정책으로 태깅된다(보조금 한 낱말로 7/14건 오태깅).
    "정부/정책": ["산업부", "환경부", "기재부", "국토부", "중기부", "과기정통부", "공정위",
                  "국정감사", "예비타당성", "예비타당성조사", "예타 면제", "국정과제",
                  "부처 합동", "범부처", "특화단지", "국가첨단전략산업", "소부장 특별법",
                  "규제 완화", "규제 혁신", "규제 샌드박스", "세액공제", "국비 지원",
                  "추가경정예산", "추경 편성", "예산안", "육성 방안", "종합대책",
                  "개정안", "제정안", "시행령", "특별법", "입법예고", "본회의 통과",
                  "국회 통과", "법안 발의", "중대재해처벌법", "노란봉투법"],
    # 미국·유럽·중국 등 주요국의 명시적 통상 조치. 정의는 TRADE_MEASURE_KW 와 맞춘다.
    # detect_categories 가 '제목에 조치명' 조건을 한 번 더 건다.
    "글로벌 통상환경": TRADE_MEASURE_KW,
    "시장/주가": ["주가", "증권", "코스피", "코스닥", "목표주가", "시황", "상한가", "하한가",
                  "거래량", "거래대금", "시가총액", "PER", "PBR", "공매도", "외국인 순매수",
                  "기관 순매수", "배당"],
}



POLICY_CATEGORY = "정부/정책"


# 이 카테고리들은 '핵심어가 제목에 있을 때만' 태깅한다. 요약·본문에 스친 언급은
# 부차 주제라, 넣으면 필터가 변별력을 잃는다(통상·정책 모두 실제로 그랬다).
TITLE_ANCHORED_CATEGORIES = (TRADE_CATEGORY, POLICY_CATEGORY)



# 카테고리 규칙 소문자 사전계산 — detect_categories 가 호출마다 각 단어를
# 소문자로 바꾸던 것을 제거한다(API 필터 스캔에서 행 수 × 카테고리 수만큼 반복됐다).
_CATEGORY_RULES_LOWER: dict[str, list[str]] = {
    name: [w.lower() for w in words] for name, words in CATEGORY_RULES.items()
}



# 중요도 가중치 (PRD F3.2)
SCORE_FUTUREM_TITLE = 50


SCORE_FUTUREM_BODY = 40


SCORE_GROUP = 25


SCORE_BATTERY_TITLE = 25   # 배터리 생태계(소재·셀·전기차·ESS·원료) 키워드가 제목에


SCORE_BATTERY_BODY = 12    # 본문에만


SCORE_TRADE = 15           # 글로벌 통상환경 신호


SCORE_POLICY = 20


SCORE_MAJOR_PRESS = 10


SCORE_MARKET_PENALTY = -15



# 기본 중요도 항목 — 마스터 패널이 점수·사용여부를 그대로 편집한다(단일 출처).
# 판정 로직(is_battery_scope 등)은 코드에 있어 항목 자체를 지울 순 없지만,
# enabled=false 로 두면 점수 기여가 0이 되어 사실상 삭제와 같은 효과를 낸다.
SCORE_RULE_DEFS: dict[str, tuple[int, str]] = {
    "futurem_title": (SCORE_FUTUREM_TITLE, "포스코퓨처엠이 제목에"),
    "futurem_body": (SCORE_FUTUREM_BODY, "포스코퓨처엠이 본문에만"),
    "group": (SCORE_GROUP, "다른 계열사(홀딩스·DX·인터내셔널·이앤씨 등)"),
    "battery_title": (SCORE_BATTERY_TITLE, "배터리 생태계(소재·셀·전기차·ESS·원료)가 제목에"),
    "battery_body": (SCORE_BATTERY_BODY, "배터리 생태계가 본문에만"),
    "trade": (SCORE_TRADE, "해외 통상 조치(IRA·CBAM·반덤핑 등) 신호"),
    "policy": (SCORE_POLICY, "정책 키워드(전기요금·배출권·특화단지 등)"),
    "major_press": (SCORE_MAJOR_PRESS, "주요 언론사(연합·전자신문·머니투데이 등)"),
    "market_penalty": (SCORE_MARKET_PENALTY, "단순 시황·주가 기사(목표주가·코스피·투자의견)"),
}


SCORE_CUSTOM_MAX = 30       # 사용자 추가 항목 상한


SCORE_RULES_CACHE_SEC = 20  # run_state 재조회 간격 — 기사마다 DB 를 다시 묻지 않는다



_score_rules_cache: dict[str, Any] = {"at": 0.0, "overrides": {}, "customs": []}




def get_score_rules(storage: "Storage") -> tuple[dict[str, dict], list[dict]]:
    """마스터 패널에서 고친 중요도 규칙(기본 항목 재정의 + 사용자 추가 항목).

    호출마다 DB 를 묻지 않도록 짧게 캐시한다(파이프라인 한 회차 안에서 기사마다
    다시 조회하면 Supabase 왕복이 기사 수만큼 늘어난다)."""
    now = time.monotonic()
    if now - _score_rules_cache["at"] > SCORE_RULES_CACHE_SEC:
        state = storage.get_run_state()
        _score_rules_cache["overrides"] = jload(state.get("score_overrides"), {}) or {}
        _score_rules_cache["customs"] = jload(state.get("score_custom_rules"), []) or []
        _score_rules_cache["at"] = now
    return _score_rules_cache["overrides"], _score_rules_cache["customs"]


SCORE_PEOPLE_NEWS = 12     # 인사·부고 — 웹 전용(임계값 미만), 알림 안 나감



POLICY_KEYWORDS = ["정책", "규제", "법안", "수사", "사고", "화재", "제재", "과징금",
                   "국회", "산업부", "환경부", "보조금", "특화단지", "인허가", "감사", "고발"]


MARKET_ONLY_KEYWORDS = ["목표주가", "투자의견", "코스피", "시황", "주가 전망", "증권가"]


# score_article 이 호출마다 소문자 변환하던 것을 사전계산한다.
_POLICY_KEYWORDS_LOWER = [w.lower() for w in POLICY_KEYWORDS]


_MARKET_ONLY_KEYWORDS_LOWER = [w.lower() for w in MARKET_ONLY_KEYWORDS]




GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


# NAVER 검색 API는 developers.naver.com 에서 NAVER Cloud Platform 의
# 'NAVER API HUB' 로 통합되었다. 엔드포인트와 헤더가 바뀌었다.
#   구: https://openapi.naver.com/v1/search/news.json  +  X-Naver-Client-Id / -Secret
#   신: 아래 URL  +  X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY
NAVER_NEWS_API = "https://naverapihub.apigw.ntruss.com/search/v1/news"




POLICY_SITE = "site:www.korea.kr"   # '정책' 키워드는 정책브리핑으로만 검색한다




def collect_google_rss(http: HttpClient, keyword_rows: Sequence[dict]) -> list[RawItem]:
    feedparser = _import("feedparser", "feedparser")
    items: list[RawItem] = []
    for row in keyword_rows:
        keyword = row["keyword"]
        query = f"{POLICY_SITE} {keyword}" if row.get("category") == "정책" else keyword
        url = GOOGLE_NEWS_RSS.format(q=urlencode({"q": query})[2:])
        try:
            resp = http.get(url)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Google RSS 조회 실패 (%s): %s", keyword, exc)
            continue
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            link = entry.get("link", "")
            if not link:
                continue
            src_title = (entry.get("source", {}) or {}).get("title", "")
            # 정책브리핑 검색인데 생활정보 블로그(gonggam.korea.kr)면 버린다. (사용자 지정)
            if row.get("category") == "정책" and "gonggam" in f"{src_title} {entry.get('title','')}".lower():
                continue
            published = parse_feed_datetime(entry.get("published") or entry.get("updated"),
                                            entry.get("published_parsed") or entry.get("updated_parsed"))
            items.append(RawItem(
                url_source=normalize_url(link),
                url_original=link,
                title=html_mod.unescape(entry.get("title", "")).strip(),
                published_at=published,
                source_type="google_rss",
                press_hint=(entry.get("source", {}) or {}).get("title", ""),
                snippet=html_mod.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", ""))).strip(),
            ))
    return items




POSCO_MENTION_RE = re.compile(r"포스코|posco", re.IGNORECASE)




def _kw_hit(text: str, keywords: Sequence[str]) -> bool:
    """키워드 목록이 비어 있으면 True(조건 없음), 아니면 하나라도 본문에 있으면 True."""
    return (not keywords) or any(k in text for k in keywords)



# 대한민국 정책브리핑 기사 URL (생활정보 gonggam.korea.kr 은 제외).
KOREA_KR_NEWS_RE = re.compile(r"//(?:www\.)?korea\.kr/news/", re.IGNORECASE)



# 정책 기사가 '포스코 산업에 영향이 있는가' 판정용. 포스코 미언급이어도 이 중 하나면 수집한다.
POLICY_RELEVANCE_KW = [
    "철강", "제철", "이차전지", "배터리", "리튬", "니켈", "양극재",
    "전기요금", "전력", "전력수급", "송전", "발전",
    # 'ㅇㅇㅇ에너지기술평가원'·'에너지경제신문'처럼 기관명·매체명에 흔히 들어가는
    # 부분 문자열이라 '에너지' 단독은 뺐다(실제 오탐: 포항시 공공기관 유치 기사가
    # '한국에너지기술평가원' 언급만으로 정책 관련 기사로 잘못 수집됐다). 의미가
    # 뚜렷한 복합어로만 인정한다.
    "에너지 정책", "에너지 안보", "에너지 전환", "에너지 위기", "청정에너지",
    "탄소", "배출권", "탄소중립", "탄소국경", "CBAM", "RE100", "재생에너지", "수소",
    "산업", "제조", "공급망", "핵심광물", "통상", "관세", "무역", "수출",
    "산업단지", "특화단지", "투자", "보조금", "세제", "인센티브", "국가전략기술",
    "반도체", "친환경", "기후", "환경규제", "국토", "건설", "인프라", "SOC",
    "예산", "국정감사", "규제완화", "노동", "고용",
]




POLICY_BRIEF_PRESS = "대한민국 정책브리핑"



def _kw_hit_any(text: str, keywords: Sequence[str]) -> bool:
    """_kw_hit 과 달리 빈 목록이면 False (조건이 반드시 있어야 하는 경우)."""
    return any(k in text for k in keywords)




def _kw_first_hit(text: str, keywords: Sequence[str]) -> str | None:
    """본문에 처음 걸린 키워드를 돌려준다. 없으면 None. (발송 이유 표시용)"""
    for k in keywords:
        if k and k in text:
            return k
    return None




def is_trade_topic(title: str, extra: str = "") -> bool:
    """글로벌 통상환경 기사인가.

    통상 조치는 헤드라인에 드러나므로 '제목'에 조치명이 있어야 한다.
    (요약·본문에만 스친 언급은 부차 주제 — 오태깅을 막는다.)
    포스코 관련 산업(철강·배터리)이 제목·요약에 함께 있어야 한다.
    """
    return _kw_hit_any(title, TRADE_MEASURE_KW) and _kw_hit_any(f"{title}\n{extra}", TRADE_INDUSTRY_KW)




def is_policy_brief(row: dict) -> bool:
    """정책브리핑(korea.kr) 기사인지. press_name 또는 URL 로 판정한다."""
    if (row.get("press_name") or "") == POLICY_BRIEF_PRESS:
        return True
    return bool(KOREA_KR_NEWS_RE.search(row.get("url_canonical") or row.get("url_original") or ""))




def is_trade_article(row: dict) -> bool:
    """저장된 기사가 통상환경 기사인지 — 카테고리 태그 또는 제목 기준으로 판정."""
    if TRADE_CATEGORY in jload(row.get("categories"), []):
        return True
    return is_trade_topic(row.get("title") or "", row.get("summary_text") or "")




# 포스코퓨처엠 사업(양극재·음극재)에 영향을 주는 배터리 생태계 전반.
# 소재·기술 개발뿐 아니라 셀 업체·전기차 수요·ESS 시장까지 — 전부 전방 수요다.
# 이 범위에 들면 포스코 회사명이 없어도 수집한다. (사용자 지정)
BATTERY_SCOPE_KW = [
    # 소재·차세대 기술
    "전고체", "리튬메탈", "리튬금속 배터리", "황리튬", "황-리튬", "리튬황",
    "나트륨이온", "나트륨 배터리", "소듐이온", "소듐 배터리",
    "하이니켈", "단결정 양극재", "코발트프리", "망간리치", "LMFP", "LFP",
    "실리콘 음극", "실리콘음극재", "SiOx", "리튬메탈 음극", "무음극",
    "고체 전해질", "고분자 전해질", "건식 전극", "드라이 전극", "전해액", "분리막",
    "양극재", "음극재", "전구체", "차세대 배터리", "차세대 이차전지",
    # 전지·셀
    "이차전지", "2차전지", "배터리셀", "배터리 셀", "배터리팩", "배터리 공장", "기가팩토리",
    "배터리 수주", "배터리 합작", "배터리 투자", "배터리 시장", "배터리 수요",
    # 셀·완성차 업체 (전방 수요)
    "LG에너지솔루션", "삼성SDI", "SK온", "CATL", "BYD", "파나소닉",
    "테슬라", "Tesla", "리비안", "루시드", "폭스바겐 배터리", "GM 배터리", "포드 배터리",
    # 소재 경쟁사 (포스코퓨처엠 양극재·음극재·전구체 직접 경쟁) — 회사명 정확 매칭이라 오탐 낮음
    "에코프로", "엘앤에프", "코스모신소재", "대주전자재료", "나노신소재", "한솔케미칼",
    "BTR", "베이터루이", "샨샨", "샨산", "룽바이", "롱바이", "CNGR", "중웨이",
    "화유코발트", "당성과기", "스미토모금속광산", "니치아",
    "LG화학 양극재", "LG화학 첨단소재",
    # 국내 배터리 생태계 (셀·소재·부품·리사이클) — 제목에 '배터리'가 없어도 이 회사명이면 관련 기사다.
    # 지명과 겹치는 짧은 이름(금양·천보)은 여기 두지 않고 BATTERY_COMPANY_KW + 가드로만 본다.
    "에코프로비엠", "에코프로머티리얼즈", "성일하이텍", "새빗켐", "탑머티리얼",
    "SK아이이테크놀로지", "더블유씨피", "동화일렉트로라이트", "엔켐", "솔루스첨단소재",
    # 전기차 수요
    "전기차 판매", "전기차 수요", "전기차 보조금", "전기차 캐즘", "EV 수요", "전기차 시장",
    # ESS (에너지저장)
    "ESS", "에너지저장장치", "에너지저장시스템", "전력저장", "계통 안정화", "BESS",
    # 원료
    "리튬 가격", "니켈 가격", "코발트 가격", "탄산리튬", "수산화리튬", "흑연 공급",
]




def is_battery_scope(title: str, extra: str = "") -> bool:
    """포스코퓨처엠 전·후방(소재·셀·전기차·ESS·원료) 기사인가.

    이 범위면 포스코 미언급이어도 수집한다 — 전방 수요·경쟁 동향이 사업에 직결된다.
    지명과 겹치는 짧은 회사명은 battery_company_hit 이 가드와 함께 판정한다.
    """
    probe = f"{title}\n{extra}"
    return _kw_hit_any(probe, BATTERY_SCOPE_KW) or battery_company_hit(probe)




# 배터리 생태계 '회사명'만 추린 고정밀 신호.
# 일반어('배터리'·'전기차')와 달리 회사명은 검색 블러브에 우연히 끼어들지 않으므로,
# 제목이 아닌 **리드(스니펫)** 에서 발견돼도 관련 기사로 인정한다.
#   실제 누락 사례: "금양, 기장공장을 데이터센터로 물적분할 추진…상폐 돌파구"
#   — 제목에 '배터리'가 없어 제목 전용 필터에서 통째로 버려졌다(본문은 배터리 사업 얘기).
BATTERY_COMPANY_KW = [
    "LG에너지솔루션", "삼성SDI", "SK온", "CATL", "BYD", "파나소닉",
    "에코프로", "에코프로비엠", "에코프로머티리얼즈", "엘앤에프", "코스모신소재",
    "대주전자재료", "나노신소재", "한솔케미칼", "금양", "성일하이텍", "새빗켐",
    "탑머티리얼", "SK아이이테크놀로지", "더블유씨피", "천보", "동화일렉트로라이트",
    "엔켐", "솔루스첨단소재",
    "BTR", "베이터루이", "샨샨", "룽바이", "CNGR", "중웨이", "화유코발트", "당성과기",
]



# 짧은 회사명은 지명·학교명과 글자가 겹친다. 그 형태로만 나오면 회사 언급이 아니다.
#   예: '부산 기장군 금양읍 도시계획' → '금양'(배터리 셀 업체)이 아니다.
BATTERY_COMPANY_FALSE_HINTS: dict[str, list[str]] = {
    "금양": ["금양읍", "금양면", "금양리", "금양동", "금양초", "금양중", "금양고", "금양역"],
    "천보": ["천보산", "천보사", "천보루"],
}




def battery_company_hit(text: str) -> bool:
    """배터리 생태계 회사명이 실제로 언급됐는가 (지명·학교명 오탐 제외)."""
    t = text or ""
    for kw in BATTERY_COMPANY_KW:
        if kw not in t:
            continue
        hints = BATTERY_COMPANY_FALSE_HINTS.get(kw)
        if hints:
            stripped = t
            for h in hints:
                stripped = stripped.replace(h, "")
            if kw not in stripped:
                continue   # 지명·기관명 형태로만 나왔다 — 회사 언급 아님
        return True
    return False




# ── 인사·부고 (포스코 관점 없이 공지 원문만 요약) ───────────────────────
PEOPLE_NEWS_CATEGORY = "인사·부고"


_OBIT_TITLE_RE = re.compile(r"^\s*(?:\[|【)\s*(?:부고|訃告|부음)\s*(?:\]|】)")


_OBIT_URL_RE = re.compile(r"obituary", re.I)


_PERSONNEL_TITLE_RE = re.compile(r"^\s*(?:\[|【)\s*(?:인사|人事|승진|신임|취임|프로필)\s*(?:\]|】)")


# 말머리가 없는 정부부처·기관 인사 공지. '인사발령'·'보직인사'는 사실상 인사 공지에서만
# 쓰는 표현이라 제목에 있으면 personnel 로 본다. (motir.go.kr 등 부처 사이트 대응)
_GOV_PERSONNEL_RE = re.compile(r"인사\s*발령|보직\s*인사|전보\s*발령|승진\s*임용")




def people_news_kind(url: str, title: str) -> str:
    """인사·부고 기사면 종류('obituary' | 'personnel'), 아니면 ''.

    제목 말머리([부고]·[인사]·[승진]…)나 연합뉴스 부고 섹션 URL 로 판정한다.
    말머리가 없어도 '인사발령'·'보직인사' 같은 정부부처 인사 공지 표현이 있으면 personnel.
    '동정'(장관 활동 등)은 포함하지 않는다 — 일반 기사로 흐르게 둔다.
    """
    t = title or ""
    if _OBIT_TITLE_RE.search(t) or _OBIT_URL_RE.search(url or ""):
        return "obituary"
    if _PERSONNEL_TITLE_RE.search(t) or _GOV_PERSONNEL_RE.search(t):
        return "personnel"
    return ""




_NOTICE_HEAD_RE = re.compile(
    r"^.*?(?:구독\s*구독중\s*이전\s*다음|이미지\s*확대.*?자료사진\s*\])\s*")


_NOTICE_TAIL_RE = re.compile(
    r"\s*(?:\(\S+=\s*연합뉴스\)|※\s*부고\s*요청|&lt;저작권자|<저작권자|제보는\s*카카오톡"
    r"|무단\s*전재|\d{4}/\d{2}/\d{2}\s*\d{2}:\d{2}\s*송고).*$")




def format_people_notice(body: str, kind: str) -> str:
    """인사·부고 공지에서 핵심 블록만 남긴다. LLM 을 쓰지 않는다.

    부고:  '▲ 별세·상주 … = 빈소, 발인, 장지 ☎ 전화'
    인사:  '◇ 부서 ▲ 직책 이름 …'
    """
    text = re.sub(r"\s+", " ", (body or "").strip())
    text = _NOTICE_HEAD_RE.sub("", text, count=1)
    text = _NOTICE_TAIL_RE.sub("", text).strip()
    if kind == "obituary":
        m = re.search(r"▲.*?☎[\s\d\-()]+", text) or re.search(r"▲.*", text)
        if m:
            text = m.group(0).strip()
    else:
        m = re.search(r"[◇▲■].*", text)
        if m:
            text = m.group(0).strip()
    return text[:800]




# ── 인사·부고 LLM 구조화 (사용자 지정 2026-09-09) ──────────────────────
# 소스 기사(연합·뉴시스 인사 RSS)는 이름·소속만 나열한 덤프라 읽기 어렵다.
# LLM 으로 사람별 항목(소속 국·과 / 직급 / 승진·전보 / 고시·회차 / 이력 / 업무 / 학력)을
# 뽑되, **기사에 실제로 적힌 것만** 채운다. 외부 검색으로 보강하지 않는다.
PEOPLE_NOTICE_SYSTEM = (
    "당신은 정부·공공기관 인사·부고 공지를 정리하는 편집자다. "
    "주어진 기사 본문에 실제로 적힌 사실만 사용하고, 없는 항목은 빈 값으로 둔다. "
    "고시 회차·이력·학력 등은 기사에 없으면 절대 추측하지 않는다. JSON 으로만 답한다."
)


PEOPLE_NOTICE_PROMPT = """[구분] {kind}
[제목] {title}
[언론사] {press}
[본문]
{body}

위 공지를 사람별로 정리해 JSON 하나로만 답하라.

인사(승진·전보·임용·취임 등)이면 각 사람마다:
  name       이름
  org        소속 (부처 + 국/과, 기사에 있는 만큼)
  position   직급/직위 (예: 부이사관, 국장)
  change     인사 종류 (승진 | 전보 | 신규임용 | 취임 | 연임 | 퇴직 등)
  exam       임용 경로 (예: "행정고시 45회", "기술고시 30회") — 없으면 ""
  career     주요 이력 배열 (역임 보직과 기간, 예: "기재부 재정성과평가과장(23.5~25.5)") — 없으면 []
  duty       담당·역점 업무 — 없으면 ""
  education  출신 학교 — 없으면 ""
→ {{"kind":"personnel","people":[{{"name":"...","org":"...","position":"...","change":"...","exam":"","career":[],"duty":"","education":""}}]}}

부고면 각 대상마다:
  subject   별세자 (직함 포함)
  relation  부고 주체와의 관계 (예: "OOO 부장 부친상") — 없으면 ""
  mourners  상주 — 없으면 ""
  wake      빈소 — 없으면 ""
  funeral   발인 일시 — 없으면 ""
  burial    장지 — 없으면 ""
  contact   연락처 — 없으면 ""
→ {{"kind":"obituary","deaths":[{{"subject":"...","relation":"","mourners":"","wake":"","funeral":"","burial":"","contact":""}}]}}

규칙:
- 기사에 없는 항목은 빈 문자열/빈 배열로 둔다. 지어내지 말 것.
- 사람이 여러 명이면 모두 담되, 최대 20명.
- 본문이 이름 나열뿐이면 name·org·position·change 만 채우면 된다."""




def _pp(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()




def format_people_llm(parsed: dict, kind: str) -> str:
    """people_notice() 의 JSON 을 카드·텔레그램에 넣을 사람별 텍스트 블록으로 만든다."""
    if not isinstance(parsed, dict):
        return ""
    blocks: list[str] = []
    if kind == "obituary" or parsed.get("kind") == "obituary":
        for d in (parsed.get("deaths") or [])[:20]:
            if not isinstance(d, dict):
                continue
            head = _pp(d.get("subject")) or "부고"
            rel = _pp(d.get("relation"))
            lines = [f"ㆍ{head}" + (f" / {rel}" if rel else "")]
            if _pp(d.get("mourners")):
                lines.append(f"   · 상주: {_pp(d.get('mourners'))}")
            if _pp(d.get("wake")):
                lines.append(f"   · 빈소: {_pp(d.get('wake'))}")
            fb = " / ".join(x for x in (
                f"발인 {_pp(d.get('funeral'))}" if _pp(d.get("funeral")) else "",
                f"장지 {_pp(d.get('burial'))}" if _pp(d.get("burial")) else "") if x)
            if fb:
                lines.append(f"   · {fb}")
            if _pp(d.get("contact")):
                lines.append(f"   · ☎ {_pp(d.get('contact'))}")
            blocks.append("\n".join(lines))
    else:
        for p in (parsed.get("people") or [])[:20]:
            if not isinstance(p, dict):
                continue
            name = _pp(p.get("name"))
            if not name:
                continue
            head_bits = [_pp(p.get("org")), name,
                         " ".join(x for x in (_pp(p.get("position")), _pp(p.get("change"))) if x)]
            lines = ["ㆍ" + " ".join(b for b in head_bits if b).strip()]
            if _pp(p.get("exam")):
                lines.append(f"   · {_pp(p.get('exam'))}")
            career = [_pp(c) for c in (p.get("career") or []) if _pp(c)]
            if career:
                joined = " ".join(f"{i}) {c}" for i, c in enumerate(career, 1))
                lines.append(f"   · 이력: {joined}")
            if _pp(p.get("duty")):
                lines.append(f"   · 업무: {_pp(p.get('duty'))}")
            if _pp(p.get("education")):
                lines.append(f"   · 학력: {_pp(p.get('education'))}")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip()




def extract_ministry(html: str, body: str) -> str:
    """정책브리핑 기사에서 발표 부처명을 뽑는다. 못 찾으면 '정책브리핑'."""
    text = f"{html}\n{body}"
    for pat in (
        r"문의\s*[:：]\s*(?:&lt;|<)?\s*총괄\s*(?:&gt;|>)?\s*([가-힣]{2,12}(?:부|처|청|위원회|실))",
        r"문의\s*[:：]\s*([가-힣]{2,12}(?:부|처|청|위원회))",
        r"자료\s*=\s*([가-힣]{2,12}(?:부|처|청|위원회))",
        r"\(([가-힣]{2,12}(?:부|처|청))\s*제공\)",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return "정책브리핑"




# is_battery_scope / is_trade_topic 가 놓치는 흔한 제목어. 검색 카테고리가 이미
# 배터리·통상이라 이 단어가 제목에 있으면 관련 기사로 본다(범용 명사는 넣지 않는다).
_NAVER_BATTERY_TITLE_KW = ["배터리", "이차전지", "2차전지", "전기차", "EV", "충전소", "배터리팩"]


_NAVER_TRADE_TITLE_KW = ["관세", "반덤핑", "상계관세", "세이프가드", "수출규제", "수출통제",
                         "무역장벽", "무역분쟁", "무역전쟁", "통상", "FTA", "덤핑", "무역확장법"]




def _naver_item_relevant(title: str, category: str, keyword: str = "",
                         snippet: str = "") -> bool:
    """네이버가 느슨하게 매칭한 무관 기사를 거른다.

    네이버 description 은 검색어를 그대로 되풀이하는 블러브(기사 요약 아님)라
    **일반어**('배터리'·'통상' 등)로는 걸러지지 않는다. 그래서 느슨한 신호는
    **제목**에 있어야 통과시킨다. 이 필터가 없으면 "니켈" 검색의 원자재 시황,
    "국정감사" 검색의 정치 기사가 배터리·포스코 태그를 달고 대거 유입된다.

    다만 **회사명·명시적 조치명**은 블러브에 우연히 끼어들지 않는 고정밀 신호라
    제목이 아닌 **리드(스니펫)** 에서 발견돼도 인정한다 — 제목이 사업을 안 드러내는
    기사('금양, 기장공장을 데이터센터로…')가 통째로 버려지던 문제를 막는다.

    · 포스코·계열사·배터리 회사명·통상 조치명이 제목/리드에 있으면 통과 (고정밀)
    · '그룹사' 검색은 포스코가 없으면 탈락
    · 검색어가 제목에 그대로 들어 있으면 통과(정밀 매칭)
    · 그 밖에는 카테고리별 느슨한 신호가 **제목**에 있어야 통과
    """
    t = title or ""
    lead = f"{t}\n{snippet or ''}"
    # ── 고정밀 신호: 제목이 아니라 리드에 있어도 인정 ──────────────────
    if POSCO_MENTION_RE.search(lead) or detect_group_companies(lead):
        return True
    if category != "그룹사" and (battery_company_hit(lead)
                                 or _kw_hit_any(lead, TRADE_MEASURE_KW)):
        return True
    # ── 느슨한 신호: 제목에만 있어야 인정 ──────────────────────────────
    if category == "그룹사":
        return False
    if keyword and keyword in t:
        return True
    if category == "정책":
        return _kw_hit_any(t, POLICY_RELEVANCE_KW)
    if category == "통상":
        return (is_trade_topic(t, "") or _kw_hit_any(t, TRADE_MEASURE_KW)
                or _kw_hit_any(t, _NAVER_TRADE_TITLE_KW))
    # '산업'·기타 — 배터리 생태계 신호가 제목에 있어야 한다
    return is_battery_scope(t, "") or _kw_hit_any(t, _NAVER_BATTERY_TITLE_KW)




SOURCE_FETCH_WORKERS = 10   # 키워드·피드별 조회 동시 실행 수 (2026-09-14, 아래 참고)



# 네이버 무료 한도(하루 25,000회) 대응 — 키워드 전부를 매 회차 조회하면 한도를 넘는다.
# 실측(2026-09-15): 활성 키워드 118개 × 하루 288회차 = 33,984회 → 한도의 1.4배.
# 한도를 넘기면 그날 남은 시간 내내 429 로 네이버 수집이 통째로 막혀 기사를 놓친다.
# 그래서 '그룹사'(포스코 계열사명) 키워드만 매 회차 조회하고, 나머지(산업·정책·통상)는
# 아래 수만큼 나눠 교대로 조회한다. 그룹사 7 + 나머지 111/2 ≈ 63개/회차 → 하루 약 18,100회.
NAVER_ALWAYS_CATEGORY = "그룹사"


NAVER_ROTATE_SLOTS_DEFAULT = 2


# 마스터 패널 '수집 키워드 관리'에서 고를 수 있는 분류. _naver_item_relevant 가
# 이 값으로 느슨한 신호 종류를 나누므로(그룹사=고정밀만, 정책/통상=전용 키워드,
# 그 외=산업으로 취급) 목록에 없는 값은 만들지 않는다.
KEYWORD_CATEGORIES = [NAVER_ALWAYS_CATEGORY, "산업", "정책", "통상"]




def naver_rotate_slots() -> int:
    """회차 분할 수. **호출 시점에** 환경변수를 읽는다.

    모듈 상단 상수로 두면 안 된다 — .env 는 load_config() 안에서 읽는데 그건
    모듈 import 보다 나중이라, 도커(--env-file 로 진짜 환경변수)에서는 반영되고
    로컬 실행에서는 무시되는 식으로 **실행 방식에 따라 동작이 갈린다.**
    """
    return get_env_int("NAVER_ROTATE_SLOTS", NAVER_ROTATE_SLOTS_DEFAULT, 1, 12)




def select_naver_keywords(keyword_rows: Sequence[dict], cycle: int) -> list[dict]:
    """이번 회차에 조회할 키워드만 고른다. (일일 한도 대응 — 위 상수 설명 참고)

    '그룹사'는 매 회차 전부, 나머지는 cycle 을 슬롯 수로 나눈 몫의 것만 낸다.
    슬롯이 1이면 기존처럼 전부 조회한다(끄고 싶을 때 .env 로 조절).
    """
    slots = naver_rotate_slots()
    always = [r for r in keyword_rows
              if (r.get("category") if isinstance(r, dict) else "") == NAVER_ALWAYS_CATEGORY]
    rotating = [r for r in keyword_rows
                if (r.get("category") if isinstance(r, dict) else "") != NAVER_ALWAYS_CATEGORY]
    if slots <= 1 or not rotating:
        return list(keyword_rows)
    slot = cycle % slots
    return always + [r for i, r in enumerate(rotating) if i % slots == slot]




def collect_naver(http: HttpClient, cfg: Config, keyword_rows: Sequence[dict]) -> list[RawItem]:
    """NAVER API HUB 뉴스 검색. 직접 크롤링은 약관 위반이므로 하지 않는다. (PRD §7-4)

    무료 한도: 뉴스 검색 하루 25,000회 / 월 775,000회.
    키워드 29개를 1분마다 조회하면 하루 41,760회로 한도를 넘는다.
    → run_once 에서 `NAVER_INTERVAL_SEC`(기본 300초) 간격으로만 호출한다.

    키워드마다 독립적인 API 호출이라 병렬로 보낸다(2026-09-14) — 활성 키워드가
    100개를 넘어가면서 순차 처리 시 이 단계만으로 사이클 시간의 대부분(실측
    118개 키워드 기준 100초 이상)을 써버리는 게 확인됐다. HttpClient 는 이미
    커넥션 풀 16개로 병렬 사용을 전제해 뒀다(§ prefetch_articles).
    401/403(인증 실패)·429(한도 초과)는 여전히 감지하되, 병렬 실행에서는
    '연속 3회 실패 시 즉시 중단' 같은 순차 전용 최적화는 의미가 없어 빠졌다
    — 대신 전체 실패율로 설정 문제를 사후 판단한다.

    keyword_rows: [{"keyword": ..., "category": ...}, ...]
    """
    if not cfg.naver_enabled:
        return []
    headers = {"X-NCP-APIGW-API-KEY-ID": cfg.naver_client_id,
               "X-NCP-APIGW-API-KEY": cfg.naver_client_secret}
    stop_event = threading.Event()   # 429 한 번 보면 아직 안 나간 요청은 건너뛴다

    def _fetch_one(row: Any) -> tuple[list[RawItem], int, bool]:
        if stop_event.is_set():
            return [], 0, False
        keyword = row["keyword"] if isinstance(row, dict) else row
        category = row.get("category", "") if isinstance(row, dict) else ""
        try:
            resp = http.get(
                NAVER_NEWS_API,
                # 5분 간격 폴링에는 최신 30건이면 충분하다. 100건을 받으면
                # 대부분 backfill·중복이라 분석 백로그만 부풀린다.
                params={"query": keyword, "display": 30, "sort": "date"},
                headers=headers,
            )
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"{resp.status_code} 인증 실패 — .env 의 NAVER_CLIENT_ID/SECRET 이 "
                    "NAVER API HUB 의 Client ID/Secret 인지 확인하세요."
                )
            if resp.status_code == 429:
                stop_event.set()
                log.warning("Naver API 호출 한도 초과(429). 이번 실행의 네이버 수집을 중단합니다.")
                return [], 0, True
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Naver API 조회 실패 (%s): %s", keyword, exc)
            return [], 0, True

        local_items: list[RawItem] = []
        local_dropped = 0
        for entry in data.get("items", []):
            link = entry.get("originallink") or entry.get("link", "")
            if not link:
                continue
            title = html_mod.unescape(re.sub(r"<[^>]+>", "", entry.get("title", ""))).strip()
            snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", entry.get("description", ""))).strip()
            if not _naver_item_relevant(title, category, keyword, snippet):
                local_dropped += 1
                continue
            published = parse_feed_datetime(entry.get("pubDate"))
            local_items.append(RawItem(
                url_source=normalize_url(link), url_original=link, title=title,
                published_at=published, source_type="naver_api", snippet=snippet,
            ))
        return local_items, local_dropped, False

    items: list[RawItem] = []
    dropped = 0
    fail_count = 0
    if keyword_rows:
        with ThreadPoolExecutor(max_workers=min(SOURCE_FETCH_WORKERS, len(keyword_rows))) as pool:
            for its, drp, failed in pool.map(_fetch_one, keyword_rows):
                items.extend(its)
                dropped += drp
                if failed:
                    fail_count += 1
    if fail_count >= 3 and not items:
        log.error("Naver API 다수 실패(%d/%d건) — 설정을 점검하세요. 이번 실행 네이버 수집분 없음.",
                  fail_count, len(keyword_rows))
    if dropped:
        log.info("네이버 무관 기사 %d건 제외 (제목에 관련성 신호 없음)", dropped)
    return items




def collect_rss_feeds(http: HttpClient, feeds: Sequence[dict]) -> list[RawItem]:
    """언론사 자체 RSS. DB(feed_sources)로 추가하며 코드 수정이 필요 없다. (§6 확장성)

    피드마다 독립적인 요청이라 병렬로 받는다(2026-09-14) — 느린·응답 없는 피드
    하나가 나머지 전체를 붙잡지 않는다(§ collect_naver 와 같은 근거).
    """
    feedparser = _import("feedparser", "feedparser")

    def _fetch_one(feed_row: dict) -> list[RawItem]:
        url = feed_row.get("url") or ""
        if not url:
            return []
        try:
            resp = http.get(url)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("RSS 조회 실패 (%s): %s", feed_row.get("name"), exc)
            return []
        local_items: list[RawItem] = []
        for entry in feedparser.parse(resp.content).entries:
            link = entry.get("link", "")
            if not link:
                continue
            published = parse_feed_datetime(entry.get("published") or entry.get("updated"),
                                            entry.get("published_parsed") or entry.get("updated_parsed"))
            local_items.append(RawItem(
                url_source=normalize_url(link),
                url_original=link,
                title=html_mod.unescape(entry.get("title", "")).strip(),
                published_at=published,
                source_type="rss",
                press_hint=feed_row.get("name", ""),
                snippet=html_mod.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", ""))).strip(),
            ))
        return local_items

    items: list[RawItem] = []
    if feeds:
        with ThreadPoolExecutor(max_workers=min(SOURCE_FETCH_WORKERS, len(feeds))) as pool:
            for its in pool.map(_fetch_one, feeds):
                items.extend(its)
    return items




# =====================================================================
# 8. 리다이렉트 해제 및 본문 추출 (PRD F2.1-B, F4.5)
#    여기부터 HTTP 요청이 발생한다. 게이트 통과분만 도달해야 한다.
# =====================================================================

GOOGLE_HOSTS = ("news.google.com", "google.com")




def decode_html(resp: Any) -> str:
    """응답 바이트를 올바른 인코딩으로 디코드한다.

    requests 는 charset 헤더가 없으면 ISO-8859-1 로 디코드한다(RFC 2616).
    한국 언론사 상당수가 charset 헤더 없이 본문 meta 로만 utf-8/euc-kr 을 알린다.
    그대로 .text 를 쓰면 기자명·제목이 'ì¡ìë¯¼' 처럼 깨진다.
    """
    raw = resp.content or b""
    # 1) HTML meta 의 charset 을 우선한다.
    m = re.search(rb'charset=["\']?\s*([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    if m:
        enc = m.group(1).decode("ascii", "ignore").lower().replace("euc-kr", "cp949")
        try:
            return raw.decode(enc, "replace")
        except LookupError:
            pass
    # 2) 응답 헤더의 charset (requests 가 채운 encoding)
    enc = (resp.encoding or "").lower()
    if enc and enc not in ("iso-8859-1", "ascii"):
        try:
            return raw.decode(enc.replace("euc-kr", "cp949"), "replace")
        except LookupError:
            pass
    # 3) UTF-8 → CP949 순서로 시도
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")




def resolve_canonical(http: HttpClient, url: str) -> tuple[str, str]:
    """리다이렉트를 해제해 최종 원문 URL 과 HTML 을 돌려준다.

    반환: (정규화된 canonical URL, HTML 본문). 실패 시 ("", "").
    """
    try:
        # 본문 페이지는 8초 안에 응답 없으면 포기한다. 느린 사이트 하나가
        # 1회 실행 전체를 지연시키지 않게 한다. (PRD §6 실행 시간)
        resp = http.get(url, allow_redirects=True, timeout=8)
        resp.raise_for_status()
    except Exception as exc:
        log.debug("리다이렉트 해제 실패 %s: %s", url, exc)
        return "", ""

    final_url = resp.url
    text = decode_html(resp)

    host = (urlsplit(final_url).hostname or "").lower()
    if any(host.endswith(g) for g in GOOGLE_HOSTS):
        # Google News 는 HTML 안에서 JS 로 이동시키는 경우가 있다. 링크를 직접 찾는다.
        found = _extract_google_target(text)
        if found:
            try:
                resp2 = http.get(found, allow_redirects=True)
                resp2.raise_for_status()
                final_url, text = resp2.url, decode_html(resp2)
            except Exception:
                final_url = found
                text = ""
        else:
            return "", ""

    # canonical(HTML) → 리다이렉트 최종 URL → 최초 요청 URL 순으로 신뢰한다.
    # 루트만 남는 값은 기사 링크가 아니므로 건너뛴다.
    canonical = _canonical_from_html(text)
    if not canonical:
        canonical = final_url if not _is_bare_root(final_url) else url
    return normalize_url(canonical), text




def prefetch_articles(http: HttpClient, urls: Sequence[str], workers: int = 6) -> dict[str, tuple[str, str]]:
    """여러 기사의 리다이렉트 해제 + HTML 을 병렬로 받는다.

    한국 언론사 사이트는 응답이 느려 순차 처리하면 1회 실행이 수 분 걸린다.
    게이트(G2·G2.5)를 이미 통과한 항목만 여기 오므로 재조회 비용 규칙과 무관하다.
    """
    result: dict[str, tuple[str, str]] = {}
    if not urls:
        return result
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
        futures = {pool.submit(resolve_canonical, http, u): u for u in urls}
        for fut in futures:
            url = futures[fut]
            try:
                result[url] = fut.result()
            except Exception as exc:
                log.debug("prefetch 실패 %s: %s", url, exc)
                result[url] = ("", "")
    return result




def _extract_google_target(html: str) -> str:
    """Google News 중간 페이지에서 실제 기사 URL 을 찾는다."""
    for pattern in (r'data-n-au="(https?://[^"]+)"', r'<a[^>]+href="(https?://(?!news\.google)[^"]+)"'):
        m = re.search(pattern, html)
        if m:
            return html_mod.unescape(m.group(1))
    return ""




def _is_bare_root(url: str) -> bool:
    """경로 없는 도메인 루트('http://site.com/')인지. 기사 링크로는 쓸 수 없다."""
    p = urlsplit(url)
    return p.path in ("", "/") and not p.query




def _canonical_from_html(html: str) -> str:
    """<link rel="canonical"> / og:url 이 있으면 채택한다. (news-dedup-normalize)

    일부 구형 뉴스 CMS(예: techholic)는 기사 페이지에서도 canonical 을
    홈페이지 루트로 잘못 지정한다. 루트만 있는 값은 버리고 다음 후보로 넘어간다.
    """
    if not html:
        return ""
    for pattern in (
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pattern, html, re.IGNORECASE)
        if m and m.group(1).startswith("http"):
            cand = html_mod.unescape(m.group(1))
            if not _is_bare_root(cand):
                return cand
    return ""




# 순서 = 신뢰도. JSON-LD > 본문 서명('OOO 기자') > meta[name=author].
# meta author 에 매체명을 넣는 사이트가 많아 가장 뒤에 둔다.
AUTHOR_PATTERNS = [
    re.compile(r'"author"\s*:\s*{[^}]*"name"\s*:\s*"([^"]{2,20})"'),
    # '이름 기자' 뿐 아니라 '이름 인턴기자'·'이름 수습기자' 처럼 이름과 '기자' 사이에
    # 직함 수식어가 붙는 경우도 이름을 잡는다. 수식어 없이 '인턴기자'만 있으면(이름이
    # 없는 경우) 이 수식어 자체가 캡처되는데, _valid_author 의 NON_AUTHOR_WORDS 가
    # 그런 수식어를 걸러낸다.
    re.compile(r'([가-힣]{2,4})\s*(?:칼럼|시민|객원|명예|인턴|수습|선임|특약)?기자'),
    re.compile(r'<meta[^>]+(?:name|property)=["\'](?:author|article:author|dable:author)["\'][^>]+content=["\']([^"\']{2,30})["\']', re.I),
]



# 기자명 자리에 매체명이 잡히는 것을 막는다 (예: '중앙이코노미뉴스')
MEDIA_NAME_SUFFIX = (
    "뉴스", "일보", "신문", "미디어", "타임즈", "타임스", "저널", "방송", "닷컴",
    "통신", "데일리", "포스트", "투데이", "경제", "신보", "타임", "프레스", "위키",
)




def decode_unicode_escapes(text: str) -> str:
    r"""HTML/JSON-LD 에서 긁어온 문자열에 남아 있는 '\uXXXX' 리터럴을 실제 글자로 바꾼다.

    JSON-LD 의 "name":"한혜선" 을 정규식으로 캡처하면 역슬래시-u 표기가
    그대로 남아 '한혜선' 대신 '한혜선' 이 저장된다. (한글 음절 U+AC00~U+D7A3)
    """
    if not text or "\\u" not in text:
        return text
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)




def clean_author(name: str) -> str:
    """기자명 표기를 다듬는다. '영남본부=장원규' → '장원규', '송영민 기자' → '송영민'."""
    name = (name or "").strip()
    name = re.sub(r"^[가-힣A-Za-z]{2,10}\s*[=:]\s*", "", name)   # '영남본부=' 같은 소속 머리표
    name = re.sub(r"\s*(기자|팀|특파원|논설위원|편집위원)\s*$", "", name).strip()
    name = re.sub(r"[·,/]\s*(사진|영상|취재)\s*$", "", name).strip()
    return name




def fix_mojibake(text: str) -> str:
    """UTF-8 바이트를 latin-1 로 잘못 디코드한 문자열('ì¡ìë¯¼')을 되살린다.

    본문 HTML 을 잘못된 인코딩으로 읽었을 때 기자명에 깨진 글자가 섞인다.
    latin-1 재인코딩 후 utf-8 디코딩이 성공하고 한글이 나오면 그 값을 채택한다.
    """
    if not text or not re.search(r"[ÂÃÌÍ][\x80-\xBF]|[ìíîï][\x80-\xBF]", text):
        return text
    try:
        restored = text.encode("latin-1").decode("utf-8")
    except (UnicodeError, ValueError):
        return text
    return restored if _has_hangul(restored) else text




def _valid_author(raw: str, press_key: str) -> str:
    """기자명 후보를 정제·검증한다. 부적합하면 빈 문자열."""
    name = clean_author(fix_mojibake(decode_unicode_escapes((raw or "").strip())))
    if not (2 <= len(name) <= 20):
        return ""
    if re.match(r"(https?:|www\.)", name, re.I) or _looks_like_domain(name):
        return ""
    if not name.isascii() and not _has_hangul(name):
        return ""
    if name.endswith(MEDIA_NAME_SUFFIX):
        return ""
    key = normalize_chip(name)
    if press_key and (key == press_key or key in press_key or press_key in key):
        return ""
    if key in NON_AUTHOR_WORDS:
        return ""
    return name




def extract_author(html: str, body: str, press_name: str = "") -> str:
    """기자명 추출. '[언론사, 기자]' 머리표 조립에 쓴다. (PRD F4.1)

    확실하지 않으면 빈 문자열을 돌려주고 '[언론사]' 만 표기하는 편이 낫다.
    """
    press_key = normalize_chip(press_name)
    # 주석(<!-- … -->) 안에 죽은 '관련기사' 위젯 마크업을 그대로 남겨 두는 사이트가
    # 있다(실사례: PPSS — 화면에 안 보이는 주석 속 다른 기사 3건의 '권미나 기자'가
    # 이 기사의 실제 기자 '이승민 인턴기자'보다 더 많이 잡혀 최빈값으로 오채택됐다).
    # 정규식은 HTML 구조를 모르므로 주석을 먼저 지워야 한다.
    html = re.sub(r"<!--.*?-->", "", html or "", flags=re.S)

    # 1) JSON-LD author (보통 정확, 단일 매치)
    for source in (html, body):
        if source and (m := AUTHOR_PATTERNS[0].search(source)):
            if name := _valid_author(m.group(1), press_key):
                return name

    # 2) 본문 서명 'OOO 기자' — 서명은 기사에 여러 번 나오므로 최빈값을 택한다.
    #    카테고리 라벨('칼럼기자', '시민기자')은 1회만 나와 자연히 밀린다.
    #    body(추출된 본문)를 html(사이드바·관련기사 위젯 포함 전체)보다 먼저 본다 —
    #    본문에 서명이 있으면 그걸로 충분하고, 전체 html 까지 보면 이 기사와 무관한
    #    다른 기사의 서명이 최빈값을 오염시킬 수 있다.
    for source in (body, html):
        if not source:
            continue
        names = [n for n in (_valid_author(g, press_key)
                             for g in AUTHOR_PATTERNS[1].findall(source)) if n]
        if names:
            return Counter(names).most_common(1)[0][0]

    # 3) meta[name=author] (매체명을 넣는 사이트가 많아 최후 순위, 단일 매치)
    for source in (html, body):
        if source and (m := AUTHOR_PATTERNS[2].search(source)):
            if name := _valid_author(m.group(1), press_key):
                return name

    return ""




# 기자명 자리에 자주 잘못 들어오는 값들 (직함·부서·라벨)
NON_AUTHOR_WORDS = {
    "뉴시스", "연합뉴스", "뉴스1", "편집국", "온라인뉴스팀", "산업부", "경제부",
    "취재팀", "디지털뉴스팀", "특별취재팀", "무단전재", "재배포금지",
    "칼럼", "시민", "객원", "명예", "인턴", "수습", "선임", "본지", "특약",
    "한국", "일요", "주말", "사진", "영상", "그래픽", "독자", "논설", "사설",
}




def extract_thumbnail(html: str) -> str:
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html or "", re.I)
    return html_mod.unescape(m.group(1)) if m else ""




def extract_title(html: str) -> str:
    """수동 URL 등록용 — 피드 제목이 없을 때 HTML 에서 제목을 뽑는다."""
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<title[^>]*>([^<]+)</title>',
    ):
        m = re.search(pattern, html or "", re.I)
        if m:
            title = html_mod.unescape(m.group(1)).strip()
            # ' - 언론사' 같은 사이트명 꼬리 정리
            title = re.sub(r'\s*[|·\-–—]\s*[^|·\-–—]{1,25}$', '', title).strip()
            if len(title) >= 5:
                return title
    return ""




def repair_truncated_title(feed_title: str, html: str) -> str:
    """네이버 검색 API 는 제목을 '...' 로 잘라 준다. 원문 HTML 의 온전한 제목으로 되돌린다.

    - feed 제목이 '...'·'…' 로 끝나고,
    - HTML 제목이 더 길며 feed 제목의 앞부분과 시작이 같으면
    그 HTML 제목을 쓴다. 아니면 원래 제목을 그대로 둔다(오교체 방지).
    """
    t = (feed_title or "").strip()
    if not (t.endswith("...") or t.endswith("…") or t.endswith("···")):
        return feed_title
    full = extract_title(html)
    if not full or len(full) <= len(t):
        return feed_title
    head = t.rstrip(".·…").strip()[:10]
    return full if head and head[:8] in full else feed_title




PUBLISHED_META_PATTERNS = [
    r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+property=["\']og:published_time["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+name=["\'](?:pubdate|publishdate|date|sailthru\.date)["\'][^>]+content=["\']([^"\']+)["\']',
    r'"datePublished"\s*:\s*"([^"]+)"',
]




def extract_published(html: str) -> datetime | None:
    for pattern in PUBLISHED_META_PATTERNS:
        m = re.search(pattern, html or "", re.I)
        if m:
            dt = parse_dt(m.group(1))
            if dt:
                return dt
    return None




_JSON_BODY_RE = re.compile(r'"type"\s*:\s*"text"\s*,\s*"content"\s*:\s*"((?:[^"\\]|\\.)*)"')




def _json_script_body(html: str) -> str:
    """SPA(Next.js 등)가 본문을 <script> 안 JSON 으로 넣는 사이트 대응.

    news1.kr 처럼 문단을 {"type":"text","content":"..."} 배열로 담는 구조를 읽는다.
    이런 페이지는 <script> 를 걷어내는 readability·BeautifulSoup 폴백이 내비게이션만
    긁어 와서 본문이 통째로 비고, '포스코'가 본문에만 있는 기사가 무관 처리됐다.
    """
    parts: list[str] = []
    for mo in _JSON_BODY_RE.finditer(html or ""):
        try:
            txt = json.loads(f'"{mo.group(1)}"')
        except ValueError:
            continue
        txt = txt.strip()
        if txt:
            parts.append(txt)
    return "\n".join(parts)




def extract_body(html: str) -> str:
    """Readability 를 1순위로 본문을 뽑는다. 실패해도 예외를 던지지 않는다. (F4.5)"""
    if not html:
        return ""
    try:
        readability = _import("readability", "readability-lxml")
        bs4 = _import("bs4", "beautifulsoup4")
        doc = readability.Document(html)
        soup = bs4.BeautifulSoup(doc.summary(), "html.parser")
        text = soup.get_text("\n", strip=True)
        readable = text
    except SystemExit:
        raise
    except Exception as exc:
        log.debug("Readability 실패: %s", exc)
        readable = ""
    # SPA(Next.js 등)는 본문이 <script> JSON 안에 있어 readability 가 내비게이션만 긁는다.
    # JSON 문단 쪽이 더 길면 그것을 본문으로 쓴다. (문자열 포함 검사라 대부분 비용 0)
    if '"type":"text"' in (html or ""):
        json_body = _json_script_body(html)
        if len(json_body) > len(readable):
            return json_body
    if len(readable) >= 200:
        return readable
    # 폴백: 전체 문서에서 텍스트만 긁는다.
    try:
        bs4 = _import("bs4", "beautifulsoup4")
        soup = bs4.BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)
    except SystemExit:
        raise
    except Exception:
        return ""




_NOTICE_FOOTER_HINT = ("무단 전재", "사업자등록번호", "Copyright", "저작권자", "All rights reserved")




def _notice_text(html: str, body: str) -> str:
    """인사·부고 공지 텍스트를 최대한 알차게 뽑는다.

    이런 공지는 몇 줄로 짧아서 Readability 가 본문 대신 <푸터(회사 정보)>를 잡는 일이 잦다.
    body · og:description · meta description · <article> 컨테이너 텍스트 중
    푸터가 아닌 가장 긴 것을 고른다.
    """
    cands = [body or ""]
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']'):
        m = re.search(pat, html or "", re.I)
        if m:
            cands.append(html_mod.unescape(m.group(1)))
    m = re.search(r"<article[^>]*>(.*?)</article>", html or "", re.S | re.I)
    if m:
        seg = re.sub(r"<script.*?</script>", " ", m.group(1), flags=re.S | re.I)
        seg = re.sub(r"<br\s*/?>", "\n", seg, flags=re.I)
        seg = html_mod.unescape(re.sub(r"<[^>]+>", " ", seg))
        seg = re.sub(r"[ \t]+", " ", seg).strip()
        if seg:
            cands.append(seg)
    good = [c.strip() for c in cands
            if c.strip() and not any(h in c for h in _NOTICE_FOOTER_HINT)]
    return max(good, key=len) if good else (body or "")




# =====================================================================
# 9. 분류 및 중요도 스코어 (PRD F3)
# =====================================================================

def detect_group_companies(text: str) -> list[str]:
    """룰 기반 그룹사 판정. LLM 보다 우선한다. (PRD F4.2)

    '포스코퓨처엠'이 잡히면 상위 개념인 '포스코'는 붙이지 않는다. 칩 중복의 원인이 된다.
    """
    lowered = (text or "").lower()
    found: list[str] = []
    for canonical, aliases in _GROUP_ALIASES_LOWER.items():
        if canonical == "포스코":
            continue  # 마지막에 따로 판단
        if any(alias in lowered for alias in aliases):
            found.append(canonical)
    if not found and any(alias in lowered for alias in _GROUP_ALIASES_LOWER["포스코"]):
        found.append("포스코")
    return found




def normalize_group_list(groups: Iterable[str]) -> list[str]:
    """그룹사 목록 정리. 구체 계열사가 있으면 상위 개념 '포스코'는 뺀다.

    룰 기반 결과와 LLM 결과를 합칠 때 '포스코홀딩스 · 포스코퓨처엠 · 포스코' 처럼
    상위·하위가 같이 붙는다. 칩이 늘어나기만 하고 정보량은 늘지 않는다.
    """
    cleaned = dedupe_chips(g for g in groups if g in GROUP_COMPANIES)
    if len(cleaned) > 1 and "포스코" in cleaned:
        cleaned = [g for g in cleaned if g != "포스코"]
    return cleaned




def detect_categories(title: str, summary: str = "") -> list[str]:
    """카테고리 태깅. (PRD F3.1)

    제목+요약에서만 판정한다. 본문 전체를 스캔하면 부차적 언급까지 걸려
    거의 모든 기사에 카테고리가 붙어 필터가 변별력을 잃는다.
    """
    lowered = f"{title or ''}\n{summary or ''}".lower()
    title_l = (title or "").lower()
    found = [name for name, words in _CATEGORY_RULES_LOWER.items()
             if any(w in lowered for w in words)]
    # 제목 고정 카테고리(글로벌 통상환경·정부/정책): 핵심어가 제목에 없으면 부차 주제라 뺀다.
    for cat in TITLE_ANCHORED_CATEGORIES:
        if cat in found and not _kw_hit_any(title_l, _CATEGORY_RULES_LOWER[cat]):
            found.remove(cat)
    # '그룹사'는 별도의 그룹사 필터가 담당하므로 카테고리에는 넣지 않는다.
    return dedupe_chips(found)




def score_article(title: str, body: str, group_companies: Sequence[str], press_tier: int,
                  overrides: dict[str, dict] | None = None,
                  custom_rules: Sequence[dict] | None = None) -> int:
    """중요도 0~100. (PRD F3.2)

    overrides: 마스터 패널에서 고친 기본 항목 {키: {"points": int, "enabled": bool}}.
    custom_rules: 마스터 패널에서 추가한 키워드 기반 항목
                  [{"keywords": [...], "scope": "title"|"title_or_body", "points": int}, ...].
    둘 다 없으면(선택 인자) 기존 하드코딩 상수 그대로 동작한다."""
    title_l = (title or "").lower()
    full_l = f"{title} {body}".lower()
    score = 0

    def pts(key: str) -> int:
        default, _label = SCORE_RULE_DEFS[key]
        rule = (overrides or {}).get(key)
        if not rule:
            return default
        if rule.get("enabled") is False:
            return 0
        try:
            return int(rule.get("points", default))
        except (TypeError, ValueError):
            return default

    futurem = _GROUP_ALIASES_LOWER["포스코퓨처엠"]
    if any(a in title_l for a in futurem):
        score += pts("futurem_title")
    elif any(a in full_l for a in futurem):
        score += pts("futurem_body")

    if any(g != "포스코퓨처엠" for g in group_companies):
        score += pts("group")

    # 배터리 생태계 기사(포스코 미언급 허용 대상)는 그룹사 언급이 없어도
    # 전방 수요·경쟁 동향이라 최소 중요도를 준다. 예전엔 0점이라 큐에서 굶었다.
    if is_battery_scope(title_l, ""):
        score += pts("battery_title")
    elif is_battery_scope("", body[:1500]):
        score += pts("battery_body")
    if is_trade_topic(title):
        score += pts("trade")

    if any(w in full_l for w in _POLICY_KEYWORDS_LOWER):
        score += pts("policy")
    if press_tier <= 1:
        score += pts("major_press")

    # 단순 시황·주가 기사는 알림 피로를 유발하므로 감점한다.
    if any(w in title_l for w in _MARKET_ONLY_KEYWORDS_LOWER):
        score += pts("market_penalty")

    # 사용자가 마스터 패널에서 추가한 키워드 항목 — 제목(또는 제목+본문)에
    # 키워드 중 하나라도 있으면 점수를 더하거나(양수) 뺀다(음수).
    for rule in (custom_rules or []):
        kws = [str(k).lower() for k in (rule.get("keywords") or []) if str(k).strip()]
        if not kws:
            continue
        hay = title_l if rule.get("scope") == "title" else full_l
        if any(k in hay for k in kws):
            try:
                score += int(rule.get("points") or 0)
            except (TypeError, ValueError):
                pass

    return int(clamp(score, 0, 100))




# =====================================================================
# 10. LLM 분석 (PRD F4)
#     요약 · 포스코 관점 · 키워드 · 그룹사 · 감성 · SWOT 을 호출 1회로 받는다.
#     항목별로 나눠 호출하면 과금이 4배가 된다.
# =====================================================================

ANALYSIS_SYSTEM = "당신은 한국어 뉴스 분석 어시스턴트다. 주어진 기사 내용만을 근거로 분석하고 JSON 으로만 답한다."



ANALYSIS_PROMPT = """아래 기사를 분석해 JSON 하나로만 답하라.

[요약 규칙]
- summary: 정확히 3~5문장, 한국어, 각 문장 40자 내외
- 순서: (1) 무슨 일이 (2) 누가·어디서 (3) 수치·규모
- 기사에 없는 사실·배경·전망을 추가하지 않는다
- 의견·평가·추측 표현을 쓰지 않는다
- 원문 문장을 그대로 복사하지 않고 재서술한다
- 언론사명과 기자명은 summary 에 넣지 않는다 (표시할 때 따로 붙인다)
- 기사가 요약하기에 불충분하면 summary 를 ["요약불가"] 로만 채운다

[포스코 관점]
- perspective: 이 기사에 등장하는 포스코 그룹 계열사(포스코홀딩스·포스코·포스코퓨처엠·
  포스코이앤씨·포스코DX·포스코인터내셔널 등) 입장에서의 시사점 1~2문장
- 포스코퓨처엠이 아닌 다른 계열사 기사여도(예: 포스코이앤씨 수주, 포스코 노사 이슈)
  그 계열사 관점에서 반드시 작성한다 — 포스코퓨처엠 사업으로 좁혀서 판단하지 않는다
- 배터리·이차전지·양극재·음극재·전기차·ESS 등 이차전지 산업 기사는(포스코퓨처엠·
  포스코 그룹사 이름이 본문에 없어도) 포스코퓨처엠 관점(소재·공급망·경쟁 동향)의
  시사점을 반드시 작성한다 — 포스코퓨처엠이 이 그룹의 이차전지 소재 사업체다
- 계열사 이름이 등장해도 사업적 영향으로 볼 내용이 없는 단순 언급(임직원의
  소속 표기, 행사·포럼·대담 참석, 수상 등)이면 시사점 대신 그 사실 자체를
  적는다 — 절대 빈 문자열로 두지 않는다.
  예: "포스코퓨처엠 소속 홍길동 OO실장이 OO행사에 참석했다는 언급입니다."
- "검토할 필요가 있습니다" 수준의 확인 요청 톤으로 쓰고 단정하지 않는다
- 어느 계열사와도, 이차전지 산업과도 관련이 없으면 빈 문자열

[키워드]
- keywords: 기사 핵심 키워드 최대 6개, 한국어 명사구
- 서로 중복되거나 포함관계인 키워드를 넣지 않는다
- 회사명은 keywords 가 아니라 group_companies 에 넣는다

[관련 그룹사]
- group_companies: 기사 본문에 그 회사 이름이 실제로 등장하는 경우에만 넣는다
  포스코홀딩스, 포스코퓨처엠, 포스코DX, 포스코인터내셔널, 포스코이앤씨, 포스코
- 여러 소식을 묶은 브리핑·모음 기사는 본문 전체(주요 항목이 아닌 다른 항목
  포함)를 끝까지 확인한다 — 요약 문장에 담기지 않은 항목이라도 본문에 회사
  이름이 있으면 반드시 group_companies 에 넣는다
- '이차전지·배터리·공급망 뉴스니까 포스코퓨처엠' 같은 추측은 금지한다
- 회사명이 본문에 없으면 관련 산업 기사라도 빈 배열로 둔다
- 목록에 없는 회사명을 만들어내지 않는다

[감성]
- sentiment: "긍정" | "중립" | "부정" 중 하나
- 주가 호재/악재가 아니라 포스코 그룹의 대외협력 대응 필요성 기준으로 판단한다

[SWOT]
- swot: 이 기사의 사안이 포스코 그룹(철강·이차전지소재·인프라 전반)에 주는
  영향을 S/W/O/T 로 평가한다. 각 항목 score(0~100 정수)와 text(1~2줄 근거)
- 기사 본문에서 실제로 읽어낼 수 있는 함의를 적고, 최소한 한 항목은 채운다.
  신사업·수요 확대·우호적 협력 = O / 경쟁 심화·규제·공급과잉·자원 리스크 = T /
  자사 기술력·생산능력·계약·점유율 = S / 비용 부담·생산 차질·구조적 약점 = W
- 정말로 근거를 찾을 수 없는 항목만 score 0, text "해당 없음"

[출력 형식 — 이 구조를 정확히 지킨다]
{{"summary":["문장1","문장2","문장3"],"perspective":"...","keywords":["..."],
"group_companies":["..."],"sentiment":"중립",
"swot":{{"s":{{"score":0,"text":"..."}},"w":{{"score":0,"text":"..."}},
"o":{{"score":0,"text":"..."}},"t":{{"score":0,"text":"..."}}}}}}

제목: {title}
언론사: {press}
본문:
{body}
"""



MAX_BODY_CHARS = 6000  # 토큰 비용 상한. 기사 본문 대부분은 이 안에 들어간다.




@dataclass
class Analysis:
    summary_sentences: list[str] = field(default_factory=list)
    perspective: str = ""
    keywords: list[str] = field(default_factory=list)
    group_companies: list[str] = field(default_factory=list)
    sentiment: str = "중립"
    swot: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)
    ok: bool = False

    @property
    def summary_text(self) -> str:
        return " ".join(s.strip() for s in self.summary_sentences if s.strip())
