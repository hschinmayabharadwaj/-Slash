"""File-based contract between the host (worker/CLI) and the agent inside the sandbox.

Host writes  <io_dir>/input.json   -> container reads it at /io/input.json
Agent writes <io_dir>/output.json  <- container writes it at /io/output.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .guards import escape_slack, tail


class AgentError(RuntimeError):
    """The agent run failed. str(e) is safe to show to the person who asked."""
    pass


def write_input(io_dir: Path, payload: Dict[str, Any]) -> None:
    """Write input payload for the agent to read.
    
    Args:
        io_dir: I/O directory path
        payload: Input data dictionary
    """
    io_dir.mkdir(parents=True, exist_ok=True)
    (io_dir / "output.json").unlink(missing_ok=True)
    (io_dir / "input.json").write_text(json.dumps(payload, indent=2))
    # The container runs as the host user, but make sure it can always write output
    io_dir.chmod(0o777)


def read_output(io_dir: Path, result: Any) -> Dict[str, Any]:
    """Validate a finished sandbox run and return the agent's output dict.
    
    Args:
        io_dir: I/O directory path
        result: Sandbox result object with 'timed_out' attribute
        
    Returns:
        Agent output dictionary
        
    Raises:
        AgentError: If run failed or output is invalid
    """
    if hasattr(result, 'timed_out') and result.timed_out:
        raise AgentError(
            "That took longer than my time limit, so I stopped. Try a smaller request."
        )
    
    path = io_dir / "output.json"
    if not path.exists():
        raise AgentError(
            "The coding sandbox failed to start or crashed before finishing. "
            "This is a problem on my side, not with your request."
        )
    
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise AgentError("The agent returned something I couldn't read.") from None
    
    if not data.get("ok"):
        reason = escape_slack(tail(str(data.get("error") or "no reason given"), 300))
        raise AgentError(f"I couldn't complete that ({reason}).")
    
    return data
