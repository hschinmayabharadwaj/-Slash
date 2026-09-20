#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# Simple Rebuild and Push Docker Images (AMD64)
# ═══════════════════════════════════════════════════════════════════════════

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +%T)]${NC} $1"; }
success() { echo -e "${GREEN}✅ $1${NC}"; }
error() { echo -e "${RED}❌ $1${NC}"; exit 1; }
header() { echo -e "${CYAN}━━━ $1 ━━━${NC}"; }

# Configuration
REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}"

if [ -z "$AWS_ACCOUNT" ]; then
  error "Could not determine AWS account ID. Set AWS_ACCOUNT_ID or configure AWS CLI."
fi

ECR_BASE="$AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

log "AWS Account: $AWS_ACCOUNT"
log "Region: $REGION"

# ═══════════════════════════════════════════════════════════════════════════
# STEP 1: ECR LOGIN
# ═══════════════════════════════════════════════════════════════════════════
header "Step 1: ECR Login"

log "Logging into Amazon ECR..."
aws ecr get-login-password --region "$REGION" | \
  docker login --username AWS --password-stdin "$ECR_BASE" || \
  error "Failed to login to ECR"

success "Logged into ECR"

# ═══════════════════════════════════════════════════════════════════════════
# STEP 2: BUILD AND PUSH SANDBOX IMAGE
# ═══════════════════════════════════════════════════════════════════════════
header "Step 2: Building Sandbox Image"

SANDBOX_REPO="$ECR_BASE/slack-agent-sandbox"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

log "Building slack-agent-sandbox..."
docker build \
  --file Dockerfile \
  --tag "$SANDBOX_REPO:latest" \
  --tag "$SANDBOX_REPO:$TIMESTAMP" \
  . || error "Failed to build sandbox image"

log "Pushing slack-agent-sandbox:latest..."
docker push "$SANDBOX_REPO:latest" || error "Failed to push sandbox:latest"

log "Pushing slack-agent-sandbox:$TIMESTAMP..."
docker push "$SANDBOX_REPO:$TIMESTAMP" || error "Failed to push sandbox:$TIMESTAMP"

success "Sandbox image pushed: $SANDBOX_REPO:latest"

# ═══════════════════════════════════════════════════════════════════════════
# STEP 3: BUILD AND PUSH MAIN BOT IMAGE
# ═══════════════════════════════════════════════════════════════════════════
header "Step 3: Building Main Bot Image"

BOT_REPO="$ECR_BASE/slack-coding-agent"

log "Building slack-coding-agent..."
docker build \
  --file Dockerfile \
  --tag "$BOT_REPO:latest" \
  --tag "$BOT_REPO:$TIMESTAMP" \
  . || error "Failed to build bot image"

log "Pushing slack-coding-agent:latest..."
docker push "$BOT_REPO:latest" || error "Failed to push bot:latest"

log "Pushing slack-coding-agent:$TIMESTAMP..."
docker push "$BOT_REPO:$TIMESTAMP" || error "Failed to push bot:$TIMESTAMP"

success "Bot image pushed: $BOT_REPO:latest"

# ═══════════════════════════════════════════════════════════════════════════
# STEP 4: VERIFY IMAGES
# ═══════════════════════════════════════════════════════════════════════════
header "Step 4: Verifying Images"

log "Verifying slack-agent-sandbox..."
aws ecr describe-images \
  --repository-name slack-agent-sandbox \
  --image-ids imageTag=latest \
  --region "$REGION" \
  --query 'imageDetails[0].[imagePushedAt,imageSizeInBytes]' \
  --output table

log "Verifying slack-coding-agent..."
aws ecr describe-images \
  --repository-name slack-coding-agent \
  --image-ids imageTag=latest \
  --region "$REGION" \
  --query 'imageDetails[0].[imagePushedAt,imageSizeInBytes]' \
  --output table

success "Images verified in ECR"

# ═══════════════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════════════
header "Build Complete"

echo ""
success "All images rebuilt and pushed successfully!"
echo ""
echo "Image tags:"
echo "  - slack-agent-sandbox:latest"
echo "  - slack-agent-sandbox:$TIMESTAMP"
echo "  - slack-coding-agent:latest"
echo "  - slack-coding-agent:$TIMESTAMP"
echo ""
echo "Next steps:"
echo "  1. Redeploy ECS services:"
echo "     aws ecs update-service --cluster SlackAgentCluster --service <worker-service> --force-new-deployment --region $REGION"
echo ""
echo "  2. Or restart local worker to test:"
echo "     cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent"
echo "     source venv/bin/activate"
echo "     python -m slackagent.worker"
echo ""
echo "  3. Test with a demo task in Slack"
echo ""
