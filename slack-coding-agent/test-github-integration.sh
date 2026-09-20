#!/bin/bash
# Test GitHub Integration

cd /Users/chinmayabharadwajhs/@Slash/slack-coding-agent

echo "========================================================================"
echo "  Testing GitHub Integration"
echo "========================================================================"
echo ""

# Test 1: Cryptography
echo "✓ Test 1: Cryptography Package"
python -c "import cryptography; print(f'  ✅ cryptography {cryptography.__version__} installed')" 2>&1 || echo "  ❌ cryptography not installed"

echo ""

# Test 2: PyJWT with RSA
echo "✓ Test 2: JWT with RSA (requires cryptography)"
python -c "
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)
token = jwt.encode({'test': 'data'}, private_pem, algorithm='RS256')
print('  ✅ JWT RSA signing works')
" 2>&1 || echo "  ❌ JWT RSA signing failed"

echo ""

# Test 3: GitHub Authentication
echo "✓ Test 3: GitHub App Authentication"
python << 'EOF'
from slackagent.github_ops import GitHubClient
from slackagent.config import load_config
import sys

try:
    config = load_config('config.yaml')
    gh = GitHubClient(config=config.github)
    
    # Test by generating an installation token
    token = gh._get_installation_token()
    
    if token:
        print('  ✅ GitHub auth successful! Installation token obtained')
        print(f'     App ID: {config.github.app_id}')
        print(f'     Installation ID: {config.github.installation_id}')
        print(f'     Token length: {len(token)} characters')
    else:
        print('  ❌ Failed to get installation token')
        sys.exit(1)
        
except FileNotFoundError as e:
    print(f'  ❌ Configuration error: {e}')
    print('     Make sure config.yaml and github-app-key.pem exist')
    sys.exit(1)
except Exception as e:
    print(f'  ❌ GitHub auth failed: {e}')
    print('     Check your GitHub App ID, Installation ID, and private key')
    sys.exit(1)
EOF

if [ $? -ne 0 ]; then
    echo ""
    echo "  💡 Troubleshooting:"
    echo "     - Check config.yaml has correct GitHub App ID"
    echo "     - Verify github-app-key.pem exists and is readable"
    echo "     - Ensure GitHub App is installed on repositories"
    exit 1
fi

echo ""

# Test 4: Check Running Processes
echo "✓ Test 4: Bot and Worker Processes"
BOT_RUNNING=$(ps aux | grep 'slackagent.main' | grep -v grep | wc -l)
WORKER_RUNNING=$(ps aux | grep 'slackagent.worker' | grep -v grep | wc -l)

if [ "$BOT_RUNNING" -gt 0 ]; then
    echo "  ✅ Bot is running (slackagent.main)"
else
    echo "  ⚠️  Bot is NOT running"
    echo "     Start with: python -m slackagent.main"
fi

if [ "$WORKER_RUNNING" -gt 0 ]; then
    echo "  ✅ Worker is running (slackagent.worker)"
else
    echo "  ⚠️  Worker is NOT running"
    echo "     Start with: python -m slackagent.worker"
fi

echo ""

# Test 5: Database
echo "✓ Test 5: Database Status"
if [ -f "data/tasks.db" ]; then
    TASK_COUNT=$(sqlite3 data/tasks.db "SELECT COUNT(*) FROM tasks;" 2>/dev/null || echo "0")
    echo "  ✅ Database exists: data/tasks.db"
    echo "     Total tasks: $TASK_COUNT"
    
    # Show recent tasks
    RECENT=$(sqlite3 data/tasks.db "SELECT task_id, status, description FROM tasks ORDER BY created_at DESC LIMIT 3;" 2>/dev/null || echo "")
    if [ -n "$RECENT" ]; then
        echo "     Recent tasks:"
        echo "$RECENT" | while IFS='|' read -r id status desc; do
            echo "     - [$status] $desc (ID: $id)"
        done
    fi
else
    echo "  ⚠️  Database not found (will be created on first run)"
fi

echo ""
echo "========================================================================"
echo "  Summary"
echo "========================================================================"
echo ""

# Overall status
ALL_GOOD=true

python -c "import cryptography" 2>/dev/null || ALL_GOOD=false
python -c "from slackagent.github_ops import GitHubClient" 2>/dev/null || ALL_GOOD=false

if [ "$ALL_GOOD" = true ] && [ "$BOT_RUNNING" -gt 0 ] && [ "$WORKER_RUNNING" -gt 0 ]; then
    echo "✅ All systems ready!"
    echo ""
    echo "You can now test in Slack:"
    echo "  @your-bot-name owner/repository Add a TODO comment to README.md"
elif [ "$ALL_GOOD" = true ]; then
    echo "⚠️  Configuration is correct, but services are not running"
    echo ""
    echo "Start the bot and worker:"
    echo "  # Terminal 1"
    echo "  source venv/bin/activate"
    echo "  python -m slackagent.main"
    echo ""
    echo "  # Terminal 2"
    echo "  source venv/bin/activate"
    echo "  python -m slackagent.worker"
else
    echo "❌ Some components need attention (see above)"
fi

echo ""
echo "========================================================================"
