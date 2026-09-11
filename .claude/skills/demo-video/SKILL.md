---
name: demo-video
description: >-
  YAML 시나리오 한 장으로 Playwright 웹 화면 시연 영상(mp4)을 자동 생성한다. 가상 커서(곡선 이동)·클릭
  리플 효과·하단 자막·챕터 타이틀 카드·특정 영역 스포트라이트 하이라이트를 자동으로 얹어서, 실제 사람이 화면을 조작하며
  설명하는 것처럼 보이는 데모 영상을 만든다. 사용자가 "데모 영상", "시연 영상", "화면 녹화", "제품 소개 영상",
  "튜토리얼 영상"을 Playwright/자동화로 만들어 달라고 하거나, 웹 앱 사용법을 mp4로 남기고 싶다고 하면 매번 Playwright
  코드를 새로 짜지 말고 반드시 이 스킬을 사용할 것 — YAML 시나리오만 작성하면 scripts/run_scenario.js 러너가
  녹화부터 mp4 변환까지 전부 처리한다.
---

# demo-video

웹 화면을 자동으로 조작하면서 녹화하고, 커서·자막·하이라이트·챕터 카드 같은 연출을 입혀 mp4로 뽑아내는 스킬이다.
**시나리오는 YAML로만 작성한다.** `scripts/run_scenario.js` 가 그 YAML을 읽어 실행하는 범용 러너이므로,
새 데모를 찍을 때마다 Playwright 코드를 새로 짤 필요가 없다 — 코드를 고치는 경우는 새로운 종류의 연출(스텝
타입)을 추가할 때뿐이다.

## 왜 DOM에 커서·자막을 그리는가

Playwright의 `recordVideo` 는 페이지가 실제로 렌더링한 화면만 캡처한다. OS 마우스 커서는 브라우저가 그리는
것이 아니라 운영체제가 화면 위에 합성하는 것이라서, headless든 headful이든 **녹화 영상에는 절대 찍히지 않는다.**
그래서 `scripts/inject.js` 를 `context.addInitScript()` 로 등록해, 커서·자막·하이라이트·챕터 카드를
전부 `<div>` 로 페이지 안에 직접 그린다. `addInitScript` 는 새 문서가 로드될 때마다(즉 `goto` 로 페이지를
옮겨도) 페이지 자신의 스크립트보다 먼저 다시 실행되므로, 화면이 바뀌어도 오버레이가 계속 따라온다.

## 최초 1회 설치

```bash
bash scripts/setup.sh
```

`playwright`, `yaml`, `ffmpeg-static` npm 패키지를 설치하고 Playwright 크로미움 브라우저를 내려받는다.
**mp4(H.264) 변환에 `ffmpeg-static` 을 쓰는 이유**: Playwright 가 내부적으로 녹화에 쓰는 번들 ffmpeg는
VP8/webm 인코딩만 지원해서 mp4를 만들 수 없다. 그래서 실제 화면 녹화는 Playwright가 webm으로 하게 두고,
그 결과물을 `ffmpeg-static` 이 받아온 정식 ffmpeg 바이너리로 H.264 mp4 재인코딩한다.

`playwright install` 이 사내망 등에서 막혀 있으면(다운로드 서버 접근 차단 등), 이미 설치된 크로미움 실행
파일 경로를 `DEMO_VIDEO_CHROMIUM_PATH` 환경변수로 지정해 그걸 재사용할 수 있다:

```bash
DEMO_VIDEO_CHROMIUM_PATH=/path/to/chromium node scripts/run_scenario.js --scenario my.yaml
```

## 사용법

```bash
node scripts/run_scenario.js --scenario <시나리오.yaml> [--out <디렉터리>] [--headed]
```

- 결과 mp4는 기본적으로 이 스킬 폴더의 `out/` 아래에 저장된다(`--out` 으로 바꿀 수 있음).
- 시나리오 실행 중 어떤 스텝에서든 실패하면(요소를 못 찾음, 타임아웃 등) **그 시점의 스크린샷**을
  `out/failure-<타임스탬프>.png` 로 남기고, 그때까지 녹화된 영상도 mp4로 변환해 저장한다 — 어디서
  멈췄는지 바로 확인할 수 있다.
- 먼저 `scripts/fixture/example-scenario.yaml` 로 동작을 확인해 보는 것을 권장한다. 서버 없이도
  같은 폴더의 정적 HTML(`fixture/demo-page.html`)을 열어서 로그인→요약 화면 데모를 찍는다.

## YAML 시나리오 스키마

```yaml
title: "화면 이름"              # 로그 출력용, 선택
baseUrl: "http://localhost:8000" # goto 의 상대 경로 기준. 실제 서버를 시연할 때 사용
startUrl: "/"                    # 첫 화면. 없으면 baseUrl 을 그대로 연다
viewport: { width: 1920, height: 1080 }  # 기본값이 이미 1920x1080이라 보통 생략 가능
output: "my-demo"                # out/my-demo.mp4 로 저장. 기본값 "demo"
timeout: 15000                   # 각 Playwright 동작의 기본 타임아웃(ms)

steps:
  - chapter: "1. 로그인"          # 전체 화면 타이틀 카드(챕터 간지). subtitle/duration 은 선택
    subtitle: "먼저 로그인합니다"
    duration: 2200                # 카드가 떠 있는 시간(ms). 기본 2200

  - goto: "/login"                 # baseUrl 기준 상대경로. http(s):// 나 file:// 전체 URL 도 가능

  - highlight: "#login-form"       # 대상 요소에 노란 테두리 + 나머지 화면 어둡게(스포트라이트)
    caption: "로그인 폼입니다"      # 모든 스텝에 caption 을 곁들일 수 있다(하단 자막)
    duration: 1800                 # 하이라이트 유지 시간(ms). 기본 1800
    hold: false                    # true 면 다음에 hideHighlight 를 명시할 때까지 안 지움

  - click: "#username"             # 커서가 곡선으로 이동 → 클릭 리플 → 실제 클릭
    caption: "아이디 입력란을 클릭합니다"

  - type:                          # 클릭 후 사람처럼 한 글자씩(무작위 지연) 입력
      selector: "#username"
      text: "demo_user"

  - wait: 800                      # 그냥 대기(ms)

  - hover: "#help-icon"            # 클릭 없이 커서만 이동해 올려놓기

  - scroll: "#footer"              # 해당 요소가 보이도록 스크롤

  - hideHighlight: true            # highlight 를 hold:true 로 띄워 둔 걸 수동으로 끌 때

  - clearCaption: true             # 자막을 비운다(다음 자막이 나올 때까지 안 보이게)
```

### 스텝 타입 정리

| 스텝 키 | 동작 |
|---|---|
| `chapter` (+`subtitle`,`duration`) | 전체 화면 타이틀 카드를 띄웠다가 닫는다 |
| `goto` | 페이지 이동 (`baseUrl` 상대경로 / 절대 URL / `file://`) |
| `click` | 커서를 곡선으로 이동시키고 클릭 리플을 보여준 뒤 클릭 |
| `type` (`selector`,`text`) | 클릭 후 한 글자씩 무작위 간격으로 입력 |
| `hover` | 커서만 이동, 클릭 없음 |
| `highlight` (+`caption`,`duration`,`hold`) | 대상 요소 스포트라이트 하이라이트 |
| `hideHighlight` | 열려 있는 하이라이트를 닫는다 |
| `scroll` | 해당 요소가 보이도록 스크롤 |
| `wait` | 순수 대기(ms) |
| `clearCaption` | 하단 자막 비우기 |
| (모든 스텝) `caption` | 그 스텝을 실행하기 직전에 하단 자막을 띄운다. 다음 자막이 나오거나 `clearCaption` 을 만나기 전까지 유지된다 |

시나리오는 위에서 아래로 순서대로 실행된다. 실제 화면을 시연할 때는 `baseUrl` 로 로컬/스테이징 서버 주소를
넣고, 서버 없이 정적 HTML만 보여줄 때는 `baseUrl` 을 생략하고 `goto`/`startUrl` 에 시나리오 파일 기준
상대 경로를 쓰면(자동으로 `file://` 로 해석됨) 된다.

## 연출을 더 추가하고 싶을 때

`scripts/inject.js` 에 `window.__demo` 에 새 함수를 추가하고, `scripts/run_scenario.js` 의
`runStep()` 에 그 함수를 호출하는 새 스텝 키를 하나 추가하면 된다. 커서 이동은 2차 베지어 곡선으로
`requestAnimationFrame` 애니메이션을 도는 방식이라(`inject.js` 의 `moveCursorTo`), 다른 연출도 같은
패턴(요소를 만들고, CSS 트랜지션/키프레임으로 움직이고, Node 쪽에서 `page.evaluate` 로 호출)을 따르면
자연스럽게 어울린다.

## 파일 구조

```
demo-video/
├── SKILL.md
├── package.json
├── scripts/
│   ├── setup.sh              최초 설치(npm install + playwright install)
│   ├── run_scenario.js       범용 러너 — YAML 을 읽어 Playwright 를 조작하고 mp4로 변환
│   ├── inject.js             브라우저 안에서 도는 커서·자막·하이라이트·챕터 카드 오버레이
│   └── fixture/
│       ├── demo-page.html        서버 없이 바로 테스트할 수 있는 정적 데모 페이지
│       └── example-scenario.yaml 위 페이지를 사용하는 예시 시나리오
└── out/                       결과 mp4·실패 스크린샷 (실행할 때 생성됨, git에는 커밋하지 않음)
```
