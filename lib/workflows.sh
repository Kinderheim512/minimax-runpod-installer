#!/usr/bin/env bash

install_workflows() {

    log_step "Installation des workflows"

mkdir -p "${INSTALL_DIR}/user/default/workflows"
mkdir -p "${INSTALL_DIR}/user/workflows"

cp -f "${PROJECT_ROOT}/workflows/"*.json \
    "${INSTALL_DIR}/user/default/workflows/" 2>/dev/null || true

cp -f "${PROJECT_ROOT}/workflows/"*.json \
    "${INSTALL_DIR}/user/workflows/" 2>/dev/null || true
    log_ok "Workflows installés."

}