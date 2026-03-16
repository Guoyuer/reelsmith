#!/usr/bin/env bash
# Start all services for the vlog pipeline:
#   1. Ollama LLM server
#   2. Synology Photos API (localhost:8000)
#   3. Dagster webserver + daemon (localhost:3000)
#
# Usage: ./start.sh        (start all)
#        ./start.sh stop   (stop all)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNOLOGY_DIR="$SCRIPT_DIR/../synology-photos-project"
VLOG_DIR="$SCRIPT_DIR"
DAGSTER_PORT="${DAGSTER_PORT:-3000}"
API_PORT="${API_PORT:-8000}"

stop_all() {
    echo "Stopping services..."
    # Kill by port — reliable regardless of PID files
    lsof -ti :"$DAGSTER_PORT" 2>/dev/null | xargs kill -9 2>/dev/null
    lsof -ti :"$API_PORT" 2>/dev/null | xargs kill -9 2>/dev/null
    # Kill any remaining dagster subprocesses (daemon, grpc, code-server)
    pkill -9 -f "dagster" 2>/dev/null
    sleep 1
    echo "All services stopped."
}

if [ "${1:-}" = "stop" ]; then
    stop_all
    exit 0
fi

# Stop any existing instances first
stop_all 2>/dev/null

echo "=== Starting Vlog Pipeline Services ==="
echo ""

# 1. Ollama
echo "[1/3] Ensuring Ollama is running..."
if ! pgrep -x "ollama" > /dev/null 2>&1; then
    ollama serve > /dev/null 2>&1 &
    for i in $(seq 1 15); do
        curl -s -o /dev/null http://localhost:11434/api/tags 2>/dev/null && break
        sleep 1
    done
    echo "  Ollama started"
else
    echo "  Ollama already running"
fi

# Ensure required models are pulled
for model in llava:7b llama3:8b; do
    if ! ollama list 2>/dev/null | grep -q "$model"; then
        echo "  Pulling $model..."
        ollama pull "$model"
    fi
done

# 2. Synology Photos API
echo "[2/3] Starting Synology Photos API on :$API_PORT..."
(cd "$SYNOLOGY_DIR" && source venv/bin/activate && uvicorn web.api.main:app --host 0.0.0.0 --port "$API_PORT" &) 2>/dev/null

# 3. Dagster (webserver + daemon)
echo "[3/3] Starting Dagster on :$DAGSTER_PORT..."
export DAGSTER_HOME="$VLOG_DIR/.dagster_home"
mkdir -p "$DAGSTER_HOME"
(cd "$VLOG_DIR" && source venv/bin/activate && dagster dev -m pipeline.definitions -p "$DAGSTER_PORT" &) 2>/dev/null

# Wait for services to be ready
echo ""
echo "Waiting for services..."
for i in $(seq 1 30); do
    api_ok=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$API_PORT/docs" 2>/dev/null || echo "000")
    dag_ok=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$DAGSTER_PORT" 2>/dev/null || echo "000")
    [ "$api_ok" = "200" ] && [ "$dag_ok" = "200" ] && break
    sleep 1
done

echo ""
echo "=== Services Ready ==="
echo "  Ollama:               http://localhost:11434"
echo "  Synology Photos API:  http://localhost:$API_PORT"
echo "  Dagster UI:           http://localhost:$DAGSTER_PORT"
echo ""
echo "Run pipeline:"
echo "  python run.py -n singapore full -f 2025-06-13 -t 2025-06-17 --duration 60"
echo ""
echo "Stop all:  ./start.sh stop"
