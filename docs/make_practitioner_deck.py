"""P-FM NEWS 실무자 발표자료(.pptx) — 10분 · 12장 · 16:9.

대상: 이 플랫폼을 매일 쓸 실무자(대관·전략·이차전지소재 담당).
관점: "이걸로 뭘 할 수 있나 / 어떻게 쓰나". 아키텍처는 다루지 않는다.
"""
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

# ── 팔레트 (웹 style.css 계열) ──────────────────────────────────────
NAVY      = RGBColor(0x16, 0x33, 0x7A)
NAVY_DARK = RGBColor(0x0E, 0x24, 0x56)
NAVY_SOFT = RGBColor(0xEA, 0xF0, 0xFB)
LINE      = RGBColor(0xC8, 0xD6, 0xF0)
GOLD      = RGBColor(0xF4, 0xB7, 0x40)
INK       = RGBColor(0x1B, 0x1F, 0x2A)
INK_SOFT  = RGBColor(0x5B, 0x64, 0x76)
WHITE     = RGBColor(0xFF, 0xFF, 0xFF)
PAPER     = RGBColor(0xF7, 0xF8, 0xFB)
RED       = RGBColor(0xC0, 0x39, 0x2B)
GREEN     = RGBColor(0x1E, 0x7E, 0x54)

FONT = "맑은 고딕"
W, H = Inches(13.333), Inches(7.5)
M    = Inches(0.72)
CW   = W - 2 * M

prs = Presentation()
prs.slide_width, prs.slide_height = W, H
BLANK = prs.slide_layouts[6]


def slide():
    s = prs.slides.add_slide(BLANK)
    bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, W, H)
    bg.fill.solid(); bg.fill.fore_color.rgb = WHITE
    bg.line.fill.background(); bg.shadow.inherit = False
    return s


def box(s, x, y, w, h, fill=None, line=None, lw=1.0, radius=None):
    shape = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
    sh = s.shapes.add_shape(shape, x, y, w, h)
    if fill is None:
        sh.fill.background()
    else:
        sh.fill.solid(); sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line; sh.line.width = Pt(lw)
    sh.shadow.inherit = False
    if radius:
        try:
            sh.adjustments[0] = radius
        except Exception:
            pass
    return sh


def text(s, x, y, w, h, runs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.2, wrap=True):
    tb = s.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    paras = runs if runs and isinstance(runs[0], list) else [runs]
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        for item in para:
            txt, size, bold, color = item[0], item[1], item[2], item[3]
            r = p.add_run(); r.text = txt
            r.font.name = FONT; r.font.size = Pt(size); r.font.bold = bold
            r.font.color.rgb = color
    return tb


def header(s, tag, title, sub=None):
    box(s, 0, 0, W, Inches(0.09), fill=NAVY)
    text(s, M, Inches(0.42), CW, Inches(0.3), [(tag, 11.5, True, GOLD)], spacing=1.0)
    text(s, M, Inches(0.72), CW, Inches(0.6), [(title, 26, True, NAVY_DARK)], spacing=1.05)
    y = Inches(1.36)
    if sub:
        text(s, M, y, CW, Inches(0.36), [(sub, 13, False, INK_SOFT)], spacing=1.25)
        y = Inches(1.80)
    box(s, M, y, CW, Emu(9525), fill=LINE)
    return y + Inches(0.30)


n = 0
def page(s):
    global n
    n += 1
    text(s, M, H - Inches(0.52), Inches(7), Inches(0.28),
         [("P-FM NEWS · 포스코 그룹 뉴스 인텔리전스", 9.5, False, RGBColor(0x9A, 0xA3, 0xB2))], spacing=1.0)
    text(s, W - M - Inches(1.2), H - Inches(0.52), Inches(1.2), Inches(0.28),
         [(f"{n} / 12", 10, True, INK_SOFT)], align=PP_ALIGN.RIGHT, spacing=1.0)


def bullet(s, x, y, w, items, size=13.5, gap=Inches(0.62), dot=NAVY):
    for i, it in enumerate(items):
        yy = y + gap * i
        c = s.shapes.add_shape(MSO_SHAPE.OVAL, x, yy + Inches(0.07), Inches(0.10), Inches(0.10))
        c.fill.solid(); c.fill.fore_color.rgb = dot
        c.line.fill.background(); c.shadow.inherit = False
        if isinstance(it, tuple):
            runs = [(it[0], size, True, INK), ("  " + it[1], size, False, INK_SOFT)]
        else:
            runs = [(it, size, False, INK)]
        text(s, x + Inches(0.28), yy, w - Inches(0.28), gap, runs, spacing=1.3)


def card(s, x, y, w, h, title, lines, accent=NAVY):
    box(s, x, y, w, h, fill=PAPER, line=LINE, radius=0.055)
    box(s, x, y, Inches(0.07), h, fill=accent)
    text(s, x + Inches(0.26), y + Inches(0.18), w - Inches(0.44), Inches(0.34),
         [(title, 13.5, True, NAVY_DARK)], spacing=1.05)
    text(s, x + Inches(0.26), y + Inches(0.62), w - Inches(0.44), h - Inches(0.7),
         [[(ln, 11, False, INK_SOFT)] for ln in lines], spacing=1.32)


def table(s, x, y, w, cols, rows, widths, size=11, rh=Inches(0.44)):
    tot = sum(widths); xs, acc = [], x
    for ratio in widths:
        cw = Emu(int(w * ratio / tot)); xs.append((acc, cw)); acc += cw
    box(s, x, y, w, rh, fill=NAVY)
    for (cx, cw), label in zip(xs, cols):
        text(s, cx + Inches(0.14), y + Inches(0.11), cw - Inches(0.2), rh, [(label, size, True, WHITE)], spacing=1.0)
    for ri, row in enumerate(rows):
        yy = y + rh + rh * ri
        if ri % 2 == 0:
            box(s, x, yy, w, rh, fill=PAPER)
        box(s, x, yy + rh - Emu(9525), w, Emu(9525), fill=LINE)
        for (cx, cw), cell in zip(xs, row):
            val, col, bold = cell if isinstance(cell, tuple) else (cell, INK, False)
            text(s, cx + Inches(0.14), yy + Inches(0.12), cw - Inches(0.2), rh, [(val, size, bold, col)], spacing=1.0)
    return y + rh + rh * len(rows)


def chip(s, x, y, label, fill=NAVY_SOFT, color=NAVY_DARK):
    w = Inches(0.125) * len(label) + Inches(0.30)
    box(s, x, y, w, Inches(0.34), fill=fill, radius=0.5)
    text(s, x, y + Inches(0.065), w, Inches(0.26), [(label, 10.5, True, color)], align=PP_ALIGN.CENTER, spacing=1.0)
    return x + w + Inches(0.12)


def arrow(s, x, y, w):
    a = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, x, y, w, Inches(0.22))
    a.fill.solid(); a.fill.fore_color.rgb = LINE
    a.line.fill.background(); a.shadow.inherit = False


# ════════════════════════════════════════════════════════════════════
# 1 · 표지
# ════════════════════════════════════════════════════════════════════
s = slide()
box(s, 0, 0, W, H, fill=NAVY)
box(s, 0, H - Inches(0.16), W, Inches(0.16), fill=GOLD)
text(s, M, Inches(2.15), CW, Inches(0.4), [("실무자 사용 안내", 15, True, GOLD)], spacing=1.0)
text(s, M, Inches(2.62), CW, Inches(1.5),
     [("P-FM News Intelligence", 44, True, WHITE)], spacing=1.05)
text(s, M, Inches(3.75), CW, Inches(0.9),
     [("포스코 그룹·이차전지 뉴스를 ", 17, False, RGBColor(0xC7, 0xD3, 0xEE)),
      ("실시간 수집·요약·알림", 17, True, WHITE),
      ("하는 사내 플랫폼", 17, False, RGBColor(0xC7, 0xD3, 0xEE))], spacing=1.3)
for i, t in enumerate(["매일 자동 수집", "AI 요약 + 포스코 관점", "텔레그램 알림", "대외협력 모니터링"]):
    chip(s, M + Inches(3.05) * i, Inches(5.0), t, fill=RGBColor(0x24, 0x46, 0x8F), color=WHITE)
text(s, M, Inches(6.35), CW, Inches(0.4),
     [("10분 · 화면 둘러보기 · 알림 설정 · 중요도 점수", 12, False, RGBColor(0x9A, 0xB0, 0xD8))], spacing=1.0)

# ════════════════════════════════════════════════════════════════════
# 2 · 왜 필요한가
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "01  왜 만들었나", "하루 수천 건 기사, 놓치면 안 되는 것은 소수",
           "관련 뉴스를 사람이 매일 훑는 것은 불가능하고, 정작 중요한 건은 묻힙니다.")
text(s, M, y + Inches(0.05), CW, Inches(0.4),
     [("하루에 들어오는 기사", 13, True, NAVY_DARK)], spacing=1.0)
box(s, M, y + Inches(0.5), CW, Inches(0.66), fill=PAPER, line=LINE, radius=0.06)
text(s, M + Inches(0.3), y + Inches(0.66), Inches(3.2), Inches(0.4),
     [("약 3,600건", 22, True, NAVY)], spacing=1.0)
text(s, M + Inches(3.4), y + Inches(0.72), CW - Inches(3.6), Inches(0.4),
     [("네이버·구글·언론사 RSS 에서 그룹사·이차전지·정책·통상 키워드로 수집", 11.5, False, INK_SOFT)], spacing=1.2)
arrow(s, W / 2 - Inches(0.2), y + Inches(1.42), Inches(0.4))
box(s, M, y + Inches(1.9), CW, Inches(0.66), fill=NAVY_SOFT, line=LINE, radius=0.06)
text(s, M + Inches(0.3), y + Inches(2.06), Inches(3.2), Inches(0.4),
     [("실제 알림 5~15건/일", 22, True, NAVY)], spacing=1.0)
text(s, M + Inches(3.9), y + Inches(2.12), CW - Inches(4.1), Inches(0.4),
     [("중요도 점수·관련성 필터·중복 제거를 거쳐 걸러진 것만", 11.5, False, INK_SOFT)], spacing=1.2)
bullet(s, M, y + Inches(3.05), CW, [
    ("놓치면 큰일 나는 것", "포스코 노조 파업 · 미국 반덤핑 관세 · 경쟁사 증설·투자 · IRA/CBAM 세부지침"),
    ("사람이 하던 일", "담당자가 아침마다 포털 검색 → 요약 → 관련자 공유 (1~2시간)"),
    ("이 플랫폼이 하는 일", "그 과정을 자동화하고, 중요한 건만 텔레그램으로 밀어 줌"),
], gap=Inches(0.66))
page(s)

# ════════════════════════════════════════════════════════════════════
# 3 · 전체 흐름
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "02  한눈에", "기사가 여러분에게 오기까지",
           "5분마다 자동으로 돕니다. 여러분은 마지막 두 칸만 보면 됩니다.")
steps = [
    ("수집", "네이버·구글·RSS\n키워드 검색"),
    ("걸러내기", "관련성·중복 제거\n중요도 점수"),
    ("AI 분석", "3~5문장 요약\n포스코 관점 · SWOT"),
    ("웹 게시", "포토카드로\n전체 기사·대외협력"),
    ("알림", "중요 기사만\n텔레그램 채널"),
]
bw = Inches(2.18); gap = Inches(0.30)
x0 = M
for i, (t, d) in enumerate(steps):
    x = x0 + (bw + gap) * i
    hl = i >= 3
    box(s, x, y + Inches(0.5), bw, Inches(1.7),
        fill=(NAVY if hl else PAPER), line=(None if hl else LINE), radius=0.08)
    text(s, x, y + Inches(0.74), bw, Inches(0.4),
         [(f"{i+1}. {t}", 13.5, True, (WHITE if hl else NAVY_DARK))], align=PP_ALIGN.CENTER, spacing=1.0)
    text(s, x + Inches(0.12), y + Inches(1.2), bw - Inches(0.24), Inches(0.9),
         [(d, 10, False, (RGBColor(0xC7, 0xD3, 0xEE) if hl else INK_SOFT))], align=PP_ALIGN.CENTER, spacing=1.28)
    if i < 4:
        arrow(s, x + bw + Inches(0.02), y + Inches(1.26), gap - Inches(0.04))
box(s, M, y + Inches(2.75), CW, Inches(1.55), fill=NAVY_SOFT, radius=0.06)
text(s, M + Inches(0.34), y + Inches(2.98), CW - Inches(0.68), Inches(1.2),
     [[("여러분이 보는 것  ", 12.5, True, NAVY),
       ("① 웹 화면 — 전체 기사·대외협력·주간동향 (필터·검색·즐겨찾기)", 12, False, INK)],
      [("", 6, False, INK)],
      [("                     ", 12.5, True, NAVY),
       ("② 텔레그램 채널 — 중요 기사가 요약·포스코 관점과 함께 실시간 도착", 12, False, INK)]], spacing=1.35)
page(s)

# ════════════════════════════════════════════════════════════════════
# 4 · 화면 1 — 전체 기사
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "03  화면 둘러보기 ①", "전체 기사 — 포토카드 피드",
           "수집·분석이 끝난 기사가 최신순으로. 필터로 원하는 것만 봅니다.")
card(s, M, y + Inches(0.1), Inches(5.9), Inches(2.05), "카드 하나에 담긴 것", [
    "· 제목 · 언론사 · 기자 · 발행시각 · 썸네일",
    "· AI 요약 3~5문장 (사실만, 재서술)",
    "· 포스코 관점 — 소재사업 시사점 1~2문장",
    "· SWOT 배지 (숫자 클릭 → S/W/O/T 근거)",
    "· 그룹사·카테고리·키워드 태그",
])
card(s, M + Inches(6.2), y + Inches(0.1), Inches(5.9), Inches(2.05), "필터 (왼쪽 접이식)", [
    "· 기간 — 오늘 / 7일 / 30일 / 전체",
    "· 정렬 — 최신순 / 중요도순",
    "· 그룹사 · 카테고리 · 언론사 (중복 선택)",
    "· 검색 — 제목·요약·기자·언론사",
    "· ★ 즐겨찾기 — 카드 우측 별표, 브라우저에 저장",
])
box(s, M, y + Inches(2.5), CW, Inches(1.7), fill=NAVY_SOFT, radius=0.06)
text(s, M + Inches(0.34), y + Inches(2.72), CW - Inches(0.68), Inches(1.3),
     [[("팁  ", 12.5, True, NAVY),
       ("‘중요도순’ + 카테고리 ‘배터리·이차전지’ 로 두면 소재사업 핵심만 위로 올라옵니다.", 12, False, INK)],
      [("팁  ", 12.5, True, NAVY),
       ("기사 URL 을 직접 넣어 카드로 만들 수 있습니다 (하단 ‘URL 로 포토카드 만들기’) — 슬라이드 10.", 12, False, INK)]],
     spacing=1.4)
page(s)

# ════════════════════════════════════════════════════════════════════
# 5 · 화면 2 — 대외협력
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "04  화면 둘러보기 ②", "대외협력 (/ea) — 대관·통상 모니터링",
           "헤더 ‘🏛 대외협력’ 버튼. 뉴스 피드와 분리된 전용 화면입니다.")
table(s, M, y + Inches(0.05), CW,
      ["카테고리", "무엇", "핵심 지표"],
      [
       ("입법·행정예고", "정부 입법·행정예고 (국민참여입법센터)", ("의견제출 마감 D-day", RED, True)),
       ("국회 의안", "발의 법률안 (열린국회정보)", "소관 상임위"),
       ("정책 동향", "정부/정책 기사 (korea.kr 등)", "발표 부처"),
       ("통상 환경", "KOTRA 해외시장뉴스 (반덤핑·CBAM·수출통제)", ("영향도 high/med", RED, True)),
       ("부처별 동향", "부처 정책뉴스 (산업통상부 등)", "부처"),
      ],
      widths=[1.4, 3.6, 1.7], size=11)
box(s, M, y + Inches(3.15), CW, Inches(1.1), fill=NAVY_SOFT, radius=0.06)
text(s, M + Inches(0.34), y + Inches(3.34), CW - Inches(0.68), Inches(0.8),
     [[("필터  ", 12.5, True, NAVY),
       ("카테고리 · 기관(부처·상임위·KOTRA) · 영향도 · 포스코 그룹사 · 정렬(마감임박/최신/영향도).  ", 11.5, False, INK)],
      [("            ", 12.5, True, NAVY),
       ("입법·행정예고는 마감 D-7 이내면 상단에 빨간 배너로 뜹니다.", 11.5, False, INK)]], spacing=1.4)
page(s)

# ════════════════════════════════════════════════════════════════════
# 6 · 화면 3 — 주간동향 · 즐겨찾기
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "05  화면 둘러보기 ③", "주간동향 · 즐겨찾기",
           "매주 정리된 요약과, 내가 따로 모아 둔 기사.")
card(s, M, y + Inches(0.1), Inches(5.9), Inches(3.0), "📈 주간동향", [
    "· 월요일 아침 자동 생성 (이메일로도 발송)",
    "· 한 주치 기사를 계열사·주제별로 묶음",
    "· 계열사별 주간 종합 SWOT (LLM 합성)",
    "· 정부/정책 · 글로벌 통상환경 영향 분석",
    "· 지난 레포트도 드롭다운에서 다시 조회",
    "· 수신자는 마스터 패널에서 추가 (슬라이드 9)",
], accent=GOLD)
card(s, M + Inches(6.2), y + Inches(0.1), Inches(5.9), Inches(3.0), "★ 즐겨찾기", [
    "· 카드 우측 상단 별표를 누르면 저장",
    "· 헤더 ‘★ 즐겨찾기’ 로 모아 보기",
    "· 브라우저(기기)별로 저장 — 로그인 불필요",
    "· 회의·보고 준비용 스크랩북으로 활용",
    "· 별표 해제하면 즐겨찾기 화면에서 바로 사라짐",
])
page(s)

# ════════════════════════════════════════════════════════════════════
# 7 · 텔레그램 알림
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "06  텔레그램 알림", "무엇이, 언제 오나",
           "채널 초대 링크는 헤더 ‘✈ Telegram 채널’. 봇이 개별 카드로 보냅니다.")
bullet(s, M, y + Inches(0.05), CW, [
    ("오는 기사", "중요도 ‘임계값(기본 50)’ 이상 + 관련성 통과 + 발행 6시간 이내"),
    ("한 건씩 개별 카드", "제목·언론사·요약·포스코 관점·점수·태그 — 묶음(다이제스트) 없음"),
    ("야간 규칙", "밤 11시~오전 7시엔 80점 이상 또는 예외 규칙 건만 즉시, 나머지는 아침에"),
    ("중복은 자동 제거", "같은 사건을 여러 매체가 쓰면 대표 1건만, 더 권위 있는 매체로 승격"),
], gap=Inches(0.64))
box(s, M, y + Inches(3.0), CW, Inches(1.25), fill=NAVY_SOFT, radius=0.06)
text(s, M + Inches(0.34), y + Inches(3.22), CW - Inches(0.68), Inches(0.9),
     [[("직접 등록한 기사  ", 12, True, NAVY),
       ("URL 로 만든 카드가 임계값을 넘으면 채널에도 발송됩니다 (사람이 고른 것이라 야간·억제 무시).", 11.5, False, INK)],
      [("발송이 멈췄다면  ", 12, True, NAVY),
       ("마스터 패널 ‘텔레그램 알림 사용’ 스위치 또는 봇 /stop 상태를 확인하세요.", 11.5, False, INK)]],
     spacing=1.4)
page(s)

# ════════════════════════════════════════════════════════════════════
# 8 · 중요도 점수
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "07  중요도 점수", "0~100점, 규칙으로 매깁니다",
           "제목·본문·언론사를 규칙으로 채점. 임계값·정렬·야간 규칙의 기준입니다.")
table(s, M, y + Inches(0.02), CW,
      ["무엇이 있으면", "점수"],
      [
       ("포스코퓨처엠이 제목에 / 본문에만", ("+50 / +40", NAVY, True)),
       ("다른 계열사(홀딩스·DX·인터내셔널·이앤씨 등)", ("+25", NAVY, True)),
       ("배터리 생태계(소재·셀·전기차·ESS·원료) 제목에 / 본문에만", ("+25 / +12", NAVY, True)),
       ("해외 통상 조치(IRA·CBAM·반덤핑 등) 신호", ("+15", NAVY, True)),
       ("정책 키워드(전기요금·배출권·특화단지 등)", ("+20", NAVY, True)),
       ("주요 언론사(연합·전자신문·머니투데이 등)", ("+10", NAVY, True)),
       ("단순 시황·주가 기사(목표주가·코스피·투자의견)", ("−15", RED, True)),
      ],
      widths=[5.0, 1.3], size=11, rh=Inches(0.42))
box(s, M, y + Inches(3.7), CW, Inches(0.62), fill=NAVY_SOFT, radius=0.06)
text(s, M + Inches(0.32), y + Inches(3.85), CW - Inches(0.64), Inches(0.4),
     [("예)  ", 12, True, NAVY),
      ("“포스코퓨처엠 양극재 3만톤 증설”(조선일보) = 50 + 10 = 60점  ·  임계값 50이면 → 알림 발송", 11.5, False, INK)],
     spacing=1.2)
page(s)

# ════════════════════════════════════════════════════════════════════
# 9 · 마스터 설정
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "08  마스터 설정", "내가 원하는 알림만 받기",
           "헤더 ‘🔒 마스터’ + 비밀번호. 설정은 전 사용자 공통으로 적용됩니다.")
table(s, M, y + Inches(0.02), CW,
      ["단계", "설정", "이렇게 쓰세요"],
      [
       ("①", "텔레그램 알림 사용", "전체 ON/OFF"),
       ("②", "중요도 임계값", "낮추면 많이 · 올리면 핵심만 (권장 40+)"),
       ("③", "무조건 발송 점수 / 항상 발송 키워드", "이건 밤에도·점수 무관하게 꼭 받겠다"),
       ("④", "정책브리핑 · 통상환경 켜기", "2단 키워드로 관심 주제만 골라 받기"),
       ("⑤", "주간 레포트 수신자", "이메일 추가"),
       ("⑥", "비밀번호", "운영자만"),
      ],
      widths=[0.5, 3.2, 3.4], size=11, rh=Inches(0.42))
box(s, M, y + Inches(3.35), CW, Inches(0.95), fill=NAVY_SOFT, radius=0.06)
text(s, M + Inches(0.32), y + Inches(3.52), CW - Inches(0.64), Inches(0.7),
     [[("2단 키워드 예)  ", 12, True, NAVY),
       ("① ‘전기요금’ + ② ‘산업’  →  “산업용 전기요금” 은 발송, “가정용 전기요금” 은 제외.", 11.5, False, INK)]],
     spacing=1.3)
page(s)

# ════════════════════════════════════════════════════════════════════
# 10 · URL 직접 등록
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "09  직접 추가", "빠진 기사를 직접 등록",
           "수집기가 놓친 기사를 두 가지 방법으로 카드로 만듭니다.")
card(s, M, y + Inches(0.1), Inches(5.9), Inches(2.6), "방법 A — 웹에서", [
    "1. 하단 ‘URL 로 포토카드 만들기’ 에 링크 붙여넣기",
    "2. ‘분석’ → 미리보기(제목·요약·SWOT) 확인",
    "3. ‘등록’ → 전체 기사 목록에 추가",
    "· 오래된 기사도 등록됨 · 「✍ 직접 등록」 배지 표시",
])
card(s, M + Inches(6.2), y + Inches(0.1), Inches(5.9), Inches(2.6), "방법 B — 텔레그램 봇에게", [
    "1. 봇 DM 에 기사 URL 을 그대로 전송",
    "2. 10~20초 뒤 분석된 카드가 답장으로 옴",
    "3. 임계값을 넘으면 채널에도 자동 발송",
    "· 이동 중에 빠르게 공유할 때 편리",
], accent=GOLD)
box(s, M, y + Inches(3.0), CW, Inches(0.95), fill=NAVY_SOFT, radius=0.06)
text(s, M + Inches(0.32), y + Inches(3.2), CW - Inches(0.64), Inches(0.6),
     [("등록한 기사도 요약·포스코 관점·SWOT 이 자동 생성되고, 중요도순 정렬에서 위로 올라옵니다.",
       11.5, False, INK)], spacing=1.3)
page(s)

# ════════════════════════════════════════════════════════════════════
# 11 · 데이터 출처 · 한계
# ════════════════════════════════════════════════════════════════════
s = slide()
y = header(s, "10  꼭 알아둘 것", "출처와 한계",
           "업무에 쓰기 전에 이 네 가지를 기억하세요.")
bullet(s, M, y + Inches(0.05), CW, [
    ("요약은 재서술, 원문이 원본", "본문 전문은 저장·게시하지 않습니다. 인용·보고 전 반드시 ‘원문’ 링크 확인."),
    ("AI 분석은 초안", "포스코 관점·SWOT·대외협력 영향도는 검토용 초안이며 확정 의견이 아닙니다."),
    ("시세는 참고용", "헤더 티커의 주가·환율은 지연될 수 있고 투자 권유가 아닙니다."),
    ("수집 사각지대", "네이버·RSS·정부 사이트 기반이라 100% 아닙니다 — 빠지면 URL 로 직접 등록."),
], gap=Inches(0.68))
box(s, M, y + Inches(3.15), CW, Inches(0.6), fill=NAVY_SOFT, radius=0.06)
text(s, M + Inches(0.32), y + Inches(3.30), CW - Inches(0.64), Inches(0.4),
     [("저작권은 각 언론사에 있습니다. 화면은 제목·요약·링크만 제공합니다.", 11.5, False, INK)], spacing=1.2)
page(s)

# ════════════════════════════════════════════════════════════════════
# 12 · 시작하기
# ════════════════════════════════════════════════════════════════════
s = slide()
box(s, 0, 0, W, H, fill=NAVY)
box(s, 0, H - Inches(0.16), W, Inches(0.16), fill=GOLD)
text(s, M, Inches(1.5), CW, Inches(0.4), [("11  시작하기", 15, True, GOLD)], spacing=1.0)
text(s, M, Inches(2.0), CW, Inches(0.8), [("3분이면 됩니다", 34, True, WHITE)], spacing=1.05)
items = [
    ("1. 웹 접속", "사내망에서 서버 주소로 접속 — 전체 기사가 바로 보입니다"),
    ("2. 텔레그램 채널 참여", "헤더 ‘✈ Telegram 채널’ 초대 링크 → 채널 입장 → 알림 수신 시작"),
    ("3. 즐겨찾기·필터 익히기", "중요도순 정렬 + 관심 카테고리로 나만의 화면 구성"),
    ("4. (운영자) 마스터 설정", "임계값·키워드·주간 레포트 수신자 조정"),
]
for i, (t, d) in enumerate(items):
    yy = Inches(3.1) + Inches(0.82) * i
    text(s, M, yy, CW, Inches(0.4), [(t, 15, True, WHITE)], spacing=1.0)
    text(s, M + Inches(3.3), yy + Inches(0.03), CW - Inches(3.3), Inches(0.4),
         [(d, 12, False, RGBColor(0xC7, 0xD3, 0xEE))], spacing=1.2)
text(s, M, Inches(6.5), CW, Inches(0.4),
     [("문의 · 개선 요청 — 대외협력팀  ·  기사 누락 신고는 URL 직접 등록으로", 11.5, False, RGBColor(0x9A, 0xB0, 0xD8))], spacing=1.0)

# ════════════════════════════════════════════════════════════════════
import sys, os
out = sys.argv[1] if len(sys.argv) > 1 else "P-FM_NEWS_실무자_발표.pptx"
prs.save(out)
print(f"생성 완료: {out}  ({len(prs.slides._sldIdLst)}장, {os.path.getsize(out)/1024:.0f} KB)")
