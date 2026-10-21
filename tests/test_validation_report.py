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
import shutil
import subprocess
import tempfile
import unittest

from helpers import make_playbook, write_file, write_playbook
from validation_report import ReportError, build_report

INSTANCE_ID = "4b41988d-9fb1-4a11-b3e5-18f17c87bc84"


def playbook(name: str, identifier: str, **fields) -> dict:
    raw_data = make_playbook(name)
    raw_data["identifier"] = identifier
    raw_data.update(fields)
    return raw_data


class BuildReportTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                self.repo,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    def commit(self) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")

    def folder(self, *parts: str) -> str:
        return os.path.join(self.repo, "Playbooks", "Default", *parts)

    def test_reports_changes_and_findings(self):
        phishing = playbook("Phishing Triage", "id-phishing", isEnabled=True)
        broken = playbook("Broken", "id-broken")
        broken["steps"][0]["parameters"] = []
        to_fix = playbook("To Fix", "id-to-fix")
        to_fix["steps"][0]["parameters"] = []
        for raw_data in (
            phishing,
            playbook("Malware", "id-malware"),
            playbook("Old Block", "id-old-block"),
            playbook("Reformatted", "id-reformatted"),
            broken,
            to_fix,
        ):
            write_playbook(self.repo, raw_data)
        write_file(os.path.join(self.repo, "main.py"), "print('sync')\n")
        self.commit()
        self.git("checkout", "-q", "-b", "feature")

        # Disable, change a parameter and the trigger, remove the parallel actions
        phishing["isEnabled"] = False
        phishing["trigger"]["type"] = "TAG_NAME"
        phishing["steps"][0]["parameters"][0]["value"] = INSTANCE_ID
        del phishing["steps"][1]
        phishing["stepsRelations"] = []
        shutil.rmtree(self.folder("phishing_triage"))
        write_playbook(self.repo, phishing)
        # Rename, delete, reformat, add with an error and fix
        shutil.rmtree(self.folder("malware"))
        write_playbook(self.repo, playbook("Malware Response", "id-malware"))
        shutil.rmtree(self.folder("old_block"))
        definition = os.path.join(self.folder("reformatted"), "definition.json")
        with open(definition, encoding="utf-8") as f:
            data = json.load(f)
        write_file(definition, json.dumps(data, indent=2))
        added = playbook("New Playbook", "id-new", isEnabled=True)
        added["steps"][1]["parallelActions"][0]["parameters"] = []
        write_playbook(self.repo, added)
        write_playbook(self.repo, playbook("To Fix", "id-to-fix"))
        write_file(os.path.join(self.repo, "main.py"), "print('sync playbooks')\n")
        self.commit()

        report = build_report(self.repo, "main")

        self.assertIn("**Result:** ❌ 2 error(s), 1 of them new in this merge request", report)
        for line in (
            # Needs attention
            "- **Phishing Triage** is now disabled",
            "- The trigger of **Phishing Triage** changed",
            "- 2 step(s) removed from **Phishing Triage**",
            "- **Malware** is renamed to **Malware Response**",
            "- **New Playbook** is added and enabled",
            "- **Old Block** is deleted. sync-playbooks doesn't delete playbooks",
            "- The sync tool or its pipeline changed: `main.py`",
            # Playbook changes
            "| Added | **New Playbook** | `Playbooks/Default/new_playbook` |",
            "| Changed | **Phishing Triage** | `Playbooks/Default/phishing_triage` |",
            "| Renamed | **Malware Response** | `Playbooks/Default/malware_response` |",
            "| Deleted | **Old Block** | `Playbooks/Default/old_block` |",
            "- Renamed from **Malware**",
            "- `isEnabled`: `true` → `false`",
            '- Trigger `type`: `"ALL"` → `"TAG_NAME"`',
            f'- Step **Get Case Data** changed: parameter `IntegrationInstance`: `null` → `"{INSTANCE_ID}"`',
            "- Step removed: **Enrich**",
            "- Step removed: **Enrich IP** (VirusTotal)",
            "- Connection removed: **Get Case Data** → **Enrich**",
            "- Moved from `Playbooks/Default/malware` to `Playbooks/Default/malware_response`",
            # Findings
            "### New in this merge request (1)",
            "- ❌ Error `Playbooks/Default/new_playbook/steps/enrich_bbbbb.json`:",
            "### Already on the base branch (1)",
            "- ❌ Error `Playbooks/Default/broken/steps/get_case_data_aaaaa.json`:",
            "### Fixed by this merge request (1)",
            "- ❌ Error `Playbooks/Default/to_fix/steps/get_case_data_aaaaa.json`:",
            # Other files
            "| `main.py` | Modified |",
        ):
            self.assertIn(line, report)
        # The fixed playbook only changed in the step with the error
        self.assertIn(
            "- Step **Get Case Data** changed: parameter `FallbackIntegrationInstance` "
            "added: `null`; parameter `IntegrationInstance` added: `null`",
            report,
        )
        # Reformatting a playbook doesn't change it
        self.assertNotIn("Reformatted", report)
        self.assertNotIn("Playbooks/", report.split("## Other changed files")[1])

    def test_no_changes(self):
        write_playbook(self.repo, make_playbook())
        self.commit()
        report = build_report(self.repo, "main")
        self.assertIn("**Result:** ✅ No errors, 0 warning(s). Checked 1 playbook(s).", report)
        self.assertIn("No playbook changes.", report)
        self.assertIn("No findings.", report)
        self.assertNotIn("## Needs attention", report)

    def test_unknown_base(self):
        write_file(os.path.join(self.repo, "main.py"), "")
        self.commit()
        with self.assertRaisesRegex(ReportError, "Can't find origin/develop"):
            build_report(self.repo, "origin/develop")


if __name__ == "__main__":
    unittest.main()
