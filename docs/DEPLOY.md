# AWS 배포 가이드 (ECS Fargate)

이 문서는 P-FM NEWS를 AWS에 올리는 실제 절차다. 사내 PC 운영은
[RUNBOOK.md](RUNBOOK.md)를 본다. 여기서는 **ECS Fargate**를 기본 경로로
안내한다 — 서버 관리가 없고(서버리스 컨테이너), 이미 이 프로젝트가
`serve`/`worker` 프로세스 분리·헬스체크·다중 인스턴스 안전장치를
ECS 배포 전제로 만들어 두었기 때문이다(§ 왜 Fargate인가 참고).

DB는 이미 Supabase(외부 클라우드)를 쓰고 있으므로 **AWS에 DB를 새로
준비할 필요가 없다** — 컨테이너는 완전히 무상태(stateless)다.

> **현재 실제 운영 방식 (2026-09-14)**: 아래 §1~§10은 ECS Fargate 기준
> 참고 가이드다. 실제로는 비용(ALB+Fargate 2태스크 ≈ 월 $40대)과 사내
> PC 네트워크 제약(로컬 Docker/WSL2 설치 불가) 때문에 더 저렴한 **EC2
> 단일 인스턴스**(§11) 방식으로 운영 중이다 — 이미지는 로컬 Docker
> 없이 **AWS CodeBuild**가 빌드해 ECR로 푸시하고, EC2(`i-0eb37f241ecbba234`,
> `3.38.148.194`)에서 `serve`+`worker` 컨테이너 2개를 직접 띄운다.
> 접속은 SSH 대신 **SSM(Session Manager)**을 쓴다(키 관리·포트 22
> 노출이 없다). 코드를 고친 뒤 재배포는 `deploy/redeploy.sh` 한 번이면
> 끝난다(CodeBuild 빌드 대기 → ECR 푸시 → EC2 pull·컨테이너 재시작까지
> 자동). 비밀값은 SSM Parameter Store(`pfm-news-env`, SecureString)에
> 저장해 두고 인스턴스가 시작할 때 `/opt/pfm-news/.env`로 받아 온다.

## 0. 왜 이 구조인가

- **컨테이너 2개, 인스턴스는 각 1개**: `serve`(API·웹) + `worker`(수집·텔레그램봇·대외협력).
  한쪽을 배포·재시작해도 다른 쪽은 안 끊긴다. `run` 단일 프로세스는 쓰지 않는다.
- **로드밸런서는 `serve`에만** 붙인다. `worker`는 웹서버가 없어 ALB 대상이 될 수 없다.
- **헬스체크가 서로 다르다**: `serve`는 `/healthz`(HTTP), `worker`는
  `python backend/main.py healthcheck`(컨테이너 CMD 헬스체크) — Dockerfile 주석 참고.
- **desiredCount는 항상 1**: 둘 다 2개 이상 뜨면 수집·알림이 중복된다.
  실수로 겹쳐도 `pipeline_lock_owner` DB 락이 안전판이지만, 의도적으로 늘리면 안 된다.
- **비밀 값은 Secrets Manager**로 관리한다 — `.env` 파일을 이미지에 굽지 않는다.

## 1. 사전 준비

- AWS 계정, `aws configure`로 CLI 인증 완료
- Docker Desktop (이미지 빌드용)
- Supabase 프로젝트 (이미 사용 중인 것 그대로 — `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`)
- 리전 하나 정하기 (예: `ap-northeast-2` 서울)

```bash
export AWS_REGION=ap-northeast-2
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

## 2. ECR에 이미지 푸시

```bash
aws ecr create-repository --repository-name pfm-news --region $AWS_REGION

aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

docker build -t pfm-news:latest .
docker tag pfm-news:latest $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/pfm-news:latest
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/pfm-news:latest
```

## 3. 비밀 값 등록 (Secrets Manager)

`.env`에 있는 값 중 **비밀값**만 Secrets Manager에 하나의 시크릿으로 등록한다
(모델명·간격 같은 비밀 아닌 값은 태스크 정의의 일반 `environment`로 넣는다).

```bash
aws secretsmanager create-secret --name pfm-news/env --region $AWS_REGION \
  --secret-string '{
    "OPENAI_API_KEY": "sk-...",
    "NVIDIA_EMBED_API_KEY": "nvapi-...",
    "NVIDIA_LLM_API_KEY": "nvapi-...",
    "SUPABASE_URL": "https://xxxx.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "eyJ...",
    "TELEGRAM_BOT_TOKEN": "123:AA...",
    "TELEGRAM_CHAT_ID": "-100...",
    "NAVER_CLIENT_ID": "...",
    "NAVER_CLIENT_SECRET": "...",
    "MASTER_PASSWORD": "...",
    "WEB_PASSWORD": "...",
    "SMTP_USER": "...",
    "SMTP_APP_PASSWORD": "...",
    "EA_KOTRA_SERVICE_KEY": "...",
    "LANGSMITH_API_KEY": "..."
  }'
```

값이 바뀌면 `aws secretsmanager update-secret`으로 갱신한 뒤, 해당 서비스만
`force-new-deployment`로 재시작하면 새 값을 읽는다(컨테이너가 시작할 때만 읽으므로).

## 4. 네트워크 (VPC)

가장 저렴한 구성은 **퍼블릭 서브넷 2개 + NAT 게이트웨이 없이** 태스크에
퍼블릭 IP를 직접 부여하는 것이다(이 프로젝트는 인바운드는 ALB만 받고,
아웃바운드는 언론사·API 호출뿐이라 NAT 없이도 동작한다 — NAT 게이트웨이는
시간당 요금 + 트래픽 요금이 붙어 이런 소규모 서비스엔 비용 대비 이득이 적다).

기존 VPC를 쓰거나 콘솔의 "VPC 및 관련 리소스 생성" 마법사로 퍼블릭 서브넷
2개짜리 VPC를 만든다. (완전히 새로 만든다면 콘솔 마법사가 CLI보다 빠르다.)

## 5. ALB — `serve`용 (worker는 붙이지 않는다)

```bash
# 보안 그룹: ALB는 443/80 인바운드 허용, ECS 태스크는 ALB에서만 8000 허용
aws ec2 create-security-group --group-name pfm-alb-sg --description "ALB" --vpc-id $VPC_ID
aws ec2 create-security-group --group-name pfm-task-sg --description "ECS task" --vpc-id $VPC_ID
aws ec2 authorize-security-group-ingress --group-id $ALB_SG --protocol tcp --port 443 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id $TASK_SG --protocol tcp --port 8000 --source-group $ALB_SG

aws elbv2 create-load-balancer --name pfm-alb --subnets $SUBNET_1 $SUBNET_2 \
  --security-groups $ALB_SG --scheme internet-facing --type application

aws elbv2 create-target-group --name pfm-serve-tg --protocol HTTP --port 8000 \
  --vpc-id $VPC_ID --target-type ip \
  --health-check-path /healthz --health-check-interval-seconds 30
```

HTTPS를 쓰려면 ACM에서 도메인 인증서를 발급받아 443 리스너에 붙인다(80은
443으로 리다이렉트). 사내에서만 쓰고 도메인이 없다면 80으로 시작해도 되지만,
`/api/web/login`·`/api/master/login`이 비밀번호를 주고받으므로 **가능하면
HTTPS를 반드시 쓴다** — 이미 만들어 둔 보안 헤더(HSTS는 미포함)와 로그인
잠금장치가 전송 구간 암호화까지 대신해 주지는 않는다.

## 6. ECS 클러스터·태스크 정의

```bash
aws ecs create-cluster --cluster-name pfm-news
```

태스크 정의는 **`serve`용과 `worker`용 두 개**를 만든다. 이미지는 같고
`command`와 헬스체크만 다르다.

`task-def-serve.json` (핵심 부분만 발췌):
```json
{
  "family": "pfm-news-serve",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256",
  "memory": "512",
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/ecsTaskExecutionRole",
  "containerDefinitions": [{
    "name": "serve",
    "image": "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/pfm-news:latest",
    "command": ["python", "backend/main.py", "serve"],
    "portMappings": [{"containerPort": 8000}],
    "environment": [
      {"name": "DB_BACKEND", "value": "supabase"},
      {"name": "APP_TZ_OFFSET", "value": "9"},
      {"name": "PORT", "value": "8000"},
      {"name": "LLM_MODEL", "value": "gpt-5.6-luna"},
      {"name": "EMBEDDING_MODEL", "value": "text-embedding-3-small"},
      {"name": "NVIDIA_EMBED_MODEL", "value": "nvidia/nemotron-3-embed-1b"},
      {"name": "NVIDIA_LLM_MODEL", "value": "google/gemma-4-31b-it"}
    ],
    "secrets": [
      {"name": "OPENAI_API_KEY", "valueFrom": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:pfm-news/env:OPENAI_API_KEY::"},
      {"name": "SUPABASE_URL", "valueFrom": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:pfm-news/env:SUPABASE_URL::"},
      {"name": "SUPABASE_SERVICE_ROLE_KEY", "valueFrom": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:pfm-news/env:SUPABASE_SERVICE_ROLE_KEY::"},
      {"name": "MASTER_PASSWORD", "valueFrom": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:pfm-news/env:MASTER_PASSWORD::"},
      {"name": "WEB_PASSWORD", "valueFrom": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:pfm-news/env:WEB_PASSWORD::"}
    ],
    "healthCheck": {
      "command": ["CMD-SHELL", "python -c \"import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4)\""],
      "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 40
    },
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/pfm-news-serve",
        "awslogs-region": "<REGION>",
        "awslogs-stream-prefix": "serve"
      }
    }
  }]
}
```

`task-def-worker.json`은 위에서 세 가지만 바꾼다:
- `"family": "pfm-news-worker"`
- `"command": ["python", "backend/main.py", "worker"]`, `portMappings` 제거
- `"healthCheck"`: `["CMD-SHELL", "python backend/main.py healthcheck"]` — **반드시 이걸로
  바꿔야 한다.** `serve`용 `/healthz` 헬스체크를 그대로 두면 `worker`엔 웹서버가
  없어 항상 실패 판정된다.
- `secrets`에 `TELEGRAM_BOT_TOKEN`·`TELEGRAM_CHAT_ID`·`NAVER_CLIENT_ID`·
  `NAVER_CLIENT_SECRET`·`EA_KOTRA_SERVICE_KEY`·`SMTP_USER`·`SMTP_APP_PASSWORD` 등
  수집·알림 쪽에 필요한 값들도 추가한다(공통 값은 두 태스크 정의 모두에 넣어도 무방).

CloudWatch 로그 그룹을 먼저 만든다:
```bash
aws logs create-log-group --log-group-name /ecs/pfm-news-serve
aws logs create-log-group --log-group-name /ecs/pfm-news-worker

aws ecs register-task-definition --cli-input-json file://task-def-serve.json
aws ecs register-task-definition --cli-input-json file://task-def-worker.json
```

## 7. 서비스 생성 (desiredCount=1, 둘 다)

```bash
# serve — ALB 타겟그룹 연결
aws ecs create-service \
  --cluster pfm-news --service-name pfm-serve \
  --task-definition pfm-news-serve --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1,$SUBNET_2],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}" \
  --load-balancers "targetGroupArn=$TG_ARN,containerName=serve,containerPort=8000"

# worker — 로드밸런서 없음
aws ecs create-service \
  --cluster pfm-news --service-name pfm-worker \
  --task-definition pfm-news-worker --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_1,$SUBNET_2],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}"
```

`assignPublicIp=ENABLED`는 NAT 없이 아웃바운드 인터넷(언론사 RSS·OpenAI·Supabase
호출)이 가능하게 하기 위함이다 — 4번의 "NAT 없이" 구성과 짝을 이룬다.

## 8. 배포 확인

```bash
aws elbv2 describe-target-health --target-group-arn $TG_ARN   # healthy 인지
curl -I http://<ALB DNS 이름>/                                # 200 + 보안 헤더 확인
aws ecs describe-services --cluster pfm-news --services pfm-serve pfm-worker \
  --query 'services[].{name:serviceName,running:runningCount,desired:desiredCount}'
```

worker가 healthy로 안 잡히면 CloudWatch 로그(`/ecs/pfm-news-worker`)에서
`수집 루프 시작` 로그가 찍혔는지 먼저 본다 — 찍혔으면 헬스체크 명령이
`serve`용(`/healthz`)으로 잘못 남아 있는 경우가 대부분이다(6번 참고).

## 9. 코드 업데이트 (재배포)

```bash
docker build -t pfm-news:latest .
docker tag pfm-news:latest $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/pfm-news:latest
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/pfm-news:latest

aws ecs update-service --cluster pfm-news --service pfm-serve --force-new-deployment
aws ecs update-service --cluster pfm-news --service pfm-worker --force-new-deployment
```

두 서비스를 **따로** 재배포할 수 있다는 것 자체가 serve/worker 분리의
핵심 이점이다 — 웹 UI만 고친 배포라면 `pfm-serve`만 재배포하면 `worker`는
수집을 계속한다.

## 10. 비용 개요 (서울 리전 기준, desiredCount=1×2 태스크, 256 CPU/512MB 상시 가정)

| 항목 | 대략 비용 |
|---|---|
| Fargate (2 태스크, 0.25 vCPU/0.5GB, 24시간) | 월 $18~25 수준 |
| ALB | 월 $16~20 + 트래픽 소량 |
| ECR·CloudWatch Logs·Secrets Manager | 월 $1~3 (소규모 로그·시크릿 1개 기준) |
| NAT 게이트웨이 | **미사용(4번 구성)** — 안 쓰면 월 $30+ 절약 |

가장 비용에 민감하다면 **§11 대안**을 본다.

## 11. 대안 — 비용을 더 낮추고 싶다면 (EC2 단일 인스턴스)

트래픽이 적고 ALB·Fargate 상시 비용도 아끼고 싶다면, `t4g.micro`(ARM, 프리티어
대상 가능) EC2 인스턴스 1대에 Docker Compose로 `serve`+`worker` 컨테이너
2개를 그냥 띄우는 방법도 있다 — ALB 없이 EC2의 퍼블릭 IP에 Elastic IP만
고정하고, `serve` 컨테이너 포트만 보안그룹에서 열면 된다. 대신:
- 인스턴스 자체가 죽으면(패치 재부팅 등) 둘 다 같이 죽는다 — Fargate의
  '컨테이너별 독립 재시작'이라는 장점을 포기하는 트레이드오프다.
- OS 패치·Docker 업데이트를 직접 관리해야 한다(Fargate는 이게 없다).
- 사내에서 이미 로컬 PC로 같은 구성(`serve`+`worker` 두 프로세스)을 운영해 본
  경험이 있으므로([RUNBOOK.md](RUNBOOK.md)), 이 방식이 심리적으로도 가장 익숙하다.

`docker-compose.yml` 예시:
```yaml
services:
  serve:
    image: <ECR 이미지>
    command: ["python", "backend/main.py", "serve"]
    ports: ["80:8000"]
    env_file: .env
    restart: unless-stopped
  worker:
    image: <ECR 이미지>
    command: ["python", "backend/main.py", "worker"]
    env_file: .env
    restart: unless-stopped
```
(이 경우 `.env` 파일을 인스턴스에 직접 올리게 되므로, 인스턴스 접근 권한을
꼭 필요한 사람에게만 주고 파일 권한을 `chmod 600 .env`로 제한한다.)

## 12. 배포 전 마지막 체크리스트

- [ ] `.env`의 `MASTER_PASSWORD`·`WEB_PASSWORD`가 로컬 개발용 약한 값이 아닌지
- [ ] `WEB_PASSWORD`가 비어 있지 않은지 (비어 있으면 사이트가 완전히 공개된다)
- [ ] HTTPS(ACM 인증서)를 붙였는지 — 로그인 비밀번호가 평문으로 오간다
- [ ] Supabase에 `pipeline_lock_owner`/`pipeline_lock_at` 컬럼이 추가돼 있는지
      (RUNBOOK.md § 다중 인스턴스 안전장치의 SQL)
- [ ] `serve`/`worker` 두 서비스 모두 desiredCount=1인지 (그 이상 X)
- [ ] `worker` 태스크의 헬스체크가 `/healthz`가 아니라 `healthcheck` 명령인지
- [ ] CloudWatch에서 실제 로그가 찍히는지(두 서비스 모두)
