#!/usr/bin/env bash
# Run the database migrations once, as a one-off ECS task, and wait for the result.
# Needed only with gateway_count > 1 (a single gateway migrates as it starts). Run it after
# pushing new images and before the containers switch over (deploy.sh, then this, is safe
# for the additive migrations this project has).
set -euo pipefail

INFRA="$(cd "$(dirname "$0")/.." && pwd)"
out() { terraform -chdir="$INFRA" output -raw "$1"; }
REGION="$(out region)"
CLUSTER="$(out cluster_name)"
SUBNETS="$(terraform -chdir="$INFRA" output -json private_subnet_ids | tr -d '[]" ')"

TASK_ARN="$(aws ecs run-task --region "$REGION" --cluster "$CLUSTER" \
  --task-definition "$(out gateway_task_definition)" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$(out gateway_security_group_id)],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"gateway","command":["alembic","upgrade","head"]}]}' \
  --query 'tasks[0].taskArn' --output text)"
echo "==> migration task started: ${TASK_ARN##*/}"

aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"
CODE="$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)"
echo "==> exit code $CODE (logs: CloudWatch group $(out gateway_log_group))"
[ "$CODE" = "0" ]
