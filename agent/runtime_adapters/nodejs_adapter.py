#!/usr/bin/env python3
"""
PersonalServer Native Node.js Runtime Adapter
=============================================
Executes Node.js web service releases in isolated directory environments:
- Verifies host Node.js (node >= 14.0) and npm toolchain
- Discovers entry point via package.json ('main', 'scripts.start') or conventional candidates
- Installs dependencies locally into release node_modules via npm ci / npm install
- Guarantees zero global npm package modifications and zero shared node_modules
- Enforces bounded dependency installation timeouts
- Safely manages lifecycle via SafeProcessLifecycleManager
- Integrates seamlessly with ResourceScheduler, health checks, and ApplicationRouter
"""

import os
import sys
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.process_manager import (
    SafeProcessLifecycleManager,
    ProcessIdentityRecord,
    LOGS_DIR,
)
from agent.runtime_adapters.base import RuntimeAdapter

# Conventional fallback entry points searched in order
NODE_ENTRY_CANDIDATES = [
    "server.js",
    "app.js",
    "index.js",
    "main.js",
    "src/server.js",
    "src/app.js",
    "src/index.js",
    "src/main.js",
    "bin/www"
]


class NodejsProcessAdapter(RuntimeAdapter):
    """
    Hermetic Node.js runtime adapter that prepares release dependencies
    and manages process execution via SafeProcessLifecycleManager.
    """

    def __init__(
        self,
        node_executable: Optional[str] = None,
        npm_executable: Optional[str] = None
    ):
        self._node_exe = node_executable
        self._npm_exe = npm_executable

    @property
    def runtime_name(self) -> str:
        return "nodejs"

    def get_binaries(self) -> Tuple[Optional[str], Optional[str]]:
        """Resolves node and npm binary paths."""
        node_bin = self._node_exe or shutil.which("node") or shutil.which("nodejs")
        npm_bin = self._npm_exe or shutil.which("npm")
        return node_bin, npm_bin

    def validate_environment(self) -> Tuple[bool, Optional[str]]:
        """
        Validates host Node.js and npm availability and runtime version.
        Requires Node.js >= 14.0.0.
        """
        node_bin, npm_bin = self.get_binaries()
        if not node_bin:
            return False, "Node.js executable ('node') not found on PATH."
        if not npm_bin:
            return False, "npm package manager executable ('npm') not found on PATH."

        try:
            res = subprocess.run(
                [node_bin, "-v"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5.0
            )
            if res.returncode != 0:
                return False, f"Node.js check failed with exit code {res.returncode}: {res.stderr.strip()}"
            
            raw_ver = res.stdout.strip().lstrip("v")
            parts = raw_ver.split(".")
            major = int(parts[0]) if parts and parts[0].isdigit() else 0
            if major < 14:
                return False, f"Node.js >= 14.0.0 required. Host Node.js version is v{raw_ver}."
        except subprocess.TimeoutExpired:
            return False, "Node.js version check timed out after 5s."
        except Exception as e:
            return False, f"Node.js environment validation failed: {e}"

        try:
            res_npm = subprocess.run(
                [npm_bin, "-v"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5.0
            )
            if res_npm.returncode != 0:
                return False, f"npm check failed with exit code {res_npm.returncode}: {res_npm.stderr.strip()}"
            return True, None
        except subprocess.TimeoutExpired:
            return False, "npm version check timed out after 5s."
        except Exception as e:
            return False, f"npm environment validation failed: {e}"

    def detect_entry_point(self, release_dir: Path) -> Tuple[Optional[List[str]], Optional[str]]:
        """
        Inspects package.json or candidate files to determine the execution command.
        Returns (command_tokens, error_message).
        """
        release_dir = Path(release_dir).resolve()
        node_bin, npm_bin = self.get_binaries()
        target_node = node_bin or "node"
        target_npm = npm_bin or "npm"

        pkg_path = release_dir / "package.json"
        pkg_data: Dict[str, Any] = {}

        if pkg_path.exists():
            try:
                content = pkg_path.read_text(encoding="utf-8")
                pkg_data = json.loads(content)
            except Exception as e:
                return None, f"Invalid package.json: {e}"

            if not isinstance(pkg_data, dict):
                return None, "Invalid package.json: Root element must be an object."

            # 1. Check 'main' field
            main_field = pkg_data.get("main")
            if main_field and isinstance(main_field, str):
                main_path = (release_dir / main_field).resolve()
                if main_path.exists() and main_path.is_file():
                    return [target_node, str(main_path)], None
                # Try with .js extension if omitted
                if not main_field.endswith(".js"):
                    main_js = (release_dir / f"{main_field}.js").resolve()
                    if main_js.exists() and main_js.is_file():
                        return [target_node, str(main_js)], None

            # 2. Check conventional entry files
            for cand in NODE_ENTRY_CANDIDATES:
                cand_path = release_dir / cand
                if cand_path.exists() and cand_path.is_file():
                    return [target_node, str(cand_path.resolve())], None

            # 3. Check 'scripts.start'
            scripts = pkg_data.get("scripts", {})
            if isinstance(scripts, dict) and "start" in scripts:
                start_script = str(scripts["start"]).strip()
                tokens = start_script.split()
                if tokens:
                    first_tok = os.path.basename(tokens[0]).lower()
                    if first_tok in ("node", "nodejs", "node.exe"):
                        tokens[0] = target_node
                        # Resolve script path if second token is a local file
                        if len(tokens) > 1 and not tokens[1].startswith("-"):
                            cand_file = (release_dir / tokens[1]).resolve()
                            if cand_file.exists():
                                tokens[1] = str(cand_file)
                        return tokens, None
                    else:
                        # Complex or wrapped start script -> execute via npm start
                        return [target_npm, "start"], None

            return None, (
                f"No entry point found for application in '{release_dir}'. "
                "Specify 'main' or 'scripts.start' in package.json, or supply server.js / app.js / index.js."
            )

        # Standalone Node.js script without package.json
        for cand in NODE_ENTRY_CANDIDATES:
            cand_path = release_dir / cand
            if cand_path.exists() and cand_path.is_file():
                return [target_node, str(cand_path.resolve())], None

        return None, (
            f"No Node.js entry point or package.json found in '{release_dir}'. "
            "Expected package.json, server.js, app.js, or index.js."
        )

    def prepare_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        env_vars: Dict[str, str],
        timeout_sec: float = 180.0
    ) -> Tuple[bool, Optional[str]]:
        """
        Installs dependencies inside release_dir/node_modules without global modifications.
        Uses npm ci when package-lock.json is present; falls back to npm install.
        """
        release_dir = Path(release_dir).resolve()
        if not release_dir.exists() or not release_dir.is_dir():
            return False, f"Release directory '{release_dir}' not found."

        # Validate entry point and package.json validity upfront
        cmd_tokens, detect_err = self.detect_entry_point(release_dir)
        if not cmd_tokens:
            return False, detect_err

        node_bin, npm_bin = self.get_binaries()
        if not npm_bin:
            return False, "npm executable not found on PATH."

        pkg_path = release_dir / "package.json"
        if not pkg_path.exists():
            # Standalone script without external dependencies
            return True, None

        # Inspect dependencies
        try:
            pkg_data = json.loads(pkg_path.read_text(encoding="utf-8"))
        except Exception as e:
            return False, f"Invalid package.json: {e}"

        deps = pkg_data.get("dependencies", {})
        dev_deps = pkg_data.get("devDependencies", {})
        lock_path = release_dir / "package-lock.json"

        # If no dependencies declared and no lockfile, skip npm install
        if not deps and not dev_deps and not lock_path.exists():
            return True, None

        # Prepare npm execution environment
        npm_env = os.environ.copy()
        npm_env["NODE_ENV"] = "production"
        npm_env["npm_config_audit"] = "false"
        npm_env["npm_config_fund"] = "false"
        npm_env["npm_config_update_notifier"] = "false"
        npm_env["npm_config_loglevel"] = "error"

        # Determine install command (npm ci preferred when lockfile present)
        use_ci = lock_path.exists() and lock_path.is_file()

        if use_ci:
            # Validate lockfile integrity
            try:
                json.loads(lock_path.read_text(encoding="utf-8"))
            except Exception as e:
                return False, f"Invalid package-lock.json: {e}"

            install_cmd = [npm_bin, "ci", "--omit=dev", "--no-audit", "--no-fund"]
        else:
            install_cmd = [npm_bin, "install", "--omit=dev", "--no-audit", "--no-fund"]

        node_modules_dir = release_dir / "node_modules"

        try:
            res = subprocess.run(
                install_cmd,
                cwd=str(release_dir),
                env=npm_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_sec
            )

            # If npm ci failed due to mismatch with package.json, fallback to npm install
            if res.returncode != 0 and use_ci:
                fallback_cmd = [npm_bin, "install", "--omit=dev", "--no-audit", "--no-fund"]
                res = subprocess.run(
                    fallback_cmd,
                    cwd=str(release_dir),
                    env=npm_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_sec
                )

            if res.returncode != 0:
                # Clean up incomplete node_modules
                if node_modules_dir.exists():
                    shutil.rmtree(str(node_modules_dir), ignore_errors=True)

                err_summary = res.stderr.strip() or res.stdout.strip()
                safe_err = "\n".join(
                    line for line in err_summary.splitlines()[-10:]
                    if not any(k in line.lower() for k in ("token", "password", "secret", "key"))
                )
                return False, f"Dependency installation failed (exit code {res.returncode}):\n{safe_err}"

            return True, None

        except subprocess.TimeoutExpired:
            if node_modules_dir.exists():
                shutil.rmtree(str(node_modules_dir), ignore_errors=True)
            return False, f"Dependency installation timed out after {timeout_sec}s."
        except Exception as e:
            if node_modules_dir.exists():
                shutil.rmtree(str(node_modules_dir), ignore_errors=True)
            return False, f"Failed executing dependency installer: {e}"

    def start_application(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        port: int,
        env_vars: Dict[str, str],
        start_cmd: Optional[List[str]] = None,
        log_file: Optional[Path] = None
    ) -> ProcessIdentityRecord:
        """
        Starts the Node.js application process with isolated environment variables
        and logging via SafeProcessLifecycleManager.
        """
        release_dir = Path(release_dir).resolve()
        node_bin, npm_bin = self.get_binaries()
        target_node = node_bin or "node"

        # 1. Determine execution command
        if start_cmd and len(start_cmd) > 0:
            cmd = list(start_cmd)
            first_tok = os.path.basename(cmd[0]).lower()
            if first_tok in ("node", "nodejs", "node.exe"):
                cmd[0] = target_node
        else:
            cmd_tokens, err = self.detect_entry_point(release_dir)
            if not cmd_tokens:
                raise RuntimeError(err or "Unable to determine Node.js startup command.")
            cmd = cmd_tokens

        # 2. Build isolated process environment
        proc_env = os.environ.copy()
        proc_env["PORT"] = str(port)
        proc_env["HOST"] = "0.0.0.0"
        proc_env["APPLICATION_ID"] = app_id
        proc_env["DEPLOYMENT_ID"] = deployment_id
        proc_env["NODE_ENV"] = "production"

        # Prepend release node_modules/.bin to PATH
        node_bin_dir = release_dir / "node_modules" / ".bin"
        if node_bin_dir.exists():
            proc_env["PATH"] = f"{node_bin_dir}{os.pathsep}{proc_env.get('PATH', '')}"

        # Inject application environment variables
        for k, v in env_vars.items():
            proc_env[str(k)] = str(v)

        # 3. Dedicated application log file
        app_logs_dir = LOGS_DIR / "apps"
        app_logs_dir.mkdir(parents=True, exist_ok=True)
        target_log = log_file or (app_logs_dir / f"{app_id}.log")

        # 4. Start via SafeProcessLifecycleManager
        return SafeProcessLifecycleManager.start_process(
            app_id=app_id,
            deployment_id=deployment_id,
            cmd=cmd,
            cwd=release_dir,
            port=port,
            env=proc_env,
            log_file=target_log
        )

    def cleanup_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path
    ) -> None:
        """Stops the application process and cleans up temporary files on failure."""
        try:
            SafeProcessLifecycleManager.stop_process(app_id, timeout_sec=2.0)
        except Exception:
            pass

        # Clean up failed node_modules if needed
        nm_dir = Path(release_dir) / "node_modules"
        if nm_dir.exists():
            try:
                shutil.rmtree(str(nm_dir), ignore_errors=True)
            except Exception:
                pass
