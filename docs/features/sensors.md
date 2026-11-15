# Feature: Telemetry & Hardware Sensors

**Subsystem:** SensorManager & Rich Device Telemetry  
**Implementation:** `SensorBridgeManager.java`, `config/node_telemetry.py`, `agent/device_runtime/android/providers/`  

---

## 1. Overview

PersonalServer collects comprehensive physical telemetry from edge nodes to optimize cluster scheduling, monitor node battery health, and enforce automated thermal safeguards.

---

## 2. Telemetry Channels & Metrics

1. **Power & Battery Telemetry:**
   - **Battery Percentage:** `0–100%` state of charge.
   - **Charging Status:** `CHARGING_AC`, `CHARGING_USB`, `DISCHARGING`, `FULL`.
   - **Battery Health:** `GOOD`, `OVERHEAT`, `DEAD`, `OVER_VOLTAGE`.
   - **Battery Temperature:** Measured in tenths of a degree Celsius (e.g. `34.5°C`).
2. **Thermal & CPU Metrics:**
   - **CPU Temperature:** Read via Android `/sys/class/thermal/thermal_zone*` sysfs interfaces or `vcgencmd` on Raspberry Pi.
   - **Thermal Status:** `NORMAL`, `THROTTLED`, `CRITICAL`.
3. **Network Connectivity:**
   - **Signal Strength:** Wi-Fi RSSI measured in dBm (e.g. `-58 dBm`).
   - **Link Speed:** Wi-Fi connection link speed in Mbps.
   - **Interface Type:** `WIFI`, `CELLULAR`, `ETHERNET`.
4. **Hardware Sensors:**
   - Ambient Light Sensor (`TYPE_LIGHT` in lux).
   - Accelerometer & Gyroscope (orientation detection).

---

## 3. Active Thermal Safeguard (Milestone M3)

To safeguard Li-ion batteries on 24/7 connected devices:
- **Warning Threshold (40°C):** Telemetry dashboard displays an orange warning badge.
- **Throttling Threshold (42°C):** Active CCTV video stream throttles frame rate to 5 FPS; non-critical background jobs are suspended.
- **Emergency Halt (45°C):** Camera sensor is immediately closed; high-load processes are paused until battery temperature drops below 38°C.
