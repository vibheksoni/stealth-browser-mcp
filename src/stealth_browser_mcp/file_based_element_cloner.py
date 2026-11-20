import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .comprehensive_element_cloner import comprehensive_element_cloner
from .debug_logger import debug_logger
from .element_cloner import element_cloner
from .paths import clone_output_dir
from .path_policy import write_mode_0600


class FileBasedElementCloner:
    """Element cloner that saves data to files and returns file paths."""

    def __init__(self, output_dir: Optional[str] = None):
        """
        Initialize with output directory for clone files.

        Args:
            output_dir (Optional[str]): Directory to save clone files.
                Defaults to element_clones in the data directory, an absolute
                path that avoids startup failures when the host process uses a
                read-only cwd.
        """
        self._output_dir_override = Path(output_dir) if output_dir is not None else None

    @property
    def output_dir(self) -> Path:
        """Directory for this caller's clone files (per MCP session when set)."""
        path = self._output_dir_override if self._output_dir_override is not None else clone_output_dir()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _safe_process_framework_handlers(self, framework_handlers):
        """Safely process framework handlers that might be dict or list."""
        if isinstance(framework_handlers, dict):
            return {k: len(v) if isinstance(v, list) else str(v) for k, v in framework_handlers.items()}
        elif isinstance(framework_handlers, list):
            return {"handlers": len(framework_handlers)}
        else:
            return {"value": str(framework_handlers)}

    def _generate_filename(self, prefix: str, extension: str = "json") -> str:
        """
        Generate unique filename with timestamp.

        Args:
            prefix (str): Prefix for the filename.
            extension (str): File extension.

        Returns:
            str: Generated filename.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        return f"{prefix}_{timestamp}_{unique_id}.{extension}"

    def _save_to_file(self, data: Dict[str, Any], filename: str) -> str:
        """
        Save data to file and return absolute path.

        Args:
            data (Dict[str, Any]): Data to save.
            filename (str): Name of the file.

        Returns:
            str: Absolute path to the saved file.
        """
        file_path = self.output_dir / filename
        write_mode_0600(file_path, json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return str(file_path.absolute())

    def _save_extraction(
        self,
        data: Any,
        extraction_type: str,
        selector: Optional[str],
        options: Dict[str, Any],
        build_summary: Callable[[Dict[str, Any]], Dict[str, Any]],
        summary_key: str = "summary",
    ) -> Dict[str, Any]:
        """
        Save an extraction result to disk and return its summary.

        Extraction errors are returned as-is so they are never written to
        disk or reported as a successful save.

        Args:
            data (Any): Extraction result returned by a cloner.
            extraction_type (str): Type label used for the filename and metadata.
            selector (Optional[str]): CSS selector that was extracted.
            options (Dict[str, Any]): Extraction options recorded in the metadata.
            build_summary (Callable[[Dict[str, Any]], Dict[str, Any]]): Builds the summary from the data.
            summary_key (str): Response key that holds the summary.

        Returns:
            Dict[str, Any]: File path and summary, or the extraction error.
        """
        if not isinstance(data, dict):
            return {"error": f"Unexpected extraction result: {type(data).__name__}", "selector": selector}
        if "error" in data:
            return data
        data['_metadata'] = {
            'extraction_type': extraction_type,
            'selector': selector,
            'timestamp': datetime.now().isoformat(),
            'options': options,
        }
        file_path = self._save_to_file(data, self._generate_filename(extraction_type))
        debug_logger.log_info("file_element_cloner", f"{extraction_type}_to_file", f"Saved {extraction_type} data to {file_path}")
        return {
            "file_path": file_path,
            "extraction_type": extraction_type,
            "selector": selector,
            "url": data.get('url'),
            summary_key: build_summary(data),
        }

    async def extract_element_styles_to_file(
        self,
        tab,
        selector: str,
        include_computed: bool = True,
        include_css_rules: bool = True,
        include_pseudo: bool = True,
        include_inheritance: bool = False
    ) -> Dict[str, Any]:
        """
        Extract element styles and save to file, returning file path.

        Args:
            tab: Browser tab instance
            selector (str): CSS selector for the element
            include_computed (bool): Include computed styles
            include_css_rules (bool): Include matching CSS rules
            include_pseudo (bool): Include pseudo-element styles
            include_inheritance (bool): Include style inheritance chain

        Returns:
            Dict[str, Any]: File path and summary of extracted styles
        """
        try:
            options = {
                'include_computed': include_computed,
                'include_css_rules': include_css_rules,
                'include_pseudo': include_pseudo,
                'include_inheritance': include_inheritance,
            }
            style_data = await element_cloner.extract_element_styles(tab, selector=selector, **options)
            return self._save_extraction(style_data, "styles", selector, options, lambda data: {
                "computed_styles_count": len(data.get('computed_styles', {})),
                "css_rules_count": len(data.get('css_rules', [])),
                "pseudo_elements_count": len(data.get('pseudo_elements', {})),
            }, summary_key="components")
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_styles_to_file", e)
            return {"error": str(e)}

    async def extract_complete_element_to_file(
        self,
        tab,
        selector: str,
        include_children: bool = True
    ) -> Dict[str, Any]:
        """
        Extract complete element using working comprehensive cloner and save to file.

        Args:
            tab: Browser tab object.
            selector (str): CSS selector for the element.
            include_children (bool): Whether to include children.

        Returns:
            Dict[str, Any]: Summary of extraction and file path.
        """
        try:
            complete_data = await comprehensive_element_cloner.extract_complete_element(
                tab, selector, include_children
            )

            def summarize(data: Dict[str, Any]) -> Dict[str, Any]:
                element_data = data.get('element') or {}
                html = element_data.get('html') or {}
                return {
                    "tag_name": html.get('tagName', 'unknown'),
                    "computed_styles_count": len(element_data.get('styles') or {}),
                    "attributes_count": len(html.get('attributes') or []),
                    "event_listeners_count": len(element_data.get('eventListeners') or []),
                    "children_count": len(data.get('children') or []),
                    "has_pseudo_elements": bool(element_data.get('pseudoElements')),
                    "css_rules_count": len(element_data.get('cssRules') or []),
                    "file_size_kb": round(len(json.dumps(data, default=str)) / 1024, 2),
                }

            return self._save_extraction(
                complete_data,
                "complete_comprehensive",
                selector,
                {'include_children': include_children},
                summarize,
            )
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_complete_to_file", e)
            return {"error": str(e)}

    async def extract_element_structure_to_file(
        self,
        tab,
        element=None,
        selector: Optional[str] = None,
        include_children: bool = False,
        include_attributes: bool = True,
        include_data_attributes: bool = True,
        max_depth: int = 3
    ) -> Dict[str, Any]:
        """
        Extract structure and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (Optional[str]): CSS selector for the element.
            include_children (bool): Whether to include children.
            include_attributes (bool): Whether to include attributes.
            include_data_attributes (bool): Whether to include data attributes.
            max_depth (int): Maximum depth for extraction.

        Returns:
            Dict[str, Any]: Summary of extraction and file path.
        """
        try:
            options = {
                'include_children': include_children,
                'include_attributes': include_attributes,
                'include_data_attributes': include_data_attributes,
                'max_depth': max_depth,
            }
            structure_data = await element_cloner.extract_element_structure(tab, element, selector, **options)
            return self._save_extraction(structure_data, "structure", selector, options, lambda data: {
                "tag_name": data.get('tag_name'),
                "attributes_count": len(data.get('attributes', {})),
                "data_attributes_count": len(data.get('data_attributes', {})),
                "children_count": len(data.get('children', [])),
                "dom_path": data.get('dom_path'),
            })
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_structure_to_file", e)
            return {"error": str(e)}

    async def extract_element_events_to_file(
        self,
        tab,
        element=None,
        selector: Optional[str] = None,
        include_inline: bool = True,
        include_listeners: bool = True,
        include_framework: bool = True,
        analyze_handlers: bool = True
    ) -> Dict[str, Any]:
        """
        Extract events and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (Optional[str]): CSS selector for the element.
            include_inline (bool): Include inline event handlers.
            include_listeners (bool): Include event listeners.
            include_framework (bool): Include framework event handlers.
            analyze_handlers (bool): Analyze event handlers.

        Returns:
            Dict[str, Any]: Summary of extraction and file path.
        """
        try:
            options = {
                'include_inline': include_inline,
                'include_listeners': include_listeners,
                'include_framework': include_framework,
                'analyze_handlers': analyze_handlers,
            }
            event_data = await element_cloner.extract_element_events(tab, element, selector, **options)
            return self._save_extraction(event_data, "events", selector, options, lambda data: {
                "inline_handlers_count": len(data.get('inline_handlers', [])),
                "event_listeners_count": len(data.get('event_listeners', [])),
                "detected_frameworks": data.get('detected_frameworks', []),
                "framework_handlers": self._safe_process_framework_handlers(data.get('framework_handlers', {})),
            })
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_events_to_file", e)
            return {"error": str(e)}

    async def extract_element_animations_to_file(
        self,
        tab,
        element=None,
        selector: Optional[str] = None,
        include_css_animations: bool = True,
        include_transitions: bool = True,
        include_transforms: bool = True,
        analyze_keyframes: bool = True
    ) -> Dict[str, Any]:
        """
        Extract animations and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (Optional[str]): CSS selector for the element.
            include_css_animations (bool): Include CSS animations.
            include_transitions (bool): Include transitions.
            include_transforms (bool): Include transforms.
            analyze_keyframes (bool): Analyze keyframes.

        Returns:
            Dict[str, Any]: Summary of extraction and file path.
        """
        try:
            options = {
                'include_css_animations': include_css_animations,
                'include_transitions': include_transitions,
                'include_transforms': include_transforms,
                'analyze_keyframes': analyze_keyframes,
            }
            animation_data = await element_cloner.extract_element_animations(tab, element, selector, **options)
            return self._save_extraction(animation_data, "animations", selector, options, lambda data: {
                "has_animations": (data.get('animations') or {}).get('animation_name', 'none') != 'none',
                "has_transitions": (data.get('transitions') or {}).get('transition_property', 'none') != 'none',
                "has_transforms": (data.get('transforms') or {}).get('transform', 'none') != 'none',
                "keyframes_count": len(data.get('keyframes', [])),
            })
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_animations_to_file", e)
            return {"error": str(e)}

    async def extract_element_assets_to_file(
        self,
        tab,
        element=None,
        selector: Optional[str] = None,
        include_images: bool = True,
        include_backgrounds: bool = True,
        include_fonts: bool = True,
        fetch_external: bool = False
    ) -> Dict[str, Any]:
        """
        Extract assets and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (Optional[str]): CSS selector for the element.
            include_images (bool): Include images.
            include_backgrounds (bool): Include background images.
            include_fonts (bool): Include fonts.
            fetch_external (bool): Fetch external assets.

        Returns:
            Dict[str, Any]: Summary of extraction and file path.
        """
        try:
            options = {
                'include_images': include_images,
                'include_backgrounds': include_backgrounds,
                'include_fonts': include_fonts,
                'fetch_external': fetch_external,
            }
            asset_data = await element_cloner.extract_element_assets(tab, element, selector, **options)
            return self._save_extraction(asset_data, "assets", selector, options, lambda data: {
                "images_count": len(data.get('images', [])),
                "background_images_count": len(data.get('background_images', [])),
                "font_family": (data.get('fonts') or {}).get('family'),
                "custom_fonts_count": len((data.get('fonts') or {}).get('custom_fonts', [])),
                "icons_count": len(data.get('icons', [])),
                "videos_count": len(data.get('videos', [])),
                "audio_count": len(data.get('audio', [])),
            })
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_assets_to_file", e)
            return {"error": str(e)}

    async def extract_related_files_to_file(
        self,
        tab,
        element=None,
        selector: Optional[str] = None,
        analyze_css: bool = True,
        analyze_js: bool = True,
        follow_imports: bool = False,
        max_depth: int = 2
    ) -> Dict[str, Any]:
        """
        Extract related files and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (Optional[str]): CSS selector for the element.
            analyze_css (bool): Analyze CSS files.
            analyze_js (bool): Analyze JS files.
            follow_imports (bool): Follow imports.
            max_depth (int): Maximum depth for import following.

        Returns:
            Dict[str, Any]: Summary of extraction and file path.
        """
        try:
            options = {
                'analyze_css': analyze_css,
                'analyze_js': analyze_js,
                'follow_imports': follow_imports,
                'max_depth': max_depth,
            }
            file_data = await element_cloner.extract_related_files(tab, element, selector, **options)
            return self._save_extraction(file_data, "related_files", selector, options, lambda data: {
                "stylesheets_count": len(data.get('stylesheets', [])),
                "scripts_count": len(data.get('scripts', [])),
                "imports_count": len(data.get('imports', [])),
                "modules_count": len(data.get('modules', [])),
            })
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_related_files_to_file", e)
            return {"error": str(e)}

    @staticmethod
    def _summarize_complete_clone(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Summarize each component of a complete clone.

        Args:
            data (Dict[str, Any]): Complete clone data keyed by component.

        Returns:
            Dict[str, Any]: Per-component summary, with errors surfaced per component.
        """
        builders = {
            'styles': lambda c: {
                'computed_styles_count': len(c.get('computed_styles', {})),
                'css_rules_count': len(c.get('css_rules', [])),
                'pseudo_elements_count': len(c.get('pseudo_elements', {})),
            },
            'structure': lambda c: {
                'tag_name': c.get('tag_name'),
                'attributes_count': len(c.get('attributes', {})),
                'children_count': len(c.get('children', [])),
            },
            'events': lambda c: {
                'inline_handlers_count': len(c.get('inline_handlers', [])),
                'detected_frameworks': c.get('detected_frameworks', []),
            },
            'animations': lambda c: {
                'has_animations': (c.get('animations') or {}).get('animation_name', 'none') != 'none',
                'keyframes_count': len(c.get('keyframes', [])),
            },
            'assets': lambda c: {
                'images_count': len(c.get('images', [])),
                'background_images_count': len(c.get('background_images', [])),
            },
            'related_files': lambda c: {
                'stylesheets_count': len(c.get('stylesheets', [])),
                'scripts_count': len(c.get('scripts', [])),
            },
        }
        summary = {}
        for name, build in builders.items():
            component = data.get(name)
            if not isinstance(component, dict):
                continue
            summary[name] = {'error': component['error']} if 'error' in component else build(component)
        return summary

    async def clone_element_complete_to_file(
        self,
        tab,
        element=None,
        selector: Optional[str] = None,
        extraction_options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Master function that extracts all element data and saves to file.
        Returns file path instead of full data.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (Optional[str]): CSS selector for the element.
            extraction_options (Optional[Dict[str, Any]]): Extraction options.

        Returns:
            Dict[str, Any]: Summary of extraction and file path.
        """
        try:
            complete_data = await element_cloner.clone_element_complete(
                tab, element, selector, extraction_options
            )
            return self._save_extraction(
                complete_data,
                "complete_clone",
                selector,
                extraction_options or {},
                self._summarize_complete_clone,
                summary_key="components",
            )
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "clone_complete_to_file", e)
            return {"error": str(e)}

    def list_clone_files(self) -> List[Dict[str, Any]]:
        """
        List all clone files in the output directory.

        Returns:
            List[Dict[str, Any]]: List of file info dictionaries.
        """
        files = []
        for file_path in self.output_dir.glob("*.json"):
            try:
                stat = file_path.stat()
                file_info = {
                    "file_path": str(file_path.absolute()),
                    "filename": file_path.name,
                    "size": stat.st_size,
                    "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
                }
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, dict) and '_metadata' in data:
                        file_info['metadata'] = data['_metadata']
                except (OSError, ValueError):
                    pass
                files.append(file_info)
            except Exception as e:
                debug_logger.log_warning("file_element_cloner", "list_files", f"Error reading {file_path}: {e}")
        files.sort(key=lambda x: x['created'], reverse=True)
        return files

    def cleanup_old_files(self, max_age_hours: int = 24) -> int:
        """
        Clean up clone files older than specified hours.

        Args:
            max_age_hours (int): Maximum age of files in hours.

        Returns:
            int: Number of deleted files.
        """
        cutoff_time = time.time() - (max_age_hours * 3600)
        deleted_count = 0
        for file_path in self.output_dir.glob("*.json"):
            try:
                if file_path.stat().st_ctime < cutoff_time:
                    file_path.unlink()
                    deleted_count += 1
                    debug_logger.log_info("file_element_cloner", "cleanup", f"Deleted old file: {file_path.name}")
            except Exception as e:
                debug_logger.log_warning("file_element_cloner", "cleanup", f"Error deleting {file_path}: {e}")
        return deleted_count


file_based_element_cloner = FileBasedElementCloner()
