# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from cache import Cache
from config import   SECOPS_CUSTOMER_ID, SECOPS_PROJECT_ID,SECOPS_REGION
from constants import ALL_ENVIRONMENTS_IDENTIFIER, ROOT_README, STEP_TYPE
from jinja2 import Template
from models import APIError, File, SocRole, Workflow, WorkflowMenuCard, WorkflowTypes
from requests.exceptions import HTTPError
from secops_client import SecOpsClient
from utils import WD, push_playbook, update_objects

LOGGER = logging.getLogger("rac")

# Step parameters holding an integration instance id, and the parameter keys that
# store the instance display name on pull. Instance ids are unique per platform,
# so the display name is used to find the same instance in the target platform,
# and the default instance of the integration is used for steps without one.
INSTANCE_DISPLAY_NAME_KEYS = {
    "IntegrationInstance": "InstanceDisplayName",
    "FallbackIntegrationInstance": "FallbackInstanceDisplayName",
}


def _is_not_found(error: BaseException | None) -> bool:
    """Checks if an API error was caused by a 404 response."""
    while error is not None:
        if isinstance(error, HTTPError) and error.response is not None:
            return error.response.status_code == 404
        error = error.__cause__
    return False


class ResponseManager:
    """Manages the lifecycle of SecOps playbooks."""

    def __init__(self):
        if not all([SECOPS_CUSTOMER_ID, SECOPS_PROJECT_ID, SECOPS_REGION]):
            raise APIError(
                "Missing SecOps env vars: SECOPS_CUSTOMER_ID, SECOPS_PROJECT_ID, SECOPS_REGION."
            )
        try:
            self.client = SecOpsClient(
                customer_id=SECOPS_CUSTOMER_ID,
                project_id=SECOPS_PROJECT_ID,
                region=SECOPS_REGION,
            )
            LOGGER.info("Custom SecOps client initialized successfully.")
        except Exception as e:
            raise APIError(f"Failed to initialize Custom SecOps client: {e}") from e

    def get_soc_roles(self) -> list[SocRole]:
        """Retrieves a list of all available SOC roles from the SecOps API."""
        return self.client.list_soc_roles()

    def get_playbooks(self) -> list[WorkflowMenuCard]:
        """Retrieves a list of all playbooks and blocks from the Chronicle API."""
        return self.client.get_playbooks()

    def get_playbook(self, identifier: str):
        """Retrieves full information for a legacy playbooks or block by its identifier."""
        return self.client.get_playbook(identifier)

    def store_playbook(self, playbook: Workflow) -> None:
        """Writes a workflow to the repo

        Args:
            playbook: A playbook object

        """
        push_playbook(playbook)

    def install_playbooks(self, workflows: list[Workflow]) -> None:
        """Install or update playbooks and blocks

        Args:
            workflows: A list of workflow instances to install

        Raises:
            Exception: When a playbook environment doesn't exist

        """
        # Validate all playbook environments exist as environments or environment groups
        environments = (
            self.client.get_environment_names()
            + self.client.get_environment_group_names()
            + [ALL_ENVIRONMENTS_IDENTIFIER]
        )
        for p in workflows:
            invalid_environments = [x for x in p.environments if x not in environments]
            if invalid_environments:
                raise ValueError(
                    f"Playbook '{p.name}' is assigned to environment(s) that don't exist: "
                    f"{', '.join(invalid_environments)}. "
                    f"Available environments: {', '.join(environments)}"
                )

        # Remove duplicates and split by type
        workflows = list(set(workflows))
        cache: Cache[str, int] = Cache()
        playbook_installer = WorkflowInstaller(
            self.client, cache, {workflow.name for workflow in workflows}
        )
        blocks, playbooks = [], []
        for workflow in workflows:
            if workflow.type == WorkflowTypes.BLOCK:
                blocks.append(workflow)
            else:
                playbooks.append(workflow)

        # Save blocks first
        for block in blocks:
            playbook_installer.install_workflow(block)

        playbook_installer.refresh_cache_item("playbooks")

        for playbook in playbooks:
            playbook_installer.install_workflow(playbook)


    def generate_root_readme(self) -> str:
        """Generates the readme file contents for the root of the repository

        Contains all assets currently in the repository

        Returns: Readme file contents
        """

        def strip_new_lines(s: str):
            # Description might be None
            return s.replace("\n", "").strip() if s else s

        #integrations = [
        #    {
        #        "name": integration.definition["DisplayName"],
        #        "description": strip_new_lines(integration.definition["Description"]),
        #    }
        #    for integration in self.content.get_integrations()
        #]

        playbooks = [
            {
                "name": playbook.name,
                "description": strip_new_lines(playbook.description),
            }
            for playbook in self.content.get_playbooks()
        ]

        #connectors = [
        #    {
        #        "name": connector.name,
        #        "description": strip_new_lines(connector.description),
        #        "hasMappings": bool(self.content.get_mapping(connector.integration)),
        #    }
        #    for connector in self.content.get_connectors()
        #]

        #jobs = [
        #    {"name": job.name, "description": strip_new_lines(job.description)}
        #    for job in self.content.get_jobs()
        #]

        #visual_families = [
        #    {"name": vf.name, "description": strip_new_lines(vf.description)}
        #    for vf in self.content.get_visual_families()
        #]

        readme = Template(ROOT_README)
        return readme.render(
        #    connectors=connectors,
        #    integrations=integrations,
        #    jobs=jobs,
        #    visualFamilies=visual_families,
            playbooks=playbooks
        )

    def update_readme(self, readme: str, base_path: str = "") -> None:
        """Creates or updates a readme file in basePath

        Valid base_path format:
        - "Playbooks/PlaybookName/"

        Args:
            readme: The readme contents
            base_path: Base path in the repo to write the readme file. Writes to root by default.

        """
        if base_path and not base_path.endswith(
            "/"
        ):  # Ensure trailing slash for base_path if not empty
            base_path += "/"

        # Ensure content passed to LocalFolderManager is bytes
        readme_content_bytes = readme.encode("utf-8")
        update_objects([File("README.md", readme_content_bytes)], base_path=base_path)
        LOGGER.info(f"Updated README.md at {base_path}README.md")

    def commit_and_push(self, message: str) -> None:
        """Commits all the changes and pushes the commit to the repo

        Args:
            message: The commit message

        """
        if self.content.metadata.get_setting_by_name("update_root_readme"):
            # Generate root readme
            root_readme = self.generate_root_readme()
            self.update_readme(root_readme)

        self.content.metadata.system_version = self.api.get_system_version()
        self.content.push_metadata()
        self.git_client.commit_and_push(message)

    def update_instance_name_in_steps(
        self,
        workflow: Workflow,
    ) -> None:
        """Updates name of instances in the steps."""
        for step in WorkflowInstaller._flatten_playbook_steps(workflow.steps):
            if (
                step.get("type") == STEP_TYPE
                and step.get("actionProvider") == "Scripts"
            ):
                self._update_instance_display_names_for_step(workflow, step)

    def _update_instance_display_names_for_step(
        self, workflow: Workflow, step: dict
    ) -> None:
        """Updates display names for integration instance parameters in a step.

        Args:
            workflow: The workflow object.
            step: The workflow step dictionary to process.
        """
        integration_name = step["integration"]

        for param in step.get("parameters", []):
            param_name = param.get("name")
            param_value = param.get("value")
            for key in INSTANCE_DISPLAY_NAME_KEYS.values():
                param.pop(key, None)
            try:
                if not workflow._is_integration_instance_param(param_name, param_value):
                    continue

                display_name = self.client.get_integration_instance_name(
                    integration_name, param_value
                )

                match param_name:
                    case "IntegrationInstance":
                        param["InstanceDisplayName"] = display_name
                    case "FallbackIntegrationInstance":
                        param["FallbackInstanceDisplayName"] = display_name
            except APIError as e:
                # ignoring 404 errors as they expected in migrations between instances.
                if not _is_not_found(e):
                    raise


class WorkflowInstaller:
    """Helper class for installing workflows"""

    def __init__(
        self,
        client: SecOpsClient,
        mod_time_cache: Cache[str, int],
        git_workflow_names: set[str] | None = None,
    ) -> None:
        self.client = client
        self._mod_time_cache: Cache[str, int] = mod_time_cache
        self._git_workflow_names: set[str] = git_workflow_names or set()
        self._cache: dict[str, WorkflowMenuCard] = {}

    def install_workflow(self, workflow: Workflow) -> None:
        """Save a playbook or block in the current platform

        Args:
            workflow: A Playbook object to install

        """
        installed_workflow = self._find_installed_workflow(workflow)
        if installed_workflow:
            self._update_workflow_if_needed(workflow, installed_workflow)

        else:
            self.install_new_workflow(workflow)

    def _find_installed_workflow(self, workflow: Workflow) -> WorkflowMenuCard | None:
        """Find the installed workflow that a git workflow belongs to.

        A workflow renamed in git keeps its identifier, so it is matched by
        identifier as long as its old name is gone from git. Otherwise (a copied
        workflow folder, or a migration between instances) it is matched by name.

        Raises:
            ValueError: If a renamed workflow takes the name of another installed
                workflow.
        """
        identifier = workflow.raw_data.get("identifier")
        installed = next(
            (
                x
                for x in self._installed_playbooks.values()
                if identifier and x.identifier == identifier
            ),
            None,
        )
        if (
            installed is None
            or installed.name == workflow.name
            # The old name is still in git, so this workflow is a copy
            or installed.name in self._git_workflow_names
        ):
            return self._installed_playbooks.get(workflow.name)

        if workflow.name in self._installed_playbooks:
            raise ValueError(
                f"Can't rename workflow '{installed.name}' to '{workflow.name}': "
                "another installed workflow already has that name"
            )
        LOGGER.info(f"Workflow '{installed.name}' was renamed to '{workflow.name}'")
        return installed

    def _update_workflow_if_needed(
        self, workflow: Workflow, installed_workflow: WorkflowMenuCard
    ) -> None:
        if not self._workflow_was_modified(workflow, installed_workflow):
            LOGGER.info(f"Skipped update for unchanged workflow '{workflow.name}'")
            self._filter_and_save_context()
            return

        self._log_merge_conflicts(workflow, installed_workflow)
        self.update_local_workflow(workflow, installed_workflow)

    def _log_merge_conflicts(
        self, workflow: Workflow, installed_workflow: WorkflowMenuCard
    ) -> None:
        if self._has_merge_conflicts(workflow, installed_workflow):
            LOGGER.warning(
                "Both the git playbook and local installed playbook were modified."
                "  Git version will override local changes!",
            )

    def _has_merge_conflicts(
        self, workflow: Workflow, installed_workflow: WorkflowMenuCard
    ) -> bool:
        cached_time: int = self._mod_time_cache.get(installed_workflow.name, -1)
        local_time: int = self._get_local_workflow_mod_time(
            installed_workflow.name, -1
        )
        return min(local_time, workflow.modification_time) > cached_time

    def _workflow_was_modified(
        self, workflow: Workflow, installed_workflow: WorkflowMenuCard
    ) -> bool:
        cached_time: int = self._mod_time_cache.get(installed_workflow.name, -1)
        return workflow.modification_time > cached_time

    def update_local_workflow(
        self, workflow: Workflow, installed_workflow: WorkflowMenuCard
    ) -> None:
        """Update an existing workflow in the platform."""
        LOGGER.info(f"Updating existing workflow '{workflow.name}'")
        self._adjust_workflow_ids(workflow, installed_workflow)
        self._process_steps(workflow)
        self._remove_instance_display_names(workflow)
        self.client.save_playbook(workflow)
        self._save_workflow_mod_time_to_context(workflow)
        LOGGER.info(f"Workflow '{workflow.name}' was updated successfully")

    def _adjust_workflow_ids(
        self, workflow: Workflow, installed_workflow: WorkflowMenuCard
    ) -> None:
        """Adjust workflow identifiers to match the installed workflow's identifiers."""
        local_playbook: dict[str, Any] = self.client.get_playbook(
            installed_workflow.identifier
        )
        self._copy_ids_from_existing_workflow(workflow, local_playbook)
        self._process_steps(workflow, local_playbook)

    def install_new_workflow(self, workflow: Workflow) -> None:
        """Install a new workflow to the platform."""
        LOGGER.info(f"Installing new workflow '{workflow.name}'")
        self._define_workflow_as_new(workflow)
        self._process_steps(workflow)
        self._remove_instance_display_names(workflow)
        self.client.save_playbook(workflow)
        self._save_workflow_mod_time_to_context(workflow)
        LOGGER.info(f"New Playbook '{workflow.name}' was installed successfully")

    def _process_steps(
        self,
        workflow: Workflow,
        installed_workflow: dict | None = None,
    ) -> None:
        """Iterate the playbook steps and assign the correct integration instances and block
        identifiers

        Args:
            workflow: Workflow to iterate steps
            installed_workflow: Optional current installed playbooks to copy identifiers

        """
        # A dict where the keys are the old step identifiers and values are the new step identifiers
        # Used for patching step relations
        identifier_mappings = {}
        # Flatten steps to include action containers
        old_steps = (
            self._flatten_playbook_steps(installed_workflow.get("steps"))
            if installed_workflow
            else None
        )
        for step in self._flatten_playbook_steps(workflow.raw_data.get("steps")):
            provider = step.get("actionProvider")
            step_type = step.get("type")

            step["workflowIdentifier"] = workflow.raw_data.get("identifier")
            # Take the step identifier if the same step instance name already exists.
            existing_step = (
                next(
                    (
                        x
                        for x in old_steps
                        if (
                            x.get("instanceName") == step.get("instanceName")
                            and x.get("actionProvider") == step.get("actionProvider")
                        )
                    ),
                    None,
                )
                if old_steps
                else None
            )
            if existing_step:
                old_step_identifier = step.get("identifier")
                identifier_mappings[old_step_identifier] = existing_step.get(
                    "identifier",
                )
                step["identifier"] = existing_step.get("identifier")
                step["originalStepIdentifier"] = existing_step.get(
                    "originalStepIdentifier",
                )

                step_debug_data = step.get("debugData")
                if step_debug_data and step_debug_data.get("originalStepIdentifier"):
                    step_debug_data["originalStepIdentifier"] = existing_step.get(
                        "originalStepIdentifier",
                    )
                if step_debug_data and step_debug_data.get(
                    "originalWorkflowIdentifier",
                ):
                    step_debug_data["originalWorkflowIdentifier"] = (
                        installed_workflow.get("originalPlaybookIdentifier")
                    )

            if step_type == STEP_TYPE and provider == "Scripts":  # Regular Action
                self._assign_integration_instance_to_step(
                    step,
                    workflow.environments,
                    existing_step,
                )
            elif step_type == "BLOCK":  # Nested Workflow
                self._link_nested_block_step(step)

        for relation in workflow.raw_data.get("stepsRelations"):
            if relation.get("fromStep") in identifier_mappings:
                relation["fromStep"] = identifier_mappings.get(relation.get("fromStep"))
            if relation.get("toStep") in identifier_mappings:
                relation["toStep"] = identifier_mappings.get(relation.get("toStep"))

        self._adjust_loop_keys_and_parameters(identifier_mappings, workflow)

    def _adjust_loop_keys_and_parameters(self, identifier_mappings, workflow):
        for step in self._flatten_playbook_steps(workflow.raw_data.get("steps")):
            if step.get("startLoopStepIdentifier"):
                mapped_id = identifier_mappings.get(step["startLoopStepIdentifier"])
                if mapped_id:
                    step["startLoopStepIdentifier"] = mapped_id

            if step.get("endLoopStepIdentifier"):
                mapped_id = identifier_mappings.get(step["endLoopStepIdentifier"])
                if mapped_id:
                    step["endLoopStepIdentifier"] = mapped_id

            parameters = step.get("parameters", [])
            for param in parameters:
                param_name = param.get("name")
                param_value = param.get("value")

                # Handle Start/EndLoopStepIdentifier parameter
                if (
                    param_name in {"StartLoopStepIdentifier", "EndLoopStepIdentifier"}
                    and param_value
                ):
                    mapped_id = identifier_mappings.get(param_value)
                    if mapped_id:
                        param["value"] = mapped_id

    def _save_workflow_mod_time_to_context(self, workflow: Workflow) -> None:
        self.refresh_cache_item("playbooks")
        new_mod_time: int = self._get_local_workflow_mod_time(workflow.name, -1)
        self._mod_time_cache[workflow.name] = new_mod_time
        self._filter_and_save_context()

    def _filter_and_save_context(self) -> None:
        self._mod_time_cache.filter_items(set(self._installed_playbooks))
        self._mod_time_cache.push_local_to_external()

    def _get_local_workflow_mod_time(
        self,
        __workflow_name: str,
        default: int | None = None,
        /,
    ) -> int:
        playbook: dict[str, Any] = self._installed_playbooks[__workflow_name]
        return playbook.modification_time or default

    @property
    def _installed_playbooks(self) -> dict[str, WorkflowMenuCard]:
        """Currently installed playbooks and blocks"""
        if "playbooks" not in self._cache:
            self._cache["playbooks"] = {x.name: x for x in self.client.get_playbooks()}
        return self._cache.get("playbooks")

    @property
    def _playbook_categories(self) -> dict:
        """Currently configured playbook categories"""
        if "categories" not in self._cache:
            self._cache["categories"] = {
                x.name: x for x in self.client.get_playbook_categories()
            }
        return self._cache.get("categories")

    def refresh_cache_item(self, item_name) -> None:
        if item_name in self._cache:
            del self._cache[item_name]

    def _assign_integration_instance_to_step(
        self,
        step: dict,
        environments: list,
        existing_step: dict | None = None,
    ) -> None:
        """Reconfigure an integration instance of a workflow step.

        Since instance ids are different in every platform, it will first assign the step to the
        instances with the display names stored on pull, or to the default instance of the
        integration if the step has no display name or no instance has that name.
        If that isn't possible and old_steps is supplied, It will try to match the same step in the
        old playbook and assign the step to the same integration instance.
        Otherwise, If the playbook is assigned to only one environment (and not all environments),
        it will assign the first integration instance it finds.
        If the playbook is assigned to All Environments or more than one environment, The step will
        be set to dynamic mode and assigned to the first shared instance, or None if it doesn't
        exist.

        Args:
            step: The step to reconfigure
            environments: Playbook assigned environments, for searching integration instances
            existing_step: Optional - if the step is already defined, take the integration instance
            from it instead

        """
        if self._assign_target_integration_instances(step, environments):
            return

        if existing_step:
            instance = self._get_step_parameter_by_name(
                existing_step,
                "IntegrationInstance",
            ).get("value")
            self._set_step_parameter_by_name(step, "IntegrationInstance", instance)
            fallback = self._get_step_parameter_by_name(
                existing_step,
                "FallbackIntegrationInstance",
            ).get("value")
            self._set_step_parameter_by_name(
                step,
                "FallbackIntegrationInstance",
                fallback,
            )
            # remove integration and fallback integration fields from params
            for param in existing_step.get("parameters", []):
                if param.get("InstanceDisplayName"):
                    del param["InstanceDisplayName"]
                elif param.get("FallbackInstanceDisplayName"):
                    del param["FallbackInstanceDisplayName"]

            # remove integration and fallback integration fields from params
            for param in existing_step.get("parameters", []):
                if param.get("InstanceDisplayName"):
                    del param["InstanceDisplayName"]
                elif param.get("FallbackInstanceDisplayName"):
                    del param["FallbackInstanceDisplayName"]

            return

        _instance_display_name = self._get_instance_display_name(
            step,
            "IntegrationInstance",
            "InstanceDisplayName",
        )

        # If the playbook is for one specific environment, choose the first integration instance
        # from that environment. Otherwise, set the step to dynamic mode and set the first shared
        # integration instance as fallback
        if len(environments) == 1 and environments[0] != ALL_ENVIRONMENTS_IDENTIFIER:
            integration_instances = self._find_integration_instances_for_step(
                step.get("integration"),
                environments[0],
            )
            if integration_instances:
                self._set_step_parameter_by_name(
                    step,
                    "IntegrationInstance",
                    integration_instances[0].get("identifier"),
                )
                self._set_step_parameter_by_name(
                    step,
                    "FallbackIntegrationInstance",
                    None,
                )
        else:
            integration_instances = self._find_integration_instances_for_step(
                step.get("integration"),
                ALL_ENVIRONMENTS_IDENTIFIER,
            )
            self._set_step_parameter_by_name(
                step,
                "IntegrationInstance",
                "AutomaticEnvironment",
            )
            if integration_instances:
                self._set_step_parameter_by_name(
                    step,
                    "FallbackIntegrationInstance",
                    None or integration_instances[0].get("identifier"),
                )
            else:
                self._set_step_parameter_by_name(
                    step,
                    "FallbackIntegrationInstance",
                    None,
                )
        # remove integration and fallback integration fields from params
        for param in step.get("parameters", []):
            if param.get("InstanceDisplayName"):
                del param["InstanceDisplayName"]
            elif param.get("FallbackInstanceDisplayName"):
                del param["FallbackInstanceDisplayName"]
        # remove integration and fallback integration fields from params
        for param in step.get("parameters", []):
            if param.get("InstanceDisplayName"):
                del param["InstanceDisplayName"]
            elif param.get("FallbackInstanceDisplayName"):
                del param["FallbackInstanceDisplayName"]

    def _assign_target_integration_instances(
        self,
        step: dict,
        environments: list,
    ) -> bool:
        """Assign the step to integration instances of this platform.

        An instance is matched by the display name stored on pull. If the step has no display
        name (only the instance id of the platform it was pulled from), or no instance has that
        name, the default instance of the integration is used. Steps in dynamic mode keep the
        AutomaticEnvironment instance.

        Args:
            step: The step to reconfigure
            environments: Playbook assigned environments, to choose between instances with the
            same display name

        Returns:
            True if instances were assigned, False if the step has no instance to assign or the
            integration has no default instance when one is needed (the step isn't changed)

        """
        integration_name = step.get("integration")
        instance_ids = []
        for param in step.get("parameters", []):
            display_name_key = INSTANCE_DISPLAY_NAME_KEYS.get(param.get("name"))
            if not display_name_key:
                continue

            display_name = param.get(display_name_key)
            if display_name:
                instance_id = self._find_integration_instance_id(
                    integration_name,
                    display_name,
                    environments,
                )
                if instance_id:
                    instance_ids.append((param, instance_id))
                    continue
                LOGGER.warning(
                    f"Integration instance '{display_name}' of integration '{integration_name}' "
                    f"used by step '{step.get('instanceName')}' wasn't found, "
                    "using the default instance"
                )
            elif param.get("value") in (None, "", "AutomaticEnvironment"):
                # No instance, or dynamic mode
                continue

            instance_id = self._get_default_integration_instance_id(integration_name)
            if not instance_id:
                LOGGER.warning(
                    f"Integration '{integration_name}' used by step '{step.get('instanceName')}' "
                    "has no default instance, assigning the step to another instance"
                )
                return False
            instance_ids.append((param, instance_id))

        for param, instance_id in instance_ids:
            param["value"] = instance_id
        return bool(instance_ids)

    def _find_integration_instance_id(
        self,
        integration_name: str,
        display_name: str,
        environments: list,
    ) -> str | None:
        """Find the id of an integration instance by its display name

        Args:
            integration_name: The integration of the instance
            display_name: The display name of the instance
            environments: Playbook assigned environments, preferred if more than one instance
            has the display name

        Returns:
            The integration instance id, or None if not found

        """
        instances = [
            x
            for x in self._get_integration_instances(integration_name)
            if x.get("displayName") == display_name
        ]
        instances = [
            x for x in instances if x.get("environment") in environments
        ] or instances
        if not instances:
            return None
        if len(instances) > 1:
            LOGGER.warning(
                f"Found {len(instances)} instances of integration '{integration_name}' "
                f"named '{display_name}', using the first one"
            )
        return self._get_instance_id(instances[0])

    def _get_default_integration_instance_id(self, integration_name: str) -> str | None:
        """Get the id of the default instance of an integration, None if it has none"""
        cache_key = f"default_instance_of_integration_{integration_name}"
        if cache_key not in self._cache:
            try:
                instance = self.client.get_default_integration_instance(
                    integration_name
                )
            except APIError as e:
                if not _is_not_found(e):
                    raise
                instance = {}
            self._cache[cache_key] = self._get_instance_id(instance)
        return self._cache.get(cache_key)

    @staticmethod
    def _get_instance_id(instance: dict) -> str | None:
        """The instance id is the last part of the instance resource name"""
        return (instance.get("name") or "").rsplit("/", 1)[-1] or None

    def _get_integration_instances(self, integration_name: str) -> list[dict]:
        """Get all the instances of an integration, empty if the integration isn't installed"""
        cache_key = f"instances_of_integration_{integration_name}"
        if cache_key not in self._cache:
            try:
                self._cache[cache_key] = self.client.list_integration_instances(
                    integration_name
                )
            except APIError as e:
                if not _is_not_found(e):
                    raise
                LOGGER.warning(f"Integration '{integration_name}' isn't installed")
                self._cache[cache_key] = []
        return self._cache.get(cache_key)

    def _remove_instance_display_names(self, workflow: Workflow) -> None:
        """Remove the instance display names stored on pull, they aren't part of the API"""
        for step in self._flatten_playbook_steps(workflow.raw_data.get("steps")):
            for param in step.get("parameters", []):
                for key in INSTANCE_DISPLAY_NAME_KEYS.values():
                    param.pop(key, None)

    def _get_instance_display_name(
        self,
        step: dict,
        parameter_name: str,
        display_name_key: str,
    ) -> str | None:
        """Helper to get the display name of an integration instance parameter."""
        param_json = self._get_step_parameter_by_name(step, parameter_name)

        if param_json is not None:
            return param_json.get(display_name_key)

        return None

    def _find_integration_instances_for_step(
        self,
        integration_name: str,
        environment: str,
    ) -> list[dict]:
        """Find integration instances available for integration per environment

        Args:
            integration_name: The integration name to look for
            environment: The environment to fetch the integration instances

        Returns:
            A list of configured integration instances

        """
        cache_key = f"integration_instances_{environment}"
        if cache_key not in self._cache:
            self._cache[cache_key] = self.client.get_integrations_instances(environment)

        instances = self._cache.get(cache_key)
        instances.sort(key=lambda x: x.get("displayName"))
        instances.sort(key=lambda x: x.get("displayName"))

        return [
            x
            for x in instances
            if x.get("integrationIdentifier") == integration_name
            and x.get("isConfigured")
        ]

    @staticmethod
    def _flatten_playbook_steps(steps: list) -> list[dict]:
        """Flatten playbook steps with parallel actions to one list

        Args:
            steps: The playbook steps to flatten

        Returns:
            Flattened list of steps

        """
        flat_steps = []
        for step in steps:
            if step.get("actionProvider") == "ParallelActionsContainer":
                flat_steps.extend(step.get("parallelActions"))
            flat_steps.append(step)
        return flat_steps

    def _set_step_parameter_by_name(
        self,
        step: dict,
        parameter_name: str,
        parameter_value: str | None,
    ):
        """Set the value of a step parameter

        Args:
            step: The step to reconfigure
            parameter_name: Name of the parameter to reconfigure
            parameter_value: New value of the parameter

        """
        self._get_step_parameter_by_name(step, parameter_name)["value"] = (
            parameter_value
        )

    @staticmethod
    def _get_step_parameter_by_name(step: dict, parameter_name: str) -> dict | None:
        """Get a workflow step parameter by name

        Args:
            step: The step to get the parameter from
            parameter_name: Name of the parameter

        Returns:
            The parameter dict instance, or None if not found

        """
        return next(
            (x for x in step.get("parameters") if x.get("name") == parameter_name),
            None,
        )

    @staticmethod
    def _copy_ids_from_existing_workflow(workflow: Workflow, other: dict) -> None:
        """Reconfigure 'workflow' values according to 'other' workflow

        Args:
            workflow: The workflow to copy the ids to
            other: A dict of the other workflow to copy the ids from

        """
        workflow.raw_data["id"] = other.get("id")
        workflow.raw_data["identifier"] = other.get("identifier")
        workflow.raw_data["originalPlaybookIdentifier"] = other.get(
            "originalPlaybookIdentifier",
        )
        workflow.raw_data["trigger"]["id"] = other.get("trigger").get("id")
        workflow.raw_data["trigger"]["identifier"] = other.get("trigger").get(
            "identifier",
        )
        workflow.raw_data["categoryName"] = other.get("categoryName")
        workflow.raw_data["categoryId"] = other.get("categoryId")

    def _define_workflow_as_new(self, workflow: Workflow) -> None:
        """Generate a new identifier and create the playbook category if it doesn't exist

        Args:
            workflow: A new workflow to reconfigure

        """
        workflow.raw_data["identifier"] = workflow.raw_data[
            "originalPlaybookIdentifier"
        ] = str(uuid.uuid4())
        workflow.raw_data["trigger"]["id"] = 0
        workflow.raw_data["trigger"]["identifier"] = str(uuid.uuid4())

        if workflow.category not in self._playbook_categories:
            category = self.client.create_playbook_category(workflow.category)
            self.refresh_cache_item("categories")
        else:
            category = self._playbook_categories.get(workflow.category)

        workflow.raw_data["categoryId"] = category.id

    def _link_nested_block_step(self, step: dict) -> None:
        """Links a nested block step to the correct block that is stored on the system

        Args:
            step: A nested workflow step to reconfigure

        """
        if (
            step.get("name") in self._installed_playbooks
            and self._installed_playbooks[step.get("name")].playbookType
            == WorkflowTypes.BLOCK.value
        ):
            nested_workflow_identifier = self._get_step_parameter_by_name(
                step,
                "NestedWorkflowIdentifier",
            )
            if nested_workflow_identifier:
                nested_workflow_identifier["value"] = self._installed_playbooks[
                    step.get("name")
                ].identifier
