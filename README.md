<div align="center">

<img src="media/UndetectedStealthBrowser-2.png" alt="Stealth Browser MCP logo" width="200"/>

# Stealth Browser MCP

**Stealth browser automation for MCP-compatible AI agents.**

Navigate Cloudflare challenges, anti-bot checks, and login walls with real Chrome-family browser instances powered by [nodriver](https://github.com/ultrafunkamsterdam/nodriver), Chrome DevTools Protocol, and [FastMCP](https://github.com/jlowin/fastmcp).

[![MCP](https://img.shields.io/badge/MCP-Compatible-blue?style=flat-square)](https://modelcontextprotocol.io)
[![Version](https://img.shields.io/badge/version-0.2.5-blue?style=flat-square)](pyproject.toml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)](pyproject.toml)
[![Stars](https://img.shields.io/github/stars/vibheksoni/stealth-browser-mcp?style=flat-square)](https://github.com/vibheksoni/stealth-browser-mcp/stargazers)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](CONTRIBUTING.md)

</div>

---

## Sponsors

<a href="https://go.nodemaven.com/stealthbrowsermcpreadmesept"><img src="media/nodemaven-sponsor.png" alt="NodeMaven" width="100%"></a>

[NodeMaven](https://go.nodemaven.com/stealthbrowsermcpreadmesept) — The most efficient proxy provider for Web Scraping and Automation with the Highest Quality IP on the market.

**Why NodeMaven?**

- ZIP targeting
- 99.9% uptime
- IP filtering: all proxies have fraud score <97%
- No KYC required
- Unique free tools: Proxy Bandwidth Checker, Meta Tag Checker, IP Lookup and others!

**Special codes for Stealth-browser-mcp users**

- `STEALTHMCP35` — 35% off to Mobile and Residential Proxies
- `STEALTHMCP40` — 40% off to ISP (Static) Proxies

[Get started with NodeMaven](https://go.nodemaven.com/stealthbrowsermcpreadmesept)

> Sponsored placement. NodeMaven is a paid sponsor of this project.

---

## Table of Contents

- [Sponsors](#sponsors)
- [Demo](#demo)
- [Features](#features)
- [Quickstart](#quickstart)
- [Agent Skill](#agent-skill)
- [Trust Model](#trust-model)
- [Modular Architecture](#modular-architecture)
- [Server Configuration](#server-configuration)
- [Toolbox](#toolbox)
- [Stealth vs Playwright MCP](#stealth-vs-playwright-mcp)
- [Troubleshooting](#troubleshooting)
- [Examples](#examples)
- [Showcase](#showcase)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Support](#support)
- [License](#license)

---

## Demo

<div align="center">
<img src="media/showcase-demo-full.gif" alt="Stealth Browser MCP demo" width="800" style="border-radius: 8px;">
<br><br>
<a href="media/Showcase%20Stealth%20Browser%20Mcp.mp4" download>
  <img src="https://img.shields.io/badge/Watch%20HD%20Video-red?style=for-the-badge&logo=video&logoColor=white" alt="Watch HD Video">
</a>
</div>

*Stealth Browser MCP passing a Cloudflare challenge, cloning UI elements, and inspecting network traffic through AI chat commands.*

---

## Features

- **Anti-bot resistance** - Has passed Cloudflare and Queue-It style challenges in testing; results vary by site, region, browser version, and detector version.
- **101 tools across 11 sections** - From basic navigation to advanced CDP function execution.
- **Modular loading** - Run the full 101-tool surface or a minimal 21-tool core; disable sections you do not need.
- **Pixel-accurate element cloning** - Extract complete elements with CSS, DOM structure, events, animations, and assets via CDP.
- **Network inspection** - Inspect requests, responses, headers, payloads, and captured bodies through your AI agent.
- **Dynamic hook system** - Restricted Python hooks can intercept, block, redirect, fulfill, or modify request/response flows.
- **CDP execution** - Run JavaScript, direct CDP commands, and pre-document scripts through trusted local MCP clients.
- **Instant text input** - Paste large content via CDP, type with human-like keystrokes, or press real keys with trusted CDP key events.
- **Cross-platform** - Windows, macOS, Linux, Docker, and CI environments with automatic browser/platform detection.
- **Browser support** - Chrome, Chromium, and Microsoft Edge with automatic executable discovery.
- **Clean MCP integration** - Works through standard MCP clients without custom brokers.

---

## Quickstart

### 1. Clone and install

Requires Python 3.10+ and Chrome, Chromium, or Microsoft Edge.

```bash
git clone https://github.com/vibheksoni/stealth-browser-mcp.git
cd stealth-browser-mcp
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Add to your MCP client

**Claude Code CLI (recommended):**

Windows:

```powershell
claude mcp add-json stealth-browser-mcp "{\"type\":\"stdio\",\"command\":\"C:\\path\\to\\stealth-browser-mcp\\venv\\Scripts\\python.exe\",\"args\":[\"C:\\path\\to\\stealth-browser-mcp\\src\\server.py\"]}"
```

macOS / Linux:

```bash
claude mcp add-json stealth-browser-mcp '{
  "type": "stdio",
  "command": "/path/to/stealth-browser-mcp/venv/bin/python",
  "args": ["/path/to/stealth-browser-mcp/src/server.py"]
}'
```

Replace the example path with your real clone path.

<details>
<summary><strong>Manual JSON configuration (Claude Desktop, Cursor, etc.)</strong></summary>

Windows (`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "stealth-browser-mcp": {
      "command": "C:\\path\\to\\stealth-browser-mcp\\venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\stealth-browser-mcp\\src\\server.py"],
      "env": {}
    }
  }
}
```

macOS / Linux (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "stealth-browser-mcp": {
      "command": "/path/to/stealth-browser-mcp/venv/bin/python",
      "args": ["/path/to/stealth-browser-mcp/src/server.py"],
      "env": {}
    }
  }
}
```

</details>

<details>
<summary><strong>Experimental FastMCP CLI install</strong></summary>

These commands are provided for users who prefer FastMCP's installer. The stdio JSON setup above is the recommended path.

```bash
pip install fastmcp
fastmcp install claude-desktop src/server.py --with-requirements requirements.txt
fastmcp install claude-code src/server.py --with-requirements requirements.txt
fastmcp install cursor src/server.py --with-requirements requirements.txt
```

</details>

### 3. Test it

Restart your MCP client and ask your agent:

> Use stealth-browser to navigate to https://example.com and take a screenshot.

---

## Agent Skill

This repo includes an agent skill at `skills/stealth-browser-mcp` for clients that support Codex-style skills. It teaches agents the recommended tool order, state checks, pre-document CDP script usage, network debugging flow, and browser cleanup rules.

Use the skill when an LLM struggles to choose the right MCP tool or forgets to verify browser state after actions. If your client does not load repo-local skills automatically, add or symlink `skills/stealth-browser-mcp` into your client skills directory.

---

## Trust Model

This server exposes powerful local browser-control primitives: CDP execution, page JavaScript execution, cookie/storage access, network interception, dynamic hooks, header mutation, navigation, and file uploads from allowlisted local paths.

Treat the MCP client or agent as the security principal. The recommended deployment is local `stdio` transport with a trusted desktop MCP client. If you use HTTP transport, set `STEALTH_BROWSER_MCP_AUTH_TOKEN` and do not expose unauthenticated HTTP outside trusted local or private networks.

---

## Modular Architecture

Choose exactly what functionality you need. Run the full 101-tool suite or strip it down to the 21-tool core.

| Mode | Tools | Use Case |
|------|-------|----------|
| **Full** (default) | 101 | Complete browser automation and debugging |
| **Minimal** (`--minimal`) | 21 | Core browser automation and interaction |
| **Custom** (`--disable-*`) | Your choice | Disable specific sections |
| **Xpool safe** (`--xpool-safe`) | 84 | Disable CDP function tools that trigger `Runtime.enable` |

```bash
python src/server.py --minimal
python src/server.py --disable-cdp-functions --disable-dynamic-hooks
python src/server.py --list-sections
python src/server.py --debug
python src/server.py --transport http --host 127.0.0.1 --port 8000
```

Use `--debug` or set `STEALTH_BROWSER_DEBUG=1` to enable verbose server diagnostics on `stderr`. Normal MCP `stdio` runs stay quiet by default to avoid corrupting JSON-RPC transport output.

**Available sections:**

| Section | Tools | Description |
|---------|-------|-------------|
| `browser-management` | 8 | Core browser operations |
| `element-interaction` | 13 | Page interaction and manipulation |
| `element-extraction` | 9 | Element cloning and extraction |
| `file-extraction` | 9 | File-based extraction tools |
| `network-debugging` | 10 | Network monitoring and capture |
| `cdp-functions` | 17 | Chrome DevTools Protocol execution |
| `progressive-cloning` | 10 | Advanced element cloning |
| `cookies-storage` | 3 | Cookie management |
| `tabs` | 5 | Tab management |
| `debugging` | 7 | Debug and system tools |
| `dynamic-hooks` | 10 | AI-assisted network hooks |

---

## Server Configuration

These are environment variables for the MCP server process. Set them in your shell, service manager, Docker environment, or MCP client `env` block.

| Variable | Default | Meaning |
|----------|---------|---------|
| `STEALTH_BROWSER_MCP_AUTH_TOKEN` | unset | Optional bearer token for HTTP transport. When set, HTTP clients must send `Authorization: Bearer <token>`. |
| `MCP_AUTH_TOKEN` | unset | Backward-compatible alias for `STEALTH_BROWSER_MCP_AUTH_TOKEN`. |
| `BROWSER_IDLE_TIMEOUT` | `600` | Global idle timeout in seconds before an unused browser instance is auto-closed. Set `0` to disable idle reaping globally. |
| `BROWSER_IDLE_REAPER_INTERVAL` | `60` | Background reaper check interval in seconds. |
| `BROWSER_TAB_RECYCLE_NAVIGATIONS` | `0` | Replace the main tab with a fresh one after this many navigations. `0` keeps the same tab, which preserves history and session storage. |
| `BROWSER_ORPHAN_PROFILE_MAX_AGE` | `21600` | Startup cleanup threshold in seconds for stale `uc_*` temp profiles. Set `0` to disable this startup sweep. |
| `STEALTH_BROWSER_EXECUTABLE` | auto-detect | Path to the Chrome, Chromium, or Edge executable to launch instead of the auto-detected one. |
| `BROWSER_FILE_UPLOAD_ALLOWED_DIRS` | repo root | Directories that `file_upload()` may read from. Separate multiple roots with `;` on Windows or `:` on macOS/Linux. |
| `STEALTH_BROWSER_DEBUG` | `0` | Enable verbose debug logging to `stderr` when set to `1`. |
| `DEBUG` | `0` | Legacy alias for `STEALTH_BROWSER_DEBUG`. |
| `XPOOL_SAFE_MODE` | `0` | Disable the `cdp-functions` section at startup. |
| `PORT` | `8000` | Default HTTP port when `--port` is not provided. |

**PowerShell example:**

```powershell
$env:STEALTH_BROWSER_MCP_AUTH_TOKEN='replace-with-a-long-random-token'
$env:BROWSER_IDLE_TIMEOUT='900'
$env:BROWSER_IDLE_REAPER_INTERVAL='30'
$env:BROWSER_ORPHAN_PROFILE_MAX_AGE='43200'
$env:BROWSER_FILE_UPLOAD_ALLOWED_DIRS='C:\Users\me\uploads;C:\Users\me\Documents'
python src/server.py
```

**MCP client `env` block:**

```json
{
  "mcpServers": {
    "stealth-browser-mcp": {
      "command": "C:\\path\\to\\stealth-browser-mcp\\venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\stealth-browser-mcp\\src\\server.py"],
      "env": {
        "STEALTH_BROWSER_MCP_AUTH_TOKEN": "replace-with-a-long-random-token",
        "BROWSER_IDLE_TIMEOUT": "900",
        "BROWSER_FILE_UPLOAD_ALLOWED_DIRS": "C:\\Users\\me\\uploads;C:\\Users\\me\\Documents"
      }
    }
  }
}
```

**HTTP client example:**

```python
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

client = Client(
    "http://127.0.0.1:8000/mcp/",
    auth=BearerAuth("replace-with-a-long-random-token"),
)
```

**Per-instance idle override:**

- `spawn_browser(idle_timeout_seconds=1800)` keeps that instance for 30 minutes of inactivity.
- `spawn_browser(idle_timeout_seconds=0)` disables idle reaping for that one instance.

---

## Toolbox

<details>
<summary><strong>Browser Management (8)</strong></summary>

| Tool | Description |
|------|-------------|
| `spawn_browser()` | Create a stealth browser instance |
| `list_instances()` | List active browser sessions |
| `close_instance()` | Clean shutdown of a browser |
| `get_instance_state()` | Full browser state information |
| `navigate()` | Navigate to URLs |
| `go_back()` | Navigate back in history |
| `go_forward()` | Navigate forward in history |
| `reload_page()` | Reload current page |

</details>

<details>
<summary><strong>Element Interaction (13)</strong></summary>

| Tool | Description |
|------|-------------|
| `query_elements()` | Find elements by CSS selector or XPath |
| `click_element()` | Click an element |
| `type_text()` | Human-like typing with newline support |
| `paste_text()` | Instant text paste via CDP |
| `press_key()` | Press a real key with trusted CDP key events (Enter, Tab, arrows, modifiers) |
| `file_upload()` | Upload allowlisted local files to file inputs |
| `select_option()` | Select dropdown options |
| `get_element_state()` | Inspect element state |
| `wait_for_element()` | Wait for an element |
| `scroll_page()` | Scroll naturally |
| `execute_script()` | Run page JavaScript |
| `get_page_content()` | Read page HTML and text |
| `take_screenshot()` | Capture screenshots |

</details>

<details>
<summary><strong>Network Debugging (10)</strong></summary>

| Tool | Description |
|------|-------------|
| `list_network_requests()` | List captured network requests |
| `get_request_details()` | Inspect request headers and payload |
| `get_response_details()` | Inspect response metadata |
| `get_response_content()` | Read captured response content |
| `search_network_requests()` | Search captured requests |
| `export_network_data()` | Export captured network data |
| `import_network_data()` | Import captured network data |
| `set_network_capture_filters()` | Configure capture filters |
| `get_network_capture_filters()` | Read active capture filters |
| `modify_headers()` | Add or replace request headers |

</details>

<details>
<summary><strong>Cookies and Storage (3)</strong></summary>

| Tool | Description |
|------|-------------|
| `get_cookies()` | Read cookies |
| `set_cookie()` | Set cookies |
| `clear_cookies()` | Clear cookies |

</details>

<details>
<summary><strong>Debugging (7)</strong></summary>

| Tool | Description |
|------|-------------|
| `get_debug_view()` | Debug logs and statistics with pagination |
| `clear_debug_view()` | Clear debug logs |
| `export_debug_logs()` | Export logs as JSON, pickle, or gzip |
| `get_debug_lock_status()` | Inspect debug logger lock state |
| `hot_reload()` | Reload modules without server restart |
| `reload_status()` | Check module reload status |
| `validate_browser_environment_tool()` | Diagnose browser/platform compatibility |

</details>

<details>
<summary><strong>Tabs (5)</strong></summary>

| Tool | Description |
|------|-------------|
| `list_tabs()` | List open tabs |
| `switch_tab()` | Change active tab |
| `close_tab()` | Close a tab |
| `get_active_tab()` | Get current active tab |
| `new_tab()` | Create a new tab |

</details>

<details>
<summary><strong>Element Extraction (9)</strong></summary>

| Tool | Description |
|------|-------------|
| `extract_element_styles()` | Extract computed CSS properties |
| `extract_element_structure()` | Extract DOM structure |
| `extract_element_events()` | Extract event listeners |
| `extract_element_animations()` | Extract animations and transitions |
| `extract_element_assets()` | Extract images, fonts, videos, and assets |
| `extract_element_styles_cdp()` | Pure CDP style extraction |
| `extract_related_files()` | Find related CSS/JS files |
| `clone_element_complete()` | Complete element cloning |
| `extract_complete_element_cdp()` | Complete CDP-based element clone |

</details>

<details>
<summary><strong>Progressive Cloning (10)</strong></summary>

| Tool | Description |
|------|-------------|
| `clone_element_progressive()` | Initial lightweight element clone |
| `expand_styles()` | Expand styles on demand |
| `expand_events()` | Expand events on demand |
| `expand_children()` | Expand child nodes on demand |
| `expand_css_rules()` | Expand CSS rule data |
| `expand_pseudo_elements()` | Expand pseudo-elements |
| `expand_animations()` | Expand animation data |
| `list_stored_elements()` | List stored progressive clones |
| `clear_stored_element()` | Clear one stored element |
| `clear_all_elements()` | Clear all stored elements |

</details>

<details>
<summary><strong>File-Based Extraction (9)</strong></summary>

| Tool | Description |
|------|-------------|
| `clone_element_to_file()` | Save complete clone to file |
| `extract_complete_element_to_file()` | Save complete extraction to file |
| `extract_element_styles_to_file()` | Save styles to file |
| `extract_element_structure_to_file()` | Save structure to file |
| `extract_element_events_to_file()` | Save events to file |
| `extract_element_animations_to_file()` | Save animations to file |
| `extract_element_assets_to_file()` | Save assets to file |
| `list_clone_files()` | List saved clone files |
| `cleanup_clone_files()` | Clean up old clone files |

</details>

<details>
<summary><strong>CDP Function Execution (14)</strong></summary>

| Tool | Description |
|------|-------------|
| `list_cdp_commands()` | List available CDP commands |
| `execute_cdp_command()` | Run direct CDP commands |
| `add_script_to_evaluate_on_new_document()` | Install scripts before page JavaScript runs |
| `get_execution_contexts()` | List JavaScript execution contexts |
| `discover_global_functions()` | Find global JavaScript functions |
| `discover_object_methods()` | Discover methods on JavaScript objects |
| `call_javascript_function()` | Execute a discovered JavaScript function |
| `inspect_function_signature()` | Inspect function details |
| `inject_and_execute_script()` | Inject and run custom JavaScript |
| `create_persistent_function()` | Create functions that survive reloads |
| `create_page_binding()` | Expose `window.<name>()` so page JavaScript can call back to the agent |
| `get_page_binding_calls()` | Read queued page binding calls, optionally waiting for one |
| `resolve_page_binding_call()` | Answer a page binding call with a value or an error |
| `remove_page_binding()` | Remove a page binding and reject its pending calls |
| `execute_function_sequence()` | Execute ordered function calls |
| `execute_python_in_browser()` | Translate Python to JavaScript with py2js and run it |
| `get_function_executor_info()` | Inspect executor state |

</details>

<details>
<summary><strong>Dynamic Hooks (10)</strong></summary>

| Tool | Description |
|------|-------------|
| `create_dynamic_hook()` | Create a restricted Python network hook |
| `create_simple_dynamic_hook()` | Create hooks from common presets |
| `list_dynamic_hooks()` | List active hooks |
| `get_dynamic_hook_details()` | Inspect hook source and metadata |
| `remove_dynamic_hook()` | Remove a hook |
| `get_hook_documentation()` | Read request object and action docs |
| `get_hook_examples()` | View hook examples |
| `get_hook_requirements_documentation()` | Read matching and best-practice docs |
| `get_hook_common_patterns()` | Common hook patterns |
| `validate_hook_function()` | Validate hook code before use |

</details>

---

## Stealth vs Playwright MCP

Detection results are point-in-time and depend on site policy, region, browser version, IP reputation, and detector version. See [STEALTH_TESTS.md](STEALTH_TESTS.md) for weekly automated results from the benchmark in `benchmarks/stealth/`, which runs on Linux, Windows, and macOS in GitHub Actions, plus the earlier manual snapshot.

| Feature | Stealth Browser MCP | Playwright MCP |
|---------|---------------------|----------------|
| Cloudflare / Queue-It style checks | Passes common JS challenges in testing | Commonly blocked |
| Hardened sites | Uses real Chrome-family browser instances through nodriver | Frequently flagged as automation |
| Login-walled pages | Can inspect loaded content and overlays through CDP | Often requires manual scripting |
| UI element cloning | CDP-accurate extraction | Limited |
| Network debugging | Full request/response inspection through AI tools | Basic |
| API reverse engineering | Payload inspection through chat | Manual tools only |
| Dynamic hook system | Restricted Python hooks for real-time interception | Not available |
| Modular architecture | 11 sections, 21-101 tools | Fixed tool surface |
| Total tools | 101 customizable tools | About 20 |

---

## Troubleshooting

**No compatible browser found**
Install Chrome, Chromium, or Microsoft Edge. The server auto-detects the first available browser. Run `validate_browser_environment_tool()` to diagnose.

**Tools hang or return malformed JSON**
Debug output must not print to stdout during MCP `stdio` transport. Pull the latest `master` branch and keep debug logging disabled unless diagnosing a problem.

**Need verbose diagnostics without noisy normal runs**
Use `python src/server.py --debug` or set `STEALTH_BROWSER_DEBUG=1`. Debug logs are emitted to `stderr`; normal MCP `stdio` runs stay quiet by default.

**Browser crashes on Linux, Docker, or CI**
Run with `--sandbox=false` only if your environment requires it, or ensure the environment supports Chromium sandboxing. The server auto-detects root/container environments and adjusts launch arguments.

**Orphan Chromium processes or `uc_*` temp profiles accumulate**
The server reaps idle browser instances automatically and performs startup cleanup of tracked orphan browser processes plus stale `uc_*` temp profiles. Set `BROWSER_IDLE_TIMEOUT=0` only if you want fully manual browser lifetime management.

**Too many tools cluttering the AI chat**
Use `--minimal` for 20 core tools, `--xpool-safe` for xpool-compatible operation, or selectively disable sections:

```bash
python src/server.py --disable-cdp-functions --disable-dynamic-hooks --disable-progressive-cloning
```

**Module not found errors**
Make sure you activated the virtual environment and installed dependencies:

```bash
pip install -r requirements.txt
```

---

## Examples

- **Market research** - Extract pricing and features from competitors, then output a comparison table.
- **UI cloning** - Recreate a pricing section with exact fonts, styles, assets, and interactions.
- **Inventory monitoring** - Watch a product page and alert when stock changes.
- **API reverse engineering** - Intercept requests, map endpoints, and inspect data flow.
- **Form automation** - Upload files from allowlisted paths and verify resulting page state.

All driven from a single AI agent conversation.

---

## Showcase

<div align="center">
<img src="media/AugmentHeroClone.PNG" alt="Augment Code Hero Recreation" width="700" style="border-radius: 8px;">
</div>

**Augment Code hero clone** - A user asked Claude to clone the hero section from [augmentcode.com](https://www.augmentcode.com/). The agent spawned a stealth browser, navigated to the site, extracted the complete element via CDP (styles, structure, assets), and generated a pixel-accurate HTML recreation with responsive design and animations. The entire process took under two minutes of conversation.

[View the recreation](demo/augment-hero-recreation.html) | [Full walkthrough](demo/augment-hero-clone.md)

---

## Roadmap

See the live plan in [ROADMAP.md](ROADMAP.md). Contributions welcome.

---

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and open a PR. First-time contributors welcome.

If this project saves you time, star the repo. It helps more than you think.

---

## Support

If this tool saved you time or made you money, consider supporting development:

- [Buy me a coffee](https://buymeacoffee.com/vibheksoni)

---

## License

MIT - see [LICENSE](LICENSE).
