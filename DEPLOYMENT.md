# Déploiement

Ce document explique comment installer et faire tourner le projet, du poste de
l'enseignant au serveur de l'établissement.

---

## 1. Choisir son scénario

| Scénario | Pour qui | Difficulté | Coût |
|---|---|---|---|
| **[A. Poste de l'enseignant](#a-poste-de-lenseignant)** | Un professeur, une machine | ⭐ Facile | 0 |
| **[B. Serveur de l'établissement](#b-serveur-de-létablissement)** | Toute l'école, réseau local | ⭐⭐ Moyen | 0 |
| **[C. Serveur distant (VPS)](#c-serveur-distant-vps)** | Accès depuis chez soi, plusieurs utilisateurs | ⭐⭐⭐ Avancé | ~5 €/mois |
| **[D. Docker](#d-docker)** | Tous les cas, si Docker est connu | ⭐⭐ Moyen | 0 |

> **Recommandation pour une démonstration de concours : le scénario A.**
> Aucune donnée ne quitte la machine, ce qui répond directement au critère
> « protection des données », et il n'y a rien à administrer.

---

## 2. Prérequis communs

### 2.1 Le seul vrai obstacle : le jeton Hugging Face

La séparation des locuteurs utilise
`pyannote/speaker-diarization-community-1`, un dépôt **gated** : il ne peut pas
être téléchargé anonymement.

Sans jeton, **le reste fonctionne** (la transcription est correcte), mais toutes
les lignes sont attribuées à un seul locuteur et le code couleur par personne
n'a plus de sens.

1. Créer un compte gratuit sur <https://huggingface.co>
2. Ouvrir <https://huggingface.co/pyannote/speaker-diarization-community-1>
   et cliquer **Agree**
3. Créer un jeton **read** sur <https://huggingface.co/settings/tokens>

```bash
cp .env.example .env      # puis remplacer hf_your_token_here
```

### 2.2 Ressources

| | Minimum | Recommandé |
|---|---|---|
| RAM | 8 Go | 16 Go |
| Disque | 15 Go | 30 Go (modèles ~6 Go + vidéos) |
| CPU | 4 cœurs | 8 cœurs |
| GPU | — | Optionnel, gros gain sur la chaîne 3D |

**Sans GPU, tout fonctionne.** La chaîne de sous-titres est en CPU
(`faster-whisper`, modèle `small`). Comptez **15 à 30 s pour 30 s de vidéo** sur
un quadruple cœur.

### 2.3 Navigateur

L'export vidéo utilise l'API `MediaRecorder`, disponible uniquement sur
**Chrome** et **Edge**. La saisie, la transcription et l'aperçu fonctionnent
partout.

---

## A. Poste de l'enseignant

### Installation

```bash
git clone <url-du-dépôt>
cd ai-animation-pipeline

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File tools/fetch-dependencies.ps1
cp .env.example .env            # puis renseigner HF_TOKEN
```

### Lancement

Double-clic sur **`launch.bat`**, ou :

```bash
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Puis <http://127.0.0.1:8000/>.

Le premier lancement télécharge les modèles : **comptez 5 à 10 minutes**.
Ensuite c'est immédiat.

### Au quotidien

- `launch.bat` sert aussi de **rechargement** : il arrête l'instance sur le
  port 8000 et la relance avec le code à jour.
- Les travaux sont dans `jobs_captions/cap_*/`. Un vidéo et son sous-titrage
  d'origine : **supprimez le dossier**.
- Les journaux : `uvicorn.serve.log`, et `worker.log` par travail.

---

## B. Serveur de l'établissement

Même installation que A, avec trois différences.

### 1. Écouter sur le réseau local

```bash
python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000
```

Les autres postes du réseau ouvrent alors `http://ADRESSE-IP-DU-SERVEUR:8000/`.

> ⚠️ **Cette application n'a aucune authentification.** N'exposez pas le port
> 8000 sur Internet. Sur un réseau scolaire fermé, c'est acceptable ; sinon,
> placez un reverse-proxy avec identifiants devant (voir §5).

### 2. Démarrage automatique (Windows)

```powershell
# needs admin
New-ScheduledTaskAction  -Execute "python" `
  -Argument "-m uvicorn app.api.main:app --host 0.0.0.0 --port 8000" `
  -WorkingDirectory "C:\aipipeline"
New-ScheduledTaskTrigger -Daily -At 8am
Register-ScheduledTask -TaskName "AIPipeline" `
  -Action (New-ScheduledTaskAction ...) -Trigger (New-ScheduledTaskTrigger ...)
```

### 3. Sauvegardes

À sauvegarder : `jobs_captions/` (travaux) et `.env` (le jeton).
Les modèles se retéléchargent ; ne pas les sauvegarder.

---

## C. Serveur distant (VPS)

Utile pour travailler depuis chez soi. Un VPS **4 Go de RAM minimum**.

### Avec Docker, c'est le plus simple

```bash
scp -r . user@votre-serveur:/opt/aipipeline
ssh user@votre-serveur
cd /opt/aipipeline
cp .env.example .env && nano .env       # renseigner HF_TOKEN
docker compose up -d
```

Accès : `http://ADRESSE-IP:8000/`

### Recommandations de sécurité

L'application n'a pas d'authentification. Sur un VPS public, mettez **Caddy**
ou **nginx** devant :

```nginx
server {
    listen 80;
    server_name captions.votre-domaine.tn;

    location / {
        auth_basic "Pipeline";
        auth_basic_user_file /etc/nginx/.htpasswd;

        # L'export vidéo est long : ne coupez pas la requête.
        proxy_read_timeout 3600s;
        client_max_body_size 200M;

        proxy_pass http://127.0.0.1:8000;
    }
}
```

Puis `sudo htpasswd -c /etc/nginx/.htpasswd enseignant`.

> **Le timeout est indispensable** : une vidéo de 3 minutes passe 3 minutes à
> être capturée dans le navigateur, et une requête Nginx coupée à 60 s par
> défaut interromprait l'export.

---

## D. Docker

```bash
cp .env.example .env      # renseigner HF_TOKEN
docker compose up -d
docker compose logs -f
```

Ce que le `Dockerfile` fait, et pourquoi :

| Décision | Raison |
|---|---|
| `CMD` lance `uvicorn`, pas `--help` | L'ancien `CMD` affichait une aide et quittait : l'image ne démarrait jamais l'application. |
| Le front (`*.html`) est copié dans l'image | L'application sert `index.html` / `captions.html` depuis le disque. Sans copie, toutes les pages renvoient 404. |
| `.env` **n'est pas** copié | Un jeton dans une couche d'image est lisible par quiconque tire l'image. Il passe par variable d'environnement à l'exécution. |
| Les modèles ne sont pas téléchargés au build | Le modèle de diarisation est *gated* : impossible sans jeton. Ils arrivent dans le volume `/models` au premier usage. |
| `USER appuser` | L'image ne tourne pas en root. |
| `HEALTHCHECK` sur l'API | `docker ps` reflète la réalité du service. |
| `shm_size: 1gb` | PyTorch a besoin de `/dev/shm` plus grand que le défaut de 64 Mo. |

---

## 5. Points d'attention en déploiement

| Sujet | Détail |
|---|---|
| **Pas d'authentification** | À protéger soi-même (reverse-proxy) avant toute exposition réseau. |
| **Export = temps réel** | Une vidéo de 3 min monopolise l'onglet 3 min. `proxy_read_timeout` doit être allongé. |
| **Export = navigateur Chromium** | Sur Firefox/Safari, la transcription et l'aperçu fonctionnent, l'export non. |
| **Espace disque** | Chaque travail = vidéo source + WAV + export ≈ 3× la taille. |
| **Concurrence** | Le worker de transcription est un sous-processus par travail. En Usage intensif, mettre une file d'attente. |
| **Sauvegarde** | `jobs_captions/` est la seule donnée irremplaçable. |

---

## 6. Diagnostic

| Symptôme | Cause probable | Solution |
|---|---|---|
| `Directory 'assets' does not exist` au démarrage | Clone incomplet | Les répertoires sont créés au démarrage ; vérifier que `main.py` est bien à jour. |
| Un seul locuteur dans les sous-titres | `HF_TOKEN` absent ou conditions non acceptées | Reprendre § 2.1. |
| L'export ne démarre pas | Navigateur non-Chromium | Utiliser Chrome ou Edge. |
| L'export s'arrête à 60 s | Timeout du proxy | Allonger `proxy_read_timeout`. |
| Le premier traitement dure très longtemps | Téléchargement des modèles | Normal, 5–10 min. Voir `worker.log`. |
| `ffmpeg not found` | `fetch-dependencies.ps1` non lancé, ou FFmpeg absent du `PATH` | Lancer le script, ou installer FFmpeg. |
| Page blanche / 404 | Interface non copiée (Docker) | Reconstruire l'image. |

### Vérifier que tout est en place

```bash
python -m pytest app/tests -q          # 273 tests, aucune donnée requise
curl http://127.0.0.1:8000/api/caption-jobs
ffmpeg -version
```

---

## 7. Et si on ne veut rien déployer du tout ?

Pour une démonstration, le projet fonctionne tel quel sur le poste de
l'enseignant (scénario A). Le déploiement serveur n'est utile que si plusieurs
personnes doivent l'utiliser, ou pour travailler hors de l'établissement.

C'est un choix défendable, et même préférable pour la protection des données
pédagogiques.
