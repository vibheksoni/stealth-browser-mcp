import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stealth_browser_mcp import element_kind


def facts(**overrides):
    """Build a probe-facts mapping with sane defaults, then apply overrides."""
    base = {
        "tag_name": "input",
        "type": "",
        "input_type": "",
        "contenteditable": "",
        "is_content_editable": False,
        "is_contenteditable_element": False,
        "aria_role": "",
        "read_only": False,
        "disabled": False,
        "required": False,
        "multiple": False,
        "class_list": [],
        "class_names": "",
        "has_shadow_root": False,
        "iframe": None,
        "hints": [],
    }
    base.update(overrides)
    return base


class ClassifyTableTests(unittest.TestCase):
    def test_input_types(self):
        cases = {
            ("input", "text", ""): element_kind.ElementKind.TEXT,
            ("input", "", "text"): element_kind.ElementKind.TEXT,
            ("input", "password", "password"): element_kind.ElementKind.PASSWORD,
            ("input", "email", "email"): element_kind.ElementKind.EMAIL,
            ("input", "number", "number"): element_kind.ElementKind.NUMBER,
            ("input", "date", "date"): element_kind.ElementKind.DATE,
            ("input", "time", "time"): element_kind.ElementKind.TIME,
            ("input", "datetime", "datetime"): element_kind.ElementKind.DATETIME,
            ("input", "datetime-local", "datetime"): element_kind.ElementKind.DATETIME,
            ("input", "month", "month"): element_kind.ElementKind.MONTH,
            ("input", "week", "week"): element_kind.ElementKind.WEEK,
            ("input", "color", "color"): element_kind.ElementKind.COLOR,
            ("input", "range", "range"): element_kind.ElementKind.RANGE,
            ("input", "file", "file"): element_kind.ElementKind.FILE,
            ("input", "checkbox", "checkbox"): element_kind.ElementKind.CHECKBOX,
            ("input", "radio", "radio"): element_kind.ElementKind.RADIO,
            ("input", "submit", "button"): element_kind.ElementKind.BUTTON,
            ("input", "reset", "button"): element_kind.ElementKind.BUTTON,
            ("button", "", "button"): element_kind.ElementKind.BUTTON,
        }
        for (tag, attr_type, input_type), kind in cases.items():
            with self.subTest(tag=tag, attr_type=attr_type, input_type=input_type):
                self.assertEqual(element_kind.classify(facts(tag_name=tag, type=attr_type, input_type=input_type)), kind)

    def test_textarea_is_textarea(self):
        self.assertEqual(element_kind.classify(facts(tag_name="textarea")), element_kind.ElementKind.TEXTAREA)

    def test_select_is_select(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="select", input_type="")),
            element_kind.ElementKind.SELECT,
        )

    def test_contenteditable_true_enables_editing(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="div", is_content_editable=True)),
            element_kind.ElementKind.CONTENTEDITABLE,
        )

    def test_contenteditable_empty_string_enables_editing(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="div", contenteditable="")),
            element_kind.ElementKind.CONTENTEDITABLE,
        )

    def test_contenteditable_false_is_not_editable(self):
        facts_map = facts(tag_name="div", contenteditable="false", is_content_editable=False)
        self.assertEqual(element_kind.classify(facts_map), element_kind.ElementKind.UNKNOWN)

    def test_contenteditable_false_with_no_fallback_is_unknown(self):
        facts_map = facts(tag_name="span", is_content_editable=False)
        self.assertEqual(element_kind.classify(facts_map), element_kind.ElementKind.UNKNOWN)

    def test_prosemirror_is_rich_editor(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="div", is_content_editable=True, hints=["ProseMirror"])),
            element_kind.ElementKind.RICH_EDITOR,
        )

    def test_ql_editor_is_rich_editor(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="div", is_content_editable=True, hints=["ql-editor"])),
            element_kind.ElementKind.RICH_EDITOR,
        )

    def test_codemirror_is_rich_editor(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="div", is_content_editable=True, hints=["CodeMirror"])),
            element_kind.ElementKind.RICH_EDITOR,
        )

    def test_role_combobox_is_combobox(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="input", aria_role="combobox")),
            element_kind.ElementKind.COMBOBOX,
        )

    def test_role_combobox_but_select_still_select(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="select", aria_role="combobox")),
            element_kind.ElementKind.SELECT,
        )

    def test_react_select_class_on_contenteditable_is_combobox(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="div", is_content_editable=True, hints=["react-select"])),
            element_kind.ElementKind.COMBOBOX,
        )

    def test_react_select_on_select_is_combobox(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="select", hints=["react-select"])),
            element_kind.ElementKind.COMBOBOX,
        )

    def test_unknown_input_type_is_unknown(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="input", type="search", input_type="search")),
            element_kind.ElementKind.UNKNOWN,
        )

    def test_type_attribute_used_when_input_type_empty(self):
        self.assertEqual(
            element_kind.classify(facts(tag_name="input", type="email", input_type="")),
            element_kind.ElementKind.EMAIL,
        )

    def test_non_dict_input_treated_as_empty(self):
        self.assertEqual(element_kind.classify(None), element_kind.ElementKind.UNKNOWN)
        self.assertEqual(element_kind.classify("garbage"), element_kind.ElementKind.UNKNOWN)


class ProbeSourceTests(unittest.TestCase):
    def test_probe_file_exists_and_nonempty(self):
        probe_path = element_kind.JS_DIR / element_kind.PROBE_JS
        self.assertTrue(probe_path.is_file(), "probe JS must ship in the package")
        self.assertGreater(len(probe_path.read_text(encoding="utf-8").strip()), 0, "probe JS must be non-empty")

    def test_probe_declared_as_package_data(self):
        # The wheel targets the whole package, so a file inside it ships as
        # package data; confirm the file resolves from importlib.resources and
        # the file path is inside the package tree.
        probe_path = Path(element_kind.JS_DIR / element_kind.PROBE_JS)
        package_dir = Path(element_kind.__file__).resolve().parent
        self.assertTrue(probe_path.is_file())
        self.assertTrue(probe_path.resolve().is_relative_to(package_dir))
        resolved = str(element_kind.resources.files(element_kind.__package__))
        self.assertIn(package_dir.as_posix(), resolved.replace(os.sep, "/"),
                      "probe is read from the package via importlib.resources")

    def test_render_probe_returns_source_with_placeholder(self):
        # render_probe reads from importlib.resources, not the on-disk source,
        # so it must work the same way an installed package would. The source
        # must still carry the element placeholder for substitution.
        source = element_kind.render_probe()
        self.assertIn("$ELEMENT$", source, "probe must carry the element placeholder")
        self.assertEqual(source, element_kind.render_probe())


class ProbeRenderSubstitutionTests(unittest.TestCase):
    def test_placeholder_replaced_with_json_literal(self):
        source = element_kind.render_probe()
        literals = {"ELEMENT": element_kind._json_for("handle-1")}
        out = element_kind.PLACEHOLDER_PATTERN.sub(lambda m: literals[m.group(1)], source)
        self.assertNotIn("$ELEMENT$", out)
        self.assertIn('handle-1', out)


if __name__ == "__main__":
    unittest.main()
