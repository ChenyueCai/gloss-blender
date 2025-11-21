import bpy
import asyncio
import threading
import websockets
import json
import time

from .utils.mesh import update_texture
from .utils.io import from_binary


SERVER_URL = "ws://localhost:6060/websocket"


class WSClient:
    """Singleton-like WS client that persists across add-on reloads."""
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
        self.receive_queue = []

    # -----------------------------------------------------------
    # -------------------- ASYNC WORKERS -------------------------
    # -----------------------------------------------------------

    async def connect(self):
        self.ws = await websockets.connect(self.uri, max_size=40 * 1024 * 1024)
        print("WS connected")

    async def send_worker(self):
        while self._running:
            data = await self.send_queue.get()
            if self.ws is None:
                await self.connect()
            try:
                await self.ws.send(json.dumps(data))
            except Exception as e:
                print("Send failed:", e)

    async def receive_worker(self):
        while self._running:
            if self.ws is None:
                await self.connect()
            try:
                msg = await self.ws.recv()
                self.receive_queue.append(msg)
            except Exception as e:
                print("Receive failed:", e)
                await asyncio.sleep(1)

    async def main_worker(self):
        await asyncio.gather(
            self.send_worker(),
            self.receive_worker(),
        )

    async def close_ws(self):
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None

    # -----------------------------------------------------------
    # ---------------------- MANAGEMENT --------------------------
    # -----------------------------------------------------------

    def start(self):
        """Start loop ONLY if not already running."""
        if self._running:
            print("WS already running — reusing existing instance")
            return

        print("Starting WS client...")
        self._running = True

        if self.send_queue is None:
            self.send_queue = asyncio.Queue()

        # Do NOT recreate loop if it already exists (on reload)
        if self.loop is None:
            self.loop = asyncio.new_event_loop()

        def runner():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.main_worker())

        # Reuse existing thread if exists
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=runner, daemon=True)
            self.thread.start()

        print("WS client started")

    def stop(self, force=False):
        """
        Stop WS only if Blender is quitting.
        During add-on reload, keep it alive to speed up development.
        """
        # Always unregister timer
        try:
            bpy.app.timers.unregister(self.poll_messages)
        except:
            pass

        if not self._running:
            print("WS client already stopped")
            return

        # If Blender is not quitting -> skip full shutdown
        if not force and not bpy.app.is_quit:
            print("WS stop skipped (addon reload). WebSocket kept alive.")
            return

        print("Stopping WS client...")

        self._running = False

        if self.loop:
            asyncio.run_coroutine_threadsafe(self.close_ws(), self.loop)
            self.loop.call_soon_threadsafe(self.loop.stop)

        if self.thread:
            try:
                self.thread.join(timeout=2)
            except:
                pass

        self.thread = None
        self.loop = None

        print("WS client fully stopped")

    # -----------------------------------------------------------
    # --------------------- USER API -----------------------------
    # -----------------------------------------------------------

    def send(self, data: dict):
        if self.loop and self._running:
            asyncio.run_coroutine_threadsafe(self.send_queue.put(data), self.loop)

    def poll_messages(self):
        """Called in Blender main thread via timer."""
        while self.receive_queue:
            message = self.receive_queue.pop(0)

            if type(message) == bytes:
                message = from_binary(message)
                start = time.time()
                new_texture = message["texture"]
                update_texture(new_texture)
                print(f"Processed message in {time.time() - start:.2f} sec")

            elif type(message) == str:
                print(message)

        return 0.2


# -----------------------------------------
# GLOBAL SINGLETON INSTANCE
# -----------------------------------------
ws_client = WSClient()


def register_client():
    ws_client.start()
    bpy.app.timers.register(ws_client.poll_messages)


def unregister_client():
    # Do NOT force stop unless Blender is quitting
    ws_client.stop(force=False)
