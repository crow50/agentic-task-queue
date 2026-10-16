"""`git push` of a task branch must be able to use the queue's GH_TOKEN.

GH_TOKEN reaches `gh` through the environment, but `git push` over https asks git's
credential helpers, and the queue user has none configured. The dispatcher therefore
adds one, for github.com only and only when GH_TOKEN is set: `gh auth git-credential`
(the configured GH_BIN), which answers with that token. Real git, fake gh.
"""

import unittest

from test_worktree_pr import PrCase

CREDENTIAL = """
import subprocess, sys
import dispatcher
proc = subprocess.run(
    ["git", "credential", "fill"], input=f"protocol=https\\nhost={sys.argv[1]}\\n\\n",
    capture_output=True, text=True, env=dispatcher.publish_env(), cwd=sys.argv[2],
)
print(proc.returncode)
print(proc.stdout)
"""

HELPER_ANSWER = {"match": "git-credential", "stdout": "username=x-access-token\npassword=ghp_fine_grained\n"}


class CredentialTests(PrCase):
    def fill(self, host="github.com"):
        proc = self.sb.python("-c", CREDENTIAL, host, str(self.sb.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code, _, out = proc.stdout.partition("\n")
        return int(code), out

    def helper_calls(self):
        return [c["argv"] for c in self.sb.gh_calls() if "git-credential" in c["argv"]]

    def test_with_a_token_git_gets_its_password_from_gh_for_github(self):
        self.sb.configure(GH_TOKEN="ghp_fine_grained")
        self.sb.gh_rules([HELPER_ANSWER])
        code, out = self.fill()

        self.assertEqual(code, 0, out)
        self.assertIn("password=ghp_fine_grained", out)
        self.assertEqual(self.helper_calls(), [["auth", "git-credential", "get"]])

    def test_the_helper_is_asked_for_github_and_nothing_else(self):
        self.sb.configure(GH_TOKEN="ghp_fine_grained")
        self.sb.gh_rules([HELPER_ANSWER])
        code, _ = self.fill(host="example.com")

        self.assertNotEqual(code, 0)  # nobody answers, and git may not prompt
        self.assertEqual(self.helper_calls(), [])

    def test_without_a_token_no_helper_is_added(self):
        self.sb.configure(GH_TOKEN=None)
        self.sb.gh_rules([HELPER_ANSWER])
        code, _ = self.fill()

        self.assertNotEqual(code, 0)
        self.assertEqual(self.helper_calls(), [])

    def test_the_helper_is_registered_under_the_github_https_key_only(self):
        self.sb.configure(GH_TOKEN="ghp_fine_grained")
        code = """
import dispatcher
env = dispatcher.publish_env()
count = int(env["GIT_CONFIG_COUNT"])
pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
print(sorted(pairs))
"""
        out = self.sb.python("-c", code).stdout
        self.assertIn("credential.https://github.com.helper", out)

    def test_the_claude_login_still_stays_out_of_git_and_gh(self):
        self.sb.configure(GH_TOKEN="ghp_fine_grained", CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-secret")
        out = self.sb.python("-c", "import dispatcher; print(sorted(dispatcher.publish_env()))").stdout

        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", out)
        self.assertIn("GH_TOKEN", out)


if __name__ == "__main__":
    unittest.main()
