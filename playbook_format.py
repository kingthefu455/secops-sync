#! /usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Splits playbooks into the folder layout 
    <playbook_name>/
        definition.json
        trigger.json
        overviews.json
        steps/<instance_name>_<identifier[:5]>.json
        widgets/<widget_title>_<widget_description>.json
        widgets/<widget_title>_<widget_description>.html

Every widget of the overview templates is moved to its own JSON file with all
its data as the API returns it, and removed from overviews.json. Before a push,
the widgets are put back into their template: the one whose identifier the
widget holds (templateIdentifier), or the only template of the playbook.

HTML widgets also get an HTML file, like content-hub's mp tool does: the data
definition the API holds as JSON text (dataDefinitionJson) is stored parsed,
without its htmlContent, and the HTML is stored in the .html file of the same
name with its real line breaks. The data of other widgets stays as it is.

Overview and widget files have sorted keys, so they don't change when the API
orders the keys differently.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

from config import (
    PLAYBOOK_DEFINITION_FILE,
    PLAYBOOK_OVERVIEWS_FILE,
    PLAYBOOK_STEPS_DIR,
    PLAYBOOK_TRIGGER_FILE,
    PLAYBOOK_WIDGETS_DIR,
)

LOGGER = logging.getLogger("rac")

# API keys stored in their own file instead of the definition file
_STEPS_KEY = "steps"
_OVERVIEWS_KEY = "overviewTemplates"
_SEPARATE_FILES = {
    "trigger": PLAYBOOK_TRIGGER_FILE,
    _OVERVIEWS_KEY: PLAYBOOK_OVERVIEWS_FILE,
}

# Markers written by earlier versions in overviews.json, still read
_FILE_MARKER = "$file"
_JSON_MARKER = "$json"
# Key of the template in an overviewTemplates entry, when the API wraps it
_TEMPLATE_KEY = "overviewTemplate"
_TEMPLATE_ID_KEY = "identifier"
# Key of the widget list in an overview template
_WIDGETS_KEY = "widgets"
# Key holding the position of a widget in its template
_ORDER_KEY = "order"
# content-hub's WidgetType.HTML, as number or name
_HTML_WIDGET_TYPE = 3
_HTML_WIDGET_TYPE_NAME = "html"
# Widget data definition, JSON text in the API
_DATA_JSON_KEY = "dataDefinitionJson"
# Key of widget HTML in the data definition, same as content-hub
_HTML_KEY = "htmlContent"
# Widget file names are cut to stay below file system limits
_MAX_WIDGET_NAME = 120


def to_snake_case(s: str) -> str:
    """Same conversion as content-hub's mp tool, used for playbook folder names."""
    return (
        re.sub(r"(?<=[a-z])(?=[A-Z])|[^a-zA-Z\d]", " ", s)
        .strip()
        .replace(" ", "_")
        .replace("-", "_")
        .lower()
    )


def step_file_name(instance_name: str | None, identifier: str | None) -> str:
    """Same naming as content-hub's mp tool: <instance_name>_<identifier[:5]>."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "", (instance_name or "").replace(" ", "_"))
    return f"{sanitized}_{(identifier or '')[:5]}".lower()


def widget_file_name(title: str | None, description: str | None) -> str:
    """Widget HTML file name: <title>_<description>, without unsafe characters."""
    parts = [
        re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (part or "").strip()).replace(" ", "_")
        for part in (title, description)
    ]
    name = "_".join(part for part in parts if part)[:_MAX_WIDGET_NAME].strip("._")
    return name.lower() or "widget"


def to_files(raw_data: dict) -> dict[str, Any]:
    """Splits API playbook JSON into its files.

    Returns:
        A mapping of file path (relative to the playbook folder) to file data:
        JSON data, or text for widget HTML files.
    """
    definition = copy.deepcopy(raw_data)
    steps = definition.pop(_STEPS_KEY, [])
    files: dict[str, Any] = {PLAYBOOK_DEFINITION_FILE: definition}
    for key, file_name in _SEPARATE_FILES.items():
        if key in definition:
            files[file_name] = definition.pop(key)

    for step in steps:
        base = step_file_name(step.get("instanceName"), step.get("identifier"))
        files[_unique_path(files, f"{PLAYBOOK_STEPS_DIR}/{base}", ".json")] = step

    if PLAYBOOK_OVERVIEWS_FILE in files:
        files[PLAYBOOK_OVERVIEWS_FILE] = _split_overviews(
            _sort_keys(files[PLAYBOOK_OVERVIEWS_FILE]), files
        )
    return files


def _unique_path(files: dict[str, Any], base: str, extension: str) -> str:
    path, suffix = f"{base}{extension}", 1
    while path in files:
        suffix += 1
        path = f"{base}_{suffix}{extension}"
    return path


def _overview_templates(overviews: Any) -> list[dict]:
    """Returns the templates of overviewTemplates, wrapped in overviewTemplate or not."""
    templates = []
    for entry in overviews if isinstance(overviews, list) else []:
        template = entry.get(_TEMPLATE_KEY, entry) if isinstance(entry, dict) else None
        if isinstance(template, dict):
            templates.append(template)
    return templates


def _belongs_to(widget: dict, template: dict, templates: list[dict]) -> bool:
    """True when the widget holds the identifier of the template, or when the
    template is the only one."""
    template_id = template.get(_TEMPLATE_ID_KEY)
    if isinstance(template_id, str) and template_id in widget.values():
        return True
    return len(templates) == 1


def _split_overviews(overviews: Any, files: dict[str, Any]) -> Any:
    """Moves the widgets of the overview templates to their own files.

    A widget that couldn't be put back into its template stays in overviews.json.

    Args:
        overviews: Overview templates data, changed in place.
        files: Playbook files, widget JSON and HTML files are added to it.

    Returns:
        The overview templates data without the moved widgets.
    """
    templates = _overview_templates(overviews)
    for template in templates:
        widgets = template.get(_WIDGETS_KEY)
        if not isinstance(widgets, list):
            continue
        moved = [w for w in widgets if isinstance(w, dict) and _belongs_to(w, template, templates)]
        # Widgets with the same title and description get numbered file
        # names. Naming them in an order from their content keeps the
        # numbers stable when the API returns the widgets in another order.
        for widget in sorted(moved, key=lambda w: json.dumps(w, sort_keys=True)):
            _split_widget(widget, files)
        kept = [w for w in widgets if all(w is not m for m in moved)]
        if kept:
            template[_WIDGETS_KEY] = kept
        else:
            del template[_WIDGETS_KEY]
    return overviews


def _split_widget(widget: dict, files: dict[str, Any]) -> None:
    """Moves a widget to widgets/<title>_<description>.json and, for an HTML
    widget, its HTML to the .html file of the same name."""
    base = widget_file_name(
        _text(widget.get("title")) or _text(widget.get("name")),
        _text(widget.get("description")),
    )
    path = _unique_path(files, f"{PLAYBOOK_WIDGETS_DIR}/{base}", ".json")
    widget = dict(widget)
    data_text = widget.get(_DATA_JSON_KEY)
    data = _parse_json_object(data_text) if isinstance(data_text, str) else None
    if (
        _is_html_widget(widget)
        and data is not None
        and isinstance(data.get(_HTML_KEY), str)
        and data[_HTML_KEY]
    ):
        files[_html_path(path)] = data.pop(_HTML_KEY)
        widget[_DATA_JSON_KEY] = data
    files[path] = _sort_keys(widget)


def _join_widgets(overviews: Any, files: dict[str, Any], used: set[str]) -> Any:
    """Reverses _split_overviews: puts the widget files back into their templates.

    Args:
        overviews: Overview templates data, changed in place.
        files: Playbook files.
        used: Widget files already referenced from overviews.json by earlier
            versions, which are not added again.
    """
    widgets = [
        (path, _join_widget(path, files))
        for path in sorted(files)
        if path.startswith(f"{PLAYBOOK_WIDGETS_DIR}/")
        and path.endswith(".json")
        and path not in used
    ]
    templates = _overview_templates(overviews)
    own: dict[int, list] = {id(template): [] for template in templates}
    for path, widget in widgets:
        template = next((t for t in templates if _belongs_to(widget, t, templates)), None)
        if template is None:
            LOGGER.warning(f"Widget {path} doesn't belong to any overview template, it is skipped")
            continue
        own[id(template)].append(widget)
    for template in templates:
        widgets_of_template = template.get(_WIDGETS_KEY, []) + own[id(template)]
        template[_WIDGETS_KEY] = sorted(widgets_of_template, key=_widget_order)
    return overviews


def _widget_order(widget: Any) -> tuple[int, float]:
    try:
        return 0, float(widget.get(_ORDER_KEY))
    except (AttributeError, TypeError, ValueError):
        return 1, 0.0


def _join_widget(path: str, files: dict[str, Any]) -> Any:
    """Reverses _split_widget: puts the HTML back into the data definition text."""
    widget = copy.deepcopy(files[path])
    if isinstance(widget, dict) and isinstance(widget.get(_DATA_JSON_KEY), dict):
        data = widget[_DATA_JSON_KEY]
        if _html_path(path) in files:
            data[_HTML_KEY] = files[_html_path(path)]
        widget[_DATA_JSON_KEY] = json.dumps(data, ensure_ascii=False)
    return widget


def _is_html_widget(widget: dict) -> bool:
    widget_type = widget.get("type")
    if isinstance(widget_type, str):
        return widget_type.casefold() == _HTML_WIDGET_TYPE_NAME
    return widget_type == _HTML_WIDGET_TYPE and not isinstance(widget_type, bool)


def _html_path(widget_path: str) -> str:
    return f"{widget_path.removesuffix('.json')}.html"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _parse_json_object(text: str) -> dict | None:
    if not text.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _sort_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sort_keys(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sort_keys(x) for x in value]
    return value


def _join_markers(value: Any, files: dict[str, Any], used: set[str]) -> Any:
    """Resolves the markers earlier versions wrote in overviews.json.

    Args:
        value: Overview templates data.
        files: Playbook files.
        used: Referenced widget JSON files are added to it.
    """
    if isinstance(value, list):
        return [_join_markers(x, files, used) for x in value]
    if not isinstance(value, dict):
        return value
    if value.keys() == {_JSON_MARKER}:
        return json.dumps(_join_markers(value[_JSON_MARKER], files, used), ensure_ascii=False)
    if value.keys() == {_FILE_MARKER}:
        path = value[_FILE_MARKER]
        if path.endswith(".json"):
            used.add(path)
            return _join_widget(path, files)
        return files[path]
    return {k: _join_markers(v, files, used) for k, v in value.items()}


def from_files(files: dict[str, Any]) -> dict:
    """Reassembles API playbook JSON from the data of its files.

    Args:
        files: A mapping of file path (relative to the playbook folder) to file
            data (parsed JSON, or text for widget HTML files), as returned by
            to_files.
    """
    raw_data = copy.deepcopy(files[PLAYBOOK_DEFINITION_FILE])
    for key, file_name in _SEPARATE_FILES.items():
        if file_name in files:
            raw_data[key] = copy.deepcopy(files[file_name])
    if _OVERVIEWS_KEY in raw_data:
        used: set[str] = set()
        overviews = _join_markers(raw_data[_OVERVIEWS_KEY], files, used)
        raw_data[_OVERVIEWS_KEY] = _join_widgets(overviews, files, used)
    # Steps are linked through identifiers, so their order doesn't matter.
    # Sorting by file name keeps the result stable.
    raw_data[_STEPS_KEY] = [
        copy.deepcopy(files[path])
        for path in sorted(files)
        if path.startswith(f"{PLAYBOOK_STEPS_DIR}/")
    ]
    return raw_data
