"""Tests for security controls."""

import pytest

from slackagent.security import (
    PathGuard,
    SecretScanner,
    SecurityError,
    UserValidator,
    InputSanitizer,
)


class TestPathGuard:
    """Tests for PathGuard."""

    def test_protected_path_exact_match(self) -> None:
        """Test exact path matching."""
        guard = PathGuard([".env", "secrets.txt"])

        assert guard.is_path_protected(".env")
        assert guard.is_path_protected("secrets.txt")
        assert not guard.is_path_protected("config.txt")

    def test_protected_path_glob_pattern(self) -> None:
        """Test glob pattern matching."""
        guard = PathGuard([".env*", "**/*.pem"])

        assert guard.is_path_protected(".env")
        assert guard.is_path_protected(".env.local")
        assert guard.is_path_protected("config/app.pem")
        assert guard.is_path_protected("keys/github.pem")
        assert not guard.is_path_protected("config.txt")

    def test_protected_path_recursive_glob(self) -> None:
        """Test recursive glob patterns."""
        guard = PathGuard([".git/**", "**/secrets/**"])

        assert guard.is_path_protected(".git/config")
        assert guard.is_path_protected(".git/hooks/pre-commit")
        assert guard.is_path_protected("app/secrets/api_key.txt")
        assert not guard.is_path_protected("app/config.txt")

    def test_check_path_access_allowed(self) -> None:
        """Test path access check for allowed paths."""
        guard = PathGuard([".env"])

        # Should not raise
        guard.check_path_access("config.txt")

    def test_check_path_access_denied(self) -> None:
        """Test path access check for protected paths."""
        guard = PathGuard([".env"])

        with pytest.raises(SecurityError, match="Access denied"):
            guard.check_path_access(".env")


class TestSecretScanner:
    """Tests for SecretScanner."""

    def test_scan_anthropic_api_key(self) -> None:
        """Test scanning for API keys."""
        scanner = SecretScanner()

        # Test Gemini API key
        text = "API_KEY=AIzaSyC-abc123def456ghi789jkl012mno345pqr"
        findings = scanner.scan(text)
        assert len(findings) == 1
        assert findings[0][0] == "Google API Key (Gemini)"

        # Test Anthropic API key
        text2 = "API_KEY=sk-ant-1234567890abcdefghijklmnopqrstuvwxyz12345"
        findings2 = scanner.scan(text2)
        assert len(findings2) == 1
        assert findings2[0][0] == "Anthropic API Key"

    def test_scan_slack_token(self) -> None:
        """Test scanning for Slack tokens."""
        scanner = SecretScanner()

        text = "SLACK_TOKEN=xoxb-1234567890-1234567890-abcdefghijklmnop"
        findings = scanner.scan(text)

        assert len(findings) >= 1
        assert any("Slack" in finding[0] for finding in findings)

    def test_scan_github_token(self) -> None:
        """Test scanning for GitHub tokens."""
        scanner = SecretScanner()

        text = "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyz"
        findings = scanner.scan(text)

        assert len(findings) >= 1
        assert any("GitHub" in finding[0] for finding in findings)

    def test_scan_no_secrets(self) -> None:
        """Test scanning text with no secrets."""
        scanner = SecretScanner()

        text = "This is just normal text with no secrets"
        findings = scanner.scan(text)

        assert len(findings) == 0

    def test_scan_diff_added_lines(self) -> None:
        """Test scanning diff for secrets in added lines."""
        scanner = SecretScanner()

        diff = """diff --git a/config.py b/config.py
index 1234567..abcdefg 100644
--- a/config.py
+++ b/config.py
@@ -1,3 +1,4 @@
 # Configuration
+API_KEY = "sk-ant-1234567890abcdefghijklmnopqrstuvwxyz12345"
 DEBUG = True
"""

        findings = scanner.scan_diff(diff)

        assert len(findings) >= 1
        assert findings[0][0] == "config.py"
        assert "Anthropic" in findings[0][1]

    def test_scan_diff_removed_lines_ignored(self) -> None:
        """Test that removed lines are not scanned."""
        scanner = SecretScanner()

        diff = """diff --git a/config.py b/config.py
index 1234567..abcdefg 100644
--- a/config.py
+++ b/config.py
@@ -1,4 +1,3 @@
 # Configuration
-API_KEY = "sk-ant-1234567890abcdefghijklmnopqrstuvwxyz12345"
 DEBUG = True
"""

        findings = scanner.scan_diff(diff)

        # Should not find secrets in removed lines
        assert len(findings) == 0


class TestUserValidator:
    """Tests for UserValidator."""

    def test_allow_all_users(self) -> None:
        """Test allowing all users when list is empty."""
        validator = UserValidator([])

        assert validator.is_user_allowed("U12345")
        assert validator.is_user_allowed("U67890")

    def test_allow_specific_users(self) -> None:
        """Test allowing only specific users."""
        validator = UserValidator(["U12345", "U67890"])

        assert validator.is_user_allowed("U12345")
        assert validator.is_user_allowed("U67890")
        assert not validator.is_user_allowed("U99999")

    def test_check_user_access_allowed(self) -> None:
        """Test user access check for allowed users."""
        validator = UserValidator(["U12345"])

        # Should not raise
        validator.check_user_access("U12345")

    def test_check_user_access_denied(self) -> None:
        """Test user access check for denied users."""
        validator = UserValidator(["U12345"])

        with pytest.raises(SecurityError, match="Access denied"):
            validator.check_user_access("U99999")


class TestInputSanitizer:
    """Tests for InputSanitizer."""

    def test_sanitize_shell_arg_safe(self) -> None:
        """Test sanitizing safe shell arguments."""
        sanitizer = InputSanitizer()

        assert sanitizer.sanitize_shell_arg("hello") == "hello"
        assert sanitizer.sanitize_shell_arg("test-file.txt") == "test-file.txt"

    def test_sanitize_shell_arg_dangerous(self) -> None:
        """Test sanitizing dangerous shell arguments."""
        sanitizer = InputSanitizer()

        with pytest.raises(SecurityError):
            sanitizer.sanitize_shell_arg("test; rm -rf /")

        with pytest.raises(SecurityError):
            sanitizer.sanitize_shell_arg("test | cat /etc/passwd")

        with pytest.raises(SecurityError):
            sanitizer.sanitize_shell_arg("test && malicious")

    def test_sanitize_path_traversal(self) -> None:
        """Test path traversal detection."""
        sanitizer = InputSanitizer()

        with pytest.raises(SecurityError, match="Path traversal"):
            sanitizer.sanitize_path("../../../etc/passwd")

        with pytest.raises(SecurityError, match="Path traversal"):
            sanitizer.sanitize_path("./subdir/../../sensitive")
