# SUPPORT CASES — Slack Coding Agent (post-refactor runbook)

Runbook for diagnosing the HTTPS dashboard (HTTP API + site Lambda), the
sandbox vCPU budget, and the model-backend switch. Region `us-east-2`,
account `958357664308`, IAM user `slash-bot`.

## 0. Layout after the refactor

| Component            | How it is served now                                          | Removed                      |
| -------------------- | ------------------------------------------------------------- | ---------------------------- |
| Dashboard HTTPS      | API GW HTTP API `slack-agent-dashboard` → Lambda `slack-agent-dashboard-site` (serves `spa/` + dashboard API in one origin) | CloudFront, S3 website, App Runner, dedicated dashboard Fargate |
| Approve/reject       | site Lambda proxies `POST /tasks/{id}/approve|reject` to the REST API (`REST_API_URL`) | —                            |
| Sandbox model        | `MODEL_BACKEND` = sim \| anthropic \| bedrock (SSM `/slack-agent/model-backend`, env fallback `MODEL_BACKEND`) | hard-coded `CLAUDE_CODE_USE_BEDROCK=1` |
| vCPU budget          | bot 0.5 + worker 0.5 + ≤2 sandboxes ×1.0 = **3.0 vCPU**, enforced by worker ASG max=1 + `MAX_CONCURRENT_SANDBOXES=2` + `SANDBOX_VCPU_BUDGET=3.0` | 4+ vCPU oracle |

## 1. Dashboard returns 500 in browser (site Lambda)

1. Check the site Lambda:
   ```
   aws lambda invoke --function-name slack-agent-dashboard-site --region us-east-2 \
     --payload '{"version":"2.0","rawPath":"/health","requestContext":{"http":{"method":"GET","path":"/health"}}}' out.json
   cat out.json   # expect {"statusCode":200,"body":"{\"status\":\"ok\",...}"}
   ```
2. `401/403` on the HTTP API → stage keys not the issue; HTTP API is open. Check the
   `DashboardSite` function URL route permission (synth adds `lambda:InvokeFunction`
   for the API). Redploy if permission drifted.
3. `/ → "dashboard SPA not bundled"`: the SPA folder `infrastructure/lambda/dashboard_api/spa/`
   is missing or stale. Rebuild and redeploy:
   ```
   (cd dashboard && npm ci && REACT_APP_SAME_ORIGIN=true npm run build)   # via build:spa
   cp -R dashboard/build/. infrastructure/lambda/dashboard_api/spa/
   ```
   `api-stack.ts` refuses to synth without `spa/index.html`.

## 2. Approve/Reject buttons fail with 503

Site forwards to `REST_API_URL` (set to `this.api.url`). Verify it resolved:
```
aws lambda get-function-configuration --function-name slack-agent-dashboard-site --region us-east-2 \
  --query 'Environment.Variables.REST_API_URL'
curl -s "${REST_API_URL}tasks/{taskId}/approve" -X POST -d '{}' -H 'content-type: application/json'
```
If `503 REST_API_URL not configured`, the env var is missing → redeploy the stack.

## 3. "SIM mode" banner is on but you want real code

1. Flip the backend at runtime (no redeploy for bedrock↔sim):
   ```
   aws ssm put-parameter --name /slack-agent/model-backend --value bedrock \
     --type String --overwrite --region us-east-2
   ```
2. Valid values: `sim`, `anthropic`, `bedrock`. Anything else falls back to `bedrock`.
   The worker caches the value at startup — a running worker picks the new value on
   its next restart/redeploy; the dashboard picks it up per request (cached ~5 min).
3. For `anthropic`: create the secret, then redeploy with the flag:
   ```
   aws secretsmanager create-secret --name slack-agent/ANTHROPIC_API_KEY \
     --secret-string 'sk-ant-...' --region us-east-2
   INCLUDE_ANTHROPIC_API_KEY=true <deploy>
   ```
   Without the flag the task definition has no reference (deploy works but workers
   ignore anthropic). DO NOT add the secret reference while the secret is missing —
   ECS will refuse to start bot/worker containers.

## 4. Sandboxes never launch / tasks stuck in PLAN_APPROVED

The gate blocks when the fleet is at the cap. Symptoms in worker logs:
`vCPU budget exceeded (max_concurrent=2, budget=3.0, running=…, requested=…)`.
- Raise headroom: set worker ASG max back up only after removing the gate
  (`MAX_CONCURRENT_SANDBOXES` / `SANDBOX_VCPU_BUDGET`), or reduce
  `SANDBOX_VCPU_BASELINE`. Defaults: baseline 1.0, budget 3.0 → room for 2×1.0 sandboxes.
- Gate only accounts ECS `RUNNING` tasks under the sandbox task definition family;
  if `list_tasks` is unavailable the gate is skipped (fail-open) — container logs note this.

## 5. Cost is higher than expected (vCPU 3.0 budget)

- Both bot and worker are 0.5 vCPU + 1 GB; two sandboxes can run at 1.0 vCPU each.
- Worst case (2 sandboxes always-on) ≈ **$90/mo** Fargate + small Lambda/API/log costs.
- Remove the dashboard’s old footprint? It's already gone in this refactor
  (App Runner + dashboard Fargate + CloudFront): saves ~$55–65/mo.
- SIM mode costs $0 for model calls; Bedrock/Anthropic tokens are billed separately.

## 6. Rollback

Nothing is removed destructively this round: the REST dashboard API unchanged, S3/CloudFront
resources for the dashboard are NOT managed by CDK (they were created outside the stack), so a
plain `git revert` of `infrastructure/lib/*` + redeploy restores the old paths.