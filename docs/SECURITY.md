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
and rejects unexpected Host and Origin values. `STEALTH_ALLOW_REMOTE_ATTACH=1`
is ignored when HTTP is bound off loopback. Caller file paths stay under the
data directory. Caller Python hooks stay off unless `STEALTH_ALLOW_CODE_HOOKS=1`
on loopback HTTP.
