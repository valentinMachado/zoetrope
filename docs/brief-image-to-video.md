# Projet : animation image → vidéo en local

Brief de contexte pour reprendre le projet. Rien n'est encore installé ni codé.

## Objectif

Un programme local qui prend **une image fixe** (photo réaliste, aquarelle, dessin) **+ un prompt** et produit une vidéo animée. Usage strictement personnel, aucun déploiement prévu.

## Matériel

- **GPU : à vérifier en priorité** (`nvidia-smi`). Probablement une GTX 1080 8 Go. Toutes les décisions ci-dessous partent de cette hypothèse — si c'est du 20xx/30xx/40xx, revoir les choix de modèle.
- 32 Go de RAM système (suffisant pour l'offload CPU du text encoder)
- Windows, Python + ffmpeg déjà en place

Contrainte Pascal : pas de fp8, pas de bf16, fp16 castré (1/64 du débit fp32). Tout tombe en fp32 → lent. Compter 30-60 min pour 3-5 s en 480p.

## Choix de modèle

| Option | Statut |
|---|---|
| **Wan 2.2 TI2V-5B** | Point de départ. Tient sur 8 Go sans quantization. |
| **Wan 2.2 14B GGUF Q4/Q5 + T5-XXL offload CPU** | Meilleure qualité, ~8 Go de VRAM, nécessite les 32 Go de RAM. Lent sur Pascal. |
| **[Wan2GP](https://github.com/deepbeepmeep/Wan2GP)** | Toolbox autonome, supporte explicitement les GTX 10XX. Chemin le plus court pour un premier rendu. |

## Approche retenue pour les séquences longues

**Keyframes + interpolation FLF2V** — pas de chaînage naïf de la dernière frame.

1. Générer N images fixes (image de départ + étapes clés du plan) avec un modèle d'image
2. Faire du **first-last-frame-to-video** entre chaque paire consécutive
3. Concaténer

Chaque segment est ancré aux deux extrémités, donc la dérive est bornée par construction au lieu de s'accumuler. Bonus : contrôle narratif, et un segment raté se relance seul.

Rejeté : réutiliser la dernière frame comme input du segment suivant. Perte de génération à chaque aller-retour VAE + dérive sémantique (le style glisse, l'aquarelle se photoréalise) + la dernière frame est souvent la pire du clip.

## Style

Le style est porté par l'image d'entrée, donc pas besoin d'entraîner quoi que ce soit au départ.

Ordre à respecter avant d'envisager un LoRA :
1. Tester sans LoRA sur des clips de 2-3 s, mesurer la dérive
2. Chercher un LoRA de style existant sur Civitai
3. Seulement si rien ne colle : entraîner

Si entraînement : LoRA de **style sur images fixes** (pas sur vidéo), 20-40 images légendées. Hors de portée du 1080 en local (24-48 Go de VRAM selon les configs). Coût unique de ~4 $ sur fal.ai, ou Colab T4 gratuit pour le 5B. Le fichier produit (50-200 Mo) s'utilise ensuite en local indéfiniment.

## Architecture cible

```
Front web (upload image + prompt + params + galerie)
      ↓ HTTP
Backend : queue SQLite + worker
      ↓ API REST ComfyUI (/prompt, workflow JSON templaté)
ComfyUI headless (--listen)
      ↓
ffmpeg (color-match, concat, interpolation RIFE, encode)
```

**Asynchrone obligatoire.** Avec 30-60 min par clip, une UI temps réel est inutilisable. Le modèle d'usage est : préparer une file de jobs le soir → ça tourne la nuit → trier les résultats le matin.

### Modèle de données

```
job {
  id, status, created_at
  segments: [
    { prompt, keyframe_in, keyframe_out, duration, seed, status }
  ]
  preset: { style, lora, lora_weight, resolution, steps, cfg }
}
```

### À prévoir dès le départ

- **Stocker systématiquement la seed** de chaque segment (rejouer un résultat qui marche)
- **Mode preview** : 256px, 8 steps, ~2 min — valider le mouvement avant le rendu final
- **Relance par segment** sans refaire tout le job
- Dossier `loras/` piloté par preset
- **Color-match** de chaque segment sur le premier avant concat (histogram matching ou ffmpeg) — la dérive colorimétrique est la plus visible et la plus facile à corriger en post

## Ordre de travail suggéré

1. `nvidia-smi` → confirmer le GPU
2. Installer Wan2GP, générer un premier clip pour mesurer les temps réels
3. Si ça tient : installer ComfyUI headless, récupérer un workflow FLF2V, le tester à la main
4. Templater le workflow JSON, écrire le client API
5. Queue + worker
6. Front
