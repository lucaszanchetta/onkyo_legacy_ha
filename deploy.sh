#!/usr/bin/env bash

set -euo pipefail

ip="${1:-}"
remote_base="/root/homeassistant/custom_components"
remote_component="$remote_base/onkyo_legacy"
local_component="./custom_components/onkyo_legacy"

is_valid_ip() {
    local ip=$1
    local a b c d
    [[ $ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS='.' read -r a b c d <<< "$ip"
    (( a <= 255 && b <= 255 && c <= 255 && d <= 255 ))
}

if [ $# -ne 1 ]; then
    echo "Usage: $0 <ip>"
    exit 1
fi

if ! is_valid_ip "$ip"; then
    echo "Invalid IP: $ip"
    exit 1
fi

if [ ! -d "$local_component" ]; then
    echo "Local component folder not found: $local_component"
    exit 1
fi

echo "Ensuring remote custom_components exists..."
ssh "root@$ip" "mkdir -p '$remote_base'"

echo "Removing old remote component if present..."
ssh "root@$ip" "rm -rf '$remote_component'"

echo "Copying local component to remote host..."
scp -r "$local_component" "root@$ip:$remote_base/"

echo "Restarting Home Assistant Core..."
ssh "root@$ip" "ha core restart"

echo "Done."
