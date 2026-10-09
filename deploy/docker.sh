#!/bin/sh
# Run iot-bot v2 in Docker on the Pi. Usage: deploy/docker.sh build|ship|check|discover|start|stop|restart|logs
# Data dir (IOTBOT_DATA, default ./data) holds .env, devices.json, users.json, state/ and logs/.
set -eu
cd "$(dirname "$0")/.."
NAME=iotbot
IMAGE=iotbot:latest
# Docker 20.10.7 blocks clone3 with EPERM, so glibc >= 2.34 images cannot start threads.
# This profile is moby's v20.10.7 default plus clone3 -> ENOSYS (via TRACE with no tracer).
SECCOMP="$PWD/deploy/seccomp-clone3.json"

data_dir() {
    # Absolute path, or Docker treats it as a named volume; must exist, or dockerd creates it as root
    DATA=$(cd "${IOTBOT_DATA:-data}" 2>/dev/null && pwd) || {
        echo "data dir '${IOTBOT_DATA:-data}' not found; create it with .env, devices.json, users.json" >&2
        exit 2
    }
    [ -f "$DATA/.env" ] || { echo "no $DATA/.env (copy .env.example, chmod 600)" >&2; exit 2; }
}

run() {
    data_dir
    # Host network: Broadlink discovery is a UDP broadcast on the LAN.
    # Read-only root, data on a bind mount, host clock zone for log timestamps.
    docker run --network host \
        --security-opt "seccomp=$SECCOMP" \
        --read-only --tmpfs /tmp \
        -v "$DATA:/data" \
        -v /etc/localtime:/etc/localtime:ro \
        "$@"
}

case "${1:-}" in
    build)
        REV=$(git rev-parse --short HEAD 2>/dev/null || echo local)
        docker build -t "$IMAGE" -t "iotbot:$REV" .
        ;;
    ship)
        # Run on the PC, not the Pi: Docker 20.10.7 cannot apply the seccomp profile to build steps,
        # so builds fail there. Needs qemu-user-static-arm on the PC.
        # Usage: deploy/docker.sh ship johnson@johrasp.lan
        HOST="${2:?usage: $0 ship user@host}"
        REV=$(git rev-parse --short HEAD 2>/dev/null || echo local)
        docker buildx build --platform linux/arm/v7 -t "$IMAGE" -t "iotbot:$REV" --load .
        docker save "$IMAGE" "iotbot:$REV" | gzip -1 | ssh "$HOST" "gunzip | docker load"
        ;;
    check)
        run --rm "$IMAGE" --check
        ;;
    discover)
        run --rm "$IMAGE" --discover
        ;;
    start)
        if docker container inspect "$NAME" >/dev/null 2>&1; then
            echo "$NAME already exists; use: $0 restart" >&2
            exit 1
        fi
        # Docker has no RestartPreventExitStatus: refuse to start on a config/data error (exit 2/3)
        # instead of retrying once a minute forever
        run --rm "$IMAGE" --check
        # Restarts on crash and at boot. Logs capped at 3 x 10 MB.
        run -d --name "$NAME" --restart unless-stopped \
            --log-opt max-size=10m --log-opt max-file=3 \
            "$IMAGE"
        ;;
    stop)
        # Removes the container and its docker logs (the bot's own JSONL logs stay in data/logs)
        docker rm -f "$NAME"
        ;;
    restart)
        docker rm -f "$NAME" >/dev/null 2>&1 || true
        "$0" start
        ;;
    logs)
        docker logs -f --tail 100 "$NAME"
        ;;
    *)
        echo "usage: $0 build|ship|check|discover|start|stop|restart|logs" >&2
        exit 64
        ;;
esac
