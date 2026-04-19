#!/bin/bash
# ChirpStack v4 bootstrap for Proxmox LXC (Debian 12).
set -euo pipefail
echo "=== ChirpStack v4 bootstrap ==="
apt-get update -qq
apt-get install -yq curl ca-certificates gnupg git

if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo ${VERSION_CODENAME}) stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
fi

INSTALL_DIR=/opt/chirpstack
mkdir -p $INSTALL_DIR && cd $INSTALL_DIR
[ ! -d chirpstack-docker ] && git clone --depth 1 https://github.com/chirpstack/chirpstack-docker.git
cd chirpstack-docker
docker compose pull
docker compose up -d

for i in {1..30}; do
    curl -sf http://localhost:8080/ >/dev/null 2>&1 && { echo "UI ready on :8080"; break; }
    sleep 2
done

IP=$(hostname -I | awk '{print $1}')
echo "=========================================="
echo "ChirpStack v4 ready"
echo "  UI:   http://${IP}:8080  (admin/admin -> CHANGE)"
echo "  MQTT: tcp://${IP}:1883"
echo "  UDP:  ${IP}:1700"
echo "=========================================="
