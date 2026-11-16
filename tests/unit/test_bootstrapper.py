"""
Unit tests for PersonalServer Universal Node Bootstrapper (bootstrap/*.sh)
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_DIR = ROOT / "bootstrap"


def _get_bash():
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if git_bash.exists():
        return str(git_bash)
    bash = shutil.which("bash")
    if bash and not bash.lower().endswith(r"system32\bash.exe"):
        return bash
    return None


@pytest.fixture
def temp_node_dir(tmp_path):
    """Creates a temporary workspace mimicking PersonalServer base dir."""
    node_dir = tmp_path / "PersonalServer"
    node_dir.mkdir(parents=True, exist_ok=True)
    return node_dir


def test_detect_script_execution():
    """Verify detect.sh executes cleanly and outputs expected environment variables."""
    detect_sh = BOOTSTRAP_DIR / "detect.sh"
    assert detect_sh.exists(), "bootstrap/detect.sh must exist"

    bash = _get_bash()
    if not bash:
        pytest.skip("Git bash not available")

    res = subprocess.run([bash, str(detect_sh).replace("\\", "/")], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, f"detect.sh failed: {res.stderr}"
    stdout = res.stdout
    assert "OS:" in stdout
    assert "Platform:" in stdout
    assert "Architecture:" in stdout
    assert "CPU Cores:" in stdout


def test_configure_generates_valid_node_identity(temp_node_dir):
    """Verify configure.sh generates valid config/node.json matching schema."""
    configure_sh = BOOTSTRAP_DIR / "configure.sh"
    bash = _get_bash()
    if not bash:
        pytest.skip("Git bash not available in PATH")

    env = os.environ.copy()
    env["PERSONALSERVER_BASE_DIR"] = str(temp_node_dir).replace("\\", "/")
    env["PLATFORM"] = "linux"
    env["OS"] = "Linux"
    env["ARCH"] = "x86_64"
    env["CPU_CORES"] = "4"
    env["RAM_MB"] = "8192"
    env["STORAGE_GB"] = "100"
    env["PYTHON_CMD"] = "python"

    res = subprocess.run(
        [bash, str(configure_sh).replace("\\", "/"), "--name", "test-node", "--role", "worker"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, f"configure.sh failed: {res.stderr}"

    config_file = temp_node_dir / "config" / "node.json"
    assert config_file.exists(), "config/node.json must be generated"

    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["name"] == "test-node"
    assert data["role"] == "worker"
    assert "server-linux-" in data["node_id"]
    assert data["cpu_cores"] == 4
    assert data["ram_mb"] == 8192
    assert data["capabilities"]["python_runtime"] is True


def test_configure_preserves_existing_identity(temp_node_dir):
    """Verify configure.sh does NOT overwrite existing node identity on re-run."""
    config_dir = temp_node_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "node.json"

    initial_config = {
        "node_id": "server-original-12345",
        "name": "original-device",
        "role": "worker",
        "platform": "debian",
    }
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(initial_config, f)

    configure_sh = BOOTSTRAP_DIR / "configure.sh"
    bash = _get_bash()
    if not bash:
        pytest.skip("Git bash not available in PATH")

    env = os.environ.copy()
    env["PERSONALSERVER_BASE_DIR"] = str(temp_node_dir).replace("\\", "/")
    env["PYTHON_CMD"] = "python"

    res = subprocess.run([bash, str(configure_sh).replace("\\", "/")], env=env, capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, f"configure.sh failed: {res.stderr}"

    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Must retain original node_id!
    assert data["node_id"] == "server-original-12345"


def test_configure_saves_credentials_securely(temp_node_dir):
    """Verify controller url and enrollment token are saved securely."""
    configure_sh = BOOTSTRAP_DIR / "configure.sh"
    bash = _get_bash()
    if not bash:
        pytest.skip("Git bash not available in PATH")

    env = os.environ.copy()
    env["PERSONALSERVER_BASE_DIR"] = str(temp_node_dir).replace("\\", "/")
    env["PYTHON_CMD"] = "python"

    res = subprocess.run(
        [
            bash,
            str(configure_sh).replace("\\", "/"),
            "--controller",
            "http://100.85.108.5:8000",
            "--token",
            "test-secret-token-xyz",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, f"configure.sh failed: {res.stderr}"

    node_conf = temp_node_dir / "config" / "node.conf"
    assert node_conf.exists()
    assert "CONTROLLER_URL=http://100.85.108.5:8000" in node_conf.read_text(encoding="utf-8")

    token_file = temp_node_dir / "config" / "secrets" / "enrollment.token"
    assert token_file.exists()
    assert token_file.read_text(encoding="utf-8") == "test-secret-token-xyz"


def test_install_script_dry_run(temp_node_dir):
    """Verify install.sh --dry-run prints plan without creating directories."""
    install_sh = BOOTSTRAP_DIR / "install.sh"
    bash = _get_bash()
    if not bash:
        pytest.skip("Git bash not available in PATH")

    env = os.environ.copy()
    env["PERSONALSERVER_BASE_DIR"] = str(temp_node_dir).replace("\\", "/")

    res = subprocess.run(
        [bash, str(install_sh).replace("\\", "/"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, f"install.sh --dry-run failed: {res.stderr}"
    assert "DRY RUN COMPLETE" in res.stdout
    assert not (temp_node_dir / "config" / "node.json").exists()


def test_uninstall_script_preserves_config_by_default(temp_node_dir):
    """Verify uninstall.sh preserves config unless --purge is passed."""
    uninstall_sh = BOOTSTRAP_DIR / "uninstall.sh"
    bash = _get_bash()
    if not bash:
        pytest.skip("Git bash not available in PATH")

    config_dir = temp_node_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "node.json").write_text("{}", encoding="utf-8")

    runtime_dir = temp_node_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "test.pid").write_text("12345", encoding="utf-8")

    env = os.environ.copy()
    env["PERSONALSERVER_BASE_DIR"] = str(temp_node_dir).replace("\\", "/")

    res = subprocess.run([bash, str(uninstall_sh).replace("\\", "/")], env=env, capture_output=True, text=True, timeout=10)
    assert res.returncode == 0
    assert (config_dir / "node.json").exists(), "Config must be preserved by default"
    assert not (runtime_dir / "test.pid").exists(), "Runtime PID file must be cleaned"

    config_dir = temp_node_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "node.json").write_text("{}", encoding="utf-8")

    runtime_dir = temp_node_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "test.pid").write_text("12345", encoding="utf-8")

    env = os.environ.copy()
    env["PERSONALSERVER_BASE_DIR"] = str(temp_node_dir)

    res = subprocess.run([bash, str(uninstall_sh)], env=env, capture_output=True, text=True, timeout=10)
    assert res.returncode == 0
    assert (config_dir / "node.json").exists(), "Config must be preserved by default"
    assert not (runtime_dir / "test.pid").exists(), "Runtime PID file must be cleaned"
