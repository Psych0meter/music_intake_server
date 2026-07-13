#!/usr/bin/env bash
# Copyright (c) 2026 Psych0meter
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


BRANCH="${BRANCH:-main}"


msg_info "Installing Dependencies"

$STD apt-get install -y \
  curl \
  git \
  python3 \
  python3-venv \
  python3-pip \
  ffmpeg \
  build-essential \
  cmake \
  pkg-config \
  libavcodec-dev \
  libavformat-dev \
  libavutil-dev \
  libfftw3-dev \
  libgcrypt20-dev \
  libboost-dev \
  sqlite3 \
  libchromaprint-tools \
  libchromaprint1 \
  libasound2-dev \
  libpipewire-0.3-dev \
  libclang-dev \
  libpulse-dev \
  libgtk-4-dev \
  libsoup-3.0-dev \
  libadwaita-1-dev \
  blueprint-compiler \
  libdbus-1-dev \
  gettext \
  intltool

msg_ok "Installed Dependencies"



msg_info "Creating musicintake user and directories"

useradd \
  -m \
  -s /bin/bash \
  musicintake

mkdir -p \
  /opt/music-intake/{app/templates,pipeline,db,config,migrations} \
  /mnt/nas-intake/{approved,rejected,library}

chown -R musicintake:musicintake /opt/music-intake

msg_ok "User and directories created"



msg_info "Fetching application source (branch: ${BRANCH})"

git clone -q \
  --branch "${BRANCH}" \
  https://github.com/Psych0meter/music_intake_server.git \
  /opt/music-intake-src

RELEASE=$(git -C /opt/music-intake-src rev-parse --short HEAD)

echo "${BRANCH}@${RELEASE}" > /opt/music-intake_version.txt


cp -r /opt/music-intake-src/app/* \
  /opt/music-intake/app/

cp -r /opt/music-intake-src/pipeline/* \
  /opt/music-intake/pipeline/

cp -r /opt/music-intake-src/migrations/* \
  /opt/music-intake/migrations/

cp /opt/music-intake-src/migrate.py \
  /opt/music-intake/migrate.py

cp /opt/music-intake-src/config/beets-config.yaml \
  /opt/music-intake/config/beets-config.yaml

cp /opt/music-intake-src/config/scan_roots.txt.example \
  /opt/music-intake/config/scan_roots.txt


chmod +x /opt/music-intake/pipeline/import_approved.sh

chown -R musicintake:musicintake /opt/music-intake

msg_ok "Application deployed (${BRANCH}@${RELEASE})"



msg_info "Setting up Python environment"

runuser -u musicintake -- bash -c "
python3 -m venv /opt/music-intake/venv
source /opt/music-intake/venv/bin/activate
pip install --upgrade pip -q
pip install -r /opt/music-intake-src/requirements.txt -q
"

msg_ok "Python environment ready"



msg_info "Building SongRec from source"

curl \
  --proto '=https' \
  --tlsv1.2 \
  -sSf \
  https://sh.rustup.rs \
  | sh -s -- -y --default-toolchain stable \
  >/dev/null 2>&1

source "$HOME/.cargo/env"

git clone -q \
  https://github.com/marin-m/SongRec.git \
  /tmp/songrec


cd /tmp/songrec || exit 1


$STD cargo build \
  --release \
  --no-default-features \
  -F ffmpeg,pulse,pipewire,mpris


install -m 755 \
  target/release/songrec \
  /usr/local/bin/songrec


cd /
rm -rf /tmp/songrec


msg_ok "SongRec built and installed"



msg_info "Creating configuration"

cp /opt/music-intake-src/config/secrets.env.example \
   /opt/music-intake/config/secrets.env

chmod 600 \
   /opt/music-intake/config/secrets.env

chown musicintake:musicintake \
   /opt/music-intake/config/secrets.env \
   /opt/music-intake/config/scan_roots.txt


msg_ok "Configuration created"



msg_info "Creating systemd services"


cat <<'EOF' >/etc/systemd/system/music-migrate.service
[Unit]
Description=Music Intake database migration

[Service]
Type=oneshot
User=musicintake
WorkingDirectory=/opt/music-intake
ExecStart=/opt/music-intake/venv/bin/python3 /opt/music-intake/migrate.py
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF



cat <<'EOF' >/etc/systemd/system/music-recognize.service
[Unit]
Description=Music Intake recognition daemon
After=network.target music-migrate.service
Requires=music-migrate.service

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



cat <<'EOF' >/etc/systemd/system/music-review-ui.service
[Unit]
Description=Music Intake Flask review UI
After=network.target music-migrate.service
Requires=music-migrate.service

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



cat <<'EOF' >/etc/systemd/system/music-import.timer
[Unit]
Description=Run approved music import periodically

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
EOF



cat <<'EOF' >/etc/systemd/system/music-import.service
[Unit]
Description=Import approved music

[Service]
Type=oneshot
User=musicintake
ExecStart=/opt/music-intake/pipeline/import_approved.sh
EOF



systemctl daemon-reload


msg_info "Running database migration"

systemctl enable music-migrate.service
systemctl start music-migrate.service

msg_ok "Database migration completed"



msg_info "Starting application services"

systemctl enable --now music-recognize.service
systemctl enable --now music-review-ui.service
systemctl enable --now music-import.timer

msg_ok "Services enabled and started"



msg_info "Setup notes"

echo -e "  Edit:"
echo -e "  /opt/music-intake/config/secrets.env"
echo ""
echo -e "  Required:"
echo -e "  ACOUSTID_API_KEY=<your_key>"
echo ""
echo -e "  Then restart:"
echo -e "  systemctl restart music-recognize.service"

msg_ok "Setup notes displayed"



motd_ssh
customize


cat <<'EOF' >/usr/bin/update
#!/usr/bin/env bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Psych0meter/music_intake_server/main/ct/music-intake.sh)"
EOF

chmod +x /usr/bin/update

msg_ok "Update script configured"


cleanup_lxc