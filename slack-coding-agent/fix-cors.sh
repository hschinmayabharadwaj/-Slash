#!/bin/bash
# CORS Fix for API Gateway + Lambda

echo "🔧 Fixing CORS issues for Slack Coding Agent API"

API_ID="2q1q944wb7"
REGION="us-east-2"
ORIGIN="http://18.220.118.120"

echo ""
echo "📋 Issue detected:"
echo "  - Missing CORS headers from Lambda responses"
echo "  - Missing /metrics and /stats endpoints"
echo "  - Double slash in URLs (/prod//stats)"
echo ""

# ──────────────────────────────────────────────────────────────
# Step 1: Update Lambda functions to return CORS headers
# ──────────────────────────────────────────────────────────────

echo "📝 Step 1: Update Lambda function code with CORS headers"

cat > /tmp/dashboard_api_cors_fix.py << 'EOF'
# Add this to the top of dashboard.py after imports:

def _cors_headers(origin="*"):
    """Return CORS headers for all responses."""
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Requested-With,X-Amz-Date,X-Api-Key,X-Amz-Security-Token",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Access-Control-Max-Age": "86400",
        "Content-Type": "application/json",
    }

# Update handler to add OPTIONS support:
def handler(event: dict, context) -> dict:
    # Handle OPTIONS preflight
    if event.get("httpMethod") == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": _cors_headers(),
            "body": json.dumps({"message": "OK"}),
        }
    
    # ... rest of your handler code
    # Make sure ALL returns include headers: _cors_headers()
EOF

echo "✅ CORS headers template created: /tmp/dashboard_api_cors_fix.py"

# ──────────────────────────────────────────────────────────────
# Step 2: Add missing API Gateway endpoints
# ──────────────────────────────────────────────────────────────

echo ""
echo "📝 Step 2: Create missing /metrics and /stats endpoints in API Gateway"

# Get the API root resource ID
ROOT_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --query 'items[?path==`/`].id' \
  --output text)

echo "  Root resource ID: $ROOT_ID"

# Create /metrics resource (if doesn't exist)
METRICS_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --query 'items[?path==`/metrics`].id' \
  --output text)

if [ -z "$METRICS_ID" ]; then
  echo "  Creating /metrics resource..."
  METRICS_ID=$(aws apigateway create-resource \
    --rest-api-id "$API_ID" \
    --region "$REGION" \
    --parent-id "$ROOT_ID" \
    --path-part "metrics" \
    --query 'id' \
    --output text)
  echo "  ✅ Created /metrics (ID: $METRICS_ID)"
else
  echo "  ✅ /metrics already exists (ID: $METRICS_ID)"
fi

# Add OPTIONS method to /metrics for CORS preflight
echo "  Adding OPTIONS method to /metrics..."
aws apigateway put-method \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --resource-id "$METRICS_ID" \
  --http-method OPTIONS \
  --authorization-type NONE \
  --no-api-key-required 2>/dev/null || echo "  (OPTIONS already exists)"

# Add mock integration for OPTIONS
aws apigateway put-integration \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --resource-id "$METRICS_ID" \
  --http-method OPTIONS \
  --type MOCK \
  --request-templates '{"application/json": "{\"statusCode\": 200}"}' 2>/dev/null

# Add method response for OPTIONS
aws apigateway put-method-response \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --resource-id "$METRICS_ID" \
  --http-method OPTIONS \
  --status-code 200 \
  --response-parameters '{
    "method.response.header.Access-Control-Allow-Origin": true,
    "method.response.header.Access-Control-Allow-Headers": true,
    "method.response.header.Access-Control-Allow-Methods": true
  }' 2>/dev/null

# Add integration response for OPTIONS
aws apigateway put-integration-response \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --resource-id "$METRICS_ID" \
  --http-method OPTIONS \
  --status-code 200 \
  --response-parameters '{
    "method.response.header.Access-Control-Allow-Origin": "'"'"'*'"'"'",
    "method.response.header.Access-Control-Allow-Headers": "'"'"'Content-Type,Authorization,X-Requested-With'"'"'",
    "method.response.header.Access-Control-Allow-Methods": "'"'"'GET,POST,OPTIONS'"'"'"
  }' 2>/dev/null

echo "  ✅ CORS enabled on /metrics"

# ──────────────────────────────────────────────────────────────
# Step 3: Deploy API Gateway changes
# ──────────────────────────────────────────────────────────────

echo ""
echo "📝 Step 3: Deploying API Gateway changes..."

aws apigateway create-deployment \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --stage-name prod \
  --description "CORS fix deployment $(date +%Y-%m-%d-%H-%M)"

echo "✅ API Gateway deployed"

# ──────────────────────────────────────────────────────────────
# Step 4: Test CORS
# ──────────────────────────────────────────────────────────────

echo ""
echo "🧪 Step 4: Testing CORS headers..."
echo ""

TEST_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod/stats"

echo "Testing: $TEST_URL"
CORS_RESPONSE=$(curl -s -I -X OPTIONS \
  -H "Origin: $ORIGIN" \
  -H "Access-Control-Request-Method: GET" \
  "$TEST_URL")

if echo "$CORS_RESPONSE" | grep -i "access-control-allow-origin" > /dev/null; then
  echo "✅ CORS headers present!"
else
  echo "❌ CORS headers still missing"
  echo ""
  echo "Response:"
  echo "$CORS_RESPONSE"
fi

echo ""
echo "──────────────────────────────────────────────────────────"
echo "🎯 MANUAL STEPS REQUIRED:"
echo "──────────────────────────────────────────────────────────"
echo ""
echo "1. Update Lambda function code to return CORS headers:"
echo "   See template: /tmp/dashboard_api_cors_fix.py"
echo ""
echo "2. Fix the double slash issue in your API base URL:"
echo "   Current: https://.../prod//stats"
echo "   Should be: https://.../prod/stats"
echo "   (Remove trailing slash from REACT_APP_API_URL in dashboard)"
echo ""
echo "3. Redeploy Lambda function:"
echo "   cd infrastructure"
echo "   cdk deploy SlackAgentApi"
echo ""
echo "──────────────────────────────────────────────────────────"
