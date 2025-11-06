import asyncio
import json
import time
from typing import Dict, Any, List, Tuple, Optional
from metrics import RollingStats, Jitter

METRIC_SUMMARY_EVERY_S = 5.0 #how many seconds between esach metric summary

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
from network_emulator import NetworkEmulator

def get_timestamp():
    """Return formatted timestamp for logging: HH:MM:SS.mmm"""
    now = time.time()
    ms = int((now * 1000) % 1000)
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{ms:03d}"

ALPN = "game/1"
RELIABLE_TIMEOUT_MS = 200  # T milliseconds threshold

def make_server_cfg() -> QuicConfiguration:
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.max_datagram_frame_size = 1200
    # ⚠️ Make sure these files exist (see: openssl command I gave earlier)
    cfg.load_cert_chain("server.crt", "server.key")
    return cfg

class GameServer(QuicConnectionProtocol):
    def __init__(self, *args, emulator: Optional[NetworkEmulator] = None, **kwargs):
        super().__init__(*args, **kwargs)
        # Application-layer buffer for reliable packets {app_seq: (timestamp_rx, data_bytes)}
        self.reliable_buffer: Dict[int, Tuple[float, bytes]] = {}
        self.next_expected_seq = 0
        # Track when we started waiting for each missing sequence {seq: wait_start_ts}
        self.missing_seq_wait_start: Dict[int, float] = {}
        self.flush_task: Optional[asyncio.Task] = None
        self._last_counters = {"rel_rx": 0, "unrel_rx": 0, "bytes": 0}
        
        # Network emulator (defaults to disabled if not provided)
        self.emulator = emulator if emulator is not None else NetworkEmulator(enabled=False)

        self._start_time = time.time()
        self.metrics_task: Optional[asyncio.Task] = None
    
    def _safe_transmit(self):
        """Safely handle async transmit in both sync and async contexts."""
        if not self.emulator.enabled:
            # If emulation disabled, just call transmit directly
            self.transmit()
        else:
            # If emulation enabled, create async task if loop is running
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.emulator.transmit(self.transmit))
            except RuntimeError:
                # No running loop, just call transmit directly (fallback)
                self.transmit()
        #set up metric storage
        self.metrics = {
            "reliable": {
                "owl": RollingStats(),
                "jitter": Jitter(),
                "rx": 0, "stale": 0, "ooo": 0, "dup": 0, "bytes": 0
            },
            "unreliable": {
                "owl": RollingStats(),
                "jitter": Jitter(),
                "rx": 0, "bytes": 0
            }
        }

    def _had_activity_since_last(self):
        r = self.metrics["reliable"];
        u = self.metrics["unreliable"]
        cur = {
            "rel_rx": r.get("rx", 0),
            "unrel_rx": u.get("rx", 0),
            "bytes": r.get("bytes", 0) + u.get("bytes", 0),
        }
        changed = any(cur[k] != self._last_counters[k] for k in cur)
        self._last_counters = cur
        return changed

    def _print_metrics_summary(self):
        now = time.time()
        print("\n[server] 📊 ---- METRIC SUMMARY ----")
        for pkt_type in ("reliable", "unreliable"):
            m = self.metrics[pkt_type]
            dur = max(1e-6, now - self._start_time)
            tput = m.get("bytes", 0) / 1024.0 / dur #kB/s

            header = f"[metrics][{pkt_type.upper()}] RX={m.get('rx', 0)} Bytes={m.get('bytes', 0)}"
            if pkt_type == "reliable":
                header += (
                    f" Stale={m.get('stale', 0)}"
                    f" OOO={m.get('ooo', 0)}"
                    f" Dup={m.get('dup', 0)}"
                )
            print(header)

            owl_stats = m.get("owl")
            if owl_stats and len(owl_stats.samples) > 0:
                p = owl_stats.percentiles()
                avg_owl = owl_stats.avg()
                jitter_val = m["jitter"].value()
                print(
                    f"    OWL(ms): avg={avg_owl:.2f}, "
                    f"p50={p.get(50, float('nan')):.2f}, "
                    f"p95={p.get(95, float('nan')):.2f}, "
                    f"jitter(RFC3550)={jitter_val:.2f}"
                )
            else:
                print("    OWL(ms): no samples")

            print(f"    Throughput ≈ {tput:.2f} kB/s")
        print("[server] --------------------------\n")

    def connection_made(self, transport):
        super().connection_made(transport)
        # Part e background task to flush the reliable packet buffer RDT
        self.flush_task = asyncio.create_task(self._flush_reliable_data())
        self.metrics_task = asyncio.create_task(self._periodic_metrics_print())

    def connection_lost(self, exc):
        super().connection_lost(exc)
        if self.flush_task:
            self.flush_task.cancel()
        if self.metrics_task:
            self.metrics_task.cancel()

    async def _periodic_metrics_print(self):
        try:
            while True:
                await asyncio.sleep(METRIC_SUMMARY_EVERY_S)
                if self._had_activity_since_last():
                    self._print_metrics_summary()
                    #send heartbeat only when there was recent activity
                    try:
                        stats = {
                            "type": "server_metrics",
                            "unrel_rx": self.metrics["unreliable"]["rx"],
                            "rel_rx": self.metrics["reliable"]["rx"],
                        }
                        sid = self._quic.get_next_available_stream_id(is_unidirectional=False)
                        self._quic.send_stream_data(sid, json.dumps(stats).encode(), end_stream=False)
                        await self.emulator.transmit(self.transmit)
                    except Exception as e:
                        print(f"[{get_timestamp()}] [server] Warning: failed to send metrics heartbeat: {e}")
        except asyncio.CancelledError:
            pass

    async def _flush_reliable_data(self):
        
        try:
            while True:
                await asyncio.sleep(0.01)  # Check frequently
                now = time.time()
                skipped_this_cycle = False
                
                # Part E : Retransmission based on timer, and if any packet is lost and retransmision not reached by RELIABLE_TIMEOUT_MS, skip and display the rest
                if self.next_expected_seq not in self.reliable_buffer:
                    # Track when we started waiting for this missing sequence
                    if self.next_expected_seq not in self.missing_seq_wait_start:
                        self.missing_seq_wait_start[self.next_expected_seq] = now
                    
                    # Check if we've been waiting for this sequence for > RELIABLE_TIMEOUT_MS
                    wait_start_ts = self.missing_seq_wait_start[self.next_expected_seq]
                    time_waiting_for_missing = (now - wait_start_ts) * 1000
                    
                    # Find the smallest sequence number in buffer that's greater than next_expected_seq
                    later_sequences = [seq for seq in self.reliable_buffer.keys() if seq > self.next_expected_seq]
                    
                    if later_sequences:
                        # Get the smallest later sequence (first out-of-order packet received)
                        next_available_seq = min(later_sequences)
                        (rx_ts, _) = self.reliable_buffer[next_available_seq]
                        time_waiting_for_later = (now - rx_ts) * 1000
                        
                        # Only skip if:
                        # 1. We've been waiting for the missing packet for > RELIABLE_TIMEOUT_MS
                        # 2. The later packet has also been waiting long enough (to ensure it's actually out-of-order)
                        # 3. The later packet arrived AFTER we started waiting for the missing packet
                        #    (This prevents skipping a packet that just became expected - give it a full timeout period)
                        #    If the later packet arrived before we started waiting, it means we just skipped the previous packet
                        #    and should give the new expected packet a chance to arrive
                        later_packet_arrived_after_wait_start = rx_ts >= wait_start_ts
                        
                        if time_waiting_for_missing > RELIABLE_TIMEOUT_MS and time_waiting_for_later > RELIABLE_TIMEOUT_MS and later_packet_arrived_after_wait_start:
                            skipped_seq = self.next_expected_seq
                            self.metrics["reliable"]["stale"] += 1
                            print(f"[{get_timestamp()}] [server] ⚠️ RELIABLE SKIP: Seq={skipped_seq} skipped. Waited {time_waiting_for_missing:.2f}ms, Next Seq={next_available_seq} waited {time_waiting_for_later:.2f}ms > {RELIABLE_TIMEOUT_MS}ms.")
                            
                            # Send skip notification to client so it can stop retransmitting
                            skip_notification = json.dumps({
                                "type": "skip",
                                "seq": skipped_seq,
                                "next_seq": next_available_seq
                            }).encode()
                            skip_stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
                            self._quic.send_stream_data(skip_stream_id, skip_notification, end_stream=False)
                            await self.emulator.transmit(self.transmit)  # Use await to ensure it's sent
                            
                            # Clean up tracking and skip the missing packet
                            self.missing_seq_wait_start.pop(self.next_expected_seq, None)
                            self.next_expected_seq += 1
                            skipped_this_cycle = True
                            
                            # When we skip a packet, the next packet becomes expected
                            # Don't start tracking its wait time immediately - give it a chance to arrive
                            # The next iteration will start tracking if it's still missing
                            continue

                # Part C : Reorder if not in order (and D)
                while self.next_expected_seq in self.reliable_buffer:
                    # Packet arrived, remove from missing tracking
                    self.missing_seq_wait_start.pop(self.next_expected_seq, None)
                    rx_ts, data_bytes = self.reliable_buffer.pop(self.next_expected_seq)
                    try:
                        
                        packet = json.loads(data_bytes.decode())
                        
                        # one-way latency OWL
                        sent_ts = packet.get("ts", now)
                        owl_ms = (rx_ts - sent_ts) * 1000
                        
                        # Logging
                        print(f"[{get_timestamp()}] [server] ✅ RELIABLE RX: Seq={self.next_expected_seq}, OWL={owl_ms:.2f}ms, Data={packet.get('data')}")

                        m_r = self.metrics["reliable"]
                        m_r["rx"] += 1
                        m_r["bytes"] += len(data_bytes)
                        m_r["owl"].add(owl_ms)
                        m_r["jitter"].add(owl_ms)

                        # Echo ACK for RTT calculation on client side
                        self._quic.send_stream_data(self._quic.get_next_available_stream_id(is_unidirectional=False), b"ack:" + data_bytes, end_stream=False)
                        await self.emulator.transmit(self.transmit)

                    except Exception as e:
                        print(f"[{get_timestamp()}] [server] Error processing reliable packet: {e}")
                    
                    self.next_expected_seq += 1

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[{get_timestamp()}] [server] Flush task error: {e}")


    def quic_event_received(self, event) -> None:
        if isinstance(event, HandshakeCompleted):
            peer = self._quic._peer_address if hasattr(self._quic, "_peer_address") else "<peer>"
            print(f"[{get_timestamp()}] [server] Handshake completed with {peer}")
            # Send initial reliable hello on a fresh bidirectional stream
            stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
            self._quic.send_stream_data(stream_id, json.dumps({"type": "server_hello"}).encode(), end_stream=False)
            self._safe_transmit()

        elif isinstance(event, StreamDataReceived):
            # buffer here
            rx_ts = time.time()
            r_str = "reliable"
            
            try:
                data_str = event.data.decode()
                if data_str.startswith('{"type": "client_hello"}'):
                    print(f"[{get_timestamp()}] [server] [Reliable] Initial client_hello received.")
                    return

                packet = json.loads(data_str)
                app_seq = packet.get("seq")
                pkt_ts = packet.get("ts")
                
                if app_seq is None or pkt_ts is None:
                    print(f"[{get_timestamp()}] [server] [Reliable] RC: missing seq/ts: {packet}.")
                    return

                if app_seq < self.next_expected_seq:
                    self.metrics[r_str]["dup"] = self.metrics[r_str].get("dup", 0) + 1
                    print(f"[{get_timestamp()}] [server] 🔄 [Reliable] RX: Seq={app_seq}, current expected={self.next_expected_seq}")
                    return

                # part e buffer - only store if not already buffered (preserve original arrival time)
                if app_seq not in self.reliable_buffer:
                    # First time receiving this sequence - store with current timestamp
                    self.reliable_buffer[app_seq] = (rx_ts, event.data)
                else:
                    # Duplicate retransmission - keep original timestamp, don't overwrite
                    self.metrics[r_str]["dup"] = self.metrics[r_str].get("dup", 0) + 1
                    # Don't update the buffer entry - keep the original arrival time

                if app_seq > self.next_expected_seq:
                    # part g: Packet arrived out-of-order
                    self.metrics[r_str]["ooo"] += 1
                    print(f"[{get_timestamp()}] [server] ⏳ [Reliable] RX: Seq={app_seq} (O.O.O), Expected={self.next_expected_seq}. Buffering...")

                else:
                    print(f"[{get_timestamp()}] [server] [Reliable] RX: Unsequenced data: {event.data!r}")
            
            except json.JSONDecodeError:
                print(f"[{get_timestamp()}] [server] [Reliable] RX: Non-JSON data on stream {event.stream_id}")


        elif isinstance(event, DatagramFrameReceived):
            # Unreliable from client (Movement data) Part g
            rx_ts = time.time()
            
            # header parse
            seq = int.from_bytes(event.data[2:4], "big")
            payload = event.data[4:]
            
            try:
                
                payload_str = payload.decode()
                sent_ts_str = payload_str.split("ts:")[1]
                sent_ts = float(sent_ts_str)
                
                owl_ms = (rx_ts - sent_ts) * 1000

                m_u = self.metrics["unreliable"]
                m_u["rx"] += 1
                m_u["bytes"] += len(event.data)
                m_u["owl"].add(owl_ms)
                m_u["jitter"].add(owl_ms)
                
                # Logging
                print(f"[{get_timestamp()}] [server] ⏩ [Unreliable] RX: Seq={seq}, OWL={owl_ms:.2f}ms, Data={payload_str.split(',ts:')[0]}")
            except Exception:
                print(f"[{get_timestamp()}] [server] ⏩ [Unreliable] RX: Seq={seq}, Data={payload!r}")


async def main(host="0.0.0.0", port=4433,
               emulation_enabled: bool = False, delay_ms: float = 0,
               jitter_ms: float = 0, packet_loss_rate: float = 0.0,
               drop_sequences: Optional[set] = None):
    """
    Main server function.
    
    Args:
        host: Server hostname
        port: Server port
        emulation_enabled: Enable network emulation
        delay_ms: Base delay in milliseconds
        jitter_ms: Jitter variation in milliseconds
        packet_loss_rate: Packet loss rate (0.0 to 1.0)
        drop_sequences: Set of sequence numbers to selectively drop (not used on server side)
    """
    cfg = make_server_cfg()
    
    # Create network emulator
    emulator = NetworkEmulator(
        enabled=emulation_enabled,
        delay_ms=delay_ms,
        jitter_ms=jitter_ms,
        packet_loss_rate=packet_loss_rate,
        drop_sequences=drop_sequences or set()
    )
    
    # Create protocol factory that passes emulator to each connection
    def create_protocol(*args, **kwargs):
        return GameServer(*args, emulator=emulator, **kwargs)
    
    await serve(host, port, configuration=cfg, create_protocol=create_protocol)
    print(f"[{get_timestamp()}] [server] QUIC listening on {host}:{port} (ALPN={ALPN})")
    print(f"[{get_timestamp()}] [server] Reliable data skip threshold (T): {RELIABLE_TIMEOUT_MS} ms.")
    if emulation_enabled:
        print(f"[{get_timestamp()}] [server] Network emulation: delay={delay_ms}ms, jitter={jitter_ms}ms, loss={packet_loss_rate:.2%}")
    # ⛔️ Keep the server running forever
    await asyncio.Event().wait()

if __name__ == "__main__":
    # Test retransmission: no random loss, ACKs will get through
    asyncio.run(main(
        emulation_enabled=True,
        delay_ms=0,
        jitter_ms=0,
        packet_loss_rate=0.0,     # No random loss - ACKs won't be dropped
        drop_sequences=set()      # Not used on server side
    ))
