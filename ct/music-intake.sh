#!/usr/bin/env bash
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

# Capture the user's explicit environment variable before defaulting
USER_BRANCH="${BRANCH}"
BRANCH="${BRANCH:-main}"
REPO="https://github.com/Psych0meter/music_intake_server"

# ---------------------------------------------------------------------------
# community-scripts engine bootstrap.
#
# The engine moved out of community-scripts/ProxmoxVE (misc/build.func is
# gone - 404) into community-scripts/core (core/build.func). It resolves the
# install script as ${COMMUNITY_SCRIPTS_URL}/install/<app>-install.sh, so
# point that at THIS repo/branch instead of the upstream default.
# ---------------------------------------------------------------------------
export COMMUNITY_SCRIPTS_URL="${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/Psych0meter/music_intake_server/${BRANCH}}"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")

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

  if [[ ! -d /opt/music-intake-src/.git ]]; then
    msg_error "Source checkout missing"
    exit
  fi

  msg_info "Stopping Services"
  systemctl stop music-recognize.service
  systemctl stop music-review-ui.service
  msg_ok "Stopped Services"

  msg_info "Updating ${APP} Source"
  cd /opt/music-intake-src

  # Resolve target branch: Prioritize explicit env variable, then fall back to tracking file
  if [[ -n "${USER_BRANCH}" ]]; then
    TARGET_BRANCH="${USER_BRANCH}"
  else
    TARGET_BRANCH=$(cat /opt/music-intake_version.txt 2>/dev/null | cut -d'@' -f1)
    TARGET_BRANCH="${TARGET_BRANCH:-main}"
  fi

  git fetch origin

  if git show-ref --verify --quiet "refs/remotes/origin/${TARGET_BRANCH}"; then
    git checkout "${TARGET_BRANCH}"
    git reset --hard "origin/${TARGET_BRANCH}"
  else
    echo -e "${YW}Target branch ${TARGET_BRANCH} no longer exists, switching to main${CL}"
    git checkout main
    git reset --hard origin/main
    TARGET_BRANCH="main"
  fi
  DEPLOYED_REV="$(git rev-parse --short HEAD)"
  # Same "<branch>@<sha>" format the installer writes, so the deployed commit
  # can always be checked with: cat /opt/music-intake_version.txt
  echo "${TARGET_BRANCH}@${DEPLOYED_REV}" > /opt/music-intake_version.txt
  msg_ok "Updated source (${TARGET_BRANCH}@${DEPLOYED_REV})"

  msg_info "Deploying Application Files"
  rm -rf /opt/music-intake/app/*
  rm -rf /opt/music-intake/pipeline/*
  rm -rf /opt/music-intake/migrations/*

  cp -r app/* /opt/music-intake/app/
  cp -r pipeline/* /opt/music-intake/pipeline/
  cp -r migrations/* /opt/music-intake/migrations/
  cp migrate.py /opt/music-intake/migrate.py

  # beets-config.yaml is read fresh by pipeline/import_approved.sh on every
  # run, so it MUST be redeployed or config fixes (e.g. the incremental /
  # duplicate_action fix) never reach the container. It holds no secrets
  # (the AcoustID key comes from secrets.env), so overwrite - keeping a backup.
  if ! cmp -s config/beets-config.yaml /opt/music-intake/config/beets-config.yaml; then
    cp -a /opt/music-intake/config/beets-config.yaml /opt/music-intake/config/beets-config.yaml.bak 2>/dev/null || true
    cp config/beets-config.yaml /opt/music-intake/config/beets-config.yaml
  fi
  chmod +x /opt/music-intake/pipeline/import_approved.sh

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


# The engine fetches ${COMMUNITY_SCRIPTS_URL}/install/music-intake-install.sh
# and runs it inside the new container itself - no manual lxc-attach needed.
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
