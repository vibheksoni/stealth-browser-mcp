# stealth-browser-mcp plan: summary (v2, 2026-11-19)

1. Plan is at ~/stealth-mcp-1119/PLAN.md (v1 kept as PLAN.v1.md). Fork: public arpeake/stealth-browser-mcp, install from git, no PyPI.
2. New model, per Alan: each chat launches its own browser seeded from his Chrome profile A, with his logins, extensions and settings.
3. Each chat's browser is a separate Chrome process with its own windows, tabs and tool calls. No chat can steer another chat or Alan's own Chrome.
4. How: a trimmed "seed" copy of A (no caches), then a fast per-chat clone launched with the same browser type. Nothing is ever written back to A.
5. Logins carry over because the clone runs as the same OS user with the same browser. Found: nodriver forces --password-store=basic, which would break this on Linux; fixed in SB-47.
6. Known limits: sites that rotate tokens can sign out Alan's Chrome or other clones; clones run with Chrome sync off; Windows can only refresh the seed while Chrome is closed.
7. Optional save_to_seed keeps a login an agent made for later chats. The v1 attach/lease design is dropped.
8. Input: the `fill` tool tries methods in turn and checks the result. Real OS clipboard paste is opt-in, with one lock for the whole machine, and puts the old clipboard back.
9. Linux and Windows (Chrome, Edge, Brave) in pass one, with a Windows CI job to prove cookies carry over. SB-01 kill-bug fix ships first. 17 Hermes slices and 18 agy slices can run at the same time.
10. New questions for Alan: Q7 logout risk (seed from A or a separate Agents profile), Q8 Windows seed refresh while Chrome is open, Q9 max browsers at once (4 recommended).
