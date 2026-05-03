import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

try:
    from .debug_logger import debug_logger
except ImportError:
    from debug_logger import debug_logger

project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from comprehensive_element_cloner import ComprehensiveElementCloner
from element_cloner import element_cloner

class FileBasedElementCloner:
    """Element cloner that saves data to files and returns file paths."""

    def __init__(self, output_dir: Optional[str] = None):
        """
        Initialize with output directory for clone files.

        Args:
            output_dir (str | None): Directory to save clone files.
                Defaults to <project_root>/element_clones (absolute path,
                avoids cwd-dependent failures when launched from Claude Desktop).
        """
        if output_dir is None:
            self.output_dir = Path(__file__).resolve().parent.parent / "element_clones"
        else:
            self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.comprehensive_cloner = ComprehensiveElementCloner()
    
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
            debug_logger.log_info("file_element_cloner", "extract_styles_to_file",
                                f"Starting style extraction for selector: {selector}")
            
            # Extract styles using element_cloner
            style_data = await element_cloner.extract_element_styles(
                tab,
                selector=selector,
                include_computed=include_computed,
                include_css_rules=include_css_rules,
                include_pseudo=include_pseudo,
                include_inheritance=include_inheritance
            )
            
            # Generate filename and save
            filename = self._generate_filename("styles")
            file_path = self._save_to_file(style_data, filename)
            
            # Create summary
            summary = {
                "file_path": str(file_path),
                "extraction_type": "styles",
                "selector": selector,
                "url": getattr(tab, 'url', 'unknown'),
                "components": {
                    "computed_styles_count": len(style_data.get('computed_styles', {})),
                    "css_rules_count": len(style_data.get('css_rules', [])),
                    "pseudo_elements_count": len(style_data.get('pseudo_elements', {})),
                    "custom_properties_count": len(style_data.get('custom_properties', {}))
                }
            }
            
            debug_logger.log_info("file_element_cloner", "extract_styles_to_file",
                                f"Styles saved to {file_path}")
            return summary
            
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_styles_to_file", e)
            return {"error": str(e)}

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
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return str(file_path.absolute())

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
            complete_data = await self.comprehensive_cloner.extract_complete_element(
                tab, selector, include_children
            )
            complete_data['_metadata'] = {
                'extraction_type': 'complete_comprehensive',
                'selector': selector,
                'timestamp': datetime.now().isoformat(),
                'include_children': include_children
            }
            filename = self._generate_filename("complete_comprehensive")
            file_path = self._save_to_file(complete_data, filename)
            debug_logger.log_info("file_element_cloner", "extract_complete_to_file",
                                  f"Saved complete element data to {file_path}")
            summary = {
                "file_path": file_path,
                "extraction_type": "complete_comprehensive",
                "selector": selector,
                "url": complete_data.get('url', 'unknown'),
                "summary": {
                    "tag_name": complete_data.get('html', {}).get('tagName', 'unknown'),
                    "computed_styles_count": len(complete_data.get('styles', {})),
                    "attributes_count": len(complete_data.get('html', {}).get('attributes', [])),
                    "event_listeners_count": len(complete_data.get('eventListeners', [])),
                    "children_count": len(complete_data.get('children', [])) if include_children else 0,
                    "has_pseudo_elements": bool(complete_data.get('pseudoElements')),
                    "css_rules_count": len(complete_data.get('cssRules', [])),
                    "animations_count": len(complete_data.get('animations', [])),
                    "file_size_kb": round(len(json.dumps(complete_data)) / 1024, 2)
                }
            }
            return summary
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_complete_to_file", e)
            return {"error": str(e)}

    async def extract_element_structure_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        include_children: bool = False,
        include_attributes: bool = True,
        include_data_attributes: bool = True,
        max_depth: int = 3
    ) -> Dict[str, str]:
        """
        Extract structure and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (str): CSS selector for the element.
            include_children (bool): Whether to include children.
            include_attributes (bool): Whether to include attributes.
            include_data_attributes (bool): Whether to include data attributes.
            max_depth (int): Maximum depth for extraction.

        Returns:
            Dict[str, str]: Summary of extraction and file path.
        """
        try:
            structure_data = await element_cloner.extract_element_structure(
                tab, element, selector, include_children,
                include_attributes, include_data_attributes, max_depth
            )
            structure_data['_metadata'] = {
                'extraction_type': 'structure',
                'selector': selector,
                'timestamp': datetime.now().isoformat(),
                'options': {
                    'include_children': include_children,
                    'include_attributes': include_attributes,
                    'include_data_attributes': include_data_attributes,
                    'max_depth': max_depth
                }
            }
            filename = self._generate_filename("structure")
            file_path = self._save_to_file(structure_data, filename)
            debug_logger.log_info("file_element_cloner", "extract_structure_to_file",
                                  f"Saved structure data to {file_path}")
            return {
                "file_path": file_path,
                "extraction_type": "structure",
                "selector": selector,
                "summary": {
                    "tag_name": structure_data.get('tag_name'),
                    "attributes_count": len(structure_data.get('attributes', {})),
                    "data_attributes_count": len(structure_data.get('data_attributes', {})),
                    "children_count": len(structure_data.get('children', [])),
                    "dom_path": structure_data.get('dom_path')
                }
            }
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_structure_to_file", e)
            return {"error": str(e)}

    async def extract_element_events_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        include_inline: bool = True,
        include_listeners: bool = True,
        include_framework: bool = True,
        analyze_handlers: bool = True
    ) -> Dict[str, str]:
        """
        Extract events and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (str): CSS selector for the element.
            include_inline (bool): Include inline event handlers.
            include_listeners (bool): Include event listeners.
            include_framework (bool): Include framework event handlers.
            analyze_handlers (bool): Analyze event handlers.

        Returns:
            Dict[str, str]: Summary of extraction and file path.
        """
        try:
            event_data = await element_cloner.extract_element_events(
                tab, element, selector, include_inline,
                include_listeners, include_framework, analyze_handlers
            )
            event_data['_metadata'] = {
                'extraction_type': 'events',
                'selector': selector,
                'timestamp': datetime.now().isoformat(),
                'options': {
                    'include_inline': include_inline,
                    'include_listeners': include_listeners,
                    'include_framework': include_framework,
                    'analyze_handlers': analyze_handlers
                }
            }
            filename = self._generate_filename("events")
            file_path = self._save_to_file(event_data, filename)
            debug_logger.log_info("file_element_cloner", "extract_events_to_file",
                                  f"Saved events data to {file_path}")
            return {
                "file_path": file_path,
                "extraction_type": "events",
                "selector": selector,
                "summary": {
                    "inline_handlers_count": len(event_data.get('inline_handlers', [])),
                    "event_listeners_count": len(event_data.get('event_listeners', [])),
                    "detected_frameworks": event_data.get('detected_frameworks', []),
                    "framework_handlers": self._safe_process_framework_handlers(event_data.get('framework_handlers', {}))
                }
            }
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_events_to_file", e)
            return {"error": str(e)}

    async def extract_element_animations_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        include_css_animations: bool = True,
        include_transitions: bool = True,
        include_transforms: bool = True,
        analyze_keyframes: bool = True
    ) -> Dict[str, str]:
        """
        Extract animations and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (str): CSS selector for the element.
            include_css_animations (bool): Include CSS animations.
            include_transitions (bool): Include transitions.
            include_transforms (bool): Include transforms.
            analyze_keyframes (bool): Analyze keyframes.

        Returns:
            Dict[str, str]: Summary of extraction and file path.
        """
        try:
            animation_data = await element_cloner.extract_element_animations(
                tab, element, selector, include_css_animations,
                include_transitions, include_transforms, analyze_keyframes
            )
            animation_data['_metadata'] = {
                'extraction_type': 'animations',
                'selector': selector,
                'timestamp': datetime.now().isoformat(),
                'options': {
                    'include_css_animations': include_css_animations,
                    'include_transitions': include_transitions,
                    'include_transforms': include_transforms,
                    'analyze_keyframes': analyze_keyframes
                }
            }
            filename = self._generate_filename("animations")
            file_path = self._save_to_file(animation_data, filename)
            debug_logger.log_info("file_element_cloner", "extract_animations_to_file",
                                  f"Saved animations data to {file_path}")
            return {
                "file_path": file_path,
                "extraction_type": "animations",
                "selector": selector,
                "summary": {
                    "has_animations": animation_data.get('animations', {}).get('animation_name', 'none') != 'none',
                    "has_transitions": animation_data.get('transitions', {}).get('transition_property', 'none') != 'none',
                    "has_transforms": animation_data.get('transforms', {}).get('transform', 'none') != 'none',
                    "keyframes_count": len(animation_data.get('keyframes', []))
                }
            }
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_animations_to_file", e)
            return {"error": str(e)}

    async def extract_element_assets_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        include_images: bool = True,
        include_backgrounds: bool = True,
        include_fonts: bool = True,
        fetch_external: bool = False
    ) -> Dict[str, str]:
        """
        Extract assets and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (str): CSS selector for the element.
            include_images (bool): Include images.
            include_backgrounds (bool): Include background images.
            include_fonts (bool): Include fonts.
            fetch_external (bool): Fetch external assets.

        Returns:
            Dict[str, str]: Summary of extraction and file path.
        """
        try:
            asset_data = await element_cloner.extract_element_assets(
                tab, element, selector, include_images,
                include_backgrounds, include_fonts, fetch_external
            )
            asset_data['_metadata'] = {
                'extraction_type': 'assets',
                'selector': selector,
                'timestamp': datetime.now().isoformat(),
                'options': {
                    'include_images': include_images,
                    'include_backgrounds': include_backgrounds,
                    'include_fonts': include_fonts,
                    'fetch_external': fetch_external
                }
            }
            filename = self._generate_filename("assets")
            file_path = self._save_to_file(asset_data, filename)
            debug_logger.log_info("file_element_cloner", "extract_assets_to_file",
                                  f"Saved assets data to {file_path}")
            return {
                "file_path": file_path,
                "extraction_type": "assets",
                "selector": selector,
                "summary": {
                    "images_count": len(asset_data.get('images', [])),
                    "background_images_count": len(asset_data.get('background_images', [])),
                    "font_family": asset_data.get('fonts', {}).get('family'),
                    "custom_fonts_count": len(asset_data.get('fonts', {}).get('custom_fonts', [])),
                    "icons_count": len(asset_data.get('icons', [])),
                    "videos_count": len(asset_data.get('videos', [])),
                    "audio_count": len(asset_data.get('audio', []))
                }
            }
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_assets_to_file", e)
            return {"error": str(e)}

    async def extract_related_files_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        analyze_css: bool = True,
        analyze_js: bool = True,
        follow_imports: bool = False,
        max_depth: int = 2
    ) -> Dict[str, str]:
        """
        Extract related files and save to file, return file path.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (str): CSS selector for the element.
            analyze_css (bool): Analyze CSS files.
            analyze_js (bool): Analyze JS files.
            follow_imports (bool): Follow imports.
            max_depth (int): Maximum depth for import following.

        Returns:
            Dict[str, str]: Summary of extraction and file path.
        """
        try:
            file_data = await element_cloner.extract_related_files(
                tab, element, selector, analyze_css, analyze_js, follow_imports, max_depth
            )
            file_data['_metadata'] = {
                'extraction_type': 'related_files',
                'selector': selector,
                'timestamp': datetime.now().isoformat(),
                'options': {
                    'analyze_css': analyze_css,
                    'analyze_js': analyze_js,
                    'follow_imports': follow_imports,
                    'max_depth': max_depth
                }
            }
            filename = self._generate_filename("related_files")
            file_path = self._save_to_file(file_data, filename)
            debug_logger.log_info("file_element_cloner", "extract_related_files_to_file",
                                  f"Saved related files data to {file_path}")
            return {
                "file_path": file_path,
                "extraction_type": "related_files",
                "selector": selector,
                "summary": {
                    "stylesheets_count": len(file_data.get('stylesheets', [])),
                    "scripts_count": len(file_data.get('scripts', [])),
                    "imports_count": len(file_data.get('imports', [])),
                    "modules_count": len(file_data.get('modules', []))
                }
            }
        except Exception as e:
            debug_logger.log_error("file_element_cloner", "extract_related_files_to_file", e)
            return {"error": str(e)}

    async def clone_element_complete_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        extraction_options: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Master function that extracts all element data and saves to file.
        Returns file path instead of full data.

        Args:
            tab: Browser tab object.
            element: DOM element object.
            selector (str): CSS selector for the element.
            extraction_options (Dict[str, Any]): Extraction options.

        Returns:
            Dict[str, Any]: Summary of extraction and file path.
        """
        try:
            complete_data = await element_cloner.clone_element_complete(
                tab, element, selector, extraction_options
            )
            if 'error' in complete_data:
                return complete_data
            complete_data['_metadata'] = {
                'extraction_type': 'complete_clone',
                'selector': selector,
                'timestamp': datetime.now().isoformat(),
                'extraction_options': extraction_options
            }
            filename = self._generate_filename("complete_clone")
            file_path = self._save_to_file(complete_data, filename)
            summary = {
                "file_path": file_path,
                "extraction_type": "complete_clone",
                "selector": selector,
                "url": complete_data.get('url'),
                "components": {}
            }
            if 'styles' in complete_data:
                styles = complete_data['styles']
                summary['components']['styles'] = {
                    'computed_styles_count': len(styles.get('computed_styles', {})),
                    'css_rules_count': len(styles.get('css_rules', [])),
                    'pseudo_elements_count': len(styles.get('pseudo_elements', {}))
                }
            if 'structure' in complete_data:
                structure = complete_data['structure']
                summary['components']['structure'] = {
                    'tag_name': structure.get('tag_name'),
                    'attributes_count': len(structure.get('attributes', {})),
                    'children_count': len(structure.get('children', []))
                }
            if 'events' in complete_data:
                events = complete_data['events']
                summary['components']['events'] = {
                    'inline_handlers_count': len(events.get('inline_handlers', [])),
                    'detected_frameworks': events.get('detected_frameworks', [])
                }
            if 'animations' in complete_data:
                animations = complete_data['animations']
                summary['components']['animations'] = {
                    'has_animations': animations.get('animations', {}).get('animation_name', 'none') != 'none',
                    'keyframes_count': len(animations.get('keyframes', []))
                }
            if 'assets' in complete_data:
                assets = complete_data['assets']
                summary['components']['assets'] = {
                    'images_count': len(assets.get('images', [])),
                    'background_images_count': len(assets.get('background_images', []))
                }
            if 'related_files' in complete_data:
                files = complete_data['related_files']
                summary['components']['related_files'] = {
                    'stylesheets_count': len(files.get('stylesheets', [])),
                    'scripts_count': len(files.get('scripts', []))
                }
            debug_logger.log_info("file_element_cloner", "clone_complete_to_file",
                                  f"Saved complete clone data to {file_path}")
            return summary
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
                file_info = {
                    "file_path": str(file_path.absolute()),
                    "filename": file_path.name,
                    "size": file_path.stat().st_size,
                    "created": datetime.fromtimestamp(file_path.stat().st_ctime).isoformat(),
                    "modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
                }
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if '_metadata' in data:
                            file_info['metadata'] = data['_metadata']
                except:
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
        import time
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