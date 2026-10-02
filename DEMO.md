# Guide de démonstration

Tout ce qu'il faut pour une démonstration de 3 minutes qui ne tourne pas mal.
À lire une fois, à exécuter la veille.

---

## 1. La veille

```bash
python tools/check_setup.py
```

Il doit afficher `READY`. Trois notes sont normales et sans conséquence :

- `HuggingFace token … already cached` — le jeton ne sert qu'au premier
  téléchargement ;
- `Server … not answering` — il n'est simplement pas démarré ;
- `Port 8000 … free` — rien ne tourne encore.

### Préparer la vidéo de démonstration

Créez **deux** travaux à l'avance. Une démonstration live ne doit dépendre
d'aucun calcul en direct.

1. Une vidéo **courte** (10 à 20 s), une seule voix → transcription en ~10 s,
   démonstration rapide.
2. La vidéo **portrait** (720×1280) → montre l'adaptation au format.

Puis lancez l'export du travail 1 et laissez-le se terminer. Vous aurez ainsi,
à l'ouverture de la session, un travail déjà transcrit **et** un MP4 déjà
généré.

> **Ordre des travaux dans « Vos vidéos » :** du plus récent au plus ancien.
> Le travail pré-exporté sera donc **en second**. C'est volontaire : cela montre
> la liste de plusieurs vidéos, et vous Demonstratez en ouvrant la seconde.
> Si vous préférez l'inverse, renommez les fichiers source avant le dépôt.

### Repérer la barre d'onglet

Chrome affiche le titre d'onglet quand on change d'onglet, par exemple
`video (2)`. Parce que l'export se fait **en direct dans l'onglet**, s'éloigner
pendant l'export peut le ralentir ou l'interrompre.

---

## 2. Le déroulé — 3 minutes

| Temps | Écran | Ce que vous dites | Ce que vous faites |
|---|---|---|---|
| 0:00 | Accueil | « Voici notre projet : transformer une vidéo en support de cours accessible. » | Montrer la page d'accueil, les 3 entrées 3D et la section **Video Captions** |
| 0:20 | Intake | « L'enseignant dépose une vidéo ou un lien. » | Ouvrir `captions.html`, montrer la liste **Vos vidéos** et le glisser-déposer |
| 0:45 | Aperçu | « Regardez : les mots s'allument au moment où ils sont prononcés. » | Ouvrir le travail court, **Play**, laisser 5 s |
| 1:15 | Édition | « Et si la reconnaissance se trompe, l'enseignant corrige. » | Modifier un mot dans **Transcription**, montrer la correction immédiate à l'écran |
| 1:45 | Export | « Le résultat est une vidéo, pas un fichier de sous-titres. » | Ouvrir le travail **pré-exporté** : le lecteur et le bouton **Télécharger le mp4** |
| 2:20 | Preuve | « Le mot actif est le seul surligné — vérifié par un test automatisé. » | Ouvrir `/api/health`, puis `GET /api/caption-jobs/<id>/export-status` → `"ready": true` |
| 2:40 | Fin | « Les données ne quittent jamais la machine : la reconnaissance est locale. » | Pointer `HF_TOKEN` dans `.env.example`, sans jamais ouvrir le vrai `.env` |

### Ce qu'il ne faut PAS faire en direct

- **Ne transcrivez rien pour la première fois.** Si les modèles ne sont pas en
  cache, le premier traitement bloque le navigateur 5 à 10 minutes. Vérifiez avec
  `check_setup.py` qu'ils le sont.
- **Ne lancez pas un export de 3 minutes.** L'export est **en temps réel** : il
  dure exactement la longueur du clip. Exportez le travail court (10 s).
- **Ne montrez pas la capture de la section 3D.** Le décor n'est pas finalisé et
  la capture 3D du README le montre. Restez sur les sous-titres, qui sont le
  travail que vous défendez.
- **N'ouvrez pas `.env`.** Montrez `.env.example` et expliquez qu'il contient un
  jeton. Si un jury le demande, dites simplement qu'il n'est jamais versionné.

---

## 3. Le filet de sécurité

Trois niveaux de repli, du plus simple au plus échec :

### Niveau 1 — Le mode démonstration (aucune installation)

`captions.html` **sans aucun travail ouvert** charge la fixture
`captions_demo/captions.json` et fait tourner l'horloge sur une horloge
synthétique. Aucune vidéo, aucun modèle, aucune transcription : les sous-titres
s'affichent et le mot actif s'allume.

> C'est l'argument le plus fort du projet pour la démonstration : **il
> fonctionne même sans réseau et sans GPU.**

### Niveau 2 — Les captures du README

`docs/screenshots/` contient 10 captures réelles, générées par
`node tools/capture_screenshots.mjs`. Si le Wi-Fi de la salle tombe, vous
projetez les captures — ce sont de vraies images du logiciel, pas des maquettes.

### Niveau 3 — La vidéo déjà exportée

Le travail pré-exporté contient `export.mp4`. Vous pouvez la lire directement
depuis un lecteur, sans navigateur ni serveur.

---

## 4. Les questions probables du jury

| Question | Réponse courte et honnête |
|---|---|
| « Les données partent-elles sur Internet ? » | Non. WhisperX tourne en local ; le code de transcription ne fait aucun appel réseau. Seul le mode « Génération IA » de la chaîne 3D utilise une API, et il est séparé. |
| « Comment les élèves malentendants sont-ils pris en charge ? » | Les sous-titres sont **incrustés dans le fichier** : la ressource reste utilisable sans le son. |
| « Et les élèves en difficulté de lecture ? » | Le mot actif crée un lien explicite entre le son et le texte : c'est un appui à la lecture et à la prononciation. |
| « Que se passe-t-il si l'export échoue ? » | La page l'affiche au lieu de faire semblant. Aucun fichier partiel n'est proposé : l'encodage écrit dans un fichier temporaire puis renomme. |
| « C'est mesuré ? » | 273 tests automatisés, dont la garantie « un seul mot surligné à la fois ». La partie classroom est une campagne que nous documentons dans le README §10. |
| « Un hébergeur gratuit suffirait-il ? » | Non : le worker de transcription atteint 1,76 Go de mémoire (mesuré) et les modèles pèsent 879 Mo. Le README §1 du déploiement compare les offres. |
| « Pourquoi 3D *et* sous-titres ? » | Les deux sortent de la même brique (reconnaissance vocale + diarisation). Le sous-titre sert l'accessibilité ; le 3D sert la motivation. |

---

## 5. Après la démonstration

- Récupérez `jobs_captions/` si vous voulez conserver les travaux.
- `python -m pytest app/tests -q` doit afficher 273 tests verts.
- Vérifiez que rien de confidentiel n'a été copié dans `media/`.
