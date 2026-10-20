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

"""Checks the playbooks in the repo before they are synced to the instance.

The checks make no API calls and need no credentials, so they can run on every
pull request. Playbooks are read the same way sync-playbooks reads them.

Errors are problems that make sync-playbooks or the next pull-playbooks fail,
or that leave a playbook pointing at steps it doesn't have. Warnings are files
the sync ignores and folders pull-playbooks will move.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import playbook_format
from config import (
    PLAYBOOK_DEFINITION_FILE,
    PLAYBOOK_OVERVIEWS_FILE,
    PLAYBOOK_STEPS_DIR,
    PLAYBOOK_TRIGGER_FILE,
    PLAYBOOK_WIDGETS_DIR,
    PLAYBOOKS_PATH,
)
from constants import STEP_TYPE
from models import PlaybookTypes, Workflow
from utils import iter_playbook_paths, read_playbook_files

ERROR = "error"
WARNING = "warning"

# Files pull-playbooks writes to a playbook folder, besides steps and widgets
_PLAYBOOK_FILES = {
    PLAYBOOK_DEFINITION_FILE,
    PLAYBOOK_TRIGGER_FILE,
    PLAYBOOK_OVERVIEWS_FILE,
    "README.md",
}
# Step parameters sync-playbooks sets the integration instance in
_INSTANCE_PARAMETERS = ("IntegrationInstance", "FallbackIntegrationInstance")


@dataclass(frozen=True)
class Issue:
    """A problem in a file. The path is relative to the repo root."""

    severity: str
    path: str
    message: str
    line: int | None = None

    def format(self, github: bool = False) -> str:
        """Formats the issue as a log line, or as a GitHub Actions annotation."""
        location = f"{self.path}:{self.line}" if self.line else self.path
        if github:
            properties = f"file={_escape(self.path, is_property=True)}"
            if self.line:
                properties += f",line={self.line}"
            # The job log only shows the message, so it repeats the location
            return f"::{self.severity} {properties}::{_escape(f'{location}: {self.message}')}"
        return f"{self.severity.upper()} {location}: {self.message}"


@dataclass
class Report:
    playbook_count: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [x for x in self.issues if x.severity == ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [x for x in self.issues if x.severity == WARNING]


@dataclass
class _Sources:
    """Paths of the files that hold each part of a playbook."""

    definition: str
    trigger: str
    overviews: str
    # One path per step, in the order of the playbook steps
    steps: list[str]


def check_playbooks(repo_root: str) -> Report:
    """Checks every playbook in the repo.

    All playbooks are checked, not only changed ones, since sync-playbooks
    installs all of them and playbook names must be unique across the repo.
    """
    checker = _Checker(repo_root)
    playbooks_root = os.path.join(repo_root, PLAYBOOKS_PATH)
    if os.path.isdir(playbooks_root):
        checker.check_all(playbooks_root)
    return checker.report


class _Checker:
    def __init__(self, repo_root: str):
        self._repo_root = repo_root
        self.report = Report()

    def _relative(self, path: str) -> str:
        return os.path.relpath(path, self._repo_root).replace(os.sep, "/")

    def _error(self, path: str, message: str, line: int | None = None) -> None:
        self.report.issues.append(Issue(ERROR, self._relative(path), message, line))

    def _warning(self, path: str, message: str) -> None:
        self.report.issues.append(Issue(WARNING, self._relative(path), message))

    def check_all(self, playbooks_root: str) -> None:
        unreadable = self._check_json_files(playbooks_root)
        # Playbook name to path, and pull-playbooks folder to (name, path)
        names: dict[str, str] = {}
        folders: dict[str, tuple[str, str]] = {}

        for path in iter_playbook_paths(playbooks_root):
            self.report.playbook_count += 1
            if any(x == path or x.startswith(path + os.sep) for x in unreadable):
                # Its JSON errors are reported already
                continue
            playbook = self._check_playbook(path)
            if playbook is None:
                continue

            if playbook.name in names:
                self._error(
                    path,
                    f"Playbook '{playbook.name}' is also stored in "
                    f"{self._relative(names[playbook.name])}. "
                    "Playbook names must be unique",
                )
                continue
            names[playbook.name] = path

            folder = f"{PLAYBOOKS_PATH}/{playbook.category}/{playbook.folder_name}"
            if folder in folders:
                other_name, other_path = folders[folder]
                self._error(
                    path,
                    f"Playbook '{playbook.name}' and playbook '{other_name}' "
                    f"({self._relative(other_path)}) both map to the folder {folder}, "
                    "so pull-playbooks can't store both. Rename one of them",
                )
            folders[folder] = (playbook.name, path)
            if os.path.isdir(path) and self._relative(path) != folder:
                self._warning(
                    os.path.join(path, PLAYBOOK_DEFINITION_FILE),
                    f"pull-playbooks will move playbook '{playbook.name}' to {folder}",
                )

    def _check_json_files(self, playbooks_root: str) -> set[str]:
        """Reports the JSON files that can't be parsed and returns their paths."""
        unreadable = set()
        for dir_path, dir_names, file_names in os.walk(playbooks_root):
            dir_names.sort()
            for file_name in sorted(file_names):
                if not file_name.endswith(".json"):
                    continue
                path = os.path.join(dir_path, file_name)
                try:
                    with open(path, encoding="utf-8") as f:
                        json.load(f)
                except json.JSONDecodeError as e:
                    self._error(path, f"Invalid JSON: {e.msg} (column {e.colno})", e.lineno)
                    unreadable.add(path)
                except (OSError, ValueError) as e:
                    self._error(path, f"Can't read the file: {e}")
                    unreadable.add(path)
        return unreadable

    def _check_playbook(self, path: str) -> Workflow | None:
        """Checks a playbook folder, or the file of a legacy playbook.

        Returns:
            The playbook as sync-playbooks reads it, or None if it has errors.
        """
        error_count = len(self.report.errors)
        if os.path.isdir(path):
            try:
                files = read_playbook_files(path)
            except (OSError, ValueError) as e:
                self._error(path, f"Can't read the playbook: {e}")
                return None
            if not self._check_folder(path, files):
                return None
            raw_data = playbook_format.from_files(files)
            definition = os.path.join(path, PLAYBOOK_DEFINITION_FILE)
            sources = _Sources(
                definition=definition,
                trigger=os.path.join(path, PLAYBOOK_TRIGGER_FILE)
                if PLAYBOOK_TRIGGER_FILE in files
                else definition,
                overviews=os.path.join(path, PLAYBOOK_OVERVIEWS_FILE)
                if PLAYBOOK_OVERVIEWS_FILE in files
                else definition,
                steps=[
                    os.path.join(path, x)
                    for x in sorted(files)
                    if x.startswith(f"{PLAYBOOK_STEPS_DIR}/")
                ],
            )
        else:
            with open(path, encoding="utf-8") as f:
                raw_data = json.load(f)
            if not isinstance(raw_data, dict):
                self._error(path, "A playbook file must hold a JSON object")
                return None
            steps = raw_data.get("steps")
            sources = _Sources(
                definition=path,
                trigger=path,
                overviews=path,
                steps=[path] * len(steps) if isinstance(steps, list) else [],
            )

        self._check_definition(raw_data, sources)
        if len(self.report.errors) > error_count:
            return None
        try:
            return Workflow(raw_data)
        except Exception as e:  # noqa: BLE001
            self._error(sources.definition, f"sync-playbooks can't read the playbook: {e!r}")
            return None

    def _check_folder(self, path: str, files: dict) -> bool:
        """Checks that the files of a playbook folder fit together.

        Returns:
            False if the files can't be put together into a playbook.
        """
        definition = files[PLAYBOOK_DEFINITION_FILE]
        if not isinstance(definition, dict):
            self._error(
                os.path.join(path, PLAYBOOK_DEFINITION_FILE),
                "The definition file must hold a JSON object",
            )
            return False

        ok = True
        templates = files.get(PLAYBOOK_OVERVIEWS_FILE, definition.get("overviewTemplates"))
        referenced = set()
        for ref in playbook_format.iter_file_refs(templates):
            if isinstance(ref, str) and ref in files:
                referenced.add(ref)
            else:
                self._error(
                    os.path.join(path, PLAYBOOK_OVERVIEWS_FILE),
                    f"Widget file {ref} doesn't exist",
                )
                ok = False

        for dir_path, _, file_names in os.walk(path):
            for file_name in sorted(file_names):
                file_path = os.path.join(dir_path, file_name)
                relative = os.path.relpath(file_path, path).replace(os.sep, "/")
                folder, _, name = relative.rpartition("/")
                if relative.startswith(f"{PLAYBOOK_WIDGETS_DIR}/") and relative in files:
                    if relative not in referenced:
                        self._warning(
                            file_path,
                            "No widget uses this file, so sync-playbooks ignores it",
                        )
                elif not (
                    relative in _PLAYBOOK_FILES
                    or (folder == PLAYBOOK_STEPS_DIR and name.endswith(".json"))
                ):
                    self._warning(file_path, "sync-playbooks ignores this file")
        return ok

    def _check_definition(self, raw_data: dict, sources: _Sources) -> None:
        """Checks the playbook fields sync-playbooks reads."""
        definition = sources.definition
        name = raw_data.get("name")
        if not isinstance(name, str) or not name.strip():
            self._error(definition, "'name' must be a non-empty string")
        if not isinstance(raw_data.get("trigger"), dict):
            self._error(sources.trigger, "'trigger' must be an object")

        playbook_types = [x.value for x in PlaybookTypes]
        if raw_data.get("playbookType") not in playbook_types:
            self._error(
                definition,
                f"'playbookType' must be one of {', '.join(playbook_types)}, "
                f"not {raw_data.get('playbookType')!r}",
            )
        try:
            int(raw_data.get("modificationTimeUnixTimeInMs"))
        except (TypeError, ValueError):
            self._error(definition, "'modificationTimeUnixTimeInMs' must be a number")
        if not isinstance(raw_data.get("environments", []), list):
            self._error(definition, "'environments' must be a list")

        templates = raw_data.get("overviewTemplates", [])
        if not isinstance(templates, list) or not all(
            isinstance(x, dict) and isinstance(x.get("roles"), list) for x in templates
        ):
            self._error(
                sources.overviews,
                "'overviewTemplates' must be a list of objects that each have a 'roles' list",
            )

        relations = raw_data.get("stepsRelations")
        if not isinstance(relations, list):
            self._error(definition, "'stepsRelations' must be a list")
            relations = []
        steps = raw_data.get("steps")
        if not isinstance(steps, list):
            self._error(definition, "'steps' must be a list")
            return
        self._check_steps(steps, sources, relations)

    def _check_steps(self, steps: list, sources: _Sources, relations: list) -> None:
        """Checks the steps and that everything referencing a step finds it."""
        # Every step, including parallel actions, with the file it is stored in
        all_steps: list[tuple[dict, str]] = []
        # Identifier of each step file, to its instance name and file
        step_files: dict[str, tuple[str, str]] = {}
        for step, path in zip(steps, sources.steps):
            if not isinstance(step, dict):
                self._error(path, "A step must be an object")
                continue
            identifier = step.get("identifier")
            if isinstance(identifier, str) and identifier:
                if identifier in step_files:
                    other_name, other_path = step_files[identifier]
                    self._error(
                        path,
                        f"Step '{step.get('instanceName')}' has the same identifier as "
                        f"step '{other_name}' ({self._relative(other_path)}): {identifier}",
                    )
                else:
                    step_files[identifier] = (step.get("instanceName"), path)

            all_steps.append((step, path))
            if step.get("actionProvider") == "ParallelActionsContainer":
                children = step.get("parallelActions")
                if isinstance(children, list) and all(isinstance(x, dict) for x in children):
                    all_steps.extend((x, path) for x in children)
                else:
                    self._error(
                        path,
                        f"Step '{step.get('instanceName')}': "
                        "'parallelActions' must be a list of steps",
                    )

        identifiers = {
            x.get("identifier") for x, _ in all_steps if isinstance(x.get("identifier"), str)
        }
        for step, path in all_steps:
            self._check_step(step, path, identifiers)

        for relation in relations:
            if not isinstance(relation, dict):
                self._error(sources.definition, "'stepsRelations' must only hold objects")
                continue
            for key in ("fromStep", "toStep"):
                if _is_missing_step(relation.get(key), identifiers):
                    self._error(
                        sources.definition,
                        f"A step relation's '{key}' is step {relation.get(key)}, "
                        "which isn't in the playbook",
                    )

    def _check_step(self, step: dict, path: str, identifiers: set[str]) -> None:
        name = step.get("instanceName")
        is_action = step.get("type") == STEP_TYPE and step.get("actionProvider") == "Scripts"
        if "parameters" in step or is_action or step.get("type") == "BLOCK":
            parameters = step.get("parameters")
            if not isinstance(parameters, list) or not all(
                isinstance(x, dict) for x in parameters
            ):
                self._error(path, f"Step '{name}': 'parameters' must be a list of objects")
            elif is_action:
                parameter_names = [x.get("name") for x in parameters]
                missing = [x for x in _INSTANCE_PARAMETERS if x not in parameter_names]
                if missing:
                    self._error(
                        path,
                        f"Step '{name}' is an integration action without the "
                        f"parameter(s) {', '.join(missing)}. sync-playbooks sets the "
                        "integration instance in them",
                    )

        for key in ("startLoopStepIdentifier", "endLoopStepIdentifier"):
            if _is_missing_step(step.get(key), identifiers):
                self._error(
                    path,
                    f"Step '{name}': '{key}' is step {step.get(key)}, "
                    "which isn't in the playbook",
                )


def _is_missing_step(identifier: object, identifiers: set[str]) -> bool:
    return isinstance(identifier, str) and bool(identifier) and identifier not in identifiers


def _escape(text: str, is_property: bool = False) -> str:
    """Escapes text for a GitHub Actions workflow command."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if is_property:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text
