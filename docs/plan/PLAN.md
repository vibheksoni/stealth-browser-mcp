---
title: stealth-browser-mcp enhancement plan (per-chat browsers from a real profile, input reliability, expansions)
vertical: stealth-browser-mcp
altitude: plan
status: design
owner-branch: none yet (fork https://github.com/arpeake/stealth-browser-mcp, public; local clone at repo/)
touches: src/stealth_browser_mcp/browser_manager.py, src/stealth_browser_mcp/browser_launcher.py, src/stealth_browser_mcp/process_cleanup.py, src/stealth_browser_mcp/platform_utils.py, src/stealth_browser_mcp/dom_handler.py, src/stealth_browser_mcp/input_actions.py, src/stealth_browser_mcp/server.py, src/stealth_browser_mcp/models.py, .github/workflows/ci.yml
depends-on: none
related: upstream https://github.com/vibheksoni/stealth-browser-mcp @ 4f5d6b7; v1 of this plan at PLAN.v1.md
lanes: hermes, agy (exclusive)
---

# stealth-browser-mcp enhancement plan (v2)

## Progress

| Section | Status | Evidence | Updated |
|---|---|---|---|
| Code read + current-state map | done | cites at 4f5d6b7 | 2026-11-19 |
| Baseline tests | done | `python -m unittest discover -s tests`: 157 ran, OK, 3 skipped (Py 3.14 venv) | 2026-11-19 |
| Hosting (Q1) | done | Alan 2026-11-19 8:29: public fork `arpeake/stealth-browser-mcp` | 2026-11-19 |
| Alan answers Q2-Q6 + 8:33 clarification folded in | done | this file (v2) | 2026-11-19 |
| Slices | not-started | - | 2026-11-19 |

## Abstract

Alan's model: he is signed in to Chrome profile A; every chat launches ITS OWN browser seeded from A
(logins, cookies, extensions, settings), independent of every other chat and of Alan's own Chrome.
Mechanism: a curated "seed" copy of A (no caches) refreshed from A, then a fast per-chat clone of the
seed into a temp user-data-dir, launched as a separate Chrome process with the same browser binary.
Nothing is ever written back to A. Optional "save to seed" keeps a login an agent made for the next
chats. Attach/lease (v1) is dropped. Input: a verifying `fill` tool with a fallback chain, plus opt-in
real OS clipboard paste under a machine-wide lock. Linux and Windows (Chrome, Edge, Brave) in pass one.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Each chat = its own Chrome process on its own cloned user-data-dir | Only way to give every chat a fully separate session it alone controls; Chrome's profile lock forbids two processes on one dir |
| D2 | Clone from a seed, not straight from A | Copy cost paid once per refresh; A is read only at refresh time; Windows locks some of A's files while Alan's Chrome runs |
| D3 | Never write to A. Save-back goes to the seed only, explicit, under a lock | Writing into a live everyday profile risks corrupting it and racing Alan's own Chrome |
| D4 | Never decrypt cookies in our code; launch the clone with the same browser family on the same OS user | The OS key (Linux keyring, Windows DPAPI / App-Bound) unlocks them for the real binary; no infostealer-shaped code |
| D5 | Clones launch with `--disable-sync` and the source's password-store backend | Stops N clones pushing into Alan's Google sync; `--password-store=basic` (nodriver default) would make Linux keyring cookies unreadable |
| D6 | Seed excludes saved passwords and payment data by default; extensions included | Extensions are part of "his settings"; passwords and cards are opt-in flags |
| D7 | Drop attach + tab leases (v1 SB-04/05/06/07/08) | Separate processes already give per-chat isolation; leases add cross-process lifetime code with no remaining user need |
| D8 | SB-01 (kill bug) ships first | Today closing one instance kills every browser on the same dir, which could include Alan's Chrome if a raw dir is ever passed |
| D9 | Real OS clipboard paste: opt-in, one machine-wide lock | Alan Q5. "Server-wide" must be machine-wide: each chat runs its own server process but the OS clipboard is shared |
| D10 | Install from git, no PyPI | Alan Q6 |

## Open questions for Alan (new only)

| # | Question | Options (recommended first) |
|---|---|---|
| Q7 | Logout risk on sites that rotate refresh tokens (some Microsoft, Google Workspace, SaaS SSO apps). A clone using a token can sign out the original (your Chrome) or other clones. Seed from which profile? | **(A) Seed from A, accept occasional re-login, keep a per-site denylist of known-rotating sites that clones skip (Recommended)** - matches your ask, risk is bounded and visible; (B) seed from a dedicated "Agents" profile you sign into once - zero risk to your own Chrome, but one extra sign-in per site; (C) seed from A with no safeguards |
| Q8 | Windows: Chrome locks its Cookies file while it runs. When your Chrome is open, refresh can only reuse the last seed. OK? | **(A) Use last seed, report its age, refresh automatically next time Chrome is closed (Recommended)** - never touches your running Chrome; (B) block launch until you close Chrome; (C) also copy it live by tricks that bypass the lock - rejected, that is malware technique |
| Q9 | Max concurrent per-chat browsers on this box | **(A) 4 by default, env override (Recommended)** - about 0.4 to 1 GB RAM each with extensions; (B) 8; (C) unlimited |

Labels: **V** = verified (code cite at 4f5d6b7 or venv file), **I** = inferred (known Chrome/CDP behavior,
not run here), **U** = unverified (a slice must test it).

## 1. Current-state map

### Process and transport

| Fact | Label | Cite |
|---|---|---|
| Default transport is stdio: each chat starts its own server process with its own `BrowserManager` | V | `server.py:3123-3133`, `server.py:178` |
| Instances live in an in-process dict; only the PID file is shared across server processes | V | `browser_manager.py:77-81`; `process_cleanup.py:59` |
| PID entries carry `owner_pid`; startup recovery skips entries owned by other live servers | V | `process_cleanup.py:286-299`, `586-630` |
| Idle reaper closes instances after `BROWSER_IDLE_TIMEOUT` (600 s) | V | `browser_manager.py:74`, `315-347` |

So separate chats already get separate servers and separate browsers. What is missing is "seeded from A".

### Profiles and launch

| Fact | Label | Cite |
|---|---|---|
| A profile is just `spawn_browser(user_data_dir=...)`; no registry, no lock | V | `server.py:231`, `models.py:97` |
| Without `user_data_dir`, a temp `uc_*` dir is made and deleted on close; orphan sweep at startup | V | `browser_launcher.py:463-464`, `process_cleanup.py:515-524` |
| Every spawn launches a new Chrome on a free port | V | `browser_launcher.py:431-460` |
| nodriver always passes `--password-store=basic` (and `--no-first-run`) via `*config()` | V | venv `nodriver/core/config.py:117-129`; `browser_launcher.py:112-114` |
| Browser binary is process-wide (`STEALTH_BROWSER_EXECUTABLE` or search); Brave not in the search list; no per-spawn choice | V | `platform_utils.py:200-270` |
| **Bug:** closing an instance kills every browser process whose `--user-data-dir` matches | V | `process_cleanup.py:368-401`, `418`, `725-737` |
| Per-spawn proxy, timezone, UA, headers, headless, sandbox exist | V | `browser_manager.py:419-429` |

### Input paths, other gaps

Unchanged from v1 (see PLAN.v1.md section 1): `type_text` US-layout keys with no read-back
(`dom_handler.py:244-285`), `paste_text` is one `insertText` with no paste event (`dom_handler.py:287-319`),
no clipboard, no shadow DOM / iframe / XPath on the input path (`input_actions.py:48-71`), fixed 2 s
"networkidle" (`browser_manager.py:957-959`), no HAR, downloads, or accessibility tree.

## 2. Ask 1: each chat gets its own browser from profile A

### What Alan gets

- Chat 1 calls `spawn_browser(seed="A")` and gets a new browser window already signed in to his sites,
  with his extensions and settings. Chat 2 does the same and gets another, separate window.
- Each chat's browser is its own Chrome process: its own windows, tabs, focus, typing, downloads and
  tool calls. No chat can see or steer another chat's browser or Alan's own Chrome.
- After the first copy, logins are independent: logging out in chat 1 does not log out chat 2. A login
  done in chat 1 is gone when it closes, unless it is saved to the seed (`save_to_seed`).

### Why separate processes, not one Chrome with a window per chat

| Option | Meets "own session"? | Logins shared how | Cost | Verdict |
|---|---|---|---|---|
| A. Separate Chrome per chat, each on its own clone | Yes, fully: own process, own storage | Copied at launch; diverge after | One Chrome of RAM per chat; a copy per launch | **Chosen** |
| B. One Chrome on A's dir, one window per chat (v1 attach) | Windows and tool calls separate, but storage is live-shared: one chat's logout or cart change hits all; one crash kills all; needs cross-process lifetime code | Live | One Chrome total | Dropped |
| C. Two Chromes on the same dir | Impossible: Chrome's `SingletonLock` (Linux) / `lockfile` (Windows) makes the second launch hand off to the first and exit (I) | - | - | Not possible |
| D. Drive Alan's running Chrome directly | No: steers his own browser; Chrome 136+ ignores `--remote-debugging-port` on the default dir (I) | Live | - | Rejected |

### Pipeline

```
source A (Alan's real dir, read only) --refresh_seed--> seed  data_dir()/seeds/<name>/  (curated, no caches)
seed --clone (reflink or copy, ~seconds)--> session  data_dir()/sessions/<session_id>/  --launch--> own Chrome
session --save_to_seed (explicit, lock)--> seed      (never back to A)
```

- **Source discovery** (SB-41): user-data roots per OS and browser, profiles from `Local State`
  `profile.info_cache` (name, email shown in the picker).

  | Browser | Linux | Windows |
  |---|---|---|
  | Chrome | `~/.config/google-chrome` | `%LOCALAPPDATA%\Google\Chrome\User Data` |
  | Edge | `~/.config/microsoft-edge` | `%LOCALAPPDATA%\Microsoft\Edge\User Data` |
  | Brave | `~/.config/BraveSoftware/Brave-Browser` | `%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data` |

  Snap/Flatpak variants listed as I; binaries resolved per family (Brave added to the search).
- **Seed manifest** (SB-42): copy `Local State` + the one profile subdir (keep its name, launch with
  `--profile-directory`). Include: `Network/Cookies`, `Preferences`, `Secure Preferences`, `Local Storage`,
  `IndexedDB` (per-origin size cap, default 200 MB total), `Extensions`, `Local Extension Settings`,
  `Extension State`, `Bookmarks`, `Web Data` minus cards (opt-in), `Login Data` (opt-in). Skip: `Cache`,
  `Code Cache`, `GPUCache`, `Service Worker/CacheStorage`, `Session Storage`, `Sessions`, `History`,
  `Favicons`, `Crashpad`, `Safe Browsing`, on-device model dirs (`OptGuideOnDeviceModel` can be GBs),
  `Singleton*`, `lockfile`, `*.tmp`.
- **Copy engine** (SB-44): SQLite files via the SQLite backup API (consistent even while Chrome writes;
  Linux) and plain copy for LevelDB dirs. Windows: if a file is locked by Alan's running Chrome, skip the
  refresh and keep the last seed (Q8). Seed refresh is atomic: build in `seeds/<name>.new`, then rename.
- **Clone** (SB-44, SB-47): `cp --reflink=auto`-style copy (near instant on btrfs/xfs; ReFS/Dev Drive block
  clone on Windows is a later nicety), else plain copy. Overlay filesystems rejected (root or FUSE needed,
  Windows has no equivalent).
- **Launch** (SB-47): same browser family binary as the source, `--disable-sync`, `--profile-directory=<subdir>`,
  password store matching the source on Linux (drop nodriver's `--password-store=basic`; detect
  `gnome-libsecret` / `kwallet*` / `basic`). The clone dir is not the default dir, so remote debugging works.
- **Carryover check** (SB-47): after launch, compare cookie row count in the seed DB (count only, no
  decrypt) with `Storage.getCookies` count. A large gap means the OS key did not unlock them; the tool
  result says so plainly ("logins did not carry over: keyring not reachable").
- **Sessions + cleanup** (SB-43): `sessions/<id>/session.json` = owner pid, browser pid, seed, created.
  Close deletes the dir (Windows: retry while handles release). Janitor at startup and every reap tick
  deletes sessions whose owner and browser pids are dead. `keep=true` on spawn keeps a session dir.
- **Concurrency** (SB-43): machine-wide cap `STEALTH_MAX_SESSIONS` (Q9, default 4), counted under a file
  lock; spawn over the cap returns a clear error naming live sessions.
- **Save to seed** (SB-49): `save_to_seed(session_id)` closes the session browser (flush), then under the
  seed lock replaces the seed's state files with the session's (manifest only). Last writer wins; the tool
  says so. Never touches A.

### Re-evaluating copy (v1 rejected copy-and-sync-back)

v1 rejected copying because it assumed a MERGE back into one shared profile. Alan's model has no merge:
clones are disposable, and the only write-back is an explicit whole-replace into the seed.

| Topic | What happens | Label | Handling |
|---|---|---|---|
| Cookies, Linux | Encrypted with a key from the user's keyring (`Chrome Safe Storage`, per browser) or a fixed fallback; same user + same binary + same password store decrypts | I | D5 password store fix; carryover check; doctor flags no keyring (SSH/systemd without D-Bus) |
| Cookies, Windows | `Local State` holds a DPAPI key (per Windows user) and, on Chrome 127+, an App-Bound key checked against the installed Chrome path. Same user + installed binary should decrypt | U | Copy `Local State` with the profile; Windows CI test (SB-48); report clearly if not |
| Token-rotating sites | Clone refreshes a token; the old one dies in A or in other clones; reuse detection can revoke all | I | Q7: denylist skip, visible re-login; save_to_seed to keep a fresh login |
| Device-bound sessions (Google DBSC on Windows, some banks) | Key in TPM tied to the original profile; clone may be signed out of that site | U | Report per site; Q7 option B avoids it |
| Google account / Chrome sync | Clones signed in to Chrome could sync tabs, history, settings back into Alan's account | I | `--disable-sync` (D5); web sign-in to Google sites still works via cookies |
| Extensions | Copied with their settings; each clone runs its own copy; state changes are not saved back | I | Password-manager extensions (1Password, Bitwarden) likely ask to unlock per clone; documented |
| Stale seed | Seed older than A misses new logins | I | `refresh_seed` on spawn when source is readable and seed is older than `max_age` (default 1 h); else use last seed and report age |
| Same account in N browsers at once | Normal for most sites; a few flag "new device" | I | Documented |

### Cost (I, measured in SB-47)

| Item | Estimate |
|---|---|
| Full Chrome profile | 1 to 10+ GB, mostly caches, on-device models, IndexedDB |
| Seed after manifest | 50 to 500 MB typical |
| Seed refresh | 2 to 20 s (SSD); once per `max_age` |
| Clone per chat | under 1 s with reflink; 1 to 5 s plain copy; target spawn under 5 s |
| RAM per session | 300 to 600 MB idle headful, about 1 GB with extensions and a few heavy tabs |
| Disk per session | seed size (shared blocks with reflink), deleted on close |

### Attach / leases

Dropped (D7). If Alan later wants several chats watching one live browser, v1 section 2 has the design.

## 3. Ask 2: input that works

Unchanged from v1 (PLAN.v1.md section 3: element-kind table, `fill` fallback chain, targeting), except:

- `tab_id` arguments and per-tab leases are gone; `fill` acts on the instance's current tab.
- Clipboard (SB-45, SB-15), per Q5: `paste_text(mode="clipboard")` and `paste(selector)`:
  1. take the machine-wide clipboard lock (`data_dir()/locks/clipboard.lock`, OS file lock, 10 s timeout);
  2. save the user's current clipboard text (best effort; Linux `wl-paste`/`xclip`, Windows Win32 API);
  3. `Browser.grantPermissions(["clipboardReadWrite","clipboardSanitizedWrite"])`, focus emulation on,
     `navigator.clipboard.write` text (and html if given);
  4. trusted Ctrl+V (`dispatchKeyEvent` with `commands=["paste"]`);
  5. verify by read-back (`fill` rules), restore the saved clipboard, release the lock.
  Off unless `STEALTH_ENABLE_OS_CLIPBOARD=1`. Headless keeps a private clipboard (I) and needs no lock.

## 4. Ask 3: optional expansions

As in PLAN.v1.md section 4, with these changes: "profile import" and "fork profile" are now the core
pipeline (section 2); "snapshots" become seed snapshots (SB-38); "relaunch headless" is just a spawn
argument and is retired (SB-40).

## 5. Slices

**Hermes** = local model, small, self-contained, unit tests with fakes and tmpdirs only (patterns:
`tests/test_input_actions.py` `RecordingTab`, `tests/test_tabs_and_contexts.py` `FakeBrowser`).
**agy** = Antigravity, medium, may add real-browser tests gated by `STEALTH_BROWSER_TESTS=1`
(`/usr/bin/google-chrome` here). Every slice: unit suite green, no stdout prints in server paths
(AGENTS.md), ASCII only, no slice reads a real profile on a dev machine (synthetic fixtures only).
Dependencies only run Hermes -> agy; Hermes front-loads them, agy starts on dependency-free slices.

SB-03 is repurposed (profile lock -> generic cross-process file lock). Retired from v1: SB-04, SB-05, SB-06, SB-07, SB-08, SB-09,
SB-37 (folded into SB-41/42/44/46), SB-40.

### Hermes lane (order: SB-01, 03, 02, 41, 42, 44, 43, 10, 11, 12, 13, 45, 18, 20, 39, 30, 31)

| ID | Title | Size | Deps | Acceptance |
|---|---|---|---|---|
| SB-01 | Kill only the launched browser tree, not every process on the dir | S | - | Fake psutil: foreign Chrome on same dir survives; launched pid + children killed; gone pid -> True; recovery tests pass |
| SB-03 | `file_lock.py`: cross-process lock (`fcntl.flock` / `msvcrt.locking`), timeout, holder pid recorded, stale holder detection | S | - | Two-subprocess test: second waits then times out; dead holder -> acquirable; works on Windows (CI) |
| SB-02 | `profiles.py`: seed names `[a-z0-9_-]{1,40}`, paths under `data_dir()/seeds` and `sessions`, `seed.json` (source browser, source dir, profile subdir, password store, refreshed_at, defaults) | S | - | Bad names (`..`, `/`, empty) rejected; JSON round-trip; delete refuses while sessions from it are live |
| SB-41 | Source discovery: Linux + Windows roots for Chrome/Edge/Brave, `Local State` parse, binary per family (adds Brave) | S | - | Fake trees for each OS x browser; malformed `Local State` handled; path builder tests with fake env vars |
| SB-42 | Seed manifest: include/exclude rules, IndexedDB cap, opt-in passwords/cards, plan output with sizes and skip reasons | S | SB-41 | Table tests on a synthetic tree; caches and model dirs skipped; opt-ins off by default |
| SB-44 | Copy engine: SQLite backup API, LevelDB dir copy, reflink-when-available, locked-file detection (Windows sharing violation -> "source busy"), atomic `.new` + rename | S | SB-42 | tmpdir with sqlite fixtures written during copy stays consistent; simulated lock -> busy result, old seed intact |
| SB-43 | Session registry: session dirs + `session.json`, machine-wide count + cap under SB-03 lock, janitor for dead sessions | S | SB-02, SB-03 | Fake psutil: dead sessions removed, live kept, cap enforced across two processes |
| SB-10 | Text normalize + compare + safe preview (password -> length/hash) | S | - | 12+ table cases incl. emoji ZWJ, combining marks, CRLF, NBSP |
| SB-11 | Element-kind probe JS + `classify()` | S | - | 15+ classify cases; JS ships as package data |
| SB-12 | Native value setter strategy | S | - | RecordingTab asserts setter JS + args |
| SB-13 | Synthetic paste strategy (`ClipboardEvent` + `DataTransfer`) | S | - | RecordingTab payload; returns `defaultPrevented` |
| SB-45 | OS clipboard helper: machine-wide lock (SB-03), save/restore text on Linux (wl-paste/xclip) and Windows (Win32), opt-in env flag | S | SB-03 | Fakes for each backend; lock held across set+paste; flag off -> clear error |
| SB-18 | Date/time/color/range formatters | S | - | ISO + common inputs -> HTML formats; invalid rejected |
| SB-20 | `value_env` secrets (`STEALTH_SECRET_` only) + redaction | S | - | Non-prefixed rejected; redaction in debug_logger payloads |
| SB-39 | Seed identity defaults merged at spawn (explicit args win) | S | SB-02 | Pure merge-function tests |
| SB-30 | `doctor` tool: binaries per family, keyring / password store reachable (Linux), seed ages, live sessions vs cap, stale locks, data dir writable | S | SB-03, SB-43 | Fake-environment tests per check; output redacted |
| SB-31 | HAR 1.2 export | S | - | Fixture capture -> HAR shape check; tool registered in `test_tool_schemas.py` |

### agy lane (order: SB-21, 16, 36, 46, 47, 48, 14, 15, 49, 50, 19, 34, 33, 32, 35, 38, 17, 22, 60)

| ID | Title | Size | Deps | Acceptance |
|---|---|---|---|---|
| SB-21 | Browser fixture harness: local `http.server`, fixture pages (inputs, React-controlled, contenteditable, shadow DOM, iframe, dates, ProseMirror, a login page that sets cookies + localStorage) | M | - | Gated suite boots and tears down, no network, < 60 s |
| SB-16 | Deep selectors, same-origin `frame=`, XPath on input path | M | - | Fixture tests per target type; old selectors unchanged |
| SB-36 | Real waits: `wait_for` + true `networkidle` | M | - | Delayed-fetch fixture returns on idle, not 2 s |
| SB-46 | `list_sources` + `refresh_seed` tools on Linux | M | SB-41, 42, 44, SB-21 | Gated: make a synthetic source by launching Chrome on a temp dir, log in to fixture, close; refresh; seed has cookies, no caches; source hash unchanged |
| SB-47 | Per-chat session spawn: `spawn_browser(seed=)`, clone, family binary, `--disable-sync`, password-store fix, carryover check, delete on close, timing log | M | SB-01, 43, 46 | Gated: two sessions from one seed both logged in to fixture; logout in one leaves the other in; closing one leaves the other; dirs removed; spawn time recorded |
| SB-48 | Windows CI job (`windows-latest`, Chrome + Edge preinstalled; Brave via installer if cheap) running SB-46/47 gated tests | M | SB-47 | Green on Windows: DPAPI + App-Bound cookies carry over to the clone, or the test records exactly where it fails |
| SB-14 | `fill` tool + `verify` flags + `paste_text(mode=)` | M | SB-10, 11, 12, 13, 18, 20, 21 | Every kind row passes with `strategy=auto`; password never echoed |
| SB-15 | Real OS clipboard paste, Linux + Windows | M | SB-14, SB-45 | Paste-only fixture editor accepts; second process blocks on lock; user clipboard restored |
| SB-49 | `save_to_seed(session_id)` | M | SB-47 | Gated: login in session, save, new session from seed is logged in; concurrent save blocked by lock; source untouched |
| SB-50 | Extensions in clones: copied extensions load and keep settings; unpacked-extension path fixups | M | SB-47 | Local MIT fixture extension present and configured in two clones |
| SB-19 | `choose_option` for custom comboboxes | M | SB-16 | react-select-like + ARIA listbox fixtures |
| SB-34 | Accessibility snapshot + `ref=` targeting | M | SB-16 | Size cap; click/fill by ref |
| SB-33 | Session save/restore JSON (storageState-like, mode 0600) | M | - | Round-trip into a fresh temp profile |
| SB-32 | Downloads dir + `wait_for_download`; drag-drop zones | M | - | Fixture download lands in session dir; path escape rejected |
| SB-35 | Action trace bundle, redacted | M | SB-20 | Size cap; secrets absent (grep test) |
| SB-38 | Seed snapshots / rollback | M | SB-44 | Snapshot under seed lock; restore exact file set |
| SB-17 | Cross-origin iframe targeting | M | SB-16 | Two-port fixture |
| SB-22 | Grapheme-aware typing + IME option | S | SB-14 | Emoji ZWJ + CJK read back exactly |
| SB-60 | Remote MCP transport + agent ergonomics: streamable HTTP with bearer-token auth (token from env var `STEALTH_MCP_TOKEN` or file `STEALTH_MCP_TOKEN_FILE`, never committed, never logged), binds 127.0.0.1 by default with configurable host and port; (a) screenshot tools return MCP image content (not only paths); (b) each MCP client session automatically gets its own isolated browser session (SB-47 per-chat session), closed on disconnect; (c) every action returns a compact page summary (url, title, interactive elements list, size-capped) | M | SB-47, SB-34, SB-36 | Tests: missing/wrong/malformed token -> 401 and no tool runs; correct token works; token never in logs/output; two client sessions get distinct browser sessions and cannot see each other's tabs; closing one leaves the other; screenshot result contains an image content block; every action result has summary fields within the size cap. No Nginx, DNS or deployment work (owned by Rudy and Alan) |

macOS sources and Keychain (later pass): one slice pair, SB-41/SB-48 style, after Windows is green.

## 6. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Clone refreshes a rotating token and signs out Alan's Chrome | Annoyance, re-login | Q7; denylist; documented; `save_to_seed` |
| Reading A while Alan's Chrome writes | Torn seed | SQLite backup API; atomic seed rename; Windows waits for Chrome closed (Q8) |
| Any code path writes to A | Damage to his real profile | A opened read-only; tests hash the source before and after; review gate |
| Seeds and sessions hold live session cookies on disk | Exposure if data dir leaks | Dirs mode 0700 (POSIX), never inside the repo, sessions deleted on close, janitor |
| SB-01 changes orphan cleanup | Leaked Chromes | Keep temp-dir sweep; tests for kill and survive cases |
| Linux keyring not reachable from the MCP server env | Silent logged-out clones | Carryover check + doctor |
| Windows App-Bound encryption rejects clones | Logged-out clones on Windows | SB-48 proves it early; if it fails, fallback is Q7 option B (sign in once inside a seed) |
| RAM with many sessions | Box slowdown | Cap (Q9); headless option per spawn |
| Upstream drift | Merge pain | Small slices, weekly rebase; upstream SB-01, SB-16 XPath, SB-36 |
| Stealth regressions (focus emulation, clipboard grants, flags) | Detection | Run `benchmarks.stealth` before/after SB-47, 15, 34 |

## 7. Hosting and license

- Public fork `arpeake/stealth-browser-mcp` (settled, Q1). Work on branches, PR-sized slices; generic fixes
  go upstream as separate PRs without AI trailers (upstream `AGENTS.md`).
- MIT: keep the copyright and permission notice. Install from git; no PyPI (Q6).
- Leave the upstream sponsor block untouched to keep diffs small.

## Alan answers 2026-11-19 10:00 AM ET (Q7-Q9)
- Q7: Seed from profile A; Alan accepts the sign-out risk from token-rotating sites. [Verified: Alan widget]
- Q8: Windows seed refresh: best-effort copy while Chrome is open (skip locked files, report what was skipped). [Verified: Alan widget]
- Q9: No limit on concurrent browsers (no default cap; resource use is the operator's call). [Verified: Alan widget]

## Added 2026-11-19 12:53 PM ET (Alan approved)
- SB-60 (agy lane, last in the agy order): remote MCP transport (streamable HTTP + bearer token, localhost default, configurable host/port), image content for screenshots, per-MCP-session isolated browser session, compact page summary on every action. Out of scope: Nginx, DNS, deployment (Rudy and Alan).
