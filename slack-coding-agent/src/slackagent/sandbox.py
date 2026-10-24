"""Local Docker sandbox: one throwaway container per agent run or check.

Isolation choices (all deliberate):
  * read-only root filesystem, /tmp is a size-limited tmpfs;
  * all Linux capabilities dropped, ``no-new-privileges``, pid/memory/CPU limits;
  * runs as the host user so it can write the mounted checkout;
  * ``/workspace/.git`` is mounted read-only so the agent cannot plant git
    config or hooks that would later run on the host;
  * no GitHub credentials ever enter a container. The agent container only
    gets the AI API key (via the environment, never argv) and only gets
    network access when the AI provider needs it;
  * repository code (tests, linters) runs only in check containers, which
    always have the network disabled;
  * a hard wall-clock timeout kills the container.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .config import DockerConfig
from .guards import tail


@dataclass
class SandboxResult:
    """Result of a sandbox execution."""

    returncode: int
    timed_out: bool
    stdout: str
    stderr: str


class DockerSandbox:
    """Runs the agent and trusted checks in hardened Docker containers."""

    def __init__(
        self,
        docker_config: DockerConfig,
        network_mode: str = "none",
        agent_network: str = "bridge",
        ai_provider: str = "gemini",
    ):
        self.config = docker_config
        self.checks_network = network_mode
        # The offline "sim" provider needs no network at all.
        self.agent_network = "none" if ai_provider == "sim" else agent_network
        self.ai_provider = ai_provider

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
        """Run the agent (``mode`` = plan | implement) in a fresh container.

        Planning mounts the checkout read-only; implementation read-write.
        """
        if mode not in ("plan", "implement"):
            raise ValueError(f"unknown agent mode: {mode}")
        name = f"slackagent-{mode}-{uuid.uuid4().hex[:8]}"
        args = self._base_args(
            name, self.agent_network, repo_dir, workspace_rw=(mode == "implement")
        )
        args += ["-v", f"{Path(io_dir).resolve()}:/io:rw"]

        child_env: Dict[str, str] = {}
        args += ["-e", f"AI_PROVIDER={self.ai_provider}"]
        if api_key:
            # "-e NAME" without a value copies it from the docker CLI's own
            # environment, keeping the secret out of the process list.
            args += ["-e", "GEMINI_API_KEY"]
            child_env["GEMINI_API_KEY"] = api_key
        https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if https_proxy and self.agent_network != "none":
            args += ["-e", f"HTTPS_PROXY={https_proxy}"]

        args += [
            image,
            "python", "-m", "slackagent.agent_runner",
            "--mode", mode,
            "--input", "/io/input.json",
            "--output", "/io/output.json",
        ]
        return self._run(args, name=name, timeout_s=timeout_s, extra_env=child_env)

    def run_checks(
        self, *, repo_dir: Path, image: str, command: str, timeout_s: int
    ) -> SandboxResult:
        """Run a trusted command from config (never model-written), network off."""
        name = f"slackagent-checks-{uuid.uuid4().hex[:8]}"
        args = self._base_args(name, self.checks_network, repo_dir, workspace_rw=True)
        args += [image, "sh", "-c", command]
        return self._run(args, name=name, timeout_s=timeout_s)

    def preflight(self, images: List[str]) -> List[str]:
        """Check Docker availability and images. Returns problems (empty = OK)."""
        try:
            info = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"Docker is not available: {e}"]
        if info.returncode != 0:
            return ["Docker daemon is not reachable (is it running, and do you have access?)"]

        problems: List[str] = []
        for image in dict.fromkeys(images):
            found = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
            if found.returncode != 0:
                problems.append(
                    f"Sandbox image {image!r} not found - build it with: "
                    f"docker build -t {image} ."
                )
        return problems

    # ------------------------------------------------------------------ internals

    def _base_args(
        self, name: str, network: str, repo_dir: Path, *, workspace_rw: bool
    ) -> List[str]:
        """Hardened ``docker run`` arguments shared by every container."""
        repo = Path(repo_dir).resolve()
        args = [
            "docker", "run", "--rm", "--name", name,
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.config.pids_limit),
            "--memory", self.config.memory_limit,
            "--memory-swap", self.config.memory_limit,
            "--cpus", self.config.cpu_limit,
            "--network", network,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp/home",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{repo}:/workspace:{'rw' if workspace_rw else 'ro'}",
        ]
        if (repo / ".git").exists():
            args += ["-v", f"{repo / '.git'}:/workspace/.git:ro"]
        args += ["-w", "/workspace"]
        return args

    def _run(
        self,
        args: List[str],
        *,
        name: str,
        timeout_s: int,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> SandboxResult:
        """Execute ``docker run`` and kill the container on timeout."""
        env = {**os.environ, **(extra_env or {})}
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
        )
        try:
            out, err = proc.communicate(timeout=timeout_s)
            return SandboxResult(proc.returncode, False, tail(out, 8000), tail(err, 8000))
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            proc.kill()
            out, err = proc.communicate()
            return SandboxResult(-9, True, tail(out, 8000), tail(err, 8000))
