# Security notes

This server is for a single-user host. The intended production mode is
HTTP on loopback with a bearer token, or stdio on the same machine.

## Accepted residual: the loopback DevTools port (F9)

Spawned Chrome is controlled over a DevTools port on 127.0.0.1. nodriver
picks a random port and does not add DevTools authentication. Another
local user, or another process in the same network namespace, can attach
to that port and drive the browser.

Do not run this server on a shared login or a shared netns.
`--remote-debugging-pipe` would close that gap, but nodriver does not
support it today without a large rewrite, so the port remains
loopback-only and is documented here rather than treated as a release
blocker. Attaching to a browser that is already running still needs the
port.

## HTTP

HTTP transport always requires a bearer token, binds to loopback by
default, and rejects unexpected Host and Origin values. Binding
`--host 0.0.0.0` (or any non-loopback address) without `STEALTH_MCP_TOKEN`
or `STEALTH_MCP_TOKEN_FILE` set is refused; the server will not
auto-generate a token for a non-loopback bind. Generated token files are
deleted when the server shuts down. `GET /healthz` is unauthenticated
after the Host check.

Idle MCP sessions are closed after `STEALTH_MCP_SESSION_TTL` seconds of
no requests (default 600). Set it to 0 to disable that expiry.

## Do not put a reverse proxy in front of a loopback bind

A loopback bind behind a reverse proxy counts as off loopback. Do not
set `--allowed-host` or `--allowed-origin`, and do not set any of the
privileged flags below, if a proxy will forward to this server.

Privileged flags are also ignored on a request that carries
`X-Forwarded-For`, `Forwarded`, `Via`, `X-Real-IP`, `X-Forwarded-Host`,
or `X-Forwarded-Proto`. The check uses the live request, not the
session's first `initialize`. Detection depends on the proxy adding at
least one of those headers; nginx's default `proxy_pass` adds none of
them, so a loopback bind behind such a proxy is not detected as proxied.

## Opt-in flags (off by default) and their risks

All of these are refused at HTTP startup when the bind is off loopback
or `--allowed-host` / `--allowed-origin` is set, except
`STEALTH_ALLOW_FILE_URLS`, which is a navigation-scheme flag rather than
a bind-scope flag.

- `STEALTH_ALLOW_CODE_HOOKS=1`: lets `create_dynamic_hook` /
  `validate_hook_function` compile caller Python on the server. A
  successful hook runs in the server process. Use
  `create_simple_dynamic_hook` instead unless you trust every client.
- `STEALTH_ALLOW_RAW_CDP=1`: lets `execute_cdp_command` call methods
  outside the allowlist. Navigation URLs still go through the
  file:/chrome: block. `Page.setDownloadBehavior` stays off the
  allowlist unless this flag is on, and even then `download_path` must
  stay under the caller's session workspace (or `data_dir()` in stdio).
- `STEALTH_ALLOW_ATTACH=1`: enables `attach_browser` in HTTP mode. When
  on, a caller may only attach to a Chrome this server spawned for that
  session, or to an external browser this server does not already own.
  stdio does not need this flag.
- `STEALTH_ALLOW_REMOTE_ATTACH=1`: allows attach to a non-loopback host.
  Ignored when HTTP is bound off loopback. In HTTP mode,
  `STEALTH_ALLOW_ATTACH=1` is also required. Combined with attach, this
  is the most sensitive pair of flags; leave them off unless you need
  them.
- `STEALTH_ALLOW_SEED_REFRESH=1`: allows `refresh_seed` in HTTP mode to
  copy from a real browser profile or `source_dir`. `source_dir` must
  stay under a known browser profile root, and `profile` must be a
  relative profile name (no `..` or absolute path). stdio does not need
  this flag. Seeds are a shared, machine-level namespace: any session
  can spawn a seed another session saved.
- `STEALTH_ALLOW_SEED_PASSWORDS=1`: allows `refresh_seed` to copy saved
  passwords or payment cards. In HTTP mode this also requires
  `STEALTH_ALLOW_SEED_REFRESH=1`. In multi-client HTTP, every session
  holds the same token, so these flags plus loopback and a non-proxied
  request are the real owner control, not "owns a browser".
- `STEALTH_ALLOW_FILE_URLS=1`: allows `navigate` / `new_tab` (and
  allowlisted CDP navigate) to open `file:`, `chrome:`, or `devtools:`
  URLs, which can read host files or browser internals.

Caller file paths stay under the session workspace or the configured
write roots (`data_dir()` plus `STEALTH_EXPORT_DIRS`).
`BROWSER_FILE_UPLOAD_ALLOWED_DIRS` is read-only and does not widen
writes.
