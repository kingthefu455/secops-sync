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

"""Prints a report of what a merge request changes and what the validation finds.

    python validation_report.py --base origin/main

The report compares HEAD with its merge base with --base:

- Needs attention: the changes reviewers should look at first.
- Playbook changes: the added, changed, renamed and deleted playbooks, with
  the fields, trigger, steps and connections that changed.
- Findings: the validation findings of the checked out playbooks, grouped
  into the playbooks the merge request changes and the other playbooks.
- Other changed files: the changed files outside the playbooks.

The script only prints the report and writes no files: it reads the playbooks
of both commits from git. To also keep Python from writing its .pyc cache,
run it with python -B. It exits with 0 even if there are errors, since the
validate-playbooks command is the check that fails on them.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Container
from dataclasses import dataclass

import playbook_format
from config import (
    INTEGRATIONS_PATH,
    PLAYBOOK_DEFINITION_FILE,
    PLAYBOOK_STEPS_DIR,
    PLAYBOOK_WIDGETS_DIR,
    PLAYBOOKS_PATH,
)
from validation import ERROR, Issue, Report, check_playbooks

# Playbook keys that are compared on their own, not as plain fields
_PARTS = {"name", "trigger", "steps", "stepsRelations", "overviewTemplates"}
# Longer values are reported as changed, without showing them
_MAX_VALUE_LENGTH = 80
_FILE_STATUSES = {"A": "Added", "D": "Deleted", "M": "Modified", "T": "Type changed"}


class ReportError(Exception):
    """Raised when git can't provide the changes to report."""


@dataclass(eq=False)
class Playbook:
    path: str  # Folder, or file of a legacy playbook, relative to the repo root
    data: dict  # The playbook as the API takes it, without timestamps

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.path)


@dataclass
class PlaybookChange:
    old: Playbook | None  # None if the merge request adds the playbook
    new: Playbook | None  # None if the merge request deletes the playbook
    details: list[str]  # What changed, one line per change

    @property
    def kind(self) -> str:
        if self.old is None:
            return "Added"
        if self.new is None:
            return "Deleted"
        return "Renamed" if self.old.name != self.new.name else "Changed"

    @property
    def playbook(self) -> Playbook:
        return self.new or self.old


@dataclass
class ChangedFile:
    status: str
    path: str


def build_report(repo_root: str, base: str) -> str:
    """Builds the report of the changes from the merge base with base to HEAD."""
    merge_base = _merge_base(repo_root, base)
    head = _git(repo_root, "rev-parse", "HEAD").decode().strip()
    base_playbooks = _read_playbooks(repo_root, merge_base)
    head_playbooks = _read_playbooks(repo_root, head)
    findings = check_playbooks(repo_root)

    changes = _compare_playbooks(base_playbooks, head_playbooks)
    changed_files = _changed_files(repo_root, merge_base)
    playbook_paths = [x.path for x in base_playbooks + head_playbooks]
    other_files = [
        x for x in changed_files if not any(_is_in(x.path, p) for p in playbook_paths)
    ]
    changed_paths = [x.path for x in changed_files]
    in_changed_playbooks = [
        x for x in findings.issues if _is_changed(x.path, playbook_paths, changed_paths)
    ]

    sections = [
        _summary(head, base, merge_base, findings, in_changed_playbooks),
        _section("Needs attention", _attention(changes, changed_files)),
        _section("Playbook changes", _playbook_lines(changes)),
        _section("Findings", _finding_lines(findings, in_changed_playbooks)),
        _section("Other changed files", [f"{x.status}: {x.path}" for x in other_files]),
    ]
    return "\n\n".join(sections)


# Reading from git


def _git(repo_root: str, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", repo_root, *args], input=stdin, capture_output=True
    )
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip()
        raise ReportError(f"git {' '.join(args)} failed: {error}")
    return result.stdout


def _merge_base(repo_root: str, base: str) -> str:
    """Returns the commit where HEAD branched off from base."""
    try:
        _git(repo_root, "rev-parse", "--verify", f"{base}^{{commit}}")
    except ReportError:
        raise ReportError(
            f"Can't find {base}. Fetch it first, for example: git fetch origin main"
        ) from None
    try:
        return _git(repo_root, "merge-base", base, "HEAD").decode().strip()
    except ReportError:
        raise ReportError(
            f"HEAD and {base} have no common commit. A shallow clone needs more "
            "history, for example GIT_DEPTH: 0 in GitLab CI"
        ) from None


def _changed_files(repo_root: str, merge_base: str) -> list[ChangedFile]:
    """Lists the files changed from merge_base to HEAD.

    A renamed file is listed as deleted and added.
    """
    output = _git(
        repo_root, "diff", "--name-status", "--no-renames", "-z", merge_base, "HEAD"
    )
    # The output alternates between a status letter and a path, each ending with NUL
    fields = output.decode(errors="replace").split("\0")[:-1]
    return [
        ChangedFile(_FILE_STATUSES.get(status, status), path)
        for status, path in zip(fields[::2], fields[1::2], strict=True)
    ]


def _read_playbooks(repo_root: str, commit: str) -> list[Playbook]:
    """Reads the playbooks of a commit from git, in the same layout as utils does.

    A folder with a definition file is a playbook. Any other JSON file is a
    legacy single-file playbook. Playbooks that can't be read are skipped, the
    findings report them.
    """
    files = _git_files(repo_root, commit, PLAYBOOKS_PATH)
    folders = {
        os.path.dirname(x)
        for x in files
        if os.path.basename(x) == PLAYBOOK_DEFINITION_FILE
    }
    legacy_files = [
        x for x in files if x.endswith(".json") and not any(_is_in(x, f) for f in folders)
    ]

    playbooks = []
    for path in sorted(folders) + sorted(legacy_files):
        try:
            if path in folders:
                data = playbook_format.from_files(_playbook_files(files, path))
            else:
                data = json.loads(files[path])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            playbooks.append(Playbook(path, _without_timestamps(data)))
    return playbooks


def _git_files(repo_root: str, commit: str, folder: str) -> dict[str, bytes]:
    """Returns the content of the files in a folder of a commit, by path."""
    listing = _git(repo_root, "ls-tree", "-r", "-z", "--name-only", commit, "--", folder)
    paths = listing.decode().split("\0")[:-1]
    # cat-file --batch prints "<sha> <type> <size>", a newline, the content and
    # a newline for every object it is asked for
    request = "".join(f"{commit}:{x}\n" for x in paths).encode()
    output = _git(repo_root, "cat-file", "--batch", stdin=request)
    files, position = {}, 0
    for path in paths:
        header_end = output.index(b"\n", position)
        size = int(output[position:header_end].split()[2])
        files[path] = output[header_end + 1 : header_end + 1 + size]
        position = header_end + 1 + size + 1
    return files


def _playbook_files(files: dict[str, bytes], folder: str) -> dict:
    """Picks the files of a playbook folder that utils.read_playbook_files reads."""
    result = {}
    for path, content in files.items():
        if not path.startswith(f"{folder}/"):
            continue
        name = path[len(folder) + 1 :]
        subfolder, _, file_name = name.rpartition("/")
        if subfolder in ("", PLAYBOOK_STEPS_DIR) and file_name.endswith(".json"):
            result[name] = json.loads(content)
        elif subfolder == PLAYBOOK_WIDGETS_DIR and file_name.endswith(".html"):
            result[name] = content.decode("utf-8")
    return result


def _without_timestamps(value: object) -> object:
    """Removes the timestamps the platform sets on save. They aren't changes to review."""
    if isinstance(value, dict):
        return {
            k: _without_timestamps(v)
            for k, v in value.items()
            if not k.endswith("UnixTimeInMs")
        }
    if isinstance(value, list):
        return [_without_timestamps(x) for x in value]
    return value


# Comparing playbooks


def _compare_playbooks(
    base: list[Playbook], head: list[Playbook]
) -> list[PlaybookChange]:
    """Lists the playbooks the merge request adds, changes, renames or deletes."""
    pairs, deleted = _pair_playbooks(base, head)
    changes = []
    for old, new in pairs:
        if old is None:
            changes.append(PlaybookChange(None, new, []))
        elif details := _differences(old, new):
            changes.append(PlaybookChange(old, new, details))
    changes += [PlaybookChange(old, None, []) for old in deleted]
    return changes


def _pair_playbooks(
    base: list[Playbook], head: list[Playbook]
) -> tuple[list[tuple[Playbook | None, Playbook]], list[Playbook]]:
    """Pairs every playbook with its base version, which is None for new playbooks.

    Returns:
        The pairs, and the base playbooks without a pair, which are deleted.
    """
    unmatched = list(base)
    old_versions: list[Playbook | None] = []
    # Match by name first, since a copied playbook keeps the identifier of its original
    for new in head:
        old = next((x for x in unmatched if x.name == new.name), None)
        if old:
            unmatched.remove(old)
        old_versions.append(old)
    # A renamed playbook keeps its identifier
    for i, new in enumerate(head):
        identifier = new.data.get("identifier")
        if old_versions[i] is None and identifier:
            old = next(
                (x for x in unmatched if x.data.get("identifier") == identifier), None
            )
            if old:
                unmatched.remove(old)
            old_versions[i] = old
    return list(zip(old_versions, head, strict=True)), unmatched


def _differences(old: Playbook, new: Playbook) -> list[str]:
    """Describes how a playbook changed, one line per change."""
    lines = []
    if old.name != new.name:
        lines.append(f"Renamed from {_quote(old.name)}")
    if old.path != new.path:
        lines.append(f"Moved from {old.path} to {new.path}")
    lines += _field_differences(old.data, new.data, ignore=_PARTS)

    old_trigger, new_trigger = _dict(old.data.get("trigger")), _dict(new.data.get("trigger"))
    lines += [f"Trigger {x}" for x in _field_differences(old_trigger, new_trigger)]

    lines += _step_differences(old.data, new.data)
    lines += _connection_differences(old.data, new.data)
    if old.data.get("overviewTemplates") != new.data.get("overviewTemplates"):
        lines.append("The overview templates or their widgets changed")
    return lines


def _step_differences(old_data: dict, new_data: dict) -> list[str]:
    old, new = _steps(old_data), _steps(new_data)
    lines = [f"Step added: {_step_label(x)}" for key, x in new.items() if key not in old]
    lines += [f"Step removed: {_step_label(x)}" for key, x in old.items() if key not in new]
    for key, step in new.items():
        if key not in old:
            continue
        changes = _field_differences(
            old[key], step, ignore={"parameters", "parallelActions"}
        )
        changes += [
            f"parameter {x}"
            for x in _field_differences(_parameters(old[key]), _parameters(step))
        ]
        if changes:
            name = _quote(step.get("instanceName") or key)
            lines.append(f"Step {name} changed: {'; '.join(changes)}")
    return lines


def _connection_differences(old_data: dict, new_data: dict) -> list[str]:
    old, new = _connections(old_data), _connections(new_data)
    steps = {**_steps(old_data), **_steps(new_data)}

    def label(connection: tuple[str, str]) -> str:
        return " -> ".join(
            _quote(steps.get(x, {}).get("instanceName") or x) for x in connection
        )

    lines = [f"Connection added: {label(x)}" for x in sorted(new - old)]
    lines += [f"Connection removed: {label(x)}" for x in sorted(old - new)]
    return lines


def _field_differences(
    old: dict, new: dict, ignore: Container[str] = ()
) -> list[str]:
    """Describes the keys whose values differ, one line per key."""
    lines = []
    for key in sorted(old.keys() | new.keys()):
        if key in ignore:
            continue
        if key not in new:
            lines.append(f"{key} removed")
        elif key not in old:
            value = _short_json(new[key])
            lines.append(f"{key} added" + (f": {value}" if value else ""))
        elif old[key] != new[key]:
            before, after = _short_json(old[key]), _short_json(new[key])
            if before and after:
                lines.append(f"{key}: {before} -> {after}")
            else:
                lines.append(f"{key} changed")
    return lines


def _steps(data: dict) -> dict[str, dict]:
    """Returns the steps of a playbook and their parallel actions, by identifier."""
    steps = [x for x in _list(data.get("steps")) if isinstance(x, dict)]
    steps += [
        x
        for step in steps
        for x in _list(step.get("parallelActions"))
        if isinstance(x, dict)
    ]
    return {str(x.get("identifier") or x.get("instanceName")): x for x in steps}


def _parameters(step: dict) -> dict[str, object]:
    """Returns the parameter values of a step, by parameter name."""
    return {
        str(x.get("name")): x.get("value")
        for x in _list(step.get("parameters"))
        if isinstance(x, dict)
    }


def _connections(data: dict) -> set[tuple[str, str]]:
    """Returns the connections between the steps, as (from, to) step identifiers."""
    return {
        (str(x.get("fromStep")), str(x.get("toStep")))
        for x in _list(data.get("stepsRelations"))
        if isinstance(x, dict)
    }


def _step_label(step: dict) -> str:
    label = _quote(step.get("instanceName") or step.get("identifier"))
    action = " / ".join(
        str(x) for x in (step.get("integration"), step.get("actionName")) if x
    )
    return f"{label} ({action})" if action else label


# Report sections


def _summary(
    head: str,
    base: str,
    merge_base: str,
    findings: Report,
    in_changed_playbooks: list[Issue],
) -> str:
    if merge_base.startswith(base):
        compared = f"its merge base {merge_base[:7]}"
    else:
        compared = f"{base} (merge base {merge_base[:7]})"

    errors, warnings = findings.errors, findings.warnings
    changed_errors = [x for x in in_changed_playbooks if x.severity == ERROR]
    if errors:
        result = (
            f"{len(errors)} error(s), {len(changed_errors)} of them in playbooks "
            f"this merge request changes, and {len(warnings)} warning(s)"
        )
    else:
        result = f"no errors, {len(warnings)} warning(s)"

    return "\n".join(
        [
            "VALIDATION REPORT",
            f"Compares HEAD ({head[:7]}) with {compared}.",
            f"Result: {result}. Checked {findings.playbook_count} playbook(s).",
        ]
    )


def _section(title: str, lines: list[str]) -> str:
    """Formats a section: the title in capitals and the lines indented under it."""
    return "\n".join([title.upper()] + [f"  {x}" for x in lines or ["None."]])


def _attention(changes: list[PlaybookChange], changed_files: list[ChangedFile]) -> list[str]:
    """Lists the changes reviewers should look at first."""
    lines = []
    for change in changes:
        old, new = change.old, change.new
        name = _quote(change.playbook.name)
        if old is None:
            if new.data.get("isEnabled") is True:
                lines.append(f"{name} is added and enabled")
        elif new is None:
            lines.append(
                f"{name} is deleted. sync-playbooks doesn't delete playbooks, so it "
                "stays in SecOps and the next pull-playbooks adds it back. "
                "Delete it in SecOps as well"
            )
        else:
            if old.name != new.name:
                lines.append(f"{_quote(old.name)} is renamed to {name}")
            if old.data.get("isEnabled") != new.data.get("isEnabled"):
                state = "enabled" if new.data.get("isEnabled") else "disabled"
                lines.append(f"{name} is now {state}")
            if old.data.get("environments") != new.data.get("environments"):
                lines.append(f"The environments of {name} changed")
            if old.data.get("trigger") != new.data.get("trigger"):
                lines.append(f"The trigger of {name} changed")
            removed = len(_steps(old.data).keys() - _steps(new.data).keys())
            if removed:
                lines.append(f"{removed} step(s) removed from {name}")

    tool_files = [x.path for x in changed_files if _is_tool_file(x.path)]
    if tool_files:
        lines.append(f"The sync tool or its pipeline changed: {', '.join(tool_files)}")
    return [f"- {x}" for x in lines]


def _playbook_lines(changes: list[PlaybookChange]) -> list[str]:
    """Lists every changed playbook, with what changed indented under it."""
    lines = []
    for change in changes:
        playbook = change.playbook
        lines.append(f"{change.kind}: {_quote(playbook.name)} ({playbook.path})")
        lines += [f"  - {x}" for x in change.details]
    return lines


def _finding_lines(findings: Report, in_changed_playbooks: list[Issue]) -> list[str]:
    groups = {
        "In playbooks this merge request changes": in_changed_playbooks,
        "In other playbooks": [
            x for x in findings.issues if x not in in_changed_playbooks
        ],
    }
    lines = []
    for title, issues in groups.items():
        if issues:
            lines.append(f"{title} ({len(issues)}):")
            lines += [f"  {_finding(x)}" for x in issues]
    return lines


def _finding(issue: Issue) -> str:
    severity = "ERROR" if issue.severity == ERROR else "WARNING"
    location = f"{issue.path}:{issue.line}" if issue.line else issue.path
    return f"{severity} {location}: {issue.message}"


def _is_changed(path: str, playbook_paths: list[str], changed_paths: list[str]) -> bool:
    """Whether the merge request changes the playbook of a path, or the path itself."""
    playbook = next((x for x in playbook_paths if _is_in(path, x)), path)
    return any(_is_in(x, playbook) for x in changed_paths)


def _is_tool_file(path: str) -> bool:
    """Whether the file belongs to the sync tool or the pipeline that checks it."""
    # Integrations holds the code of the pulled integrations, not of this tool
    is_code = path.endswith(".py") and not path.startswith(
        ("tests/", f"{INTEGRATIONS_PATH}/")
    )
    return is_code or path in ("requirements.txt", ".gitlab-ci.yml")


# Small helpers


def _quote(name: object) -> str:
    return f"'{name}'"


def _short_json(value: object) -> str | None:
    """Returns the value as JSON, or None if it's too long to show."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= _MAX_VALUE_LENGTH else None


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _is_in(path: str, folder: str) -> bool:
    return path == folder or path.startswith(f"{folder}/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prints a report of what a merge request changes "
        "and what the validation finds."
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        help="Branch or commit the merge request is merged into (default: origin/main)",
    )
    args = parser.parse_args()
    try:
        repo_root = _git(".", "rev-parse", "--show-toplevel").decode().strip()
        print(build_report(repo_root, args.base))
    except ReportError as e:
        print(f"Can't build the report: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
