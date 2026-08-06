\# Troubleshooting



\---



\# CUDA Out Of Memory



Reduce:



\- Resolution

\- Number of Frames

\- Steps



or use a GPU with more VRAM.



\---



\# Missing Models



Run



```bash

bash install.sh

```



The installer automatically repairs missing files.



\---



\# Hugging Face Error



Verify:



\- License accepted

\- Token validity

\- Internet connection



\---



\# Workflow asks to download models



Usually the models are installed in the wrong folder.



Verify:



```

ComfyUI/models/diffusion\_models/

ComfyUI/models/text\_encoders/

ComfyUI/models/vae/

```



\---



\# Download interrupted



Simply run



```bash

bash install.sh

```



Downloads resume automatically.



\---



\# LoRA not visible



Restart ComfyUI.



Verify that the LoRA is located inside



```

ComfyUI/models/loras/

```



\---



\# ComfyUI won't start



Run



```bash

bash check.sh

```



Verify:



\- CUDA

\- PyTorch

\- GPU

\- Disk space



\---



\# Hugging Face license



Accept the MiniMax H3 license:



https://huggingface.co/Comfy-Org/MiniMax-H3



\---



\# Disk Full



Use a larger RunPod volume.



The installer will continue where it stopped.



\---



\# CivitAI Download Failed



Verify:



\- URL

\- Internet connection

\- File availability



\---



\# Need more help?



Open a GitHub Issue and attach:



\- install.log

\- update.log

\- check.sh output

