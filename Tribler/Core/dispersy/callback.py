# Python 2.5 features
from __future__ import with_statement

"""
A callback thread running Dispersy.
"""

from heapq import heappush, heappop
from thread import get_ident
from threading import Thread, Lock, Event
from time import sleep, time
from types import GeneratorType
from dprint import dprint

if __debug__:
    import atexit
    # dprint warning when registered call, or generator call, takes more than N seconds
    CALL_DELAY_FOR_WARNING = 0.5
    # dprint warning when registered call, or generator call, should have run N seconds ago
    QUEUE_DELAY_FOR_WARNING = 1.0

class Yielder(object):
    pass

class Delay(Yielder):
    def __init__(self, delay):
        assert isinstance(delay, float)
        assert delay > 0.0
        self._delay = delay

    def handle(self, cself, requests, expired, actual_time, deadline, priority, root_id, call, callback):
        heappush(requests, (deadline + self._delay), priority, root_id, (call[0], "desync"), callback)

class Switch(Yielder):
    def __init__(self, cother):
        assert isinstance(cother, Callback)
        self._cother = cother

    def handle(self, cself, requests, expired, actual_time, deadline, priority, root_id, call, callback):
        with self._cother._lock:
            if not isinstance(root_id, str):
                self._cother._id += 1
                root_id = self._cother._id
            self._cother._new_actions.append(("register", (deadline, priority, root_id, call, callback)))

            # wakeup if sleeping
            self._cother._event.set()

class Idle(Yielder):
    def __init__(self, max_delay=300.0):
        assert isinstance(max_delay, float)
        assert max_delay > 0.0
        self._max_delay = max_delay

    def handle(self, cself, requests, expired, actual_time, deadline, priority, root_id, call, callback):
        self._expired = expired
        self._origional_priority = priority
        self._origional_root_id = root_id
        self._origional_generator = call[0]
        self._origional_callback = callback
        generator = self._take_step(actual_time - deadline)
        generator.send(None)
        heappush(expired, (0, deadline, root_id, (generator, "desync"), None))

    def _take_step(self, desync):
        while self._max_delay > 0.0:
            desync = (yield desync)
            if desync < 0.1:
                break
            self._max_delay -= desync
        heappush(self._expired, (self._origional_priority, time(), self._origional_root_id, (self._origional_generator, None), self._origional_callback))

class Callback(object):
    def __init__(self):
        # _event is used to wakeup the thread when new actions arrive
        self._event = Event()

        # _lock is used to protect variables that are written to on multiple threads
        self._lock = Lock()

        # _thread_ident is used to detect when methods are called from the same thread
        self._thread_ident = 0

        # _state contains the current state of the thread.  it is protected by _lock and follows the
        # following states:
        #
        #                                 -> fatal-exception -> STATE_EXCEPTION
        #                                /
        # STATE_INIT -> start() -> STATE_RUNNING
        #                                \
        #                                 -> stop() -> PLEASE_STOP -> STATE_FINISHED
        self._state = "STATE_INIT"
        if __debug__: dprint("STATE_INIT")

        # _exception is set to SystemExit, KeyboardInterrupt, GeneratorExit, or AssertionError when
        # any of the registered callbacks raises any of these exceptions.  in this case _state will
        # be set to STATE_EXCEPTION.  it is protected by _lock
        self._exception = None

        # _id contains a running counter to ensure that every scheduled callback has its own unique
        # identifier.  it is protected by _lock
        self._id = 0

        # _new_actions contains a list of actions that must be handled on the running thread.  it is
        # protected by _lock
        self._new_actions = []  # (type, action)
                                # type=register, action=(deadline, priority, root_id, (call, args, kargs), callback)
                                # type=unregister, action=root_id

        if __debug__:
            def must_close(callback):
                assert callback.is_finished
            atexit.register(must_close, self)

    @property
    def is_running(self):
        """
        Returns True when the state is STATE_RUNNING.
        """
        return self._state == "STATE_RUNNING"

    @property
    def is_finished(self):
        """
        Returns True when the state is either STATE_FINISHED or STATE_EXCEPTION.  In either case the
        thread is no longer running.
        """
        return self._state == "STATE_FINISHED" or self._state == "STATE_EXCEPTION"

    @property
    def exception(self):
        """
        Returns the exception that caused the thread to exit when when any of the registered callbacks
        raises either SystemExit, KeyboardInterrupt, GeneratorExit, or AssertionError.
        """
        return self._exception

    def register(self, call, args=(), kargs=None, delay=0.0, priority=0, id_="", callback=None, callback_args=(), callback_kargs=None):
        assert callable(call), "CALL must be callable"
        assert isinstance(args, tuple), "ARGS has invalid type: %s" % type(args)
        assert kargs is None or isinstance(kargs, dict), "KARGS has invalid type: %s" % type(kargs)
        assert isinstance(delay, (int, float)), "DELAY has invalid type: %s" % type(delay)
        assert isinstance(priority, int), "PRIORITY has invalid type: %s" % type(priority)
        assert isinstance(id_, str), "ID_ has invalid type: %s" % type(id_)
        assert callback is None or callable(callback), "CALLBACK must be None or callable"
        assert isinstance(callback_args, tuple), "CALLBACK_ARGS has invalid type: %s" % type(callback_args)
        assert callback_kargs is None or isinstance(callback_kargs, dict), "CALLBACK_KARGS has invalid type: %s" % type(callback_kargs)
        if __debug__: dprint("register ", call, " after ", delay, " seconds")
        with self._lock:
            if not id_:
                self._id += 1
                id_ = self._id
            self._new_actions.append(("register", (delay + time(),
                                                   512 - priority,
                                                   id_,
                                                   (call, args, {} if kargs is None else kargs),
                                                   None if callback is None else (callback, callback_args, {} if callback_kargs is None else callback_kargs))))
            # wakeup if sleeping
            self._event.set()
            return id_

    def persistent_register(self, id_, call, args=(), kargs=None, delay=0.0, priority=0, callback=None, callback_args=(), callback_kargs=None):
        """
        Register a callback only if ID_ has not already been registered.

        Example:
         > callback.persistent_register("my-id", my_func, ("first",), delay=60.0)
         > callback.persistent_register("my-id", my_func, ("second",))
         > -> my_func("first") will be called after 60 seconds, my_func("second") will not be called at all

        Example:
         > callback.register("my-id", my_func, ("first",), delay=60.0)
         > callback.persistent_register("my-id", my_func, ("second",))
         > -> my_func("first") will be called after 60 seconds, my_func("second") will not be called at all
        """
        assert isinstance(id_, str), "ID_ has invalid type: %s" % type(id_)
        assert id_, "ID_ may not be an empty string"
        assert hasattr(call, "__call__"), "CALL must be callable"
        assert isinstance(args, tuple), "ARGS has invalid type: %s" % type(args)
        assert kargs is None or isinstance(kargs, dict), "KARGS has invalid type: %s" % type(kargs)
        assert isinstance(delay, float), "DELAY has invalid type: %s" % type(delay)
        assert isinstance(priority, int), "PRIORITY has invalid type: %s" % type(priority)
        assert callback is None or callable(callback), "CALLBACK must be None or callable"
        assert isinstance(callback_args, tuple), "CALLBACK_ARGS has invalid type: %s" % type(callback_args)
        assert callback_kargs is None or isinstance(callback_kargs, dict), "CALLBACK_KARGS has invalid type: %s" % type(callback_kargs)
        if __debug__: dprint("reregister ", call, " after ", delay, " seconds")
        with self._lock:
            self._new_actions.append(("persistent-register", (delay + time(),
                                                              512 - priority,
                                                              id_,
                                                              (call, args, {} if kargs is None else kargs),
                                                              None if callback is None else (callback, callback_args, {} if callback_kargs is None else callback_kargs))))
            # wakeup if sleeping
            self._event.set()
            return id_

    def unregister(self, id_):
        """
        Unregister a callback using the ID_ obtained from the register(...) method
        """
        assert isinstance(id_, (str, int)), "ROOT_ID has invalid type: %s" % type(id_)
        if __debug__: dprint(id_)
        with self._lock:
            self._new_actions.append(("unregister", id_))

    def start(self, name="Generic-Callback", wait=True):
        """
        Start the asynchronous thread.

        Creates a new thread and calls the _loop() method.
        """
        assert self._state == "STATE_INIT", "Already (done) running"
        assert isinstance(name, str)
        assert isinstance(wait, bool), "WAIT has invalid type: %s" % type(wait)
        if __debug__: dprint()
        thread = Thread(target=self._loop, name=name)
        thread.daemon = True
        thread.start()

        if wait:
            # Wait until the thread has started
            while self._state == "STATE_INIT":
                sleep(0.01)

    def stop(self, timeout=10.0, wait=True, exception=None):
        """
        Stop the asynchronous thread.

        When called with wait=True on the same thread we will return immediately.
        """
        assert isinstance(timeout, float)
        assert isinstance(wait, bool)
        if __debug__: dprint()
        if self._state == "STATE_RUNNING":
            with self._lock:
                if exception:
                    self._exception = exception
                self._state = "STATE_PLEASE_STOP"
                if __debug__: dprint("STATE_PLEASE_STOP")

                # wakeup if sleeping
                self._event.set()

            if wait and not self._thread_ident == get_ident():
                while self._state == "STATE_PLEASE_STOP" and timeout > 0.0:
                    sleep(0.01)
                    timeout -= 0.01

                if __debug__:
                    if timeout <= 0.0:
                        dprint("timeout.  perhaps callback.stop() was called on the same thread?")

        return self._state == "STATE_FINISHED" or self._state == "STATE_EXCEPTION"

    def _loop(self):
        if __debug__: dprint()

        # put some often used methods and object in the local namespace
        get_timestamp = time
        lock = self._lock
        new_actions = self._new_actions

        # the timestamp that the callback is currently handling
        actual_time = 0
        # requests are ordered by deadline and moved to -expired- when they need to be handled
        requests = [] # (deadline, priority, root_id, (call, args, kargs), callback)
        # expired requests are ordered and handled by priority
        expired = [] # (priority, deadline, root_id, (call, args, kargs), callback)

        self._thread_ident = get_ident()

        with lock:
            assert self._state == "STATE_INIT"
            self._state = "STATE_RUNNING"
            if __debug__: dprint("STATE_RUNNING")

        while self._state == "STATE_RUNNING":
            with lock:
                # schedule all new actions
                for type_, action in new_actions:
                    if type_ == "register":
                        heappush(requests, action)
                    elif type_ == "persistent-register":
                        for tup in chain(requests, expired):
                            if tup[2] == action[2]:
                                break
                        else:
                            # no break, register callback
                            heappush(requests, action)
                    else:
                        assert type_ == "unregister"
                        if __debug__: dprint("unregister ", len([request for request in requests if request[2] == action]), ":", len([request for request in expired if request[2] == action]), " from requests ", action)
                        requests = [request for request in requests if not request[2] == action]
                        expired = [request for request in expired if not request[2] == action]
                del new_actions[:]
                self._event.clear()

            actual_time = get_timestamp()

            # move expired requests from REQUESTS to EXPIRED
            while requests and requests[0][0] <= actual_time:
                # notice that the deadline and priority entries are swiched, hence, the entries in
                # the EXPIRED list are ordered by priority instead of deadline
                deadline, priority, root_id, call, callback = heappop(requests)
                heappush(expired, (priority, deadline, root_id, call, callback))

            if expired:
                if __debug__:
                    for counter, (deadline, _, _, call, _) in enumerate(requests):
                        desync = actual_time - deadline
                        level = "error" if desync > 0.0 else "normal"
                        dprint("%2d" % counter, " queue waiting %.4fs" % desync, " for request ", call[0], level=level)

                    for counter, (_, deadline, _, call, _) in enumerate(expired):
                        desync = actual_time - deadline
                        level = "warning" if desync > QUEUE_DELAY_FOR_WARNING else "normal"
                        dprint("%2d" % counter, " queue desync  %.4fs" % desync, " for expired ", call[0], level=level)

                # we need to handle the next call in line
                priority, deadline, root_id, call, callback = heappop(expired)

                while True:
                    # call can be either:
                    # 1. A (generator, arg)
                    # 2. A (callable, args, kargs) tuple

                    if isinstance(call[0], GeneratorType):
                        try:
                            # start next generator iteration
                            if __debug__:
                                debug_begin = get_timestamp()
                            result = call[0].send(actual_time - deadline if call[1] == "desync" else call[1])
                        except StopIteration:
                            if callback:
                                heappush(expired, (priority, deadline, root_id, (callback[0], (result,) + callback[1], callback[2]), None))
                        except (SystemExit, KeyboardInterrupt, GeneratorExit, AssertionError), exception:
                            dprint(exception=True, level="error")
                            with lock:
                                self._state = "STATE_EXCEPTION"
                                self._exception = exception
                        except Exception, exception:
                            if callback:
                                heappush(expired, (priority, deadline, root_id, (callback[0], (exception,) + callback[1], callback[2]), None))
                            dprint(exception=True, level="error")
                        else:
                            if isinstance(result, float):
                                # schedule CALL again in RESULT seconds
                                # equivalent to: yield Delay(SECONDS)
                                assert result >= 0.0
                                heappush(requests, (deadline + result, priority, root_id, (call[0], "desync"), callback))
                            elif isinstance(result, Yielder):
                                # let the Yielder object handle everything
                                result.handle(self, requests, expired, actual_time, deadline, priority, root_id, call, callback)
                            else:
                                dprint("yielded invalid type ", type(result), level="error")
                        finally:
                            if __debug__:
                                debug_delay = get_timestamp() - debug_begin
                                debug_level = "warning" if debug_delay > CALL_DELAY_FOR_WARNING else "normal"
                                dprint("call took %.4fs to " % debug_delay, call[0], level=debug_level)

                    else:
                        assert callable(call[0])
                        try:
                            # callback
                            if __debug__:
                                debug_begin = get_timestamp()
                            result = call[0](*call[1], **call[2])
                        except (SystemExit, KeyboardInterrupt, GeneratorExit, AssertionError), exception:
                            dprint(exception=True, level="error")
                            with lock:
                                self._state = "STATE_EXCEPTION"
                                self._exception = exception
                        except Exception, exception:
                            if callback:
                                heappush(expired, (priority, deadline, root_id, (callback[0], (exception,) + callback[1], callback[2]), None))
                            dprint(exception=True, level="error")
                        else:
                            if isinstance(result, GeneratorType):
                                # we only received the generator, no actual call has been made to the
                                # function yet, therefore we call it again immediately
                                call = (result, None)
                                continue

                            elif callback:
                                heappush(expired, (priority, deadline, root_id, (callback[0], (result,) + callback[1], callback[2]), None))

                        finally:
                            if __debug__:
                                debug_delay = get_timestamp() - debug_begin
                                debug_level = "warning" if debug_delay > CALL_DELAY_FOR_WARNING else "normal"
                                dprint("call took %.4fs to " % debug_delay, call[0], level=debug_level)

                    # break out of the while loop
                    break

            else:
                # we need to wait for new requests
                if requests:
                    # there are no requests that have to be handled right now. Sleep for a while.
                    if __debug__: dprint("wait at most %.1fs" % min(300.0, requests[0][0] - actual_time), " before next call")
                    self._event.wait(min(300.0, requests[0][0] - actual_time))

                else:
                    # there are no requests on the list, wait till something is scheduled
                    if __debug__: dprint("wait at most 300.0s before next call")
                    self._event.wait(300.0)
                continue

        # send GeneratorExit exceptions to remaining generators
        for _, _, _, call, _ in expired + requests:
            if isinstance(call[0], GeneratorType):
                if __debug__: dprint("raise Shutdown in ", call[0])
                try:
                    call[0].close()
                except:
                    dprint(exception=True, level="error")

        # set state to finished
        with lock:
            if __debug__: dprint("STATE_FINISHED")
            self._state = "STATE_FINISHED"

if __debug__:
    if __name__ == "__main__":
        c = Callback()
        c.start()
        d = Callback()
        d.start()

        def call():
            dprint(time())

        sleep(2)
        dprint(time())
        c.register(call, delay=1.0)

        sleep(2)
        dprint(line=1)

        def call():
            delay = 3.0
            for i in range(10):
                dprint(time(), " ", i)
                sleep(delay)
                if delay > 0.0:
                    delay -= 1.0
                yield 1.0
        c.register(call)
        sleep(11)
        dprint(line=1)

        def call():
            delay = 3.0
            for i in range(10):
                dprint(time(), " ", i)
                yield Switch(d)
                # perform code on Callback d
                sleep(delay)
                if delay > 0.0:
                    delay -= 1.0

                yield Switch(c)
                # perform code on Callback c
        c.register(call)
        sleep(11.0)
        dprint(line=1)

        # CPU intensive call... should 'back off'
        def call():
            for i in xrange(10):
                sleep(2.0)
                desync = (yield 1.0)
                dprint("desync... ", desync)
                while desync > 0.1:
                    dprint("backing off... ", desync)
                    desync = (yield desync)
                    dprint("next try... ", desync)

        c.register(call)
        sleep(21.0)
        dprint(line=1)

        d.stop()
        c.stop()
