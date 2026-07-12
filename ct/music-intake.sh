#!/usr/bin/env bash
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)
# Copyright (c) 2021-2026 Psych0meter
# Author: Psych0meter
# License: MIT
# Source: https://github.com/Psych0meter/music_intake_server

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

  msg_info "Updating ${APP}"
  cd /opt/music-intake-src || { msg_error "Source checkout missing"; exit; }
  git pull -q
  cp -r app/* /opt/music-intake/app/
  cp -r pipeline/* /opt/music-intake/pipeline/
  chown -R musicintake:musicintake /opt/music-intake
  systemctl restart music-recognize.service music-review-ui.service
  msg_ok "Updated ${APP}"
  exit
}

start
build_container
description

msg_ok "Completed Successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Review UI:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:5000${CL}"
echo -e ""
echo -e "${INFO}${YW}Manual steps still required (host-side, then inside the container):${CL}"
echo -e "${TAB}1. Bind-mount your NAS source:"
echo -e "${TAB}   pct set ${CTID} -mp0 /path/to/your/music,mp=/mnt/nas-source"
echo -e "${TAB}2. Bind-mount a managed output area (separate from the source):"
echo -e "${TAB}   pct set ${CTID} -mp1 /path/to/managed,mp=/mnt/nas-intake"
echo -e "${TAB}3. Inside the container, edit:"
echo -e "${TAB}   /opt/music-intake/config/scan_roots.txt   (which folders to scan)"
echo -e "${TAB}   /opt/music-intake/config/secrets.env      (ACOUSTID_API_KEY, required)"
echo -e "${TAB}4. Restart the daemon after editing secrets.env:"
echo -e "${TAB}   pct exec ${CTID} -- systemctl restart music-recognize.service"
echo -e ""
echo -e "${INFO}${YW}See docs/CONFIGURATION.md in the repo for full details.${CL}"
