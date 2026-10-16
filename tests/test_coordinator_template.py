"""The coordinator writes tasks; the dispatcher refuses a grant TASK_ALLOWED_TOOLS does not list.

The default list has bare `Write` and `Edit`, not `Write(<path>)`. A coordinator that scoped
`Write` to the one script a task creates had its task rejected before any worker ran, so the
template says what to grant.
"""

import re
import unittest

from test_baseline import Sandbox
from test_verifier import REPO


class WriteGrantTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def test_the_template_tells_the_coordinator_to_grant_bare_write_and_edit(self):
        text = " ".join((REPO / "coordinator" / "CLAUDE.md.template").read_text().split())

        self.assertRegex(text, r"bare `Write` and `Edit`")
        self.assertRegex(text, r"never `Write\(<path>\)`")

    def test_a_write_grant_scoped_to_a_path_is_rejected_and_a_bare_one_is_not(self):
        self.sb.add_task("scoped", allowed_tools="Read,Edit,Write(scripts/verify-x.sh)")
        self.sb.add_task("bare", allowed_tools="Read,Edit,Write")
        scoped = self.sb.python(str(self.sb.base / "dispatcher.py"), "check", str(self.sb.base / "tasks" / "pending" / "scoped.md"))
        bare = self.sb.python(str(self.sb.base / "dispatcher.py"), "check", str(self.sb.base / "tasks" / "pending" / "bare.md"))

        self.assertNotEqual(scoped.returncode, 0)
        self.assertRegex(scoped.stdout + scoped.stderr, re.escape("Write(scripts/verify-x.sh)"))
        self.assertEqual(bare.returncode, 0, bare.stdout + bare.stderr)


if __name__ == "__main__":
    unittest.main()
