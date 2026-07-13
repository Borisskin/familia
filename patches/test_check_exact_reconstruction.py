from __future__ import annotations

import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
