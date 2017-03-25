#! python3

"""worker module.

A small library helping you create threaded app. Implemented with event queue
and parent/child pattern.

The document has mention "thread" object multiple times, but it actually leads
to :class:`Worker` instead of builtin :class:`threading.Thread`.
"""

import queue, threading, traceback, time, inspect, weakref, sys

__version__ = "0.7.0"

SHORTCUTS = [
    "bubble", "broadcast", "listen", "unlisten", "wait",
    "wait_forever", "parent_fire", "children_fire", "update", "later",
    "async", "sync", "exit"
]

__all__ = [
    "Event", "Listener", "Worker", "Async", "Channel",
    "current", "is_main", "sleep"
] + SHORTCUTS

class WorkerExit(BaseException):
    """Raise this error to exit current thread. Used by
    :meth:`Worker.stop`.
    """
    pass

class Event:
    """Event interface. Shouldn't use directly."""
    def __init__(
            self, name, data=None, *,
            bubble=False, broadcast=False, target=None
        ):
        """Constructor.

        :param name:      str
        :param data:      any
        :param bubble:    bool. Set to true to bubble through parent thread.

        :param broadcast: bool. Set to true to broadcast through child
                          threads.

        :param target:    :class:`Worker`. If not set, use current thread as
                          the target.
        """

        self.name = name
        self.data = data
        self.target = target

        self.bubble = bubble
        self.broadcast = broadcast

class Listener:
    """Listener interface. Shouldn't use directly."""
    def __init__(self, callback, event_name, *, target=None, priority=0):
        """Constructor.

        :param callback:   function, which would recieve an :class:`Event`
                           object.

        :param event_name: str. Match :attr:`Event.name`.

        :param target:     :class:`Worker` or None. If target is specified, the
                           callback is only invoked if the target thread
                           matches :attr:`Event.target`.

        :param priority:   int. When processing an event, the listeners would
                           be executed in priority order, highest first.
        """
        self.callback = callback
        self.event_name = event_name
        self.target = target
        self.priority = priority

class Worker:
    """Main Worker class"""
    def __init__(self, worker=None, parent=None, daemon=None, print_traceback=True):
        """Constructor.

        :param worker: callable or None. This function is used to overwrite
            :meth:`worker`.

        :param parent: :class:`Worker`, False, or None. The parent thread.

            If parent is None (the default), it will use current
            thread as the parent.

            If parent is False. The thread is parent-less.

        :param daemon: bool or None. Make thread becomes a "daemon thread",
            see also :meth:`is_daemon`.
                       
        :param print_traceback: If True, a traceback will be printed when
            the thread crashed. Note that with workers created by
            :class:`Async`, this flag is set to False.
        """
        self.children = set()
        self.pending = set()

        self.listeners = {}
        self.listener_pool = {}

        self.thread = None
        self.event_que = None
        self.event_cache = None

        self.suspend = False

        self.err = None
        self.ret = None

        self.async_handle = None

        if worker:
            self.worker = worker
            self.node_name = str(worker)
        else:
            self.worker = self.wait_forever
            self.node_name = str(self)

        if parent is None and not WORKER_POOL.is_main():
            parent = WORKER_POOL.current()

        self.parent_node = parent

        if self.parent_node:
            self.parent_node.children.add(self)

        self.daemon = daemon
        
        self.print_traceback = print_traceback

        # listen to builtin event
        @self.listen("STOP_THREAD", priority=-100)
        def _(event):
            raise WorkerExit

        @self.listen("PAUSE_THREAD", priority=-100)
        def _(event):
            if not self.suspend and self.thread:
                self.suspend = True
                self.wait_event("RESUME_THREAD", cache=True)
                self.suspend = False

        @self.listen("CHILD_THREAD_START", priority=100)
        def _(event):
            self.children.add(event.target)

        @self.listen("CHILD_THREAD_END", priority=-100)
        def _(event):
            self.children.remove(event.target)

        @self.listen("WAIT_THREAD_PENDING")
        def _(event):
            self.pending.add(event.target)

        @self.listen("EVENT_REJECT")
        def _(event):
            err_event, err_target = event.data
            if err_event.name == "WAIT_THREAD_PENDING":
                self.fire("WAIT_THREAD_PENDING_DONE", target=err_target)

        @self.listen("EXECUTE")
        def _(event):
            callback, args, kwargs = event.data
            callback(*args, **kwargs)

    def fire(self, event, *args, **kwargs):
        """Dispatch an event.

        :param event: :class:`Event` or str. If event is a str, it would be
                      converted into an Event object by passing all the
                      arguments to the constructor.
        """
        if not isinstance(event, Event):
            event = Event(event, *args, **kwargs)
        if not event.target:
            event.target = current()
        self._que_event(event)
        self._transfer_event(event)
        return self

    def bubble(self, *args, **kwargs):
        """Bubble event through parent. A shortcut to :meth:`parent_fire`,
        with ``bubble=True``.
        """
        kwargs["bubble"] = True
        self.parent_fire(*args, **kwargs)
        return self

    def broadcast(self, *args, **kwargs):
        """Broadcast event through children. A shortcut to
        :meth:`children_fire`, with ``broadcast=True``.
        """
        kwargs["broadcast"] = True
        self.children_fire(*args, **kwargs)
        return self

    def _que_event(self, event):
        """Que the event"""
        try:
            self.event_que.put(event)
        except AttributeError:
            if event.target and event.target is not self:
                event.target.fire("EVENT_REJECT", data=(event, self))

    def _transfer_event(self, event):
        """Bubble or broadcast event"""
        if event.bubble:
            self.parent_fire(event)

        if event.broadcast:
            self.children_fire(event)

    def _process_event(self, event):
        """Deliver the event to listeners."""
        if event.name in self.listeners:
            for listener in self.listeners[event.name]:
                if listener.target is None or listener.target is event.target:
                    try:
                        listener.callback(event)
                    except Exception as err: # pylint: disable=broad-except
                        print("Error occurred in listener: " + self.node_name)
                        traceback.print_exc()
                        self.fire("LISTENER_ERROR", data=err, bubble=True)

    def listen(self, event_name, *args, **kwargs):
        """This is a decorator. Listen/handle specific events. Use it like:

        .. code:: python

            @listen("EVENT_NAME")
            def handler(event):
                # handle event...

        The additional arguments would be passed into :class:`Listener`
        constructor.

        Note that there are some names already taken by this module, they
        includes:

        * ``STOP_THREAD`` - let current thread to stop.
        * ``PAUSE_THREAD`` - let current thread to pause.
        * ``RESUME_THREAD`` - let current thread to resume.
        * ``CHILD_THREAD_START`` - a child thread has started.
        * ``CHILD_THREAD_STOP`` - a child thread has been stopped.
        * ``CHILD_THREAD_DONE`` - a child thread finished.
        * ``CHILD_THREAD_ERROR`` - a child thread failed to finish.
        * ``CHILD_THREAD_END`` - a child thread ended.

        * ``WAIT_THREAD_PENDING`` - some other thread want to wait current
          thread to end.

        * ``WAIT_THREAD_PENDING_DONE`` - the thread current thread waiting
          has ended.

        * ``EVENT_REJECT`` - failed to fire an event. Maybe the thread recieving
          the event is not running.

        * ``EXECUTE`` - let current thread execute a callback.

        * ``LISTENER_ERROR`` - Uncaught error while processing listener. This
          event bubbles up.
        """
        def listen_message(callback):
            """Decorate callback"""
            listener = Listener(callback, event_name, *args, **kwargs)

            if event_name not in self.listeners:
                self.listeners[event_name] = [listener]
            else:
                i = 0
                for t_listener in self.listeners[event_name]:
                    if t_listener.priority < listener.priority:
                        break
                    i += 1
                self.listeners[event_name].insert(i, listener)

            if callback not in self.listener_pool:
                self.listener_pool[callback] = []
            self.listener_pool[callback].append(listener)
            return callback
        return listen_message

    def unlisten(self, callback):
        """Unlisten a callback"""
        for listener in self.listener_pool[callback]:
            self.listeners[listener.event_name].remove(listener)
        del self.listener_pool[callback]

    def is_running(self):
        """Check if the thread is running"""
        return self.thread is not None

    def is_daemon(self):
        """Check if the thread is a daemon thread.

        If the thread is not a daemon, its parent will ensure this thread to
        end (through :meth:`join`) before the end of parent itself.

        If :attr:`self.daemon` flag is not None, return flag value.

        Otherwise, return :meth:`parent.is_daemon`.

        If there is no parent thread, return False.
        """
        if self.daemon is not None:
            return self.daemon

        parent = self.parent_node
        if parent:
            return parent.is_daemon()
        return False

    def wait(self, param, *args, **kwargs):
        """A shortcut method to :meth:`wait_event`, :meth:`wait_thread`, and
        :meth:`wait_timeout`.

        The method is chosen according to the type of the first argument.

        * str - :meth:`wait_event`
        * :class:`Worker` - :meth:`wait_thread`
        * :class:`Async` - just call :meth:`Async.get` on the object.
        * others - :meth:`wait_timeout`
        """
        if isinstance(param, str):
            return self.wait_event(param, *args, **kwargs)
        if isinstance(param, Worker):
            return self.wait_thread(param, *args, **kwargs)
        if isinstance(param, Async):
            return param.get()
        return self.wait_timeout(param)

    def wait_timeout(self, timeout):
        """Wait for timeout (in seconds)"""
        return self.wait_event(None, timeout=timeout)

    def wait_forever(self):
        """Create an infinite event loop."""
        return self.wait_event(None)

    def wait_thread(self, thread):
        """Wait thread to end. Return ``(thread_error, thread_result)``
        tuple.
        """
        thread.fire("WAIT_THREAD_PENDING")
        self.wait_event("WAIT_THREAD_PENDING_DONE", target=thread)
        return (thread.err, thread.ret)

    def wait_event(self, name, timeout=None, target=None, cache=False):
        """Wait for specific event. Return event data.

        :param name:    str. The name of the event.
        :param timeout: int, in seconds. If provided, return None when time up.

        :param target:  :class:`Worker`, If provided, it must match
                        ``event.target``.

        :param cache:   Cache event after being processed. Used by
                        ``PAUSE_THREAD``.
        """
        if timeout:
            end_time = time.time() + timeout
        else:
            end_time = None

        while True:
            try:
                event = self.event_cache.get_nowait()
            except queue.Empty:
                break
            if name == event.name and (not target or target == event.target):
                return event.data
            if end_time and time.time() > end_time:
                return

        if end_time:
            timeout = end_time - time.time()

        while timeout is None or timeout > 0:
            try:
                event = self.event_que.get(timeout=timeout)
                self._process_event(event)
            except queue.Empty:
                # timeup
                return
            if event.name == name and (not target or target == event.target):
                return event.data
            if cache:
                self.event_cache.put(event)
            if end_time:
                timeout = end_time - time.time()

    def parent_fire(self, *args, **kwargs):
        """Dispatch event on parent. See :meth:`fire` for the
        arguments.
        """
        parent = self.parent_node
        if parent:
            kwargs["target"] = self
            self.parent_node.fire(*args, **kwargs)

    def children_fire(self, *args, **kwargs):
        """Dispatch event on children. See :meth:`fire` for the
        arguments.
        """
        kwargs["target"] = self
        for child in self.children.copy():
            child.fire(*args, **kwargs)

    def _wrap_worker(self, *args, **kwargs):
        """Real target sent to threading.Thread."""

        # add to pool
        WORKER_POOL.add(self)

        # tell parent start
        self.parent_fire("CHILD_THREAD_START")

        # execute target
        self.ret = None
        self.err = None

        try:
            self.ret = self.worker(*args, **kwargs)
        except WorkerExit:
            self.parent_fire("CHILD_THREAD_STOP")
        except BaseException as err:
            self.err = err
            if self.print_traceback:
                print("Thread crashed: " + self.node_name)
                traceback.print_exc()
            self.parent_fire("CHILD_THREAD_ERROR", data=err)
        else:
            self.parent_fire("CHILD_THREAD_DONE", data=self.ret)

        # cache some data for later use
        event_que = self.event_que
        native_thread = self.thread

        # mark thread as end
        self.event_que = None
        self.event_cache = None
        self.thread = None

        # cleanup queue
        while True:
            try:
                event = event_que.get_nowait()
                self._process_event(event)
            except queue.Empty:
                break
            except WorkerExit:
                pass
            except BaseException:
                print("Error occured in listener cleanup: " + self.node_name)
                traceback.print_exc()

        # tell parent thread end
        self.parent_fire("CHILD_THREAD_END", data=(self.err, self.ret))

        # tell pending thread end
        for thread in self.pending.copy():
            thread.fire("WAIT_THREAD_PENDING_DONE")
            self.pending.remove(thread)

        # close async handle
        if self.async_handle:
            self.async_handle.stop()
            self.async_handle = None

        # stop childrens
        self._cleanup_children()

        # remove from pool
        WORKER_POOL.remove(native_thread)

    def _cleanup_children(self):
        for child in self.children.copy():
            if child.is_daemon():
                child.stop()
            else:
                child.stop().join()
            self.children.remove(child)

    def update(self):
        """Process all events inside the event queue."""
        while True:
            try:
                event = self.event_que.get_nowait()
                self._process_event(event)
            except queue.Empty:
                break

    def start(self, *args, **kwargs):
        """Start thread. The arguments will be passed into
        :meth:`Worker.worker`.
        """
        if not self.thread:
            self.thread = threading.Thread(
                target=self._wrap_worker,
                daemon=self.daemon,
                args=args,
                kwargs=kwargs
            )
            self.event_que = queue.Queue()
            self.event_cache = queue.Queue()
            self.thread.start()
        return self

    def start_overlay(self, *args, **kwargs):
        """Execute :meth:`Worker.worker`, but overlay on the current thread
        instead of creating a new thread.

        Useful if you want to do some setup and create an event loop on main
        thread.
        """
        if not self.thread:
            self.thread = threading.current_thread()
            self.event_que = queue.Queue()
            self.event_cache = queue.Queue()
            self._wrap_worker(*args, **kwargs)
        return self

    def stop(self):
        """Stop thread"""
        self.fire("STOP_THREAD")
        return self

    def pause(self):
        """Pause thread"""
        self.fire("PAUSE_THREAD")
        return self

    def resume(self):
        """Resume thread"""
        self.fire("RESUME_THREAD")
        return self

    def join(self):
        """Wait thread to stop.

        :meth:`join` is a little different than :meth:`wait_thread`.

        ``thread.join()`` uses native :meth:`threading.Thread.join`, it blocks
        current thread until the thread is stopped.

        ``wait_thread(thread)`` enters an event loop and waiting for an
        ``WAIT_THREAD_PENDING_DONE`` event fired by the thread.
        """
        real_thread = self.thread
        if real_thread:
            real_thread.join()
        return self

    @staticmethod
    def exit():
        """Exit thread"""
        raise WorkerExit

    @staticmethod
    def async(callback, *args, **kwargs):
        """Create Async object. See :class:`Async`."""
        return Async(callback, *args, **kwargs)

    @staticmethod
    def sync(callback, *args, **kwargs):
        """Create Async object but :meth:`Async.get` the result immediately.

        This is useful to put blocking task into a daemon thread.
        """
        return Async(callback, *args, **kwargs).get()

    def later(self, callback, timeout, *args, **kwargs):
        """Create a timer which will resolve later.

        :param callback: callable
        :param timeout:  number, in seconds.

        The additional arguments are passed into the callback function.

        The timer is created in a daemon thread, but the callback is sent back
        to the current thread after the timer expired, and is handled/called
        in a special listener.
        """
        cmd = (callback, args, kwargs)
        Worker(later_worker, daemon=True).start(cmd, timeout, self)

    @classmethod
    def partial(cls, *args, **kwargs):
        """A decorator to convert a function into a Worker object, for
        convenience:

        .. code:: python

            @Worker
            def thread1():
                # do stuff...

            @Worker.partial(daemon=True)
            def thread2():
                # do stuff...

            thread1.start()
            thread2.start()
        """
        def wrapper(func):
            return Worker(func, *args, **kwargs)
        return wrapper

def later_worker(cmd, timeout, handle):
    """Delay worker"""
    sleep(timeout)
    handle.fire("EXECUTE", cmd)

class Async:
    """Async class. Used to create async task."""
    def __init__(self, callback, *args, **kwargs):
        """Constructor.

        :param callback: :class:`Worker` or callable. If callback is not a
                         :class:`Worker`, it would be converted into a daemon,
                         parent-less worker.

        The additional arguments are passed into :meth:`Worker.start`.
        """
        if isinstance(callback, Worker):
            self.thread = callback
        else:
            self.thread = Worker(callback, parent=False, daemon=True, print_traceback=False)
        self.thread.start(*args, **kwargs)

    def get(self):
        """Wait thread to end and return the result. Raise if getting an
        error.
        """
        handle = current()
        handle.async_handle = self.thread
        err, ret = handle.wait_thread(self.thread)
        handle.async_handle = None
        if err:
            raise err
        return ret

class RootWorker(Worker):
    """Root worker. Represent main thread"""
    def __init__(self):
        super().__init__(parent=False)
        self.thread = threading.main_thread()
        self.event_que = queue.Queue()
        self.event_cache = queue.Queue()

    def wait_event(self, *args, **kwargs):
        """Suppress WorkerExit and BaseException"""
        try:
            super().wait_event(*args, **kwargs)
        except WorkerExit:
            self._cleanup_children()
        except BaseException:
            print("Uncaught BaseException in main thread wait_event")
            traceback.print_exc()

    def exit(self):
        """Suppress exit"""
        self._cleanup_children()

class Pool:
    """Worker pool"""
    def __init__(self):
        self.pool = {}
        self.lock = threading.Lock()

    def current(self):
        """Return current worker"""
        with self.lock:
            return self.pool[threading.current_thread()][-1]

    def add(self, node):
        """Add worker to pool"""
        with self.lock:
            if node.thread not in self.pool:
                self.pool[node.thread] = []
            self.pool[node.thread].append(node)

    def remove(self, thread):
        """Remove worker from pool"""
        with self.lock:
            if len(self.pool[thread]) == 1:
                del self.pool[thread]
            else:
                self.pool[thread].pop()

    def is_main(self, thread=None):
        """Check if the thread is the main thread.

        thread - the thread to check. Use current thread if not provided.
        """
        if not thread:
            thread = self.current()
        with self.lock:
            return thread is self.pool[threading.main_thread()][-1]

# init worker pool
WORKER_POOL = Pool()

# init RootWorker
WORKER_POOL.add(RootWorker())

class Channel:
    """Channel class. Used to communicate between threads.

    Events published to the channel are broadcasted to all subscriber
    threads.
    """
    def __init__(self):
        """Constructor."""
        self.pool = weakref.WeakSet()
        self.lock = threading.Lock()

    def sub(self, thread=None):
        """Subscribe to channel.

        thread - the subscriber thread. Use current thread if not provided.
        """
        if thread is None:
            thread = WORKER_POOL.current()
        with self.lock:
            self.pool.add(thread)

    def pub(self, *args, **kwargs):
        """Publish event to channel. See :meth:`Worker.fire` for arguments."""
        with self.lock:
            for thread in self.pool:
                thread.fire(*args, **kwargs)

    def unsub(self, thread=None):
        """Unsubscribe to channel.

        thread - the subscriber thread. Use current thread if not provided.
        """
        if thread is None:
            thread = WORKER_POOL.current()
        with self.lock:
            self.pool.remove(thread)

# define shortcuts
def create_shortcut(key):
    def shortcut(*args, **kwargs):
        return getattr(WORKER_POOL.current(), key)(*args, **kwargs)
    shortcut.__doc__ = "Shortcut to :meth:`current().{key}`".format(key=key)
    return shortcut

for key in SHORTCUTS:
    globals()[key] = create_shortcut(key)
    
def current():
    """Get current thread"""
    return WORKER_POOL.current()

def is_main(thread=None):
    """Check if the thread is the main thread.

    Use current thread if thread is None.
    """
    return WORKER_POOL.is_main(thread)

def sleep(timeout):
    """Shortcut to :meth:`current().wait_timeout`"""
    return WORKER_POOL.current().wait(float(timeout))
