---
name: telegram-news-notifier
description: 텔레그램 봇 알림 발송 설계 - 메시지 포맷, rate limit 준수 큐, 다이제스트 묶음, 야간 모드, 중복 발송 방지, 봇 명령어. Use when sending notifications through a Telegram bot (or similar push channel), designing a send queue, hitting Telegram rate limits (429 / retry_after), preventing duplicate or flooding alerts, or implementing bot commands like /start /stop /latest.
---

# 텔레그램 알림 발송

발송은 "성공하면 끝"이 아니라 **큐·재시도·중복방지·유량제어**가 있는 하위 시스템이다.

## 1. 발송 트리거

- 기사가 DB에 저장되고 **요약까지 완료된 시점**에 큐에 적재한다. 저장 직후 즉시 발송하면 요약 없는 알림이 나간다.
- 백필 기사(`is_backfill = true`)와 억제 모드 실행분은 `status = 'skipped'`로 큐에 남기되 발송하지 않는다.

## 2. 중복 발송 방지 (0건이어야 함)

```sql
create table notifications (
  id          uuid primary key default gen_random_uuid(),
  article_id  uuid references articles(id),
  channel     text not null,          -- 'telegram'
  chat_id     text not null,
  status      text not null,          -- 'queued'|'sent'|'failed'|'skipped'
  error       text,
  retry_count int default 0,
  sent_at     timestamptz,
  created_at  timestamptz default now()
);
create unique index on notifications (article_id, channel, chat_id);
create index on notifications (status, created_at);
```

**애플리케이션 체크가 아니라 UNIQUE 제약이 보증한다.** 재시도·동시 실행·재기동에서도 중복이 불가능해야 한다.

## 3. 유량 제어

- 텔레그램 제한: 동일 채팅방 **분당 20건**, 전체 초당 30건 근처.
- 큐에서 **초당 1건 이하**로 배출한다. 여유를 크게 잡는 편이 안전하다.
- `429` 응답의 `retry_after`를 반드시 존중한다. 임의 재시도 금지.
- 한 사이클 대상이 **3건 이상이면 하나의 다이제스트 메시지로 묶는다.**

## 4. 야간 모드

- 23:00–07:00: 중요도 80 이상만 즉시 발송, 나머지는 07:00 다이제스트로 이월.
- 시간대는 설정값. 서버 UTC와 사용자 로컬 시간대를 반드시 구분해 처리한다.

## 5. 메시지 포맷

```
🔴 [포스코퓨처엠] 제목이 여기에 들어갑니다

한국경제 · 3분 전

• 요약 첫 문장
• 요약 두 번째 문장
• 요약 세 번째 문장

🔗 원문 보기
```

- 배지: 80+ 🔴 / 50–79 🟠
- `parse_mode: HTML`, `disable_web_page_preview: false`
- **HTML 이스케이프 필수** — 기사 제목의 `<`, `>`, `&`가 파싱 오류를 낸다. 실패의 흔한 원인.
- 메시지 4096자 제한. 다이제스트는 넘치면 분할한다.

## 6. 봇 명령어

| 명령 | 동작 |
|---|---|
| `/start` | 구독 등록 |
| `/stop` | 구독 해제 |
| `/latest` | 최근 5건 즉시 조회 |
| `/today` | 오늘 다이제스트 |
| `/filter` | 카테고리별 구독 설정 |
| `/threshold [n]` | 알림 임계값 조정 |

- 그룹방과 개인 DM은 임계값 기본값을 다르게 잡는다(그룹방이 더 보수적).
- 봇 토큰과 chat_id는 환경변수로만 관리한다. 코드·클라이언트 번들에 절대 포함 금지.

## 7. 실패 처리

- 재시도 3회(지수 백오프) → 최종 실패 시 `status='failed'` + `error` 기록 + 운영 화면 노출.
- 발송 실패가 연속 N회면 운영자에게 경보 1회(중복 경보 억제).
- 사용자가 봇을 차단(`403 Forbidden: bot was blocked`)하면 재시도하지 말고 구독을 해제한다.

## 8. 검증

- [ ] 동일 기사 중복 발송 0건 (파이프라인 강제 재실행으로 확인).
- [ ] 재기동 직후 과거분이 한꺼번에 발송되지 않는다(억제 모드).
- [ ] 20건 동시 대상 → 다이제스트 묶음 + rate limit 위반 0.
- [ ] 제목에 `<`, `&`가 포함된 기사가 정상 발송된다.
- [ ] 발행 → 도달 P90 5분 이내.
