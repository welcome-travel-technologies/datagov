"""Validate both production topologies using synthetic settings; no Docker daemon needed."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from deploy import compose_environment


def main():
    folder = Path(__file__).resolve().parent
    config = json.loads((folder / "deployment.example.json").read_text())
    state = {"images": {key: "example.azurecr.io/" + name + "@sha256:" + "a" * 64 for key, name in {
        "backend": "datagov-backend", "frontend": "datagov-frontend",
        "gateway": "datagov-gateway", "engine": "document-explorer"}.items()}}
    with tempfile.TemporaryDirectory(prefix="datagov-compose-check-") as scratch:
        shared = Path(scratch)
        for name in ("backend", "engine", "qdrant"):
            (shared / (name + ".env")).write_text("SYNTHETIC_CHECK=literal$dollar#hash\n")
        env = compose_environment(config, state, shared)
        command = ["docker", "compose", "--env-file", os.devnull, "-f", str(folder / "compose.yml")]
        for local in (False, True):
            args = command + (["-f", str(folder / "compose.qdrant.yml")] if local else [])
            rendered = json.loads(subprocess.check_output(args + ["config", "--format", "json"], env=env, text=True))
            services = rendered["services"]
            assert ("document-vectors" in services) == local
            assert "db" not in services
            for name, service in services.items():
                assert "build" not in service
                assert not service.get("ports") or name == "nginx"
            # Compose escapes dollars in its rendered model so it can be read again.
            assert services["web"]["environment"]["SYNTHETIC_CHECK"] in {"literal$dollar#hash", "literal$$dollar#hash"}
            assert services["document-api"]["read_only"]
            assert all(v["read_only"] for v in services["document-api"]["volumes"])
            assert all(v["external"] for v in rendered["volumes"].values())
            print("Production Compose valid:", "local Qdrant" if local else "managed Qdrant")


if __name__ == "__main__":
    main()
