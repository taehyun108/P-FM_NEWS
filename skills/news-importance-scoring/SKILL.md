---
name: news-importance-scoring
description: 기사 카테고리 태깅과 0~100 중요도 스코어링, 그리고 스코어 기반 알림 게이팅 설계. Use when deciding which articles deserve a push notification, building a relevance/importance score, tagging articles by category, tuning alert thresholds, or fixing notification fatigue (알림 피로, 알림 스팸) in a news monitoring system.
---

# 기사 분류 및 중요도 스코어링

**수집한 전부를 알림으로 밀어 넣으면 알림 채널은 사실상 무시된다.** 스코어 게이팅은 알림 채널의 생존 조건이다.

## 1. 원칙

- **저장은 전부, 알림은 선별.** 웹/아카이브에는 전건 노출하고 필터로 조절한다. 푸시만 임계값을 건다.
- 스코어는 **룰 기반을 먼저** 만든다. LLM 분류는 보조로만 쓴다(비용·지연·재현성).
- 가중치와 임계값은 **DB 또는 환경변수 설정값**이다. 코드 상수로 박지 않는다.

## 2. 카테고리 태깅 (복수 태그 허용)

`article_tags(article_id, tag, confidence)` — 하나의 기사에 여러 태그를 붙인다.

축을 섞지 말 것. 보통 3개 축으로 나뉜다.
- **주체 축**: 어느 회사/조직 (모회사·계열사·경쟁사)
- **주제 축**: 산업·기술·시장·정책/규제·사건사고
- **지역 축**: 사업장·지역명

`confidence`를 남겨두면 나중에 임계값으로 오탐 태그를 걸러낼 수 있다.

## 3. 중요도 스코어 (0~100)

가산·감산의 합을 0~100으로 클램프한다.

| 요소 | 가중치 | 비고 |
|---|---|---|
| 핵심 대상(자사) 직접 언급 | +40 | **제목**에 언급 시 +50 |
| 관계사·그룹사 언급 | +25 | |
| 정책·규제·수사·사고 키워드 | +20 | 대응이 필요한 이슈 |
| 지역 사업장 언급 | +15 | |
| 주요 언론사(tier 1) | +10 | |
| 단순 시황·주가 기사 | −15 | 알림 피로의 주범 |

설계 포인트
- **제목 언급과 본문 언급을 구분한다.** 본문 끝에 한 번 스친 언급은 기사의 주제가 아니다.
- 감산 항목을 반드시 둔다. 가산만 있으면 모든 기사가 임계값을 넘는다.
- 부정 이슈(수사·사고·리콜·소송) 키워드는 별도 가산 → 초동 대응 시점이 중요하다.

```python
def score_article(article, cfg) -> int:
    """룰 기반 중요도 스코어. 가중치는 설정에서 주입받는다."""
    s = 0
    title, body = article.title, article.body or ""
    if cfg.core_name in title:      s += cfg.w_core_title      # 제목 언급
    elif cfg.core_name in body:     s += cfg.w_core_body
    if any(k in title or k in body for k in cfg.affiliates):    s += cfg.w_affiliate
    if any(k in title or k in body for k in cfg.policy_keywords): s += cfg.w_policy
    if any(k in title or k in body for k in cfg.regions):       s += cfg.w_region
    if article.press_tier == 1:     s += cfg.w_major_press
    if any(k in title for k in cfg.market_noise_keywords):      s += cfg.w_market_penalty  # 음수
    return max(0, min(100, s))
```

## 4. 알림 게이팅

| 채널 | 정책 |
|---|---|
| 웹·아카이브 | 전건 저장·노출, 필터로 조절 |
| 푸시(텔레그램 등) | 임계값(기본 50) 이상만 |
| 야간(23:00–07:00) | 80 이상만 즉시, 나머지는 아침 다이제스트로 이월 |
| 백필 기사(`is_backfill`) | 스코어 무관하게 알림 제외 |

- 임계값은 사용자별로 조정 가능하게 만든다(`/threshold` 류 명령).
- 스코어 구간별 시각 배지: 80+ 🔴 / 50–79 🟠 / 그 미만은 알림 없음.

## 5. 튜닝 방법

1. 1주치 기사를 수집해두고 **스코어만 재계산**해본다(재수집 금지).
2. 상위 20건·하위 20건을 수동 검토해 오탐/미탐을 센다.
3. 목표: 중요 기사 누락률 5% 이하, 알림 유효율(열람 전환) 30% 이상.
4. 가중치를 바꿨으면 과거 데이터로 재평가 → 임계값을 다시 정한다.

## 6. 검증

- [ ] 동일 기사에 대해 스코어 계산이 결정적(deterministic)이다.
- [ ] 단순 주가 시황 기사가 임계값을 넘지 않는다.
- [ ] 제목 언급 기사가 본문 스침 기사보다 항상 높은 점수를 받는다.
- [ ] 가중치를 설정에서 바꾸면 코드 수정 없이 반영된다.
