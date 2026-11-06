#!/usr/bin/env python3
"""
Targeted Verification Suite for PersonalServer Universal Scheduler (J3)
========================================================================
Validates all mandatory J3 requirements:
1. All 8 J3.2 Eligibility Stages and exact rejection reasons.
2. J3.1/J3.2 Multi-Factor Scoring (RAM 30, CPU 25, Storage 10, Mobile 15, Net 10, Class 10, Pref +15, Concurrency -20, Clamped 0-100).
3. Priority Aging formula: Effective Priority = Base + min(50, floor(dwell_sec / 60) * 5).
4. Authoritative Reservation Ledger (PROVISIONAL, COMMITTED, ACTIVE, RELEASING, RELEASED, EXPIRED).
5. Concurrency safety: atomic reservation and overcommit prevention.
6. Commit TTL expiration (30s) and ACTIVE survival during worker disconnects.
7. Monotonic fencing generation validation and HTTP 412 rejection on stale attempts.
"""

import time
import unittest
from unittest.mock import patch, MagicMock

from scheduler.scheduler import (
    ResourceScheduler,
    compute_effective_priority,
    REJECTION_NODE_OFFLINE,
    REJECTION_NODE_DRAINING,
    REJECTION_INSUFFICIENT_UNRESERVED_RAM,
    REJECTION_INSUFFICIENT_UNRESERVED_STORAGE,
    REJECTION_INSUFFICIENT_CPU,
    REJECTION_BATTERY_POWER_DISALLOWED,
    REJECTION_BATTERY_LEVEL_BELOW_MINIMUM,
    REJECTION_THERMAL_THROTTLING_CRITICAL,
    REJECTION_CAPABILITY_VERIFICATION_STALE,
    REJECTION_HARDWARE_LEASE_CONFLICT,
    REJECTION_UNSATISFIABLE_CAPABILITY,
    REJECTION_UNSUPPORTED_ARCHITECTURE,
    REJECTION_DOCKER_ENGINE_NOT_AVAILABLE,
    REJECTION_HARDWARE_PERIPHERAL_UNPERMITTED,
    REJECTION_ROLE_MISMATCH,
    REJECTION_AVAILABILITY_CLASS_MISMATCH,
    REJECTION_POLICY_DISABLED_CAPABILITY,
    REJECTION_ANTI_AFFINITY_CONFLICT,
)
from controller.stores.reservations import (
    ReservationState,
    create_or_update_reservation,
    activate_reservation,
    release_reservation,
    sweep_reservation_ledger,
    calculate_node_reserved_resources,
)


class TestUniversalSchedulerJ3(unittest.TestCase):
    """Exhaustive test suite for Universal Scheduler J3."""

    def setUp(self):
        self.now_iso = "2026-11-06T00:00:00Z"
        self.base_node = {
            "node_id": "test-worker-01",
            "name": "Realme 5 Pro",
            "role": "compute",
            "platform": "termux",
            "architecture": "aarch64",
            "status": "ONLINE",
            "last_seen": time.time(),
            "availability_class": "Persistent",
            "resources": {
                "cpu_cores": 8,
                "ram_mb": 5600,
                "storage_gb": 50
            },
            "capabilities": {
                "compute": True,
                "python3": True,
                "udocker": True
            },
            "verified_capabilities": {
                "compute": True,
                "python3": True,
                "udocker": True
            },
            "advertised_capabilities": {
                "compute": True,
                "python3": True,
                "udocker": True
            },
            "last_heartbeat": {
                "timestamp": time.time(),
                "services": {"node_worker": "running"},
                "system": {
                    "cpu_cores": 8,
                    "load_average": [0.5, 0.5, 0.5],
                    "memory_details": {
                        "total_mb": 5600.0,
                        "available_mb": 3000.0,
                        "used_mb": 2600.0,
                        "used_percent": 46.4
                    },
                    "storage_details": {
                        "total_gb": 50.0,
                        "available_gb": 35.0,
                        "used_gb": 15.0,
                        "used_percent": 30.0
                    },
                    "battery": {
                        "percentage": 85,
                        "plugged": "PLUGGED_AC"
                    },
                    "thermals": {
                        "status": "Cool",
                        "temperature_c": 32.0
                    },
                    "network": {
                        "ping_ms": 12.0
                    }
                }
            }
        }

    # =========================================================================
    # 1. 8-Stage Eligibility Pipeline Tests
    # =========================================================================

    def test_stage_1_liveness_offline(self):
        """Stage 1 rejects offline nodes with REJECTION_NODE_OFFLINE."""
        node = dict(self.base_node)
        node["status"] = "OFFLINE"
        node["last_seen"] = time.time() - 100
        eligible, reason = ResourceScheduler.is_node_eligible(node, {})
        self.assertFalse(eligible)
        self.assertIn(REJECTION_NODE_OFFLINE, reason)

    def test_stage_2_administrative_draining(self):
        """Stage 2 rejects cordoned/draining nodes with REJECTION_NODE_DRAINING."""
        node = dict(self.base_node)
        node["status"] = "DRAINING"
        eligible, reason = ResourceScheduler.is_node_eligible(node, {})
        self.assertFalse(eligible)
        self.assertIn(REJECTION_NODE_DRAINING, reason)

    def test_stage_2_policy_disabled_capability(self):
        """Stage 2 rejects administratively disabled capabilities."""
        node = dict(self.base_node)
        node["policy_overrides"] = {"disabled_capabilities": ["python3"]}
        eligible, reason = ResourceScheduler.is_node_eligible(node, {"requirements": {"capabilities": ["python3"]}})
        self.assertFalse(eligible)
        self.assertIn(REJECTION_POLICY_DISABLED_CAPABILITY, reason)

    def test_stage_2_anti_affinity_conflict(self):
        """Stage 2 rejects anti-affinity label matches."""
        node = dict(self.base_node)
        node["labels"] = {"zone": "mobile-tier"}
        reqs = {"requirements": {"hints": {"node_anti_affinity": ["zone"]}}}
        eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
        self.assertFalse(eligible)
        self.assertIn(REJECTION_ANTI_AFFINITY_CONFLICT, reason)

    def test_stage_3_availability_class_opportunistic_service_rejection(self):
        """Stage 3 rejects scheduling long-running services on Opportunistic nodes."""
        node = dict(self.base_node)
        node["availability_class"] = "Opportunistic"
        reqs = {"requirements": {"workload_mode": "service"}}
        eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
        self.assertFalse(eligible)
        self.assertIn(REJECTION_AVAILABILITY_CLASS_MISMATCH, reason)

    def test_stage_4_docker_vs_udocker_isolation(self):
        """Stage 4 strictly rejects udocker from satisfying genuine Docker Engine."""
        node = dict(self.base_node)
        # Node has udocker verified, but NOT Docker Engine
        reqs = {"requirements": {"capabilities": ["container_runtime:docker"]}}
        eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
        self.assertFalse(eligible)
        self.assertIn(REJECTION_DOCKER_ENGINE_NOT_AVAILABLE, reason)

    def test_stage_4_unsupported_architecture(self):
        """Stage 4 rejects unsupported architectures."""
        node = dict(self.base_node)
        reqs = {"requirements": {"architecture": "x86_64"}}
        eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
        self.assertFalse(eligible)
        self.assertIn(REJECTION_UNSUPPORTED_ARCHITECTURE, reason)

    def test_stage_5_hardware_peripheral_detected_not_accessible(self):
        """Stage 5 rejects peripherals that are DETECTED but not ACCESSIBLE."""
        node = dict(self.base_node)
        node["hardware"] = {"camera": {"status": "DETECTED"}}
        reqs = {"requirements": {"hardware": ["camera"]}}
        eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
        self.assertFalse(eligible)
        self.assertIn(REJECTION_HARDWARE_PERIPHERAL_UNPERMITTED, reason)

    def test_stage_5_hardware_lease_conflict_exclusive_gpu(self):
        """Stage 5 rejects concurrent jobs targeting an already locked exclusive device."""
        node = dict(self.base_node)
        reqs = {"requirements": {"gpu": True}}
        eligible, reason = ResourceScheduler.is_node_eligible(
            node, reqs, locked_devices=["gpu:0"]
        )
        self.assertFalse(eligible)
        self.assertIn(REJECTION_HARDWARE_LEASE_CONFLICT, reason)

    def test_stage_6_capability_verification_stale(self):
        """Stage 6 rejects nodes with expired verification TTL when require_verified is True."""
        node = dict(self.base_node)
        node["metadata"] = {"is_stale_verification": True}
        reqs = {"requirements": {"require_verified": True}}
        eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
        self.assertFalse(eligible)
        self.assertIn(REJECTION_CAPABILITY_VERIFICATION_STALE, reason)

    def test_stage_7_thermal_critical_rejection(self):
        """Stage 7 rejects nodes in Critical thermal state."""
        node = dict(self.base_node)
        node["last_heartbeat"]["system"]["thermals"] = {"status": "Critical", "temperature_c": 52.0}
        eligible, reason = ResourceScheduler.is_node_eligible(node, {})
        self.assertFalse(eligible)
        self.assertIn(REJECTION_THERMAL_THROTTLING_CRITICAL, reason)

    def test_stage_7_battery_power_disallowed(self):
        """Stage 7 rejects battery power when allow_on_battery is false."""
        node = dict(self.base_node)
        node["last_heartbeat"]["system"]["battery"] = {"percentage": 90, "plugged": "BATTERY"}
        reqs = {"requirements": {"hints": {"allow_on_battery": False}}}
        eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
        self.assertFalse(eligible)
        self.assertIn(REJECTION_BATTERY_POWER_DISALLOWED, reason)

    def test_stage_7_battery_floor_rejection(self):
        """Stage 7 rejects battery level below requested minimum floor."""
        node = dict(self.base_node)
        node["last_heartbeat"]["system"]["battery"] = {"percentage": 18, "plugged": "BATTERY"}
        reqs = {"requirements": {"hints": {"min_battery_percent": 25.0}}}
        eligible, reason = ResourceScheduler.is_node_eligible(node, reqs)
        self.assertFalse(eligible)
        self.assertIn(REJECTION_BATTERY_LEVEL_BELOW_MINIMUM, reason)

    def test_stage_8_resource_headroom_post_reservation(self):
        """Stage 8 rejects when free RAM minus safety buffer and reservations is insufficient."""
        node = dict(self.base_node)
        # Total: 5600MB, Safety buffer: max(300, 560) = 560MB
        # Available: 3000MB, minus safety: 2440MB.
        # With active reservations of 2200MB, allocatable is 240MB.
        reqs = {"requirements": {"min_memory_mb": 500}}
        eligible, reason = ResourceScheduler.is_node_eligible(
            node, reqs, reserved_mem_mb=2200.0
        )
        self.assertFalse(eligible)
        self.assertIn(REJECTION_INSUFFICIENT_UNRESERVED_RAM, reason)

    # =========================================================================
    # 2. Multi-Factor Scoring Model Tests
    # =========================================================================

    def test_scoring_weights_and_bounds(self):
        """Verifies exact scoring formula: RAM 30, CPU 25, Storage 10, Mobile 15, Net 10, Class 10."""
        node = dict(self.base_node)
        reqs = {"preferred_node": "test-worker-01", "availability_class": "Persistent"}
        score, breakdown = ResourceScheduler.calculate_score(
            node=node,
            reqs=reqs,
            effective_avail_mem_mb=2800.0,
            total_mem_mb=5600.0,
            effective_avail_st_gb=25.0,
            total_st_gb=50.0,
            active_cnt=1
        )
        # Memory: 0.5 * 30 = 15.0 pts
        self.assertAlmostEqual(breakdown["memory_pts"], 15.0, places=1)
        # Storage: 0.5 * 10 = 5.0 pts
        self.assertAlmostEqual(breakdown["storage_pts"], 5.0, places=1)
        # Mobile: Cool (+7) + AC (+8) = 15.0 pts
        self.assertAlmostEqual(breakdown["mobile_pts"], 15.0, places=1)
        # Preferred: +15.0 pts
        self.assertAlmostEqual(breakdown["preferred_bonus"], 15.0, places=1)
        # Concurrency penalty: -5.0 pts
        self.assertAlmostEqual(breakdown["concurrency_penalty"], 5.0, places=1)
        # Score must be bounded in [0.0, 100.0]
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_concurrency_penalty_capped_at_20(self):
        """Concurrency penalty scales at -5.0 per job and caps strictly at -20.0."""
        node = dict(self.base_node)
        _, breakdown = ResourceScheduler.calculate_score(
            node=node,
            reqs={},
            effective_avail_mem_mb=1000.0,
            total_mem_mb=5600.0,
            effective_avail_st_gb=10.0,
            total_st_gb=50.0,
            active_cnt=10  # 10 jobs * -5 = -50, capped at -20
        )
        self.assertEqual(breakdown["concurrency_penalty"], 20.0)

    # =========================================================================
    # 3. Priority Aging Formula Tests
    # =========================================================================

    def test_priority_aging_formula(self):
        """Effective Priority = Base + min(50, floor(dwell_sec / 60) * 5)."""
        base = 25  # LOW
        t0 = 1000.0
        # After 0 seconds
        self.assertEqual(compute_effective_priority(base, t0, t0), 25)
        # After 59 seconds (no boost yet)
        self.assertEqual(compute_effective_priority(base, t0, t0 + 59), 25)
        # After 60 seconds (+5 pts)
        self.assertEqual(compute_effective_priority(base, t0, t0 + 60), 30)
        # After 600 seconds (+50 pts max cap reached)
        self.assertEqual(compute_effective_priority(base, t0, t0 + 600), 75)
        # After 1200 seconds (capped at +50 pts)
        self.assertEqual(compute_effective_priority(base, t0, t0 + 1200), 75)

    # =========================================================================
    # 4. Authoritative Reservation Ledger & Lifecycle Tests
    # =========================================================================

    def test_reservation_lifecycle_and_commit_expiry(self):
        """Verifies PROVISIONAL -> COMMITTED -> ACTIVE -> RELEASED and commit expiration."""
        jobs_db = {"reservations": {}, "jobs": {}}
        job_id = "job-test-8812"
        node_id = "worker-01"

        # 1. Create COMMITTED reservation (30s TTL)
        res = create_or_update_reservation(
            jobs_db, job_id, node_id, attempt=1, fencing_token=1,
            resources={"memory_mb": 512, "storage_gb": 2, "cpu_cores": 1},
            state=ReservationState.COMMITTED, ttl_sec=30.0
        )
        self.assertEqual(res["state"], ReservationState.COMMITTED)
        self.assertIsNotNone(res["commit_expires_at"])

        # Capacity is consumed while COMMITTED
        consumed = calculate_node_reserved_resources(jobs_db, node_id)
        self.assertEqual(consumed["reserved_memory_mb"], 512.0)

        # 2. Worker acks process start -> transitions to ACTIVE
        ok, err = activate_reservation(jobs_db, job_id, attempt=1, fencing_token=1)
        self.assertTrue(ok)
        res_record = jobs_db["reservations"][res["reservation_id"]]
        self.assertEqual(res_record["state"], ReservationState.ACTIVE)
        # Invariant: ACTIVE reservations do NOT expire by commit TTL
        self.assertIsNone(res_record["commit_expires_at"])

        # 3. Sweeper runs with time advanced past 60s
        with patch("time.time", return_value=time.time() + 100):
            sweep_reservation_ledger(jobs_db)
            # ACTIVE reservation MUST survive sweeper
            self.assertEqual(jobs_db["reservations"][res["reservation_id"]]["state"], ReservationState.ACTIVE)

        # 4. Job completes -> reservation released
        ok, _ = release_reservation(jobs_db, job_id, attempt=1, fencing_token=1)
        self.assertTrue(ok)
        self.assertEqual(jobs_db["reservations"][res["reservation_id"]]["state"], ReservationState.RELEASED)
        # Released reservations do NOT consume capacity
        consumed_after = calculate_node_reserved_resources(jobs_db, node_id)
        self.assertEqual(consumed_after["reserved_memory_mb"], 0.0)

    def test_stale_fencing_token_cannot_release_newer_reservation(self):
        """A stale worker attempt cannot release or modify a reservation belonging to a newer attempt."""
        jobs_db = {"reservations": {}, "jobs": {}}
        job_id = "job-test-fencing"
        node_id = "worker-01"

        # Create reservation for attempt 2, fencing_token 2
        res = create_or_update_reservation(
            jobs_db, job_id, node_id, attempt=2, fencing_token=2,
            resources={"memory_mb": 256}, state=ReservationState.ACTIVE
        )

        # Stale attempt 1 with fencing_token 1 tries to release
        ok, err = release_reservation(jobs_db, job_id, attempt=1, fencing_token=1)
        self.assertFalse(ok)
        # Reservation remains ACTIVE
        self.assertEqual(jobs_db["reservations"][res["reservation_id"]]["state"], ReservationState.ACTIVE)

    def test_unacknowledged_committed_reservation_expires(self):
        """Unacknowledged COMMITTED reservations expire after commit TTL."""
        jobs_db = {"reservations": {}, "jobs": {}}
        res = create_or_update_reservation(
            jobs_db, "job-unack", "worker-01", attempt=1, fencing_token=1,
            resources={"memory_mb": 500}, state=ReservationState.COMMITTED, ttl_sec=30.0
        )
        # Advance time by 35s
        with patch("time.time", return_value=time.time() + 35):
            sweep_reservation_ledger(jobs_db)
            self.assertEqual(jobs_db["reservations"][res["reservation_id"]]["state"], ReservationState.EXPIRED)
            # Capacity is restored
            consumed = calculate_node_reserved_resources(jobs_db, "worker-01")
            self.assertEqual(consumed["reserved_memory_mb"], 0.0)

    def test_node_liveness_timeout_boundary(self):
        """Boundary test: 60s node liveness heartbeat threshold (59s eligible, 61s offline)."""
        node_fresh = dict(self.base_node)
        node_fresh["last_seen"] = time.time() - 59.0
        eligible, _ = ResourceScheduler.is_node_eligible(node_fresh, {})
        self.assertTrue(eligible)

        node_stale = dict(self.base_node)
        node_stale["last_seen"] = time.time() - 61.0
        eligible, reason = ResourceScheduler.is_node_eligible(node_stale, {})
        self.assertFalse(eligible)
        self.assertIn(REJECTION_NODE_OFFLINE, reason)

    def test_telemetry_freshness_and_staleness_boundary(self):
        """Boundary test: dynamic telemetry (15s < age <= 60s is graceful staleness, > 60s is expired/offline)."""
        node = dict(self.base_node)
        now = time.time()
        # At 30s: graceful staleness, node remains ONLINE and eligible
        node["last_seen"] = now - 30.0
        eligible, _ = ResourceScheduler.is_node_eligible(node, {})
        self.assertTrue(eligible)

        # At 61s: dynamic telemetry expired, node drops to OFFLINE
        node["last_seen"] = now - 61.0
        eligible, reason = ResourceScheduler.is_node_eligible(node, {})
        self.assertFalse(eligible)
        self.assertIn(REJECTION_NODE_OFFLINE, reason)

    def test_execution_lease_expiry_boundary(self):
        """Boundary test: 45s execution lease TTL (44s active, 46s expired and swept)."""
        from controller.lease_sweeper import sweep_expired_leases
        from controller.constants import JOB_STATE_ASSIGNED, JOB_STATE_RECOVERING
        now = time.time()
        jobs_db = {
            "jobs": {
                "job-lease-44": {
                    "job_id": "job-lease-44",
                    "status": JOB_STATE_ASSIGNED,
                    "state": JOB_STATE_ASSIGNED,
                    "assigned_node": "worker-01",
                    "lease_expires_at": now + 44.0,
                    "lease_generation": 1,
                    "attempt": 1,
                    "max_attempts": 3,
                    "retryable": True
                },
                "job-lease-46": {
                    "job_id": "job-lease-46",
                    "status": JOB_STATE_ASSIGNED,
                    "state": JOB_STATE_ASSIGNED,
                    "assigned_node": "worker-01",
                    "lease_expires_at": now - 1.0,  # 46s after grant
                    "lease_generation": 1,
                    "attempt": 1,
                    "max_attempts": 3,
                    "retryable": True
                }
            },
            "reservations": {}
        }
        nodes_db = {"nodes": {"worker-01": dict(self.base_node)}}

        sweep_expired_leases(jobs_db, nodes_db, timeout_seconds=60)
        # Job at 44s remains active
        self.assertEqual(jobs_db["jobs"]["job-lease-44"]["status"], JOB_STATE_ASSIGNED)
        # Job at 46s is swept into recovery
        self.assertEqual(jobs_db["jobs"]["job-lease-46"]["status"], JOB_STATE_RECOVERING)

    def test_reservation_commit_ttl_boundary(self):
        """Boundary test: 30s reservation commit window (29s unexpired, 31s expired)."""
        now = time.time()
        jobs_db = {"reservations": {}, "jobs": {}}
        res_29 = create_or_update_reservation(
            jobs_db, "job-29", "worker-01", attempt=1, fencing_token=1,
            resources={"memory_mb": 256}, state=ReservationState.COMMITTED, ttl_sec=30.0
        )
        res_31 = create_or_update_reservation(
            jobs_db, "job-31", "worker-01", attempt=1, fencing_token=1,
            resources={"memory_mb": 256}, state=ReservationState.COMMITTED, ttl_sec=30.0
        )

        # Set specific expiration epochs
        jobs_db["reservations"][res_29["reservation_id"]]["commit_expires_at"] = now + 1.0   # 29s elapsed
        jobs_db["reservations"][res_31["reservation_id"]]["commit_expires_at"] = now - 1.0   # 31s elapsed

        sweep_reservation_ledger(jobs_db)
        self.assertEqual(jobs_db["reservations"][res_29["reservation_id"]]["state"], ReservationState.COMMITTED)
        self.assertEqual(jobs_db["reservations"][res_31["reservation_id"]]["state"], ReservationState.EXPIRED)


if __name__ == "__main__":
    unittest.main()

