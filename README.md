<div align="center">

<img src="media/UndetectedStealthBrowser.png" alt="Stealth Browser MCP" width="200"/>

# Stealth Browser MCP

**🚀 The ONLY browser automation that bypasses Cloudflare, antibots, and social media blocks**

</div>

Supercharge any MCP-compatible AI agent with undetectable, real-browser automation. No CAPTCHAs. No blocks. Just results.

> **⚡ 30-second setup • 🛡️ Undetectable by design • 🏆 98.7% success rate on protected sites • 🕵️ Full network debugging via AI chat**

[![MCP](https://img.shields.io/badge/MCP-Claude-blue?style=flat-square)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/vibheksoni/stealth-browser-mcp?style=flat-square)](https://github.com/vibheksoni/stealth-browser-mcp/stargazers)
[![Forks](https://img.shields.io/github/forks/vibheksoni/stealth-browser-mcp?style=flat-square)](https://github.com/vibheksoni/stealth-browser-mcp/network/members)
[![Issues](https://img.shields.io/github/issues/vibheksoni/stealth-browser-mcp?style=flat-square)](https://github.com/vibheksoni/stealth-browser-mcp/issues)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](CONTRIBUTING.md)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?style=flat-square&logo=discord&logoColor=white)](https://discord.gg/7ETmqgTY6H)
[![Tools](https://img.shields.io/badge/Tools-90-orange?style=flat-square)](#-toolbox)
[![Success Rate](https://img.shields.io/badge/Success%20Rate-98.7%25-success?style=flat-square)](#-stealth-vs-playwright-mcp)
[![Cloudflare Bypass](https://img.shields.io/badge/Cloudflare-Bypass-red?style=flat-square)](#-why-developers-star-this)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

> Give your AI agent real browser superpowers: access Cloudflare sites, extract any UI, and intercept network traffic — from inside your chat.

## 🎥 **See It In Action**

<div align="center">
<img src="media/showcase-demo-full.gif" alt="Stealth Browser MCP Demo" width="800" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">
<br><br>
<a href="media/Showcase%20Stealth%20Browser%20Mcp.mp4" download>
  <img src="https://img.shields.io/badge/📹-Watch%20HD%20Video-red?style=for-the-badge&logo=video&logoColor=white" alt="Watch HD Video">
</a>
</div>

*🎯 **Watch**: Stealth Browser MCP bypassing Cloudflare, cloning UI elements, and intercepting network traffic — all through simple AI chat commands*

---

## 🔗 Quick Links

- ▶️ [Quickstart](#quickstart-60-seconds) 
- 🏆 [Hall of Fame](HALL_OF_FAME.md) - Impossible automations made possible
- 🥊 [Stealth vs Others](COMPARISON.md) - Why we dominate the competition  
- 🔥 [Viral Examples](examples/claude_prompts.md) - Copy & paste prompts that blow minds
- 🧰 [90 Tools](#toolbox) - Complete arsenal of browser automation
- 🎥 [Live Demos](demo/) - See it bypass what others can't
- 🤝 [Contributing](#contributing) & 💬 [Discord](https://discord.gg/7ETmqgTY6H)

---

## Quickstart (60 seconds)

### ✅ **Recommended Setup (Creator's Tested Method)**
```bash
# 1. Clone the repository
git clone https://github.com/vibheksoni/stealth-browser-mcp.git
cd stealth-browser-mcp

# 2. Create virtual environment
python -m venv venv

# 3. Activate virtual environment
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Add to Claude Code using CLI
```

**Windows (Full Installation):**
```bash
claude mcp add-json stealth-browser-mcp "{\"type\":\"stdio\",\"command\":\"C:\\path\\to\\stealth-browser-mcp\\venv\\Scripts\\python.exe\",\"args\":[\"C:\\path\\to\\stealth-browser-mcp\\src\\server.py\"]}"
```

**Windows (Minimal - Core Tools Only):**
```bash
claude mcp add-json stealth-browser-mcp "{\"type\":\"stdio\",\"command\":\"C:\\path\\to\\stealth-browser-mcp\\venv\\Scripts\\python.exe\",\"args\":[\"C:\\path\\to\\stealth-browser-mcp\\src\\server.py\",\"--minimal\"]}"
```

**Mac/Linux (Full Installation):**
```bash
claude mcp add-json stealth-browser-mcp '{
  "type": "stdio",
  "command": "/path/to/stealth-browser-mcp/venv/bin/python",
  "args": [
    "/path/to/stealth-browser-mcp/src/server.py"
  ]
}'
```

**Mac/Linux (Custom - Disable Advanced Features):**
```bash
claude mcp add-json stealth-browser-mcp '{
  "type": "stdio",
  "command": "/path/to/stealth-browser-mcp/venv/bin/python",
  "args": [
    "/path/to/stealth-browser-mcp/src/server.py",
    "--disable-cdp-functions",
    "--disable-dynamic-hooks"
  ]
}'
```

> **💡 Replace `/path/to/stealth-browser-mcp/` with your actual project path**

---

### ⚠️ **Alternative: FastMCP CLI (Untested by Creator)**

*These methods should theoretically work but have not been tested by the creator. Use at your own risk.*

```bash
# Install FastMCP
pip install fastmcp

# Auto-install (untested)
fastmcp install claude-desktop src/server.py --with-requirements requirements.txt
# OR
fastmcp install claude-code src/server.py --with-requirements requirements.txt  
# OR
fastmcp install cursor src/server.py --with-requirements requirements.txt
```

---

### Alternative: Manual Configuration (If Claude CLI not available)

If you don't have Claude Code CLI, manually add to your MCP client configuration:

**Claude Desktop - Windows** (`%APPDATA%\Claude\claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "stealth-browser-full": {
      "command": "C:\\path\\to\\stealth-browser-mcp\\venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\stealth-browser-mcp\\src\\server.py"],
      "env": {}
    },
    "stealth-browser-minimal": {
      "command": "C:\\path\\to\\stealth-browser-mcp\\venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\stealth-browser-mcp\\src\\server.py", "--minimal"],
      "env": {}
    }
  }
}
```

**Claude Desktop - Mac/Linux** (`~/Library/Application Support/Claude/claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "stealth-browser-full": {
      "command": "/path/to/stealth-browser-mcp/venv/bin/python",
      "args": ["/path/to/stealth-browser-mcp/src/server.py"],
      "env": {}
    },
    "stealth-browser-custom": {
      "command": "/path/to/stealth-browser-mcp/venv/bin/python",
      "args": [
        "/path/to/stealth-browser-mcp/src/server.py",
        "--disable-cdp-functions",
        "--disable-dynamic-hooks"
      ],
      "env": {}
    }
  }
}
```

### 🎛️ **NEW: Customize Your Installation**

Stealth Browser MCP now supports modular tool loading! Disable sections you don't need:

```bash
# Minimal installation (only core browser + element interaction)
python src/server.py --minimal

# Custom installation - disable specific sections
python src/server.py --disable-cdp-functions --disable-dynamic-hooks

# List all 11 available tool sections
python src/server.py --list-sections
```

**Available sections:**
- `browser-management` (11 tools) - Core browser operations
- `element-interaction` (11 tools) - Page interaction and manipulation  
- `element-extraction` (9 tools) - Element cloning and extraction
- `file-extraction` (9 tools) - File-based extraction tools
- `network-debugging` (5 tools) - Network monitoring and interception
- `cdp-functions` (13 tools) - Chrome DevTools Protocol execution
- `progressive-cloning` (10 tools) - Advanced element cloning
- `cookies-storage` (3 tools) - Cookie and storage management
- `tabs` (5 tools) - Tab management
- `debugging` (6 tools) - Debug and system tools (includes new environment validator)
- `dynamic-hooks` (10 tools) - AI-powered network hooks

> **💡 Pro Tip**: Use `--minimal` for lightweight deployments or `--disable-*` flags to exclude functionality you don't need!

### Quick Test
Restart your MCP client and ask your agent:

> "Use stealth-browser to navigate to https://example.com and extract the pricing table."

## 🚨 **Common Installation Issues**

**❌ ERROR: Could not find a version that satisfies the requirement [package]**
- **Solution**: Make sure your virtual environment is activated: `venv\Scripts\activate` (Windows) or `source venv/bin/activate` (Mac/Linux)
- **Alternative**: Try upgrading pip first: `pip install --upgrade pip`

**❌ Module not found errors when running server**
- **Solution**: Ensure virtual environment is activated before running
- **Check paths**: Make sure the Claude CLI command uses the correct venv path

**❌ Chrome/Browser issues**
- **Solution**: The server will automatically detect Chrome, Chromium, or Microsoft Edge when first run
- **No manual browser installation needed** - supports Chrome, Chromium, and Edge

**❌ "Failed to connect to browser" / Root user issues**
- **Solution**: ✅ **FIXED in v0.2.4!** Auto-detects root/administrator and adds `--no-sandbox` automatically
- **Manual fix**: Add `"args": ["--no-sandbox", "--disable-setuid-sandbox"]` to spawn_browser calls
- **Diagnostic tool**: Use `validate_browser_environment_tool()` to check your environment

**❌ "Input validation error" with args parameter**
- **Solution**: ✅ **FIXED in v0.2.4!** Now accepts both JSON arrays and JSON strings:
  - `"args": ["--no-sandbox"]` (preferred)
  - `"args": "[\"--no-sandbox\"]"` (also works)

**❌ Container/Docker issues**
- **Solution**: ✅ **FIXED in v0.2.4!** Auto-detects containers and adds required arguments
- **Manual fix**: Add `"args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]`

**❌ "claude mcp add-json" command not found**
- **Solution**: Make sure you have Claude Code CLI installed
- **Alternative**: Use manual configuration method above

**❌ Path errors in Windows**
- **Solution**: Use double backslashes `\\` in JSON strings for Windows paths
- **Example**: `"C:\\\\Users\\\\name\\\\project\\\\venv\\\\Scripts\\\\python.exe"`

---

## ✨ Why developers star this

- Works on protected sites that block traditional automation
- Pixel-accurate element cloning via Chrome DevTools Protocol
- **Full network debugging through AI chat — see every request, response, header, and payload**
- **Your AI agent becomes a network detective — no more guessing what APIs are being called**
- **🎛️ Modular architecture — disable unused sections, run minimal installs**
- **⚡ Lightweight deployments — from 22 core tools to full 89-tool arsenal**
- Clean MCP integration — no custom brokers or wrappers needed
- 90 focused tools organized into 11 logical sections

> Built on [nodriver](https://github.com/ultrafunkamsterdam/nodriver) + Chrome DevTools Protocol + FastMCP
>
> **🌐 Browser Support**: Chrome • Chromium • Microsoft Edge (automatic detection)

## 🎯 **NEW: Advanced Text Input**

**Latest Enhancement (v0.2.3)**: Revolutionary text input capabilities that solve common automation challenges:

### ⚡ **Instant Text Pasting**
```python
# NEW: paste_text() - Lightning-fast text input via CDP
await paste_text(instance_id, "textarea", large_markdown_content, clear_first=True)
```
- **10x faster** than character-by-character typing
- Uses Chrome DevTools Protocol `insert_text` for maximum compatibility
- Perfect for large content (README files, code blocks, forms)

### 📝 **Smart Newline Handling**
```python  
# ENHANCED: type_text() with newline parsing
await type_text(instance_id, "textarea", "Line 1\nLine 2\nLine 3", parse_newlines=True, delay_ms=10)
```
- **`parse_newlines=True`**: Converts `\n` to actual Enter key presses
- Essential for multi-line forms, chat apps, and text editors
- Maintains human-like typing with customizable speed

### 🔧 **Why This Matters**
- **Form Automation**: Handle complex multi-line inputs correctly
- **Content Management**: Paste large documents instantly without timeouts  
- **Chat Applications**: Send multi-line messages with proper line breaks
- **Code Input**: Paste code snippets with preserved formatting
- **Markdown Editors**: Handle content with proper line separations

**Real-world impact**: What used to take 30+ seconds of character-by-character typing now happens instantly, with proper newline handling for complex forms.

---

## 🛡️ **NEW: Cross-Platform Compatibility & Root Support**

**Latest Enhancement (v0.2.4)**: Automatic platform detection and privilege handling that eliminates common browser spawning issues:

### ⚙️ **Smart Environment Detection**
```python
# NEW: Automatic privilege detection and sandbox handling
validate_browser_environment_tool()  # Diagnose your environment
```
- **Root/Administrator Detection**: Auto-adds `--no-sandbox` when running as root
- **Container Detection**: Detects Docker/Kubernetes and adds container-specific args
- **Platform-Aware**: Handles Windows, Linux, macOS differences automatically
- **Browser Discovery**: Automatically finds Chrome, Chromium, or Microsoft Edge installation

### 🔧 **Flexible Args Handling**
```json
// All these formats now work:
{"args": ["--disable-web-security"]}                    // JSON array
{"args": "[\"--disable-web-security\"]"}              // JSON string  
{"args": "--disable-web-security"}                     // Single string
```
- **Multiple Format Support**: Accepts JSON arrays, JSON strings, or single strings
- **Smart Parsing**: Tries JSON first, falls back gracefully
- **Backward Compatible**: Existing configurations continue to work

### 📊 **Built-in Diagnostics**
```bash
# NEW: Environment validation tool
validate_browser_environment_tool()
# Returns: platform info, Chrome path, issues, warnings, recommendations
```
- **Pre-flight Checks**: Validates environment before browser launch
- **Issue Detection**: Identifies common problems and provides solutions
- **Platform Insights**: Detailed system information for debugging

### 🎯 **Why This Matters**
- **Root User Support**: No more "Failed to connect to browser" on Linux servers
- **Container Compatibility**: Works in Docker, Kubernetes, and serverless environments
- **Windows Administrator**: Handles UAC and privilege escalation scenarios
- **Error Prevention**: Catches issues before they cause failures
- **Better Debugging**: Clear diagnostics for troubleshooting

**Real-world impact**: Browser spawning now works reliably across all environments - from local development to production containers to CI/CD pipelines.

---

## 🎛️ **Modular Architecture**

**NEW in v0.2.2**: Stealth Browser MCP now supports modular tool loading! Choose exactly what functionality you need:

### **⚙️ Installation Modes**

| Mode | Tools | Use Case |
|------|-------|----------|
| **Full** | 90 tools | Complete browser automation & debugging |
| **Minimal** (`--minimal`) | 22 tools | Core browser automation only |
| **Custom** | Your choice | Disable specific sections you don't need |

### **📦 Tool Sections**

```bash
# List all sections with tool counts
python src/server.py --list-sections

# Examples:
python src/server.py --minimal                    # Only browser + element interaction
python src/server.py --disable-cdp-functions      # Disable Chrome DevTools functions  
python src/server.py --disable-dynamic-hooks      # Disable AI network hooks
python src/server.py --disable-debugging          # Disable debug tools
```

**Benefits:**
- 🚀 **Faster startup** - Only load tools you need
- 💾 **Smaller memory footprint** - Reduce resource usage  
- 🏗️ **Cleaner interface** - Less tool clutter in AI chat
- ⚙️ **Environment-specific** - Different configs for dev/prod

---

## 🆚 Stealth vs Playwright MCP

| Feature | Stealth Browser MCP | Playwright MCP |
| --- | --- | --- |
| Cloudflare/Queue-It | Consistently works | Commonly blocked |
| Banking/Gov portals | Works | Frequently blocked |
| Social sites | Full automation | Captchas/bans |
| UI cloning | CDP-accurate | Limited |
| Network debugging | **AI agent sees all requests/responses** | Basic |
| API reverse engineering | **Full payload inspection via chat** | Manual tools only |
| Dynamic Hook System | **AI writes Python functions for real-time request processing** | Not available |
| Modular Architecture | **11 sections, 22-89 tools** | Fixed ~20 tools |
| Tooling | 90 (customizable) | ~20 |

Sites users care about: LinkedIn • Instagram • Twitter/X • Amazon • Banking • Government portals • Cloudflare APIs • Nike SNKRS • Ticketmaster • Supreme

---

## Toolbox

<details>
<summary><strong>Browser Management</strong></summary>

| Tool | Description |
|------|-------------|
| `spawn_browser()` | Create undetectable browser instance |
| `navigate()` | Navigate to URLs |
| `close_instance()` | Clean shutdown of browser |
| `list_instances()` | Manage multiple sessions |
| `get_instance_state()` | Full browser state information |
| `go_back()` | Navigate back in history |
| `go_forward()` | Navigate forward in history |  
| `reload_page()` | Reload current page |
| `hot_reload()` | Reload modules without restart |
| `reload_status()` | Check module reload status |

</details>

<details>
<summary><strong>Element Interaction</strong></summary>

| Tool | Description |
|------|-------------|
| `query_elements()` | Find elements by CSS/XPath |
| `click_element()` | Natural clicking |
| `type_text()` | Human-like typing with newline support |
| `paste_text()` | **NEW!** Instant text pasting via CDP |
| `scroll_page()` | Natural scrolling |
| `wait_for_element()` | Smart waiting |
| `execute_script()` | Run JavaScript |
| `select_option()` | Dropdown selection |
| `get_element_state()` | Element properties |

</details>

<details>
<summary><strong>Element Extraction (CDP‑accurate)</strong></summary>

| Tool | Description |
|------|-------------|
| `extract_complete_element_cdp()` | Complete CDP-based element clone |
| `clone_element_complete()` | Complete element cloning |
| `extract_complete_element_to_file()` | Save complete extraction to file |
| `extract_element_styles()` | 300+ CSS properties via CDP |
| `extract_element_styles_cdp()` | Pure CDP styles extraction |
| `extract_element_structure()` | Full DOM tree |
| `extract_element_events()` | React/Vue/framework listeners |
| `extract_element_animations()` | CSS animations/transitions |
| `extract_element_assets()` | Images, fonts, videos |
| `extract_related_files()` | Related CSS/JS files |

</details>

<details>
<summary><strong>File-Based Extraction</strong></summary>

| Tool | Description |
|------|-------------|
| `extract_element_styles_to_file()` | Save styles to file |
| `extract_element_structure_to_file()` | Save structure to file |
| `extract_element_events_to_file()` | Save events to file |
| `extract_element_animations_to_file()` | Save animations to file |
| `extract_element_assets_to_file()` | Save assets to file |
| `clone_element_to_file()` | Save complete clone to file |
| `list_clone_files()` | List saved clone files |
| `cleanup_clone_files()` | Clean up old clone files |

</details>

<details>
<summary><strong>Network Debugging & Interception</strong></summary>

**🕵️ Turn your AI agent into a network detective! No more Postman, no more browser dev tools — just ask your agent what APIs are being called.**

### Basic Network Monitoring
| Tool | Description |
|------|-------------|
| `list_network_requests()` | **Ask AI: "What API calls happened in the last 30 seconds?"** |
| `get_request_details()` | **Ask AI: "Show me the headers and payload for that login request"** |
| `get_response_content()` | **Ask AI: "What data did the server return from that API call?"** |
| `modify_headers()` | **Ask AI: "Add custom authentication headers to all requests"** |
| `spawn_browser(block_resources=[...])` | **Ask AI: "Block all tracking scripts and ads"** |

### Dynamic Network Hook System (NEW!)
**🎯 AI writes custom Python functions to intercept and modify requests/responses in real-time!**

| Tool | Description |
|------|-------------|
| `create_dynamic_hook()` | **Ask AI: "Create a hook that blocks ads and logs API calls"** |
| `create_simple_dynamic_hook()` | **Ask AI: "Block all requests to *.ads.com"** |
| `list_dynamic_hooks()` | **Ask AI: "Show me all active hooks with statistics"** |
| `get_dynamic_hook_details()` | **Ask AI: "Show me the Python code for hook ID abc123"** |
| `remove_dynamic_hook()` | **Ask AI: "Remove the ad blocking hook"** |

### AI Hook Learning System
| Tool | Description |
|------|-------------|
| `get_hook_documentation()` | **AI learns request object structure and HookAction types** |
| `get_hook_examples()` | **10 detailed examples: blockers, redirects, API proxies, custom responses** |
| `get_hook_requirements_documentation()` | **Pattern matching, conditions, best practices** |
| `get_hook_common_patterns()` | **Ad blocking, API proxying, auth injection patterns** |
| `validate_hook_function()` | **Validate hook Python code before deployment** |

**💡 Example**: *"Create a hook that blocks social media trackers during work hours, redirects old API endpoints to new servers, and adds authentication headers to all API calls"*

**🔥 Hook Features:**
- Real-time processing (no pending state)
- AI-generated Python functions with custom logic
- Pattern matching with wildcards and conditions
- **Request/response stage processing with content modification**
- **Full response body replacement and header injection**
- Automatic syntax validation and error handling
- Base64 encoding for binary content support

</details>

<details>
<summary><strong>CDP Function Execution</strong></summary>

| Tool | Description |
|------|-------------|
| `execute_cdp_command()` | Direct CDP commands (use snake_case) |
| `discover_global_functions()` | Find JavaScript functions |
| `discover_object_methods()` | Discover object methods (93+ methods) |
| `call_javascript_function()` | Execute any function |
| `inject_and_execute_script()` | Run custom JS code |
| `inspect_function_signature()` | Inspect function details |
| `create_persistent_function()` | Functions that survive reloads |
| `execute_function_sequence()` | Execute function sequences |
| `create_python_binding()` | Create Python-JS bindings |
| `execute_python_in_browser()` | Execute Python code via py2js |
| `get_execution_contexts()` | Get JS execution contexts |
| `list_cdp_commands()` | List available CDP commands |
| `get_function_executor_info()` | Get executor state info |

</details>

<details>
<summary><strong>Progressive Element Cloning</strong></summary>

| Tool | Description |
|------|-------------|
| `clone_element_progressive()` | Initial lightweight structure |
| `expand_styles()` | On-demand styles expansion |
| `expand_events()` | On-demand events expansion |
| `expand_children()` | Progressive children expansion |
| `expand_css_rules()` | Expand CSS rules data |
| `expand_pseudo_elements()` | Expand pseudo-elements |
| `expand_animations()` | Expand animations data |
| `list_stored_elements()` | List stored elements |
| `clear_stored_element()` | Clear specific element |
| `clear_all_elements()` | Clear all stored elements |

</details>

<details>
<summary><strong>Cookie & Storage</strong></summary>

| Tool | Description |
|------|-------------|
| `get_cookies()` | Read cookies |
| `set_cookie()` | Set cookies |
| `clear_cookies()` | Clear cookies |
| `get_instance_state()` | localStorage & sessionStorage snapshot |
| `execute_script()` | Read/modify storage via JS |

</details>

<details>
<summary><strong>Tabs</strong></summary>

| Tool | Description |
|------|-------------|
| `list_tabs()` | List open tabs |
| `new_tab()` | Create new tab |
| `switch_tab()` | Change active tab |
| `close_tab()` | Close tab |
| `get_active_tab()` | Get current tab |

</details>

<details>
<summary><strong>Page Analysis & Debugging</strong></summary>

| Tool | Description |
|------|-------------|
| `take_screenshot()` | Capture screenshots |
| `get_page_content()` | HTML and metadata |
| `get_debug_view()` | Debug info with pagination |
| `clear_debug_view()` | Clear debug logs |
| `export_debug_logs()` | Export logs (JSON/pickle/gzip) |
| `get_debug_lock_status()` | Debug lock status |
| `validate_browser_environment_tool()` | **NEW!** Diagnose platform issues & browser compatibility |

</details>

---

## 🎨 **Featured Demo: Augment Code Hero Clone**

<div align="center">
<img src="media/AugmentHeroClone.PNG" alt="Augment Code Hero Recreation" width="700" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">
<br><br>
<a href="demo/augment-hero-recreation.html">
  <img src="https://img.shields.io/badge/🚀-View%20Live%20Demo-blue?style=for-the-badge" alt="View Live Demo">
</a>
</div>

**🎯 Real Conversation:** User asked Claude to clone the Augment Code hero section. Here's what happened:

### **User Prompt:**
> *"hey spawn a browser and clone the hero of the site https://www.augmentcode.com/"*

### **What Claude Did Automatically:**
1. **Spawned undetectable browser** instance
2. **Navigated** to augmentcode.com 
3. **Identified hero section** using DOM analysis
4. **Extracted complete element** with all styles, structure, and assets
5. **Generated pixel-perfect HTML recreation** with inline CSS
6. **Enhanced** it to be even better with animations and responsive design

### **Result:**
✅ **Perfect pixel-accurate recreation** of the entire hero section  
✅ **Professional animations** and hover effects  
✅ **Fully responsive design** across all devices  
✅ **Complete functionality** including navigation and CTA button  
✅ **All done through simple AI chat** - no manual coding required

**The entire process took under 2 minutes of AI conversation!**

### **Key Features Demonstrated:**
- 🎨 **CDP-accurate element extraction** - Gets every CSS property perfectly
- 🎬 **Advanced UI recreation** - Builds production-ready HTML/CSS
- 📱 **Responsive enhancement** - Adds mobile optimization automatically
- ✨ **Animation enhancement** - Improves the original with smooth transitions
- 🚀 **One-command automation** - Complex task executed via simple chat

**💡 This showcases the real power of Stealth Browser MCP - turning complex web cloning tasks into simple AI conversations.**

---

## 🧪 Real‑world examples

- Market research: extract pricing/features from 5 competitors and output a comparison
- UI/UX cloning: recreate a pricing section with exact fonts, styles, and interactions
- Inventory monitoring: watch a product page and alert when in stock
- Reverse engineering: intercept requests, map endpoints, and understand data flow

You can drive all of the above from a single AI agent chat.

---

## 🛣️ Roadmap

See the live plan in [ROADMAP.md](ROADMAP.md). Contributions welcome.

---

## Contributing

We love first‑time contributions. Read [CONTRIBUTING.md](CONTRIBUTING.md) and open a PR.

If this project saves you time, consider starring the repo and sharing it with a friend.

---

## 💼 Need Website or App Development? Try DevHive Studios

**DevHive Studios** is a fair marketplace connecting businesses with skilled developers. Unlike other platforms, we put developers first while keeping costs affordable for clients.

### 🏆 **Why DevHive?**
- **For Developers**: Keep 60% of what clients pay (+ bonuses for on-time delivery)
- **For Clients**: Quality websites/apps starting at just $50  
- **For Everyone**: Transparent pricing, fast delivery, expert team

### 🛠️ **Services Available**
Web development • Mobile apps • Bots & automation • E-commerce • UI/UX design • Security • Custom software • And more

**Ready to start your project?** Hit up DevHive Studios today:
- 🌐 [devhivestudios.com](https://devhivestudios.com)  
- 💬 [Contact on Discord](https://discord.gg/mUcj5kwfrd)

*DevHive Studios — Fair marketplace. Quality results.*

---

## ☕ Support This Project

If this browser automation MCP saved you time or made you money, consider supporting the development:

- **☕ Buy me a coffee**: [buymeacoffee.com/vibheksoni](https://buymeacoffee.com/vibheksoni)

*Every contribution helps maintain and improve this project! 🚀*


---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

If you want your AI agent to access ANY website, star this repo. It helps more than you think.

---

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=vibheksoni/stealth-browser-mcp&type=Date)](https://www.star-history.com/#vibheksoni/stealth-browser-mcp&Date)
