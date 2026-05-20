"""
CDP Function Executor - Direct JavaScript function execution via Chrome DevTools Protocol

This module provides comprehensive function execution capabilities using nodriver's CDP access:
1. Direct CDP command execution
2. JavaScript function discovery and execution  
3. Dynamic script injection and execution
4. Python-JavaScript bridge functionality
"""

import asyncio
import json
import uuid
import inspect
from typing import Dict, List, Any, Optional, Callable, Union
from datetime import datetime

import nodriver as uc
from nodriver import Tab

from debug_logger import debug_logger


class ExecutionContext:
    """Represents a JavaScript execution context."""

    def __init__(self, id: str, name: str, origin: str, unique_id: str, aux_data: dict = None):
        """
        Args:
            id (str): Execution context identifier.
            name (str): Name of the context.
            origin (str): Origin URL of the context.
            unique_id (str): Unique identifier for the context.
            aux_data (dict, optional): Auxiliary data for the context.
        """
        self.id = id
        self.name = name
        self.origin = origin
        self.unique_id = unique_id
        self.aux_data = aux_data or {}


class FunctionInfo:
    """Information about a discovered JavaScript function."""

    def __init__(self, name: str, path: str, signature: str = None, description: str = None):
        """
        Args:
            name (str): Function name.
            path (str): Path to the function (e.g., "window.document.getElementById").
            signature (str, optional): Function signature.
            description (str, optional): Description of the function.
        """
        self.name = name
        self.path = path
        self.signature = signature
        self.description = description


class FunctionCall:
    """Represents a function call to be executed."""

    def __init__(self, function_path: str, args: List[Any] = None, context_id: str = None):
        """
        Args:
            function_path (str): Path to the function.
            args (List[Any], optional): Arguments to pass to the function.
            context_id (str, optional): Execution context identifier.
        """
        self.function_path = function_path
        self.args = args or []
        self.context_id = context_id


class CDPFunctionExecutor:
    """Main class for CDP-based function execution."""

    def __init__(self):
        """
        Initializes the CDPFunctionExecutor instance.
        """
        self._python_bindings: Dict[str, Callable] = {}
        self._persistent_functions: Dict[str, Dict[str, str]] = {}

    async def enable_runtime(self, tab: Tab) -> bool:
        """
        Enables CDP Runtime domain for a tab.

        Args:
            tab (Tab): The browser tab.

        Returns:
            bool: True if enabled, False otherwise.
        """
        try:
            await tab.send(uc.cdp.runtime.enable())
            debug_logger.log_info("cdp_function_executor", "enable_runtime", f"Runtime enabled for tab")
            return True
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "enable_runtime", e)
            return False

    async def list_cdp_commands(self) -> List[str]:
        """
        Lists CDP commands the generic executor will accept.

        Includes core Runtime methods plus a curated set of Page-domain
        methods (notably addScriptToEvaluateOnNewDocument) that are useful
        for pre-page-script API spoofing (WebGL renderer, navigator props).

        Returns:
            List[str]: List of command names.
        """
        commands = [
            # Runtime domain
            "evaluate", "callFunctionOn", "addBinding", "removeBinding",
            "compileScript", "runScript", "awaitPromise", "getProperties",
            "getExceptionDetails", "globalLexicalScopeNames", "queryObjects",
            "releaseObject", "releaseObjectGroup", "terminateExecution",
            "setAsyncCallStackDepth", "setCustomObjectFormatterEnabled",
            "setMaxCallStackSizeToCapture", "runIfWaitingForDebugger",
            "discardConsoleEntries", "getHeapUsage", "getIsolateId",
            # Page domain (resolved from uc.cdp.page when not found on Runtime)
            "addScriptToEvaluateOnNewDocument",
            "removeScriptToEvaluateOnNewDocument",
            "reload", "navigate", "stopLoading", "getFrameTree",
            "setBypassCSP", "setDocumentContent",
        ]
        return commands

    def _resolve_cdp_method(self, command: str):
        """
        Resolve a CDP command name across Runtime and Page domains.

        nodriver exposes each CDP domain as a submodule under uc.cdp (e.g.
        uc.cdp.runtime, uc.cdp.page). Method names are snake_cased on the
        Python side but the wire uses camelCase. We accept either.
        """
        snake = "".join(["_" + c.lower() if c.isupper() else c for c in command]).lstrip("_")
        for domain in (uc.cdp.runtime, uc.cdp.page):
            for name in (command, snake):
                method = getattr(domain, name, None)
                if callable(method):
                    return method
        return None

    async def execute_cdp_command(self, tab: Tab, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a CDP command (Runtime or Page domain) with given parameters.

        Args:
            tab (Tab): The browser tab.
            command (str): CDP command name (camelCase or snake_case).
            params (Dict[str, Any]): Parameters for the command (snake_case keys).

        Returns:
            Dict[str, Any]: Result of the command execution.
        """
        try:
            await self.enable_runtime(tab)
            cdp_method = self._resolve_cdp_method(command)
            if not cdp_method:
                raise ValueError(f"Unknown CDP command: {command}")
            result = await tab.send(cdp_method(**params))
            debug_logger.log_info("cdp_function_executor", "execute_cdp_command", f"Executed {command} with params: {params}")
            return {
                "success": True,
                "result": result,
                "command": command,
                "params": params
            }
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "execute_cdp_command", e)
            return {
                "success": False,
                "error": str(e),
                "command": command,
                "params": params
            }

    async def get_execution_contexts(self, tab: Tab) -> List[ExecutionContext]:
        """
        Gets all available execution contexts.

        Args:
            tab (Tab): The browser tab.

        Returns:
            List[ExecutionContext]: List of execution contexts.
        """
        try:
            await self.enable_runtime(tab)
            script = """
            (function() {
                return {
                    location: window.location.href,
                    title: document.title,
                    readyState: document.readyState,
                    contexts: [{
                        name: 'main',
                        origin: window.location.origin,
                        url: window.location.href
                    }]
                };
            })()
            """
            result = await tab.send(uc.cdp.runtime.evaluate(
                expression=script,
                return_by_value=True,
                await_promise=True
            ))
            if result and result[0] and result[0].value:
                context_data = result[0].value
                contexts = []
                for i, ctx in enumerate(context_data.get('contexts', [])):
                    contexts.append(ExecutionContext(
                        id=str(i),
                        name=ctx['name'],
                        origin=ctx['origin'],
                        unique_id=f"{ctx['origin']}_{i}"
                    ))
                return contexts
            return []
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "get_execution_contexts", e)
            return []

    async def discover_global_functions(self, tab: Tab, context_id: str = None) -> List[FunctionInfo]:
        """
        Discovers all global JavaScript functions.

        Args:
            tab (Tab): The browser tab.
            context_id (str, optional): Execution context identifier.

        Returns:
            List[FunctionInfo]: List of discovered functions.
        """
        try:
            await self.enable_runtime(tab)
            discovery_script = """
            (function() {
                const functions = [];
                function isFunction(obj) {
                    return typeof obj === 'function';
                }
                function discoverFunctions(obj, path = '', depth = 0) {
                    if (depth > 3) return;
                    try {
                        for (const key of Object.getOwnPropertyNames(obj)) {
                            if (key.startsWith('_') || key === 'constructor') continue;
                            try {
                                const value = obj[key];
                                const fullPath = path ? `${path}.${key}` : key;
                                if (isFunction(value)) {
                                    functions.push({
                                        name: key,
                                        path: fullPath,
                                        signature: value.toString().split('{')[0].trim(),
                                        description: `Function at ${fullPath}`
                                    });
                                } else if (typeof value === 'object' && value !== null && depth < 2) {
                                    discoverFunctions(value, fullPath, depth + 1);
                                }
                            } catch (e) {
                            }
                        }
                    } catch (e) {
                    }
                }
                discoverFunctions(window, 'window');
                discoverFunctions(document, 'document');
                discoverFunctions(console, 'console');
                const globalFuncs = ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 
                                   'fetch', 'alert', 'confirm', 'prompt', 'parseInt', 'parseFloat'];
                for (const funcName of globalFuncs) {
                    if (typeof window[funcName] === 'function') {
                        functions.push({
                            name: funcName,
                            path: funcName,
                            signature: window[funcName].toString().split('{')[0].trim(),
                            description: `Global function ${funcName}`
                        });
                    }
                }
                return functions;
            })()
            """
            result = await tab.send(uc.cdp.runtime.evaluate(
                expression=discovery_script,
                return_by_value=True,
                await_promise=True
            ))
            if result and result[0] and result[0].value:
                functions_data = result[0].value
                functions = []
                for func_data in functions_data:
                    functions.append(FunctionInfo(
                        name=func_data['name'],
                        path=func_data['path'],
                        signature=func_data.get('signature'),
                        description=func_data.get('description')
                    ))
                return functions
            return []
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "discover_global_functions", e)
            return []

    async def discover_object_methods(self, tab: Tab, object_path: str) -> List[FunctionInfo]:
        """
        Discovers methods of a specific JavaScript object.

        Args:
            tab (Tab): The browser tab.
            object_path (str): Path to the JavaScript object.

        Returns:
            List[FunctionInfo]: List of discovered methods.
        """
        try:
            await self.enable_runtime(tab)
            
            object_result = await tab.send(uc.cdp.runtime.evaluate(
                expression=object_path,
                return_by_value=False
            ))
            
            if not object_result or not object_result[0] or not object_result[0].object_id:
                debug_logger.log_warning("cdp_function_executor", "discover_object_methods", f"Could not get object reference for {object_path}")
                return []
                
            object_id = object_result[0].object_id
            
            properties_result = await tab.send(uc.cdp.runtime.get_properties(
                object_id=object_id,
                own_properties=False,
                accessor_properties_only=False
            ))
            
            if not properties_result or not properties_result[0]:
                debug_logger.log_warning("cdp_function_executor", "discover_object_methods", f"No properties returned for {object_path}")
                return []
                
            properties = properties_result[0]
            methods = []
            
            for prop in properties:
                try:
                    if prop.value and prop.value.type_ == "function":
                        methods.append(FunctionInfo(
                            name=prop.name,
                            path=f'{object_path}.{prop.name}',
                            signature=prop.value.description or f"function {prop.name}()",
                            description=f"Method {prop.name} of {object_path}"
                        ))
                except Exception as e:
                    debug_logger.log_warning("cdp_function_executor", "discover_object_methods", f"Error processing property {prop.name}: {e}")
                    continue
                    
            debug_logger.log_info("cdp_function_executor", "discover_object_methods", f"Found {len(methods)} methods for {object_path}")
            return methods
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "discover_object_methods", e)
            return []

    async def call_discovered_function(self, tab: Tab, function_path: str, args: List[Any]) -> Dict[str, Any]:
        """
        Calls a discovered JavaScript function with arguments.

        Args:
            tab (Tab): The browser tab.
            function_path (str): Path to the function.
            args (List[Any]): Arguments to pass.

        Returns:
            Dict[str, Any]: Result of the function call.
        """
        try:
            await self.enable_runtime(tab)
            js_args = json.dumps(args) if args else '[]'
            call_script = f"""
            (function() {{
                try {{
                    const pathParts = '{function_path}'.split('.');
                    let context = window;
                    let func = window;
                    
                    for (let i = 0; i < pathParts.length; i++) {{
                        if (i === pathParts.length - 1) {{
                            func = context[pathParts[i]];
                        }} else {{
                            context = context[pathParts[i]];
                            func = context;
                        }}
                    }}
                    
                    if (typeof func !== 'function') {{
                        throw new Error('Not a function: {function_path}');
                    }}
                    
                    const args = {js_args};
                    const result = func.apply(context, args);
                    return {{
                        success: true,
                        result: result,
                        function_path: '{function_path}',
                        args: args
                    }};
                }} catch (error) {{
                    return {{
                        success: false,
                        error: error.message,
                        function_path: '{function_path}',
                        args: {js_args}
                    }};
                }}
            }})()
            """
            result = await tab.send(uc.cdp.runtime.evaluate(
                expression=call_script,
                return_by_value=True,
                await_promise=True
            ))
            if result and result[0] and result[0].value:
                return result[0].value
            elif result and result[1]:
                return {
                    "success": False,
                    "error": f"Runtime exception: {result[1].text}",
                    "function_path": function_path,
                    "args": args
                }
            return {
                "success": False,
                "error": "No result returned",
                "function_path": function_path,
                "args": args
            }
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "call_discovered_function", e)
            return {
                "success": False,
                "error": str(e),
                "function_path": function_path,
                "args": args
            }

    async def inspect_function_signature(self, tab: Tab, function_path: str) -> Dict[str, Any]:
        """
        Inspects a function's signature and details.

        Args:
            tab (Tab): The browser tab.
            function_path (str): Path to the function.

        Returns:
            Dict[str, Any]: Signature and details of the function.
        """
        try:
            await self.enable_runtime(tab)
            inspect_script = f"""
            (function() {{
                try {{
                    const func = {function_path};
                    if (typeof func !== 'function') {{
                        return {{
                            success: false,
                            error: 'Not a function: {function_path}'
                        }};
                    }}
                    return {{
                        success: true,
                        name: func.name || 'anonymous',
                        path: '{function_path}',
                        signature: func.toString(),
                        length: func.length,
                        is_async: func.constructor.name === 'AsyncFunction',
                        is_generator: func.constructor.name === 'GeneratorFunction'
                    }};
                }} catch (error) {{
                    return {{
                        success: false,
                        error: error.message
                    }};
                }}
            }})()
            """
            result = await tab.send(uc.cdp.runtime.evaluate(
                expression=inspect_script,
                return_by_value=True,
                await_promise=True
            ))
            if result and result[0] and result[0].value:
                return result[0].value
            return {"success": False, "error": "No result returned"}
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "inspect_function_signature", e)
            return {"success": False, "error": str(e)}

    async def inject_and_execute_script(self, tab: Tab, script_code: str, context_id: str = None) -> Dict[str, Any]:
        """
        Injects and executes custom JavaScript code.

        Args:
            tab (Tab): The browser tab.
            script_code (str): JavaScript code to execute.
            context_id (str, optional): Execution context identifier.

        Returns:
            Dict[str, Any]: Result of script execution.
        """
        try:
            await self.enable_runtime(tab)
            wrapped_script = f"""
            (function() {{
                try {{
                    const result = (function() {{
                        {script_code}
                    }})();
                    return {{
                        success: true,
                        result: result,
                        executed_at: new Date().toISOString()
                    }};
                }} catch (error) {{
                    return {{
                        success: false,
                        error: error.message,
                        stack: error.stack,
                        executed_at: new Date().toISOString()
                    }};
                }}
            }})()
            """
            result = await tab.send(uc.cdp.runtime.evaluate(
                expression=wrapped_script,
                return_by_value=True,
                await_promise=True,
                allow_unsafe_eval_blocked_by_csp=True
            ))
            if result and result[0] and result[0].value:
                return result[0].value
            elif result and result[1]:
                return {
                    "success": False,
                    "error": f"Runtime exception: {result[1].text}",
                    "line_number": result[1].line_number,
                    "column_number": result[1].column_number
                }
            return {"success": False, "error": "No result returned"}
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "inject_and_execute_script", e)
            return {"success": False, "error": str(e)}

    async def create_persistent_function(self, tab: Tab, function_name: str, function_code: str, instance_id: str) -> Dict[str, Any]:
        """
        Creates a persistent JavaScript function that survives page reloads.

        Args:
            tab (Tab): The browser tab.
            function_name (str): Name of the function.
            function_code (str): JavaScript code for the function.
            instance_id (str): Instance identifier.

        Returns:
            Dict[str, Any]: Result of function creation.
        """
        try:
            await self.enable_runtime(tab)
            if instance_id not in self._persistent_functions:
                self._persistent_functions[instance_id] = {}
            self._persistent_functions[instance_id][function_name] = function_code
            create_script = f"""
            (function() {{
                try {{
                    window.{function_name} = {function_code};
                    return {{
                        success: true,
                        function_name: '{function_name}',
                        created_at: new Date().toISOString(),
                        available_as: 'window.{function_name}'
                    }};
                }} catch (error) {{
                    return {{
                        success: false,
                        error: error.message,
                        function_name: '{function_name}'
                    }};
                }}
            }})()
            """
            result = await tab.send(uc.cdp.runtime.evaluate(
                expression=create_script,
                return_by_value=True,
                await_promise=True
            ))
            if result and result[0] and result[0].value:
                return result[0].value
            return {"success": False, "error": "Failed to create function"}
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "create_persistent_function", e)
            return {"success": False, "error": str(e)}

    async def execute_function_sequence(self, tab: Tab, function_calls: List[FunctionCall]) -> List[Dict[str, Any]]:
        """
        Executes a sequence of function calls.

        Args:
            tab (Tab): The browser tab.
            function_calls (List[FunctionCall]): List of function calls to execute.

        Returns:
            List[Dict[str, Any]]: Results of each function call.
        """
        results = []
        for i, func_call in enumerate(function_calls):
            try:
                debug_logger.log_info("cdp_function_executor", "execute_function_sequence", f"Executing call {i+1}/{len(function_calls)}: {func_call.function_path}")
                result = await self.call_discovered_function(
                    tab,
                    func_call.function_path,
                    func_call.args
                )
                results.append({
                    "sequence_index": i,
                    "function_call": {
                        "function_path": func_call.function_path,
                        "args": func_call.args,
                        "context_id": func_call.context_id
                    },
                    "result": result
                })
            except Exception as e:
                debug_logger.log_error("cdp_function_executor", "execute_function_sequence", e)
                results.append({
                    "sequence_index": i,
                    "function_call": {
                        "function_path": func_call.function_path,
                        "args": func_call.args,
                        "context_id": func_call.context_id
                    },
                    "result": {
                        "success": False,
                        "error": str(e)
                    }
                })
        return results

    async def create_python_binding(self, tab: Tab, binding_name: str, python_function: Callable) -> Dict[str, Any]:
        """
        Creates a binding that allows JavaScript to call Python functions.

        Args:
            tab (Tab): The browser tab.
            binding_name (str): Name of the binding.
            python_function (Callable): Python function to bind.

        Returns:
            Dict[str, Any]: Result of binding creation.
        """
        try:
            await self.enable_runtime(tab)
            self._python_bindings[binding_name] = python_function
            await tab.send(uc.cdp.runtime.add_binding(name=binding_name))
            wrapper_script = f"""
            (function() {{
                if (!window.{binding_name}) {{
                    window.{binding_name} = function(...args) {{
                        return new Promise((resolve, reject) => {{
                            const callId = Math.random().toString(36).substr(2, 9);
                            window.addEventListener(`{binding_name}_response_${{callId}}`, function(event) {{
                                if (event.detail.success) {{
                                    resolve(event.detail.result);
                                }} else {{
                                    reject(new Error(event.detail.error));
                                }}
                            }}, {{ once: true }});
                            window.chrome.runtime.sendMessage({{
                                binding: '{binding_name}',
                                args: args,
                                callId: callId
                            }});
                        }});
                    }};
                }}
                return {{
                    success: true,
                    binding_name: '{binding_name}',
                    available_as: 'window.{binding_name}'
                }};
            }})()
            """
            result = await tab.send(uc.cdp.runtime.evaluate(
                expression=wrapper_script,
                return_by_value=True,
                await_promise=True
            ))
            if result and result[0] and result[0].value:
                return result[0].value
            return {"success": False, "error": "Failed to create binding"}
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "create_python_binding", e)
            return {"success": False, "error": str(e)}

    async def execute_python_in_browser(self, tab: Tab, python_code: str) -> Dict[str, Any]:
        """
        Executes Python code by translating it to JavaScript with timeout protection.

        Args:
            tab (Tab): The browser tab.
            python_code (str): Python code to execute.

        Returns:
            Dict[str, Any]: Result of execution.
        """
        try:
            js_code = self._translate_python_to_js(python_code)
            debug_logger.log_info("cdp_function_executor", "execute_python_in_browser", f"Translated JS: {js_code}")
            
            import asyncio
            result = await asyncio.wait_for(
                self.inject_and_execute_script(tab, js_code),
                timeout=10.0
            )
            return result
        except asyncio.TimeoutError:
            return {"success": False, "error": "Python execution timeout - code may have infinite loop or syntax error"}
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "execute_python_in_browser", e)
            return {"success": False, "error": str(e)}

    def _translate_python_to_js(self, python_code: str) -> str:
        """
        Professional Python to JavaScript translation using py2js library.

        Args:
            python_code (str): Python code to translate.

        Returns:
            str: Translated JavaScript code.
        """
        try:
            import py2js
            
            js_code = py2js.convert(python_code)
            debug_logger.log_info("cdp_function_executor", "_translate_python_to_js", f"py2js generated: {js_code}")
            
            lines = python_code.strip().split('\n')
            last_line = lines[-1].strip() if lines else ""
            
            if (last_line and 
                '=' not in last_line and 
                not last_line.startswith(('def ', 'class ', 'if ', 'for ', 'while ', 'try:', 'with ', 'import ', 'from '))):
                
                wrapped_code = f"(() => {{ {js_code}; return {last_line}; }})()"
                return wrapped_code
            else:
                return f"(() => {{ {js_code}; }})()"
                
        except ImportError:
            debug_logger.log_warning("cdp_function_executor", "_translate_python_to_js", "py2js not available, using fallback")
            return self._fallback_python_to_js(python_code)
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "_translate_python_to_js", e, {"python_code": python_code})
            return self._fallback_python_to_js(python_code)
    
    def _fallback_python_to_js(self, python_code: str) -> str:
        """
        Fallback Python to JavaScript translation for basic cases.

        Args:
            python_code (str): Python code to translate.

        Returns:
            str: Basic translated JavaScript code.
        """
        import re
        
        lines = python_code.strip().split('\n')
        js_lines = []
        
        for line in lines:
            js_line = line
            
            replacements = {
                "True": "true",
                "False": "false", 
                "None": "null",
                "print(": "console.log(",
                ".append(": ".push(",
            }
            
            for py_syntax, js_syntax in replacements.items():
                js_line = js_line.replace(py_syntax, js_syntax)
            
            if '=' in js_line and not js_line.strip().startswith('//'):
                if re.match(r'^\s*[a-zA-Z_][a-zA-Z0-9_]*\s*=', js_line):
                    js_line = re.sub(r'^(\s*)([a-zA-Z_][a-zA-Z0-9_]*\s*=)', r'\1let \2', js_line)
            
            js_lines.append(js_line)
        
        js_code = ";\n".join(js_lines) + ";"
        
        last_line = lines[-1].strip() if lines else ""
        if last_line and '=' not in last_line and not last_line.endswith(':'):
            js_code = js_code.rsplit(';', 2)[0] + f"; return {last_line};"
        
        wrapped_code = f"(function() {{ {js_code} }})()"
        
        return wrapped_code

    async def call_python_from_js(self, binding_name: str, args: List[Any]) -> Dict[str, Any]:
        """
        Handles JavaScript calls to Python functions.

        Args:
            binding_name (str): Name of the Python binding.
            args (List[Any]): Arguments to pass to the Python function.

        Returns:
            Dict[str, Any]: Result of the Python function call.
        """
        try:
            if binding_name not in self._python_bindings:
                return {"success": False, "error": f"Unknown binding: {binding_name}"}
            python_function = self._python_bindings[binding_name]
            if asyncio.iscoroutinefunction(python_function):
                result = await python_function(*args)
            else:
                result = python_function(*args)
            return {
                "success": True,
                "result": result,
                "binding_name": binding_name,
                "args": args
            }
        except Exception as e:
            debug_logger.log_error("cdp_function_executor", "call_python_from_js", e)
            return {
                "success": False,
                "error": str(e),
                "binding_name": binding_name,
                "args": args
            }

    async def get_function_executor_info(self, instance_id: str = None) -> Dict[str, Any]:
        """
        Gets information about the function executor state.

        Args:
            instance_id (str, optional): Instance identifier.

        Returns:
            Dict[str, Any]: Information about the executor.
        """
        return {
            "python_bindings": list(self._python_bindings.keys()),
            "persistent_functions": self._persistent_functions.get(instance_id, {}) if instance_id else self._persistent_functions,
            "available_commands": await self.list_cdp_commands(),
            "executor_version": "1.0.0",
            "capabilities": [
                "direct_cdp_execution",
                "function_discovery",
                "dynamic_script_injection",
                "python_js_bridge"
            ]
        }