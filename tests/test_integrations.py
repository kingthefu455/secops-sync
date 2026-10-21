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

import io
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

import utils
from helpers import write_file
from manager import ResponseManager
from models import Integration, IntegrationPackage
from validation_report import _ChangedFile, _file_attention

FILES = {
    "Integration-MyTools.def": b'{"Identifier": "MyTools"}',
    "ActionsScripts/Ping.py": b"print('pong')\r\n",
    "Resources/logo.png": b"\x89PNG\x00\xff",
}


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("ActionsScripts/", b"")
        for path, content in files.items():
            package.writestr(path, content)
    return buffer.getvalue()


def integration(identifier: str, staging: bool = False) -> Integration:
    return Integration(
        name=f"projects/p/locations/us/instances/i/integrations/{identifier}",
        identifier=identifier,
        displayName=identifier,
        custom=True,
        staging=staging,
    )


class IntegrationPackageTest(unittest.TestCase):
    def test_zip_round_trip_keeps_the_files(self):
        package = IntegrationPackage.from_zip("MyTools", make_zip(FILES))
        self.assertEqual(package.files, FILES)
        self.assertEqual(IntegrationPackage.from_zip("MyTools", package.to_zip()).files, FILES)


class LocalIntegrationsTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        patcher = mock.patch.object(utils, "WD", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stored_package_reads_back_the_same(self):
        utils.push_integration(IntegrationPackage("MyTools", FILES))
        packages = list(utils.get_local_integrations())
        self.assertEqual([x.identifier for x in packages], ["MyTools"])
        self.assertEqual(packages[0].files, FILES)

    def test_store_replaces_removed_files(self):
        stale = os.path.join(self.repo, "Integrations", "MyTools", "ActionsScripts", "Old.py")
        write_file(stale, "")
        utils.push_integration(IntegrationPackage("MyTools", FILES))
        self.assertFalse(os.path.exists(stale))

    def test_identifier_that_isnt_a_folder_name_raises(self):
        for identifier in ("..", "a/b"):
            with self.assertRaisesRegex(ValueError, "isn't a valid folder name"):
                utils.push_integration(IntegrationPackage(identifier, FILES))


class InstallIntegrationsTest(unittest.TestCase):
    def setUp(self):
        self.manager = ResponseManager.__new__(ResponseManager)
        self.manager.client = mock.Mock()
        self.manager.client.import_integration.return_value = {}

    def test_imports_new_and_changed_integrations_only(self):
        self.manager.client.list_integrations.return_value = [
            integration("Unchanged"),
            integration("Changed"),
            integration("New", staging=True),
        ]
        self.manager.client.export_integration.side_effect = lambda x: make_zip(
            {"a.py": b"old" if x.identifier == "Changed" else b"same"}
        )
        self.manager.install_integrations(
            [
                IntegrationPackage("Unchanged", {"a.py": b"same"}),
                IntegrationPackage("Changed", {"a.py": b"new"}),
                IntegrationPackage("New", {"a.py": b"new"}),
            ]
        )
        imported = [x.args[0] for x in self.manager.client.import_integration.call_args_list]
        self.assertEqual(imported, ["Changed", "New"])
        package = self.manager.client.import_integration.call_args_list[0].args[1]
        self.assertEqual(IntegrationPackage.from_zip("Changed", package).files, {"a.py": b"new"})


class FileAttentionTest(unittest.TestCase):
    def test_integration_code_isnt_the_sync_tools_code(self):
        attention = _file_attention(
            [_ChangedFile("M", "Integrations/MyTools/ActionsScripts/Ping.py", None, 1, 1)]
        )
        self.assertEqual(attention, [])


if __name__ == "__main__":
    unittest.main()
