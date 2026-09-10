from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "patches" / "check_exact_reconstruction.py"
UPSTREAM_REPO = ROOT.parent / "nanobot"
UPSTREAM_COMMIT = "950dddec499fbbe0353e997158c99808f0bb41e1"


class ExactReconstructionAcceptanceTest(unittest.TestCase):
    def test_declared_vendored_scope_reconstructs_exactly(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--repo",
                str(ROOT),
                "--upstream-repo",
                str(UPSTREAM_REPO),
                "--upstream",
                UPSTREAM_COMMIT,
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("apply_valid=true", completed.stdout)
        self.assertIn("exact_equal=true", completed.stdout)
        self.assertIn("unowned_delta_count=0", completed.stdout)
        self.assertIn("direct_familia_import_count=0", completed.stdout)

    def test_global_excludes_cannot_hide_declared_baseline_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            excludes_path = temp_path / "global-excludes"
            excludes_path.write_text("AGENTS.md\n", encoding="utf-8")
            global_config = temp_path / "global.gitconfig"
            global_config.write_text(
                f"[core]\n\texcludesFile = {excludes_path.as_posix()}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["GIT_CONFIG_GLOBAL"] = str(global_config)
            for name in (
                "GIT_CONFIG_COUNT",
                "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0",
            ):
                env.pop(name, None)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(CHECKER),
                    "--repo",
                    str(ROOT),
                    "--upstream-repo",
                    str(UPSTREAM_REPO),
                    "--upstream",
                    UPSTREAM_COMMIT,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("apply_valid=true", completed.stdout)
        self.assertIn("exact_equal=true", completed.stdout)
        self.assertIn("RESULT=PASS", completed.stdout)


if __name__ == "__main__":
    unittest.main()
