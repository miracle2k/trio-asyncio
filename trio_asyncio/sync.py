import trio
import attr
import asyncio
import threading
import traceback

from functools import partial

from .base import BaseTrioEventLoop
from .handles import Handle
from .acall import AsyncWorker,AsyncJob

import logging
logger = logging.getLogger(__name__)

async def _sync(proc, *args):
    return proc(*args)

class NoStartAsyncJob(AsyncJob):
    pass

class LoopAsyncWorker(AsyncWorker):
    def __init__(self, main, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.main = main
    async def run_job(self, job):
        print("RUN",job,isinstance(self,NoStartAsyncJob),self.main._stopped.is_set())
        if not isinstance(job,NoStartAsyncJob) and self.main._stopped.is_set():
            await self.nursery.start(self.main._main_loop)
        await super().run_job(job)

class SyncTrioEventLoop(BaseTrioEventLoop):
    """
    This is the "compatibility mode" implementation of the Trio/asyncio
    event loop. It runs async code in a separate thread.

    For best results, you should switch to the asynchronous :class:`TrioEventLoop`.
    """

    _thread = None
    _trio_exc = None

    def __init__(self, **kw):
        super().__init__(**kw)

        # for exchanging operations between the threads
        self._trio_worker = None # AsyncRunner for the Trio thread
        self._main_worker = None # AsyncRunner for the main thread

        # sync thread startup and shutdown
        self._startup_done = threading.Event()
        self._stop_done = threading.Event()
        self._stop_done.set()

        # flag to shut down the thread
        self._stop_thread = trio.Event()

        # Synchronization
        self._some_deferred = 0
        #self._stop_count = 0

    async def _main_loop(self, *args,**kwargs):
        self._trio_worker.sync_off()
        try:
            await super()._main_loop(*args,**kwargs)
        finally:
            if self._trio_worker is not None:
                self._trio_worker.sync_on()

    def stop(self, final=False):
        """Halt the main loop.

        If this method is called from the main thread, it will wait until
        the loop is stopped.
        """
        if self._thread is None or (not final and self._stopped.is_set()):
            return

        def kick():
            if final:
                self._stop_thread.set()
            else:
                self._stop_done.set()
            raise StopIteration
        async def stop_me():
            h = trio.Event()
            def kick_():
                h.set()
                kick()
            self._queue_handle(Handle(kick_,(),self,True))
            await h.wait()
        if threading.current_thread() != self._thread:
            assert not final
            self._trio_worker.run_soon(stop_me)
            self._stop_done.wait()
        else:
            self._queue_handle(Handle(kick,(),self,True))

    async def _run_in_job(self, proc, *args):
        f = asyncio.Future()

    def _queue_handle(self, handle):
        self._check_closed()
        if "_call_connection_lost" in repr(handle):
            traceback.print_stack()
        def put(self,handle):
            self._some_deferred -= 1
            self._q.put_nowait(handle)
            
        # If we don't have a token, the main loop is not yet running
        # thus we can't have a race condition.
        # 
        # On the other hand, if a request has been submitted (but not yet
        # processed) through self._token, any other requestss also must be
        # sent that way, otherwise they'd overtake each other.
        if self._token is not None and (self._some_deferred or threading.current_thread() != self._thread):
            self._some_deferred += 1
            self._token.run_sync_soon(put,self, handle)
        else:
            self._q.put_nowait(handle)
        return handle

    def run_forever(self):
        self.__start_loop()
        if self._thread == threading.current_thread() or self._main_worker is not None:
            raise RuntimeError("You can't nest calls to run_until_complete()/run_forever().")

        async def delegate():
            async with trio.open_nursery() as nursery:
                async with AsyncWorker(nursery) as worker:
                    try:
                        self._main_worker = worker
                        return await self._trio_worker.run(self.wait_stopped)
                    finally:
                        self._main_worker = None
        return trio.run(delegate)

    def is_running(self):
        if self._closed:
            return False
        return self._thread is not None

    def time(self):
        if self._task is None:
            self.__start_loop()
        return super().time()

    def _add_reader(self, fd, callback, *args):
        if self._thread is None or self._thread == threading.current_thread():
            super()._add_reader(fd, callback, *args)
        else:
            self._trio_worker.run_soon(super()._add_reader, fd, callback, *args, sync=True, timeout=999)

    def _remove_reader(self, fd):
        if self._thread is None or self._thread == threading.current_thread():
            super()._remove_reader(fd)
        else:
            self._trio_worker.run_soon(super()._remove_reader, fd, sync=True, timeout=999)

    def _add_writer(self, fd, callback, *args):
        if self._thread is None or self._thread == threading.current_thread():
            super()._add_writer(fd, callback, *args)
        else:
            self._trio_worker.run_soon(super()._add_writer, fd, callback, *args, sync=True, timeout=999)
        
    def _remove_writer(self, fd):
        if self._thread is None or self._thread == threading.current_thread():
            super()._remove_writer(fd)
        else:
            self._trio_worker.run_soon(super()._remove_writer, fd, sync=True, timeout=999)
        
    def run_until_complete(self, future):
        """Run until the Future is done.

        If the argument is a coroutine, it is wrapped in a Task.

        WARNING: It would be disastrous to call run_until_complete()
        with the same coroutine twice -- it would wrap it in two
        different Tasks and that can't be good.

        Return the Future's result, or raise its exception.
        """

        self.__start_loop()
        if self._thread == threading.current_thread() or self._main_worker is not None:
            raise RuntimeError("You can't nest calls to run_until_complete()/run_forever().")

        async def _stop_wait():
            self.stop()
            await self.wait_stopped()

        async def delegate():
            async with trio.open_nursery() as nursery:
                async with AsyncWorker(nursery) as worker:
                    try:
                        self._main_worker = worker
                        return await self._trio_worker.run(self._run_coroutine, future)
                    finally:
                        await self._trio_worker.run(_stop_wait)
                        self._main_worker = None
        return trio.run(delegate)

    async def _run_coroutine(self, future):
        """Helper for run_until_complete().

        We need to make sure that a RuntimeError is raised if the loop is stopped
        before the future completes.

        This runs in the trio thread.
        """
        done = trio.Event()
        result = None
        future = asyncio.ensure_future(future, loop=self)

        def is_done(_):
            nonlocal result

            result = trio.hazmat.Result.capture(future.result)
            done.set()
        future.add_done_callback(is_done)

        async def monitor_stop(task_status=trio.TASK_STATUS_IGNORED):
            nonlocal result

            task_status.started()
            await self.wait_stopped()
            result = trio.hazmat.Error(RuntimeError('Event loop stopped before Future completed.'))
            done.set()

        async with trio.open_nursery() as nursery:
            await nursery.start(monitor_stop)
            await done.wait()
            future.remove_done_callback(is_done)
            nursery.cancel_scope.cancel()
            return result.unwrap()

    def __start_loop(self):
        """Make sure that the trio main loop is running."""
        self._check_closed()

        if self._thread is None:
            self._stop_done.clear()
            self._thread = threading.Thread(
                target=trio.run,
                args=(self.__trio_thread_main,))
            self._thread.start()
            self._startup_done.wait()
            if self._stop_done.is_set():
                raise RuntimeError("could not start the trio main loop") from self._trio_exc
            trio.child_watcher(sync=True)

    async def __trio_thread_main(self):
        """This is the main code of the trio-asyncio main loop.
        It will start an AsyncWorker and run while 
        """
        try:
            async with trio.open_nursery() as nursery:
                async with LoopAsyncWorker(self,nursery, sync=True) as worker:
                    try:
                        self._trio_worker = worker

                        asyncio.set_event_loop(self)
                        await self._main_loop_init(nursery)
                        await nursery.start(self._main_loop)
                        self._startup_done.set()
                        worker.sync_on()
                        print("MAIN G")
                        await self._stop_thread.wait()
                        self._startup_done.clear()
                    finally:
                        self._trio_worker = None
                        self.stop(final=True)
                        await self.wait_stopped()
                        await self._main_loop_exit()
                        self._stop_done.set()
                        self._thread = None
        except BaseException as exc:
            self._trio_exc = exc
            logger.exception("Trio thread main loop")
            self._stop_done.set()
            self._startup_done.set()
            self._thread = None
            raise

    def __in_main_thread(self, proc, *args):
        if self._thread == threading.current_thread():
            self._main_worker.run_soon(proc, *args)
        else:
            if not self._closed:
                self.__start_loop()
            return proc(*args)
        
    def add_signal_handler(self, sig, callback, *args):
        """Signals must be added from the main thread."""
        return self.__in_main_thread(super().add_signal_handler, sig, callback, *args)

    def remove_signal_handler(self, sig):
        """Signals must be removed in the main thread."""
        return self.__in_main_thread(super().remove_signal_handler, sig)

    def __enter__(self):
        if self._thread is not None:
            raise RuntimeError("This loop is already running.")
        self.__start_loop()
        
    def __exit__(self, *tb):
        self.stop()
        self.close()
        assert self._thread is None

    def close(self):
        super().close()

    def _close(self):
        """Hook to terminate the thread"""
        print("CL A")
        if self._thread is not None:
            if self._thread == threading.current_thread():
                raise RuntimeError("You can't close a sync loop from the inside")
            print("CL B")
            self._trio_worker.run_soon(self._stop_thread.set)
            print("CL C")
            self._thread.join()
            print("CL D")
            self._thread = None
        print("CL E")
        super()._close()
        print("CL F")

