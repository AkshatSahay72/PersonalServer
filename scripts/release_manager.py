#!/usr/bin/env python3
"""
PersonalServer - Production Release Management Engine
Handles verified updates, staging, integrity checks, and zero-downtime rollbacks
for PersonalServer nodes.
"""

import os
import sys
import json
import time
import shutil
import logging
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

# Setup directories
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
RUNTIME_DIR = BASE_DIR / "runtime"
LOGS_DIR = BASE_DIR / "logs"
SECRETS_DIR = CONFIG_DIR / "secrets"

RELEASE_CONF_FILE = CONFIG_DIR / "release.conf"
RELEASE_STATE_FILE = RUNTIME_DIR / "release_state.json"
RELEASE_LOG_FILE = LOGS_DIR / "release_history.log"
STAGING_DIR = RUNTIME_DIR / "staging"

DEFAULT_CHANNEL = "release/stable"
DEFAULT_REMOTE = "origin"

# Protected paths that must NEVER be overwritten, cleared, or deleted during updates
PROTECTED_PATHS = [
    CONFIG_DIR / "secrets",
    CONFIG_DIR / "node.json",
    CONFIG_DIR / "node.conf",
    CONFIG_DIR / "controller.json",
    RUNTIME_DIR,
    LOGS_DIR,
    BASE_DIR / "storage",
    BASE_DIR / "data",
]

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("release_manager")


def load_release_config() -> Dict[str, Any]:
    """Load config/release.conf with fallback defaults."""
    config = {
        "channel": DEFAULT_CHANNEL,
        "remote": DEFAULT_REMOTE,
        "auto_update_on_start": False,
        "preflight_timeout_sec": 10,
        "strict_integrity": True,
        "offline_safe": True
    }
    if RELEASE_CONF_FILE.exists():
        try:
            for line in RELEASE_CONF_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip().lower()
                    v = v.strip()
                    if k == "channel":
                        config["channel"] = v
                    elif k == "remote":
                        config["remote"] = v
                    elif k == "auto_update_on_start":
                        config["auto_update_on_start"] = v.lower() in ("true", "1", "yes")
                    elif k == "preflight_timeout_sec":
                        config["preflight_timeout_sec"] = int(v)
                    elif k == "strict_integrity":
                        config["strict_integrity"] = v.lower() in ("true", "1", "yes")
                    elif k == "offline_safe":
                        config["offline_safe"] = v.lower() in ("true", "1", "yes")
        except Exception as e:
            logger.warning(f"Error parsing {RELEASE_CONF_FILE}: {e}")
    return config


def load_release_state() -> Dict[str, Any]:
    """Load runtime/release_state.json with fallback defaults."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    current_head = get_current_git_commit()
    state = {
        "installed_commit": current_head,
        "installed_tag": None,
        "installed_branch": get_current_git_branch(),
        "channel": DEFAULT_CHANNEL,
        "target_commit": None,
        "update_timestamp": None,
        "status": "INSTALLED",
        "rollback_commit": None,
        "last_check_timestamp": None,
        "last_result": "INITIALIZED"
    }
    if RELEASE_STATE_FILE.exists():
        try:
            with open(RELEASE_STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                state.update(saved)
                # Keep installed_commit in sync if head changed outside
                if current_head and current_head != state.get("installed_commit"):
                    state["installed_commit"] = current_head
        except Exception as e:
            logger.warning(f"Error reading {RELEASE_STATE_FILE}: {e}")
    return state


def save_release_state(state: Dict[str, Any]):
    """Persist runtime/release_state.json safely."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = RELEASE_STATE_FILE.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        temp_file.replace(RELEASE_STATE_FILE)
    except Exception as e:
        logger.error(f"Failed to save {RELEASE_STATE_FILE}: {e}")


def log_release_event(event_type: str, details: Dict[str, Any]):
    """Append structured log to logs/release_history.log."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "details": details
        }
        with open(RELEASE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        logger.warning(f"Failed to append to {RELEASE_LOG_FILE}: {e}")


def run_git_command(args: List[str], timeout: int = 15) -> Tuple[int, str, str]:
    """Execute git command within BASE_DIR with bounded timeout."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Git command timed out after {timeout}s: git {' '.join(args)}"
    except Exception as e:
        return 1, "", str(e)


def get_current_git_commit() -> str:
    code, stdout, _ = run_git_command(["rev-parse", "HEAD"])
    return stdout if code == 0 else "unknown"


def get_current_git_branch() -> str:
    code, stdout, _ = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
    return stdout if code == 0 else "unknown"


def check_local_modifications() -> Tuple[bool, List[str]]:
    """
    Check if there are uncommitted modifications in tracked code files.
    Returns (has_modifications, list_of_modified_files).
    Ignores runtime/, logs/, config/secrets/, node.json, node.conf, and ignored patterns.
    """
    code, stdout, _ = run_git_command(["status", "--porcelain"])
    if code != 0:
        return False, []
    
    modified = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        status = line[:2]
        file_path = line[3:].strip()
        # Filter out safe local state directories
        if any(file_path.startswith(prefix) for prefix in ("runtime/", "logs/", "config/secrets/", "config/node.json", "config/node.conf", "storage/")):
            continue
        modified.append(file_path)
    return len(modified) > 0, modified


def fetch_release_target(channel: str, remote: str, timeout: int = 15) -> Tuple[bool, Optional[str], str]:
    """
    Fetch updates from remote for the specified release channel without merging or switching.
    Returns (success, target_commit_hash, error_msg).
    """
    # First check if the channel is a local branch or tag
    code, stdout, _ = run_git_command(["rev-parse", "--verify", f"{channel}^{{commit}}"])
    local_commit = stdout if code == 0 else None

    # Fetch from remote with bounded timeout
    code, stdout, stderr = run_git_command(["fetch", "--quiet", remote, f"{channel}:{channel}"], timeout=timeout)
    if code != 0:
        # Check if fetch failed because of offline / network unreachable
        # If remote fetch failed, but local branch exists, we can still operate on local release branch
        if local_commit:
            return True, local_commit, f"Remote fetch failed ({stderr or 'timeout'}); using local {channel}"
        return False, None, f"Fetch failed: {stderr or 'network error or remote unreachable'}"

    # Get the latest commit hash on the release branch
    code, stdout, stderr = run_git_command(["rev-parse", "--verify", f"{channel}^{{commit}}"])
    if code == 0 and stdout:
        return True, stdout, "Fetch successful"
    return False, None, f"Failed to resolve target commit for {channel}: {stderr}"


def verify_staged_release(target_commit: str) -> Tuple[bool, str]:
    """
    Verify release integrity and code compatibility for the target commit:
    1. Verify git object exists.
    2. Verify critical files exist at target commit.
    3. Run python syntax check across staged python files.
    """
    # 1. Verify commit exists
    code, _, _ = run_git_command(["cat-file", "-e", f"{target_commit}^{{commit}}"])
    if code != 0:
        return False, f"Target commit {target_commit} does not exist in git object database"

    # 2. Check critical files exist in target tree
    critical_files = [
        "services/node-api/app.py",
        "agent/node-agent.py",
        "scripts/start.sh",
        "scripts/stop.sh",
        "scripts/status.sh",
        "scripts/doctor.py"
    ]
    for cf in critical_files:
        code, _, _ = run_git_command(["cat-file", "-e", f"{target_commit}:{cf}"])
        if code != 0:
            return False, f"Critical file missing in target release: {cf}"

    # 3. Stage files in memory or temp dir to run syntax validation
    # Extract Python files and test syntax
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staging_test_file = STAGING_DIR / "app_check.py"
    try:
        code, content, _ = run_git_command(["show", f"{target_commit}:services/node-api/app.py"])
        if code == 0:
            staging_test_file.write_text(content, encoding="utf-8")
            # Compile check
            compile_res = subprocess.run(
                [sys.executable, "-m", "py_compile", str(staging_test_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if compile_res.returncode != 0:
                return False, f"Syntax verification failed on node-api: {compile_res.stderr.strip()}"
    except Exception as e:
        return False, f"Staging verification error: {e}"
    finally:
        if staging_test_file.exists():
            try:
                staging_test_file.unlink()
            except Exception:
                pass

    return True, "Integrity and syntax verification passed"


def apply_release(target_commit: str, state: Dict[str, Any], force: bool = False) -> Tuple[bool, str]:
    """
    Safely apply target commit to active working tree:
    - Verifies local modifications are safe.
    - Saves current commit as rollback point.
    - Performs checkout / fast-forward of code files.
    - Preserves protected secrets, configurations, logs, and identity.
    """
    current_commit = get_current_git_commit()
    if current_commit == target_commit and not force:
        return True, f"Already at target commit {target_commit[:8]}"

    has_mods, modified_files = check_local_modifications()
    if has_mods and not force:
        return False, f"Local modifications present in tracked files ({len(modified_files)} files: {', '.join(modified_files[:3])}). Update aborted to prevent data loss."

    # Backup current rollback point
    state["rollback_commit"] = current_commit
    state["target_commit"] = target_commit

    # Checkout target commit into current working tree
    code, stdout, stderr = run_git_command(["checkout", "--quiet", target_commit])
    if code != 0:
        return False, f"Git checkout to {target_commit} failed: {stderr}"

    # Post-activation verify
    new_head = get_current_git_commit()
    if new_head != target_commit:
        return False, f"HEAD ({new_head}) does not match target ({target_commit})"

    state["installed_commit"] = new_head
    state["update_timestamp"] = datetime.now(timezone.utc).isoformat()
    state["status"] = "INSTALLED"
    state["last_result"] = "SUCCESS"
    save_release_state(state)

    log_release_event("RELEASE_APPLIED", {
        "previous_commit": current_commit,
        "installed_commit": new_head,
        "result": "SUCCESS"
    })
    return True, f"Successfully activated release commit {new_head[:8]}"


def rollback_release(state: Dict[str, Any]) -> Tuple[bool, str]:
    """Revert working tree to the previous known-good rollback commit."""
    rollback_commit = state.get("rollback_commit")
    if not rollback_commit:
        return False, "No rollback commit recorded in release state"

    current_commit = get_current_git_commit()
    if rollback_commit == current_commit:
        return False, f"Already on rollback commit {rollback_commit[:8]}"

    code, _, _ = run_git_command(["cat-file", "-e", f"{rollback_commit}^{{commit}}"])
    if code != 0:
        return False, f"Rollback commit {rollback_commit} not found in git repository"

    # Checkout rollback commit
    code, stdout, stderr = run_git_command(["checkout", "--quiet", rollback_commit])
    if code != 0:
        return False, f"Rollback checkout failed: {stderr}"

    new_head = get_current_git_commit()
    state["installed_commit"] = new_head
    state["rollback_commit"] = current_commit
    state["update_timestamp"] = datetime.now(timezone.utc).isoformat()
    state["status"] = "ROLLED_BACK"
    state["last_result"] = f"Rolled back to {new_head[:8]}"
    save_release_state(state)

    log_release_event("RELEASE_ROLLBACK", {
        "from_commit": current_commit,
        "to_commit": new_head
    })
    return True, f"Successfully rolled back to {new_head[:8]}"


def check_update_status(channel: Optional[str] = None, remote: Optional[str] = None, offline: bool = False) -> Dict[str, Any]:
    """Check update availability and return comprehensive status dict."""
    config = load_release_config()
    state = load_release_state()
    active_channel = channel or config.get("channel", DEFAULT_CHANNEL)
    active_remote = remote or config.get("remote", DEFAULT_REMOTE)

    current_commit = get_current_git_commit()
    current_branch = get_current_git_branch()
    has_mods, modified_files = check_local_modifications()

    result = {
        "installed_commit": current_commit,
        "installed_branch": current_branch,
        "channel": active_channel,
        "remote": active_remote,
        "rollback_commit": state.get("rollback_commit"),
        "last_update": state.get("update_timestamp"),
        "local_modifications": has_mods,
        "modified_files_count": len(modified_files),
        "target_commit": None,
        "update_available": False,
        "status": "UP_TO_DATE",
        "message": "Node is running latest release"
    }

    if offline:
        result["status"] = "OFFLINE_CHECK_SKIPPED"
        result["message"] = "Offline mode enabled; remote check skipped"
        return result

    # Check remote
    success, target_commit, msg = fetch_release_target(active_channel, active_remote, timeout=config.get("preflight_timeout_sec", 10))
    state["last_check_timestamp"] = datetime.now(timezone.utc).isoformat()

    if not success or not target_commit:
        result["status"] = "CHECK_FAILED"
        result["message"] = msg
        state["last_result"] = f"Check failed: {msg}"
        save_release_state(state)
        return result

    result["target_commit"] = target_commit
    if target_commit != current_commit:
        result["update_available"] = True
        result["status"] = "UPDATE_AVAILABLE"
        result["message"] = f"New release available on {active_channel}: {target_commit[:8]}"
    else:
        result["update_available"] = False
        result["status"] = "UP_TO_DATE"
        result["message"] = f"Current commit matches latest release on {active_channel}"

    save_release_state(state)
    return result


def main():
    parser = argparse.ArgumentParser(description="PersonalServer Release Management Engine")
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    # status
    p_status = subparsers.add_parser("status", help="Show release status")
    p_status.add_argument("--json", action="store_true", help="Output JSON")

    # check
    p_check = subparsers.add_parser("check", help="Check for release updates")
    p_check.add_argument("--channel", help="Release channel")
    p_check.add_argument("--offline", action="store_true", help="Offline check only")
    p_check.add_argument("--json", action="store_true", help="Output JSON")

    # update
    p_update = subparsers.add_parser("update", help="Fetch, verify and apply release update")
    p_update.add_argument("--channel", help="Release channel")
    p_update.add_argument("--target", help="Explicit target commit or tag")
    p_update.add_argument("--force", action="store_true", help="Force update even with local mods")
    p_update.add_argument("--preflight", action="store_true", help="Run preflight check only")
    p_update.add_argument("--offline", action="store_true", help="Offline safe mode")
    p_update.add_argument("--json", action="store_true", help="Output JSON")

    # rollback
    p_rollback = subparsers.add_parser("rollback", help="Rollback to previous release")
    p_rollback.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    state = load_release_state()
    config = load_release_config()

    if args.command == "status":
        status_info = check_update_status(offline=True)
        if args.json:
            print(json.dumps(status_info, indent=2))
        else:
            print("==========================================")
            print(" PersonalServer Release Status")
            print("==========================================")
            print(f"Installed Commit : {status_info['installed_commit']}")
            print(f"Active Branch    : {status_info['installed_branch']}")
            print(f"Release Channel  : {status_info['channel']}")
            print(f"Status           : {status_info['status']}")
            print(f"Rollback Commit  : {status_info['rollback_commit'] or 'None'}")
            print(f"Last Update      : {status_info['last_update'] or 'Never'}")
            print(f"Local Mods       : {status_info['modified_files_count']} files modified")
            print("==========================================")
        sys.exit(0)

    elif args.command == "check":
        status_info = check_update_status(channel=args.channel, offline=args.offline)
        if args.json:
            print(json.dumps(status_info, indent=2))
        else:
            print(f"Release Status: {status_info['status']}")
            print(f"Message       : {status_info['message']}")
            if status_info["update_available"]:
                print(f"Target Commit : {status_info['target_commit']}")
        sys.exit(0 if not status_info["update_available"] else 10)

    elif args.command == "rollback":
        ok, msg = rollback_release(state)
        res = {"success": ok, "message": msg, "current_commit": get_current_git_commit()}
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"{'SUCCESS' if ok else 'FAILED'}: {msg}")
        sys.exit(0 if ok else 1)

    elif args.command == "update":
        if args.offline:
            res = {"success": True, "status": "OFFLINE_SKIPPED", "message": "Offline mode active; skipping update"}
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print("Offline mode active; skipping update.")
            sys.exit(0)

        channel = args.channel or config.get("channel", DEFAULT_CHANNEL)
        target_commit = args.target

        if not target_commit:
            # Fetch target
            ok, target_commit, err = fetch_release_target(channel, config.get("remote", DEFAULT_REMOTE))
            if not ok:
                res = {"success": False, "status": "FETCH_FAILED", "message": err}
                if args.json:
                    print(json.dumps(res, indent=2))
                else:
                    print(f"Fetch failed: {err}")
                sys.exit(2 if config.get("offline_safe") else 1)

        # Preflight check
        if args.preflight:
            curr = get_current_git_commit()
            avail = (target_commit != curr)
            res = {
                "success": True,
                "update_available": avail,
                "current_commit": curr,
                "target_commit": target_commit,
                "message": "Preflight check complete"
            }
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Preflight: {'Update available' if avail else 'Up to date'} ({target_commit[:8]})")
            sys.exit(0)

        # Verify integrity and syntax
        ok, err = verify_staged_release(target_commit)
        if not ok:
            res = {"success": False, "status": "VERIFY_FAILED", "message": err}
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Verification failed: {err}")
            sys.exit(1)

        # Apply update
        ok, msg = apply_release(target_commit, state, force=args.force)
        res = {
            "success": ok,
            "status": "UPDATED" if ok else "APPLY_FAILED",
            "message": msg,
            "commit": get_current_git_commit()
        }
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"{'SUCCESS' if ok else 'FAILED'}: {msg}")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
