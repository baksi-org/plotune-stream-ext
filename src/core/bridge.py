import json
import asyncio
import aiohttp
import logging
import contextlib

from plotune_sdk.models import Variable

logger = logging.getLogger("bridge")


class Bridge:
    def __init__(
        self,
        variable: Variable,
        queue_size: int = 100,
        secure: bool = True,
        queue: asyncio.Queue = None,
        interval: float = 0.02,
    ):
        self.variable = variable
        self.name = variable.name
        self.interval = interval
        if secure:
            secure = variable.source_ip not in ("127.0.0.1", "localhost")

        self.schema = r"wss://" if secure else r"ws://"
        self.url = f"{self.schema}{variable.source_ip}:{variable.source_port}/fetch/{variable.name}"

        self.queue = queue or asyncio.Queue(maxsize=queue_size)
        self._running = False
        self._task: asyncio.Task | None = None

    async def listen(self):
        backoff = 1

        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    logger.info("[%s] Connecting to %s", self.name, self.url)

                    async with session.ws_connect(self.url) as ws:
                        logger.info("[%s] Connected", self.name)
                        backoff = 1  # reset backoff

                        async for msg in ws:
                            if not self._running:
                                break

                            try:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    self.queue.put_nowait(json.loads(msg.data))

                                elif msg.type == aiohttp.WSMsgType.BINARY:
                                    self.queue.put_nowait(msg.data)

                                elif msg.type == aiohttp.WSMsgType.ERROR:
                                    logger.error(
                                        "[%s] WS error: %s",
                                        self.name,
                                        ws.exception(),
                                    )
                                    break
                                await asyncio.sleep(self.interval)
                            except asyncio.QueueFull:
                                await asyncio.sleep(self.interval * 10)

                except aiohttp.ClientConnectorError as e:
                    logger.warning("[%s] Connect failed: %s", self.name, e)

                except Exception:
                    logger.exception("[%s] Unexpected error", self.name)

                if self._running:
                    logger.info("[%s] Reconnecting in %ss", self.name, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)

        logger.info("[%s] Listener stopped", self.name)

    def start(self) -> asyncio.Queue:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self.listen())
        return self.queue

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
