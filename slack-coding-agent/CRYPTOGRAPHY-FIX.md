# Cryptography Fix - Rebuild and Redeploy Guide

## Problem
The Docker images are missing the `cryptography` Python package, which is required for GitHub JWT authentication (PyJWT with RSA keys).

## Solution Overview
1. ✅ Add cryptography to Dockerfile with build dependencies
2. ⏳ Rebuild Docker images for AMD64 architecture
3. ⏳ Push images to Amazon ECR
4. ⏳ Redeploy worker to use new images
5. ⏳ Test end-to-end demo task

---

## Step 1: Verify Dockerfile Changes

The Dockerfile has been updated with:

**Added build dependencies:**
```dockerfile
# Install git, basic tools, and build dependencies for cryptography
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    python3-dev \
    cargo \
    rustc \
    && rm -rf /var/lib/apt/lists/*
```

**Added cryptography package:**
```dockerfile
RUN pip install --no-cache-dir \
    boto3 \
    pyyaml \
    PyJWT \
    cryptography>=41.0.0 \  # <-- ADDED
    requests \
    pytest \
    black \
    ruff \
    mypy \
    pylint
```

Check the changes:
```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
git diff Dockerfile
```

---

## Step 2: Rebuild and Push Docker Images

### Option A: Simple Build (Recommended)

Use the simple rebuild script:

```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent

# Set AWS region (optional, defaults to us-east-1)
export AWS_REGION="us-east-1"

# Run the rebuild script
./rebuild-simple.sh
```

This will:
- Login to Amazon ECR
- Build both images (`slack-agent-sandbox` and `slack-coding-agent`)
- Push with `latest` tag and timestamped tag
- Verify images in ECR

**Expected time:** 5-10 minutes depending on network speed

### Option B: BuildX Multi-Platform (Advanced)

If you need explicit AMD64 platform targeting:

```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent

# Create buildx builder (first time only)
docker buildx create --name multiarch --use
docker buildx inspect --bootstrap

# Set AWS region
export AWS_REGION="us-east-1"

# Run the buildx rebuild script
./rebuild-images.sh
```

### Option C: Manual Build

```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent

# Set variables
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION="us-east-1"
ECR_BASE="$AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

# ECR login
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ECR_BASE

# Build sandbox image
docker build -f Dockerfile \
  -t $ECR_BASE/slack-agent-sandbox:latest \
  .

# Push sandbox image
docker push $ECR_BASE/slack-agent-sandbox:latest

# Build bot image (same Dockerfile)
docker build -f Dockerfile \
  -t $ECR_BASE/slack-coding-agent:latest \
  .

# Push bot image
docker push $ECR_BASE/slack-coding-agent:latest
```

---

## Step 3: Verify Images in ECR

Check that the new images are in ECR:

```bash
# Check sandbox image
aws ecr describe-images \
  --repository-name slack-agent-sandbox \
  --image-ids imageTag=latest \
  --region us-east-1 \
  --query 'imageDetails[0].[imagePushedAt,imageSizeInBytes,imageDigest]' \
  --output table

# Check bot image
aws ecr describe-images \
  --repository-name slack-coding-agent \
  --image-ids imageTag=latest \
  --region us-east-1 \
  --query 'imageDetails[0].[imagePushedAt,imageSizeInBytes,imageDigest]' \
  --output table
```

You should see timestamps from within the last few minutes.

---

## Step 4: Redeploy Worker

### Option A: ECS Fargate (if deployed to AWS)

Force ECS to pull new images:

```bash
REGION="us-east-1"
CLUSTER="SlackAgentCluster"

# List services
aws ecs list-services --cluster $CLUSTER --region $REGION

# Find the worker service name (something like SlackAgentWorkerService)
SERVICE_NAME="<your-worker-service-name>"

# Force new deployment
aws ecs update-service \
  --cluster $CLUSTER \
  --service $SERVICE_NAME \
  --force-new-deployment \
  --region $REGION

# Monitor deployment
aws ecs describe-services \
  --cluster $CLUSTER \
  --services $SERVICE_NAME \
  --region $REGION \
  --query 'services[0].deployments' \
  --output table

# Wait for status to be PRIMARY with desiredCount = runningCount
```

### Option B: Local Worker (for testing)

If running the worker locally:

```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent

# Pull the new image locally
AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION="us-east-1"
docker pull $AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com/slack-agent-sandbox:latest

# Stop existing worker
pkill -f "slackagent.worker" || true

# Restart worker
source venv/bin/activate
python -m slackagent.worker &

# Monitor logs
tail -f logs/worker.log  # if logging to file
# OR just watch stdout
```

### Option C: Update Task Definition (if using specific versions)

If your ECS task definition uses a specific image tag:

```bash
# Get current task definition
TASK_DEF_FAMILY="SlackAgentWorkerTask"
aws ecs describe-task-definition \
  --task-definition $TASK_DEF_FAMILY \
  --region us-east-1 \
  > task-def.json

# Edit task-def.json to update image URIs to :latest

# Register new revision
aws ecs register-task-definition \
  --cli-input-json file://task-def.json \
  --region us-east-1

# Update service to use new revision
aws ecs update-service \
  --cluster SlackAgentCluster \
  --service <service-name> \
  --task-definition $TASK_DEF_FAMILY \
  --region us-east-1
```

---

## Step 5: Test End-to-End

### Test 1: Verify Cryptography Import

Test that cryptography is installed in the container:

```bash
# Run a test container locally
docker run --rm \
  $AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com/slack-agent-sandbox:latest \
  python -c "import cryptography; print(f'cryptography {cryptography.__version__} installed successfully')"
```

Expected output:
```
cryptography 41.x.x installed successfully
```

### Test 2: Verify PyJWT with RSA

Test JWT signing (which requires cryptography):

```bash
docker run --rm \
  $AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com/slack-agent-sandbox:latest \
  python -c "
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Generate test RSA key
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)

# Sign a test token
payload = {'test': 'data'}
token = jwt.encode(payload, private_pem, algorithm='RS256')
print(f'✅ JWT signing with RSA works! Token: {token[:50]}...')
"
```

Expected output:
```
✅ JWT signing with RSA works! Token: eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...
```

### Test 3: Submit Demo Task in Slack

In your Slack workspace, send a test message:

```
@codingbot test-org/test-repo Add a simple comment to README.md
```

**Expected flow:**
1. ✅ Bot acknowledges task
2. ✅ Planning phase completes (no cryptography errors)
3. ✅ Implementation phase completes
4. ✅ GitHub PR is created successfully

**Monitor logs:**
```bash
# ECS logs
aws logs tail /aws/ecs/SlackAgentWorker --follow --region us-east-1

# Local logs
tail -f logs/worker.log
```

**Success indicators:**
- No `ModuleNotFoundError: No module named 'cryptography'` errors
- GitHub JWT authentication succeeds
- Branch and PR creation work properly

---

## Troubleshooting

### Issue: "Cannot find cryptography module"

**Solution:** Rebuild the image with build dependencies:
```bash
# Ensure Dockerfile has gcc, libssl-dev, cargo, rustc
# Re-run rebuild script
./rebuild-simple.sh
```

### Issue: "Docker build fails with cargo error"

**Solution:** The base image may need Rust for cryptography's native extensions. Add to Dockerfile:
```dockerfile
RUN apt-get update && apt-get install -y cargo rustc
```

### Issue: "ECS task won't pull new image"

**Solution:** Force stop existing tasks:
```bash
# List tasks
aws ecs list-tasks --cluster SlackAgentCluster --region us-east-1

# Stop task
aws ecs stop-task --cluster SlackAgentCluster --task <task-arn> --region us-east-1

# ECS will start new task with fresh image pull
```

### Issue: "Image size too large"

The cryptography package adds ~50MB due to native libraries. This is expected.

**Check image size:**
```bash
docker images | grep slack-agent-sandbox
```

Typical size: 400-600MB (was 300-400MB before cryptography)

### Issue: "Local Docker build works but ECR push fails"

**Solution:** Check ECR repository exists:
```bash
aws ecr describe-repositories --region us-east-1 | grep slack-agent-sandbox

# If missing, create it:
aws ecr create-repository \
  --repository-name slack-agent-sandbox \
  --image-scanning-configuration scanOnPush=true \
  --region us-east-1
```

---

## Verification Checklist

- [ ] Dockerfile updated with cryptography>=41.0.0
- [ ] Build dependencies (gcc, libssl-dev, cargo, rustc) added to Dockerfile
- [ ] Docker images rebuilt successfully
- [ ] Images pushed to ECR with `latest` tag
- [ ] ECR images have recent timestamp (within last hour)
- [ ] Worker service redeployed / restarted
- [ ] Container starts without import errors
- [ ] JWT signing test passes
- [ ] End-to-end Slack demo task completes successfully
- [ ] GitHub PR created without authentication errors

---

## Quick Reference Commands

```bash
# Rebuild everything
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
./rebuild-simple.sh

# Force ECS redeploy
aws ecs update-service --cluster SlackAgentCluster --service <service> --force-new-deployment --region us-east-1

# Test container locally
docker run --rm $(aws ecr describe-repositories --repository-names slack-agent-sandbox --query 'repositories[0].repositoryUri' --output text):latest python -c "import cryptography; print('OK')"

# Restart local worker
pkill -f slackagent.worker; source venv/bin/activate; python -m slackagent.worker &

# Monitor logs
aws logs tail /aws/ecs/SlackAgentWorker --follow
```

---

## Summary

The fix is complete when:
1. ✅ Dockerfile includes cryptography package and build dependencies
2. ✅ New Docker images are built and pushed to ECR
3. ✅ Worker is redeployed to use new images
4. ✅ Demo task completes without cryptography import errors
5. ✅ GitHub PR is successfully created

**Estimated total time:** 15-20 minutes
