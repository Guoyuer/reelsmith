#!/usr/bin/env python3
"""One-click setup & launcher for the vlog pipeline.

Usage:
    python start.py              # first run: install deps, configure, start services
    python start.py              # subsequent runs: just start services
    python start.py stop         # stop all services
    python start.py setup        # re-run setup only (no start)

On first run, this script will:
    1. Create Python venvs and install dependencies (vlog + synology-photos-project)
    2. Check for FFmpeg and Ollama, with install instructions if missing
    3. Walk you through .env configuration (NAS credentials, API keys)
    4. Pull required Ollama models
    5. Start all services (Ollama, Synology API, Dagster)
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
# Add common tool locations so shutil.which() finds them.
if sys.platform == "win32":
    _extra_paths = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama"),
    ]
    for p in _extra_paths:
        if os.path.isdir(p) and p not in os.environ.get("PATH", ""):
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _find_python(venv_dir: Path) -> str:
    """Find the python executable inside a venv (cross-platform)."""
    if sys.platform == "win32":
        python = venv_dir / "Scripts" / "python.exe"
    else:
        python = venv_dir / "bin" / "python"
    return str(python) if python.exists() else sys.executable


def _find_pip(venv_dir: Path) -> str:
    """Find the pip executable inside a venv (cross-platform)."""
    if sys.platform == "win32":
        pip = venv_dir / "Scripts" / "pip.exe"
    else:
        pip = venv_dir / "bin" / "pip"
    return str(pip) if pip.exists() else f"{_find_python(venv_dir)} -m pip"


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
            # /T kills the process tree (important for dagster which spawns children)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
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
    pid_file = PID_DIR / f"{name}.pid"
    pid_file.unlink(missing_ok=True)


def _popen_detached(cmd: list[str], **kwargs) -> subprocess.Popen:
    """Start a detached background process that survives parent exit.

    On Windows, uses CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW to properly
    daemonize. On Unix, uses start_new_session=True.
    """
    if sys.platform == "win32":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    kwargs.setdefault("stdout", subprocess.DEVNULL)
    kwargs.setdefault("stderr", subprocess.DEVNULL)
    return subprocess.Popen(cmd, **kwargs)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, printing it first for visibility."""
    print(f"  $ {' '.join(cmd[:4])}{'...' if len(cmd) > 4 else ''}")
    return subprocess.run(cmd, **kwargs)


def _ask(prompt: str, default: str = "") -> str:
    """Prompt user for input with an optional default."""
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer or default


# ---------------------------------------------------------------------------
# Setup: venvs, deps, .env, external tools
# ---------------------------------------------------------------------------

def _setup_venv(project_dir: Path, extras: str = "") -> None:
    """Create venv and install deps for a project if not already done."""
    venv_dir = project_dir / "venv"
    python = _find_python(venv_dir)

    if not venv_dir.exists():
        print(f"  Creating venv in {venv_dir}...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        # Ensure pip is available
        subprocess.run([_find_python(venv_dir), "-m", "ensurepip"], capture_output=True)

    # Install/upgrade deps
    pip_python = _find_python(venv_dir)
    req_file = project_dir / "requirements.txt"
    pyproject = project_dir / "pyproject.toml"

    if pyproject.exists():
        install_arg = f"-e .[{extras}]" if extras else "-e ."
        _run([pip_python, "-m", "pip", "install", install_arg],
             cwd=str(project_dir), capture_output=True)
    elif req_file.exists():
        _run([pip_python, "-m", "pip", "install", "-r", "requirements.txt"],
             cwd=str(project_dir), capture_output=True)


def _setup_synology_deps() -> None:
    """Install extra deps needed by the synology API server."""
    if not SYNOLOGY_DIR.exists():
        return
    pip_python = _find_python(SYNOLOGY_DIR / "venv")
    # These are imported by main.py but not in requirements.txt
    _run([pip_python, "-m", "pip", "install",
          "fastapi", "uvicorn", "psycopg2-binary", "orjson"],
         capture_output=True)


def _setup_env_file(filepath: Path, fields: list[tuple[str, str, str]]) -> None:
    """Interactively create a .env file if it doesn't exist.

    fields: list of (KEY, description, default_value)
    """
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


def setup() -> bool:
    """Run full setup. Returns True if setup succeeded."""
    print()
    print("=" * 60)
    print("  Vlog Pipeline — First-Time Setup")
    print("=" * 60)

    # --- Python venvs ---
    print("\n[1/5] Setting up Python environments...")
    _setup_venv(SCRIPT_DIR, extras="test")
    if SYNOLOGY_DIR.exists():
        _setup_venv(SYNOLOGY_DIR)
        _setup_synology_deps()
    else:
        print(f"  WARNING: synology-photos-project not found at {SYNOLOGY_DIR}")
        print(f"  Clone it:  git clone <repo-url> {SYNOLOGY_DIR}")

    # --- FFmpeg ---
    print("\n[2/5] Checking FFmpeg...")
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if ffprobe and ffmpeg:
        print(f"  FFmpeg found: {ffmpeg}")
    else:
        print("  FFmpeg/ffprobe NOT found on PATH!")
        if sys.platform == "win32":
            print("  Install: winget install Gyan.FFmpeg")
            print("  Then restart this terminal (or add to PATH).")
        elif sys.platform == "darwin":
            print("  Install: brew install ffmpeg")
        else:
            print("  Install: sudo apt install ffmpeg  (or your package manager)")
        print()
        ans = _ask("Continue without FFmpeg? Video rendering will fail (y/N)", "n")
        if ans.lower() != "y":
            return False

    # --- Ollama ---
    print("\n[3/5] Checking Ollama...")
    ollama = shutil.which("ollama")
    if ollama:
        print(f"  Ollama found: {ollama}")
    else:
        # Check common install locations on Windows
        if sys.platform == "win32":
            for candidate in [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
                Path("C:/Program Files/Ollama/ollama.exe"),
            ]:
                if candidate.exists():
                    ollama = str(candidate)
                    print(f"  Ollama found: {ollama}")
                    break

    if not ollama:
        print("  Ollama NOT found!")
        if sys.platform == "win32":
            print("  Install: winget install Ollama.Ollama")
        elif sys.platform == "darwin":
            print("  Install: brew install ollama")
        else:
            print("  Install: curl -fsSL https://ollama.com/install.sh | sh")
        print("  Then restart this terminal.")
        print()
        ans = _ask("Continue without Ollama? Vision analysis will be skipped (y/N)", "n")
        if ans.lower() != "y":
            return False

    # --- .env files ---
    print("\n[4/5] Configuring environment...")

    _setup_env_file(SCRIPT_DIR / ".env", [
        ("SYNOLOGY_API_BASE", "Synology Photos API URL", "http://localhost:8000"),
        ("OLLAMA_BASE", "Ollama server URL", "http://localhost:11434"),
        ("VISION_MODEL", "Vision model for photo analysis", "llava:7b"),
        ("PLANNING_MODEL", "Planning model", "qwen2.5-coder:7b"),
        ("WHISPER_MODEL", "Whisper model size (tiny/base/small/medium)", "small"),
        ("WORKSPACE", "Workspace directory", "./workspace"),
        ("ANTHROPIC_API_KEY", "Anthropic API key (for Claude planner, optional)", ""),
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

    # --- Ollama models ---
    print("\n[5/5] Pulling Ollama models (if needed)...")
    ollama_cmd = ollama or "ollama"
    try:
        check = subprocess.run([ollama_cmd, "list"], capture_output=True, text=True)
        models_output = check.stdout or ""
        for model in ["llava:7b", "llama3:8b"]:
            if model not in models_output:
                print(f"  Pulling {model} (this may take a while)...")
                subprocess.run([ollama_cmd, "pull", model])
            else:
                print(f"  {model} already available")
    except FileNotFoundError:
        print("  Skipped (Ollama not installed)")

    print()
    print("Setup complete!")
    return True


# ---------------------------------------------------------------------------
# Start / Stop services
# ---------------------------------------------------------------------------

def stop_all() -> None:
    """Stop all running services."""
    print("Stopping services...")
    for name in ["dagster", "synology_api", "ollama"]:
        _kill_pid(name)
    if PID_DIR.exists():
        for f in PID_DIR.glob("*.pid"):
            f.unlink(missing_ok=True)
    print("All services stopped.")


def start_all() -> None:
    """Start all services (Ollama, Synology API, Dagster)."""
    # Check if first-time setup is needed
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

    # 1. Ollama
    print("[1/3] Ensuring Ollama is running...")
    ollama_cmd = shutil.which("ollama")
    if not ollama_cmd and sys.platform == "win32":
        candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.exists():
            ollama_cmd = str(candidate)

    if not _is_port_open(11434):
        if ollama_cmd:
            try:
                proc = _popen_detached([ollama_cmd, "serve"])
                _write_pid("ollama", proc.pid)
                for _ in range(15):
                    if _is_port_open(11434):
                        break
                    time.sleep(1)
                print("  Ollama started")
            except FileNotFoundError:
                print("  WARNING: Could not start Ollama")
        else:
            print("  WARNING: Ollama not found. Vision analysis will not work.")
    else:
        print("  Ollama already running")

    # 2. Synology Photos API
    print(f"[2/3] Starting Synology Photos API on :{API_PORT}...")
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

    # 3. Dagster (webserver + daemon + code server)
    print(f"[3/3] Starting Dagster on :{DAGSTER_PORT}...")
    dagster_home = SCRIPT_DIR / ".dagster_home"
    dagster_home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DAGSTER_HOME": str(dagster_home)}
    vlog_python = _find_python(SCRIPT_DIR / "venv")

    # Write a dagster.yaml if missing (avoids warnings)
    dagster_yaml = dagster_home / "dagster.yaml"
    if not dagster_yaml.exists():
        dagster_yaml.write_text("")

    # Use dagster dev with stderr/stdout to a log file for debugging
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
    print(f"  Ollama:               http://localhost:11434")
    print(f"  Synology Photos API:  http://localhost:{API_PORT}")
    print(f"  Dagster UI:           http://localhost:{DAGSTER_PORT}")
    print()
    print("Run pipeline:")
    print("  python run.py -n mytrip full -f 2025-06-13 -t 2025-06-17 --duration 60")
    print()
    print("Stop all:  python start.py stop")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "stop":
        stop_all()
    elif cmd == "setup":
        setup()
    else:
        start_all()
