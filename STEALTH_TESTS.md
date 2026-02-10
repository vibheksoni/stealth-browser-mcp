# Stealth Detection Test Results

Verified bypass results for stealth-browser-mcp against major bot detection systems.

**Test Date:** 2026-02-10
**Browser:** Chrome 144 (nodriver)
**Platform:** Windows 10 (Win64)
**Mode:** Non-headless

---

## 1. Cloudflare Challenge (nowsecure.nl)

**Result: PASSED**

The browser navigated through Cloudflare's bot challenge without any manual intervention.
The page resolved to the "NOWSECURE BY NODRIVER" victory screen, confirming full bypass
of Cloudflare's JavaScript challenge, TLS fingerprinting, and behavioral analysis.

---

## 2. CreepJS Fingerprint Analysis

**Result: PASSED**

CreepJS performs deep browser fingerprinting across dozens of vectors. Key findings:

| Detection Vector | Result |
|-----------------|--------|
| Headless detected | 0% |
| Stealth detected | 0% |
| Like headless | 25% (chromium flag, normal for real Chrome) |
| Privacy resistance | unknown (no fingerprint blocking detected) |
| Security resistance | unknown (clean) |
| Extension detected | unknown (none) |

**Fingerprint consistency:**
- GPU: NVIDIA GeForce RTX 2070 SUPER (real hardware)
- Platform: Windows 10 (64-bit), 16 cores, 8GB RAM
- Timezone: America/New_York (Eastern Standard Time)
- Language: en-US
- WebRTC: Real local/STUN candidates exposed
- Confidence: moderate (consistent fingerprint)

---

## 3. Sannysoft Bot Detection

**Result: 20/20 TESTS PASSED**

Every single detection vector returned "ok" or consistent values.

| Test | Result |
|------|--------|
| PHANTOM_UA | ok |
| PHANTOM_PROPERTIES | ok |
| PHANTOM_ETSL | ok |
| PHANTOM_LANGUAGE | ok |
| PHANTOM_WEBSOCKET | ok |
| MQ_SCREEN | ok |
| PHANTOM_OVERFLOW | ok |
| PHANTOM_WINDOW_HEIGHT | ok |
| HEADCHR_UA | ok |
| HEADCHR_CHROME_OBJ | ok |
| HEADCHR_PERMISSIONS | ok |
| HEADCHR_PLUGINS | ok |
| HEADCHR_IFRAME | ok |
| CHR_DEBUG_TOOLS | ok |
| SELENIUM_DRIVER | ok |
| CHR_BATTERY | ok |
| CHR_MEMORY | ok |
| TRANSPARENT_PIXEL | ok |
| SEQUENTUM | ok |
| VIDEO_CODECS | ok |

**Additional consistent properties:**
- User Agent: Standard Chrome 144 UA string
- WebDriver: missing (passed - not detected as automated)
- Chrome object: present (passed - real Chrome API)
- Permissions: prompt (normal behavior)
- Plugins: 5 (standard Chrome PDF plugins)
- Languages: en-US, en
- WebGL Vendor: Google Inc. (NVIDIA)
- Broken Image: 16x16 (correct dimensions)
- Canvas fingerprints: All consistent hashes across contexts

---

## 4. Intoli Headless Detection (Round II)

**Result: ALL TESTS PASSED**

| Test | Result |
|------|--------|
| User Agent (Old) | Chrome/144.0.0.0 (clean) |
| WebDriver (New) | missing (passed) |
| WebDriver Advanced | pass |
| Chrome (New) | present (passed) |
| Permissions (New) | prompt (normal) |
| Plugins Length (Old) | 5 (correct) |
| Plugins type | PluginArray (pass) |
| Languages (Old) | en-US, en (consistent) |
| WebGL Vendor | Google Inc. (NVIDIA) |
| WebGL Renderer | ANGLE (NVIDIA GeForce RTX 2070 SUPER) |
| Broken Image Dimensions | 16x16 (correct) |

---

## 5. X.com (Twitter) Login Wall Bypass

**Result: BYPASSED**

Navigated to `x.com/elonmusk` which presents a login modal blocking content.
The browser loaded the full profile data behind the modal. After removing the
overlay via DOM manipulation, all profile data was accessible:

- Display name, handle, bio, join date
- Follower/following counts (234.3M followers)
- Full tweet timeline with engagement metrics
- All data-testid attributes queryable

This demonstrates real-world scraping capability against a major platform
with aggressive bot detection (Arkose Labs/FunCaptcha integration).

---

## Summary

| Detection System | Type | Result |
|-----------------|------|--------|
| Cloudflare | JS Challenge + TLS | PASSED |
| CreepJS | Deep Fingerprinting | 0% stealth / 0% headless |
| Sannysoft | Bot Detection Suite | 20/20 tests passed |
| Intoli | Headless Detection | All tests passed |
| X.com | Login Wall + Arkose | Bypassed, data scraped |

All tests confirm that stealth-browser-mcp running on nodriver is undetectable
by current industry-standard bot detection systems.
