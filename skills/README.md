# PRD 기반 스킬 모음

`PRD.md`(POSCO Future M News Intelligence)의 설계 결정을 Claude Code 스킬로 정리한 것이다.
이 프로젝트 전용 내용은 최소화하고, 다른 뉴스·수집·알림 프로젝트에서도 그대로 쓸 수 있게 일반화했다.

## 스킬 목록

| 스킬 | 다루는 문제 | PRD 대응 |
|---|---|---|
| `news-pipeline-gate` | 기수집 기사 재조회 비용 0으로 만들기 (G0~G6 게이트, 이중 URL 키, `url_ledger`, 신선도 컷오프, 억제 모드) | F1.1 |
| `news-dedup-normalize` | URL 정규화 2단계, 3단계 중복 판정, 언론사 도메인 식별 | F2 |
| `news-importance-scoring` | 카테고리 태깅, 0~100 중요도 스코어, 알림 게이팅 | F3 |
| `llm-news-summary` | 본문 추출·폴백, 한국어 3~5줄 요약 규격, LLM 비용 상한 | F4 |
| `telegram-news-notifier` | 발송 큐, rate limit, 다이제스트, 야간 모드, 중복 발송 0 | F7 |
| `supabase-news-schema` | 스키마·인덱스·RLS, 벌크 업서트 | F5 |
| `env-secret-guard` | `.env` 전용 시크릿 관리와 유출 점검 | §6 보안, §7 |

## 전역 설치

```bash
bash skills/install.sh
```

`~/.claude/skills/` 아래로 복사되므로 이 저장소 밖의 어떤 프로젝트에서도 동작한다.
스킬 내용을 수정했으면 같은 명령을 다시 실행하면 갱신된다.

## 함께 설치한 외부 스킬

```bash
npx skills add https://github.com/vercel-labs/skills --skill find-skills --global
```

`find-skills`는 필요한 스킬을 찾아 설치해 주는 스킬이며, 이 저장소가 아니라
`~/.agents/skills/find-skills` 에 설치되고 `~/.claude/skills/` 에서 심볼릭 링크로 참조된다.

## 스킬 작성 규칙

- 디렉터리명과 frontmatter의 `name` 이 일치해야 한다.
- `description` 에는 "언제 쓰는지"를 한국어·영어 트리거 단어와 함께 적는다. 이 문장만으로 호출 여부가 결정된다.
- 본문은 결정과 근거, 그리고 검증 체크리스트 위주로 쓴다.
