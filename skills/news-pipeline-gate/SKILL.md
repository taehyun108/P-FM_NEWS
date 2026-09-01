---
name: news-pipeline-gate
description: 뉴스·RSS·피드 수집 파이프라인에서 "이미 수집한 기사"를 네트워크/LLM 비용 없이 걸러내는 게이트 설계. Use when building or debugging a news crawler, RSS poller, or feed collector — 신규 기사 판정, 중복 수집, 재조회 비용, 커서(cursor) vs 저장소 대조, Google News 리다이렉트 해제, 신선도 컷오프(freshness cutoff), 백필(backfill), 알림 폭탄 방지 문제를 다룰 때 사용한다.
---

# 뉴스 수집 게이트 (재조회 비용 0 설계)

주기적으로 피드를 폴링하는 수집기에서 **가장 큰 실패 모드는 "이미 본 기사를 매번 다시 처리하는 것"** 이다.
목록 API는 항상 최근 전체를 돌려주므로, 아무 장치가 없으면 매 실행마다 전 항목의 리다이렉트를 풀고 본문을 받고 요약을 호출한다.

## 1. 절대 규칙

1. **발행 시각 커서를 쓰지 않는다.** `published_at > last_cursor` 방식은 기사를 영구 누락시킨다.
   - 피드 캐시 지연으로 뒤늦게 목록에 뜬 기사의 `published_at`은 최초 발행 시각이다 → 커서가 이미 지나갔으면 영원히 안 들어온다.
   - 언론사 수정 기사·분 단위 없는 소스 때문에 발행 시각 자체가 부정확하다.
2. **판정은 저장소 대조(store comparison)로 한다.** 판정 권위는 DB 전체 기간이며 만료가 없다.
3. **네트워크 요청과 LLM 호출은 게이트를 통과한 항목에 대해서만 발생한다.**

## 2. 게이트 단계 — 순서 자체가 요구사항

| 단계 | 처리 | 비용 | 비고 |
|---|---|---|---|
| G0 | 실행 내 중복 제거 (in-memory set) | 0 | |
| G1 | seen-set 캐시 대조 (최근 72h) | 메모리 | 성능용. 정확성에 관여하지 않음 |
| G2 | DB 배치 조회 (**전체 기간**) | 쿼리 1회 | **판정 권위. 기수집분은 여기서 전부 탈락** |
| G2.5 | 신선도 컷오프 (피드가 준 발행시각 사용) | 0 | 네트워크 불필요 |
| G3 | 리다이렉트 해제 → canonical URL | HTTP | 신규분만 |
| G4 | 본문 추출 | HTTP | 신규분만 |
| G5 | 중복 그룹 판정 | 쿼리 | |
| G6 | LLM 요약 | API 과금 | 대표 기사만 |

## 3. 이중 URL 키 — 리다이렉트 해제를 게이트 뒤로 미룬다

Google News 등은 원문이 아닌 자체 리다이렉트 URL을 준다. 원문 URL로 신규 판정을 하면 **매 실행 전 항목의 리다이렉트를 다시 풀어야 한다.**

| 컬럼 | 값 | 용도 |
|---|---|---|
| `url_source` | 소스가 준 URL 원본(추적 파라미터만 제거, 소문자화) | **신규 판정 키. 네트워크 없이 계산 가능** |
| `url_canonical` | 리다이렉트 해제 후 최종 원문 URL | 중복 제거·표시·링크 |
| `url_source_aliases[]` | 같은 기사를 가리키는 다른 소스 URL | 키워드별 URL 상이 대응 |

- 두 컬럼 모두 UNIQUE. aliases에는 GIN 인덱스.
- G5에서 서로 다른 `url_source`가 같은 `url_canonical`로 밝혀지면 alias에 누적 → 다음 실행부터 G1에서 탈락.

## 4. 영구 제외 원장 (url_ledger)

저장하지 않기로 확정한 URL은 본 테이블에 없으므로, 조치가 없으면 **매 실행 G3까지 도달한다.**

```sql
create table url_ledger (
  url_source text primary key,
  reason     text not null,   -- 'stale' | 'no_pubdate' | 'extract_failed' | 'blocked'
  first_seen timestamptz default now(),
  hit_count  int default 1    -- 재등장 횟수(모니터링)
);
```

- G2 조회 대상에 `articles` + `url_ledger`를 **함께** 포함시킨다.
- 만료시키지 않는다. `hit_count` 상위 항목을 주기 점검해 게이트 누수를 감지한다.

## 5. 배치 조회 (실행당 DB 왕복 = 조회 1 + 저장 1)

```sql
select url_source from articles where url_source = any($1)
union all
select a.url_source from articles, unnest(url_source_aliases) as a(url_source)
                    where a.url_source = any($1)
union all
select url_source from url_ledger where url_source = any($1);
```

항목별 개별 조회는 금지. 반드시 배열 파라미터 1회 쿼리.

## 6. 신선도 컷오프 — "최신"의 정의

| 발행 경과 | 저장 | 알림 |
|---|---|---|
| 6h 이내 | O | O (중요도 임계값 충족 시) |
| 6~72h | O (`is_backfill = true`) | X — 웹/아카이브에만 노출 |
| 72h 초과 | X (`url_ledger`에 `stale` 기록) | X |

컷오프 값은 환경변수/설정으로 조정 가능해야 한다. 뒤늦게 인덱싱된 기사가 "속보"로 알림되는 것을 막는 장치다.

## 7. 부트스트랩 / 장애 복구 — 알림 억제 모드

- 최초 실행은 **억제 모드**: 과거분을 수집·저장하되 발송은 전건 `skipped`.
- 파이프라인이 N분(기본 30분) 이상 중단 후 재개될 때도 억제 모드로 1회 실행 후 정상 전환.
- 모드는 `run_state` 테이블(`notify_mode`, `last_success_at`)로 관리한다.

## 8. 검증 기준 (구현했다고 말하기 전에 통과시킬 것)

- [ ] 1회 실행의 **외부 HTTP 요청 수 = 소스 조회 수 + 신규 기사 수**. 신규 0건이면 추가 요청 0.
- [ ] 회귀 테스트: 수집 후 7일·30일·180일 경과한 과거 기사를 소스 응답에 강제 주입 → 세 건 모두 G2에서 탈락, HTTP 요청 0회.
- [ ] seen-set 캐시를 통째로 비워도 결과가 동일하다(느려질 뿐).
- [ ] `collection_logs.skipped_seen_count`, `http_request_count`를 매 실행 기록한다.
- [ ] 실행 시간이 다음 폴링 주기를 넘기면 해당 회차 스킵 + 경보.

## 9. 흔한 구현 실수

- G2 조회에 `url_ledger`를 빼먹음 → 스킵된 기사가 매번 G3까지 감.
- 리다이렉트를 먼저 풀고 나서 신규 판정 → 게이트 무력화(가장 흔한 실수).
- seen-set 캐시를 판정 권위로 착각 → 캐시 윈도 밖 기사 재수집.
- 저장 시 UNIQUE 제약 없이 애플리케이션 체크만 의존 → 재시도·경합에서 중복 저장.
