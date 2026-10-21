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

"""Prints a Markdown report of what a merge request changes and what the validation finds.

    python validation_report.py --base origin/main

Compares the committed HEAD with its merge base with --base: the playbooks the
merge request adds, deletes, renames or changes (down to steps, parameters,
connections and the trigger), the other files it changes, and the validation
findings it adds, fixes or leaves as they are.

The script only reports. It exits with 0 even if there are errors, since the
validate-playbooks command is the check that fails on them.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

import playbook_format
from config import INTEGRATIONS_PATH, PLAYBOOK_WIDGETS_DIR, PLAYBOOKS_PATH
from utils import iter_playbook_paths, read_playbook_files
from validation import ERROR, Issue, Report, check_playbooks

# Parts of a playbook compared on their own, instead of as definition fields
_PARTS = {"steps", "stepsRelations", "trigger", "overviewTemplates"}
# Longer values are only reported as changed, without the values
_MAX_VALUE_LENGTH = 80
# Most changes listed for one playbook
_MAX_CHANGES = 40


class ReportError(Exception):
    """Raised when git can't provide the changes to report."""


@dataclass(eq=False)
class _Playbook:
    # Folder, or file of a legacy playbook, relative to the repo root
    path: str
    raw_data: dict
    # Widget HTML files of the folder by path, empty for legacy playbooks
    widgets: dict[str, str]

    @property
    def name(self) -> str:
        return str(self.raw_data.get("name") or self.path)


@dataclass
class _ChangedFile:
    status: str
    path: str
    old_path: str | None
    lines_added: int | None
    lines_deleted: int | None


def build_report(repo_root: str, base: str) -> str:
    """Builds the report of the changes from the merge base with base to HEAD."""
    merge_base = _merge_base(repo_root, base)
    head = _git(repo_root, "rev-parse", "HEAD").decode().strip()
    changed_files = _changed_files(repo_root, merge_base)

    with tempfile.TemporaryDirectory() as tmp:
        base_root, head_root = os.path.join(tmp, "base"), os.path.join(tmp, "head")
        _export_playbooks(repo_root, merge_base, base_root)
        _export_playbooks(repo_root, head, head_root)
        base_playbooks = _load_playbooks(base_root)
        head_playbooks = _load_playbooks(head_root)
        base_findings = check_playbooks(base_root)
        head_findings = check_playbooks(head_root)

    if merge_base.startswith(base):
        compared = f"its merge base `{merge_base[:7]}`"
    else:
        compared = f"`{base}` (merge base `{merge_base[:7]}`)"
    lines = [
        "# Validation report",
        "",
        f"Compares `HEAD` (`{head[:7]}`) with {compared}.",
        "",
        _result_line(base_findings, head_findings),
        "",
    ]
    attention: list[str] = []
    playbook_lines = _playbook_changes(
        base_playbooks, head_playbooks, changed_files, attention
    )
    other_files = [
        x
        for x in changed_files
        if not any(
            _contains(p.path, path)
            for p in base_playbooks + head_playbooks
            for path in (x.path, x.old_path)
            if path
        )
    ]
    attention += _file_attention(changed_files)

    if attention:
        lines += ["## Needs attention", ""] + [f"- {x}" for x in attention] + [""]
    lines += ["## Playbook changes", ""] + playbook_lines
    lines += ["## Findings", ""] + _findings(base_findings, head_findings)
    lines += ["## Other changed files", ""] + _file_table(other_files)
    return "\n".join(lines)


def _git(repo_root: str, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", repo_root, *args], capture_output=True)
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip()
        raise ReportError(f"git {' '.join(args)} failed: {error}")
    return result.stdout


def _merge_base(repo_root: str, base: str) -> str:
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
            "history, for example actions/checkout with fetch-depth: 0"
        ) from None


def _changed_files(repo_root: str, merge_base: str) -> list[_ChangedFile]:
    """Lists the files changed from merge_base to HEAD, with renames detected."""
    tokens = _git(repo_root, "diff", "--name-status", "-M", "-z", merge_base, "HEAD")
    tokens = tokens.decode(errors="replace").split("\0")
    files: list[_ChangedFile] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i]
        if status[0] in "RC":
            files.append(_ChangedFile(status[0], tokens[i + 2], tokens[i + 1], None, None))
            i += 3
        else:
            files.append(_ChangedFile(status[0], tokens[i + 1], None, None, None))
            i += 2

    # Binary files have "-" instead of line counts
    tokens = _git(repo_root, "diff", "--numstat", "-M", "-z", merge_base, "HEAD")
    tokens = tokens.decode(errors="replace").split("\0")
    counts: dict[str, tuple[int | None, int | None]] = {}
    i = 0
    while i < len(tokens) and tokens[i]:
        added, deleted, path = tokens[i].split("\t", 2)
        if path:
            i += 1
        else:
            # Renamed: the old and the new path follow
            path = tokens[i + 2]
            i += 3
        counts[path] = (
            int(added) if added.isdigit() else None,
            int(deleted) if deleted.isdigit() else None,
        )
    for file in files:
        file.lines_added, file.lines_deleted = counts.get(file.path, (None, None))
    return files


def _export_playbooks(repo_root: str, revision: str, dest: str) -> None:
    """Writes the playbooks folder of a commit to dest."""
    os.makedirs(dest)
    if not _git(repo_root, "ls-tree", "--name-only", revision, PLAYBOOKS_PATH).strip():
        return
    archive = _git(repo_root, "archive", "--format=tar", revision, PLAYBOOKS_PATH)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def _load_playbooks(repo_root: str) -> list[_Playbook]:
    """Reads the playbooks of a repo. Unreadable ones are left to the findings."""
    root = os.path.join(repo_root, PLAYBOOKS_PATH)
    if not os.path.isdir(root):
        return []
    playbooks = []
    for path in iter_playbook_paths(root):
        try:
            if os.path.isdir(path):
                files = read_playbook_files(path)
                raw_data = playbook_format.from_files(files)
            else:
                files = {}
                with open(path, encoding="utf-8") as f:
                    raw_data = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(raw_data, dict):
            widgets = {
                k: v for k, v in files.items() if k.startswith(f"{PLAYBOOK_WIDGETS_DIR}/")
            }
            relative = os.path.relpath(path, repo_root).replace(os.sep, "/")
            playbooks.append(_Playbook(relative, raw_data, widgets))
    return playbooks


def _contains(playbook_path: str, path: str) -> bool:
    return path == playbook_path or path.startswith(f"{playbook_path}/")


def _result_line(base_findings: Report, head_findings: Report) -> str:
    errors, warnings = head_findings.errors, head_findings.warnings
    checked = f"Checked {head_findings.playbook_count} playbook(s)"
    if not errors:
        return f"**Result:** ✅ No errors, {len(warnings)} warning(s). {checked}."
    base_keys = {_finding_key(x) for x in base_findings.errors}
    new = [x for x in errors if _finding_key(x) not in base_keys]
    return (
        f"**Result:** ❌ {len(errors)} error(s), {len(new)} of them new in this "
        f"merge request, and {len(warnings)} warning(s). {checked}."
    )


# Playbook changes


def _playbook_changes(
    base: list[_Playbook],
    head: list[_Playbook],
    changed_files: list[_ChangedFile],
    attention: list[str],
) -> list[str]:
    """Lists the added, deleted, renamed and changed playbooks.

    Adds the changes reviewers should look at first to attention.
    """
    pairs, added, deleted = _match_playbooks(base, head)
    rows: list[str] = []
    sections: list[str] = []

    for playbook in added:
        steps = _flatten_steps(playbook.raw_data.get("steps"))
        state = _enabled_state(playbook.raw_data)
        rows.append(_row("Added", playbook, f"{len(steps)} step(s){state}"))
        if playbook.raw_data.get("isEnabled") is True:
            attention.append(f"{_bold(playbook.name)} is added and enabled")

    for old, new in pairs:
        changes = _changes(old, new, attention)
        if changes:
            change = "Renamed" if old.name != new.name else "Changed"
            details = f"from {_bold(old.name)}" if change == "Renamed" else ""
            rows.append(_row(change, new, details or f"{len(changes)} change(s)"))
            if len(changes) > _MAX_CHANGES:
                changes = changes[:_MAX_CHANGES] + [
                    f"… and {len(changes) - _MAX_CHANGES} more changes"
                ]
            sections += [f"### {_md(new.name)}", ""] + [f"- {x}" for x in changes] + [""]
        elif any(
            _contains(p.path, x.path) or (x.old_path and _contains(p.path, x.old_path))
            for p in (old, new)
            for x in changed_files
        ):
            rows.append(
                _row(
                    "No content change",
                    new,
                    "Only the README, formatting, file names or timestamps changed",
                )
            )

    for playbook in deleted:
        rows.append(_row("Deleted", playbook, ""))
        attention.append(
            f"{_bold(playbook.name)} is deleted. sync-playbooks doesn't delete "
            "playbooks, so it stays in SecOps and the next pull-playbooks adds it "
            "back. Delete it in SecOps as well"
        )

    if not rows:
        return ["No playbook changes.", ""]
    table = ["| Change | Playbook | Folder | Details |", "|---|---|---|---|"] + rows
    return table + [""] + sections


def _match_playbooks(
    base: list[_Playbook], head: list[_Playbook]
) -> tuple[list[tuple[_Playbook, _Playbook]], list[_Playbook], list[_Playbook]]:
    """Pairs each playbook with its base version.

    Playbooks are matched by name, then by identifier, which a renamed playbook
    keeps. A copied folder keeps the identifier too, but its original is
    matched by name first.

    Returns:
        The pairs of base and head playbook, the added and the deleted playbooks.
    """
    unmatched = list(base)
    pairs, rest, added = [], [], []
    for playbook in head:
        old = next((x for x in unmatched if x.name == playbook.name), None)
        if old:
            unmatched.remove(old)
            pairs.append((old, playbook))
        else:
            rest.append(playbook)
    for playbook in rest:
        identifier = playbook.raw_data.get("identifier")
        old = next(
            (
                x
                for x in unmatched
                if identifier and x.raw_data.get("identifier") == identifier
            ),
            None,
        )
        if old:
            unmatched.remove(old)
            pairs.append((old, playbook))
        else:
            added.append(playbook)
    return pairs, added, unmatched


def _changes(old: _Playbook, new: _Playbook, attention: list[str]) -> list[str]:
    """Lists the changes of a playbook and adds the important ones to attention."""
    changes = []
    name = _bold(new.name)
    if old.name != new.name:
        changes.append(f"Renamed from {_bold(old.name)}")
        attention.append(f"{_bold(old.name)} is renamed to {name}")
    if old.path != new.path:
        changes.append(f"Moved from {_code(old.path)} to {_code(new.path)}")

    old_data, new_data = old.raw_data, new.raw_data
    changes += _field_changes(old_data, new_data, ignore=_PARTS | {"name", "id"})
    if _clean(old_data.get("isEnabled")) != _clean(new_data.get("isEnabled")):
        state = "enabled" if new_data.get("isEnabled") else "disabled"
        attention.append(f"{name} is now {state}")
    if _clean(old_data.get("environments")) != _clean(new_data.get("environments")):
        old_environments = _value(old_data.get("environments"))
        new_environments = _value(new_data.get("environments"))
        attention.append(
            f"The environments of {name} changed: {old_environments} → {new_environments}"
        )

    old_trigger, new_trigger = old_data.get("trigger"), new_data.get("trigger")
    if _clean(old_trigger) != _clean(new_trigger):
        attention.append(f"The trigger of {name} changed")
        if isinstance(old_trigger, dict) and isinstance(new_trigger, dict):
            changes += [f"Trigger {x}" for x in _trigger_changes(old_trigger, new_trigger)]
        else:
            changes.append("Trigger changed")

    old_steps = _flatten_steps(old_data.get("steps"))
    new_steps = _flatten_steps(new_data.get("steps"))
    step_changes = _step_changes(old_steps, new_steps)
    removed = sum(1 for x in step_changes if x.startswith("Step removed"))
    if removed:
        attention.append(f"{removed} step(s) removed from {name}")
    changes += step_changes

    step_names = {
        x["identifier"]: x.get("instanceName")
        for x in old_steps + new_steps
        if isinstance(x.get("identifier"), str)
    }
    changes += _relation_changes(old_data, new_data, step_names)
    changes += _overview_changes(old_data, new_data)
    for path in sorted(old.widgets.keys() | new.widgets.keys()):
        if path not in old.widgets:
            changes.append(f"Widget HTML added: {_code(path)}")
        elif path not in new.widgets:
            changes.append(f"Widget HTML removed: {_code(path)}")
        elif old.widgets[path] != new.widgets[path]:
            changes.append(f"Widget HTML changed: {_code(path)}")
    return changes


def _trigger_changes(old: dict, new: dict) -> list[str]:
    changes = _field_changes(old, new, ignore={"id", "conditions"})
    old_conditions = [_condition(x) for x in old.get("conditions") or []]
    new_conditions = [_condition(x) for x in new.get("conditions") or []]
    changes += [f"condition added: {x}" for x in new_conditions if x not in old_conditions]
    changes += [
        f"condition removed: {x}" for x in old_conditions if x not in new_conditions
    ]
    return changes


def _condition(condition: object) -> str:
    if not isinstance(condition, dict):
        return _value(condition)
    parts = [condition.get("fieldName"), condition.get("matchType"), condition.get("value")]
    return _value(" ".join(str(x) for x in parts if x is not None))


def _flatten_steps(steps: object) -> list[dict]:
    """Lists the steps with their parallel actions, like sync-playbooks does."""
    flat = []
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        flat.append(step)
        children = step.get("parallelActions")
        if isinstance(children, list):
            flat += [x for x in children if isinstance(x, dict)]
    return flat


def _step_changes(old_steps: list[dict], new_steps: list[dict]) -> list[str]:
    def by_key(steps: list[dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for step in steps:
            result.setdefault(str(step.get("identifier") or step.get("instanceName")), step)
        return result

    old, new = by_key(old_steps), by_key(new_steps)
    changes = [f"Step added: {_step_label(new[x])}" for x in new if x not in old]
    changes += [f"Step removed: {_step_label(old[x])}" for x in old if x not in new]
    for key in (x for x in new if x in old):
        step_changes = _field_changes(
            old[key], new[key], ignore={"parameters", "parallelActions"}
        ) + _parameter_changes(old[key], new[key])
        if step_changes:
            name = new[key].get("instanceName") or key
            changes.append(f"Step {_bold(name)} changed: {'; '.join(step_changes)}")
    return changes


def _step_label(step: dict) -> str:
    label = _bold(step.get("instanceName") or step.get("identifier"))
    action = " / ".join(
        str(x) for x in (step.get("integration"), step.get("actionName")) if x
    )
    return f"{label} ({_md(action)})" if action else label


def _parameter_changes(old_step: dict, new_step: dict) -> list[str]:
    def by_name(step: dict) -> dict[str, dict]:
        parameters = step.get("parameters")
        if not isinstance(parameters, list):
            return {}
        return {str(x.get("name")): x for x in parameters if isinstance(x, dict)}

    old, new = by_name(old_step), by_name(new_step)
    changes = []
    for name in sorted(old.keys() | new.keys()):
        if name not in new:
            changes.append(f"parameter `{name}` removed")
        elif name not in old:
            changes.append(f"parameter `{name}` added: {_value(new[name].get('value'))}")
        elif _clean(old[name].get("value")) != _clean(new[name].get("value")):
            change = _change(name, old[name].get("value"), new[name].get("value"))
            changes.append(f"parameter {change}")
        elif _clean(old[name]) != _clean(new[name]):
            changes.append(f"parameter `{name}` changed")
    return changes


def _relation_changes(old_data: dict, new_data: dict, step_names: dict) -> list[str]:
    def by_steps(data: dict) -> dict[tuple[str, str], dict]:
        relations = data.get("stepsRelations")
        result: dict[tuple[str, str], dict] = {}
        for relation in relations if isinstance(relations, list) else []:
            if isinstance(relation, dict):
                key = (str(relation.get("fromStep")), str(relation.get("toStep")))
                result.setdefault(key, relation)
        return result

    def label(key: tuple[str, str]) -> str:
        return " → ".join(_bold(step_names.get(x) or x) for x in key)

    old, new = by_steps(old_data), by_steps(new_data)
    changes = [f"Connection added: {label(x)}" for x in sorted(new.keys() - old.keys())]
    changes += [f"Connection removed: {label(x)}" for x in sorted(old.keys() - new.keys())]
    for key in sorted(old.keys() & new.keys()):
        relation_changes = _field_changes(old[key], new[key], ignore={"fromStep", "toStep"})
        if relation_changes:
            changes.append(
                f"Connection {label(key)} changed: {'; '.join(relation_changes)}"
            )
    return changes


def _overview_changes(old_data: dict, new_data: dict) -> list[str]:
    def by_key(data: dict) -> dict[str, dict]:
        templates = data.get("overviewTemplates")
        return {
            str(x.get("identifier") or x.get("name") or i): x
            for i, x in enumerate(templates if isinstance(templates, list) else [])
            if isinstance(x, dict)
        }

    old, new = by_key(old_data), by_key(new_data)
    changes = []
    for key in new.keys() | old.keys():
        template = new[key] if key in new else old[key]
        name = _bold(template.get("name") or key)
        if key not in old:
            changes.append(f"Overview template added: {name}")
        elif key not in new:
            changes.append(f"Overview template removed: {name}")
        elif template_changes := _field_changes(old[key], new[key]):
            changes.append(
                f"Overview template {name} changed: {'; '.join(template_changes)}"
            )
    return sorted(changes)


def _field_changes(
    old: dict, new: dict, ignore: AbstractSet[str] = frozenset()
) -> list[str]:
    changes = []
    for key in sorted(old.keys() | new.keys()):
        if key in ignore or _is_timestamp(key):
            continue
        if key not in new:
            changes.append(f"`{key}` removed")
        elif key not in old:
            changes.append(f"`{key}` added: {_value(new[key])}")
        elif _clean(old[key]) != _clean(new[key]):
            changes.append(_change(key, old[key], new[key]))
    return changes


def _change(key: str, old: object, new: object) -> str:
    old_text, new_text = _json(old), _json(new)
    if max(len(old_text), len(new_text)) > _MAX_VALUE_LENGTH:
        return f"`{key}` changed"
    return f"`{key}`: {_value(old)} → {_value(new)}"


def _is_timestamp(key: object) -> bool:
    return isinstance(key, str) and key.endswith("UnixTimeInMs")


def _clean(value: object) -> object:
    """Removes the timestamps the platform sets, which aren't changes to review."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if not _is_timestamp(k)}
    if isinstance(value, list):
        return [_clean(x) for x in value]
    return value


def _enabled_state(raw_data: dict) -> str:
    enabled = raw_data.get("isEnabled")
    return ", enabled" if enabled is True else ", disabled" if enabled is False else ""


def _row(change: str, playbook: _Playbook, details: str) -> str:
    return f"| {change} | {_bold(playbook.name)} | {_code(playbook.path)} | {details} |"


# Findings and other files


def _finding_key(issue: Issue) -> tuple[str, str, str]:
    # Lines move when files change, so they aren't part of the key
    return issue.severity, issue.path, issue.message


def _findings(base_findings: Report, head_findings: Report) -> list[str]:
    base_keys = {_finding_key(x) for x in base_findings.issues}
    head_keys = {_finding_key(x) for x in head_findings.issues}
    head_issues, base_issues = head_findings.issues, base_findings.issues
    groups = [
        (
            "New in this merge request",
            [x for x in head_issues if _finding_key(x) not in base_keys],
        ),
        (
            "Already on the base branch",
            [x for x in head_issues if _finding_key(x) in base_keys],
        ),
        (
            "Fixed by this merge request",
            [x for x in base_issues if _finding_key(x) not in head_keys],
        ),
    ]
    lines = []
    for title, issues in groups:
        if issues:
            lines += [f"### {title} ({len(issues)})", ""]
            lines += [_finding(x) for x in issues] + [""]
    return lines or ["No findings.", ""]


def _finding(issue: Issue) -> str:
    icon = "❌ Error" if issue.severity == ERROR else "⚠️ Warning"
    location = f"{issue.path}:{issue.line}" if issue.line else issue.path
    return f"- {icon} {_code(location)}: {_md(issue.message)}"


def _file_attention(changed_files: list[_ChangedFile]) -> list[str]:
    tool = [
        x.path
        for x in changed_files
        if (
            x.path.endswith(".py")
            and not x.path.startswith(("tests/", f"{INTEGRATIONS_PATH}/"))
        )
        or x.path == "requirements.txt"
    ]
    checks = [x.path for x in changed_files if x.path.startswith(".github/")]
    attention = []
    if tool:
        attention.append(
            "The sync tool's code changed: " + ", ".join(_code(x) for x in tool)
        )
    if checks:
        attention.append(
            "The GitHub configuration, which runs these checks, changed: "
            + ", ".join(_code(x) for x in checks)
        )
    return attention


def _file_table(files: list[_ChangedFile]) -> list[str]:
    if not files:
        return ["None.", ""]
    statuses = {
        "A": "Added",
        "M": "Modified",
        "D": "Deleted",
        "R": "Renamed",
        "C": "Copied",
    }
    lines = ["| File | Change | Lines |", "|---|---|---|"]
    for file in files:
        path = _code(file.path)
        if file.old_path:
            path = f"{_code(file.old_path)} → {path}"
        if file.lines_added is None:
            counts = "binary"
        else:
            counts = f"+{file.lines_added} −{file.lines_deleted}"
        lines.append(f"| {path} | {statuses.get(file.status, file.status)} | {counts} |")
    return lines + [""]


# Markdown


def _md(text: object) -> str:
    """Escapes text for Markdown."""
    return re.sub(r"([\\`*_\[\]<>|])", r"\\\1", str(text))


def _code(text: str) -> str:
    # A pipe ends a table cell even in code
    return "`" + text.replace("`", "'").replace("|", "\\|") + "`"


def _bold(text: object) -> str:
    return f"**{_md(text)}**"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _value(value: object) -> str:
    text = _json(value)
    if len(text) > _MAX_VALUE_LENGTH:
        text = text[: _MAX_VALUE_LENGTH - 1] + "…"
    return "`" + text.replace("`", "'") + "`"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prints a Markdown report of what a merge request changes "
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
        report = build_report(repo_root, args.base)
    except ReportError as e:
        print(f"Can't build the report: {e}", file=sys.stderr)
        sys.exit(2)
    print(report)


if __name__ == "__main__":
    main()
