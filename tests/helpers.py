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
import os

import playbook_format
from config import PLAYBOOKS_PATH


def instance_parameters() -> list[dict]:
    return [
        {"name": "IntegrationInstance", "value": None},
        {"name": "FallbackIntegrationInstance", "value": None},
    ]


def make_playbook(name: str = "Phishing Triage") -> dict:
    """API JSON of a valid playbook with an action, parallel actions and a widget."""
    return {
        "name": name,
        "identifier": "0f0f0f0f-0000-4000-8000-000000000000",
        "playbookType": "REGULAR",
        "categoryName": "Default",
        "modificationTimeUnixTimeInMs": "1700000000000",
        "environments": ["*"],
        "trigger": {"type": "ALL", "logicalOperator": "AND", "conditions": []},
        "steps": [
            {
                "identifier": "aaaaa111-0000-4000-8000-000000000000",
                "instanceName": "Get Case Data",
                "type": "ACTION",
                "actionProvider": "Scripts",
                "integration": "Tools",
                "parameters": instance_parameters(),
            },
            {
                "identifier": "bbbbb222-0000-4000-8000-000000000000",
                "instanceName": "Enrich",
                "actionProvider": "ParallelActionsContainer",
                "parallelActions": [
                    {
                        "identifier": "ccccc333-0000-4000-8000-000000000000",
                        "instanceName": "Enrich IP",
                        "type": "ACTION",
                        "actionProvider": "Scripts",
                        "integration": "VirusTotal",
                        "parameters": instance_parameters(),
                    }
                ],
            },
        ],
        "stepsRelations": [
            {
                "fromStep": "aaaaa111-0000-4000-8000-000000000000",
                "toStep": "bbbbb222-0000-4000-8000-000000000000",
            }
        ],
        "overviewTemplates": [
            {
                "name": "Overview",
                "roles": [],
                "widgets": [
                    {
                        "title": "Summary",
                        "identifier": "ddddd444-0000-4000-8000-000000000000",
                        "dataDefinitionJson": json.dumps(
                            {"htmlContent": "<p>Summary</p>", "type": 1}
                        ),
                    }
                ],
            }
        ],
    }


def write_playbook(repo: str, raw_data: dict, folder: str | None = None) -> str:
    """Writes a playbook in the layout pull-playbooks uses and returns its folder."""
    folder = folder or os.path.join(
        repo,
        PLAYBOOKS_PATH,
        raw_data["categoryName"],
        playbook_format.to_snake_case(raw_data["name"]),
    )
    for path, data in playbook_format.to_files(raw_data).items():
        full_path = os.path.join(folder, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data, indent=4))
    return folder


def write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
