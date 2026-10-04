"""The paths that control the queue's behavior and its safety checks need an owner.

CODEOWNERS only requests review; making it mandatory is a branch ruleset
setting on GitHub ("Require review from Code Owners"), outside the repo.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CODEOWNERS = REPO / ".github" / "CODEOWNERS"

PROTECTED = ("/dispatcher.py", "/coordinator/CLAUDE.md", "/tests/", "/.github/")


def rules():
    found = {}
    for line in CODEOWNERS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pattern, *owners = line.split()
            found[pattern] = owners
    return found


class CodeownersTests(unittest.TestCase):
    def test_protected_paths_have_an_owner(self):
        found = rules()
        for pattern in PROTECTED:
            self.assertIn(pattern, found, f"{pattern} has no CODEOWNERS rule")
            self.assertTrue(
                found[pattern] and all(o.startswith("@") for o in found[pattern]),
                f"{pattern} needs at least one @owner, got {found[pattern]}",
            )

    def test_protected_paths_exist(self):
        for pattern in PROTECTED:
            self.assertTrue((REPO / pattern.strip("/")).exists(), f"{pattern} does not exist")


if __name__ == "__main__":
    unittest.main()
