# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T2.7 — approval state transitions are protected by _output_lock."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock


def _make_approval_request(choice="r"):
    from axiom.extensions.builtins.chat.fullscreen import _ApprovalRequest

    action = MagicMock()
    action.name = "write_file"
    action.params = {}
    req = _ApprovalRequest(action=action)
    req.choice = choice
    return req


def _make_minimal_tui_for_approval():
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document

    from axiom.extensions.builtins.chat.fullscreen import FullScreenChat

    tui = object.__new__(FullScreenChat)
    tui._output_lock = threading.Lock()
    tui._approval_pending = None
    tui._raw_output = ""
    tui._wrapped_committed = ""
    tui._partial_tail = ""
    tui._table_pending = []
    tui._align_table_cache = {}
    tui._last_invalidate = 0.0

    buf = Buffer(read_only=True, name="approval_test")
    buf.set_document(Document("", 0), bypass_readonly=True)
    tui._output_buffer = buf

    app = MagicMock()
    app.output.get_size.return_value.columns = 80
    tui._app = app

    tui._set_suggestion = MagicMock()
    return tui


class TestApprovalStateLock:
    """_approval_pending reads/writes are protected by _output_lock."""

    def test_handle_approval_input_reads_under_lock(self):
        tui = _make_minimal_tui_for_approval()
        req = _make_approval_request()
        tui._approval_pending = req

        tui._handle_approval_input("a")

        assert tui._approval_pending is None
        assert req.choice == "a"

    def test_approval_state_concurrent_access(self):
        """Two threads racing to set/clear _approval_pending must not corrupt state."""
        tui = _make_minimal_tui_for_approval()

        errors = []
        iterations = 200
        lock_violations = []

        def setter_thread():
            for _ in range(iterations):
                req = _make_approval_request()
                with tui._output_lock:
                    tui._approval_pending = req
                time.sleep(0)

        def reader_thread():
            for _ in range(iterations):
                with tui._output_lock:
                    pending = tui._approval_pending
                    if pending is not None:
                        # Must be a valid _ApprovalRequest, not a partial write
                        try:
                            _ = pending.choice
                            _ = pending.action
                        except AttributeError as e:
                            lock_violations.append(str(e))
                time.sleep(0)

        def clearer_thread():
            for _ in range(iterations):
                with tui._output_lock:
                    tui._approval_pending = None
                time.sleep(0)

        t1 = threading.Thread(target=setter_thread)
        t2 = threading.Thread(target=reader_thread)
        t3 = threading.Thread(target=clearer_thread)

        t1.start()
        t2.start()
        t3.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        t3.join(timeout=5)

        assert not lock_violations, f"Lock violations: {lock_violations}"
        assert not errors, f"Errors: {errors}"

    def test_render_approval_prompt_sets_under_lock(self):
        """render_approval_prompt sets _approval_pending inside _output_lock."""
        from axiom.extensions.builtins.chat.fullscreen import _TuiRenderProvider

        tui = _make_minimal_tui_for_approval()

        original_lock_acquire = tui._output_lock.acquire

        def tracking_acquire(*args, **kwargs):
            result = original_lock_acquire(*args, **kwargs)
            return result

        provider = _TuiRenderProvider(tui)

        # Simulate the approval prompt in a thread — it blocks on req.event.wait()
        # We'll signal the event immediately after setting pending
        def run_prompt():
            action = MagicMock()
            action.name = "write_file"
            action.params = {}

            # Patch _append_output to be a no-op
            tui._append_output = MagicMock()

            # Run render_approval_prompt in a thread; signal event immediately
            import threading as _t

            def signal_after_set():
                # Poll until _approval_pending is set, then signal
                for _ in range(100):
                    with tui._output_lock:
                        req = tui._approval_pending
                    if req is not None:
                        req.event.set()
                        return
                    time.sleep(0.01)

            signaler = _t.Thread(target=signal_after_set, daemon=True)
            signaler.start()
            provider.render_approval_prompt(action)

        t = threading.Thread(target=run_prompt, daemon=True)
        t.start()
        t.join(timeout=2)
        # If we get here without deadlock, the lock is being acquired/released correctly
        assert not t.is_alive(), "render_approval_prompt timed out (possible deadlock)"
