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
    # Renseigné uniquement par la branche PREFER_CUDA130 ci-dessous : build
    # "sûr" (celui normalement associé au CUDA détecté) vers lequel
    # install_pytorch() peut retomber automatiquement si cu130 échoue à la
    # vérification CUDA post-installation.
    PREFER_CUDA130_FALLBACK_VERSION=""
    PREFER_CUDA130_FALLBACK_INDEX=""

    if [[ -n "${TORCH_VERSION_OVERRIDE:-}" && -n "${TORCH_CUDA_INDEX_OVERRIDE:-}" ]]; then
        SELECTED_TORCH_VERSION="$TORCH_VERSION_OVERRIDE"
        SELECTED_TORCH_CUDA_INDEX="$TORCH_CUDA_INDEX_OVERRIDE"
        log_info "Build PyTorch forcé via config.env (TORCH_VERSION_OVERRIDE) : ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX}"
        # Échappatoire de reproductibilité stricte : aucun repli automatique
        # ici, volontairement — si vous forcez un build précis, c'est que
        # vous savez ce que vous faites (ou que vous voulez reproduire une
        # installation validée à l'identique) ; on ne le contourne jamais
        # dans son dos. Pour un cu130 "tenté puis vérifié avec repli sûr",
        # utilisez PREFER_CUDA130=true plutôt que cet override.
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

        # PREFER_CUDA130 (config.env) : le champ "CUDA Version" de nvidia-smi
        # rapporte la version CUDA MAXIMALE supportée par le pilote hôte —
        # normalement fiable, mais quelques pilotes/nvidia-smi la rapportent
        # de façon conservatrice. Si PREFER_CUDA130=true et que le build
        # normalement retenu ci-dessus n'est PAS déjà cu130, on TENTE quand
        # même cu130 (le plus performant pour MiniMax H3) plutôt que de s'en
        # tenir aveuglément à la table — mais on mémorise le build "sûr"
        # (celui juste calculé) comme repli : install_pytorch() y retombera
        # automatiquement et silencieusement si la vérification CUDA après
        # coup échoue (pilote réellement trop ancien pour cu130). Jamais de
        # pod cassé : au pire, on revient au comportement par défaut.
        if [[ "${PREFER_CUDA130:-false}" == "true" && "$SELECTED_TORCH_CUDA_INDEX" != "cu130" ]]; then
            local cu130_entry=""
            for entry in "${PYTORCH_BUILD_TABLE[@]}"; do
                [[ "$(cut -d: -f3 <<< "$entry")" == "cu130" ]] && cu130_entry="$entry"
            done
            if [[ -n "$cu130_entry" ]]; then
                PREFER_CUDA130_FALLBACK_VERSION="$SELECTED_TORCH_VERSION"
                PREFER_CUDA130_FALLBACK_INDEX="$SELECTED_TORCH_CUDA_INDEX"
                SELECTED_TORCH_VERSION="$(cut -d: -f2 <<< "$cu130_entry")"
                SELECTED_TORCH_CUDA_INDEX="$(cut -d: -f3 <<< "$cu130_entry")"
                log_warn "PREFER_CUDA130=true : tentative de PyTorch ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX} malgré un CUDA détecté (${detected}) normalement associé à ${PREFER_CUDA130_FALLBACK_INDEX}."
                log_warn "Repli automatique et vérifié sur ${PREFER_CUDA130_FALLBACK_VERSION}+${PREFER_CUDA130_FALLBACK_INDEX} si cu130 s'avère incompatible avec le pilote de ce pod."
            else
                log_warn "PREFER_CUDA130=true mais aucune entrée cu130 dans PYTORCH_BUILD_TABLE — ignoré."
            fi
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
#
# _install_pytorch_build(version, index_cuda) -> installe CE build précis
# (idempotent : ne réinstalle rien si déjà en place et CUDA opérationnel).
# Factorisée hors de install_pytorch() pour être appelable deux fois : le
# build tenté, puis — si PREFER_CUDA130 est actif et que la vérification
# CUDA échoue — le build de repli automatique (voir plus bas).
################################################################################

_install_pytorch_build() {
    local version="$1" index="$2"
    local expected="${version}+${index}"

    # shellcheck disable=SC1091  # cf. note dans setup_python_venv.
    source "${VENV_DIR}/bin/activate"

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

    log_info "Installation de PyTorch ${expected} (index ${index})..."
    log_info "(le PyTorch éventuellement préinstallé dans l'image de base, s'il ne correspond pas exactement, sera remplacé)"

    # Désinstallation explicite AVANT d'installer, plutôt que de laisser pip
    # gérer un uninstall-puis-install combiné dans la même transaction —
    # constaté en pratique (repli PREFER_CUDA130, cu130 -> cu118) : quand le
    # build cible diffère de celui déjà en place, pip échoue silencieusement
    # à désinstaller certaines dépendances transitives dont la version exacte
    # change d'un build à l'autre (sympy, triton — mêmes noms de paquet,
    # versions différentes selon le build torch), laissant des métadonnées
    # .dist-info manquantes ("Can't uninstall '...'. No files were found to
    # uninstall.") et un venv dans un état instable même quand l'installation
    # se termine sans erreur bloquante. `|| true` : rien à désinstaller au
    # tout premier install (pip répond juste "not installed", pas une erreur
    # à traiter comme telle) — jamais bloquant.
    python -m pip uninstall -y torch torchvision torchaudio triton sympy >/dev/null 2>&1 || true

    if ! retry "$DOWNLOAD_MAX_RETRIES" \
        python -m pip install \
        "torch==${version}" torchvision torchaudio \
        --index-url "https://download.pytorch.org/whl/${index}"
    then
        deactivate
        log_error "Échec de l'installation de PyTorch ${expected} depuis l'index ${index}."
        log_error "Si l'erreur pip ci-dessus indique qu'aucune version ne correspond (ex: 'Could not find a version that satisfies...'), l'index ${index} ne publie probablement plus/pas encore ${version} : vérifiez https://download.pytorch.org/whl/${index}/ et corrigez la ligne correspondante dans PYTORCH_BUILD_TABLE (lib/python.sh)."
        return 1
    fi

    deactivate
    log_ok "PyTorch ${expected} installé."
}

################################################################################
# bake_pytorch_best_guess() — UNIQUEMENT à la construction de l'image Docker
# (docker-build-steps-heavy.sh), jamais appelée par install.sh/update.sh. Installe
# par avance le build PyTorch le plus récent connu de PYTORCH_BUILD_TABLE
# (actuellement cu130) SANS connaître le GPU réel du pod (aucun GPU visible
# à la construction de l'image) : un pari assumé, pas une détection —
# beaucoup de pods RunPod ont un pilote assez récent pour le supporter.
#
# Pourquoi c'est sûr, dans les deux cas :
#   - Pilote du pod compatible cu130 (cas majoritaire) : au démarrage du
#     conteneur, install_pytorch() (via docker-entrypoint.sh) retrouve ce
#     build déjà installé ET fonctionnel — l'idempotence déjà présente dans
#     _install_pytorch_build() ne retélécharge RIEN, le démarrage du pod
#     saute quasiment cette étape.
#   - Pilote du pod trop ancien (cas minoritaire) : le mécanisme
#     PREFER_CUDA130 (voir select_pytorch_build()/install_pytorch()
#     ci-dessus), activé par défaut UNIQUEMENT dans cette image Docker (voir
#     Dockerfile, ENV PREFER_CUDA130), tente ce même build cu130 déjà en
#     place — pip le trouve "déjà satisfait" (aucun retéléchargement) —,
#     détecte via verify_cuda() qu'il ne fonctionne pas, et retombe
#     automatiquement sur le build réellement compatible. Seul CE build de
#     repli est téléchargé : ni plus ni moins que si rien n'avait été
#     pré-installé. Jamais de pod cassé, jamais de perte de temps notable,
#     dans aucun des deux cas.
################################################################################

bake_pytorch_best_guess() {
    log_step "Pré-installation de PyTorch dans l'image (pari : le build le plus récent connu)"

    local latest="${PYTORCH_BUILD_TABLE[-1]}"
    local version index
    version="$(cut -d: -f2 <<< "$latest")"
    index="$(cut -d: -f3 <<< "$latest")"

    log_info "Build pré-installé : ${version}+${index} — vérifié, et remplacé si besoin, au démarrage de chaque conteneur (voir PREFER_CUDA130, config.env)."
    _install_pytorch_build "$version" "$index"
}

################################################################################
# bake_sageattention_wheel() — UNIQUEMENT à la construction de l'image Docker
# (docker-build-steps-heavy.sh), appelée juste après bake_pytorch_best_guess().
# Compile SageAttention UNE SEULE FOIS dans l'environnement contrôlé du build
# et produit une wheel .whl réutilisable, au lieu de laisser chaque
# conteneur la recompiler depuis les sources à chaque démarrage (10-20 min,
# et c'est ce chemin qui échoue le plus souvent selon l'état du pod : nvcc
# absent, RAM insuffisante, réseau...). install_sageattention() (lib/python.sh,
# appelée par install.sh au démarrage du conteneur) installe directement
# cette wheel si son index CUDA correspond au torch réellement retenu à ce
# moment-là ; sinon (repli PREFER_CUDA130 sur un pilote pod trop ancien) elle
# recompile normalement — exactement comme sur un pod nu sans image Docker,
# aucune régression dans ce cas.
#
# Best-effort et non bloquant, comme install_sageattention() : un échec ici
# ne doit jamais faire échouer le build de l'image (docker-build-steps-heavy.sh
# tourne sous `set -e`) — seulement priver le conteneur de la wheel
# pré-compilée, auquel cas install_sageattention() recompile normalement au
# démarrage, comme avant ce mécanisme.
#
# Aucun GPU visible à la construction de l'image : TORCH_CUDA_ARCH_LIST est
# donc fixé explicitement (SAGEATTENTION_ARCH_LIST, config.env) plutôt que
# déduit de nvidia-smi, pour produire une wheel "fat binary" couvrant
# Ampere à Blackwell au lieu de deviner une seule architecture.
################################################################################
bake_sageattention_wheel() {
    log_step "Pré-compilation de SageAttention dans l'image (wheel réutilisable, aucune compilation au démarrage du pod)"

    # Réglage dédié à cette étape de build, volontairement indépendant de
    # SAGE_ATTENTION (qui contrôle l'exécution, pas la construction de
    # l'image) — voir le commentaire complet dans config.env.
    if [[ "${SAGEATTENTION_DOCKER_BAKE:-true}" == "false" ]]; then
        log_info "SAGEATTENTION_DOCKER_BAKE=false — pré-compilation sautée (le conteneur compilera depuis les sources au démarrage si SAGE_ATTENTION=auto/true)."
        return 0
    fi

    # shellcheck disable=SC1091  # cf. note dans setup_python_venv.
    source "${VENV_DIR}/bin/activate"

    local torch_cuda="" torch_major="" torch_minor="" cuda_index=""
    torch_cuda="$(python -c 'import torch; print(torch.version.cuda or "")' 2>/dev/null)"
    if [[ -z "$torch_cuda" ]]; then
        log_warn "torch.version.cuda introuvable dans le venv — pré-compilation de SageAttention sautée (le conteneur compilera depuis les sources au démarrage, comme avant ce mécanisme)."
        deactivate
        return 0
    fi
    torch_major="$(cut -d. -f1 <<< "$torch_cuda")"
    torch_minor="$(cut -d. -f2 <<< "$torch_cuda")"
    cuda_index="cu${torch_major}${torch_minor}"

    # --- toolkit CUDA de la même branche majeure (même logique de vérification
    # que install_sageattention(), voir _sage_find_matching_nvcc) ---
    local matched_cuda_home="" matched_nvcc_ver="" _match=""
    _match="$(_sage_find_matching_nvcc "${torch_major}" "/usr/local/cuda-${torch_major}.${torch_minor}" /usr/local/cuda-"${torch_major}".*)" || true
    if [[ -n "$_match" ]]; then
        matched_cuda_home="${_match%%:*}"
        matched_nvcc_ver="${_match##*:}"
    fi
    if [[ -z "$matched_cuda_home" ]] && require_cmd apt-get; then
        _sage_ensure_nvidia_cuda_apt_repo || true
        local toolkit_pkg="cuda-toolkit-${torch_major}-${torch_minor}"
        wait_for_apt_lock
        apt-get update -y >>"$LOG_FILE" 2>&1 || log_warn "apt-get update a échoué — on tente quand même l'installation du toolkit CUDA."
        if ! retry "$DOWNLOAD_MAX_RETRIES" apt-get install -y "$toolkit_pkg" >>"$LOG_FILE" 2>&1; then
            log_warn "${toolkit_pkg} indisponible — repli sur cuda-toolkit-${torch_major} (branche majeure, vérifiée ci-dessous — la compatibilité mineure est garantie par NVIDIA, cf. commentaire de _sage_find_matching_nvcc)."
            retry "$DOWNLOAD_MAX_RETRIES" apt-get install -y "cuda-toolkit-${torch_major}" >>"$LOG_FILE" 2>&1 || true
        fi
        _match="$(_sage_find_matching_nvcc "${torch_major}" "/usr/local/cuda-${torch_major}.${torch_minor}" /usr/local/cuda-"${torch_major}".*)" || true
        if [[ -n "$_match" ]]; then
            matched_cuda_home="${_match%%:*}"
            matched_nvcc_ver="${_match##*:}"
        fi
    fi
    if [[ -z "$matched_cuda_home" ]]; then
        log_warn "Aucun nvcc de la branche CUDA ${torch_major}.x disponible pendant le build de l'image — pré-compilation de SageAttention sautée (le conteneur tentera la compilation depuis les sources au démarrage)."
        log_error_tail "apt-get install cuda-toolkit (build image)"
        deactivate
        return 0
    fi
    log_ok "Toolkit CUDA disponible pour la pré-compilation : ${matched_cuda_home}/bin/nvcc (release ${matched_nvcc_ver}, même branche majeure que torch ${torch_major}.${torch_minor})."

    # --- Triton : même contrainte exacte que install_sageattention() -------
    if python -c "import triton" 2>/dev/null; then
        log_ok "Triton déjà présent ($(python -c 'import triton; print(triton.__version__)' 2>/dev/null))."
    else
        local triton_req=""
        triton_req="$(python - <<'PYEOF'
from importlib.metadata import requires, PackageNotFoundError
try:
    reqs = requires("torch") or []
except PackageNotFoundError:
    reqs = []
for r in reqs:
    if r.split(";")[0].strip().lower().startswith("triton"):
        print(r.split(";")[0].strip())
        break
PYEOF
)"
        if [[ -n "$triton_req" ]]; then
            python -m pip install --quiet "$triton_req" >>"$LOG_FILE" 2>&1 || \
                log_warn "Échec d'installation de ${triton_req} — la pré-compilation risque d'échouer (non bloquant)."
        else
            log_warn "Contrainte Triton introuvable dans les métadonnées de torch — pré-compilation tentée sans garantie."
        fi
    fi

    # --- clone (cache partagé avec install_sageattention(), pas de double
    # téléchargement si le cache existe déjà pour une raison quelconque) ----
    local src_dir="${SAGEATTENTION_CACHE_DIR:-${PROJECT_ROOT}/.cache/SageAttention}"
    mkdir -p "$(dirname "$src_dir")" 2>>"$LOG_FILE" || true
    if [[ ! -d "${src_dir}/.git" ]]; then
        log_info "Clonage de SageAttention (${SAGEATTENTION_REPO})..."
        if ! retry "$DOWNLOAD_MAX_RETRIES" git clone "$SAGEATTENTION_REPO" "$src_dir" >>"$LOG_FILE" 2>&1; then
            log_warn "Échec du clonage de SageAttention pendant le build — pré-compilation sautée."
            deactivate
            return 0
        fi
    fi

    local wheel_dir="${SAGEATTENTION_WHEEL_CACHE_DIR:-${PROJECT_ROOT}/.cache/sageattention-wheel}"
    rm -rf "$wheel_dir"
    mkdir -p "$wheel_dir"

    log_info "Compilation de la wheel SageAttention pour ${cuda_index} (archs cibles : ${SAGEATTENTION_ARCH_LIST}) — pas de GPU requis pour cette étape, seulement nvcc."
    local build_rc=0
    (
        cd "$src_dir" || exit 1
        # Variables passées en préfixe de LA SEULE commande qui en a besoin
        # (portée strictement limitée à cette invocation pip, jamais
        # "exportées" au sens shell du terme) plutôt qu'avec `export` séparé
        # — évite les faux positifs ShellCheck SC2030/SC2031 (qui suppose
        # sinon qu'on cherche à faire persister ces variables au-delà de ce
        # sous-shell, alors que le sous-shell n'existe ici que pour isoler
        # le `cd`).
        CUDA_HOME="$matched_cuda_home" \
        PATH="${matched_cuda_home}/bin:${PATH}" \
        TORCH_CUDA_ARCH_LIST="$SAGEATTENTION_ARCH_LIST" \
        EXT_PARALLEL=4 \
        NVCC_APPEND_FLAGS="--threads 8" \
        MAX_JOBS="$SAGEATTENTION_BUILD_JOBS" \
        python -m pip wheel . -w "$wheel_dir" --no-build-isolation --no-deps
    ) >>"$LOG_FILE" 2>&1 || build_rc=$?

    if [[ "$build_rc" -ne 0 ]] || ! find "$wheel_dir" -maxdepth 1 -name '*.whl' 2>/dev/null | grep -q .; then
        log_warn "Échec de pré-compilation de SageAttention pendant le build de l'image (code ${build_rc}) — le conteneur recompilera depuis les sources au démarrage, comme avant ce mécanisme."
        log_error_tail "pré-compilation SageAttention (build image)"
        rm -rf "$wheel_dir"
        deactivate
        return 0
    fi

    echo "$cuda_index" > "${wheel_dir}/cuda_index.txt"
    log_ok "Wheel SageAttention pré-compilée pour ${cuda_index} : $(find "$wheel_dir" -maxdepth 1 -name '*.whl' -printf '%f\n' 2>/dev/null | head -n1) — les conteneurs démarrés depuis cette image l'installeront directement, sans compiler."
    deactivate
}

install_pytorch() {
    log_step "Sélection et installation de PyTorch"

    local detected
    detected="$(detect_cuda_runtime)"
    select_pytorch_build "$detected"

    log_info "CUDA runtime détecté : ${detected:-inconnu}"
    log_info "Build PyTorch retenu : ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX}"

    _install_pytorch_build "$SELECTED_TORCH_VERSION" "$SELECTED_TORCH_CUDA_INDEX" || return 1

    if verify_cuda; then
        log_ok "PyTorch ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX} installé (une seule fois) et fonctionnel."
        return 0
    fi

    # Le build tenté ne fonctionne pas (CUDA indisponible — typiquement un
    # pilote GPU du pod trop ancien pour ce build, cf. message pip/torch
    # "The NVIDIA driver on your system is too old" dans ${LOG_FILE}).
    # PREFER_CUDA130_FALLBACK_VERSION/INDEX n'est renseigné par
    # select_pytorch_build() QUE si PREFER_CUDA130=true a fait tenter cu130
    # à la place du build normalement associé au CUDA détecté (voir
    # commentaire PREFER_CUDA130 dans config.env) : dans ce cas précis, on
    # retombe automatiquement sur ce build "sûr" plutôt que de laisser le
    # pod dans un état cassé.
    if [[ -z "${PREFER_CUDA130_FALLBACK_VERSION:-}" ]]; then
        log_error "CUDA indisponible après installation de PyTorch ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX} — vérifiez le pilote GPU du pod (nvidia-smi) et ${LOG_FILE}."
        return 1
    fi

    log_warn "CUDA indisponible avec ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX} (pilote du pod probablement trop ancien pour ce build — détail dans ${LOG_FILE})."
    log_warn "PREFER_CUDA130=true : repli automatique sur le build associé au CUDA détecté (${detected:-inconnu}) : ${PREFER_CUDA130_FALLBACK_VERSION}+${PREFER_CUDA130_FALLBACK_INDEX}..."

    SELECTED_TORCH_VERSION="$PREFER_CUDA130_FALLBACK_VERSION"
    SELECTED_TORCH_CUDA_INDEX="$PREFER_CUDA130_FALLBACK_INDEX"
    # shellcheck disable=SC2034  # alias de compatibilité, cf. select_pytorch_build.
    TORCH_VERSION="$SELECTED_TORCH_VERSION"
    # shellcheck disable=SC2034
    TORCH_CUDA_INDEX="$SELECTED_TORCH_CUDA_INDEX"

    _install_pytorch_build "$SELECTED_TORCH_VERSION" "$SELECTED_TORCH_CUDA_INDEX" || return 1

    if ! verify_cuda; then
        log_error "CUDA toujours indisponible après repli sur ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX} — le pilote GPU de ce pod semble trop ancien pour tout build PyTorch connu de PYTORCH_BUILD_TABLE. Vérifiez 'nvidia-smi --query-gpu=driver_version --format=csv,noheader' et ${LOG_FILE}."
        return 1
    fi

    log_ok "Repli automatique réussi : PyTorch ${SELECTED_TORCH_VERSION}+${SELECTED_TORCH_CUDA_INDEX} installé et fonctionnel."
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
# pip_install_requirements <chemin_vers_requirements.txt>
# Point d'entrée COMMUN pour installer un requirements.txt tiers (nœud
# custom optionnel, ComfyUI-Manager...) — utilisé par lib/nodes.sh et
# lib/manager.sh plutôt que d'appeler `pip install -r` directement à
# plusieurs endroits.
#
# Filtre torch/torchvision/torchaudio quand DOCKER_BUILD_NO_TORCH=true
# (positionné UNIQUEMENT par docker-build-steps-heavy.sh, jamais par install.sh/
# update.sh) : à la construction de l'image Docker, aucun GPU n'est visible
# donc PYTORCH_BUILD_TABLE ne peut pas être résolue (même raison que
# install_comfyui_requirements_no_torch() ci-dessus). Sans ce filtrage, un
# nœud custom listant "torch" (même sans version précise) dans son propre
# requirements.txt ferait télécharger, à la construction de l'image, un
# torch générique publié sur PyPI (pas l'index CUDA choisi par ce projet) —
# constaté en pratique avec un torch 2.13.0 PyPI tirant des sous-paquets
# NVIDIA (cuda-bindings/cuda-toolkit/triton) eux-mêmes en conflit de version
# entre nœuds, sans jamais faire échouer le build (juste des avertissements
# pip et du temps/espace disque perdus) puisque install_pytorch(), au
# démarrage du conteneur, réinstalle de toute façon le SEUL build attendu
# par-dessus. Ce filtrage évite simplement ce gaspillage — install.sh/
# update.sh, hors Docker, ne définissent jamais DOCKER_BUILD_NO_TORCH et
# passent donc chaque requirements.txt tel quel, comme avant.
################################################################################

pip_install_requirements() {
    local req="$1"
    [[ -f "$req" ]] || return 0

    local target="$req"
    local tmp=""
    if [[ "${DOCKER_BUILD_NO_TORCH:-false}" == "true" ]]; then
        tmp="$(mktemp)"
        grep -viE '^[[:space:]]*(torch|torchvision|torchaudio)[[:space:]]*(\[[^]]*\])?[[:space:]]*([<>=!~;@].*)?$' "$req" > "$tmp"
        target="$tmp"
    fi

    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    pip install -r "$target" --quiet >>"$LOG_FILE" 2>&1
    local rc=$?
    deactivate

    [[ -n "$tmp" ]] && rm -f "$tmp"
    return "$rc"
}

################################################################################
# Dépendances ComfyUI SANS PyTorch — utilisée UNIQUEMENT à la CONSTRUCTION de
# l'image Docker pré-installée (Dockerfile), quand aucun GPU n'est visible et
# que le bon index CUDA (PYTORCH_BUILD_TABLE) ne peut donc pas être choisi.
# PyTorch est installé séparément, au DÉMARRAGE du conteneur, par
# install_pytorch() (appelée depuis docker-entrypoint.sh une fois le GPU
# réellement visible via nvidia-smi) — jamais dupliqué ici. install.sh/
# update.sh continuent d'utiliser exclusivement install_comfyui_requirements()
# ci-dessus (torch inclus), qui reste le SEUL chemin utilisé hors Docker.
################################################################################

install_comfyui_requirements_no_torch() {
    log_step "Installation des dépendances ComfyUI (sans PyTorch — image Docker)"

    local req="${INSTALL_DIR}/requirements.txt"
    [[ -f "$req" ]] || {
        log_error "requirements.txt introuvable dans ${INSTALL_DIR} (ComfyUI a-t-il bien été cloné ?)."
        exit 1
    }

    # On retire toute ligne torch/torchvision/torchaudio (avec ou sans
    # contrainte de version) du requirements.txt de ComfyUI avant de
    # l'installer, plutôt que de maintenir une copie séparée du fichier dans
    # ce dépôt : ${req} reste la seule source de vérité de ComfyUI pour ses
    # propres dépendances, seul le filtrage est fait ici.
    local req_no_torch
    req_no_torch="$(mktemp)"
    grep -viE '^[[:space:]]*(torch|torchvision|torchaudio)[[:space:]]*([<>=!~;].*)?$' "$req" > "$req_no_torch"

    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    retry "$DOWNLOAD_MAX_RETRIES" python -m pip install -r "$req_no_torch"
    deactivate
    rm -f "$req_no_torch"

    log_ok "Dépendances ComfyUI installées (PyTorch volontairement exclu à ce stade — voir docker-entrypoint.sh)."
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

    log_ok "Dépendances additionnelles (Hugging Face CLI, hf_xet...) installées."
}

################################################################################
# SageAttention — compilation depuis les sources (aucun wheel officiel ne
# couvre toutes les combinaisons torch/CUDA, cf. config.env pour le contexte
# complet). Idempotente comme install_pytorch() : si le module s'importe déjà
# dans le venv, on ne recompile rien. Best-effort et non bloquant : un échec
# ici ne doit jamais interrompre install.sh (les nœuds KJNodes qui en
# dépendent basculent simplement sur un chemin d'attention plus lent/plus
# gourmand en VRAM si le module est absent).
#
# IMPORTANT (cf. post-mortem régression cu118/SageAttention) : la source de
# vérité pour le toolkit de compilation est `torch.version.cuda` LU DANS LE
# VENV au moment de l'appel — jamais le runtime CUDA du pilote (nvidia-smi)
# ni une variable calculée plus tôt par install_pytorch(). La table
# PYTORCH_BUILD_TABLE redirige déjà volontairement certains runtimes pilote
# vers un index cu différent (ex : driver 12.8 -> torch cu126, cf. commentaire
# de la table) ; réutiliser le runtime pilote ici installerait un toolkit qui
# ne correspond pas au torch réellement installé, et SageAttention se
# compilerait contre le mauvais jeu d'en-têtes/bibliothèques CUDA — c'est
# exactement ce qui a produit l'échec observé (torch cu118 compilé avec un
# toolkit 12.4, deux branches MAJEURES différentes : 11 vs 12). On ne fait
# donc jamais confiance à un nvcc "trouvé dans le PATH" : sa version réelle
# (`nvcc --version`) est vérifiée AVANT de lancer quoi que ce soit.
#
# En revanche, la comparaison ne porte que sur la branche MAJEURE (12, 13...),
# pas sur la mineure exacte : NVIDIA garantit la "CUDA Minor Version
# Compatibility" (compiler/exécuter une extension avec un toolkit d'une
# mineure différente mais de la même majeure que celle de torch fonctionne).
# Exiger l'égalité stricte de la mineure a été tenté puis abandonné : le
# dépôt apt NVIDIA ne conserve en général que la dernière mineure connue
# d'une branche majeure (ex: `cuda-toolkit-13-0` peut disparaître dès que
# 13.1/13.2 sort, seul `cuda-toolkit-13` reste installable) — l'égalité
# stricte finissait donc par rejeter systématiquement un toolkit pourtant
# valide, empêchant toute compilation (wheel ET source) en boucle.
################################################################################

# _sage_find_matching_nvcc <torch_major> <candidate_cuda_home...>
# Cherche, parmi les répertoires candidats donnés, un toolkit CUDA versionné
# dont le nvcc rapporte la même branche MAJEURE que <torch_major> (la mineure
# n'a pas besoin de correspondre, cf. commentaire ci-dessus). Si trouvé,
# affiche sur stdout "chemin_cuda_home:version_nvcc_reelle" (ex.
# "/usr/local/cuda-13:13.2") et retourne 0 ; sinon retourne 1 sans rien
# afficher. Ne modifie jamais PATH/CUDA_HOME du shell appelant — se contente
# d'appeler chaque nvcc par son chemin absolu.
_sage_find_matching_nvcc() {
  local want_major="$1"; shift
  local d ver ver_major
  for d in "$@"; do
    [[ -x "${d}/bin/nvcc" ]] || continue
    ver="$("${d}/bin/nvcc" --version 2>/dev/null | grep -oE 'release [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -n1)"
    [[ -n "$ver" ]] || continue
    ver_major="$(cut -d. -f1 <<< "$ver")"
    if [[ "$ver_major" == "$want_major" ]]; then
      printf '%s:%s\n' "$d" "$ver"
      return 0
    fi
  done
  return 1
}

# _sage_ensure_nvidia_cuda_apt_repo
# Les paquets "cuda-toolkit-*" ne sont JAMAIS servis par les dépôts Ubuntu
# par défaut (archive.ubuntu.com/security.ubuntu.com) — seul le dépôt apt
# officiel NVIDIA (developer.download.nvidia.com) les fournit. L'image de
# base de ce projet est ubuntu:22.04 nue (voir Dockerfile), qui ne
# configure PAS ce dépôt : sans cette fonction, tout `apt-get install
# cuda-toolkit-X-Y` échoue systématiquement avec "Unable to locate
# package", quelle que soit la version demandée ou le nombre de tentatives
# (c'est un dépôt manquant, pas un nom de paquet incorrect ni un problème
# réseau transitoire — retry ne peut donc jamais corriger ça seul).
#
# Idempotent : si le dépôt est déjà configuré (pod avec une image
# nvidia/cuda de base, ou exécution précédente de cette fonction), ne fait
# rien. Best-effort et non bloquant comme le reste de ce fichier : en cas
# d'échec (offline, distro non reconnue...), log un warning et laisse
# l'appelant constater ensuite que cuda-toolkit reste introuvable via son
# propre repli habituel — ne fait jamais échouer l'installation globale.
_sage_ensure_nvidia_cuda_apt_repo() {
  require_cmd apt-get || return 0

  # Déjà configuré ? (paquet cuda-keyring installé, ou fichier de dépôt
  # NVIDIA déjà présent) — rien à faire.
  if dpkg -s cuda-keyring >/dev/null 2>&1 \
    || ls /etc/apt/sources.list.d/*cuda*.list >/dev/null 2>&1 \
    || ls /etc/apt/sources.list.d/*cuda*.sources >/dev/null 2>&1; then
    return 0
  fi

  if ! require_cmd curl && ! require_cmd wget; then
    log_warn "Ni curl ni wget disponibles — impossible de configurer le dépôt apt CUDA de NVIDIA."
    return 1
  fi

  # distro : "ubuntuMMYY" (ex: ubuntu2204), à partir de /etc/os-release.
  # arch   : x86_64 -> x86_64, aarch64/arm64 -> sbsa (nom utilisé par NVIDIA
  #          pour les dépôts CUDA ARM serveur).
  local distro="" ver_id="" arch="" nvidia_arch=""
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    ver_id="$(. /etc/os-release && echo "$VERSION_ID")"
  fi
  if [[ -z "$ver_id" ]]; then
    log_warn "Impossible de déterminer la version d'Ubuntu (/etc/os-release absent ou incomplet) — dépôt apt CUDA NVIDIA non configuré."
    return 1
  fi
  distro="ubuntu${ver_id//./}"

  arch="$(uname -m)"
  case "$arch" in
    x86_64) nvidia_arch="x86_64" ;;
    aarch64|arm64) nvidia_arch="sbsa" ;;
    *)
      log_warn "Architecture ${arch} non reconnue pour le dépôt apt CUDA NVIDIA — installation sautée."
      return 1
      ;;
  esac

  local keyring_url="https://developer.download.nvidia.com/compute/cuda/repos/${distro}/${nvidia_arch}/cuda-keyring_1.1-1_all.deb"
  local keyring_deb="/tmp/cuda-keyring_1.1-1_all.deb"

  log_info "Dépôt apt CUDA NVIDIA absent (paquets cuda-toolkit-* introuvables sans lui sur une image Ubuntu nue) — configuration depuis ${keyring_url}."

  local sudo_cmd=""
  [[ "$(id -u)" -ne 0 ]] && require_cmd sudo && sudo_cmd="sudo"

  if require_cmd curl; then
    retry "$DOWNLOAD_MAX_RETRIES" curl -fsSL -o "$keyring_deb" "$keyring_url" >>"$LOG_FILE" 2>&1
  else
    retry "$DOWNLOAD_MAX_RETRIES" wget -q -O "$keyring_deb" "$keyring_url" >>"$LOG_FILE" 2>&1
  fi

  if [[ ! -s "$keyring_deb" ]]; then
    log_warn "Échec du téléchargement de cuda-keyring (${distro}/${nvidia_arch}) — dépôt apt CUDA NVIDIA non configuré, cuda-toolkit-* restera introuvable."
    rm -f "$keyring_deb"
    return 1
  fi

  if ! $sudo_cmd dpkg -i "$keyring_deb" >>"$LOG_FILE" 2>&1; then
    log_warn "Échec de l'installation de cuda-keyring — dépôt apt CUDA NVIDIA non configuré, cuda-toolkit-* restera introuvable."
    rm -f "$keyring_deb"
    return 1
  fi
  rm -f "$keyring_deb"

  if ! $sudo_cmd apt-get update -y >>"$LOG_FILE" 2>&1; then
    log_warn "apt-get update a échoué après l'ajout du dépôt CUDA NVIDIA — on tente quand même la suite."
  fi

  log_ok "Dépôt apt CUDA NVIDIA configuré (${distro}/${nvidia_arch}) — cuda-toolkit-* devrait maintenant être installable."
  return 0
}

install_sageattention() {
  log_step "SageAttention (accélération / réduction VRAM des nœuds H3)"

  local mode="${SAGE_ATTENTION:-auto}"
  if [[ "$mode" == "false" ]]; then
    log_info "SAGE_ATTENTION=false — installation sautée."
    return 0
  fi

  # shellcheck disable=SC1091  # cf. note dans setup_python_venv.
  source "${VENV_DIR}/bin/activate"

  if python -c "import sageattention" 2>/dev/null; then
    log_ok "SageAttention déjà installé et importable — pas de recompilation."
    deactivate
    return 0
  fi

  local cc=""
  cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ')"

  if [[ "$mode" == "auto" ]]; then
    if [[ -z "$cc" ]] || ! awk -v c="$cc" -v m="$SAGEATTENTION_MIN_COMPUTE_CAP" 'BEGIN{exit !(c+0 >= m+0)}'; then
      log_info "SAGE_ATTENTION=auto, compute capability ${cc:-inconnue} < ${SAGEATTENTION_MIN_COMPUTE_CAP} (ou non détectée) — installation sautée."
      log_info "Forcez avec SAGE_ATTENTION=true dans config.env si vous voulez tenter quand même."
      deactivate
      return 0
    fi
    log_info "SAGE_ATTENTION=auto, compute capability ${cc} >= ${SAGEATTENTION_MIN_COMPUTE_CAP} — tentative d'installation."
  else
    log_info "SAGE_ATTENTION=true (forcé) — tentative d'installation (compute capability détectée : ${cc:-inconnue})."
  fi

  # --- 1) source de vérité : torch.version.cuda, lu dans le venv actif ----
  local torch_cuda=""
  torch_cuda="$(python -c 'import torch; print(torch.version.cuda or "")' 2>/dev/null)"
  if [[ -z "$torch_cuda" ]]; then
    log_warn "Impossible de lire torch.version.cuda dans le venv (torch non importable ou build CPU) — SageAttention sauté."
    deactivate
    return 0
  fi
  # Normalisation en major.minor (torch.version.cuda est déjà sous cette
  # forme en pratique, ex "12.6" — on se protège juste d'un éventuel suffixe).
  local torch_major torch_minor torch_cuda_norm
  torch_major="$(cut -d. -f1 <<< "$torch_cuda")"
  torch_minor="$(cut -d. -f2 <<< "$torch_cuda")"
  torch_cuda_norm="${torch_major}.${torch_minor}"
  log_info "Torch installé dans le venv : CUDA ${torch_cuda_norm} (torch.version.cuda) — c'est la seule référence utilisée ci-dessous pour choisir le toolkit de compilation."

  # --- 1bis) wheel pré-compilée dans l'image Docker, si présente ET cohérente
  # avec le torch réellement retenu (voir bake_sageattention_wheel() et
  # SAGEATTENTION_WHEEL_CACHE_DIR, config.env). Absente sur un pod nu sans
  # image Docker : on passe alors directement à la compilation habituelle
  # ci-dessous, comportement strictement inchangé dans ce cas.
  local baked_dir="${SAGEATTENTION_WHEEL_CACHE_DIR:-${PROJECT_ROOT}/.cache/sageattention-wheel}"
  local baked_index_file="${baked_dir}/cuda_index.txt"
  local current_index="cu${torch_major}${torch_minor}"
  if [[ -f "$baked_index_file" ]]; then
    local baked_index=""
    baked_index="$(cat "$baked_index_file" 2>/dev/null)"
    if [[ "$baked_index" == "$current_index" ]]; then
      local baked_whl=""
      baked_whl="$(find "$baked_dir" -maxdepth 1 -name '*.whl' 2>/dev/null | head -n1)"
      if [[ -n "$baked_whl" ]]; then
        log_info "Wheel SageAttention pré-compilée dans l'image trouvée (${baked_whl##*/}, ${baked_index}) — installation directe, sans compilation."
        if python -m pip install --quiet "$baked_whl" >>"$LOG_FILE" 2>&1 && python -c "import sageattention" 2>/dev/null; then
          log_ok "SageAttention installé depuis la wheel pré-compilée — aucune compilation nécessaire sur ce pod."
          deactivate
          return 0
        fi
        log_warn "Échec d'installation de la wheel pré-compilée (${baked_whl##*/}) — bascule sur la compilation depuis les sources ci-dessous."
        log_error_tail "installation de la wheel pré-compilée"
      fi
    else
      log_info "Wheel pré-compilée présente dans l'image mais pour un autre index CUDA (${baked_index:-inconnu} != ${current_index}, probablement un repli PREFER_CUDA130) — ignorée, compilation depuis les sources."
    fi
  fi

  # --- 2) toolkit CUDA cohérent avec torch_cuda_norm, PAS avec le driver --
  # Étape 2a : peut-être déjà présent tel quel (toolkits multiples possibles
  # sur l'image de base : on scanne explicitement tous les /usr/local/cuda-*
  # de la même branche majeure et on vérifie CHACUN par sa version réelle,
  # jamais par son seul nom de répertoire).
  local matched_cuda_home="" matched_nvcc_ver=""
  local _match=""
  _match="$(_sage_find_matching_nvcc "$torch_major" "/usr/local/cuda-${torch_cuda_norm}" /usr/local/cuda-"${torch_major}".*)" || true
  if [[ -n "$_match" ]]; then
    matched_cuda_home="${_match%%:*}"
    matched_nvcc_ver="${_match##*:}"
  fi

  if [[ -n "$matched_cuda_home" ]]; then
    log_ok "Toolkit CUDA branche ${torch_major}.x déjà présent et vérifié (${matched_cuda_home}/bin/nvcc, release ${matched_nvcc_ver}) — cohérent avec torch CUDA ${torch_cuda_norm}, pas de réinstallation."
  elif require_cmd apt-get; then
    local toolkit_pkg="cuda-toolkit-${torch_cuda_norm/./-}"
    local sudo_cmd=""
    [[ "$(id -u)" -ne 0 ]] && require_cmd sudo && sudo_cmd="sudo"
    _sage_ensure_nvidia_cuda_apt_repo || true
    log_info "Aucun nvcc de la branche CUDA ${torch_major}.x (build torch) trouvé — installation de ${toolkit_pkg} (peut prendre plusieurs minutes, ~3-4 Go)."
    wait_for_apt_lock
    if ! $sudo_cmd apt-get update -y >>"$LOG_FILE" 2>&1; then
      log_warn "apt-get update a échoué — on tente quand même l'installation du toolkit CUDA."
    fi
    if retry "$DOWNLOAD_MAX_RETRIES" $sudo_cmd apt-get install -y "$toolkit_pkg" >>"$LOG_FILE" 2>&1; then
      log_ok "${toolkit_pkg} installé — vérification de la version réelle de nvcc avant toute compilation."
    else
      # Repli : certaines versions mineures (ex: 12.7) ne publient pas de
      # paquet cuda-toolkit-<major>-<minor> dédié — le paquet majeur seul
      # (ex: cuda-toolkit-12) pointe alors vers la dernière mineure connue
      # de cette branche. Cette dernière mineure n'est PAS forcément celle
      # que torch attend : c'est vérifié explicitement juste après, jamais
      # supposé.
      local major_pkg="cuda-toolkit-${torch_major}"
      log_warn "${toolkit_pkg} indisponible — repli sur ${major_pkg} (la version réelle obtenue sera vérifiée avant de compiler, pas supposée correcte)."
      if ! retry "$DOWNLOAD_MAX_RETRIES" $sudo_cmd apt-get install -y "$major_pkg" >>"$LOG_FILE" 2>&1; then
        log_warn "Échec d'installation de ${toolkit_pkg} et ${major_pkg} — SageAttention sera sauté cette fois."
        log_error_tail "apt-get install ${toolkit_pkg} / ${major_pkg}"
        log_warn "H3 restera fonctionnel sans SageAttention, juste plus lent / plus gourmand en VRAM."
        deactivate
        return 0
      fi
      log_ok "${major_pkg} installé (repli) — vérification de la version réelle de nvcc avant toute compilation."
    fi
    # Étape 2b : re-scan après l'installation apt — jamais de confiance dans
    # le simple fait que `apt-get install` ait réussi : seule la version
    # réellement rapportée par ce nvcc précis fait foi (branche majeure
    # vérifiée, cf. commentaire d'en-tête de _sage_find_matching_nvcc).
    _match="$(_sage_find_matching_nvcc "$torch_major" "/usr/local/cuda-${torch_cuda_norm}" /usr/local/cuda-"${torch_major}".*)" || true
    if [[ -n "$_match" ]]; then
      matched_cuda_home="${_match%%:*}"
      matched_nvcc_ver="${_match##*:}"
    fi
  else
    log_warn "apt-get indisponible — recherche d'un toolkit déjà présent sur le pod de la branche CUDA ${torch_major}.x."
  fi

  if [[ -z "$matched_cuda_home" ]]; then
    log_warn "Aucun nvcc de la branche CUDA ${torch_major}.x (build torch : ${torch_cuda_norm}) n'a pu être installé ou localisé."
    log_warn "Compiler SageAttention avec un nvcc d'une autre branche MAJEURE produirait un échec opaque (ou pire, un module qui s'importe mais plante à l'exécution) — compilation annulée par précaution."
    log_warn "H3 reste pleinement fonctionnel sans SageAttention, juste plus lent / plus gourmand en VRAM. Si plusieurs toolkits sont installés sur ce pod, vérifiez /usr/local/cuda-*."
    deactivate
    return 0
  fi

  log_ok "nvcc cohérent sélectionné : ${matched_cuda_home}/bin/nvcc (release ${matched_nvcc_ver}) — même branche majeure que torch.version.cuda=${torch_cuda_norm} (compatibilité mineure garantie par NVIDIA)."
  log_info "CUDA_HOME/PATH pour la compilation seront positionnés localement au sous-shell de build (aucune modification permanente de l'environnement du script)."

  # --- Triton (dépendance de compilation/exécution) -----------------------
  # Jamais de `pip install triton` non versionné : le wheel torch installé
  # (cf. PYTORCH_BUILD_TABLE) déclare dans ses propres métadonnées une
  # contrainte de version précise sur Triton, et une version différente
  # installée par-dessus risquerait de casser des opérations déjà compilées
  # contre le Triton attendu par torch. On ne touche donc à Triton que s'il
  # est absent du venv, et uniquement avec la contrainte exacte que torch
  # lui-même déclare requérir (lue dynamiquement dans ses métadonnées, pas
  # figée en dur ici — reste correcte quel que soit le build torch retenu
  # par PYTORCH_BUILD_TABLE, cu118 comme cu130).
  if python -c "import triton" 2>/dev/null; then
    log_ok "Triton déjà présent et importable ($(python -c 'import triton; print(triton.__version__)' 2>/dev/null)) — pas de réinstallation."
  else
    local triton_req=""
    triton_req="$(python - <<'PYEOF'
from importlib.metadata import requires, PackageNotFoundError
try:
    reqs = requires("torch") or []
except PackageNotFoundError:
    reqs = []
for r in reqs:
    if r.split(";")[0].strip().lower().startswith("triton"):
        print(r.split(";")[0].strip())
        break
PYEOF
)"
    if [[ -n "$triton_req" ]]; then
      log_info "Triton absent — installation de la contrainte exacte requise par torch : ${triton_req}"
      if ! python -m pip install --quiet "$triton_req" >>"$LOG_FILE" 2>&1; then
        log_warn "Échec d'installation de ${triton_req} — la compilation de SageAttention risque d'échouer (non bloquant, consultez ${LOG_FILE})."
      fi
    else
      log_warn "Impossible de déterminer la contrainte Triton requise par torch dans ses métadonnées — Triton non installé (jamais d'installation non versionnée, cf. commentaire ci-dessus). La compilation de SageAttention risque d'échouer sans Triton."
    fi
  fi

  # --- clone / mise à jour dans un cache persistant ------------------------
  # mkdir peut échouer (permissions, disque) : ne doit pas faire sortir
  # install.sh entier sous `set -e`, seulement sauter cette étape (cf.
  # commentaire d'en-tête de la fonction : best-effort, non bloquant).
  if ! mkdir -p "$(dirname "$SAGEATTENTION_CACHE_DIR")" 2>>"$LOG_FILE"; then
    log_warn "Impossible de créer $(dirname "$SAGEATTENTION_CACHE_DIR") — SageAttention sauté (consultez ${LOG_FILE})."
    deactivate
    return 0
  fi
  if [[ -d "${SAGEATTENTION_CACHE_DIR}/.git" ]]; then
    if [[ "${SAGEATTENTION_UPDATE:-false}" == "true" ]]; then
      log_info "Cache SageAttention existant — mise à jour demandée (SAGEATTENTION_UPDATE=true, git pull)."
      git -C "$SAGEATTENTION_CACHE_DIR" pull --ff-only >>"$LOG_FILE" 2>&1 || \
        log_warn "git pull a échoué sur le cache existant — on compile la version déjà présente."
    else
      log_info "Cache SageAttention existant — compilation de la version déjà clonée (pas de mise à jour automatique ; SAGEATTENTION_UPDATE=true dans config.env pour en forcer une)."
    fi
  else
    log_info "Clonage de SageAttention (${SAGEATTENTION_REPO})..."
    if ! retry "$DOWNLOAD_MAX_RETRIES" git clone "$SAGEATTENTION_REPO" "$SAGEATTENTION_CACHE_DIR" >>"$LOG_FILE" 2>&1; then
      log_warn "Échec du clonage de SageAttention — installation sautée (consultez ${LOG_FILE})."
      deactivate
      return 0
    fi
  fi

  # --- compilation ----------------------------------------------------------
  # IMPORTANT : le sous-shell est combiné à l'assignation de build_rc en une
  # seule liste OR ("(...) || build_rc=$?"). Un sous-shell exécuté comme
  # instruction isolée propage son échec à `set -e` du shell appelant (donc
  # ferait sortir tout install.sh) ; placé comme premier membre d'une liste
  # OR, son échec est exempté (seul l'échec du DERNIER membre compte pour
  # errexit — ici une simple affectation, qui ne peut pas échouer).
  log_info "Compilation de SageAttention (10-20 min selon le pod, MAX_JOBS=${SAGEATTENTION_BUILD_JOBS})..."
  local build_rc=0
  (
    cd "$SAGEATTENTION_CACHE_DIR" || exit 1
    # Variables passées en préfixe de LA SEULE commande qui en a besoin
    # (portée strictement limitée à cette invocation pip, jamais
    # "exportées" au sens shell du terme) plutôt qu'avec `export` séparé —
    # évite les faux positifs ShellCheck SC2030/SC2031 (qui suppose sinon
    # qu'on cherche à faire persister ces variables au-delà de ce
    # sous-shell, alors que le sous-shell n'existe ici que pour isoler le
    # `cd`, jamais exportées dans le shell parent d'install.sh/update.sh,
    # qui continue avec son PATH d'origine après le retour de
    # install_sageattention()).
    CUDA_HOME="$matched_cuda_home" \
    PATH="${matched_cuda_home}/bin:${PATH}" \
    EXT_PARALLEL=4 \
    NVCC_APPEND_FLAGS="--threads 8" \
    MAX_JOBS="$SAGEATTENTION_BUILD_JOBS" \
    pip install -e . --no-build-isolation
  ) >>"$LOG_FILE" 2>&1 || build_rc=$?

  if [[ "$build_rc" -ne 0 ]]; then
    log_warn "Échec de compilation de SageAttention (code ${build_rc}) — installation sautée."
    log_error_tail "compilation SageAttention"
    log_warn "H3 restera fonctionnel sans SageAttention, juste plus lent / plus gourmand en VRAM."
    deactivate
    return 0
  fi

  local import_err=""
  if import_err="$(python -c "import sageattention" 2>&1 >/dev/null)"; then
    log_ok "SageAttention compilé et importable."
  else
    log_warn "Compilation terminée sans erreur mais le module ne s'importe pas — installation considérée en échec."
    echo -e "${C_RED}${C_BOLD}----- Erreur à l'import de sageattention -----${C_RESET}" >&2
    while IFS= read -r _line; do echo "    ${_line}"; done <<<"$import_err" >&2
    echo -e "${C_RED}${C_BOLD}----- Fin -----${C_RESET}" >&2
  fi

  deactivate
  return 0
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
