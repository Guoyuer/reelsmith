#!/usr/bin/env bash
# Start all services for the vlog pipeline:
#   1. Ollama LLM server
#   2. Synology Photos API (localhost:8000)
#   3. Dagster webserver + daemon (localhost:3000)
#
# Usage: ./start.sh
# Stop:  ./start.sh stop

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNOLOGY_DIR="$SCRIPT_DIR/../synology-photos-project"
VLOG_DIR="$SCRIPT_DIR"
DAGSTER_PORT="${DAGSTER_PORT:-3000}"
API_PORT="${API_PORT:-8000}"
PID_DIR="$VLOG_DIR/.pids"

mkdir -p "$PID_DIR"

stop_all() {
    echo "Stopping services..."
    for pidfile in "$PID_DIR"/*.pid; do
        [ -f "$pidfile" ] || continue
        pid=$(cat "$pidfile")
        name=$(basename "$pidfile" .pid)
        if kill -0 "$pid" 2>/dev/null; then
            echo "  Stopping $name (PID $pid)"
            kill "$pid" 2>/dev/null
            for i in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
    done
    echo "All services stopped."
}

if [ "${1:-}" = "stop" ]; then
    stop_all
    exit 0
fi

# Stop any existing instances first
stop_all 2>/dev/null || true

echo "=== Starting Vlog Pipeline Services ==="
echo ""

# 1. Ollama
echo "[1/3] Ensuring Ollama is running..."
if ! pgrep -x "ollama" > /dev/null 2>&1; then
    ollama serve > /dev/null 2>&1 &
    echo $! > "$PID_DIR/ollama.pid"
    # Wait for Ollama to be ready
    for i in $(seq 1 15); do
        if curl -s -o /dev/null http://localhost:11434/api/tags 2>/dev/null; then
            break
        fi
        sleep 1
    done
    echo "  Ollama started (PID $(cat "$PID_DIR/ollama.pid"))"
else
    echo "  Ollama already running"
fi

# Ensure required models are pulled
for model in llava:7b qwen2.5-coder:7b; do
    if ! ollama list 2>/dev/null | grep -q "$model"; then
        echo "  Pulling $model..."
        ollama pull "$model"
    fi
done

# 2. Synology Photos API
echo "[2/3] Starting Synology Photos API on :$API_PORT..."
cd "$SYNOLOGY_DIR"
source venv/bin/activate
uvicorn web.api.main:app --host 0.0.0.0 --port "$API_PORT" &
echo $! > "$PID_DIR/synology-api.pid"
deactivate 2>/dev/null || true

# 3. Dagster (webserver + daemon)
echo "[3/3] Starting Dagster on :$DAGSTER_PORT..."
cd "$VLOG_DIR"
source venv/bin/activate
export DAGSTER_HOME="$VLOG_DIR/.dagster_home"
mkdir -p "$DAGSTER_HOME"
dagster dev -m pipeline.definitions -p "$DAGSTER_PORT" &
echo $! > "$PID_DIR/dagster.pid"

# Wait for services to be ready
echo ""
echo "Waiting for services..."
for i in $(seq 1 30); do
    api_ok=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$API_PORT/docs" 2>/dev/null || echo "000")
    dag_ok=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$DAGSTER_PORT" 2>/dev/null || echo "000")
    if [ "$api_ok" = "200" ] && [ "$dag_ok" = "200" ]; then
        break
    fi
    sleep 1
done

echo ""
echo "=== Services Ready ==="
echo "  Ollama:               http://localhost:11434"
echo "  Synology Photos API:  http://localhost:$API_PORT"
echo "  Dagster UI:           http://localhost:$DAGSTER_PORT"
echo ""
echo "Run pipeline:"
echo "  python run.py -n singapore auto -f 2025-06-13 -t 2025-06-17 --style upbeat --duration 180"
echo ""
echo "Stop all:  ./start.sh stop"
