"""Validate every production topology using synthetic settings; no Docker daemon needed."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from deploy import compose_command, compose_environment

LOGS_VOLUME = "datagov_platform_analyzer_logs"
# Each luccy container and the marker its own env file in shared/ carries (deploy.ANALYZER_ENV_FILES).
ANALYZER_MARKERS = {"analyzer": "ANALYZER_SERVING_CHECK", "analyzer-sync": "ANALYZER_SYNC_CHECK",
                    "analyzer-compare": "ANALYZER_COMPARE_CHECK"}
LITERAL = {"literal$dollar#hash", "literal$$dollar#hash"}


def seconds(value):
    """A Compose duration as rendered ("10m0s", "310s" or a number of seconds)."""
    if isinstance(value, (int, float)):
        return value
    parts = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", value or "")
    assert parts and value, value
    hours, minutes, secs = (float(part or 0) for part in parts.groups())
    return hours * 3600 + minutes * 60 + secs


def render(config, env, folder, profiles=()):
    command = compose_command(config, folder)
    for profile in profiles:
        command += ["--profile", profile]
    return json.loads(subprocess.check_output(command + ["config", "--format", "json"], env=env, text=True))


def main():
    folder = Path(__file__).resolve().parent
    example = json.loads((folder / "deployment.example.json").read_text())
    state = {"images": {key: "example.azurecr.io/" + name + "@sha256:" + "a" * 64 for key, name in {
        "backend": "datagov-backend", "frontend": "datagov-frontend",
        "gateway": "datagov-gateway", "engine": "document-explorer",
        "analyzer": "datagov-document-analyzer"}.items()}}
    with tempfile.TemporaryDirectory(prefix="datagov-compose-check-") as scratch:
        shared = Path(scratch)
        for name in ("backend", "engine", "qdrant"):
            (shared / (name + ".env")).write_text("SYNTHETIC_CHECK=literal$dollar#hash\n")
        # One marker per luccy env file, to prove each container reads only its own.
        for name, marker in ANALYZER_MARKERS.items():
            (shared / (name + ".env")).write_text(f"# comment line\n{marker}=literal$dollar#hash\n")
        for analyzer in (False, True):
            for local in (False, True):
                config = dict(example, local_qdrant=local,
                              analyzer={"enabled": analyzer, "logs_volume": LOGS_VOLUME})
                env = compose_environment(config, state, shared)
                rendered = render(config, env, folder)
                services = rendered["services"]
                assert ("document-vectors" in services) == local
                assert ("analyzer" in services) == analyzer
                assert "db" not in services
                for name, service in services.items():
                    assert "build" not in service
                    assert not service.get("ports") or name == "nginx"
                # Compose escapes dollars in its rendered model so it can be read again.
                assert services["web"]["environment"]["SYNTHETIC_CHECK"] in {"literal$dollar#hash", "literal$$dollar#hash"}
                assert services["document-api"]["read_only"]
                assert all(v["read_only"] for v in services["document-api"]["volumes"])
                assert all(v["external"] for v in rendered["volumes"].values())
                if analyzer:
                    check_analyzer(rendered, render(config, env, folder, ["analyzer-jobs"]))
                    # The comparison's env file is optional (only for LUCCY_EVAL_ENABLED=True).
                    compare = shared / "analyzer-compare.env"
                    kept = compare.read_text()
                    compare.unlink()
                    render(config, env, folder, ["analyzer-jobs"])
                    compare.write_text(kept)
                else:
                    assert not any("LUCCY" in key for key in services["web"]["environment"])
                print("Production Compose valid:", "local Qdrant" if local else "managed Qdrant",
                      "with the analyzer" if analyzer else "")


def check_analyzer(rendered, with_jobs):
    services, jobs = rendered["services"], with_jobs["services"]
    analyzer = services["analyzer"]
    # Each luccy container reads only its own env file; luccy's environment (its Gemini and
    # Qdrant keys) stays out of DataGov's containers.
    for name, service in jobs.items():
        own = ANALYZER_MARKERS.get(name)
        if own:
            assert service["environment"][own] in LITERAL, name
        assert not {m for m in ANALYZER_MARKERS.values() if m != own} & set(service.get("environment") or {}), name
    for name in ("web", "worker"):
        assert services[name]["environment"]["LUCCY_API_URL"] == "http://analyzer:8787"
    assert analyzer["cap_drop"] == ["ALL"] and analyzer["security_opt"] == ["no-new-privileges:true"]
    assert "/health" in analyzer["healthcheck"]["test"][-1]
    # Room for luccy's models and answers (it measured 3.6 GiB after one answer).
    assert int(analyzer["mem_limit"]) >= 4 * 1024 ** 3
    assert seconds(analyzer["stop_grace_period"]) >= 300
    assert [v["target"] for v in analyzer["volumes"]] == ["/app/serving/logs"]
    assert rendered["volumes"]["analyzer-logs"]["name"] == LOGS_VOLUME
    # Releases never start the one-off jobs; their profile does.
    assert not {"analyzer-sync", "analyzer-compare"} & set(services)
    for name, module in (("analyzer-sync", "luccy_qdrant.sync"), ("analyzer-compare", "luccy_qdrant.compare")):
        assert jobs[name]["command"] == ["python", "-m", module]
        assert jobs[name]["image"] == analyzer["image"]
        assert jobs[name]["environment"]["DATAGOV_URL"] == "http://web:8000"
        assert not jobs[name].get("ports") and "build" not in jobs[name]
        assert jobs[name]["cap_drop"] == ["ALL"] and jobs[name]["security_opt"] == ["no-new-privileges:true"]
        assert int(jobs[name]["mem_limit"]) > 0
    # A comparison question takes minutes across five arms: a stop lets those in flight finish.
    assert seconds(jobs["analyzer-compare"]["stop_grace_period"]) >= 300


if __name__ == "__main__":
    main()
