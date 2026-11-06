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


def validate_peer_configuration(
    candidate_urls: List[str],
    local_node_url: str
) -> Tuple[bool, str, List[str]]:
    """
    Validates controller peer URL configuration (Phase 3).
    Returns: (is_valid: bool, status_msg: str, remote_peers: List[str])

    Rules:
    - Each URL must be valid HTTP/HTTPS with host and port.
    - At least one remote peer (excluding local node URL) must be configured.
    - Missing or self-only peer list is marked invalid and prevents uncoordinated promotion.
    """
    clean_local = local_node_url.rstrip("/").lower()
    remote_peers = []

    for raw_url in candidate_urls:
        u = str(raw_url).strip().rstrip("/")
        if not u:
            continue
        try:
            parsed = urllib.parse.urlparse(u)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return False, f"invalid_url: '{u}' is not a valid HTTP/HTTPS URL", []
        except Exception as e:
            return False, f"invalid_url: Failed to parse '{u}': {e}", []

        if u.lower() != clean_local:
            if u not in remote_peers:
                remote_peers.append(u)

    if not remote_peers:
        return False, "missing_peers: No remote controller peer URLs configured in candidate_urls", []

    return True, "valid", remote_peers


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
        peer_probe_ports: Optional[List[int]] = None,
        auto_promotion_enabled: Optional[bool] = None
    ):
        self._lock = threading.RLock()
        self.paths = paths or get_controller_paths()
        self.lease_file = getattr(self.paths, "leader_lease_file", self.paths.data_dir / "leader_lease.json")

        self.node_id = resolve_controller_node_id(node_id=node_id, paths=self.paths)
        self.node_url = str(node_url or "http://127.0.0.1:8000").rstrip("/")
        
        # Load candidate URLs from args or environment or node.conf
        env_candidates = os.environ.get("PERSONALSERVER_CONTROLLER_URLS", "")
        if not env_candidates:
            node_conf = getattr(self.paths, "config_dir", BASE_DIR / "config") / "node.conf"
            if node_conf.exists():
                try:
                    with open(node_conf, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.strip().startswith("PERSONALSERVER_CONTROLLER_URLS="):
                                env_candidates = line.split("=", 1)[1].strip().strip('"').strip("'")
                            elif not env_candidates and line.strip().startswith("CONTROLLER_URL="):
                                c_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                                if c_url:
                                    env_candidates = c_url
                except Exception:
                    pass
        parsed_candidates = [u.strip().rstrip("/") for u in env_candidates.split(",") if u.strip()]
        self.candidate_urls: List[str] = list(candidate_urls or parsed_candidates or [self.node_url])
        if self.node_url not in self.candidate_urls:
            self.candidate_urls.append(self.node_url)

        # Validate peer configuration (Phase 3)
        self.peer_config_valid, self.peer_config_status, self.configured_peers = validate_peer_configuration(
            self.candidate_urls, self.node_url
        )

        self.configured_role = role.lower()  # "leader", "standby", or "auto"
        self.lease_duration_sec = float(lease_duration_sec)
        self.heartbeat_interval_sec = float(heartbeat_interval_sec)
        self.check_interval_sec = float(check_interval_sec)
        self.auth_token = auth_token
        if not self.auth_token:
            et_file = getattr(self.paths, "enrollment_token_file", self.paths.secrets_dir / "enrollment.token")
            if et_file.exists():
                try:
                    self.auth_token = et_file.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
            if not self.auth_token:
                reg_file = getattr(self.paths, "runtime_dir", BASE_DIR / "runtime") / "registration.json"
                if reg_file.exists():
                    try:
                        with open(reg_file, "r", encoding="utf-8") as f:
                            self.auth_token = json.load(f).get("auth_token")
                    except Exception:
                        pass

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

        # Leadership authority model (Phase 4):
        # In a 2-node cluster without an external quorum witness, third-party lock service
        # (e.g. etcd, DynamoDB, Consul), or hardware fencing, isolated nodes cannot guarantee
        # exclusive lease ownership during partitions. To prevent dual-leader term collisions,
        # automatic uncoordinated promotion is disabled by default. Leadership transitions
        # must occur via controlled handover (POST /ha/handover) or explicit admin promotion.
        self.auto_promotion_enabled: bool = (
            auto_promotion_enabled if auto_promotion_enabled is not None
            else (str(os.environ.get("PERSONALSERVER_HA_AUTO_PROMOTION", "0")).lower() in ("1", "true", "yes"))
        )
        self.authority_mechanism: str = "cooperative_lease_no_external_grant"

        # Concurrency & freshness guards (Phase 2):
        self._eval_in_progress: bool = False
        self._eval_seq: int = 0

        # State fields
        self.state: str = HAState.STANDBY if self.configured_role == "standby" else HAState.LEADER
        self.current_lease: LeaderLease = LeaderLease.load_from_file(self.lease_file)
        self.state_version: int = self.current_lease.state_version
        self.missed_checks: int = 0
        self.max_missed_checks: int = 3
        self.standby_nodes: Dict[str, Dict[str, Any]] = {}
        self.last_sync_timestamp: Optional[str] = None

        # Leader management & failover preferences
        self.cluster_mode: str = str(os.environ.get("PERSONALSERVER_HA_MODE", "automatic")).lower()
        self.preferred_leader_id: Optional[str] = os.environ.get("PERSONALSERVER_PREFERRED_LEADER")
        self.last_transition: Dict[str, Any] = {
            "timestamp": get_current_iso_timestamp(),
            "reason": "Cluster initialization",
            "from_node": None,
            "to_node": self.node_id if self.is_leader else None,
            "term": self.current_term
        }

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
            role = self.configured_role

            if role == "leader":
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

            if role == "standby":
                self.state = HAState.STANDBY
                self.current_lease = disk_lease
                controller_logger.info("HA_INIT", "HAManager initialized as STANDBY")
                return

            # If local node already holds a valid, unexpired on-disk lease for itself,
            # resume active leadership without claiming an uncoordinated new lease.
            if disk_lease.is_valid() and disk_lease.leader_id == self.node_id:
                self.state = HAState.LEADER
                self.current_lease = disk_lease
                self.state_version = max(self.state_version, disk_lease.state_version)
                controller_logger.info(
                    "HA_INIT",
                    "Resumed existing active LEADER lease from disk (Term %d, Version %d).",
                    self.current_lease.term, self.state_version
                )
                return

            # Role == "auto": snapshot parameters needed for probe outside lock
            peer_valid = self.peer_config_valid
            peer_status = self.peer_config_status
            candidates = list(self.candidate_urls)
            local_url = self.node_url

        # Network discovery OUTSIDE the lock (Phase 2 & Phase 3)
        if not peer_valid:
            controller_logger.warning(
                "HA_INIT",
                "Peer configuration invalid (%s). Cannot claim leadership automatically; holding STANDBY.",
                peer_status
            )
            with self._lock:
                self.state = HAState.STANDBY
                self.current_lease = disk_lease
            return

        active_url, leader_info = find_active_leader(candidates, exclude_url=local_url)

        # Reacquire lock and apply discovery result atomically
        with self._lock:
            now = time.time()
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
                self.current_lease.save_to_file(self.lease_file)
                controller_logger.info("HA_INIT", "HAManager discovered active leader at %s. Running as STANDBY.", active_url)
            else:
                # No active leader found on network
                if self.auto_promotion_enabled:
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
                    controller_logger.info("HA_INIT", "No remote leader found. Auto-promotion enabled: elected self as LEADER (Term %d).", new_term)
                else:
                    self.state = HAState.STANDBY
                    self.current_lease = disk_lease
                    controller_logger.warning(
                        "HA_INIT",
                        "No remote leader discovered and auto_promotion_enabled is False. "
                        "Unreachable peer is not proof of death. Holding STANDBY until cooperative handover or authoritative grant."
                    )

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

    def _fetch_sync_snapshot_unlocked(self, leader_url: str, auth_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Fetches cluster state snapshot from leader HTTP endpoint OUTSIDE the lock.
        Returns the snapshot dict or None on failure.
        """
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
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            controller_logger.warning("HA_SYNC_FAILED", "Failed to fetch cluster state snapshot from %s: %s", leader_url, e)
            return None

    def _apply_sync_snapshot_locked(self, payload: Dict[str, Any], leader_url: str) -> bool:
        """
        Applies a fetched cluster state snapshot atomically to local storage UNDER self._lock.
        Assumes self._lock is already held by caller.
        """
        remote_term = payload.get("term", 0)
        remote_version = payload.get("state_version", 0)

        if remote_term < self.current_term and self.state == HAState.LEADER:
            return False  # Do not overwrite leader with older term

        try:
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
                "Synchronized cluster state from leader %s (State Version: %d, Term: %d)",
                leader_url, remote_version, remote_term
            )
            return True
        except Exception as e:
            controller_logger.warning("HA_SYNC_FAILED", "Failed to apply cluster state snapshot from %s: %s", leader_url, e)
            return False

    def sync_state_from_leader(self, leader_url: str, auth_token: Optional[str] = None) -> bool:
        """Pulls full cluster database snapshot from leader and stores locally without holding lock during fetch."""
        payload = self._fetch_sync_snapshot_unlocked(leader_url, auth_token)
        if not payload:
            return False
        with self._lock:
            return self._apply_sync_snapshot_locked(payload, leader_url)

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

    def probe_peer_leader(
        self,
        leader_url: Optional[str],
        candidate_urls: List[str],
        local_node_url: str,
        timeout: Optional[float] = None
    ) -> Tuple[bool, str, Optional[Dict[str, Any]], Optional[str]]:
        """
        Executes network I/O to probe the leader OUTSIDE the lock.
        Returns: (is_alive: bool, failure_mode: str, status_data: Optional[dict], resolved_leader_url: Optional[str])
        """
        resolved_url = leader_url
        if not resolved_url or resolved_url == local_node_url:
            discovered_url, leader_info = find_active_leader(candidate_urls, exclude_url=local_node_url)
            if discovered_url:
                resolved_url = discovered_url
            else:
                return False, HAFailureMode.UNKNOWN, None, None

        url = f"{resolved_url}/ha/status"
        req = urllib.request.Request(url, headers={"User-Agent": "PersonalServer-HAManager/1.0"})
        probe_timeout = timeout if timeout is not None else min(2.5, max(0.4, self.check_interval_sec * 2.0))

        try:
            with urllib.request.urlopen(req, timeout=probe_timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    remote_role = data.get("role", "")
                    if remote_role == HAState.LEADER:
                        return True, HAFailureMode.HEALTHY, data, resolved_url
                    else:
                        return False, HAFailureMode.API_ERROR, data, resolved_url
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504):
                return False, HAFailureMode.API_ERROR, None, resolved_url
        except Exception:
            pass

        # Controller did not answer /ha/status. Test host liveness on peer ports to differentiate
        # Process Dead vs Network Partition.
        is_host_alive = self.probe_host_connectivity(resolved_url)
        if is_host_alive:
            return False, HAFailureMode.PROCESS_DEAD, None, resolved_url
        else:
            return False, HAFailureMode.NETWORK_PARTITION, None, resolved_url

    def check_remote_leader(self) -> Tuple[bool, str]:
        """
        Pings current leader /ha/status (and fallback /health) without holding lock during I/O.
        Returns (is_alive, failure_mode).
        """
        with self._lock:
            leader_url = self.current_leader_url
            candidates = list(self.candidate_urls)
            local_url = self.node_url
            snapshot_version = self.state_version

        alive, mode, data, resolved_url = self.probe_peer_leader(
            leader_url=leader_url,
            candidate_urls=candidates,
            local_node_url=local_url
        )

        sync_payload = None
        if alive and data and resolved_url:
            if data.get("state_version", 0) > snapshot_version:
                sync_payload = self._fetch_sync_snapshot_unlocked(resolved_url)

        with self._lock:
            if resolved_url:
                self.current_lease.leader_url = resolved_url
            if alive:
                if data:
                    self.current_lease.lease_expires_at = time.time() + data.get("lease_remaining_sec", self.lease_duration_sec)
                    if data.get("leader_id"):
                        self.current_lease.leader_id = data["leader_id"]
                    if data.get("term"):
                        self.current_lease.term = data["term"]
                    self.current_lease.save_to_file(self.lease_file)
                self.missed_checks = 0
                self.consecutive_partition_events = 0
                if sync_payload and resolved_url:
                    self._apply_sync_snapshot_locked(sync_payload, resolved_url)
            return alive, mode

    def trigger_failover_evaluation(self):
        """Called periodically by monitor loop. Triggers failover if leader is dead."""
        # 1. Snapshot state and verify not overlapping under lock
        with self._lock:
            if self._eval_in_progress:
                return
            self._eval_in_progress = True
            self._eval_seq += 1
            eval_seq = self._eval_seq

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
                    self._eval_in_progress = False
                    return
                # Renew local lease
                self.renew_lease()
                self._eval_in_progress = False
                return

            if self.state not in (HAState.STANDBY, HAState.FENCED):
                self._eval_in_progress = False
                return

            snapshot_term = self.current_term
            snapshot_state = self.state
            snapshot_leader_id = self.current_leader_id
            snapshot_leader_url = self.current_leader_url
            snapshot_version = self.state_version
            candidates = list(self.candidate_urls)
            local_url = self.node_url

        # 2. Network probe OUTSIDE the lock (Phase 2)
        try:
            # Check remote leader using check_remote_leader if patched or fallback to probe_peer_leader
            alive, failure_mode = self.check_remote_leader()
            status_data, resolved_url = None, None

            sync_payload = None
            if alive and status_data and resolved_url:
                if status_data.get("state_version", 0) > snapshot_version:
                    sync_payload = self._fetch_sync_snapshot_unlocked(resolved_url)

            # 3. Reacquire lock and verify freshness before applying results
            with self._lock:
                # Overlapping / out-of-order evaluation check
                if eval_seq != self._eval_seq:
                    controller_logger.info(
                        "HA_EVAL_DISCARD",
                        "Discarding stale evaluation result (seq %d != active seq %d)",
                        eval_seq, self._eval_seq
                    )
                    return

                # Term change check
                if self.current_term != snapshot_term:
                    controller_logger.info(
                        "HA_EVAL_DISCARD",
                        "Discarding stale evaluation result: term changed (%d -> %d)",
                        snapshot_term, self.current_term
                    )
                    return

                # State change check
                if self.state != snapshot_state:
                    controller_logger.info(
                        "HA_EVAL_DISCARD",
                        "Discarding stale evaluation result: state changed (%s -> %s)",
                        snapshot_state, self.state
                    )
                    return

                # Peer identity change check
                if snapshot_leader_id and self.current_leader_id != snapshot_leader_id:
                    controller_logger.info(
                        "HA_EVAL_DISCARD",
                        "Discarding stale evaluation result: leader_id changed (%s -> %s)",
                        snapshot_leader_id, self.current_leader_id
                    )
                    return

                self.last_failure_mode = failure_mode

                if alive:
                    self.missed_checks = 0
                    self.consecutive_partition_events = 0
                    if resolved_url:
                        self.current_lease.leader_url = resolved_url
                    if status_data:
                        self.current_lease.lease_expires_at = time.time() + status_data.get("lease_remaining_sec", self.lease_duration_sec)
                        if status_data.get("leader_id"):
                            self.current_lease.leader_id = status_data["leader_id"]
                        if status_data.get("term"):
                            self.current_lease.term = status_data["term"]
                        self.current_lease.save_to_file(self.lease_file)
                    if self.state == HAState.FENCED:
                        self.state = HAState.STANDBY
                    if sync_payload and resolved_url:
                        self._apply_sync_snapshot_locked(sync_payload, resolved_url)
                    return

                # Leader is NOT alive
                self.missed_checks += 1
                lease_expired = not self.current_lease.is_valid()

                if failure_mode == HAFailureMode.NETWORK_PARTITION:
                    self.consecutive_partition_events += 1
                    controller_logger.warning(
                        "HA_PARTITION",
                        "Suspected network/Tailscale partition to leader %s (consecutive: %d). Holding STANDBY to prevent split-brain.",
                        snapshot_leader_url, self.consecutive_partition_events
                    )
                    return

                # Process dead, API error, or unknown failure:
                if self.missed_checks >= self.max_missed_checks or lease_expired:
                    # Phase 4 Authority & Promotion Safety Check
                    if not self.auto_promotion_enabled:
                        controller_logger.warning(
                            "HA_NO_AUTO_PROMOTION",
                            "Remote leader failure detected (%s, missed checks: %d), but auto_promotion_enabled=False. "
                            "Unreachable peer is not proof of death, and no distributed authoritative grant exists. Holding STANDBY.",
                            failure_mode, self.missed_checks
                        )
                        return

                    if not self.peer_config_valid:
                        controller_logger.warning(
                            "HA_FAILOVER_BLOCKED",
                            "Peer configuration is invalid (%s). Cannot promote self; holding STANDBY.",
                            self.peer_config_status
                        )
                        return

                    if failure_mode == HAFailureMode.PROCESS_DEAD:
                        controller_logger.warning(
                            "HA_FAILOVER",
                            "Leader controller process on %s is CONFIRMED DEAD (host alive, missed checks: %d). Promoting self!",
                            snapshot_leader_url, self.missed_checks
                        )
                        self.elect_self(reason=f"Leader process at {snapshot_leader_url} terminated")
                    elif failure_mode == HAFailureMode.API_ERROR:
                        controller_logger.warning(
                            "HA_FAILOVER",
                            "Leader at %s returned API errors. Promoting self!",
                            snapshot_leader_url
                        )
                        self.elect_self(reason=f"Leader API failure on {snapshot_leader_url}")
                    elif failure_mode == HAFailureMode.UNKNOWN and not self.partition_safe:
                        self.elect_self(reason=f"Leader unreachable (unknown failure)")
        finally:
            with self._lock:
                self._eval_in_progress = False

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

    def controlled_handover(self, target_node_id: str, admin_token: Optional[str] = None) -> Dict[str, Any]:
        """
        Executes a controlled, atomic leadership handover to target_node_id.
        1. Validates target node eligibility and health in cluster database under lock.
        2. Probes target node reachability and calls /ha/promote OUTSIDE lock.
        3. Reacquires lock, verifies term/leadership freshness, and steps down local leader only upon confirmed promotion.
        4. Updates transition provenance.
        """
        # 1. Resolve and validate target node information OUTSIDE HA lock (never acquire DB_LOCK under self._lock)
        if not self.is_leader:
            return {
                "status": "failed",
                "error": f"Cannot execute handover: Local node '{self.node_id}' is not the active LEADER."
            }

        if target_node_id == self.node_id:
            return {
                "status": "failed",
                "error": f"Target node '{target_node_id}' is already the active LEADER."
            }

        from controller.stores import load_nodes_db
        nodes_db = load_nodes_db(paths=self.paths)
        target_entry = nodes_db.get("nodes", {}).get(target_node_id)
        if not target_entry:
            return {
                "status": "failed",
                "error": f"Target node '{target_node_id}' is not registered in the cluster."
            }

        from controller.leader_ranking import apply_default_leadership_metadata
        apply_default_leadership_metadata(target_node_id, target_entry)

        if not target_entry.get("leader_eligible", False):
            return {
                "status": "failed",
                "error": f"Target node '{target_node_id}' has leader_eligible=False and cannot assume leadership."
            }

        if str(target_entry.get("status", "")).upper() != "ONLINE":
            return {
                "status": "failed",
                "error": f"Target node '{target_node_id}' is not ONLINE (current status: {target_entry.get('status')})."
            }

        target_ip = target_entry.get("tailscale_ip") or target_entry.get("ip") or target_entry.get("host")
        if not target_ip:
            return {
                "status": "failed",
                "error": f"Target node '{target_node_id}' has no reachable IP or Tailscale address."
            }
        target_url = f"http://{target_ip}:8000"

        # 2. Acquire HA lock for state validation and transition preparation
        with self._lock:
            if not self.is_leader:
                return {
                    "status": "failed",
                    "error": f"Cannot execute handover: Local node '{self.node_id}' is not the active LEADER."
                }

            if target_node_id == self.node_id:
                return {
                    "status": "failed",
                    "error": f"Target node '{target_node_id}' is already the active LEADER."
                }

            # Snapshot term for promote
            snapshot_term = self.current_term
            new_term = snapshot_term + 1
            token = admin_token or self.auth_token

        # 3. Call target /ha/promote OUTSIDE self._lock (Phase 2)
        payload = json.dumps({
            "reason": f"Controlled handover from {self.node_id}",
            "term": new_term
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{target_url}/ha/promote",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}" if token else "",
                "User-Agent": "PersonalServer-Handover/1.0"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status != 200:
                    return {
                        "status": "failed",
                        "error": f"Target node returned HTTP {resp.status} during promotion request."
                    }
                promo_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {
                "status": "failed",
                "error": f"Failed to promote target node at {target_url}: {e}"
            }

        # 4. Confirmed promotion: reacquire lock and step down local node
        with self._lock:
            # Check term freshness
            if self.current_term != snapshot_term or self.state != HAState.LEADER:
                return {
                    "status": "failed",
                    "error": "Cluster state or leadership term changed during handover promotion."
                }

            self.step_down(new_term, target_node_id, target_url)
            self.last_transition = {
                "timestamp": get_current_iso_timestamp(),
                "reason": f"Controlled handover to {target_node_id}",
                "from_node": self.node_id,
                "to_node": target_node_id,
                "term": new_term
            }
            return {
                "status": "success",
                "action": "handover_completed",
                "from_node": self.node_id,
                "to_node": target_node_id,
                "target_url": target_url,
                "term": new_term,
                "promotion_response": promo_data
            }

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

    def resolve_leader_name(self, leader_id: Optional[str], nodes: Optional[Dict[str, Any]] = None) -> Tuple[str, bool, bool]:
        """
        Resolves leader_id against the cluster node database.
        Returns (display_name, is_online, is_registered).
        """
        if not leader_id or str(leader_id).strip() in ("None", "unknown", "-", ""):
            return ("None", False, False)

        target_id = str(leader_id).strip()
        nodes_dict = nodes
        if nodes_dict is None:
            nodes_file = getattr(self.paths, "nodes_file", self.paths.data_dir / "nodes.json")
            if nodes_file.exists():
                try:
                    with open(nodes_file, "r", encoding="utf-8") as f:
                        db = json.load(f)
                        nodes_dict = db.get("nodes", {})
                except Exception:
                    pass
        if nodes_dict:
            node = nodes_dict.get(target_id)
            if not node:
                for nid, n in nodes_dict.items():
                    if n.get("name") == target_id or n.get("hostname") == target_id:
                        node = n
                        target_id = nid
                        break
            if node:
                name = node.get("name") or node.get("hostname") or target_id
                status = (node.get("status") or "").upper()
                is_online = (status == "ONLINE")
                return (name, is_online, True)
        return (target_id, False, False)

    def get_status_dict(self) -> Dict[str, Any]:
        """Returns structured HA status dictionary for HTTP endpoints and Web UI."""
        # 1. Read node database snapshot BEFORE acquiring self._lock (never acquire DB_LOCK under self._lock)
        nodes = {}
        try:
            from controller.stores import load_nodes_db
            nodes_db = load_nodes_db(paths=self.paths)
            if isinstance(nodes_db, dict):
                nodes = nodes_db.get("nodes", {})
        except Exception:
            pass

        # 2. Acquire self._lock ONLY for snapshotting local HA state
        with self._lock:
            role = self.state
            node_id = self.node_id
            node_url = self.node_url
            leader_id = self.current_leader_id
            leader_url = self.current_leader_url
            current_term = self.current_term
            state_version = self.state_version
            lease_valid = self.current_lease.is_valid()
            lease_remaining_sec = round(self.current_lease.time_remaining_sec(), 1)
            missed_checks = self.missed_checks
            last_failure_mode = self.last_failure_mode
            partition_safe = self.partition_safe
            candidate_urls = list(self.candidate_urls)
            peer_config_valid = self.peer_config_valid
            peer_config_status = self.peer_config_status
            configured_peers = list(self.configured_peers)
            auto_promotion_enabled = self.auto_promotion_enabled
            authority_mechanism = self.authority_mechanism
            standbys_count = len(self.standby_nodes)
            standbys = list(self.standby_nodes.values())
            last_sync_timestamp = self.last_sync_timestamp
            cluster_mode = self.cluster_mode
            preferred_leader_id = self.preferred_leader_id
            last_transition = dict(self.last_transition) if self.last_transition else None

        # 3. Resolve leader display name and candidate rankings OUTSIDE all locks
        leader_name, leader_online, leader_registered = self.resolve_leader_name(leader_id, nodes=nodes)

        ranked = []
        try:
            from controller.leader_ranking import rank_leader_candidates
            ranked_items = rank_leader_candidates(
                nodes,
                current_leader_id=leader_id,
                preferred_leader_id=preferred_leader_id,
                mode=cluster_mode
            )
            for nid, score, n in ranked_items:
                ranked.append({
                    "node_id": nid,
                    "name": n.get("name") or nid,
                    "score": score,
                    "priority": n.get("priority", 50),
                    "leader_eligible": n.get("leader_eligible", True),
                    "preferred_leader": n.get("preferred_leader", False),
                    "status": n.get("status", "UNKNOWN"),
                    "worker_enabled": n.get("worker_enabled", True),
                    "build_worker_enabled": n.get("build_worker_enabled", False),
                })
        except Exception:
            pass

        return {
            "ha_enabled": True,
            "role": role,
            "node_id": node_id,
            "node_url": node_url,
            "leader_id": leader_id,
            "leader_name": leader_name,
            "leader_online": leader_online,
            "leader_registered": leader_registered,
            "leader_url": leader_url,
            "term": current_term,
            "state_version": state_version,
            "lease_valid": lease_valid,
            "lease_remaining_sec": lease_remaining_sec,
            "missed_checks": missed_checks,
            "last_failure_mode": last_failure_mode,
            "partition_safe": partition_safe,
            "candidate_urls": candidate_urls,
            "peer_config_valid": peer_config_valid,
            "peer_config_status": peer_config_status,
            "configured_peers": configured_peers,
            "auto_promotion_enabled": auto_promotion_enabled,
            "authority_mechanism": authority_mechanism,
            "split_brain_mitigation": "automatic_promotion_disabled" if not auto_promotion_enabled else "active_fencing",
            "standbys_count": standbys_count,
            "standbys": standbys,
            "last_sync_timestamp": last_sync_timestamp,
            "cluster_mode": cluster_mode,
            "preferred_leader_id": preferred_leader_id,
            "last_transition": last_transition,
            "ranked_candidates": ranked,
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
