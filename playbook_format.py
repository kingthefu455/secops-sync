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
"""

from __future__ import annotations

import copy
import re
from typing import Any

from config import (
    PLAYBOOK_DEFINITION_FILE,
    PLAYBOOK_OVERVIEWS_FILE,
    PLAYBOOK_STEPS_DIR,
    PLAYBOOK_TRIGGER_FILE,
)

# API keys stored in their own file instead of the definition file
_STEPS_KEY = "steps"
_SEPARATE_FILES = {
    "trigger": PLAYBOOK_TRIGGER_FILE,
    "overviewTemplates": PLAYBOOK_OVERVIEWS_FILE,
}


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
        A mapping of file path (relative to the playbook folder) to file data.
    """
    definition = copy.deepcopy(raw_data)
    steps = definition.pop(_STEPS_KEY, [])
    files: dict[str, Any] = {PLAYBOOK_DEFINITION_FILE: definition}
    for key, file_name in _SEPARATE_FILES.items():
        if key in definition:
            files[file_name] = definition.pop(key)

    for step in steps:
        base = step_file_name(step.get("instanceName"), step.get("identifier"))
        path, suffix = f"{PLAYBOOK_STEPS_DIR}/{base}.json", 1
        while path in files:
            suffix += 1
            path = f"{PLAYBOOK_STEPS_DIR}/{base}_{suffix}.json"
        files[path] = step
    return files


def from_files(files: dict[str, Any]) -> dict:
    """Reassembles API playbook JSON from the data of its files.

    Args:
        files: A mapping of file path (relative to the playbook folder) to file
            data, as returned by to_files.
    """
    raw_data = copy.deepcopy(files[PLAYBOOK_DEFINITION_FILE])
    for key, file_name in _SEPARATE_FILES.items():
        if file_name in files:
            raw_data[key] = copy.deepcopy(files[file_name])
    # Steps are linked through identifiers, so their order doesn't matter.
    # Sorting by file name keeps the result stable.
    raw_data[_STEPS_KEY] = [
        copy.deepcopy(files[path])
        for path in sorted(files)
        if path.startswith(f"{PLAYBOOK_STEPS_DIR}/")
    ]
    return raw_data
