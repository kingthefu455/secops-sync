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
import tempfile
import unittest
from unittest import mock

import utils
from helpers import make_playbook, write_file, write_playbook


class GetLocalPlaybooksTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        patcher = mock.patch.object(utils, "WD", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_playbook_folders_and_legacy_files(self):
        write_playbook(self.repo, make_playbook("Phishing Triage"))
        write_file(
            os.path.join(self.repo, "Playbooks", "Legacy", "malware.json"),
            json.dumps(make_playbook("Malware")),
        )
        playbooks = {x.name: x for x in utils.get_local_playbooks()}
        self.assertEqual(sorted(playbooks), ["Malware", "Phishing Triage"])
        widget = playbooks["Phishing Triage"].overview_templates[0]["widgets"][0]
        self.assertEqual(
            json.loads(widget["dataDefinitionJson"])["htmlContent"], "<p>Summary</p>"
        )

    def test_duplicate_names_raise(self):
        write_playbook(self.repo, make_playbook())
        write_playbook(
            self.repo,
            make_playbook(),
            folder=os.path.join(self.repo, "Playbooks", "Other", "copy"),
        )
        with self.assertRaisesRegex(ValueError, "is stored twice"):
            list(utils.get_local_playbooks())


if __name__ == "__main__":
    unittest.main()
