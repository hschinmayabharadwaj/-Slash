import { subHours, subMinutes, subDays, formatISO } from 'date-fns';

const now = new Date();

// ─── Helpers ─────────────────────────────────────────────────────────────────

function ts(offsetMinutes = 0) {
  return formatISO(subMinutes(now, offsetMinutes));
}

function durationMs(minutes) {
  return minutes * 60 * 1000;
}

// ─── Tasks ───────────────────────────────────────────────────────────────────

export const MOCK_TASKS = [
  {
    id: 'task-001',
    shortId: 1,
    repository: 'acme-corp/api-service',
    description: 'Add input validation to the user registration endpoint to prevent SQL injection and XSS attacks',
    status: 'completed',
    user: 'U04XK2M3P',
    username: 'alice.chen',
    createdAt: ts(240),
    updatedAt: ts(195),
    completedAt: ts(195),
    durationMs: durationMs(45),
    prUrl: 'https://github.com/acme-corp/api-service/pull/147',
    prNumber: 147,
    plan: `## Plan: Add Input Validation to User Registration

### Problem Analysis
The current \`/api/v1/users/register\` endpoint accepts raw user input without validation, creating potential security vulnerabilities.

### Proposed Changes
1. **Add Pydantic validators** to the \`UserCreate\` schema:
   - Email format validation using \`EmailStr\`
   - Password strength requirements (min 8 chars, uppercase, digit, special char)
   - Username sanitization (alphanumeric + underscore only)

2. **Add rate limiting** to the registration endpoint using \`slowapi\`

3. **Add integration tests** covering:
   - Valid registration
   - Duplicate email rejection
   - Invalid email format
   - Weak password rejection
   - SQL injection attempts

### Files to Modify
- \`src/schemas/user.py\` — add validators
- \`src/routers/users.py\` — add rate limiting
- \`tests/test_user_registration.py\` — new test file

### Estimated Impact
- Low risk change — purely additive validation
- No breaking changes to valid requests`,
    diff: `diff --git a/src/schemas/user.py b/src/schemas/user.py
index 3a2f4c1..9b8d2e5 100644
--- a/src/schemas/user.py
+++ b/src/schemas/user.py
@@ -1,12 +1,34 @@
 from pydantic import BaseModel
+from pydantic import EmailStr, validator
+import re
 
 class UserCreate(BaseModel):
-    email: str
-    password: str
-    username: str
+    email: EmailStr
+    password: str
+    username: str
+
+    @validator('password')
+    def password_strength(cls, v):
+        if len(v) < 8:
+            raise ValueError('Password must be at least 8 characters')
+        if not re.search(r'[A-Z]', v):
+            raise ValueError('Password must contain uppercase letter')
+        if not re.search(r'\\d', v):
+            raise ValueError('Password must contain a digit')
+        if not re.search(r'[^a-zA-Z0-9]', v):
+            raise ValueError('Password must contain a special character')
+        return v
+
+    @validator('username')
+    def username_format(cls, v):
+        if not re.match(r'^[a-zA-Z0-9_]{3,32}$', v):
+            raise ValueError('Username must be 3-32 alphanumeric characters or underscores')
+        return v.lower()
diff --git a/tests/test_user_registration.py b/tests/test_user_registration.py
new file mode 100644
index 0000000..c4f91a2
--- /dev/null
+++ b/tests/test_user_registration.py
@@ -0,0 +1,42 @@
+import pytest
+from httpx import AsyncClient
+from main import app
+
+@pytest.mark.asyncio
+async def test_valid_registration():
+    async with AsyncClient(app=app, base_url="http://test") as ac:
+        resp = await ac.post("/api/v1/users/register", json={
+            "email": "test@example.com",
+            "password": "Secure123!",
+            "username": "testuser"
+        })
+    assert resp.status_code == 201
+
+@pytest.mark.asyncio
+async def test_sql_injection_rejected():
+    async with AsyncClient(app=app, base_url="http://test") as ac:
+        resp = await ac.post("/api/v1/users/register", json={
+            "email": "test@example.com",
+            "password": "Secure123!",
+            "username": "admin'--"
+        })
+    assert resp.status_code == 422`,
    checks: {
      lint: { status: 'passed', output: 'All checks passed (0 errors, 0 warnings)', duration: 3200 },
      tests: { status: 'passed', output: '42 passed, 0 failed, 0 skipped in 8.4s', duration: 8400 },
    },
    timeline: [
      { status: 'pending', timestamp: ts(240), note: 'Task created from Slack mention' },
      { status: 'planning', timestamp: ts(238), note: 'Planning phase started' },
      { status: 'awaiting_approval', timestamp: ts(230), note: 'Plan ready for review' },
      { status: 'implementing', timestamp: ts(225), note: 'Plan approved by alice.chen' },
      { status: 'awaiting_impl_approval', timestamp: ts(210), note: 'Implementation complete, diff ready' },
      { status: 'creating_pr', timestamp: ts(200), note: 'Diff approved, creating PR' },
      { status: 'completed', timestamp: ts(195), note: 'PR #147 created successfully' },
    ],
  },

  {
    id: 'task-002',
    shortId: 2,
    repository: 'acme-corp/frontend',
    description: 'Refactor the product listing page to use React Query for data fetching and add loading skeletons',
    status: 'implementing',
    user: 'U07BC9D2Q',
    username: 'bob.martinez',
    createdAt: ts(90),
    updatedAt: ts(15),
    completedAt: null,
    durationMs: null,
    prUrl: null,
    prNumber: null,
    plan: `## Plan: Refactor Product Listing with React Query

### Overview
Replace manual \`useEffect\`/\`useState\` data fetching with \`@tanstack/react-query\` for better caching, loading states, and error handling.

### Steps
1. Install \`@tanstack/react-query\` 
2. Set up \`QueryClient\` provider in \`App.tsx\`
3. Create \`useProducts\` hook using \`useQuery\`
4. Add \`ProductSkeleton\` component (5 skeleton cards)
5. Add error boundary for fetch failures
6. Update \`ProductList\` to use hook and skeleton`,
    diff: null,
    checks: null,
    timeline: [
      { status: 'pending', timestamp: ts(90), note: 'Task created from Slack mention' },
      { status: 'planning', timestamp: ts(88), note: 'Planning phase started' },
      { status: 'awaiting_approval', timestamp: ts(75), note: 'Plan ready for review' },
      { status: 'implementing', timestamp: ts(60), note: 'Plan approved by bob.martinez' },
    ],
  },

  {
    id: 'task-003',
    shortId: 3,
    repository: 'acme-corp/data-pipeline',
    description: 'Fix memory leak in the ETL worker that causes OOM crashes after ~6 hours of runtime',
    status: 'awaiting_approval',
    user: 'U02PL8R7W',
    username: 'carol.smith',
    createdAt: ts(45),
    updatedAt: ts(20),
    completedAt: null,
    durationMs: null,
    prUrl: null,
    prNumber: null,
    plan: `## Plan: Fix Memory Leak in ETL Worker

### Root Cause Analysis
After reviewing \`worker/etl_processor.py\`, the leak appears in:
1. \`DataFrameCache\` — frames accumulate without eviction
2. Database connections not closed in error paths
3. Event listeners attached in a loop but never removed

### Fix Strategy
1. Add LRU eviction to \`DataFrameCache\` (max 100 frames, 512MB)
2. Use context managers for all DB connections
3. Track and remove event listeners properly
4. Add memory usage logging every 5 minutes

### Testing
- Unit test cache eviction
- 12-hour stress test with memory profiler`,
    diff: null,
    checks: null,
    timeline: [
      { status: 'pending', timestamp: ts(45), note: 'Task created from Slack mention' },
      { status: 'planning', timestamp: ts(43), note: 'Planning phase started' },
      { status: 'awaiting_approval', timestamp: ts(20), note: 'Plan ready — awaiting review' },
    ],
  },

  {
    id: 'task-004',
    shortId: 4,
    repository: 'acme-corp/auth-service',
    description: 'Implement refresh token rotation and add device fingerprinting to the JWT auth flow',
    status: 'pending',
    user: 'U09MN4T6X',
    username: 'dave.wilson',
    createdAt: ts(5),
    updatedAt: ts(5),
    completedAt: null,
    durationMs: null,
    prUrl: null,
    prNumber: null,
    plan: null,
    diff: null,
    checks: null,
    timeline: [
      { status: 'pending', timestamp: ts(5), note: 'Task created from Slack mention' },
    ],
  },

  {
    id: 'task-005',
    shortId: 5,
    repository: 'acme-corp/api-service',
    description: 'Add Prometheus metrics endpoint and instrument all API routes with latency histograms',
    status: 'failed',
    user: 'U04XK2M3P',
    username: 'alice.chen',
    createdAt: ts(300),
    updatedAt: ts(280),
    completedAt: null,
    durationMs: durationMs(20),
    prUrl: null,
    prNumber: null,
    plan: `## Plan: Add Prometheus Metrics

### Steps
1. Add \`prometheus-fastapi-instrumentator\` dependency
2. Create \`/metrics\` endpoint
3. Add custom histograms for business metrics`,
    diff: null,
    checks: {
      lint: { status: 'failed', output: 'E501 line too long (120 > 79 characters) — src/metrics.py:34', duration: 1200 },
      tests: { status: 'failed', output: 'FAILED tests/test_metrics.py::test_metrics_endpoint — ImportError: cannot import name PrometheusInstrumentator', duration: 2100 },
    },
    timeline: [
      { status: 'pending', timestamp: ts(300), note: 'Task created' },
      { status: 'planning', timestamp: ts(298), note: 'Planning phase started' },
      { status: 'awaiting_approval', timestamp: ts(290), note: 'Plan ready' },
      { status: 'implementing', timestamp: ts(285), note: 'Plan approved' },
      { status: 'failed', timestamp: ts(280), note: 'Checks failed: lint errors and import error in tests' },
    ],
  },

  {
    id: 'task-006',
    shortId: 6,
    repository: 'acme-corp/mobile-app',
    description: 'Add biometric authentication (Face ID / Touch ID) to the login screen using expo-local-authentication',
    status: 'completed',
    user: 'U07BC9D2Q',
    username: 'bob.martinez',
    createdAt: ts(480),
    updatedAt: ts(420),
    completedAt: ts(420),
    durationMs: durationMs(60),
    prUrl: 'https://github.com/acme-corp/mobile-app/pull/89',
    prNumber: 89,
    plan: null,
    diff: `diff --git a/screens/LoginScreen.tsx b/screens/LoginScreen.tsx
index 7c3d1a2..4e8f9b0 100644
--- a/screens/LoginScreen.tsx
+++ b/screens/LoginScreen.tsx
@@ -1,8 +1,12 @@
 import React, { useState } from 'react';
-import { View, TextInput, Button } from 'react-native';
+import { View, TextInput, Button, TouchableOpacity } from 'react-native';
+import * as LocalAuthentication from 'expo-local-authentication';
+import { Ionicons } from '@expo/vector-icons';
 
+const BiometricButton = ({ onSuccess }) => {
+  const authenticate = async () => {
+    const result = await LocalAuthentication.authenticateAsync({
+      promptMessage: 'Sign in to CodingBot',
+      fallbackLabel: 'Use Password',
+    });
+    if (result.success) onSuccess();
+  };
+  return (
+    <TouchableOpacity onPress={authenticate} style={styles.biometric}>
+      <Ionicons name="finger-print" size={32} color="#238636" />
+    </TouchableOpacity>
+  );
+};`,
    checks: {
      lint: { status: 'passed', output: 'No issues found', duration: 2800 },
      tests: { status: 'passed', output: '18 passed in 4.2s', duration: 4200 },
    },
    timeline: [
      { status: 'pending', timestamp: ts(480), note: 'Task created' },
      { status: 'planning', timestamp: ts(478), note: 'Planning started' },
      { status: 'awaiting_approval', timestamp: ts(465), note: 'Plan ready' },
      { status: 'implementing', timestamp: ts(460), note: 'Approved' },
      { status: 'awaiting_impl_approval', timestamp: ts(440), note: 'Implementation complete' },
      { status: 'creating_pr', timestamp: ts(425), note: 'Diff approved' },
      { status: 'completed', timestamp: ts(420), note: 'PR #89 created' },
    ],
  },

  {
    id: 'task-007',
    shortId: 7,
    repository: 'acme-corp/infra',
    description: 'Create Terraform module for ECS Fargate service with auto-scaling and ALB',
    status: 'awaiting_impl_approval',
    user: 'U02PL8R7W',
    username: 'carol.smith',
    createdAt: ts(120),
    updatedAt: ts(30),
    completedAt: null,
    durationMs: null,
    prUrl: null,
    prNumber: null,
    plan: `## Plan: Terraform ECS Fargate Module

Create a reusable Terraform module at \`modules/ecs-service/\` with:
- ECS Cluster + Fargate service
- Task definition with configurable CPU/memory
- Application Load Balancer + target group
- Auto-scaling policy (CPU > 70% → scale out)
- CloudWatch log group
- IAM roles`,
    diff: `diff --git a/modules/ecs-service/main.tf b/modules/ecs-service/main.tf
new file mode 100644
index 0000000..1a2c3d4
--- /dev/null
+++ b/modules/ecs-service/main.tf
@@ -0,0 +1,87 @@
+resource "aws_ecs_cluster" "this" {
+  name = var.cluster_name
+  setting {
+    name  = "containerInsights"
+    value = "enabled"
+  }
+}
+
+resource "aws_ecs_task_definition" "this" {
+  family                   = var.service_name
+  cpu                      = var.cpu
+  memory                   = var.memory
+  network_mode             = "awsvpc"
+  requires_compatibilities = ["FARGATE"]
+  execution_role_arn       = aws_iam_role.execution.arn
+  task_role_arn            = aws_iam_role.task.arn
+
+  container_definitions = jsonencode([{
+    name      = var.service_name
+    image     = var.image_uri
+    essential = true
+    portMappings = [{
+      containerPort = var.container_port
+      protocol      = "tcp"
+    }]
+    logConfiguration = {
+      logDriver = "awslogs"
+      options = {
+        "awslogs-group"         = aws_cloudwatch_log_group.this.name
+        "awslogs-region"        = data.aws_region.current.name
+        "awslogs-stream-prefix" = "ecs"
+      }
+    }
+  }])
+}`,
    checks: {
      lint: { status: 'passed', output: 'terraform fmt: all files formatted correctly', duration: 890 },
      tests: { status: 'passed', output: 'terraform validate: success', duration: 1200 },
    },
    timeline: [
      { status: 'pending', timestamp: ts(120), note: 'Task created' },
      { status: 'planning', timestamp: ts(118), note: 'Planning started' },
      { status: 'awaiting_approval', timestamp: ts(105), note: 'Plan ready' },
      { status: 'implementing', timestamp: ts(90), note: 'Plan approved' },
      { status: 'awaiting_impl_approval', timestamp: ts(30), note: 'Implementation complete — awaiting diff review' },
    ],
  },

  {
    id: 'task-008',
    shortId: 8,
    repository: 'acme-corp/api-service',
    description: 'Migrate database connection pool from psycopg2 to asyncpg for better async performance',
    status: 'planning',
    user: 'U09MN4T6X',
    username: 'dave.wilson',
    createdAt: ts(12),
    updatedAt: ts(10),
    completedAt: null,
    durationMs: null,
    prUrl: null,
    prNumber: null,
    plan: null,
    diff: null,
    checks: null,
    timeline: [
      { status: 'pending', timestamp: ts(12), note: 'Task created' },
      { status: 'planning', timestamp: ts(10), note: 'Planning started — Gemini analyzing codebase' },
    ],
  },

  {
    id: 'task-009',
    shortId: 9,
    repository: 'acme-corp/frontend',
    description: 'Add dark mode toggle with system preference detection and localStorage persistence',
    status: 'completed',
    user: 'U04XK2M3P',
    username: 'alice.chen',
    createdAt: ts(720),
    updatedAt: ts(660),
    completedAt: ts(660),
    durationMs: durationMs(60),
    prUrl: 'https://github.com/acme-corp/frontend/pull/234',
    prNumber: 234,
    plan: null,
    diff: null,
    checks: {
      lint: { status: 'passed', output: 'ESLint: 0 errors, 0 warnings', duration: 2100 },
      tests: { status: 'passed', output: '67 passed in 12.3s', duration: 12300 },
    },
    timeline: [
      { status: 'pending', timestamp: ts(720) },
      { status: 'planning', timestamp: ts(718) },
      { status: 'awaiting_approval', timestamp: ts(705) },
      { status: 'implementing', timestamp: ts(700) },
      { status: 'awaiting_impl_approval', timestamp: ts(675) },
      { status: 'creating_pr', timestamp: ts(665) },
      { status: 'completed', timestamp: ts(660) },
    ],
  },

  {
    id: 'task-010',
    shortId: 10,
    repository: 'acme-corp/data-pipeline',
    description: 'Add retry logic with exponential backoff to all external API calls in the ingestion workers',
    status: 'completed',
    user: 'U07BC9D2Q',
    username: 'bob.martinez',
    createdAt: ts(1440),
    updatedAt: ts(1380),
    completedAt: ts(1380),
    durationMs: durationMs(60),
    prUrl: 'https://github.com/acme-corp/data-pipeline/pull/56',
    prNumber: 56,
    plan: null,
    diff: null,
    checks: {
      lint: { status: 'passed', output: 'Flake8: 0 issues', duration: 1500 },
      tests: { status: 'passed', output: '29 passed in 6.1s', duration: 6100 },
    },
    timeline: [],
  },

  {
    id: 'task-011',
    shortId: 11,
    repository: 'acme-corp/auth-service',
    description: 'Add PKCE support to the OAuth2 authorization code flow',
    status: 'completed',
    user: 'U02PL8R7W',
    username: 'carol.smith',
    createdAt: ts(1200),
    updatedAt: ts(1140),
    completedAt: ts(1140),
    durationMs: durationMs(60),
    prUrl: 'https://github.com/acme-corp/auth-service/pull/201',
    prNumber: 201,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [],
  },

  {
    id: 'task-012',
    shortId: 12,
    repository: 'acme-corp/api-service',
    description: 'Implement cursor-based pagination for the /products endpoint replacing offset pagination',
    status: 'failed',
    user: 'U09MN4T6X',
    username: 'dave.wilson',
    createdAt: ts(900),
    updatedAt: ts(860),
    completedAt: null,
    durationMs: durationMs(40),
    prUrl: null,
    prNumber: null,
    plan: null,
    diff: null,
    checks: {
      lint: { status: 'passed', output: 'No issues', duration: 1800 },
      tests: { status: 'failed', output: '3 failed, 41 passed — TypeError: cursor must be base64 encoded string', duration: 9200 },
    },
    timeline: [],
  },

  {
    id: 'task-013',
    shortId: 13,
    repository: 'acme-corp/frontend',
    description: 'Add skeleton loading states to the dashboard while data is fetching',
    status: 'completed',
    user: 'U04XK2M3P',
    username: 'alice.chen',
    createdAt: subDays(now, 1).toISOString(),
    updatedAt: subDays(now, 1).toISOString(),
    completedAt: subDays(now, 1).toISOString(),
    durationMs: durationMs(55),
    prUrl: 'https://github.com/acme-corp/frontend/pull/228',
    prNumber: 228,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [],
  },

  {
    id: 'task-014',
    shortId: 14,
    repository: 'acme-corp/mobile-app',
    description: 'Fix push notifications not arriving on Android 13+ due to new permission model',
    status: 'completed',
    user: 'U07BC9D2Q',
    username: 'bob.martinez',
    createdAt: subDays(now, 1).toISOString(),
    updatedAt: subDays(now, 1).toISOString(),
    completedAt: subDays(now, 1).toISOString(),
    durationMs: durationMs(35),
    prUrl: 'https://github.com/acme-corp/mobile-app/pull/85',
    prNumber: 85,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [],
  },

  {
    id: 'task-015',
    shortId: 15,
    repository: 'acme-corp/infra',
    description: 'Set up CloudWatch alarms for ECS CPU, memory, and ALB 5xx error rates',
    status: 'completed',
    user: 'U02PL8R7W',
    username: 'carol.smith',
    createdAt: subDays(now, 1).toISOString(),
    updatedAt: subDays(now, 1).toISOString(),
    completedAt: subDays(now, 1).toISOString(),
    durationMs: durationMs(42),
    prUrl: 'https://github.com/acme-corp/infra/pull/33',
    prNumber: 33,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [],
  },

  {
    id: 'task-016',
    shortId: 16,
    repository: 'acme-corp/api-service',
    description: 'Add OpenAPI/Swagger documentation to all API endpoints',
    status: 'completed',
    user: 'U09MN4T6X',
    username: 'dave.wilson',
    createdAt: subDays(now, 2).toISOString(),
    updatedAt: subDays(now, 2).toISOString(),
    completedAt: subDays(now, 2).toISOString(),
    durationMs: durationMs(90),
    prUrl: 'https://github.com/acme-corp/api-service/pull/140',
    prNumber: 140,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [],
  },

  {
    id: 'task-017',
    shortId: 17,
    repository: 'acme-corp/data-pipeline',
    description: 'Optimize slow SQL queries in the reporting module (P95 latency > 10s)',
    status: 'failed',
    user: 'U04XK2M3P',
    username: 'alice.chen',
    createdAt: subDays(now, 2).toISOString(),
    updatedAt: subDays(now, 2).toISOString(),
    completedAt: null,
    durationMs: durationMs(75),
    prUrl: null,
    prNumber: null,
    plan: null,
    diff: null,
    checks: {
      lint: { status: 'passed' },
      tests: { status: 'failed', output: 'FAILED: query returns incorrect results after index addition' },
    },
    timeline: [],
  },

  {
    id: 'task-018',
    shortId: 18,
    repository: 'acme-corp/frontend',
    description: 'Implement infinite scroll for the activity feed replacing the paginated view',
    status: 'completed',
    user: 'U07BC9D2Q',
    username: 'bob.martinez',
    createdAt: subDays(now, 2).toISOString(),
    updatedAt: subDays(now, 2).toISOString(),
    completedAt: subDays(now, 2).toISOString(),
    durationMs: durationMs(50),
    prUrl: 'https://github.com/acme-corp/frontend/pull/220',
    prNumber: 220,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [],
  },

  {
    id: 'task-019',
    shortId: 19,
    repository: 'acme-corp/auth-service',
    description: 'Add MFA via TOTP (Google Authenticator compatible) to the login flow',
    status: 'completed',
    user: 'U02PL8R7W',
    username: 'carol.smith',
    createdAt: subDays(now, 3).toISOString(),
    updatedAt: subDays(now, 3).toISOString(),
    completedAt: subDays(now, 3).toISOString(),
    durationMs: durationMs(120),
    prUrl: 'https://github.com/acme-corp/auth-service/pull/195',
    prNumber: 195,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [],
  },

  {
    id: 'task-020',
    shortId: 20,
    repository: 'acme-corp/api-service',
    description: 'Add Redis caching layer to the product catalog API to reduce DB load',
    status: 'completed',
    user: 'U09MN4T6X',
    username: 'dave.wilson',
    createdAt: subDays(now, 3).toISOString(),
    updatedAt: subDays(now, 3).toISOString(),
    completedAt: subDays(now, 3).toISOString(),
    durationMs: durationMs(80),
    prUrl: 'https://github.com/acme-corp/api-service/pull/135',
    prNumber: 135,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [],
  },

  {
    id: 'task-021',
    shortId: 21,
    repository: 'acme-corp/infra',
    description: 'Add WAF rules to the ALB to block common OWASP Top 10 attacks',
    status: 'creating_pr',
    user: 'U04XK2M3P',
    username: 'alice.chen',
    createdAt: ts(35),
    updatedAt: ts(8),
    completedAt: null,
    durationMs: null,
    prUrl: null,
    prNumber: null,
    plan: null,
    diff: null,
    checks: { lint: { status: 'passed' }, tests: { status: 'passed' } },
    timeline: [
      { status: 'pending', timestamp: ts(35) },
      { status: 'planning', timestamp: ts(33) },
      { status: 'awaiting_approval', timestamp: ts(25) },
      { status: 'implementing', timestamp: ts(20) },
      { status: 'awaiting_impl_approval', timestamp: ts(12) },
      { status: 'creating_pr', timestamp: ts(8), note: 'Creating PR on GitHub...' },
    ],
  },
];

// ─── Stats ────────────────────────────────────────────────────────────────────

export function computeStats(tasks) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const activeStatuses = ['planning', 'awaiting_approval', 'implementing', 'awaiting_impl_approval', 'creating_pr'];

  const completedToday = tasks.filter(
    (t) => t.completedAt && new Date(t.completedAt) >= today
  ).length;

  // Yesterday completed (for trend)
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const completedYesterday = tasks.filter((t) => {
    if (!t.completedAt) return false;
    const d = new Date(t.completedAt);
    return d >= yesterday && d < today;
  }).length;

  const activeNow = tasks.filter((t) => activeStatuses.includes(t.status)).length;
  const failed = tasks.filter((t) => t.status === 'failed').length;

  return {
    total: tasks.length,
    activeNow,
    completedToday,
    completedYesterday,
    failed,
    failedYesterday: 2,
    activeYesterday: 3,
    totalYesterday: tasks.length - 3,
  };
}

// ─── Hourly histogram ────────────────────────────────────────────────────────

export function computeHourlyData(tasks) {
  const hours = Array.from({ length: 24 }, (_, i) => {
    const h = subHours(now, 23 - i);
    return {
      hour: h.getHours().toString().padStart(2, '0') + ':00',
      count: 0,
    };
  });

  tasks.forEach((t) => {
    const created = new Date(t.createdAt);
    const diffH = Math.floor((now - created) / (1000 * 60 * 60));
    if (diffH >= 0 && diffH < 24) {
      const idx = 23 - diffH;
      hours[idx].count += 1;
    }
  });

  return hours;
}

// ─── Status distribution ─────────────────────────────────────────────────────

export function computeStatusDist(tasks) {
  const map = {};
  tasks.forEach((t) => {
    map[t.status] = (map[t.status] || 0) + 1;
  });
  return Object.entries(map).map(([name, value]) => ({ name, value }));
}

// ─── Recent activity feed ────────────────────────────────────────────────────

export const MOCK_ACTIVITY = [
  { id: 1, time: ts(8), icon: '🔄', text: 'Task #21 creating PR on GitHub' },
  { id: 2, time: ts(15), icon: '⏳', text: 'Task #7 awaiting diff approval from carol.smith' },
  { id: 3, time: ts(15), icon: '🤖', text: 'Task #2 implementing — Gemini writing code' },
  { id: 4, time: ts(20), icon: '📋', text: 'Task #3 plan ready — awaiting approval from carol.smith' },
  { id: 5, time: ts(30), icon: '✅', text: 'Task #21 implementation approved, checks passed' },
  { id: 6, time: ts(45), icon: '✅', text: 'Task #1 completed — PR #147 created' },
  { id: 7, time: ts(60), icon: '🚀', text: 'Task #2 plan approved — implementation started' },
  { id: 8, time: ts(90), icon: '📬', text: 'Task #3 received from @carol.smith in #engineering' },
  { id: 9, time: ts(120), icon: '❌', text: 'Task #5 failed — lint errors in src/metrics.py' },
  { id: 10, time: ts(195), icon: '🎉', text: 'Task #1 completed — PR #147 merged' },
];

// ─── Bot / infrastructure status ─────────────────────────────────────────────

export const MOCK_INFRA_STATUS = {
  ecs: { status: 'healthy', detail: '2/2 tasks running', region: 'us-east-1' },
  sqs: { status: 'healthy', depth: 2, detail: '2 messages in queue' },
  aurora: { status: 'healthy', detail: 'Available — 3ms avg query time' },
  lastActivity: ts(8),
};
