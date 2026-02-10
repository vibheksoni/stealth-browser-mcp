"""
Dynamic Hook AI Interface - Functions for AI to create and manage dynamic hooks

This module provides AI-friendly functions for creating, managing, and learning
about dynamic hook functions.
"""

from typing import Dict, List, Any, Optional
from dynamic_hook_system import dynamic_hook_system
from hook_learning_system import hook_learning_system
from debug_logger import debug_logger
import json


class DynamicHookAIInterface:
    """AI interface for dynamic hook system."""
    
    def __init__(self):
        self.hook_system = dynamic_hook_system
        self.learning_system = hook_learning_system
    
    async def create_dynamic_hook(self, name: str, requirements: Dict[str, Any], 
                                 function_code: str, instance_ids: Optional[List[str]] = None,
                                 priority: int = 100) -> Dict[str, Any]:
        """
        Create a new dynamic hook with AI-generated function.
        
        Args:
            name: Human-readable hook name
            requirements: Dictionary of matching criteria (url_pattern, method, etc.)
            function_code: Python function code that processes requests
            instance_ids: Browser instances to apply hook to (all if None) 
            priority: Hook priority (lower = higher priority)
            
        Returns:
            Dict with hook_id and status
        """
        try:
            validation = self.learning_system.validate_hook_function(function_code)
            if not validation["valid"]:
                return {
                    "success": False,
                    "error": "Invalid function code",
                    "issues": validation["issues"],
                    "warnings": validation["warnings"]
                }
            
            hook_id = await self.hook_system.create_hook(
                name=name,
                requirements=requirements,
                function_code=function_code,
                instance_ids=instance_ids,
                priority=priority
            )
            
            result = {
                "success": True,
                "hook_id": hook_id,
                "message": f"Created dynamic hook '{name}' with ID {hook_id}"
            }
            
            if validation["warnings"]:
                result["warnings"] = validation["warnings"]
            
            return result
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "create_dynamic_hook", f"Failed to create hook {name}: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def list_dynamic_hooks(self, instance_id: Optional[str] = None) -> Dict[str, Any]:
        """
        List all dynamic hooks.
        
        Args:
            instance_id: Optional filter by browser instance
            
        Returns:
            Dict with hooks list and count
        """
        try:
            hooks = self.hook_system.list_hooks()
            
            if instance_id:
                instance_hook_ids = self.hook_system.instance_hooks.get(instance_id, [])
                filtered_hooks = []
                for hook in hooks:
                    hook_id = hook.get("hook_id") or hook.get("id")
                    if hook_id and hook_id in instance_hook_ids:
                        filtered_hooks.append(hook)
                hooks = filtered_hooks
            
            return {
                "success": True,
                "hooks": hooks,
                "count": len(hooks)
            }
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "list_dynamic_hooks", f"Failed to list hooks: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def get_hook_details(self, hook_id: str) -> Dict[str, Any]:
        """
        Get detailed information about a specific hook.
        
        Args:
            hook_id: Hook identifier
            
        Returns:
            Dict with detailed hook information
        """
        try:
            hook_details = self.hook_system.get_hook_details(hook_id)
            
            if not hook_details:
                return {
                    "success": False,
                    "error": f"Hook {hook_id} not found"
                }
            
            return {
                "success": True,
                "hook": hook_details
            }
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "get_hook_details", f"Failed to get hook details: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def remove_dynamic_hook(self, hook_id: str) -> Dict[str, Any]:
        """
        Remove a dynamic hook.
        
        Args:
            hook_id: Hook identifier to remove
            
        Returns:
            Dict with removal status
        """
        try:
            success = await self.hook_system.remove_hook(hook_id)
            
            if success:
                return {
                    "success": True,
                    "message": f"Removed hook {hook_id}"
                }
            else:
                return {
                    "success": False,
                    "error": f"Hook {hook_id} not found"
                }
                
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "remove_dynamic_hook", f"Failed to remove hook: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def get_request_documentation(self) -> Dict[str, Any]:
        """
        Get comprehensive documentation of the request object for AI learning.
        
        Returns:
            Dict with request object documentation
        """
        try:
            return {
                "success": True,
                "documentation": self.learning_system.get_request_object_documentation()
            }
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "get_request_documentation", f"Failed to get documentation: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def get_hook_examples(self) -> Dict[str, Any]:
        """
        Get example hook functions for AI learning.
        
        Returns:
            Dict with hook examples and explanations
        """
        try:
            return {
                "success": True,
                "examples": self.learning_system.get_hook_examples()
            }
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "get_hook_examples", f"Failed to get examples: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def get_requirements_documentation(self) -> Dict[str, Any]:
        """
        Get documentation on hook requirements and matching criteria.
        
        Returns:
            Dict with requirements documentation
        """
        try:
            return {
                "success": True,
                "documentation": self.learning_system.get_requirements_documentation()
            }
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "get_requirements_documentation", f"Failed to get requirements docs: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def get_common_patterns(self) -> Dict[str, Any]:
        """
        Get common hook patterns and use cases.
        
        Returns:
            Dict with common patterns
        """
        try:
            return {
                "success": True,
                "patterns": self.learning_system.get_common_patterns()
            }
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "get_common_patterns", f"Failed to get patterns: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def validate_hook_function(self, function_code: str) -> Dict[str, Any]:
        """
        Validate hook function code for issues.
        
        Args:
            function_code: Python function code to validate
            
        Returns:
            Dict with validation results
        """
        try:
            validation = self.learning_system.validate_hook_function(function_code)
            return {
                "success": True,
                "validation": validation
            }
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "validate_hook_function", f"Failed to validate function: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def create_simple_hook(self, name: str, url_pattern: str, action: str, 
                                target_url: Optional[str] = None, 
                                custom_headers: Optional[Dict[str, str]] = None,
                                instance_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Create a simple hook using predefined templates (easier for AI).
        
        Args:
            name: Hook name
            url_pattern: URL pattern to match 
            action: Action type (block, redirect, add_headers, log)
            target_url: Target URL for redirect
            custom_headers: Headers to add
            instance_ids: Browser instances
            
        Returns:
            Dict with creation result
        """
        try:
            if action == "block":
                function_code = '''
def process_request(request):
    return HookAction(action="block")
'''
            elif action == "redirect":
                if not target_url:
                    return {"success": False, "error": "target_url required for redirect action"}
                function_code = f'''
def process_request(request):
    return HookAction(action="redirect", url="{target_url}")
'''
            elif action == "add_headers":
                if not custom_headers:
                    return {"success": False, "error": "custom_headers required for add_headers action"}
                headers_str = str(custom_headers).replace("'", '"')
                function_code = f'''
def process_request(request):
    new_headers = request["headers"].copy()
    new_headers.update({headers_str})
    return HookAction(action="modify", headers=new_headers)
'''
            elif action == "log":
                function_code = '''
def process_request(request):
    import sys
    print(f"[HOOK LOG] {request['method']} {request['url']}", file=sys.stderr)
    return HookAction(action="continue")
'''
            else:
                return {"success": False, "error": f"Unknown action: {action}"}
            
            requirements = {"url_pattern": url_pattern}
            
            return await self.create_dynamic_hook(
                name=name,
                requirements=requirements,
                function_code=function_code,
                instance_ids=instance_ids
            )
            
        except Exception as e:
            debug_logger.log_error("dynamic_hook_ai", "create_simple_hook", f"Failed to create simple hook: {e}")
            return {
                "success": False,
                "error": str(e)
            }


dynamic_hook_ai = DynamicHookAIInterface()