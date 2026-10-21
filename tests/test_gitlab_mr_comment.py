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
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import gitlab_mr_comment
from gitlab_mr_comment import MARKER, CommentError, comment

TOKEN = "glpat-test"
BOT_ID = 99
NOTES_PATH = "/api/v4/projects/123/merge_requests/7/notes"


class FakeGitLab(BaseHTTPRequestHandler):
    """The notes API of one merge request, with two notes per page."""

    notes: list[dict] = []
    requests: list[tuple[str, str]] = []
    fail_with: int | None = None

    def log_message(self, *args):
        pass

    def _send(self, status: int, data, next_page: str = ""):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Next-Page", next_page)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        url = urlparse(self.path)
        self.requests.append((self.command, url.path))
        if self.headers.get("PRIVATE-TOKEN") != TOKEN:
            return self._send(401, {"message": "401 Unauthorized"})
        if self.fail_with:
            return self._send(self.fail_with, {"message": "403 Forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length)) if length else None

        if self.command == "GET" and url.path == "/api/v4/user":
            return self._send(200, {"id": BOT_ID, "username": "project_123_bot"})
        if self.command == "GET" and url.path == NOTES_PATH:
            page = int(parse_qs(url.query)["page"][0])
            notes = self.notes[(page - 1) * 2 : page * 2]
            next_page = str(page + 1) if page * 2 < len(self.notes) else ""
            return self._send(200, notes, next_page)
        if self.command == "POST" and url.path == NOTES_PATH:
            note = {"id": 1000 + len(self.notes), "author": {"id": BOT_ID}, "body": data["body"]}
            self.notes.append(note)
            return self._send(201, note)
        if self.command == "PUT" and url.path.startswith(f"{NOTES_PATH}/"):
            note_id = int(url.path.rsplit("/", 1)[1])
            note = next(x for x in self.notes if x["id"] == note_id)
            note["body"] = data["body"]
            return self._send(200, note)
        return self._send(404, {"message": "404 Not Found"})

    do_GET = do_POST = do_PUT = _handle


class CommentTest(unittest.TestCase):
    def setUp(self):
        FakeGitLab.notes = []
        FakeGitLab.requests = []
        FakeGitLab.fail_with = None
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitLab)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.environ = {
            "CI_API_V4_URL": f"http://127.0.0.1:{server.server_port}/api/v4",
            "CI_PROJECT_ID": "123",
            "CI_MERGE_REQUEST_IID": "7",
            "CI_COMMIT_SHORT_SHA": "abc1234",
            "CI_PIPELINE_IID": "42",
            "CI_PIPELINE_URL": "https://gitlab.example.com/group/project/-/pipelines/4242",
            gitlab_mr_comment.TOKEN_VARIABLE: TOKEN,
        }

    def test_adds_a_comment_then_updates_it(self):
        self.assertEqual(comment("# Report 1", self.environ), "Added comment 1000")
        self.assertEqual(comment("# Report 2", self.environ), "Updated comment 1000")
        self.assertEqual(len(FakeGitLab.notes), 1)
        body = FakeGitLab.notes[0]["body"]
        self.assertTrue(body.startswith(f"{MARKER}\n# Report 2\n"))
        self.assertIn(
            "_Updated for commit abc1234 by [pipeline #42]"
            "(https://gitlab.example.com/group/project/-/pipelines/4242)._",
            body,
        )

    def test_finds_its_comment_on_a_later_page_and_ignores_other_authors(self):
        FakeGitLab.notes = [
            {"id": 1, "author": {"id": 5}, "body": "Looks good"},
            {"id": 2, "author": {"id": 5}, "body": f"Quoting the bot: {MARKER}"},
            {"id": 3, "author": {"id": BOT_ID}, "body": "Another bot comment"},
            {"id": 4, "author": {"id": BOT_ID}, "body": f"{MARKER}\nold report"},
        ]
        self.assertEqual(comment("# New", self.environ), "Updated comment 4")
        self.assertEqual(FakeGitLab.notes[1]["body"], f"Quoting the bot: {MARKER}")
        self.assertIn("# New", FakeGitLab.notes[3]["body"])

    def test_cuts_a_report_that_is_too_long(self):
        comment("A line of the report\n" * 60_000, self.environ)
        body = FakeGitLab.notes[0]["body"]
        self.assertLess(len(body), 1_000_000)
        self.assertIn("**The report is cut here.**", body)

    def test_api_errors_are_reported(self):
        FakeGitLab.fail_with = 403
        with self.assertRaisesRegex(CommentError, "GET /user failed with 403"):
            comment("# Report", self.environ)


if __name__ == "__main__":
    unittest.main()
