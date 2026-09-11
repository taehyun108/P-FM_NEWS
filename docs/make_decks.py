"""P-FM NEWS 발표자료 생성 — 실무자용 · 임원용 (각 10분).

26년 포스코퓨처엠 AI활용전문가 과정 공식 템플릿을 그대로 베이스로 쓴다.
템플릿의 마스터·배경·폰트를 상속받으므로 디자인이 어긋나지 않는다.

    python docs/make_decks.py [템플릿.pptx]

템플릿 경로를 안 주면 Downloads 에서 가장 큰 .pptx 를 찾는다.
"""
from __future__ import annotations

import copy
import glob
import os
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

# ── 템플릿 테마에서 가져온 색 (theme1.xml) ──────────────────────────
NAVY = RGBColor(0x0E, 0x28, 0x41)   # dk2 — 제목·강조
TEAL = RGBColor(0x15, 0x60, 0x82)   # accent1 — 보조 강조
ORANGE = RGBColor(0xE9, 0x71, 0x32)  # accent2 — 경고·문제
GREEN = RGBColor(0x19, 0x6B, 0x24)  # accent3 — 성과
SOFT = RGBColor(0xDC, 0xEA, 0xF7)   # 연파랑 박스
SOFT2 = RGBColor(0xED, 0xF3, 0xFA)
INK = RGBColor(0x25, 0x30, 0x3F)
MUTED = RGBColor(0x6B, 0x7A, 0x90)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LINE = RGBColor(0xC8, 0xD6, 0xE8)

# ── 레이아웃 인덱스 (템플릿 마스터 0) ──────────────────────────────
LAY_TITLE = 0    # 제목 슬라이드 — 그라데이션
LAY_GRAD = 1     # 제목만 — 그라데이션 + 상단 과정명
LAY_PLAIN = 3    # 5_제목만 — 밝은 배경 + 상단 과정명 + 큐브 아이콘
LAY_BLANK = 5    # 빈 화면 — 그라데이션 (섹션 표지·마무리)

# ── 26.67 x 15 inch 캔버스 기준 그리드 ─────────────────────────────
L = 2.0          # 좌측 기준선
W = 22.6         # 본문 폭
TITLE_Y = 2.45
BODY_TOP = 4.35
BODY_BOT = 13.6


# ── 헬퍼 ────────────────────────────────────────────────────────────
def wipe_slides(prs: Presentation) -> None:
    """템플릿의 예시 슬라이드를 모두 지운다."""
    ids = prs.slides._sldIdLst
    for sld in list(ids):
        prs.part.drop_rel(sld.rId)
        ids.remove(sld)


def drop_empty_placeholders(slide, keep=()) -> None:
    """텍스트를 넣지 않은 개체 틀을 제거한다 (편집 화면의 안내문 방지)."""
    for ph in list(slide.placeholders):
        if ph.placeholder_format.idx in keep:
            continue
        if not (ph.has_text_frame and ph.text_frame.text.strip()):
            ph._element.getparent().remove(ph._element)


def txt(slide, x, y, w, h, text, size=20, bold=False, color=INK,
        align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, line=1.25, space_after=0):
    """텍스트 상자 하나. text 에 개행이 있으면 문단으로 나뉜다."""
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, part in enumerate(str(text).split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = line
        p.space_after = Pt(space_after)
        run = p.add_run()
        run.text = part
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        run.font.name = "맑은 고딕"
    return box


def rounded(slide, x, y, w, h, fill=SOFT, outline=None, radius=0.06):
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                Inches(x), Inches(y), Inches(w), Inches(h))
    sh.adjustments[0] = radius
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill
    if outline is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = outline
        sh.line.width = Pt(1)
    sh.shadow.inherit = False
    sh.text_frame.text = ""
    return sh


def bar(slide, x, y, w, h, fill):
    sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                Inches(x), Inches(y), Inches(w), Inches(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill
    sh.line.fill.background()
    sh.shadow.inherit = False
    return sh


def slide_plain(prs, title, kicker="", timing=""):
    """밝은 배경 본문 슬라이드. 제목 + (부제) + (발표 시간 표시)."""
    s = prs.slides.add_slide(prs.slide_layouts[LAY_PLAIN])
    s.shapes.title.text_frame.text = ""
    tp = s.shapes.title.text_frame.paragraphs[0]
    r = tp.add_run(); r.text = title
    r.font.size = Pt(40); r.font.bold = True; r.font.color.rgb = NAVY
    r.font.name = "맑은 고딕"
    drop_empty_placeholders(s, keep=(0,))
    if kicker:
        txt(s, L + 0.85, TITLE_Y + 1.18, W - 3.5, 0.6, kicker, size=18, color=MUTED)
    if timing:
        txt(s, 21.0, 13.9, 3.6, 0.45, timing, size=14, bold=True, color=LINE,
            align=PP_ALIGN.RIGHT)
    return s


def slide_section(prs, num, title, sub):
    """섹션 표지 — 그라데이션 배경."""
    s = prs.slides.add_slide(prs.slide_layouts[LAY_BLANK])
    drop_empty_placeholders(s)
    txt(s, L, 5.6, 6.0, 1.4, num, size=64, bold=True, color=TEAL)
    txt(s, L, 7.0, W, 1.7, title, size=54, bold=True, color=NAVY)
    txt(s, L, 8.95, W, 1.2, sub, size=23, color=INK)
    bar(s, L, 6.85, 2.2, 0.06, ORANGE)
    return s


def kpi_row(s, items, y=None, h=2.5, top_gap=0.0):
    """숫자 카드 가로 배열. items = [(값, 라벨, 보조설명), ...]"""
    y = (BODY_TOP + top_gap) if y is None else y
    n = len(items)
    gap = 0.42
    w = (W - gap * (n - 1)) / n
    for i, (val, label, note) in enumerate(items):
        x = L + i * (w + gap)
        rounded(s, x, y, w, h, fill=SOFT2)
        bar(s, x, y, w, 0.09, TEAL)
        txt(s, x + 0.6, y + 0.5, w - 1.2, 1.0, val, size=42, bold=True, color=NAVY)
        txt(s, x + 0.6, y + 1.5, w - 1.2, 0.5, label, size=17, bold=True, color=INK)
        if note:
            txt(s, x + 0.6, y + 1.98, w - 1.2, 0.5, note, size=13.5, color=MUTED)
    return y + h


def check_rows(s, rows, y=None, h=1.35, gap=0.3, fill=SOFT):
    """템플릿의 '체크 + 연파랑 박스' 리스트. rows = [(제목, 설명), ...]

    제목·설명 모두 상자 안에서 세로 가운데 정렬한다 — 위로 붙으면 상자가 비어 보인다.
    """
    y = BODY_TOP if y is None else y
    for head, body in rows:
        rounded(s, L, y, W, h, fill=fill)
        txt(s, L + 0.6, y, 0.7, h, "✔", size=24, bold=True, color=TEAL,
            anchor=MSO_ANCHOR.MIDDLE)
        txt(s, L + 1.55, y + 0.1, 7.0, h - 0.2, head, size=20.5, bold=True, color=NAVY,
            anchor=MSO_ANCHOR.MIDDLE, line=1.15)
        txt(s, L + 9.0, y + 0.1, W - 9.7, h - 0.2, body, size=17.5, color=INK,
            anchor=MSO_ANCHOR.MIDDLE, line=1.28)
        y += h + gap
    return y


def flow_steps(s, steps, y=None, h=1.9):
    """번호 흐름 — 좌측 원 번호 + 제목 + 설명. 원들을 세로선 하나로 잇는다."""
    y = BODY_TOP if y is None else y
    n = len(steps)
    # 세로 연결선은 첫 원 중심 ~ 마지막 원 중심까지 하나로 긋고, 원을 그 위에 얹는다.
    bar(s, L + 0.40, y + h / 2, 0.045, h * (n - 1), LINE)
    for i, (head, body) in enumerate(steps, 1):
        cy = y + h / 2
        circ = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(L), Inches(cy - 0.42),
                                  Inches(0.84), Inches(0.84))
        circ.fill.solid(); circ.fill.fore_color.rgb = NAVY
        circ.line.color.rgb = WHITE; circ.line.width = Pt(2.5)
        circ.shadow.inherit = False
        tf = circ.text_frame; tf.text = ""
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
        r = p.add_run(); r.text = f"{i:02d}"
        r.font.size = Pt(17); r.font.bold = True; r.font.color.rgb = WHITE
        r.font.name = "맑은 고딕"
        txt(s, L + 1.35, y + 0.16, 7.0, 0.68, head, size=21.5, bold=True, color=NAVY)
        txt(s, L + 1.35, y + 0.88, W - 1.7, h - 1.0, body, size=17, color=INK, line=1.2)
        y += h
    return y


def two_col(s, left, right, y=None, h=7.6):
    """좌/우 대비 패널. left·right = (제목, 색, [줄, ...])

    줄 간격은 상자 높이에 맞춰 나눠 아래가 비어 보이지 않게 한다.
    """
    y = BODY_TOP if y is None else y
    cw = (W - 1.0) / 2
    for i, (head, color, lines) in enumerate((left, right)):
        x = L + i * (cw + 1.0)
        rounded(s, x, y, cw, h, fill=SOFT2 if i else RGBColor(0xFA, 0xF0, 0xE9))
        bar(s, x, y, cw, 0.12, color)
        txt(s, x + 0.75, y + 0.5, cw - 1.5, 0.85, head, size=25, bold=True, color=color)
        top = y + 1.75
        step = (h - 2.25) / max(len(lines), 1)
        for j, ln in enumerate(lines):
            ty = top + j * step
            bar(s, x + 0.8, ty + 0.26, 0.16, 0.16, color)
            txt(s, x + 1.25, ty, cw - 2.0, step - 0.1, ln, size=17.5, color=INK, line=1.28)
    return y + h


def closing(prs, line1, line2):
    s = prs.slides.add_slide(prs.slide_layouts[LAY_BLANK])
    drop_empty_placeholders(s)
    txt(s, L, 6.3, W, 1.8, line1, size=58, bold=True, color=NAVY)
    txt(s, L, 8.45, W, 1.0, line2, size=24, color=INK)
    bar(s, L, 8.1, 2.6, 0.06, ORANGE)
    return s


def title_slide(prs, kicker, title, sub, who):
    s = prs.slides.add_slide(prs.slide_layouts[LAY_TITLE])
    drop_empty_placeholders(s)
    txt(s, L, 4.5, W, 0.7, kicker, size=20, bold=True, color=TEAL)
    txt(s, L, 5.4, W, 2.8, title, size=66, bold=True, color=NAVY, line=1.12)
    txt(s, L, 8.6, W, 1.0, sub, size=24, color=INK)
    bar(s, L, 10.1, 3.0, 0.07, ORANGE)
    # 발표자 정보는 그라데이션 위에 얹히므로 회색이 아니라 진한 색으로 둔다.
    txt(s, L, 10.6, W, 1.4, who, size=19, bold=True, color=NAVY, line=1.4)
    return s


def agenda(prs, items):
    """목차 — 그라데이션 배경 + 좌측 세로선 + 번호."""
    s = prs.slides.add_slide(prs.slide_layouts[LAY_GRAD])
    s.shapes.title.text_frame.text = ""
    p = s.shapes.title.text_frame.paragraphs[0]
    r = p.add_run(); r.text = "발표 순서"
    r.font.size = Pt(40); r.font.bold = True; r.font.color.rgb = NAVY
    r.font.name = "맑은 고딕"
    drop_empty_placeholders(s, keep=(0,))
    y = BODY_TOP + 0.2
    step = (BODY_BOT - y) / max(len(items), 1)
    bar(s, L + 0.42, y + 0.35, 0.03, step * (len(items) - 1) + 0.2, LINE)
    for i, (name, note, mins) in enumerate(items, 1):
        cy = y + (i - 1) * step
        dot = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(L + 0.28), Inches(cy + 0.3),
                                 Inches(0.31), Inches(0.31))
        dot.fill.solid(); dot.fill.fore_color.rgb = TEAL
        dot.line.fill.background(); dot.shadow.inherit = False
        txt(s, L + 1.05, cy + 0.05, 1.9, 0.9, f"{i:02d}", size=34, bold=True, color=TEAL)
        txt(s, L + 3.0, cy + 0.12, 8.0, 0.75, name, size=25, bold=True, color=NAVY)
        # 그라데이션 배경 위라 회색은 묻힌다 — 잉크색으로 둔다.
        txt(s, L + 3.0, cy + 0.95, 15.0, 0.6, note, size=17, color=INK)
        txt(s, 20.6, cy + 0.18, 4.0, 0.6, mins, size=19, bold=True, color=TEAL,
            align=PP_ALIGN.RIGHT)
    return s


# ── 실측값 (2026-09-09, 운영 7일차) ────────────────────────────────
F = dict(
    days="7일", cycles="2,454", collected="2,607", active="2,148",
    analyzed="2,405", swot="2,267", sent="415", skipped="1,659",
    press="392", keywords="118", ea="71", per_day="367",
    sent_per_day="58", fail="0", db="19.8MB",
    tok_in="1,842", tok_out="803", tok_total="6.4M", cycle_sec="37초",
)


# ══════════════════════════════════════════════════════════════════
# 실무자용 — "무엇을, 어떻게 쓰나"
# ══════════════════════════════════════════════════════════════════
def build_practitioner(template: str, out: str) -> None:
    prs = Presentation(template)
    wipe_slides(prs)

    # 월간 건수·부담 시간은 일평균(F['per_day'])×30일 환산 추정치. 실측값은 F(7일)에서 그대로 가져온다.
    per_month = int(F["per_day"].replace(",", "")) * 30            # 11,010건
    month_hours = round(per_month * 5 / 60)                        # 약 918시간 (건당 5분 가정)
    month_fte = round(month_hours / 173, 1)                        # 월 소정근로 173시간 기준 환산

    title_slide(
        prs, "26년 포스코퓨처엠 AI활용전문가 과정 · 사무계",
        "기사를 확인하는 일에서\n골든타임을 지키는 일로",
        "포스코 그룹 뉴스 수집·요약·알림 자동화 — 실무자용 사용 안내",
        "대외협력  |  김태현\n2026. 09.")

    agenda(prs, [
        ("왜 필요한가", "기사량 · 골든타임 · 업무 범위 — 세 가지 문제", "3분"),
        ("무엇을 만들었나", "5분마다 자동으로 도는 파이프라인", "1분"),
        ("어떻게 쓰나", "웹 아카이브 · 대외협력 · 텔레그램 세 화면", "4분"),
        ("무엇이 달라졌나", "7일 무인 운영 실측", "2분"),
    ])

    # ── 01 문제 ───────────────────────────────────────────────────
    slide_section(prs, "01", "왜 필요한가", "기사량 · 골든타임 · 업무 범위 — 세 가지 문제")

    s = slide_plain(prs, "문제① — 기사량이 감당되지 않는다",
                    "한 사람이 다 읽을 수 있는 양이 아니다", "0:00 – 1:00")
    kpi_row(s, [(f"{F['per_day']}건", "하루 수집", "최근 7일 실측 평균"),
                (f"{F['collected']}건", "1주일 누적", "7일 실측"),
                (f"{per_month:,}건", "한 달 환산", "일평균 × 30일"),
                (f"{month_hours:,}시간", "월간 확인 부담", "건당 5분 가정 시")],
            y=BODY_TOP + 0.1, h=2.5)
    check_rows(s, [
        ("정보 과부하", "기사가 넘쳐날수록 무엇을 먼저 봐야 할지 판단하기 어렵고, 확인 기준이 사람마다 달라 일관성을 지키기 힘들다"),
        ("시간이 없다", f"이 많은 양을 매달 건당 5분씩만 훑어도 약 {month_hours:,}시간(정규직 약 {month_fte}명 분) — 판단·대응에 쓸 시간이 남지 않는다"),
    ], y=BODY_TOP + 2.95, h=1.55, gap=0.35)

    s = slide_plain(prs, "문제② — 골든타임을 놓치면 안 된다",
                    "확인이 늦어질수록, 시장은 확인되지 않은 내용만으로 반응한다", "1:00 – 2:00")
    check_rows(s, [
        ("확인되지 않은 정보도 섞여 들어온다", "오보·왜곡 보도, 노조·파업설, 경쟁사발 루머성 기사도 수백 건 속에 함께 수집된다"),
        ("골든타임이 있다", "정정 요청·해명자료·IR 대응은 초기 대응이 늦을수록 효과가 떨어지고, 늦으면 주가 등 시장 반응에 그대로 영향을 준다"),
        ("사람이 놓치면 대응도 늦어진다", "수백 건 중 이 한 건을 담당자가 직접 찾아내야 하는 구조라면, 대응 시작 자체가 늦어진다"),
    ], y=BODY_TOP + 0.3, h=1.7, gap=0.35)
    kpi_row(s, [("5분 이내", "텔레그램 도달 목표", "기사 발행 후 P90 기준")],
            y=BODY_TOP + 6.35, h=1.9)

    s = slide_plain(prs, "문제③ — 봐야 할 곳이 계속 늘어난다",
                    "배터리 · 부처 · 환경 — 대외협력의 업무 범위", "2:00 – 3:00")
    check_rows(s, [
        ("이차전지·배터리", "소재·셀·전고체·나트륨 배터리 등 기술 트렌드가 몇 달 단위로 바뀐다"),
        ("정부 부처 동향", "산업통상부·환경부 등에서 입법예고·행정예고·정책 발표가 수시로 나온다"),
        ("환경·통상 규제", "탄소중립, CBAM, IRA, 반덤핑 관세 등 해외 규제 변화가 사업에 직접 영향을 준다"),
        ("전부 한 사람 몫", "이 세 영역을 동시에, 매일 놓치지 않고 확인해야 하는 게 대외협력 업무의 특성이다"),
    ], y=BODY_TOP + 0.3, h=1.7, gap=0.35)

    # ── 02 해결 ───────────────────────────────────────────────────
    slide_section(prs, "02", "무엇을 만들었나",
                  "사람은 '확인'이 아니라 '판단'에만 시간을 쓴다")
    s = slide_plain(prs, "기사 하나가 거치는 길",
                    "5분마다 자동으로 도는 파이프라인 — 사람 손이 닿는 곳은 마지막 판단뿐",
                    "3:00 – 4:00")
    flow_steps(s, [
        ("수집", f"구글뉴스 RSS · 네이버 검색에서 키워드 {F['keywords']}개로 최신 기사 목록을 받는다"),
        ("게이트", "이미 본 기사 · 오래된 기사 · 중복 기사 · 포스코와 무관한 기사를 여기서 버린다"),
        ("AI 분석 1회", "요약 · 포스코 관점 · 키워드 · 감성 · SWOT 을 한 번에 뽑는다 (기사당 1회)"),
        ("중요도 채점", "퓨처엠 제목 언급 +50, 계열사 +25, 정책·통상 가점, 단순 시황 감점 → 0~100"),
        ("저장 · 웹 게시", "웹에는 전부 올라간다. 원문 본문은 요약용으로만 임시 보관 후 삭제"),
        ("텔레그램 알림", "임계값 이상 + 6시간 이내 기사만, 발행 5분 이내(P90)로 개별 카드 발송"),
    ], y=BODY_TOP + 0.05, h=1.45)

    # ── 03 사용법 ─────────────────────────────────────────────────
    slide_section(prs, "03", "어떻게 쓰나",
                  "화면 셋 — 웹 아카이브 · 대외협력 · 텔레그램")
    s = slide_plain(prs, "① 웹 아카이브 — 훑고, 좁히고, 찾는다",
                    "포토카드 그리드 + 다중 선택 필터", "4:00 – 5:20")
    check_rows(s, [
        ("카드 한 장에 판단 재료가 다 있다",
         "[언론사, 기자] 요약 3~5줄 + 포스코 관점 + 감성·중요도 + 키워드 칩 + SWOT 배지"),
        ("필터는 같은 줄 안 OR, 다른 줄 사이 AND",
         "그룹사(퓨처엠+홀딩스) × 카테고리(정책) → 둘 다 만족하는 기사만"),
        ("SWOT 배지에 마우스를 올리면 근거가 나온다",
         "강점·약점·기회·위협 각 1~2줄. 근거가 없으면 배지 자체를 띄우지 않는다"),
        ("URL 을 붙여 넣으면 그 기사도 카드가 된다",
         "같은 방식으로 분석해 미리보기 → 확인 후 등록. 등록 즉시 텔레그램으로도 나간다"),
    ], y=BODY_TOP + 0.35, h=1.72, gap=0.4)

    s = slide_plain(prs, "② 대외협력 — 마감이 있는 일부터",
                    "입법·행정예고 / 국회 의안 / 정책 동향 / 통상 환경 / 부처별 동향",
                    "5:20 – 6:40")
    kpi_row(s, [("D-day", "마감 임박순 정렬", "빨강 = 3일 이내"),
                ("영향도", "높음 · 보통 · 낮음", "근거 조문 없으면 '없음' 강제"),
                ("그룹사", "규제 대상 사업으로 판정", "원문에 회사명이 없어도"),
                ("대응 초안", "AI가 검토 포인트 제시", "법률 자문 아님")],
            y=BODY_TOP + 0.3, h=2.8)
    check_rows(s, [
        ("의견 제출 기한을 놓치지 않는다",
         "D-7 이내 항목은 화면 상단에 경고 배너로 따로 모아 보여 준다"),
        ("포스코가 안 나와도 잡는다",
         f"'철강·이차전지 규제'는 회사명이 없다. 규칙으로 그룹사를 매칭한다 (현재 {F['ea']}건 추적)"),
    ], y=BODY_TOP + 3.5, h=1.7, gap=0.4)

    s = slide_plain(prs, "③ 텔레그램 — 골든타임을 지킨다",
                    "전부 보내면 아무도 안 본다. 그래서 게이트가 있다", "6:40 – 8:00")
    kpi_row(s, [(F["collected"], "7일간 수집", "일 평균 " + F["per_day"] + "건"),
                (F["sent"], "실제 발송", "일 평균 " + F["sent_per_day"] + "건"),
                (F["skipped"], "게이트에서 제외", "중요도 미달·지연분"),
                ("5분", "발행 → 도달 목표", "P90 기준")],
            y=BODY_TOP + 0.3, h=2.8)
    check_rows(s, [
        ("왜 왔는지가 로그에 남는다",
         "'항상발송 키워드 포스코퓨처엠' · '중요도 55 ≥ 임계값 50' 처럼 이유를 그대로 기록"),
        ("놓친 기사는 직접 등록",
         "웹 하단 URL 등록 또는 텔레그램 봇 DM 으로 링크 전송 — 등록 즉시 채널에도 발송된다"),
        ("기준은 마스터 패널에서 바꾼다",
         "임계값 슬라이더 · 항상 발송 키워드 · 주제별 2단 키워드 필터 — 코드 수정 없이"),
    ], y=BODY_TOP + 3.5, h=1.62, gap=0.36)

    # ── 04 결과 ───────────────────────────────────────────────────
    slide_section(prs, "04", "무엇이 달라졌나", "7일 무인 운영 실측")
    s = slide_plain(prs, "7일간의 기록", "사람이 개입한 시간 0분", "8:00 – 9:30")
    kpi_row(s, [(F["cycles"], "수집 사이클", "5분 주기 · 무중단"),
                (F["analyzed"], "AI 분석 완료", "요약+관점+키워드+감성+SWOT"),
                (F["sent"], "텔레그램 발송", f"실패 {F['fail']}건"),
                (F["press"], "모니터링 언론사", f"키워드 {F['keywords']}개")],
            y=BODY_TOP + 0.25, h=2.8)
    two_col(s,
            ("이전", ORANGE, [
                "사람이 포털을 돌며 확인 — 주기가 제각각",
                "같은 기사 10~20건을 손으로 걸러 냄",
                "오보·이슈가 나와도 발견이 늦음",
                "요약은 읽어 보고 직접 정리",
            ]),
            ("지금", GREEN, [
                "5분마다 자동 수집 — 놓침 없음",
                "중복 4단계 판정으로 대표 1건만 남김",
                "중요 기사는 5분 이내(P90) 텔레그램 알림",
                "요약·포스코 관점·SWOT 자동 생성",
            ]),
            y=BODY_TOP + 3.45, h=5.6)

    closing(prs, "감사합니다.",
            "확인은 시스템이, 판단은 사람이.")
    prs.save(out)
    print(f"  ✓ 실무자용 {len(prs.slides._sldIdLst)}장 → {out}")


# ══════════════════════════════════════════════════════════════════
# 임원용 — "왜 했고, 무엇이 남았나"
# ══════════════════════════════════════════════════════════════════
def build_executive(template: str, out: str) -> None:
    prs = Presentation(template)
    wipe_slides(prs)

    title_slide(
        prs, "26년 포스코퓨처엠 AI활용전문가 과정 · 사무계",
        "대외 이슈를\n놓치지 않는 구조",
        "포스코 그룹 뉴스 인텔리전스 — 7일 무인 운영 결과 보고",
        "대외협력  |  김태현\n2026. 09.")

    agenda(prs, [
        ("왜 했는가", "인지 지연이 만드는 비용", "2분"),
        ("무엇을 만들었나", "한 장 요약", "2분"),
        ("무엇이 남았나", "7일 실측 성과", "3분"),
        ("어떻게 지속하는가", "비용 · 리스크 · 확산", "3분"),
    ])

    # ── 01 배경 ───────────────────────────────────────────────────
    slide_section(prs, "01", "왜 했는가", "대외 이슈 인지가 늦으면 생기는 일")
    s = slide_plain(prs, "인지 지연이 곧 대응 지연이다",
                    "대외협력 업무의 병목은 '판단'이 아니라 '확인'이었다", "0:00 – 2:00")
    two_col(s,
            ("문제", ORANGE, [
                "확인 주기가 담당자마다 달라 놓치는 기사가 생긴다",
                "규제·지역 이슈는 초동 대응 시점이 결과를 가른다",
                "통신사 전재로 같은 기사가 10~20건씩 중복 유입된다",
                "읽을 기사와 아닌 기사를 가르는 데 시간이 든다",
            ]),
            ("전제", TEAL, [
                "확인은 규칙으로 자동화할 수 있다",
                "판단은 사람이 해야 한다 — 자동화 대상이 아니다",
                "따라서 확인을 걷어내면 판단할 시간이 늘어난다",
                "단, 알림이 많으면 아무도 안 본다 (게이팅이 생존 조건)",
            ]),
            y=BODY_TOP + 0.35, h=7.3)

    # ── 02 개요 ───────────────────────────────────────────────────
    slide_section(prs, "02", "무엇을 만들었나", "수집 → 선별 → 분석 → 전달, 무인 파이프라인")
    s = slide_plain(prs, "한 장 요약",
                    "파이썬 단일 파일 + SQLite. 외부 의존은 뉴스 소스와 AI API 뿐",
                    "2:00 – 4:00")
    kpi_row(s, [("5분", "수집 주기", "무중단 자동 실행"),
                ("7단계", "선별 게이트", "중복·무관 기사 제거"),
                ("1회", "기사당 AI 호출", "요약+관점+SWOT 통합"),
                ("3개", "전달 채널", "웹 · 텔레그램 · 주간 이메일")],
            y=BODY_TOP + 0.3, h=2.8)
    check_rows(s, [
        ("웹 아카이브", "포토카드 그리드 + 다중 선택 필터. 과거 기사가 사라지지 않는다"),
        ("대외협력 화면", "입법·행정예고와 국회 의안을 마감 임박순으로. AI 영향분석 초안 포함"),
        ("텔레그램 채널", "중요도 임계값을 넘은 기사만 개별 카드로. 보낸 이유가 로그에 남는다"),
        ("주간동향 레포트", "월요일 07시, 그룹사 5 + 정책·통상 2 섹션 종합 이메일"),
    ], y=BODY_TOP + 3.5, h=1.42, gap=0.32)

    # ── 03 성과 ───────────────────────────────────────────────────
    slide_section(prs, "03", "무엇이 남았나", "7일 무인 운영 실측")
    s = slide_plain(prs, "실측 성과", "사람이 개입한 시간 0분", "4:00 – 5:30")
    kpi_row(s, [(F["cycles"], "수집 사이클", "5분 주기 · 장애 0회"),
                (F["collected"], "수집 기사", f"일 평균 {F['per_day']}건"),
                (F["analyzed"], "AI 분석 완료", f"SWOT {F['swot']}건 포함"),
                (F["sent"], "텔레그램 발송", f"발송 실패 {F['fail']}건")],
            y=BODY_TOP + 0.3, h=2.8)
    check_rows(s, [
        ("놓침이 사라졌다",
         "수집 판정 권위를 DB 전체 기간으로 두어, 뒤늦게 색인된 기사도 빠짐없이 들어온다"),
        ("알림 피로가 없다",
         f"수집 {F['collected']}건 중 실제 발송은 {F['sent']}건(16%). 나머지는 웹에서만 본다"),
        ("판단 재료가 카드 한 장에 모인다",
         "요약 · 포스코 관점 · 감성 · 중요도 · 키워드 · SWOT 근거까지 한 화면"),
    ], y=BODY_TOP + 3.5, h=1.62, gap=0.36)

    s = slide_plain(prs, "설계에서 지킨 원칙", "'되게 만드는 것'과 '계속 되게 하는 것'은 다르다",
                    "5:30 – 7:00")
    check_rows(s, [
        ("비용은 구조로 막는다",
         f"기사당 AI 호출 1회 · 일일 상한 · 중복 대표 1건만 분석. 실측 평균 {F['tok_in']} in / {F['tok_out']} out 토큰"),
        ("재조회는 0이다",
         "이미 본 기사는 네트워크·AI 를 거치기 전에 걸러진다. 비용이 시간에 비례해 늘지 않는다"),
        ("근거 없는 분석은 만들지 않는다",
         "SWOT 근거가 없으면 배지를 숨기고, 대외협력 영향도는 조문 인용이 없으면 '없음'으로 강제"),
        ("저작권은 협상 대상이 아니다",
         "제목·언론사·발행시각·요약·원문 링크만 공개. 본문 전문은 게시하지 않는다"),
    ], y=BODY_TOP + 0.35, h=1.72, gap=0.42)

    # ── 04 지속 ───────────────────────────────────────────────────
    slide_section(prs, "04", "어떻게 지속하는가", "비용 · 리스크 · 확산")
    s = slide_plain(prs, "지속 가능성 점검", "지금 상태로 얼마나 갈 수 있는가", "7:00 – 8:30")
    kpi_row(s, [(F["db"], "7일치 저장 용량", "안정 구간 200~300MB 예상"),
                ("550일", "핵심 기사 보관", "잡음은 90일 후 정리"),
                (F["cycle_sec"], "사이클 평균 소요", "주기 300초 대비 12%"),
                (F["tok_total"], "누적 AI 토큰", "일일 상한으로 통제")],
            y=BODY_TOP + 0.3, h=2.8)
    two_col(s,
            ("남은 리스크", ORANGE, [
                "배포 위치 미정 — 현재 로컬 PC 상시 실행",
                "Supabase 전환 준비 완료, 스키마 미배포",
                "무료 뉴스 소스 의존 (RSS 캐시 지연)",
                "AI 요약의 사실성은 사람 확인이 필요",
            ]),
            ("다음 단계", GREEN, [
                "상시 가동 서버로 이전 (Fly / Render / 사내)",
                "Supabase 전환으로 다중 사용자 접근",
                "부서별 키워드·임계값 프로파일 분리",
                "대응 액션 초안 자동 생성 (v2 후보)",
            ]),
            y=BODY_TOP + 3.45, h=5.6)

    s = slide_plain(prs, "확산 제안", "같은 구조를 다른 업무에 옮길 수 있다", "8:30 – 10:00")
    check_rows(s, [
        ("바로 쓸 수 있는 곳",
         "구매(원료·부자재 시황) · 안전환경(규제 예고) · IR(경쟁사 공시·시황)"),
        ("옮겨 붙는 것은 '게이트'다",
         "수집·중복제거·관련성 판정·비용 통제 구조는 그대로 두고, 키워드와 관점 프롬프트만 바꾼다"),
        ("추가 비용은 AI 호출뿐",
         "인프라는 파이썬 프로세스 1개와 DB 파일 1개. 도메인이 늘어도 구조는 그대로"),
        ("필요한 결정",
         "① 배포 위치 ② 접근 범위(사내 한정 여부) ③ AI 일일 예산 상한"),
    ], y=BODY_TOP + 0.35, h=1.72, gap=0.42)

    closing(prs, "감사합니다.",
            "확인은 시스템이, 판단은 사람이.")
    prs.save(out)
    print(f"  ✓ 임원용 {len(prs.slides._sldIdLst)}장 → {out}")


# ══════════════════════════════════════════════════════════════════
# 교육과정 최종발표 — "문제 정의 → 구현 → 결과" 3부 구성
# ══════════════════════════════════════════════════════════════════
def build_course_final(template: str, out: str) -> None:
    prs = Presentation(template)
    wipe_slides(prs)

    # 실무자용(build_practitioner)의 "문제① 기사량" 슬라이드와 같은 계산 — 월간 환산치.
    per_month = int(F["per_day"].replace(",", "")) * 30
    month_hours = round(per_month * 5 / 60)
    month_fte = round(month_hours / 173, 1)

    title_slide(
        prs, "26년 포스코퓨처엠 AI활용전문가 과정 · 최종 발표",
        "문제를 발견하고,\n시스템으로 답하다",
        "P-FM News Intelligence — 포스코그룹 뉴스 자동 수집·분석·알림 프로젝트",
        "대외협력  |  김태현\n2026. 09.")

    agenda(prs, [
        ("문제 정의", "무엇이 문제였고, 왜 그 문제를 골랐는가", "4분"),
        ("주요 기능 및 구현", "어떻게 만들었고, 교육·피드백을 어떻게 반영했는가", "5분"),
        ("결과 및 시연", "무엇이 달라졌고, 무엇을 배웠는가", "6분"),
    ])

    # ── 01 문제 정의 ─────────────────────────────────────────────
    slide_section(prs, "01", "문제 정의", "무엇이 문제였고, 왜 그 문제를 골랐는가")

    s = slide_plain(prs, "대외협력팀이 매일 반복하던 일",
                    "포스코그룹·정책·배터리 뉴스를 사람이 포털을 돌며 직접 확인했다")
    check_rows(s, [
        ("확인 주기가 사람마다 다르다", "아침에 한 번 보는 사람, 수시로 보는 사람 — 놓치는 기사가 사람마다 달랐다"),
        ("확인과 판단이 뒤섞여 있었다", "기사를 읽고 정리하는 데 쓴 시간만큼, 실제 대응을 고민할 시간이 줄었다"),
        ("같은 기사가 반복해서 들어온다", "통신사 전재로 동일한 내용이 10~20건씩 중복 유입된다"),
    ], y=BODY_TOP + 0.35, h=1.9, gap=0.4)

    s = slide_plain(prs, "문제가 중요한 이유 — 숫자로 보면",
                    "\"이 정도면 괜찮다\"고 넘길 수 있는 양이 아니었다")
    kpi_row(s, [(f"{F['per_day']}건", "하루 수집", "최근 7일 실측 평균"),
                (f"{F['collected']}건", "1주일 누적", "7일 실측"),
                (f"{per_month:,}건", "한 달 환산", "일평균 × 30일"),
                (f"{month_hours:,}시간", "월간 확인 부담", f"건당 5분 가정 시 · 정규직 약 {month_fte}명 분")],
            y=BODY_TOP + 0.3, h=2.8)
    check_rows(s, [
        ("골든타임이 있다", "오보·이슈 대응이 늦어지면, 시장은 확인되지 않은 내용만으로 먼저 반응한다"),
        ("정보 과부하", "기사가 넘쳐날수록 판단 기준이 사람마다 달라져 일관성을 지키기 어렵다"),
    ], y=BODY_TOP + 3.5, h=1.7, gap=0.4)

    s = slide_plain(prs, "문제를 바라본 관점 — 가설",
                    "\"확인\"과 \"판단\"을 분리할 수 있다면, 사람은 판단에만 시간을 쓸 수 있다")
    two_col(s,
            ("가설", TEAL, [
                "확인(수집·분류·중복제거)은 규칙 + AI로 자동화할 수 있다",
                "판단(대응 전략 수립)은 사람만 할 수 있는 영역이다",
                "따라서 확인을 걷어내면, 그만큼 판단할 시간이 늘어난다",
                "단, 알림이 많으면 아무도 안 본다 — 게이팅이 생존 조건이다",
            ]),
            ("이 문제를 고른 이유", ORANGE, [
                "대외협력 업무에서 본인이 매일 직접 겪던 반복 업무였다",
                "수집 → 필터 → 분석 → 알림 이라는 구조는 이 업무에만 국한되지 않는다",
                "구매·안전환경·IR 등 다른 업무에도 같은 틀을 옮길 수 있다고 판단했다",
                "PRD 원칙: 재조회 비용 0 · 알림 게이팅 · 저작권 준수를 분석 기준으로 삼았다",
            ]),
            y=BODY_TOP + 0.35, h=7.3)

    # ── 02 주요 기능 및 구현 ─────────────────────────────────────
    slide_section(prs, "02", "주요 기능 및 구현", "어떻게 만들었고, 교육·피드백을 어떻게 반영했는가")

    s = slide_plain(prs, "서비스 개요 — 기사 하나가 거치는 길",
                    "5분마다 자동으로 도는 파이프라인")
    flow_steps(s, [
        ("수집", "구글뉴스 RSS · 네이버 검색 API로 키워드별 최신 기사 목록을 받는다"),
        ("게이트", "이미 본 기사 · 오래된 기사 · 중복 기사 · 무관한 기사를 여기서 버린다"),
        ("AI 분석 1회", "요약 · 관점 · 키워드 · 감성 · SWOT 을 한 번에 뽑는다 (기사당 1회)"),
        ("중요도 채점", "핵심 키워드 언급·정책/통상 가점, 단순 시황 감점 → 0~100점"),
        ("저장 · 웹 게시", "직접 만든 웹페이지(포토카드 아카이브)에 전부 올라간다"),
        ("텔레그램 알림", "임계값 이상 · 6시간 이내 기사만 개별 카드로 즉시 발송"),
    ], y=BODY_TOP + 0.05, h=1.45)

    s = slide_plain(prs, "사용 기술 소개", "크롤링·API로 데이터를 모으고, 직접 웹페이지까지 만들었다")
    check_rows(s, [
        ("수집 — 크롤링 · 공개 API", "Google News RSS · 네이버 뉴스 검색 API · 국민참여입법센터 · 열린국회정보 · KOTRA 해외시장뉴스"),
        ("분석 — LLM · 임베딩", "OpenAI API(요약·키워드·감성·SWOT 통합 호출) · text-embedding(중복 판정)"),
        ("저장 — 이중 DB 백엔드", "SQLite(로컬 개발) / Supabase(운영) — 설정 한 줄로 전환"),
        ("전달 — 웹 · 텔레그램", "직접 구현한 정적 웹페이지(HTML·CSS·JS) + Telegram Bot API"),
        ("백엔드", "Python 단일 파일로 수집·분석·API·알림을 전부 처리"),
    ], y=BODY_TOP + 0.3, h=1.5, gap=0.3)

    s = slide_plain(prs, "교육 내용과의 연계", "2주차 크롤링에서 시작해, 데이터를 보여주는 화면까지 직접 만들었다")
    check_rows(s, [
        ("2주차 크롤링 → 뉴스 수집 파이프라인",
         "Google News RSS · 네이버 뉴스를 주기적으로 가져오는 구조에 크롤링에서 배운 방식을 그대로 적용했다"),
        ("같은 방식을 공개 API로 확장",
         "크롤링으로 배운 패턴을 국민참여입법센터·열린국회정보·KOTRA 같은 공공 API 연동에도 동일하게 적용했다(대외협력 탭)"),
        ("데이터를 보여주는 화면도 직접 구현",
         "수집·분석한 결과를 확인할 수 있도록 포토카드 아카이브 웹페이지를 프론트엔드까지 직접 만들었다"),
        ("대안 검토",
         "다른 아키텍처와 비교 검토하기보다, 배운 크롤링·API 방식을 실제 업무 문제에 바로 적용하는 쪽을 택했다"),
    ], y=BODY_TOP + 0.35, h=1.72, gap=0.4)

    s = slide_plain(prs, "시행착오 — 실제 운영하며 발견한 것들",
                    "수집한 데이터는 늘 예상과 다르게 지저분했다")
    check_rows(s, [
        ("계열사 언급을 잘못 판단",
         "여러 계열사를 나열하기만 한 문장을 '관련 있다'고 잘못 태그하는 경우를 발견해 판단 로직을 다시 짰다"),
        ("기자명이 엉뚱하게 뽑힘",
         "기사 본문 HTML 주석 속 문구를 기자명으로 잘못 골라내는 문제를 찾아 고쳤다"),
        ("언론사명이 도메인 그대로 남음",
         "언론사 이름을 못 찾으면 URL 도메인이 그대로 노출되는 문제를 매체명 매핑으로 보완했다"),
    ], y=BODY_TOP + 0.35, h=1.9, gap=0.42)

    s = slide_plain(prs, "멘토링 · 코칭 피드백 반영",
                    "기능적인 피드백을 받은 그대로 화면에 반영했다")
    check_rows(s, [
        ("받은 피드백", "코치님들로부터 페이지네이션 등 화면 사용성에 대한 기능적 피드백을 받았다"),
        ("반영한 것", "전체 기사 목록과 대외협력 화면 모두에 페이지네이션을 추가해, 스크롤 없이도 원하는 페이지로 바로 이동할 수 있게 했다"),
    ], y=BODY_TOP + 0.5, h=2.1, gap=0.5)

    # ── 03 결과 및 시연 ───────────────────────────────────────────
    slide_section(prs, "03", "결과 및 시연", "무엇이 달라졌고, 무엇을 배웠는가")

    s = slide_plain(prs, "기대 효과", "7일 무인 운영 실측 기준")
    kpi_row(s, [(F["collected"], "7일간 수집", "일 평균 " + F["per_day"] + "건"),
                (F["analyzed"], "AI 분석 완료", "요약+관점+키워드+감성+SWOT"),
                (F["sent"], "텔레그램 발송", f"실패 {F['fail']}건"),
                ("5분 이내", "발행 → 도달 목표", "P90 기준")],
            y=BODY_TOP + 0.25, h=2.8)
    two_col(s,
            ("정량적 효과", TEAL, [
                "월 918시간 걸리던 확인 작업이 자동화로 대체됨",
                "발행 → 텔레그램 도달 5분 이내(P90) 목표",
                "7일간 발송 실패 0건, 중복은 대표 1건으로 정리",
            ]),
            ("정성적 효과", GREEN, [
                "확인이 아니라 판단에 시간을 쓸 수 있게 됨",
                "골든타임 확보로 이슈 대응력이 높아짐",
                "배터리·부처·환경까지 커버리지가 넓어짐",
            ]),
            y=BODY_TOP + 3.45, h=5.6)

    s = slide_plain(prs, "시연", "실제 화면으로 바로 보여드리겠습니다")
    check_rows(s, [
        ("웹 화면", "기사 수집 · 필터 · 대외협력 · 주간동향까지 실제 데이터로 시연"),
        ("텔레그램 알림", "실제로 채널에 도착한 메시지와 발송 로그를 확인"),
    ], y=BODY_TOP + 1.0, h=1.9, gap=0.5)

    s = slide_plain(prs, "인사이트 및 회고", "데이터를 모으는 것과, 쓸모 있게 만드는 것은 다른 일이었다")
    check_rows(s, [
        ("배운 점", "크롤링·API로 데이터를 가져오는 것과, 그걸 중복 없이 걸러 중요도까지 판단하게 만드는 것 사이에는 큰 차이가 있다는 것을 직접 부딪히며 배웠다"),
        ("배운 점", "실제 수집되는 기사는 늘 예상과 다르게 지저분해서, 규칙과 AI를 같이 써야 하는 이유를 체감했다"),
        ("아쉬운 점", "아직 상시 서버가 아니라 로컬 PC에서 운영 중이라, 무인 운영을 위한 배포는 다음 과제로 남았다"),
    ], y=BODY_TOP + 0.35, h=1.72, gap=0.4)

    # ── 교육 소감 ──────────────────────────────────────────────
    s = slide_plain(prs, "교육 소감", "")
    txt(s, L, BODY_TOP + 0.3, W, 1.2,
        "짧은 시간에도 많은 지원을 해주신 정유진 코치님을 비롯한 여러 코치님들께 감사드립니다.",
        size=24, bold=True, color=NAVY, line=1.5)
    txt(s, L, BODY_TOP + 2.1, W, 2.4,
        "회사라는, 다소 낯설 수 있는 공간을 학교처럼 편안하게 만들어 주시고,\n"
        "좋은 추억으로 남을 한 달을 만들어 주셔서 감사합니다.",
        size=22, color=INK, line=1.6)

    closing(prs, "감사합니다.",
            "확인은 시스템이, 판단은 사람이.")
    prs.save(out)
    print(f"  ✓ 교육과정 최종발표 {len(prs.slides._sldIdLst)}장 → {out}")


def find_template() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    cands = glob.glob(os.path.join(os.path.expanduser("~"), "Downloads", "*.pptx"))
    if not cands:
        raise SystemExit("템플릿 .pptx 를 찾지 못했습니다. 경로를 인자로 주세요.")
    return max(cands, key=os.path.getsize)   # 이미지가 든 공식 템플릿이 가장 크다


if __name__ == "__main__":
    tpl = find_template()
    here = os.path.dirname(os.path.abspath(__file__))
    print(f"템플릿: {tpl}")
    build_practitioner(tpl, os.path.join(here, "P-FM_NEWS_실무자_발표_10분.pptx"))
    build_executive(tpl, os.path.join(here, "P-FM_NEWS_임원_발표_10분.pptx"))
    build_course_final(tpl, os.path.join(here, "P-FM_NEWS_교육과정_최종발표.pptx"))
