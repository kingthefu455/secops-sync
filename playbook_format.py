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
        widgets/<widget_title>_<identifier[:5]>.html

Widgets hold JSON and HTML as text. In overviews.json, JSON text is stored
parsed with sorted keys as {"$json": ...} and HTML is moved to its own file,
referenced as {"$file": ...}. The files then only change when the content
does, not when the API formats the text differently.
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
# Key of widget HTML, same as content-hub's widget data definition
_HTML_KEY = "htmlContent"


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
        files[PLAYBOOK_OVERVIEWS_FILE] = _split_text(
            files[PLAYBOOK_OVERVIEWS_FILE], files, "widget"
        )
    return files


def _unique_path(files: dict[str, Any], base: str, extension: str) -> str:
    path, suffix = f"{base}{extension}", 1
    while path in files:
        suffix += 1
        path = f"{base}_{suffix}{extension}"
    return path


def _split_text(value: Any, files: dict[str, Any], file_base: str) -> Any:
    """Parses JSON text and moves HTML text to widget files.

    Args:
        value: Overview templates data to convert.
        files: Playbook files, widget HTML files are added to it.
        file_base: Base name for HTML files, from the closest widget.

    Returns:
        A converted copy of value.
    """
    if isinstance(value, list):
        return [_split_text(x, files, file_base) for x in value]
    if not isinstance(value, dict):
        return value

    if isinstance(value.get("title"), str):
        file_base = step_file_name(value["title"], value.get("identifier"))
    result = {}
    for key, item in value.items():
        if key == _HTML_KEY and isinstance(item, str) and item:
            path = _unique_path(files, f"{PLAYBOOK_WIDGETS_DIR}/{file_base}", ".html")
            files[path] = item
            result[key] = {_FILE_MARKER: path}
        elif isinstance(item, str) and (parsed := _parse_json_object(item)) is not None:
            result[key] = {_JSON_MARKER: _split_text(_sort_keys(parsed), files, file_base)}
        else:
            result[key] = _split_text(item, files, file_base)
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
    """Reverses _split_text: turns markers back into the text the API expects."""
    if isinstance(value, list):
        return [_join_text(x, files) for x in value]
    if not isinstance(value, dict):
        return value
    if value.keys() == {_JSON_MARKER}:
        return json.dumps(_join_text(value[_JSON_MARKER], files), ensure_ascii=False)
    if value.keys() == {_FILE_MARKER}:
        return files[value[_FILE_MARKER]]
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
