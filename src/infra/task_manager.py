"""TaskGroup-based background job manager with concurrency limits."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)


class TaskManager:
    """Manages background job processing with concurrency limits and graceful shutdown."""

    def __init__(self, max_concurrent: int = 8) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._shutdown_event = asyncio.Event()

    async def submit(self, job_id: str, coro: Coroutine[None, None, None]) -> None:
        """Submit a job for processing. Tracks the task and respects concurrency limits."""
        task = asyncio.create_task(self._run_with_semaphore(job_id, coro))
        self._active_tasks[job_id] = task
        task.add_done_callback(lambda _t: self._active_tasks.pop(job_id, None))

    async def _run_with_semaphore(self, job_id: str, coro: Coroutine[None, None, None]) -> None:
        async with self._semaphore:
            if self._shutdown_event.is_set():
                logger.warning("Skipping job %s due to shutdown", job_id)
                # ``coro`` was created by the caller but never awaited on this
                # branch. Returning without closing it leaves an un-started
                # coroutine to be garbage-collected, which emits
                # "coroutine ... was never awaited" — a RuntimeWarning on the
                # shutdown path, i.e. exactly where the logs matter most and
                # where ``-W error`` turns it into a second failure on top of
                # the drop. ``close()`` finalises it silently instead.
                coro.close()
                return
            try:
                await coro
            except Exception:
                logger.exception("Background job %s failed", job_id)

    async def cancel(self, job_id: str) -> bool:
        """Cancel a specific job and wait for it to actually stop.

        ``task.cancel()`` is fire-and-forget — it schedules a
        ``CancelledError`` but the task may still be running its cancel
        handler when the call returns. Awaiting the task here lets the
        caller return only after the background task has fully unwound,
        avoiding a 200-while-still-uploading ghost-write window.
        """
        task = self._active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # The done_callback that pops from ``_active_tasks`` is
            # scheduled via ``loop.call_soon`` and hasn't fired yet when
            # ``await task`` returns. Remove the entry explicitly so
            # ``active_count`` reflects reality immediately; the
            # callback is idempotent via ``pop(..., None)``.
            self._active_tasks.pop(job_id, None)
            return True
        return False

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Graceful shutdown: stop accepting, wait for active, cancel stragglers."""
        self._shutdown_event.set()
        if self._active_tasks:
            tasks = list(self._active_tasks.values())
            logger.info("Waiting for %d active tasks to complete...", len(tasks))
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5.0)
            # A cancelled straggler is an upload that was dropped mid-flight,
            # not routine shutdown chatter -- surface it at WARNING so it is
            # visible under the fleet's LOG_LEVEL=WARNING.
            log = logger.warning if pending else logger.info
            log("Shutdown complete: %d done, %d cancelled", len(done), len(pending))

    @property
    def active_count(self) -> int:
        return len(self._active_tasks)
