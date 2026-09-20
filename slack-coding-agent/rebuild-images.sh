#!/bin/bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# Rebuild and Push Docker Images for AMD64
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

# Configuration
REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}"
PLATFORM="linux/amd64"

if [ -z "$AWS_ACCOUNT" ]; then
  error "Could not determine AWS account ID. Set AWS_ACCOUNT_ID or configure AWS CLI."
fi

ECR_BASE="$AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

log "AWS Account: $AWS_ACCOUNT"
log "Region: $REGION"
log "Platform: $PLATFORM"

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
# STEP 2: BUILD SANDBOX IMAGE
# ═══════════════════════════════════════════════════════════════════════════
header "Step 2: Building Sandbox Image (slack-agent-sandbox)"

SANDBOX_REPO="$ECR_BASE/slack-agent-sandbox"
SANDBOX_TAG="latest"

log "Building sandbox image for $PLATFORM..."
docker buildx build \
  --platform "$PLATFORM" \
  --file Dockerfile \
  --tag "$SANDBOX_REPO:$SANDBOX_TAG" \
  --tag "$SANDBOX_REPO:$(date +%Y%m%d-%H%M%S)" \
  --push \
  . || error "Failed to build sandbox image"

success "Sandbox image built and pushed: $SANDBOX_REPO:$SANDBOX_TAG"

# ═══════════════════════════════════════════════════════════════════════════
# STEP 3: BUILD MAIN BOT IMAGE (optional, if different from sandbox)
# ═══════════════════════════════════════════════════════════════════════════
header "Step 3: Building Main Bot Image (slack-coding-agent)"

BOT_REPO="$ECR_BASE/slack-coding-agent"
BOT_TAG="latest"

log "Building bot image for $PLATFORM..."
docker buildx build \
  --platform "$PLATFORM" \
  --file Dockerfile \
  --tag "$BOT_REPO:$BOT_TAG" \
  --tag "$BOT_REPO:$(date +%Y%m%d-%H%M%S)" \
  --push \
  . || error "Failed to build bot image"

success "Bot image built and pushed: $BOT_REPO:$BOT_TAG"

# ═══════════════════════════════════════════════════════════════════════════
# STEP 4: VERIFY IMAGES
# ═══════════════════════════════════════════════════════════════════════════
header "Step 4: Verifying Images"

log "Checking slack-agent-sandbox..."
aws ecr describe-images \
  --repository-name slack-agent-sandbox \
  --image-ids imageTag=latest \
  --region "$REGION" \
  --query 'imageDetails[0].[imagePushedAt,imageSizeInBytes]' \
  --output table || warn "Could not verify sandbox image"

log "Checking slack-coding-agent..."
aws ecr describe-images \
  --repository-name slack-coding-agent \
  --image-ids imageTag=latest \
  --region "$REGION" \
  --query 'imageDetails[0].[imagePushedAt,imageSizeInBytes]' \
  --output table || warn "Could not verify bot image"

success "Images verified in ECR"

# ═══════════════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════════════
header "Build Complete"

echo ""
success "All images rebuilt and pushed successfully!"
echo ""
echo "Next steps:"
echo "  1. Force redeploy ECS services to use new images:"
echo "     aws ecs update-service --cluster SlackAgentCluster --service <service-name> --force-new-deployment --region $REGION"
echo ""
echo "  2. Or update Step Functions / Lambda to use new image tags"
echo ""
echo "  3. Test with a demo task in Slack"
echo ""
