# patches/sitecustomize.py — correctif LOCAL et TEMPORAIRE, déployé par
# lib/python.sh::install_comfy_kitchen_pep585_shim() dans le site-packages du
# venv ComfyUI (contrôlé par COMFY_KITCHEN_PEP585_SHIM dans config.env).
#
# À retirer dès que l'un des deux tickets amont suivants est résolu :
#   - pytorch/pytorch#146594 (torch.library.infer_schema ne reconnaît pas les
#     génériques PEP 585 comme `list[int]`, seulement `typing.List[int]`)
#   - Comfy-Org/comfy-kitchen publie une version dont les kernels eager
#     (na.py) n'utilisent plus ce style d'annotation
#
# Contexte complet (regression analysis, voir CHANGELOG.md) : ComfyUI est
# cloné sur `master` non épinglé ; son requirements.txt épingle
# `comfy-kitchen` par version exacte, bumpée plusieurs fois très récemment
# (0.2.20 -> 0.2.26). Un de ces bumps a introduit/révélé un paramètre annoté
# `list[int]` dans un `@torch.library.custom_op`, qui échoue à l'import avec :
#   ValueError: infer_schema(func): Parameter kernel_size has unsupported
#   type list[int]. The valid types are: dict_keys([... typing.List[int] ...])
#
# CE FICHIER NE MODIFIE NI torch, NI comfy_kitchen, NI ComfyUI. Il complète en
# mémoire, au démarrage de l'interpréteur (mécanisme standard `site` module,
# avant tout import applicatif), le dictionnaire
# torch._library.infer_schema.SUPPORTED_PARAM_TYPES : pour chaque entrée
# typing.List[X] déjà supportée, on ajoute l'alias PEP 585 list[X] pointant
# vers le même type de schéma — rien n'est codé en dur sur `kernel_size` ou
# `na3d` spécifiquement, donc ça couvre tout paramètre comfy_kitchen du même
# style, présent ou futur.
#
# RETRAIT : mettre COMFY_KITCHEN_PEP585_SHIM=false dans config.env puis
# relancer l'installateur, ou supprimer directement le fichier déployé dans
# le venv (aucune autre trace ailleurs) :
#   rm "$(source venv/bin/activate && python -c \
#     'import sysconfig; print(sysconfig.get_paths()["purelib"])')/sitecustomize.py"

try:
    import typing
    from types import GenericAlias

    import torch._library.infer_schema as _infer_schema

    _SUPPORTED = _infer_schema.SUPPORTED_PARAM_TYPES
    _bridged = 0

    for _typing_key, _schema_str in list(_SUPPORTED.items()):
        _origin = typing.get_origin(_typing_key)
        if _origin is list:
            _args = typing.get_args(_typing_key)
            if _args:
                _builtin_key = GenericAlias(list, _args)
                if _builtin_key not in _SUPPORTED:
                    _SUPPORTED[_builtin_key] = _schema_str
                    _bridged += 1

    if _bridged:
        import logging

        logging.getLogger("comfy_kitchen_pep585_shim").info(
            "Shim PEP585 actif : %d alias list[...] ajoutés à "
            "torch.library.infer_schema.SUPPORTED_PARAM_TYPES "
            "(contournement temporaire, cf. pytorch/pytorch#146594). "
            "Désactivable via COMFY_KITCHEN_PEP585_SHIM=false dans config.env.",
            _bridged,
        )

except Exception:
    # Ne jamais faire échouer le démarrage de Python à cause de ce correctif
    # best-effort : si torch n'est pas encore importable à ce stade, ou si sa
    # structure interne a changé entre versions, on laisse simplement passer.
    # Pire cas : comfy_kitchen échoue avec son erreur normale, pas pire qu'en
    # l'absence de ce fichier.
    pass
