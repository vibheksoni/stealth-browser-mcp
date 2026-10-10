---
name: stealth-browser-mcp
description: Use this skill when operating stealth-browser-mcp from an AI agent or MCP client for browser automation, page inspection, element interaction, screenshots, file uploads, CDP commands, network debugging, cookies/storage, stealth setup, or reliable multi-step browser workflows. It provides the correct tool order, state checks, lifecycle cleanup, and pre-document script guidance.
---

# Stealth Browser MCP

## Core Workflow

1. Start with `spawn_browser` and keep the returned `instance_id` for every follow-up tool call.
2. Use `navigate` for page loads, then verify state with `get_instance_state`, `get_page_content`, or `take_screenshot`.
3. Inspect before acting. Use `query_elements`, `get_page_content`, and screenshots to find stable selectors and confirm the target element is visible.
4. Act with the narrowest tool that fits the task, such as `click_element`, `type_text`, `scroll_page`, `wait_for_element`, or `file_upload`.
5. Verify after each important action with page content, DOM queries, URL/title state, or a screenshot.
6. Close browsers with `close_instance` when the task is complete unless the user explicitly wants the session left open.

## Tool Selection

- Use `--minimal` when an agent only needs browser spawning, navigation, screenshots, page content, and basic interaction.
- Use full mode for CDP execution, network debugging, cookie/storage work, dynamic hooks, and advanced element extraction.
- `click_element`, `type_text`, and `press_key` all send trusted input events. Use `press_key` for control keys such as Enter, Tab, Escape, or arrow keys in react-select and typeahead fields, and `paste_text` when speed matters more than per-key events.
- Use `create_page_binding` when page JavaScript needs to ask the agent for a value. Read calls with `get_page_binding_calls` (it can wait) and answer with `resolve_page_binding_call`.
- Use `execute_script` for normal page JavaScript.
- Use `execute_cdp_command` only when a CDP primitive is required or when page JavaScript cannot access the needed browser state.
- Use network tools after navigation starts: `list_network_requests`, `search_network_requests`, `get_request_details`, `get_response_details`, and `get_response_content`.
- Use extraction tools when the task is to recreate, clone, or analyze page elements with styles, assets, events, or animations.

## Pre-Document Scripts

Use `add_script_to_evaluate_on_new_document` before navigation when a site must see modified browser APIs during its first inline scripts. This is required for WebGL renderer/vendor spoofing, `navigator` property patches, and similar anti-bot checks that run during page parse.

Keep `world_name=None` for main-world browser API spoofing. Set `run_immediately=True` when the patch should also run in the current document.

## Reliability Rules

- Prefer stable CSS selectors, roles, visible text, or attributes over brittle generated classes.
- Wait for page state instead of assuming navigation or rendering has finished.
- Treat every tool result as stateful: pass the exact `instance_id`, request IDs, and selectors returned by prior steps.
- For file uploads, pass absolute file paths inside directories allowed by `BROWSER_FILE_UPLOAD_ALLOWED_DIRS`.
- If output is too large, ask for a narrower selector, use filters, or rely on file-backed extraction responses.
- Keep debug logging disabled during normal MCP stdio usage. Enable `--debug` or `STEALTH_BROWSER_DEBUG=1` only when diagnosing server behavior.

## Lifecycle And Security

- The server reaps idle browsers by default. `BROWSER_IDLE_TIMEOUT=0` disables idle reaping globally.
- HTTP transport is unauthenticated by default for compatibility. Set `STEALTH_BROWSER_MCP_AUTH_TOKEN` when exposing HTTP transport beyond a trusted local environment.
- Do not expose unauthenticated HTTP transport to public networks.
- Close instances after tests and one-off automations to release browser processes and temporary profiles.
