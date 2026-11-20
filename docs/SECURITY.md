# Security notes

This server is for a single-user host.

Spawned Chrome is controlled over a DevTools port on 127.0.0.1. nodriver
picks a random port and does not add DevTools authentication. Another local
user, or another process in the same network namespace, can attach to that
port and drive the browser.

Do not run this server on a shared login or a shared netns. `--remote-debugging-pipe`
would close that gap, but nodriver does not support it today without a large
rewrite, so the port remains loopback-only and undocumented to other users.

HTTP transport always requires a bearer token, binds to loopback by default,
and rejects unexpected Host and Origin values. `attach_browser` is off in HTTP mode unless `STEALTH_ALLOW_ATTACH=1`. When it is on, a caller may only attach to a Chrome this server spawned for that session, or to an external browser this server does not already own. `STEALTH_ALLOW_REMOTE_ATTACH=1` is ignored when HTTP is bound off loopback. Caller file paths stay under the
data directory. Caller Python hooks stay off unless `STEALTH_ALLOW_CODE_HOOKS=1`
on loopback HTTP. `execute_cdp_command` uses a method allowlist;
navigation URLs go through the same file:/chrome: block as `navigate`.
`Page.setDownloadBehavior` is off the allowlist. Methods outside the
allowlist require `STEALTH_ALLOW_RAW_CDP=1` on loopback HTTP, and even
then a download path must stay under the data directory.


A loopback bind behind a reverse proxy counts as off loopback. Do not set
`--allowed-host` or `--allowed-origin`, and do not set
`STEALTH_ALLOW_CODE_HOOKS`, `STEALTH_ALLOW_RAW_CDP`,
`STEALTH_ALLOW_ATTACH`, `STEALTH_ALLOW_REMOTE_ATTACH`,
`STEALTH_ALLOW_SEED_PASSWORDS`, or `STEALTH_ALLOW_SEED_REFRESH`, if a proxy will forward to this server.
Those flags are also ignored on a request that carries `X-Forwarded-For`
or `Forwarded`.

HTTP always requires a token. Binding `--host 0.0.0.0` (or any non-loopback
address) without `STEALTH_MCP_TOKEN` or `STEALTH_MCP_TOKEN_FILE` set is
refused; the server will not auto-generate a token for a non-loopback bind.
Generated token files are deleted when the server shuts down.

Idle MCP sessions are closed after `STEALTH_MCP_SESSION_TTL` seconds of no
requests (default 600). Set it to 0 to disable that expiry. `refresh_seed` from a real browser profile or `source_dir` requires
`STEALTH_ALLOW_SEED_REFRESH=1` in HTTP mode; the flag is refused off
loopback and behind a proxy. `include_passwords` or `include_cards` also
requires `STEALTH_ALLOW_SEED_PASSWORDS=1`. In multi-client HTTP, every
session holds the same token, so these flags plus loopback and a
non-proxied request are the real owner control, not "owns a browser".
Seeds are a shared, machine-level namespace: any session can spawn a seed
another session saved.
