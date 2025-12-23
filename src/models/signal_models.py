import asyncio
from dataclasses import dataclass, field
from collections import deque
from typing import Deque, List

@dataclass
class Payload:
    value: float
    time: float

@dataclass
class ConsumeSignal:
    key: str
    data: Deque[Payload] = field(
        default_factory=lambda: deque(maxlen=1200)
    )
    subscribers: List[asyncio.Queue] = field(default_factory=list)

    def append(self, payload: Payload):
        self.data.append(payload)

        for queue in self.subscribers:
            if not queue.full(): # drops if full
                queue.put_nowait(payload)

    def subscribe(self, max_queue_size: int = 100) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=max_queue_size)
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        if queue in self.subscribers:
            self.subscribers.remove(queue)
