#!/usr/bin/env bash
# ==============================================================================
# lib/python.sh
# Gestion de Python, du venv et de PyTorch
# Version 2
# ==============================================================================

PY_MIN_MAJOR=3
PY_MIN_MINOR=10

################################################################################
# Création du venv
################################################################################

setup_python_venv() {

    log_step "Configuration de l'environnement Python"

    local pybin="python3"

    if ! require_cmd "$pybin"; then
        log_error "python3 introuvable."
        exit 1
    fi

    local ver
    ver="$("$pybin" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

    local major="${ver%%.*}"
    local minor="${ver##*.}"

    if (( major < PY_MIN_MAJOR || (major == PY_MIN_MAJOR && minor < PY_MIN_MINOR) ))
    then
        log_warn "Python ${ver} détecté."
    else
        log_ok "Python ${ver} OK."
    fi

    if [[ ! -d "$VENV_DIR" ]]
    then

        log_info "Création du venv"

        "$pybin" -m venv "$VENV_DIR"

    else

        log_ok "Venv déjà présent."

    fi

    source "${VENV_DIR}/bin/activate"

    python -m pip install --upgrade \
        pip \
        setuptools \
        wheel

    deactivate

    log_ok "Environnement virtuel prêt."

}

################################################################################
# Détection CUDA
################################################################################

detect_cuda_index() {

    local cuda

    cuda="$(nvidia-smi --query-gpu=cuda_version --format=csv,noheader 2>/dev/null)"
    cuda="${cuda%%$'\n'*}"
    cuda="$(echo "$cuda" | tr -d '[:space:]')"

    case "$cuda" in
        13.*)   echo "cu130" ;;
        12.8*)  echo "cu128" ;;
        12.6*)  echo "cu126" ;;
        12.4*)  echo "cu124" ;;
        *)      echo "cu128" ;;
    esac
}

################################################################################
# Installation PyTorch
################################################################################

install_pytorch() {

    log_step "Installation de PyTorch"

    local CUDA_INDEX

    CUDA_INDEX="$(detect_cuda_index)"

    log_info "Version CUDA détectée : ${CUDA_INDEX}"

    source "${VENV_DIR}/bin/activate"

    python -m pip uninstall -y torch torchvision torchaudio >/dev/null 2>&1 || true
    python -m pip cache purge >/dev/null 2>&1 || true

    retry "$DOWNLOAD_MAX_RETRIES" \
        python -m pip install \
        --upgrade \
        --force-reinstall \
        --no-cache-dir \
        torch \
        torchvision \
        torchaudio \
        --index-url "https://download.pytorch.org/whl/${CUDA_INDEX}"

python - <<EOF
import sys
import torch

print("PyTorch :", torch.__version__)
print("CUDA    :", torch.version.cuda)
print("GPU     :", torch.cuda.is_available())

if not torch.cuda.is_available():
    sys.exit(1)
EOF
    deactivate

    log_ok "PyTorch installé."

}

################################################################################
# Dépendances ComfyUI
################################################################################

install_comfyui_requirements() {

    log_step "Installation des dépendances ComfyUI"

    local req="${INSTALL_DIR}/requirements.txt"

    [[ -f "$req" ]] || {
        log_error "requirements.txt introuvable."
        exit 1
    }

    source "${VENV_DIR}/bin/activate"

    retry "$DOWNLOAD_MAX_RETRIES" python -m pip install -r "$req"

    deactivate

    install_pytorch || return 1
    verify_cuda || return 1

    log_ok "ComfyUI prêt."
}

################################################################################
# Dépendances supplémentaires
################################################################################

install_extra_requirements() {

    log_step "Installation des dépendances supplémentaires"

    local req="${PROJECT_ROOT}/requirements.txt"

    if [[ ! -f "$req" ]]
    then

        log_warn "Pas de requirements supplémentaires."

        return

    fi

    source "${VENV_DIR}/bin/activate"

   retry "$DOWNLOAD_MAX_RETRIES" python -m pip install -r "$req"

python -m pip install -U hf_xet

    deactivate

    log_ok "Dépendances supplémentaires installées."

}

################################################################################
# Vérification CUDA
################################################################################

verify_cuda() {

    log_step "Vérification PyTorch"

    source "${VENV_DIR}/bin/activate"

    python << EOF

import torch

print("=" * 60)
print("PyTorch :", torch.__version__)
print("CUDA    :", torch.version.cuda)
print("GPU     :", torch.cuda.is_available())

if not torch.cuda.is_available():
    raise SystemExit(1)

EOF

    deactivate

    log_ok "CUDA opérationnel."

}