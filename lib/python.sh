#!/usr/bin/env bash
# ==============================================================================
# lib/python.sh
# Gestion de Python, du venv et de PyTorch
# ==============================================================================
#
# CORRECTIF (problème 2) : PyTorch n'est désormais installé qu'UNE SEULE FOIS,
# directement dans la version voulue (2.11.0+cu128), AVANT le
# requirements.txt de ComfyUI. Comme la ligne "torch" de ce requirements.txt
# n'est pas versionnée, pip la voit déjà satisfaite et ne la retélécharge
# pas. Une vérification finale (verify_cuda) s'assure qu'aucune dépendance de
# requirements.txt n'a ensuite changé cette version dans son dos.

PY_MIN_MAJOR=3
PY_MIN_MINOR=10

# Version Torch fixée volontairement : évite toute réinstallation ultérieure
# et tout risque de détection foireuse de version CUDA sur les nouveaux
# pilotes. À changer ici uniquement si vous voulez une autre version.
TORCH_VERSION="2.11.0"
TORCH_CUDA_INDEX="cu128"

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
    python -m pip install --upgrade pip setuptools wheel
    deactivate

    log_ok "Environnement virtuel prêt."
}

################################################################################
# Installation PyTorch — UNIQUE point d'installation de torch/torchvision/
# torchaudio dans tout le projet. Idempotente : si la bonne version tourne
# déjà avec CUDA opérationnel, rien n'est retéléchargé.
################################################################################

install_pytorch() {
    log_step "PyTorch ${TORCH_VERSION}+${TORCH_CUDA_INDEX}"

    source "${VENV_DIR}/bin/activate"

    if python -c "
import sys
try:
    import torch
    ok = torch.__version__.startswith('${TORCH_VERSION}') and torch.cuda.is_available()
except Exception:
    ok = False
sys.exit(0 if ok else 1)
" 2>/dev/null; then
        log_ok "PyTorch ${TORCH_VERSION} déjà installé avec CUDA opérationnel — pas de réinstallation."
        deactivate
        return 0
    fi

    log_info "Installation de PyTorch ${TORCH_VERSION} (index ${TORCH_CUDA_INDEX})..."
    retry "$DOWNLOAD_MAX_RETRIES" \
        python -m pip install \
        "torch==${TORCH_VERSION}" torchvision torchaudio \
        --index-url "https://download.pytorch.org/whl/${TORCH_CUDA_INDEX}"

    deactivate
    log_ok "PyTorch ${TORCH_VERSION} installé (une seule fois)."
}

################################################################################
# Dépendances ComfyUI — torch est déjà en place à ce stade ; pip verra la
# ligne non-versionnée "torch" de requirements.txt comme satisfaite et ne la
# retéléchargera pas.
################################################################################

install_comfyui_requirements() {
    log_step "Installation des dépendances ComfyUI"

    local req="${INSTALL_DIR}/requirements.txt"
    [[ -f "$req" ]] || {
        log_error "requirements.txt introuvable dans ${INSTALL_DIR} (ComfyUI a-t-il bien été cloné ?)."
        exit 1
    }

    install_pytorch || return 1

    source "${VENV_DIR}/bin/activate"
    retry "$DOWNLOAD_MAX_RETRIES" python -m pip install -r "$req"
    deactivate

    # Vérification finale : si une dépendance de requirements.txt a quand
    # même fait dévier torch de la version voulue, on le sait tout de suite
    # plutôt que de découvrir un CUDA cassé au lancement.
    verify_cuda || {
        log_error "CUDA indisponible après l'installation des dépendances ComfyUI."
        log_error "Une dépendance de requirements.txt a peut-être modifié torch — vérifiez ${LOG_FILE}."
        return 1
    }

    log_ok "ComfyUI prêt (PyTorch ${TORCH_VERSION}+${TORCH_CUDA_INDEX})."
}

################################################################################
# Dépendances additionnelles du projet (inchangé)
################################################################################

install_extra_requirements() {
    log_step "Installation des dépendances additionnelles du projet"

    local req="${PROJECT_ROOT}/requirements.txt"
    if [[ ! -f "$req" ]]; then
        log_warn "requirements.txt du projet introuvable, on saute."
        return 0
    fi

    source "${VENV_DIR}/bin/activate"
    retry "$DOWNLOAD_MAX_RETRIES" python -m pip install -r "$req"
    python -m pip install -U hf_xet
    deactivate

    log_ok "Dépendances additionnelles (Hugging Face CLI, hf_transfer, hf_xet...) installées."
}

################################################################################
# Vérification CUDA
################################################################################

verify_cuda() {
    log_step "Vérification PyTorch / CUDA"

    source "${VENV_DIR}/bin/activate"
    python << 'EOF'
import torch
print("=" * 60)
print("PyTorch :", torch.__version__)
print("CUDA    :", torch.version.cuda)
print("GPU     :", torch.cuda.is_available())
print("=" * 60)
if not torch.cuda.is_available():
    raise SystemExit(1)
EOF
    local rc=$?
    deactivate

    if [[ "$rc" -ne 0 ]]; then
        return 1
    fi
    log_ok "CUDA opérationnel."
}
