# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and adheres to Semantic Versioning where practical.

## [Unreleased]
### Added
- **`press_key()` tool** - Presses real keys through CDP `Input.dispatchKeyEvent`, producing trusted `keydown`/`keypress`/`keyup` events. Supports named keys (Enter, Tab, Escape, arrows, Backspace, Delete, Home, End, PageUp, PageDown, Space, F1-F12), single printable characters, modifier combinations (Shift, Control/Ctrl, Alt, Meta/Cmd), optional focus selector, repeat count, and inter-press delay.
- **Tests** - `tests/test_press_key.py` covers the key-to-CDP mapping, the modifier bitmask, and the dispatch sequence. A browser-backed test asserting `isTrusted` runs when `STEALTH_BROWSER_TESTS=1` is set.

- **Stealth benchmark** - `python -m benchmarks.stealth` runs the browser against a bundled local probe, an init script capability check, Sannysoft, Intoli, CreepJS, Cloudflare (nowsecure.nl), and the tls.peet.ws TLS and HTTP/2 fingerprint API. Each target reports PASS, DEGRADED, FAIL, or UNREACHABLE with its individual checks, and each run writes a JSON result file. A weekly GitHub Actions workflow runs it on Linux, Windows, and macOS in headless and headed mode plus the previous Chrome release, commits results to `docs/stealth-results/`, regenerates the automated section of `STEALTH_TESTS.md`, and opens a `stealth-regression` issue when a target gets worse (#25).
- **`STEALTH_BROWSER_EXECUTABLE`** - Environment variable that selects the browser executable instead of auto-detection.
- **Page bindings** - `create_page_binding`, `get_page_binding_calls`, `resolve_page_binding_call`, and `remove_page_binding` let page JavaScript call `window.<name>(...args)` and get a Promise that the agent answers. Calls are queued as JSON (64 KB per call, 500 queued per instance), `auto_resolve` answers immediately with a fixed value, and bindings follow every tab and navigation. Nothing runs on the server host. This replaces `create_python_binding`.
- **`BROWSER_TAB_RECYCLE_NAVIGATIONS`** - Opt-in main tab recycling. It was always on at 25 navigations before.
- **Launch flags benchmark target** - Fails when the real Chrome command line has automation, sandbox-disabling, or unsupported flags.
- **Input fidelity benchmark target** - Checks that `click_element` and `type_text` produce trusted events in a real order without touching the page DOM.
- **Tests** - `tests/test_js_values.py`, `tests/test_element_cloners.py`, and `tests/test_hooks_and_proxy.py` cover result conversion, template escaping, cloner data paths, hook matching, proxy parsing, and debug log export. CI now runs the unit test suite.

### Fixed
- **Sandbox flags** - Chrome keeps its sandbox on Windows and macOS, including under an administrator account. Only Linux as root or in a container gets `--no-sandbox`. `--disable-setuid-sandbox`, `--single-process`, and `--disable-gpu` are no longer added anywhere, so headed windows no longer show the "unsupported command-line flag" bar, tabs no longer share one crash-prone process in containers, and WebGL stays available.
- **Browser startup** - Chrome gets up to 30 seconds to start instead of under 3, which cold CI runners and slow disks missed. A failed start no longer leaves a Chrome process and temp profile behind, and Chrome's output goes to the null device instead of an unread pipe that could fill up and block it.
- **Trusted clicks** - `click_element` scrolls the element into view, moves the mouse along a short path, and presses and releases the button with CDP input events, so pages see trusted `pointerdown`, `mousedown`, `mouseup`, and `click` events. It used to call `el.click()` from JavaScript, which produces an untrusted click with no mouse events, and nodriver's click flashed a highlight element into the page DOM. Covered or hidden elements still fall back to a JavaScript click.
- **Trusted typing** - `type_text` sends keydown, keypress, input, and keyup for every character, holds Shift for capitals and symbols, and inserts other characters like an input method. It used to send bare `char` events. Clearing a field now selects the content and presses Backspace, so React and other frameworks see the change. `parse_newlines` and `shift_enter` press real Enter and Shift+Enter keys.
- **Speed** - `navigate` returns in about 30 ms on a simple page instead of about 550 ms, `close_instance` takes about 0.2 s instead of hitting a 5 s timeout every time, `click_element` dropped its fixed 0.5 s wait, `list_tabs` and `new_tab` no longer wait 0.5 s per tab, and `wait_for_element` polls every 100 ms without fetching the whole DOM tree. Element lookups for click, type, paste, and key presses fetch only the document root.
- **Network hooks** - Fetch interception is enabled only while a hook applies to the instance, so pages without hooks no longer pause every request twice. Hooks created after spawn now update the interception patterns of running instances, and removing the last hook turns interception off.
- **Network capture memory** - Only text-like response bodies (documents, XHR, fetch, scripts, JSON, text, XML) are stored, up to 2 MB each and 64 MB per instance, oldest dropped first. Images, media, and fonts are fetched on demand instead of copied for every request. Stored bodies also serve `get_response_content` after the page has moved on. Chrome internal URLs are no longer captured.
- **Tab state** - Timezone override, extra headers, init scripts, network capture, and page bindings are applied to every tab the instance uses, including tabs opened with `new_tab`, switched to, or created to replace a closed or crashed tab. A replaced tab used to silently lose the timezone override and headers. `new_tab` configures the tab before loading the URL.
- **Closed tab recovery** - Tools recover when the page closes its own tab or the tab crashes, opening a new window when the last one is gone.
- **Persistent functions** - `create_persistent_function` now really survives reloads and new tabs, validates the name and code, and keeps the function out of `Object.keys(window)`.
- **Cleanup** - Every close path (tool, idle reaper, shutdown) clears network data, persistent functions, and bindings for the instance. Shutdown and idle reaping close instances in parallel, and nodriver's target refresh tasks no longer error after the browser exits.
- **Headless detection** - Headless instances no longer report `HeadlessChrome` in `navigator.userAgent`, worker user agents, or the `User-Agent` header. The real headless user agent is read once per browser build and launched with the headless marker removed, unless a custom `user_agent` is set. Headless runs of the stealth benchmark went from 2 of 7 targets passing to 7 of 7, including CreepJS (67% headless to 0%) and the Sannysoft `HEADCHR_UA` and `CHR_MEMORY` checks.
- **Execution contexts** - `get_execution_contexts` returns the real CDP contexts for every frame and isolated world instead of a single fake entry. `context_id` (numeric id or unique id) is now honored by `discover_global_functions`, `inject_and_execute_script`, and `execute_function_sequence`.
- **Tabs** - `close_tab` moves the instance to another open tab when the current tab is closed, and closing the last tab leaves a blank tab so the browser stays usable.
- **Responsiveness** - Browser process and profile cleanup runs in a worker thread, so closing an instance no longer freezes other tool calls for several seconds.
- **Authenticated proxies** - Plain HTTP requests through an authenticated proxy no longer fail with 407 after the first request on a keep-alive connection. CONNECT responses are checked by status code.
- **Script results** - `execute_script` returns plain JSON values instead of raw CDP deep-serialized nodes, and reports JavaScript exceptions as failures instead of success.
- **Element cloning** - Selectors are JSON-escaped in every extractor, so quotes and backslashes work. Progressive `expand_*` tools, `extract_complete_element_to_file` summaries, CDP matched styles, and `clone_element_to_file` sub-extractors now return real data. File tools no longer save extraction errors as successful files.
- **Broken tools** - `extract_element_assets`, `extract_related_files`, and `discover_object_methods` no longer crash on an invalid `await`. `set_cookie(same_site=...)`, `spawn_browser(extra_headers=...)`, and `navigate(referrer=...)` no longer fail on CDP type errors. Browser state, cookie, and network resources serialize correctly.
- **Navigation** - `wait_until="load"` and `"domcontentloaded"` wait for the page event instead of returning immediately. The referrer applies to the navigation only and no longer replaces spawn-time headers.
- **Page tools** - `reload_page` honors `ignore_cache`, `take_screenshot` honors `format` and `full_page`, `select_option` works by text and can be called repeatedly, `scroll_page` accepts negative amounts, and `wait_for_element` respects short timeouts.
- **Network capture** - Resource types are recorded, so capture filters and type searches work. `response_contains` only matches captured bodies. Captured requests are capped per instance and bodies are read after loading finishes.
- **Dynamic hooks** - Resource type and custom conditions match as documented, `|` alternatives work in URL and method requirements, global hooks apply to new instances, response headers are passed to hooks, compile errors are reported, and simple hook values can no longer inject code.
- **Process cleanup** - Owner liveness checks work on Windows, the shared PID file is written atomically, and a reused PID is never killed during recovery.
- **Proxies** - Percent-encoded proxy credentials are decoded, and proxy errors no longer include passwords.
- **Debug logs** - Exporting logs no longer deadlocks the server, log lists are capped, and `max_errors=0` returns no entries.
- **Python in browser** - The final expression is translated once instead of being re-run as raw Python.
- `get_instance_state` accepts fractional `devicePixelRatio` values.

### Changed
- `STEALTH_TESTS.md` now leads with generated benchmark results. The 2026-02-10 manual snapshot is kept below it, and the X.com section no longer implies an Arkose challenge was solved.
- `add_script_to_evaluate_on_new_document` logic moved into `BrowserManager.add_init_script` so the benchmark tests the same code path, and installed scripts are re-applied to new and replacement tabs.
- The MCP server instructions no longer claim instances are undetectable and point to the measured results instead.
- `element-interaction` section now exposes 13 tools; full surface is 101 tools, minimal surface is 21.
- HTTP transport binds to `127.0.0.1` by default and warns on stderr when bound to another host without an auth token.
- `hot_reload` refuses to run while browser instances are open, so they are not orphaned.
- Removed the unused `response_stage_hooks` module and unused JavaScript templates.

### Removed
- **`create_python_binding`** - Removed for security. It ran arbitrary Python from the client on the server host with `exec()`, any website could call the binding it exposed, and the bridge never delivered calls to Python. Use the page binding tools instead.

### Notes
- `type_text(parse_newlines=True)` now presses a real Enter key for each newline. Use `press_key()` for other control keys in React-select and typeahead widgets (Greenhouse, Ashby, Lever, Workday).

## [0.2.5] - 2026-02-10
### Fixed
- **MCP JSON-RPC Protocol Corruption** - All debug `print()` calls redirected from stdout to stderr, fixing tool hangs after `spawn_browser` and `navigate` (#8)
- **Python Version Requirement** - Corrected `requires-python` from `>=3.8` to `>=3.10` (fastmcp requires 3.10+)
- **Missing Dependency** - Added `uvicorn[standard]` to pyproject.toml (was only in requirements.txt)
- **SECURITY.md Branch Reference** - Fixed `main` to `master`

### Added
- **Microsoft Edge Support** - Automatic browser detection for Chrome, Chromium, and Edge (thanks [@Hamza5](https://github.com/Hamza5))
- **Troubleshooting Section** - Common issues and fixes documented in README

### Changed
- **README Rewrite** - Reduced from 706 to 468 lines; removed duplicate tool listings, stale labels, and excessive emojis
- **Hall of Fame** - Replaced fabricated testimonials with real contributor table and verified use cases
- **Example Prompts** - Stripped marketing hype, kept practical copy-paste prompts
- **Repo Cleanup** - Removed internal Checklist.md from tracking, updated Discord links

## [0.2.4] - 2025-08-11
### Fixed
- **🛡️ Root User Browser Spawning** - Fixed "Failed to connect to browser" when running as root/administrator
- **📝 Args Parameter Validation** - Fixed "Input validation error" for JSON string args format
- **🐳 Container Environment Support** - Added Docker/Kubernetes compatibility with auto-detection
- **🔧 Cross-Platform Compatibility** - Enhanced Windows/Linux/macOS support with platform-aware configuration

### Added
- **🔍 `validate_browser_environment_tool()`** - New diagnostic tool for environment validation
- **⚙️ Smart Platform Detection** - Auto-detects root privileges, containers, and OS-specific requirements
- **🔄 Flexible Args Parsing** - Supports JSON arrays, JSON strings, and single string formats
- **📊 Enhanced Logging** - Added platform information to browser spawning debug logs
- **🛠️ `platform_utils.py`** - Comprehensive cross-platform utility module

### Enhanced
- **Browser Argument Handling** - Automatically merges user args with platform-required args
- **Environment Detection** - Detects root/administrator, container environments, and Chrome installation
- **Error Messages** - More descriptive error messages with platform-specific guidance
- **Sandbox Management** - Intelligent sandbox disabling based on environment detection

### Technical
- Added `merge_browser_args()` function for smart argument merging
- Added `is_running_as_root()` cross-platform privilege detection
- Added `is_running_in_container()` for Docker/Kubernetes detection
- Enhanced `spawn_browser()` with comprehensive args parsing
- Improved browser configuration with nodriver Config object
- Total tool count increased from 89 to 90 tools

## [0.2.3] - 2025-08-10
### Added
- **⚡ `paste_text()` function** - Lightning-fast text input via Chrome DevTools Protocol
- **📝 Enhanced `type_text()`** - Added `parse_newlines` parameter for proper Enter key handling
- **🚀 CDP-based text input** - Uses `insert_text()` method for instant large content pasting
- **💡 Smart newline parsing** - Converts `\n` strings to actual Enter key presses when enabled

### Enhanced  
- **Text Input Performance** - `paste_text()` is 10x faster than character-by-character typing
- **Multi-line Form Support** - Proper handling of complex multi-line inputs and text areas
- **Content Management** - Handle large documents (README files, code blocks) without timeouts
- **Chat Application Support** - Send multi-line messages with preserved line breaks

### Technical
- Implemented `DOMHandler.paste_text()` using `cdp.input_.insert_text()` 
- Enhanced `DOMHandler.type_text()` with line-by-line processing for newlines
- Added proper fallback clearing methods for both functions
- Updated MCP server endpoints with new `paste_text` tool
- Updated tool count from 88 to 89 functions

## [0.2.2] - 2025-08-10
### Added
- **🎛️ Modular Tool System** - CLI arguments to disable specific tool sections
- **⚡ --minimal mode** - Run with only core browser management and element interaction tools
- **📋 --list-sections** - List all 11 tool sections with tool counts
- **🔧 Granular Control** - Individual disable flags for each of 11 tool sections:
  - `--disable-browser-management` (11 tools)
  - `--disable-element-interaction` (10 tools) 
  - `--disable-element-extraction` (9 tools)
  - `--disable-file-extraction` (9 tools)
  - `--disable-network-debugging` (5 tools)
  - `--disable-cdp-functions` (13 tools)
  - `--disable-progressive-cloning` (10 tools)
  - `--disable-cookies-storage` (3 tools)
  - `--disable-tabs` (5 tools)
  - `--disable-debugging` (6 tools)
  - `--disable-dynamic-hooks` (10 tools)
- **🏗️ Clean Architecture** - Section-based decorator system for conditional tool registration

### Changed
- Updated CLI help text to show "88 tools" and new section options
- Reorganized tool registration using `@section_tool()` decorator pattern
- All tools now conditionally register based on disabled sections set

### Technical
- Implemented `DISABLED_SECTIONS` global set for tracking disabled functionality
- Added `is_section_enabled()` helper function
- Created `@section_tool("section-name")` decorator for conditional registration
- Tools are only registered if their section is enabled

## [0.2.1] - 2025-08-09
### Added
- **🚀 Dynamic Network Hook System** - AI-powered request/response interception
- **🧠 AI Hook Learning System** - 10 comprehensive hook examples and documentation
- **⚡ Real-time Processing** - No pending state, immediate hook execution
- **🐍 Custom Python Functions** - AI writes hook logic with full syntax validation
- **🔧 Hook Management Tools** - Create, list, validate, and remove hooks dynamically

### Fixed
- RequestId type conversion issues in CDP calls
- Missing imports in hook learning system
- Syntax errors in browser manager integration
- **Smithery.ai deployment Docker build failure** - Added `git` to Dockerfile system dependencies for py2js installation
- **Smithery.ai PORT environment variable support** - Server now reads PORT env var as required by Smithery deployments
- **Docker health check endpoint** - Updated health check to use correct /mcp endpoint with dynamic PORT

### Changed
- Replaced old network hook system with dynamic architecture
- Updated documentation to reflect new capabilities
- **Removed 13 broken/incomplete network hook functions** - Moved to `oldstuff/old_funcs.py` for reference
- **Corrected MCP tool count to 88 functions** - Updated all documentation consistently

### Removed
- `create_request_hook`, `create_response_hook`, `create_redirect_hook`, `create_block_hook`, `create_custom_response_hook` - These functions were calling non-existent methods
- `list_network_hooks`, `get_network_hook_details`, `remove_network_hook`, `update_network_hook_status` - Management functions for the broken hook system
- `list_pending_requests`, `get_pending_request_details`, `modify_pending_request`, `execute_pending_request` - Pending request management (replaced by real-time dynamic hooks)

## [0.2.0] - 2025-08-08
### Added
- Initial dynamic network hook system implementation
- Real-time request/response processing architecture

## [0.1.0] - 2025-08-07
### Added
- Initial public README overhaul
- Community health files (CoC, Contributing, Security, Roadmap, Changelog)
- Issue and PR templates


