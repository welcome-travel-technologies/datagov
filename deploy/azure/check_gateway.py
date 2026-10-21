"""Probe the gateway's nginx.conf in a throwaway nginx against a dummy upstream (needs Docker).

The private machine routes must answer 404 from nginx itself in every spelling (case, repeated
slashes, %-escapes, dot segments), while the public API, the Document Analyzer's routes and the
SPA still reach their upstreams. Containers and the network are removed afterwards.

    python deploy/azure/check_gateway.py [--image nginx:1.30.4-alpine] [--prefix NAME]
"""
import argparse
import http.client
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from deploy import INTERNAL_PROBES

ROOT = Path(__file__).resolve().parents[2]
# Stands in for web:8000 and frontend:3000 and echoes the path each was asked for.
UPSTREAM = """server { listen 8000; location / { default_type text/plain; return 200 "web $request_uri"; } }
server { listen 3000; location / { default_type text/plain; return 200 "frontend $request_uri"; } }
"""
BLOCKED = INTERNAL_PROBES + (
    "/api/Knowledge/Internal/jobs/claim/",
    "/api/knowledge/INTERNAL/jobs/claim/",
    "//api/knowledge/internal/jobs/claim/",
    "/api//knowledge///internal/jobs/claim/",
    "/api/knowledge/internal",
    "/api/knowledge/Internal",
    "/api/knowledge/internal/jobs/claim/?next=/login",
    "/api/knowledge/./internal/jobs/claim/",
    "/api/catalog/../knowledge/internal/jobs/claim/",
    "/api/knowledge/%69nternal/jobs/claim/",
    "/api/knowledge/%49NTERNAL/jobs/claim/",
    "/api%2Fknowledge%2Finternal/jobs/claim/",
    "/api/document-analyzer/internal/documents/digest/",
    "/api/Document-Analyzer/internal/documents/",
    "/api/document-analyzer/Internal/eval/runs/",
    "//api/document-analyzer/internal/documents/",
    "//API//Document-Analyzer//Internal//documents/",
    "/api/document-analyzer//internal/eval/answer/",
    "/api/document-analyzer/internal",
    "/api/document-analyzer/%69nternal/documents/",
    # The Luccy page location never shadows a private route.
    "/api/document-analyzer/luccy/../internal/documents/",
)
TO_WEB = (
    "/api/document-analyzer/status/",
    # luccy's own page and its routes, behind DataGov's sign-in gate (page_gate).
    "/api/document-analyzer/luccy/",
    "/api/document-analyzer/luccy/chats",
    "/api/document-analyzer/luccy/ask/stream/job-1",
    "/api/document-analyzer/luccy/documents/42",
    "/api/branding/",
    "/api/knowledge/documents/",
    "/api/knowledge/uploads/",
    "/api/knowledge/internals/",   # not a private route: Django answers it (404)
    "/admin/login/",
    "/.well-known/oauth-authorization-server",
)
TO_FRONTEND = ("/login", "/knowledge", "/document-analyzer", "/API/branding/")


def gateway_image():
    text = (ROOT / "deploy/azure/Dockerfile.gateway").read_text()
    return next(line.split()[1] for line in text.splitlines() if line.startswith("FROM "))


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        # The raw path as given: no client-side normalization of case, slashes or escapes.
        connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", "localhost")
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read().decode(errors="replace")
    finally:
        connection.close()


def probe(port):
    failures = []
    for path in BLOCKED:
        status, body = get(port, path)
        if status != 404 or body.startswith(("web ", "frontend ")):
            failures.append(f"{path}: expected nginx's own 404, got {status} {body[:60]!r}")
    for upstream, paths in (("web ", TO_WEB), ("frontend ", TO_FRONTEND)):
        for path in paths:
            status, body = get(port, path)
            if status != 200 or not body.startswith(upstream):
                failures.append(f"{path}: expected {upstream.strip()}, got {status} {body[:60]!r}")
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=gateway_image())
    parser.add_argument("--prefix", default="datagov-gateway-check-" + uuid.uuid4().hex[:8])
    args = parser.parse_args()
    network, upstream, gateway = args.prefix + "-net", args.prefix + "-upstream", args.prefix + "-gateway"
    try:
        docker("network", "create", network)
        docker("create", "--name", upstream, "--network", network,
               "--network-alias", "web", "--network-alias", "frontend", args.image)
        with tempfile.TemporaryDirectory() as scratch:
            conf = Path(scratch) / "upstream.conf"
            conf.write_text(UPSTREAM)
            docker("cp", str(conf), upstream + ":/etc/nginx/conf.d/default.conf")
        docker("start", upstream)
        docker("create", "--name", gateway, "--network", network, "-p", "127.0.0.1::80", args.image)
        docker("cp", str(ROOT / "nginx.conf"), gateway + ":/etc/nginx/conf.d/default.conf")
        docker("start", gateway)
        tested = docker("exec", gateway, "nginx", "-t")
        print((tested.stdout + tested.stderr).strip())
        port = int(docker("port", gateway, "80/tcp").stdout.split()[0].rsplit(":", 1)[1])
        for _ in range(40):
            try:
                get(port, "/login")
                break
            except (OSError, http.client.HTTPException):
                time.sleep(0.25)
        failures = probe(port)
        if failures:
            raise SystemExit("Gateway check failed (" + args.image + "):\n  " + "\n  ".join(failures))
        print(f"Gateway valid ({args.image}): {len(BLOCKED)} private spellings answer 404 from nginx; "
              f"{len(TO_WEB)} API and {len(TO_FRONTEND)} SPA routes reach their upstream")
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"docker {' '.join(error.cmd[1:3])} failed: {error.stderr.strip()}") from None
    finally:
        subprocess.run(["docker", "rm", "-f", gateway, upstream], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    main()
