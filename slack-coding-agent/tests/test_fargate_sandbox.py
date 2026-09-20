"""Tests for the Fargate sandbox (stubbed AWS clients, no AWS access)."""

import io
import json
from pathlib import Path

import pytest

from slackagent.fargate_sandbox import FargateConfig, FargateSandbox
from slackagent.sandbox import SandboxResult


class FakeS3:
    """In-memory S3 client."""

    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, ContentType=None, ServerSideEncryption=None):
        self.objects[f"{Bucket}/{Key}"] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, *, Bucket, Key):
        class Body:
            def __init__(self, data):
                self._buf = io.BytesIO(data)

            def read(self):
                return self._buf.read()

        return {"Body": Body(self.objects[f"{Bucket}/{Key}"])}

    def list_objects_v2(self, *, Bucket, Prefix):
        keys = sorted(k.split("/", 1)[1] for k in self.objects if k.startswith(f"{Bucket}/"))
        contents = [{"Key": k} for k in keys if k.startswith(Prefix)]
        return {"Contents": contents}

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[f"{Bucket}/{Key}"])

    def upload_file(self, Filename, Bucket, Key):
        self.objects[f"{Bucket}/{Key}"] = Path(Filename).read_bytes()

    def download_fileobj(self, Bucket, Key, fileobj):
        fileobj.write(self.objects[f"{Bucket}/{Key}"])


class FakeECS:
    """In-memory ECS client with controllable task lifecycle."""

    def __init__(self, describe_pattern=None):
        self.pattern = list(describe_pattern or ["STOPPED"])
        self.stopped = False
        self.exit_code = 0
        self.calls = {"run_task": 0, "stop_task": 0}
        self.last_run_kwargs = None

    def run_task(self, **kwargs):
        self.calls["run_task"] += 1
        self.last_run_kwargs = kwargs
        return {"tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123456789012:task/cluster/abc"}]}

    def describe_tasks(self, *, cluster, tasks):
        if self.stopped or (self.pattern and self.pattern[0] == "STOPPED"):
            return {"tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": self.exit_code}]}]}
        if self.pattern:
            return {"tasks": [{"lastStatus": self.pattern.pop(0)}]}
        return {"tasks": [{"lastStatus": "RUNNING"}]}

    def stop_task(self, *, cluster, task, reason=None):
        self.calls["stop_task"] += 1
        self.stopped = True


@pytest.fixture
def io_dir(tmp_path):
    d = tmp_path / "io"
    d.mkdir()
    return d


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "main.py").write_text("print('hi')\n")
    (r / ".git").mkdir()
    return r


def make_sandbox(tmp_path, *, ecs=None, s3=None, exit_timeout=False):
    if s3 is None:
        s3 = FakeS3()
    if ecs is None:
        ecs = FakeECS(describe_pattern=["RUNNING", "STOPPED"])
    config = FargateConfig(
        bucket="sandbox-bucket",
        cluster="sandbox-cluster",
        task_definition="sandbox-taskdef",
        container_name="sandbox",
        subnet_ids=["subnet-1"],
        security_group_ids=["sg-1"],
        region="us-east-1",
        poll_interval=0.01,
    )
    return FargateSandbox(config, ecs=ecs, s3=s3)


def test_run_agent_bridges_io_and_never_leaks_api_key(tmp_path, repo, io_dir):
    s3 = FakeS3()
    ecs = FakeECS(describe_pattern=["RUNNING", "STOPPED"])
    sandbox = make_sandbox(tmp_path, ecs=ecs, s3=s3)

    (io_dir / "input.json").write_text(json.dumps({"task": "add tests"}))

    result = sandbox.run_agent(
        mode="plan", repo_dir=repo, io_dir=io_dir, image="img:latest",
        timeout_s=30, api_key="SECRET-KEY",
    )

    assert isinstance(result, SandboxResult)
    assert result.returncode == 0
    assert result.timed_out is False

    run_kwargs = ecs.last_run_kwargs
    assert run_kwargs["launchType"] == "FARGATE"
    assert run_kwargs["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "ENABLED"

    overrides = run_kwargs["overrides"]["containerOverrides"][0]
    env = {e["name"]: e["value"] for e in overrides["environment"]}
    assert "SECRET-KEY" not in json.dumps(env)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["S3_BUCKET"] == "sandbox-bucket"
    assert env["SANDBOX_RUN_ID"].startswith("sandbox/")
    assert overrides["command"][:3] == ["python", "-m", "slackagent.fargate_entry"]
    assert overrides["command"][3:] == ["agent", "plan"]

    run_id = env["SANDBOX_RUN_ID"]
    assert f"sandbox-bucket/{run_id}/checkout.tar.gz" in s3.objects
    assert f"sandbox-bucket/{run_id}/io/input.json" in s3.objects


def test_run_agent_timeout_stops_task(tmp_path, repo, io_dir):
    ecs = FakeECS(describe_pattern=["RUNNING"])
    sandbox = make_sandbox(tmp_path, ecs=ecs, s3=FakeS3())

    (io_dir / "input.json").write_text(json.dumps({"task": "x"}))
    result = sandbox.run_agent(
        mode="plan", repo_dir=repo, io_dir=io_dir, image="img", timeout_s=1, api_key=None,
    )

    assert result.timed_out is True
    assert ecs.calls["stop_task"] > 0


def test_run_agent_pulls_output_back_into_io_dir(tmp_path, repo, io_dir):
    s3 = FakeS3()
    ecs = FakeECS(describe_pattern=["RUNNING", "STOPPED"])
    sandbox = make_sandbox(tmp_path, ecs=ecs, s3=s3)

    (io_dir / "input.json").write_text(json.dumps({"task": "x"}))

    def capture_before_pull(**kwargs):
        sandbox_io = next(
            k for k in s3.objects if k.endswith("io/input.json")
        )
        output_key = sandbox_io.replace("io/input.json", "io/output.json")
        s3.objects[output_key] = json.dumps({"ok": True, "plan": {"feasible": True, "summary": "s"}}).encode()

    orig_run_task = ecs.run_task

    def run_task(**kwargs):
        result = orig_run_task(**kwargs)
        sandbox_io = next(
            k for k in s3.objects if k.endswith("io/input.json")
        )
        output_key = sandbox_io.replace("io/input.json", "io/output.json")
        s3.objects[output_key] = json.dumps({"ok": True, "plan": {"feasible": True, "summary": "s"}}).encode()
        return result

    ecs.run_task = run_task
    sandbox.run_agent(mode="plan", repo_dir=repo, io_dir=io_dir, image="img", timeout_s=30, api_key=None)

    assert (io_dir / "output.json").exists()
    payload = json.loads((io_dir / "output.json").read_text())
    assert payload["ok"] is True


def test_run_checks_uses_fargate_entry(tmp_path, repo):
    ecs = FakeECS(describe_pattern=["RUNNING", "STOPPED"])
    ecs.exit_code = 7
    sandbox = make_sandbox(tmp_path, ecs=ecs, s3=FakeS3())

    result = sandbox.run_checks(repo_dir=repo, image="img", command="pytest", timeout_s=30)

    assert result.returncode == 7
    command = ecs.last_run_kwargs["overrides"]["containerOverrides"][0]["command"]
    assert command == ["python", "-m", "slackagent.fargate_entry", "checks", "pytest"]


def test_preflight_reports_missing_config():
    sandbox = FargateSandbox(FargateConfig(bucket="", cluster="", task_definition="", region="us-east-1"))
    problems = sandbox.preflight(["img"])
    assert problems, "preflight should flag empty config"