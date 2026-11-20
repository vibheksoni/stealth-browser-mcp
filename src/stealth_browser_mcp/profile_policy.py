"""Managed Chrome profile policy parser and classifier (SB-70).

ASCII only. Pure functions for inspecting chrome://policy rows,
identifying enterprise management status, and classifying blockers,
warnings, and notes that affect CDP automation.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence


KNOWN_POLICIES: Dict[str, Dict[str, Any]] = {
    "RemoteDebuggingAllowed": {
        "category": "blocker",
        "effect": "Remote debugging is disallowed (RemoteDebuggingAllowed=false), blocking --remote-debugging-port and remote debugging pipe.",
        "what_to_do": "Request IT administrator to enable RemoteDebuggingAllowed or use a separate unmanaged profile.",
    },
    "DeveloperToolsAvailability": {
        "category": "warning",
        "effect": "Developer tools are disallowed (DeveloperToolsAvailability=2); may block CDP-driven automation tools in some builds.",
        "what_to_do": "Request IT administrator to set DeveloperToolsAvailability to 0 or 1, or use an unmanaged profile.",
    },
    "ExtensionInstallForcelist": {
        "category": "warning",
        "effect": "Enterprise extensions are force-installed and may inspect or alter page behavior.",
        "what_to_do": "Review forced extensions to ensure they do not conflict with automation or navigation.",
    },
    "ExtensionInstallBlocklist": {
        "category": "warning",
        "effect": "Extension installation is restricted by enterprise policy.",
        "what_to_do": "Ensure required automation extensions are allowlisted if extension support is needed.",
    },
    "ExtensionSettings": {
        "category": "warning",
        "effect": "Enterprise extension settings are enforced, potentially forcing or blocking extensions.",
        "what_to_do": "Check extension management settings for conflicts with automation tools or extensions.",
    },
    "URLBlocklist": {
        "category": "warning",
        "effect": "Navigation to specified URLs or patterns is blocked by enterprise policy.",
        "what_to_do": "Ensure target automation URLs are not blocked, or add them to URLAllowlist.",
    },
    "URLAllowlist": {
        "category": "warning",
        "effect": "Enterprise URL allowlist is enforced; navigation outside allowlisted domains may be blocked.",
        "what_to_do": "Verify all domains accessed during automation are included in URLAllowlist.",
    },
    "BrowserSignin": {
        "category": "note",
        "effect": "Browser sign-in policy is configured.",
        "what_to_do": "Sign in manually in the dedicated profile if corporate account authentication is required.",
    },
    "ForceBrowserSignin": {
        "category": "note",
        "effect": "Browser sign-in is required before Chrome can be used.",
        "what_to_do": "Complete human SSO sign-in once in the dedicated automation profile before running tasks.",
    },
    "CommandLineFlagSecurityWarningsEnabled": {
        "category": "note",
        "effect": "Security warnings for custom command line flags are enabled.",
        "what_to_do": "Informational warnings may appear when passing custom flags; verify flags are accepted.",
    },
    "SafeBrowsingProtectionLevel": {
        "category": "note",
        "effect": "Safe Browsing policy is active and may inspect or restrict downloads and navigations.",
        "what_to_do": "Allowlist internal testing domains or download endpoints if false positives occur.",
    },
    "SafeBrowsingEnabled": {
        "category": "note",
        "effect": "Safe Browsing policy is active and may inspect or restrict downloads and navigations.",
        "what_to_do": "Allowlist internal testing domains or download endpoints if false positives occur.",
    },
    "IncognitoModeAvailability": {
        "category": "warning",
        "effect": "Incognito mode is disabled (IncognitoModeAvailability=1).",
        "what_to_do": "Run automation in a persistent or dedicated profile rather than incognito mode.",
    },
    "DownloadRestrictions": {
        "category": "warning",
        "effect": "File downloads are restricted by enterprise policy.",
        "what_to_do": "Verify download endpoints and file types comply with enterprise download restrictions.",
    },
    "PasswordManagerEnabled": {
        "category": "note",
        "effect": "Built-in password manager policy is configured.",
        "what_to_do": "Do not rely on the browser password manager for agent credentials; use secure environment variables.",
    },
    "SyncDisabled": {
        "category": "note",
        "effect": "Chrome Sync is disabled by enterprise policy.",
        "what_to_do": "Browser state, bookmarks, and extensions will not sync to other profiles or devices.",
    },
    "CloudManagementEnrollmentToken": {
        "category": "note",
        "effect": "Machine-level cloud management enrollment token is present.",
        "what_to_do": "Browser is enrolled in Google Workspace / CBCM; policies apply across all profiles.",
    },
    "MachineLevelUserCloudPolicyEnrollmentToken": {
        "category": "note",
        "effect": "Machine-level cloud management enrollment token is present.",
        "what_to_do": "Browser is enrolled in Google Workspace / CBCM; policies apply across all profiles.",
    },
    "DeviceTrustConnectorEnabled": {
        "category": "warning",
        "effect": "Context-aware access and Device Trust Connector are active; device-bound sessions may not transfer to a separate profile.",
        "what_to_do": "Device-bound cookies cannot be cloned to other profiles; automate inside the authorized profile directly.",
    },
}

SCRAPE_POLICY_JS = """(() => {
    const rows = [];
    const extractFromRoot = (root) => {
        if (!root) return;
        const candidates = root.querySelectorAll ? root.querySelectorAll('.policy-row, policy-row, tr, [role="row"], .row') : [];
        for (const el of candidates) {
            const sub = el.shadowRoot || el;
            const nameEl = sub.querySelector('.name, .policy-name, [class*="name"]');
            if (!nameEl) continue;
            const name = (nameEl.textContent || '').trim();
            if (!name || name.toLowerCase() === 'policy name' || name.toLowerCase() === 'name') continue;
            const valEl = sub.querySelector('.value, .policy-value, [class*="value"]');
            const srcEl = sub.querySelector('.source, .policy-source, [class*="source"]');
            const scpEl = sub.querySelector('.scope, .policy-scope, [class*="scope"]');
            const lvlEl = sub.querySelector('.level, .policy-level, [class*="level"]');
            const stEl = sub.querySelector('.status, .policy-status, [class*="status"]');
            rows.push({
                name: name,
                value: valEl ? (valEl.textContent || '').trim() : '',
                source: srcEl ? (srcEl.textContent || '').trim() : '',
                scope: scpEl ? (scpEl.textContent || '').trim() : '',
                level: lvlEl ? (lvlEl.textContent || '').trim() : '',
                status: stEl ? (stEl.textContent || '').trim() : ''
            });
        }
    };

    const tables = document.querySelectorAll('policy-table');
    if (tables.length > 0) {
        for (const t of tables) {
            extractFromRoot(t.shadowRoot || t);
        }
    }
    if (rows.length === 0) {
        const walk = (node) => {
            if (!node) return;
            if (node.shadowRoot) {
                extractFromRoot(node.shadowRoot);
                walk(node.shadowRoot);
            }
            const children = node.children || [];
            for (let i = 0; i < children.length; i++) {
                walk(children[i]);
            }
        };
        walk(document.body || document.documentElement);
    }
    return rows;
})()"""


def _normalize_value(val: Any) -> Any:
    """Normalize string and primitive policy values."""
    if isinstance(val, str):
        stripped = val.strip()
        lowered = stripped.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return int(stripped)
        except ValueError:
            pass
        try:
            return json.loads(stripped)
        except Exception:
            pass
        return stripped
    return val


def parse_policy_rows(rows: Any) -> List[Dict[str, Any]]:
    """
    Parse and normalize policy rows scraped from chrome://policy.

    Args:
        rows: Sequence of dicts with policy attributes (name, value, source, level, status, scope).

    Returns:
        List[Dict[str, Any]]: Cleaned and normalized policy row dictionaries.
    """
    if not isinstance(rows, (list, tuple)):
        return []

    parsed: List[Dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if raw_name is None:
            continue
        name = str(raw_name).strip()
        if not name:
            continue

        raw_val = item.get("value")
        norm_val = _normalize_value(raw_val)

        source = str(item.get("source") or "").strip()
        level = str(item.get("level") or "").strip()
        status = str(item.get("status") or "").strip()
        scope = str(item.get("scope") or "").strip()

        parsed.append({
            "name": name,
            "value": norm_val,
            "source": source,
            "level": level,
            "status": status,
            "scope": scope,
        })

    return parsed


def classify_policies(rows: Any) -> Dict[str, Any]:
    """
    Classify policy rows into management status, blockers, warnings, and notes.

    Args:
        rows: Raw or parsed policy row dictionaries.

    Returns:
        Dict[str, Any]: Classification dictionary with:
            - managed (bool): True if corporate policies are present.
            - blockers (List[Dict[str, str]]): Policies preventing automation.
            - warnings (List[Dict[str, str]]): Policies that may interfere with automation.
            - notes (List[Dict[str, str]]): Informational enterprise settings.
            Each item in blockers, warnings, and notes is {"policy", "effect", "what_to_do"}.
    """
    parsed = parse_policy_rows(rows)
    if not parsed:
        return {
            "managed": False,
            "blockers": [],
            "warnings": [],
            "notes": [],
        }

    blockers_by_policy: Dict[str, Dict[str, str]] = {}
    warnings_by_policy: Dict[str, Dict[str, str]] = {}
    notes_by_policy: Dict[str, Dict[str, str]] = {}

    for row in parsed:
        name = row["name"]
        val = row["value"]

        # 1. RemoteDebuggingAllowed (blocker)
        if name == "RemoteDebuggingAllowed":
            if val is False or val in (0, "0", "false", "False"):
                info = KNOWN_POLICIES["RemoteDebuggingAllowed"]
                blockers_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 2. DeveloperToolsAvailability (warning)
        elif name == "DeveloperToolsAvailability":
            if val in (2, "2"):
                info = KNOWN_POLICIES["DeveloperToolsAvailability"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 3. ExtensionInstallForcelist (warning)
        elif name == "ExtensionInstallForcelist":
            if val not in (None, "", [], {}):
                info = KNOWN_POLICIES["ExtensionInstallForcelist"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 4. ExtensionInstallBlocklist (warning)
        elif name == "ExtensionInstallBlocklist":
            if val not in (None, "", [], {}):
                info = KNOWN_POLICIES["ExtensionInstallBlocklist"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 5. ExtensionSettings (warning)
        elif name == "ExtensionSettings":
            if val not in (None, "", [], {}):
                info = KNOWN_POLICIES["ExtensionSettings"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 6. URLBlocklist (warning)
        elif name == "URLBlocklist":
            if val not in (None, "", [], {}):
                info = KNOWN_POLICIES["URLBlocklist"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 7. URLAllowlist (warning)
        elif name == "URLAllowlist":
            if val not in (None, "", [], {}):
                info = KNOWN_POLICIES["URLAllowlist"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 8. BrowserSignin (note)
        elif name == "BrowserSignin":
            if val not in (None, ""):
                info = KNOWN_POLICIES["BrowserSignin"]
                notes_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 9. ForceBrowserSignin (note)
        elif name == "ForceBrowserSignin":
            if val in (True, 1, "1", "true", "True"):
                info = KNOWN_POLICIES["ForceBrowserSignin"]
                notes_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 10. CommandLineFlagSecurityWarningsEnabled (note)
        elif name == "CommandLineFlagSecurityWarningsEnabled":
            if val not in (None, "", False, 0, "0", "false"):
                info = KNOWN_POLICIES["CommandLineFlagSecurityWarningsEnabled"]
                notes_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 11. SafeBrowsing* (note)
        elif name.startswith("SafeBrowsing"):
            notes_by_policy[name] = {
                "policy": name,
                "effect": "Safe Browsing policy is active and may inspect or restrict downloads and navigations.",
                "what_to_do": "Allowlist internal testing domains or download endpoints if false positives occur.",
            }

        # 12. IncognitoModeAvailability (warning)
        elif name == "IncognitoModeAvailability":
            if val in (1, "1"):
                info = KNOWN_POLICIES["IncognitoModeAvailability"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 13. DownloadRestrictions (warning)
        elif name == "DownloadRestrictions":
            if val not in (0, "0", False, None, ""):
                info = KNOWN_POLICIES["DownloadRestrictions"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 14. PasswordManagerEnabled (note)
        elif name == "PasswordManagerEnabled":
            if val not in (None, ""):
                info = KNOWN_POLICIES["PasswordManagerEnabled"]
                notes_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 15. SyncDisabled (note)
        elif name == "SyncDisabled":
            if val in (True, 1, "1", "true", "True"):
                info = KNOWN_POLICIES["SyncDisabled"]
                notes_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 16. CloudManagementEnrollmentToken (note)
        elif name in ("CloudManagementEnrollmentToken", "MachineLevelUserCloudPolicyEnrollmentToken"):
            if val not in (None, ""):
                info = KNOWN_POLICIES["CloudManagementEnrollmentToken"]
                notes_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

        # 17. DeviceTrustConnectorEnabled (warning)
        elif name == "DeviceTrustConnectorEnabled":
            if val in (True, 1, "1", "true", "True"):
                info = KNOWN_POLICIES["DeviceTrustConnectorEnabled"]
                warnings_by_policy[name] = {
                    "policy": name,
                    "effect": info["effect"],
                    "what_to_do": info["what_to_do"],
                }

    blockers = sorted(blockers_by_policy.values(), key=lambda x: x["policy"])
    warnings = sorted(warnings_by_policy.values(), key=lambda x: x["policy"])
    notes = sorted(notes_by_policy.values(), key=lambda x: x["policy"])

    return {
        "managed": True,
        "blockers": blockers,
        "warnings": warnings,
        "notes": notes,
    }
