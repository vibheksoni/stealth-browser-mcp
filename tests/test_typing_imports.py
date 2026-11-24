"""Guard: names from typing used in src/tests must be imported.

Python 3.14 evaluates annotations lazily, so a missing ``from typing import Any``
passes on 3.14 and crashes at import time on 3.10 to 3.13 (found on Windows CI).
"""
import ast
import pathlib
import unittest

NAMES = {"Any", "Dict", "List", "Optional", "Tuple", "Set", "Callable", "Union", "Iterable", "Mapping", "Sequence", "Awaitable", "Type"}
ROOT = pathlib.Path(__file__).resolve().parent.parent


def missing_typing_names(source):
    tree = ast.parse(source)
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            bound |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.Import):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted((used & NAMES) - bound)


class TypingImportTests(unittest.TestCase):
    def test_detector_finds_missing_name(self):
        self.assertEqual(missing_typing_names("def f(x: Any) -> None:\n    pass\n"), ["Any"])
        self.assertEqual(missing_typing_names("from typing import Any\ndef f(x: Any): pass\n"), [])

    def test_src_and_tests_import_their_typing_names(self):
        problems = []
        for base in ("src/stealth_browser_mcp", "tests", "scripts"):
            for path in sorted((ROOT / base).rglob("*.py")):
                missing = missing_typing_names(path.read_text(encoding="utf-8"))
                if missing:
                    problems.append("{}: {}".format(path.relative_to(ROOT), ", ".join(missing)))
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
