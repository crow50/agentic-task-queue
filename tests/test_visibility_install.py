"""Phase 8: what the installer does about the new settings, and `install.py --check` probing every model in models.json."""

import json
import unittest

from test_install import InstallCase

UNSUPPORTED = {"exit": 1, "stdout": "", "stderr": "There's an issue with the selected model (m-gone). It may not exist."}


class VisibilityDefaultsTests(InstallCase):
    def test_a_fresh_install_gets_the_visibility_defaults(self):
        self.install_ok()
        env = self.env()
        self.assertEqual((env["CONTEXT_WARN_PCT"], env["CONTEXT_ROTATE_PCT"], env["COORDINATOR_MEMORY_MAX_KB"]), ("70", "85", "64"))
        self.assertEqual((env["DAILY_COST_BUDGET_USD"], env["DAILY_TOKEN_BUDGET"]), ("0", "0"))  # budgets are your numbers

    def test_an_existing_install_without_them_gets_them_once_and_an_explicit_value_stays(self):
        self.install_ok()
        text = "".join(line + "\n" for line in (self.base / ".env").read_text().splitlines()
                       if not line.startswith(("CONTEXT_", "COORDINATOR_MEMORY")))
        (self.base / ".env").write_text(text + "CONTEXT_ROTATE_PCT=0\n")
        self.install_ok()
        env = self.env()
        self.assertEqual((env["CONTEXT_WARN_PCT"], env["CONTEXT_ROTATE_PCT"]), ("70", "0"))


class ModelProbeTests(InstallCase):
    def setUp(self):
        super().setUp()
        self.install_ok()
        self.telegram.requests.clear()
        (self.base / "models.json").write_text(json.dumps({
            "capabilities": {"m-ok": "code", "m-gone": "code"}, "needs": {},
            "roles": {"worker": {"default": ["m-ok", "m-gone"]}}}))

    def check(self, steps):
        scenario = self.calls.parent / "scenario.json"
        scenario.write_text(json.dumps({"other": steps}))
        self.calls.unlink(missing_ok=True)
        return self.run_main("--check", defaults=False)

    def test_a_model_the_account_cannot_use_is_named_with_what_happens_next(self):
        code, out, _ = self.check([{"result": "ok"}, {"result": "ok"}, UNSUPPORTED])
        self.assertEqual([c["model"] for c in self.claude_calls()][1:], ["m-ok", "m-gone"])
        self.assertRegex(out, r"WARN +models.json: models this account can use.*cannot use m-gone")
        self.assertEqual(code, 0)  # a warning, not a failure: the dispatcher falls back by itself

    def test_when_every_model_answers_it_says_so(self):
        _, out, _ = self.check([{"result": "ok"}])
        self.assertRegex(out, r"PASS +models.json: models this account can use.*all 2 answered")

    def test_no_models_file_means_no_extra_calls(self):
        (self.base / "models.json").unlink()
        self.check([{"result": "ok"}])
        self.assertEqual(len(self.claude_calls()), 1)  # just the login probe


if __name__ == "__main__":
    unittest.main()
