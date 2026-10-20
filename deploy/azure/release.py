"""GitHub runner client for the VM deployment protocol; contains no credentials.

The same client can live in the engine repository's tools/azure_release.py.
App releases transfer a small, explicit deployment bundle; engine releases call
the controller installed by the latest successful app release.
"""
import argparse
import base64
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

# The app release's deployment bundle; deploy.py copies the same files (BUNDLE_FILES).
BUNDLE = ("deploy.py", "compose.yml", "compose.qdrant.yml", "compose.analyzer.yml")


def manifest_from_env(component, env):
    if env.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Only main may deploy")
    registry = env["ACR_LOGIN_SERVER"]
    if not re.fullmatch(r"[a-z0-9.-]+\.azurecr\.io", registry):
        raise ValueError("Invalid ACR login server")
    keys = {"backend": "datagov-backend", "frontend": "datagov-frontend", "gateway": "datagov-gateway"} if component == "app" else {"engine": "document-explorer"}
    if component == "app" and env.get("ANALYZER_DIGEST"):
        # Only when CI built the optional Document Analyzer image (ANALYZER_IMAGE_ENABLED).
        keys["analyzer"] = "datagov-document-analyzer"
    images = {}
    for key, repository in keys.items():
        digest = env[key.upper() + "_DIGEST"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Image digest missing or invalid")
        images[key] = f"{registry}/{repository}@{digest}"
    revision = env["GITHUB_SHA"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid Git revision")
    sequence = int(env["GITHUB_RUN_ID"])
    if sequence < 1:
        raise ValueError("Invalid run ID")
    return {"component": component, "sequence": sequence, "revision": revision, "images": images}


def remote_script(manifest, bundle=None):
    files = {name: (bundle / name).read_text(encoding="utf-8") for name in BUNDLE} if bundle else {}
    payload = base64.b64encode(gzip.compress(json.dumps({"manifest": manifest, "files": files}).encode())).decode()
    # The payload is encoded data, never shell syntax; no runtime secrets are sent.
    return "set -eu\numask 077\npython3 - <<'DATAGOV_PY'\n" + f"""import base64,gzip,json,pathlib,subprocess,tempfile
payload=json.loads(gzip.decompress(base64.b64decode('{payload}')))
with tempfile.TemporaryDirectory(prefix='datagov-release-') as scratch:
    folder=pathlib.Path(scratch)
    manifest=folder/'manifest.json'
    manifest.write_text(json.dumps(payload['manifest']))
    for name,content in payload['files'].items():
        if name not in {BUNDLE!r}:
            raise SystemExit('Invalid deployment bundle')
        (folder/name).write_text(content)
    controller=folder/'deploy.py' if payload['files'] else pathlib.Path('/opt/datagov/current/deploy.py')
    command=['python3',str(controller),'--manifest',str(manifest)]
    if payload['files']:
        command += ['--bundle',str(folder)]
    subprocess.run(command,check=True)
DATAGOV_PY
"""


def succeeded(response, manifest):
    expected = f"DATAGOV_DEPLOY_OK:{manifest['component']}:{manifest['sequence']}:{manifest['revision']}"
    return any(expected in item.get("message", "").splitlines() for item in response.get("value", []))


def invoke(script, manifest, env, run=subprocess.run, sleep=time.sleep):
    for key in ("AZURE_VM_NAME", "AZURE_RESOURCE_GROUP"):
        if not env.get(key) or env[key].startswith("-"):
            raise ValueError(f"Missing or invalid {key}")
    with tempfile.TemporaryDirectory(prefix="datagov-invoke-") as scratch:
        path = Path(scratch) / "release.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        command = ["az", "vm", "run-command", "invoke", "--resource-group", env["AZURE_RESOURCE_GROUP"],
                   "--name", env["AZURE_VM_NAME"], "--command-id", "RunShellScript",
                   "--scripts", "@" + str(path), "--output", "json", "--only-show-errors"]
        for attempt in range(20):
            result = run(command, text=True, capture_output=True, timeout=1800)
            if result.returncode == 0:
                response = json.loads(result.stdout)
                if not succeeded(response, manifest):
                    # Azure can report a successful operation for a failed guest script.
                    raise RuntimeError("VM did not confirm a healthy release. Inspect /opt/datagov/releases/*/deploy.log and pending.json; do not assume rollback.")
                print(f"Healthy {manifest['component']} release: {manifest['revision']}")
                return
            # Only retry a definite rejection before execution. Never retry a timeout
            # or ambiguous VM failure, since the first invocation may still be running.
            busy = "Run command extension execution is in progress" in result.stderr
            if not busy or attempt == 19:
                raise RuntimeError("Azure Run Command failed. Inspect the Azure operation and VM logs before retrying.")
            sleep(30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", choices=("app", "engine"), required=True)
    args = parser.parse_args()
    manifest = manifest_from_env(args.component, os.environ)
    bundle = Path(__file__).parent if args.component == "app" else None
    invoke(remote_script(manifest, bundle), manifest, os.environ)


if __name__ == "__main__":
    main()
