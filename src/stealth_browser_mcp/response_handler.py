"""Response handler for managing large responses and automatic file-based fallbacks."""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import clone_output_dir
from .path_policy import write_mode_0600

UNSAFE_FILENAME_CHARS = re.compile(r"[^\w.-]+")
MAX_PREFIX_LENGTH = 80


class ResponseHandler:
    """Handle large responses by automatically falling back to file-based storage."""
    
    def __init__(self, max_tokens: int = 20000, clone_dir: Optional[str] = None):
        """
        Initialize the response handler.

        Args:
            max_tokens (int): Maximum token estimate before falling back to file storage.
            clone_dir (Optional[str]): Directory to store large response files.
        """
        self.max_tokens = max_tokens
        if clone_dir is None:
            self.clone_dir = clone_output_dir()
        else:
            self.clone_dir = Path(clone_dir)
        self.clone_dir.mkdir(parents=True, exist_ok=True)
    
    def estimate_tokens(self, data: Any) -> int:
        """
        Estimate token count for data (rough approximation).
        
        Args:
            data: The data to estimate tokens for
            
        Returns:
            Estimated token count
        """
        if isinstance(data, (dict, list)):
            # Convert to JSON string and estimate ~4 chars per token
            json_str = json.dumps(data, ensure_ascii=False, default=str)
            return len(json_str) // 4
        elif isinstance(data, str):
            return len(data) // 4
        else:
            return len(str(data)) // 4
    
    def handle_response(
        self, 
        data: Any, 
        fallback_filename_prefix: str = "large_response",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Handle response data, automatically falling back to file storage if too large.
        
        Args:
            data: The response data
            fallback_filename_prefix: Prefix for filename if file storage is needed
            metadata: Additional metadata to include in file response
            
        Returns:
            Either the original data or file storage info if data was too large
        """
        estimated_tokens = self.estimate_tokens(data)
        
        if estimated_tokens <= self.max_tokens:
            # Data is small enough, return as-is
            return data
        
        # Data is too large, save to file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        safe_prefix = UNSAFE_FILENAME_CHARS.sub("_", fallback_filename_prefix).strip("._")[:MAX_PREFIX_LENGTH]
        filename = f"{safe_prefix or 'large_response'}_{timestamp}_{unique_id}.json"
        file_path = self.clone_dir / filename
        
        # Prepare file content with metadata
        file_content = {
            "metadata": {
                "created_at": datetime.now().isoformat(),
                "estimated_tokens": estimated_tokens,
                "auto_saved_due_to_size": True,
                **(metadata or {})
            },
            "data": data
        }
        
        write_mode_0600(
            file_path,
            json.dumps(file_content, indent=2, ensure_ascii=False, default=str),
        )
        
        # Return file info instead of data
        file_size_kb = file_path.stat().st_size / 1024
        
        return {
            "file_path": str(file_path),
            "filename": filename,
            "file_size_kb": round(file_size_kb, 2),
            "estimated_tokens": estimated_tokens,
            "reason": "Response too large, automatically saved to file",
            "metadata": metadata or {}
        }


# Global instance
response_handler = ResponseHandler()
