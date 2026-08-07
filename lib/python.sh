#!/usr/bin/env bash
# ==============================================================================
# lib/python.sh
# Gestion de Python, du venv et de PyTorch
# ==============================================================================
#
# CORRECTIF (problème 2) : PyTorch n'est désormais installé qu'UNE SEULE FOIS,
# directement dans la version voulue, AVANT le requirements.txt de ComfyUI.
# Comme la ligne "torch" de ce requirements.txt n'est pas versionnée, pip la
# voit déjà satisfaite et ne la retélécharge pas. Une vérification finale
# (verify_cuda) s'assure qu'aucune dépendance de requirements.txt n'a ensuite
# changé cette version dans son dos.
#
# CORRECTIF (sélection PyTorch) : la version de PyTorch/CUDA à installer
# n'est plus figée en dur. Les images de base RunPod annoncent des versions
# CUDA différentes (12.8, 13.x, ...) et le PyTorch qu'elles embarquent par
# défaut n'est pas toujours celui qui exploite correctement le runtime CUDA
# détecté (ex : MiniMax H3 exige cu130 ou plus pour ses opérations
# optimisées). Le runtime CUDA du pilote est donc détecté automatiquement
# via `nvidia-smi`, et le build PyTorch correspondant est choisi dans une
# table unique (PYTORCH_BUILD_TABLE, ci-dessous) — c'est le seul endroit du
# projet où une version de PyTorch est épinglée en dur.

PY_MIN_MAJOR=3
PY_MIN_MINOR=10

################################################################################
# Table de sélection PyTorch — SOURCE UNIQUE DE VÉRITÉ
################################################################################
#
# Format de chaque entrée : "cuda_min:torch_version:index_cuda"
#   cuda_min      -> version CUDA minimale du pilote (telle que rapportée par
#                    `nvidia-smi`) à partir de laquelle ce build est retenu
#   torch_version -> version de PyTorch à épingler pour ce build
#   index_cuda    -> suffixe d'index sur download.pytorch.org/whl/<index_cuda>
#
# Triée par cuda_min croissant. Pour ajouter un nouveau build (ex: cu132 le
# jour où il sort), ajoutez UNE ligne ici — rien d'autre à modifier dans le
# projet, aucun autre fichier ne doit contenir de version PyTorch en dur.
#
# Dernière vérification : PyTorch 2.12.0 (dernière stable). Depuis cette
# version, PyTorch a RETIRÉ l'index cu128 de sa matrice de build officielle
# (cf. notes de version pytorch/pytorch v2.12.0 : "CUDA 12.8 binaries have
# been removed from the PyTorch binary build matrix... users explicitly
# pinning the cu128 index URL will need to switch to cu130 or cu126").
# cu128 est donc un index gelé qui ne recevra plus aucune nouvelle version :
# épingler un torch_version récent dessus échouera systématiquement.
# Les pilotes rapportant CUDA 12.8 sont redirigés vers cu126, qui reste
# maintenu à jour en amont et reste compatible avec un pilote 12.8 (les
# wheels CUDA sont compatibles avec tout pilote égal ou plus récent que la
# version pour laquelle ils sont compilés). cu130 n'est PAS une alternative
# valide ici : un pilote 12.8 est trop ancien pour exécuter un build cu130
# ("CUDA initialization: The NVIDIA driver on your system is too old").
# L'entrée cu118 est un filet de sécurité pour les images RunPod plus
# anciennes dont le pilote n'expose pas CUDA >= 12.6.
#
# IMPORTANT pour la maintenance future : ne jamais supposer qu'un seul
# torch_version est publié identiquement sur tous les index CUDA. PyTorch
# retire ou ajoute des index par version (cf. RFC "CUDA support matrix" sur
# pytorch/pytorch à chaque cycle de release) — vérifier RELEASE.md ET les
# versions réellement listées sur download.pytorch.org/whl/<index>/ avant
# de faire évoluer une ligne de cette table.
PYTORCH_BUILD_TABLE=(
    "11.8:2.5.1:cu118"
    "12.6:2.12.0:cu126"
    "12.8:2.12.0:cu126"
    "13.0:2.12.0:cu130"
)

# Échappatoire de compatibilité / reproductibilité stricte : si les DEUX
# variables suivantes sont définies (ex: dans config.env), la détection
# automatique est court-circuitée et ce build est installé tel quel, quel
# que soit le CUDA détecté. Pratique pour figer une installation validée
# sur un pod donné.
#   TORCH_VERSION_OVERRIDE="2.12.0"
#   TORCH_CUDA_INDEX_OVERRIDE="cu130"

################################################################################
# Détection du runtime CUDA (celui du pilote GPU, via nvidia-smi)
################################################################################

detect_cuda_runtime() {
    if ! require_cmd nvidia-smi; then
        return 0
    fi
    nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | head -n1 | awk '{print $NF}'
}

# cuda_ge A B -> vrai (0) si la version CUDA A >= B (comparaison numérique,
# format X.Y, via un tri de version standard).
cuda_ge() {
    [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" == "$1" ]]
}

################################################################################
# Sélection du build PyTorch adapté au runtime CUDA détecté.
# Renseigne SELECTED_TORCH_VERSION / SELECTED_TORCH_CUDA_INDEX, et met à
# jour TORCH_VERSION / TORCH_CUDA_INDEX (compatibilité avec d'éventuels
# autres scripts du projet qui liraient encore ces deux noms).
################################################################################

select_pytorch_build() {
    local detected="$1"
    SELECTED_TORCH_VERSION=""
    SELECTED_TORCH_CUDA_INDEX=""

    if [[ -n "${TORCH_VERSION_OVERRIDE:-}" && -n "${TORCH_CUDA_INDEX_OVERRIDE:-}" ]]; then
        SELECTED_TORCH_VERSION="$TORCH_VERSION_OVERRIDE"
        SELECTED_TORCH_CUDA_INDEX="$TORCH_CUDA_INDEX_OVERRIDE"
        log_info "Build PyTorch forcé via config.env (TORCH_VERSION_OVERRIDE) : ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX}"
    elif [[ -z "$detected" ]]; then
        log_warn "Impossible de détecter la version CUDA du pilote (nvidia-smi absent ou GPU non visible)."
        local fallback="${PYTORCH_BUILD_TABLE[-1]}"
        SELECTED_TORCH_VERSION="$(cut -d: -f2 <<< "$fallback")"
        SELECTED_TORCH_CUDA_INDEX="$(cut -d: -f3 <<< "$fallback")"
        log_warn "Repli sur le build le plus récent connu : ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX}."
        log_warn "Si ce n'est pas le bon choix, définissez TORCH_VERSION_OVERRIDE / TORCH_CUDA_INDEX_OVERRIDE dans config.env."
    else
        local entry cuda_min
        for entry in "${PYTORCH_BUILD_TABLE[@]}"; do
            cuda_min="$(cut -d: -f1 <<< "$entry")"
            if cuda_ge "$detected" "$cuda_min"; then
                SELECTED_TORCH_VERSION="$(cut -d: -f2 <<< "$entry")"
                SELECTED_TORCH_CUDA_INDEX="$(cut -d: -f3 <<< "$entry")"
            fi
        done

        if [[ -z "$SELECTED_TORCH_VERSION" ]]; then
            # Runtime CUDA plus ancien que toutes les entrées connues : on
            # prend la plus basse (la mieux adaptée à un vieux pilote), et on
            # prévient que la mise à jour du pilote est recommandée.
            local lowest="${PYTORCH_BUILD_TABLE[0]}"
            SELECTED_TORCH_VERSION="$(cut -d: -f2 <<< "$lowest")"
            SELECTED_TORCH_CUDA_INDEX="$(cut -d: -f3 <<< "$lowest")"
            log_warn "CUDA ${detected} est plus ancien que tous les builds connus — utilisation du plus ancien (${SELECTED_TORCH_CUDA_INDEX})."
            log_warn "Une mise à jour du pilote GPU du pod est recommandée."
        fi
    fi

    # Alias de compatibilité, au cas où d'autres scripts du projet
    # (check.sh, update.sh, ...) liraient encore TORCH_VERSION/TORCH_CUDA_INDEX.
    # Non utilisées plus loin dans CE fichier : shellcheck ne fait pas d'analyse
    # inter-fichiers et les marque donc à tort comme inutilisées.
    # shellcheck disable=SC2034
    TORCH_VERSION="$SELECTED_TORCH_VERSION"
    # shellcheck disable=SC2034
    TORCH_CUDA_INDEX="$SELECTED_TORCH_CUDA_INDEX"
}

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

    # shellcheck disable=SC1091  # ${VENV_DIR}/bin/activate n'existe pas encore
    # au moment du lint : il est créé par `venv` juste au-dessus, à l'exécution.
    source "${VENV_DIR}/bin/activate"
    python -m pip install --upgrade pip setuptools wheel
    deactivate

    log_ok "Environnement virtuel prêt."
}

################################################################################
# Installation PyTorch — UNIQUE point d'installation de torch/torchvision/
# torchaudio dans tout le projet. Idempotente : si le build voulu tourne
# déjà avec CUDA opérationnel, rien n'est retéléchargé. Le PyTorch
# éventuellement préinstallé dans l'image de base n'est en revanche jamais
# réutilisé "tel quel" sans vérifier qu'il correspond au build attendu pour
# le runtime CUDA détecté.
################################################################################

install_pytorch() {
    log_step "Sélection et installation de PyTorch"

    local detected
    detected="$(detect_cuda_runtime)"
    select_pytorch_build "$detected"

    log_info "CUDA runtime détecté : ${detected:-inconnu}"
    log_info "Build PyTorch retenu : ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX}"

    # shellcheck disable=SC1091  # cf. note dans setup_python_venv : le venv
    # est créé au préalable par cette fonction, pas visible au lint statique.
    source "${VENV_DIR}/bin/activate"

    local expected="${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX}"

    if python -c "
import sys
try:
    import torch
    ok = torch.__version__ == '${expected}' and torch.cuda.is_available()
except Exception:
    ok = False
sys.exit(0 if ok else 1)
" 2>/dev/null; then
        log_ok "PyTorch ${expected} déjà installé avec CUDA opérationnel — pas de réinstallation."
        deactivate
        return 0
    fi

    log_info "Installation de PyTorch ${expected} (index ${SELECTED_TORCH_CUDA_INDEX})..."
    log_info "(le PyTorch éventuellement préinstallé dans l'image de base, s'il ne correspond pas exactement, sera remplacé)"
    if ! retry "$DOWNLOAD_MAX_RETRIES" \
        python -m pip install \
        "torch==${SELECTED_TORCH_VERSION}" torchvision torchaudio \
        --index-url "https://download.pytorch.org/whl/${SELECTED_TORCH_CUDA_INDEX}"
    then
        deactivate
        log_error "Échec de l'installation de PyTorch ${expected} depuis l'index ${SELECTED_TORCH_CUDA_INDEX}."
        log_error "Si l'erreur pip ci-dessus indique qu'aucune version ne correspond (ex: 'Could not find a version that satisfies...'), l'index ${SELECTED_TORCH_CUDA_INDEX} ne publie probablement plus/pas encore ${SELECTED_TORCH_VERSION} : vérifiez https://download.pytorch.org/whl/${SELECTED_TORCH_CUDA_INDEX}/ et corrigez la ligne correspondante dans PYTORCH_BUILD_TABLE (lib/python.sh)."
        return 1
    fi

    deactivate
    log_ok "PyTorch ${expected} installé (une seule fois)."
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

    # shellcheck disable=SC1091  # cf. note dans setup_python_venv.
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

    log_ok "ComfyUI prêt (PyTorch ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX})."
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

    # shellcheck disable=SC1091  # cf. note dans setup_python_venv.
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

    local detected
    detected="$(detect_cuda_runtime)"

    # shellcheck disable=SC1091  # cf. note dans setup_python_venv.
    source "${VENV_DIR}/bin/activate"
    CUDA_RUNTIME_DETECTED="${detected:-inconnu}" python << 'EOF'
import os
import torch

detected = os.environ.get("CUDA_RUNTIME_DETECTED", "inconnu")

print("=" * 60)
print(f"CUDA runtime detected : {detected}")
print(f"PyTorch installed     : {torch.__version__}")
print(f"GPU disponible         : {torch.cuda.is_available()}")
print("=" * 60)

if not torch.cuda.is_available():
    raise SystemExit(1)

# Avertissement si le build PyTorch installé a été compilé pour une version
# de CUDA plus ancienne que le runtime détecté sur le pod : PyTorch reste
# généralement utilisable (compatibilité ascendante des pilotes NVIDIA),
# mais certaines opérations optimisées pour le runtime le plus récent
# peuvent ne pas être disponibles (c'est exactement le message rapporté par
# MiniMax H3 sur les pods CUDA 13.x avec un build plus ancien).
try:
    if detected != "inconnu":
        runtime_major = float(detected)
        torch_cuda = torch.version.cuda or "0"
        torch_major = float(torch_cuda)
        if torch_major < runtime_major:
            print(
                f"[WARN] PyTorch a été compilé pour CUDA {torch_cuda}, plus ancien que le "
                f"runtime détecté ({detected}). Certaines opérations optimisées pour ce "
                f"runtime peuvent être indisponibles ou plus lentes. Si un nœud/modèle "
                f"réclame explicitement un cu plus récent, ajoutez l'entrée correspondante "
                f"dans PYTORCH_BUILD_TABLE (lib/python.sh) puis relancez l'installation."
            )
except Exception:
    pass
EOF
    local rc=$?
    deactivate

    if [[ "$rc" -ne 0 ]]; then
        return 1
    fi
    log_ok "CUDA opérationnel."
}
