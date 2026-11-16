# PersonalServer — Project Charter & Vision

**Owner:** Akshat Sahay  
**Initial Baseline:** November 2026  
**License:** MIT  

---

## 1. Executive Summary

PersonalServer is an autonomous, decentralized, and modular personal infrastructure platform designed to unify recycled and existing personal hardware into a resilient, private server cluster.

In an era dominated by proprietary cloud services and recurring subscription hardware, PersonalServer reclaims compute sovereignty. It transforms spare Android smartphones, older laptops, Raspberry Pis, and desktop workstations into interconnected nodes capable of hosting web services, executing batch workflows, streaming live hardware cameras, and running personal automation.

---

## 2. Core Pillars

1. **Hardware Inclusivity & Recycled Hardware:**
   Consumer hardware—especially Android smartphones—packs multi-core CPUs, low-power memory, wireless radios, and built-in battery backups (UPS). PersonalServer treats these devices as primary production compute nodes, not toys.

2. **Zero External Runtime Dependencies:**
   The entire core control plane, agent, consensus engine, and scheduling engine are written using the standard library of **Python 3.9+**. It operates out of the box without requiring third-party database servers, message queues, or orchestrators.

3. **Geographic Mesh Networking:**
   Nodes are not confined to a single local home network. PersonalServer unites nodes located across different cities and states (e.g. Punjab and Gujarat) into an encrypted **Tailscale WireGuard Mesh** (`100.x.y.z`), with secure web access provided by Cloudflare Zero Trust.

4. **Hardware Capability Bridging:**
   Through an unprivileged companion app (`com.personalserver.bridge`), Android hardware features (Camera2, microphone, sensors, power wake-locks) are safely bridged to the Linux/Termux environment over local loopback interfaces.

5. **On-Demand Utility Model:**
   High-power hardware features—such as live camera streaming and audio capture—operate strictly on-demand. When a live session ends, hardware locks are completely released, preserving battery longevity and ensuring thermal safety.

---

## 3. Long-Term Horizons

While the immediate priority focuses on stabilizing Android nodes and camera streaming, PersonalServer is architected to evolve into:
- **Federated Edge Storage:** Encrypted, distributed file storage and automatic snapshot backups across nodes.
- **Local AI & SLM Inference:** Offloading small language models (SLMs) and neural tasks onto capable edge nodes (NNAPI, GPU, CPU).
- **Personal Automation Engine:** Event-driven automation workflows executing autonomously across the cluster.
- **Autonomous Cluster Healing:** Intelligent auto-rebalancing, thermal mitigation, and self-healing across nodes.
