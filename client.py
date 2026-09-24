import asyncio
import json
import threading
from typing import Dict

import bpy
import websockets

from .protocol import (
    MSG_BRUSH_ICON,
    MSG_STATUS,
    MSG_TEXTURE_CHUNK,
)
from .utils.io import from_binary

SERVER_URL = "ws://localhost:10017/websocket"

#: How often the router drains the inbox, in seconds. Fast enough that a
#: multi-chunk texture is not gated on the timer, cheap enough to leave
#: registered for the whole session.
POLL_INTERVAL = 0.05


def infer_message_type(payload):
    """Return the routing key for a decoded server message.

    Messages from a current server carry an explicit ``type``. The two legacy
    shapes are recognised so an add-on update does not hard-require a server
    update:

    - a binary payload carrying ``chunk_total`` is a texture chunk;
    - a binary payload carrying the ``"brush icon"`` key is a brush icon.
    """
    msg_type = payload.get("type")
    if msg_type:
        return msg_type
    if payload.get("chunk_total") is not None:
        return MSG_TEXTURE_CHUNK
    if payload.get("brush icon") is not None:
        return MSG_BRUSH_ICON
    return MSG_STATUS


class WSClient:
    """Singleton websocket client shared across Blender add-on reloads."""
    _instance = None

    def __new__(cls, *args, **kwargs):
        # Ensure only one instance exists across reloads
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, uri=SERVER_URL):
        if getattr(self, "_initialized", False):
            return  # Do NOT re-init on module reload
        self._initialized = True

        self.uri = uri
        self.ws = None
        self.loop = None
        self.thread = None
        self._running = False
        self.send_queue = None
        # Single inbox with a single owner: ``poll_messages``. Do not add a
        # second consumer -- that is what silently ate brush icons before.
        self.receive_queue = []
        self.status = "Disconnected"
        self.last_error = ""

        # type -> [handler]. Handlers run on Blender's main thread.
        self._handlers = {}
        self._handler_lock = threading.Lock()
        self._queue_ready = threading.Event()
        self.stats = {
            "received": 0,
            "dispatched": 0,
            "unhandled": 0,
            "decode_errors": 0,
            "handler_errors": 0,
        }

    def _ensure_runtime_state(self):
        """Backfill attributes added since this singleton was constructed.

        The instance deliberately survives add-on reloads (``__init__``
        returns early), so an instance created by an older version of this
        module can outlive it. Without this, adding a field here would raise
        ``AttributeError`` on the first reload after an update.
        """
        if not hasattr(self, "_handlers"):
            self._handlers = {}
        if not hasattr(self, "_handler_lock"):
            self._handler_lock = threading.Lock()
        if not hasattr(self, "_queue_ready"):
            self._queue_ready = threading.Event()
        if not hasattr(self, "stats"):
            self.stats = {
                "received": 0, "dispatched": 0, "unhandled": 0,
                "decode_errors": 0, "handler_errors": 0,
            }
        if not hasattr(self, "receive_queue"):
            self.receive_queue = []

    @property
    def is_connected(self):
        """Return ``True`` when a websocket connection object is available."""
        return self.ws is not None

    def _set_status(self, status, error=""):
        """Store the latest user-facing connection status and error text."""
        self.status = status
        self.last_error = error

    def configure(self, uri):
        """Update the websocket endpoint used by future connections."""
        self.uri = uri

    def restart(self, uri=None):
        """Reconnect the client, optionally switching to a new endpoint first."""
        if uri:
            self.configure(uri)
        self.stop()
        self.start()

    # ---------------------------------------------------------
    # Message routing
    # ---------------------------------------------------------

    def register_handler(self, msg_type, handler):
        """Route messages of ``msg_type`` to ``handler(payload)``.

        Several handlers may share a type; each is called in registration
        order. Registering the same callable twice is a no-op.
        """
        self._ensure_runtime_state()
        with self._handler_lock:
            handlers = self._handlers.setdefault(msg_type, [])
            if handler not in handlers:
                handlers.append(handler)

    def unregister_handler(self, msg_type, handler):
        """Stop routing ``msg_type`` to ``handler``. Safe if not registered."""
        with self._handler_lock:
            handlers = self._handlers.get(msg_type)
            if not handlers:
                return
            if handler in handlers:
                handlers.remove(handler)
            if not handlers:
                self._handlers.pop(msg_type, None)

    def _snapshot_handlers(self, msg_type):
        """Return a copy of the handler list so a handler may unregister itself."""
        with self._handler_lock:
            return list(self._handlers.get(msg_type, ()))

    @staticmethod
    def decode(raw):
        """Decode one raw frame into a dict payload.

        Text frames are JSON when the server speaks the tagged protocol; a
        legacy bare string is wrapped so it still routes as a status line.
        """
        if isinstance(raw, bytes):
            return from_binary(raw)
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return {"type": MSG_STATUS, "data": {"message": raw}}
        if not isinstance(payload, dict):
            return {"type": MSG_STATUS, "data": {"message": str(raw)}}
        return payload

    def dispatch(self, payload):
        """Deliver one decoded payload to its handlers.

        Returns ``True`` when at least one handler ran. A handler raising does
        not stop the others and never kills the router.
        """
        msg_type = infer_message_type(payload)
        handlers = self._snapshot_handlers(msg_type)
        if not handlers:
            self.stats["unhandled"] += 1
            if msg_type == MSG_STATUS:
                data = payload.get("data") or {}
                print("[gloss]", data.get("message", payload))
            else:
                print(f"[gloss] no handler for message type {msg_type!r}")
            return False

        for handler in handlers:
            try:
                handler(payload)
            except Exception as exc:  # a bad handler must not stall the queue
                self.stats["handler_errors"] += 1
                print(f"[gloss] handler for {msg_type!r} failed: {exc}")
        self.stats["dispatched"] += 1
        return True

    def poll_messages(self):
        """Drain the inbox and route each message. Blender main-thread timer.

        This is the ONLY consumer of ``receive_queue``.
        """
        while self.receive_queue:
            raw = self.receive_queue.pop(0)
            self.stats["received"] += 1
            try:
                payload = self.decode(raw)
            except Exception as exc:
                self.stats["decode_errors"] += 1
                print(f"[gloss] failed to decode message: {exc}")
                continue
            self.dispatch(payload)
        return POLL_INTERVAL

    # ---------------------------------------------------------
    # Connection + Workers
    # ---------------------------------------------------------

    async def connect(self):
        """Try connecting to the websocket."""
        self._set_status(f"Connecting to {self.uri}")
        try:
            self.ws = await websockets.connect(
                self.uri,
                # Generous but bounded: a 4K uint8 RGBA texture is 64 MB and is
                # chunked well below this. The old 6.7 GB ceiling let a bad
                # length prefix trigger a huge allocation.
                max_size=128 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=None,
            )
            print("WS connected")
            self._set_status(f"Connected to {self.uri}")
        except Exception as e:
            print("WS connect failed:", e)
            self._set_status("Disconnected", str(e))
            await asyncio.sleep(1)
            self.ws = None

    async def send_worker(self):
        """Continuously send messages from the send_queue with reconnect."""
        while self._running:
            if self.ws is None:
                await asyncio.sleep(0.5)
                continue
            data = await self.send_queue.get()
            try:
                if isinstance(data, Dict):
                    await self.ws.send(json.dumps(data))
                else:
                    await self.ws.send(data)
            except websockets.ConnectionClosed:
                print("Send failed — WS closed, retry with re-queuing...")
                await self.send_queue.put(data)
                self.ws = None
                self._set_status("Disconnected", "Connection closed while sending")
                await asyncio.sleep(0.5)

            except Exception as e:
                print("Send failed:", e)
                await self.send_queue.put(data)
                self.ws = None
                self._set_status("Disconnected", str(e))
                await asyncio.sleep(1)

    async def receive_worker(self):
        """Continuously listen for messages and reconnect on failures."""
        print("receive_worker started")
        while self._running:
            if self.ws is None:
                print("receive_worker: ws is None, connecting...")
                await self.connect()
                if self.ws is None:
                    await asyncio.sleep(1)
                    continue

            try:
                msg = await self.ws.recv()
                # One inbox, one consumer. Binary frames are NOT duplicated to
                # a second queue any more.
                self.receive_queue.append(msg)

            except websockets.ConnectionClosed:
                print("Receive failed — WS closed, reconnecting...")
                self.ws = None
                self._set_status("Disconnected", "Connection closed while receiving")
                await asyncio.sleep(0.5)

            except Exception as e:
                print("Receive failed:", e)
                self.ws = None
                self._set_status("Disconnected", str(e))
                await asyncio.sleep(1)

    async def main_worker(self):
        """Create the send queue on this loop, then run sender and receiver.

        The queue MUST be constructed inside the worker loop. On Python <= 3.9
        ``asyncio.Queue()`` binds to whatever loop is current at construction
        time, so building it on Blender's main thread tied it to a different
        (never-running) loop and every queued send was silently lost. Blender
        4.x ships Python 3.11, where queues bind lazily, which hid this.
        """
        self.send_queue = asyncio.Queue()
        self._queue_ready.set()
        await asyncio.gather(
            self.send_worker(),
            self.receive_worker(),
        )

    async def close_ws(self):
        """Close socket only — workers will exit with _running=False."""
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        self._set_status("Disconnected")

    # ---------------------------------------------------------
    # Start / Stop
    # ---------------------------------------------------------

    def start(self):
        """Start loop ONLY if not already running."""
        self._ensure_runtime_state()
        if self._running:
            print("WS already running — reusing existing instance")
            return

        print("Starting WS client...")
        self._running = True

        if self.loop is None:
            self.loop = asyncio.new_event_loop()
        self._queue_ready.clear()

        def runner():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.main_worker())

        # Reuse existing thread if exists
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=runner, daemon=True)
            self.thread.start()

        # Wait for the worker loop to publish its queue so a send issued
        # immediately after start() is not dropped.
        self._queue_ready.wait(timeout=5)

        try:
            if not bpy.app.timers.is_registered(self.poll_messages):
                bpy.app.timers.register(self.poll_messages)
        except Exception:
            pass

    def stop(self):
        """
        Stop WS only if Blender is quitting.
        During add-on reload, keep it alive to speed up development.
        """
        self._ensure_runtime_state()
        # Always unregister timer
        try:
            bpy.app.timers.unregister(self.poll_messages)
        except Exception:
            pass

        if not self._running:
            print("WS client already stopped")
            return

        print("Stopping WS client...")

        self._running = False

        if self.loop:
            asyncio.run_coroutine_threadsafe(self.close_ws(), self.loop)
            self.loop.call_soon_threadsafe(self.loop.stop)

        if self.thread:
            try:
                self.thread.join(timeout=2)
            except Exception:
                pass

        self.thread = None
        self.loop = None
        self.send_queue = None
        self._queue_ready.clear()
        self.ws = None
        self._set_status("Disconnected")

        print("WS client fully stopped")

    # ---------------------------------------------------------
    # Public API for Blender
    # ---------------------------------------------------------

    def send(self, data: dict):
        """Non-blocking send from Blender (thread-safe)."""
        if self.loop and self.send_queue is not None:
            asyncio.run_coroutine_threadsafe(self.send_queue.put(data), self.loop)


# ---------------------------------------------------------
# Global instance + Blender hooks
# ---------------------------------------------------------

ws_client = WSClient()


def register_client():
    """Start the shared websocket client for the add-on."""
    ws_client.start()


def unregister_client():
    """Stop the shared websocket client for the add-on."""
    ws_client.stop()
