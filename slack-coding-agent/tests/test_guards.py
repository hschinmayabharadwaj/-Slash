"""Tests for the security guards module."""

import tempfile
from pathlib import Path

import pytest

from slackagent.guards import (
    _glob_regex,
    matches_any,
    FileChange,
    DiffReport,
    inspect_worktree,
    check_policy,
    scan_secrets,
    sanitize_untrusted,
    escape_slack,
    neutralize_github_mentions,
    slugify,
    tail,
    kill_switch_on,
    SecurityError,
    PathGuard,
    SecretScanner,
    SecurityManager,
)


class TestPathMatching:
    """Tests for path pattern matching."""
    
    def test_glob_regex_simple(self):
        """Test simple glob patterns."""
        pattern = _glob_regex("*.py")
        assert pattern.match("main.py")
        assert pattern.match("test.py")
        assert not pattern.match("dir/main.py")
    
    def test_glob_regex_recursive(self):
        """Test recursive glob patterns."""
        pattern = _glob_regex("**/*.py")
        assert pattern.match("main.py")
        assert pattern.match("src/main.py")
        assert pattern.match("src/pkg/main.py")
    
    def test_glob_regex_question_mark(self):
        """Test question mark wildcard."""
        pattern = _glob_regex("test_?.py")
        assert pattern.match("test_a.py")
        assert pattern.match("test_1.py")
        assert not pattern.match("test_10.py")
    
    def test_matches_any(self):
        """Test matches_any function."""
        patterns = [".env*", "**/secrets/**", "*.pem"]
        
        assert matches_any(".env", patterns)
        assert matches_any(".env.local", patterns)
        assert matches_any("app/secrets/api.txt", patterns)
        assert matches_any("key.pem", patterns)
        assert not matches_any("main.py", patterns)
        assert not matches_any("env_file.txt", patterns)
    
    def test_matches_any_case_insensitive(self):
        """Test case-insensitive matching."""
        patterns = [".ENV", "SECRETS/**"]
        
        assert matches_any(".env", patterns)
        assert matches_any("app/Secrets/file.txt", patterns)


class TestDiffInspection:
    """Tests for git diff inspection."""
    
    def test_file_change_creation(self):
        """Test FileChange dataclass."""
        change = FileChange(path="src/main.py", status="M", added=10, deleted=5)
        assert change.path == "src/main.py"
        assert change.status == "M"
        assert change.added == 10
        assert change.deleted == 5
        assert not change.binary
    
    def test_diff_report_total_changed(self):
        """Test DiffReport.total_changed calculation."""
        report = DiffReport(
            files=[
                FileChange(path="a.py", added=5, deleted=2),
                FileChange(path="b.py", added=3, deleted=1),
            ]
        )
        assert report.total_changed == 11  # 5+2+3+1


class TestPolicyChecking:
    """Tests for policy enforcement."""
    
    def test_max_files_violation(self):
        """Test detection of too many changed files."""
        report = DiffReport(
            files=[FileChange(path=f"file{i}.py") for i in range(25)]
        )
        problems = check_policy(report, max_files=20, max_changed_lines=1000, deny_patterns=[])
        assert len(problems) == 1
        assert "25 files" in problems[0]
    
    def test_max_lines_violation(self):
        """Test detection of too many changed lines."""
        report = DiffReport(
            files=[FileChange(path="main.py", added=600, deleted=0)]
        )
        problems = check_policy(report, max_files=20, max_changed_lines=500, deny_patterns=[])
        assert len(problems) == 1
        assert "600 lines" in problems[0]
    
    def test_protected_path_violation(self):
        """Test detection of protected path access."""
        report = DiffReport(
            files=[FileChange(path=".env.local")]
        )
        problems = check_policy(
            report, max_files=20, max_changed_lines=500, deny_patterns=[".env*"]
        )
        assert len(problems) == 1
        assert ".env.local" in problems[0]
    
    def test_binary_file_detection(self):
        """Test binary file detection."""
        report = DiffReport(
            files=[FileChange(path="image.png", binary=True)]
        )
        problems = check_policy(report, max_files=20, max_changed_lines=500, deny_patterns=[])
        assert len(problems) == 1
        assert "binary" in problems[0]
    
    def test_symlink_detection(self):
        """Test symlink/submodule detection."""
        report = DiffReport(
            files=[FileChange(path="link", mode="120000")]
        )
        problems = check_policy(report, max_files=20, max_changed_lines=500, deny_patterns=[])
        assert len(problems) == 1
        assert "symlink" in problems[0]
    
    def test_no_violations(self):
        """Test when there are no violations."""
        report = DiffReport(
            files=[FileChange(path="main.py", added=10, deleted=5)]
        )
        problems = check_policy(report, max_files=20, max_changed_lines=500, deny_patterns=[])
        assert len(problems) == 0


class TestSecretScanning:
    """Tests for secret detection."""
    
    def test_google_api_key(self):
        """Test detection of Google/Gemini API keys."""
        # Pattern requires exactly 35 alphanumeric/-/_ chars after "AIza" (39 total)
        lines = ["API_KEY=AIzaSyC0abc123def456ghi789jklmnopqrXXXX"]
        found = scan_secrets(lines)
        assert len(found) == 1
        assert "Google/Gemini" in found[0]
    
    def test_anthropic_api_key(self):
        """Test detection of Anthropic API keys."""
        # Pattern requires 20+ chars after "sk-ant-"
        lines = ["ANTHROPIC_KEY=sk-ant-1234567890abcdefghij"]
        found = scan_secrets(lines)
        assert len(found) == 1
        assert "Anthropic" in found[0]
    
    def test_aws_access_key(self):
        """Test detection of AWS keys."""
        lines = ["AKIAIOSFODNN7EXAMPLE"]
        found = scan_secrets(lines)
        assert len(found) == 1
        assert "AWS" in found[0]
    
    def test_github_token(self):
        """Test detection of GitHub tokens."""
        lines = ["ghp_abcdefghijklmnopqrstuvwxyz123456"]
        found = scan_secrets(lines)
        assert len(found) == 1
        assert "GitHub" in found[0]
    
    def test_slack_token(self):
        """Test detection of Slack tokens."""
        lines = ["xoxb-1234567890-abcdefghij"]
        found = scan_secrets(lines)
        assert len(found) == 1
        assert "Slack" in found[0]
    
    def test_no_secrets(self):
        """Test when no secrets are found."""
        lines = ["print('hello')", "x = 42", "result = process()"]
        found = scan_secrets(lines)
        assert len(found) == 0
    
    def test_multiple_secrets(self):
        """Test detection of multiple secret types."""
        lines = [
            "API_KEY=AIzaSyC0abc123def456ghi789jklmnopqrXXXX",
            "GITHUB_TOKEN=ghp_XYZ7890123456789012345678",
        ]
        found = scan_secrets(lines)
        assert len(found) == 2


class TestTextSanitization:
    """Tests for text safety functions."""
    
    def test_sanitize_untrusted(self):
        """Test text sanitization."""
        text = "Hello\x00 World"
        result = sanitize_untrusted(text, 100)
        assert "\x00" not in result
    
    def test_sanitize_tags(self):
        """Test tag removal."""
        text = "Hello <request>bad</request> World"
        result = sanitize_untrusted(text, 100)
        assert "<request>" not in result
        assert "[tag removed]" in result
    
    def test_sanitize_truncation(self):
        """Test text truncation."""
        text = "x" * 200
        result = sanitize_untrusted(text, 100)
        assert len(result) < 200
        assert result.endswith("[truncated]")
    
    def test_escape_slack(self):
        """Test Slack escaping."""
        text = "Hello & <World>"
        result = escape_slack(text)
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result
    
    def test_neutralize_github_mentions(self):
        """Test GitHub mention neutralization."""
        text = "Thanks @john for help"
        result = neutralize_github_mentions(text)
        assert "@john" not in result
        assert "@\u200bjohn" in result
    
    def test_slugify(self):
        """Test slug generation."""
        assert slugify("Hello World") == "hello-world"
        assert slugify("Test 123!") == "test-123"
        assert slugify("Already-Slug") == "already-slug"
        assert slugify("") == "change"
    
    def test_tail(self):
        """Test text tail extraction."""
        text = "a" * 2000
        result = tail(text, 100)
        assert len(result) == 101  # "…" + 100 chars
        assert result.startswith("…")
        
        short_text = "short"
        result = tail(short_text, 100)
        assert result == short_text


class TestKillSwitch:
    """Tests for kill switch functionality."""
    
    def test_kill_switch_env(self, monkeypatch):
        """Test environment variable kill switch."""
        monkeypatch.setenv("BOT_DISABLED", "true")
        
        class MockConfig:
            security = type('obj', (object,), {'kill_switch_file': '/tmp/fake'})()
        
        assert kill_switch_on(MockConfig())
    
    def test_kill_switch_file(self, tmp_path):
        """Test file-based kill switch."""
        kill_file = tmp_path / "kill_switch"
        kill_file.touch()
        
        class MockConfig:
            security = type('obj', (object,), {'kill_switch_file': str(kill_file)})()
        
        assert kill_switch_on(MockConfig())
    
    def test_kill_switch_off(self):
        """Test when kill switch is off."""
        class MockConfig:
            security = type('obj', (object,), {'kill_switch_file': '/nonexistent'})()
        
        assert not kill_switch_on(MockConfig())


class TestSecurityClasses:
    """Tests for compatibility classes."""
    
    def test_path_guard(self):
        """Test PathGuard class."""
        guard = PathGuard([".env*", "**/secrets/**"])
        
        assert guard.is_path_protected(".env.local")
        assert guard.is_path_protected("app/secrets/file.txt")
        assert not guard.is_path_protected("main.py")
    
    def test_path_guard_error(self):
        """Test PathGuard raises SecurityError."""
        guard = PathGuard([".env*"])
        
        with pytest.raises(SecurityError):
            guard.check_path_access(".env.local", "read")
    
    def test_secret_scanner(self):
        """Test SecretScanner class."""
        scanner = SecretScanner()
        
        findings = scanner.scan("API_KEY=AIzaSyC0abc123def456ghi789jklmnopqrXXXX")
        assert len(findings) == 1
    
    def test_security_manager(self):
        """Test SecurityManager class."""
        class MockConfig:
            protected_paths = [".env*"]
            max_file_size_mb = 10
            scan_secrets = True
        
        manager = SecurityManager(MockConfig())
        
        # Test path checking
        assert manager.path_guard.is_path_protected(".env.local")
        
        # Test file size checking (should not raise for non-existent file)
        # In real use, this would check actual file size


if __name__ == "__main__":
    pytest.main([__file__, "-v"])