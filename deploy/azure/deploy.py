"""Linux VM release controller. Standard library only; invoked by Azure Run Command.

Runtime configuration and secrets belong in ROOT/shared, never in a release bundle.
This deliberately does not create/delete volumes or reverse database migrations.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BUNDLE_FILES = ("deploy.py", "compose.yml", "compose.qdrant.yml", "compose.analyzer.yml")
APP_KEYS = {"backend", "frontend", "gateway"}
# The Document Analyzer image; CI builds it only while ANALYZER_IMAGE_ENABLED is true.
OPTIONAL_APP_KEYS = {"analyzer"}
REPOSITORIES = {"backend": "datagov-backend", "frontend": "datagov-frontend",
                "gateway": "datagov-gateway", "engine": "document-explorer",
                "analyzer": "datagov-document-analyzer"}
VOLUME_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]+"
# Private machine routes that must answer 404 from the public edge in every spelling: nginx
# blocks them case-insensitively, the frontend never proxies an "internal" segment in any
# case, Django refuses them when proxied.
INTERNAL_PROBES = (
    "/api/knowledge/internal/jobs/claim/",
    "/API/KNOWLEDGE/INTERNAL/jobs/claim/",
    "/Api/Knowledge/Internal/jobs/claim/",
    "/api/document-analyzer/internal/documents/",
    "/API/DOCUMENT-ANALYZER/INTERNAL/documents/",
    "/Api/Document-Analyzer/Internal/documents/",
)
ANALYZER_TOKENS = ("LUCCY_API_KEY", "LUCCY_SYNC_TOKEN", "LUCCY_EVAL_TOKEN")
# Every luccy container reads its own env file in shared/ and holds only the secrets it needs
# (docs/document-analyzer.md, "Who holds which secret"); the others must stay out of it.
ANALYZER_SECRETS = ANALYZER_TOKENS + ("SERVING_GEMINI_API_KEY", "LUCCY_QDRANT_API_KEY", "LUCCY_QDRANT_SYNC_API_KEY")
ANALYZER_ENV_FILES = {
    # analyzer: answers DataGov's questions; reads Qdrant with the read-only key.
    "analyzer.env": ("LUCCY_API_KEY", "SERVING_GEMINI_API_KEY", "LUCCY_QDRANT_API_KEY"),
    # analyzer-sync: reads DataGov's export, writes the luccy keys and luccy_kw with the write key.
    "analyzer-sync.env": ("LUCCY_SYNC_TOKEN", "LUCCY_QDRANT_SYNC_API_KEY"),
    # analyzer-compare: asks luccy and DataGov, reads the export, judges with Gemini. No Qdrant.
    # Only for the comparison (LUCCY_EVAL_ENABLED=True in backend.env); otherwise not needed.
    "analyzer-compare.env": ("LUCCY_API_KEY", "LUCCY_EVAL_TOKEN", "LUCCY_SYNC_TOKEN", "SERVING_GEMINI_API_KEY"),
}
EVAL_ONLY = ("LUCCY_EVAL_TOKEN", "analyzer-compare.env")
# One-off jobs for an operator or a timer (deploy.py --job NAME); never part of a release.
JOBS = {
    # The engine's chunk-order backfill: the analyzer and whole-page evidence need it once.
    "backfill-ordinals": ["exec", "-T", "document-worker", "python", "-m", "document_explorer.ordinals", "backfill"],
    "analyzer-sync": ["--profile", "analyzer-jobs", "run", "--rm", "--no-deps", "-T", "analyzer-sync"],
    "analyzer-compare": ["--profile", "analyzer-jobs", "run", "--rm", "--no-deps", "-T", "analyzer-compare"],
}
# Options the comparison accepts on the VM (e.g. --dry-run --limit=10); plain tokens, no shell.
COMPARE_OPTION = r"[A-Za-z0-9=._,:-]+"


def image_ref(value, registry, component):
    pattern = re.escape(registry + "/" + REPOSITORIES[component]) + r"@sha256:[a-f0-9]{64}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Expected immutable {component} image in configured registry")
    return value


def validate_manifest(data, registry):
    if set(data) != {"component", "sequence", "revision", "images"}:
        raise ValueError("Unexpected release fields")
    component = data["component"]
    if component not in {"app", "engine"}:
        raise ValueError("Unknown release component")
    if type(data["sequence"]) is not int or data["sequence"] < 1:
        raise ValueError("Release sequence must be a positive GitHub run ID")
    if not re.fullmatch(r"[a-f0-9]{40}", data["revision"]):
        raise ValueError("Release revision must be a full Git commit")
    images = set(data["images"])
    if images != (APP_KEYS if component == "app" else {"engine"}) and not (
            component == "app" and images == APP_KEYS | OPTIONAL_APP_KEYS):
        raise ValueError("Incomplete component image set")
    for key, value in data["images"].items():
        image_ref(value, registry, key)


def validate_config(config):
    if not re.fullmatch(r"[a-zA-Z0-9]{5,50}", config["registry_name"]):
        raise ValueError("Invalid ACR name")
    if not re.fullmatch(r"[a-z0-9.-]+\.azurecr\.io", config["registry_host"]):
        raise ValueError("Invalid ACR login server")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", config["project"]):
        raise ValueError("Invalid Compose project name")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", config["public_host"]):
        raise ValueError("Invalid public host")
    if not re.fullmatch(r"(?:127\.0\.0\.1|0\.0\.0\.0):[0-9]{1,5}", config["gateway_bind"]):
        raise ValueError("gateway_bind must explicitly specify IPv4 interface and port")
    if not 1 <= int(config["gateway_bind"].rsplit(":", 1)[1]) <= 65535:
        raise ValueError("Invalid gateway port")
    if type(config["local_qdrant"]) is not bool:
        raise ValueError("local_qdrant must be a JSON boolean")
    if set(config["volumes"]) != {"static", "media", "originals", "artifacts", "imports", "vectors"}:
        raise ValueError("All persistent volumes must be explicitly named")
    names = list(config["volumes"].values())
    analyzer = analyzer_settings(config)
    if analyzer:
        names.append(analyzer["logs_volume"])
    if len(set(names)) != len(names) or any(not isinstance(n, str) or not re.fullmatch(VOLUME_NAME, n) for n in names):
        raise ValueError("Volume names must be distinct valid Docker volume names")


def analyzer_settings(config):
    """The optional Document Analyzer's settings, or None when it is off.

    Off unless deployment.json holds {"analyzer": {"enabled": true, "logs_volume": "<volume>"}}.
    A missing key or "enabled": false leaves every release exactly as it was. It is not part
    of the recorded topology: it keeps no document data (it reads the engine's Qdrant), only
    luccy's operational logs in a volume the operator creates."""
    value = config.get("analyzer")
    if value is None:
        return None
    if not isinstance(value, dict) or "enabled" not in value or not set(value) <= {"enabled", "logs_volume"}:
        raise ValueError('analyzer must be {"enabled": true|false, "logs_volume": "<volume name>"}')
    if type(value["enabled"]) is not bool:
        raise ValueError("analyzer.enabled must be a JSON boolean")
    if not value["enabled"]:
        return None
    if not isinstance(value.get("logs_volume"), str) or not re.fullmatch(VOLUME_NAME, value["logs_volume"]):
        raise ValueError("An enabled analyzer needs logs_volume, an existing Docker volume name")
    return value


def read_env(path):
    if path.stat().st_mode & 0o077:
        raise ValueError(f"Secret file must have permissions 0600: {path.name}")
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise ValueError(f"Invalid or duplicate environment key in {path.name}")
        if "REPLACE_WITH" in value:
            raise ValueError(f"Unconfigured value in {path.name}: {key}")
        values[key] = value
    return values


def validate_secrets(shared, config):
    backend, engine = read_env(shared / "backend.env"), read_env(shared / "engine.env")
    for key in ("DOCUMENT_EXPLORER_API_KEY", "DOCUMENT_EXPLORER_WORKER_TOKEN"):
        if len(backend.get(key, "")) < 24 or backend[key] != engine.get(key):
            raise ValueError(f"Missing, short or mismatched {key}")
    if backend["DOCUMENT_EXPLORER_API_KEY"] == backend["DOCUMENT_EXPLORER_WORKER_TOKEN"]:
        raise ValueError("Service and worker keys must differ")
    if len(backend.get("SECRET_KEY", "")) < 32 or not engine.get("SERVING_GEMINI_API_KEY"):
        raise ValueError("Django/provider credentials are missing")
    target = backend.get("DATAGOV_DATABASE_TARGET")
    if target not in {"production", "local"}:
        raise ValueError("Set an explicit database target")
    prefix = "PROD_DB_" if target == "production" else "DB_"
    if any(not backend.get(prefix + key) for key in ("HOST", "NAME", "USER", "PASSWORD")):
        raise ValueError("Explicit existing database settings are required")
    if backend.get("DOCUMENT_EXPLORER_PUBLIC_URL") != "https://" + config["public_host"]:
        raise ValueError("Document links must use the configured public HTTPS host")
    if config["public_host"] not in backend.get("DJANGO_ALLOWED_HOSTS", "").split(","):
        raise ValueError("Public host is missing from DJANGO_ALLOWED_HOSTS")
    if "web" not in backend.get("DJANGO_ALLOWED_HOSTS", "").split(","):
        raise ValueError("Add the private worker callback host web to DJANGO_ALLOWED_HOSTS")
    if backend.get("DOCUMENT_EXPLORER_ENABLED") != "True":
        raise ValueError("Enable Document Explorer for this deployment")
    qdrant = None
    if config["local_qdrant"]:
        qdrant = read_env(shared / "qdrant.env")
        if engine.get("QDRANT_URL") != "http://document-vectors:6333":
            raise ValueError("Local Qdrant URL does not match the private service")
        if len(engine.get("QDRANT_API_KEY", "")) < 24 or engine["QDRANT_API_KEY"] != qdrant.get("QDRANT__SERVICE__API_KEY"):
            raise ValueError("Local Qdrant keys are missing or mismatched")
        # Optional: a key that may search but never write (Qdrant answers 403 to any change).
        read_only = qdrant.get("QDRANT__SERVICE__READ_ONLY_API_KEY", "")
        if read_only and (len(read_only) < 24 or read_only == engine["QDRANT_API_KEY"]):
            raise ValueError("QDRANT__SERVICE__READ_ONLY_API_KEY in qdrant.env must be a different random key of 24+ characters")
    elif not engine.get("QDRANT_URL", "").startswith("https://") or not engine.get("QDRANT_API_KEY"):
        raise ValueError("Managed Qdrant requires HTTPS and its API key")
    if analyzer_settings(config):
        validate_analyzer_secrets(shared, backend, engine, qdrant)


def evaluation_enabled(backend):
    return backend.get("LUCCY_EVAL_ENABLED", "").lower() == "true"


def validate_analyzer_secrets(shared, backend, engine, qdrant=None):
    """Only when deployment.json enables the analyzer. Each luccy container has its own env
    file (ANALYZER_ENV_FILES) with only the secrets it needs; backend.env holds DataGov's copy
    of the tokens. The eval token and analyzer-compare.env are checked only while the
    comparison is on (LUCCY_EVAL_ENABLED). qdrant is qdrant.env with local Qdrant, None with
    managed Qdrant."""
    evaluation = evaluation_enabled(backend)
    needed = [key for key in ANALYZER_TOKENS if evaluation or key not in EVAL_ONLY]
    for key in needed:
        if len(backend.get(key, "")) < 32:
            raise ValueError(f"Missing or short {key} in backend.env (openssl rand -hex 32)")
    tokens = {backend[key] for key in needed}
    if len(tokens) != len(needed):
        raise ValueError(f"{', '.join(needed)} must differ")
    if tokens & {backend.get(key) for key in ("SECRET_KEY", "DOCUMENT_EXPLORER_API_KEY", "DOCUMENT_EXPLORER_WORKER_TOKEN")}:
        raise ValueError("Use new random values for the analyzer, not existing DataGov keys")
    if not backend.get("LUCCY_ORGANIZATION_ID", "").isdigit():
        raise ValueError("Set LUCCY_ORGANIZATION_ID in backend.env to the numeric DataGov organization id")
    files = {}
    for name in ANALYZER_ENV_FILES:
        if name in EVAL_ONLY and not evaluation:
            continue
        if not (shared / name).is_file():
            raise ValueError(f"The analyzer is enabled in deployment.json, but shared/{name} is missing"
                             + (" (needed while LUCCY_EVAL_ENABLED=True)" if name in EVAL_ONLY else ""))
        files[name] = read_env(shared / name)
    serving, sync = files["analyzer.env"], files["analyzer-sync.env"]
    # Qdrant: the sync writes with the engine's key; the analyzer searches with the read-only
    # key when qdrant.env sets one (it must then), otherwise with the engine's key. A managed
    # cluster's read-only key cannot be checked here: luccy's /health reports a refused key.
    write = engine.get("QDRANT_API_KEY")
    read_only = (qdrant or {}).get("QDRANT__SERVICE__READ_ONLY_API_KEY", "")
    if sync.get("LUCCY_QDRANT_SYNC_API_KEY") != write:
        raise ValueError("LUCCY_QDRANT_SYNC_API_KEY in analyzer-sync.env must be the engine's QDRANT_API_KEY (the sync writes)")
    if read_only and serving.get("LUCCY_QDRANT_API_KEY") != read_only:
        raise ValueError("LUCCY_QDRANT_API_KEY in analyzer.env must be QDRANT__SERVICE__READ_ONLY_API_KEY from qdrant.env")
    if qdrant is not None and not read_only and serving.get("LUCCY_QDRANT_API_KEY") != write:
        raise ValueError("LUCCY_QDRANT_API_KEY in analyzer.env must be the engine's QDRANT_API_KEY, "
                         "or set QDRANT__SERVICE__READ_ONLY_API_KEY in qdrant.env and use that")
    known = {**{key: backend.get(key) for key in ANALYZER_TOKENS}, "LUCCY_QDRANT_SYNC_API_KEY": write,
             "LUCCY_QDRANT_API_KEY": serving.get("LUCCY_QDRANT_API_KEY"),
             "SERVING_GEMINI_API_KEY": serving.get("SERVING_GEMINI_API_KEY")}
    for name, values in files.items():
        needs = ANALYZER_ENV_FILES[name]
        for key in needs:
            if not values.get(key):
                raise ValueError(f"Set {key} in {name}")
            if key in ANALYZER_TOKENS and values[key] != backend.get(key):
                raise ValueError(f"{key} differs between backend.env and {name}")
        others = [key for key in ANALYZER_SECRETS if key not in needs]
        if any(key in values for key in others):
            raise ValueError(f"{name} may hold only {', '.join(needs)}; remove {', '.join(k for k in others if k in values)}")
        # The same secret under another name counts too (the engine's key may serve both roles).
        foreign = {known[key] for key in others} - {known[key] for key in needs} - {None, ""}
        if foreign & set(values.values()):
            raise ValueError(f"{name} holds another analyzer container's secret under a different name")
    for name, values in (("analyzer.env", serving), ("analyzer-sync.env", sync)):
        if values.get("LUCCY_QDRANT_URL") != engine.get("QDRANT_URL"):
            raise ValueError(f"LUCCY_QDRANT_URL in {name} must be the engine's QDRANT_URL")
        chunks = values.get("LUCCY_LIVE_CHUNKS_COLLECTION", values.get("DOCUMENT_EXPLORER_COLLECTION", "document_explorer_v1"))
        if chunks != engine.get("DOCUMENT_EXPLORER_COLLECTION", "document_explorer_v1"):
            raise ValueError(f"{name} must name the engine's chunk collection (DOCUMENT_EXPLORER_COLLECTION in engine.env)")
    # Optional: the image's default is 2.
    if serving.get("LUCCY_MAX_CONCURRENT_ANSWERS", "2") not in {"1", "2", "3"}:
        raise ValueError("LUCCY_MAX_CONCURRENT_ANSWERS in analyzer.env must be 1, 2 or 3 (the analyzer has 6 GiB)")
    if evaluation and not backend.get("LUCCY_EVAL_USER_ID", "").isdigit():
        raise ValueError("LUCCY_EVAL_ENABLED needs LUCCY_EVAL_USER_ID, a DataGov user id, in backend.env")


def topology(config):
    return {key: config[key] for key in ("project", "volumes", "local_qdrant", "registry_host")}


def next_state(current, manifest, config, release_dir):
    validate_config(config)
    validate_manifest(manifest, config["registry_host"])
    component = manifest["component"]
    if current and current["topology"] != topology(config):
        raise ValueError("Storage/topology changes require a planned migration, not an application release")
    previous = (current or {}).get("components", {}).get(component)
    if previous and manifest["sequence"] < previous["sequence"]:
        raise ValueError("Refusing an older release; rerun deployment from the current main commit")
    if previous and manifest["sequence"] == previous["sequence"] and manifest != previous:
        raise ValueError("A successful release ID cannot be reused for different images")
    if not current and component == "engine":
        raise ValueError("Deploy DataGov first; publish the initial engine image without deployment")
    state = json.loads(json.dumps(current)) if current else {
        "images": {"engine": image_ref(config["initial_engine_image"], config["registry_host"], "engine")},
        "components": {}, "topology": topology(config),
    }
    state["images"].update(manifest["images"])
    state["components"][component] = manifest
    if component == "app":
        state["app_release"] = str(release_dir)
    return state


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def compose_environment(config, state, shared):
    env = dict(os.environ)
    # Never inherit a caller's Compose overrides or implicitly load a .env file.
    for key in list(env):
        if key.startswith("COMPOSE_"):
            del env[key]
    env.update(COMPOSE_DISABLE_ENV_FILE="1", SHARED_DIR=str(shared),
               PUBLIC_HOST=config["public_host"], GATEWAY_BIND=config["gateway_bind"])
    env.update({key.upper() + "_IMAGE": value for key, value in state["images"].items()})
    env.update({key.upper() + "_VOLUME": value for key, value in config["volumes"].items()})
    analyzer = analyzer_settings(config)
    if analyzer:
        env["ANALYZER_LOGS_VOLUME"] = analyzer["logs_volume"]
    return env


def compose_files(config, folder):
    """compose.yml, plus the local Qdrant and the analyzer overlays when they are configured."""
    files = [folder / "compose.yml"]
    if config["local_qdrant"]:
        files.append(folder / "compose.qdrant.yml")
    if analyzer_settings(config):
        files.append(folder / "compose.analyzer.yml")
    return files


def compose_command(config, folder):
    command = ["docker", "compose", "--project-name", config["project"], "--env-file", os.devnull]
    for path in compose_files(config, folder):
        command.extend(["-f", str(path)])
    return command


@contextlib.contextmanager
def release_lock(root):
    import fcntl  # Linux only; serialize updates from both repositories.
    with (root / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def rollout(compose, component, local_qdrant, run, analyzer=False, note=print):
    if local_qdrant and component == "app":
        run(compose + ["up", "-d", "--wait", "--wait-timeout", "120", "document-vectors"])
    if component == "app":
        # A single VM has a maintenance window. Never serve old code during migrations.
        run(compose + ["stop", "-t", "90", "nginx", "frontend", "worker", "document-worker", "web"])
        for command in (["migrate", "--noinput"], ["createcachetable"], ["collectstatic", "--noinput"]):
            run(compose + ["run", "--rm", "--no-deps", "web", "python", "manage.py", *command])
        run(compose + ["up", "-d", "--wait", "--wait-timeout", "300",
                       "web", "frontend", "nginx", "worker", "document-api", "document-worker"])
        if analyzer:
            # After DataGov is back: luccy loads its models before /health turns 200 (its
            # health check allows 300 s), so a slow analyzer never holds DataGov down. A
            # failed one never fails the release either: DataGov is already serving.
            try:
                run(compose + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "600", "analyzer"])
            except (RuntimeError, subprocess.SubprocessError):
                note("analyzer not healthy; DataGov release complete")
    else:
        run(compose + ["stop", "-t", "90", "document-worker", "document-api"])
        run(compose + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "300",
                       "document-api", "document-worker"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def verify_public_routes(host, open_url=None, sleep=time.sleep):
    """Read-only HTTPS checks before either component can promote its release.

    Never follow a redirect to a login/error page and mistake it for readiness.
    Public checks use no session, service token, document text or model calls.
    """
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("Invalid public host")
    open_url = open_url or urllib.request.build_opener(NoRedirect()).open
    checks = (("/login", 200, "html"), ("/knowledge", 200, "html"),
              ("/api/branding/", 200, "json"),
              ("/api/knowledge/documents/", 403, "json"),
              *((path, 404, None) for path in INTERNAL_PROBES))
    results = []
    for path, expected, body_type in checks:
        for attempt in range(4):
            request = urllib.request.Request("https://" + host + path, headers={
                "User-Agent": "DataGov-Deployment-Check/1.0", "Cache-Control": "no-cache"})
            try:
                try:
                    response = open_url(request, timeout=10)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status = response.code
                    body = response.read(131073)
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 3:
                    sleep(5)
                    continue
                raise RuntimeError(f"Public HTTPS check could not reach {path}") from None
            if (status >= 500 or status == 429) and attempt < 3:
                sleep(5)
                continue
            if status != expected:
                raise RuntimeError(f"Public HTTPS check failed for {path}: expected {expected}, got {status}")
            if body_type == "json":
                try:
                    valid = content_type == "application/json" and len(body) <= 131072 and isinstance(json.loads(body), dict)
                except (ValueError, UnicodeError):
                    valid = False
                if not valid:
                    raise RuntimeError(f"Public HTTPS check expected JSON for {path}")
            if body_type == "html" and (content_type != "text/html" or b"<html" not in body.lower()):
                raise RuntimeError(f"Public HTTPS check expected HTML for {path}")
            results.append({"path": path, "status": status})
            break
    return results


def registry_login(config, env, log, run, token_file=None):
    if token_file is None:
        run(["az", "login", "--identity", "--allow-no-subscriptions", "--output", "none"])
        run(["az", "acr", "login", "--name", config["registry_name"], "--output", "none"])
        return
    # Manual releases use the operator's short-lived ACR token, transferred
    # privately. Consume it once; never persist it in a release or command line.
    details = token_file.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077 or details.st_uid != os.getuid():
        raise ValueError("Manual registry token must be an owned regular file with permissions 0600")
    if details.st_size > 65536:
        raise ValueError("Invalid manual registry credential file")
    try:
        credentials = json.loads(token_file.read_text())
        if set(credentials) != {"registry_host", "access_token"} or credentials["registry_host"] != config["registry_host"]:
            raise ValueError("Manual registry credential does not match the configured registry")
        token = credentials["access_token"]
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError("Invalid manual registry token")
        log.write("Authenticating with a one-time operator registry token\n")
        log.flush()
        result = subprocess.run(
            ["docker", "login", config["registry_host"], "--username", "00000000-0000-0000-0000-000000000000", "--password-stdin"],
            input=token, text=True, env=env, stdout=log, stderr=log, check=False, timeout=60,
        )
        if result.returncode:
            raise RuntimeError("Manual registry authentication failed")
    finally:
        token_file.unlink(missing_ok=True)


def without_analyzer(config):
    """deployment.json as if the analyzer were off, for one release or job."""
    return {key: value for key, value in config.items() if key != "analyzer"}


def check_analyzer_release(state, compose_dir):
    if "analyzer" not in state["images"]:
        raise ValueError("The analyzer is enabled in deployment.json, but no app release has published its "
                         "image yet: set the repository variable ANALYZER_IMAGE_ENABLED and release DataGov")
    if not (compose_dir / "compose.analyzer.yml").is_file():
        raise ValueError("The analyzer is enabled in deployment.json, but the current app release predates "
                         "it: release DataGov once before enabling the analyzer")


@contextlib.contextmanager
def job_lock(root, name):
    """One run of each job at a time; a timer that fires during a long run skips its turn."""
    import fcntl  # Linux only, like release_lock.
    with (root / f".job-{name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def run_job(root, name, options=(), run=subprocess.run):
    """Run one of JOBS against the last successful release, for an operator or a timer.

    It never starts, stops or recreates a release's services: one-off containers only
    (`run --rm --no-deps`), or a command inside the running document worker (`exec`).
    Only analyzer-compare takes options, passed to `python -m luccy_qdrant.compare`."""
    if name not in JOBS:
        raise ValueError("Unknown job")
    if options and name != "analyzer-compare":
        raise ValueError("Only analyzer-compare takes options")
    if any(not re.fullmatch(COMPARE_OPTION, option) for option in options):
        raise ValueError("Invalid comparison option")
    shared = root / "shared"
    config = json.loads((shared / "deployment.json").read_text())
    validate_config(config)
    validate_secrets(shared, config)
    if (root / "pending.json").exists():
        raise ValueError("A release is unfinished (pending.json): resolve it before running jobs")
    state = json.loads((root / "state.json").read_text())
    if state["topology"] != topology(config):
        raise ValueError("deployment.json no longer matches the deployed topology")
    compose_dir = Path(state["app_release"])
    compose_dir.relative_to(root / "releases")
    if analyzer_settings(config):
        try:
            check_analyzer_release(state, compose_dir)
        except ValueError:
            if name.startswith("analyzer-"):
                raise
            config = without_analyzer(config)       # other jobs never wait on the analyzer
    elif name.startswith("analyzer-"):
        raise ValueError('The analyzer is off: set "analyzer": {"enabled": true, ...} in deployment.json and release')
    if name == "analyzer-compare":
        backend = read_env(shared / "backend.env")
        if backend.get("LUCCY_EVAL_ENABLED", "").lower() != "true":
            raise ValueError("The comparison needs LUCCY_EVAL_ENABLED=True and LUCCY_EVAL_USER_ID in backend.env, released")
    command = compose_command(config, compose_dir) + JOBS[name]
    if options:
        command += ["python", "-m", "luccy_qdrant.compare", *options]
    with job_lock(root, name) as acquired:
        if not acquired:
            print(f"{name} is already running; skipped", flush=True)
            return 0
        return run(command, env=compose_environment(config, state, shared), check=False).returncode


def apply(root, manifest, bundle=None, registry_token_file=None):
    config = json.loads((root / "shared/deployment.json").read_text())
    validate_config(config)
    validate_manifest(manifest, config["registry_host"])
    validate_secrets(root / "shared", config)
    with release_lock(root):
        state_path, pending_path = root / "state.json", root / "pending.json"
        current = json.loads(state_path.read_text()) if state_path.exists() else None
        if pending_path.exists():
            pending = json.loads(pending_path.read_text())
            if pending["component"] != manifest["component"]:
                raise ValueError("Resolve the other component's failed deployment before continuing")
            if pending["sequence"] > manifest["sequence"]:
                raise ValueError("Refusing a release older than the unfinished deployment")
        release_dir = root / "releases" / f"{manifest['component']}-{manifest['sequence']}-{manifest['revision']}"
        state = next_state(current, manifest, config, release_dir)
        release_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if manifest["component"] == "app":
            if bundle is None:
                raise ValueError("App releases require their deployment bundle")
            for name in BUNDLE_FILES:
                shutil.copyfile(bundle / name, release_dir / name)
        compose_dir = Path(state["app_release"])
        compose_dir.relative_to(root / "releases")
        analyzer = analyzer_settings(config)
        if analyzer:
            try:
                check_analyzer_release(state, compose_dir)
            except ValueError as problem:
                # The analyzer is optional: release DataGov without it rather than refuse.
                print(f"WARNING: analyzer skipped in this release: {problem}", flush=True)
                config, analyzer = without_analyzer(config), None
        env = compose_environment(config, state, root / "shared")
        compose = compose_command(config, compose_dir)
        log_path = release_dir / "deploy.log"
        print(f"Release {manifest['component']} {manifest['revision']}; private log: {log_path}", flush=True)
        with log_path.open("a") as log, tempfile.TemporaryDirectory(prefix="datagov-auth-") as auth:
            env.update(AZURE_CONFIG_DIR=auth + "/azure", DOCKER_CONFIG=auth + "/docker")

            def run(args):
                log.write("Running: " + " ".join(args) + "\n")
                log.flush()
                completed = subprocess.run(args, env=env, stdout=log, stderr=log, check=False, timeout=1200)
                if completed.returncode:
                    raise RuntimeError(f"Release command failed; inspect private log: {log_path}")

            def note(message):
                log.write(message + "\n")
                log.flush()
                print(message, flush=True)

            version = subprocess.check_output(["docker", "compose", "version", "--short"], text=True).strip()
            numbers = re.match(r"v?(\d+)\.(\d+)", version)
            if not numbers or tuple(map(int, numbers.groups())) < (2, 30):
                raise ValueError("Docker Compose 2.30+ is required for literal secret env files")
            run(compose + ["config", "--quiet"])
            for key, name in config["volumes"].items():
                if key != "vectors" or config["local_qdrant"]:
                    run(["docker", "volume", "inspect", "--format", "{{.Name}}", name])
            if analyzer:
                run(["docker", "volume", "inspect", "--format", "{{.Name}}", analyzer["logs_volume"]])
            registry_login(config, env, log, run, registry_token_file)
            services = ["document-api", "document-worker"] if manifest["component"] == "engine" else [
                "web", "worker", "frontend", "nginx", "document-api", "document-worker"]
            if config["local_qdrant"] and manifest["component"] == "app":
                services.append("document-vectors")
            if analyzer and manifest["component"] == "app":
                services.append("analyzer")
            run(compose + ["pull", *services])
            atomic_json(pending_path, manifest)
            rollout(compose, manifest["component"], config["local_qdrant"], run, analyzer=bool(analyzer), note=note)
            log.write("Public HTTPS checks: " + json.dumps(verify_public_routes(config["public_host"])) + "\n")
            log.flush()
            if current:
                atomic_json(root / "previous.json", current)
            atomic_json(release_dir / "state.json", state)
            if manifest["component"] == "app":
                link = root / ".current-new"
                link.unlink(missing_ok=True)
                link.symlink_to(release_dir, target_is_directory=True)
                link.replace(root / "current")
            atomic_json(state_path, state)
            pending_path.unlink()
        print(f"DATAGOV_DEPLOY_OK:{manifest['component']}:{manifest['sequence']}:{manifest['revision']}", flush=True)


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=Path("/opt/datagov"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--manifest", type=Path)
    action.add_argument("--job", choices=sorted(JOBS),
                        help="Run a one-off job against the current release (docs/document-analyzer.md); "
                             "analyzer-compare passes further options to the comparison")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--registry-token-file", type=Path,
                        help="Manual release only: privately transferred short-lived ACR credentials; consumed on login")
    args, options = parser.parse_known_args()
    if options and args.job != "analyzer-compare":
        parser.error("unrecognized arguments: " + " ".join(options))
    os.umask(0o077)
    try:
        if args.job:
            code = run_job(args.root.resolve(strict=True), args.job, options)
        else:
            apply(args.root.resolve(strict=True), json.loads(args.manifest.read_text()), args.bundle, args.registry_token_file)
            code = 0
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"{'Job' if args.job else 'Deployment'} failed: {error}") from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
