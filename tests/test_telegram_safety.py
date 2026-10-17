"""Phase 7 item 6: hostile text arrives intact on every send path.

One fixture (unbalanced backticks, `_` and `*`, `<` and `&`, `[`, a 5,000-character line, emoji above
the BMP, a pasted 20 KB JSON blob) goes through coordinator replies, reports, the live status edit and the
approval announcement, against a fake Telegram that refuses what the real Bot API refuses (a 400 for
broken HTML, empty text, or more than 4096 characters). Each path must deliver
every part of it, as formatted or plain text or as an attachment, and anything over the limit must be
an attachment.
"""

import html
import json
import re
import textwrap
import unittest

from test_baseline import PASS
from test_limits import LimitsSandbox

BLOB = json.dumps({"rows": [{"id": i, "name": f"item-{i}", "note": "x" * 40, "tags": ["a", "b", "c"]} for i in range(260)]})
LONG_LINE = "A" * 5000
EMOJI = "😀🎉👩‍💻🏳️‍🌈 "
SMALL = "\n".join([
    "MK01 unbalanced `backtick and then ``` a fence that never closes",
    "MK02 snake_case_name and *a star and **double and __under and _x and 2*3*4",
    "MK03 less < than, and & amp &amp; <b>not bold</b> <script>alert(1)</script> </i>",
    "MK04 [link text](not a url) and [a bracket and (paren",
    "MK05 " + EMOJI * 40,
])
BIG = SMALL + "\nMK06 " + LONG_LINE + "\nMK07 " + BLOB + "\n"
EMOJI_REPORT = "MK08 " + (EMOJI * 600)  # 6,000 characters of astral emoji: over the limit, so an attachment
MARKERS = {SMALL: [f"MK0{i}" for i in range(1, 6)], BIG: [f"MK0{i}" for i in range(1, 8)], EMOJI_REPORT: ["MK08"]}


class SafetyCase(unittest.TestCase):
    def setUp(self):
        self.sb = LimitsSandbox()
        self.addCleanup(self.sb.close)
        self.sb.telegram.strict = True

    # ----------------------------------------------------------- what arrived

    def channels(self):
        """Everything a person reads, one string per channel: each message as text, each document's body."""
        out = []
        for request in self.sb.telegram.requests:
            if not request.accepted:
                continue
            if request.method in ("sendMessage", "editMessageText"):
                text = request.params.get("text", "")
                if request.params.get("parse_mode") == "HTML":
                    text = html.unescape(re.sub(r"<[^>]+>", "", text))
                out.append(text)
            elif request.method == "sendDocument":
                out.append(request.body.decode("utf-8", "replace"))
        return out

    def assertIntact(self, original, markers=None):
        channels = self.channels()
        joined = "\n".join(channels)
        for marker in markers or MARKERS[original]:
            self.assertIn(marker, joined, f"{marker} never arrived")
        if "A" * 5000 in original:
            self.assertTrue(any("A" * 5000 in c for c in channels), "the 5,000 character line was cut or split")
        if BLOB in original:
            self.assertTrue(any(BLOB in c for c in channels), "the JSON blob was cut or split")
            self.assertTrue(self.sb.telegram.calls("sendDocument"), "the blob should be an attachment")

    def assertNothingLostToErrors(self):
        """A 400 followed by a good resend is fine; what is not fine is a message that never got through."""
        self.assertEqual(
            [r for r in self.sb.telegram.rejected if "too long" in r[1] and not self.sb.telegram.calls("sendDocument")], [],
            "text over the limit was sent as a message",
        )

    # ------------------------------------------------------------ send paths

    def report(self, text):
        self.sb.snippet("import dispatcher, sys; dispatcher.send_report('Report', sys.argv[1], task_id='r')", text)

    def telegram_text(self, text):
        self.sb.snippet("import dispatcher, sys; dispatcher.send_telegram(sys.argv[1])", text)

    def coordinator_reply(self, text):
        code = textwrap.dedent("""
            import sys
            import coordinator_bot as bot
            bot.COORD_DIR.mkdir(parents=True, exist_ok=True)
            bot.send_replies([sys.argv[1]], {})
            """)
        self.sb.snippet(code, text)

    def passed_task(self, text):
        self.sb.set_scenario(worker=[{"result": text}], review=[PASS])
        self.sb.add_task("t")
        self.sb.run()
        self.assertEqual(self.sb.names("done"), ["t"])


class ReportTests(SafetyCase):
    def test_a_small_hostile_report(self):
        self.report(SMALL)
        self.assertIntact(SMALL)

    def test_a_big_hostile_report_becomes_an_attachment(self):
        self.report(BIG)
        self.assertIntact(BIG)
        self.assertNothingLostToErrors()

    def test_a_long_run_of_emoji_above_the_bmp(self):
        self.report(EMOJI_REPORT)
        self.assertIntact(EMOJI_REPORT)
        self.assertNothingLostToErrors()

    def test_plain_notices_are_safe_too(self):
        self.telegram_text(SMALL)
        self.assertIntact(SMALL)

    def test_a_passed_task_with_a_hostile_report(self):
        self.passed_task(BIG)
        self.assertIntact(BIG)
        self.assertNothingLostToErrors()

    def test_a_passed_task_with_a_long_emoji_report(self):
        self.passed_task(EMOJI_REPORT)
        self.assertIntact(EMOJI_REPORT)

    def test_a_failed_task_with_hostile_feedback(self):
        feedback = SMALL + "\nMK06 " + LONG_LINE
        self.sb.set_scenario(worker=[{"result": "tried"}], review=[{"result": "VERDICT: FAIL\n" + feedback}])
        self.sb.add_task("t", max_attempts="1")
        self.sb.run()
        self.assertEqual(self.sb.names("failed"), ["t"])
        self.assertIntact(feedback, ["MK01", "MK02", "MK03", "MK04", "MK05", "MK06"])


class CoordinatorReplyTests(SafetyCase):
    def test_a_small_hostile_reply(self):
        self.coordinator_reply(SMALL)
        self.assertIntact(SMALL)

    def test_a_big_hostile_reply_becomes_an_attachment(self):
        self.coordinator_reply(BIG)
        self.assertIntact(BIG)
        self.assertNothingLostToErrors()

    def test_a_long_emoji_reply(self):
        self.coordinator_reply(EMOJI_REPORT)
        self.assertIntact(EMOJI_REPORT)

    def test_the_reply_that_replaces_a_big_one_is_recorded_for_reactions(self):
        code = textwrap.dedent("""
            import json, sys
            import coordinator_bot as bot
            state = {}
            bot.send_replies([sys.argv[1]], state)
            print(json.dumps(state))
            """)
        state = json.loads(self.sb.snippet(code, BIG).strip().splitlines()[-1])
        self.assertTrue(state.get("sent_messages"), "a reaction to the preview must still map back to the reply")


class StatusAndApprovalTests(SafetyCase):
    def test_the_live_status_line_carries_hostile_tool_text_as_plain_text(self):
        hostile = "echo '<b>&amp;' `x` _y_ *z* [w] " + EMOJI * 3
        stream = [{"tool": "Bash", "input": {"command": hostile}, "tokens": 5},
                  {"sleep": 0.1}, {"tool": "Bash", "input": {"command": hostile + " again"}, "tokens": 9}]
        self.sb.configure(STATUS_EDIT_INTERVAL_SECONDS="0")
        self.sb.set_scenario(worker=[{"result": "ok", "stream": stream}], review=[PASS])
        self.sb.add_task("t", allowed_tools="Read,Glob")
        self.sb.run()

        statuses = [r for r in self.sb.telegram.requests if r.method in ("sendMessage", "editMessageText")
                    and "Bash" in r.params.get("text", "") and "⚙️" in r.params.get("text", "")]
        self.assertTrue(statuses, "no status message arrived")
        self.assertIn(hostile[:40], statuses[0].params["text"])
        self.assertEqual(self.sb.telegram.rejected, [])

    def test_an_approval_announcement_with_a_hostile_task(self):
        body = "# Task\n\n" + SMALL + "\nMK06 " + LONG_LINE + "\n\n## Acceptance Criteria\n- " + SMALL.replace("\n", "\n- ") + "\n"
        self.sb.add_task("t", state="proposed", body=body, allowed_tools="Read,Write")
        self.sb.run()

        announcement = [c for c in self.channels() if "Approval needed" in c]
        self.assertEqual(len(announcement), 1, self.channels())
        self.assertIn("MK01", announcement[0])
        self.assertEqual(self.sb.telegram.rejected, [])


if __name__ == "__main__":
    unittest.main()
