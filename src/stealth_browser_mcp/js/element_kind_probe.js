/**
 * Probes a single DOM element and returns a compact fact object that a pure
 * Python classifier can map to a concrete element kind. The probe never decides
 * the kind itself; it only gathers signals so the logic lives in one tested
 * place. The returned object is JSON serialisable.
 *
 * @const element {Element|null} - The element to probe, or null when the
 *     selector resolves to nothing.
 * @const result {object} - Fact object describing the element.
 * @returns {object} - Fact object, or an error object when the element is null.
 */
(function() {
    const el = $ELEMENT$;
    if (!el) return { error: 'Element not found' };

    const result = {
        tag_name: el.tagName ? el.tagName.toLowerCase() : null,
        type: (el.getAttribute('type') || '') + '',
        input_type: (el.type || '') + '',
        contenteditable: (el.getAttribute('contenteditable') || '') + '',
        is_content_editable: !!el.isContentEditable,
        aria_role: (el.getAttribute('role') || '') + '',
        read_only: !!el.readOnly,
        disabled: !!el.disabled,
        required: !!el.required,
        multiple: !!el.multiple,
        is_contenteditable_element: !!el.isContentEditable
    };

    // Class names as an array and a lowercased string for substring hints.
    result.class_list = Array.from(el.classList);
    result.class_names = result.class_list.join(' ');

    // Shadow root presence (open or closed) so custom-element inputs show up.
    result.has_shadow_root = !!el.shadowRoot;

    // The element's closest ancestor <frame> or <iframe>, if any.
    let frame = null;
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
        const name = node.tagName ? node.tagName.toLowerCase() : '';
        if (name === 'iframe' || name === 'frame') {
            frame = node.id || (node.getAttribute('id') || '') + '';
            break;
        }
        node = node.parentNode;
    }
    result.iframe = frame || null;

    // A few framework/editor hints that change how a kind should be handled.
    const hintNames = ['ProseMirror', 'ql-editor', 'react-select', 'Select2',
        'CodeMirror', 'Monaco', 'trix-editor', 'ck-editor'];
    result.hints = hintNames.filter(function(name) {
        return result.class_names.toLowerCase().indexOf(name.toLowerCase()) !== -1;
    });

    // Select element specifics.
    if (result.tag_name === 'select') {
        result.multiple_select = !!el.multiple;
        result.options = el.options ? el.options.length : 0;
        result.selectedIndex = el.selectedIndex;
    }

    return result;
})();
