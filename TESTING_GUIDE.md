# Network Emulator Testing Guide

The network emulator has been successfully integrated into both `client.py` and `server.py`. This guide shows you how to test various network scenarios.

## 🎯 Test Scenarios

### 1. Test Retransmission with Selective Packet Drops

**Goal:** Verify that client retransmits dropped packets and server handles missing packets correctly.

**Client Configuration (`client.py`):**
```python
asyncio.run(main(
    emulation_enabled=True,
    delay_ms=0,
    jitter_ms=0,
    packet_loss_rate=0.0,
    drop_sequences={3, 7, 12}  # Drop specific reliable packets
))
```

**Server Configuration (`server.py`):**
```python
asyncio.run(main())  # No emulation - ACKs arrive cleanly
```

**What to Observe:**
- Client logs show `RETRANSMIT: Seq=3/7/12` multiple times
- Server logs show `RELIABLE SKIP: Seq=3/7/12` after 200ms timeout
- Other packets (4, 5, 6, 8, 9, etc.) are ACKed immediately without retransmission
- Metrics show `RETX > 0` and `Stale > 0`

---

### 2. Test Packet Reordering with Jitter

**Goal:** Verify that server buffers out-of-order packets and delivers them in sequence.

**Client Configuration (`client.py`):**
```python
asyncio.run(main(
    emulation_enabled=True,
    delay_ms=10,
    jitter_ms=50,  # High jitter causes reordering
    packet_loss_rate=0.0,
    drop_sequences=set()  # No drops
))
```

**Server Configuration (`server.py`):**
```python
asyncio.run(main())  # No emulation
```

**What to Observe:**
- Server logs show `⏳ [Reliable] RX: Seq=X (O.O.O), Expected=Y. Buffering...`
- Later: `✅ RELIABLE RX: Seq=Y` (missing packet arrives)
- Then: `✅ RELIABLE RX: Seq=X` (buffered packet delivered in order)
- Metrics show `OOO > 0` (out-of-order packets received)
- NO `Stale` count (all packets eventually arrive)

---

### 3. Test Combined: Retransmission + Reordering

**Goal:** Verify both mechanisms work together.

**Client Configuration (`client.py`):**
```python
asyncio.run(main(
    emulation_enabled=True,
    delay_ms=10,
    jitter_ms=30,  # Moderate jitter
    packet_loss_rate=0.0,
    drop_sequences={5, 15}  # A few drops
))
```

**Server Configuration (`server.py`):**
```python
asyncio.run(main())  # No emulation
```

**What to Observe:**
- Mix of reordering (buffering) and retransmission (dropped packets)
- Metrics show both `OOO > 0` and `Stale > 0`
- Some packets delivered in order from buffer, others skipped after timeout

---

### 4. Test Random Packet Loss

**Goal:** Simulate realistic network with random packet loss.

**Client Configuration (`client.py`):**
```python
asyncio.run(main(
    emulation_enabled=True,
    delay_ms=20,
    jitter_ms=10,
    packet_loss_rate=0.1,  # 10% random loss
    drop_sequences=set()
))
```

**Server Configuration (`server.py`):**
```python
asyncio.run(main())  # No emulation
```

**What to Observe:**
- Random packets (both reliable and unreliable) are dropped
- Client retransmits dropped reliable packets
- Emulator stats show ~10% drop rate
- Note: ACK loss can also occur, affecting retransmission timing

---

### 5. Test Bidirectional Emulation

**Goal:** Simulate network conditions in both directions.

**Client Configuration (`client.py`):**
```python
asyncio.run(main(
    emulation_enabled=True,
    delay_ms=20,
    jitter_ms=15,
    packet_loss_rate=0.05,  # 5% loss on data packets
))
```

**Server Configuration (`server.py`):**
```python
asyncio.run(main(
    emulation_enabled=True,
    delay_ms=10,
    jitter_ms=5,
    packet_loss_rate=0.02  # 2% loss on ACKs
))
```

**What to Observe:**
- Delays and jitter affect both data packets and ACKs
- ACK loss causes client to wait longer before retransmitting
- More realistic simulation of real-world network behavior

---

## 📊 Key Metrics to Monitor

### Client Metrics
- **TX**: Total reliable packets sent
- **ACK**: Packets acknowledged by server
- **RETX**: Retransmitted packets
- **PDR**: Packet Delivery Rate (ACK/TX)
- **RTT**: Round-trip time statistics (avg, p50, p95)
- **Emulator Stats**: Total packets processed, dropped count, drop rate

### Server Metrics
- **RX**: Total packets received
- **Stale**: Packets skipped due to timeout (>200ms)
- **OOO**: Out-of-order packets received
- **Dup**: Duplicate packets (retransmissions that arrived after original)
- **OWL**: One-way latency (from packet timestamp)
- **Jitter**: Variation in packet arrival times

---

## 🔧 How to Run Tests

### Step 1: Start Server
```bash
cd "/path/to/Assignment 4"
source .venv/bin/activate
python3 server.py
```

### Step 2: Configure Test Scenario
Edit `client.py` or `server.py` to uncomment the desired test configuration.

### Step 3: Run Client
```bash
# In a new terminal
cd "/path/to/Assignment 4"
source .venv/bin/activate
python3 client.py
```

### Step 4: Observe Output
- Client terminal: Shows sent packets, ACKs, retransmissions, and final metrics
- Server terminal: Shows received packets, buffering, skips, and metrics
- Both show emulator statistics if enabled

---

## 🎓 Understanding the Output

### Example: Selective Drop Test

**Client Output:**
```
[client] [Reliable] SENT: Seq=3, Size=112, TS=1762416422.3486
[emulator] 🎯 SELECTIVE DROP: Seq=3 (configured drop list), total dropped: 1/5
[client] [Reliable] RETRANSMIT: Seq=3, Retry=1, Timeout=100.0ms
[client] [Reliable] RETRANSMIT: Seq=3, Retry=2, Timeout=100.0ms
[client] [Reliable] RETRANSMIT: Seq=3, Retry=3, Timeout=100.0ms
```
➡️ Seq 3 was dropped, client is retransmitting

**Server Output:**
```
[server] ⏳ [Reliable] RX: Seq=4 (O.O.O), Expected=3. Buffering...
[server] ⏳ [Reliable] RX: Seq=5 (O.O.O), Expected=3. Buffering...
[server] ⚠️ RELIABLE SKIP: Seq=3 skipped. Waited 210.5ms > 200ms.
[server] ✅ RELIABLE RX: Seq=4, OWL=5.23ms, Data={...}
[server] ✅ RELIABLE RX: Seq=5, OWL=6.15ms, Data={...}
```
➡️ Server buffered packets 4 and 5, waited for 3, then skipped it and delivered 4, 5 in order

---

## 💡 Tips for Testing

1. **Start Simple**: Begin with no emulation to verify basic functionality
2. **Test One Thing**: Use selective drops OR jitter, not both initially
3. **Check Both Sides**: Compare client and server logs to understand flow
4. **Watch Timestamps**: Use timestamps to verify delays and timing
5. **Adjust Parameters**: Tweak delay, jitter, and loss rates to see different behaviors
6. **Clean Runs**: Kill old server processes before starting: `pkill -f "python3 server.py"`

---

## 🐛 Troubleshooting

### Server won't start (Address already in use)
```bash
lsof -ti:4433 | xargs kill -9
```

### No packets being dropped
- Verify `emulation_enabled=True`
- Check `drop_sequences` set is correct
- Ensure `packet_loss_rate` is > 0 if testing random loss

### All packets being retransmitted
- Check if ACKs are also being dropped (server-side emulation)
- Reduce server-side packet loss for cleaner tests
- Verify client retransmission timeout is reasonable

### No reordering observed
- Increase `jitter_ms` (try 50-100ms)
- Increase base `delay_ms` to amplify jitter effect
- Run for longer duration to see more packets

---

## 📝 Example Test Run

```bash
# Terminal 1 - Server (no emulation)
python3 server.py

# Terminal 2 - Client (test reordering)
# Edit client.py to enable jitter test (already configured)
python3 client.py

# Observe output:
# - Client sends packets with variable delays
# - Server receives them out of order
# - Server buffers and delivers in order
# - Final metrics show OOO count
```

---

## 🎉 Success Criteria

Your implementation is working correctly if:

✅ **Retransmission**: Dropped packets are retransmitted by client  
✅ **Timeout & Skip**: Server skips packets after 200ms, sends skip notification  
✅ **Reordering**: Server buffers out-of-order packets and delivers in sequence  
✅ **Metrics**: All metrics (RETX, Stale, OOO, Dup, PDR) are tracked correctly  
✅ **Emulator**: Drop rates match configuration, timestamps show delays  


