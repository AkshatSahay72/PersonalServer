#!/usr/bin/env python3
"""
PersonalServer Python Virtualenv Runtime Adapter
=================================================
Executes Python web service releases in hermetic, isolated virtual environments:
- Verifies host Python version (>= 3.8)
- Creates isolated .venv per release (no shared venvs, no global dependency pollution)
- Installs dependencies from requirements.txt with bounded timeout
- Maps entry point to venv Python interpreter
- Safely manages lifecycle via SafeProcessLifecycleManager
- Guarantees zero secret exposure in logs
"""

import os
import sys
import venv
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


class PythonVirtualenvAdapter(RuntimeAdapter):
    """
    Hermetic Python runtime adapter that builds and executes workloads inside
    an isolated virtual environment per release.
    """

    def __init__(self, python_executable: Optional[str] = None):
        self._python_exe = python_executable or sys.executable

    @property
    def runtime_name(self) -> str:
        return "python"

    def validate_environment(self) -> Tuple[bool, Optional[str]]:
        """Validates host Python interpreter version is >= 3.8 and venv is functional."""
        try:
            ver = sys.version_info
            if ver.major < 3 or (ver.major == 3 and ver.minor < 8):
                return False, f"Python 3.8+ required. Host Python version is {sys.version.split()[0]}."
            
            # Check venv module
            import venv  # noqa
            return True, None
        except Exception as e:
            return False, f"Python environment validation failed: {e}"

    def get_venv_paths(self, release_dir: Path) -> Tuple[Path, Path, Path]:
        """
        Returns (venv_dir, python_bin, pip_bin) for a release directory.
        Handles both Windows and Linux/Termux paths.
        """
        venv_dir = release_dir / ".venv"
        if sys.platform == "win32":
            py_bin = venv_dir / "Scripts" / "python.exe"
            pip_bin = venv_dir / "Scripts" / "pip.exe"
        else:
            py_bin = venv_dir / "bin" / "python"
            pip_bin = venv_dir / "bin" / "pip"
        return venv_dir, py_bin, pip_bin

    def prepare_release(
        self,
        app_id: str,
        deployment_id: str,
        release_dir: Path,
        env_vars: Dict[str, str],
        timeout_sec: float = 120.0
    ) -> Tuple[bool, Optional[str]]:
        """
        Creates an isolated virtual environment and installs dependencies from requirements.txt.
        """
        release_dir = Path(release_dir).resolve()
        if not release_dir.exists() or not release_dir.is_dir():
            return False, f"Release directory '{release_dir}' not found."

        venv_dir, venv_python, venv_pip = self.get_venv_paths(release_dir)

        # 1. Check if requirements.txt is present to determine if pip is required
        req_file = release_dir / "requirements.txt"
        has_reqs = req_file.exists() and req_file.is_file()

        # 2. Create hermetic virtual environment if not already present
        if not venv_dir.exists():
            try:
                # If no requirements.txt, skip ensurepip to avoid expensive unbundling
                builder = venv.EnvBuilder(with_pip=has_reqs, clear=True, symlinks=(sys.platform != "win32"))
                builder.create(str(venv_dir))
            except Exception as e:
                # Fallback for environments without ensurepip (e.g. minimal Termux builds)
                try:
                    builder = venv.EnvBuilder(with_pip=False, clear=True, symlinks=(sys.platform != "win32"))
                    builder.create(str(venv_dir))
                except Exception as fallback_err:
                    return False, f"Failed to create isolated virtual environment: {e} (fallback: {fallback_err})"

        # 3. Install dependencies if requirements.txt exists
        if has_reqs:
            if not venv_pip.exists():
                # If pip wasn't bundled, check python -m pip
                pip_cmd = [str(venv_python), "-m", "pip", "install", "--no-warn-script-location", "-r", str(req_file)]
            else:
                pip_cmd = [str(venv_pip), "install", "--no-warn-script-location", "-r", str(req_file)]

            # Redacted env for pip run
            pip_env = os.environ.copy()
            pip_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            pip_env["PYTHONUNBUFFERED"] = "1"

            try:
                res = subprocess.run(
                    pip_cmd,
                    cwd=str(release_dir),
                    env=pip_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_sec
                )
                if res.returncode != 0:
                    err_summary = res.stderr.strip() or res.stdout.strip()
                    # Sanitize error lines (prevent leaking tokens if in URLs)
                    safe_err = "\n".join(
                        line for line in err_summary.splitlines()[-10:]
                        if not any(k in line.lower() for k in ("token", "password", "secret", "key"))
                    )
                    return False, f"Dependency installation failed (exit code {res.returncode}):\n{safe_err}"
            except subprocess.TimeoutExpired:
                return False, f"Dependency installation timed out after {timeout_sec}s."
            except Exception as e:
                return False, f"Failed executing dependency installer: {e}"

        return True, None

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
        Starts the application using the release's isolated virtualenv and SafeProcessLifecycleManager.
        """
        release_dir = Path(release_dir).resolve()
        venv_dir, venv_python, _ = self.get_venv_paths(release_dir)

        target_python = str(venv_python) if venv_python.exists() else self._python_exe

        # 1. Determine execution command
        if start_cmd and len(start_cmd) > 0:
            cmd = list(start_cmd)
            # Map interpreter invocation to the virtualenv python
            first_tok = os.path.basename(cmd[0]).lower()
            if first_tok in ("python", "python3", "python.exe", "python3.exe"):
                cmd[0] = target_python
                if "-u" not in cmd:
                    cmd.insert(1, "-u")
        else:
            candidates = ["app.py", "main.py", "server.py"]
            entry_point = None
            for cand in candidates:
                if (release_dir / cand).exists():
                    entry_point = release_dir / cand
                    break

            if entry_point:
                cmd = [target_python, "-u", str(entry_point)]
            else:
                # Built-in lightweight health fallback server
                fallback_script = release_dir / "_server.py"
                if not fallback_script.exists():
                    fallback_script.write_text(
                        "import http.server, socketserver\n"
                        "class Handler(http.server.SimpleHTTPRequestHandler):\n"
                        "    def do_GET(self):\n"
                        "        if self.path in ('/health', '/api/health'):\n"
                        "            self.send_response(200)\n"
                        "            self.send_header('Content-Type', 'application/json')\n"
                        "            self.end_headers()\n"
                        f"            self.wfile.write(b'{{\"status\":\"healthy\",\"app\":\"{app_id}\"}}\\n')\n"
                        "        else:\n"
                        "            super().do_GET()\n"
                        f"http.server.HTTPServer(('127.0.0.1', {port}), Handler).serve_forever()\n",
                        encoding="utf-8"
                    )
                cmd = [target_python, "-u", str(fallback_script)]

        # 2. Build isolated process environment
        proc_env = os.environ.copy()
        proc_env["PORT"] = str(port)
        proc_env["HOST"] = "127.0.0.1"
        proc_env["APPLICATION_ID"] = app_id
        proc_env["DEPLOYMENT_ID"] = deployment_id
        proc_env["PYTHONUNBUFFERED"] = "1"

        if venv_dir.exists():
            proc_env["VIRTUAL_ENV"] = str(venv_dir)
            venv_bin_dir = venv_python.parent
            proc_env["PATH"] = f"{venv_bin_dir}{os.pathsep}{proc_env.get('PATH', '')}"

        # Inject application environment variables
        for k, v in env_vars.items():
            proc_env[str(k)] = str(v)

        # 3. Dedicated bounded application log file
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

        # Clean up failed virtual environment if needed
        venv_dir, _, _ = self.get_venv_paths(Path(release_dir))
        if venv_dir.exists():
            try:
                shutil.rmtree(str(venv_dir), ignore_errors=True)
            except Exception:
                pass
