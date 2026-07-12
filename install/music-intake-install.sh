#!/usr/bin/env bash
# Copyright (c) 2021-2026 Psych0meter
# Author: Psych0meter
# License: MIT
# Source: https://github.com/Psych0meter/music_intake_server

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
$STD apt-get install -y \
  python3 python3-venv python3-pip \
  ffmpeg build-essential cmake pkg-config \
  libavcodec-dev libavformat-dev libavutil-dev \
  libfftw3-dev libgcrypt20-dev libboost-dev \
  git sqlite3 libchromaprint-tools libchromaprint1 \
  libasound2-dev libpipewire-0.3-dev libclang-dev libpulse-dev \
  libgtk-4-dev libsoup-3.0-dev libadwaita-1-dev blueprint-compiler \
  libdbus-1-dev gettext intltool
msg_ok "Installed Dependencies"

msg_info "Creating musicintake user and directories"
useradd -m -s /bin/bash musicintake
mkdir -p /opt/music-intake/{app/templates,pipeline,db,config}
mkdir -p /mnt/nas-intake/{approved,rejected,library}
chown -R musicintake:musicintake /opt/music-intake
msg_ok "User and directories created"

msg_info "Fetching application source"
git clone -q https://github.com/Psych0meter/music_intake_server.git /opt/music-intake-src
cp -r /opt/music-intake-src/app/* /opt/music-intake/app/
cp -r /opt/music-intake-src/pipeline/* /opt/music-intake/pipeline/
cp /opt/music-intake-src/config/beets-config.yaml /opt/music-intake/config/beets-config.yaml
cp /opt/music-intake-src/config/scan_roots.txt.example /opt/music-intake/config/scan_roots.txt
chown -R musicintake:musicintake /opt/music-intake
chmod +x /opt/music-intake/pipeline/import_approved.sh
msg_ok "Application deployed to /opt/music-intake"

msg_info "Setting up Python environment"
sudo -u musicintake bash -c '
  python3 -m venv /opt/music-intake/venv
  source /opt/music-intake/venv/bin/activate
  pip install --upgrade pip -q
  pip install -r /opt/music-intake-src/requirements.txt -q
'
msg_ok "Python environment ready"

msg_info "Building SongRec from source (this takes several minutes)"
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable >/dev/null 2>&1
# shellcheck disable=SC1091
source "$HOME/.cargo/env"
git clone -q https://github.com/marin-m/SongRec.git /tmp/songrec
cd /tmp/songrec || exit
# GTK4/libadwaita GUI feature deliberately excluded - not needed for
# headless CLI recognition, and libadwaita's version requirement
# regularly outpaces Debian stable's packaged version.
$STD cargo build --release --no-default-features -F ffmpeg,pulse,pipewire,mpris
install -m 755 target/release/songrec /usr/local/bin/songrec
cd / || exit
rm -rf /tmp/songrec
msg_ok "SongRec built and installed"

msg_info "Creating configuration template"
cp /opt/music-intake-src/config/secrets.env.example /opt/music-intake/config/secrets.env
chmod 600 /opt/music-intake/config/secrets.env
chown musicintake:musicintake /opt/music-intake/config/secrets.env /opt/music-intake/config/scan_roots.txt
msg_ok "Config template created"

msg_info "Creating systemd services"
cat <<'EOF' > /etc/systemd/system/music-recognize.service
[Unit]
Description=Music Intake recognition daemon
After=network.target

[Service]
Type=simple
User=musicintake
EnvironmentFile=/opt/music-intake/config/secrets.env
WorkingDirectory=/opt/music-intake/pipeline
ExecStart=/opt/music-intake/venv/bin/python3 /opt/music-intake/pipeline/recognize.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat <<'EOF' > /etc/systemd/system/music-review-ui.service
[Unit]
Description=Music Intake Flask review UI
After=network.target

[Service]
Type=simple
User=musicintake
WorkingDirectory=/opt/music-intake/app
ExecStart=/opt/music-intake/venv/bin/python3 /opt/music-intake/app/server.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat <<'EOF' > /etc/systemd/system/music-import.timer
[Unit]
Description=Run beets import against the approved/ staging area every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
EOF

cat <<'EOF' > /etc/systemd/system/music-import.service
[Unit]
Description=beets import of approved/ into the managed library

[Service]
Type=oneshot
User=musicintake
ExecStart=/opt/music-intake/pipeline/import_approved.sh
EOF

systemctl daemon-reload
systemctl enable -q --now music-recognize.service
systemctl enable -q --now music-review-ui.service
systemctl enable -q --now music-import.timer
msg_ok "Services enabled and started"

msg_info "Note: ACOUSTID_API_KEY is not set yet"
echo -e "  Edit /opt/music-intake/config/secrets.env, then:"
echo -e "  systemctl restart music-recognize.service"
msg_ok "Setup notes displayed"

motd_ssh
customize
cleanup_lxc
