import asyncio
import logging
from collections import deque
from plotune_sdk import PlotuneRuntime
from plotune_sdk.src.streams import PlotuneStream
from plotune_sdk.models import Variable

from typing import Any, Dict, List, Optional, TypeAlias, Union
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from models import ConsumeSignal, Payload
from utils import form_dict_to_input, dynamic_form, get_config, get_custom_config

from core.bridge import Bridge

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class StreamAgent:
    def __init__(self):
        self.config = get_config()
        self.custom_config = get_custom_config()

        self.write_interval = float(self.custom_config.get("write_interval", 0.2))
        self.bridge_interval = float(self.custom_config.get("bridge_interval", 0.02))
        self.secure_bridge = self.custom_config.get("secure_bridge", False)

        self._runtime: Optional[PlotuneRuntime] = None
        self._api: Optional[FastAPI] = None

        self.signals: Dict[str, ConsumeSignal] = {}
        self._signal_lock = asyncio.Lock()

        self.bridges: Dict[str, dict] = {}  # variable.name -> {"bridge": Bridge, "queue": asyncio.Queue}
        
        # Round-robin
        self.bridge_order = deque()
        self._bridge_lock = asyncio.Lock()
        
        self._stream_producer_status = False
        self._register_events()

    def _register_events(self) -> None:
        server = self.runtime.server
        server.on_event("/form")(self._handle_form)
        server.on_event("/form", method="POST")(self._new_connection)
        server.on_event("/bridge/{variable_name}", method="POST")(self.on_bridge)
        server.on_event("/unbridge/{variable_name}", method="POST")(self.on_unbridge)
        server.on_ws()(self.stream)
        logger.debug("Runtime events registered")

    async def on_bridge(self, variable: Variable):
        print(f"{variable.name} requested to stream")
                
        bridge = Bridge(
            variable, 
            secure=False, 
            interval=self.bridge_interval
        )
        
        q = bridge.start()
        
        async with self._bridge_lock:
            self.bridges[variable.name] = {
                "bridge": bridge,
                "queue": q,
                "variable": variable
            }
            self.bridge_order.append(variable.name)
            
        print(f"{variable.name} bridged with separate queue")

    async def on_unbridge(self, variable: Variable):
        print(f"{variable.name} requested to remove from stream")
        
        async with self._bridge_lock:
            if variable.name in self.bridges:
                bridge_info = self.bridges[variable.name]
                await bridge_info["bridge"].stop()
                del self.bridges[variable.name]
                
                
                if variable.name in self.bridge_order:
                    self.bridge_order.remove(variable.name)

    async def _handle_form(self, data: dict) -> Any:
        print("Form requested")
        return dynamic_form()

    async def _new_connection(self, data: dict) -> Dict[str, str]:
        print("Form delivered")
        
        stream = form_dict_to_input(data)
        stream_name = stream.stream_name
        stream_type = stream.stream_type

        stream = self.runtime.create_stream(stream_name)
        asyncio.run_coroutine_threadsafe(self.runtime._ensure_stream_running(stream), self.runtime.loop)
        
        if stream_type == "consumer":
            stream.on_consume()(self.listen_stream)

        if stream_type == "producer":
            asyncio.create_task(self.stream_loop(stream))

        return {"status": "success", "message": f"{stream_name} registered"}
    
    async def stream_loop(self, stream: PlotuneStream):
        logger.info("Producer stream loop started (fair scheduling)")

        async def write(key, timestamp, value):
            await stream.aproduce(key=key, timestamp=timestamp, value=value)

        while True:
            try:
                async with self._bridge_lock:
                    if not self.bridge_order:
                        
                        await asyncio.sleep(0.1)
                        continue
                    
                    current_bridge_name = self.bridge_order[0]
                    self.bridge_order.rotate(-1)
                    
                    bridge_info = self.bridges.get(current_bridge_name)
                
                if bridge_info:
                    queue = bridge_info["queue"]
                    
                    try:
                        data = queue.get_nowait()
                        
                        await write(
                            key=data.get("signal_name"),
                            timestamp=data.get("timestamp"),
                            value=data.get("value")
                        )
                        
                        await asyncio.sleep(self.write_interval)
                        
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0.01)
                    except Exception as e:
                        logger.error(f"Error processing data from {current_bridge_name}: {e}")
                else:
                    await asyncio.sleep(0.01)
                    
            except Exception as e:
                logger.error(f"Error in stream_loop: {e}")
                await asyncio.sleep(1)

    async def listen_stream(self, msg: dict):
        data = msg.get("payload")
        key = data.get("key")
        _time = float(data.get("time"))
        value = float(data.get("value"))

        if key not in self.signals:
            async with self._signal_lock:
                if key not in self.signals:
                    await self.runtime.core_client.add_variable(
                        variable_name=key,
                        variable_desc=f"{key} from stream",
                    )
                    self.signals[key] = ConsumeSignal(key)

        self.signals[key].append(Payload(value=value, time=_time))

    async def stream(
        self,
        signal_name: str,
        websocket: WebSocket,
        data: Any,
    ) -> None:
        logger.info("Client requested signal '%s'", signal_name)
        
        if signal_name not in self.signals:
            await websocket.close(1000, "Signal not found")
            return
            
        signal = self.signals[signal_name]

        # Historically
        for payload in sorted(signal.data, key=lambda p: p.time):
            await websocket.send_json({
                "timestamp": payload.time,
                "value": payload.value,
            })
        
        queue = signal.subscribe()
        # Real-time
        try:
            while True:
                payload = await queue.get()
                await websocket.send_json({
                    "timestamp": payload.time,
                    "value": payload.value,
                })
        except WebSocketDisconnect:
            signal.unsubscribe(queue)

    @property
    def api(self) -> FastAPI:
        if not self._api:
            self._api = self.runtime.server.api
        return self._api

    @property
    def runtime(self) -> PlotuneRuntime:
        if self._runtime:
            return self._runtime

        connection = self.config.get("connection", {})
        target = connection.get("target", "127.0.0.1")
        port = connection.get("target_port", "8000")
        core_url = f"http://{target}:{port}"

        self._runtime = PlotuneRuntime(
            ext_name=self.config.get("id"),
            core_url=core_url,
            config=self.config,
        )

        return self._runtime
    
    def start(self) -> None:
        print("Starting Stream Agent")
        self.runtime.start()