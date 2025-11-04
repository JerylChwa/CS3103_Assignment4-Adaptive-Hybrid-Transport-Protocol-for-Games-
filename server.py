# server.py
import asyncio
import json

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass

from aioquic.asyncio import serve, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import (
    DatagramFrameReceived,
    StreamDataReceived,
    HandshakeCompleted,
)

ALPN = "game/1"

def make_server_cfg() -> QuicConfiguration:
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.max_datagram_frame_size = 1200
    # ⚠️ Make sure these files exist (see: openssl command I gave earlier)
    cfg.load_cert_chain("server.crt", "server.key")
    return cfg

class GameServer(QuicConnectionProtocol):
    def quic_event_received(self, event) -> None:
        if isinstance(event, HandshakeCompleted):
            peer = self._quic._peer_address if hasattr(self._quic, "_peer_address") else "<peer>"
            print(f"[server] Handshake completed with {peer}")
            # Send a reliable hello on a fresh bidirectional stream
            stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
            self._quic.send_stream_data(stream_id, json.dumps({"type": "server_hello"}).encode(), end_stream=False)
            self.transmit()

        elif isinstance(event, StreamDataReceived):
            print(f"[server] STREAM RX id={event.stream_id} len={len(event.data)}")
            # Echo ack
            self._quic.send_stream_data(event.stream_id, b"ack:" + event.data, end_stream=False)
            self.transmit()

        elif isinstance(event, DatagramFrameReceived):
            print(f"[server] DGRAM RX len={len(event.data)}, data:{event.data}")

async def main(host="0.0.0.0", port=4433):
    cfg = make_server_cfg()
    await serve(host, port, configuration=cfg, create_protocol=GameServer)
    print(f"[server] QUIC listening on {host}:{port} (ALPN={ALPN})")
    # ⛔️ Keep the server running forever
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
