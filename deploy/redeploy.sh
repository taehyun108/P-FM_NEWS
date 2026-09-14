#!/usr/bin/env bash
# P-FM NEWS 재배포 스크립트 (2026-09-14)
#
# 코드를 고친 뒤 이 스크립트 하나로 "새 이미지 빌드 → ECR 푸시 → EC2에서
# pull 후 컨테이너 재시작"까지 끝낸다. 로컬에 Docker가 없어도 된다 —
# 이미지 빌드는 AWS CodeBuild에서 일어난다(docs/DEPLOY.md 참고).
#
# 사전 조건: aws configure 로 자격증명이 연결돼 있어야 한다.
set -euo pipefail

AWS_REGION="ap-northeast-2"
PROJECT="pfm-news-build"
INSTANCE_ID="i-0eb37f241ecbba234"
ECR_URI="685173641556.dkr.ecr.${AWS_REGION}.amazonaws.com/pfm-news"

echo "[1/3] CodeBuild로 이미지 빌드·ECR 푸시 시작..."
BUILD_ID=$(aws codebuild start-build --project-name "$PROJECT" --region "$AWS_REGION" \
  --query 'build.id' --output text)
echo "빌드 ID: $BUILD_ID"

echo "[2/3] 빌드 완료 대기 중..."
while true; do
  STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$AWS_REGION" \
    --query 'builds[0].buildStatus' --output text)
  if [ "$STATUS" != "IN_PROGRESS" ]; then
    break
  fi
  sleep 10
done

if [ "$STATUS" != "SUCCEEDED" ]; then
  echo "빌드 실패: $STATUS — CodeBuild 콘솔에서 로그를 확인하세요."
  exit 1
fi
echo "빌드 성공."

echo "[3/3] EC2에 새 이미지 배포 중..."
CMD_ID=$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[
    \"aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin ${ECR_URI%/*}\",
    \"docker pull $ECR_URI:latest\",
    \"docker rm -f pfm-serve pfm-worker 2>/dev/null || true\",
    \"docker run -d --name pfm-serve --restart unless-stopped -p 80:8000 --env-file /opt/pfm-news/.env $ECR_URI:latest python backend/main.py serve\",
    \"docker run -d --name pfm-worker --restart unless-stopped --env-file /opt/pfm-news/.env $ECR_URI:latest python backend/main.py worker\",
    \"sleep 5\",
    \"docker ps\"
  ]" \
  --region "$AWS_REGION" --query 'Command.CommandId' --output text)

while true; do
  STATUS=$(aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --region "$AWS_REGION" --query 'Status' --output text 2>/dev/null || echo "InProgress")
  if [ "$STATUS" != "InProgress" ] && [ "$STATUS" != "Pending" ]; then
    break
  fi
  sleep 10
done

if [ "$STATUS" != "Success" ]; then
  echo "재시작 실패: $STATUS"
  aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --region "$AWS_REGION" --query 'StandardErrorContent' --output text
  exit 1
fi

echo "배포 완료. http://3.38.148.194/ 에서 확인하세요."
