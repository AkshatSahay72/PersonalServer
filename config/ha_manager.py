#!/usr/bin/env python3
"""
PersonalServer High Availability (HA) & Master Failover Subsystem
================================================================
Provides distributed leader lease tracking, automatic master failure detection,
state synchronization, deterministic election / promotion, split-brain immunity,
and seamless worker / client redirection.
"""

import os
import sys
import json
import time
import shutil
import socket
import secrets
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.platform_config import get_controller_paths, normalize_path
from config.structured_logger import controller_logger


class HAState:
    LEADER = "leader"
    STANDBY = "standby"
    CANDIDATE = "candidate"
    DISABLED = "disabled"


def get_current_iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class LeaderLease:
    """
    Lease object holding cluster leadership, election term,
    expiration timestamp, and cluster state version.
    """

    def __init__(
        self,
        term: int = 0,
        leader_id: str = "",
        leader_url: str = "",
        lease_expires_at: float = 0.0,
        state_version: int = 0,
        last_renewed_at: Optional[str] = None
    ):
        self.term = int(term)
        self.leader_id = str(leader_id)
        self.leader_url = str(leader_url).rstrip("/")
        self.lease_expires_at = float(lease_expires_at)
        self.state_version = int(state_version)
        self.last_renewed_at = last_renewed_at or get_current_iso_timestamp()

    def is_valid(self, grace_sec: float = 0.0) -> bool:
        """Returns True if the lease has not expired."""
        return (time.time() - grace_sec) < self.lease_expires_at

    def time_remaining_sec(self) -> float:
        """Returns remaining seconds before the lease expires (or 0.0)."""
        return max(0.0, self.lease_expires_at - time.time())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "term": self.term,
            "leader_id": self.leader_id,
            "leader_url": self.leader_url,
            "lease_expires_at": self.lease_expires_at,
            "state_version": self.state_version,
            "last_renewed_at": self.last_renewed_at,
            "time_remaining_sec": round(self.time_remaining_sec(), 2),
            "is_valid": self.is_valid()
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LeaderLease":
        return cls(
            term=data.get("term", 0),
            leader_id=data.get("leader_id", ""),
            leader_url=data.get("leader_url", ""),
            lease_expires_at=data.get("lease_expires_at", 0.0),
            state_version=data.get("state_version", 0),
            last_renewed_at=data.get("last_renewed_at")
        )

    def save_to_file(self, file_path: Path):
        """Atomically saves lease metadata to disk."""
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = file_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
            tmp.replace(file_path)
        except Exception as e:
            controller_logger.warning("Failed to persist leader lease to %s: %s", file_path, e)

    @classmethod
    def load_from_file(cls, file_path: Path) -> "LeaderLease":
        """Loads lease metadata from disk if available, otherwise returns empty lease."""
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return cls.from_dict(data)
            except Exception:
                pass
        return cls()


class HAManager:
    """
    Cluster High Availability & Leader Election Manager.
    Coordinates active leadership, standby state replication, heartbeat monitoring,
    and automatic failover.
    """

    def __init__(
        self,
        node_id: Optional[str] = None,
        node_url: Optional[str] = None,
        candidate_urls: Optional[List[str]] = None,
        role: str = "auto",
        lease_duration_sec: float = 10.0,
        heartbeat_interval_sec: float = 3.0,
        check_interval_sec: float = 2.0,
        paths=None,
        auth_token: Optional[str] = None,
        on_promotion_callback: Optional[Callable[[], None]] = None,
        on_demotion_callback: Optional[Callable[[], None]] = None
    ):
        self._lock = threading.RLock()
        self.paths = paths or get_controller_paths()
        self.lease_file = getattr(self.paths, "leader_lease_file", self.paths.data_dir / "leader_lease.json")

        self.node_id = str(node_id or socket.gethostname() or "node-master").strip()
        self.node_url = str(node_url or "http://127.0.0.1:8000").rstrip("/")
        
        # Load candidate URLs from args or environment
        env_candidates = os.environ.get("PERSONALSERVER_CONTROLLER_URLS", "")
        parsed_candidates = [u.strip().rstrip("/") for u in env_candidates.split(",") if u.strip()]
        self.candidate_urls: List[str] = list(candidate_urls or parsed_candidates or [self.node_url])
        if self.node_url not in self.candidate_urls:
            self.candidate_urls.append(self.node_url)

        self.configured_role = role.lower()  # "leader", "standby", or "auto"
        self.lease_duration_sec = float(lease_duration_sec)
        self.heartbeat_interval_sec = float(heartbeat_interval_sec)
        self.check_interval_sec = float(check_interval_sec)
        self.auth_token = auth_token

        self.on_promotion_callback = on_promotion_callback
        self.on_demotion_callback = on_demotion_callback

        # State fields
        self.state: str = HAState.STANDBY if self.configured_role == "standby" else HAState.LEADER
        self.current_lease: LeaderLease = LeaderLease.load_from_file(self.lease_file)
        self.state_version: int = self.current_lease.state_version
        self.missed_checks: int = 0
        self.max_missed_checks: int = 3
        self.standby_nodes: Dict[str, Dict[str, Any]] = {}
        self.last_sync_timestamp: Optional[str] = None

        # Background thread control
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False

    @property
    def is_leader(self) -> bool:
        with self._lock:
            return self.state == HAState.LEADER

    @property
    def is_standby(self) -> bool:
        with self._lock:
            return self.state == HAState.STANDBY

    @property
    def current_term(self) -> int:
        with self._lock:
            return self.current_lease.term

    @property
    def current_leader_id(self) -> str:
        with self._lock:
            return self.current_lease.leader_id

    @property
    def current_leader_url(self) -> str:
        with self._lock:
            return self.current_lease.leader_url

    def initialize(self):
        """Initializes HA state, discovering active leader or claiming initial lease."""
        with self._lock:
            # Check existing on-disk lease
            disk_lease = LeaderLease.load_from_file(self.lease_file)
            now = time.time()

            if self.configured_role == "leader":
                # Forced primary leader
                new_term = max(1, disk_lease.term + 1)
                self.current_lease = LeaderLease(
                    term=new_term,
                    leader_id=self.node_id,
                    leader_url=self.node_url,
                    lease_expires_at=now + self.lease_duration_sec,
                    state_version=max(self.state_version, disk_lease.state_version)
                )
                self.state = HAState.LEADER
                self.current_lease.save_to_file(self.lease_file)
                controller_logger.info("HA_INIT", "HAManager initialized as forced LEADER (Term %d)", new_term)
                return

            if self.configured_role == "standby":
                self.state = HAState.STANDBY
                self.current_lease = disk_lease
                controller_logger.info("HA_INIT", "HAManager initialized as STANDBY")
                return

            # Role == "auto": test if active leader is reachable
            active_url, leader_info = find_active_leader(self.candidate_urls, exclude_url=self.node_url)
            if active_url and leader_info:
                # Standby behind discovered leader
                self.state = HAState.STANDBY
                self.current_lease = LeaderLease(
                    term=leader_info.get("term", disk_lease.term),
                    leader_id=leader_info.get("leader_id", ""),
                    leader_url=active_url,
                    lease_expires_at=now + leader_info.get("time_remaining_sec", self.lease_duration_sec),
                    state_version=leader_info.get("state_version", disk_lease.state_version)
                )
                controller_logger.info("HA_INIT", "HAManager discovered active leader at %s. Running as STANDBY.", active_url)
            else:
                # No active leader found on network, claim initial leadership
                new_term = max(1, disk_lease.term + (1 if disk_lease.leader_id != self.node_id else 0))
                self.state = HAState.LEADER
                self.current_lease = LeaderLease(
                    term=new_term,
                    leader_id=self.node_id,
                    leader_url=self.node_url,
                    lease_expires_at=now + self.lease_duration_sec,
                    state_version=max(self.state_version, disk_lease.state_version)
                )
                self.current_lease.save_to_file(self.lease_file)
                controller_logger.info("HA_INIT", "No remote leader found. HAManager elected self as LEADER (Term %d).", new_term)

    def bump_state_version(self) -> int:
        """Increments state version when cluster databases (nodes, jobs, apps) change."""
        with self._lock:
            self.state_version += 1
            if self.state == HAState.LEADER:
                self.current_lease.state_version = self.state_version
                self.current_lease.save_to_file(self.lease_file)
            return self.state_version

    def renew_lease(self) -> bool:
        """Renews leadership lease if currently LEADER."""
        with self._lock:
            if self.state != HAState.LEADER:
                return False
            now = time.time()
            self.current_lease.lease_expires_at = now + self.lease_duration_sec
            self.current_lease.last_renewed_at = get_current_iso_timestamp()
            self.current_lease.state_version = self.state_version
            self.current_lease.save_to_file(self.lease_file)
            return True

    def elect_self(self, reason: str = "Leader failure detected"):
        """Promotes local node to LEADER and acquires lease."""
        with self._lock:
            self.state = HAState.CANDIDATE
            new_term = self.current_lease.term + 1
            now = time.time()

            controller_logger.info(
                "HA_ELECTION",
                "Initiating election: Promoting %s to LEADER (Term %d). Reason: %s",
                self.node_id, new_term, reason
            )

            self.current_lease = LeaderLease(
                term=new_term,
                leader_id=self.node_id,
                leader_url=self.node_url,
                lease_expires_at=now + self.lease_duration_sec,
                state_version=self.state_version,
                last_renewed_at=get_current_iso_timestamp()
            )
            self.current_lease.save_to_file(self.lease_file)
            self.state = HAState.LEADER
            self.missed_checks = 0

            # Execute promotion hook
            if self.on_promotion_callback:
                try:
                    self.on_promotion_callback()
                except Exception as e:
                    controller_logger.error("HA_CALLBACK_ERROR", "Error in on_promotion_callback: %s", e)

    def step_down(self, new_term: int, new_leader_id: str, new_leader_url: str):
        """Demotes local node from LEADER to STANDBY to avoid split-brain."""
        with self._lock:
            was_leader = (self.state == HAState.LEADER)
            self.state = HAState.STANDBY
            now = time.time()
            self.current_lease = LeaderLease(
                term=new_term,
                leader_id=new_leader_id,
                leader_url=new_leader_url,
                lease_expires_at=now + self.lease_duration_sec,
                state_version=self.state_version
            )
            self.current_lease.save_to_file(self.lease_file)
            controller_logger.info(
                "HA_STEP_DOWN",
                "Stepping down to STANDBY (Term %d). Active leader is %s at %s.",
                new_term, new_leader_id, new_leader_url
            )

            if was_leader and self.on_demotion_callback:
                try:
                    self.on_demotion_callback()
                except Exception as e:
                    controller_logger.error("HA_CALLBACK_ERROR", "Error in on_demotion_callback: %s", e)

    def register_standby_heartbeat(self, standby_id: str, standby_url: str, standby_version: int):
        """Records a standby node's liveness and sync status on the leader."""
        with self._lock:
            self.standby_nodes[standby_id] = {
                "node_id": standby_id,
                "url": standby_url,
                "state_version": standby_version,
                "last_seen_at": get_current_iso_timestamp(),
                "in_sync": (standby_version >= self.state_version)
            }

    def sync_state_from_leader(self, leader_url: str, auth_token: Optional[str] = None) -> bool:
        """Pulls full cluster database snapshot from leader and stores locally."""
        token = auth_token or self.auth_token
        url = f"{leader_url.rstrip('/')}/ha/sync"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "PersonalServer-HAManager/1.0",
                "Authorization": f"Bearer {token}" if token else ""
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status != 200:
                    return False
                payload = json.loads(resp.read().decode("utf-8"))

            remote_term = payload.get("term", 0)
            remote_version = payload.get("state_version", 0)

            with self._lock:
                if remote_term < self.current_term and self.state == HAState.LEADER:
                    return False  # Do not overwrite leader with older term

                # Persist snapshot to local files atomically
                p = getattr(self, "paths", None) or get_controller_paths()
                p.ensure_directories()
                for key, target_path in [
                    ("nodes", p.nodes_file),
                    ("jobs", p.jobs_file),
                    ("apps", p.apps_file),
                    ("onboarding_codes", p.onboarding_codes_file)
                ]:
                    if key in payload and payload[key] is not None:
                        tmp = target_path.with_suffix(".tmp")
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(payload[key], f, indent=2)
                        tmp.replace(target_path)

                self.state_version = remote_version
                self.last_sync_timestamp = get_current_iso_timestamp()

                # Update current lease to reflect synced active leader
                self.current_lease = LeaderLease(
                    term=remote_term,
                    leader_id=payload.get("leader_id", ""),
                    leader_url=leader_url,
                    lease_expires_at=time.time() + self.lease_duration_sec,
                    state_version=remote_version
                )
                self.current_lease.save_to_file(self.lease_file)

                controller_logger.info(
                    "HA_SYNC",
                    f"Synchronized cluster state from leader {leader_url} (State Version: {remote_version}, Term: {remote_term})"
                )
                return True

        except Exception as e:
            controller_logger.warning("HA_SYNC_FAILED", f"Failed to sync cluster state from {leader_url}: {e}")
            return False

    def export_state_snapshot(self) -> Dict[str, Any]:
        """Exports complete database snapshot for standby replication."""
        with self._lock:
            p = getattr(self, "paths", None) or get_controller_paths()
            def read_json_safe(fpath: Path, default_val: Any) -> Any:
                if fpath.exists():
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            return json.load(f)
                    except Exception:
                        pass
                return default_val

            return {
                "term": self.current_term,
                "state_version": self.state_version,
                "leader_id": self.node_id,
                "leader_url": self.node_url,
                "nodes": read_json_safe(p.nodes_file, {"cluster_name": "PersonalServer", "nodes": {}}),
                "jobs": read_json_safe(p.jobs_file, {"jobs": {}}),
                "apps": read_json_safe(p.apps_file, {"applications": {}}),
                "onboarding_codes": read_json_safe(p.onboarding_codes_file, {"codes": {}}),
                "timestamp": get_current_iso_timestamp()
            }

    def check_remote_leader(self) -> bool:
        """Pings current leader /ha/status. Returns True if alive."""
        leader_url = self.current_leader_url
        if not leader_url or leader_url == self.node_url:
            discovered_url, leader_info = find_active_leader(self.candidate_urls, exclude_url=self.node_url)
            if discovered_url:
                with self._lock:
                    self.current_lease.leader_url = discovered_url
                    if leader_info:
                        self.current_lease.leader_id = leader_info.get("leader_id", "")
                        self.current_lease.term = leader_info.get("term", self.current_lease.term)
                        self.current_lease.lease_expires_at = time.time() + leader_info.get("time_remaining_sec", self.lease_duration_sec)
                leader_url = discovered_url
            else:
                return False

        url = f"{leader_url}/ha/status"
        req = urllib.request.Request(url, headers={"User-Agent": "PersonalServer-HAManager/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    remote_term = data.get("term", 0)
                    remote_version = data.get("state_version", 0)
                    remote_role = data.get("role", "")

                    if remote_role == HAState.LEADER:
                        with self._lock:
                            self.current_lease.lease_expires_at = time.time() + data.get("lease_remaining_sec", self.lease_duration_sec)
                            self.missed_checks = 0
                            # If remote state is newer, trigger sync
                            if remote_version > self.state_version:
                                self.sync_state_from_leader(leader_url)
                        return True
        except Exception:
            pass

        with self._lock:
            self.missed_checks += 1
        return False

    def trigger_failover_evaluation(self):
        """Called periodically by monitor loop. Triggers failover if leader is dead."""
        with self._lock:
            if self.state == HAState.LEADER:
                # Renew local lease
                self.renew_lease()
                return

            if self.state == HAState.STANDBY:
                alive = self.check_remote_leader()
                if not alive:
                    # Check lease expiration on disk and missed check counter
                    lease_expired = not self.current_lease.is_valid()
                    if self.missed_checks >= self.max_missed_checks or lease_expired:
                        controller_logger.warning(
                            "HA_FAILOVER",
                            "Master at %s is DOWN (missed checks: %d, lease valid: %s). Promoting self!",
                            self.current_leader_url, self.missed_checks, not lease_expired
                        )
                        self.elect_self(reason=f"Master {self.current_leader_url} unreachable")

    def manual_failover(self, target_node_url: Optional[str] = None) -> Dict[str, Any]:
        """Manually transfers leadership or forces local election."""
        with self._lock:
            if target_node_url and target_node_url != self.node_url:
                # Step down and defer to target
                new_term = self.current_term + 1
                self.step_down(new_term, "target-leader", target_node_url)
                return {"status": "success", "action": "stepped_down", "target": target_node_url, "term": new_term}
            else:
                # Force local promotion
                self.elect_self(reason="Manual failover trigger")
                return {"status": "success", "action": "promoted_local", "node_id": self.node_id, "term": self.current_term}

    def start_monitor(self):
        """Starts background monitor and lease renewal thread."""
        with self._lock:
            if self._is_running:
                return
            self._is_running = True
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="HAMonitorThread")
            self._monitor_thread.start()

    def stop_monitor(self):
        """Stops background monitor thread cleanly."""
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False
            self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2.0)

    def _monitor_loop(self):
        """Background thread executing periodic heartbeat and failover evaluation."""
        while not self._stop_event.is_set():
            try:
                self.trigger_failover_evaluation()
            except Exception as e:
                controller_logger.error("HA_ERROR", "Error in HA monitor loop: %s", e)

            # Sleep interval depends on role
            interval = self.heartbeat_interval_sec if self.is_leader else self.check_interval_sec
            self._stop_event.wait(timeout=interval)

    def get_status_dict(self) -> Dict[str, Any]:
        """Returns structured HA status dictionary for HTTP endpoints and Web UI."""
        with self._lock:
            return {
                "ha_enabled": True,
                "role": self.state,
                "node_id": self.node_id,
                "node_url": self.node_url,
                "leader_id": self.current_leader_id,
                "leader_url": self.current_leader_url,
                "term": self.current_term,
                "state_version": self.state_version,
                "lease_valid": self.current_lease.is_valid(),
                "lease_remaining_sec": round(self.current_lease.time_remaining_sec(), 1),
                "missed_checks": self.missed_checks,
                "candidate_urls": self.candidate_urls,
                "standbys_count": len(self.standby_nodes),
                "standbys": list(self.standby_nodes.values()),
                "last_sync_timestamp": self.last_sync_timestamp,
                "timestamp": get_current_iso_timestamp()
            }


def find_active_leader(
    candidate_urls: List[str],
    timeout: float = 2.0,
    exclude_url: Optional[str] = None
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Probes candidate URLs to find the active cluster LEADER.
    Returns (leader_url, status_dict) or (None, None).
    """
    for raw_url in candidate_urls:
        url = raw_url.rstrip("/")
        if exclude_url and url == exclude_url.rstrip("/"):
            continue

        endpoint = f"{url}/ha/status"
        req = urllib.request.Request(endpoint, headers={"User-Agent": "PersonalServer-HAFinder/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("role") == HAState.LEADER and data.get("lease_valid", True):
                        return url, data
        except Exception:
            pass

    # Fallback: check /health endpoint on candidates if /ha/status is not yet answering
    for raw_url in candidate_urls:
        url = raw_url.rstrip("/")
        if exclude_url and url == exclude_url.rstrip("/"):
            continue
        try:
            req = urllib.request.Request(f"{url}/health", headers={"User-Agent": "PersonalServer-HAFinder/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("status") == "online":
                        return url, {"role": "leader", "leader_url": url, "term": 1, "state_version": 1}
        except Exception:
            pass

    return None, None
