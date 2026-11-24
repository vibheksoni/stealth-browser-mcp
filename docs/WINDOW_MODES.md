# Window Modes and Focus Management

Stealth Browser MCP supports dedicated window modes so headful automation runs without popping up windows or stealing OS focus from the user.

> **Important: Offscreen vs Headless**
>
> `offscreen` is a real headed window (not `--headless`). Some environments
> (Remote Desktop / RDP, single-display setups, and some window managers)
> ignore or clamp `--window-position=-32000,-32000`, so the window can appear on
> screen. For unattended soaks, CI, and any shared or remote desktop, callers must
> use `window="headless"` or `headless=True`.
>
> If headed execution is required when offscreen placement is unsupported or
> clamped, `window="minimized"` is the headed alternative.

## Available Window Modes

| Mode | Behavior | Focus Behavior | Typical Use Case |
|------|----------|----------------|------------------|
| `offscreen` | Browser window is positioned off-screen at `(-32000, -32000)` | Never takes OS focus; CDP focus emulation enabled | Default mode: headful stealth rendering without desktop intrusion |
| `minimized` | Browser window starts minimized to the taskbar or dock | Never takes OS focus; CDP focus emulation enabled | Background automation where off-screen positioning is unsupported |
| `headless` | Chrome `--headless` execution | No window created; CDP focus emulation enabled | Headless automation, Docker containers, and CI environments |
| `normal` | Visible, standard desktop window at `(0, 0)` | Normal desktop focus behavior; `switch_tab` activates the window | Local interactive debugging and visual inspection |

## Configuration

### 1. `spawn_browser` Parameter

Specify the `window` parameter when spawning an instance:

```python
await spawn_browser(window="offscreen")
await spawn_browser(window="minimized")
await spawn_browser(window="headless")
await spawn_browser(window="normal")
```

Setting `headless=True` is equivalent to `window="headless"`.

### 2. Environment Variable

Set the default window mode across all spawned instances using the `STEALTH_WINDOW_MODE` environment variable:

```bash
export STEALTH_WINDOW_MODE="offscreen"
```

Resolution precedence:
1. Explicit `window` argument passed to `spawn_browser`.
2. `headless=True` flag passed to `spawn_browser`.
3. `STEALTH_WINDOW_MODE` environment variable.
4. Default mode: `offscreen`.

## Headless Fallback Without a Display

On Linux platforms without an active GUI display server (neither `DISPLAY` nor `WAYLAND_DISPLAY` is set in the environment), any non-headless mode automatically falls back to `headless`.

When this fallback occurs, `spawn_browser` reports a diagnostic note in its return dictionary:

```json
{
  "window": "headless",
  "window_note": "No display available (DISPLAY/WAYLAND_DISPLAY unset); using headless."
}
```

On Windows and macOS, display servers are always considered available.

## The No-Focus Guarantee

Stealth Browser MCP ensures that background browser automation never interrupts the user's active workflow:

- **Browser Spawn**: Windows launched in `offscreen` or `minimized` mode never request foreground activation.
- **Navigation**: Navigating to new URLs with `navigate` does not bring the window or tab to the front.
- **New Tabs**: New tabs and tab replacements are created in the background using CDP `Target.createTarget` with `background=True`.
- **Tab Switching**: Switching active tabs via `switch_tab` does not call `bring_to_front()` unless the instance was explicitly launched in `normal` mode.
- **Focus Emulation**: For `offscreen`, `minimized`, and `headless` modes, CDP `Emulation.setFocusEmulationEnabled(True)` is enabled so pages observe `document.hasFocus() === true` and standard DOM visibility states even while not visible to the user.

## Bot-Detection Results

Measured on Linux X11 without GPU running Chrome 158:

| Window Mode | bot.sannysoft.com | creepjs "like headless" | creepjs "headless" | creepjs "stealth" | WebGL Renderer |
|-------------|-------------------|--------------------------|--------------------|-------------------|----------------|
| `normal` | 0 failed rows | 38% | 0% | 0% | None (no GPU: "err") |
| `offscreen` | 0 failed rows | 38% | 0% | 0% | None (no GPU: "err") |
| `minimized` | 0 failed rows | 38% | 0% | 0% | None (no GPU: "err") |
| `headless` | 0 failed rows | 50% | 0% | 0% | SwiftShader |

Across all three headful modes (`normal`, `offscreen`, and `minimized`), bot detection signatures are identical, with creepjs "like headless" scoring 38% across the board -- confirming that window positioning does not alter detection risk. In comparison, `headless` exhibits higher "like headless" suspicion (50%) and exposes SwiftShader software WebGL rendering. Consequently, `offscreen` matches `normal` on detection while offering significantly better stealth than `headless`, and remains the recommended default.

## Verify Placement

For headful modes (`normal`, `minimized`, `offscreen`), `spawn_browser` queries CDP `Browser.getWindowBounds` after applying geometry and includes `window_bounds` (`{left, top, width, height, windowState}`) in the spawn result. When a platform or window manager clamps off-screen positioning (observed on certain X11, Wayland, or macOS desktop environments), `spawn_browser` records the clamped bounds and adds a diagnostic `window_note` (e.g. `"window clamped to 0,0; use window=minimized or headless if it is visible"`). Callers can inspect `window_bounds` and `window_note` in the spawn response to verify actual geometry and position.
