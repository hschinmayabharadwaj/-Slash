"""Container entry point for the Render deployment.

The image runs `python app.py` next to config.json, sarvas_web/
and bin/agent-service. It starts the Go agent-service on loopback inside the
container, then serves the FastAPI backend (the page is on Vercel). All
settings come from the host's environment variables via config.json.
"""
import atexit
import os
import secrets
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
sys.path.insert(0, HERE)

from sarvas_web.server import main  # noqa: E402


def start_agent_service() -> None:
    # The shared token never leaves the container, so a fresh one per boot is enough.
    os.environ.setdefault("AGENT_SERVICE_TOKEN", secrets.token_hex(32))
    binary = os.path.join(HERE, "bin", "agent-service")
    # Print the setup check (never secret values) to the logs: the free plan has
    # no shell, and this is where "Build is off" explains which setting is missing.
    try:
        subprocess.run([binary, "-config", CONFIG, "-check"], timeout=90)
    except subprocess.TimeoutExpired:
        print("setup check timed out; starting anyway", flush=True)
    proc = subprocess.Popen([binary, "-config", CONFIG])
    atexit.register(proc.terminate)
    for _ in range(50):
        if proc.poll() is not None:
            sys.exit(f"agent-service exited with code {proc.returncode}; check the environment variables")
        try:
            urllib.request.urlopen("http://127.0.0.1:8766/healthz", timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    sys.exit("agent-service did not start")


if __name__ == "__main__":
    start_agent_service()
    main(["app.py", CONFIG])
