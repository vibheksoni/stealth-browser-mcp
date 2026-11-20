"""Agent guidance, connect instructions, and input recipes playbook (SB-68).

Provides:
- INPUT_RECIPES data table for standard and rich web form interaction patterns.
- build_connect_instructions(): compact instructions returned during client connect.
- build_input_playbook(): detailed markdown guide with interaction patterns.
- how_to_input_prompt(): prompt content generator for field-specific input advice.

ASCII only. No stdout prints in server paths.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

INPUT_RECIPES: List[Dict[str, Any]] = [
    {
        "id": "text_fill",
        "field_type": "text",
        "when": "Standard single-line text input fields where fast, reliable value setting is desired.",
        "tool": "fill",
        "args_example": {
            "selector": "#title",
            "value": "Recipe Task Title",
            "strategy": "auto",
        },
        "verify": "Read back with get_element_state(selector='#title') or evaluate window.__state().",
        "pitfalls": [
            "SPAs may silently drop input on fast re-renders; always verify the read-back value."
        ],
        "runnable": True,
        "state_key": "title",
        "expected_value": "Recipe Task Title",
    },
    {
        "id": "text_type",
        "field_type": "text",
        "when": "Input fields with keyup/keydown listeners, live search, or autocomplete triggers.",
        "tool": "type_text",
        "args_example": {
            "selector": "#title",
            "text": "Recipe Typed Title",
            "clear_first": True,
        },
        "verify": "Read back with get_element_state(selector='#title') or evaluate window.__state().",
        "pitfalls": [
            "Types keystroke by keystroke; slower for large strings than fill or paste_text."
        ],
        "runnable": True,
        "state_key": "title",
        "expected_value": "Recipe Typed Title",
    },
    {
        "id": "textarea_fill",
        "field_type": "textarea",
        "when": "Multi-line plain textareas where exact line breaks must be preserved.",
        "tool": "fill",
        "args_example": {
            "selector": "#desc-plain",
            "value": "Recipe Line 1\n\nRecipe Line 3",
            "strategy": "auto",
        },
        "verify": "Read back with get_element_state(selector='#desc-plain') or evaluate window.__state().",
        "pitfalls": [
            "Ensure newline characters (\\n) are used; verify exact preservation in read-back."
        ],
        "runnable": True,
        "state_key": "descPlain",
        "expected_value": "Recipe Line 1\n\nRecipe Line 3",
    },
    {
        "id": "rich_text_paste",
        "field_type": "rich_text",
        "when": "Contenteditable rich text editors where long multi-line text should insert in one paste event.",
        "tool": "paste_text",
        "args_example": {
            "selector": "#desc-rich",
            "text": "Paragraph 1\nParagraph 2",
            "clear_first": True,
        },
        "verify": "Inspect DOM child paragraph elements or evaluate window.__state().",
        "pitfalls": [
            "Rich text editors may require an initial click if the editor is not yet focused."
        ],
        "runnable": True,
        "state_key": "rich",
        "expected_value": ["Paragraph 1", "Paragraph 2"],
    },
    {
        "id": "rich_text_type",
        "field_type": "rich_text",
        "when": "Contenteditable rich text editors typing key by key with Enter creating new paragraphs.",
        "tool": "type_text",
        "args_example": {
            "selector": "#desc-rich",
            "text": "Para A\nPara B",
            "clear_first": True,
            "parse_newlines": True,
        },
        "verify": "Inspect DOM child paragraph elements or evaluate window.__state().",
        "pitfalls": [
            "parse_newlines=True is required so \\n triggers Enter keydown to create paragraphs."
        ],
        "runnable": True,
        "state_key": "rich",
        "expected_value": ["Para A", "Para B"],
    },
    {
        "id": "rich_text_fill",
        "field_type": "rich_text",
        "when": "Contenteditable rich text editors where multi-line text splits into separate paragraphs.",
        "tool": "fill",
        "args_example": {
            "selector": "#desc-rich",
            "value": "Para 1\nPara 2",
            "strategy": "auto",
        },
        "verify": "Read back child paragraphs with query_elements or evaluate window.__state().",
        "pitfalls": [
            "Multi-line text becomes separate paragraphs; rich styling buttons require click_element."
        ],
        "runnable": True,
        "state_key": "rich",
        "expected_value": ["Para 1", "Para 2"],
    },
    {
        "id": "number_fill",
        "field_type": "number",
        "when": "Numeric input fields (e.g. input type='number') taking integer or decimal values.",
        "tool": "fill",
        "args_example": {
            "selector": "#hours",
            "value": "8.5",
            "strategy": "auto",
        },
        "verify": "Read back with get_element_state(selector='#hours') or evaluate window.__state().",
        "pitfalls": [
            "Non-numeric strings raise InputFormatError ('numeric value') and leave the field unchanged."
        ],
        "runnable": True,
        "state_key": "hours",
        "expected_value": "8.5",
    },
    {
        "id": "select_native",
        "field_type": "select",
        "when": "Standard HTML <select> dropdown elements where option value attribute is known.",
        "tool": "select_option",
        "args_example": {
            "selector": "#priority",
            "value": "high",
        },
        "verify": "Read back with get_element_state(selector='#priority') or evaluate window.__state().",
        "pitfalls": [
            "Only works on native <select> elements; use choose_option for custom comboboxes."
        ],
        "runnable": True,
        "state_key": "priority",
        "expected_value": "high",
    },
    {
        "id": "select_choose",
        "field_type": "select",
        "when": "Native or custom select dropdown where choosing by visible option text is preferred.",
        "tool": "choose_option",
        "args_example": {
            "selector": "#priority",
            "option_text": "medium",
        },
        "verify": "Read back with get_element_state(selector='#priority') or evaluate window.__state().",
        "pitfalls": [
            "Matches visible text; ensure text matches option label without leading/trailing spaces."
        ],
        "runnable": True,
        "state_key": "priority",
        "expected_value": "medium",
    },
    {
        "id": "dropdown_custom",
        "field_type": "dropdown",
        "when": "Custom dropdown button/div triggers (role='listbox' / 'combobox') chosen by visible text.",
        "tool": "choose_option",
        "args_example": {
            "selector": "#status-dd",
            "option_text": "doing",
        },
        "verify": "Read button label or evaluate window.__state().",
        "pitfalls": [
            "If choose_option cannot detect popup, click the trigger then click the option item ('li[data-value=...]')."
        ],
        "runnable": True,
        "state_key": "status",
        "expected_value": "doing",
    },
    {
        "id": "chips_type_click",
        "field_type": "chips",
        "when": "Chip or tag combobox with dynamic suggestions: type prefix, wait for list, click suggestion.",
        "tool": "click_element",
        "args_example": {
            "selector": "#label-list li",
        },
        "verify": "Query rendered chips with query_elements(selector='#chips .chip') or window.__state().",
        "pitfalls": [
            "Type prefix first and call wait_for_element before clicking; duplicate tags are ignored."
        ],
        "runnable": False,
    },
    {
        "id": "chips_type_enter",
        "field_type": "chips",
        "when": "Chip or tag input using keyboard Enter to commit the highlighted or typed suggestion.",
        "tool": "press_key",
        "args_example": {
            "selector": "#label-input",
            "key": "Enter",
        },
        "verify": "Check that new chip element was appended to #chips or inspect window.__state().",
        "pitfalls": [
            "Must wait for suggestion list to populate before sending Enter; duplicate tags are ignored."
        ],
        "runnable": False,
    },
    {
        "id": "chips_remove",
        "field_type": "chips",
        "when": "Remove an existing chip or tag by clicking its close (x) button.",
        "tool": "click_element",
        "args_example": {
            "selector": "span.chip[data-label='frontend'] button.chip-x",
        },
        "verify": "Verify chip is absent from DOM with query_elements or inspect window.__state().",
        "pitfalls": [
            "Ensure selector targets the specific chip's x button to avoid removing the wrong tag."
        ],
        "runnable": False,
    },
    {
        "id": "file_upload_native",
        "field_type": "file",
        "when": "File input elements (<input type='file'>) or styled wrapper buttons for uploading local files.",
        "tool": "file_upload",
        "args_example": {
            "selector": "input[type=file]",
            "paths": ["/path/to/allowlisted/file.pdf"],
        },
        "verify": "Check file input files property or success message via get_element_state.",
        "pitfalls": [
            "Only absolute paths within allowlisted local directories are accepted; multi-file upload requires the multiple attribute."
        ],
        "runnable": False,
    },
]


def build_connect_instructions() -> str:
    """Return connect instructions for agents (ASCII only, ~400-800 tokens / 1200-3200 chars)."""
    return (
        "This MCP server provides undetectable browser automation using nodriver (CDP-based).\n\n"
        "Key features:\n"
        "- Headful background automation with offscreen window placement (no OS focus stolen).\n"
        "- Direct input primitives (fill, type_text, paste_text, press_key, select_option, choose_option).\n"
        "- Verification-first workflow: SPAs can drop inputs on re-render, always read back state.\n"
        "- Enterprise managed profiles: attach to existing Chrome via remote debugging (attach_browser).\n\n"
        "Agent Input Guide:\n"
        "1. Text & Textarea: Use 'fill' with strategy='auto' as the primary tool for single-line text\n"
        "   inputs and multiline textareas. 'fill' preserves exact newlines in textareas. Use 'type_text'\n"
        "   (with parse_newlines=True and clear_first=True) when fields have keyup/keydown listeners\n"
        "   or live autocomplete triggers that require individual keystroke events.\n"
        "2. Rich Text (contenteditable): Use 'paste_text' (clear_first=True) for inserting long multiline\n"
        "   text in a single paste event, or 'fill' with strategy='auto' (multiline text splits into\n"
        "   separate paragraphs). Formatting controls (bold, italic) require clicking toolbar buttons.\n"
        "3. Number Inputs: Use 'fill' with strategy='auto' and a numeric string (e.g. '12.5'). 'fill' on\n"
        "   a number input validates numeric format; non-numeric values raise InputFormatError ('numeric\n"
        "   value') and leave the field unchanged.\n"
        "4. Dropdowns & Selects: For standard HTML <select> elements, call 'select_option' by value attribute\n"
        "   or 'choose_option' by visible text. For custom combobox/dropdown widgets (button/div triggers),\n"
        "   call 'choose_option' with option_text, or click the trigger button then click the option item\n"
        "   ('li[data-value=...]').\n"
        "5. Chips & Tags: Type the tag prefix using 'type_text', call 'wait_for_element' on the suggestion list\n"
        "   ('#label-list li'), then click the suggestion item or call 'press_key' with key='Enter'. Remove\n"
        "   chips by clicking their delete button ('button.chip-x'). Duplicate tags are ignored.\n"
        "6. Verification Rule: Web frameworks and SPAs may silently discard inputs on re-render. Always\n"
        "   verify by reading back values using 'get_element_state', 'query_elements', or 'execute_script'.\n"
        "7. Background Safety: Automation windows default to offscreen placement (see docs/WINDOW_MODES.md).\n"
        "   Pages receive CDP focus emulation (document.hasFocus() is true) without stealing OS focus.\n"
        "8. File Uploads: Use 'file_upload' exclusively with allowlisted local file paths.\n"
        "9. Managed Profiles: For corporate Google Workspace accounts, see docs/MANAGED_PROFILES.md and\n"
        "   use 'attach_browser' on a dedicated user-data-dir with remote debugging.\n\n"
        "For complete recipes, read resource stealth://guide/input or run prompt how_to_input."
    )


def build_input_playbook() -> str:
    """Build comprehensive markdown input playbook from recipes and general rules."""
    lines: List[str] = [
        "# Browser Input Playbook for AI Agents",
        "",
        "This playbook documents proven patterns for interacting with web forms, rich text editors,",
        "dropdowns, comboboxes, and file inputs using Stealth Browser MCP.",
        "",
        "## General Rules & Core Architecture",
        "",
        "1. **Verification-First Rule**: Modern SPAs (React, Vue, Angular) often re-render components",
        "   and can discard synthetic input values. Always read back values after input using",
        "   `get_element_state`, `query_elements`, or `execute_script`.",
        "2. **Background & Focus Management**: Windows run in `offscreen` mode by default at",
        "   `(-32000, -32000)` without stealing operating system focus. CDP focus emulation is",
        "   enabled so `document.hasFocus() === true`. Do not attempt to force window focus (see",
        "   `docs/WINDOW_MODES.md`).",
        "3. **File Upload Restrictions**: `file_upload` operates strictly through CDP `DOM.setFileInputFiles`",
        "   and only accepts absolute paths from allowlisted local directories.",
        "4. **Enterprise Profiles**: For Google Workspace accounts subject to corporate policies (SSO,",
        "   2FA, device trust), launch Chrome with remote debugging on a dedicated profile directory",
        "   and use `attach_browser` (see `docs/MANAGED_PROFILES.md`).",
        "",
        "## Input Recipes Table",
        "",
        "| ID | Field Type | Recommended Tool | When to Use |",
        "|----|------------|------------------|-------------|",
    ]

    for r in INPUT_RECIPES:
        lines.append(f"| `{r['id']}` | `{r['field_type']}` | `{r['tool']}` | {r['when']} |")

    lines.extend([
        "",
        "## Detailed Recipe Specifications",
        "",
    ])

    for r in INPUT_RECIPES:
        lines.append(f"### Recipe: `{r['id']}` ({r['field_type']})")
        lines.append("")
        lines.append(f"- **When**: {r['when']}")
        lines.append(f"- **Tool**: `{r['tool']}`")
        lines.append(f"- **Arguments Example**: `{r['args_example']}`")
        lines.append(f"- **Verification**: {r['verify']}")
        if r.get("pitfalls"):
            lines.append("- **Pitfalls & Notes**:")
            for p in r["pitfalls"]:
                lines.append(f"  - {p}")
        lines.append("")

    lines.extend([
        "## Interaction Category Guidelines",
        "",
        "### 1. Plain Text Inputs & Textareas",
        "- `fill` with `strategy='auto'` is the standard, fastest path.",
        "- Multi-line text with newlines (`\\n`) in textareas is preserved exactly by `fill`.",
        "- Use `type_text` (with `clear_first=True` and `parse_newlines=True`) if the input has active",
        "  keystroke listeners (e.g. search suggestions, autocompletes, or custom formatting).",
        "",
        "### 2. Contenteditable & Rich Text Editors",
        "- For inserting long multi-line descriptions at once, prefer `paste_text(selector, text, clear_first=True)`.",
        "- When using `fill` on contenteditable elements, multi-line text is split into separate paragraph (`<p>`) nodes.",
        "- When using `type_text`, specify `parse_newlines=True` so `\\n` triggers an Enter key event rather than raw text.",
        "",
        "### 3. Number Fields",
        "- Number inputs (`<input type='number'>`) require numeric text (e.g. `'12.5'` or `'0'`).",
        "- Calling `fill` on a number input with non-numeric text raises `InputFormatError` ('numeric value') and",
        "  leaves the existing value unchanged.",
        "",
        "### 4. Dropdowns (Native vs Custom)",
        "- Native `<select>`: use `select_option(selector, value=...)` or `choose_option(selector, option_text=...)`.",
        "- Custom dropdowns (buttons or divs with `role='listbox'` or `role='combobox'`): call `choose_option` with",
        "  the trigger selector and visible `option_text`. Alternatively, call `click_element` on the trigger,",
        "  followed by `click_element` on the option item (`li[data-value=...]`).",
        "",
        "### 5. Chips and Tag Comboboxes",
        "- Type the prefix using `type_text`.",
        "- Wait for suggestions to render using `wait_for_element('#label-list li')`.",
        "- Select the tag by clicking the suggestion with `click_element` or pressing Enter with `press_key('Enter')`.",
        "- Remove an existing chip by clicking its close button (`button.chip-x`).",
        "- Duplicate entries are ignored by standard tag components.",
        "",
        "### 6. File Uploads",
        "- Always use `file_upload(selector, paths)` with absolute paths.",
        "- Paths must reside within allowlisted directories; upload fails if paths are outside allowlist.",
        "- Multi-file uploads require the `<input type='file'>` to have the `multiple` attribute.",
    ])

    return "\n".join(lines)


def how_to_input_prompt(field_type: Optional[str] = None) -> str:
    """Generate prompt content for how to input into form fields."""
    if field_type:
        normalized = field_type.strip().lower()
        matching = [r for r in INPUT_RECIPES if r["field_type"].lower() == normalized]
        if matching:
            lines = [
                f"Recommended input recipes for field type '{normalized}':",
                "",
            ]
            for r in matching:
                lines.append(f"Recipe `{r['id']}` (Tool: `{r['tool']}`):")
                lines.append(f"  When: {r['when']}")
                lines.append(f"  Example: {r['args_example']}")
                lines.append(f"  Verify: {r['verify']}")
                if r.get("pitfalls"):
                    lines.append(f"  Pitfalls: {'; '.join(r['pitfalls'])}")
                lines.append("")
            lines.append("Always verify the final value after input using get_element_state or execute_script.")
            return "\n".join(lines).strip()
        else:
            valid_types = sorted(list({r["field_type"] for r in INPUT_RECIPES}))
            return (
                f"No specific recipe found for field type '{field_type}'.\n\n"
                f"Available field types: {', '.join(valid_types)}.\n\n"
                "Read resource stealth://guide/input for full guidance."
            )

    lines = [
        "Browser Automation Input Recipes Summary:",
        "",
    ]
    for r in INPUT_RECIPES:
        lines.append(f"- {r['id']} ({r['field_type']}): tool `{r['tool']}` -- {r['when']}")

    lines.extend([
        "",
        "To get detailed arguments and pitfalls for a specific field type, run how_to_input with field_type (e.g. text, textarea, rich_text, number, select, dropdown, chips, file).",
        "For complete instructions, read resource stealth://guide/input.",
    ])
    return "\n".join(lines).strip()
