# Windows Platform Support

This document outlines Windows portability features, operational behavior, and known platform limitations for stealth-browser-mcp.

## Architecture and Portability

### 1. Cross-Process File Locking
On Windows, file locking is provided via `msvcrt.locking`. To avoid blocking read access to lock holder metadata:
- Locks are acquired on a dedicated 1-byte region at a high offset (`0x7FFFFFF0`).
- This leaves the initial file bytes and metadata files readable by other handles and inspection tools.
- Handles are closed on release and in finalizers to prevent sharing violations (`WinError 32`) during temporary directory cleanup.

### 2. Graceful Browser Shutdown
Process teardown on Windows (`Popen.terminate`) invokes `TerminateProcess`, which immediately terminates Chrome without allowing background threads to flush SQLite caches:
- Browser teardown first issues a CDP `Browser.close` command via nodriver.
- The teardown logic waits up to 10 seconds for the process to exit gracefully so SQLite databases (such as `Cookies`, `Preferences`, and `Web Data`) are committed to disk.
- If the browser does not exit within the timeout, termination falls back to process termination.

### 3. Path Normalization
Browser user-data roots and profile subdirectories are modeled deterministically:
- Windows paths use `ntpath` (`%LOCALAPPDATA%`, `\`).
- Linux and macOS paths use `posixpath` (`HOME`, `/`).
- Path comparison helpers resolve 8.3 short names (`RUNNER~1` vs `runneradmin`) and case variations.

### 4. Display Density and Device Pixel Ratio
On high-DPI Windows displays:
- Element and clip screenshots captured via CDP `Page.captureScreenshot` are rendered in physical device pixels (`CSS pixels * window.devicePixelRatio`).
- Screenshot metadata includes the `device_pixel_ratio` attribute.

## Known Limitations: Chrome App-Bound Encryption

Starting with Chrome 127 on Windows, Google Chrome implements App-Bound Encryption for cookies and saved credentials:

### What Works
- Headless and headful browser launches with isolated user-data directories.
- CDP automation, navigation, and DOM manipulation.
- Multiple isolated browser sessions.
- Screenshot clipping, file downloads, and unpacked extension loading.

### What Does Not Work
- Direct external reading or migration of raw `Cookies` SQLite database rows across independent Windows processes without the originating DPAPI elevation context.
- Synthetic profile seeds relying on unencrypted SQLite cookie rows may find cookie rows encrypted or restricted under Windows DPAPI App-Bound protection.
