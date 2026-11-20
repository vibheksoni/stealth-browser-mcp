<div align="center">

<img src="https://raw.githubusercontent.com/vibheksoni/stealth-browser-mcp/master/media/UndetectedStealthBrowser-2.png" alt="Stealth Browser MCP logo" width="200"/>

# Stealth Browser MCP

**Stealth browser automation for MCP-compatible AI agents.**

Navigate Cloudflare challenges, anti-bot checks, and login walls with real Chrome-family browser instances powered by [nodriver](https://github.com/ultrafunkamsterdam/nodriver), Chrome DevTools Protocol, and [FastMCP](https://github.com/jlowin/fastmcp).

[![MCP](https://img.shields.io/badge/MCP-Compatible-blue?style=flat-square)](https://modelcontextprotocol.io)
[![PyPI](https://img.shields.io/pypi/v/browserstealthmcp?style=flat-square)](https://pypi.org/project/browserstealthmcp/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/pyproject.toml)
[![Stars](https://img.shields.io/github/stars/vibheksoni/stealth-browser-mcp?style=flat-square)](https://github.com/vibheksoni/stealth-browser-mcp/stargazers)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/CONTRIBUTING.md)

</div>

---

## Sponsors

<a href="https://go.nodemaven.com/stealthbrowsermcpreadmeoct"><img src="https://raw.githubusercontent.com/vibheksoni/stealth-browser-mcp/master/media/nodemaven-sponsor.png" alt="NodeMaven" width="100%"></a>

[NodeMaven](https://go.nodemaven.com/stealthbrowsermcpreadmeoct) — The most efficient proxy provider for Web Scraping and Automation with the Highest Quality IP on the market.

**Why NodeMaven?**

- ZIP targeting
- 99.9% uptime
- IP filtering: all proxies have fraud score <97%
- No KYC required
- Unique free tools: Proxy Bandwidth Checker, Meta Tag Checker, IP Lookup and others!

**Special codes for Stealth-browser-mcp users**

- `STEALTHMCP35` — 35% off to Mobile and Residential Proxies
- `STEALTHMCP40` — 40% off to ISP (Static) Proxies
- `STEALTHMCP2X`: 2X traffic for Black Friday

[Get started with NodeMaven](https://go.nodemaven.com/stealthbrowsermcpreadmeoct)

> Sponsored placement. NodeMaven is a paid sponsor of this project.

---

## Table of Contents

- [Sponsors](#sponsors)
- [Demo](#demo)
- [Features](#features)
- [Quickstart](#quickstart)
- [Agent Skill](#agent-skill)
- [Agent Guide](#agent-guide)
- [Trust Model](#trust-model)
- [Modular Architecture](#modular-architecture)
  - [Tool Surface](#tool-surface)
- [Server Configuration](#server-configuration)
- [Security](#security)
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
<img src="https://raw.githubusercontent.com/vibheksoni/stealth-browser-mcp/master/media/showcase-demo-full.gif" alt="Stealth Browser MCP demo" width="800" style="border-radius: 8px;">
<br><br>
<a href="https://raw.githubusercontent.com/vibheksoni/stealth-browser-mcp/master/media/Showcase%20Stealth%20Browser%20Mcp.mp4" download>
  <img src="https://img.shields.io/badge/Watch%20HD%20Video-red?style=for-the-badge&logo=video&logoColor=white" alt="Watch HD Video">
</a>
</div>

*Stealth Browser MCP passing a Cloudflare challenge, cloning UI elements, and inspecting network traffic through AI chat commands.*

---

## Features

- **Anti-bot resistance** - Has passed Cloudflare and Queue-It style challenges in testing; results vary by site, region, browser version, and detector version.
- **123 tools across 11 sections** - From basic navigation to advanced CDP function execution.
- **Modular loading** - Run the 33-tool core surface (default) or the full 123-tool suite; disable sections you do not need.
- **Pixel-accurate element cloning** - Extract complete elements with CSS, DOM structure, events, animations, and assets via CDP.
- **Network inspection** - Inspect requests, responses, headers, payloads, and captured bodies through your AI agent.
- **Dynamic hook system** - Restricted Python hooks can intercept, block, redirect, fulfill, or modify request/response flows.
- **CDP execution** - Run JavaScript, direct CDP commands, and pre-document scripts through trusted local MCP clients.
- **Instant text input** - Paste large content via CDP, type with human-like keystrokes, or press real keys with trusted CDP key events.
- **Cross-platform** - Windows, macOS, Linux, Docker, and CI environments with automatic browser/platform detection.
- **Browser support** - Chrome, Chromium, and Microsoft Edge with automatic executable discovery.
- **Non-intrusive window modes** - Background automation with `offscreen`, `minimized`, `headless`, or `normal` windows that never steal OS focus ([docs/WINDOW_MODES.md](https://github.com/arpeake/stealth-browser-mcp/blob/master/docs/WINDOW_MODES.md)).
- **Enterprise and managed profiles** - Guide for corporate Google Workspace profiles, policy checks, and CDP restrictions ([docs/MANAGED_PROFILES.md](https://github.com/arpeake/stealth-browser-mcp/blob/master/docs/MANAGED_PROFILES.md)).
- **Attach to running Chrome** - Connect to an already running Chrome browser via remote debugging port to automate within corporate or managed profiles ([docs/MANAGED_PROFILES.md](https://github.com/arpeake/stealth-browser-mcp/blob/master/docs/MANAGED_PROFILES.md)).
- **Clean MCP integration** - Works through standard MCP clients without custom brokers.

---

## Quickstart

### 1. Install

Requires Python 3.10+ and Chrome, Chromium, or Microsoft Edge.

**With pipx (recommended):**

```bash
pipx install browserstealthmcp
```

This puts a `stealth-browser-mcp` command on your `PATH`. Upgrade with `pipx upgrade browserstealthmcp`. `uvx browserstealthmcp` runs it without installing. To run the latest commit instead of a release, use `pipx install git+https://github.com/vibheksoni/stealth-browser-mcp.git`.

The PyPI package is named `browserstealthmcp` because `stealth-browser-mcp` on PyPI belongs to a different, unrelated project. `pipx install stealth-browser-mcp` installs that project, not this one.

**From a clone (for development):**

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

**Installed with pipx:**

```bash
claude mcp add stealth-browser-mcp -- stealth-browser-mcp
```

Or in any client's JSON config:

```json
{
  "mcpServers": {
    "stealth-browser-mcp": {
      "command": "stealth-browser-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

If your client cannot find the command, use the full path printed by `pipx list`. With uv you can skip the install and use `"command": "uvx", "args": ["browserstealthmcp"]`.

**From a clone with Claude Code CLI:**

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

## Agent Guide

Stealth Browser MCP provides built-in agent instructions at connect, an input playbook resource, and an interactive prompt to guide AI agents on reliable form input and verification:

- **Instructions at connect**: Delivered during client initialization, summarizing background offscreen execution (`window="offscreen"`), focus guarantees ([docs/WINDOW_MODES.md](https://github.com/arpeake/stealth-browser-mcp/blob/master/docs/WINDOW_MODES.md)), enterprise managed profiles ([docs/MANAGED_PROFILES.md](https://github.com/arpeake/stealth-browser-mcp/blob/master/docs/MANAGED_PROFILES.md)), input primitives, verification rules, and tool surface categories.
- **Input playbook resource (`stealth://guide/input`)**: A comprehensive Markdown resource documenting exact recipes for single-line text, multiline textareas, contenteditable rich text, numeric validation, native and custom dropdowns, tag chips, and file uploads.
- **Interactive recipe prompt (`how_to_input`)**: An MCP prompt that returns recommended tool calls, argument examples, verification methods, and pitfalls for a specific `field_type` (e.g. `text`, `textarea`, `rich_text`, `number`, `select`, `dropdown`, `chips`, `file`), or the full summary when omitted.

---

## Trust Model

This server exposes powerful local browser-control primitives: CDP execution, page JavaScript execution, cookie/storage access, network interception, dynamic hooks, header mutation, navigation, and file uploads from allowlisted local paths.

Treat the MCP client or agent as the security principal. The recommended deployment is local `stdio` transport with a trusted desktop MCP client. HTTP transport always requires a bearer token, binds to loopback by default, and rejects unexpected Host and Origin values. Do not bind HTTP off loopback. See [docs/SECURITY.md](https://github.com/arpeake/stealth-browser-mcp/blob/master/docs/SECURITY.md).

---

## Modular Architecture

Choose exactly what functionality you need. Run the 33-tool core surface (default) or the full 123-tool suite.

Stealth Browser MCP separates tool visibility (--tools core|full) vs availability (--minimal, --xpool-safe, --disable-*):
- **Visibility (`--tools core|full`)**: Governs which tools are advertised in `tools/list`. In `core` mode (the default), 33 essential tools are listed while extended tools remain callable by name and expandable on demand. In `full` mode, all registered tools are listed.
- **Availability (`--minimal`, `--xpool-safe`, `--disable-*`)**: Governs which tools exist at all. Availability flags unregister tools so they are completely removed.

| Mode / Flag | Tools | Type | Description |
|-------------|-------|------|-------------|
| **Core** (default) | 33 | Visibility | Essential navigation, attach/spawn (`attach_browser`, `spawn_browser`), input, tabs, cookies, and screenshots (extended tools remain callable by name) |
| **Full** (`--tools full`) | 123 | Visibility | Advertises all 123 tools in `tools/list` |
| **Minimal** (`--minimal`) | 38 | Availability | Keeps only browser management and element interaction (disables other sections) |
| **Xpool safe** (`--xpool-safe`) | 105 | Availability | Disables CDP function tools that trigger `Runtime.enable` |
| **Custom** (`--disable-*`) | Variable | Availability | Disables specific sections entirely |

> Note: `--agent` and `STEALTH_MCP_PROFILE=agent` are deprecated aliases of the default `--tools core` visibility profile. They are accepted without error and remove nothing.

### Tool Surface

By default, Stealth Browser MCP operates in **core mode** to minimize connect handshake overhead. At connect time, MCP clients fetch parameter schemas and descriptions for all visible tools:
- **Full surface (123 tools, uncompacted)**: ~105K characters (~26K tokens)
- **Compacted surface**: ~80K characters (~20K tokens)
- **Core surface (default, 33 tools)**: <40K characters (<10K tokens)

In core mode, 33 essential tools (including `attach_browser` and `spawn_browser`) are listed. Extended tools remain fully functional and callable by name at any time without blocking.

- **Dynamic expansion**: Call `expand_tools(category="input")` to add a specific category's tools to `tools/list` for the current session, or `expand_tools(category="all")` to reveal all tools.
- **Catalog resource**: Read `stealth://tools/reference` to view a complete Markdown catalog of all tools grouped by category with one-line descriptions.
- **Category discovery**: Call `list_tool_categories()` to get tool counts, category memberships, and the active mode.
- **Mode selection**: Run with `--tools full` or set `STEALTH_MCP_TOOLS=full` (CLI flag takes precedence) to disable filtering and list all 123 tools by default.
- **Deprecated aliases**: `--agent` and `STEALTH_MCP_PROFILE=agent` are deprecated aliases of the default `--tools core` visibility profile; they accept without error and remove nothing.
- **Compacted tool descriptions**: Tool descriptions and parameter schemas are compacted in place to minimize connect-time token overhead. Verbose docstring sections, redundant schema titles, and default-null entries are stripped while preserving nullable types, argument defaults, and validation.
- **Optional `instance_id`**: For single-browser sessions, `instance_id` is optional across all tools in both visibility profiles. When omitted, server middleware automatically resolves and injects the session's active browser.

```bash
stealth-browser-mcp --minimal
stealth-browser-mcp --disable-cdp-functions --disable-dynamic-hooks
stealth-browser-mcp --list-sections
stealth-browser-mcp --debug
stealth-browser-mcp --transport http --host 127.0.0.1 --port 8000
```

From a clone, `python src/server.py` takes the same flags.

Use `--debug` or set `STEALTH_BROWSER_DEBUG=1` to enable verbose server diagnostics on `stderr`. Normal MCP `stdio` runs stay quiet by default to avoid corrupting JSON-RPC transport output.

**Available sections:**

| Section | Tools | Description |
|---------|-------|-------------|
| `browser-management` | 20 | Core browser operations |
| `element-interaction` | 19 | Page interaction and manipulation |
| `element-extraction` | 9 | Element cloning and extraction |
| `file-extraction` | 9 | File-based extraction tools |
| `network-debugging` | 11 | Network monitoring and capture |
| `cdp-functions` | 17 | Chrome DevTools Protocol execution |
| `progressive-cloning` | 10 | Advanced element cloning |
| `cookies-storage` | 5 | Cookie management |
| `tabs` | 5 | Tab management |
| `debugging` | 8 | Debug and system tools |
| `dynamic-hooks` | 10 | AI-assisted network hooks |

---

## Server Configuration

These are environment variables for the MCP server process. Set them in your shell, service manager, Docker environment, or MCP client `env` block.

| Variable | Default | Meaning |
|----------|---------|---------|
| `STEALTH_MCP_TOKEN` | unset | Bearer token for HTTP transport. HTTP always requires a token; clients must send `Authorization: Bearer <token>`. If no token is configured, the server writes one to a 0600 file and prints only the path (never the value) to stderr. |
| `STEALTH_MCP_TOKEN_FILE` | unset | Path to a 0600 token file. If this variable is set and the file is missing, empty, unreadable, group/world-readable, or a symlink not owned by the current user, the server exits. |
| `STEALTH_MCP_ALLOWED_HOSTS` | loopback only | Extra `Host` values accepted by HTTP (comma-separated). Also `--allowed-host`. |
| `STEALTH_MCP_ALLOWED_ORIGINS` | empty | `Origin` values accepted by HTTP (comma-separated). Default: any `Origin` is rejected. Also `--allowed-origin`. |
| `STEALTH_BROWSER_MCP_AUTH_TOKEN` | unset | Legacy alias for `STEALTH_MCP_TOKEN`. |
| `MCP_AUTH_TOKEN` | unset | Legacy alias for `STEALTH_MCP_TOKEN`. |
| `BROWSER_IDLE_TIMEOUT` | `600` | Global idle timeout in seconds before an unused browser instance is auto-closed. Set `0` to disable idle reaping globally. |
| `BROWSER_IDLE_REAPER_INTERVAL` | `60` | Background reaper check interval in seconds. |
| `BROWSER_TAB_RECYCLE_NAVIGATIONS` | `0` | Replace the main tab with a fresh one after this many navigations. `0` keeps the same tab, which preserves history and session storage. |
| `BROWSER_ORPHAN_PROFILE_MAX_AGE` | `21600` | Startup cleanup threshold in seconds for stale `uc_*` temp profiles. Set `0` to disable this startup sweep. |
| `STEALTH_BROWSER_EXECUTABLE` | auto-detect | Path to the Chrome, Chromium, or Edge executable to launch instead of the auto-detected one. |
| `BROWSER_FILE_UPLOAD_ALLOWED_DIRS` | repo root or data dir | Directories that `file_upload()` may read from. Separate multiple roots with `;` on Windows or `:` on macOS/Linux. Defaults to the repo root for a clone and to the `uploads` folder in the data directory for an installed package. |
| `STEALTH_BROWSER_MCP_DATA_DIR` | repo root or user data dir | Where element clones and large responses are written. Defaults to the repo root for a clone, and for an installed package to `%LOCALAPPDATA%\stealth-browser-mcp` on Windows, `~/Library/Application Support/stealth-browser-mcp` on macOS, or `$XDG_DATA_HOME/stealth-browser-mcp` (`~/.local/share/stealth-browser-mcp`) on Linux. |
| `STEALTH_BROWSER_DEBUG` | `0` | Enable verbose debug logging to `stderr` when set to `1`. |
| `DEBUG` | `0` | Legacy alias for `STEALTH_BROWSER_DEBUG`. |
| `XPOOL_SAFE_MODE` | `0` | Disable the `cdp-functions` section at startup. |
| `PORT` | `8000` | Default HTTP port when `--port` is not provided. |
| `STEALTH_ALLOW_CODE_HOOKS` | unset | Allow `create_dynamic_hook` / `validate_hook_function` to compile caller Python on the server. Off by default. The server refuses to start if this is set and HTTP is bound off loopback. Use `create_simple_dynamic_hook` instead. |
| `STEALTH_MANAGED_PROFILES_ROOT` | `data_dir()` | Directory that `spawn_browser(user_data_dir=...)` must stay under. Paths outside this root, including symlink escapes, are rejected. Also used as the default when a relative `user_data_dir` is given. |
| `STEALTH_ALLOW_FILE_URLS` | unset | Allow `navigate` / `new_tab` to open `file:`, `chrome:`, or `devtools:` URLs. Off by default because those schemes can read host files or browser internals. |
| `STEALTH_ALLOW_RAW_CDP` | unset | Allow `execute_cdp_command` to call methods outside the allowlist. Off by default. Navigation URLs still go through the file:/chrome: block. `setDownloadBehavior` still requires a `download_path` under the data directory. The server refuses to start if this is set and HTTP is bound off loopback. |
| `STEALTH_ALLOW_ATTACH` | unset | Allow `attach_browser` in HTTP mode. Off by default. When on, attach is limited to a Chrome this server spawned for the calling session, or to an external browser this server does not already own. stdio mode does not need this flag. |
| `STEALTH_ALLOW_REMOTE_ATTACH` | unset | Allow `attach_browser` to a non-loopback host. Off by default. Ignored when HTTP is bound off loopback. In HTTP mode, `STEALTH_ALLOW_ATTACH=1` is also required. |
| `STEALTH_EXPORT_DIRS` | unset | Extra directories `write` paths may use (screenshots, exports). `BROWSER_FILE_UPLOAD_ALLOWED_DIRS` is read-only and does not widen writes. |
| `STEALTH_MCP_SESSION_TTL` | `600` | Seconds of HTTP inactivity before a session's browsers are closed. `0` disables this expiry. |
| `STEALTH_ALLOW_SEED_REFRESH` | unset | Allow `refresh_seed` in HTTP mode to copy from a real browser profile or `source_dir`. Off by default. Refused off loopback and behind a proxy. `source_dir` must stay under a known browser profile root. stdio does not need this flag. |
| `STEALTH_ALLOW_SEED_PASSWORDS` | unset | Allow `refresh_seed(include_passwords=true)` or `include_cards=true`. Off by default. Also requires `STEALTH_ALLOW_SEED_REFRESH=1` in HTTP mode. Refused off loopback and behind a proxy. |

**PowerShell example:**

```powershell
$env:STEALTH_MCP_TOKEN='replace-with-a-long-random-token'
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
        "STEALTH_MCP_TOKEN": "replace-with-a-long-random-token",
        "BROWSER_IDLE_TIMEOUT": "900",
        "BROWSER_FILE_UPLOAD_ALLOWED_DIRS": "C:\\Users\\me\\uploads;C:\\Users\\me\\Documents"
      }
    }
  }
}
```

HTTP binds to `127.0.0.1` by default. Requests whose `Host` is not loopback are rejected unless listed in `--allowed-host`. Requests that include an `Origin` header are rejected unless listed in `--allowed-origin` (agent clients typically send no `Origin`). `GET /healthz` is unauthenticated after the Host check.

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
- `spawn_browser(window="offscreen")` configures window mode (`offscreen`, `minimized`, `headless`, `normal`); see [docs/WINDOW_MODES.md](https://github.com/arpeake/stealth-browser-mcp/blob/master/docs/WINDOW_MODES.md).

---

## Security

This server is for a single-user host. Spawned Chrome exposes a loopback DevTools port with no DevTools authentication, so other local users can attach to it. Do not run it on a shared login or a shared netns. See [docs/SECURITY.md](https://github.com/arpeake/stealth-browser-mcp/blob/master/docs/SECURITY.md).

---

## Toolbox

<details>
<summary><strong>Browser Management (9)</strong></summary>

| Tool | Description |
|------|-------------|
| `spawn_browser()` | Create a stealth browser instance |
| `attach_browser()` | Attach to an already running Chrome browser via remote debugging port |
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

`take_screenshot` returns an MCP image with metadata (format, size, scale, quality, byte count, SHA-256), so remote clients can show it without reading the server's disk. Large captures, such as long full-page screenshots, are scaled down to fit client limits (8000 px per side, about 5 MB), and the metadata reports the original size. Pass `format="jpeg"` with a lower `quality`, or `max_width`, for smaller images, `selector` to capture an element, `clip` for a page region, or `file_path` to save the full-resolution image on the server.

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

Detection results are point-in-time and depend on site policy, region, browser version, IP reputation, and detector version. See [STEALTH_TESTS.md](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/STEALTH_TESTS.md) for weekly automated results from the benchmark in `benchmarks/stealth/`, which runs on Linux, Windows, and macOS in GitHub Actions, plus the earlier manual snapshot.

| Feature | Stealth Browser MCP | Playwright MCP |
|---------|---------------------|----------------|
| Cloudflare / Queue-It style checks | Passes common JS challenges in testing | Commonly blocked |
| Hardened sites | Uses real Chrome-family browser instances through nodriver | Frequently flagged as automation |
| Login-walled pages | Can inspect loaded content and overlays through CDP | Often requires manual scripting |
| UI element cloning | CDP-accurate extraction | Limited |
| Network debugging | Full request/response inspection through AI tools | Basic |
| API reverse engineering | Payload inspection through chat | Manual tools only |
| Dynamic hook system | Restricted Python hooks for real-time interception | Not available |
| Modular architecture | 11 sections, 33-123 tools | Fixed tool surface |
| Total tools | 123 customizable tools | About 20 |

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

**macOS disk fills up with `code_sign_clone` folders**
Older versions let Chrome copy its app bundle into `com.google.Chrome.code_sign_clone` at every launch, and the copy stayed behind when an instance crashed or was killed. The server now launches Chrome with `MacAppCodeSignClone` turned off, so no new copies are made. To remove copies left by older versions, quit Chrome and run `rm -rf /private/var/folders/*/*/X/com.google.Chrome.code_sign_clone` (use `com.microsoft.edgemac.code_sign_clone` for Edge).

**Too many tools cluttering the AI chat**
Use core mode (default, 33 tools), `--minimal` for 38 tools, `--xpool-safe` for xpool-compatible operation, or selectively disable sections:

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
<img src="https://raw.githubusercontent.com/vibheksoni/stealth-browser-mcp/master/media/AugmentHeroClone.PNG" alt="Augment Code Hero Recreation" width="700" style="border-radius: 8px;">
</div>

**Augment Code hero clone** - A user asked Claude to clone the hero section from [augmentcode.com](https://www.augmentcode.com/). The agent spawned a stealth browser, navigated to the site, extracted the complete element via CDP (styles, structure, assets), and generated a pixel-accurate HTML recreation with responsive design and animations. The entire process took under two minutes of conversation.

[View the recreation](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/demo/augment-hero-recreation.html) | [Full walkthrough](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/demo/augment-hero-clone.md)

---

## Roadmap

See the live plan in [ROADMAP.md](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/ROADMAP.md). Contributions welcome.

---

## Contributing

Read [CONTRIBUTING.md](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/CONTRIBUTING.md) and open a PR. See [docs/TESTING.md](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/docs/TESTING.md) for the test matrix and gated suite instructions. First-time contributors welcome.

If this project saves you time, star the repo. It helps more than you think.

---

## Support

If this tool saved you time or made you money, consider supporting development:

- [Buy me a coffee](https://buymeacoffee.com/vibheksoni)

---

## License

MIT - see [LICENSE](https://github.com/vibheksoni/stealth-browser-mcp/blob/master/LICENSE).
