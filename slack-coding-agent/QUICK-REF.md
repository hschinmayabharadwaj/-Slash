# Quick Reference: Cryptography Fix

## What Was Done

### 1. ✅ Updated Dockerfile
- Added build dependencies: `gcc`, `g++`, `libffi-dev`, `libssl-dev`, `python3-dev`, `cargo`, `rustc`
- Added `cryptography>=41.0.0` to pip install

### 2. 📝 Created Scripts
- `rebuild-simple.sh` - Simple rebuild and push to ECR
- `rebuild-images.sh` - BuildX multi-platform rebuild
- `test-cryptography.py` - Verification test script
- `CRYPTOGRAPHY-FIX.md` - Detailed guide

## Quick Commands

### Rebuild Images
```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
./rebuild-simple.sh
```

### Test Locally
```bash
# Test in current environment
python test-cryptography.py

# Test in Docker container
docker run --rm \
  $(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com/slack-agent-sandbox:latest \
  python -c "import cryptography; print('✅ OK')"
```

### Redeploy Worker (ECS)
```bash
aws ecs update-service \
  --cluster SlackAgentCluster \
  --service <worker-service-name> \
  --force-new-deployment \
  --region us-east-1
```

### Redeploy Worker (Local)
```bash
pkill -f slackagent.worker
source venv/bin/activate
python -m slackagent.worker &
```

### Test End-to-End
In Slack:
```
@codingbot owner/repo Add a comment to README
```

## Files Changed
- ✅ `Dockerfile` - Added cryptography and build deps
- ✅ `rebuild-simple.sh` - New rebuild script
- ✅ `rebuild-images.sh` - New buildx script  
- ✅ `test-cryptography.py` - New test script
- ✅ `CRYPTOGRAPHY-FIX.md` - Detailed guide
- ✅ `QUICK-REF.md` - This file

## Verification Checklist
- [ ] Run `./rebuild-simple.sh` successfully
- [ ] Images appear in ECR with recent timestamp
- [ ] Run `python test-cryptography.py` - all tests pass
- [ ] Worker redeployed/restarted
- [ ] Test task in Slack completes without errors
- [ ] GitHub PR created successfully

## Estimated Time
- Rebuild: 5-10 minutes
- Deploy: 2-5 minutes
- Test: 2 minutes
- **Total: ~15-20 minutes**

## Next Steps
1. Run `./rebuild-simple.sh`
2. Redeploy worker
3. Test in Slack
4. Record demo video
