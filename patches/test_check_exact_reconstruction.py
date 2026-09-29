from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from patches.check_exact_reconstruction import TreeEntry, _indexed_tree, _write_baseline_index


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "patches" / "check_exact_reconstruction.py"
UPSTREAM_REPO = Path(
    os.environ.get("FAMILIA_UPSTREAM_REPO", str(ROOT.parent / "nanobot"))
)
UPSTREAM_COMMIT = "1bb712d3488915ca4ed9ccc1a93067ff722f5ab9"


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

    def test_write_baseline_index_tracks_755_to_644(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = "scripts/install.sh"
            _write_baseline_index(
                root,
                {path: TreeEntry(mode="100755", data=b"#!/bin/sh\necho ok\n")},
            )
            self.assertEqual(_indexed_tree(root)[path].mode, "100755")
            _write_baseline_index(
                root,
                {path: TreeEntry(mode="100644", data=b"#!/bin/sh\necho ok\n")},
            )
            self.assertEqual(_indexed_tree(root)[path].mode, "100644")


if __name__ == "__main__":
    unittest.main()
