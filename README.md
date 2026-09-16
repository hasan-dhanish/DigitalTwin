# 🏙️ City Digital Twin Real-Time Synchronization Engine
**QNX eHACK 2026 — Problem Statement #48**

This repository contains the starter codebase and production RTOS implementation for the **City Digital Twin Real-Time Synchronization Engine** under **QNX Neutrino RTOS**.

---

## 📁 Repository Structure

```
QNX/
├── config/
│   └── system_config.json        # Stream definitions, task priorities & deadline parameters
├── src/
│   ├── twin_engine.py            # Main POSIX Python engine & CLI dashboard
│   ├── sensor_simulators.py      # Multi-rate sensor generator threads
│   └── qnx_twin_engine.c         # Production C / POSIX implementation for QNX target
├── run.py                        # Root launcher script (Python)
├── Makefile                      # Build configuration for QNX (qcc) & GCC
└── README.md                     # Documentation & Launch Guide
```

---

## 🚀 Quick Start (Running the Engine)

### 1. Run Python RTOS Simulation (Cross-Platform)
To run the full multi-threaded synchronization engine with live terminal telemetry:

```bash
python run.py
```

### 2. Compile & Run Production C Implementation (QNX / Linux Target)

#### On QNX Target (using `qcc`):
```bash
qcc -Vgcc_ntox86_64 -O2 src/qnx_twin_engine.c -o twin_engine -lrt
./twin_engine
```

#### On Linux / WSL (using `gcc`):
```bash
make
./twin_engine
```

---

## ⚙️ How It Works

### Multi-Rate Ingestion & Priority Hierarchy
1. **Twin Synchronizer Core (`Priority Level 255 - Highest`)**: Atomic snapshot generation across all multi-rate ring queues every $10\text{ ms}$, enforcing bounded sync latency ($<15.0\text{ ms}$).
2. **Watchdog Monitor (`Priority Level 200`)**: Scans streams every $50\text{ ms}$. If telemetry lags past $150\text{ ms}$, it flags the stream as `STALE FAULT` and degrades system state safely.
3. **Data Acquisition Threads (`Priority Level 150`)**:
   - `Traffic Telemetry`: $50\text{ ms}$ rate
   - `Smart Power Grid`: $100\text{ ms}$ rate
   - `Water Pressure`: $500\text{ ms}$ rate
   - `Air Quality`: $1000\text{ ms}$ rate
4. **CLI Dashboard (`Priority Level 50 - Lowest`)**: Renders real-time telemetry, freshness metrics, latency peaks, and deadline miss count.

---

## 🎮 Interactive Fault Injection

When running `python run.py`, press keys to simulate hardware anomalies live during your demo:
- **`1`**: Simulate Traffic Sensor Disconnection (Triggers Watchdog `STALE FAULT`).
- **`2`**: Inject 500ms Network Transmission Spike (Triggers `DEADLINE BREACH`).
- **`3`**: Overload Background CPU Budget.
- **`R`**: Reset System to Nominal State.
- **`Q`**: Exit Engine.
