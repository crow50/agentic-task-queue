"""Phase 8 Part A item 5: models.json, its fallback chains, and what happens when a model is not supported."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from taskq_lib import models  # noqa: E402
from test_baseline import PASS  # noqa: E402
from test_limits import LimitsCase  # noqa: E402

TABLE = {
    "capabilities": {"big": "code", "small": "code", "talker": "chat"},
    "needs": {"worker": "code", "review": "code", "coordinator": "code"},
    "roles": {
        "worker": {"default": ["big", "small"], "admin": ["small", "big"]},
        "review": {"default": ["small", "big"]},
        "coordinator": {"default": ["talker", "big"]},
    },
}
UNSUPPORTED = {"exit": 1, "stdout": "", "stderr": "There's an issue with the selected model (fake-worker). "
                                                   "It may not exist or you may not have access to it."}


class TableTests(unittest.TestCase):
    def test_the_shipped_table_is_valid_and_so_is_the_built_in_one(self):
        shipped = json.loads((REPO / "models.json").read_text())
        self.assertEqual(models.problems(shipped), [])
        self.assertEqual(models.problems(models.DEFAULT_TABLE), [])

    def test_problems_name_what_is_wrong(self):
        self.assertTrue(models.problems([]))
        bad = {**TABLE, "roles": {"worker": {"default": ["ghost"]}}}
        self.assertIn('"ghost" has no entry', models.problems(bad)[0])
        self.assertTrue(models.problems({**TABLE, "roles": {"cook": {"default": ["big"]}}}))

    def test_load_falls_back_to_the_built_in_table_only_when_there_is_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(models.load(Path(tmp) / "models.json"), models.DEFAULT_TABLE)
            broken = Path(tmp) / "models.json"
            broken.write_text("{nope")
            with self.assertRaises(ValueError):
                models.load(broken)

    def test_a_chat_model_is_never_chosen_for_a_role_that_needs_code(self):
        self.assertEqual(models.chain(TABLE, "coordinator"), ["big"])
        self.assertEqual(models.resolve(TABLE, "coordinator", None, "talker"), "big")

    def test_value_class_picks_the_chain_and_default_covers_the_rest(self):
        self.assertEqual(models.chain(TABLE, "worker", "admin"), ["small", "big"])
        self.assertEqual(models.chain(TABLE, "worker", "research"), ["big", "small"])

    def test_an_unusable_model_hands_over_to_the_next_in_the_chain(self):
        self.assertEqual(models.resolve(TABLE, "worker", None, "big", unusable={"big"}), "small")
        self.assertEqual(models.resolve(TABLE, "worker", None, "big", unusable={"big", "small"}), None)
        self.assertEqual(models.resolve(TABLE, "worker", None, "auto"), "big")
        self.assertEqual(models.resolve(TABLE, "worker", "admin", "auto"), "small")

    def test_a_model_the_table_does_not_know_is_used_as_asked(self):
        self.assertEqual(models.resolve(TABLE, "worker", None, "brand-new"), "brand-new")


class UnsupportedModelTests(LimitsCase):
    def setUp(self):
        super().setUp()
        (self.sb.base / "models.json").write_text(json.dumps({
            "capabilities": {"fake-worker": "code", "fake-escalated": "code", "fake-review": "code"},
            "needs": {},
            "roles": {"worker": {"default": ["fake-worker", "fake-escalated"]},
                      "review": {"default": ["fake-review"]},
                      "coordinator": {"default": ["fake-worker", "fake-escalated"]}}}))

    def test_an_unsupported_model_uses_no_attempt_says_so_once_and_the_next_run_uses_the_next_model(self):
        self.sb.set_scenario(worker=[UNSUPPORTED, {"result": "Done."}], review=[PASS])
        self.sb.add_task("t")
        self.sb.run()
        meta, _ = self.sb.read_task("pending", "t")
        self.assertEqual(meta["attempts"], "0")
        (notice,) = [t for t in self.sb.texts() if "isn't available" in t]
        self.assertIn("fake-worker", notice)
        self.assertIn("fake-escalated", notice)
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual([c["model"] for c in self.sb.claude_calls() if c["role"] == "worker"], ["fake-worker", "fake-escalated"])
        self.assertEqual(len([t for t in self.sb.texts() if "isn't available" in t]), 1)

    def test_the_status_of_the_failed_call_is_model(self):
        self.sb.set_scenario(worker=[UNSUPPORTED])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual([u["status"] for u in self.sb.usage()], ["model"])

    def test_model_auto_takes_the_first_model_of_the_roles_chain(self):
        self.sb.add_task("t", model="auto", review_model="auto")
        self.sb.run()
        self.assertEqual([c["model"] for c in self.sb.claude_calls()], ["fake-worker", "fake-review"])

    def test_with_every_model_gone_the_task_waits_and_the_queue_pauses_with_a_reason(self):
        (self.sb.base / "models.json").write_text(json.dumps({
            "capabilities": {"fake-worker": "code"}, "needs": {},
            "roles": {"worker": {"default": ["fake-worker"]}, "review": {"default": ["fake-worker"]}}}))
        self.sb.set_scenario(worker=[UNSUPPORTED])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertIn("no usable model", (self.sb.base / "state" / "paused_until").read_text())


if __name__ == "__main__":
    unittest.main()


class CoordinatorFallbackTests(unittest.TestCase):
    def test_the_coordinator_moves_to_the_next_model_when_one_is_refused(self):
        from test_limits_coordinator import USER, BotCase, message

        class Case(BotCase):
            def runTest(self):
                self.sb.configure(TELEGRAM_USER_ID=str(USER), COORDINATOR_MODEL="fake-worker")
                (self.sb.base / "models.json").write_text(json.dumps({
                    "capabilities": {"fake-worker": "code", "fake-escalated": "code"}, "needs": {},
                    "roles": {"coordinator": {"default": ["fake-worker", "fake-escalated"]}}}))
                self.sb.set_scenario(other=[UNSUPPORTED, {"result": "Hello."}])
                self.handle(message("hi", sender=USER))
                self.assertEqual([c["model"] for c in self.others()], ["fake-worker", "fake-escalated"])
                self.assertIn("Hello.", self.sent())

        result = unittest.TextTestRunner(stream=open("/dev/null", "w")).run(Case())
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
