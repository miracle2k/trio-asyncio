# This code implements helper functions that work without running
# a TrioEventLoop.

import trio
import asyncio
import sys

__all__ = ['run_future']


async def run_future(future):
    """Wait for an asyncio future/coroutine from Trio code.

    Cancelling the current Trio scope will cancel the future/coroutine.

    Cancelling the future/coroutine will cause an ``asyncio.CancelledError``.
    """
    task = trio.hazmat.current_task()
    raise_cancel = None

    def done_cb(_):
        trio.hazmat.reschedule(task, trio.hazmat.Result.capture(future.result))

    future.add_done_callback(done_cb)

    def abort_cb(raise_cancel_arg):
        # Save the cancel-raising function
        nonlocal raise_cancel
        raise_cancel = raise_cancel_arg
        # Attempt to cancel our future
        future.cancel()
        # Keep waiting
        return trio.hazmat.Abort.FAILED

    try:
        res = await trio.hazmat.wait_task_rescheduled(abort_cb)
        return res
    except asyncio.CancelledError as exc:
        if raise_cancel is not None:
            try:
                raise_cancel()
            finally:
                # Try to preserve the exception chain,
                # for more detailed tracebacks
                sys.exc_info()[1].__cause__ = exc
        else:
            raise
