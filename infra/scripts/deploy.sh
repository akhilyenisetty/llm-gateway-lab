#!/usr/bin/env bash
# Build both images, push them to ECR, and roll the ECS services onto them.
# Run from anywhere after `terraform apply`:  infra/scripts/deploy.sh
#
#   docker build (arm64) ─► push :latest to ECR ─► ECS starts new containers
#   ─► load balancer health checks pass ─► old containers drain and stop
set -euo pipefail

INFRA="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(dirname "$INFRA")"
out() { terraform -chdir="$INFRA" output -raw "$1"; }

REGION="$(out region)"
GATEWAY_REPO="$(out gateway_repository_url)"
DASHBOARD_REPO="$(out dashboard_repository_url)"
CLUSTER="$(out cluster_name)"
REGISTRY="${GATEWAY_REPO%%/*}"

echo "==> logging in to $REGISTRY"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

# Fargate runs ARM64 (Graviton) here; on an Apple Silicon Mac this builds natively.
echo "==> building and pushing the gateway"
docker buildx build --platform linux/arm64 -t "$GATEWAY_REPO:latest" --push "$ROOT"
echo "==> building and pushing the dashboard"
docker buildx build --platform linux/arm64 -t "$DASHBOARD_REPO:latest" --push "$ROOT/dashboard"

# Same tag, new image: ask ECS to start fresh containers, which pull it.
for service in "$(out gateway_service_name)" "$(out dashboard_service_name)"; do
  echo "==> redeploying $service"
  aws ecs update-service --region "$REGION" --cluster "$CLUSTER" --service "$service" \
    --force-new-deployment --query 'service.serviceName' --output text
done

echo "==> waiting for the services to be stable (a few minutes)"
aws ecs wait services-stable --region "$REGION" --cluster "$CLUSTER" \
  --services "$(out gateway_service_name)" "$(out dashboard_service_name)"
echo "==> done: $(out url)"
