"""Phase 8 Part B item 6: naming the assistant. The name is plain text from a chat, so it is checked, and the patch that
records it touches the two tracked files and nothing else."""

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from taskq_lib import naming  # noqa: E402

IDENTITY = (REPO / "coordinator" / "identity.md").read_text()
ENV_EXAMPLE = (REPO / ".env.example").read_text()


class CleanTests(unittest.TestCase):
    def test_plain_names_pass_and_odd_ones_do_not(self):
        self.assertEqual(naming.clean(" Juno "), "Juno")
        self.assertEqual(naming.clean("**Mira Vale**"), "Mira Vale")
        for bad in ("", "1st", "a" * 25, "x\ny", "<b>hi</b>", "rm -rf /;", None, "ünder"):
            self.assertIsNone(naming.clean(bad), bad)


class PatchTests(unittest.TestCase):
    def test_the_patch_touches_only_identity_and_the_env_example_default(self):
        patch = naming.name_patch(IDENTITY, ENV_EXAMPLE, "Juno")
        self.assertEqual(re.findall(r"^\+\+\+ b/(\S+)$", patch, re.M), ["coordinator/identity.md", ".env.example"])
        added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
        self.assertEqual(sorted(added), ["ASSISTANT_NAME=Juno", "My name is **Juno**."])

    def test_the_same_name_makes_an_empty_patch(self):
        self.assertEqual(naming.name_patch(IDENTITY, ENV_EXAMPLE, "taskq"), "")

    def test_a_bad_name_or_a_file_without_the_line_is_refused(self):
        with self.assertRaises(ValueError):
            naming.name_patch(IDENTITY, ENV_EXAMPLE, "no way!")
        with self.assertRaises(ValueError):
            naming.name_patch("nothing here\n", ENV_EXAMPLE, "Juno")
        with self.assertRaises(ValueError):
            naming.name_patch(IDENTITY, "X=1\n", "Juno")

    def test_shipped_files_carry_the_default_the_patch_edits(self):
        self.assertIn("My name is **taskq**.", IDENTITY)
        self.assertRegex(ENV_EXAMPLE, r"(?m)^ASSISTANT_NAME=taskq$")


class ProposalTests(unittest.TestCase):
    def test_three_names_with_reasons_are_read_from_loose_text(self):
        text = "Here you go:\n1. Juno | calm and short\n- Mira | easy to say aloud\n* Pip | small and quick\nNote: pick any."
        self.assertEqual(naming.parse_proposals(text), [("Juno", "calm and short"), ("Mira", "easy to say aloud"), ("Pip", "small and quick")])

    def test_duplicates_bad_names_and_extra_lines_are_dropped(self):
        text = "Juno | a\njuno | again\n42 | number\nMira | b\nPip | c\nZed | d"
        self.assertEqual([n for n, _ in naming.parse_proposals(text)], ["Juno", "Mira", "Pip"])


if __name__ == "__main__":
    unittest.main()
