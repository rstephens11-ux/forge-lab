#!/bin/bash
# Run ON the rig (the Linux box with ComfyUI). Puts Comfy's comfy-api-proxy (Comfy API v2)
# in front of each ComfyUI as a systemd user service, so Forge Lab can submit through the
# Comfy SDK:
#     ComfyUI :8188 (card 1)  <-  comfy-api-proxy :8288
#     ComfyUI :8189 (card 2)  <-  comfy-api-proxy :8289   (only if you run a second ComfyUI)
# Then copy ~/.comfy-proxy-token to the Mac as ~/forge-lab/comfy_proxy_token (chmod 600).
#   usage: bash rig_comfy_proxy.sh [COMFYUI_DIR=~/ComfyUI] [PORTS="8188 8189"]
set -e
COMFY_DIR="${1:-$HOME/ComfyUI}"
PORTS="${2:-8188 8189}"
[ -d ~/comfy-proxy-venv ] || python3 -m venv ~/comfy-proxy-venv
~/comfy-proxy-venv/bin/pip install -q -U comfy-api-proxy
[ -s ~/.comfy-proxy-token ] || (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > ~/.comfy-proxy-token)
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/comfy-api-proxy@.service <<UNIT
[Unit]
Description=Comfy API v2 proxy in front of the ComfyUI on port %i (Forge Lab)
After=network-online.target

[Service]
ExecStart=/bin/bash -c 'exec %h/comfy-proxy-venv/bin/comfy-api-proxy run --comfyui http://127.0.0.1:%i --host 0.0.0.0 --port \$\$((%i + 100)) --token "\$\$(cat %h/.comfy-proxy-token)" --state-dir %h/comfy-proxy-state/%i --comfyui-base-dir $COMFY_DIR'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
for c in $PORTS; do mkdir -p ~/comfy-proxy-state/$c; done
systemctl --user daemon-reload
for c in $PORTS; do systemctl --user enable --now comfy-api-proxy@$c; done
loginctl enable-linger "$USER" 2>/dev/null || echo "(could not enable linger; the proxies stop when you log out)"
sleep 3
for c in $PORTS; do systemctl --user --no-pager --lines=0 status comfy-api-proxy@$c | head -3; done
