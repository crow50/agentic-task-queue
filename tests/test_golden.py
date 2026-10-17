"""Phase 8 Part A item 6: scripts/golden.py runs the golden tasks against a route and prints a table."""

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "golden"
sys.path.insert(0, str(REPO / "scripts"))

import golden  # noqa: E402


def run(*args):
    return subprocess.run([sys.executable, str(REPO / "scripts" / "golden.py"), *args],
                          capture_output=True, text=True, timeout=300)


class GoldenTaskFileTests(unittest.TestCase):
    def test_there_are_five_tasks_each_with_a_verify_command_and_a_scripted_fake_run(self):
        tasks = golden.golden_tasks()
        self.assertEqual(len(tasks), 5)
        for task in tasks:
            meta, body = golden.split_task(task / "task.md")
            self.assertIn("verify", meta, task.name)
            self.assertIn("## Acceptance Criteria", body)
            self.assertTrue((task / "fake.json").exists() and (task / "files").is_dir(), task.name)

    def test_the_research_task_counts_results_and_wants_a_reason_for_every_exclusion(self):
        meta, body = golden.split_task(GOLDEN / "scout" / "task.md")
        self.assertEqual(meta["value_class"], "research")
        script = (GOLDEN / "scout" / "files" / "scripts" / "verify-shortlist.sh").read_text()
        self.assertIn("because", script)
        self.assertIn("candidates should be", script)

    def test_one_task_claims_success_without_doing_anything_and_is_expected_to_fail(self):
        meta, _ = golden.split_task(GOLDEN / "claims-success" / "task.md")
        self.assertEqual((meta["expect"], meta["fake_only"]), ("fail", "true"))
        self.assertNotIn("write", (GOLDEN / "claims-success" / "fake.json").read_text())


class GoldenRunTests(unittest.TestCase):
    def test_a_route_against_the_fake_claude_prints_a_table_and_exits_zero(self):
        proc = run("--route", "baseline")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for task in ("hello", "fix-bug", "word-count", "scout", "claims-success"):
            self.assertRegex(proc.stdout, rf"(?m)^{task}\s")
        self.assertIn("pass rate 5/5", proc.stdout)
        self.assertRegex(proc.stdout, r"cost per pass \d")
        self.assertRegex(proc.stdout, r"(?m)^claims-success\s+failed\s+fail\s+yes")

    def test_the_savings_route_makes_fewer_calls_and_the_table_compares_the_routes(self):
        proc = run("--route", "baseline", "--route", "savings", "--only", "hello", "--only", "fix-bug")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = [int(c) for c in re.findall(r"calls (\d+)\s+time", proc.stdout)]
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1], calls[0])
        self.assertIn("savings costs", proc.stdout)

    def test_an_unknown_route_is_refused_and_list_names_the_routes_and_tasks(self):
        self.assertEqual(run("--route", "nope").returncode, 2)
        listing = run("--list").stdout
        self.assertIn("route savings", listing)
        self.assertIn("task  scout", listing)


if __name__ == "__main__":
    unittest.main()
