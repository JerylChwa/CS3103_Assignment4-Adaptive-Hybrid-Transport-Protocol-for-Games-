import asyncio
import json
import random
from typing import Optional

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass

from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import (
    DatagramFrameReceived,
    StreamDataReceived,
    ConnectionTerminated,
    ProtocolNegotiated,
    HandshakeCompleted,
)

ALPN = "game/1"

def make_dgram(msg_type: int, channel: int, seq: int, payload: bytes) -> bytes:
    return bytes([msg_type & 0xFF, channel & 0xFF]) + seq.to_bytes(2, "big") + payload

class GameClientProtocol:
    """
    Thin wrapper so we can await on events and send periodically.
    """

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.quic: QuicConnection = endpoint._quic
        self.seq_by_channel = {}

    def next_seq(self, channel: int) -> int:
        s = self.seq_by_channel.get(channel, -1) + 1
        self.seq_by_channel[channel] = s
        return s

    async def run(self):
        # Open a reliable stream for control/critical state
        ctrl_stream = self.quic.get_next_available_stream_id(is_unidirectional=False)
        hello = json.dumps({"type": "client_hello"}).encode()
        self.quic.send_stream_data(ctrl_stream, hello, end_stream=False)
        self.endpoint.transmit()

        # Start two background tasks: receive loop (events) and movement tick loop
        recv_task = asyncio.create_task(self._recv_loop())
        move_task = asyncio.create_task(self._movement_loop(channel=1, hz=30))

        await asyncio.sleep(5)  # demo runtime
        recv_task.cancel()
        move_task.cancel()

    async def _recv_loop(self):
        """
        aioquic delivers events to the `endpoint` object internally, but we can
        await the connection to go idle to force flushes. In practice, you’d
        subclass QuicConnectionProtocol; here we just keep the connection open.
        """
        try:
            while True:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    async def _movement_loop(self, channel: int, hz: int = 30):
        """
        Send movement updates via QUIC DATAGRAM at fixed rate.
        """
        period = 1.0 / hz
        try:
            while True:
                await asyncio.sleep(period)
                x = random.uniform(-10, 10)
                y = random.uniform(-10, 10)
                payload = f"pos:{x:.2f},{y:.2f}".encode()
                seq = self.next_seq(channel)
                self.quic.send_datagram_frame(make_dgram(1, channel, seq, payload))
                self.endpoint.transmit()
        except asyncio.CancelledError:
            pass

class ClientEvents(QuicConnectionProtocol):
    """
    If you want to *receive* reliable/unreliable from the server, handle it here.
    """
    def quic_event_received(self, event) -> None:
        if isinstance(event, HandshakeCompleted):
            # ready to send; GameClientProtocol will do that
            pass
        elif isinstance(event, StreamDataReceived):
            # Reliable data from server
            # print("STREAM RX:", event.stream_id, event.data)
            pass
        elif isinstance(event, DatagramFrameReceived):
            # Unreliable from server
            # print("DGRAM RX:", event.data[:16])
            pass

async def main(host: str = "127.0.0.1", port: int = 4433):
    cfg = QuicConfiguration(
        is_client=True,
        alpn_protocols=[ALPN],
    )
    # In dev, skip certificate validation (don’t do this in prod)
    cfg.verify_mode = False
    cfg.max_datagram_frame_size = 1200

    async with connect(host, port, configuration=cfg, create_protocol=ClientEvents) as endpoint:
        client = GameClientProtocol(endpoint)
        await client.run()

if __name__ == "__main__":
    asyncio.run(main())
