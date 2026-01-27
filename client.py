import bpy
import asyncio
import threading
import websockets
import json
import os
import time
import torchvision
from typing import Dict
from .utils.mesh import update_texture
from .utils.io import from_binary, to_binary
import queue

SERVER_URL = "ws://localhost:8080/websocket"


class WSClient:
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
        self.send_queue = None  # Created in start() within event loop
        self.receive_queue = [] # thread-safe list for Blender main thread
        self.ws_chunks = queue.Queue() 

    # ---------------------------------------------------------
    # Connection + Workers
    # ---------------------------------------------------------

    async def connect(self):
        """Try connecting to the websocket."""
        try:
            self.ws = await websockets.connect(self.uri, max_size=400 * 4096*4096, ping_interval=20, ping_timeout=None)
            print("WS connected")
        except Exception as e:
            print("WS connect failed:", e)
            await asyncio.sleep(1)
            self.ws = None

    async def send_worker(self):
        """Continuously send messages from the send_queue (receive_worker handles reconnect)."""
        print("send_worker started")
        while self._running:
            # Wait for connection to be established by receive_worker
            if self.ws is None:
                await asyncio.sleep(0.5)
                continue

            #print("send_worker: waiting for data from queue...")
            data = await self.send_queue.get()
            print(f"send_worker: got data, sending...")

            try:
                if isinstance(data, Dict):
                    await self.ws.send(json.dumps(data))
                else:
                    await self.ws.send(data)
                print("send_worker: sent successfully")
            except websockets.ConnectionClosed:
                print("Send failed — WS closed, will retry...")
                await self.send_queue.put(data)  # Re-queue for retry
                self.ws = None
                await asyncio.sleep(0.5)

            except Exception as e:
                print("Send failed:", e)
                await self.send_queue.put(data)  # Re-queue for retry
                self.ws = None
                await asyncio.sleep(1)

    async def receive_worker(self):
        """Continuously listen for messages and reconnect on failures."""
        print("receive_worker started")
        while self._running:
            if self.ws is None:
                print("receive_worker: ws is None, connecting...")
                await self.connect()
                if self.ws is None:
                    print("receive_worker: connect failed, sleeping...")
                    await asyncio.sleep(1)
                    continue

            try:
                #print("receive_worker: waiting for message...")
                msg = await self.ws.recv()
                print(f"Received message")
                self.receive_queue.append(msg)
                if isinstance(msg, bytes):
                    self.ws_chunks.put(msg)

            except websockets.ConnectionClosed:
                print("Receive failed — WS closed, reconnecting...")
                self.ws = None
                await asyncio.sleep(0.5)

            except Exception as e:
                print("Receive failed:", e)
                self.ws = None
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

        # Do NOT recreate loop if it already exists (on reload)
        if self.loop is None:
            self.loop = asyncio.new_event_loop()

        def runner():
            asyncio.set_event_loop(self.loop)
            # Create queue inside the event loop context
            if self.send_queue is None:
                self.send_queue = asyncio.Queue()
            self.loop.run_until_complete(self.main_worker())

        # Reuse existing thread if exists
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=runner, daemon=True)
            self.thread.start()

    def stop(self):
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

    # ---------------------------------------------------------
    # Public API for Blender
    # ---------------------------------------------------------

    def send(self, data: dict):
        """Non-blocking send from Blender (thread-safe)."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self.send_queue.put(data),
                self.loop
            )

    def poll_str_messages(self):
        """Called by Blender every 0.2s on main thread."""
        while self.receive_queue:
            message = self.receive_queue.pop(0)
                
            if isinstance(message, str):
                print(message)

        return 0.2  # run again after 0.2s

    def poll_bin_messages(self):
        while self.receive_queue:
            message = self.receive_queue.pop(0)
            if isinstance(message, bytes):
                return message
        return None
            # if isinstance(message, bytes):
            #     message = from_binary(message)
            #     return message
    
    # def poll_ws_chunks(self, max_items=100):
    #     """Called from Blender main thread"""
    #     chunks = []
    #     for _ in range(max_items):
    #         try:
    #             chunks.append(self.ws_chunks.get_nowait())
    #         except asyncio.QueueEmpty:
    #             break
    #     return chunks


# ---------------------------------------------------------
# Global instance + Blender hooks
# ---------------------------------------------------------

ws_client = WSClient()


def register_client():
    ws_client.start()
    bpy.app.timers.register(ws_client.poll_str_messages)


def unregister_client():
    ws_client.stop()