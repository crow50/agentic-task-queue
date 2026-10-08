"""Phase 2 (PLAN.md): Telegram delivery.

Written before the code. Long reports and output files become attachments, so
nothing depends on a path only the machine running the queue can see. Each test
runs dispatcher.py as a subprocess in the Sandbox the baseline tests use, with
the fake claude and fake Telegram. The tests are stdlib only; the multipart
body the dispatcher uploads is decoded here by hand.
"""

import html
import re
import unittest

from test_baseline import PASS, REPO, Sandbox, fail

MESSAGE_LIMIT = 4096
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024


def long_report(chars, prefix="Findings"):
    """A report of exactly `chars` characters made of short lines (no outer whitespace)."""
    lines, size, n = [], 0, 0
    while size < chars:
        n += 1
        line = f"{prefix} {n}: the café sells 𝒏𝒆𝒕 — at 3 < 4 & 5 > 2"
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)[:chars].rstrip() + "!"


def multipart_parts(request):
    """Decode a recorded multipart/form-data request into {field: (filename, bytes)}."""
    match = re.search(r'boundary="?([^";]+)"?', request.content_type)
    assert request.content_type.startswith("multipart/form-data") and match, request.content_type
    delimiter = b"--" + match.group(1).encode()
    parts = {}
    for chunk in request.body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        head, _, content = chunk.partition(b"\r\n\r\n")
        content = content[:-2] if content.endswith(b"\r\n") else content
        headers = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', headers).group(1)
        filename = re.search(r'filename="([^"]*)"', headers)
        parts[name] = (filename.group(1) if filename else None, content)
    return parts


class DeliveryCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.workspace = self.sb.base / "workspace"
        self.workspace.mkdir()

    def snippet(self, code, *args):
        proc = self.sb.python("-c", code, *args)
        if proc.returncode != 0:
            raise AssertionError(f"snippet failed\n{proc.stderr}")
        return proc.stdout

    def messages(self):
        return self.sb.telegram.calls("sendMessage")

    def message_texts(self):
        return [html.unescape(r.params["text"]) for r in self.messages()]

    def documents(self):
        """[(filename, content bytes, form fields)] for every sendDocument upload."""
        found = []
        for request in self.sb.telegram.calls("sendDocument"):
            parts = multipart_parts(request)
            filename, content = parts.pop("document")
            found.append((filename, content, {k: v[1].decode() for k, v in parts.items()}))
        return found

    def make_file(self, relative, content=b"data\n"):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def finish(self, report, name="t", **meta):
        """Run one cycle whose worker returns `report` and whose review passes."""
        self.sb.set_scenario(worker=[{"result": report}], review=[PASS])
        self.sb.add_task(name, **meta)
        self.sb.run()


# ---------------------------------------------------------------- send_report


class LongReportTests(DeliveryCase):
    def test_a_10000_character_report_is_a_preview_plus_the_full_file(self):
        report = long_report(10_000)
        self.finish(report)

        self.assertEqual(self.sb.names("done"), ["t"])
        (message,) = self.messages()
        self.assertLessEqual(len(message.params["text"]), MESSAGE_LIMIT)
        (document,) = self.documents()
        filename, content, fields = document
        self.assertEqual(filename, "t.md")
        self.assertEqual(content.decode("utf-8"), report)  # byte for byte
        self.assertEqual(fields["chat_id"], "4242")
        self.assertEqual(self.sb.telegram.calls("sendDocument")[0].token, "test-token")

    def test_the_preview_is_the_title_and_the_start_of_the_report(self):
        report = long_report(10_000)
        self.finish(report)

        (text,) = self.message_texts()
        self.assertIn("Task done: t.md (attempt 1/3)", text)
        self.assertIn("Findings 1:", text)
        self.assertNotIn("Findings 200:", text)  # the rest is in the attachment
        self.assertIn("attached", text.lower())  # says where the rest is

    def test_no_message_mentions_the_task_file_or_a_path_on_the_host(self):
        self.finish(long_report(10_000))

        for text in self.message_texts():
            self.assertNotIn("task file", text.lower())
            self.assertNotIn(str(self.sb.base), text)
            self.assertNotIn("tasks/", text)

    def test_a_failed_task_with_long_feedback_gets_the_same_treatment(self):
        feedback = long_report(9_000, prefix="Missing")
        self.sb.set_scenario(review=[{"result": "VERDICT: FAIL\n" + feedback}])
        self.sb.add_task("t", max_attempts="1")
        self.sb.run()

        self.assertEqual(self.sb.names("failed"), ["t"])
        (text,) = self.message_texts()
        self.assertIn("Task failed: t.md", text)
        (document,) = self.documents()
        self.assertEqual(document[0], "t.md")
        self.assertEqual(document[1].decode("utf-8"), feedback)
        self.assertNotIn("task file", text.lower())

    def test_send_report_is_one_function_for_every_notice(self):
        report = long_report(7_000)
        self.snippet(
            "import dispatcher, sys\n"
            "dispatcher.send_report('❌ Task not run: dep.md', sys.argv[1], task_id='dep')\n",
            report,
        )

        (text,) = self.message_texts()
        self.assertIn("Task not run: dep.md", text)
        (document,) = self.documents()
        self.assertEqual((document[0], document[1].decode("utf-8")), ("dep.md", report))

    def test_send_report_sends_extra_files_as_documents_after_the_report(self):
        extra = self.make_file("out/data.csv", "a,b\n1,2\n")
        self.snippet(
            "import dispatcher, sys\nfrom pathlib import Path\n"
            "dispatcher.send_report('Title', 'short text', files=[Path(sys.argv[1])])\n",
            str(extra),
        )

        self.assertEqual(self.message_texts(), ["Title\n\nshort text"])
        ((filename, content, _),) = self.documents()
        self.assertEqual((filename, content), ("data.csv", b"a,b\n1,2\n"))

    def test_a_failed_upload_says_so_instead_of_dropping_the_report_silently(self):
        self.sb.telegram.queue_response("sendDocument", status=500)
        self.finish(long_report(10_000))

        self.assertEqual(self.sb.names("done"), ["t"])  # a delivery problem never fails the task
        texts = self.message_texts()
        self.assertEqual(len(texts), 2)
        self.assertIn("attach", texts[1].lower())
        self.assertNotIn("tasks/", texts[1])


class ShortReportTests(DeliveryCase):
    def test_a_2000_character_report_is_one_message_and_no_document(self):
        report = long_report(2_000)
        self.finish(report)

        (text,) = self.message_texts()
        self.assertIn(report, text)
        self.assertEqual(self.documents(), [])

    def test_a_report_just_under_the_limit_stays_one_message(self):
        report = long_report(3_300)
        self.finish(report)

        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.documents(), [])

    def test_a_report_just_over_the_limit_becomes_a_document(self):
        self.finish(long_report(3_600))

        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(len(self.documents()), 1)


# ---------------------------------------------------------------- split before converting


class HtmlSplitTests(DeliveryCase):
    TRICKY = "Result: a < b & c <d> and ```python\nx = 1 < 2"  # an unclosed code fence

    def test_html_rejected_with_400_still_arrives_as_plain_text(self):
        self.sb.telegram.queue_response("sendMessage", status=400)
        self.finish(self.TRICKY)

        first, second = self.messages()
        self.assertEqual(first.params["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", second.params)
        self.assertIn(self.TRICKY, second.params["text"])  # raw characters, unescaped
        self.assertEqual(self.documents(), [])

    def test_a_long_report_full_of_angle_brackets_still_fits_after_conversion(self):
        # "<" becomes "&lt;": cut the raw text first, or the converted message overflows.
        report = "\n".join("<&> " * 20 for _ in range(300)).strip()
        self.finish(report)

        (message,) = self.messages()
        self.assertEqual(message.params["parse_mode"], "HTML")
        self.assertLessEqual(len(message.params["text"]), MESSAGE_LIMIT)
        (document,) = self.documents()
        self.assertEqual(document[1].decode("utf-8"), report)

    def test_a_long_report_with_rejected_html_falls_back_to_a_plain_preview(self):
        self.sb.telegram.queue_response("sendMessage", status=400)
        report = long_report(10_000)
        self.finish(report)

        first, second = self.messages()
        self.assertNotIn("parse_mode", second.params)
        self.assertLessEqual(len(second.params["text"]), MESSAGE_LIMIT)
        self.assertEqual(self.documents()[0][1].decode("utf-8"), report)


# ---------------------------------------------------------------- deliver:


class DeliverTests(DeliveryCase):
    def test_deliver_sends_each_listed_file_with_its_own_name(self):
        self.make_file("reports/a.md", "# Report A\n")
        self.make_file("out/b.csv", "x,y\n1,2\n")
        self.finish("Wrote both files.", deliver="reports/a.md, out/b.csv")

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(
            [(name, content) for name, content, _ in self.documents()],
            [("a.md", b"# Report A\n"), ("b.csv", b"x,y\n1,2\n")],
        )
        self.assertIn("Task done: t.md", self.message_texts()[0])

    def test_deliver_is_relative_to_the_tasks_cwd(self):
        other = self.sb.root / "elsewhere"
        other.mkdir()
        (other / "r.md").write_text("from cwd\n")
        self.finish("done", deliver="r.md", cwd=str(other))

        self.assertEqual([(n, c) for n, c, _ in self.documents()], [("r.md", b"from cwd\n")])

    def test_a_path_outside_cwd_is_rejected_and_nothing_is_uploaded(self):
        self.finish("done", deliver="../../etc/passwd")

        self.assertEqual(self.sb.telegram.calls("sendDocument"), [])
        self.assertNotIn("t", self.sb.names("done"))  # rejected before any claude call
        self.assertEqual(self.sb.claude_calls(), [])
        self.assertIn("deliver", "\n".join(self.message_texts()))

    def test_an_absolute_path_outside_cwd_is_rejected(self):
        self.finish("done", deliver="/etc/passwd")

        self.assertEqual(self.sb.telegram.calls("sendDocument"), [])

    def test_a_symlink_that_leaves_cwd_is_not_uploaded(self):
        secret = self.sb.root / "secret.txt"
        secret.write_text("do not send\n")
        (self.workspace / "link.txt").symlink_to(secret)
        self.finish("done", deliver="link.txt")

        self.assertEqual(self.sb.names("done"), ["t"])  # the task passed; only the file is refused
        self.assertEqual(self.sb.telegram.calls("sendDocument"), [])
        self.assertTrue(
            any("link.txt" in t and "outside" in t for t in self.message_texts()), self.message_texts()
        )
        self.assertNotIn(b"do not send", b"".join(r.body for r in self.sb.telegram.requests))

    def test_a_file_over_the_telegram_limit_is_skipped_with_a_note(self):
        with open(self.workspace / "big.bin", "wb") as fh:
            fh.truncate(TELEGRAM_FILE_LIMIT + 1)  # sparse: costs no disk
        self.make_file("small.txt", "ok\n")
        self.finish("done", deliver="big.bin, small.txt")

        self.assertEqual([n for n, _, _ in self.documents()], ["small.txt"])
        notes = [t for t in self.message_texts() if "big.bin" in t]
        self.assertTrue(notes and "50 MB" in notes[0], self.message_texts())

    def test_a_listed_file_that_was_never_written_is_reported(self):
        self.finish("done", deliver="missing.md")

        self.assertEqual(self.sb.names("done"), ["t"])
        self.assertEqual(self.sb.telegram.calls("sendDocument"), [])
        self.assertTrue(any("missing.md" in t for t in self.message_texts()), self.message_texts())

    def test_nothing_is_delivered_for_a_task_that_does_not_pass(self):
        self.make_file("a.md", "x\n")
        self.sb.set_scenario(review=[fail("not yet")])
        self.sb.add_task("t", deliver="a.md", max_attempts="3")
        self.sb.run()

        self.assertEqual(self.sb.names("pending"), ["t"])
        self.assertEqual(self.sb.telegram.calls("sendDocument"), [])


# ---------------------------------------------------------------- what the worker and coordinator are told


class InstructionTests(DeliveryCase):
    def test_the_worker_is_told_the_reader_only_sees_telegram(self):
        self.finish("done")

        prompt = self.sb.claude_calls("worker")[0]["prompt"]
        self.assertIn("Telegram", prompt)
        self.assertIn("deliver:", prompt)
        self.assertIn("inline", prompt)

    def test_the_coordinator_is_taught_deliver_instead_of_a_whole_writeup_in_the_reply(self):
        text = (REPO / "coordinator" / "CLAUDE.md").read_text()

        self.assertIn("`deliver:`", text)
        self.assertRegex(text, r"(?s)saved to.*?listed in `deliver:`")
        self.assertNotRegex(text, r"(?i)final reply contains the complete writeup")

    def test_the_dispatcher_no_longer_points_at_a_file_only_the_host_can_see(self):
        source = (REPO / "dispatcher.py").read_text()

        self.assertNotIn("full report is in the task file", source)
        self.assertNotIn("truncate_for_telegram", source)


class LibraryTests(unittest.TestCase):
    def test_httpx_is_pinned_with_hashes_for_the_uploads(self):
        text = (REPO / "requirements.txt").read_text()
        pinned = {m.group(1).lower() for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", text, re.M)}

        self.assertIn("httpx", pinned)
        for name in ("httpx", "httpcore", "h11", "anyio", "idna", "certifi"):
            self.assertIn(name, pinned)
        entries = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", text[text.index("httpx=="):])
        self.assertIn("--hash=sha256:", entries[0])


if __name__ == "__main__":
    unittest.main()
