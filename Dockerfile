# P-FM NEWS — 어느 컨테이너 호스트(Koyeb·Render·Fly·Railway·Cloud Run)에서도
# 그대로 도는 이미지. 리슨 포트는 플랫폼이 주입하는 PORT 를 따른다.
FROM python:3.12-slim

# readability-lxml 이 쓰는 lxml 은 slim 이미지에 휠이 있어 빌드 도구가 필요 없다.
# 없을 때만 컴파일하도록 최소 패키지만 둔다.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 의존성만 먼저 복사해 레이어 캐시를 살린다(코드만 바뀌면 재설치하지 않는다).
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY . .

# 스케줄 판단(야간 억제·주간 레포트·대외협력)은 이 값을 기준으로 한다.
# 컨테이너는 UTC 로 돌기 때문에 반드시 필요하다.
ENV APP_TZ_OFFSET=9 \
    API_HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# 헬스체크 — 플랫폼이 따로 프로브를 걸지 않을 때를 위한 기본값.
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=4)"

# run = API 서버 + 수집/시세/텔레그램봇/대외협력 스레드. 인스턴스는 반드시 1개.
CMD ["python", "backend/main.py", "run"]

# ── serve + worker 로 나눠 띄울 때 (2026-09-11, 권장 — 한쪽이 죽거나
#    재시작해도 다른 쪽은 안 끊긴다. 자세한 내용은 docs/RUNBOOK.md) ──────
# 이 이미지 그대로 컨테이너(ECS 태스크 등)를 2개 만들고 command 만 바꾼다:
#   서버 컨테이너: command = ["python", "backend/main.py", "serve"]
#                  → 위 HEALTHCHECK(=/healthz) 그대로 쓴다.
#   worker 컨테이너: command = ["python", "backend/main.py", "worker"]
#                  → 웹서버가 없어 위 HEALTHCHECK(/healthz)가 항상 실패한다.
#                    ECS 태스크 정의(또는 `docker run --health-cmd`)에서
#                    healthCheck 를 아래로 **반드시** 덮어써야 한다:
#                      command: ["CMD", "python", "backend/main.py", "healthcheck"]
#    둘 다 desiredCount=1 로 고정한다 — 수집 루프가 도는 프로세스가
#    동시에 2개 이상 뜨면 수집·알림이 중복된다(run 과 worker 도 동시 금지).
