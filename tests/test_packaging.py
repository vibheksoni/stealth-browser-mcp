import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import element_cloner, file_upload_security, paths
from stealth_browser_mcp import server

try:
    import tomllib
except ImportError:
    tomllib = None


class InstalledLayoutTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        package_dir = self.base / "site-packages" / "stealth_browser_mcp"
        package_dir.mkdir(parents=True)
        self.patches = [
            mock.patch.object(paths, "PACKAGE_DIR", package_dir),
            mock.patch.object(paths, "user_data_dir", return_value=self.base / "data"),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(paths.DATA_DIR_ENV, None)
        os.environ.pop(file_upload_security.FILE_UPLOAD_ALLOWED_DIRS_ENV, None)

    def test_installed_package_is_not_a_checkout(self):
        self.assertIsNone(paths.source_checkout_root())

    def test_clones_go_to_user_data_dir(self):
        self.assertEqual(paths.clone_output_dir().resolve(), (self.base / "data" / "element_clones").resolve())

    def test_upload_root_is_created_in_user_data_dir(self):
        roots = file_upload_security.get_allowed_upload_roots()
        self.assertEqual(roots, [(self.base / "data" / "uploads").resolve()])
        self.assertTrue(roots[0].is_dir())

    def test_data_dir_env_overrides_default(self):
        target = self.base / "custom"
        with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: str(target)}):
            self.assertEqual(paths.data_dir(), target.resolve())
            self.assertEqual(paths.clone_output_dir(), target.resolve() / "element_clones")


class CheckoutLayoutTests(unittest.TestCase):
    def test_checkout_keeps_repository_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(paths.DATA_DIR_ENV, None)
            self.assertEqual(paths.source_checkout_root(), ROOT)
            self.assertEqual(paths.clone_output_dir(), ROOT / "element_clones")
            self.assertEqual(paths.default_upload_root(), ROOT)


class PackageContentsTests(unittest.TestCase):
    def test_every_js_template_is_package_data(self):
        source = (ROOT / "src" / "stealth_browser_mcp" / "element_cloner.py").read_text(encoding="utf-8")
        templates = set(re.findall(r"'(\w+\.js)'", source))
        self.assertTrue(templates)
        for name in templates:
            self.assertTrue((element_cloner.JS_DIR / name).is_file(), name)

    def test_main_accepts_argv(self):
        with mock.patch("sys.stdout"), self.assertRaises(SystemExit) as raised:
            server.main(["--list-sections"])
        self.assertEqual(raised.exception.code, 0)

    def test_readme_links_work_on_pypi(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        targets = re.findall(r'(?:src|href)="([^"]+)"', readme) + re.findall(r"\]\(([^)\s]+)\)", readme)
        relative = [target for target in targets if not target.startswith(("http://", "https://", "mailto:", "#"))]
        self.assertEqual(relative, [], "PyPI renders the README without the repo, so links must be absolute")

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_console_script_points_at_main(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["scripts"]["stealth-browser-mcp"], "stealth_browser_mcp.server:main")
        self.assertEqual(project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"], ["src/stealth_browser_mcp"])
        for requirement in project["project"]["dependencies"]:
            self.assertNotIn("@", requirement, "PyPI rejects direct URL dependencies")


if __name__ == "__main__":
    unittest.main()
