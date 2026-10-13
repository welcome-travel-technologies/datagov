import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

FOLDER = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, FOLDER / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


deploy, release = module("deploy"), module("release")


def config():
    value = json.loads((FOLDER / "deployment.example.json").read_text())
    value["initial_engine_image"] = image("engine", "a")
    return value


def image(key, digit):
    return "yourregistry.azurecr.io/" + deploy.REPOSITORIES[key] + "@sha256:" + digit * 64


def manifest(component="app", sequence=1, digit="b"):
    keys = deploy.APP_KEYS if component == "app" else {"engine"}
    return {"component": component, "sequence": sequence, "revision": digit * 40,
            "images": {key: image(key, digit) for key in keys}}


class StateTests(unittest.TestCase):
    def test_app_and_engine_releases_preserve_each_other(self):
        initial = deploy.next_state(None, manifest(), config(), "/opt/datagov/releases/app-1")
        engine = deploy.next_state(initial, manifest("engine", 8, "c"), config(), "unused")
        latest = deploy.next_state(engine, manifest("app", 10, "d"), config(), "/opt/datagov/releases/app-10")
        self.assertEqual(latest["images"]["engine"], image("engine", "c"))
        self.assertEqual(engine["app_release"], initial["app_release"])
        self.assertEqual(initial["images"]["engine"], image("engine", "a"))

    def test_engine_requires_bootstrap(self):
        with self.assertRaisesRegex(ValueError, "Deploy DataGov first"):
            deploy.next_state(None, manifest("engine"), config(), "unused")

    def test_mutable_foreign_and_injected_image_references_rejected(self):
        for value in ("yourregistry.azurecr.io/document-explorer:latest", image("backend", "a"),
                      "other.azurecr.io/document-explorer@sha256:" + "a" * 64,
                      image("engine", "a") + "\n$(echo unsafe)"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy.image_ref(value, "yourregistry.azurecr.io", "engine")

    def test_storage_topology_changes_cannot_silently_create_empty_database(self):
        state = deploy.next_state(None, manifest(), config(), "release")
        for key, value in (("local_qdrant", False), ("project", "different"),
                           ("volumes", {**config()["volumes"], "vectors": "empty_vectors"})):
            changed = config()
            changed[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "planned migration"):
                deploy.next_state(state, manifest(sequence=2), changed, "release-2")

    def test_older_release_or_mutated_successful_id_rejected(self):
        state = deploy.next_state(None, manifest(sequence=5), config(), "release")
        for request in (manifest(sequence=4), manifest(sequence=5, digit="c")):
            with self.assertRaises(ValueError):
                deploy.next_state(state, request, config(), "release")

    def test_engine_only_rollout_does_not_touch_application_database_or_qdrant(self):
        calls = []
        deploy.rollout(["docker", "compose"], "engine", True, calls.append)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("document-vectors", str(calls))
        self.assertNotIn("migrate", str(calls))
        self.assertNotIn("down", str(calls))
        self.assertIn("--no-deps", calls[-1])

    def test_migration_failure_stops_before_starting_application(self):
        calls = []
        def run(command):
            calls.append(command)
            if "migrate" in command:
                raise RuntimeError("migration failed")
        with self.assertRaises(RuntimeError):
            deploy.rollout(["docker", "compose"], "app", False, run)
        self.assertEqual(calls[0][2], "stop")
        self.assertFalse(any("up" in command for command in calls))

    def test_failed_health_does_not_promote_successful_state(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "shared").mkdir()
            (root / "shared/deployment.json").write_text(json.dumps(config()))
            current = deploy.next_state(None, manifest(), config(), root / "releases/app-1")
            (root / "state.json").write_text(json.dumps(current))
            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, 1 if "--wait" in command else 0)
            with patch.object(deploy, "validate_secrets"), patch.object(deploy, "release_lock", return_value=contextlib.nullcontext()), \
                 patch.object(deploy.subprocess, "check_output", return_value="2.33.1"), patch.object(deploy.subprocess, "run", side_effect=run):
                with self.assertRaises(RuntimeError):
                    deploy.apply(root, manifest("engine", 2, "c"))
            self.assertEqual(json.loads((root / "state.json").read_text()), current)
            self.assertTrue((root / "pending.json").exists())
            self.assertFalse((root / "previous.json").exists())

    def test_successful_engine_update_records_previous_images(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "shared").mkdir()
            (root / "shared/deployment.json").write_text(json.dumps(config()))
            current = deploy.next_state(None, manifest(), config(), root / "releases/app-1")
            (root / "state.json").write_text(json.dumps(current))
            with patch.object(deploy, "validate_secrets"), patch.object(deploy, "release_lock", return_value=contextlib.nullcontext()), \
                 patch.object(deploy.subprocess, "check_output", return_value="2.33.1"), \
                 patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                deploy.apply(root, manifest("engine", 2, "c"))
            self.assertEqual(json.loads((root / "previous.json").read_text()), current)
            self.assertEqual(json.loads((root / "state.json").read_text())["images"]["engine"], image("engine", "c"))
            self.assertFalse((root / "pending.json").exists())

    @unittest.skipIf(os.name == "nt", "Linux deployment symlink")
    def test_successful_app_installs_current_controller(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "shared").mkdir()
            (root / "shared/deployment.json").write_text(json.dumps(config()))
            with patch.object(deploy, "validate_secrets"), patch.object(deploy, "release_lock", return_value=contextlib.nullcontext()), \
                 patch.object(deploy.subprocess, "check_output", return_value="2.33.1"), \
                 patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
                deploy.apply(root, manifest(), FOLDER)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual((root / "current").resolve(), Path(state["app_release"]))
            self.assertEqual((root / "current/deploy.py").read_text(), (FOLDER / "deploy.py").read_text())
            self.assertFalse((root / "pending.json").exists())

    def test_unfinished_other_component_or_newer_release_blocks_deployment(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "shared").mkdir()
            (root / "shared/deployment.json").write_text(json.dumps(config()))
            (root / "pending.json").write_text(json.dumps(manifest(sequence=10)))
            with patch.object(deploy, "validate_secrets"), patch.object(deploy, "release_lock", return_value=contextlib.nullcontext()):
                with self.assertRaisesRegex(ValueError, "other component"):
                    deploy.apply(root, manifest("engine", 11))
                with self.assertRaisesRegex(ValueError, "older than the unfinished"):
                    deploy.apply(root, manifest(sequence=9), FOLDER)

    @unittest.skipIf(os.name == "nt", "POSIX secret permissions")
    def test_worker_callback_host_and_keys_are_validated(self):
        with tempfile.TemporaryDirectory() as scratch:
            shared = Path(scratch)
            values = {
                "backend.env": "SECRET_KEY=" + "s" * 32 + "\nDATAGOV_DATABASE_TARGET=local\nDB_HOST=db\nDB_NAME=catalog\nDB_USER=catalog\nDB_PASSWORD=synthetic\nDOCUMENT_EXPLORER_ENABLED=True\nDJANGO_ALLOWED_HOSTS=datagov.welcomd.com,web\nDOCUMENT_EXPLORER_PUBLIC_URL=https://datagov.welcomd.com\nDOCUMENT_EXPLORER_API_KEY=" + "a" * 24 + "\nDOCUMENT_EXPLORER_WORKER_TOKEN=" + "w" * 24 + "\n",
                "engine.env": "DOCUMENT_EXPLORER_API_KEY=" + "a" * 24 + "\nDOCUMENT_EXPLORER_WORKER_TOKEN=" + "w" * 24 + "\nSERVING_GEMINI_API_KEY=synthetic\nQDRANT_URL=http://document-vectors:6333\nQDRANT_API_KEY=" + "q" * 24 + "\n",
                "qdrant.env": "QDRANT__SERVICE__API_KEY=" + "q" * 24 + "\n",
            }
            for name, content in values.items():
                (shared / name).write_text(content)
                (shared / name).chmod(0o600)
            deploy.validate_secrets(shared, config())
            (shared / "backend.env").write_text(values["backend.env"].replace(",web", ""))
            with self.assertRaisesRegex(ValueError, "callback host web"):
                deploy.validate_secrets(shared, config())
            (shared / "backend.env").write_text(values["backend.env"])
            (shared / "engine.env").write_text(values["engine.env"].replace("a" * 24, "b" * 24))
            with self.assertRaisesRegex(ValueError, "mismatched"):
                deploy.validate_secrets(shared, config())


class TransportTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX secret ownership")
    def test_manual_registry_login_consumes_token_without_logging_it(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as scratch:
                path = Path(scratch) / "registry.json"
                token = "synthetic-short-lived-operator-token"
                path.write_text(json.dumps({"registry_host": config()["registry_host"], "access_token": token}))
                path.chmod(0o600)
                log = io.StringIO()
                with patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode)) as execute:
                    if returncode:
                        with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                            deploy.registry_login(config(), {}, log, self.fail, path)
                    else:
                        deploy.registry_login(config(), {}, log, self.fail, path)
                self.assertFalse(path.exists())
                self.assertEqual(execute.call_args.kwargs["input"], token)
                self.assertNotIn(token, str(execute.call_args.args))
                self.assertNotIn(token, log.getvalue())

    @unittest.skipIf(os.name == "nt", "POSIX secret ownership")
    def test_manual_registry_login_rejects_public_symlink_and_foreign_credentials(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "registry.json"
            path.write_text(json.dumps({"registry_host": "foreign.azurecr.io", "access_token": "synthetic"}))
            path.chmod(0o644)
            with patch.object(deploy.subprocess, "run") as execute:
                with self.assertRaisesRegex(ValueError, "0600"):
                    deploy.registry_login(config(), {}, io.StringIO(), self.fail, path)
                path.chmod(0o600)
                link = Path(scratch) / "link.json"
                link.symlink_to(path)
                with self.assertRaisesRegex(ValueError, "regular file"):
                    deploy.registry_login(config(), {}, io.StringIO(), self.fail, link)
                with self.assertRaisesRegex(ValueError, "does not match"):
                    deploy.registry_login(config(), {}, io.StringIO(), self.fail, path)
                execute.assert_not_called()
                self.assertFalse(path.exists())

    def test_azure_success_without_guest_success_is_failure(self):
        self.assertFalse(release.succeeded({"value": [{"code": "ProvisioningState/succeeded", "message": "script failed"}]}, manifest()))
        marker = "DATAGOV_DEPLOY_OK:app:1:" + "b" * 40
        self.assertTrue(release.succeeded({"value": [{"message": "[stdout]\n" + marker + "\n[stderr]"}]}, manifest()))
        self.assertFalse(release.succeeded({"value": [{"message": "prefix " + marker}]}, manifest()))

    def test_only_definite_busy_rejection_is_retried(self):
        env = {"AZURE_VM_NAME": "vm", "AZURE_RESOURCE_GROUP": "group"}
        calls, sleeps = [], []
        def busy_then_success(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 1, "", "Run command extension execution is in progress")
            return subprocess.CompletedProcess(command, 0, json.dumps({"value": [{"message": "DATAGOV_DEPLOY_OK:app:1:" + "b" * 40}]}), "")
        release.invoke("script", manifest(), env, run=busy_then_success, sleep=sleeps.append)
        self.assertEqual(sleeps, [30])
        calls.clear()
        def failed(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "timeout; outcome unknown")
        with self.assertRaises(RuntimeError):
            release.invoke("script", manifest(), env, run=failed, sleep=sleeps.append)
        self.assertEqual(len(calls), 1)

    def test_main_branch_and_digest_required(self):
        env = {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "12",
               "ACR_LOGIN_SERVER": "yourregistry.azurecr.io", "ENGINE_DIGEST": "sha256:" + "b" * 64}
        self.assertEqual(release.manifest_from_env("engine", env)["images"]["engine"], image("engine", "b"))
        for key, value in (("GITHUB_REF", "refs/pull/1/merge"), ("ENGINE_DIGEST", "latest")):
            with self.assertRaises(ValueError):
                release.manifest_from_env("engine", {**env, key: value})

    def test_payload_preserves_bundle_and_never_embeds_shell_interpolation(self):
        script = release.remote_script(manifest(), FOLDER)
        self.assertNotIn("SECRET_KEY", script)
        code = script.split("DATAGOV_PY'\n", 1)[1].rsplit("DATAGOV_PY", 1)[0]
        compile(code, "remote.py", "exec")

    @unittest.skipIf(os.name == "nt", "POSIX secret permissions")
    def test_secret_files_literal_parsing_and_mode(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "secret.env"
            path.write_text("KEY=literal$dollar#hash\n")
            path.chmod(0o600)
            self.assertEqual(deploy.read_env(path)["KEY"], "literal$dollar#hash")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                deploy.read_env(path)


if __name__ == "__main__":
    unittest.main()
