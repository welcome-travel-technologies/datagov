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

BUNDLE_FILES = ("deploy.py", "compose.yml", "compose.qdrant.yml")
APP_KEYS = {"backend", "frontend", "gateway"}
REPOSITORIES = {"backend": "datagov-backend", "frontend": "datagov-frontend",
                "gateway": "datagov-gateway", "engine": "document-explorer"}


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
    if set(data["images"]) != (APP_KEYS if component == "app" else {"engine"}):
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
    if len(set(names)) != len(names) or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", n) for n in names):
        raise ValueError("Volume names must be distinct valid Docker volume names")


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
    if config["local_qdrant"]:
        qdrant = read_env(shared / "qdrant.env")
        if engine.get("QDRANT_URL") != "http://document-vectors:6333":
            raise ValueError("Local Qdrant URL does not match the private service")
        if len(engine.get("QDRANT_API_KEY", "")) < 24 or engine["QDRANT_API_KEY"] != qdrant.get("QDRANT__SERVICE__API_KEY"):
            raise ValueError("Local Qdrant keys are missing or mismatched")
    elif not engine.get("QDRANT_URL", "").startswith("https://") or not engine.get("QDRANT_API_KEY"):
        raise ValueError("Managed Qdrant requires HTTPS and its API key")


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
    return env


@contextlib.contextmanager
def release_lock(root):
    import fcntl  # Linux only; serialize updates from both repositories.
    with (root / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def rollout(compose, component, local_qdrant, run):
    if local_qdrant and component == "app":
        run(compose + ["up", "-d", "--wait", "--wait-timeout", "120", "document-vectors"])
    if component == "app":
        # A single VM has a maintenance window. Never serve old code during migrations.
        run(compose + ["stop", "-t", "90", "nginx", "frontend", "worker", "document-worker", "web"])
        for command in (["migrate", "--noinput"], ["createcachetable"], ["collectstatic", "--noinput"]):
            run(compose + ["run", "--rm", "--no-deps", "web", "python", "manage.py", *command])
        run(compose + ["up", "-d", "--wait", "--wait-timeout", "300",
                       "web", "frontend", "nginx", "worker", "document-api", "document-worker"])
    else:
        run(compose + ["stop", "-t", "90", "document-worker", "document-api"])
        run(compose + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "300",
                       "document-api", "document-worker"])


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
        env = compose_environment(config, state, root / "shared")
        compose = ["docker", "compose", "--project-name", config["project"], "--env-file", "/dev/null",
                   "-f", str(compose_dir / "compose.yml")]
        if config["local_qdrant"]:
            compose.extend(["-f", str(compose_dir / "compose.qdrant.yml")])
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

            version = subprocess.check_output(["docker", "compose", "version", "--short"], text=True).strip()
            numbers = re.match(r"v?(\d+)\.(\d+)", version)
            if not numbers or tuple(map(int, numbers.groups())) < (2, 30):
                raise ValueError("Docker Compose 2.30+ is required for literal secret env files")
            run(compose + ["config", "--quiet"])
            for key, name in config["volumes"].items():
                if key != "vectors" or config["local_qdrant"]:
                    run(["docker", "volume", "inspect", "--format", "{{.Name}}", name])
            registry_login(config, env, log, run, registry_token_file)
            services = ["document-api", "document-worker"] if manifest["component"] == "engine" else [
                "web", "worker", "frontend", "nginx", "document-api", "document-worker"]
            if config["local_qdrant"] and manifest["component"] == "app":
                services.append("document-vectors")
            run(compose + ["pull", *services])
            atomic_json(pending_path, manifest)
            rollout(compose, manifest["component"], config["local_qdrant"], run)
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/datagov"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--registry-token-file", type=Path,
                        help="Manual release only: privately transferred short-lived ACR credentials; consumed on login")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        apply(args.root.resolve(strict=True), json.loads(args.manifest.read_text()), args.bundle, args.registry_token_file)
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Deployment failed: {error}") from None


if __name__ == "__main__":
    main()
