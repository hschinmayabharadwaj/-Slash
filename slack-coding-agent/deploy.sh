#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# Slack Coding Agent — AWS Deployment Script
# ═══════════════════════════════════════════════════════════════════════════

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +%T)]${NC} $1"; }
success() { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
error() { echo -e "${RED}❌ $1${NC}"; exit 1; }
header() { echo -e "${CYAN}━━━ $1 ━━━${NC}"; }

# Banner
cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════╗
║                                                                   ║
║      ____  _            _      ____          _                   ║
║     / ___|| | __ _  ___| | __ / ___|___   __| | ___ _ __         ║
║     \___ \| |/ _` |/ __| |/ / | |   / _ \ / _` |/ _ \ '__|       ║
║      ___) | | (_| | (__|   <  | |__| (_) | (_| |  __/ |          ║
║     |____/|_|\__,_|\___|_|\_\  \____\___/ \__,_|\___|_|          ║
║                                                                   ║
║                     🤖 AWS Deployment v1.0                        ║
║                                                                   ║
╚═══════════════════════════════════════════════════════════════════╝
EOF

# Parse arguments
REGION="${AWS_REGION:-us-east-1}"
ENV="${DEPLOY_ENV:-prod}"
SKIP_BUILD=false
SKIP_INFRA=false
SKIP_DASHBOARD=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --region) REGION="$2"; shift 2 ;;
    --env) ENV="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --skip-infra) SKIP_INFRA=true; shift ;;
    --skip-dashboard) SKIP_DASHBOARD=true; shift ;;
    *) error "Unknown option: $1" ;;
  esac
done

export AWS_REGION="$REGION"

log "Region: $REGION | Environment: $ENV"

# ═══════════════════════════════════════════════════════════════════════════
# PREFLIGHT CHECKS
# ═══════════════════════════════════════════════════════════════════════════
header "Preflight Checks"

command -v aws >/dev/null 2>&1 || error "AWS CLI not found. Install: https://aws.amazon.com/cli/"
command -v docker >/dev/null 2>&1 || error "Docker not found. Install: https://docker.com/"
command -v node >/dev/null 2>&1 || error "Node.js not found. Install: https://nodejs.org/"
command -v python3 >/dev/null 2>&1 || error "Python 3 not found."
command -v cdk >/dev/null 2>&1 || { warn "CDK CLI not found. Installing..."; npm install -g aws-cdk; }

docker info >/dev/null 2>&1 || error "Docker daemon not running. Start Docker Desktop."

success "All prerequisites installed"

# Check AWS credentials
log "Checking AWS credentials..."
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || error "AWS credentials not configured"
AWS_IDENTITY=$(aws sts get-caller-identity --query Arn --output text)
success "AWS Account: $AWS_ACCOUNT"
success "Identity: $AWS_IDENTITY"

export CDK_DEFAULT_ACCOUNT="$AWS_ACCOUNT"
export CDK_DEFAULT_REGION="$REGION"

# ═══════════════════════════════════════════════════════════════════════════
# STEP 1: BUILD & PUSH DOCKER IMAGES TO ECR
# ═══════════════════════════════════════════════════════════════════════════
if [ "$SKIP_BUILD" = false ]; then
  header "Step 1: Building Docker Images"

  # Create ECR repositories if they don't exist
  for REPO in slack-coding-agent slack-agent-sandbox; do
    aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" 2>/dev/null || {
      log "Creating ECR repository: $REPO"
      aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
        --image-scanning-configuration scanOnPush=true >/dev/null
    }
  done

  # ECR login
  log "Logging into ECR..."
  aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
  success "ECR login successful"

  # ECS Fargate task definitions in this project use Linux ARM64.
  # Always build for that platform explicitly: a locally built image can carry
  # incorrect architecture metadata when it is built on a different host.
  # Build and push the image directly so ECR receives the required amd64
  # manifest rather than a host-architecture-specific local image.
  ECR_REGISTRY="$AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

  # Build bot image
  log "Building slack-coding-agent image..."
  docker buildx build --platform linux/arm64 --push \
    -t "$ECR_REGISTRY/slack-coding-agent:latest" -f Dockerfile .
  success "Bot image pushed"

  # Build sandbox image
  log "Building slack-agent-sandbox image..."
  docker buildx build --platform linux/arm64 --push \
    -t "$ECR_REGISTRY/slack-agent-sandbox:latest" -f Dockerfile.sandbox . || {
    warn "Dockerfile.sandbox not found, using main Dockerfile"
    docker buildx build --platform linux/arm64 --push \
      -t "$ECR_REGISTRY/slack-agent-sandbox:latest" -f Dockerfile .
  }
  success "Sandbox image pushed"
else
  warn "Skipping Docker build (--skip-build)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# STEP 2: BUILD THE DASHBOARD SPA (bundled into the site Lambda)
# ═══════════════════════════════════════════════════════════════════════════
# The React app is built with REACT_APP_SAME_ORIGIN=true and copied into
# infrastructure/lambda/dashboard_api/spa/ so the HTTP API + site Lambda can
# serve it from the same https origin (CloudFront / S3 / App Runner removed).
# This MUST run before cdk synth because api-stack.ts fails without spa/index.html.
if [ "$SKIP_DASHBOARD" = false ] && [ -d "dashboard" ]; then
  header "Step 2: Building Dashboard SPA (same-origin, into site Lambda)"

  cd infrastructure
  npm run build:spa
  cd ..

  if [ ! -f "infrastructure/lambda/dashboard_api/spa/index.html" ]; then
    error "SPA build produced no index.html — dashboard will not be served"
  fi
  success "Dashboard SPA built and bundled into site Lambda"
else
  [ "$SKIP_DASHBOARD" = true ] && warn "Skipping dashboard SPA build (--skip-dashboard)"
  [ ! -d "dashboard" ] && warn "Dashboard directory not found"
fi

# ═══════════════════════════════════════════════════════════════════════════
# STEP 3: DEPLOY CDK INFRASTRUCTURE
# ═══════════════════════════════════════════════════════════════════════════
if [ "$SKIP_INFRA" = false ]; then
  header "Step 3: Deploying CDK Infrastructure"

  cd infrastructure

  log "Installing CDK dependencies..."
  npm ci

  log "Building TypeScript..."
  npm run build

  log "Bootstrapping CDK (if needed)..."
  cdk bootstrap aws://$AWS_ACCOUNT/$REGION --region "$REGION" || warn "CDK bootstrap skipped (already done)"

  log "Synthesizing CloudFormation templates..."
  cdk synth

  log "Deploying all stacks..."
  cdk deploy --all --require-approval never --outputs-file ../cdk-outputs.json

  cd ..
  success "CDK infrastructure deployed"

  if [ -f "cdk-outputs.json" ]; then
    log "CDK Outputs saved to cdk-outputs.json"
  fi
else
  warn "Skipping CDK deployment (--skip-infra)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# STEP 4: CREATE SECRETS IN SECRETS MANAGER
# ═══════════════════════════════════════════════════════════════════════════
header "Step 4: Verifying Secrets"

SECRETS=(
  "slack-agent/SLACK_BOT_TOKEN"
  "slack-agent/SLACK_APP_TOKEN"
  "slack-agent/SLACK_SIGNING_SECRET"
  "slack-agent/GITHUB_APP_ID"
  "slack-agent/GITHUB_PRIVATE_KEY"
  "slack-agent/GITHUB_INSTALLATION_ID"
  "slack-agent/GEMINI_API_KEY"
)

for SECRET in "${SECRETS[@]}"; do
  aws secretsmanager describe-secret --secret-id "$SECRET" --region "$REGION" >/dev/null 2>&1 && {
    success "Secret exists: $SECRET"
  } || {
    warn "Secret NOT found: $SECRET"
    echo "To create it: aws secretsmanager create-secret --name $SECRET --secret-string 'YOUR_VALUE' --region $REGION"
  }
done

# ═══════════════════════════════════════════════════════════════════════════
# STEP 5: VERIFY DEPLOYMENT
# ═══════════════════════════════════════════════════════════════════════════
header "Step 5: Verifying Deployment"

# Check ECS services
log "Checking ECS services..."
CLUSTER_NAME="SlackAgentCluster"
for SERVICE in slack-agent-bot slack-agent-worker; do
  STATUS=$(aws ecs describe-services --cluster "$CLUSTER_NAME" --services "$SERVICE" \
    --query 'services[0].status' --output text --region "$REGION" 2>/dev/null || echo "NOT_FOUND")
  if [ "$STATUS" = "ACTIVE" ]; then
    success "ECS Service $SERVICE: ACTIVE"
  else
    warn "ECS Service $SERVICE: $STATUS"
  fi
done

# Check API Gateway
API_URL=$(jq -r '.SlackAgentApi.ApiUrl // empty' cdk-outputs.json 2>/dev/null)
if [ -n "$API_URL" ]; then
  log "Testing API Gateway health endpoint..."
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "${API_URL}health")
  if [ "$HTTP_CODE" = "200" ]; then
    success "API Gateway is healthy: $API_URL"
  else
    warn "API Gateway returned HTTP $HTTP_CODE"
  fi
fi

# Check the HTTPS dashboard (HTTP API + site Lambda)
DASHBOARD_URL=$(jq -r '.SlackAgentApi.DashboardUrl // empty' cdk-outputs.json 2>/dev/null)
if [ -n "$DASHBOARD_URL" ]; then
  log "Testing dashboard at $DASHBOARD_URL ..."
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "${DASHBOARD_URL}")
  if [ "$HTTP_CODE" = "200" ]; then
    success "Dashboard is healthy: $DASHBOARD_URL"
  else
    warn "Dashboard returned HTTP $HTTP_CODE (function may still be warming up)"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# DEPLOYMENT SUMMARY
# ═══════════════════════════════════════════════════════════════════════════
header "🎉 Deployment Complete!"

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}                   SERVICE ENDPOINTS                        ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"

if [ -f "cdk-outputs.json" ]; then
  DASHBOARD_URL=$(jq -r '.SlackAgentApi.DashboardUrl // empty' cdk-outputs.json)
  API_URL=$(jq -r '.SlackAgentApi.ApiUrl // empty' cdk-outputs.json)
  WEBHOOK_URL=$(jq -r '.SlackAgentApi.WebhookUrl // empty' cdk-outputs.json)

  [ -n "$DASHBOARD_URL" ] && echo -e "${BLUE}🌐 Dashboard (HTTPS):${NC} $DASHBOARD_URL"
  [ -n "$API_URL" ] && echo -e "${BLUE}🔌 API Gateway:${NC} $API_URL"
  [ -n "$WEBHOOK_URL" ] && echo -e "${BLUE}🪝 Slack Webhook:${NC} $WEBHOOK_URL"

  echo ""
  echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
  echo -e "${YELLOW}📋 Next Steps:${NC}"
  echo ""
  echo "1. Configure Slack webhook URL in your Slack App:"
  echo "   ${WEBHOOK_URL}"
  echo ""
  echo "2. Create a Cognito user for dashboard access:"
  echo "   aws cognito-idp admin-create-user --user-pool-id <POOL_ID> --username admin@example.com"
  echo ""
  echo "3. View CloudWatch dashboard:"
  echo "   https://console.aws.amazon.com/cloudwatch/home#dashboards:name=SlackAgentOperations"
  echo ""
  echo "4. Monitor ECS services:"
  echo "   https://console.aws.amazon.com/ecs/v2/clusters/SlackAgentCluster"
  echo ""
  echo -e "${GREEN}✅ All services deployed successfully!${NC}"
else
  warn "CDK outputs file not found — service URLs unavailable"
fi

echo ""
