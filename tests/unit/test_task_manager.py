"""Tests for the background task manager."""

from __future__ import annotations

import asyncio
import gc
import inspect
import warnings

import pytest

from src.infra.task_manager import TaskManager


@pytest.mark.unit
class TestTaskManager:
    @pytest.mark.asyncio
    async def test_submit_runs_coroutine(self) -> None:
        tm = TaskManager(max_concurrent=2)
        result: list[str] = []

        async def job():
            result.append("done")

        await tm.submit("j1", job())
        # Give the event loop a tick to run the task
        await asyncio.sleep(0.05)
        assert result == ["done"]
        assert tm.active_count == 0

    @pytest.mark.asyncio
    async def test_active_count(self) -> None:
        tm = TaskManager(max_concurrent=4)
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_job():
            started.set()
            await release.wait()

        await tm.submit("j1", blocking_job())
        await started.wait()
        assert tm.active_count == 1

        release.set()
        await asyncio.sleep(0.05)
        assert tm.active_count == 0

    @pytest.mark.asyncio
    async def test_cancel_running_task(self) -> None:
        """``cancel()`` must return only after the task has FULLY unwound.

        ``active_count == 0`` alone cannot prove this: ``cancel()`` pops from
        ``_active_tasks`` unconditionally, so the registry empties even for a
        bare fire-and-forget ``task.cancel()`` that leaves the upload thread
        still publishing — the 200-while-still-uploading ghost-write window.
        This test therefore observes the *task object* and a cancel handler
        that yields: neither can have finished unless ``cancel()`` awaited.
        """
        tm = TaskManager(max_concurrent=2)
        started = asyncio.Event()
        unwound: list[str] = []

        async def long_job():
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                # A cleanup step that suspends. A bare ``task.cancel()``
                # returns before the task has even received its
                # CancelledError, so this cannot have run yet.
                await asyncio.sleep(0)
                unwound.append("cleanup-done")

        await tm.submit("j1", long_job())
        await started.wait()
        task = tm._active_tasks["j1"]

        assert await tm.cancel("j1") is True

        # The task itself is finished — not merely deregistered.
        assert task.done(), "cancel() returned while the background task was still running"
        assert task.cancelled()
        # ...and its cancel handler ran to completion before cancel() returned.
        assert unwound == ["cleanup-done"], (
            "cancel() returned before the job's cleanup finished: a caller that "
            "reports 200 here can still be mid-upload (ghost-write window)"
        )
        assert tm.active_count == 0

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_false(self) -> None:
        tm = TaskManager(max_concurrent=2)
        assert await tm.cancel("no-such-job") is False

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self) -> None:
        tm = TaskManager(max_concurrent=1)
        order: list[str] = []
        gate = asyncio.Event()

        async def job_a():
            order.append("a-start")
            gate.set()
            await asyncio.sleep(0.1)
            order.append("a-end")

        async def job_b():
            order.append("b-start")

        await tm.submit("a", job_a())
        await gate.wait()
        await tm.submit("b", job_b())
        await asyncio.sleep(0.2)

        # b-start must come after a-end (semaphore=1)
        assert order.index("a-end") < order.index("b-start")

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_active(self) -> None:
        tm = TaskManager(max_concurrent=4)
        completed = False

        async def slow_job():
            nonlocal completed
            await asyncio.sleep(0.1)
            completed = True

        await tm.submit("j1", slow_job())
        await asyncio.sleep(0.01)  # let the task acquire the semaphore and start
        await tm.shutdown(timeout=5.0)
        assert completed is True
        assert tm.active_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_cancels_stragglers(self) -> None:
        tm = TaskManager(max_concurrent=4)

        async def forever_job():
            await asyncio.sleep(999)

        await tm.submit("j1", forever_job())
        await asyncio.sleep(0.01)
        await tm.shutdown(timeout=0.05)
        assert tm.active_count == 0

    @pytest.mark.asyncio
    async def test_shutdown_skips_new_jobs(self) -> None:
        tm = TaskManager(max_concurrent=4)
        ran = False

        async def job() -> None:
            nonlocal ran
            ran = True

        await tm.shutdown(timeout=1.0)
        coro = job()
        try:
            await tm.submit("j1", coro)
            await asyncio.sleep(0.05)
            assert ran is False
        finally:
            # If submit() declined the coroutine (post-shutdown), close it
            # explicitly so we don't emit an "unawaited coroutine" warning.
            coro.close()

    @pytest.mark.asyncio
    async def test_failed_job_does_not_crash_manager(self) -> None:
        tm = TaskManager(max_concurrent=2)

        async def bad_job():
            raise RuntimeError("boom")

        await tm.submit("j1", bad_job())
        await asyncio.sleep(0.05)
        assert tm.active_count == 0
        # Manager still works
        result: list[str] = []

        async def good_job():
            result.append("ok")

        await tm.submit("j2", good_job())
        await asyncio.sleep(0.05)
        assert result == ["ok"]


@pytest.mark.unit
class TestShutdownDoesNotLeakCoroutines:
    """A job that is dropped because shutdown started must have its coroutine
    finalised. Returning without awaiting or closing it leaves an un-started
    coroutine for the GC, which emits 'coroutine ... was never awaited' — a
    RuntimeWarning raised on the shutdown path, and a hard error under -W error.
    """

    @pytest.mark.asyncio
    async def test_skipped_job_coroutine_is_closed_not_leaked(self) -> None:
        tm = TaskManager(max_concurrent=1)
        ran: list[str] = []

        async def job():
            ran.append("ran")

        coro = job()
        tm._shutdown_event.set()

        await tm.submit("j-shutdown", coro)
        await asyncio.sleep(0.05)

        # Dropped, as intended...
        assert ran == []
        # ...and finalised, not abandoned. CORO_CREATED here means the
        # coroutine is still awaiting garbage collection and WILL warn.
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
        assert tm.active_count == 0

    @pytest.mark.asyncio
    async def test_skipped_job_emits_no_never_awaited_warning(self) -> None:
        tm = TaskManager(max_concurrent=1)

        # Distinctively named so the assertion below can filter on OUR
        # coroutine. ``gc.collect()`` finalises everything the interpreter has
        # pending, so an unrelated test that leaked a coroutine earlier in the
        # session lands its warning in this window too — asserting on "any
        # never-awaited warning" made this test fail for someone else's leak
        # (observed once in a full-suite run before this filter was added).
        async def _uniquely_named_skipped_job():
            pass

        coro = _uniquely_named_skipped_job()
        tm._shutdown_event.set()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await tm.submit("j-warn", coro)
            await asyncio.sleep(0.05)
            del coro
            gc.collect()

        leaked = [
            w
            for w in caught
            if "was never awaited" in str(w.message)
            and "_uniquely_named_skipped_job" in str(w.message)
        ]
        assert leaked == [], f"leaked coroutine warning: {leaked}"
