"""ECS Fargate-backed sandbox with the same public interface as DockerSandbox.

The checkout and the I/O files are passed to the task through S3 and the task
is launched with ``ecs:RunTask`` in ``awsvpc`` networking mode.  Security
properties that differ from :class:`DockerSandbox`:

* No GitHub token and no provider API key ever enter the container by default.
  The task role is limited to its own S3 prefix plus ``bedrock:InvokeModel``,
  and the agent backend is chosen per launch by ``MODEL_BACKEND``:

    ``sim``       -> deterministic fake agent, no model call (cost \$0)
    ``bedrock``   -> Claude Code against Bedrock via the task role
    ``anthropic`` -> Claude direct API; the worker injects ``ANTHROPIC_API_KEY``

* A hard wall-clock timeout is enforced by stopping the task.
* A max-concurrent-sandbox gate plus a vCPU budget hold the fleet within the
  configured cap (bot 0.5 + 1 worker 0.5 + up to 2 sandboxes × 1.0 = 3.0 vCPU).

The container side that performs the S3 bootstrap is
:mod:`slackagent.fargate_entry`; it is shipped inside the sandbox image.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from .guards import tail
from .sandbox import SandboxResult

logger = logging.getLogger(__name__)

VALID_MODEL_BACKENDS = ("sim", "anthropic", "bedrock")


@dataclass
class FargateConfig:
    """Configuration for the ECS sandbox."""

    bucket: str
    cluster: str
    task_definition: str
    container_name: str = "sandbox"
    subnet_ids: List[str] = None
    security_group_ids: List[str] = None
    region: str = "us-east-1"
    poll_interval: float = 1.0
    platform_version: str = "1.4.0"
    assign_public_ip: str = "ENABLED"
    model_backend: str = "bedrock"
    plan_cpu: int = 512
    implement_cpu: int = 1024
    max_concurrent_sandboxes: int = 2
    vcpu_budget: float = 3.0
    vcpu_baseline: float = 1.0
    gate_wait_max: float = 300.0

    @classmethod
    def from_env(cls) -> "FargateConfig":
        """Build a config from environment variables (AWS deployment defaults)."""
        backend = (os.environ.get("MODEL_BACKEND") or "bedrock").strip().lower()
        if backend not in VALID_MODEL_BACKENDS:
            backend = "bedrock"
        return cls(
            bucket=os.environ.get("SANDBOX_BUCKET", ""),
            cluster=os.environ.get("SANDBOX_CLUSTER", ""),
            task_definition=os.environ.get("SANDBOX_TASK_DEFINITION", ""),
            container_name=os.environ.get("SANDBOX_CONTAINER_NAME", "sandbox"),
            subnet_ids=_csv(os.environ.get("SANDBOX_SUBNETS", "")),
            security_group_ids=_csv(os.environ.get("SANDBOX_SECURITY_GROUPS", "")),
            region=os.environ.get("AWS_REGION", "us-east-1"),
            assign_public_ip=os.environ.get("SANDBOX_ASSIGN_PUBLIC_IP", "ENABLED"),
            model_backend=backend,
            plan_cpu=int(os.environ.get("SANDBOX_PLAN_CPU", "512") or 512),
            implement_cpu=int(os.environ.get("SANDBOX_IMPLEMENT_CPU", "1024") or 1024),
            max_concurrent_sandboxes=int(os.environ.get("MAX_CONCURRENT_SANDBOXES", "2") or 2),
            vcpu_budget=float(os.environ.get("SANDBOX_VCPU_BUDGET", "3.0") or 3.0),
            vcpu_baseline=float(os.environ.get("SANDBOX_VCPU_BASELINE", "1.0") or 1.0),
        )


def _csv(value: str) -> List[str]:
    return [part for part in value.split(",") if part]


class FargateSandbox:
    """Runs agent / checks in an ECS Fargate task using the S3 bridge.

    The public methods mirror :class:`~slackagent.sandbox.DockerSandbox` so the
    worker can switch implementations without changing its call sites.
    """

    def __init__(
        self,
        config: FargateConfig = None,
        *,
        ecs=None,
        s3=None,
        region: str = None,
        bucket: str = None,
        cluster: str = None,
        task_definition: str = None,
        subnet_ids: List[str] = None,
        security_group_ids: List[str] = None,
    ):
        """Initialise the sandbox.

        A ``boto3`` session is created lazily because the worker runs in
        containers where the AWS SDK may be provisioned after import time.
        Inject ``ecs`` / ``s3`` clients to unit-test without AWS access.
        """
        if config is None:
            config = FargateConfig.from_env()
        if region:
            config.region = region
        if bucket:
            config.bucket = bucket
        if cluster:
            config.cluster = cluster
        if task_definition:
            config.task_definition = task_definition
        if subnet_ids is not None:
            config.subnet_ids = subnet_ids
        if security_group_ids is not None:
            config.security_group_ids = security_group_ids
        self.config = config
        self._ecs = ecs
        self._s3 = s3
        self._region = config.region
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ clients

    def _ecs_client(self):
        if self._ecs is None:
            import boto3

            self._ecs = boto3.client("ecs", region_name=self._region)
        return self._ecs

    def _s3_client(self):
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3", region_name=self._region)
        return self._s3

    # ------------------------------------------------------------------ public

    def run_agent(
        self,
        *,
        mode: str,
        repo_dir: Path,
        io_dir: Path,
        image: str,
        timeout_s: int,
        api_key: str,
    ) -> SandboxResult:
        """Run the agent in a Fargate task.

        ``api_key`` is part of the shared interface but is deliberately never
        passed into the task: the container runs with
        ``CLAUDE_CODE_USE_BEDROCK=1`` and authenticates to Bedrock through its
        task role.
        """
        if api_key:
            logger.warning(
                "FargateSandbox.run_agent ignores api_key (model backend auth is used instead)"
            )
        key_prefix = f"sandbox/{uuid.uuid4().hex}"
        self._upload_checkout(repo_dir, key_prefix)
        self._upload_io_dir(io_dir, key_prefix)

        cpu = self.config.implement_cpu
        if mode == "plan":
            cpu = self.config.plan_cpu
        command = ["python", "-m", "slackagent.fargate_entry", "agent", mode]
        return self._run_task(
            key_prefix=key_prefix,
            command=command,
            image=image,
            timeout_s=timeout_s,
            pull_io_back=True,
            io_dir=Path(io_dir),
            cpu=cpu,
        )

    def run_checks(
        self, *, repo_dir: Path, image: str, command: str, timeout_s: int
    ) -> SandboxResult:
        """Run a trusted (config-defined) command in a fresh Fargate task.

        ``fargate_entry`` extracts the checkout into the container before the
        command runs.
        """
        key_prefix = f"sandbox/{uuid.uuid4().hex}"
        self._upload_checkout(repo_dir, key_prefix)
        return self._run_task(
            key_prefix=key_prefix,
            command=["python", "-m", "slackagent.fargate_entry", "checks", command],
            image=image,
            timeout_s=timeout_s,
            pull_io_back=False,
        )

    def run_command(
        self, workdir: Path, command: List[str], timeout: Optional[int] = None
    ) -> dict[str, Any]:
        """Compatibility wrapper returning the old dict shape."""
        result = self.run_checks(
            repo_dir=workdir,
            image=self.config_shim_image(),
            command=" ".join(command),
            timeout_s=timeout or self._default_timeout(),
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }

    def preflight(self, images: List[str]) -> List[str]:
        """Report missing configuration / reachability problems (empty = OK)."""
        problems: List[str] = []
        if not self.config.bucket:
            problems.append("SANDBOX_BUCKET is not set")
        if not self.config.cluster:
            problems.append("SANDBOX_CLUSTER is not set")
        if not self.config.task_definition:
            problems.append("SANDBOX_TASK_DEFINITION is not set")
        if not self.config.subnet_ids:
            problems.append("SANDBOX_SUBNETS is not set")
        for image in dict.fromkeys(images):
            if not image:
                problems.append("empty sandbox image name configured")
        if self.config.model_backend == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
            problems.append("MODEL_BACKEND=anthropic but ANTHROPIC_API_KEY is not set on the worker")
        return problems

    def config_shim_image(self) -> str:
        """Return the image name configured on this sandbox (for preflight)."""
        return "slack-coding-agent-sandbox:latest"

    # ------------------------------------------------------------------ internals

    def _wait_for_sandbox_slot(self, ecs, cpu: int) -> None:
        """Block until a sandbox can launch within the fleet vCPU budget.

        A launch is allowed only when both hold:

        * running sandbox count < ``max_concurrent_sandboxes``, and
        * ``vcpu_baseline + running_sandbox_vcpu + this_cpu <= vcpu_budget``.

        The baseline covers the bot (0.5) + one worker (0.5) so the default
        budget of 3.0 permits at most 2 running sandboxes at 1.0 each.
        If ECS task listing is unavailable (tests/stubs) the gate is skipped.
        """
        if self.config.max_concurrent_sandboxes <= 0:
            return
        try:
            getattr(ecs, "list_tasks")
        except AttributeError:
            return
        deadline = time.monotonic() + self.config.gate_wait_max
        while True:
            running = self._count_running_sandbox_vcpu(ecs)
            if running is None:
                return
            launch_vcpu = max(cpu, 1) / 1024.0
            if running < self.config.max_concurrent_sandboxes and (
                self.config.vcpu_baseline + running + launch_vcpu
            ) <= self.config.vcpu_budget:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "sandbox concurrency/vCPU budget exceeded "
                    f"(max_concurrent={self.config.max_concurrent_sandboxes}, "
                    f"budget={self.config.vcpu_budget}, running={running:.1f}, "
                    f"requested={launch_vcpu:.1f})"
                )
            time.sleep(self.config.poll_interval)

    def _count_running_sandbox_vcpu(self, ecs) -> Optional[float]:
        """Estimate running sandbox vCPU via ECS task counts.

        Conservative: every running task is billed at the task definition's
        registered CPU (plan overrides down are ignored). Returns None when the
        ECS client doesn't support the APIs used (unit-test stubs).
        """
        try:
            resp = ecs.list_tasks(cluster=self.config.cluster, family=os.environ.get("SANDBOX_TASK_DEFINITION", "") or self.config.task_definition)
            arns = resp.get("taskArns") or []
            if not arns:
                return 0.0
            details = ecs.describe_tasks(cluster=self.config.cluster, tasks=arns[:20])
            running = [
                t for t in (details.get("tasks") or [])
                if (t.get("lastStatus") or "") == "RUNNING"
            ]
            return len(running) * self._sandbox_task_cpu(ecs)
        except Exception as exc:  # pragma: no cover - stub/network tolerance
            logger.warning("sandbox vCPU accounting unavailable (%s); skipping gate", exc)
            return None

    def _sandbox_task_cpu(self, ecs) -> float:
        try:
            resp = ecs.describe_task_definition(taskDefinition=self.config.task_definition)
            cpu = int((resp.get("taskDefinition") or {}).get("cpu", "1024"))
            return cpu / 1024.0
        except Exception:  # pragma: no cover
            return 1.0

    def _upload_checkout(self, repo_dir: Path, key_prefix: str) -> str:
        """Tar (without ``.git``) the checkout into S3 under ``key_prefix``."""
        repo_dir = Path(repo_dir).resolve()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for entry in sorted(repo_dir.rglob("*")):
                rel = entry.relative_to(repo_dir)
                parts = rel.parts
                if parts and parts[0] == ".git":
                    continue
                tar.add(entry, arcname=str(rel))
        buffer.seek(0)
        key = f"{key_prefix}/checkout.tar.gz"
        self._s3_client().put_object(
            Bucket=self.config.bucket,
            Key=key,
            Body=buffer.getvalue(),
            ContentType="application/gzip",
            ServerSideEncryption="AES256",
        )
        logger.info("uploaded checkout (%d bytes) to s3://%s/%s", buffer.getbuffer().nbytes, self.config.bucket, key)
        return key

    def _upload_io_dir(self, io_dir: Path, key_prefix: str) -> None:
        """Upload every file below ``io_dir`` under ``key_prefix/io``."""
        io_dir = Path(io_dir)
        if not io_dir.exists():
            return
        for entry in sorted(io_dir.rglob("*")):
            if entry.is_file():
                self._s3_client().put_object(
                    Bucket=self.config.bucket,
                    Key=f"{key_prefix}/io/{entry.relative_to(io_dir)}",
                    Body=entry.read_bytes(),
                    ServerSideEncryption="AES256",
                )

    def _run_task(
        self,
        *,
        key_prefix: str,
        command: List[str],
        image: str,
        timeout_s: int,
        pull_io_back: bool,
        io_dir: Optional[Path] = None,
        cpu: Optional[int] = None,
    ) -> SandboxResult:
        s3 = self._s3_client()
        ecs = self._ecs_client()

        # ── Model backend: choose how the sandbox runs the agent ──
        # sim       → no model call at all (deterministic fake agent)
        # anthropic → Claude direct API; worker injects ANTHROPIC_API_KEY
        # bedrock   → Claude Code via the Bedrock task role
        backend = self.config.model_backend
        env_entries = [
            {"name": "S3_BUCKET", "value": self.config.bucket},
            {"name": "SANDBOX_RUN_ID", "value": key_prefix},
            {"name": "MODEL_BACKEND", "value": backend},
            {"name": "AWS_REGION", "value": self._region},
            {"name": "PYTHONUNBUFFERED", "value": "1"},
        ]
        if backend == "anthropic":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if api_key:
                env_entries.append({"name": "ANTHROPIC_API_KEY", "value": api_key})
                env_entries.append({"name": "CLAUDE_CODE_USE_BEDROCK", "value": "0"})
        else:
            # bedrock (default) and sim both run without any provider key
            env_entries.append({"name": "CLAUDE_CODE_USE_BEDROCK", "value": "1"})

        container_overrides = {
            "name": self.config.container_name,
            "command": command,
            "environment": env_entries,
        }

        # ── Fleet cap: at most MAX_CONCURRENT_SANDBOXES and total vCPU ≤ budget ──
        self._wait_for_sandbox_slot(ecs, cpu or self.config.implement_cpu)

        overrides: dict[str, Any] = {"containerOverrides": [container_overrides]}
        if cpu:
            overrides["cpu"] = str(cpu)

        response = ecs.run_task(
            cluster=self.config.cluster,
            taskDefinition=self.config.task_definition,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self.config.subnet_ids,
                    "securityGroups": self.config.security_group_ids,
                    "assignPublicIp": self.config.assign_public_ip,
                }
            },
            count=1,
            overrides=overrides,
            propagateTags="TASK_DEFINITION",
        )

        failures = response.get("failures") or []
        if failures:
            reason = failures[0].get("reason", "unknown")
            raise RuntimeError(f"ECS RunTask failed to launch sandbox: {reason}")
        tasks = response.get("tasks") or []
        if not tasks:
            raise RuntimeError("ECS RunTask returned no tasks")
        task_arn = tasks[0]["taskArn"]
        logger.info("sandbox task started: %s", task_arn)

        timed_out = self._wait_until_stopped(ecs, task_arn, timeout_s)
        if timed_out:
            logger.warning("sandbox task %s exceeded %ss timeout; stopping", task_arn, timeout_s)
            ecs.stop_task(cluster=self.config.cluster, task=task_arn, reason=f"exceeded {timeout_s}s timeout")
            self._wait_until_stopped(ecs, task_arn, timeout_s, hard=True)

        returncode, stdout, stderr = self._collect_result(ecs, task_arn, key_prefix)
        if pull_io_back:
            self._pull_io(io_prefix=f"{key_prefix}/io", dest=Path(io_dir) if io_dir else Path("."))

        return SandboxResult(returncode, timed_out, tail(stdout, 8000), tail(stderr, 8000))

    def _wait_until_stopped(self, ecs, task_arn: str, timeout_s: int, hard: bool = False) -> bool:
        """Wait for the task to stop. Returns True if we had to enforce the timeout."""
        started = time.monotonic()
        interval = self.config.poll_interval
        while True:
            try:
                desc = ecs.describe_tasks(cluster=self.config.cluster, tasks=[task_arn])
                status = (desc.get("tasks") or [{}])[0].get("lastStatus", "")
            except Exception as e:  # pragma: no cover - defensive against transient errors
                logger.warning("describe_tasks error for %s: %s", task_arn, e)
                status = ""
            if status == "STOPPED":
                return False
            if not hard and time.monotonic() - started > timeout_s:
                return True
            if hard and time.monotonic() - started > 30:
                return True
            time.sleep(interval)

    def _collect_result(self, ecs, task_arn: str, key_prefix: str):
        """Pull stdout/stderr/returncode from the task (S3 result object + ECS)."""
        s3 = self._s3_client()
        result = None
        try:
            blob = s3.get_object(
                Bucket=self.config.bucket,
                Key=f"{key_prefix}/result.json",
            )["Body"].read()
            import json as _json

            result = _json.loads(blob)
        except Exception as e:
            logger.warning("no result.json for %s (%s); falling back to ECS exit code", task_arn, e)

        exit_code = None
        if result is not None:
            exit_code = result.get("returncode")
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
        else:
            try:
                desc = ecs.describe_tasks(cluster=self.config.cluster, tasks=[task_arn])
                containers = (desc.get("tasks") or [{}])[0].get("containers") or []
                exit_code = containers[0].get("exitCode")
            except Exception:  # pragma: no cover
                pass
            stdout = ""
            stderr = ""
        return exit_code, stdout, stderr

    def _pull_io(self, io_prefix: str, dest: Path) -> None:
        """Download result files produced by the container into ``dest``."""
        s3 = self._s3_client()
        local = Path(dest)
        local.mkdir(parents=True, exist_ok=True)
        try:
            response = s3.list_objects_v2(
                Bucket=self.config.bucket, Prefix=f"{io_prefix}/"
            )
        except Exception as e:  # pragma: no cover
            logger.warning("list_objects_v2 error for %s: %s", io_prefix, e)
            return
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(io_prefix) + 1:]
            dest = local / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(self.config.bucket, key, str(dest))

    @staticmethod
    def _default_timeout() -> int:
        return int(os.environ.get("SANDBOX_TIMEOUT", "300"))