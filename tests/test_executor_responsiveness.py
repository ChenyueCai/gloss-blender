"""The IOLoop must keep running while GPU work runs.

Mirrors the structure ``GLAZEWebSocketHandler`` now uses: blocking work is
decorated with ``run_on_executor``, ``yield``-ed from a coroutine, and reports
progress back through ``IOLoop.add_callback``. The real handler needs CUDA and
kaolin, so this exercises the pattern directly; ``test_server_structure.py``
asserts the real handler is built this way.

Note on Tornado semantics: when ``on_message`` returns an awaitable, Tornado
waits for it before reading the *next frame on that connection*. So offloading
does not make a single connection process two fills concurrently -- which is
what we want anyway, since GPU work is serialized. What it does buy, and what
these tests assert, is that the IOLoop itself keeps running: progress messages
reach the client mid-stroke and other connections keep being served.
"""

import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import tornado.gen
import tornado.ioloop
import tornado.testing
import tornado.web
import tornado.websocket
from tornado.concurrent import run_on_executor

#: How long the fake "GPU stroke" takes.
BLOCKING_SECONDS = 1.0
#: How many progress ticks it emits along the way.
PROGRESS_TICKS = 4

class OffloadedHandler(tornado.websocket.WebSocketHandler):
    """Blocking work on an executor, progress marshalled back -- post-fix."""

    def initialize(self, executor):
        # Per-test executor. The real server deliberately shares ONE worker
        # process-wide to serialize CUDA work; here isolation keeps a stroke
        # left running by one test from queueing ahead of the next test's.
        self.executor = executor

    @run_on_executor
    def _slow_blocking(self, progress):
        for tick in range(PROGRESS_TICKS):
            time.sleep(BLOCKING_SECONDS / PROGRESS_TICKS)
            progress(tick + 1, PROGRESS_TICKS)
        return "slow-done"

    @tornado.gen.coroutine
    def on_message(self, message):
        request = json.loads(message)
        if request["type"] != "slow":
            self.write_message(json.dumps({"type": "ping", "result": "pong"}))
            return

        io_loop = tornado.ioloop.IOLoop.current()

        def progress(current, total):
            # Called from the executor thread; hop back to the IOLoop.
            io_loop.add_callback(
                self.write_message,
                json.dumps({"type": "progress", "current": current, "total": total}),
            )

        result = yield self._slow_blocking(progress)
        self.write_message(json.dumps({"type": "slow", "result": result}))


class InlineHandler(tornado.websocket.WebSocketHandler):
    """Blocking work inline on the IOLoop -- the pre-fix pattern."""

    @tornado.gen.coroutine
    def on_message(self, message):
        request = json.loads(message)
        if request["type"] != "slow":
            self.write_message(json.dumps({"type": "ping", "result": "pong"}))
            return
        # Progress cannot escape: nothing can write while the loop is blocked.
        for tick in range(PROGRESS_TICKS):
            time.sleep(BLOCKING_SECONDS / PROGRESS_TICKS)
            self.write_message(
                json.dumps({"type": "progress", "current": tick + 1,
                            "total": PROGRESS_TICKS}))
        self.write_message(json.dumps({"type": "slow", "result": "slow-done"}))


class _Base(tornado.testing.AsyncHTTPTestCase):
    handler_class = OffloadedHandler

    def get_app(self):
        self._executor = ThreadPoolExecutor(max_workers=1)
        kwargs = ({"executor": self._executor}
                  if self.handler_class is OffloadedHandler else {})
        return tornado.web.Application(
            [(r"/websocket", self.handler_class, kwargs)])

    def tearDown(self):
        # Do not wait: a test may deliberately leave a stroke mid-flight.
        self._executor.shutdown(wait=False)
        super().tearDown()

    def url(self):
        return f"ws://127.0.0.1:{self.get_http_port()}/websocket"

    @tornado.gen.coroutine
    def max_ioloop_gap(self, sample_ms=20):
        """Run one slow request; return the longest gap between IOLoop ticks.

        A blocked loop cannot run its own periodic callback, so the largest
        gap in the sample series is a direct measure of how long the loop was
        unavailable -- exactly what "does not block the server" means.
        """
        ticks = []
        sampler = tornado.ioloop.PeriodicCallback(
            lambda: ticks.append(time.time()), sample_ms)
        conn = yield tornado.websocket.websocket_connect(self.url())
        # Seed and close the series so a loop that never ticks at all still
        # yields a measurable gap rather than an empty list.
        ticks.append(time.time())
        sampler.start()
        conn.write_message(json.dumps({"type": "slow"}))
        while True:
            raw = yield conn.read_message()
            if json.loads(raw)["type"] == "slow":
                break
        sampler.stop()
        ticks.append(time.time())
        conn.close()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        raise tornado.gen.Return(max(gaps) if gaps else 0.0)

    @tornado.gen.coroutine
    def collect(self):
        """Run one slow request; record when each reply lands."""
        conn = yield tornado.websocket.websocket_connect(self.url())
        start = time.time()
        conn.write_message(json.dumps({"type": "slow"}))
        events = []
        while True:
            raw = yield conn.read_message()
            payload = json.loads(raw)
            events.append((payload["type"], time.time() - start))
            if payload["type"] == "slow":
                break
        conn.close()
        raise tornado.gen.Return(events)


class TestProgressStreamsDuringWork(_Base):
    handler_class = OffloadedHandler

    @tornado.testing.gen_test(timeout=30)
    def test_progress_arrives_before_the_work_finishes(self):
        """The panel can show 'generating 2/4' instead of appearing frozen."""
        events = yield self.collect()
        progress = [t for kind, t in events if kind == "progress"]
        finish = [t for kind, t in events if kind == "slow"][0]

        self.assertEqual(len(progress), PROGRESS_TICKS,
                         "progress messages did not reach the client")
        self.assertLess(progress[0], BLOCKING_SECONDS * 0.5,
                        "first progress message was not delivered early")
        self.assertLess(max(progress), finish,
                        "progress only arrived after the work completed")
        print(f"\n  offloaded: first progress at {progress[0] * 1000:.0f} ms, "
              f"done at {finish * 1000:.0f} ms")

    @tornado.testing.gen_test(timeout=30)
    def test_ioloop_is_never_stalled(self):
        """The IOLoop keeps ticking: no gap longer than a few frames."""
        gap = yield self.max_ioloop_gap()
        self.assertLess(gap, BLOCKING_SECONDS * 0.25,
                        "the IOLoop stalled while GPU work ran")
        print(f"\n  offloaded: longest IOLoop stall {gap * 1000:.0f} ms "
              f"during a {BLOCKING_SECONDS:.0f}s stroke")

    @tornado.testing.gen_test(timeout=30)
    def test_a_second_connection_is_served_during_the_work(self):
        """Another client gets an answer mid-stroke."""
        busy = yield tornado.websocket.websocket_connect(self.url())
        busy.write_message(json.dumps({"type": "slow"}))
        yield tornado.gen.sleep(0.1)

        start = time.time()
        other = yield tornado.websocket.websocket_connect(self.url())
        other.write_message(json.dumps({"type": "ping"}))
        reply = yield other.read_message()
        elapsed = time.time() - start

        self.assertEqual(json.loads(reply)["result"], "pong")
        self.assertLess(elapsed, BLOCKING_SECONDS * 0.6,
                        "second connection was stalled by the GPU work")
        print(f"\n  offloaded: second connection answered in "
              f"{elapsed * 1000:.0f} ms mid-stroke")
        busy.close()
        other.close()


class TestInlineBlocksEverything(_Base):
    """Characterises the pre-fix behaviour, so the contrast is explicit."""

    handler_class = InlineHandler

    @tornado.testing.gen_test(timeout=30)
    def test_no_progress_escapes_while_blocked(self):
        events = yield self.collect()
        progress = [t for kind, t in events if kind == "progress"]
        # Everything is written only once the loop is released, so every
        # message lands in a burst at the end.
        self.assertGreaterEqual(min(progress), BLOCKING_SECONDS * 0.9,
                                "inline blocking unexpectedly let progress out")
        print(f"\n  inline:    first progress at {min(progress) * 1000:.0f} ms "
              f"(i.e. only after the work finished)")

    @tornado.testing.gen_test(timeout=30)
    def test_ioloop_stalls_for_the_whole_stroke(self):
        gap = yield self.max_ioloop_gap()
        self.assertGreater(gap, BLOCKING_SECONDS * 0.8,
                           "inline blocking unexpectedly did not stall the loop")
        print(f"\n  inline:    longest IOLoop stall {gap * 1000:.0f} ms "
              f"during a {BLOCKING_SECONDS:.0f}s stroke")


if __name__ == "__main__":
    unittest.main()
