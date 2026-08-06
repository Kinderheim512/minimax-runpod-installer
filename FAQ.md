\# Frequently Asked Questions



\---



\## Which GPU is recommended?



Recommended:



\- RTX A6000

\- RTX 6000 Ada

\- L40S

\- A100

\- H100



Minimum recommended VRAM:



48 GB



\---



\## Does MiniMax H3 require custom nodes?



No.



MiniMax H3 is natively supported by ComfyUI.



\---



\## Which workflows are installed?



\- Text to Video

\- Image to Video

\- Reference to Video



\---



\## Can I use CivitAI?



Yes.



The diffusion models can be downloaded from CivitAI.



\---



\## Can I install LoRAs?



Yes.



```bash

bash install\_lora.sh URL

```



\---



\## Where are LoRAs installed?



```

ComfyUI/models/loras/

```



\---



\## How do I update?



```bash

git pull



bash install.sh

```



\---



\## How do I repair missing models?



```bash

bash install.sh

```



The installer automatically downloads only missing files.



\---



\## Where are the workflows?



```

ComfyUI/user/default/workflows/

```



\---



\## Which Python version is used?



Python 3.11



\---



\## Can I interrupt the installation?



Yes.



Simply launch



```bash

bash install.sh

```



again.



Downloads resume automatically.



\---



\## Does the installer download models twice?



No.



Only missing or corrupted files are downloaded.



\---



\## Can I backup the models?



Yes.



Simply save the following folders:



\- diffusion\_models

\- text\_encoders

\- vae

\- loras



