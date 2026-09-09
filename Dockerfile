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
