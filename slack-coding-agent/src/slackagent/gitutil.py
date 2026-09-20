"""Centralized git command execution with error handling."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Optional


class GitError(Exception):
    """Raised when a git operation fails."""
    pass


def run_git(
    repo_dir: str | Path, 
    *args: str, 
    env: Optional[Dict[str, str]] = None,
    timeout: int = 60
) -> str:
    """Run a git command in a repository directory.
    
    Args:
        repo_dir: Repository directory path
        *args: Git command arguments
        env: Optional environment variables to add
        timeout: Command timeout in seconds
    
    Returns:
        Command stdout
        
    Raises:
        GitError: If command fails
    """
    cmd = ["git", "-C", str(repo_dir)] + list(args)
    
    # Merge environment if provided
    merged_env = None
    if env:
        import os
        merged_env = {**os.environ, **env}
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env=merged_env,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise GitError(f"Git command failed: {' '.join(args)}\n{e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"Git command timed out: {' '.join(args)}") from e
