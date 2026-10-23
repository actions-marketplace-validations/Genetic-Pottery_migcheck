import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VersionTest(unittest.TestCase):
    def test_prints_the_version_from_pyproject(self):
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            version = re.search(r'^version = "(.+)"$', f.read(), re.M).group(1)
        # The GitHub Action runs migcheck from a checkout, without installing it.
        out = subprocess.run(
            [sys.executable, "-m", "migcheck", "--version"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": ROOT}, capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(out, f"migcheck {version}\n")


if __name__ == "__main__":
    unittest.main()
