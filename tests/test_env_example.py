"""Every setting the code reads must be documented in .env.example.

The scripts are parsed, not imported: importing dispatcher reads the real .env.
Settings are read through cfg("NAME"), so the names are found in the AST. The
tests also fail on a dynamic cfg(variable) call or a direct os.environ read,
either of which would hide a setting from this check.
"""

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ("dispatcher.py", "coordinator_bot.py")
ENV_EXAMPLE = REPO / ".env.example"


def parse(script):
    return ast.parse((REPO / script).read_text(), filename=script)


def is_cfg_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cfg"
    )


def settings_read(script):
    """Names passed to cfg() as string literals."""
    return {
        node.args[0].value
        for node in ast.walk(parse(script))
        if is_cfg_call(node)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def documented_settings():
    """KEY names in .env.example, whether active (KEY=) or commented out (# KEY=)."""
    text = ENV_EXAMPLE.read_text()
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))


class EnvExampleTests(unittest.TestCase):
    def test_every_setting_the_code_reads_is_documented(self):
        read = set().union(*(settings_read(script) for script in SCRIPTS))
        self.assertTrue(read, "found no settings; the scan is broken")
        missing = sorted(read - documented_settings())
        self.assertEqual(
            missing, [], f"settings read by the code but missing from .env.example: {missing}"
        )

    def test_cfg_calls_use_literal_names(self):
        dynamic = [
            f"{script}:{node.lineno}"
            for script in SCRIPTS
            for node in ast.walk(parse(script))
            if is_cfg_call(node)
            and not (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            )
        ]
        self.assertEqual(dynamic, [], f"cfg() called with a non-literal name: {dynamic}")

    def test_settings_are_only_read_through_cfg(self):
        stray = []
        for script in SCRIPTS:
            tree = parse(script)
            inside_cfg = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "cfg":
                    inside_cfg.update(id(child) for child in ast.walk(node))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                    and node.attr in ("environ", "getenv")
                    and id(node) not in inside_cfg
                ):
                    stray.append(f"{script}:{node.lineno}")
        self.assertEqual(stray, [], f"os.environ/os.getenv used outside cfg(): {stray}")


if __name__ == "__main__":
    unittest.main()
