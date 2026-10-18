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

import logging
import os
import shutil
import sys

import click
from config import EXCLUDE_FOLDER, INCLUDE_BLOCKS, PLAYBOOKS_PATH
from constants import PLAYBOOKS_ROOT_README
from jinja2 import Template
from manager import ResponseManager
from models import SocRole, Workflow, WorkflowMenuCard
from utils import WD, get_local_playbooks

LOGGER = logging.getLogger("rac")


def create_root_readme() -> str:
    """
    Creates the content for the root README file based on the playbooks in the Git repository.
    :return: The rendered README content as a string.
    """
    playbooks = []
    for pb in get_local_playbooks():
        playbooks.append(pb.raw_data)
    readme = Template(PLAYBOOKS_ROOT_README)
    return readme.render(playbooks=playbooks)


def get_soc_role_ids(soc_roles: list[SocRole]) -> dict[str, int | str]:
    """Maps SOC role display names to role IDs, the last part of the role resource
    name (projects/.../socRoles/{id}).
    """
    role_ids = {}
    for soc_role in soc_roles:
        role_id = soc_role.name.rsplit("/", 1)[-1]
        role_ids[soc_role.displayName] = int(role_id) if role_id.isdigit() else role_id
    return role_ids


def add_overview_role_names(
    workflow: Workflow, role_ids: dict[str, int | str]
) -> None:
    """Adds the role names to overview templates that only hold role IDs.

    Role IDs differ between instances, so the sync maps the names instead.
    """
    # Compared as text, in case the API returns IDs as strings
    names_by_id = {str(role_id): name for name, role_id in role_ids.items()}
    for template in workflow.overview_templates:
        if "roleNames" not in template:
            template["roleNames"] = [
                names_by_id[str(role)]
                for role in template.get("roles", [])
                if str(role) in names_by_id
            ]


def remap_overview_roles(
    workflow: Workflow, role_ids: dict[str, int | str]
) -> None:
    """Sets the overview template roles to the IDs of their role names on this
    instance, same as GitSync. Roles that don't exist here are removed.
    """
    for template in workflow.overview_templates:
        role_names = template.pop("roleNames", None)
        if not role_names:
            continue

        missing = [name for name in role_names if name not in role_ids]
        if missing:
            LOGGER.warning(
                f"Role(s) {', '.join(missing)} of view '{template.get('name')}' in "
                f"playbook '{workflow.name}' don't exist on this instance and are removed"
            )
        template["roles"] = [role_ids[name] for name in role_names if name in role_ids]


@click.group()
def cli():
    """Manages Response As Code."""


@cli.command(name="sync-playbooks")
def sync_playbooks():
    response_manager = ResponseManager()
    role_ids = get_soc_role_ids(response_manager.get_soc_roles())

    try:
        playbooks = {}
        git_playbooks = list(get_local_playbooks())
        LOGGER.info(f"Git Playbooks len {len(git_playbooks)}")

        for playbook in git_playbooks:
            LOGGER.info(f"Playbook {playbook.name}")

            remap_overview_roles(playbook, role_ids)
            LOGGER.info(
                f"Playbook Updated Overview templates: {playbook.overview_templates}"
            )

            playbooks[playbook.name] = playbook

            if INCLUDE_BLOCKS:
                for block in playbook.get_involved_blocks():
                    if block.get("name") not in playbooks:
                        block = response_manager.get_playbook(block.get("name"))
                        if block:
                            playbooks[block.name] = block

        response_manager.install_playbooks(list(playbooks.values()))

    except Exception:
        LOGGER.exception("General error performing Sync Playbooks")
        raise


@cli.command(name="pull-playbooks")
def pull_playbooks():
    """
    Synchronize SOAR playbooks from the SOAR platform to a Git repository.
    """
    _commit_msg = os.environ.get("COMMIT_MESSAGE", "Automated playbook sync")
    include_blocks = (
        os.environ.get("INCLUDE_PLAYBOOK_BLOCKS", "false").lower() == "true"
    )

    response_manager = ResponseManager()

    role_ids = get_soc_role_ids(response_manager.get_soc_roles())

    try:
        installed_playbooks: list[WorkflowMenuCard] = response_manager.get_playbooks()

        # The repo mirrors the instance, so start from an empty playbooks folder.
        # Renamed, moved and deleted playbooks then don't leave old files behind.
        playbooks_dir = os.path.join(WD, PLAYBOOKS_PATH)
        if os.path.isdir(playbooks_dir):
            shutil.rmtree(playbooks_dir)

        for playbook in installed_playbooks:
            if EXCLUDE_FOLDER and EXCLUDE_FOLDER in playbook.categoryName:
                LOGGER.info(
                    f"Skip playbook since belongs to the excluded folder: {playbook.displayName}"
                )
                continue

            LOGGER.info(f"Processing Playbook {playbook.displayName}")
            playbook_details = Workflow(
                response_manager.get_playbook(playbook.identifier)
            )
            LOGGER.info(f"Playbook details: {playbook_details}")
            add_overview_role_names(playbook_details, role_ids)

            response_manager.update_instance_name_in_steps(playbook_details)

            response_manager.store_playbook(playbook_details)

            if include_blocks:
                for block in playbook_details.get_involved_blocks():
                    installed_block = next(
                        (
                            x
                            for x in installed_playbooks
                            if x.displayName == block.displayName
                        ),
                        None,
                    )
                    if not installed_block:
                        LOGGER.warning(
                            f"Block {block.displayName} wasn't found in the repo, ignoring"
                        )
                        continue
                    block_details = Workflow(
                        response_manager.get_playbook(installed_block.identifier)
                    )
                    response_manager.update_instance_name_in_steps(block_details)
                    response_manager.store_playbook(block_details)

        #response_manager.update_readme(create_root_readme(), "Playbooks")

    except Exception:
        LOGGER.exception("General error performing Pull Playbooks")
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
        force=True,
    )

    LOGGER.setLevel(logging.INFO)
    cli()
