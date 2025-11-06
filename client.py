import asyncio
import json
import random
import time
from typing import Optional, Dict
from metrics import RollingStats, Jitter

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
    HandshakeCompleted,
)
from network_emulator import NetworkEmulator

def get_timestamp():
    """Return formatted timestamp for logging: HH:MM:SS.mmm"""
    now = time.time()
    ms = int((now * 1000) % 1000)
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{ms:03d}"

ALPN = "game/1"
RELIABLE_CHANNEL = 0  # Used for Stream Data (Critical State)
UNRELIABLE_CHANNEL = 1  # Used for Datagrams (Movement)

# Part A : Packet Header
def make_dgram(msg_type: int, channel: int, seq: int, payload: bytes) -> bytes:
    # Header: MsgType(1B), Channel(1B), Seq(2B) + Payload
    return bytes([msg_type & 0xFF, channel & 0xFF]) + seq.to_bytes(2, "big") + payload

def make_reliable_data(seq: int, payload: dict) -> bytes:
    # Application-layer reliable packet structure for state updates
    return json.dumps({
        "type": "state_update",
        "channel": RELIABLE_CHANNEL,
        "seq": seq,
        "ts": time.time(),  # Sender timestamp for RTT calculation
        "data": payload,
    }).encode()

class GameClientProtocol:
    """
    Thin wrapper (gameNetAPI) to send data via reliable (Stream) and unreliable (Datagram) channels.
    """

    def __init__(self, endpoint, emulator: Optional[NetworkEmulator] = None):
        self.endpoint = endpoint
        self.quic: QuicConnection = endpoint._quic
        self.seq_by_channel = {RELIABLE_CHANNEL: -1, UNRELIABLE_CHANNEL: -1}
        self.ctrl_stream_id: Optional[int] = None
        
        # Network emulator (defaults to disabled if not provided)
        self.emulator = emulator if emulator is not None else NetworkEmulator(enabled=False)

        self._start_time = time.time()
        self._metrics_task: Optional[asyncio.Task] = None
        # _inflight stores: {seq: {"send_ts": float, "packet_data": bytes, "retry_count": int, "last_retry_ts": float}}
        self._inflight: Dict[int, Dict] = {}

        #storage for server rx counters
        self._server_unrel_rx = 0
        
        # Retransmission settings
        self._retransmit_timeout_ms = 100  # Initial timeout (will be updated based on RTT)
        self._max_rtt_estimate_ms = 200  # Maximum RTT estimate for timeout calculation

        self.metrics = {
            "reliable": {
                "tx": 0, "ack": 0, "bytes_tx": 0, "retransmit": 0,
                "rtt": RollingStats(), "jitter": Jitter()
            },
            "unreliable": {
                "tx": 0, "bytes_tx": 0,
            }
        }

    def _print_metrics_summary(self):
        now = time.time()
        dur = max(1e-6, now - self._start_time)

        r = self.metrics["reliable"]
        r_p = r["rtt"].percentiles()
        r_tput = r["bytes_tx"] / 1024.0 / dur
        pdr = 100.0 * r["ack"] / max(1, r["tx"])
        print("\n[client] 📊 ---- METRIC SUMMARY ----")
        print(f"[metrics][RELIABLE] TX={r['tx']} ACK={r['ack']} RETX={r.get('retransmit', 0)} PDR={pdr:.1f}% BytesTX={r['bytes_tx']}")
        if len(r["rtt"].samples) > 0:
            print(f"    RTT(ms): avg={r['rtt'].avg():.2f} "
                  f"p50={r_p.get(50, float('nan')):.2f} "
                  f"p95={r_p.get(95, float('nan')):.2f} "
                  f"jitter(RFC3550)={r['jitter'].value():.2f}")
        else:
            print("    RTT(ms): No samples yet")
        print(f"    Throughput ≈ {r_tput:.2f} kB/s")

        u = self.metrics["unreliable"]
        u_tput = u["bytes_tx"] / 1024.0 / dur
        u_pdr = 100.0 * self._server_unrel_rx / max(1, u["tx"])

        print(f"[metrics][UNRELIABLE] TX={u['tx']} BytesTX={u['bytes_tx']} PDR={u_pdr:.1f}%")
        print(f"    Throughput ≈ {u_tput:.2f} kB/s")
        print("[client] --------------------------\n")



    def next_seq(self, channel: int) -> int:
        s = self.seq_by_channel.get(channel, -1) + 1
        self.seq_by_channel[channel] = s
        return s
    
    # --- Client Methods (API) ---
    # Part B : H-UDP API to mark data as reliable or unreliable + Part f
    async def send(self, data=None, reliable=True, msg_type=1):
        if reliable:
            return await self.send_reliable_state(data=data)
        else:
            return await self.send_unreliable_movement(data=data, msg_type=msg_type)

    async def send_reliable_state(self, data=None):
        """Send a reliable, sequenced game state update.
        
        Args:
            data: Optional dict payload. If None, uses default game state.
        """
        if self.ctrl_stream_id is None:
            self.ctrl_stream_id = self.quic.get_next_available_stream_id(is_unidirectional=False)
            
        seq = self.next_seq(RELIABLE_CHANNEL)
        
        # Use provided data or generate default
        if data is None:
            payload = {"player_id": 1, "score": random.randint(0, 100)}
        else:
            payload = data
        
        critical_state = make_reliable_data(
            seq=seq,
            payload=payload
        )

        self.metrics["reliable"]["tx"] += 1
        self.metrics["reliable"]["bytes_tx"] += len(critical_state)
        
        # Store packet data for retransmission
        send_ts = time.time()
        self._inflight[seq] = {
            "send_ts": send_ts,
            "packet_data": critical_state,
            "retry_count": 0,
            "last_retry_ts": send_ts
        }

        self.quic.send_stream_data(self.ctrl_stream_id, critical_state, end_stream=False)
        await self.emulator.transmit(
            self.endpoint.transmit,
            packet_info={"seq": seq, "type": "reliable"}
        )
        print(f"[{get_timestamp()}] [client] [Reliable] SENT: Seq={seq}, Size={len(critical_state)}, TS={send_ts:.4f}")
        return seq


    async def send_unreliable_movement(self, data=None, msg_type=1):
        """Send an unreliable, sequenced movement update.
        
        Args:
            data: Optional payload (str or bytes). If None, generates position data.
            msg_type: Message type for datagram header (default: 1).
        """
        # Use provided data or generate default position
        if data is None:
            x = random.uniform(-10, 10)
            y = random.uniform(-10, 10)
            payload = f"pos:{x:.2f},{y:.2f},ts:{time.time():.4f}".encode()
        else:
            # Convert to bytes if needed
            if isinstance(data, str):
                payload = data.encode()
            elif isinstance(data, bytes):
                payload = data
            else:
                raise ValueError(f"Unsupported data type for unreliable send: {type(data)}")
        
        seq = self.next_seq(UNRELIABLE_CHANNEL)
        datagram = make_dgram(msg_type=msg_type, channel=UNRELIABLE_CHANNEL, seq=seq, payload=payload)

        self.metrics["unreliable"]["tx"] += 1
        self.metrics["unreliable"]["bytes_tx"] += len(datagram)

        self.quic.send_datagram_frame(datagram)
        await self.emulator.transmit(self.endpoint.transmit)
        print(f"[{get_timestamp()}] [client] [Unreliable] SENT: Seq={seq}, Size={len(datagram)}")
        return seq

    
    async def run(self):
        # Initial reliable hello for connection setup
        if self.ctrl_stream_id is None:
            self.ctrl_stream_id = self.quic.get_next_available_stream_id(is_unidirectional=False)
            hello = json.dumps({"type": "client_hello"}).encode()
            self.quic.send_stream_data(self.ctrl_stream_id, hello, end_stream=False)
            await self.emulator.transmit(self.endpoint.transmit)

        recv_task = asyncio.create_task(self._recv_loop())
        # Part f Randomized sending loop
        mixed_task = asyncio.create_task(self._mixed_loop(reliable_hz=5, unreliable_hz=30))
        # Retransmission task
        retransmit_task = asyncio.create_task(self._retransmit_loop())

        await asyncio.sleep(10)  
        recv_task.cancel()
        mixed_task.cancel()
        retransmit_task.cancel()
        if self._metrics_task:
            self._metrics_task.cancel()
        
        self.quic.close()
        await self.emulator.transmit(self.endpoint.transmit)

    async def _recv_loop(self):
        """Keeps the connection alive and flushes QUIC events."""
        try:
            while True:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass
    
    async def _retransmit_loop(self):
        """Periodically checks for unACKed packets and retransmits them."""
        try:
            while True:
                await asyncio.sleep(0.05)  # Check every 50ms
                now = time.time()
                
                # Calculate timeout based on current RTT estimate
                rtt_samples = self.metrics["reliable"]["rtt"].samples
                if len(rtt_samples) > 0:
                    avg_rtt = sum(rtt_samples) / len(rtt_samples)
                    timeout_ms = max(100, avg_rtt * 2)  # 2x RTT, minimum 100ms
                else:
                    timeout_ms = self._retransmit_timeout_ms
                
                # Check each unACKed packet
                to_retransmit = []
                for seq, pkt_info in list(self._inflight.items()):
                    elapsed_ms = (now - pkt_info["last_retry_ts"]) * 1000
                    
                    if elapsed_ms >= timeout_ms:
                        to_retransmit.append((seq, pkt_info))
                
                # Retransmit packets that timed out
                for seq, pkt_info in to_retransmit:
                    pkt_info["retry_count"] += 1
                    pkt_info["last_retry_ts"] = now
                    
                    self.metrics["reliable"]["retransmit"] += 1
                    self.metrics["reliable"]["bytes_tx"] += len(pkt_info["packet_data"])
                    
                    self.quic.send_stream_data(self.ctrl_stream_id, pkt_info["packet_data"], end_stream=False)
                    await self.emulator.transmit(
                        self.endpoint.transmit,
                        packet_info={"seq": seq, "type": "reliable"}
                    )
                    print(f"[{get_timestamp()}] [client] [Reliable] RETRANSMIT: Seq={seq}, Retry={pkt_info['retry_count']}, Timeout={timeout_ms:.1f}ms")
                    
        except asyncio.CancelledError:
            pass

    async def _mixed_loop(self, reliable_hz: int = 5, unreliable_hz: int = 30):
        """
        Sends data randomly across both channels using the unified gameNetAPI.
        """
        reliable_period = 1.0 / reliable_hz
        unreliable_period = 1.0 / unreliable_hz
        
        last_reliable_send = 0.0
        last_unreliable_send = 0.0

        try:
            while True:
                now = time.time()
                
                # Check for reliable send opportunity
                if now - last_reliable_send >= reliable_period:
                    # Randomly decide to send reliable (e.g., 50% chance when available)
                    if random.random() >= 0:
                        # Use unified API with reliable=True
                        await self.send(reliable=True)
                        last_reliable_send = now
                
                # Check for unreliable send opportunity (higher rate)
                if now - last_unreliable_send >= unreliable_period:
                    # Use unified API with reliable=False
                    await self.send(reliable=False)
                    last_unreliable_send = now
                    
                await asyncio.sleep(min(reliable_period, unreliable_period) / 2)
                
        except asyncio.CancelledError:
            pass

class ClientEvents(QuicConnectionProtocol):
    """
    Handles events from the server and logs RTT part g
    """
    def quic_event_received(self, event) -> None:
        if isinstance(event, HandshakeCompleted):
            # ready to send;
            pass
        elif isinstance(event, StreamDataReceived):
            # Reliable data from server (echo ACK from server)
            rx_ts = time.time()
            data_str = None
            try:
                data_str = event.data.decode()
                if data_str.startswith("ack:"):
                    # Parse the echoed reliable packet to get the original timestamp
                    original_packet = json.loads(data_str[4:])
                    seq = original_packet.get("seq")

                    # Calculate RTT based on sender's original timestamp
                    sent_ts = float(original_packet.get("ts", rx_ts))

                    client = getattr(self, "metrics_client", None)
                    if client is not None and seq is not None:
                        # Remove ACKed packet from inflight
                        pkt_info = client._inflight.pop(seq, None)
                        if pkt_info:
                            send_ts = pkt_info["send_ts"]
                            # Don't infer skips from ACKs - each packet is independent
                            # Only explicit skip notifications from server should stop retransmission
                        else:
                            # Duplicate ACK or already ACKed packet
                            send_ts = sent_ts
                        
                        rtt_ms = (rx_ts - send_ts) * 1000
                        m = client.metrics["reliable"]
                        m["ack"] += 1
                        m["rtt"].add(rtt_ms)
                        m["jitter"].add(rtt_ms)
                    else:
                        rtt_ms = (rx_ts - sent_ts) * 1000
                    print(f"[{get_timestamp()}] [client] [Reliable] ACK RX: AppSeq={seq}, RTT={rtt_ms:.2f}ms")
                    return

                pkt = json.loads(data_str)
                if pkt.get("type") == "skip":
                    # Server explicitly notified us that a packet was skipped
                    skipped_seq = pkt.get("seq")
                    next_seq = pkt.get("next_seq")
                    client = getattr(self, "metrics_client", None)
                    if client is not None:
                        # Remove the explicitly skipped packet only
                        # Don't infer other packets as skipped - they should be ACKed normally if they arrive
                        skipped_info = client._inflight.pop(skipped_seq, None)
                        if skipped_info:
                            print(f"[{get_timestamp()}] [client] ⚠️ EXPLICIT SKIP: Seq={skipped_seq} skipped by server (next_seq={next_seq}). "
                                  f"Retries={skipped_info['retry_count']}")
                    return
                
                if pkt.get("type") == "server_metrics":
                    client = getattr(self, "metrics_client", None)
                    if client:
                        client._server_unrel_rx = int(pkt.get("unrel_rx", 0))
                        client._server_rel_rx = int(pkt.get("rel_rx", 0))
                        client._print_metrics_summary()
                    return

            except json.JSONDecodeError as e:
                # Try to parse as skip notification even if JSON decode fails
                if data_str:
                    try:
                        pkt = json.loads(data_str)
                        if pkt.get("type") == "skip":
                            skipped_seq = pkt.get("seq")
                            next_seq = pkt.get("next_seq")
                            client = getattr(self, "metrics_client", None)
                            if client is not None:
                                skipped_info = client._inflight.pop(skipped_seq, None)
                                if skipped_info:
                                    print(f"[{get_timestamp()}] [client] ⚠️ EXPLICIT SKIP (from exception handler): Seq={skipped_seq} skipped by server (next_seq={next_seq}). "
                                          f"Retries={skipped_info['retry_count']}")
                                # Don't infer other packets as skipped - they should be ACKed normally
                            return
                    except:
                        pass
                # Handle initial client_hello ACK or malformed data
                if data_str and "client_hello" not in data_str:
                    print(f"[{get_timestamp()}] [client] [Reliable] Data RX (non-JSON): {data_str[:100]!r}")
            except Exception as e:
                # Handle other exceptions
                if data_str and "client_hello" not in data_str:
                    print(f"[client] [Reliable] Data RX (exception): {event.data!r}, error: {e}")


async def main(host: str = "127.0.0.1", port: int = 4433, 
               emulation_enabled: bool = False, delay_ms: float = 0, 
               jitter_ms: float = 0, packet_loss_rate: float = 0.0,
               drop_sequences: Optional[set] = None):
    """
    Main client function.
    
    Args:
        host: Server hostname
        port: Server port
        emulation_enabled: Enable network emulation
        delay_ms: Base delay in milliseconds
        jitter_ms: Jitter variation in milliseconds
        packet_loss_rate: Packet loss rate (0.0 to 1.0)
        drop_sequences: Set of sequence numbers to selectively drop (for testing retransmission)
    """
    cfg = QuicConfiguration(
        is_client=True,
        alpn_protocols=[ALPN],
    )
    # In dev, skip certificate validation (don't do this in prod)
    cfg.verify_mode = False
    cfg.max_datagram_frame_size = 1200

    # Create network emulator
    emulator = NetworkEmulator(
        enabled=emulation_enabled,
        delay_ms=delay_ms,
        jitter_ms=jitter_ms,
        packet_loss_rate=packet_loss_rate,
        drop_sequences=drop_sequences or set()
    )

    async with connect(host, port, configuration=cfg, create_protocol=ClientEvents) as endpoint:
        client = GameClientProtocol(endpoint, emulator=emulator)
        endpoint.metrics_client = client
        await client.run()

if __name__ == "__main__":
    # Test retransmission: selectively drop specific sequence numbers
    # ACKs will still get through since packet_loss_rate=0
    asyncio.run(main(
        emulation_enabled=True,
        delay_ms=70,
        jitter_ms=50,
        packet_loss_rate=0.2,      # No random loss - ACKs won't be dropped
        # drop_sequences={3, 7, 12}   # Drop these specific packets to test retransmission
    ))
