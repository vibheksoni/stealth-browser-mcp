# Release notes for v1.0.0

This is the first production-quality tag on the arpeake/stealth-browser-mcp
fork. It is intended for a single-user host, over stdio or loopback HTTP
with a bearer token. Do not publish this package to PyPI or a public
registry without a separate decision.

Recommended tag: `v1.0.0` at the commit that includes this file.

## What it is for

Drive Chrome, Chromium, or Edge from an MCP client with a small default
tool surface, visible or offscreen windows, and session isolation when
more than one client shares an HTTP server. The unauthenticated loopback
DevTools port (finding F9) is an accepted residual, documented in
`docs/SECURITY.md`. A pipe-transport rewrite is planned after this tag
and is not in this release.

## Security (F1-F37, high level)

These are the issues found in four independent reviews of the HTTP
transport and the follow-up fixes that landed on this tag. Defaults are
safe; most of the remaining risk needs an opt-in flag that is off.

- **HTTP auth and bind (F1, F6, F11, F13-F15, F25, F34, F35).** HTTP
  always requires a bearer token. Unexpected Host or Origin is rejected.
  `GET /healthz` is unauthenticated after the Host check. A bad token
  file no longer crashes stdio import. A non-loopback bind will not
  auto-generate a token. Generated token files are removed on shutdown.
  Websocket upgrades are closed; unexpected ASGI scopes return 403.
- **Code hooks (F2).** Compiling caller Python on the server is off
  unless `STEALTH_ALLOW_CODE_HOOKS=1` on loopback HTTP.
- **Launch arguments (F3).** Dangerous Chrome flags, `sandbox=false`,
  and `user_data_dir` paths outside the managed root are rejected before
  launch.
- **Session isolation (F4, F5, F8, F18-F22, F27-F29, F32).** Tools,
  MCP resources (`browser://...`), saved sessions, network files, clone
  files, and debug logs are scoped to the calling session. Closing or
  deleting a client closes only that client's browsers. Attach cannot
  hijack another session's Chrome, and attach denials do not leak the
  other session's instance id.
- **Path confinement (F7, F12, F23, F33).** `file:`, `chrome:`, and
  `devtools:` URLs are blocked unless `STEALTH_ALLOW_FILE_URLS=1`.
  Writes stay under `data_dir()` or `STEALTH_EXPORT_DIRS`. Upload dirs
  are read-only. `export_debug_logs` cannot truncate a file in an upload
  dir.
- **Accepted residual (F9).** Chrome's loopback DevTools port has no
  DevTools authentication. Documented; not fixed in this release. See
  `docs/SECURITY.md`.
- **Attach (F10, F17, F26).** `attach_browser` is off in HTTP mode
  unless `STEALTH_ALLOW_ATTACH=1`. Remote attach is a second flag and is
  ignored or refused off loopback. Loopback host aliases share one
  ownership key so they cannot bypass that map.
- **CDP allowlist (F16, F37).** `execute_cdp_command` uses a method
  allowlist. Off-list methods need `STEALTH_ALLOW_RAW_CDP=1` on loopback
  HTTP. Even then, a download path must stay under the caller's session
  workspace, not another session's files.
- **Rate limits and reload (F21, F22).** Failed attach attempts share a
  global budget. `hot_reload` stays wired so a disconnect still closes
  the session's browsers.
- **Proxy and privileged flags (F24, F30).** Privileged flags are
  refused at startup off loopback or with `--allowed-host` /
  `--allowed-origin`. Request-time checks use the live request headers,
  including `Via` and `X-Real-IP`. nginx's default `proxy_pass` adds
  none of those headers, so do not put a proxy in front of a loopback
  bind. See `docs/SECURITY.md`.
- **Seeds (F31, F36).** Copying from a real browser profile in HTTP
  mode needs `STEALTH_ALLOW_SEED_REFRESH=1`. The `profile` argument
  cannot walk out of the source root. Password and card copy needs a
  second flag. Seeds are a shared machine-level namespace.
- **Session TTL (Q4).** Idle HTTP sessions are closed after
  `STEALTH_MCP_SESSION_TTL` seconds (default 600).

## Other changes in this line of work

Window modes (`normal`, `minimized`, `offscreen`, `headless`) and a
product-level X11 fallback when a Wayland session has no DRM render
node, so a visible or offscreen Chrome can open its DevTools port.
Input primitives for typing, paste, dropdowns, and chips. A lean
default tool surface for agents. Bounded snapshots and page summaries.

**RC note: offscreen window visibility on RDP.** `window="offscreen"` launches
a real headed window at `(-32000, -32000)` (it does not pass `--headless`).
Remote Desktop (RDP) sessions and window managers that clamp offscreen
coordinates can cause this window to appear on screen. Unattended use, CI,
and shared or remote desktops must explicitly request `window="headless"` or
`headless=True`.

## How to run it

See the Quickstart in `README.md` for stdio and for HTTP on loopback
with a token file. Read `docs/SECURITY.md` before enabling any
`STEALTH_ALLOW_*` flag or binding off loopback.

## What this tag is not

- Not a PyPI or public-registry release unless you publish it separately.
- Not a fix for the loopback DevTools port (F9). That is a planned
  opt-in pipe-transport rewrite after this tag.
- Not certified for a reverse-proxied or off-loopback deployment. The
  README and `docs/SECURITY.md` tell you not to do that.
