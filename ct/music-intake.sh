#!/usr/bin/env bash
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)

# Copyright (c) 2021-2026 Psych0meter
# Author: Psych0meter
# License: MIT
# Source: https://github.com/Psych0meter/music_intake_server

# ---------------------------------------------------------------------------
# Branch to deploy. Defaults to "main".
#
# Example:
# export BRANCH=debug
# bash -c "$(curl -fsSL https://raw.githubusercontent.com/Psych0meter/music_intake_server/main/ct/music-intake.sh)"
# ---------------------------------------------------------------------------

BRANCH="${BRANCH:-main}"
REPO="https://github.com/Psych0meter/music_intake_server"
INSTALL_SCRIPT_URL="https://raw.githubusercontent.com/Psych0meter/music_intake_server/${BRANCH}/install/music-intake-install.sh"

APP="Music-Intake"

var_tags="${var_tags:-music;audio;beets}"
var_cpu="${var_cpu:-4}"
var_ram="${var_ram:-4096}"
var_disk="${var_disk:-12}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors


function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/music-intake ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  msg_info "Updating Container OS"
  apt_update_safe
  $STD apt-get -o Dpkg::Options::="--force-confold" -y dist-upgrade
  msg_ok "Updated Container OS"

  msg_info "Stopping Services"
  systemctl stop music-recognize.service
  systemctl stop music-review-ui.service
  msg_ok "Stopped Services"

  if [[ ! -d /opt/music-intake-src/.git ]]; then
    msg_error "Source checkout missing"
    exit
  fi

  msg_info "Updating ${APP} Source"
  cd /opt/music-intake-src
  INSTALLED_BRANCH=$(cat /opt/music-intake_version.txt 2>/dev/null | cut -d'@' -f1)
  INSTALLED_BRANCH="${INSTALLED_BRANCH:-main}"
  git fetch origin

  if git show-ref --verify --quiet "refs/remotes/origin/${INSTALLED_BRANCH}"; then
    git checkout "${INSTALLED_BRANCH}"
    git reset --hard "origin/${INSTALLED_BRANCH}"
  else
    msg_warn "Installed branch ${INSTALLED_BRANCH} no longer exists, switching to main"
    git checkout main
    git reset --hard origin/main
    INSTALLED_BRANCH="main"
  fi
  msg_ok "Updated source (branch: ${INSTALLED_BRANCH})"

  msg_info "Deploying Application Files"
  rm -rf /opt/music-intake/app/*
  rm -rf /opt/music-intake/pipeline/*
  rm -rf /opt/music-intake/migrations/*

  cp -r app/* /opt/music-intake/app/
  cp -r pipeline/* /opt/music-intake/pipeline/
  cp -r migrations/* /opt/music-intake/migrations/
  cp migrate.py /opt/music-intake/migrate.py

  # Refresh systemd configuration files if changes were tracked in the repo
  if [ -d "services" ]; then
    cp services/*.service services/*.timer /etc/systemd/system/ 2>/dev/null || true
    systemctl daemon-reload
  fi

  chown -R musicintake:musicintake /opt/music-intake
  msg_ok "Application Files Updated"

  msg_info "Running Database Migrations"
  # Run the migrations using the application venv binary to apply pending schemas
  /opt/music-intake/venv/bin/python3 /opt/music-intake/migrate.py
  msg_ok "Database Schema Synchronized"

  msg_info "Starting Services"
  systemctl start music-recognize.service
  systemctl start music-review-ui.service
  msg_ok "Services Started"

  msg_ok "Update Successful"
  exit
}


start
build_container


# ---------------------------------------------------------------------------
# Run install script inside container.
# Uses lxc-attach because build_container already created the CT.
# ---------------------------------------------------------------------------

msg_info "Running ${APP} Install Script (branch: ${BRANCH})"

curl -fsSL "$INSTALL_SCRIPT_URL" | BRANCH="${BRANCH}" FUNCTIONS_FILE_PATH="${FUNCTIONS_FILE_PATH}" lxc-attach -n "$CTID" -- bash

msg_ok "Install Script Completed"


description


msg_ok "Completed Successfully!\n"

echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Branch deployed: ${BRANCH}${CL}"

echo -e "${INFO}${YW}Review UI:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:5000${CL}"

echo -e ""
echo -e "${INFO}${YW}Manual steps still required (host-side, then inside the container):${CL}"

echo -e "${TAB}1. Bind-mount your NAS source:"
echo -e "${TAB}   pct set ${CTID} -mp0 /path/to/your/music,mp=/mnt/nas-source"

echo -e "${TAB}2. Bind-mount managed output:"
echo -e "${TAB}   pct set ${CTID} -mp1 /path/to/managed,mp=/mnt/nas-intake"

echo -e "${TAB}3. Configure scan roots:"
echo -e "${TAB}   /opt/music-intake/config/scan_roots.txt"

echo -e "${TAB}4. Configure secrets:"
echo -e "${TAB}   /opt/music-intake/config/secrets.env"

echo -e "${TAB}   Required:"
echo -e "${TAB}   ACOUSTID_API_KEY=<your_key>"

echo -e "${TAB}5. Restart recognition service:"
echo -e "${TAB}   pct exec ${CTID} -- systemctl restart music-recognize.service"

echo -e ""

echo -e "${INFO}${YW}See docs/CONFIGURATION.md in the repository for full details.${CL}"
