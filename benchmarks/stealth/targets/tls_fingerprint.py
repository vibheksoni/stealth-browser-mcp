"""TLS and HTTP/2 fingerprint target backed by tls.peet.ws."""

import json
from typing import Any, Dict, List, Tuple

from ..results import Check
from .base import Session, Target, Unreachable, wait_for_value

READ_JSON_JS = """(() => {
  const pre = document.querySelector('pre');
  const text = (pre ? pre.innerText : (document.body ? document.body.innerText : '')).trim();
  if (!text.startsWith('{')) return null;
  try {
    JSON.parse(text);
    return text;
  } catch (error) {
    return null;
  }
})()"""

CHROME_AKAMAI_FINGERPRINT = "1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p"
CHROME_JA4_CIPHER_HASH = "8daaf6152771"
PRIVATE_KEYS = ("ip",)


class TLSFingerprintTarget(Target):
    """
    Records the TLS (JA3, JA4, PeetPrint) and HTTP/2 (Akamai) fingerprints the
    server sees. The header user agent and protocol checks are critical, the
    match against known Chrome fingerprints only degrades the target because
    those values shift with Chrome releases.
    """

    name = "tls"
    title = "TLS and HTTP/2 fingerprint"
    category = "Network fingerprinting"
    url = "https://tls.peet.ws/api/all"
    timeout = 60.0

    async def collect(self, session: Session) -> Dict[str, Any]:
        """
        Load the fingerprint API and parse its JSON.

        Args:
            session (Session): Active browser session

        Returns:
            Dict[str, Any]: API response without the client IP address
        """
        await session.navigate(self.resolve_url(session), timeout=40.0)
        text = await wait_for_value(session, READ_JSON_JS, timeout=15.0)
        try:
            payload = json.loads(text)
        except ValueError as error:
            raise Unreachable(f"Fingerprint API returned invalid JSON: {error}") from error
        for key in PRIVATE_KEYS:
            payload.pop(key, None)
        return payload

    def evaluate(self, payload: Dict[str, Any]) -> Tuple[List[Check], Dict[str, Any], str]:
        """
        Turn the fingerprint response into checks.

        Args:
            payload (Dict[str, Any]): Data returned by collect()

        Returns:
            Tuple[List[Check], Dict[str, Any], str]: Checks, details, and summary
        """
        tls = payload.get("tls") or {}
        http2 = payload.get("http2") or {}
        user_agent = payload.get("user_agent") or ""
        ja4 = tls.get("ja4") or ""
        ja4_parts = ja4.split("_")
        akamai = http2.get("akamai_fingerprint") or ""
        checks = [
            Check("header user agent has no HeadlessChrome", "HeadlessChrome" not in user_agent, True, user_agent),
            Check("HTTP/2 negotiated", payload.get("http_version") == "h2", True, payload.get("http_version")),
            Check("TLS 1.3 negotiated", ja4.startswith("t13"), True, ja4[:10]),
            Check("Chrome HTTP/2 fingerprint", akamai == CHROME_AKAMAI_FINGERPRINT, False, akamai),
            Check(
                "Chrome JA4 cipher hash",
                len(ja4_parts) > 1 and ja4_parts[1] == CHROME_JA4_CIPHER_HASH,
                False,
                ja4_parts[1] if len(ja4_parts) > 1 else None,
            ),
        ]
        details = {
            "user_agent": user_agent,
            "http_version": payload.get("http_version"),
            "ja3_hash": tls.get("ja3_hash"),
            "ja4": ja4 or None,
            "peetprint_hash": tls.get("peetprint_hash"),
            "akamai_fingerprint": akamai or None,
            "akamai_fingerprint_hash": http2.get("akamai_fingerprint_hash"),
        }
        summary = f"JA4 {ja4 or 'unknown'}, HTTP/2 {http2.get('akamai_fingerprint_hash') or 'unknown'}"
        return checks, details, summary
