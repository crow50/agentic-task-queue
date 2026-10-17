"""A secret pasted with words around it ("Here is the token 51c0...") must be caught, not only `token=...`.

Found when a random `openssl rand -hex 24` was pasted as a fake token: no pattern matched it, so it reached the model and
logs/coordinator.log in clear.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dispatcher  # noqa: E402

HEX = "51c060a0971562172688f32821fc635fab8c9654f0000c33"
B64 = "q7Zk2LwP9xT4mN8vB3cR6yH1jD5fG0aS"


class SpokenTokenTests(unittest.TestCase):
    def test_a_token_introduced_in_words_is_caught(self):
        for text in (f"Here is the token {HEX}", f"my api key is {HEX}", f"password: {HEX}", f"the secret was {B64}",
                     f"token {HEX}", f"TOKEN IS {HEX}"):
            self.assertTrue(dispatcher.secret_kinds(text), text)
            self.assertNotIn(HEX, dispatcher.redact_secrets(text))
            self.assertNotIn(B64, dispatcher.redact_secrets(text))

    def test_the_words_stay_and_only_the_value_goes(self):
        self.assertEqual(dispatcher.redact_secrets(f"Here is the token {HEX} ok"),
                         "Here is the token [redacted password or token] ok")

    def test_ordinary_text_with_long_ids_is_left_alone(self):
        for text in (
            "commit 51c060a0971562172688f32821fc635fab8c9654 fixed it",
            "retry readme-mention-draft-pr-intro-20261016-120000 please",
            "the token budget is 1000000 and the token limit is high",
            "a secret garden of thirty-two-letters-long-words-here",
            "token refresh",
        ):
            self.assertEqual(dispatcher.secret_kinds(text), [], text)


if __name__ == "__main__":
    unittest.main()
