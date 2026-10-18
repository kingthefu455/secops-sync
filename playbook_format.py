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

Every widget of the overview templates is moved to its own JSON file, and the
HTML of the widget to an HTML file next to it. overviews.json then only holds
the templates with references to the widget files, as {"$file": ...}.

Overview and widget files have sorted keys. Widgets hold JSON and HTML as
text: JSON text is stored parsed as {"$json": ...} (nested for JSON text that
was encoded twice) and HTML text is moved to the HTML file, which holds the
original HTML with its real line breaks. The files then only change when the
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

# Markers in overviews.json for text the API holds as a string
_JSON_MARKER = "$json"
_FILE_MARKER = "$file"
# Key of the widget list in an overview template
_WIDGETS_KEY = "widgets"
# Key holding the position of a widget in its template
_ORDER_KEY = "order"
# Key of widget HTML, same as content-hub's widget data definition
_HTML_KEY = "htmlContent"
# Other text is also moved to an HTML file when it contains HTML tags
_HTML_TAG = re.compile(r"<(!doctype\s|/?[a-z][\w-]*[\s>/])", re.I)
_LABEL_KEYS = {"title", "name", "description"}
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
        elif isinstance(item, str):
            # Text outside widgets is handled like widget text
            base = f"{PLAYBOOK_WIDGETS_DIR}/overview"
            result[key] = _split_text({key: item}, files, base)[key]
        else:
            result[key] = _split_overviews(item, files)
    return result


def _split_widget(widget: Any, files: dict[str, Any]) -> Any:
    """Moves a widget to widgets/<title>_<description>.json and its HTML next to it."""
    if not isinstance(widget, dict):
        return widget
    base = widget_file_name(
        _text(widget.get("title")) or _text(widget.get("name")),
        _text(widget.get("description")),
    )
    path = _unique_path(files, f"{PLAYBOOK_WIDGETS_DIR}/{base}", ".json")
    files[path] = None  # reserves the path before the HTML file is named
    files[path] = _split_text(widget, files, path.removesuffix(".json"))
    return {_FILE_MARKER: path}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _split_text(value: Any, files: dict[str, Any], file_base: str) -> Any:
    """Parses JSON text and moves HTML text to widget HTML files.

    Args:
        value: Widget data to convert.
        files: Playbook files, widget HTML files are added to it.
        file_base: Path of the widget file without extension, used for HTML files.

    Returns:
        A converted copy of value.
    """
    if isinstance(value, list):
        return [_split_text(x, files, file_base) for x in value]
    if not isinstance(value, dict):
        return value

    result = {}
    for key, item in value.items():
        if isinstance(item, str) and item and (key == _HTML_KEY or _is_html_text(key, item)):
            path = _unique_path(files, file_base, ".html")
            files[path] = item
            result[key] = {_FILE_MARKER: path}
        elif isinstance(item, str) and (parsed := _parse_json_object(item)) is not None:
            result[key] = {_JSON_MARKER: _split_text(_sort_keys(parsed), files, file_base)}
        else:
            result[key] = _split_text(item, files, file_base)
    return result


def _is_html_text(key: str, text: str) -> bool:
    """True for HTML held under another key, but not for short labels like titles."""
    return (
        key not in _LABEL_KEYS
        and ("\n" in text or len(text) >= 200)
        and _HTML_TAG.search(text) is not None
    )


def _parse_json_object(text: str) -> Any:
    """Parses JSON object text, also when it was encoded twice.

    Returns:
        The parsed object, {"$json": object} for twice encoded text, or None.
    """
    if not text.lstrip().startswith(("{", '"')):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, str) and (inner := _parse_json_object(parsed)) is not None:
        return {_JSON_MARKER: inner}
    return None


def _sort_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sort_keys(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sort_keys(x) for x in value]
    return value


def _join_text(value: Any, files: dict[str, Any]) -> Any:
    """Reverses _split_overviews: puts widgets back and turns markers back into
    the text the API expects."""
    if isinstance(value, list):
        return [_join_text(x, files) for x in value]
    if not isinstance(value, dict):
        return value
    if value.keys() == {_JSON_MARKER}:
        return json.dumps(_join_text(value[_JSON_MARKER], files), ensure_ascii=False)
    if value.keys() == {_FILE_MARKER}:
        # A widget JSON file, or the text of a widget HTML file
        return _join_text(files[value[_FILE_MARKER]], files)
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
