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

Every widget of the overview templates is moved to its own JSON file.
overviews.json then only holds the templates with references to the widget
files, as {"$file": ...}.

Like content-hub's mp tool, the data definition the API holds as JSON text
(dataDefinitionJson) is stored parsed as dataDefinition. For HTML widgets it
uses mp's widget data definition (html_height, safe_rendering, type,
widget_definition_scope, with types and scopes by name), and the HTML is
stored in the .html file of the same name, with its real line breaks. Other
widgets keep their data definition as parsed.

Overview and widget files have sorted keys, so they only change when the
content does, not when the API orders or formats the data differently.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from config import (
    PLAYBOOK_DEFINITION_FILE,
    PLAYBOOK_OVERVIEWS_FILE,
    PLAYBOOK_STEPS_DIR,
    PLAYBOOK_TRIGGER_FILE,
    PLAYBOOK_WIDGETS_DIR,
)

# API keys stored in their own file instead of the definition file
_STEPS_KEY = "steps"
_OVERVIEWS_KEY = "overviewTemplates"
_SEPARATE_FILES = {
    "trigger": PLAYBOOK_TRIGGER_FILE,
    _OVERVIEWS_KEY: PLAYBOOK_OVERVIEWS_FILE,
}

# Reference to a widget file in overviews.json
_FILE_MARKER = "$file"
# JSON text marker written by earlier versions, still read
_JSON_MARKER = "$json"
# Key of the widget list in an overview template
_WIDGETS_KEY = "widgets"
# Key holding the position of a widget in its template
_ORDER_KEY = "order"
# Widget data definition: JSON text in the API, parsed in widget files
_DATA_JSON_KEY = "dataDefinitionJson"
_DATA_KEY = "dataDefinition"
# Key of widget HTML in the data definition of HTML widgets
_HTML_KEY = "htmlContent"

# content-hub's WidgetType and WidgetDefinitionScope, stored by name like mp
_WIDGET_TYPES = {
    0: "key_value", 1: "events", 2: "json_result", 3: "html", 4: "wall_activities",
    5: "alerts", 6: "timeline", 7: "graph", 8: "text", 9: "potentially_grouped_alerts",
    10: "entities_highlights", 11: "pending_steps", 12: "case_recommendations",
    13: "statistics", 14: "case_description", 15: "insight", 16: "case_assistant",
    17: "case_aps_graph", 18: "case_related_findings", 19: "case_impacted_resources",
    20: "form", 21: "quick_actions", 22: "composite_alert", 23: "rule_overview",
    24: "case_detections", 25: "udm_events", 26: "udm_entities", 27: "event_highlights",
    28: "threat_hunt", 29: "third_party",
}
_HTML_WIDGET_TYPE = 3
_WIDGET_SCOPES = {0: "case", 1: "alert", 2: "both"}
# content-hub's HtmlWidgetDataDefinition: API key -> (widget file key, names)
_HTML_DATA_FIELDS = {
    "htmlHeight": ("html_height", None),
    "safeRendering": ("safe_rendering", None),
    "type": ("type", _WIDGET_TYPES),
    "widgetDefinitionScope": ("widget_definition_scope", _WIDGET_SCOPES),
}
_HTML_DATA_API_KEYS = {key: api_key for api_key, (key, _) in _HTML_DATA_FIELDS.items()}
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


def _split_overviews(value: Any, files: dict[str, Any]) -> Any:
    """Moves every widget of the overview templates to its own files.

    Args:
        value: Overview templates data to convert.
        files: Playbook files, widget JSON and HTML files are added to it.

    Returns:
        A converted copy of value, with references in place of the widgets.
    """
    if isinstance(value, list):
        return [_split_overviews(x, files) for x in value]
    if not isinstance(value, dict):
        return value

    result = {}
    for key, item in value.items():
        if key == _WIDGETS_KEY and isinstance(item, list):
            # Widgets with the same title and description get numbered file
            # names. Naming them in an order from their content keeps the
            # numbers stable when the API returns the widgets in another order.
            refs: list[Any] = [None] * len(item)
            for i in sorted(range(len(item)), key=lambda i: json.dumps(item[i], sort_keys=True)):
                refs[i] = _split_widget(item[i], files)
            # With an order field in every widget, the list order carries no
            # information, so sort it to keep overviews.json stable.
            if all(isinstance(w, dict) and _ORDER_KEY in w for w in item):
                refs.sort(key=lambda ref: ref[_FILE_MARKER])
            result[key] = refs
        else:
            result[key] = _split_overviews(item, files)
    return result


def _split_widget(widget: Any, files: dict[str, Any]) -> Any:
    """Moves a widget to widgets/<title>_<description>.json, with its data
    definition parsed and, for HTML widgets, its HTML in the .html file."""
    if not isinstance(widget, dict):
        return widget
    base = widget_file_name(
        _text(widget.get("title")) or _text(widget.get("name")),
        _text(widget.get("description")),
    )
    path = _unique_path(files, f"{PLAYBOOK_WIDGETS_DIR}/{base}", ".json")
    widget = dict(widget)
    data_text = widget.get(_DATA_JSON_KEY)
    data = _parse_json_object(data_text) if isinstance(data_text, str) else None
    if data is not None:
        del widget[_DATA_JSON_KEY]
        if _is_html_widget(widget):
            html = data.pop(_HTML_KEY, "") if isinstance(data.get(_HTML_KEY), str) else ""
            if html:
                files[_html_path(path)] = html
            data = _to_html_data_definition(data)
        widget[_DATA_KEY] = data
    files[path] = _sort_keys(widget)
    return {_FILE_MARKER: path}


def _join_widget(path: str, files: dict[str, Any]) -> Any:
    """Reverses _split_widget: turns the widget file back into API data."""
    widget = _join_text(files[path], files)
    if not isinstance(widget, dict) or _DATA_KEY not in widget:
        return widget
    data = widget.pop(_DATA_KEY)
    if _is_html_widget(widget) and isinstance(data, dict):
        data = _from_html_data_definition(data)
        # Like mp, HTML widgets always hold htmlContent, empty without a file
        data[_HTML_KEY] = files.get(_html_path(path), data.get(_HTML_KEY, ""))
    widget[_DATA_JSON_KEY] = json.dumps(data, ensure_ascii=False)
    return widget


def _html_path(widget_path: str) -> str:
    return f"{widget_path.removesuffix('.json')}.html"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _is_html_widget(widget: dict) -> bool:
    widget_type = widget.get("type")
    if isinstance(widget_type, str):
        return widget_type.casefold() == _WIDGET_TYPES[_HTML_WIDGET_TYPE]
    return widget_type == _HTML_WIDGET_TYPE and not isinstance(widget_type, bool)


def _to_html_data_definition(data: dict) -> dict:
    """Converts an API data definition to mp's. Unknown keys and values are kept."""
    result = {}
    for api_key, value in data.items():
        key, names = _HTML_DATA_FIELDS.get(api_key, (api_key, None))
        if names and isinstance(value, int) and value in names:
            value = names[value]
        result[key] = value
    return result


def _from_html_data_definition(data: dict) -> dict:
    """Reverses _to_html_data_definition."""
    result = {}
    for key, value in data.items():
        api_key = _HTML_DATA_API_KEYS.get(key, key)
        names = _HTML_DATA_FIELDS.get(api_key, (None, None))[1]
        if names and isinstance(value, str):
            value = next((number for number, name in names.items() if name == value), value)
        result[api_key] = value
    return result


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


def _join_text(value: Any, files: dict[str, Any]) -> Any:
    """Reverses _split_overviews: puts the widgets back into their templates."""
    if isinstance(value, list):
        return [_join_text(x, files) for x in value]
    if not isinstance(value, dict):
        return value
    if value.keys() == {_JSON_MARKER}:
        return json.dumps(_join_text(value[_JSON_MARKER], files), ensure_ascii=False)
    if value.keys() == {_FILE_MARKER}:
        path = value[_FILE_MARKER]
        if path.endswith(".json"):
            return _join_widget(path, files)
        # HTML file referenced by earlier versions
        return files[path]
    return {k: _join_text(v, files) for k, v in value.items()}


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
        raw_data[_OVERVIEWS_KEY] = _join_text(raw_data[_OVERVIEWS_KEY], files)
    # Steps are linked through identifiers, so their order doesn't matter.
    # Sorting by file name keeps the result stable.
    raw_data[_STEPS_KEY] = [
        copy.deepcopy(files[path])
        for path in sorted(files)
        if path.startswith(f"{PLAYBOOK_STEPS_DIR}/")
    ]
    return raw_data
