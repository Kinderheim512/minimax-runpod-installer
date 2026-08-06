\# Guide d'installation



Ce guide explique comment installer automatiquement \*\*ComfyUI + MiniMax H3\*\* sur un RunPod neuf.



\---



\# Prérequis



Avant de commencer, vous devez disposer de :



\- un compte RunPod ;

\- un compte Hugging Face ;

\- avoir accepté la licence MiniMax H3.



https://huggingface.co/Comfy-Org/MiniMax-H3



Vous aurez également besoin d'un token Hugging Face avec le droit \*\*Read\*\*.



\---



\# GPU recommandés



| GPU | Statut |

|------|--------|

| RTX A6000 | ✅ Recommandé |

| RTX 6000 Ada | ✅ Recommandé |

| L40S | ✅ Recommandé |

| A100 80GB | ✅ |

| H100 | ✅ |



VRAM minimale recommandée :



\*\*48 Go\*\*



\---



\# Étape 1 — Créer un RunPod



Créez un nouveau Pod.



Template recommandé :



\- PyTorch

\- CUDA 12.x

\- Ubuntu



Exposez le port :



8188 (HTTP)



\---



\# Étape 2 — Ouvrir le terminal



Exécutez :



```bash

cd /workspace



git clone https://github.com/Kinderheim512/minimax-runpod-installer.git



cd minimax-runpod-installer



bash bootstrap.sh

```



\---



\# Étape 3 — Attendre



L'installateur :



\- détecte automatiquement votre GPU ;

\- crée un environnement virtuel Python ;

\- installe PyTorch ;

\- installe ComfyUI ;

\- installe ComfyUI Manager ;

\- installe VideoHelperSuite ;

\- crée tous les dossiers de modèles ;

\- télécharge MiniMax H3 ;

\- installe les workflows officiels ;

\- optimise automatiquement ComfyUI ;

\- lance ComfyUI.



Aucune configuration manuelle n'est nécessaire.



\---



\# Étape 4 — Hugging Face



Si nécessaire, saisissez votre token Hugging Face.



L'installateur vérifie automatiquement que vous avez accepté la licence MiniMax H3.



\---



\# Étape 5 — Ouvrir ComfyUI



Ouvrez votre navigateur.



L'adresse de votre Pod ressemble à :



https://VOTRE-POD-ID-8188.proxy.runpod.net



\---



\# Workflows inclus



L'installateur installe automatiquement :



\- Text to Video

\- Image to Video

\- Reference to Video



Aucune importation manuelle n'est nécessaire.



\---



\# Installer un LoRA



Téléchargez directement un LoRA :



```bash

bash install\_lora.sh "VOTRE\_URL"

```



Exemple :



```bash

bash install\_lora.sh "https://civitai.red/api/download/models/XXXX?fileId=XXXX"

```



Le LoRA sera automatiquement installé dans :



```

ComfyUI/models/loras/

```



\---



\# Mise à jour



Pour mettre le projet à jour :



```bash

git pull



bash install.sh

```



Seuls les fichiers manquants seront téléchargés.



Les modèles déjà présents sont automatiquement ignorés.



\---



\# Vérifier l'installation



```bash

bash check.sh

```



Le script vérifie :



\- le GPU ;

\- CUDA ;

\- PyTorch ;

\- les modèles MiniMax ;

\- l'espace disque ;

\- les workflows.



\---



\# Dépannage



\## Modèles manquants



Exécutez :



```bash

bash install.sh

```



L'installateur réparera automatiquement les modèles manquants.



\---



\## Erreur CUDA Out Of Memory



Réduisez :



\- la résolution ;

\- le nombre de frames ;

\- le nombre de steps.



Ou utilisez un GPU disposant de davantage de VRAM.



\---



\## Erreur Hugging Face



Vérifiez :



\- que la licence a bien été acceptée ;

\- que votre token est valide.



\---



\# Support



Si ce projet vous est utile,



n'hésitez pas à lui attribuer une ⭐ sur GitHub.

