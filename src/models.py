"""Data models for browser MCP server."""

from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
from enum import Enum


class BrowserState(str, Enum):
    """Browser instance states."""
    STARTING = "starting"
    READY = "ready"
    NAVIGATING = "navigating"
    ERROR = "error"
    CLOSED = "closed"


class BrowserInstance(BaseModel):
    """Represents a browser instance."""
    instance_id: str = Field(description="Unique identifier for the browser instance")
    state: BrowserState = Field(default=BrowserState.STARTING)
    current_url: Optional[str] = Field(default=None, description="Current page URL")
    title: Optional[str] = Field(default=None, description="Current page title")
    created_at: datetime = Field(default_factory=datetime.now)
    last_activity: datetime = Field(default_factory=datetime.now)
    headless: bool = Field(default=False)
    user_agent: Optional[str] = None
    viewport: Dict[str, int] = Field(default_factory=lambda: {"width": 1920, "height": 1080})
    
    def update_activity(self):
        """Update last activity timestamp."""
        self.last_activity = datetime.now()


class NetworkRequest(BaseModel):
    """Represents a captured network request."""
    request_id: str = Field(description="Unique request identifier")
    instance_id: str = Field(description="Browser instance that made the request")
    url: str = Field(description="Request URL")
    method: str = Field(description="HTTP method")
    headers: Dict[str, str] = Field(default_factory=dict)
    cookies: Dict[str, str] = Field(default_factory=dict)
    post_data: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)
    resource_type: Optional[str] = None
    
    
class NetworkResponse(BaseModel):
    """Represents a captured network response."""
    request_id: str = Field(description="Associated request ID")
    status: int = Field(description="HTTP status code")
    headers: Dict[str, str] = Field(default_factory=dict)
    content_length: Optional[int] = None
    content_type: Optional[str] = None
    body: Optional[bytes] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class ElementInfo(BaseModel):
    """Information about a DOM element."""
    selector: str = Field(description="CSS selector or XPath")
    tag_name: str = Field(description="HTML tag name")
    text: Optional[str] = Field(default=None, description="Element text content")
    attributes: Dict[str, str] = Field(default_factory=dict)
    is_visible: bool = Field(default=True)
    is_clickable: bool = Field(default=False)
    bounding_box: Optional[Dict[str, float]] = None
    children_count: int = Field(default=0)


class PageState(BaseModel):
    """Complete state snapshot of a page."""
    instance_id: str
    url: str
    title: str
    ready_state: str = Field(description="Document ready state")
    cookies: List[Dict[str, Any]] = Field(default_factory=list)
    local_storage: Dict[str, str] = Field(default_factory=dict)
    session_storage: Dict[str, str] = Field(default_factory=dict)
    console_logs: List[Dict[str, Any]] = Field(default_factory=list)
    viewport: Dict[str, int] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)


class BrowserOptions(BaseModel):
    """Options for spawning a new browser instance."""
    headless: bool = Field(default=False, description="Run browser in headless mode")
    user_agent: Optional[str] = Field(default=None, description="Custom user agent string")
    viewport_width: int = Field(default=1920, description="Viewport width in pixels")
    viewport_height: int = Field(default=1080, description="Viewport height in pixels")
    proxy: Optional[str] = Field(default=None, description="Proxy server URL")
    browser_args: List[str] = Field(default_factory=list, description="Additional browser launch arguments")
    timezone_id: Optional[str] = Field(default=None, description="IANA timezone ID applied via CDP Emulation.setTimezoneOverride")
    idle_timeout_seconds: Optional[int] = Field(default=None, ge=0, description="Idle timeout override in seconds for automatic instance cleanup")
    block_resources: List[str] = Field(default_factory=list, description="Resource types to block")
    extra_headers: Dict[str, str] = Field(default_factory=dict, description="Extra HTTP headers")
    user_data_dir: Optional[str] = Field(default=None, description="Path to user data directory")
    sandbox: bool = Field(default=True, description="Enable browser sandbox mode")


class NavigationOptions(BaseModel):
    """Options for page navigation."""
    wait_until: str = Field(default="load", description="Wait condition: load, domcontentloaded, networkidle")
    timeout: int = Field(default=30000, description="Navigation timeout in milliseconds")
    referrer: Optional[str] = Field(default=None, description="Referrer URL")


class ScriptResult(BaseModel):
    """Result from script execution."""
    success: bool
    result: Any = None
    error: Optional[str] = None
    execution_time: float = Field(description="Execution time in milliseconds")


class ElementAction(str, Enum):
    """Types of element actions."""
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    HOVER = "hover"
    FOCUS = "focus"
    CLEAR = "clear"
    SCREENSHOT = "screenshot"


class HookAction(str, Enum):
    """Types of network hook actions."""
    MODIFY = "modify"
    BLOCK = "block"
    REDIRECT = "redirect"
    FULFILL = "fulfill"
    LOG = "log"


class HookStage(str, Enum):
    """Stages at which hooks can intercept."""
    REQUEST = "request"
    RESPONSE = "response"


class HookStatus(str, Enum):
    """Status of a hook."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    PAUSED = "paused"


class NetworkHook(BaseModel):
    """Represents a network hook rule."""
    hook_id: str = Field(description="Unique hook identifier")
    name: str = Field(description="Human-readable hook name")
    url_pattern: str = Field(description="URL pattern to match (supports wildcards)")
    resource_type: Optional[str] = Field(default=None, description="Resource type filter")
    stage: HookStage = Field(description="When to intercept (request/response)")
    action: HookAction = Field(description="What to do with matched requests")
    status: HookStatus = Field(default=HookStatus.ACTIVE)
    priority: int = Field(default=100, description="Hook priority (lower = higher priority)")
    
    modifications: Dict[str, Any] = Field(default_factory=dict, description="Modifications to apply")
    redirect_url: Optional[str] = Field(default=None, description="URL to redirect to")
    custom_response: Optional[Dict[str, Any]] = Field(default=None, description="Custom response data")
    
    created_at: datetime = Field(default_factory=datetime.now)
    last_triggered: Optional[datetime] = None
    trigger_count: int = Field(default=0, description="Number of times this hook was triggered")


class PendingRequest(BaseModel):
    """Represents a request awaiting modification."""
    request_id: str = Field(description="Fetch request ID")
    instance_id: str = Field(description="Browser instance ID")
    url: str = Field(description="Original request URL")
    method: str = Field(description="HTTP method")
    headers: Dict[str, str] = Field(default_factory=dict)
    post_data: Optional[str] = None
    resource_type: Optional[str] = None
    stage: HookStage = Field(description="Current interception stage")
    
    matched_hooks: List[str] = Field(default_factory=list, description="IDs of hooks that matched")
    modifications: Dict[str, Any] = Field(default_factory=dict, description="Accumulated modifications")
    status: str = Field(default="pending", description="Processing status")
    
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None


class RequestModification(BaseModel):
    """Represents modifications to apply to a request."""
    url: Optional[str] = None
    method: Optional[str] = None  
    headers: Optional[Dict[str, str]] = None
    post_data: Optional[str] = None
    intercept_response: Optional[bool] = None


class ResponseModification(BaseModel):
    """Represents modifications to apply to a response."""
    status_code: Optional[int] = None
    status_text: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    body: Optional[str] = None
