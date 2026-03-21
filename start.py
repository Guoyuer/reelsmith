#!/usr/bin/env python3
"""One-click setup & launcher for the vlog pipeline.

Usage:
    python start.py              # first run: install deps, configure, start services
    python start.py              # subsequent runs: just start services
    python start.py stop         # stop all services
    python start.py setup        # re-run setup only (no start)

On first run, this script will:
    1. Create Python venvs and install dependencies (vlog + synology-photos-project)
    2. Check for FFmpeg (with install instructions if missing)
    3. Walk you through .env configuration (NAS credentials, Gemini API key)
    4. Start all services (Synology API, Dagster)
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SYNOLOGY_DIR = SCRIPT_DIR.parent / "synology-photos-project"
DAGSTER_PORT = int(os.getenv("DAGSTER_PORT", "3000"))
API_PORT = int(os.getenv("API_PORT", "8000"))
PID_DIR = SCRIPT_DIR / ".pids"

# On Windows, winget installs to locations not always on PATH.
if sys.platform == "win32":
    _winget = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
    if os.path.isdir(_winget) and _winget not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _winget + os.pathsep + os.environ.get("PATH", "")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _find_python(venv_dir: Path) -> str:
    if sys.platform == "win32":
        python = venv_dir / "Scripts" / "python.exe"
    else:
        python = venv_dir / "bin" / "python"
    return str(python) if python.exists() else sys.executable


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("localhost", port)) == 0


def _write_pid(name: str, pid: int) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    (PID_DIR / f"{name}.pid").write_text(str(pid))


def _read_pid(name: str) -> int | None:
    pid_file = PID_DIR / f"{name}.pid"
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except ValueError:
            pass
    return None


def _kill_pid(name: str) -> None:
    pid = _read_pid(name)
    if pid is None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.3)
                except OSError:
                    break
    except (OSError, PermissionError):
        pass
    (PID_DIR / f"{name}.pid").unlink(missing_ok=True)


def _popen_detached(cmd: list[str], **kwargs) -> subprocess.Popen:
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    kwargs.setdefault("stdout", subprocess.DEVNULL)
    kwargs.setdefault("stderr", subprocess.DEVNULL)
    return subprocess.Popen(cmd, **kwargs)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd[:4])}{'...' if len(cmd) > 4 else ''}")
    return subprocess.run(cmd, **kwargs)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer or default


# ---------------------------------------------------------------------------
# Setup: venvs, deps, .env, external tools
# ---------------------------------------------------------------------------

def _setup_venv(project_dir: Path, extras: str = "") -> None:
    venv_dir = project_dir / "venv"
    if not venv_dir.exists():
        print(f"  Creating venv in {venv_dir}...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        subprocess.run([_find_python(venv_dir), "-m", "ensurepip"], capture_output=True)

    pip_python = _find_python(venv_dir)
    pyproject = project_dir / "pyproject.toml"
    req_file = project_dir / "requirements.txt"

    if pyproject.exists():
        install_arg = f"-e .[{extras}]" if extras else "-e ."
        _run([pip_python, "-m", "pip", "install", install_arg],
             cwd=str(project_dir), capture_output=True)
    elif req_file.exists():
        _run([pip_python, "-m", "pip", "install", "-r", "requirements.txt"],
             cwd=str(project_dir), capture_output=True)


def _setup_synology_deps() -> None:
    if not SYNOLOGY_DIR.exists():
        return
    pip_python = _find_python(SYNOLOGY_DIR / "venv")
    _run([pip_python, "-m", "pip", "install",
          "fastapi", "uvicorn", "psycopg2-binary", "orjson"],
         capture_output=True)


def _setup_env_file(filepath: Path, fields: list[tuple[str, str, str]]) -> None:
    if filepath.exists():
        print(f"  {filepath.name} already exists, skipping.")
        return
    print(f"\n  Creating {filepath}...")
    print(f"  (Press Enter to accept defaults shown in brackets)\n")
    lines = []
    for key, desc, default in fields:
        val = _ask(f"{desc} ({key})", default)
        lines.append(f"{key}={val}")
    filepath.write_text("\n".join(lines) + "\n")
    print(f"  Saved {filepath}")


def _auto_install(name: str, win_cmd: list[str], mac_cmd: list[str], linux_cmd: list[str]) -> bool:
    cmds = {
        "win32": (win_cmd, "winget"),
        "darwin": (mac_cmd, "brew"),
        "linux": (linux_cmd, "package manager"),
    }
    cmd, mgr = cmds.get(sys.platform, (linux_cmd, "package manager"))
    if not cmd:
        print(f"  No auto-install available for {sys.platform}.")
        return False
    ans = _ask(f"Install {name} automatically via {mgr}? (Y/n)", "y")
    if ans.lower() != "y" and ans != "":
        return False
    print(f"  Installing {name}...")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode == 0:
        print(f"  {name} installed successfully.")
        return True
    print(f"  Installation failed (exit code {result.returncode}).")
    return False


def setup() -> bool:
    """Run full setup. Returns True if setup succeeded."""
    print()
    print("=" * 60)
    print("  Vlog Pipeline — First-Time Setup")
    print("=" * 60)

    # --- Step 1: Clone synology-photos-project if missing ---
    print("\n[1/4] Checking synology-photos-project...")
    if SYNOLOGY_DIR.exists():
        print(f"  Found at {SYNOLOGY_DIR}")
    else:
        print(f"  Not found at {SYNOLOGY_DIR}")
        ans = _ask("Clone synology-photos-project from GitHub? (Y/n)", "y")
        if ans.lower() != "y" and ans != "":
            print("  Skipping. Fetch stage will not work without the Synology API.")
        else:
            repo_url = _ask("Git repo URL", "https://github.com/Guoyuer/synology-photos-project.git")
            subprocess.run(["git", "clone", repo_url, str(SYNOLOGY_DIR)], capture_output=False)

    # --- Step 2: FFmpeg ---
    print("\n[2/4] Checking FFmpeg...")
    if shutil.which("ffprobe") and shutil.which("ffmpeg"):
        print(f"  FFmpeg found: {shutil.which('ffmpeg')}")
    else:
        print("  FFmpeg NOT found.")
        installed = _auto_install(
            "FFmpeg",
            win_cmd=["winget", "install", "Gyan.FFmpeg", "--accept-source-agreements", "--accept-package-agreements"],
            mac_cmd=["brew", "install", "ffmpeg"],
            linux_cmd=["sudo", "apt", "install", "-y", "ffmpeg"],
        )
        if not installed:
            ans = _ask("Continue without FFmpeg? Video rendering will fail (y/N)", "n")
            if ans.lower() != "y":
                return False

    # --- Step 3: Python venvs ---
    print("\n[3/4] Setting up Python environments...")
    _setup_venv(SCRIPT_DIR, extras="test")
    if SYNOLOGY_DIR.exists():
        _setup_venv(SYNOLOGY_DIR)
        _setup_synology_deps()

    # --- Step 4: .env files ---
    print("\n[4/4] Configuring environment...")

    _setup_env_file(SCRIPT_DIR / ".env", [
        ("SYNOLOGY_API_BASE", "Synology Photos API URL", "http://localhost:8000"),
        ("WHISPER_MODEL", "Whisper model size (tiny/base/small/medium)", "small"),
        ("WORKSPACE", "Workspace directory", "./workspace"),
        ("GEMINI_API_KEY", "Gemini API key (get from ai.google.dev)", ""),
    ])

    if SYNOLOGY_DIR.exists():
        _setup_env_file(SYNOLOGY_DIR / ".env", [
            ("NAS_IP", "Synology NAS IP address", "192.168.1.100"),
            ("NAS_PORT", "DSM port (5000=HTTP, 5001=HTTPS)", "5000"),
            ("NAS_USERNAME", "DSM username", ""),
            ("NAS_PASSWORD", "DSM password", ""),
            ("NAS_SECURE", "Use HTTPS?", "False"),
            ("NAS_CERT_VERIFY", "Verify SSL certificate?", "False"),
            ("NAS_DSM_VERSION", "DSM version", "7"),
            ("NAS_OTP_CODE", "2FA code (leave blank if not enabled)", ""),
        ])

    print()
    print("=" * 60)
    print("  Setup complete!")
    print("=" * 60)
    return True


# ---------------------------------------------------------------------------
# Start / Stop services
# ---------------------------------------------------------------------------

def _kill_port(port: int) -> None:
    """Kill any process listening on a port (Windows + Unix)."""
    if sys.platform == "win32":
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                   capture_output=True)
                except Exception:
                    pass
    else:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], capture_output=True, text=True,
        )
        for pid in result.stdout.strip().split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def stop_all() -> None:
    print("Stopping services...")
    for name in ["dagster", "synology_api"]:
        _kill_pid(name)
    # Also kill by port in case PID tracking missed child processes
    _kill_port(DAGSTER_PORT)
    _kill_port(API_PORT)
    if PID_DIR.exists():
        for f in PID_DIR.glob("*.pid"):
            f.unlink(missing_ok=True)
    print("All services stopped.")


def start_all() -> None:
    """Start all services (Synology API, Dagster)."""
    venv_dir = SCRIPT_DIR / "venv"
    env_file = SCRIPT_DIR / ".env"
    if not venv_dir.exists() or not env_file.exists():
        print("First-time setup detected.")
        if not setup():
            print("Setup incomplete. Fix the issues above and re-run.")
            sys.exit(1)

    stop_all()
    print()
    print("=== Starting Vlog Pipeline Services ===")
    print()

    # 1. Synology Photos API
    print(f"[1/2] Starting Synology Photos API on :{API_PORT}...")
    if SYNOLOGY_DIR.exists() and (SYNOLOGY_DIR / "venv").exists():
        api_python = _find_python(SYNOLOGY_DIR / "venv")
        proc = _popen_detached(
            [api_python, "-m", "uvicorn", "web.api.main:app",
             "--host", "0.0.0.0", "--port", str(API_PORT)],
            cwd=str(SYNOLOGY_DIR),
        )
        _write_pid("synology_api", proc.pid)
    else:
        print(f"  WARNING: Synology API not found or not set up at {SYNOLOGY_DIR}")

    # 2. Dagster
    print(f"[2/2] Starting Dagster on :{DAGSTER_PORT}...")
    dagster_home = SCRIPT_DIR / ".dagster_home"
    dagster_home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DAGSTER_HOME": str(dagster_home)}
    vlog_python = _find_python(SCRIPT_DIR / "venv")

    dagster_yaml = dagster_home / "dagster.yaml"
    if not dagster_yaml.exists():
        dagster_yaml.write_text("")

    dagster_log = dagster_home / "dagster_dev.log"
    dagster_log_fh = open(dagster_log, "w")
    proc = _popen_detached(
        [vlog_python, "-m", "dagster", "dev",
         "-m", "pipeline.definitions", "-p", str(DAGSTER_PORT)],
        cwd=str(SCRIPT_DIR),
        env=env,
        stdout=dagster_log_fh,
        stderr=dagster_log_fh,
    )
    _write_pid("dagster", proc.pid)

    # Wait for services
    print()
    print("Waiting for services...")
    for i in range(30):
        api_ok = _is_port_open(API_PORT)
        dag_ok = _is_port_open(DAGSTER_PORT)
        if api_ok and dag_ok:
            break
        time.sleep(1)

    print()
    print("=== Services Ready ===")
    print(f"  Synology Photos API:  http://localhost:{API_PORT}")
    print(f"  Dagster UI:           http://localhost:{DAGSTER_PORT}")
    print()
    print("Run pipeline:")
    print("  python run.py -n mytrip full -f 2025-06-13 -t 2025-06-17 --duration 180")
    print()
    print("Stop all:  python start.py stop")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "stop":
        stop_all()
    elif cmd == "setup":
        setup()
    else:
        start_all()
