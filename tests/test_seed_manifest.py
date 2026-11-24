import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import seed_manifest as sm


def sep(path):
    """Normalise a path to forward slashes for host-agnostic comparison."""
    return os.path.normpath(path).replace("\\", "/")


def write_file(path: Path, size: int):
    """Create a file at ``path`` filled with ``size`` bytes of zeros."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(b"\x00" * size)


def find(entries, rel):
    """Find the entry whose relative path is ``rel`` (forward slashes)."""
    for entry in entries:
        if entry.rel_path == rel:
            return entry
    raise AssertionError("no entry for {}".format(rel))


def find_nested(entries, rel):
    """Find a nested (sub-origin) entry whose full relative path is ``rel``."""
    for entry in entries:
        for child in (entry.nested or []):
            if child.rel_path == rel:
                return child
    return None


class TreeBuilder(unittest.TestCase):
    """Build a synthetic profile tree with known sizes for each entry."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def _tree(self, sizes=None, extra_dirs=None, indexeddb=None):
        """Create the standard profile tree.

        ``sizes`` maps a top-level name to a file size. ``extra_dirs`` lists
        top-level names that should be directories without files. ``indexeddb``
        is a mapping of origin -> bytes for IndexedDB subdirectories.
        """
        sizes = sizes or {}
        for name, size in sizes.items():
            write_file(self.root / name, size)
        for name in extra_dirs or []:
            (self.root / name).mkdir()
        if indexeddb:
            for origin, size in indexeddb.items():
                db_dir = os.path.join(str(self.root), "IndexedDB", origin)
                os.makedirs(db_dir, exist_ok=True)
                with open(os.path.join(db_dir, "data.db"), "wb") as handle:
                    handle.write(b"\x00" * size)
        return self.root


class IncludeRuleTests(TreeBuilder):
    def test_session_folders_are_included(self):
        tree = self._tree(sizes={
            "Cookies": 100,
            "Cookies-journal": 20,
            "Preferences": 30,
            "Secure Preferences": 40,
            "Local Storage": 5,
            "Session Storage": 7,
        })
        plan = sm.build_plan(str(tree))
        for rel in ("Cookies", "Cookies-journal", "Preferences",
                    "Secure Preferences", "Local Storage", "Session Storage"):
            entry = find(plan.entries, rel)
            self.assertEqual(entry.action, sm.INCLUDE, rel)
            self.assertTrue(entry.reason, rel)

    def test_network_cookies_directory_is_included(self):
        tree = self._tree()
        write_file(self.root / "Network" / "Cookies", 256)
        plan = sm.build_plan(str(tree))
        entry = find(plan.entries, "Network")
        self.assertEqual(entry.action, sm.INCLUDE)
        # The plan folds sub-file sizes into the parent entry.
        self.assertEqual(entry.size, 256)

    def test_extensions_folders_are_included(self):
        tree = self._tree(sizes={
            "Extensions": 10,
            "Extension State": 11,
            "Local Extension Settings": 12,
            "Sync Extension Settings": 13,
        })
        plan = sm.build_plan(str(tree))
        for rel in ("Extensions", "Extension State", "Local Extension Settings",
                    "Sync Extension Settings"):
            self.assertEqual(find(plan.entries, rel).action, sm.INCLUDE, rel)

    def test_indexeddb_is_included(self):
        tree = self._tree(indexeddb={"https___a_example_com_0": 1000,
                                     "https___b_example_com_0": 2000})
        plan = sm.build_plan(str(tree))
        entry = find(plan.entries, "IndexedDB")
        self.assertEqual(entry.action, sm.INCLUDE)
        self.assertEqual(entry.size, 3000)


class ExcludeRuleTests(TreeBuilder):
    def test_caches_are_excluded(self):
        tree = self._tree(sizes={
            "Cache": 1000,
            "Code Cache": 200,
            "GPUCache": 300,
            "DawnCache": 400,
            "GrShaderCache": 500,
            "ShaderCache": 600,
            "Media Cache": 700,
        })
        plan = sm.build_plan(str(tree))
        for rel in ("Cache", "Code Cache", "GPUCache", "DawnCache",
                    "GrShaderCache", "ShaderCache", "Media Cache"):
            entry = find(plan.entries, rel)
            self.assertEqual(entry.action, sm.EXCLUDE, rel)
            self.assertEqual(entry.reason, "cache", rel)
            self.assertEqual(entry.size, {"Cache": 1000, "Code Cache": 200,
                    "GPUCache": 300, "DawnCache": 400, "GrShaderCache": 500,
                    "ShaderCache": 600, "Media Cache": 700}[rel], rel)

    def test_service_worker_cache_is_excluded(self):
        tree = self._tree()
        write_file(self.root / "Service Worker" / "CacheStorage" / "x", 99)
        plan = sm.build_plan(str(tree))
        entry = find(plan.entries, "Service Worker")
        self.assertEqual(entry.action, sm.EXCLUDE)
        self.assertEqual(entry.reason, "cache")

    def test_model_and_infra_dirs_are_excluded(self):
        tree = self._tree(sizes={
            "Crashpad": 10,
            "component_crx_cache": 20,
            "blob_storage": 30,
            "OptGuideOnDeviceModel": 40,
            "optimization_guide": 50,
            "BrowserMetrics": 60,
        })
        plan = sm.build_plan(str(tree))
        self.assertEqual(find(plan.entries, "Crashpad").reason, "crash dumps")
        self.assertEqual(find(plan.entries, "component_crx_cache").reason,
                         "extension cache")
        self.assertEqual(find(plan.entries, "blob_storage").reason, "blob storage")
        self.assertEqual(find(plan.entries, "OptGuideOnDeviceModel").reason,
                         "on-device models")
        self.assertEqual(find(plan.entries, "optimization_guide").reason,
                         "optimization cache")
        self.assertEqual(find(plan.entries, "BrowserMetrics").reason, "metrics")

    def test_unknown_entries_are_not_in_manifest(self):
        tree = self._tree(sizes={"History": 500, "Bookmarks": 30, "Favicons": 10})
        plan = sm.build_plan(str(tree))
        for rel in ("History", "Bookmarks", "Favicons"):
            entry = find(plan.entries, rel)
            self.assertEqual(entry.action, sm.EXCLUDE, rel)
            self.assertEqual(entry.reason, "not_in_manifest", rel)

    def test_exact_exclude_beats_include(self):
        # "Cache" must not be pulled in by any include rule; it is a cache.
        tree = self._tree(sizes={"Cache": 1234})
        plan = sm.build_plan(str(tree))
        entry = find(plan.entries, "Cache")
        self.assertEqual(entry.action, sm.EXCLUDE)
        self.assertEqual(entry.size, 1234)


class OptInTests(TreeBuilder):
    def test_passwords_excluded_by_default(self):
        tree = self._tree(sizes={
            "Login Data": 800,
            "Login Data-Journal": 50,
        })
        plan = sm.build_plan(str(tree))
        self.assertEqual(find(plan.entries, "Login Data").action, sm.EXCLUDE)
        self.assertEqual(find(plan.entries, "Login Data").reason, "passwords")
        # Journal/shm files are grouped with the password store's reason too.
        self.assertEqual(find(plan.entries, "Login Data-Journal").reason,
                         "passwords")

    def test_cards_excluded_by_default(self):
        tree = self._tree(sizes={
            "Web Data": 900,
            "Web Data-Journal": 60,
        })
        plan = sm.build_plan(str(tree))
        self.assertEqual(find(plan.entries, "Web Data").action, sm.EXCLUDE)
        self.assertEqual(find(plan.entries, "Web Data").reason, "payment data")
        self.assertEqual(find(plan.entries, "Web Data-Journal").reason,
                         "payment data")

    def test_passwords_included_when_opted_in(self):
        tree = self._tree(sizes={"Login Data": 800})
        options = sm.SeedOptions(include_passwords=True)
        plan = sm.build_plan(str(tree), options)
        entry = find(plan.entries, "Login Data")
        self.assertEqual(entry.action, sm.INCLUDE)
        self.assertEqual(entry.reason, "login credentials")
        self.assertEqual(entry.size, 800)

    def test_cards_included_when_opted_in(self):
        tree = self._tree(sizes={"Web Data": 900})
        options = sm.SeedOptions(include_cards=True)
        plan = sm.build_plan(str(tree), options)
        entry = find(plan.entries, "Web Data")
        self.assertEqual(entry.action, sm.INCLUDE)
        self.assertEqual(entry.reason, "payment data")

    def test_optins_are_independent(self):
        tree = self._tree(sizes={"Login Data": 100, "Web Data": 200})
        options = sm.SeedOptions(include_passwords=True, include_cards=False)
        plan = sm.build_plan(str(tree), options)
        self.assertEqual(find(plan.entries, "Login Data").action, sm.INCLUDE)
        self.assertEqual(find(plan.entries, "Web Data").action, sm.EXCLUDE)


class IndexedDbCapTests(TreeBuilder):
    def test_heavy_origin_is_skipped(self):
        # Default cap is 50 MB; an origin at 80 MB is skipped, the rest kept.
        tree = self._tree(indexeddb={
            "https___small_example_com_0": 1024,
            "https___big_example_com_0": 80 * 1024 * 1024,
        })
        plan = sm.build_plan(str(tree))
        entry = find(plan.entries, "IndexedDB")
        # The container is kept; the oversized origin is dropped as a nested
        # excluded entry. Only the kept origin counts toward the plan total.
        self.assertEqual(entry.action, sm.INCLUDE)
        big = find_nested(plan.entries, "IndexedDB/https___big_example_com_0")
        self.assertIsNotNone(big, "oversized origin should be a nested entry")
        if big is not None:
            self.assertEqual(big.action, sm.EXCLUDE)
        self.assertIn("big_example_com", entry.reason)
        # The container reports the kept total; the skipped origin's size is not
        # counted toward the plan.
        self.assertEqual(entry.size, 1024)
        self.assertEqual(plan.total_size, 1024)

    def test_cap_at_boundary_is_included(self):
        tree = self._tree(indexeddb={"https___ok_example_com_0": sm.DEFAULT_INDEXEDDB_CAP_BYTES})
        plan = sm.build_plan(str(tree))
        entry = find(plan.entries, "IndexedDB")
        self.assertEqual(entry.action, sm.INCLUDE)

    def test_custom_cap_rejects_lighter_origin(self):
        tree = self._tree(indexeddb={"https___x_example_com_0": 60 * 1024 * 1024})
        options = sm.SeedOptions(indexeddb_cap_bytes=51 * 1024 * 1024)
        plan = sm.build_plan(str(tree), options)
        entry = find(plan.entries, "IndexedDB")
        # A single origin above the cap is dropped as a nested excluded entry;
        # the container stays included with an empty kept total.
        self.assertEqual(entry.action, sm.INCLUDE)
        self.assertIn("indexeddb_cap", entry.reason)
        nested = {e.rel_path: e for e in (entry.nested or [])}
        self.assertEqual(
            nested["IndexedDB/https___x_example_com_0"].action, sm.EXCLUDE)
        self.assertEqual(entry.size, 0)
        self.assertEqual(plan.total_size, 0)

    def test_default_cap_value_is_50_mb(self):
        self.assertEqual(sm.DEFAULT_INDEXEDDB_CAP_BYTES, 50 * 1024 * 1024)


class TotalsAndPartitionTests(TreeBuilder):
    def test_included_excluded_partitions(self):
        tree = self._tree(sizes={
            "Cookies": 100,
            "Preferences": 50,
            "Cache": 9999,
            "History": 8888,
        })
        plan = sm.build_plan(str(tree))
        included_rel = {e.rel_path for e in plan.included}
        excluded_rel = {e.rel_path for e in plan.excluded}
        self.assertEqual(included_rel, {"Cookies", "Preferences"})
        self.assertEqual(excluded_rel, {"Cache", "History"})
        # Every entry is in exactly one partition.
        self.assertEqual(len(plan.entries),
                         len(plan.included) + len(plan.excluded))

    def test_total_size_counts_only_included(self):
        tree = self._tree(sizes={
            "Cookies": 100,
            "Preferences": 50,
            "Cache": 9999,
        })
        plan = sm.build_plan(str(tree))
        self.assertEqual(plan.total_size, 150)

    def test_rel_paths_use_forward_slashes(self):
        tree = self._tree(sizes={"Network": 10})
        plan = sm.build_plan(str(tree))
        for entry in plan.entries:
            self.assertNotIn(os.sep, entry.rel_path)


class EdgeCaseTests(TreeBuilder):
    def test_empty_directory_plans_nothing(self):
        plan = sm.build_plan(str(self._tree()))
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.total_size, 0)

    def test_missing_directory_raises(self):
        with self.assertRaises(NotADirectoryError):
            sm.build_plan(str(self.root / "does-not-exist"))

    def test_default_options_match_explicit_off(self):
        tree = self._tree(sizes={"Login Data": 100, "Web Data": 200})
        default_plan = sm.build_plan(str(tree))
        explicit_plan = sm.build_plan(
            str(tree), sm.SeedOptions(include_passwords=False, include_cards=False))
        self.assertEqual(
            [(e.rel_path, e.action, e.reason) for e in default_plan.entries],
            [(e.rel_path, e.action, e.reason) for e in explicit_plan.entries],
        )


if __name__ == "__main__":
    unittest.main()
