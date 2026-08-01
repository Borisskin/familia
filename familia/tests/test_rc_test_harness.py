"""Behavior and security contract for the RP-020 Linux evidence harness."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = REPO_ROOT / "scripts" / "capture_test_evidence.py"
RUNNER = REPO_ROOT / "bin" / "rc-test-env.sh"
REQUIRED_BUNDLE_ENTRIES = {
    "command.json",
    "environment.json",
    "hashes.json",
    "manifest.json",
    "source.json",
    "stderr.bin",
    "stdout.bin",
}
SECRET_SENTINEL = "rp020-secret-sentinel-7f83b636c89f4f8d"


def _supports_final_capture_environment() -> bool:
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        return False
    release = platform.release().lower()
    if "microsoft" not in release or "wsl2" not in release:
        return False
    try:
        os_release = {
            key.lower(): value.strip().strip('"')
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
            for key, value in [line.split("=", 1)]
        }
    except OSError:
        return False
    return os_release.get("id") == "ubuntu"


requires_final_capture_environment = unittest.skipUnless(
    _supports_final_capture_environment(),
    "RP-020 final-capture environment is unavailable",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "ascii"
    )


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=cwd or REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


class CollectorFixture:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="rp020-collector-")
        self.root = Path(self._temporary.name)
        self.repo = self.root / "repo"
        self.evidence = self.root / "evidence"
        self.runtime = self.root / "runtime"
        self.repo.mkdir()
        self.evidence.mkdir()
        self.runtime.mkdir()
        for name in ("venv", "home", "tmp", "cache", "pip-cache"):
            (self.runtime / name).mkdir()

        (self.repo / ".gitignore").write_text("*.secret\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        init = _run(["git", "init", "--quiet", str(self.repo)], cwd=self.root)
        if init.returncode != 0:
            raise AssertionError(init.stderr.decode("utf-8", "replace"))
        add = _run(["git", "-C", str(self.repo), "add", ".gitignore", "tracked.txt"])
        if add.returncode != 0:
            raise AssertionError(add.stderr.decode("utf-8", "replace"))
        commit = _run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=RP020 Test",
                "-c",
                "user.email=rp020@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ]
        )
        if commit.returncode != 0:
            raise AssertionError(commit.stderr.decode("utf-8", "replace"))

    def cleanup(self) -> None:
        self._temporary.cleanup()

    def command(self, run_id: str, target: list[str], *extra: str) -> list[str]:
        return [
            sys.executable,
            str(COLLECTOR),
            "--repo-root",
            str(self.repo),
            "--evidence-root",
            str(self.evidence),
            "--run-id",
            run_id,
            "--venv-root",
            str(self.runtime / "venv"),
            "--home-root",
            str(self.runtime / "home"),
            "--tmp-root",
            str(self.runtime / "tmp"),
            "--cache-root",
            str(self.runtime / "cache"),
            "--pip-cache-root",
            str(self.runtime / "pip-cache"),
            "--input",
            "tracked.txt",
            *extra,
            "--",
            *target,
        ]

    def bundle(self, run_id: str) -> Path:
        return self.evidence / run_id


class RcTestHarnessTests(unittest.TestCase):
    maxDiff = None

    def _require_collector(self) -> None:
        self.assertTrue(COLLECTOR.is_file(), "missing RP-020 evidence collector implementation")

    def _require_runner(self) -> None:
        self.assertTrue(RUNNER.is_file(), "missing RP-020 environment runner implementation")

    def test_help_paths_are_non_mutating(self) -> None:
        self._require_collector()
        self._require_runner()
        with tempfile.TemporaryDirectory(prefix="rp020-help-") as raw:
            root = Path(raw)
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            collector = _run([sys.executable, str(COLLECTOR), "--help"], cwd=root)
            runner = _run(["bash", str(RUNNER), "--help"], cwd=root)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

        self.assertEqual(collector.returncode, 0, collector.stderr)
        self.assertIn(b"usage:", collector.stdout.lower())
        self.assertEqual(runner.returncode, 0, runner.stderr)
        self.assertIn(b"usage:", runner.stdout.lower())
        self.assertEqual(after, before)

    def test_collector_import_has_no_runtime_side_effects(self) -> None:
        self._require_collector()
        with tempfile.TemporaryDirectory(prefix="rp020-import-") as raw:
            root = Path(raw)
            code = textwrap.dedent(
                f"""
                import importlib.util, json, os, pathlib, subprocess
                root = pathlib.Path({str(root)!r})
                before_env = dict(os.environ)
                before_files = sorted(str(p.relative_to(root)) for p in root.rglob('*'))
                original_popen = subprocess.Popen
                def forbidden(*args, **kwargs):
                    raise AssertionError('process launched during import')
                subprocess.Popen = forbidden
                try:
                    spec = importlib.util.spec_from_file_location('rp020_capture_import', {str(COLLECTOR)!r})
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                finally:
                    subprocess.Popen = original_popen
                after_files = sorted(str(p.relative_to(root)) for p in root.rglob('*'))
                assert dict(os.environ) == before_env
                assert after_files == before_files
                print('import-safe-ok')
                """
            )
            result = _run([sys.executable, "-c", code], cwd=root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"import-safe-ok\n")
        self.assertEqual(result.stderr, b"")

    @requires_final_capture_environment
    def test_argv_round_trips_without_shell_interpretation(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        injected = fixture.root / "injected"
        arguments = [
            "space value",
            "single'quote",
            'double"quote',
            f";touch {injected}",
            f"$(touch {injected})",
            "*?[abc]",
            "",
            "Юникод-参数",
        ]
        program = (
            "import json,sys; "
            "sys.stdout.buffer.write(json.dumps(sys.argv[1:],ensure_ascii=False,"
            "separators=(',',':')).encode())"
        )
        target = [sys.executable, "-c", program, *arguments]
        result = _run(fixture.command("argv-roundtrip", target), cwd=fixture.repo)

        expected = json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, b"")
        self.assertFalse(injected.exists())
        command = json.loads((fixture.bundle("argv-roundtrip") / "command.json").read_bytes())
        self.assertEqual(command["argv"], target)
        self.assertEqual(command["target_exit_code"], 0)
        self.assertEqual(command["collector_status"], "captured")

    @requires_final_capture_environment
    def test_exact_stream_bytes_and_arbitrary_exit_are_preserved(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        stdout = b"out\x00\xff\r\nno-final-newline"
        stderr = b"err\x00\xfe\n\rraw"
        program = (
            f"import os; os.write(1,{stdout!r}); os.write(2,{stderr!r}); "
            "raise SystemExit(37)"
        )
        result = _run(
            fixture.command("byte-fidelity", [sys.executable, "-c", program]),
            cwd=fixture.repo,
        )
        bundle = fixture.bundle("byte-fidelity")
        command = json.loads((bundle / "command.json").read_bytes())

        self.assertEqual(result.returncode, 37)
        self.assertEqual(result.stdout, stdout)
        self.assertEqual(result.stderr, stderr)
        self.assertEqual((bundle / "stdout.bin").read_bytes(), stdout)
        self.assertEqual((bundle / "stderr.bin").read_bytes(), stderr)
        self.assertEqual(command["target_exit_code"], 37)
        self.assertEqual(command["target_returncode"], 37)
        self.assertIsNone(command["target_signal"])
        self.assertEqual(command["collector_status"], "captured")

    @requires_final_capture_environment
    def test_deliberate_assertion_failure_is_not_reported_as_success(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        target = [sys.executable, "-c", "raise AssertionError('rp020 deliberate failure')"]
        result = _run(fixture.command("assertion-red", target), cwd=fixture.repo)
        bundle = fixture.bundle("assertion-red")
        command = json.loads((bundle / "command.json").read_bytes())

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, (bundle / "stdout.bin").read_bytes())
        self.assertEqual(result.stderr, (bundle / "stderr.bin").read_bytes())
        self.assertIn(b"AssertionError: rp020 deliberate failure", result.stderr)
        self.assertEqual(command["target_exit_code"], 1)
        self.assertEqual(command["collector_status"], "captured")
        self.assertTrue(json.loads((bundle / "manifest.json").read_bytes())["valid"])

    @requires_final_capture_environment
    def test_signal_identity_and_shell_status_are_retained(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        program = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"
        result = _run(
            fixture.command("signal-term", [sys.executable, "-c", program]),
            cwd=fixture.repo,
        )
        command = json.loads((fixture.bundle("signal-term") / "command.json").read_bytes())

        self.assertEqual(result.returncode, 128 + signal.SIGTERM)
        self.assertEqual(command["target_returncode"], -signal.SIGTERM)
        self.assertIsNone(command["target_exit_code"])
        self.assertEqual(command["target_signal"]["number"], signal.SIGTERM)
        self.assertEqual(command["target_signal"]["name"], "SIGTERM")
        self.assertEqual(command["shell_status"], 128 + signal.SIGTERM)

    @requires_final_capture_environment
    def test_command_not_found_is_an_explicit_collector_failure(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        result = _run(
            fixture.command("not-found", ["rp020-command-that-does-not-exist"]),
            cwd=fixture.repo,
        )
        bundle = fixture.bundle("not-found")
        command = json.loads((bundle / "command.json").read_bytes())
        failure = json.loads((bundle / "failure.json").read_bytes())

        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(SECRET_SENTINEL.encode(), result.stderr)
        self.assertEqual(command["collector_status"], "command_not_found")
        self.assertIsNone(command["target_returncode"])
        self.assertEqual(failure["kind"], "command_not_found")
        self.assertFalse(json.loads((bundle / "manifest.json").read_bytes())["valid"])

    def test_identifiers_traversal_absolute_and_reserved_names_are_rejected(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        invalid = ["", ".", "..", "../escape", "a/b", "a\\b", "/absolute", "bad\nname", "ＭＡＮＩＦＥＳＴ.JSON"]
        for index, run_id in enumerate(invalid):
            with self.subTest(run_id=run_id):
                result = _run(
                    fixture.command(run_id, [sys.executable, "-c", "pass"]),
                    cwd=fixture.repo,
                )
                self.assertEqual(result.returncode, 64, (index, result.stderr))
        self.assertEqual(list(fixture.evidence.iterdir()), [])

    def test_symlink_roots_and_existing_runs_are_refused_without_overwrite(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        real = fixture.root / "real-evidence"
        real.mkdir()
        linked = fixture.root / "linked-evidence"
        linked.symlink_to(real, target_is_directory=True)
        command = fixture.command("symlink-root", [sys.executable, "-c", "pass"])
        command[command.index(str(fixture.evidence))] = str(linked)
        symlink_result = _run(command, cwd=fixture.repo)

        existing = fixture.bundle("existing-run")
        existing.mkdir()
        sentinel = existing / "sentinel"
        sentinel.write_bytes(b"do-not-overwrite")
        overwrite_result = _run(
            fixture.command("existing-run", [sys.executable, "-c", "pass"]),
            cwd=fixture.repo,
        )

        self.assertEqual(symlink_result.returncode, 64, symlink_result.stderr)
        self.assertEqual(list(real.iterdir()), [])
        self.assertEqual(overwrite_result.returncode, 64, overwrite_result.stderr)
        self.assertEqual(sentinel.read_bytes(), b"do-not-overwrite")
        self.assertEqual(sorted(path.name for path in existing.iterdir()), ["sentinel"])

    @requires_final_capture_environment
    def test_secret_environment_is_excluded_and_secret_argv_is_rejected(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        ignored = fixture.repo / "ignored.secret"
        ignored.write_text(SECRET_SENTINEL, encoding="utf-8")
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = SECRET_SENTINEL
        program = (
            "import os,sys; "
            "assert os.getenv('OPENAI_API_KEY') is None; "
            "sys.stdout.buffer.write(b'secret-absent')"
        )
        result = _run(
            fixture.command("secret-safe", [sys.executable, "-c", program]),
            cwd=fixture.repo,
            env=env,
        )
        bundle = fixture.bundle("secret-safe")
        environment = json.loads((bundle / "environment.json").read_bytes())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"secret-absent")
        self.assertEqual(environment["excluded_sensitive_variables"]["OPENAI_API_KEY"], "<redacted-present>")
        for artifact in bundle.iterdir():
            self.assertNotIn(SECRET_SENTINEL.encode(), artifact.read_bytes(), artifact.name)

        rejected = _run(
            fixture.command(
                "secret-argv",
                [sys.executable, "-c", "pass", "--token=" + SECRET_SENTINEL],
            ),
            cwd=fixture.repo,
            env=env,
        )
        self.assertEqual(rejected.returncode, 64)
        self.assertNotIn(SECRET_SENTINEL.encode(), rejected.stderr)
        self.assertFalse(fixture.bundle("secret-argv").exists())

    @requires_final_capture_environment
    def test_secret_bearing_target_output_is_rejected_without_replay(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        (fixture.repo / "ignored.secret").write_text(SECRET_SENTINEL, encoding="utf-8")
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = SECRET_SENTINEL
        program = (
            "from pathlib import Path; import sys; "
            "secret = Path('ignored.secret').read_bytes(); "
            "sys.stdout.buffer.write(secret); sys.stderr.buffer.write(secret)"
        )

        result = _run(
            fixture.command("secret-output", [sys.executable, "-c", program]),
            cwd=fixture.repo,
            env=env,
        )

        self.assertEqual(result.returncode, 125)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"collector failure: secret-bearing target output rejected\n")
        self.assertNotIn(SECRET_SENTINEL.encode(), result.stdout + result.stderr)
        self.assertFalse(fixture.bundle("secret-output").exists())
        self.assertFalse((fixture.evidence / ".secret-output.incomplete").exists())

    @requires_final_capture_environment
    def test_source_mutation_invalidates_evidence_and_names_changed_path(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        program = "from pathlib import Path; Path('tracked.txt').write_text('changed\\n')"
        result = _run(
            fixture.command("source-mutation", [sys.executable, "-c", program]),
            cwd=fixture.repo,
        )
        bundle = fixture.bundle("source-mutation")
        source = json.loads((bundle / "source.json").read_bytes())
        manifest = json.loads((bundle / "manifest.json").read_bytes())
        command = json.loads((bundle / "command.json").read_bytes())

        self.assertEqual(result.returncode, 125)
        self.assertEqual(command["target_exit_code"], 0)
        self.assertEqual(command["collector_status"], "source_mutation")
        self.assertFalse(manifest["valid"])
        self.assertFalse(source["comparison"]["checkout_unchanged"])
        self.assertIn("tracked.txt", source["comparison"]["changed_paths"])
        self.assertEqual((fixture.repo / "tracked.txt").read_text(encoding="utf-8"), "changed\n")

    @requires_final_capture_environment
    def test_declared_untracked_input_mutation_is_detected_across_all_snapshots(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        declared_input = fixture.repo / "untracked-input.txt"
        declared_input.write_bytes(b"before\n")
        program = "from pathlib import Path; Path('untracked-input.txt').write_bytes(b'after\\n')"

        result = _run(
            fixture.command(
                "untracked-mutation",
                [sys.executable, "-c", program],
                "--input",
                "untracked-input.txt",
            ),
            cwd=fixture.repo,
        )
        source = json.loads((fixture.bundle("untracked-mutation") / "source.json").read_bytes())

        self.assertEqual(result.returncode, 125)
        self.assertFalse(source["comparison"]["checkout_unchanged"])
        self.assertIn("untracked-input.txt", source["comparison"]["changed_paths"])
        self.assertIn("final", source)
        before_identity = source["before"]["declared_input_inventory"]["untracked-input.txt"]
        after_identity = source["after"]["declared_input_inventory"]["untracked-input.txt"]
        final_identity = source["final"]["declared_input_inventory"]["untracked-input.txt"]
        self.assertNotEqual(before_identity["sha256"], after_identity["sha256"])
        self.assertEqual(after_identity["sha256"], final_identity["sha256"])
        self.assertNotEqual(
            source["comparison"]["before_declared_input_inventory_sha256"],
            source["comparison"]["after_declared_input_inventory_sha256"],
        )
        self.assertEqual(
            source["comparison"]["after_declared_input_inventory_sha256"],
            source["comparison"]["final_declared_input_inventory_sha256"],
        )

    @requires_final_capture_environment
    def test_structured_finalization_integrity_attestation_is_retained(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)

        result = _run(
            fixture.command("final-attestation", [sys.executable, "-c", "pass"]),
            cwd=fixture.repo,
        )
        bundle = fixture.bundle("final-attestation")
        source_bytes = (bundle / "source.json").read_bytes()
        source = json.loads(source_bytes)
        hashes = json.loads((bundle / "hashes.json").read_bytes())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("final", source)
        self.assertIn("integrity_attestation", source)
        attestation = source["integrity_attestation"]
        self.assertRegex(attestation["before_checked_utc"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertRegex(attestation["after_checked_utc"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertRegex(attestation["final_checked_utc"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        for phase in ("before", "after", "final"):
            self.assertEqual(
                attestation[f"{phase}_snapshot_sha256"],
                _sha256(_canonical_json(source[phase])),
            )
        self.assertTrue(attestation["checkout_unchanged_through_finalization"])
        self.assertEqual(attestation["atomic_finalize_method"], "renameat2(RENAME_NOREPLACE)")
        self.assertEqual(source["before"], source["after"])
        self.assertEqual(source["after"], source["final"])
        self.assertEqual(hashes["artifacts"]["source.json"]["sha256"], _sha256(source_bytes))
        self.assertFalse((fixture.evidence / ".final-attestation.incomplete").exists())

    @requires_final_capture_environment
    def test_bundle_layout_canonical_json_hashes_and_source_identity(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        result = _run(
            fixture.command("layout", [sys.executable, "-c", "print('ok')"]),
            cwd=fixture.repo,
        )
        bundle = fixture.bundle("layout")
        entries = {path.name for path in bundle.iterdir()}
        hashes = json.loads((bundle / "hashes.json").read_bytes())
        source = json.loads((bundle / "source.json").read_bytes())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(entries, REQUIRED_BUNDLE_ENTRIES)
        self.assertFalse((fixture.evidence / ".layout.incomplete").exists())
        for name in sorted(REQUIRED_BUNDLE_ENTRIES - {"hashes.json"}):
            data = (bundle / name).read_bytes()
            self.assertEqual(hashes["artifacts"][name]["bytes"], len(data))
            self.assertEqual(hashes["artifacts"][name]["sha256"], _sha256(data))
            if name.endswith(".json"):
                self.assertEqual(data, _canonical_json(json.loads(data)))
        normalized = dict(hashes)
        normalized["self_canonical_sha256"] = ""
        self.assertEqual(hashes["self_canonical_sha256"], _sha256(_canonical_json(normalized)))
        self.assertRegex(source["before"]["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(source["before"], source["after"])
        self.assertTrue(source["comparison"]["checkout_unchanged"])
        self.assertIn("tracked.txt", source["input_hashes"])

    @requires_final_capture_environment
    def test_mutable_wsl_and_dependency_identity_are_honest_and_complete(self) -> None:
        self._require_collector()
        fixture = CollectorFixture()
        self.addCleanup(fixture.cleanup)
        result = _run(
            fixture.command("provenance", [sys.executable, "-c", "pass"]),
            cwd=fixture.repo,
        )
        environment = json.loads((fixture.bundle("provenance") / "environment.json").read_bytes())
        platform = environment["platform"]
        dependencies = environment["dependencies"]
        python = environment["python"]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(platform["identity_mode"], "mutable_wsl_substitute")
        self.assertEqual(platform["distribution"]["id"], "ubuntu")
        self.assertTrue(platform["distribution"]["version_id"])
        self.assertIn("microsoft", platform["kernel"].lower())
        self.assertEqual(platform["dpkg_inventory_sha256"], _sha256(platform["dpkg_inventory"].encode()))
        self.assertRegex(platform["apt_configuration_aggregate_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(python["version_info"][:2], [3, 12])
        self.assertRegex(python["executable_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(python["abi"])
        self.assertTrue(python["platform_tags"])
        inventory_bytes = _canonical_json(dependencies["installed_distributions"])
        self.assertEqual(dependencies["installed_inventory_sha256"], _sha256(inventory_bytes))
        self.assertEqual(dependencies["mode"], "exactly_recorded")
        self.assertIn("mutable", dependencies["limitation"].lower())

    @requires_final_capture_environment
    def test_runner_uses_external_venv_and_captures_import_smoke(self) -> None:
        self._require_collector()
        self._require_runner()
        with tempfile.TemporaryDirectory(prefix="rp020-runner-") as raw:
            root = Path(raw)
            state = root / "state"
            evidence = root / "evidence"
            target = [
                "python",
                "-c",
                "import familia,nanobot; print('import-smoke-ok')",
            ]
            result = _run(
                [
                    "bash",
                    str(RUNNER),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--state-root",
                    str(state),
                    "--evidence-root",
                    str(evidence),
                    "--python-path",
                    str(REPO_ROOT / "familia" / "src"),
                    "--python-path",
                    str(REPO_ROOT / "nanobot"),
                    "--python-path",
                    str(REPO_ROOT / "memx"),
                    "--run-id",
                    "import-smoke",
                    "--install-locked",
                    "--",
                    *target,
                ]
            )
            bundle = evidence / "import-smoke"
            command = json.loads((bundle / "command.json").read_bytes()) if bundle.exists() else {}
            source = json.loads((bundle / "source.json").read_bytes()) if bundle.exists() else {}

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"import-smoke-ok\n")
            self.assertEqual(result.stderr, b"")
            self.assertTrue((state / "import-smoke" / "venv" / "bin" / "python").is_file())
            self.assertEqual(command["argv"], target)
            self.assertTrue(source["comparison"]["checkout_unchanged"])
            self.assertEqual((bundle / "stdout.bin").read_bytes(), b"import-smoke-ok\n")

    def test_runner_rejects_checkout_and_symlink_runtime_roots_before_creation(self) -> None:
        self._require_runner()
        with tempfile.TemporaryDirectory(prefix="rp020-runner-path-") as raw:
            root = Path(raw)
            evidence = root / "evidence"
            bad_state = REPO_ROOT / ".rp020-forbidden-state"
            in_checkout = _run(
                [
                    "bash",
                    str(RUNNER),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--state-root",
                    str(bad_state),
                    "--evidence-root",
                    str(evidence),
                    "--run-id",
                    "bad-root",
                    "--no-install",
                    "--",
                    "python",
                    "-c",
                    "pass",
                ]
            )
            real_state = root / "real-state"
            real_state.mkdir()
            linked_state = root / "linked-state"
            linked_state.symlink_to(real_state, target_is_directory=True)
            symlink = _run(
                [
                    "bash",
                    str(RUNNER),
                    "--repo-root",
                    str(REPO_ROOT),
                    "--state-root",
                    str(linked_state),
                    "--evidence-root",
                    str(evidence),
                    "--run-id",
                    "bad-symlink",
                    "--no-install",
                    "--",
                    "python",
                    "-c",
                    "pass",
                ]
            )
            real_state_contents = sorted(path.name for path in real_state.iterdir())

        self.assertEqual(in_checkout.returncode, 64, in_checkout.stderr)
        self.assertFalse(bad_state.exists())
        self.assertEqual(symlink.returncode, 64, symlink.stderr)
        self.assertEqual(real_state_contents, [])

    def test_implementation_contains_no_forbidden_execution_or_host_mutation(self) -> None:
        self._require_collector()
        self._require_runner()
        combined = COLLECTOR.read_text(encoding="utf-8") + "\n" + RUNNER.read_text(encoding="utf-8")
        forbidden = [
            "shell=True",
            "os.system(",
            "eval(",
            "sudo ",
            "apt install",
            "apt update",
            "apt upgrade",
            "VirtualBox",
            "vb22",
            "git config",
            "git checkout",
            "git reset",
            "git clean",
            "git stash",
        ]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, combined)


if __name__ == "__main__":
    unittest.main()
