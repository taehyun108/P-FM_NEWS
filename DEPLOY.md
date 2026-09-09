# 배포 가이드

로컬 PC를 끄고 클라우드에서 상시 운영하기 위한 절차. 저장소는 이미 Supabase로
옮겨져 있으므로, 컨테이너는 상태를 갖지 않는다(디스크에 남길 것이 없다).

---

## 0. 배포 전 체크리스트

| 항목 | 확인 |
|---|---|
| `DB_BACKEND=supabase` | SQLite로 두면 컨테이너 재시작 때 데이터가 날아간다 |
| `APP_TZ_OFFSET=9` | **필수.** 없으면 야간 억제·주간 레포트가 9시간 어긋난다 |
| `WEB_PASSWORD` | **필수.** 비우면 URL만 알면 누구나 들어온다 |
| `MASTER_PASSWORD` | 마스터 패널 비밀번호 |
| 인스턴스 수 = **1** | 2개 이상이면 같은 기사를 중복 발송한다 (아래 참조) |
| `KAKAO_REDIRECT_URI` | 배포 주소로 바꾸고 카카오 콘솔에도 등록 → 토큰 재발급 |

---

## 1. 왜 인스턴스는 1개여야 하나

`python backend/main.py run` 한 프로세스 안에서 API 서버와 함께 데몬 스레드가 돈다.

- 수집 루프 (300초)
- 시세 갱신 (60초)
- 텔레그램 봇 롱폴링
- 대외협력 수집 (하루 2회)

인스턴스를 2개로 늘리면 이 루프가 두 벌 돌아 **수집·LLM 호출·알림이 2배**가 된다.
텔레그램은 `(기사, 채널, 채팅방)` UNIQUE 제약이 일부 막아 주지만 카카오는 막히지 않는다.
오토스케일은 **반드시 최소=최대=1**로 고정한다.

---

## 2. 환경변수

`.env.example`을 그대로 옮기되, 배포에서 값이 달라지는 것만 정리하면:

```
DB_BACKEND=supabase
APP_TZ_OFFSET=9
WEB_PASSWORD=<팀에 공유할 접속 비밀번호>
MASTER_PASSWORD=<관리자 비밀번호>
API_HOST=0.0.0.0          # PORT가 주입되면 자동으로 0.0.0.0이 되므로 생략 가능
ALLOWED_ORIGINS=          # 비워 둔다(같은 주소에서 프런트를 서빙하므로 CORS 불필요)
KAKAO_REDIRECT_URI=https://<배포주소>/kakao/callback
```

`PORT`는 플랫폼이 주입하므로 직접 설정하지 않는다. 코드가 `PORT` → `API_PORT` 순으로 읽는다.

`.env` 파일은 커밋되지 않으므로 **플랫폼의 환경변수 화면에 직접 입력**한다.

---

## 3. 배포 방법

### Docker를 받는 곳 (Koyeb · Render · Fly.io · Cloud Run)

저장소 루트의 `Dockerfile`을 그대로 쓴다. 빌드 명령·시작 명령을 따로 지정할 필요 없다.

```
docker build -t pfm-news .
docker run -p 8000:8000 --env-file .env pfm-news
```

- 헬스체크 경로: `/healthz` (DB를 건드리지 않는 가벼운 응답)
- 포트: `PORT` 환경변수 (기본 8000)

### Procfile을 받는 곳 (Railway 등)

`Procfile`에 `web: python backend/main.py run`이 들어 있다.

### VM (Oracle Cloud Always Free 등)

```bash
git clone <repo> && cd P-FM_NEWS
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env && vi .env      # 값 입력
python backend/main.py run
```

상시 실행은 systemd 유닛으로 묶는다:

```ini
[Unit]
Description=P-FM NEWS
After=network-online.target

[Service]
WorkingDirectory=/opt/P-FM_NEWS
ExecStart=/opt/P-FM_NEWS/.venv/bin/python backend/main.py run
Restart=always
RestartSec=10
User=pfm

[Install]
WantedBy=multi-user.target
```

---

## 4. 무료 티어의 슬립

Render 무료 웹서비스는 15분 무요청이면 잠든다. 잠들면 **수집 루프도 멈춘다.**
`/healthz`를 UptimeRobot 등으로 5분마다 찌르면 깨어 있는다.
Koyeb·Fly.io·Oracle Cloud는 슬립이 없다.

---

## 5. 접속 통제

`WEB_PASSWORD`(또는 마스터 패널의 '웹페이지 비밀번호')가 설정돼 있으면 잠금이 켜진다.

- 미인증 상태에서 화면(`/`)을 열면 로그인 페이지가 뜨고, `/api/*`는 401을 준다.
- 통과하면 `pfm_web` 쿠키(HttpOnly · SameSite=Lax · 30일)를 받는다.
- 세션 토큰은 저장된 비밀번호 해시에서 파생되므로 **서버를 재시작해도 로그인이 유지**되고,
  **비밀번호를 바꾸면 기존 세션이 전부 무효**가 된다.
- 잠금과 무관하게 열려 있는 경로: `/healthz`, `/api/web/*`, `/kakao/callback`.

마스터 전용 기능(설정 변경·주간 레포트 발송)은 그 위에 마스터 토큰이 한 겹 더 필요하다.

---

## 6. 배포 후 카카오 토큰 재발급

리다이렉트 URI가 바뀌므로 토큰을 다시 받아야 한다.

1. 카카오 콘솔 → 카카오 로그인 → Redirect URI 에 `https://<배포주소>/kakao/callback` 추가
2. 플랫폼 환경변수 `KAKAO_REDIRECT_URI`를 같은 값으로 변경 후 재시작
3. `https://kauth.kakao.com/oauth/authorize?client_id=<REST_API_KEY>&redirect_uri=<인코딩된 URI>&response_type=code&scope=talk_message` 를 브라우저에서 1회 열기
4. "카카오 연결 완료"가 뜨면 끝

---

## 7. 첫 배포 확인

```bash
curl https://<배포주소>/healthz          # {"ok":true,"service":"pfm-news"}
curl https://<배포주소>/api/web/status   # {"locked":true,"authed":false}
```

브라우저로 접속해 로그인 → 대시보드가 뜨고, 로그에 `수집 루프 시작`이 찍히면 정상이다.
