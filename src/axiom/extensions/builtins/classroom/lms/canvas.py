# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Canvas LMS provider — concrete implementation of LMSProvider for Instructure Canvas.

Uses the Canvas REST API (or a mock for testing) to sync roster,
push grades, create assignments, and detect enrollment changes.

For real Canvas: uses the `canvasapi` library (optional dependency).
For testing: accepts a CanvasMockServer via config["_mock_server"].
"""

from __future__ import annotations

from .base import (
    AssignmentCreateResult,
    EnrollmentChanges,
    GradePushResult,
    LMSAnnouncement,
    LMSFile,
    LMSModule,
    LMSModuleItem,
    LMSPage,
    LMSProvider,
    LMSStudent,
    LMSWriteResult,
)


class CanvasLMSProvider(LMSProvider):
    """Canvas LMS integration following the ADR-012 provider pattern."""

    _log_prefix = "canvas-lms"
    _required_config = ("api_url", "api_token")
    _fingerprint_fields = ("api_url",)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._api_url: str = config["api_url"]
        self._api_token: str = config["api_token"]
        # For testing: inject a mock server. Production: this is None.
        self._mock = config.get("_mock_server")

    def available(self) -> bool:
        """Check if Canvas is reachable."""
        if self._mock:
            return True
        try:
            import requests

            resp = requests.get(
                f"{self._api_url}/api/v1/users/self",
                headers={"Authorization": f"Bearer {self._api_token}"},
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_roster(self, course_id: str) -> list[LMSStudent]:
        enrollments = self._get_enrollments(course_id)
        return [
            LMSStudent(
                student_id=e["user_id"],
                name=e.get("name", ""),
                email=e.get("email", ""),
                role="student",
            )
            for e in enrollments
            if e.get("type", "").lower() in ("studentenrollment", "student")
        ]

    def push_grade(
        self,
        course_id: str,
        assignment_id: str,
        student_id: str,
        score: float,
        comment: str = "",
    ) -> GradePushResult:
        if self._mock:
            result = self._mock.api_put_grade(course_id, assignment_id, student_id, score, comment)
            if result.get("success"):
                return GradePushResult(
                    success=True,
                    canvas_submission_id=result["id"],
                )
            return GradePushResult(
                success=False,
                message=result.get("error", "unknown error"),
            )

        # Real Canvas API
        try:
            import requests

            resp = requests.put(
                f"{self._api_url}/api/v1/courses/{course_id}"
                f"/assignments/{assignment_id}/submissions/{student_id}",
                headers={"Authorization": f"Bearer {self._api_token}"},
                json={
                    "submission": {
                        "posted_grade": str(score),
                    },
                    "comment": {"text_comment": comment} if comment else {},
                },
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return GradePushResult(
                    success=True,
                    canvas_submission_id=str(data.get("id", "")),
                )
            return GradePushResult(
                success=False,
                message=f"Canvas returned {resp.status_code}: {resp.text[:200]}",
            )
        except Exception as exc:
            return GradePushResult(success=False, message=str(exc))

    def create_assignment(
        self,
        course_id: str,
        name: str,
        description: str = "",
        points_possible: float = 100,
        due_at: str = "",
    ) -> AssignmentCreateResult:
        if self._mock:
            result = self._mock.api_create_assignment(
                course_id, name, description, points_possible, due_at
            )
            return AssignmentCreateResult(
                success=True,
                canvas_assignment_id=result["id"],
            )

        try:
            import requests

            resp = requests.post(
                f"{self._api_url}/api/v1/courses/{course_id}/assignments",
                headers={"Authorization": f"Bearer {self._api_token}"},
                json={
                    "assignment": {
                        "name": name,
                        "description": description,
                        "points_possible": points_possible,
                        "due_at": due_at or None,
                        "submission_types": ["online_text_entry"],
                        "published": True,
                    }
                },
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return AssignmentCreateResult(
                    success=True,
                    canvas_assignment_id=str(data.get("id", "")),
                )
            return AssignmentCreateResult(
                success=False,
                message=f"Canvas returned {resp.status_code}: {resp.text[:200]}",
            )
        except Exception as exc:
            return AssignmentCreateResult(success=False, message=str(exc))

    def get_student(self, course_id: str, student_id: str) -> LMSStudent | None:
        enrollments = self._get_enrollments(course_id)
        for e in enrollments:
            if e["user_id"] == student_id and e.get("type", "").lower() in (
                "studentenrollment",
                "student",
            ):
                return LMSStudent(
                    student_id=e["user_id"],
                    name=e.get("name", ""),
                    email=e.get("email", ""),
                    role="student",
                )
        return None

    def sync_enrollment_changes(
        self,
        course_id: str,
        known_student_ids: list[str],
    ) -> EnrollmentChanges:
        current_roster = self.get_roster(course_id)
        current_ids = {s.student_id for s in current_roster}
        known_ids = set(known_student_ids)

        added = [s for s in current_roster if s.student_id not in known_ids]
        dropped_ids = known_ids - current_ids
        dropped = [LMSStudent(student_id=sid, name="", email="") for sid in dropped_ids]
        return EnrollmentChanges(added=added, dropped=dropped)

    # -- internal -----------------------------------------------------------

    def _get_enrollments(self, course_id: str) -> list[dict]:
        if self._mock:
            return self._mock.api_get_enrollments(course_id)

        try:
            import requests

            enrollments = []
            url = f"{self._api_url}/api/v1/courses/{course_id}/enrollments"
            headers = {"Authorization": f"Bearer {self._api_token}"}
            while url:
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    return []
                enrollments.extend(resp.json())
                # Canvas pagination via Link header
                links = resp.headers.get("Link", "")
                url = None
                for part in links.split(","):
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip(" <>")
            return enrollments
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Phase 0.1 — Read-only content fetchers
    # ------------------------------------------------------------------

    def get_modules(self, course_id: str) -> list[LMSModule]:
        """List modules in a course, sorted by position."""
        if self._mock:
            raw = self._mock.api_list_modules(course_id)
        else:
            raw = self._fetch_paginated(f"/api/v1/courses/{course_id}/modules")
        modules = [
            LMSModule(
                module_id=str(m.get("id", "")),
                course_id=course_id,
                name=m.get("name", ""),
                position=int(m.get("position", 0)),
            )
            for m in raw
        ]
        modules.sort(key=lambda m: (m.position, m.name))
        return modules

    def get_module_items(self, course_id: str, module_id: str) -> list[LMSModuleItem]:
        """List items inside a module, sorted by position."""
        if self._mock:
            raw = self._mock.api_list_module_items(course_id, module_id)
        else:
            raw = self._fetch_paginated(
                f"/api/v1/courses/{course_id}/modules/{module_id}/items"
            )
        items = [
            LMSModuleItem(
                item_id=str(item.get("id", "")),
                module_id=module_id,
                course_id=course_id,
                type=item.get("type", ""),
                title=item.get("title", ""),
                content_id=str(item.get("content_id", "")),
                position=int(item.get("position", 0)),
            )
            for item in raw
        ]
        items.sort(key=lambda i: (i.position, i.title))
        return items

    def get_files(self, course_id: str) -> list[LMSFile]:
        """List files uploaded to a course."""
        if self._mock:
            raw = self._mock.api_list_files(course_id)
        else:
            raw = self._fetch_paginated(f"/api/v1/courses/{course_id}/files")
        return [
            LMSFile(
                file_id=str(f.get("id", "")),
                course_id=course_id,
                display_name=f.get("display_name", ""),
                content_type=f.get("content_type", ""),
                size=int(f.get("size", 0)),
            )
            for f in raw
        ]

    def get_file_content(self, course_id: str, file_id: str) -> bytes | None:
        """Download a file's bytes; None if missing."""
        if self._mock:
            return self._mock.api_get_file_body(course_id, file_id)
        try:
            import requests

            url = f"{self._api_url}/api/v1/files/{file_id}/download"
            headers = {"Authorization": f"Bearer {self._api_token}"}
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                return None
            return resp.content
        except Exception:
            return None

    def get_pages(self, course_id: str) -> list[LMSPage]:
        """List wiki pages in a course (with body)."""
        if self._mock:
            raw = self._mock.api_list_pages(course_id)
        else:
            raw = self._fetch_paginated(f"/api/v1/courses/{course_id}/pages")
        return [
            LMSPage(
                url_slug=p.get("url_slug", p.get("page_url", "")),
                course_id=course_id,
                title=p.get("title", ""),
                body=p.get("body", ""),
            )
            for p in raw
        ]

    def get_announcements(self, course_id: str) -> list[LMSAnnouncement]:
        """List announcements in a course, newest first."""
        if self._mock:
            raw = self._mock.api_list_announcements(course_id)
        else:
            raw = self._fetch_paginated(
                f"/api/v1/courses/{course_id}/discussion_topics?only_announcements=true"
            )
        anns = [
            LMSAnnouncement(
                announcement_id=str(a.get("id", "")),
                course_id=course_id,
                title=a.get("title", ""),
                message=a.get("message", ""),
                posted_at=a.get("posted_at", ""),
                author=a.get("author", "") or a.get("user_name", ""),
            )
            for a in raw
        ]
        # Newest first by posted_at descending
        anns.sort(key=lambda a: a.posted_at, reverse=True)
        return anns

    # ------------------------------------------------------------------
    # Phase 0.2 — Write methods
    # ------------------------------------------------------------------

    def create_page(
        self, course_id: str, *, title: str, body: str
    ) -> LMSWriteResult:
        if self._mock:
            r = self._mock.api_create_page(course_id, title=title, body=body)
            return LMSWriteResult(
                success=bool(r.get("success")),
                message=r.get("error", ""),
                lms_id=r.get("id", "") or r.get("url_slug", ""),
                url_slug=r.get("url_slug", ""),
            )
        return LMSWriteResult(success=False, message="real Canvas write not implemented")

    def update_page(
        self, course_id: str, *, url_slug: str, body: str, title: str = ""
    ) -> LMSWriteResult:
        if self._mock:
            r = self._mock.api_update_page(course_id, url_slug=url_slug, body=body)
            return LMSWriteResult(
                success=bool(r.get("success")),
                message=r.get("error", ""),
                lms_id=url_slug,
                url_slug=url_slug,
            )
        return LMSWriteResult(success=False, message="real Canvas write not implemented")

    def post_announcement(
        self, course_id: str, *, title: str, message: str, author: str = ""
    ) -> LMSWriteResult:
        if self._mock:
            r = self._mock.api_post_announcement(
                course_id, title=title, message=message, author=author
            )
            return LMSWriteResult(
                success=bool(r.get("success")),
                message=r.get("error", ""),
                lms_id=r.get("id", ""),
            )
        return LMSWriteResult(success=False, message="real Canvas write not implemented")

    def update_assignment_description(
        self, course_id: str, *, assignment_id: str, description: str
    ) -> LMSWriteResult:
        if self._mock:
            r = self._mock.api_update_assignment_description(
                course_id, assignment_id=assignment_id, description=description
            )
            return LMSWriteResult(
                success=bool(r.get("success")),
                message=r.get("error", ""),
                lms_id=assignment_id,
            )
        return LMSWriteResult(success=False, message="real Canvas write not implemented")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_paginated(self, path: str) -> list[dict]:
        """Fetch a paginated Canvas endpoint, walking the Link headers."""
        try:
            import requests

            url = f"{self._api_url}{path}"
            headers = {"Authorization": f"Bearer {self._api_token}"}
            results: list[dict] = []
            while url:
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code != 200:
                    return results
                payload = resp.json()
                if isinstance(payload, list):
                    results.extend(payload)
                else:
                    results.append(payload)
                url = None
                links = resp.headers.get("Link", "")
                for part in links.split(","):
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip(" <>")
            return results
        except Exception:
            return []
