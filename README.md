# AnimeTech

Pipeline automatique **source → détection → file → téléchargement → validation → miniature → Telegram → nettoyage**, publié dans un canal Telegram via un serveur *Local Bot API* (Docker), avec un panneau web d'administration et un bot Telegram d'administration.

> Projet personnel, en français. Les secrets ne sont **jamais** dans le dépôt : uniquement dans des fichiers `.env` ignorés par Git (voir [Sécurité](#sécurité)).

## Ce que fait le programme

- **Surveille tout le site source** toutes les 30 minutes : lit le flux « derniers épisodes » (accueil + pages suivantes) et le contrôle de chaque anime configuré. Tout épisode **sorti aujourd'hui**, même d'un anime encore inconnu, est ajouté puis publié ; les épisodes d'hier et plus anciens ne le sont jamais. Aucun doublon (identité = clé d'épisode canonique).
- **Télécharge** jusqu'à 3 épisodes en parallèle, dans l'ordre de détection ; dès qu'un téléchargement est fini, le suivant démarre. Un même anime reste dans l'ordre E01 → E02.
- **Valide** (ffprobe : intégrité, durée, codecs), génère la **miniature** et la fiche de l'anime, puis **publie** dans le canal : miniature puis vidéo, **un épisode à la fois**, dans l'ordre d'arrivée. Fichiers jusqu'à ~1,8 Go (serveur Local Bot API patché).
- **Résiste aux pannes** : reprise après arrêt brutal sans republication, nouvelles tentatives silencieuses (fenêtre de 24 h), un seul signalement si une vidéo reste inaccessible plus de 20 minutes, repli sur un autre lecteur vidéo de la page quand le lecteur par défaut échoue.
- **Nettoie** les fichiers locaux 14 jours après la publication (les messages Telegram ne sont jamais supprimés).
- **Panneau web** (`127.0.0.1:8085`) : vue d'ensemble, épisodes, file, anime, erreurs, capacités, santé, réglages ; démarrage/arrêt propre du worker.
- **Bot Telegram d'administration** (bot séparé, chats privés uniquement) : mêmes écrans, boutons d'action, notifications (épisode publié, nouvel épisode, problèmes, résumé quotidien).

## Organisation du dépôt

| Dossier | Rôle |
|---|---|
| `v2_automation/` | Le programme de production (worker, détection, file, téléchargement, publication, panneaux). |
| `v1_poc/` | Premier prototype validé : client source, manifestes HLS, téléchargeur, validateur ffprobe, client Telegram (réutilisés par la V2). |
| `source_audit/` | Audit de la source : analyseurs de pages (accueil, anime, épisode, lecteur), identité des épisodes, détection de changement de structure. |
| `v2_local_bot_api_poc/` | Serveur Telegram *Local Bot API* en Docker (`Dockerfile`, `docker-compose.yml`, scripts d'essais de gros fichiers). |
| `V1_FINAL_E2E_REPORT.md`, `V1_REAL_E2E_REPORT.md` | Rapports de validation de bout en bout (preuves, limites, décisions). |

Détail de `v2_automation/src/v2_automation/` : `discovery.py` (cycle global de 30 min), `site_feed.py` (flux du site), `release_date.py` (dates « il y a 3 minutes » / « 12 septembre 2026 »), `queues.py` / `repo.py` (SQLite, FIFO par anime), `downloader.py` (machine d'états, porte de publication), `players.py` (lecteurs de repli), `worker.py` (verrou unique, arrêt propre), `recovery.py`, `cleanup.py`, `alerts.py`, `notifier.py`, `web.py` + `web_ui/` (panneau), `admin_telegram.py` + `admin_views.py` (bot).

## Installation

Prérequis : Python 3.11+, Docker (serveur Local Bot API), `ffmpeg` / `ffprobe` (le programme les cherche dans `source_audit/tools/ffmpeg/bin`, dossier non versionné : à télécharger vous-même).

```bash
cd v2_automation
pip install -r requirements.txt
cp .env.example .env            # puis renseigner les valeurs (voir ci-dessous)
python -m v2_automation.cli init-db
```

Serveur Telegram local :

```bash
cd v2_local_bot_api_poc
cp .env.example .env            # API id / hash de https://my.telegram.org
docker compose up -d --build
```

Variables `.env` de `v2_automation` : `TELEGRAM_BOT_TOKEN` (bot de publication), `TELEGRAM_CHANNEL_ID`, `ADMIN_BOT_TOKEN` (bot d'administration, optionnel), `ADMIN_TELEGRAM_IDS`. La configuration fonctionnelle est dans `v2_automation/config/config.yaml` (intervalle de 30 min, parallélisme, limites, conservation, seuils d'alerte).

## Utilisation

```bash
python -m v2_automation.cli run-worker     # surveillance + téléchargements + publications
python -m v2_automation.cli serve          # panneau web sur http://127.0.0.1:8085
python -m v2_automation.cli run-admin      # bot Telegram d'administration
```

Sous Windows, `v2_automation/scripts/install_tasks.ps1` installe les trois processus comme tâches planifiées (démarrage à l'ouverture de session, relance après plantage) ; les boutons « Démarrer / Arrêter le worker » des deux panneaux s'appuient dessus.

## Tests

```bash
cd v2_automation && python -m pytest tests -q     # ~380 tests
cd ../v1_poc && python -m pytest -q
cd ../source_audit && python -m pytest -q
```

Les tests n'utilisent ni réseau ni Telegram réel (source et serveur simulés).

## Sécurité

- Les secrets vivent uniquement dans des `.env` (ignorés par Git) ; les modèles `.env.example` ne contiennent que des valeurs factices.
- Les jetons sont masqués dans les journaux ; le panneau web n'écoute que sur `127.0.0.1` et refuse les actions venant d'un autre site (contrôle de l'en-tête `Origin`).
- Le port de statistiques du serveur Local Bot API (`8082`) affiche les jetons en clair : il reste lié à `127.0.0.1`.
- Les alertes, notifications et le panneau d'administration ne passent **jamais** par le canal public.

## État

Voir la fin de `V1_FINAL_E2E_REPORT.md` pour les preuves réelles, les limites connues (espace disque, lecteurs vidéo pris en charge) et ce qui reste à valider dans la durée (fenêtre de 24 h, nettoyage à J+14).
