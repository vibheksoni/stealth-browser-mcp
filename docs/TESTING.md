# Testing Guide

This guide covers running unit tests, gated real-browser integration tests, and the release-candidate verification matrix.

## 1. Unit Tests (Ungated)

Unit tests run free of real browsers and network access. They use synthetic fixtures and temporary directories.

```bash
python -m unittest discover -s tests -v
```

All unit tests must pass without launching a browser or making external requests.

## 2. Gated Real-Browser Suite (Headless Default)

Real-browser tests are gated by `STEALTH_BROWSER_TESTS=1`. By default, tests run without opening visible windows on the desktop (headless or offscreen):

```bash
STEALTH_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

This validates browser integration, session isolation, and CDP control under headless operation.

## 3. Headful Test Pass (Visible Windows)

Production verification requires testing on real visible windows. The same gated suite can be run with visible normal windows:

```bash
STEALTH_BROWSER_TESTS=1 STEALTH_TEST_HEADFUL=1 STEALTH_WINDOW_MODE=normal python -m unittest discover -s tests -v
```

Requirements and behavior:
- Requires a graphical display (on Linux, `DISPLAY` or `WAYLAND_DISPLAY` must be set; Windows and macOS have display environments by default).
- When `STEALTH_TEST_HEADFUL=1` is set without an available display on Linux, tests requiring display automatically skip with a clear message.
- Browser windows will appear on screen during this pass.
- Window size assertions tolerate window-manager framing adjustments (up to 150 px for normal mode).

## 4. Release-Candidate (RC) Matrix

Before tagging a release candidate or pushing major changes, run the three-pass verification matrix:

1. **Unit suite pass**: `python -m unittest discover -s tests` (fast, ungated).
2. **Gated headless pass**: `STEALTH_BROWSER_TESTS=1 python -m unittest discover -s tests`.
3. **Gated headful pass**: `STEALTH_BROWSER_TESTS=1 STEALTH_TEST_HEADFUL=1 STEALTH_WINDOW_MODE=normal python -m unittest discover -s tests`.

Production readiness requires three consecutive green runs across the matrix.

## 5. Known Flake Handling

To prevent timing-dependent test flakes:
- **Condition-based waiting**: Tests synchronize using `wait_until` and `wait_until_async` rather than fixed `sleep` intervals.
- **SQLite cookie persistence**: Orderly browser shutdowns wait for the process to exit (killing after 10 s if stuck) and poll for database rows before copying.
- **Concurrent DB writes**: Tests that stress sqlite backup engines during active writes use bounded retries to tolerate OS thread scheduling delays while asserting `PRAGMA integrity_check`.
- **Process cleanup**: Each test uses isolated temporary directories and terminates leftover browser processes in `tearDown` or `finally` blocks.
