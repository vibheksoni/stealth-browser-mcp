"""Element and region screenshot clipping helpers for CDP Page.captureScreenshot."""

import base64
import json
from typing import Any, Dict, Optional, Union

import nodriver as uc

from .js_values import evaluate_to_python

GET_ELEMENT_RECT_JS = """(selector) => {
  let el = null;
  try {
    el = document.querySelector(selector);
  } catch (err) {
    return { error: 'invalid_selector', message: err.toString() };
  }
  if (!el) {
    return { error: 'not_found' };
  }
  try {
    if (typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  } catch (e) {}
  const rect = el.getBoundingClientRect();
  const scrollX = window.scrollX !== undefined ? window.scrollX : (window.pageXOffset || 0);
  const scrollY = window.scrollY !== undefined ? window.scrollY : (window.pageYOffset || 0);
  return {
    found: true,
    rect: {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      left: rect.left,
      top: rect.top
    },
    scroll: {
      x: scrollX,
      y: scrollY
    }
  };
}"""


def validate_clip_args(
    selector: Optional[Union[str, Dict[str, Any]]] = None,
    clip: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, float]]:
    """Validate selector and clip arguments for screenshot capture.

    selector and clip are mutually exclusive.

    Args:
        selector (Optional[Union[str, Dict[str, Any]]]): CSS selector string,
            or clip dictionary if passed as the sole positional argument.
        clip (Optional[Dict[str, Any]]): Dictionary with keys 'x', 'y',
            'width', 'height' in CSS pixels.

    Returns:
        Optional[Dict[str, float]]: Validated clip dict if clip was provided,
        or None if selector was provided or both are None.

    Raises:
        ValueError: If both selector and clip are provided, if selector is not
            a valid string, or if clip dictionary has invalid keys or sizes.
    """
    if isinstance(selector, dict) and clip is None:
        clip, selector = selector, None

    if selector is not None and clip is not None:
        raise ValueError("selector and clip are mutually exclusive. Specify either selector or clip, not both.")

    if selector is not None:
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError(f"selector must be a non-empty string, got {selector!r}")
        return None

    if clip is not None:
        if not isinstance(clip, dict):
            raise ValueError(f"clip must be a dictionary, got {type(clip).__name__}")
        required_keys = {"x", "y", "width", "height"}
        missing = required_keys - set(clip.keys())
        if missing:
            raise ValueError(f"clip dictionary is missing required keys: {sorted(missing)}")
        try:
            x = float(clip["x"])
            y = float(clip["y"])
            width = float(clip["width"])
            height = float(clip["height"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"clip coordinates and dimensions must be numeric: {exc}") from exc

        if width <= 0 or height <= 0:
            raise ValueError(f"clip width and height must be positive numbers, got width={width}, height={height}")

        return {"x": x, "y": y, "width": width, "height": height}

    return None


def compute_clip_from_rect(
    rect: Dict[str, Any],
    scroll: Optional[Union[Dict[str, Any], float, int]] = None,
    scroll_y: Optional[Union[float, int]] = None,
    *,
    scroll_x: Optional[Union[float, int]] = None,
) -> Dict[str, float]:
    """Compute page-relative clip coordinates from an element's bounding rect and scroll offsets.

    Args:
        rect (Dict[str, Any]): Bounding client rect with 'width', 'height',
            and 'x'/'left' and 'y'/'top'.
        scroll (Optional[Union[Dict[str, Any], float, int]]): Scroll offsets as
            a dict (with 'x'/'scrollX', 'y'/'scrollY'), or scroll_x offset if numeric.
        scroll_y (Optional[Union[float, int]]): Vertical scroll offset if scroll was scroll_x.
        scroll_x (Optional[Union[float, int]]): Keyword override for horizontal scroll offset.

    Returns:
        Dict[str, float]: Page-relative clip dictionary with 'x', 'y', 'width', 'height'.

    Raises:
        ValueError: If rect has non-positive width or height, missing required fields,
            or non-numeric values.
    """
    if not isinstance(rect, dict):
        raise ValueError(f"rect must be a dictionary, got {type(rect).__name__}")

    try:
        width = float(rect["width"])
        height = float(rect["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"rect must contain numeric 'width' and 'height': {exc}") from exc

    if width <= 0 or height <= 0:
        raise ValueError(f"Element has zero or negative size: width={width}, height={height}")

    rect_x_val = rect.get("x") if rect.get("x") is not None else rect.get("left")
    rect_y_val = rect.get("y") if rect.get("y") is not None else rect.get("top")

    if rect_x_val is None or rect_y_val is None:
        raise ValueError("rect must contain ('x' or 'left') and ('y' or 'top')")

    try:
        rx = float(rect_x_val)
        ry = float(rect_y_val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rect coordinates must be numeric: {exc}") from exc

    sx = 0.0
    sy = 0.0

    if isinstance(scroll, dict):
        sx_val = scroll.get("x") if scroll.get("x") is not None else scroll.get("scrollX", 0.0)
        sy_val = scroll.get("y") if scroll.get("y") is not None else scroll.get("scrollY", 0.0)
        sx = float(sx_val)
        sy = float(sy_val)
    elif isinstance(scroll, (int, float)):
        sx = float(scroll)
        if scroll_y is not None:
            sy = float(scroll_y)
    elif scroll is None:
        if scroll_x is not None:
            sx = float(scroll_x)
        if scroll_y is not None:
            sy = float(scroll_y)
        if scroll_x is None and "scrollX" in rect:
            sx = float(rect["scrollX"])
        if scroll_y is None and "scrollY" in rect:
            sy = float(rect["scrollY"])

    page_x = rx + sx
    page_y = ry + sy

    return {
        "x": float(page_x),
        "y": float(page_y),
        "width": float(width),
        "height": float(height),
    }


async def resolve_element_clip(tab: Any, selector: str) -> Dict[str, float]:
    """Resolve an element selector to page-relative clip coordinates.

    Scrolls the element into view and evaluates its bounding rect and scroll offsets.

    Args:
        tab (Any): nodriver tab.
        selector (str): CSS selector for the element.

    Returns:
        Dict[str, float]: Page-relative clip dictionary {"x", "y", "width", "height"}.

    Raises:
        ValueError: If selector is invalid syntax or element has zero or negative size.
        Exception: If element is not found on the page or evaluation fails.
    """
    expr = f"({GET_ELEMENT_RECT_JS})({json.dumps(selector)})"
    result, error = await evaluate_to_python(tab, expr)
    if error:
        raise Exception(f"Failed to evaluate selector {selector!r}: {error}. Use query_elements to check it.")

    if not isinstance(result, dict):
        raise Exception(f"Element not found for selector: {selector!r}. Use query_elements to check it.")

    if result.get("error") == "not_found" or not result.get("found"):
        raise Exception(f"Element not found for selector: {selector!r}. Use query_elements to check it.")

    if result.get("error") == "invalid_selector":
        msg = result.get("message", "invalid syntax")
        raise ValueError(f"Invalid CSS selector {selector!r}: {msg}. Use query_elements to check it.")

    rect = result.get("rect", {})
    scroll = result.get("scroll", {})

    return compute_clip_from_rect(rect, scroll)


async def capture_clip_screenshot(
    tab: Any,
    clip: Dict[str, float],
    format: str = "png",
    quality: Optional[int] = None,
) -> bytes:
    """Capture a screenshot of a specific clipped region using CDP Page.captureScreenshot.

    Args:
        tab (Any): nodriver tab.
        clip (Dict[str, float]): Region with 'x', 'y', 'width', 'height'.
        format (str): 'png' or 'jpeg'.
        quality (Optional[int]): Compression quality for jpeg format.

    Returns:
        bytes: Raw decoded screenshot bytes.
    """
    viewport = uc.cdp.page.Viewport(
        x=float(clip["x"]),
        y=float(clip["y"]),
        width=float(clip["width"]),
        height=float(clip["height"]),
        scale=1.0,
    )
    cmd = uc.cdp.page.capture_screenshot(
        format_=format,
        quality=quality if format == "jpeg" else None,
        clip=viewport,
        capture_beyond_viewport=True,
    )
    data = await tab.send(cmd)
    if not data:
        raise Exception("Could not take screenshot of region; page may not have finished loading.")
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    elif isinstance(data, (tuple, list)) and len(data) > 0:
        data = data[0]
    return base64.b64decode(data)
