# Complete Guide: Getting the Bot to Create GitHub PRs

## Prerequisites Checklist

Before the bot can create PRs, you need:

### ✅ 1. GitHub App Setup
- [ ] GitHub App created with correct permissions
- [ ] GitHub App installed on your repositories
- [ ] GitHub App private key (.pem file) available
- [ ] GitHub App ID and Installation ID known

### ✅ 2. Slack App Setup
- [ ] Slack App created
- [ ] Bot User OAuth Token (xoxb-...) obtained
- [ ] App Token (xapp-...) obtained for Socket Mode
- [ ] Slack Signing Secret obtained
- [ ] Bot invited to Slack channels

### ✅ 3. Configuration
- [ ] config.yaml file properly configured
- [ ] Environment variables set
- [ ] Bot and worker running (locally or on ECS)

### ✅ 4. Test Repository
- [ ] A GitHub repository accessible by the GitHub App
- [ ] Repository has at least one file (e.g., README.md)

---

## Step-by-Step Process

### Step 1: Verify Your GitHub App Configuration

Check your GitHub App settings at: https://github.com/settings/apps

**Required Permissions:**
- ✅ **Repository contents:** Read & Write
- ✅ **Pull requests:** Read & Write
- ✅ **Metadata:** Read-only (automatic)

**Required Events (optional but useful):**
- Pull request
- Pull request review
- Pull request review comment

**Check Installation:**
1. Go to: https://github.com/settings/installations
2. Find your Slack Coding Agent app
3. Ensure it's installed on the repository you want to test
4. Note which repositories have access

---

### Step 2: Verify Local Configuration

Check your config.yaml:

```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
cat config.yaml
```

**Required values:**

```yaml
slack:
  bot_token: xoxb-your-token-here        # Must start with xoxb-
  app_token: xapp-your-token-here        # Must start with xapp-
  signing_secret: your-signing-secret

github:
  app_id: "123456"                       # Your GitHub App ID (number)
  private_key_path: "./github-app-key.pem"  # Path to .pem file
  installation_id: "12345678"            # Your installation ID (number)
  
gemini:
  api_key: AIza...                       # Your Gemini API key
```

**Verify the GitHub private key exists:**
```bash
ls -la /Users/chinmayabharadwajhs/@Slash/slack-coding-agent/github-app-key.pem
```

---

### Step 3: Start the Bot and Worker

You have two options:

#### Option A: Run Locally (Recommended for Testing)

**Terminal 1 - Start the Bot:**
```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
source venv/bin/activate
python -m slackagent.main
```

**Expected output:**
```
⚡️ Bolt app is running!
```

**Terminal 2 - Start the Worker:**
```bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
source venv/bin/activate
python -m slackagent.worker
```

**Expected output:**
```
Worker started, polling for tasks...
```

#### Option B: Use ECS (Currently Deploying)

If bot service is running (runningCount=1):
```bash
# Check status
aws ecs describe-services \
  --cluster SlackAgentCluster \
  --services slack-agent-bot slack-agent-worker \
  --region us-east-2 \
  --query 'services[].[serviceName,runningCount]' \
  --output table

# Scale worker if needed
aws ecs update-service \
  --cluster SlackAgentCluster \
  --service slack-agent-worker \
  --desired-count 1 \
  --region us-east-2
```

---

### Step 4: Prepare a Test Repository

Choose a repository that:
1. Your GitHub App has access to
2. Has simple, editable files
3. You have permission to create branches/PRs

**Example: Create a simple test repo**
```bash
# On GitHub, create a new repo: your-username/test-bot-repo
# Or use an existing one

# Clone it locally to verify
git clone https://github.com/your-username/test-bot-repo.git
cd test-bot-repo

# Add a simple file if empty
echo "# Test Repository" > README.md
git add README.md
git commit -m "Initial commit"
git push origin main
```

---

### Step 5: Send a Message to the Bot in Slack

#### Find Your Bot in Slack:

1. Open your Slack workspace
2. Click on "Apps" in the left sidebar
3. Find your bot (e.g., "Slack Coding Agent")
4. OR create a channel and invite the bot: `/invite @your-bot-name`

#### Send a Test Task:

**Format:**
```
@your-bot-name owner/repository Task description
```

**Example 1 - Simple Task:**
```
@codingbot your-username/test-bot-repo Add a TODO comment at the top of README.md
```

**Example 2 - More Specific:**
```
@codingbot your-username/test-bot-repo Add a new section called "Installation" to README.md with instructions to run npm install
```

**Example 3 - Code File:**
```
@codingbot your-username/my-app Add input validation to the login function in src/auth.js
```

---

### Step 6: What Happens Next (Full Workflow)

#### Phase 1: Task Creation (~1-2 seconds)
**Bot Response:**
```
🤖 Task received! Cloning repository and analyzing...

Repository: your-username/test-bot-repo
Task: Add a TODO comment at the top of README.md
Task ID: abc123

I'll respond in this thread with the plan shortly.
```

**What's happening:**
- Bot creates task in database
- Worker picks up task
- Repository is cloned

#### Phase 2: Planning (~10-30 seconds)
**Bot Posts in Thread:**
```
📋 Implementation Plan

I will:
1. Open README.md
2. Add a TODO comment at the top: <!-- TODO: Add project description -->
3. Commit the change

Files to modify:
- README.md

[Approve Plan] [Reject Plan]
```

**Your Action:** Click **"Approve Plan"** button (or react with 👍)

#### Phase 3: Implementation (~30-60 seconds)
**Bot Posts:**
```
⏳ Implementing changes...
```

**What's happening:**
- Worker creates a new branch (e.g., `feat/task-abc123`)
- AI generates the code changes
- Changes are committed to the branch
- Diff is generated

**Bot Posts Diff:**
```
📝 Changes Preview

File: README.md
+++ <!-- TODO: Add project description -->
 # Test Repository

[Approve & Create PR] [Reject]
```

**Your Action:** Click **"Approve & Create PR"** button (or react with 👍)

#### Phase 4: PR Creation (~5-10 seconds)
**Bot Posts:**
```
✅ Pull Request Created!

PR #1: Add TODO comment to README.md
https://github.com/your-username/test-bot-repo/pull/1

Branch: feat/task-abc123
Status: Ready for review
```

---

### Step 7: Verify on GitHub

**Check GitHub:**
1. Go to: `https://github.com/your-username/test-bot-repo/pulls`
2. You should see a new Pull Request
3. Click on it to view:
   - ✅ Title: Auto-generated from task
   - ✅ Description: Implementation details
   - ✅ Files changed: The diff
   - ✅ Commits: Bot's commit
   - ✅ Branch: `feat/task-abc123`

**PR Description Example:**
```markdown
## Task
Add a TODO comment at the top of README.md

## Changes
- Added TODO comment at the top of README.md

## Implementation Details
Modified README.md to include a placeholder TODO comment for future enhancements.

---
Task ID: abc123
Requested by: @your-slack-username via Slack
```

---

## Troubleshooting

### Issue 1: Bot Doesn't Respond in Slack

**Check:**
```bash
# Is bot running?
ps aux | grep slackagent.main

# Check bot logs
tail -f logs/bot.log  # if configured
```

**Verify:**
- Bot token is correct (xoxb-...)
- Bot is invited to the channel
- Bot has `app_mentions:read` permission
- Event Subscriptions are enabled in Slack App

**Test:**
```bash
# Test bot locally
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
source venv/bin/activate
python -m slackagent.main
# Try sending a message
```

### Issue 2: Worker Doesn't Process Task

**Check:**
```bash
# Is worker running?
ps aux | grep slackagent.worker

# Check worker logs
tail -f logs/worker.log  # if configured

# Check database
sqlite3 data/tasks.db "SELECT task_id, status, description FROM tasks ORDER BY created_at DESC LIMIT 5;"
```

**Verify:**
- Worker is running
- No errors in worker logs
- Task appears in database with status PENDING

### Issue 3: GitHub Authentication Fails

**Error:** `ModuleNotFoundError: No module named 'cryptography'`

**Fix:** ✅ Already fixed! Use the new Docker image.

**Error:** `GitHub authentication failed` or `401 Unauthorized`

**Check:**
```bash
# Test GitHub authentication
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
source venv/bin/activate
python << 'EOF'
from slackagent.github_ops import GitHubClient
from slackagent.config import load_config

config = load_config('config.yaml')
gh = GitHubClient(
    app_id=config.github.app_id,
    private_key_path=config.github.private_key_path,
    installation_id=config.github.installation_id
)

print("Testing GitHub connection...")
repos = gh.get_repos()
print(f"✅ Connected! Found {len(repos)} repositories:")
for repo in repos[:5]:
    print(f"  - {repo}")
EOF
```

**Verify:**
- GitHub App ID is correct
- Installation ID is correct
- Private key file exists and is readable
- cryptography package is installed

### Issue 4: PR Not Created

**Error in logs:** `Permission denied` or `Resource not accessible`

**Check GitHub App Permissions:**
1. Go to: https://github.com/settings/apps/your-app
2. Permissions tab
3. Ensure these are enabled:
   - Repository contents: Read & Write ✅
   - Pull requests: Read & Write ✅

**Reinstall if needed:**
1. Go to: https://github.com/settings/installations
2. Click "Configure" on your app
3. Save (this refreshes permissions)

**Check Repository Access:**
1. Go to: https://github.com/settings/installations
2. Click on your app
3. Verify the test repository is selected under "Repository access"

### Issue 5: Branch Already Exists

**Error:** `Branch feat/task-abc123 already exists`

**Solution:**
- Use a different task (creates new branch)
- Or manually delete the branch on GitHub first
- Or specify a different branch name format in worker code

---

## Quick Test Script

Save this as `test-github-integration.sh`:

```bash
#!/bin/bash
cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent

echo "Testing GitHub Integration..."
echo ""

echo "1. Testing cryptography..."
python -c "import cryptography; print(f'✅ cryptography {cryptography.__version__}')"

echo ""
echo "2. Testing GitHub authentication..."
python << 'EOF'
from slackagent.github_ops import GitHubClient
from slackagent.config import load_config

try:
    config = load_config('config.yaml')
    gh = GitHubClient(
        app_id=config.github.app_id,
        private_key_path=config.github.private_key_path,
        installation_id=config.github.installation_id
    )
    repos = gh.get_repos()
    print(f"✅ GitHub auth successful! {len(repos)} repos accessible")
    print("   Repositories:")
    for repo in repos[:3]:
        print(f"   - {repo}")
except Exception as e:
    print(f"❌ GitHub auth failed: {e}")
EOF

echo ""
echo "3. Checking bot/worker processes..."
ps aux | grep -E 'slackagent\.(main|worker)' | grep -v grep || echo "⚠️  No processes running"

echo ""
echo "Done!"
```

Run it:
```bash
chmod +x test-github-integration.sh
./test-github-integration.sh
```

---

## Summary: What You Need to Do

### Minimal Steps to See a PR:

1. **Start the bot and worker locally:**
   ```bash
   # Terminal 1
   cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent
   source venv/bin/activate
   python -m slackagent.main
   
   # Terminal 2
   source venv/bin/activate
   python -m slackagent.worker
   ```

2. **In Slack, send:**
   ```
   @your-bot-name owner/repository Add a TODO comment to README.md
   ```

3. **Approve the plan** when bot asks

4. **Approve the implementation** when bot shows diff

5. **Check GitHub:** `https://github.com/owner/repository/pulls`

---

## Expected Timeline

| Step | Time | What's Happening |
|------|------|------------------|
| Send message | 0s | You mention bot |
| Bot responds | 1-2s | Task created |
| Planning | 10-30s | AI analyzes repo |
| Plan approval | 0s | You click button |
| Implementation | 30-60s | AI generates code |
| Impl approval | 0s | You click button |
| PR creation | 5-10s | Push & create PR |
| **Total** | **1-2 min** | PR visible on GitHub |

---

## Files to Check

- **Config:** `/Users/chinmayabharadwajhs/@Slash/slack-coding-agent/config.yaml`
- **Key:** `/Users/chinmayabharadwajhs/@Slash/slack-coding-agent/github-app-key.pem`
- **Database:** `/Users/chinmayabharadwajhs/@Slash/slack-coding-agent/data/tasks.db`
- **Logs:** Check terminal output or log files

---

**Ready to test?** Start both services and send a message! 🚀
