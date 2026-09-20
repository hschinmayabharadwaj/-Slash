"""Docker sandbox with advanced security: one throwaway container per agent run.

Isolation choices (all deliberate):
  * root filesystem read-only, /tmp is a tmpfs, all Linux capabilities dropped;
  * runs as the host user so it can write the mounted checkout, but /workspace/.git is
    mounted read-only (the agent cannot plant git config that would run on the host);
  * no GitHub token enters the container -- only GEMINI_API_KEY;
  * memory / CPU / pid limits, and a hard wall-clock timeout that kills the container;
  * tests and linters run with the network disabled by default.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    """Manages Docker container execution for agent and checks."""
    
    def __init__(self, docker_config: DockerConfig, network_mode: str = "none"):
        """Initialize Docker sandbox.
        
        Args:
            docker_config: Docker configuration
            network_mode: Network mode for containers
        """
        self.config = docker_config
        self.network_mode = network_mode
        self.agent_network = network_mode  # Can be configured separately
        self.checks_network = "none"  # Always isolated for checks

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
        """Run agent in a container with Gemini API access.
        
        Args:
            mode: "plan" (read-only) or "implement" (read-write)
            repo_dir: Repository directory to mount
            io_dir: I/O directory for input/output.json
            image: Docker image name
            timeout_s: Timeout in seconds
            api_key: Gemini API key
            
        Returns:
            SandboxResult with execution details
        """
        name = f"slackagent-{mode}-{uuid.uuid4().hex[:8]}"
        workspace_rw = (mode == "implement")
        
        args = self._base_args(
            name, 
            self.agent_network, 
            repo_dir, 
            workspace_rw=workspace_rw
        )
        
        # Mount I/O directory
        args += ["-v", f"{Path(io_dir).resolve()}:/io:rw"]
        
        # Pass Gemini API key
        args += ["-e", f"GEMINI_API_KEY={api_key}"]
        
        # Add HTTPS proxy if configured
        https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if https_proxy:
            args += ["-e", f"HTTPS_PROXY={https_proxy}", "-e", f"https_proxy={https_proxy}"]
        
        # Run Python agent script (will be created in Phase 2)
        args += [
            image, 
            "python", "-m", "slackagent.agent_runner",
            "--mode", mode,
            "--input", "/io/input.json",
            "--output", "/io/output.json",
        ]
        
        return self._run(args, name=name, timeout_s=timeout_s)

    def run_checks(
        self, 
        *, 
        repo_dir: Path, 
        image: str, 
        command: str, 
        timeout_s: int
    ) -> SandboxResult:
        """Run trusted command from config (never model-written) in a fresh container.
        
        Args:
            repo_dir: Repository directory
            image: Docker image name
            command: Shell command to run
            timeout_s: Timeout in seconds
            
        Returns:
            SandboxResult with execution details
        """
        name = f"slackagent-checks-{uuid.uuid4().hex[:8]}"
        args = self._base_args(
            name, 
            self.checks_network, 
            repo_dir, 
            workspace_rw=True
        )
        args += [image, "sh", "-c", command]
        return self._run(args, name=name, timeout_s=timeout_s)

    def run_command(
        self, 
        workdir: Path, 
        command: list[str], 
        timeout: Optional[int] = None
    ) -> dict[str, any]:
        """Run a command in sandboxed container (compatibility with old interface).
        
        Args:
            workdir: Working directory to mount
            command: Command to execute
            timeout: Optional timeout in seconds
            
        Returns:
            Dict with 'stdout', 'stderr', 'exit_code'
        """
        name = f"slackagent-cmd-{uuid.uuid4().hex[:8]}"
        args = self._base_args(
            name,
            self.checks_network,
            workdir,
            workspace_rw=True
        )
        args += [self.config.image] + command
        
        result = self._run(args, name=name, timeout_s=timeout or self.config.timeout)
        
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }

    def preflight(self, images: list[str]) -> list[str]:
        """Check Docker availability and images. Called at startup.
        
        Args:
            images: List of Docker images to check
            
        Returns:
            List of problems (empty = OK)
        """
        problems: list[str] = []
        
        try:
            info = subprocess.run(
                ["docker", "info"], 
                capture_output=True, 
                timeout=20
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"Docker is not available: {e}"]
        
        if info.returncode != 0:
            return ["Docker daemon is not reachable (is it running, and do you have access?)"]
        
        for image in dict.fromkeys(images):
            found = subprocess.run(
                ["docker", "image", "inspect", image], 
                capture_output=True
            )
            if found.returncode != 0:
                problems.append(
                    f"Sandbox image {image!r} not found - build it (see README)"
                )
        
        if not os.environ.get("GEMINI_API_KEY"):
            problems.append("GEMINI_API_KEY is not set")
        
        return problems

    # ------------------------------------------------------------------ internals

    def _base_args(
        self, 
        name: str, 
        network: str, 
        repo_dir: Path, 
        *, 
        workspace_rw: bool
    ) -> list[str]:
        """Build base Docker run arguments with security hardening.
        
        Args:
            name: Container name
            network: Network mode
            repo_dir: Repository directory
            workspace_rw: Whether workspace should be read-write
            
        Returns:
            List of Docker arguments
        """
        repo = Path(repo_dir).resolve()
        
        return [
            "docker", "run", "--rm", "--name", name,
            # Security: read-only root filesystem
            "--read-only",
            # Security: writable /tmp with restrictions
            "--tmpfs", "/tmp:rw,exec,size=1g",
            # Security: drop all capabilities
            "--cap-drop", "ALL",
            # Security: prevent privilege escalation
            "--security-opt", "no-new-privileges",
            # Resource limits: PIDs
            "--pids-limit", "64",
            # Resource limits: Memory
            "--memory", self.config.memory_limit,
            # Resource limits: CPUs
            "--cpus", self.config.cpu_limit,
            # Network isolation
            "--network", network,
            # Run as current user (not root)
            "--user", f"{os.getuid()}:{os.getgid()}",
            # Environment
            "-e", "HOME=/tmp/home",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            # Mount workspace
            "-v", f"{repo}:/workspace:{'rw' if workspace_rw else 'ro'}",
            # Security: .git is always read-only to prevent config injection
            "-v", f"{repo / '.git'}:/workspace/.git:ro",
            # Working directory
            "-w", "/workspace",
        ]

    def _run(
        self, 
        args: list[str], 
        *, 
        name: str, 
        timeout_s: int
    ) -> SandboxResult:
        """Execute Docker command with timeout handling.
        
        Args:
            args: Docker command arguments
            name: Container name
            timeout_s: Timeout in seconds
            
        Returns:
            SandboxResult with execution details
        """
        proc = subprocess.Popen(
            args, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            errors="replace"
        )
        
        try:
            out, err = proc.communicate(timeout=timeout_s)
            return SandboxResult(
                proc.returncode, 
                False, 
                tail(out, 8000), 
                tail(err, 8000)
            )
        except subprocess.TimeoutExpired:
            # Kill the container
            subprocess.run(
                ["docker", "kill", name], 
                capture_output=True
            )
            proc.kill()
            out, err = proc.communicate()
            return SandboxResult(
                -9, 
                True, 
                tail(out, 8000), 
                tail(err, 8000)
            )


# Alias for compatibility
class SandboxRunner(DockerSandbox):
    """Compatibility alias for old interface."""
    pass
