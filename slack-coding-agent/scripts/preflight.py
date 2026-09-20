#!/usr/bin/env python3
"""Preflight checks for slack-coding-agent deployment.

Verifies the conditions packed into the CDK stacks (all in the single region
``us-east-2`` — data plane, sandbox/frontend run via App Runner/ECR) before
``cdk deploy``. Exits nonzero when a blocking condition fails; prints a
checklist either way.

Usage:  python scripts/preflight.py [--bedrock-probe] [--run-tests]
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_REGION = "us-east-2"
FRONT_REGION = "us-east-2"
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
FALLBACK_MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"

PASS = "OK  "
WARN = "WARN"
FAIL = "FAIL"
results = []


def run(args, check=True, capture=True):
    proc = subprocess.run(args, capture_output=capture, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"non-zero exit {proc.returncode} from {' '.join(args)}")
    return proc


def record(label, status, detail=""):
    results.append((status, label, detail))
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bedrock-probe", action="store_true", help="run a 1-token Bedrock probe (needs model access)")
    ap.add_argument("--run-tests", action="store_true", help="run the pytest suite")
    args = ap.parse_args()

    print(f"working dir: {ROOT}")

    # -- tool availability --------------------------------------------------
    local_cdk = ROOT / "infrastructure" / "node_modules" / ".bin" / "cdk"
    cdk_path = str(local_cdk) if local_cdk.exists() else shutil.which("cdk")
    for tool in ("aws", "docker", "node", "npx"):
        path = shutil.which(tool)
        record(f"{tool} CLI", PASS if path else FAIL, path or "not found on PATH")
    record("cdk CLI", PASS if cdk_path else FAIL, cdk_path or "not found (npm ci in ./infrastructure)")

    # -- AWS identity / regions ---------------------------------------------
    try:
        ident = run(["aws", "sts", "get-caller-identity", "--region", DATA_REGION]).stdout
        ident = json.loads(ident)
        account = ident["Account"]
        record("AWS identity", PASS, f"{ident['Arn']} (account {account})")
    except Exception as exc:
        record("AWS identity", FAIL, str(exc))
        account = "?"

    for region in (DATA_REGION, FRONT_REGION):
        try:
            awk_regions = run(["aws", "ec2", "describe-regions", "--region", region]).stdout
            regions = [r["RegionName"] for r in json.loads(awk_regions)["Regions"]]
            record(f"region {region} reachable", PASS if (region in regions or "all" in "") else WARN)
        except Exception as exc:
            record(f"region {region} reachable", FAIL, str(exc))

    # -- CDK bootstrap -------------------------------------------------------
    for region in (DATA_REGION, FRONT_REGION):
        try:
            out = run(["aws", "cloudformation", "describe-stacks", "--stack-name", "CDKToolkit",
                       "--region", region], check=False)
            record(f"cdk bootstrap {region}", PASS if out.returncode == 0 else WARN,
                   "" if out.returncode == 0 else "run: cdk bootstrap aws://<acct>/{region}")
        except Exception as exc:
            record(f"cdk bootstrap {region}", FAIL, str(exc))

    # -- kill-switch param (created by API stack on deploy) ------------------
    try:
        val = run(["aws", "ssm", "get-parameter", "--name", "/slack-agent/kill-switch",
                   "--region", DATA_REGION], check=False)
        if val.returncode == 0:
            v = json.loads(val.stdout)["Parameter"]["Value"]
            record("kill-switch SSM param", PASS, f"value={v}")
        else:
            record("kill-switch SSM param", WARN, "will be created on deploy")
    except Exception as exc:
        record("kill-switch SSM param", FAIL, str(exc))

    # -- private inputs ------------------------------------------------------
    for name in (".env", "github-app-key.pem"):
        p = ROOT / name
        record(f"secret file {name}", PASS if p.exists() else FAIL, "missing" if not p.exists() else "present (not shown)")

    # -- ECR repo ------------------------------------------------------------
    try:
        out = run(["aws", "ecr", "describe-repositories", "--repository-names", "slack-agent-sandbox",
                   "--region", DATA_REGION], check=False)
        if out.returncode == 0:
            uri = json.loads(out.stdout)["repositories"][0]["repositoryUri"]
            record("ECR slack-agent-sandbox", PASS, uri)
        else:
            record("ECR slack-agent-sandbox", WARN, "repo not created yet (created on deploy)")
    except Exception as exc:
        record("ECR slack-agent-sandbox", FAIL, str(exc))

    # -- Docker daemon + local image -----------------------------------------
    try:
        dinfo = run(["docker", "info", "--format", "{{.ServerVersion}}"])
        record("docker daemon", PASS, f"server {dinfo.stdout.strip()}")
    except Exception as exc:
        record("docker daemon", FAIL, str(exc))

    try:
        dimg = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], check=False).stdout.splitlines()
        local = [i for i in dimg if i.startswith("slack-agent-sandbox")]
        record("local sandbox image", PASS if local else WARN,
               ", ".join(local) if local else "not built yet (build + push before deploy)")
    except Exception as exc:
        record("local sandbox image", FAIL, str(exc))

    # -- Bedrock model access -------------------------------------------------
    def probe(model_id):
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
        })
        body_file = ROOT / ".preflight-probe-body.json"
        out_file = ROOT / ".preflight-probe.out"
        body_file.write_text(body)
        if out_file.exists():
            out_file.unlink()
        return run(["aws", "bedrock-runtime", "invoke-model", "--model-id", model_id,
                    "--body", f"file://{body_file}", "--region", DATA_REGION, str(out_file)],
                   check=False)

    if args.bedrock_probe:
        probe_result = probe(MODEL_ID)
        if probe_result.returncode == 0:
            record(f"Bedrock {MODEL_ID}", PASS, "model access confirmed")
        else:
            err = probe_result.stderr.strip()
            if ("AccessDenied" in err or "MissingModelAccess" in err or "being verified" in err
                    or "Operation not allowed" in err):
                record(f"Bedrock {MODEL_ID}", FAIL,
                       "no model access at account level — approve model access in the Bedrock console "
                       "(reset Region/Ammo as needed) or paste `continue`")
            else:
                fallback = probe(FALLBACK_MODEL_ID)
                record(f"Bedrock {MODEL_ID}", WARN, f"primary probe failed ({err[:120]}); fallback: "
                       + ("OK" if fallback.returncode == 0 else f"also failed ({fallback.stderr.strip()[:120]})"))
    else:
        record("Bedrock model access", WARN, "skip probe (run with --bedrock-probe to test a 1-token invoke)")

    # -- tests ----------------------------------------------------------------
    if args.run_tests:
        try:
            pytest = run([str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", "-q", "-x", str(ROOT / "tests")])
            last = [l for l in pytest.stdout.splitlines() if l.strip()][-1]
            record("pytest suite", PASS, last)
        except Exception as exc:
            record("pytest suite", FAIL, str(exc).splitlines()[-1][:160] if exc else "")

    # -- summary ---------------------------------------------------------------
    print()
    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    print(f"{len(fails)} FAIL, {len(warns)} WARN, {len(results) - len(fails) - len(warns)} OK")
    if fails:
        print("\nBlocking:")
        for _, label, detail in fails:
            print(f"  - {label}: {detail}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()