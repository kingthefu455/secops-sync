#! /usr/bin/env python3
# Copyright 2025 Google LLC
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

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

import playbook_format
from constants import (
    BASE_PARAMETER_TYPES,
    CONDITION_MATCH_TYPES,
    CONDITION_OPERATORS,
    JOB_README,
    PLAYBOOK_README_TEMPLATE,
    TRIGGER_TYPES,
)
from jinja2 import Environment as JinjaEnvironment


class APIError(Exception):
    """Raised for issues communicating with the SecOps API."""


@dataclass
class SocRole:
    """Represents a SOC Role in Chronicle."""

    name: str
    displayName: str
    description: str


class Content:
    def __init__(self):
        self.readme: str | None = None


class File:
    """Represents a common file object used by the GitManager"""

    def __init__(self, path: str, contents: str | bytes):
        self.path = path
        if isinstance(contents, str):
            contents = contents.encode("utf-8")
        self.content = contents

    def __repr__(self):
        return f"{{Path: {self.path} Contents head: {self.content[:15]}...}}"


@dataclass
class WorkflowMenuCard:
    """Represents a Workflow Menu Card (legacy playbook) in Chronicle."""

    id: str
    identifier: str
    name: str
    displayName: str
    categoryName: str
    modification_time: int
    playbookType: str


class WorkflowTypes(Enum):
    PLAYBOOK = 0
    BLOCK = 1


class PlaybookTypes(Enum):
    REGULAR = "REGULAR"
    NESTED = "NESTED"


class WorkflowCategory(Content):
    """Represents a workflow category in Chronicle."""

    def __init__(self, id: str, name: str, categoryState: str, type: str):
        super().__init__()
        self.id = id
        self.name = name
        self.categoryState = categoryState
        self.type = type


class Workflow(Content):
    def __init__(self, raw_data: dict):
        super().__init__()
        self.raw_data = raw_data
        self.raw_data["id"] = 0
        self.raw_data["trigger"]["id"] = 0
        self.name = self.raw_data.get("name")
        self.description = self.raw_data.get("description")
        self.type = PlaybookTypes(self.raw_data.get("playbookType"))
        self.priority = self.raw_data.get("priority")
        self.isDebugMode = self.raw_data.get("isDebugMode", None)
        self.version = self.raw_data.get("version")
        self.trigger = self.raw_data.get("trigger", {})
        self.steps = self.raw_data.get("steps", [])
        for step in self.steps:
            step.pop("logsExplorerUrl", None)
            for param in step.get("parameters", []):
                param.pop("creationTimeUnixTimeInMs", None)
                param.pop("modificationTimeUnixTimeInMs", None)
                param.pop("additionalProperties", None)
                param.pop("InstanceDisplayName", None)
        self.isEnabled = self.raw_data.get("isEnabled")
        self.category = self.raw_data.get("categoryName", "Default")
        self.environments = self.raw_data.get("environments", [])
        self.modification_time = int(self.raw_data["modificationTimeUnixTimeInMs"])
        self.overview_templates = self.raw_data.get("overviewTemplates", [])
        self.permissions = self.raw_data.get("permissions", [])
        self.entity_access_level = self.raw_data.get("entityAccessLevel", [])
        self.default_access_level = self.raw_data.get("defaultAccessLevel", [])
        self.creation_source = self.raw_data.get("creationSource")
        self.has_restricted_environments = self.raw_data.get(
            "hasRestrictedEnvironments"
        )
        self.execution_scope = self.raw_data.get("executionScope")

    def __hash__(self):
        """Used to remove duplicates in workflow lists"""
        return hash(self.name)

    def __eq__(self, other):
        """Used to remove duplicates in workflow lists"""
        return self.name == getattr(other, "name", None)

    def generate_readme(self, additional_info: str | None = None):
        env = JinjaEnvironment()
        env.globals["WorkflowTypes"] = WorkflowTypes
        env.filters.update(
            {
                "trigger_type": lambda x: TRIGGER_TYPES.get(x),
                "condition_operator": lambda x: CONDITION_OPERATORS.get(x),
                "condition_match_type": lambda x: CONDITION_MATCH_TYPES.get(x),
                "split_action_name": lambda x: x.split("_", 1)[1] if "_" in x else x,
            },
        )
        template = PLAYBOOK_README_TEMPLATE
        if additional_info:
            template += additional_info
        readme = env.from_string(template)
        self.readme = readme.render(
            playbook=self.__dict__,
            involved_blocks=self.get_involved_blocks(),
        )

    def iter_files(self) -> Iterator[File]:
        """Yields the playbook files in the content-hub layout and the README."""
        for path, data in playbook_format.to_files(self.raw_data).items():
            yield File(path, json.dumps(data, indent=4))
        yield File("README.md", self.readme)

    @property
    def folder_name(self) -> str:
        """Name of the playbook folder, same as content-hub."""
        return playbook_format.to_snake_case(self.name)

    def get_involved_blocks(self):
        return [x for x in self.steps if x.get("type") == 5]

    def _is_integration_instance_param(
        self,
        param_name: str | None,
        param_value: str | None,
    ) -> bool:
        """Checks if a parameter is a valid integration instance parameter."""
        return param_name in (
            "IntegrationInstance",
            "FallbackIntegrationInstance",
        ) and self._is_valid_instance_id(param_value)

    def _is_valid_instance_id(self, instance_id: str) -> bool:
        try:
            val = uuid.UUID(instance_id, version=4)

        except (TypeError, ValueError):
            return False

        return str(val) == instance_id and val.version == 4