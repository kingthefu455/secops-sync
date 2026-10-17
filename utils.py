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

import json
import logging
import os
import shutil
from collections.abc import Iterator

import playbook_format
from config import  PLAYBOOK_DEFINITION_FILE,PLAYBOOK_STEPS_DIR, PLAYBOOKS_PATH
from models import File, Workflow

LOGGER = logging.getLogger("rac")
WD = os.path.abspath(".")


def get_local_playbook(playbook_name: str) -> Workflow | None:
    """Reads a playbook or block from the repo object store"""
    return next((p for p in get_local_playbooks() if p.name == playbook_name), None)


def get_local_playbooks() -> Iterator[Workflow]:
    """Reads all playbooks and blocks from the repo.

    A folder containing PLAYBOOK_DEFINITION_FILE is a playbook in the content-hub
    layout. Any other JSON file is read as a legacy single-file playbook, so repos
    can be migrated by simply pulling the playbooks again.

    Raises:
        ValueError: If two playbooks in the repo have the same name.
    """
    root = os.path.join(WD, PLAYBOOKS_PATH)
    if not os.path.isdir(root):
        LOGGER.warning(f"Playbooks path not found: {root}")
        return

    seen: dict[str, str] = {}
    for dir_path, dir_names, file_names in os.walk(root):
        dir_names.sort()
        if PLAYBOOK_DEFINITION_FILE in file_names:
            # The steps folder belongs to this playbook, don't descend into it
            dir_names.clear()
            playbooks = [(dir_path, _read_playbook_dir(dir_path))]
        else:
            playbooks = [
                (path, Workflow(_read_json(path)))
                for path in (os.path.join(dir_path, f) for f in sorted(file_names))
                if path.endswith(".json")
            ]

        for path, playbook in playbooks:
            if playbook.name in seen:
                raise ValueError(
                    f"Playbook '{playbook.name}' is stored twice: "
                    f"{seen[playbook.name]} and {path}"
                )
            seen[playbook.name] = path
            yield playbook


def _read_playbook_dir(playbook_dir: str) -> Workflow:
    """Reassembles a playbook from its definition, trigger, overview and step files."""
    files = {
        file_name: _read_json(os.path.join(playbook_dir, file_name))
        for file_name in os.listdir(playbook_dir)
        if file_name.endswith(".json")
    }
    steps_dir = os.path.join(playbook_dir, PLAYBOOK_STEPS_DIR)
    if os.path.isdir(steps_dir):
        for file_name in os.listdir(steps_dir):
            if file_name.endswith(".json"):
                path = f"{PLAYBOOK_STEPS_DIR}/{file_name}"
                files[path] = _read_json(os.path.join(steps_dir, file_name))
    return Workflow(playbook_format.from_files(files))


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def push_playbook(playbook: Workflow) -> None:
    """Writes a workflow to the repo in the content-hub layout.

    Raises:
        ValueError: If the playbook folder already holds another playbook, which
            happens when two playbook names map to the same folder name.
    """
    category_path = f"{PLAYBOOKS_PATH}/{playbook.category}"
    path = f"{category_path}/{playbook.folder_name}"

    definition_path = os.path.join(WD, path, PLAYBOOK_DEFINITION_FILE)
    if os.path.isfile(definition_path):
        stored_name = _read_json(definition_path).get("name")
        if stored_name != playbook.name:
            raise ValueError(
                f"Can't store playbook '{playbook.name}' in {path}: "
                f"the folder already holds playbook '{stored_name}'"
            )

    playbook.generate_readme()
    # Replace the whole folder so files of removed steps don't linger
    update_objects(
        playbook.iter_files(), base_path=path, replace_content_in_base_path=True
    )

def update_objects(
    files: list[File], base_path: str = "", replace_content_in_base_path: bool = False
) -> None:
    """
    Creates or updates specified files in the working directory, with an option
    to replace all content within a specified base_path.

    Behavior regarding existing content in `base_path` is controlled by
    the `replace_content_in_base_path` flag.

    - If `base_path` is provided (is a non-empty string) AND `replace_content_in_base_path` is True:
      The contents of the subdirectory specified by `base_path` (relative to
      `self.wd`) will be ENTIRELY REPLACED by the provided `files`. Existing items
      (files and subdirectories) in that `base_path` subdirectory not present
      in the `files` list (implicitly, as new content) will be deleted.

    - If `base_path` is provided AND `replace_content_in_base_path` is False:
      Files from the `files` list are created or updated within the `base_path`
      subdirectory. Existing items in that subdirectory not affected by the
      `files` list are unaffected (not deleted).

    - If `base_path` is empty (or not provided):
      Files are created or updated directly within the main working directory (`self.wd`).
      The `replace_content_in_base_path` flag has NO EFFECT in this case;
      existing items in `self.wd` not in the `files` list are always unaffected.

    In all cases, the `path` attribute of each `File` object in the `files` list
    is treated as relative to the `effective_target_dir` (which is `self.wd` if
    `base_path` is empty, or `os.path.join(self.wd, base_path)` if `base_path` is provided).

    Args:
        files (list[File]): A list of File objects to write.
                            Each File object must have a 'path' (str, relative)
                            and 'content' (bytes) attribute.
        base_path (str, optional): Relative path to a subdirectory within the
                                    working directory. Defaults to "".
        replace_content_in_base_path (bool, optional): If True and `base_path` is provided,
                                    the content of the `base_path` subdirectory
                                    is fully replaced. Defaults to False.
    """
    effective_target_dir = os.path.join(WD, base_path)

    if base_path:  # True if base_path is not an empty string
        if replace_content_in_base_path:
            LOGGER.info(
                f"Mode: Replace content. Target directory: {effective_target_dir}"
            )

            if os.path.exists(effective_target_dir):
                if not os.path.isdir(effective_target_dir):
                    LOGGER.error(
                        f"Path {effective_target_dir} exists but is not a directory. Cannot replace content."
                    )
                    raise NotADirectoryError(
                        f"Target path for replacement '{effective_target_dir}' is not a directory."
                    )
                try:
                    LOGGER.debug(
                        f"Clearing directory for replacement: {effective_target_dir}"
                    )
                    for item_name in os.listdir(effective_target_dir):
                        item_path = os.path.join(effective_target_dir, item_name)
                        if os.path.isfile(item_path) or os.path.islink(item_path):
                            os.unlink(item_path)
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)  # shutil is needed here
                    LOGGER.debug(
                        f"Successfully cleared directory: {effective_target_dir}"
                    )
                except Exception as e:
                    LOGGER.error(
                        f"Failed to clear directory {effective_target_dir} for replacement: {e}"
                    )
                    raise

            # Ensure the directory exists (it might have been just cleared or never existed)
            try:
                os.makedirs(effective_target_dir, exist_ok=True)
                LOGGER.debug(
                    f"Ensured directory exists for writing new content: {effective_target_dir}"
                )
            except OSError as e:
                LOGGER.error(
                    f"Failed to create directory {effective_target_dir} after clearing/for replacement: {e}"
                )
                raise
        else:  # base_path is provided, but replace_content_in_base_path is False
            LOGGER.info(
                f"Mode: Update/Create within base_path (no replacement). Target directory: {effective_target_dir}"
            )
            try:
                os.makedirs(
                    effective_target_dir, exist_ok=True
                )  # Ensure base_path dir exists
                LOGGER.debug(f"Ensured base directory exists: {effective_target_dir}")
            except OSError as e:
                LOGGER.error(
                    f"Failed to create base directory {effective_target_dir}: {e}"
                )
                raise
    else:  # base_path is empty
        LOGGER.info(
            f"Mode: Update/Create in working directory (no replacement). Target directory: {WD}"
        )

    # --- Common file writing loop ---
    for file_obj in files:
        if not hasattr(file_obj, "path") or not hasattr(file_obj, "content"):
            LOGGER.warning(
                f"Skipping object due to missing 'path' or 'content' attributes: {file_obj}"
            )
            continue
        if not isinstance(file_obj.path, str) or not isinstance(
            file_obj.content, bytes
        ):
            LOGGER.warning(
                f"Skipping object due to incorrect type for 'path' (str expected) or 'content' (bytes expected): path='{file_obj.path}'"
            )
            continue
        if not file_obj.path:  # Path should not be empty
            LOGGER.warning(f"Skipping object with empty path: {file_obj}")
            continue

        # file_obj.path is relative to effective_target_dir.
        # Normalize to handle mixed slashes (e.g., `foo/bar` vs `foo\\bar`), ".." etc.
        relative_path_in_file_obj = os.path.normpath(file_obj.path)

        # Security check: ensure the normalized path is truly relative and does not try to escape.
        if (
            os.path.isabs(relative_path_in_file_obj)
            or relative_path_in_file_obj.startswith("..")
            or (os.path.sep + ".." in relative_path_in_file_obj)
            or (
                ".." + os.path.sep in relative_path_in_file_obj
                and relative_path_in_file_obj.index(".." + os.path.sep) == 0
            )
        ):  # Catches "../" at start on unix
            LOGGER.error(
                f"Security risk: Invalid file path '{file_obj.path}' (normalized: '{relative_path_in_file_obj}'). "
                "Path must be relative and confined to the target directory."
            )
            continue

        full_file_path = os.path.join(effective_target_dir, relative_path_in_file_obj)

        try:
            # Ensure the parent directory for this specific file exists.
            parent_directory = os.path.dirname(full_file_path)

            # Only call makedirs if parent_directory is not empty and exists
            if (
                parent_directory
            ):  # An empty parent_directory means file is in the current dir
                os.makedirs(parent_directory, exist_ok=True)

            # Write the file (creates if new, overwrites if existing).
            with open(full_file_path, "wb") as f:
                f.write(file_obj.content)
            LOGGER.debug(f"Successfully wrote (created/updated) file: {full_file_path}")

        except OSError as e:
            LOGGER.error(f"OSError related to path {full_file_path}: {e}")
        except Exception as e:  # noqa: BLE001
            LOGGER.error(f"Unexpected error writing file {full_file_path}: {e}")

    LOGGER.info(f"Finished all file operations for: {effective_target_dir}")


def get_file_objects_from_path(dir_path: str = "") -> list[File]:
    """
    Gets a list of File objects from a specified path relative to the working directory.
    If dir_path points to a directory, it recursively finds all files within it.
    If dir_path points to a file, it returns a list containing a single File object for that file.
    Paths in the returned File objects are relative to the working directory (self.wd).

    Args:
        dir_path (str, optional): Path relative to the working directory.
                                  Defaults to "" (representing the working directory itself).

    Returns:
        list[File]: A list of File objects.

    Raises:
        FileNotFoundError: If the initial dir_path (resolved to an absolute path) does not exist.
    """
    search_root_abs = os.path.normpath(os.path.join(WD, dir_path))

    if not os.path.exists(search_root_abs):
        LOGGER.warning(f"Path not found: {search_root_abs}")
        raise FileNotFoundError(f"Path not found: {search_root_abs}")

    files_found = []

    if os.path.isfile(search_root_abs):
        try:
            with open(search_root_abs, "rb") as f:
                content = f.read()
            relative_path = os.path.relpath(search_root_abs, WD)
            files_found.append(File(path=relative_path, content=content))
            LOGGER.debug(f"Retrieved file: {relative_path}")
        except OSError as e:
            LOGGER.error(f"Failed to read file {search_root_abs}: {e}")
    elif os.path.isdir(search_root_abs):
        LOGGER.debug(f"Searching for files in directory: {search_root_abs}")
        for root, _, filenames in os.walk(search_root_abs):
            for filename in filenames:
                file_abs_path = os.path.join(root, filename)
                # Ensure we are not picking up directories that might be named like files in some OS walk implementations
                if not os.path.isfile(file_abs_path):
                    continue
                try:
                    with open(file_abs_path, "rb") as f:
                        content = f.read()
                    relative_path = os.path.relpath(file_abs_path, WD)
                    files_found.append(File(path=relative_path, contents=content))
                    LOGGER.debug(f"Retrieved file from directory walk: {relative_path}")
                except OSError as e:
                    LOGGER.error(f"Failed to read file {file_abs_path}: {e}")

    return files_found


# def generate_pr_comment_output(plan: dict, submitted_info: list,
#                                has_errors: bool):
#     """Generates a structured JSON output for a GitHub PR comment."""
#     LOGGER.info("\n--- Generating output for PR comment ---")
#     submitted_map = {info['log_type']: info for info in submitted_info}
#     report_lines = ["\n"]

#     for log_type, details in sorted(plan.items()):
#         if (details.parser_operation == Operation.NONE
#                 and details.parser_ext_operation == Operation.NONE
#                 and not details.validation_failed):
#             continue

#         line_parts = [f"- **Log Type**: `{log_type}`"]

#         # --- Parser Details ---
#         if details.config.parser:
#             action = details.parser_operation
#             line_parts.append(f"  - **Parser Action**: `{action.value}`")

#             if details.validation_failed:
#                 status_text, icon = "EVENT_VALIDATION_FAILED", "❌"
#                 details_text = "Not submitted due to local event validation failure."
#             elif action in [
#                     Operation.CREATE, Operation.UPDATE, Operation.RELEASE
#             ]:
#                 if action == Operation.RELEASE:
#                     status_text = "READY_TO_RELEASE"
#                     details_text = "Pending Release Candidate matched. Ready for activation key."
#                     icon = "🚀"
#                 else:
#                     status = details.parser_validation_status or "PENDING"
#                     icon = "✅" if status == ParserValidationStatus.PASSED.value else "❌" if status == ParserValidationStatus.FAILED.value else "⏳"
#                     status_text = f"{status} {icon}"
#                     parser_id = submitted_map.get(log_type,
#                                                   {}).get('parser_id', 'N/A')
#                     details_text = f"Submitted for deployment. Parser ID: `{parser_id}`"
#             else:  # Operation is NONE, but we're here because something else happened (e.g., ext change)
#                 status_text = "NO_CHANGE"
#                 details_text = "No changes detected for the parser."

#             line_parts.append(f"  - **Validation Status**: {status_text}")
#             line_parts.append(f"  - **Details**: {details_text}")

#         # --- Parser Extension Details ---
#         if details.config.parser_ext:
#             action = details.parser_ext_operation
#             line_parts.append(
#                 f"  - **Parser Extension Action**: `{action.value}`")

#             if details.validation_failed:
#                 status_text, icon = "EVENT_VALIDATION_FAILED", "❌"
#                 details_text = "Not submitted due to local event validation failure."
#             elif action in [Operation.CREATE, Operation.UPDATE]:
#                 status = details.parser_ext_validation_status or "PENDING"
#                 icon = "✅" if status == ParserExtensionState.VALIDATED.value else "❌" if status == ParserExtensionState.REJECTED.value else "⏳"
#                 status_text = f"{status} {icon}"
#                 ext_id = submitted_map.get(log_type,
#                                            {}).get('parser_ext_id', 'N/A')
#                 details_text = f"Submitted for deployment. Extension ID: `{ext_id}`"
#             else:  # Operation is NONE
#                 status_text = "NO_CHANGE"
#                 details_text = "No changes detected for the parser extension."

#             line_parts.append(f"  - **Validation Status**: {status_text}")
#             line_parts.append(f"  - **Details**: {details_text}")

#         # --- Comparison Report ---
#         if details.comparison_report:
#             report = details.comparison_report
#             line_parts.append(
#                 f"\n<details><summary><b>📉 UDM Comparison Report</b></summary>\n\n```text\n{report}\n```\n</details>"
#             )

#         report_lines.append("\n".join(line_parts))

#     body = "\n\n".join(report_lines)

#     if has_errors:
#         title = "❌ Parser Deployment Failed"
#         summary = "Errors were encountered during the process. See action logs for details."
#     elif not submitted_info and not report_lines:
#         title = "✅ All Parsers Up-to-Date"
#         summary = "No changes were needed for any parsers or extensions."
#         body = "All configurations in the repository are in sync with the active versions in Chronicle."
#     else:
#         title = "✅ Parser Deployment Plan"
#         summary = f"{len(submitted_info)} log type(s) had changes submitted. Review validation status below."

#     comment_data = {"title": title, "summary": summary, "details": body}

#     if GITHUB_OUTPUT_FILE:
#         LOGGER.info("Writing PR comment data to GITHUB_OUTPUT.")
#         json_output = json.dumps(comment_data)
#         try:
#             with open(GITHUB_OUTPUT_FILE, "a") as f:
#                 f.write(f"pr_comment_data<<EOF\n{json_output}\nEOF\n")
#         except IOError as e:
#             LOGGER.error(f"Failed to write to GITHUB_OUTPUT file: {e}")
#             LOGGER.info(
#                 f"PR Comment Data (fallback):\n{json.dumps(comment_data, indent=2)}"
#             )
#     else:
#         LOGGER.info(
#             f"PR Comment Data (simulation - GITHUB_OUTPUT not set):\n{json.dumps(comment_data, indent=2)}"
#         )
