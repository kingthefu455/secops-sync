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

"""Comments a report on the GitLab merge request of the pipeline.

    python gitlab_mr_comment.py validation-report.md

Runs in a merge request pipeline. Later pipelines update the comment instead
of adding one per push. The token that comments is read from the
VALIDATION_REPORT_TOKEN variable. Without it, the script prints how to set it
up and does nothing.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TOKEN_VARIABLE = "VALIDATION_REPORT_TOKEN"
# Finds the comment of earlier pipelines
MARKER = "<!-- validation-report -->"
# GitLab notes hold at most 1,000,000 characters
MAX_LENGTH = 900_000


class CommentError(Exception):
    """Raised when GitLab doesn't accept a request."""


class _GitLab:
    def __init__(self, api_url: str, token: str):
        self._api_url = api_url.rstrip("/")
        self._token = token

    def request(self, method: str, path: str, data: dict | None = None):
        """Sends a request and returns the parsed response and the next page, if any."""
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"PRIVATE-TOKEN": self._token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response), response.headers.get("X-Next-Page")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise CommentError(f"{method} {path} failed with {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise CommentError(f"{method} {path} failed: {e.reason}") from None


def comment(report: str, environ: dict[str, str]) -> str:
    """Creates or updates the report comment and returns what was done."""
    gitlab = _GitLab(environ["CI_API_V4_URL"], environ[TOKEN_VARIABLE])
    notes_path = (
        f"/projects/{environ['CI_PROJECT_ID']}"
        f"/merge_requests/{environ['CI_MERGE_REQUEST_IID']}/notes"
    )
    if len(report) > MAX_LENGTH:
        report = (
            report[: report.rfind("\n", 0, MAX_LENGTH)]
            + "\n\n**The report is cut here.** The full report is in the job artifacts."
        )
    body = (
        f"{MARKER}\n{report}\n\n---\n"
        f"_Updated for commit {environ.get('CI_COMMIT_SHORT_SHA', '')} "
        f"by [pipeline #{environ.get('CI_PIPELINE_IID', '')}]"
        f"({environ.get('CI_PIPELINE_URL', '')})._"
    )

    user, _ = gitlab.request("GET", "/user")
    page: str | None = "1"
    while page:
        notes, page = gitlab.request(
            "GET", f"{notes_path}?sort=asc&order_by=created_at&per_page=100&page={page}"
        )
        previous = next(
            (
                x
                for x in notes
                if x.get("author", {}).get("id") == user["id"]
                and MARKER in (x.get("body") or "")
            ),
            None,
        )
        if previous:
            gitlab.request("PUT", f"{notes_path}/{previous['id']}", {"body": body})
            return f"Updated comment {previous['id']}"
    note, _ = gitlab.request("POST", notes_path, {"body": body})
    return f"Added comment {note['id']}"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"Usage: {sys.argv[0]} REPORT_FILE")
    if not os.environ.get("CI_MERGE_REQUEST_IID"):
        print("Not a merge request pipeline, so there is no merge request to comment on.")
        return
    if not os.environ.get(TOKEN_VARIABLE):
        print(
            f"The {TOKEN_VARIABLE} CI/CD variable isn't set, so the report isn't "
            "commented on the merge request. Set it to a project access token with "
            "the Reporter role and the api scope to get the comment."
        )
        return
    with open(sys.argv[1], encoding="utf-8") as f:
        report = f.read()
    try:
        print(comment(report, dict(os.environ)))
    except CommentError as e:
        sys.exit(f"Can't comment the report: {e}")


if __name__ == "__main__":
    main()
