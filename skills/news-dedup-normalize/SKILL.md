---
name: news-dedup-normalize
description: URL 정규화와 기사 중복 판정(3단계 - URL 완전일치 / 본문 해시 / 제목 유사도+시각) 및 언론사 도메인 식별 설계. Use when deduplicating articles, normalizing URLs, handling wire-service syndication (연합뉴스 전재), content hashing, title similarity, or mapping domains to press outlets in a news/RSS/crawler pipeline.
---

# 기사 정규화 및 중복 제거

통신사(연합뉴스 등) 기사가 매체 전재로 **동일 내용 10~20건**으로 들어오는 것이 뉴스 수집 시스템 최대의 노이즈 요인이다.
단일 URL 비교만으로는 절대 해결되지 않는다.

## 1. URL 정규화 — 두 시점으로 분리

정규화를 언제 하느냐가 비용을 결정한다.

### (A) `url_source` 정규화 — 게이트 이전, **네트워크 금지**
- 추적 파라미터 제거: `utm_*`, `fbclid`, `gclid`, `igshid`, `spm`, `ref`, `from`
- 스킴/호스트 소문자화, 기본 포트(`:80`, `:443`) 제거
- 후행 슬래시 정리, 빈 쿼리(`?`)·프래그먼트(`#...`) 제거
- **리다이렉트를 풀지 않는다.** 여기서 풀면 기수집 기사에도 매 실행 HTTP 요청이 발생한다.

### (B) `url_canonical` 생성 — 게이트 통과분만, HTTP 허용
- 리다이렉트 체인 해제(최대 5홉, 타임아웃 필수) → 최종 원문 URL
- 페이지의 `<link rel="canonical">` / `og:url`이 있으면 우선 채택
- (A)와 동일한 정규화 규칙을 다시 적용

```python
TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "igshid", "spm", "ref", "from", "cid"}

def normalize_url(raw: str) -> str:
    """네트워크 없이 가능한 정규화만 수행한다(게이트 이전 단계용)."""
    u = urlsplit(raw.strip())
    scheme = u.scheme.lower() or "https"
    host = u.hostname.lower() if u.hostname else ""
    port = "" if u.port in (None, 80, 443) else f":{u.port}"
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=False)
             if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_KEYS]
    path = u.path.rstrip("/") or "/"
    return urlunsplit((scheme, host + port, path, urlencode(sorted(query)), ""))
```

## 2. 중복 판정 3단계 (전부 필수)

| 단계 | 판정 | 비용 | 놓치는 것 |
|---|---|---|---|
| 1 | 정규화 URL 완전 일치 | 0 | 전재 기사 |
| 2 | 본문 해시(SHA-256) 일치 | 본문 필요 | 편집이 살짝 들어간 전재 |
| 3 | 제목 정규화 후 유사도 ≥ 0.9 **AND** 발행 시각 차이 ≤ 24h | 계산 | — |

3단계는 **두 조건의 AND**다. 유사도만 보면 연재·기획 기사가 잘못 묶인다.

### 제목 정규화 (유사도 계산 전처리)
- 대괄호 말머리 제거: `[속보]`, `[단독]`, `(종합)`, `(2보)`
- 언론사명·기자명 꼬리 제거
- 공백/특수문자 정규화, 전각→반각
- 유사도는 자모 단위 또는 문자 n-gram 기반(한국어는 어절 토큰만으로 부족)

### 중복 그룹 처리
- `dedup_group_id`로 묶고 **대표 1건만** `is_representative = true`.
- 대표 선정 우선순위: ① 언론사 tier ② 본문 추출 성공 ③ 발행 시각 빠른 순.
- **LLM 요약은 대표 1건만 호출한다.** 여기서 비용의 대부분이 절약된다.
- 서로 다른 `url_source`가 같은 기사로 밝혀지면 대표 기사의 `url_source_aliases`에 누적 → 다음 실행부터 캐시 단계에서 탈락.

## 3. 언론사 식별

```sql
create table press_outlets (
  id     uuid primary key default gen_random_uuid(),
  domain text not null unique,
  name   text not null,
  tier   int default 3,                -- 1=주요지, 2=업계·경제지, 3=기타
  status text default 'pending'        -- 'approved' | 'pending' | 'blocked'
);
```

- 도메인 → 언론사 매핑은 **테이블로 관리**한다. 코드 하드코딩 금지.
- 미등록 도메인은 `pending`으로 적재하고 운영 화면에서 승인. 수집을 막지 않는다.
- 서브도메인(`biz.example.com`)은 등록 도메인 기준으로 접어서 조회.
- `tier`는 중요도 스코어와 대표 기사 선정에 재사용된다.

## 4. 검증

- [ ] 동일 통신사 기사 15건 샘플 → 대표 1건, LLM 호출 1회.
- [ ] 서로 다른 키워드로 들어온 같은 기사 → 두 `url_source` 모두 alias에 누적되고 두 번째 실행에서 HTTP 요청 0.
- [ ] 제목이 비슷한 다른 날짜 기사(연재물)가 잘못 묶이지 않는다.
- [ ] 정규화 함수 단위 테스트: 추적 파라미터·후행 슬래시·대문자 호스트·기본 포트.
