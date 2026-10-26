#!/usr/bin/env python3
"""Guided SARVAS setup for Docker Compose (standard library only).

    python3 scripts/setup.py            # interactive: writes .env, prepares storage, validates
    python3 scripts/setup.py --check    # only validate prerequisites + the existing .env

It asks for the GitHub App credentials, the AI model, storage location and
roles, writes .env with mode 0600 (backing up any previous one), creates the
data directories, and finally runs `agent-service -check` inside the agent
container to verify GitHub, the model and the sandbox for real.
Secret values are never printed.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
LOGIN_LIST = re.compile(r"^[A-Za-z0-9-]+(,[A-Za-z0-9-]+)*$")
REPO_LIST = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(,[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)*$")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    say(f"  ✅ {msg}")


def warn(msg: str) -> None:
    say(f"  ⚠️  {msg}")


def fail(msg: str) -> None:
    say(f"  ❌ {msg}")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def write_env(values: dict[str, str]) -> None:
    """Keep .env.example's layout and comments; fill in the values."""
    lines, seen = [], set()
    for line in EXAMPLE.read_text().splitlines():
        m = re.match(r"^([A-Z_]+)=", line)
        if m:
            seen.add(m.group(1))
            line = f"{m.group(1)}={values.get(m.group(1), '')}"
        lines.append(line)
    extra = [f"{k}={v}" for k, v in values.items() if k not in seen]
    if ENV.exists():
        shutil.copy2(ENV, ENV.with_suffix(".bak"))
        os.chmod(ENV.with_suffix(".bak"), 0o600)
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines + extra) + "\n")
    os.chmod(ENV, 0o600)


def ask(prompt: str, default: str = "", *, secret: bool = False, check=None, required: bool = False) -> str:
    while True:
        shown = "(keep current)" if secret and default else default
        suffix = f" [{shown}]" if shown else ""
        raw = getpass.getpass(f"{prompt}{suffix}: ") if secret else input(f"{prompt}{suffix}: ")
        value = raw.strip() or default
        if required and not value:
            say("    This is required.")
            continue
        if value and check and (problem := check(value)):
            say(f"    {problem}")
            continue
        return value


def yes(prompt: str, default: bool = False) -> bool:
    raw = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not raw else raw in ("y", "yes")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


# ------------------------------------------------------------------ checks


def check_prerequisites() -> bool:
    say("\nPrerequisites")
    good = True
    if sys.version_info < (3, 9):  # noqa: UP036 -- runs on the host's python3, not the server's
        fail("Python 3.9+ is needed to run this script")
        good = False
    if not shutil.which("docker"):
        fail("Docker is not installed (https://docs.docker.com/get-docker/)")
        return False
    if run(["docker", "info"]).returncode != 0:
        fail("Docker is installed but not running (start Docker Desktop / the docker service)")
        good = False
    else:
        ok("Docker is running")
    compose = run(["docker", "compose", "version", "--short"])
    if compose.returncode != 0:
        fail("Docker Compose v2 is missing (`docker compose`)")
        good = False
    else:
        version = compose.stdout.strip().lstrip("v")
        major_minor = tuple(int(x) for x in re.findall(r"\d+", version)[:2] or [0, 0])
        (ok if major_minor >= (2, 20) else warn)(f"Docker Compose {version}" +
                                                  ("" if major_minor >= (2, 20) else " (2.20+ recommended)"))
    return good


def check_values(v: dict[str, str]) -> bool:
    say("\nConfiguration (.env)")
    good = True
    url = urlparse(v.get("SARVAS_PUBLIC_URL", ""))
    local = url.hostname in ("localhost", "127.0.0.1")
    if not url.hostname or not (url.scheme == "https" or (url.scheme == "http" and local)):
        fail("SARVAS_PUBLIC_URL must be https://…, or http://localhost")
        good = False
    else:
        callback = v["SARVAS_PUBLIC_URL"].rstrip("/") + "/auth/callback"
        ok(f"public URL {v['SARVAS_PUBLIC_URL']} (GitHub App callback: {callback})")
    if not local and not (v.get("SARVAS_ALLOWED_USERS") or v.get("SARVAS_ADMINS") or v.get("SARVAS_ENGINEERS")):
        fail("SARVAS_ALLOWED_USERS (or admins/engineers) is required for a non-local URL")
        good = False
    if v.get("SARVAS_BIND") == "0.0.0.0" and url.scheme != "https":  # noqa: S104
        warn("SARVAS_BIND=0.0.0.0 without HTTPS exposes sign-in cookies on the network")
    for key in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "AGENT_SERVICE_TOKEN", "WEB_ENCRYPTION_KEY"):
        if not v.get(key):
            fail(f"{key} is empty")
            good = False
    if v.get("WEB_ENCRYPTION_KEY"):
        try:
            if len(base64.b64decode(v["WEB_ENCRYPTION_KEY"])) != 32:
                raise ValueError
        except ValueError:
            fail("WEB_ENCRYPTION_KEY must be 32 bytes, base64-encoded")
            good = False
    if len(v.get("AGENT_SERVICE_TOKEN", "")) < 32:
        fail("AGENT_SERVICE_TOKEN must be at least 32 characters")
        good = False
    if v.get("GITHUB_APP_ID"):
        key = (ROOT / v.get("GITHUB_PRIVATE_KEY_FILE", "")).resolve()
        if not key.is_file() or "PRIVATE KEY" not in key.read_text(errors="replace")[:100]:
            fail(f"GITHUB_PRIVATE_KEY_FILE ({v.get('GITHUB_PRIVATE_KEY_FILE')}) is not a PEM private key")
            good = False
        else:
            ok("GitHub App private key file found (Build tasks enabled)")
            if key.stat().st_mode & 0o077:
                warn(f"chmod 600 {v['GITHUB_PRIVATE_KEY_FILE']} (it is readable by other users)")
    else:
        warn("GITHUB_APP_ID not set: Ask and README work; Build tasks are off")
    provider = v.get("AI_PROVIDER")
    if provider == "gemini":
        if not v.get("GEMINI_API_KEY"):
            fail("GEMINI_API_KEY is empty")
            good = False
        if v.get("AI_SEND_CODE") == "true":
            warn("AI_SEND_CODE=true: repository excerpts and Build tasks are sent to Google")
        else:
            warn("AI_SEND_CODE is off: with Gemini, Ask and Build stay disabled (README drafting works)")
    elif provider == "openai":
        if not v.get("AI_BASE_URL", "").startswith("http") or not v.get("AI_MODEL"):
            fail("AI_PROVIDER=openai needs AI_BASE_URL and AI_MODEL")
            good = False
        if v.get("AI_SEND_CODE") != "true":
            warn("AI_SEND_CODE is off: with a remote API, Ask and Build stay disabled (README drafting works)")
    elif provider == "ollama":
        if not v.get("AI_MODEL"):
            fail("AI_MODEL must name an Ollama model")
            good = False
        else:
            ok(f"self-hosted model {v['AI_MODEL']} via {v.get('OLLAMA_URL')}")
    elif provider == "sim":
        warn("AI_PROVIDER=sim: placeholder answers and changes only")
    else:
        fail("AI_PROVIDER must be ollama, gemini, openai or sim")
        good = False
    data = (ROOT / v.get("SARVAS_DATA_DIR", "./data")).resolve()
    for sub in ("web", "work", "control", "ollama"):
        d = data / sub
        if not d.is_dir():
            fail(f"storage directory {d} is missing (run setup again)")
            good = False
        elif not os.access(d, os.W_OK):
            fail(f"{d} is not writable")
            good = False
    if good:
        ok(f"storage at {data} (SQLite database: {data / 'web' / 'web.db'})")
    return good


def validate_in_containers(v: dict[str, str]) -> bool:
    say("\nConnectivity and permissions (inside the agent container)")
    say("  Building images and starting the sandbox daemon; the first run takes a few minutes…")
    profile = ["--profile", "local-ai"] if v.get("OLLAMA_URL", "").startswith("http://ollama:") and \
        v.get("AI_PROVIDER") == "ollama" else []
    up = run(["docker", "compose", *profile, "up", "-d", "--build", "sandbox-docker", "sandbox-image",
              *(["ollama"] if profile else [])], cwd=ROOT)
    if up.returncode != 0:
        fail("docker compose couldn't start the sandbox:\n" + up.stderr[-1500:])
        return False
    if profile:
        say(f"  Pulling {v['AI_MODEL']} into the bundled Ollama (once; can take a while)…")
        pull = run(["docker", "compose", "--profile", "local-ai", "exec", "-T", "ollama", "ollama", "pull",
                    v["AI_MODEL"]], cwd=ROOT)
        (ok if pull.returncode == 0 else fail)(f"ollama pull {v['AI_MODEL']}" +
                                               ("" if pull.returncode == 0 else ": " + pull.stderr[-300:]))
    res = subprocess.run(["docker", "compose", *profile, "run", "--rm", "--build", "agent", "agent-service",
                          "-config", "/etc/sarvas/config.json", "-check"], cwd=ROOT, check=False)
    return res.returncode == 0


# ------------------------------------------------------------------ interactive


def interactive(v: dict[str, str]) -> dict[str, str]:
    say("\nWhere will people open SARVAS?")
    say("  Use http://localhost:8765 to try it on this machine, or the https:// address of your reverse proxy.")
    v["SARVAS_PUBLIC_URL"] = ask("Public URL", v.get("SARVAS_PUBLIC_URL") or "http://localhost:8765").rstrip("/")
    local = urlparse(v["SARVAS_PUBLIC_URL"]).hostname in ("localhost", "127.0.0.1")
    v.setdefault("SARVAS_BIND", "127.0.0.1")
    v.setdefault("SARVAS_PORT", "8765")
    if not local and yes("Is SARVAS behind a reverse proxy on another host (bind to all interfaces)?"):
        v["SARVAS_BIND"] = "0.0.0.0"  # noqa: S104 -- explicit choice, behind the operator's proxy

    say("\nGitHub App. Create one at GitHub → Settings → Developer settings → GitHub Apps → New:")
    say(f"  • Callback URL: {v['SARVAS_PUBLIC_URL']}/auth/callback   • Webhook: off")
    say("  • Repository permissions: Contents = Read & write, Pull requests = Read & write, Metadata = Read")
    say("  • Account permissions: Email addresses = Read (to show verified emails)")
    say("  • Nothing else: SARVAS never needs Administration, Workflows or merge rights.")
    say("  Then install it only on the repositories SARVAS may use. Never paste your GitHub password anywhere.")
    v["GITHUB_CLIENT_ID"] = ask("Client ID", v.get("GITHUB_CLIENT_ID", ""), required=True,
                                check=lambda s: None if re.match(r"^(Iv|Ov)[0-9A-Za-z.]{8,}$", s)
                                else "That doesn't look like a GitHub App client ID (Iv…/Ov…).")
    v["GITHUB_CLIENT_SECRET"] = ask("Client secret", v.get("GITHUB_CLIENT_SECRET", ""), secret=True, required=True)
    if yes("Enable Build tasks (the agent changes code and opens pull requests)?", bool(v.get("GITHUB_APP_ID"))):
        v["GITHUB_APP_ID"] = ask("App ID (numeric)", v.get("GITHUB_APP_ID", ""), required=True,
                                 check=lambda s: None if s.isdigit() else "The App ID is a number.")
        src = ask("Path to the App's private key (.pem)", v.get("GITHUB_PRIVATE_KEY_FILE") or "", required=True,
                  check=lambda s: None if Path(s).expanduser().is_file() else "File not found.")
        dest = ROOT / "secrets" / "github-app.pem"
        if Path(src).expanduser().resolve() != dest.resolve():
            dest.parent.mkdir(mode=0o700, exist_ok=True)
            shutil.copyfile(Path(src).expanduser(), dest)
        os.chmod(dest, 0o600)
        v["GITHUB_PRIVATE_KEY_FILE"] = "./secrets/github-app.pem"
        ok("private key stored at ./secrets/github-app.pem (mode 600, git-ignored)")
    else:
        v["GITHUB_APP_ID"] = ""

    say("\nWho may use it? (comma-separated GitHub logins)")
    check_logins = lambda s: None if LOGIN_LIST.match(s.replace(" ", "")) else "Use logins like alice,bob."  # noqa: E731
    v["SARVAS_ALLOWED_USERS"] = ask("Allowed users" + ("" if local else " (required)"),
                                    v.get("SARVAS_ALLOWED_USERS", ""), check=check_logins).replace(" ", "")
    v["SARVAS_ENGINEERS"] = ask("Engineers (approve medium-risk plans, review PRs)", v.get("SARVAS_ENGINEERS", ""),
                                check=check_logins).replace(" ", "")
    v["SARVAS_ADMINS"] = ask("Admins (audit log, kill switch)", v.get("SARVAS_ADMINS", ""),
                             check=check_logins).replace(" ", "")
    v["SARVAS_ALLOWED_REPOS"] = ask("Limit to repositories (owner/name,…; empty = wherever the App is installed)",
                                    v.get("SARVAS_ALLOWED_REPOS", ""),
                                    check=lambda s: None if REPO_LIST.match(s.replace(" ", ""))
                                    else "Use owner/name,owner/name.").replace(" ", "")

    say("\nAI model")
    say("  1) ollama  — self-hosted; repository code never leaves your infrastructure (recommended)")
    say("  2) gemini  — Google's API; code is sent there only if you explicitly allow it")
    say("  3) sim     — offline placeholder to try the workflow")
    say("  4) openai  — any OpenAI-compatible API: Baseten, Fireworks, vLLM on Modal, ...")
    current = {"ollama": "1", "gemini": "2", "sim": "3", "openai": "4"}.get(v.get("AI_PROVIDER", "ollama"), "1")
    choice = ask("Choose 1-4", current, check=lambda s: None if s in ("1", "2", "3", "4") else "Pick 1, 2, 3 or 4.")
    v["AI_PROVIDER"] = {"1": "ollama", "2": "gemini", "3": "sim", "4": "openai"}[choice]
    if v["AI_PROVIDER"] == "ollama":
        v["AI_MODEL"] = ask("Ollama model", v.get("AI_MODEL") if v.get("AI_MODEL", "").find("gemini") < 0
                            and v.get("AI_MODEL") else "qwen2.5-coder:7b")
        if yes("Run Ollama inside this Compose stack?", v.get("OLLAMA_URL", "http://ollama:11434")
               .startswith("http://ollama:")):
            v["OLLAMA_URL"] = "http://ollama:11434"
        else:
            v["OLLAMA_URL"] = ask("Ollama URL reachable from the containers", v.get("OLLAMA_URL") or
                                  "http://host.docker.internal:11434")
        v["AI_SEND_CODE"] = "false"
    elif v["AI_PROVIDER"] == "gemini":
        v["AI_MODEL"] = ask("Gemini model", "gemini-3.8-flash")
        v["GEMINI_API_KEY"] = ask("Gemini API key", v.get("GEMINI_API_KEY", ""), secret=True, required=True)
        say("  Ask sends retrieved code excerpts, and Build sends the files the agent reads, to Google.")
        v["AI_SEND_CODE"] = "true" if yes("Authorize sending repository code to Gemini?") else "false"
    elif v["AI_PROVIDER"] == "openai":
        say("  Baseten   https://inference.baseten.co/v1")
        say("  Fireworks https://api.fireworks.ai/inference/v1")
        say("  Modal     https://<workspace>--<app>.modal.run/v1 (your vLLM endpoint)")
        v["AI_BASE_URL"] = ask("API base URL (up to /v1)", v.get("AI_BASE_URL", ""), required=True)
        v["AI_MODEL"] = ask("Model ID", v.get("AI_MODEL", ""), required=True)
        v["AI_API_KEY"] = ask("API key", v.get("AI_API_KEY", ""), secret=True, required=True)
        say("  Ask sends retrieved code excerpts, and Build sends the files the agent reads, to that API.")
        v["AI_SEND_CODE"] = "true" if yes("Authorize sending repository code to it?") else "false"
    else:
        v["AI_MODEL"] = ""

    say("\nStorage")
    say("  SQLite database, repository checkouts + index, task workspaces and model files go here.")
    v["SARVAS_DATA_DIR"] = ask("Data directory", v.get("SARVAS_DATA_DIR") or "./data")

    v["AGENT_SERVICE_TOKEN"] = v.get("AGENT_SERVICE_TOKEN") or secrets.token_hex(32)
    v["WEB_ENCRYPTION_KEY"] = v.get("WEB_ENCRYPTION_KEY") or base64.b64encode(os.urandom(32)).decode()
    return v


def prepare_storage(v: dict[str, str]) -> None:
    data = (ROOT / v.get("SARVAS_DATA_DIR", "./data")).resolve()
    for sub in ("web", "work", "control", "ollama"):
        (data / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        for sub in ("web", "work", "control"):
            os.chown(data / sub, 1000, 1000)  # the containers' non-root user
    elif sys.platform.startswith("linux") and os.getuid() != 1000:
        warn(f"the containers run as uid 1000; run: sudo chown -R 1000:1000 {data}/web {data}/work {data}/control")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only validate prerequisites and the existing .env")
    parser.add_argument("--skip-containers", action="store_true", help="don't build/run containers to validate")
    args = parser.parse_args()

    say("SARVAS setup")
    prereq = check_prerequisites()
    values = {**read_env(EXAMPLE), **read_env(ENV)}
    if not args.check:
        try:
            values = interactive(values)
        except (KeyboardInterrupt, EOFError):
            say("\nCancelled; nothing was written.")
            return 1
        write_env(values)
        prepare_storage(values)
        say(f"\nWrote {ENV.relative_to(ROOT)} (mode 600). Secret values were not printed.")
    good = check_values(values)
    if good and prereq and not args.skip_containers:
        good = validate_in_containers(values)
    if not good or not prereq:
        say("\nFix the problems above, then run: python3 scripts/setup.py --check")
        return 1
    profile = " --profile local-ai" if values.get("OLLAMA_URL", "").startswith("http://ollama:") \
        and values.get("AI_PROVIDER") == "ollama" else ""
    say(f"\nAll set. Start SARVAS with:\n  docker compose{profile} up -d --build\n"
        f"then open {values['SARVAS_PUBLIC_URL']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
