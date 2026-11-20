# Managed Chrome Profiles and Enterprise Policies

This guide describes how to use Stealth Browser MCP with enterprise-managed Google Workspace Chrome profiles, explains common policy blockers, and outlines the recommended setup for automation.

## Fresh Temporary Profiles vs. Corporate Profiles

By default, `spawn_browser()` starts Chrome with a fresh, temporary profile directory (`uc_*`). This temporary profile:
- Does NOT inherit the user's corporate Google Workspace sign-in, session tokens, or SSO credentials.
- Operates independently from local profile data, avoiding accidental corruption of corporate browser state.
- May still be subject to machine-level cloud policies if the host OS is enrolled in Chrome Browser Cloud Management (CBCM).

## Working Inside a Dedicated Profile

To automate workflows that require corporate authentication, Chrome must be launched with remote debugging enabled and a dedicated user data directory:

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir=/path/to/dedicated-profile
```

Important Chrome constraints:
- **Chrome 136+ flag enforcement**: Remote debugging flags (`--remote-debugging-port`, `--remote-debugging-pipe`) are silently IGNORED by Chrome when `--user-data-dir` points to the default system profile directory. A non-default directory must be specified.
- **Separate sign-in required**: Because a dedicated profile directory is used, the user must perform SSO and two-factor authentication (2FA) in that profile once before running automation.

## Attaching to an Existing Browser

Attaching directly to an already running Chrome process via CDP debugging port or host is planned for SB-69 and does not yet exist in this checkout. Currently, browser sessions are managed via `spawn_browser()`, which launches and monitors instances directly.

## Enterprise Policy Restrictions

Corporate Google Workspace and Endpoint Management environments enforce policies via `chrome://policy`. Key policies that affect automation include:

| Policy | Level | Effect on Automation | Recommended Action |
|--------|-------|----------------------|--------------------|
| `RemoteDebuggingAllowed` | Blocker | Setting to `false` disables all CDP remote debugging ports and pipes. | Request IT exemption or use an unmanaged system. |
| `DeveloperToolsAvailability` | Warning | Setting to `2` completely disables DevTools and CDP access in some builds. | Request setting `0` or `1`, or run outside managed profile. |
| `ExtensionInstallForcelist` | Warning | Force-installs corporate extensions (Endpoint Verification, DLP agents). | Verify extensions do not break test automation flows. |
| `ExtensionSettings` | Warning | Enforces installation and permission rules for extensions. | Ensure automation extensions are allowlisted if required. |
| `URLBlocklist` / `URLAllowlist` | Warning | Restricts navigations to approved enterprise domains. | Add automation target domains to `URLAllowlist`. |
| `IncognitoModeAvailability` | Warning | Disables incognito mode (`1`), preventing private session launches. | Use standard or persistent profile directories. |
| `DownloadRestrictions` | Warning | Blocks or inspects file downloads based on risk or file type. | Check whether target download formats are restricted. |
| `DeviceTrustConnectorEnabled` | Warning | Enforces context-aware access and device-bound session tokens. | Automate within the authorized profile directly. |
| `BrowserSignin` / `ForceBrowserSignin` | Note | Requires corporate Google account sign-in before browsing. | Sign in manually once in the dedicated automation profile. |
| `PasswordManagerEnabled` | Note | Governs browser-stored passwords. | Use environment variables rather than the browser store. |
| `SyncDisabled` | Note | Disables profile synchronization across devices. | Keep profile state self-contained in the local user data dir. |

### Device-Bound Sessions and App-Bound Encryption
Modern Google Workspace environments often enforce Device Trust (Endpoint Verification) and Chrome App-Bound Encryption. Session cookies from the user's primary profile cannot be decrypted or copied into another profile directory. Automation must run directly within the authenticated profile session.

## Checking Policies with profile_policy_report

To inspect the active policies of a running instance, call `profile_policy_report`:

```python
report = await profile_policy_report(instance_id="...")
```

The tool inspects `chrome://policy` via shadow DOM, classifies all discovered settings, and returns:
- `managed`: `true` if enterprise policies are present, `false` otherwise.
- `blockers`: Critical policies that prevent automation (e.g., `RemoteDebuggingAllowed=false`).
- `warnings`: Policies that may disrupt navigation, extensions, downloads, or session persistence.
- `notes`: Informational settings regarding sync, sign-in, and cloud management.
- `policy_count`: Total raw policy entries found on `chrome://policy`.

## Recommended Enterprise Setup

1. **Dedicated User Data Directory**: Never reuse or point automation at the primary daily-driver profile. Create a separate automation folder (e.g. `~/.config/google-chrome-automation`).
2. **Initial Human Sign-In**: Open the dedicated profile once manually in normal mode. Complete SSO, hardware security key, or 2FA prompts.
3. **Offscreen Window Mode**: Run automated sessions using `window="offscreen"` (see [WINDOW_MODES.md](WINDOW_MODES.md)) to avoid interrupting desktop work.
4. **Credential Security**: Never hardcode or pass corporate passwords to the agent.
5. **IT Approval**: Confirm automated access complies with your organization's security policy.
