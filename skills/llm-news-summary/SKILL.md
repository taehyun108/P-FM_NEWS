---
name: llm-news-summary
description: 기사 본문 추출과 한국어 3~5줄 LLM 요약 규격, 폴백·재시도·비용 상한 설계. Use when generating article summaries with an LLM, extracting article body text (Readability), designing summary prompts, preventing hallucination in summaries, tiering models by importance, or capping daily LLM cost in a news pipeline.
---

# 기사 본문 추출 및 LLM 요약

요약은 파이프라인에서 **유일하게 건당 과금되는 단계**다. 호출 횟수를 줄이는 설계가 프롬프트 품질보다 먼저다.

## 1. 호출을 줄이는 설계 (프롬프트보다 우선)

1. 수집 게이트를 통과한 신규 기사만 요약한다(기수집분 재요약 금지).
2. 중복 그룹의 **대표 기사 1건만** 호출한다. 나머지는 대표의 요약을 참조.
3. 일일 호출 상한을 둔다. 상한 초과 시 **중요도 상위 건만** 요약하고 나머지는 큐에 남긴다.
4. 모델 티어링: 기본은 저비용 모델(Haiku 급), 고스코어 기사만 상위 모델(Sonnet 급).

## 2. 본문 추출

- Readability 계열 파서를 1순위로 쓴다.
- 실패 시 **폴백**: RSS `description` + 제목으로 요약하고 `summary_source = 'snippet'`으로 표시한다. 요약을 포기하지 않는다.
- 추출이 영구 실패하는 URL(페이월·봇 차단)은 `url_ledger`에 `extract_failed`로 기록해 다음 실행부터 재시도하지 않는다.
- 본문 원문은 **요약 목적의 임시 처리**만 하고, 보관이 필요하면 비공개 영역에 보존 기간을 정해 둔다(제안: 30일).

## 3. 요약 규격

- **3~5문장, 한국어, 문장당 40자 내외.**
- 구성: ① 무슨 일이 ② 누가·어디서 ③ 수치·규모 ④ (해당 시) 우리 관점의 시사점
- **추측·의견 금지. 기사에 없는 사실 생성 금지.**
- 원문 표현을 그대로 옮기지 않고 재서술한다(저작권).

### 프롬프트 골격

```
당신은 뉴스 요약 어시스턴트다. 아래 기사 본문만을 근거로 요약한다.

규칙:
- 정확히 3~5문장, 한국어, 각 문장 40자 내외
- 순서: (1) 무슨 일이 (2) 누가·어디서 (3) 수치·규모 (4) 시사점(있을 때만)
- 본문에 없는 사실·배경·전망을 추가하지 않는다
- 의견·평가·추측 표현을 쓰지 않는다
- 원문 문장을 그대로 복사하지 않고 재서술한다
- 본문이 요약하기에 불충분하면 정확히 "요약불가"만 출력한다

제목: {title}
언론사: {press}
본문:
{body}
```

- 출력은 JSON 스키마(`{"summary": [...문장]}`)로 강제하면 후처리가 안정된다.
- "요약불가" 탈출구를 반드시 준다. 없으면 모델이 지어낸다.

## 4. 실패 처리

- 요약 실패 시 지수 백오프로 3회 재시도.
- 최종 실패면 **요약 없이 링크·제목만 저장**하고 재처리 큐에 적재한다. 기사 자체를 버리지 않는다.
- 토큰 사용량(`token_usage`)과 모델명을 매 건 저장해 비용을 사후 추적한다.

```sql
create table summaries (
  id             uuid primary key default gen_random_uuid(),
  article_id     uuid not null references articles(id) on delete cascade,
  summary_text   text not null,
  summary_source text not null,   -- 'fulltext' | 'snippet'
  model          text not null,
  token_usage    jsonb,
  created_at     timestamptz default now()
);
```

## 5. 검증

- [ ] 요약 성공률 95% 이상(폴백 포함).
- [ ] 중복 그룹 15건 → LLM 호출 1회.
- [ ] 본문에 없는 고유명사·수치가 요약에 등장하지 않는다(샘플 20건 수동 검증).
- [ ] 일일 상한에 도달하면 호출이 실제로 멈추고, 상위 스코어 건만 처리된다.
- [ ] 재실행해도 이미 요약된 기사에 대한 호출이 0건이다.
