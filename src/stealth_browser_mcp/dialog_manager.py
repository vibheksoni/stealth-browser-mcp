"""JavaScript dialog management for browser instances (SB-POST-4A).

Handles Page.javascriptDialogOpening CDP events according to STEALTH_DIALOG_POLICY:
- dismiss: auto-dismiss dialogs (accept=False), except beforeunload which is always accepted.
- accept: auto-accept dialogs (accept=True), using default prompt text or empty string.
- manual: queue dialogs for manual inspection via get_dialogs and handle_dialog tools.

Always auto-accepts beforeunload dialogs so unsaved-changes prompts never stall runs.
ASCII only. No print statements in package code.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional
import nodriver as uc
from fastmcp.exceptions import ToolError

from .debug_logger import debug_logger

# Ensure nodriver has the alias handle_javascript_dialog pointing to handle_java_script_dialog
if not hasattr(uc.cdp.page, "handle_javascript_dialog"):
    uc.cdp.page.handle_javascript_dialog = getattr(
        uc.cdp.page, "handle_java_script_dialog", None
    )


def get_dialog_policy() -> str:
    """Return dialog policy from STEALTH_DIALOG_POLICY env var (accept|dismiss|manual).

    Defaults to 'dismiss'. Invalid values fall back to 'dismiss'.
    """
    policy = (os.environ.get("STEALTH_DIALOG_POLICY") or "").strip().lower()
    if policy in ("accept", "dismiss", "manual"):
        return policy
    return "dismiss"


def _is_beforeunload(type_val: Any) -> bool:
    """Return True if dialog type represents a beforeunload prompt."""
    if type_val is None:
        return False
    if type_val == getattr(uc.cdp.page.DialogType, "BEFOREUNLOAD", None):
        return True
    raw = str(getattr(type_val, "value", type_val) or "").strip().lower()
    return raw in ("beforeunload", "dialogtype.beforeunload")


def _type_to_str(type_val: Any) -> str:
    """Convert DialogType to a clean lowercase string."""
    if type_val is None:
        return "alert"
    raw = str(getattr(type_val, "value", type_val) or "").strip().lower()
    if "." in raw:
        raw = raw.split(".")[-1]
    return raw or "alert"


class DialogManager:
    """Manages JavaScript dialog event listening, queueing, and dispatch."""

    def __init__(self) -> None:
        self._pending: Dict[str, List[Dict[str, Any]]] = {}
        self._handled: Dict[str, List[Dict[str, Any]]] = {}
        self._configured_targets: set[str] = set()

    def clear_instance(self, instance_id: str) -> None:
        """Clear dialog state for a closed instance."""
        self._pending.pop(instance_id, None)
        self._handled.pop(instance_id, None)

    async def configure_tab(self, tab: Any, instance_id: str) -> None:
        """Enable Page domain and register Page.javascriptDialogOpening on tab."""
        target = getattr(tab, "target", None)
        target_id = str(getattr(target, "target_id", None) or id(tab))
        if target_id in self._configured_targets:
            return

        # Enable the Page domain first
        try:
            enable_cmd = getattr(uc.cdp.page, "enable", None)
            if enable_cmd and hasattr(tab, "send"):
                await tab.send(enable_cmd())
        except Exception as error:
            debug_logger.log_warning(
                "dialog_manager",
                "configure_tab",
                f"Page.enable failed on tab {target_id}: {error}",
            )

        async def on_dialog_opening(event: Any, *args: Any) -> None:
            await self._on_dialog_opening(tab, instance_id, event)

        if hasattr(tab, "add_handler"):
            try:
                tab.add_handler(uc.cdp.page.JavascriptDialogOpening, on_dialog_opening)
                self._configured_targets.add(target_id)
            except Exception as error:
                debug_logger.log_warning(
                    "dialog_manager",
                    "configure_tab",
                    f"Failed to add JavascriptDialogOpening handler: {error}",
                )

    async def _on_dialog_opening(
        self, tab: Any, instance_id: str, event: Any
    ) -> None:
        """Handle incoming Page.javascriptDialogOpening event."""
        url = str(getattr(event, "url", "") or "")
        frame_id = str(getattr(event, "frame_id", "") or "")
        message = str(getattr(event, "message", "") or "")
        type_val = getattr(event, "type_", None)
        type_str = _type_to_str(type_val)
        default_prompt = getattr(event, "default_prompt", None) or ""
        dialog_id = str(uuid.uuid4())[:8]

        handle_fn = getattr(
            uc.cdp.page, "handle_javascript_dialog", None
        ) or getattr(uc.cdp.page, "handle_java_script_dialog", None)

        # Always auto-accept beforeunload, even in manual or dismiss policy
        if _is_beforeunload(type_val):
            try:
                if handle_fn and hasattr(tab, "send"):
                    await tab.send(handle_fn(accept=True))
            except Exception as error:
                debug_logger.log_warning(
                    "dialog_manager",
                    "beforeunload",
                    f"Auto-accept beforeunload failed: {error}",
                )
            self._record_handled(
                instance_id,
                {
                    "dialog_id": dialog_id,
                    "type": "beforeunload",
                    "message": message,
                    "url": url,
                    "frame_id": frame_id,
                    "policy_action": "accept",
                    "pending": False,
                    "handled": True,
                    "default_prompt": default_prompt,
                },
            )
            return

        policy = get_dialog_policy()
        if policy == "accept":
            prompt_to_send = default_prompt if default_prompt else None
            try:
                if handle_fn and hasattr(tab, "send"):
                    await tab.send(handle_fn(accept=True, prompt_text=prompt_to_send))
            except Exception as error:
                debug_logger.log_warning(
                    "dialog_manager",
                    "auto_accept",
                    f"Auto-accept dialog failed: {error}",
                )
            self._record_handled(
                instance_id,
                {
                    "dialog_id": dialog_id,
                    "type": type_str,
                    "message": message,
                    "url": url,
                    "frame_id": frame_id,
                    "policy_action": "accept",
                    "pending": False,
                    "handled": True,
                    "default_prompt": default_prompt,
                },
            )
        elif policy == "dismiss":
            try:
                if handle_fn and hasattr(tab, "send"):
                    await tab.send(handle_fn(accept=False))
            except Exception as error:
                debug_logger.log_warning(
                    "dialog_manager",
                    "auto_dismiss",
                    f"Auto-dismiss dialog failed: {error}",
                )
            self._record_handled(
                instance_id,
                {
                    "dialog_id": dialog_id,
                    "type": type_str,
                    "message": message,
                    "url": url,
                    "frame_id": frame_id,
                    "policy_action": "dismiss",
                    "pending": False,
                    "handled": True,
                    "default_prompt": default_prompt,
                },
            )
        else:  # manual
            entry = {
                "dialog_id": dialog_id,
                "type": type_str,
                "message": message,
                "url": url,
                "frame_id": frame_id,
                "policy_action": "manual",
                "pending": True,
                "handled": False,
                "default_prompt": default_prompt,
                "_tab": tab,
            }
            self._pending.setdefault(instance_id, []).append(entry)

    def _record_handled(self, instance_id: str, record: Dict[str, Any]) -> None:
        """Record a handled dialog in the recent history (capped at 50)."""
        handled = self._handled.setdefault(instance_id, [])
        handled.append(record)
        if len(handled) > 50:
            handled.pop(0)

    def get_dialogs(self, instance_id: str) -> List[Dict[str, Any]]:
        """List pending and recently handled dialogs for an instance."""
        results: List[Dict[str, Any]] = []
        for d in self._pending.get(instance_id, []):
            results.append({
                "dialog_id": d["dialog_id"],
                "type": d["type"],
                "message": d["message"],
                "url": d["url"],
                "frame_id": d["frame_id"],
                "policy_action": d["policy_action"],
                "pending": True,
                "handled": False,
                "default_prompt": d.get("default_prompt", ""),
            })
        for d in self._handled.get(instance_id, []):
            results.append({
                "dialog_id": d["dialog_id"],
                "type": d["type"],
                "message": d["message"],
                "url": d["url"],
                "frame_id": d["frame_id"],
                "policy_action": d["policy_action"],
                "pending": False,
                "handled": True,
                "default_prompt": d.get("default_prompt", ""),
            })
        return results

    async def handle_dialog(
        self,
        instance_id: str,
        accept: bool = True,
        prompt_text: Optional[str] = None,
        dialog_id: Optional[str] = None,
        action: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Accept or dismiss a pending dialog."""
        if action is not None:
            raw_action = str(action).strip().lower()
            if raw_action in ("dismiss", "cancel", "false"):
                accept = False
            elif raw_action in ("accept", "ok", "confirm", "true"):
                accept = True

        pending_list = self._pending.get(instance_id, [])
        if not pending_list:
            raise ToolError(f"No pending dialog for instance {instance_id}")

        target_dialog: Optional[Dict[str, Any]] = None
        if dialog_id:
            for item in pending_list:
                if item["dialog_id"] == dialog_id:
                    target_dialog = item
                    pending_list.remove(item)
                    break
            if target_dialog is None:
                raise ToolError(f"Dialog '{dialog_id}' not found for instance {instance_id}")
        else:
            target_dialog = pending_list.pop(0)

        tab = target_dialog.get("_tab")
        handle_fn = getattr(
            uc.cdp.page, "handle_javascript_dialog", None
        ) or getattr(uc.cdp.page, "handle_java_script_dialog", None)

        prompt_val = (
            prompt_text
            if prompt_text is not None
            else target_dialog.get("default_prompt", "")
        )
        if tab and hasattr(tab, "send") and handle_fn:
            try:
                await tab.send(
                    handle_fn(
                        accept=accept,
                        prompt_text=prompt_val if accept and prompt_val else None,
                    )
                )
            except Exception as error:
                debug_logger.log_error(
                    "dialog_manager",
                    "handle_dialog",
                    error,
                )
                raise ToolError(f"Failed sending handleJavaScriptDialog: {error}") from error

        action_taken = "accept" if accept else "dismiss"
        clean_record = {
            "dialog_id": target_dialog["dialog_id"],
            "type": target_dialog["type"],
            "message": target_dialog["message"],
            "url": target_dialog["url"],
            "frame_id": target_dialog["frame_id"],
            "policy_action": action_taken,
            "pending": False,
            "handled": True,
            "default_prompt": target_dialog.get("default_prompt", ""),
        }
        self._record_handled(instance_id, clean_record)

        return {
            "success": True,
            "dialog_id": target_dialog["dialog_id"],
            "type": target_dialog["type"],
            "message": target_dialog["message"],
            "action": action_taken,
        }
