#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups
output="backups/gateway-$(date -u +%Y%m%dT%H%M%SZ).dump"
trap 'rm -f "$output"' ERR
docker compose exec -T postgres pg_dump -U gateway -d gateway -Fc > "$output"
printf 'Database backup: %s\nBack up MASTER_KEY separately; it is not in this file.\n' "$output"
