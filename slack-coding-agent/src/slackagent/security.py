"""Security controls: path restrictions, secret scanning, input validation."""

import fnmatch
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import SecurityConfig


class SecurityError(Exception):
    """Raised when a security policy is violated."""

    pass


class PathGuard:
    """Enforces path-based access controls."""

    def __init__(self, protected_patterns: List[str]):
        """Initialize path guard with protected patterns.

        Args:
            protected_patterns: List of glob patterns for protected paths
        """
        self.protected_patterns = protected_patterns

    def is_path_protected(self, path: str) -> bool:
        """Check if a path matches any protected pattern.

        Args:
            path: Path to check (relative or absolute)

        Returns:
            True if path is protected, False otherwise
        """
        # Normalize path
        normalized = Path(path).as_posix()

        # Check against each pattern
        for pattern in self.protected_patterns:
            # Match both full path and basename
            if fnmatch.fnmatch(normalized, pattern):
                return True
            if fnmatch.fnmatch(Path(normalized).name, pattern):
                return True
            # Handle ** (glob recursive) patterns
            if "**" in pattern:
                parts = pattern.split("**")
                if any(part.strip("/") in normalized for part in parts if part):
                    return True

        return False

    def check_path_access(self, path: str, operation: str = "access") -> None:
        """Check if path access is allowed, raise if not.

        Args:
            path: Path to check
            operation: Description of operation for error message

        Raises:
            SecurityError: If path is protected
        """
        if self.is_path_protected(path):
            raise SecurityError(
                f"Access denied: Cannot {operation} protected path '{path}'"
            )


class SecretScanner:
    """Scans text and diffs for potential secrets."""

    # Common secret patterns
    PATTERNS = [
        (r"sk-ant-[a-zA-Z0-9\-_]{40,}", "Anthropic API Key"),
        (r"xox[baprs]-[a-zA-Z0-9\-]{10,}", "Slack Token"),
        (r"ghp_[a-zA-Z0-9]{36,}", "GitHub Personal Access Token"),
        (r"ghs_[a-zA-Z0-9]{36,}", "GitHub OAuth Token"),
        (r"github_pat_[a-zA-Z0-9_]{82}", "GitHub Fine-grained Token"),
        (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
        (r"AIza[0-9A-Za-z\-_]{35}", "Google API Key"),
        (r"sk-[a-zA-Z0-9]{32,}", "Generic Secret Key"),
        (r"['\"]password['\"]:\s*['\"][^'\"]+['\"]", "Password in JSON"),
        (r"postgres://[^:]+:[^@]+@[^/]+", "Database Connection String"),
        (r"mysql://[^:]+:[^@]+@[^/]+", "MySQL Connection String"),
        (r"mongodb://[^:]+:[^@]+@[^/]+", "MongoDB Connection String"),
        (r"-----BEGIN (RSA|DSA|EC|OPENSSH) PRIVATE KEY-----", "Private Key"),
    ]

    def __init__(self) -> None:
        """Initialize secret scanner with compiled patterns."""
        self.compiled_patterns = [
            (re.compile(pattern, re.IGNORECASE), name)
            for pattern, name in self.PATTERNS
        ]

    def scan(self, text: str) -> List[Tuple[str, str]]:
        """Scan text for potential secrets.

        Args:
            text: Text to scan

        Returns:
            List of (secret_type, matched_text) tuples
        """
        findings = []
        for pattern, name in self.compiled_patterns:
            matches = pattern.finditer(text)
            for match in matches:
                findings.append((name, match.group(0)))
        return findings

    def scan_diff(self, diff_text: str) -> List[Tuple[str, str, str]]:
        """Scan a git diff for secrets in added lines.

        Args:
            diff_text: Git diff output

        Returns:
            List of (file_path, secret_type, matched_text) tuples
        """
        findings = []
        current_file = None

        for line in diff_text.split("\n"):
            # Track which file we're in
            if line.startswith("diff --git"):
                # Extract filename from "diff --git a/path b/path"
                parts = line.split()
                if len(parts) >= 4:
                    current_file = parts[2][2:]  # Remove "a/" prefix
            # Only scan added lines (starting with +)
            elif line.startswith("+") and not line.startswith("+++"):
                line_content = line[1:]  # Remove + prefix
                secrets = self.scan(line_content)
                for secret_type, matched in secrets:
                    findings.append((current_file or "unknown", secret_type, matched))

        return findings


class UserValidator:
    """Validates and controls user access."""

    def __init__(self, allowed_users: List[str]):
        """Initialize user validator.

        Args:
            allowed_users: List of allowed Slack user IDs (empty = all allowed)
        """
        self.allowed_users = set(allowed_users)
        self.allow_all = len(allowed_users) == 0

    def is_user_allowed(self, user_id: str) -> bool:
        """Check if a user is allowed to use the bot.

        Args:
            user_id: Slack user ID

        Returns:
            True if user is allowed, False otherwise
        """
        if self.allow_all:
            return True
        return user_id in self.allowed_users

    def check_user_access(self, user_id: str) -> None:
        """Check if user has access, raise if not.

        Args:
            user_id: Slack user ID

        Raises:
            SecurityError: If user is not allowed
        """
        if not self.is_user_allowed(user_id):
            raise SecurityError(
                f"Access denied: User {user_id} is not in the allowed users list"
            )


class InputSanitizer:
    """Sanitizes user input to prevent injection attacks."""

    @staticmethod
    def sanitize_shell_arg(arg: str) -> str:
        """Sanitize a string for safe use in shell commands.

        Args:
            arg: Argument to sanitize

        Returns:
            Sanitized argument (may be quoted)
        """
        # Remove any null bytes
        arg = arg.replace("\x00", "")

        # If arg contains special characters, we should quote it
        # For simplicity, we'll just escape common dangerous chars
        dangerous_chars = [";", "|", "&", "$", "`", "\n", "\r", "(", ")", "<", ">"]
        for char in dangerous_chars:
            if char in arg:
                raise SecurityError(
                    f"Invalid character '{char}' in argument: not allowed for security"
                )

        return arg

    @staticmethod
    def sanitize_path(path: str) -> str:
        """Sanitize a path to prevent directory traversal.

        Args:
            path: Path to sanitize

        Returns:
            Sanitized path

        Raises:
            SecurityError: If path contains suspicious patterns
        """
        # Resolve to absolute path to detect traversal attempts
        try:
            resolved = Path(path).resolve()
        except Exception as e:
            raise SecurityError(f"Invalid path: {e}")

        # Check for suspicious patterns
        path_str = str(resolved)
        if ".." in path:
            raise SecurityError("Path traversal detected: '..' not allowed")

        return path_str


class SecurityManager:
    """Centralized security management."""

    def __init__(self, config: SecurityConfig):
        """Initialize security manager with configuration.

        Args:
            config: Security configuration
        """
        self.config = config
        self.path_guard = PathGuard(config.protected_paths)
        self.secret_scanner = SecretScanner()
        self.user_validator = UserValidator(config.allowed_users)
        self.sanitizer = InputSanitizer()

    def check_file_access(self, path: str, operation: str = "access") -> None:
        """Check if file access is allowed.

        Args:
            path: File path
            operation: Operation description

        Raises:
            SecurityError: If access is denied
        """
        # Check path protection
        self.path_guard.check_path_access(path, operation)

        # Check file size if it exists
        if Path(path).exists():
            file_size_mb = Path(path).stat().st_size / (1024 * 1024)
            if file_size_mb > self.config.max_file_size_mb:
                raise SecurityError(
                    f"File too large: {file_size_mb:.1f}MB exceeds "
                    f"limit of {self.config.max_file_size_mb}MB"
                )

    def check_user(self, user_id: str) -> None:
        """Check if user is allowed.

        Args:
            user_id: Slack user ID

        Raises:
            SecurityError: If user is not allowed
        """
        self.user_validator.check_user_access(user_id)

    def scan_diff_for_secrets(self, diff_text: str) -> Optional[str]:
        """Scan a diff for secrets.

        Args:
            diff_text: Git diff output

        Returns:
            Error message if secrets found, None otherwise
        """
        if not self.config.scan_secrets:
            return None

        findings = self.secret_scanner.scan_diff(diff_text)
        if findings:
            messages = []
            for file_path, secret_type, matched in findings:
                # Redact the actual secret in the message
                redacted = matched[:8] + "..." if len(matched) > 8 else "***"
                messages.append(f"  - {file_path}: {secret_type} ({redacted})")

            return (
                "⚠️ **Potential secrets detected in changes:**\n"
                + "\n".join(messages)
                + "\n\nPlease remove sensitive data before committing."
            )

        return None
