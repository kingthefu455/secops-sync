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
import sys
import tempfile
import unittest

from helpers import make_playbook, write_file, write_playbook
from validation_report import ReportError, build_report

INSTANCE_ID = "4b41988d-9fb1-4a11-b3e5-18f17c87bc84"


class FileChangeRecorder:
    """Records the files and folders Python creates, changes or removes.

    Uses audit hooks, which can't be removed, so the hook only records while
    a recorder is active.
    """

    _active: list | None = None
    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND
    _EVENTS = {
        "os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.replace",
        "os.symlink", "os.link", "tempfile.mkdtemp", "tempfile.mkstemp",
    }

    @classmethod
    def _hook(cls, event: str, args: tuple) -> None:
        if cls._active is None:
            return
        # A number is an open file descriptor, like the pipe to a subprocess
        is_path = not isinstance(args[0], int)
        if event == "open" and is_path and args[2] & cls._WRITE_FLAGS:
            cls._active.append(("open for writing", args[0]))
        elif event in cls._EVENTS:
            cls._active.append((event, args[0]))

    def __enter__(self) -> list:
        if not hasattr(FileChangeRecorder, "_hook_added"):
            sys.addaudithook(FileChangeRecorder._hook)
            FileChangeRecorder._hook_added = True
        FileChangeRecorder._active = []
        return FileChangeRecorder._active

    def __exit__(self, *args) -> None:
        FileChangeRecorder._active = None


def files_and_times(folder: str) -> dict[str, int]:
    """Returns the modification time of every file in a folder, .git included."""
    return {
        os.path.join(dir_path, x): os.stat(os.path.join(dir_path, x)).st_mtime_ns
        for dir_path, _, file_names in os.walk(folder)
        for x in file_names
    }


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

        self.assertIn(
            "Result: 2 error(s), 1 of them in playbooks this merge request changes, "
            "and 0 warning(s). Checked 6 playbook(s).",
            report,
        )
        for line in (
            # Needs attention
            "  - 'Phishing Triage' is now disabled",
            "  - The trigger of 'Phishing Triage' changed",
            "  - 2 step(s) removed from 'Phishing Triage'",
            "  - 'Malware' is renamed to 'Malware Response'",
            "  - 'New Playbook' is added and enabled",
            "  - 'Old Block' is deleted. sync-playbooks doesn't delete playbooks",
            "  - The sync tool or its pipeline changed: main.py",
            # Playbook changes
            "  Added: 'New Playbook' (Playbooks/Default/new_playbook)",
            "  Changed: 'Phishing Triage' (Playbooks/Default/phishing_triage)",
            "  Renamed: 'Malware Response' (Playbooks/Default/malware_response)",
            "  Deleted: 'Old Block' (Playbooks/Default/old_block)",
            "    - Renamed from 'Malware'",
            "    - Moved from Playbooks/Default/malware to Playbooks/Default/malware_response",
            "    - isEnabled: true -> false",
            '    - Trigger type: "ALL" -> "TAG_NAME"',
            "    - Step removed: 'Enrich'",
            "    - Step removed: 'Enrich IP' (VirusTotal)",
            "    - Step 'Get Case Data' changed: parameter IntegrationInstance: "
            f'null -> "{INSTANCE_ID}"',
            "    - Connection removed: 'Get Case Data' -> 'Enrich'",
            # Findings
            "  In playbooks this merge request changes (1):\n"
            "    ERROR Playbooks/Default/new_playbook/steps/enrich_bbbbb.json: ",
            "  In other playbooks (1):\n"
            "    ERROR Playbooks/Default/broken/steps/get_case_data_aaaaa.json: ",
            # Other files
            "OTHER CHANGED FILES\n  Modified: main.py",
        ):
            self.assertIn(line, report)
        # The fixed playbook only changed in the step with the error
        self.assertIn(
            "    - Step 'Get Case Data' changed: parameter FallbackIntegrationInstance "
            "added: null; parameter IntegrationInstance added: null",
            report,
        )
        # The fixed error is gone
        self.assertNotIn("to_fix/steps/get_case_data_aaaaa.json", report)
        # Reformatting a playbook doesn't change it
        self.assertNotIn("Reformatted", report)
        # The report is plain text
        for markdown in ("#", "**", "`", "|"):
            self.assertNotIn(markdown, report)

    def test_no_changes(self):
        write_playbook(self.repo, make_playbook())
        self.commit()
        report = build_report(self.repo, "main")
        self.assertIn(
            "Result: no errors, 0 warning(s). Checked 1 playbook(s).\n\n"
            "NEEDS ATTENTION\n  None.\n\n"
            "PLAYBOOK CHANGES\n  None.\n\n"
            "FINDINGS\n  None.\n\n"
            "OTHER CHANGED FILES\n  None.",
            report,
        )

    def test_creates_no_files(self):
        write_playbook(self.repo, make_playbook())
        self.commit()
        self.git("checkout", "-q", "-b", "feature")
        write_playbook(self.repo, make_playbook("Second Playbook"))
        self.commit()
        before = files_and_times(self.repo)
        with FileChangeRecorder() as changes:
            build_report(self.repo, "main")
        # Python doesn't write files, and git doesn't change the repo
        self.assertEqual(changes, [])
        self.assertEqual(files_and_times(self.repo), before)

    def test_unknown_base(self):
        write_file(os.path.join(self.repo, "main.py"), "")
        self.commit()
        with self.assertRaisesRegex(ReportError, "Can't find origin/develop"):
            build_report(self.repo, "origin/develop")


if __name__ == "__main__":
    unittest.main()
