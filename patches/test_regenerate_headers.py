from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGENERATE = ROOT / "patches" / "regenerate.sh"


class RegenerateHeaderAcceptanceTest(unittest.TestCase):
    def test_normalizer_preserves_legal_path_segments(self) -> None:
        relative = "nanobot/lab/ab/aa/bb/actual.py"
        completed = subprocess.run(
            ["bash", str(REGENERATE)],
            cwd=ROOT,
            env={**os.environ, "NORMALIZE_HEADERS_ONLY": "1"},
            input=(
                "diff --git a/up/"
                f"{relative} b/current/{relative}\n"
                f"--- a/up/{relative}\n"
                f"+++ b/current/{relative}\n"
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                f"diff --git a/{relative} b/{relative}",
                f"--- a/{relative}",
                f"+++ b/{relative}",
            ],
        )


if __name__ == "__main__":
    unittest.main()
