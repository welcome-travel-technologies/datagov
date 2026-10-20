"""The optional Document Analyzer in the VM release controller (docs/document-analyzer.md)."""
import base64
import contextlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

FOLDER = Path(__file__).resolve().parents[1]
LOGS = "datagov_platform_analyzer_logs"


def module(name):
    spec = importlib.util.spec_from_file_location(name, FOLDER / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


deploy, release = module("deploy"), module("release")


def image(key, digit):
    return "yourregistry.azurecr.io/" + deploy.REPOSITORIES[key] + "@sha256:" + digit * 64


def config(analyzer="example", **changes):
    """deployment.example.json; analyzer=None drops the key, "on" enables it."""
    value = json.loads((FOLDER / "deployment.example.json").read_text())
    value["initial_engine_image"] = image("engine", "a")
    if analyzer is None:
        value.pop("analyzer")
    elif analyzer == "on":
        value["analyzer"] = {"enabled": True, "logs_volume": LOGS}
    elif analyzer != "example":
        value["analyzer"] = analyzer
    value.update(changes)
    return value


def manifest(component="app", sequence=1, digit="b", analyzer=False):
    keys = (deploy.APP_KEYS | ({"analyzer"} if analyzer else set())) if component == "app" else {"engine"}
    return {"component": component, "sequence": sequence, "revision": digit * 40,
            "images": {key: image(key, digit) for key in keys}}


def legacy_command(value, folder):
    """The controller's Compose command before the analyzer existed."""
    command = ["docker", "compose", "--project-name", value["project"], "--env-file", os.devnull,
               "-f", str(folder / "compose.yml")]
    if value["local_qdrant"]:
        command.extend(["-f", str(folder / "compose.qdrant.yml")])
    return command


def legacy_environment(value, state, shared):
    """The controller's Compose environment before the analyzer existed."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("COMPOSE_"):
            del env[key]
    env.update(COMPOSE_DISABLE_ENV_FILE="1", SHARED_DIR=str(shared),
               PUBLIC_HOST=value["public_host"], GATEWAY_BIND=value["gateway_bind"])
    env.update({key.upper() + "_IMAGE": v for key, v in state["images"].items()})
    env.update({key.upper() + "_VOLUME": v for key, v in value["volumes"].items()})
    return env


def compose_available():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


BACKEND = {
    "SECRET_KEY": "s" * 32, "DATAGOV_DATABASE_TARGET": "local", "DB_HOST": "db", "DB_NAME": "catalog",
    "DB_USER": "catalog", "DB_PASSWORD": "synthetic", "DOCUMENT_EXPLORER_ENABLED": "True",
    "DJANGO_ALLOWED_HOSTS": "datagov.welcomd.com,web", "DOCUMENT_EXPLORER_PUBLIC_URL": "https://datagov.welcomd.com",
    "DOCUMENT_EXPLORER_API_KEY": "a" * 24, "DOCUMENT_EXPLORER_WORKER_TOKEN": "w" * 24,
}
ENGINE = {
    "DOCUMENT_EXPLORER_API_KEY": "a" * 24, "DOCUMENT_EXPLORER_WORKER_TOKEN": "w" * 24,
    "SERVING_GEMINI_API_KEY": "synthetic", "QDRANT_URL": "http://document-vectors:6333", "QDRANT_API_KEY": "q" * 24,
}
READ_ONLY = "r" * 24
QDRANT = {"QDRANT__SERVICE__API_KEY": "q" * 24, "QDRANT__SERVICE__READ_ONLY_API_KEY": READ_ONLY}
TOKENS = {"LUCCY_API_KEY": "1" * 32, "LUCCY_SYNC_TOKEN": "2" * 32, "LUCCY_EVAL_TOKEN": "3" * 32}
SHARED_KEYS = {**TOKENS, "LUCCY_ORGANIZATION_ID": "7"}
QDRANT_URL = "http://document-vectors:6333"
# Each luccy container's own file, holding only what it needs.
ANALYZER_FILES = {
    "analyzer.env": {"LUCCY_API_KEY": TOKENS["LUCCY_API_KEY"], "SERVING_GEMINI_API_KEY": "synthetic-luccy",
                     "LUCCY_QDRANT_URL": QDRANT_URL, "LUCCY_QDRANT_API_KEY": READ_ONLY,
                     "LUCCY_MAX_CONCURRENT_ANSWERS": "2"},
    "analyzer-sync.env": {"LUCCY_SYNC_TOKEN": TOKENS["LUCCY_SYNC_TOKEN"], "LUCCY_QDRANT_URL": QDRANT_URL,
                          "LUCCY_QDRANT_SYNC_API_KEY": "q" * 24},
    "analyzer-compare.env": {**TOKENS, "SERVING_GEMINI_API_KEY": "synthetic-luccy"},
}


def write_env(path, values):
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items() if value is not None))
    if os.name != "nt":
        path.chmod(0o600)


def release_commands(value, release=None, state_images=None, fails=None):
    """Every command apply() runs for one release (Docker and Azure mocked), the Compose
    command it should use, and the state it records. A command for which `fails(command)` is
    true exits 1."""
    calls = []
    release = release or manifest("engine", 2, "c")
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "shared").mkdir()
        (root / "shared/deployment.json").write_text(json.dumps(value))
        current = deploy.next_state(None, manifest(), value, root / "releases/app-1")
        current["images"].update(state_images or {})
        (root / "releases/app-1").mkdir(parents=True)
        for name in deploy.BUNDLE_FILES:
            shutil.copyfile(FOLDER / name, root / "releases/app-1" / name)
        (root / "state.json").write_text(json.dumps(current))
        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1 if fails and fails(command) else 0)
        with patch.object(deploy, "validate_secrets"), patch.object(deploy, "release_lock", return_value=contextlib.nullcontext()), \
             patch.object(deploy, "verify_public_routes", return_value=[]), \
             patch.object(deploy.subprocess, "check_output", return_value="2.33.1"), \
             patch.object(deploy.subprocess, "run", side_effect=run):
            deploy.apply(root, release, FOLDER if release["component"] == "app" else None)
        assert not (root / "pending.json").exists()
        state = json.loads((root / "state.json").read_text())
        compose_dir = Path(state["app_release"])
        expected = legacy_command(value, compose_dir)
        if deploy.analyzer_settings(value):
            expected += ["-f", str(compose_dir / "compose.analyzer.yml")]
    return calls, expected, state


# The comparison on: then its token and analyzer-compare.env are checked too.
EVAL_ON = {"LUCCY_EVAL_ENABLED": "True", "LUCCY_EVAL_USER_ID": "12"}


def write_shared(shared, backend=None, analyzer=None, with_analyzer=True, sync=None, compare=None, qdrant=None):
    """backend.env, engine.env, qdrant.env and the three luccy files; each dict overrides keys
    of its file (None removes one)."""
    write_env(shared / "backend.env", {**BACKEND, "LUCCY_ENABLED": "True", **SHARED_KEYS, **(backend or {})})
    write_env(shared / "engine.env", ENGINE)
    write_env(shared / "qdrant.env", {**QDRANT, **(qdrant or {})})
    if with_analyzer:
        for name, changes in (("analyzer.env", analyzer), ("analyzer-sync.env", sync), ("analyzer-compare.env", compare)):
            write_env(shared / name, {**ANALYZER_FILES[name], **(changes or {})})


class OffByDefaultTests(unittest.TestCase):
    """With the analyzer off, a release renders and runs exactly what it did before."""

    def states(self):
        before = {"images": {key: image(key, "a") for key in ("backend", "frontend", "gateway", "engine")}}
        # A state that once had the analyzer keeps its image; that must not matter while it is off.
        after = {"images": {**before["images"], "analyzer": image("analyzer", "a")}}
        return before, after

    def test_example_missing_key_and_disabled_mean_off(self):
        for value in (config(), config(None), config({"enabled": False}), config({"enabled": False, "logs_volume": LOGS})):
            with self.subTest(value=value.get("analyzer")):
                deploy.validate_config(value)
                self.assertIsNone(deploy.analyzer_settings(value))

    def test_command_and_environment_are_unchanged_when_off(self):
        before, after = self.states()
        for local in (True, False):
            for variant in (None, "example", {"enabled": False}):
                value = config(variant, local_qdrant=local)
                with self.subTest(local=local, analyzer=variant):
                    self.assertEqual(deploy.compose_command(value, FOLDER), legacy_command(value, FOLDER))
                    self.assertEqual(deploy.compose_environment(value, before, Path("/opt/datagov/shared")),
                                     legacy_environment(value, before, Path("/opt/datagov/shared")))
                    self.assertNotIn("ANALYZER_LOGS_VOLUME", deploy.compose_environment(value, after, Path("/s")))

    @unittest.skipUnless(compose_available(), "needs the docker compose CLI (no daemon)")
    def test_rendered_compose_is_byte_identical_when_off(self):
        before, after = self.states()
        with tempfile.TemporaryDirectory(prefix="datagov-compose-off-") as scratch:
            shared = Path(scratch)
            for name in ("backend", "engine", "qdrant"):
                (shared / (name + ".env")).write_text("SYNTHETIC_CHECK=literal$dollar#hash\n")
            for local in (True, False):
                for variant in (None, "example", {"enabled": False}):
                    value = config(variant, local_qdrant=local)
                    with self.subTest(local=local, analyzer=variant):
                        then = subprocess.check_output(legacy_command(config(None, local_qdrant=local), FOLDER) + ["config"],
                                                       env=legacy_environment(value, before, shared))
                        now = subprocess.check_output(deploy.compose_command(value, FOLDER) + ["config"],
                                                      env=deploy.compose_environment(value, after, shared))
                        self.assertEqual(now, then)
                        self.assertNotIn(b"analyzer", now)

    def test_release_commands_are_unchanged_when_off(self):
        for value in (config(), config(None)):
            calls, expected, _ = release_commands(value, state_images={"analyzer": image("analyzer", "a")})
            compose = [c for c in calls if c[:2] == ["docker", "compose"]]
            self.assertTrue(compose and all(c[:len(expected)] == expected for c in compose))
            self.assertFalse([c for c in calls if any("analyzer" in part for part in c)])


class ConfigTests(unittest.TestCase):
    def test_bad_analyzer_settings_are_rejected(self):
        for value in ([], {"enabled": "true"}, {"enabled": True}, {"logs_volume": LOGS},
                      {"enabled": True, "logs_volume": "bad name"}, {"enabled": True, "logs_volume": LOGS, "image": "x"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy.validate_config(config(value))

    def test_logs_volume_must_be_its_own(self):
        value = config({"enabled": True, "logs_volume": config()["volumes"]["vectors"]})
        with self.assertRaisesRegex(ValueError, "distinct"):
            deploy.validate_config(value)
        deploy.validate_config(config("on"))

    def test_switching_the_analyzer_is_not_a_storage_migration(self):
        state = deploy.next_state(None, manifest(), config(), "release")
        on = deploy.next_state(state, manifest(sequence=2, analyzer=True), config("on"), "release-2")
        off = deploy.next_state(on, manifest(sequence=3), config(), "release-3")
        self.assertEqual(off["topology"], state["topology"])

    def test_manifest_accepts_the_analyzer_image_only_with_an_app_release(self):
        deploy.validate_manifest(manifest(analyzer=True), "yourregistry.azurecr.io")
        engine = manifest("engine")
        engine["images"]["analyzer"] = image("analyzer", "b")
        partial = manifest(analyzer=True)
        del partial["images"]["gateway"]
        foreign = manifest(analyzer=True)
        foreign["images"]["analyzer"] = image("backend", "b")
        for value in (engine, partial, foreign):
            with self.subTest(images=sorted(value["images"])), self.assertRaises(ValueError):
                deploy.validate_manifest(value, "yourregistry.azurecr.io")

    def test_later_releases_keep_the_last_analyzer_image(self):
        state = deploy.next_state(None, manifest(analyzer=True), config("on"), "release")
        engine = deploy.next_state(state, manifest("engine", 2, "c"), config("on"), "unused")
        app = deploy.next_state(engine, manifest("app", 3, "d"), config("on"), "release-3")
        self.assertEqual(app["images"]["analyzer"], image("analyzer", "b"))


class RolloutTests(unittest.TestCase):
    def test_analyzer_starts_after_datagov_and_never_joins_the_maintenance_window(self):
        without, with_analyzer = [], []
        deploy.rollout(["docker", "compose"], "app", True, without.append)
        deploy.rollout(["docker", "compose"], "app", True, with_analyzer.append, analyzer=True)
        self.assertEqual(with_analyzer[:-1], without)
        self.assertEqual(with_analyzer[-1][-1], "analyzer")
        self.assertIn("--wait", with_analyzer[-1])
        self.assertIn("--no-deps", with_analyzer[-1])
        self.assertNotIn("analyzer", [part for call in without for part in call])

    def test_a_failed_analyzer_never_fails_the_datagov_release(self):
        calls, notes = [], []

        def run(command):
            calls.append(command)
            if command[-1] == "analyzer":
                raise RuntimeError("Release command failed")
        deploy.rollout(["docker", "compose"], "app", True, run, analyzer=True, note=notes.append)
        self.assertEqual(calls[-1][-1], "analyzer")
        self.assertEqual(notes, ["analyzer not healthy; DataGov release complete"])
        failing = []
        with self.assertRaises(RuntimeError):         # DataGov's own services still fail a release
            deploy.rollout(["docker", "compose"], "app", True,
                           lambda command: failing.append(command) or (_ for _ in ()).throw(RuntimeError("web")),
                           analyzer=True, note=notes.append)

    def test_engine_release_leaves_the_analyzer_alone(self):
        without, with_analyzer = [], []
        deploy.rollout(["docker", "compose"], "engine", True, without.append)
        deploy.rollout(["docker", "compose"], "engine", True, with_analyzer.append, analyzer=True)
        self.assertEqual(with_analyzer, without)


class ApplyTests(unittest.TestCase):
    def test_enabled_without_an_image_releases_datagov_without_the_analyzer(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calls, _expected, state = release_commands(config("on"))
        self.assertFalse([c for c in calls if any("compose.analyzer.yml" in part for part in c)])
        self.assertIn("ANALYZER_IMAGE_ENABLED", out.getvalue())
        self.assertNotIn("analyzer", state["images"])

    def test_enabled_engine_release_validates_with_the_overlay_but_restarts_only_the_engine(self):
        calls, expected, _ = release_commands(config("on"), state_images={"analyzer": image("analyzer", "a")})
        compose = [c for c in calls if c[:2] == ["docker", "compose"]]
        self.assertTrue(compose and all(c[:len(expected)] == expected for c in compose))
        self.assertIn(["docker", "volume", "inspect", "--format", "{{.Name}}", LOGS], calls)
        changed = [c[len(expected):] for c in compose if {"up", "stop", "pull"} & set(c)]
        self.assertTrue(changed and all("analyzer" not in c for c in changed))

    @unittest.skipIf(os.name == "nt", "Linux deployment symlink")
    def test_an_unhealthy_analyzer_still_completes_the_release(self):
        """pending.json goes, state.json records the release: later engine releases and
        --job runs are not refused because of the analyzer."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calls, _expected, state = release_commands(
                config("on"), release=manifest("app", 2, "c", analyzer=True),
                fails=lambda command: command[-1] == "analyzer" and "up" in command)
        self.assertEqual(state["components"]["app"]["sequence"], 2)
        self.assertIn("analyzer not healthy; DataGov release complete", out.getvalue())
        self.assertIn("DATAGOV_DEPLOY_OK:app:2:", out.getvalue())

    @unittest.skipIf(os.name == "nt", "Linux deployment symlink")
    def test_enabled_app_release_pulls_starts_and_records_the_analyzer(self):
        calls, expected, state = release_commands(config("on"), release=manifest("app", 2, "c", analyzer=True))
        compose = [c for c in calls if c[:2] == ["docker", "compose"]]
        self.assertTrue(all(c[:len(expected)] == expected for c in compose))
        self.assertIn("analyzer", next(c for c in compose if "pull" in c))
        self.assertEqual(compose[-1][len(expected):], ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "600", "analyzer"])
        self.assertEqual(state["images"]["analyzer"], image("analyzer", "c"))


@unittest.skipIf(os.name == "nt", "POSIX secret permissions")
class SecretTests(unittest.TestCase):
    def check(self, backend=None, analyzer=None, value=None, **kwargs):
        with tempfile.TemporaryDirectory() as scratch:
            shared = Path(scratch)
            write_shared(shared, backend, analyzer, **kwargs)
            deploy.validate_secrets(shared, value or config("on"))

    def test_valid_analyzer_settings_pass(self):
        self.check()
        self.check(backend=EVAL_ON)
        # The comparison's judge may use its own Gemini key; luccy containers need no organization id.
        self.check(backend=EVAL_ON, compare={"SERVING_GEMINI_API_KEY": "another-judge-key"},
                   analyzer={"LUCCY_ORGANIZATION_ID": "7"})

    def test_the_comparison_and_concurrency_settings_are_optional(self):
        """With the comparison off, neither its token nor analyzer-compare.env is needed (nor
        checked); LUCCY_MAX_CONCURRENT_ANSWERS defaults to the image's 2."""
        with tempfile.TemporaryDirectory() as scratch:
            shared = Path(scratch)
            write_shared(shared, backend={"LUCCY_EVAL_TOKEN": None}, analyzer={"LUCCY_MAX_CONCURRENT_ANSWERS": None})
            (shared / "analyzer-compare.env").unlink()
            deploy.validate_secrets(shared, config("on"))
            with self.assertRaisesRegex(ValueError, "LUCCY_EVAL_TOKEN"):
                write_env(shared / "backend.env", {**BACKEND, "LUCCY_ENABLED": "True", **SHARED_KEYS,
                                                   "LUCCY_EVAL_TOKEN": None, **EVAL_ON})
                deploy.validate_secrets(shared, config("on"))

    def test_off_never_reads_analyzer_settings(self):
        self.check(value=config(), with_analyzer=False, backend={key: None for key in SHARED_KEYS})

    def test_mistakes_are_rejected(self):
        cases = {
            "shared/analyzer.env is missing": {"with_analyzer": False},
            "LUCCY_SYNC_TOKEN differs between backend.env and analyzer-sync.env": {"backend": {"LUCCY_SYNC_TOKEN": "9" * 32}},
            "LUCCY_EVAL_TOKEN differs between backend.env and analyzer-compare.env": {
                "backend": EVAL_ON, "compare": {"LUCCY_EVAL_TOKEN": "9" * 32}},
            "short LUCCY_API_KEY": {"backend": {"LUCCY_API_KEY": "1" * 31}},
            "short LUCCY_EVAL_TOKEN": {"backend": {**EVAL_ON, "LUCCY_EVAL_TOKEN": "3" * 31}},
            "must differ": {"backend": {"LUCCY_SYNC_TOKEN": "1" * 32}},
            "LUCCY_EVAL_TOKEN must differ": {"backend": {**EVAL_ON, "LUCCY_EVAL_TOKEN": "2" * 32}},
            "not existing DataGov keys": {"backend": {"LUCCY_API_KEY": "s" * 32}},
            "numeric DataGov organization id": {"backend": {"LUCCY_ORGANIZATION_ID": "seven"}},
            "Set SERVING_GEMINI_API_KEY in analyzer.env": {"analyzer": {"SERVING_GEMINI_API_KEY": None}},
            "Set SERVING_GEMINI_API_KEY in analyzer-compare.env": {"backend": EVAL_ON, "compare": {"SERVING_GEMINI_API_KEY": None}},
            "LUCCY_QDRANT_URL in analyzer.env": {"analyzer": {"LUCCY_QDRANT_URL": "http://elsewhere:6333"}},
            "LUCCY_QDRANT_URL in analyzer-sync.env": {"sync": {"LUCCY_QDRANT_URL": None}},
            "1, 2 or 3": {"analyzer": {"LUCCY_MAX_CONCURRENT_ANSWERS": "4"}},
            "analyzer.env must name the engine's chunk collection": {"analyzer": {"LUCCY_LIVE_CHUNKS_COLLECTION": "document_explorer_v2"}},
            "analyzer-sync.env must name the engine's chunk collection": {"sync": {"DOCUMENT_EXPLORER_COLLECTION": "document_explorer_v2"}},
            "LUCCY_EVAL_USER_ID": {"backend": {"LUCCY_EVAL_ENABLED": "True"}},
        }
        for message, kwargs in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                self.check(**kwargs)
        with self.assertRaisesRegex(ValueError, "LUCCY_ORGANIZATION_ID"):
            self.check({"LUCCY_ORGANIZATION_ID": None})

    def test_every_luccy_file_is_required(self):
        """analyzer-compare.env only while the comparison is on."""
        for name in deploy.ANALYZER_ENV_FILES:
            with self.subTest(name), tempfile.TemporaryDirectory() as scratch:
                shared = Path(scratch)
                write_shared(shared, backend=EVAL_ON)
                (shared / name).unlink()
                with self.assertRaisesRegex(ValueError, f"shared/{name} is missing"):
                    deploy.validate_secrets(shared, config("on"))

    def test_each_container_holds_only_its_own_secrets(self):
        """The serving container never holds the sync or eval token or the Qdrant write key;
        the sync never the API key, eval token or Gemini; the comparison no Qdrant key."""
        cases = {
            "analyzer.env may hold only": [{"analyzer": {"LUCCY_SYNC_TOKEN": TOKENS["LUCCY_SYNC_TOKEN"]}},
                                           {"analyzer": {"LUCCY_EVAL_TOKEN": TOKENS["LUCCY_EVAL_TOKEN"]}},
                                           {"analyzer": {"LUCCY_QDRANT_SYNC_API_KEY": "q" * 24}}],
            "analyzer-sync.env may hold only": [{"sync": {"LUCCY_API_KEY": TOKENS["LUCCY_API_KEY"]}},
                                                {"sync": {"LUCCY_EVAL_TOKEN": TOKENS["LUCCY_EVAL_TOKEN"]}},
                                                {"sync": {"SERVING_GEMINI_API_KEY": "synthetic-luccy"}},
                                                {"sync": {"LUCCY_QDRANT_API_KEY": READ_ONLY}}],
            "analyzer-compare.env may hold only": [{"backend": EVAL_ON, "compare": {"LUCCY_QDRANT_API_KEY": READ_ONLY}},
                                                   {"backend": EVAL_ON, "compare": {"LUCCY_QDRANT_SYNC_API_KEY": "q" * 24}}],
            # The same secret under another name (the eval token too, even with the comparison off).
            "another analyzer container's secret": [{"analyzer": {"EXTRA": TOKENS["LUCCY_SYNC_TOKEN"]}},
                                                    {"analyzer": {"EXTRA": TOKENS["LUCCY_EVAL_TOKEN"]}},
                                                    {"analyzer": {"QDRANT_API_KEY": "q" * 24}},
                                                    {"sync": {"GEMINI": "synthetic-luccy"}},
                                                    {"sync": {"LUCCY_API_TOKEN": TOKENS["LUCCY_API_KEY"]}},
                                                    {"backend": EVAL_ON, "compare": {"QDRANT": "q" * 24}},
                                                    {"backend": EVAL_ON, "compare": {"QDRANT": READ_ONLY}}],
        }
        for message, variants in cases.items():
            for kwargs in variants:
                with self.subTest(kwargs), self.assertRaisesRegex(ValueError, message):
                    self.check(**kwargs)

    def test_qdrant_keys(self):
        """The sync writes with the engine's key; the analyzer searches with the read-only key
        when qdrant.env sets one, and may use the engine's key only when it does not."""
        no_read_only = {"QDRANT__SERVICE__READ_ONLY_API_KEY": None}
        self.check(qdrant=no_read_only, analyzer={"LUCCY_QDRANT_API_KEY": "q" * 24})
        cases = {
            "LUCCY_QDRANT_SYNC_API_KEY in analyzer-sync.env must be the engine's": [
                {"sync": {"LUCCY_QDRANT_SYNC_API_KEY": READ_ONLY}}, {"sync": {"LUCCY_QDRANT_SYNC_API_KEY": None}}],
            "must be QDRANT__SERVICE__READ_ONLY_API_KEY": [
                {"analyzer": {"LUCCY_QDRANT_API_KEY": "q" * 24}}, {"analyzer": {"LUCCY_QDRANT_API_KEY": "x" * 24}}],
            "must be the engine's QDRANT_API_KEY, or set": [
                {"qdrant": no_read_only}, {"qdrant": no_read_only, "analyzer": {"LUCCY_QDRANT_API_KEY": None}}],
            "different random key": [
                {"qdrant": {"QDRANT__SERVICE__READ_ONLY_API_KEY": "q" * 24}, "analyzer": {"LUCCY_QDRANT_API_KEY": "q" * 24}},
                {"qdrant": {"QDRANT__SERVICE__READ_ONLY_API_KEY": "r" * 23}, "analyzer": {"LUCCY_QDRANT_API_KEY": "r" * 23}}],
        }
        for message, variants in cases.items():
            for kwargs in variants:
                with self.subTest(kwargs), self.assertRaisesRegex(ValueError, message):
                    self.check(**kwargs)

    def test_read_only_key_is_checked_without_the_analyzer_too(self):
        self.check(value=config(), with_analyzer=False, backend={key: None for key in SHARED_KEYS},
                   qdrant={"QDRANT__SERVICE__READ_ONLY_API_KEY": None})
        with self.assertRaisesRegex(ValueError, "different random key"):
            self.check(value=config(), with_analyzer=False, qdrant={"QDRANT__SERVICE__READ_ONLY_API_KEY": "q" * 24})

    def test_managed_qdrant_accepts_a_read_only_cluster_key(self):
        managed = {**ENGINE, "QDRANT_URL": "https://cluster.example:6333", "QDRANT_API_KEY": "m" * 32}
        for serving_key in ("m" * 32, "cluster-read-only-" + "k" * 16):
            with self.subTest(serving_key), tempfile.TemporaryDirectory() as scratch:
                shared = Path(scratch)
                write_shared(shared, analyzer={"LUCCY_QDRANT_URL": managed["QDRANT_URL"], "LUCCY_QDRANT_API_KEY": serving_key},
                             sync={"LUCCY_QDRANT_URL": managed["QDRANT_URL"], "LUCCY_QDRANT_SYNC_API_KEY": "m" * 32})
                write_env(shared / "engine.env", managed)
                (shared / "qdrant.env").unlink()
                deploy.validate_secrets(shared, config("on", local_qdrant=False))

    def test_luccy_files_must_be_private(self):
        for name in deploy.ANALYZER_ENV_FILES:
            with self.subTest(name), tempfile.TemporaryDirectory() as scratch:
                shared = Path(scratch)
                write_shared(shared, backend=EVAL_ON)
                (shared / name).chmod(0o644)
                with self.assertRaisesRegex(ValueError, "0600"):
                    deploy.validate_secrets(shared, config("on"))

    def test_examples_match_the_rules(self):
        """The templates in deploy/azure name exactly the secrets their container may hold."""
        for name, needs in deploy.ANALYZER_ENV_FILES.items():
            with self.subTest(name):
                text = (FOLDER / (name + ".example")).read_text()
                keys = {line.partition("=")[0] for line in text.splitlines() if line and not line.startswith("#")}
                self.assertEqual(keys & set(deploy.ANALYZER_SECRETS), set(needs))


@unittest.skipIf(os.name == "nt", "Linux job lock and secret permissions")
class JobTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / "shared").mkdir()
        write_shared(self.root / "shared")
        self.use(config("on"))
        release_dir = self.root / "releases/app-1"
        release_dir.mkdir(parents=True)
        for name in deploy.BUNDLE_FILES:
            shutil.copyfile(FOLDER / name, release_dir / name)
        state = deploy.next_state(None, manifest(analyzer=True), config("on"), release_dir)
        (self.root / "state.json").write_text(json.dumps(state))
        self.calls = []

    def use(self, value):
        (self.root / "shared/deployment.json").write_text(json.dumps(value))

    def run_fake(self, command, env, check):
        self.calls.append((command, env))
        return subprocess.CompletedProcess(command, 3)

    def test_sync_runs_a_one_off_container_against_the_current_release(self):
        self.assertEqual(deploy.run_job(self.root, "analyzer-sync", run=self.run_fake), 3)
        command, env = self.calls[0]
        self.assertEqual(command[-len(deploy.JOBS["analyzer-sync"]):], deploy.JOBS["analyzer-sync"])
        self.assertIn(str(self.root / "releases/app-1/compose.analyzer.yml"), command)
        self.assertIn("--rm", command)
        self.assertNotIn("up", command)
        self.assertEqual(env["ANALYZER_IMAGE"], image("analyzer", "b"))
        self.assertEqual(env["ANALYZER_LOGS_VOLUME"], LOGS)

    def test_analyzer_jobs_need_the_analyzer_but_the_ordinal_backfill_does_not(self):
        self.use(config())
        with self.assertRaisesRegex(ValueError, "analyzer is off"):
            deploy.run_job(self.root, "analyzer-sync", run=self.run_fake)
        deploy.run_job(self.root, "backfill-ordinals", run=self.run_fake)
        command, _ = self.calls[0]
        self.assertEqual(command[-7:], ["exec", "-T", "document-worker", "python", "-m", "document_explorer.ordinals", "backfill"])
        self.assertFalse(any("analyzer" in part for part in command))

    def test_unfinished_release_or_unknown_job_is_refused(self):
        with self.assertRaises(ValueError):
            deploy.run_job(self.root, "shell", run=self.run_fake)
        (self.root / "pending.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "unfinished"):
            deploy.run_job(self.root, "analyzer-sync", run=self.run_fake)
        self.assertEqual(self.calls, [])

    def test_comparison_needs_evaluation_enabled(self):
        with self.assertRaisesRegex(ValueError, "LUCCY_EVAL_ENABLED"):
            deploy.run_job(self.root, "analyzer-compare", run=self.run_fake)
        write_shared(self.root / "shared", backend={"LUCCY_EVAL_ENABLED": "True", "LUCCY_EVAL_USER_ID": "12"})
        deploy.run_job(self.root, "analyzer-compare", run=self.run_fake)
        self.assertEqual(self.calls[0][0][-1], "analyzer-compare")

    def test_only_the_comparison_takes_plain_options(self):
        write_shared(self.root / "shared", backend={"LUCCY_EVAL_ENABLED": "True", "LUCCY_EVAL_USER_ID": "12"})
        deploy.run_job(self.root, "analyzer-compare", ["--dry-run", "--limit=10"], run=self.run_fake)
        self.assertEqual(self.calls[0][0][-6:], ["analyzer-compare", "python", "-m", "luccy_qdrant.compare", "--dry-run", "--limit=10"])
        for name, options in (("analyzer-sync", ["--dry-run"]), ("analyzer-compare", ["--limit=10;rm"]),
                              ("analyzer-compare", ["--run-id", "a b"])):
            with self.subTest(name=name, options=options), self.assertRaises(ValueError):
                deploy.run_job(self.root, name, options, run=self.run_fake)
        self.assertEqual(len(self.calls), 1)

    def test_overlapping_run_is_skipped(self):
        import fcntl
        with (self.root / ".job-analyzer-sync.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(deploy.run_job(self.root, "analyzer-sync", run=self.run_fake), 0)
        self.assertEqual(self.calls, [])


class CommandLineTests(unittest.TestCase):
    def main(self, *argv):
        with tempfile.TemporaryDirectory() as root, patch("sys.argv", ["deploy.py", "--root", root, *argv]), \
             patch.object(deploy, "run_job", return_value=0) as job, patch.object(deploy, "apply") as release, \
             patch.object(deploy.os, "umask"):
            with self.assertRaises(SystemExit) as stopped:
                deploy.main()
        return stopped.exception.code, job, release

    def test_jobs_and_releases_are_separate_commands(self):
        code, job, release = self.main("--job", "analyzer-compare", "--dry-run", "--limit=10")
        self.assertEqual(code, 0)
        self.assertEqual(job.call_args.args[1:], ("analyzer-compare", ["--dry-run", "--limit=10"]))
        release.assert_not_called()
        for argv in (("--job", "analyzer-sync", "--dry-run"), ("--job", "shell"), (),
                     ("--manifest", "m.json", "--job", "analyzer-sync")):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                code, job, release = self.main(*argv)
                self.assertEqual(code, 2)
                job.assert_not_called()
                release.assert_not_called()


class ReleaseClientTests(unittest.TestCase):
    ENV = {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "12",
           "ACR_LOGIN_SERVER": "yourregistry.azurecr.io", "BACKEND_DIGEST": "sha256:" + "b" * 64,
           "FRONTEND_DIGEST": "sha256:" + "b" * 64, "GATEWAY_DIGEST": "sha256:" + "b" * 64}

    def test_analyzer_image_only_when_ci_built_it(self):
        for extra in ({}, {"ANALYZER_DIGEST": ""}):
            self.assertNotIn("analyzer", release.manifest_from_env("app", {**self.ENV, **extra})["images"])
        built = release.manifest_from_env("app", {**self.ENV, "ANALYZER_DIGEST": "sha256:" + "c" * 64})
        self.assertEqual(built["images"]["analyzer"], image("analyzer", "c"))
        deploy.validate_manifest(built, "yourregistry.azurecr.io")
        with self.assertRaises(ValueError):
            release.manifest_from_env("app", {**self.ENV, "ANALYZER_DIGEST": "latest"})
        engine = {**self.ENV, "ENGINE_DIGEST": "sha256:" + "d" * 64, "ANALYZER_DIGEST": "sha256:" + "c" * 64}
        self.assertEqual(set(release.manifest_from_env("engine", engine)["images"]), {"engine"})

    def test_bundle_carries_the_analyzer_overlay(self):
        self.assertEqual(release.BUNDLE, deploy.BUNDLE_FILES)
        script = release.remote_script(manifest(), FOLDER)
        code = script.split("DATAGOV_PY'\n", 1)[1].rsplit("DATAGOV_PY", 1)[0]
        compile(code, "remote.py", "exec")
        encoded = code.split("b64decode('", 1)[1].split("'", 1)[0]
        files = json.loads(gzip.decompress(base64.b64decode(encoded)))["files"]
        self.assertEqual(set(files), set(deploy.BUNDLE_FILES))
        self.assertEqual(files["compose.analyzer.yml"], (FOLDER / "compose.analyzer.yml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
