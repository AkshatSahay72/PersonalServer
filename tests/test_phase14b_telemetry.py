#!/usr/bin/env python3
"""
Unit and Integration Tests for Phase 14B:
Cluster Resource Telemetry & Utilization Foundation

Tests:
1. Linux telemetry parsing (proc/meminfo, loadavg, disk)
2. Windows telemetry parsing (GlobalMemoryStatusEx, GetSystemTimes, disk)
3. Missing metrics handling (safe fallbacks, no invented metrics)
4. Zero and invalid values (zero-division safety, negative bounds)
5. Workload counts (active and running jobs per node)
6. Docker workload counts (running container counting, stopped excluded)
7. Offline node handling (offline nodes MUST NOT contribute stale capacity)
8. Cluster aggregation math (summing online nodes only)
9. Scheduler telemetry data structure integration
10. GET /cluster/utilization schema compliance
"""

import os
import sys
import json
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from scheduler.scheduler import (
    ResourceScheduler,
    compute_node_liveness,
    extract_node_telemetry,
    parse_memory_mb,
    parse_storage_gb,
    STATE_ONLINE,
    STATE_OFFLINE,
    STATE_UNHEALTHY,
    STATE_REMOVED,
    STATE_UNKNOWN
)
from controller.controller import get_cluster_utilization


class TestPhase14BTelemetry(unittest.TestCase):

    def setUp(self):
        self.now_iso = datetime.now(timezone.utc).isoformat()
        self.stale_iso = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()

    def test_01_linux_telemetry_parsing(self):
        """Verify Linux/Termux metrics extraction and legacy string fallback parsing."""
        # 1. Structured Linux heartbeat with memory_details and storage_details
        linux_node = {
            "node_id": "node-linux-01",
            "name": "vivo-y31",
            "role": "compute",
            "platform": "termux",
            "status": "ONLINE",
            "last_seen": self.now_iso,
            "resources": {"cpu_cores": 8, "ram_mb": 5697, "storage_gb": 106},
            "last_heartbeat": {
                "timestamp": self.now_iso,
                "system": {
                    "cpu_cores": 8,
                    "load_average": [0.42, 0.38, 0.25],
                    "memory": "2.1Gi/5.6Gi",
                    "memory_details": {
                        "total_mb": 5697.0,
                        "used_mb": 2150.0,
                        "available_mb": 3547.0,
                        "used_percent": 37.7
                    },
                    "storage": "11G/106G (10%)",
                    "storage_details": {
                        "total_gb": 106.0,
                        "used_gb": 11.2,
                        "available_gb": 94.8,
                        "used_percent": 10.6
                    }
                },
                "workloads": {"running_containers": 0}
            },
            "capabilities": {"compute": True, "storage": True}
        }

        tel = extract_node_telemetry(linux_node, status=STATE_ONLINE)
        self.assertEqual(tel["status"], STATE_ONLINE)
        self.assertEqual(tel["cpu"]["cores"], 8)
        self.assertEqual(tel["cpu"]["load_average"], [0.42, 0.38, 0.25])
        self.assertIsNone(tel["cpu"].get("cpu_percent"))
        self.assertEqual(tel["memory"]["total_mb"], 5697.0)
        self.assertEqual(tel["memory"]["used_mb"], 2150.0)
        self.assertEqual(tel["memory"]["available_mb"], 3547.0)
        self.assertEqual(tel["memory"]["used_percent"], 37.7)
        self.assertEqual(tel["storage"]["total_gb"], 106.0)
        self.assertEqual(tel["storage"]["used_gb"], 11.2)
        self.assertEqual(tel["storage"]["available_gb"], 94.8)
        self.assertEqual(tel["storage"]["used_percent"], 10.6)

        # 2. Legacy Linux heartbeat without _details dict (string fallback)
        legacy_node = {
            "node_id": "node-legacy",
            "resources": {"cpu_cores": 4},
            "last_seen": self.now_iso,
            "last_heartbeat": {
                "system": {
                    "cpu_cores": 4,
                    "load_average": [1.10],
                    "memory": "2.0Gi/4.0Gi",
                    "storage": "20G/100G (20%)"
                }
            }
        }
        leg_tel = extract_node_telemetry(legacy_node, status=STATE_ONLINE)
        self.assertEqual(leg_tel["cpu"]["cores"], 4)
        self.assertEqual(leg_tel["memory"]["total_mb"], 4096.0)
        self.assertEqual(leg_tel["memory"]["used_mb"], 2048.0)
        self.assertEqual(leg_tel["memory"]["available_mb"], 2048.0)
        self.assertEqual(leg_tel["memory"]["used_percent"], 50.0)
        self.assertEqual(leg_tel["storage"]["total_gb"], 100.0)
        self.assertEqual(leg_tel["storage"]["used_gb"], 20.0)
        self.assertEqual(leg_tel["storage"]["available_gb"], 80.0)
        self.assertEqual(leg_tel["storage"]["used_percent"], 20.0)

    def test_02_windows_telemetry_parsing(self):
        """Verify Windows node telemetry parsing (cpu_percent, memory_details, storage_details)."""
        win_node = {
            "node_id": "node-win-03",
            "name": "node-03-laptop",
            "role": "compute",
            "platform": "windows",
            "status": "ONLINE",
            "last_seen": self.now_iso,
            "resources": {"cpu_cores": 12, "ram_mb": 24771, "storage_gb": 476},
            "last_heartbeat": {
                "timestamp": self.now_iso,
                "system": {
                    "cpu_cores": 12,
                    "cpu_percent": 4.5,
                    "load_average": None,
                    "memory": "14.2Gi/24.2Gi",
                    "memory_details": {
                        "total_mb": 24771.8,
                        "used_mb": 14500.2,
                        "available_mb": 10271.6,
                        "used_percent": 58.5
                    },
                    "storage": "184.2G/476.0G (39%)",
                    "storage_details": {
                        "total_gb": 476.0,
                        "used_gb": 184.2,
                        "available_gb": 291.8,
                        "used_percent": 38.7
                    }
                },
                "workloads": {"running_containers": 3}
            },
            "capabilities": {"compute": True, "docker": True, "container_runtime:docker": True}
        }

        tel = extract_node_telemetry(win_node, status=STATE_ONLINE)
        self.assertEqual(tel["status"], STATE_ONLINE)
        self.assertEqual(tel["cpu"]["cores"], 12)
        self.assertEqual(tel["cpu"]["cpu_percent"], 4.5)
        # Windows does not have unix load average; ensure it is not invented
        self.assertNotIn("load_average", tel["cpu"])
        self.assertEqual(tel["memory"]["total_mb"], 24771.8)
        self.assertEqual(tel["memory"]["used_mb"], 14500.2)
        self.assertEqual(tel["memory"]["available_mb"], 10271.6)
        self.assertEqual(tel["memory"]["used_percent"], 58.5)
        self.assertEqual(tel["storage"]["total_gb"], 476.0)
        self.assertEqual(tel["storage"]["used_gb"], 184.2)
        self.assertEqual(tel["storage"]["available_gb"], 291.8)
        self.assertEqual(tel["storage"]["used_percent"], 38.7)
        self.assertEqual(tel["workloads"]["running_containers"], 3)

    def test_03_missing_metrics_handling(self):
        """Verify missing metrics default safely without error or inventing data."""
        empty_node = {
            "node_id": "empty-node"
        }
        tel = extract_node_telemetry(empty_node, status=STATE_UNKNOWN)
        self.assertEqual(tel["status"], STATE_UNKNOWN)
        self.assertEqual(tel["cpu"]["cores"], 1)
        self.assertNotIn("cpu_percent", tel["cpu"])
        self.assertNotIn("load_average", tel["cpu"])
        self.assertEqual(tel["memory"]["total_mb"], 0.0)
        self.assertEqual(tel["memory"]["used_mb"], 0.0)
        self.assertEqual(tel["memory"]["available_mb"], 0.0)
        self.assertEqual(tel["memory"]["used_percent"], 0.0)
        self.assertEqual(tel["storage"]["total_gb"], 0.0)
        self.assertEqual(tel["storage"]["used_gb"], 0.0)
        self.assertEqual(tel["storage"]["available_gb"], 0.0)
        self.assertEqual(tel["storage"]["used_percent"], 0.0)
        self.assertEqual(tel["workloads"]["active_jobs"], 0)
        self.assertEqual(tel["workloads"]["running_jobs"], 0)
        self.assertEqual(tel["workloads"]["running_containers"], 0)

    def test_04_zero_and_invalid_values(self):
        """Verify robustness against malformed, zero, or corrupt telemetry data."""
        corrupt_node = {
            "node_id": "corrupt-node",
            "last_heartbeat": {
                "system": {
                    "cpu_cores": "invalid",
                    "cpu_percent": "abc",
                    "load_average": ["bad", "values"],
                    "memory_details": {
                        "total_mb": "not_a_num",
                        "used_mb": None
                    },
                    "storage_details": {
                        "total_gb": 0,
                        "used_gb": 0
                    }
                },
                "workloads": {
                    "running_containers": "non_int"
                }
            }
        }
        tel = extract_node_telemetry(corrupt_node, status=STATE_ONLINE)
        self.assertIsInstance(tel["cpu"]["cores"], int)
        self.assertIsInstance(tel["memory"]["total_mb"], float)
        self.assertEqual(tel["memory"]["used_percent"], 0.0)
        self.assertEqual(tel["storage"]["used_percent"], 0.0)
        self.assertEqual(tel["workloads"]["running_containers"], 0)

    def test_05_workload_counts(self):
        """Verify active job count and running job count calculation per node."""
        node = {
            "node_id": "test-worker",
            "last_seen": self.now_iso,
            "resources": {"cpu_cores": 4, "ram_mb": 4096, "storage_gb": 50}
        }
        node_jobs = [
            {"job_id": "j1", "target_node": "test-worker", "status": "RUNNING"},
            {"job_id": "j2", "target_node": "test-worker", "status": "CLAIMED"},
            {"job_id": "j3", "target_node": "test-worker", "status": "RECOVERING"},
            {"job_id": "j4", "target_node": "test-worker", "status": "SUCCEEDED"},
            {"job_id": "j5", "target_node": "test-worker", "status": "FAILED"},
            {"job_id": "j6", "target_node": "test-worker", "status": "CANCELLED"},
        ]

        tel = extract_node_telemetry(node, status=STATE_ONLINE, node_jobs=node_jobs)
        # Active jobs should be CLAIMED, RUNNING, RECOVERING (3)
        self.assertEqual(tel["workloads"]["active_jobs"], 3)
        # Running jobs should only be RUNNING (1)
        self.assertEqual(tel["workloads"]["running_jobs"], 1)

    def test_06_docker_workload_counts(self):
        """Verify Docker workload container counting."""
        docker_node = {
            "node_id": "docker-host",
            "last_seen": self.now_iso,
            "last_heartbeat": {
                "workloads": {"running_containers": 5}
            }
        }
        tel = extract_node_telemetry(docker_node, status=STATE_ONLINE)
        self.assertEqual(tel["workloads"]["running_containers"], 5)

        no_docker_node = {
            "node_id": "no-docker",
            "last_seen": self.now_iso,
            "last_heartbeat": {
                "workloads": {"running_containers": 0}
            }
        }
        tel_none = extract_node_telemetry(no_docker_node, status=STATE_ONLINE)
        self.assertEqual(tel_none["workloads"]["running_containers"], 0)

    def test_07_offline_node_handling(self):
        """CRITICAL: Offline nodes must NOT contribute stale utilization as active capacity."""
        offline_node = {
            "node_id": "node-dead",
            "name": "dead-box",
            "status": "ONLINE",  # says online in metadata, but last_seen is expired!
            "last_seen": self.stale_iso,
            "resources": {"cpu_cores": 16, "ram_mb": 65536, "storage_gb": 1000},
            "last_heartbeat": {
                "timestamp": self.stale_iso,
                "system": {
                    "cpu_cores": 16,
                    "memory_details": {"total_mb": 65536, "used_mb": 32000, "available_mb": 33536, "used_percent": 48.8},
                    "storage_details": {"total_gb": 1000, "used_gb": 500, "available_gb": 500, "used_percent": 50.0}
                },
                "workloads": {"running_containers": 10}
            }
        }

        mock_nodes_db = {"nodes": {"node-dead": offline_node}}
        mock_jobs_db = {"jobs": {}}

        util = get_cluster_utilization(mock_nodes_db, mock_jobs_db, timeout_seconds=60)

        # Node itself is marked OFFLINE
        self.assertEqual(util["nodes"]["node-dead"]["status"], STATE_OFFLINE)
        # BUT cluster active capacity MUST BE ZERO!
        self.assertEqual(util["cluster"]["cpu_cores_total"], 0)
        self.assertEqual(util["cluster"]["memory_total_mb"], 0.0)
        self.assertEqual(util["cluster"]["memory_used_mb"], 0.0)
        self.assertEqual(util["cluster"]["memory_available_mb"], 0.0)
        self.assertEqual(util["cluster"]["storage_total_gb"], 0.0)
        self.assertEqual(util["cluster"]["storage_used_gb"], 0.0)
        self.assertEqual(util["cluster"]["storage_available_gb"], 0.0)
        self.assertEqual(util["cluster"]["running_containers"], 0)

    def test_08_cluster_aggregation_math(self):
        """Verify cluster aggregate is exactly the sum of ONLINE nodes only."""
        nodes_db = {
            "nodes": {
                "node-01": {
                    "node_id": "node-01",
                    "status": "ONLINE",
                    "last_seen": self.now_iso,
                    "resources": {"cpu_cores": 8},
                    "last_heartbeat": {
                        "system": {
                            "cpu_cores": 8,
                            "memory_details": {"total_mb": 5000, "used_mb": 2000, "available_mb": 3000, "used_percent": 40},
                            "storage_details": {"total_gb": 100, "used_gb": 20, "available_gb": 80, "used_percent": 20}
                        },
                        "workloads": {"running_containers": 0}
                    }
                },
                "node-02": {
                    "node_id": "node-02",
                    "status": "ONLINE",
                    "last_seen": self.now_iso,
                    "resources": {"cpu_cores": 12},
                    "last_heartbeat": {
                        "system": {
                            "cpu_cores": 12,
                            "memory_details": {"total_mb": 24000, "used_mb": 14000, "available_mb": 10000, "used_percent": 58},
                            "storage_details": {"total_gb": 400, "used_gb": 180, "available_gb": 220, "used_percent": 45}
                        },
                        "workloads": {"running_containers": 3}
                    }
                },
                "node-03-offline": {
                    "node_id": "node-03-offline",
                    "status": "ONLINE",
                    "last_seen": self.stale_iso,
                    "resources": {"cpu_cores": 16},
                    "last_heartbeat": {
                        "system": {
                            "cpu_cores": 16,
                            "memory_details": {"total_mb": 64000, "used_mb": 30000, "available_mb": 34000, "used_percent": 46},
                            "storage_details": {"total_gb": 1000, "used_gb": 500, "available_gb": 500, "used_percent": 50}
                        },
                        "workloads": {"running_containers": 99}
                    }
                }
            }
        }

        jobs_db = {
            "jobs": {
                "j1": {"job_id": "j1", "target_node": "node-01", "status": "RUNNING"},
                "j2": {"job_id": "j2", "target_node": "node-02", "status": "CLAIMED"},
                "j3": {"job_id": "j3", "target_node": "node-02", "status": "SUCCEEDED"}
            }
        }

        util = get_cluster_utilization(nodes_db, jobs_db, timeout_seconds=60)
        c = util["cluster"]

        # Only node-01 (8c) + node-02 (12c) = 20c (node-03-offline excluded)
        self.assertEqual(c["cpu_cores_total"], 20)
        self.assertEqual(c["memory_total_mb"], 29000.0)
        self.assertEqual(c["memory_used_mb"], 16000.0)
        self.assertEqual(c["memory_available_mb"], 13000.0)
        self.assertEqual(c["storage_total_gb"], 500.0)
        self.assertEqual(c["storage_used_gb"], 200.0)
        self.assertEqual(c["storage_available_gb"], 300.0)
        self.assertEqual(c["active_jobs"], 2)
        self.assertEqual(c["running_jobs"], 1)
        self.assertEqual(c["running_containers"], 3)

        # Nodes dictionary contains all 3 with their respective status
        self.assertEqual(util["nodes"]["node-01"]["status"], STATE_ONLINE)
        self.assertEqual(util["nodes"]["node-02"]["status"], STATE_ONLINE)
        self.assertEqual(util["nodes"]["node-03-offline"]["status"], STATE_OFFLINE)

    def test_09_scheduler_telemetry_integration(self):
        """Verify ResourceScheduler includes clean telemetry in candidate data structure."""
        nodes_db = {
            "nodes": {
                "candidate-1": {
                    "node_id": "candidate-1",
                    "role": "compute",
                    "status": "ONLINE",
                    "last_seen": self.now_iso,
                    "resources": {"cpu_cores": 8, "ram_mb": 8192, "storage_gb": 100},
                    "last_heartbeat": {
                        "system": {
                            "cpu_cores": 8,
                            "load_average": [0.5],
                            "memory_details": {"total_mb": 8192, "used_mb": 2048, "available_mb": 6144, "used_percent": 25},
                            "storage_details": {"total_gb": 100, "used_gb": 20, "available_gb": 80, "used_percent": 20}
                        }
                    }
                }
            }
        }
        res = ResourceScheduler.select_node({"min_memory_mb": 1024}, nodes_db, timeout_seconds=60)
        self.assertEqual(res["selected_node"], "candidate-1")
        self.assertGreater(len(res["candidates"]), 0)

        cand = res["candidates"][0]
        self.assertIn("telemetry", cand)
        tel = cand["telemetry"]
        self.assertEqual(tel["status"], STATE_ONLINE)
        self.assertEqual(tel["cpu"]["cores"], 8)
        self.assertEqual(tel["memory"]["available_mb"], 6144)
        self.assertEqual(tel["storage"]["available_gb"], 80)

    def test_10_cluster_utilization_endpoint_http(self):
        """Verify GET /cluster/utilization returns 200 and schema over HTTP."""
        import threading
        import urllib.request
        from http.server import HTTPServer
        from controller.controller import ControllerHandler

        server = HTTPServer(("127.0.0.1", 0), ControllerHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/cluster/utilization")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))

            self.assertIn("cluster", data)
            self.assertIn("nodes", data)

            c = data["cluster"]
            required_cluster_keys = [
                "cpu_cores_total",
                "memory_total_mb",
                "memory_used_mb",
                "memory_available_mb",
                "storage_total_gb",
                "storage_used_gb",
                "storage_available_gb",
                "active_jobs",
                "running_jobs",
                "running_containers"
            ]
            for k in required_cluster_keys:
                self.assertIn(k, c)

            for nid, n in data["nodes"].items():
                self.assertIn("status", n)
                self.assertIn("cpu", n)
                self.assertIn("memory", n)
                self.assertIn("storage", n)
                self.assertIn("workloads", n)
                self.assertIn("capabilities", n)

        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
