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
    FENCED = "fenced"
    DISABLED = "disabled"


class HAFailureMode:
    HEALTHY = "HEALTHY"
    PROCESS_DEAD = "PROCESS_DEAD"
    NETWORK_PARTITION = "NETWORK_PARTITION"
    API_ERROR = "API_ERROR"
    UNKNOWN = "UNKNOWN"


from common.time_utils import get_current_iso_timestamp


def resolve_controller_node_id(node_id: Optional[str] = None, paths=None) -> str:
    """
    Authoritatively resolves the local controller's registered node ID.
    Precedence:
    1. Explicit non-empty node_id argument.
    2. PERSONALSERVER_NODE_ID environment variable.
    3. Local node configuration file (config/node.json or config/node.conf).
    4. Match local network IP or hostname against registered cluster nodes in nodes.json.
    5. Fallback to machine hostname or 'node-master'.
    """
    if node_id and str(node_id).strip() and str(node_id).strip() not in ("unknown", "None", "-"):
        return str(node_id).strip()

    env_id = os.environ.get("PERSONALSERVER_NODE_ID")
    if env_id and env_id.strip() and env_id.strip() not in ("unknown", "None", "-"):
        return env_id.strip()

    curr_paths = paths or get_controller_paths()
    config_dir = getattr(curr_paths, "config_dir", BASE_DIR / "config")
    data_dir = getattr(curr_paths, "data_dir", BASE_DIR / "controller" / "data")

    # 3. Check local node config files
    node_json = config_dir / "node.json"
    if node_json.exists():
        try:
            with open(node_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                nid = data.get("node_id") or data.get("NODE_ID")
                if nid and str(nid).strip():
                    return str(nid).strip()
        except Exception:
            pass

    node_conf = config_dir / "node.conf"
    if node_conf.exists():
        try:
            with open(node_conf, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("NODE_ID="):
                        nid = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if nid:
                            return nid
        except Exception:
            pass

    # 4. Match local network IP / Tailscale IP / hostname against registered nodes.json
    nodes_file = getattr(curr_paths, "nodes_file", data_dir / "nodes.json")
    if nodes_file.exists():
        try:
            with open(nodes_file, "r", encoding="utf-8") as f:
                nodes_db = json.load(f)
                nodes = nodes_db.get("nodes", {})

                local_ips = set(["127.0.0.1"])
                try:
                    hostname = socket.gethostname()
                    for ip in socket.gethostbyname_ex(hostname)[2]:
                        local_ips.add(ip)
                except Exception:
                    pass
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.connect(("10.255.255.255", 1))
                        local_ips.add(s.getsockname()[0])
                except Exception:
                    pass

                # Check for IP match (e.g. Tailscale IP)
                for nid, n in nodes.items():
                    if (n.get("status") or "").upper() == "REMOVED":
                        continue
                    n_ts = n.get("tailscale_ip")
                    n_host = n.get("host")
                    n_ip = n.get("ip")
                    if (n_ts and n_ts in local_ips) or (n_host and n_host in local_ips) or (n_ip and n_ip in local_ips):
                        return nid

                # Check for hostname match
                try:
                    cur_host = socket.gethostname().lower()
                    for nid, n in nodes.items():
                        if (n.get("status") or "").upper() == "REMOVED":
                            continue
                        n_name = (n.get("name") or "").lower()
                        n_hostname = (n.get("hostname") or "").lower()
                        if cur_host and (cur_host == n_name or cur_host == n_hostname):
                            return nid
                except Exception:
                    pass
        except Exception:
            pass

    return socket.gethostname() or "node-master"


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
        on_demotion_callback: Optional[Callable[[], None]] = None,
        partition_safe: Optional[bool] = None,
        peer_probe_ports: Optional[List[int]] = None
    ):
        self._lock = threading.RLock()
        self.paths = paths or get_controller_paths()
        self.lease_file = getattr(self.paths, "leader_lease_file", self.paths.data_dir / "leader_lease.json")

        self.node_id = resolve_controller_node_id(node_id=node_id, paths=self.paths)
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

        # Multi-signal failure detection & partition safety
        self.partition_safe = (
            str(os.environ.get("PERSONALSERVER_HA_PARTITION_SAFE", "1")).lower() in ("1", "true", "yes")
            if partition_safe is None else bool(partition_safe)
        )
        self.peer_probe_ports: List[int] = list(peer_probe_ports or [8080, 8088, 8022])
        self.last_failure_mode: str = HAFailureMode.UNKNOWN
        self.consecutive_partition_events: int = 0

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
                new_term = max(1, disk_lease.term + (1 if disk_lease.leader_id and disk_lease.leader_id != self.node_id else 0))
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

    def probe_host_connectivity(self, target_url: str, timeout: float = 1.5) -> bool:
        """
        Probes whether the host device of target_url is reachable on any secondary
        service port (Node API: 8080, Router: 8088, SSH: 8022, etc.).
        Returns True if host/network is alive, False if network is partitioned or host down.
        """
        try:
            parsed = urllib.parse.urlparse(target_url)
            host = parsed.hostname
            if not host:
                return False
            # Loopback is always reachable locally
            if host in ("127.0.0.1", "localhost", "::1"):
                return True

            for port in self.peer_probe_ports:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(timeout)
                    err = s.connect_ex((host, port))
                    s.close()
                    # 0 = Connected; 111 (Linux ECONNREFUSED) / 10061 (Windows WSAECONNREFUSED)
                    # Connection refused means the OS kernel on the destination device actively answered with RST!
                    # That is definitive proof that Tailscale and the remote host device are alive!
                    if err in (0, 111, 10061):
                        return True
                except (ConnectionRefusedError, socket.error):
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def check_remote_leader(self) -> Tuple[bool, str]:
        """
        Pings current leader /ha/status (and fallback /health).
        Returns (is_alive, failure_mode):
          is_alive: bool
          failure_mode: HAFailureMode constant (HEALTHY, PROCESS_DEAD, NETWORK_PARTITION, API_ERROR)
        """
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
                return False, HAFailureMode.UNKNOWN

        url = f"{leader_url}/ha/status"
        req = urllib.request.Request(url, headers={"User-Agent": "PersonalServer-HAManager/1.0"})
        probe_timeout = min(2.5, max(0.4, self.check_interval_sec * 2.0))
        try:
            with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    remote_term = data.get("term", 0)
                    remote_version = data.get("state_version", 0)
                    remote_role = data.get("role", "")

                    if remote_role == HAState.LEADER:
                        with self._lock:
                            self.current_lease.lease_expires_at = time.time() + data.get("lease_remaining_sec", self.lease_duration_sec)
                            self.missed_checks = 0
                            self.consecutive_partition_events = 0
                            # If remote state is newer, trigger sync
                            if remote_version > self.state_version:
                                self.sync_state_from_leader(leader_url)
                        return True, HAFailureMode.HEALTHY
                    else:
                        return False, HAFailureMode.API_ERROR
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504):
                return False, HAFailureMode.API_ERROR
        except Exception:
            pass

        # Controller did not answer /ha/status. Test host liveness on peer ports to differentiate
        # Process Dead vs Network Partition.
        is_host_alive = self.probe_host_connectivity(leader_url)
        if is_host_alive:
            return False, HAFailureMode.PROCESS_DEAD
        else:
            return False, HAFailureMode.NETWORK_PARTITION

    def trigger_failover_evaluation(self):
        """Called periodically by monitor loop. Triggers failover if leader is dead."""
        with self._lock:
            if self.state == HAState.LEADER:
                # Check if current lease has expired without renewal (self-fencing)
                if not self.current_lease.is_valid():
                    controller_logger.warning(
                        "HA_FENCED",
                        "Leader lease expired for %s (Term %d). Self-fencing local controller to prevent split-brain.",
                        self.node_id, self.current_term
                    )
                    self.state = HAState.FENCED
                    if self.on_demotion_callback:
                        try:
                            self.on_demotion_callback()
                        except Exception as e:
                            controller_logger.error("HA_CALLBACK_ERROR", "Error in on_demotion_callback: %s", e)
                    return
                # Renew local lease
                self.renew_lease()
                return

            if self.state in (HAState.STANDBY, HAState.FENCED):
                alive, failure_mode = self.check_remote_leader()
                self.last_failure_mode = failure_mode

                if alive:
                    self.missed_checks = 0
                    self.consecutive_partition_events = 0
                    if self.state == HAState.FENCED:
                        self.state = HAState.STANDBY
                    return

                self.missed_checks += 1
                lease_expired = not self.current_lease.is_valid()

                if failure_mode == HAFailureMode.PROCESS_DEAD:
                    # Controller process is confirmed dead while host device is reachable
                    if self.missed_checks >= self.max_missed_checks or lease_expired:
                        controller_logger.warning(
                            "HA_FAILOVER",
                            "Leader controller process on %s is CONFIRMED DEAD (host alive, missed checks: %d). Promoting self!",
                            self.current_leader_url, self.missed_checks
                        )
                        self.elect_self(reason=f"Leader process at {self.current_leader_url} terminated")
                elif failure_mode == HAFailureMode.NETWORK_PARTITION:
                    self.consecutive_partition_events += 1
                    if self.partition_safe:
                        controller_logger.warning(
                            "HA_PARTITION",
                            "Suspected network/Tailscale partition to leader %s (consecutive: %d). Holding STANDBY to prevent split-brain.",
                            self.current_leader_url, self.consecutive_partition_events
                        )
                    else:
                        # Non-partition-safe override
                        if self.missed_checks >= self.max_missed_checks and lease_expired:
                            self.elect_self(reason=f"Leader unreachable (partition override)")
                elif failure_mode == HAFailureMode.API_ERROR:
                    if self.missed_checks >= self.max_missed_checks and lease_expired:
                        controller_logger.warning(
                            "HA_FAILOVER",
                            "Leader at %s returned API errors. Promoting self!",
                            self.current_leader_url
                        )
                        self.elect_self(reason=f"Leader API failure on {self.current_leader_url}")
                elif failure_mode == HAFailureMode.UNKNOWN:
                    if self.missed_checks >= self.max_missed_checks and lease_expired and not self.partition_safe:
                        self.elect_self(reason=f"Leader unreachable (unknown failure)")

    def manual_failover(self, target_node_url: Optional[str] = None) -> Dict[str, Any]:
        """Manually transfers leadership or forces local election."""
        with self._lock:
            if target_node_url and target_node_url != self.node_url:
                # Step down and defer to target
                new_term = self.current_term + 1
                self.step_down(new_term, "target-leader", target_node_url)
                return {"status": "success", "action": "stepped_down", "target": target_node_url, "term": new_term}
            elif self.is_leader:
                # If target not specified, look for available synchronized standbys to transfer to
                if self.standby_nodes:
                    candidates = sorted(
                        self.standby_nodes.values(),
                        key=lambda s: (s.get("in_sync", False), s.get("state_version", 0)),
                        reverse=True
                    )
                    chosen_target = candidates[0].get("url")
                    if chosen_target and chosen_target != self.node_url:
                        new_term = self.current_term + 1
                        self.step_down(new_term, "target-leader", chosen_target)
                        return {
                            "status": "success",
                            "action": "stepped_down",
                            "target": chosen_target,
                            "term": new_term,
                            "standby_node_id": candidates[0].get("node_id")
                        }
                # No standbys to fail over to
                return {
                    "status": "failed",
                    "action": "no_standbys",
                    "error": "No standby controller nodes available in the cluster to transfer leadership to.",
                    "node_id": self.node_id,
                    "term": self.current_term
                }
            else:
                # Local promotion from STANDBY to LEADER
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

    def resolve_leader_name(self, leader_id: Optional[str]) -> Tuple[str, bool, bool]:
        """
        Resolves leader_id against the cluster node database.
        Returns (display_name, is_online, is_registered).
        """
        if not leader_id or str(leader_id).strip() in ("None", "unknown", "-", ""):
            return ("None", False, False)

        target_id = str(leader_id).strip()
        nodes_file = getattr(self.paths, "nodes_file", self.paths.data_dir / "nodes.json")
        if nodes_file.exists():
            try:
                with open(nodes_file, "r", encoding="utf-8") as f:
                    db = json.load(f)
                    nodes = db.get("nodes", {})
                    node = nodes.get(target_id)
                    if not node:
                        for nid, n in nodes.items():
                            if n.get("name") == target_id or n.get("hostname") == target_id:
                                node = n
                                target_id = nid
                                break
                    if node:
                        name = node.get("name") or node.get("hostname") or target_id
                        status = (node.get("status") or "").upper()
                        is_online = (status == "ONLINE")
                        return (name, is_online, True)
            except Exception:
                pass
        return (target_id, False, False)

    def get_status_dict(self) -> Dict[str, Any]:
        """Returns structured HA status dictionary for HTTP endpoints and Web UI."""
        with self._lock:
            leader_id = self.current_leader_id
            leader_name, leader_online, leader_registered = self.resolve_leader_name(leader_id)

            return {
                "ha_enabled": True,
                "role": self.state,
                "node_id": self.node_id,
                "node_url": self.node_url,
                "leader_id": leader_id,
                "leader_name": leader_name,
                "leader_online": leader_online,
                "leader_registered": leader_registered,
                "leader_url": self.current_leader_url,
                "term": self.current_term,
                "state_version": self.state_version,
                "lease_valid": self.current_lease.is_valid(),
                "lease_remaining_sec": round(self.current_lease.time_remaining_sec(), 1),
                "missed_checks": self.missed_checks,
                "last_failure_mode": self.last_failure_mode,
                "partition_safe": self.partition_safe,
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
