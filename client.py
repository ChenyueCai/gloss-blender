import asyncio
import json
import queue
import threading
from typing import Dict

import bpy
import websockets

SERVER_URL = "ws://localhost:10017/websocket"


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
        self.receive_queue = []  # thread-safe list for Blender main thread
        self.ws_chunks = queue.Queue()
        self.status = "Disconnected"
        self.last_error = ""

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
    # Connection + Workers
    # ---------------------------------------------------------

    async def connect(self):
        """Try connecting to the websocket."""
        self._set_status(f"Connecting to {self.uri}")
        try:
            self.ws = await websockets.connect(
                self.uri,
                max_size=400 * 4096 * 4096,
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
                print(f"Received message ...")
                self.receive_queue.append(msg)
                if isinstance(msg, bytes):
                    self.ws_chunks.put(msg)

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
        """Run sender and receiver tasks."""
        await asyncio.gather(
            self.send_worker(),
            self.receive_worker(),
        )

    async def close_ws(self):
        """Close socket only — workers will exit with _running=False."""
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
        self.ws = None
        self._set_status("Disconnected")

    # ---------------------------------------------------------
    # Start / Stop
    # ---------------------------------------------------------

    def start(self):
        """Start loop ONLY if not already running."""
        if self._running:
            print("WS already running — reusing existing instance")
            return

        print("Starting WS client...")
        self._running = True

        if self.loop is None:
            self.loop = asyncio.new_event_loop()
        self.send_queue = asyncio.Queue()

        def runner():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.main_worker())

        # Reuse existing thread if exists
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=runner, daemon=True)
            self.thread.start()

        try:
            if not bpy.app.timers.is_registered(self.poll_str_messages):
                bpy.app.timers.register(self.poll_str_messages)
        except:
            pass

    def stop(self):
        """
        Stop WS only if Blender is quitting.
        During add-on reload, keep it alive to speed up development.
        """
        # Always unregister timer
        try:
            bpy.app.timers.unregister(self.poll_str_messages)
        except:
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
            except:
                pass

        self.thread = None
        self.loop = None
        self.send_queue = None
        self.ws = None
        self._set_status("Disconnected")

        print("WS client fully stopped")

    # ---------------------------------------------------------
    # Public API for Blender
    # ---------------------------------------------------------

    def send(self, data: dict):
        """Non-blocking send from Blender (thread-safe)."""
        if self.loop and self.send_queue is not None:
            print(data)
            asyncio.run_coroutine_threadsafe(self.send_queue.put(data), self.loop)

    def poll_str_messages(self):
        """Called by Blender every 0.2s on main thread."""
        while self.receive_queue:
            message = self.receive_queue.pop(0)

            if isinstance(message, str):
                print(message)

        return 0.2  # run again after 0.2s

    def poll_bin_messages(self):
        """Return and consume the next queued binary message, if any."""
        while self.receive_queue:
            message = self.receive_queue.pop(0)
            if isinstance(message, bytes):
                return message
        return None


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
