"""The CI workflow must stay safe to run on any pull request.

Static checks on .github/workflows: it runs on pull requests, uses no secrets,
pins every action to a full commit SHA, installs its tools from pinned and
hashed or checksummed sources, and still runs the four checks it exists for.
The files are read as text so the tests stay stdlib-only.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO / ".github" / "workflows"
CI_REQUIREMENTS = REPO / ".github" / "ci-requirements.txt"

PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def workflows():
    found = sorted(WORKFLOW_DIR.glob("*.y*ml"))
    return {path.name: path.read_text() for path in found}


def without_comments(text):
    """Workflow text minus YAML comments, so a comment can't satisfy a 'must contain' check."""
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


class CiWorkflowTests(unittest.TestCase):
    def test_a_workflow_runs_on_pull_requests(self):
        files = workflows()
        self.assertTrue(files, "no workflow files in .github/workflows")
        triggers = []
        for name, text in files.items():
            match = re.search(r"^on:\s*\n((?:[ \t]+.*\n|\n)*)", text, re.M)
            if match and "pull_request" in match.group(1):
                triggers.append(name)
        self.assertTrue(triggers, "no workflow is triggered by pull_request")

    def test_pull_request_target_is_never_used(self):
        # It runs with a write token against fork code; nothing here needs it.
        for name, text in workflows().items():
            self.assertNotIn("pull_request_target", text, name)

    def test_every_action_is_pinned_to_a_full_commit_sha(self):
        checked = 0
        for name, text in workflows().items():
            for ref in re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.M):
                if ref.startswith("./"):
                    continue
                checked += 1
                self.assertRegex(ref, PINNED_ACTION, f"{name}: {ref} is not pinned to a SHA")
        self.assertGreater(checked, 0, "found no 'uses:' lines; the scan is broken")

    def test_workflows_use_no_secrets(self):
        for name, text in workflows().items():
            self.assertIsNone(
                re.search(r"\$\{\{[^}]*\bsecrets\b", text), f"{name} references secrets"
            )

    def test_permissions_are_declared_and_read_only(self):
        for name, text in workflows().items():
            self.assertRegex(text, r"(?m)^permissions:", f"{name} has no top-level permissions")
            self.assertIsNone(re.search(r":\s*write\b", text), f"{name} grants write access")

    def test_it_still_runs_the_four_checks(self):
        text = without_comments("\n".join(workflows().values()))
        for needle in (
            "py_compile dispatcher.py coordinator_bot.py",
            "unittest discover -s tests",
            "ruff check",
            "gitleaks git",
        ):
            self.assertIn(needle, text)

    def test_ci_tools_come_from_pinned_hashed_or_checksummed_sources(self):
        text = without_comments("\n".join(workflows().values()))
        self.assertIn("--require-hashes", text, "pip installs must verify hashes")
        self.assertIn("sha256sum -c", text, "downloaded binaries must be checksum-verified")
        requirements = CI_REQUIREMENTS.read_text()
        pins = re.findall(r"^[A-Za-z0-9_.-]+==[\w.]+", requirements, re.M)
        self.assertTrue(pins, "ci-requirements.txt pins nothing")
        self.assertGreaterEqual(requirements.count("--hash=sha256:"), len(pins))


if __name__ == "__main__":
    unittest.main()
