"""Phase 4 hardening in the dispatcher and the coordinator bridge.

Login token injection, atomic writes that survive a failed rename, transcript
pruning, the outside heartbeat, and the allowed_tools allowlist.
"""

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from test_baseline import DEFAULT_BODY, Sandbox

AUTH_EXPIRED = {"fixture": "auth-expired.txt", "exit": 1}


class HardeningSandbox(Sandbox):
    def snippet(self, code, *args):
        proc = self.python("-c", code, *args)
        if proc.returncode != 0:
            raise AssertionError(f"snippet failed\n{proc.stderr}")
        return proc.stdout


class HardeningCase(unittest.TestCase):
    def setUp(self):
        self.sb = HardeningSandbox()
        self.addCleanup(self.sb.close)


class TokenInjectionTests(HardeningCase):
    def test_every_claude_call_gets_the_login_and_github_tokens_from_dot_env(self):
        self.sb.configure(CLAUDE_CODE_OAUTH_TOKEN="oat-test", GH_TOKEN="gh-test")
        self.sb.add_task("t")
        self.sb.run()

        calls = self.sb.claude_calls()
        self.assertEqual([c["role"] for c in calls], ["worker", "review"])
        for call in calls:
            self.assertEqual(call["oauth_token"], "oat-test")
            self.assertEqual(call["gh_token"], "gh-test")

    def test_without_the_settings_claude_is_given_neither(self):
        self.sb.add_task("t")
        self.sb.run()

        for call in self.sb.claude_calls():
            self.assertIsNone(call["oauth_token"])
            self.assertIsNone(call["gh_token"])


class AtomicWriteTests(HardeningCase):
    WRITE_TASK = """
import pathlib
from unittest import mock
import dispatcher
path = pathlib.Path("t.md")
path.write_text("original")
with mock.patch("os.replace", side_effect=OSError("power cut")):
    try:
        dispatcher.write_task(path, {"model": "m"}, "body")
    except OSError:
        print("raised")
print(path.read_text())
print(sorted(p.name for p in pathlib.Path(".").glob(".*.tmp")))
"""
    SAVE_STATE = """
from unittest import mock
import coordinator_bot as bot
bot.save_state({"offset": 1, "session_id": None})
with mock.patch("os.replace", side_effect=OSError("power cut")):
    try:
        bot.save_state({"offset": 2, "session_id": None})
    except OSError:
        print("raised")
print(bot.load_state()["offset"])
"""

    def test_a_failed_rename_leaves_the_task_file_untouched_and_no_temp_file(self):
        out = self.sb.snippet(self.WRITE_TASK).splitlines()

        self.assertEqual(out, ["raised", "original", "[]"])

    def test_a_failed_rename_leaves_the_coordinator_state_untouched(self):
        self.assertEqual(self.sb.snippet(self.SAVE_STATE).splitlines(), ["raised", "1"])

    def test_write_task_round_trips(self):
        out = self.sb.snippet(
            "import pathlib, dispatcher\n"
            "p = pathlib.Path('t.md')\n"
            "dispatcher.write_task(p, {'model': 'm', 'attempts': '2'}, 'body\\n')\n"
            "print(dispatcher.parse_task(p))"
        )
        self.assertIn("'attempts': '2'", out)

    def test_recover_stale_moves_an_interrupted_task_back_to_pending(self):
        self.sb.add_task("half-done", state="active")
        (self.sb.base / "tasks" / "pending").mkdir(parents=True, exist_ok=True)
        self.sb.snippet("import dispatcher; dispatcher.recover_stale()")

        self.assertEqual(self.sb.names("pending"), ["half-done"])
        self.assertEqual(self.sb.names("active"), [])


class PruneLogsTests(HardeningCase):
    def age(self, path, days):
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))

    def test_old_transcripts_go_and_recent_ones_and_the_usage_log_stay(self):
        logs = self.sb.base / "logs"
        logs.mkdir()
        files = {
            "old.attempt-1.log": 40, "old.attempt-1.verify.log": 40, "old.attempt-2.worker.txt": 40,
            "recent.attempt-1.log": 5, "usage.jsonl": 90, "dispatcher.log": 90,
        }
        for name, days in files.items():
            (logs / name).write_text("x")
            self.age(logs / name, days)
        self.sb.run()

        kept = {p.name for p in logs.iterdir()}
        self.assertTrue({"recent.attempt-1.log", "usage.jsonl"} <= kept)
        self.assertFalse({n for n in kept if n.startswith("old.")}, kept)
        self.assertTrue((logs / "dispatcher.log").exists())

    def test_retention_zero_keeps_everything(self):
        self.sb.configure(LOG_RETENTION_DAYS="0")
        logs = self.sb.base / "logs"
        logs.mkdir()
        (logs / "old.attempt-1.log").write_text("x")
        self.age(logs / "old.attempt-1.log", 400)
        self.sb.run()

        self.assertTrue((logs / "old.attempt-1.log").exists())


class PingServer:
    def __init__(self):
        self.hits = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/ping/secret"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HeartbeatTests(HardeningCase):
    def setUp(self):
        super().setUp()
        self.ping = PingServer()
        self.addCleanup(self.ping.close)
        self.sb.configure(HEALTHCHECK_URL=self.ping.url)

    def test_a_successful_cycle_pings_once(self):
        self.sb.run()

        self.assertEqual(self.ping.hits, ["/ping/secret"])

    def test_a_cycle_that_ran_a_task_pings_too(self):
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(len(self.ping.hits), 1)

    def test_an_expired_login_sends_no_ping_so_the_outside_alert_fires(self):
        self.sb.set_scenario(worker=[AUTH_EXPIRED], other=[AUTH_EXPIRED])
        self.sb.add_task("t")
        self.sb.run()

        self.assertEqual(self.ping.hits, [])

    def test_no_url_means_no_ping_and_a_dead_url_does_not_fail_the_run(self):
        self.sb.configure(HEALTHCHECK_URL="http://127.0.0.1:9/never")
        proc = self.sb.run()

        self.assertNotIn("never", proc.stdout)  # only the error type is logged
        self.assertIn("healthcheck ping failed", proc.stdout)


class AllowedToolsTests(HardeningCase):
    CHECK = """
import json, sys
import dispatcher
print(json.dumps([dispatcher.allowed_tools_problem(v) for v in json.loads(sys.argv[1])]))
"""

    def problems(self, *values):
        return json.loads(self.sb.snippet(self.CHECK, json.dumps(values)))

    def test_ordinary_grants_pass_including_the_example_tasks(self):
        ok = [
            "Read,Glob,Grep,Edit,Write",
            "Read,Glob,Grep,Edit,Write,Bash(python3 *)",
            "Read,Write,mcp__fetch__fetch",
            "Read,Glob,Grep,Write,Bash(df *),Bash(du -sh*),Bash(uptime),Bash(free *)",
            "Read,Glob,Grep,Edit,Write,Skill(changelog-entry)",
            "Bash(gh issue create*),Bash(gh pr create*)",
            "Bash(gh issue *)",
        ]
        self.assertEqual(self.problems(*ok), [None] * len(ok))

    def test_a_task_cannot_hand_itself_the_whole_github_api(self):
        for bad in ("Bash(gh *)", "Bash(gh api *)", "Bash(gh api repos/x/y)", "Bash(gh pr merge*)", "Bash"):
            (problem,) = self.problems(f"Read,{bad}")
            self.assertIn(bad, problem)

    def test_commas_inside_parentheses_do_not_split_an_entry(self):
        self.assertEqual(self.problems("Bash(python3 -c 'a, b')"), [None])

    def test_the_allowlist_is_configurable(self):
        self.sb.configure(TASK_ALLOWED_TOOLS="Read,Bash(gh *)")
        self.assertEqual(self.problems("Read,Bash(gh api x)"), [None])
        (problem,) = self.problems("Write")
        self.assertIn("Write", problem)

    def test_the_check_command_and_the_dispatcher_reject_a_task_with_a_bad_grant(self):
        self.sb.add_task("t", allowed_tools="Read,Bash(gh api *)")
        path = self.sb.base / "tasks" / "pending" / "t.md"
        proc = self.sb.python(str(self.sb.base / "dispatcher.py"), "check", str(path))

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("TASK_ALLOWED_TOOLS", proc.stdout)
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertEqual(self.sb.claude_calls(), [])

    def test_a_task_without_allowed_tools_is_unaffected(self):
        self.sb.add_task("t", body=DEFAULT_BODY)
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])


if __name__ == "__main__":
    unittest.main()
