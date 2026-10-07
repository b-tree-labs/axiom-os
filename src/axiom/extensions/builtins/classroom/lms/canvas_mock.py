# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""In-process Canvas API mock for testing.

Not a real HTTP server — a request-response simulator that the
CanvasLMSProvider can talk to via a test adapter. Maintains in-memory
state (courses, enrollments, assignments, submissions) and responds
to the same method calls the real Canvas adapter makes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CanvasMockServer:
    """Simulates the Canvas REST API for test isolation."""

    url: str = "mock://canvas"
    _courses: dict[str, dict] = field(default_factory=dict)
    _enrollments: dict[str, list[dict]] = field(default_factory=dict)
    _assignments: dict[str, list[dict]] = field(default_factory=dict)
    _submissions: dict[str, list[dict]] = field(default_factory=dict)
    _modules: dict[str, list[dict]] = field(default_factory=dict)
    _module_items: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    _files: dict[str, list[dict]] = field(default_factory=dict)
    _file_bodies: dict[tuple[str, str], bytes] = field(default_factory=dict)
    _pages: dict[str, list[dict]] = field(default_factory=dict)
    _announcements: dict[str, list[dict]] = field(default_factory=dict)
    _next_assignment_id: int = field(default=1000)
    _next_submission_id: int = field(default=2000)
    _next_announcement_id: int = field(default=3000)

    def add_course(self, course_id: str, name: str) -> None:
        self._courses[course_id] = {"id": course_id, "name": name}
        self._enrollments.setdefault(course_id, [])
        self._assignments.setdefault(course_id, [])
        self._modules.setdefault(course_id, [])
        self._files.setdefault(course_id, [])
        self._pages.setdefault(course_id, [])
        self._announcements.setdefault(course_id, [])

    # -- Content fixtures (test-side adders) --------------------------------

    def add_module(
        self, course_id: str, *, module_id: str, name: str, position: int = 0
    ) -> None:
        self._modules.setdefault(course_id, []).append(
            {"id": module_id, "name": name, "position": position}
        )

    def add_module_item(
        self,
        course_id: str,
        module_id: str,
        *,
        item_id: str,
        type: str,
        title: str,
        content_id: str = "",
        position: int = 0,
    ) -> None:
        key = (course_id, module_id)
        self._module_items.setdefault(key, []).append(
            {
                "id": item_id,
                "type": type,
                "title": title,
                "content_id": content_id,
                "position": position,
            }
        )

    def add_file(
        self,
        course_id: str,
        *,
        file_id: str,
        display_name: str,
        content_type: str = "",
        size: int = 0,
        body: bytes = b"",
    ) -> None:
        self._files.setdefault(course_id, []).append(
            {
                "id": file_id,
                "display_name": display_name,
                "content_type": content_type,
                "size": size,
            }
        )
        self._file_bodies[(course_id, file_id)] = body

    def add_page(
        self,
        course_id: str,
        *,
        page_url: str = "",
        url_slug: str = "",
        title: str,
        body: str = "",
    ) -> None:
        slug = url_slug or page_url
        self._pages.setdefault(course_id, []).append(
            {"url_slug": slug, "title": title, "body": body}
        )

    def add_announcement(
        self,
        course_id: str,
        *,
        announcement_id: str,
        title: str,
        message: str = "",
        posted_at: str = "",
        author: str = "",
    ) -> None:
        self._announcements.setdefault(course_id, []).append(
            {
                "id": announcement_id,
                "title": title,
                "message": message,
                "posted_at": posted_at,
                "author": author,
            }
        )

    # -- API-like content fetchers (called by CanvasLMSProvider) ------------

    def api_list_modules(self, course_id: str) -> list[dict]:
        return list(self._modules.get(course_id, []))

    def api_list_module_items(self, course_id: str, module_id: str) -> list[dict]:
        return list(self._module_items.get((course_id, module_id), []))

    def api_list_files(self, course_id: str) -> list[dict]:
        return list(self._files.get(course_id, []))

    def api_get_file_body(self, course_id: str, file_id: str) -> bytes | None:
        return self._file_bodies.get((course_id, file_id))

    def api_list_pages(self, course_id: str) -> list[dict]:
        return list(self._pages.get(course_id, []))

    def api_list_announcements(self, course_id: str) -> list[dict]:
        return list(self._announcements.get(course_id, []))

    # -- API-like write methods --------------------------------------------

    def api_create_page(
        self, course_id: str, *, title: str, body: str
    ) -> dict:
        if course_id not in self._courses:
            return {"success": False, "error": "course not found"}
        slug = title.lower().replace(" ", "-")
        self._pages.setdefault(course_id, []).append(
            {"url_slug": slug, "title": title, "body": body}
        )
        return {"success": True, "url_slug": slug, "id": slug}

    def api_update_page(
        self, course_id: str, *, url_slug: str = "", page_url: str = "", body: str
    ) -> dict:
        slug = url_slug or page_url
        for page in self._pages.get(course_id, []):
            if page.get("url_slug") == slug or page.get("page_url") == slug:
                page["body"] = body
                page["url_slug"] = slug
                return {"success": True}
        return {"success": False, "error": "page not found"}

    def api_post_announcement(
        self, course_id: str, *, title: str, message: str, author: str = ""
    ) -> dict:
        if course_id not in self._courses:
            return {"success": False, "error": "course not found"}
        self._next_announcement_id += 1
        aid = str(self._next_announcement_id)
        self._announcements.setdefault(course_id, []).append(
            {
                "id": aid,
                "title": title,
                "message": message,
                "posted_at": "",
                "author": author,
            }
        )
        return {"success": True, "id": aid}

    def api_update_assignment_description(
        self, course_id: str, *, assignment_id: str, description: str
    ) -> dict:
        for a in self._assignments.get(course_id, []):
            if a["id"] == assignment_id:
                a["description"] = description
                return {"success": True}
        return {"success": False, "error": "assignment not found"}

    def add_enrollment(self, course_id: str, enrollment: dict) -> None:
        self._enrollments.setdefault(course_id, []).append(enrollment)

    def drop_enrollment(self, course_id: str, user_id: str) -> None:
        self._enrollments[course_id] = [
            e for e in self._enrollments.get(course_id, []) if e["user_id"] != user_id
        ]

    # -- API-like methods that the Canvas adapter calls ---------------------

    def api_get_enrollments(self, course_id: str) -> list[dict]:
        return list(self._enrollments.get(course_id, []))

    def api_get_user(self, course_id: str, user_id: str) -> dict | None:
        for e in self._enrollments.get(course_id, []):
            if e["user_id"] == user_id:
                return e
        return None

    def api_put_grade(
        self,
        course_id: str,
        assignment_id: str,
        student_id: str,
        score: float,
        comment: str = "",
    ) -> dict:
        # Check student exists
        user = self.api_get_user(course_id, student_id)
        if user is None:
            return {"error": f"student {student_id} not found", "success": False}

        self._next_submission_id += 1
        sub_id = str(self._next_submission_id)
        sub = {
            "id": sub_id,
            "assignment_id": assignment_id,
            "user_id": student_id,
            "score": score,
            "comment": comment,
        }
        self._submissions.setdefault(course_id, []).append(sub)
        return {"success": True, "id": sub_id}

    def api_create_assignment(
        self,
        course_id: str,
        name: str,
        description: str = "",
        points_possible: float = 100,
        due_at: str = "",
    ) -> dict:
        self._next_assignment_id += 1
        aid = str(self._next_assignment_id)
        assignment = {
            "id": aid,
            "name": name,
            "description": description,
            "points_possible": points_possible,
            "due_at": due_at,
        }
        self._assignments.setdefault(course_id, []).append(assignment)
        return {"success": True, "id": aid}
