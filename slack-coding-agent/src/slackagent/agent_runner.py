"""Agent runner with sandboxed Docker execution."""

import json
import importlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AgentConfig, DockerConfig
from .security import SecurityManager


class AgentError(Exception):
    """Raised when agent execution fails."""

    pass


def _create_anthropic_client(api_key: str) -> Any:
    """Create an Anthropic client, reporting a useful error if unavailable."""
    try:
        anthropic = importlib.import_module("anthropic")
        return anthropic.Anthropic(api_key=api_key)
    except (ImportError, AttributeError) as exc:
        raise AgentError(
            "The 'anthropic' package is required. Install it with: pip install anthropic"
        ) from exc


class SandboxRunner:
    """Runs code in isolated Docker containers."""

    def __init__(self, docker_config: DockerConfig, network_mode: str = "none"):
        """Initialize sandbox runner.

        Args:
            docker_config: Docker configuration
            network_mode: Docker network mode
        """
        self.config = docker_config
        self.network_mode = network_mode

    def run_command(
        self, workdir: Path, command: List[str], timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """Run a command in a sandboxed container.

        Args:
            workdir: Working directory to mount
            command: Command to execute
            timeout: Optional timeout in seconds

        Returns:
            Dict with 'stdout', 'stderr', 'exit_code'

        Raises:
            AgentError: If execution fails
        """
        # Build docker run command
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--network", self.network_mode,
            "--memory", self.config.memory_limit,
            "--cpus", self.config.cpu_limit,
            "--user", "sandbox",
            "--read-only",  # Read-only root filesystem
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=100m",
            "-v", f"{workdir.absolute()}:/workspace:rw",
            "-w", "/workspace",
            self.config.image,
        ]
        docker_cmd.extend(command)

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.config.timeout,
            )

            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }

        except subprocess.TimeoutExpired:
            raise AgentError(f"Command timed out after {timeout or self.config.timeout}s")
        except Exception as e:
            raise AgentError(f"Failed to run sandbox command: {e}")


class AgentRunner:
    """Runs Claude agent with tool support in sandbox."""

    def __init__(
        self,
        agent_config: AgentConfig,
        docker_config: DockerConfig,
        security_manager: SecurityManager,
        api_key: str,
    ):
        """Initialize agent runner.

        Args:
            agent_config: Agent configuration
            docker_config: Docker configuration
            security_manager: Security manager
            api_key: Anthropic API key
        """
        self.agent_config = agent_config
        self.security_manager = security_manager
        self.sandbox = SandboxRunner(docker_config, security_manager.config.network_mode)
        self.client = _create_anthropic_client(api_key)

    def run_planning_phase(
        self, workdir: Path, task_description: str
    ) -> str:
        """Run planning phase to generate implementation plan.

        Args:
            workdir: Repository working directory
            task_description: Task description from user

        Returns:
            Generated plan

        Raises:
            AgentError: If planning fails
        """
        system_prompt = f"""You are a coding assistant helping to plan implementation tasks.

Your task: Analyze the codebase and create a detailed implementation plan for the following request:

{task_description}

Your plan should include:
1. Files to be created or modified
2. Key changes to make in each file
3. Testing approach
4. Potential risks or concerns

Be specific and actionable. Format your plan clearly with numbered steps.

You have access to the repository at /workspace. You can read files to understand the codebase structure.
"""

        try:
            # Create a simple message to Claude
            response = self.client.messages.create(
                model=self.agent_config.model,
                max_tokens=self.agent_config.planning_budget,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": f"Please create an implementation plan for this task in the repository at /workspace:\n\n{task_description}",
                    }
                ],
            )

            # Extract text from response
            plan_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    plan_text += block.text

            if not plan_text:
                raise AgentError("No plan generated by agent")

            return plan_text

        except Exception as e:
            raise AgentError(f"Planning phase failed: {e}")

    def run_implementation_phase(
        self,
        workdir: Path,
        task_description: str,
        plan: str,
    ) -> str:
        """Run implementation phase to execute the plan.

        Args:
            workdir: Repository working directory
            task_description: Original task description
            plan: Approved implementation plan

        Returns:
            Summary of changes made

        Raises:
            AgentError: If implementation fails
        """
        system_prompt = f"""You are a coding assistant implementing approved changes.

Original task: {task_description}

Approved plan:
{plan}

Implement the changes according to the plan. You have access to:
- File reading and writing in /workspace
- Running tests and linters in the sandbox

Be thorough and follow the plan. After making changes, provide a summary of what you did.

IMPORTANT SECURITY NOTES:
- Do not attempt to access files outside /workspace
- Do not attempt network operations (they are blocked)
- Do not modify git configuration or protected files
"""

        try:
            # Create implementation request
            response = self.client.messages.create(
                model=self.agent_config.model,
                max_tokens=self.agent_config.implementation_budget,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": "Please implement the approved plan. Make the necessary changes to the repository at /workspace.",
                    }
                ],
            )

            # Extract summary
            summary = ""
            for block in response.content:
                if hasattr(block, "text"):
                    summary += block.text

            if not summary:
                raise AgentError("No implementation summary generated")

            return summary

        except Exception as e:
            raise AgentError(f"Implementation phase failed: {e}")

    def run_checks(self, workdir: Path) -> Dict[str, Any]:
        """Run lint and test checks in sandbox.

        Args:
            workdir: Repository working directory

        Returns:
            Dict with check results

        Raises:
            AgentError: If checks fail to run
        """
        results = {"lint": None, "test": None}

        # Try to run linter (ruff for Python projects)
        if (workdir / "pyproject.toml").exists() or (workdir / "setup.py").exists():
            try:
                lint_result = self.sandbox.run_command(
                    workdir,
                    ["ruff", "check", "."],
                    timeout=60,
                )
                results["lint"] = {
                    "success": lint_result["exit_code"] == 0,
                    "output": lint_result["stdout"] + lint_result["stderr"],
                }
            except AgentError as e:
                results["lint"] = {"success": False, "output": str(e)}

        # Try to run tests (pytest for Python projects)
        if (workdir / "tests").exists() or (workdir / "test").exists():
            try:
                test_result = self.sandbox.run_command(
                    workdir,
                    ["pytest", "-v", "--tb=short"],
                    timeout=120,
                )
                results["test"] = {
                    "success": test_result["exit_code"] == 0,
                    "output": test_result["stdout"] + test_result["stderr"],
                }
            except AgentError as e:
                results["test"] = {"success": False, "output": str(e)}

        return results


def format_check_results(results: Dict[str, Any]) -> str:
    """Format check results for display.

    Args:
        results: Check results from run_checks

    Returns:
        Formatted string
    """
    output = []

    if results.get("lint"):
        lint = results["lint"]
        status = "✅ Passed" if lint["success"] else "❌ Failed"
        output.append(f"**Lint Check:** {status}")
        if not lint["success"] and lint["output"]:
            output.append(f"```\n{lint['output'][:500]}\n```")

    if results.get("test"):
        test = results["test"]
        status = "✅ Passed" if test["success"] else "❌ Failed"
        output.append(f"**Test Check:** {status}")
        if not test["success"] and test["output"]:
            output.append(f"```\n{test['output'][:500]}\n```")

    return "\n\n".join(output) if output else "No checks available"
