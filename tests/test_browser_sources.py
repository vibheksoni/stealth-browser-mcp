import json
import os
import posixpath
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import browser_sources


def sep(path):
    """Normalise a path to forward slashes for host-agnostic comparison.

    ``os.path.join`` on the Linux CI host uses forward slashes, so a Windows
    root built with backslash separators is normalised here before comparing.
    """
    return os.path.normpath(path).replace("\\", "/")


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="ascii")


class RootBuilderTests(unittest.TestCase):
    def test_linux_roots_per_family(self):
        env = {"HOME": "/tmp/h"}
        self.assertEqual(
            browser_sources.user_data_root("chrome", "linux", env),
            posixpath.join("/tmp/h", ".config", "google-chrome"),
        )
        self.assertEqual(
            browser_sources.user_data_root("edge", "linux", env),
            posixpath.join("/tmp/h", ".config", "microsoft-edge"),
        )
        self.assertEqual(
            browser_sources.user_data_root("brave", "linux", env),
            posixpath.join("/tmp/h", ".config", "BraveSoftware", "Brave-Browser"),
        )

    def test_windows_roots_per_family(self):
        env = {"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"}
        self.assertEqual(
            sep(browser_sources.user_data_root("chrome", "windows", env)),
            "C:/Users/me/AppData/Local/Google/Chrome/User Data",
        )
        self.assertEqual(
            sep(browser_sources.user_data_root("edge", "windows", env)),
            "C:/Users/me/AppData/Local/Microsoft/Edge/User Data",
        )
        self.assertEqual(
            sep(browser_sources.user_data_root("brave", "windows", env)),
            "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data",
        )

    def test_macos_roots_per_family(self):
        env = {"HOME": "/Users/me"}
        self.assertEqual(
            browser_sources.user_data_root("chrome", "darwin", env),
            posixpath.join("/Users/me", "Library", "Application Support", "Google", "Chrome"),
        )
        self.assertEqual(
            browser_sources.user_data_root("brave", "darwin", env),
            posixpath.join("/Users/me", "Library", "Application Support", "BraveSoftware", "Brave-Browser"),
        )

    def test_env_overrides_home_and_localappdata(self):
        self.assertEqual(
            browser_sources.user_data_root("chrome", "linux", {"HOME": "/alt"}),
            posixpath.join("/alt", ".config", "google-chrome"),
        )
        # Windows uses LOCALAPPDATA, not HOME.
        self.assertEqual(
            sep(browser_sources.user_data_root("chrome", "windows", {"LOCALAPPDATA": "C:\\x"})),
            "C:/x/Google/Chrome/User Data",
        )

    def test_env_is_mandatory_and_not_read_from_real_env(self):
        # A real HOME set in the process must not leak into the fake build: an
        # empty mapping has no HOME, so the builder must not fall back to it.
        env = {}
        with self.assertRaises(KeyError):
            browser_sources.user_data_root("chrome", "linux", env)
        # And when we do pass HOME, we get exactly what we asked for.
        self.assertEqual(
            browser_sources.user_data_root("chrome", "linux", {"HOME": "/fake/home"}),
            posixpath.join("/fake/home", ".config", "google-chrome"),
        )

    def test_deterministic_platform_separators(self):
        # Linux/darwin paths always use forward slashes; Windows paths always use backslashes.
        self.assertEqual(
            browser_sources.user_data_root("chrome", "linux", {"HOME": "/base"}),
            "/base/.config/google-chrome",
        )
        self.assertEqual(
            browser_sources.user_data_root("chrome", "windows", {"LOCALAPPDATA": "C:\\base"}),
            "C:\\base\\Google\\Chrome\\User Data",
        )
        self.assertEqual(
            browser_sources.source_path("chrome", "Profile 1", "linux", {"HOME": "/base"}),
            "/base/.config/google-chrome/Profile 1",
        )
        self.assertEqual(
            browser_sources.source_path("chrome", "Profile 1", "windows", {"LOCALAPPDATA": "C:\\base"}),
            "C:\\base\\Google\\Chrome\\User Data\\Profile 1",
        )

    def test_unknown_family_and_platform_raise(self):
        with self.assertRaises(ValueError):
            browser_sources.user_data_root("firefox", "linux", {"HOME": "/x"})
        with self.assertRaises(ValueError):
            browser_sources.user_data_root("chrome", "solaris", {"HOME": "/x"})
        with self.assertRaises(ValueError):
            browser_sources.user_data_root("brave", "plan9", {})


class LocalStatePathTests(unittest.TestCase):
    def test_local_state_path_matches_root(self):
        env = {"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"}
        path = browser_sources.local_state_path("brave", "windows", env)
        self.assertEqual(
            sep(path),
            "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data/" + browser_sources.LOCAL_STATE_FILE,
        )


class SourcePathTests(unittest.TestCase):
    def test_profile_subdir_joined_under_root(self):
        env = {"HOME": "/tmp/h"}
        self.assertEqual(
            browser_sources.source_path("chrome", "Profile 1", "linux", env),
            posixpath.join("/tmp/h", ".config", "google-chrome", "Profile 1"),
        )

    def test_profile_subdir_with_backslash_is_normalised(self):
        env = {"LOCALAPPDATA": "C:\\x"}
        # Chrome stores subdir names with forward slashes; a backslash form is
        # accepted and normalised so listing and lookup agree.
        self.assertEqual(
            sep(browser_sources.source_path("chrome", "Default\\Extra", "windows", env)),
            "C:/x/Google/Chrome/User Data/Default/Extra",
        )


class NormalizePlatformTests(unittest.TestCase):
    def test_accepts_system_and_sys_platform_spellings(self):
        for good in ("linux", "Linux", "LINUX", "linux2", "win32", "Windows", "WIN32",
                     "darwin", "Darwin", "macos", "macOSX"):
            self.assertEqual(
                browser_sources.normalize_platform(good),
                {"linux": "linux", "Linux": "linux", "LINUX": "linux", "linux2": "linux",
                 "win32": "windows", "Windows": "windows", "WIN32": "windows",
                 "darwin": "darwin", "Darwin": "darwin", "macos": "darwin",
                 "macOSX": "darwin"}[good],
                good,
            )

    def test_rejects_unknown(self):
        with self.assertRaises(ValueError):
            browser_sources.normalize_platform("solaris")


class ParseLocalStateTests(unittest.TestCase):
    def _sample(self):
        return {
            "profile": {
                "info_cache": [
                    {"name": "Default", "email": ""},
                    {"name": "Profile 1", "email": "a@b.com"},
                ],
                "last_used": 1,
            }
        }

    def test_parses_profile_names_in_order(self):
        text = json.dumps(self._sample(), ensure_ascii=True)
        self.assertEqual(
            browser_sources.parse_profiles_from_local_state(text),
            ["Default", "Profile 1"],
        )
        self.assertEqual(browser_sources.last_used_profile(text), "Profile 1")

    def test_empty_or_missing_text(self):
        self.assertEqual(browser_sources.parse_profiles_from_local_state(""), [])
        self.assertEqual(browser_sources.parse_profiles_from_local_state(None), [])
        self.assertIsNone(browser_sources.last_used_profile(""))
        self.assertIsNone(browser_sources.last_used_profile(None))

    def test_malformed_json_returns_empty(self):
        for bad in ("{ not json", "", "[1,2,3]", "null", "garbage }"):
            self.assertEqual(browser_sources.parse_profiles_from_local_state(bad), [], bad)
            self.assertIsNone(browser_sources.last_used_profile(bad))

    def test_missing_profile_key(self):
        self.assertEqual(browser_sources.parse_profiles_from_local_state("{}"), [])
        self.assertEqual(
            browser_sources.parse_profiles_from_local_state(json.dumps({"x": 1})), []
        )

    def test_info_cache_not_a_list(self):
        data = {"profile": {"info_cache": {"name": "Default"}}}
        self.assertEqual(browser_sources.parse_profiles_from_local_state(json.dumps(data)), [])

    def test_info_cache_entries_without_name_are_skipped(self):
        data = {"profile": {"info_cache": [{"foo": 1}, None, {"name": "Profile 2"}]}}
        self.assertEqual(
            browser_sources.parse_profiles_from_local_state(json.dumps(data)),
            ["Profile 2"],
        )


class FakeTreeTests(unittest.TestCase):
    """End-to-end build/parse over a synthetic tree, one case per OS x family."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def _make_profile_state(self, names, last_used=0):
        return {
            "profile": {
                "info_cache": [{"name": name} for name in names],
                "last_used": last_used,
            }
        }

    def _build_tree(self, platform, family, names):
        if platform == "windows":
            env = {"LOCALAPPDATA": str(self.base)}
        elif platform == "linux":
            env = {"HOME": str(self.base)}
        else:
            env = {"HOME": str(self.base)}
        root = Path(browser_sources.user_data_root(family, platform, env).replace("\\", os.sep))
        # Local State at the root.
        write_json(root / browser_sources.LOCAL_STATE_FILE, self._make_profile_state(names))
        # One profile directory per name.
        for name in names:
            src = Path(browser_sources.source_path(family, name, platform, env).replace("\\", os.sep))
            src.mkdir(parents=True, exist_ok=True)
        return env, root

    def test_windows_chrome_tree(self):
        env, root = self._build_tree("windows", "chrome", ["Default", "Profile 1"])
        self.assertEqual(
            sep(str(root)),
            sep(str(Path(env["LOCALAPPDATA"]) / "Google\\Chrome\\User Data")),
        )
        self.assertTrue((root / browser_sources.LOCAL_STATE_FILE).is_file())
        text = (root / browser_sources.LOCAL_STATE_FILE).read_text(encoding="ascii")
        self.assertEqual(browser_sources.parse_profiles_from_local_state(text), ["Default", "Profile 1"])
        self.assertTrue(Path(browser_sources.source_path("chrome", "Profile 1", "windows", env).replace("\\", os.sep)).exists())

    def test_linux_edge_tree(self):
        env, root = self._build_tree("linux", "edge", ["Default"])
        self.assertEqual(root, Path(env["HOME"]) / ".config" / "microsoft-edge")
        self.assertTrue(root.is_dir())
        # last_used=0 resolves to the first info_cache entry, "Default".
        self.assertEqual(
            browser_sources.last_used_profile(
                (root / browser_sources.LOCAL_STATE_FILE).read_text(encoding="ascii")
            ),
            "Default",
        )

    def test_linux_brave_tree(self):
        env, root = self._build_tree("linux", "brave", ["Default", "Profile 3"])
        self.assertEqual(
            root,
            Path(env["HOME"]) / ".config" / "BraveSoftware" / "Brave-Browser",
        )
        self.assertTrue((root / "Profile 3").is_dir())
        text = (root / browser_sources.LOCAL_STATE_FILE).read_text(encoding="ascii")
        # Inject a malformed Local State on top of the tree; the tree still
        # exists but the parse returns empty rather than raising.
        (root / browser_sources.LOCAL_STATE_FILE).write_text("{ broken json", encoding="ascii")
        self.assertEqual(browser_sources.parse_profiles_from_local_state(text), ["Default", "Profile 3"])
        self.assertEqual(browser_sources.parse_profiles_from_local_state(
            (root / browser_sources.LOCAL_STATE_FILE).read_text(encoding="ascii")), [])

    def test_windows_brave_tree(self):
        env, root = self._build_tree("windows", "brave", ["Default"])
        self.assertEqual(
            sep(str(root)),
            sep(str(Path(env["LOCALAPPDATA"]) / "BraveSoftware\\Brave-Browser\\User Data")),
        )
        self.assertTrue((root / "Default").is_dir())

    def test_source_path_points_into_existing_profile_dir(self):
        self._build_tree("linux", "chrome", ["Profile 2"])
        tree_env = {"HOME": str(self.base)}
        src = browser_sources.source_path("chrome", "Profile 2", "linux", tree_env)
        self.assertTrue(Path(src).exists())
        self.assertEqual(
            src,
            browser_sources.source_path("chrome", "Profile 2", "linux", tree_env),
        )


class BinaryFamilyMappingTests(unittest.TestCase):
    def test_families_list(self):
        self.assertEqual(browser_sources.BROWSER_FAMILIES, ("chrome", "edge", "brave"))

    def test_each_family_has_candidates(self):
        for family in browser_sources.BROWSER_FAMILIES:
            self.assertTrue(browser_sources.binary_candidates(family))

    def test_brave_is_present(self):
        self.assertIn("brave-browser", browser_sources.binary_candidates("brave"))
        self.assertIn("brave", browser_sources.binary_candidates("brave"))

    def test_chrome_and_edge_candidates(self):
        self.assertIn("google-chrome", browser_sources.binary_candidates("chrome"))
        self.assertIn("msedge", browser_sources.binary_candidates("edge"))

    def test_unknown_family_raises(self):
        with self.assertRaises(ValueError):
            browser_sources.binary_candidates("firefox")


if __name__ == "__main__":
    unittest.main()
