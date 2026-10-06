#!/usr/bin/env bash
# Build all images. The base image is built first because the box images use it as FROM.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -d data/polyglot-benchmark ] || uv run bench/prepare.py

docker build -f docker/Dockerfile.base --build-arg AGENT_UID="$(id -u)" -t shootout/base:latest .
docker build -f docker/Dockerfile.proxy -t shootout/proxy:latest .
docker build -f docker/Dockerfile.claude -t shootout/claude:latest .
docker build -f docker/Dockerfile.codex -t shootout/codex:latest .
