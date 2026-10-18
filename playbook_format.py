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

Every widget of the overview templates is moved to its own JSON file with its
data as the API returns it. overviews.json then holds the templates with
references to the widget files, as {"$file": ...}, and is otherwise unchanged.

Only widgets with HTML content also get an HTML file: the data definition the
API holds as JSON text (dataDefinitionJson) is stored parsed, without its
htmlContent, and the HTML is stored in the .html file of the same name with
its real line breaks, like content-hub's mp tool does. The data definition of
other widgets stays as it is.

Overview and widget files have sorted keys, so they don't change when the API
orders the keys differently.
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
            result[key] = refs
        else:
            result[key] = _split_overviews(item, files)
    return result


def _split_widget(widget: Any, files: dict[str, Any]) -> Any:
    """Moves a widget to widgets/<title>_<description>.json and, when it has
    HTML content, the HTML to the .html file of the same name."""
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
    if data is not None and isinstance(data.get(_HTML_KEY), str) and data[_HTML_KEY]:
        files[_html_path(path)] = data.pop(_HTML_KEY)
        widget[_DATA_JSON_KEY] = data
    files[path] = _sort_keys(widget)
    return {_FILE_MARKER: path}


def _join_widget(path: str, files: dict[str, Any]) -> Any:
    """Reverses _split_widget: puts the HTML back into the data definition text."""
    widget = _join_text(files[path], files)
    if isinstance(widget, dict) and isinstance(widget.get(_DATA_JSON_KEY), dict):
        data = dict(widget[_DATA_JSON_KEY])
        if _html_path(path) in files:
            data[_HTML_KEY] = files[_html_path(path)]
        widget[_DATA_JSON_KEY] = json.dumps(data, ensure_ascii=False)
    return widget


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
