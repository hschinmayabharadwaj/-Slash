"""Container-side bootstrap for the Fargate sandbox.

This module runs INSIDE the sandbox task. It performs the S3 bridge:

1. download ``sandbox/<run-id>/checkout.tar.gz`` and extract it into
   ``/workspace``;
2. for agent runs, download the ``sandbox/<run-id>/io/*`` input files into
   ``/io``, invoke the agent (Claude Code against Amazon Bedrock -- the task
   role, since ``CLAUDE_CODE_USE_BEDROCK=1``), then write ``/io/output.json``
   and upload the whole ``io`` directory back to S3 for the host;
3. for checks runs, execute the trusted ``sh -c <command>`` in ``/workspace``
   and upload a ``result.json`` (returncode / stdout / stderr) to S3.

No GitHub token and no provider API key are provisioned in this container; the
agent authenticates to Bedrock through the task's IAM role.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

logger = logging.getLogger("fargate_entry")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

WORKSPACE = Path("/workspace")
IO_DIR = Path("/io")


def _s3():
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _env_or_die(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _download_checkout(bucket: str, run_id: str) -> None:
    """Stream the checkout tarball from S3 and extract into /workspace."""
    key = f"{run_id}/checkout.tar.gz"
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as tmp:
        _s3().download_fileobj(bucket, key, tmp)
        tmp.seek(0)
        with tarfile.open(fileobj=tmp, mode="r:gz") as tar:
            _safe_extract_all(tar, WORKSPACE)


def _git_baseline() -> None:
    """Turn /workspace into a throwaway git repo so the diff of changes is
    available even though the S3 checkout tarball excludes the original .git."""
    subprocess.run(["git", "-C", str(WORKSPACE), "init", "-q"], check=False)
    subprocess.run(
        ["git", "-C", str(WORKSPACE), "config", "user.email", "sandbox@example.invalid"],
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(WORKSPACE), "config", "user.name", "sandbox-agent"],
        check=False,
    )
    subprocess.run(["git", "-C", str(WORKSPACE), "add", "-A"], check=False)
    subprocess.run(["git", "-C", str(WORKSPACE), "commit", "-qm", "baseline"], check=False)


def _git_diff(max_chars: int = 60000) -> str:
    proc = subprocess.run(
        ["git", "-C", str(WORKSPACE), "diff", "--no-color", "-U5"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout[:max_chars]


def _safe_extract_all(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract while rejecting path traversal (../ or absolute members)."""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise RuntimeError(f"unsafe member path in checkout tarball: {member.name}")
    tar.extractall(path=dest, filter="data")


def _download_io(bucket: str, run_id: str) -> None:
    """Download every io/* object to /io."""
    IO_DIR.mkdir(parents=True, exist_ok=True)
    s3 = _s3()
    response = s3.list_objects_v2(Bucket=bucket, Prefix=f"{run_id}/io/")
    for obj in response.get("Contents", []):
        key = obj["Key"]
        rel = key[len(f"{run_id}/io/") :]
        if not rel:
            continue
        target = IO_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, key, str(target))


def _upload_io(bucket: str, run_id: str) -> None:
    s3 = _s3()
    for entry in IO_DIR.rglob("*"):
        if entry.is_file():
            s3.upload_file(
                str(entry), bucket, f"{run_id}/io/{entry.relative_to(IO_DIR)}"
            )


def _upload_result(bucket: str, run_id: str, returncode: int, stdout: str, stderr: str) -> None:
    payload = json.dumps(
        {
            "returncode": returncode,
            "stdout": stdout[-8000:],
            "stderr": stderr[-8000:],
        }
    )
    _s3().put_object(
        Bucket=bucket,
        Key=f"{run_id}/result.json",
        Body=payload.encode("utf-8"),
        ContentType="application/json",
    )


def _claude_json(prompt: str, timeout_s: int = 3600) -> dict:
    """Run Claude Code non-interactively (Bedrock via task role)."""
    env = dict(os.environ)
    env.setdefault("CLAUDE_CODE_USE_BEDROCK", "1")
    env.setdefault("ANTHROPIC_BEDROCK_REGION", env.get("AWS_REGION", "us-east-1"))
    if os.environ.get("SANDBOX_CLAUDE_BEDROCK_PROFILE"):
        env["ANTHROPIC_BEDROCK_PROFILE"] = os.environ["SANDBOX_CLAUDE_BEDROCK_PROFILE"]
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json", "--verbose"],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        data = {"content": [{"text": proc.stdout[:4000]}]}
    return {"done": proc.returncode == 0, "raw": data, "returncode": proc.returncode}


def _app_files() -> list[str]:
    """Code/document files in the workspace (for the sim backend)."""
    files = []
    for path in sorted(WORKSPACE.rglob("*")):
        if path.is_file() and path.suffix in (
            ".py", ".ts", ".js", ".go", ".rs", ".java", ".json", ".yaml", ".yml",
            ".md", ".toml", ".txt", ".sh",
        ):
            if ".git" not in path.parts and "__pycache__" not in path.parts:
                files.append(str(path.relative_to(WORKSPACE)))
    return files[:50]


def _sim_plan(task_description: str) -> dict:
    """Deterministic stand-in plan used when no model backend is reachable."""
    files = _app_files() or ["README.md"]
    return {
        "feasible": True,
        "summary": (
            f"SIM-MODE plan (no model backend). Request: {task_description[:240]}. "
            f"Will add a sim-change marker against {len(files)} file(s) so the "
            "approval → implement → PR flow can be exercised end to end."
        ),
        "files": files,
        "risk": "low",
        "risk_reason": "simulated change; no model involved",
        "questions": [],
        "how_to_verify": "Run the repository checks",
    }


def _sim_implement(task_description: str) -> None:
    """Make a visible but harmless change in the workspace."""
    files = _app_files()
    target = None
    for candidate in files:
        if candidate.lower().endswith("readme.md"):
            target = Path("README.md")
            break
    if target is None:
        target = Path(files[0]) if files else Path("sim-change.md")
    abs_target = WORKSPACE / target
    abs_target.parent.mkdir(parents=True, exist_ok=True)
    marker = f"\n<!-- sim-change @ {task_description[:80] or 'demo'} -->\n"
    existing = abs_target.read_text(errors="replace") if abs_target.exists() else "sim-change\n"
    abs_target.write_text(existing + marker)


def _run_sim(mode: str, task_input: dict, task_description: str) -> dict:
    """Run the deterministic fallback path (no model, no Bedrock)."""
    if mode == "plan":
        return {
            "ok": True,
            "text": "SIM plan",
            "plan": _sim_plan(task_description),
            "cost_usd": 0.0,
            "num_turns": 0,
            "sim": True,
        }
    _sim_implement(task_description)
    return {
        "ok": True,
        "text": "SIM implementation applied (marker change).",
        "summary": "SIM implementation applied (marker change).",
        "cost_usd": 0.0,
        "num_turns": 0,
        "sim": True,
    }


def _run_agent(mode: str) -> int:
    bucket = _env_or_die("S3_BUCKET")
    run_id = _env_or_die("SANDBOX_RUN_ID")
    _download_checkout(bucket, run_id)
    _download_io(bucket, run_id)
    _git_baseline()

    input_path = IO_DIR / "input.json"
    if not input_path.exists():
        raise SystemExit(f"/io/input.json missing for agent mode '{mode}'")
    task_input = json.loads(input_path.read_text())
    task_description = task_input.get(
        "task_description", task_input.get("task", "Implement the change.")
    )

    if mode == "plan":
        prompt = (
            "You are a coding assistant. Analyze the repository at /workspace and create a "
            "detailed implementation plan for this request:\n\n"
            f"{task_description}\n\n"
            "Reply with: a concise summary of the plan, the files that would change, "
            "the approach, risks and how to verify it.\n"
        )
        key = "plan"
    else:
        plan = task_input.get("plan", "")
        prompt = (
            "You are a coding assistant implementing an approved plan.\n"
            "Original request:\n"
            f"{task_description}\n\n"
            "Approved plan:\n"
            f"{plan}\n\n"
            "Make the changes in /workspace. Then reply with a concise summary of what "
            "you changed and how you verified it (do not modify .git, do not touch "
            "files outside /workspace).\n"
        )
        key = "implementation"

    sim = os.environ.get("SANDBOX_SIM") == "1" or shutil.which("claude") is None
    if sim:
        logger.warning("no `claude` binary / SANDBOX_SIM=1 — using SIM backend for %s", mode)
        output = _run_sim(mode, task_input, task_description)
        returncode = 0
    else:
        result = _claude_json(prompt)
        claude_text = _extract_text(result["raw"])
        if result["done"]:
            if key == "plan":
                output = {
                    "ok": True,
                    "text": claude_text,
                    "plan": _plan_object(claude_text),
                    "cost_usd": _cost_from_raw(result["raw"]),
                    "num_turns": 1,
                }
            else:
                output = {
                    "ok": True,
                    "text": claude_text,
                    "summary": claude_text,
                    "cost_usd": _cost_from_raw(result["raw"]),
                    "num_turns": 1,
                }
            returncode = 0
        else:
            logger.warning(
                "claude call failed (rc=%s); falling back to SIM backend for %s",
                result["returncode"], mode,
            )
            output = _run_sim(mode, task_input, task_description)
            returncode = 0

    (IO_DIR / "output.json").write_text(json.dumps(output))
    if mode == "implement":
        diff = _git_diff()
        if diff:
            output["diff"] = diff
            (IO_DIR / "output.json").write_text(json.dumps(output))
    _upload_io(bucket, run_id)
    _upload_result(bucket, run_id, returncode, json.dumps(output)[:2000], "")
    return returncode


def _extract_text(raw: dict) -> str:
    try:
        content = raw.get("content") or []
        texts = [block.get("text", "") for block in content if isinstance(block, dict)]
        return "\n".join(texts).strip() or str(raw)
    except Exception:
        return str(raw)


def _cost_from_raw(raw: dict) -> float:
    try:
        meta = raw.get("meta") or {}
        usage = meta.get("usage") or {}
        return float(usage.get("cost_usd", 0.0) or 0.0)
    except Exception:
        return 0.0


def _plan_object(text: str) -> dict:
    return {
        "feasible": True,
        "summary": text[:2000],
        "files": _extract_files(text),
        "risk": "medium",
        "risk_reason": "model-generated plan; human approval gate blocks risky changes",
        "questions": [],
        "how_to_verify": "Run the repository checks",
    }


def _extract_files(text: str):
    import re

    files = re.findall(r"[`']?([a-zA-Z0-9_./-]+\.(?:py|ts|js|go|rs|java|yaml|yml|json|md|toml|txt))[`']?", text)
    return list(dict.fromkeys(files))[:20]


def _run_checks(command: str) -> int:
    bucket = _env_or_die("S3_BUCKET")
    run_id = _env_or_die("SANDBOX_RUN_ID")
    _download_checkout(bucket, run_id)

    proc = subprocess.run(
        ["sh", "-c", command],
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("SANDBOX_CHECK_TIMEOUT", "900")),
    )
    _upload_result(bucket, run_id, proc.returncode, proc.stdout, proc.stderr)
    return proc.returncode


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m slackagent.fargate_entry agent <plan|implement> | checks <command>")
    kind = sys.argv[1]
    if kind == "agent":
        mode = sys.argv[2] if len(sys.argv) > 2 else "plan"
        if mode not in ("plan", "implement"):
            raise SystemExit(f"unknown agent mode: {mode}")
        return _run_agent(mode)
    if kind == "checks":
        command = " ".join(sys.argv[2:])
        return _run_checks(command)
    raise SystemExit(f"unknown command kind: {kind}")


if __name__ == "__main__":
    sys.exit(main())