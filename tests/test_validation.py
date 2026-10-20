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

import glob
import json
import os
import shutil
import tempfile
import unittest

from helpers import make_playbook, write_file, write_playbook
from validation import ERROR, Issue, check_playbooks


class CheckPlaybooksTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)

    def check(self):
        return check_playbooks(self.repo)

    def assert_one_error(self, report, path_end: str, message_part: str):
        self.assertEqual(len(report.errors), 1, report.issues)
        self.assertTrue(report.errors[0].path.endswith(path_end), report.errors[0])
        self.assertIn(message_part, report.errors[0].message)

    def test_valid_playbook(self):
        write_playbook(self.repo, make_playbook())
        report = self.check()
        self.assertEqual(report.playbook_count, 1)
        self.assertEqual(report.issues, [])

    def test_no_playbooks_folder(self):
        report = self.check()
        self.assertEqual(report.playbook_count, 0)
        self.assertEqual(report.issues, [])

    def test_valid_legacy_playbook_file(self):
        write_file(
            os.path.join(self.repo, "Playbooks", "legacy.json"),
            json.dumps(make_playbook()),
        )
        report = self.check()
        self.assertEqual(report.playbook_count, 1)
        self.assertEqual(report.issues, [])

    def test_invalid_json_is_reported_with_its_line(self):
        folder = write_playbook(self.repo, make_playbook())
        write_file(
            os.path.join(folder, "trigger.json"),
            '{\n  "type": "ALL",\n  "conditions" []\n}',
        )
        report = self.check()
        self.assert_one_error(report, "/trigger.json", "Invalid JSON")
        self.assertEqual(report.errors[0].line, 3)

    def test_missing_widget_file(self):
        folder = write_playbook(self.repo, make_playbook())
        os.remove(glob.glob(os.path.join(folder, "widgets", "*.html"))[0])
        self.assert_one_error(
            self.check(), "/overviews.json", "widgets/summary_ddddd.html doesn't exist"
        )

    def test_ignored_files_are_warnings(self):
        folder = write_playbook(self.repo, make_playbook())
        write_file(os.path.join(folder, "widgets", "old_widget.html"), "<p>Old</p>")
        write_file(os.path.join(folder, "steps", "notes.txt"), "notes")
        report = self.check()
        self.assertEqual(report.errors, [])
        self.assertEqual(
            sorted(os.path.basename(x.path) for x in report.warnings),
            ["notes.txt", "old_widget.html"],
        )

    def test_folder_that_doesnt_match_the_name_is_a_warning(self):
        write_playbook(
            self.repo,
            make_playbook(),
            folder=os.path.join(self.repo, "Playbooks", "Default", "old_name"),
        )
        report = self.check()
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("Playbooks/Default/phishing_triage", report.warnings[0].message)

    def test_duplicate_playbook_names(self):
        write_playbook(self.repo, make_playbook())
        write_playbook(
            self.repo,
            make_playbook(),
            folder=os.path.join(self.repo, "Playbooks", "Other", "phishing_triage"),
        )
        report = self.check()
        self.assertEqual(len(report.errors), 1, report.issues)
        self.assertIn("names must be unique", report.errors[0].message)

    def test_names_that_map_to_the_same_folder(self):
        write_playbook(self.repo, make_playbook("Phishing Triage"))
        write_playbook(
            self.repo,
            make_playbook("phishing_triage"),
            folder=os.path.join(self.repo, "Playbooks", "Default", "copy"),
        )
        report = self.check()
        self.assertEqual(len(report.errors), 1, report.issues)
        self.assertIn("pull-playbooks can't store both", report.errors[0].message)

    def test_copied_step_file_with_the_same_identifier(self):
        folder = write_playbook(self.repo, make_playbook())
        shutil.copy(
            os.path.join(folder, "steps", "get_case_data_aaaaa.json"),
            os.path.join(folder, "steps", "get_case_data_aaaaa_2.json"),
        )
        self.assert_one_error(
            self.check(), "/get_case_data_aaaaa_2.json", "has the same identifier"
        )

    def test_relation_to_a_deleted_step(self):
        folder = write_playbook(self.repo, make_playbook())
        os.remove(os.path.join(folder, "steps", "enrich_bbbbb.json"))
        self.assert_one_error(
            self.check(), "/definition.json", "'toStep' is step bbbbb222"
        )

    def test_loop_to_a_missing_step(self):
        playbook = make_playbook()
        playbook["steps"][0]["startLoopStepIdentifier"] = "eeeee555"
        write_playbook(self.repo, playbook)
        self.assert_one_error(
            self.check(), "/get_case_data_aaaaa.json", "'startLoopStepIdentifier'"
        )

    def test_action_without_integration_instance_parameters(self):
        playbook = make_playbook()
        playbook["steps"][1]["parallelActions"][0]["parameters"] = []
        write_playbook(self.repo, playbook)
        self.assert_one_error(
            self.check(),
            "/enrich_bbbbb.json",
            "IntegrationInstance, FallbackIntegrationInstance",
        )

    def test_overview_template_without_roles(self):
        playbook = make_playbook()
        del playbook["overviewTemplates"][0]["roles"]
        write_playbook(self.repo, playbook)
        self.assert_one_error(self.check(), "/overviews.json", "'roles' list")

    def test_missing_definition_fields(self):
        playbook = make_playbook()
        del playbook["trigger"]
        del playbook["stepsRelations"]
        del playbook["modificationTimeUnixTimeInMs"]
        playbook["playbookType"] = "PLAYBOOK"
        write_playbook(self.repo, playbook)
        messages = sorted(x.message for x in self.check().errors)
        self.assertEqual(len(messages), 4, messages)
        self.assertIn("'modificationTimeUnixTimeInMs' must be a number", messages[0])
        self.assertIn("'playbookType' must be one of REGULAR, NESTED", messages[1])
        self.assertIn("'stepsRelations' must be a list", messages[2])
        self.assertIn("'trigger' must be an object", messages[3])

    def test_github_annotation(self):
        issue = Issue(ERROR, "Playbooks/a,b/definition.json", "50% done\nnext", 3)
        self.assertEqual(
            issue.format(github=True),
            "::error file=Playbooks/a%2Cb/definition.json,line=3::"
            "Playbooks/a,b/definition.json:3: 50%25 done%0Anext",
        )


if __name__ == "__main__":
    unittest.main()
