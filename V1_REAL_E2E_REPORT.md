# V1 Real E2E Report

Date : 2026-09-19. Point d'entrée : `python v1_poc/run.py --url "<URL>"`.
L'URL est enregistrée comme épisode dans la base V2 (`v2_automation/data/v2.sqlite3`), mise en file, puis traitée par le même `DownloadManager` que le worker : pas de pipeline parallèle.

Statuts utilisés : PROVEN / FAILED / BLOCKED / NOT_TESTED / INCONCLUSIVE.

## 1. Résumé

| Étape | Statut |
|---|---|
| Extraction de la source, choix de la rendition, playlist | PROVEN (réel, épisodes 40 à 48) |
| Téléchargement, assemblage MP4 | PROVEN (600 à 714 Mo, environ 2 à 9 min) |
| Validation ffprobe + intégrité | PROVEN |
| Miniature (affiche de la source) + fiche anime en légende | PROVEN |
| Publication Telegram : miniature puis vidéo, 2 message ID | PROVEN |
| Déduplication (dont redémarrage) | PROVEN |
| Reprise / cache | PROVEN |
| Coupure de connexion à ~500 s pendant l'envoi | corrigée à la source (serveur patché) : PROVEN sur 4 mesures (570 s, 961 s, 1440 s, 1766 s) ; secours PROVEN sur 4 cas |
| Épisode pas encore publié par la source | PROVEN (épisode 49) |
| Test gros fichiers au-delà de ~650 Mo | PROVEN jusqu'à 1805 Mio (1000, 1500, 1800) |
| Arrêt brutal pendant un vrai envoi (kill) | PROVEN sur 3 scénarios réels (téléchargement, envoi de la vidéo, worker) ; 1 bug trouvé et corrigé |

## 2. Épisodes publiés (Bleach: Sennen Kessen-hen, VOSTFR)

| Épisode | Miniature | Vidéo | Taille | Remarque |
|---|---|---|---|---|
| 48 | 104 | 105 | 654 336 165 o | republié avec la fiche (1re publication 99/100 supprimée par le propriétaire) |
| 47 | 106 | 108 | 665 832 147 o | coupure de connexion, message retrouvé à la main (108) |
| 46 | 109 | 110 | 613 683 893 o | coupure, retrouvé à la main (110) |
| 45 | 111 | 112 | 713 848 893 o | coupure, retrouvé automatiquement |
| 44 | 113 | 114 | 621 661 129 o | coupure (délai client de 600 s), retrouvé automatiquement |
| 43 | 115 | 116 | 619 523 908 o | coupure à 538 s, retrouvé automatiquement |
| 42 | 117 | 118 | 643 332 989 o | coupure à 544 s, retrouvé automatiquement (journal serveur détaillé) |
| 41 | 119 | 120 | 625 167 173 o | envoi de ~570 s SANS coupure (serveur patché) |
| 40 | 121 | 122 | 375 526 670 o | version simplifiée (sans `curl`), 1600x900 (seule rendition disponible), envoi en ~7 min |
| 39 | 127 | 128 | 388 499 889 o | scénario de coupure B1 (kill pendant le téléchargement), puis reprise |
| 38 | 129 | 130 | 402 068 998 o | scénario B2 (kill pendant l'envoi de la vidéo), vidéo retrouvée par la reprise |
| 37 | 132 | 134 | | scénario B3 (kill du worker pendant l'envoi), vidéo retrouvée par le worker |
| 49 | | | | en attente `NOT_AVAILABLE_YET` (la source n'a qu'un lecteur YouTube) |

Pour les épisodes 41 à 48 : miniature = affiche de la source (`og:image`, 103 894 o) avec la fiche anime en légende, puis la vidéo avec sa description d'épisode.

## 3. Modifications du code

### Pipeline (`v2_automation`)
- `errors.py` : 10 codes d'erreur identifiables (`SOURCE_EXTRACTION_FAILED`, `NO_RENDITION`, `PLAYLIST_FETCH_FAILED`, `PLAYLIST_PARSE_FAILED`, `DOWNLOAD_FAILED`, `VALIDATION_FAILED`, `THUMBNAIL_FAILED`, `TELEGRAM_THUMBNAIL_FAILED`, `TELEGRAM_VIDEO_FAILED`, `NOT_AVAILABLE_YET`). L'exception d'origine est toujours chaînée et son texte conservé dans `last_error`.
- `metadata.py` : `MediaMetadata`, description de la vidéo sans aucune URL (titre, épisode ou film, langue, qualité mesurée), fiche anime pour la légende de la miniature (titres, type, statut, début, genres, studio, synopsis tronqué à 1024 caractères, `[@otakuu_verse]`). Les champs absents de la source sont omis, jamais inventés.
- `downloader.py` : progression `[1/7]` à `[7/7]`, playlist téléchargée une seule fois, cache MP4 (réutilisé seulement s'il est valide, sinon seul ce fichier est supprimé), cache de miniature (JPEG vérifié), affiche de la source (avec `Referer`, sinon 403) avec repli sur une image de la vidéo, dédup par `video_message_id` et par la table `publications` (jamais de renvoi, miniature jamais reposée), file remise en `queued` après un échec avec backoff de 1 à 60 min.
- `publisher.py` : miniature avec fiche en légende, envoi de la vidéo par `file://` (copie `docker cp` dans le conteneur du serveur, mode local de python-telegram-bot), `wait_for_message()` (retrouve un message publié en retard), `connection_lost()`, lecture de la durée par ffmpeg pour la miniature.
- `app_config.py` et `config.yaml` : `TELEGRAM_API_BASE_URL` lu depuis `.env`, `telegram.local_upload_container`, `upload_timeout_seconds: 7200`, `publication.channel_tag`, `downloads.not_available_retry_minutes` (30) et `not_available_window_days` (30).
- `v1_poc/run.py` (nouveau) : mode `--url`. `v1_poc/source_client.py` : titre de la page, langue, `og:image`, `SourceNotAvailableError` pour un lecteur YouTube. `v1_poc/manifest.py` : refuse une playlist sans `#EXTM3U`. `v1_poc/validator.py` : contrôle de l'intégrité des paquets vidéo.
- `scripts/large_file_test.py` (nouveau) : test progressif de gros fichiers.

### Serveur Local Bot API (`v2_local_bot_api_poc`)
- `Dockerfile` : `sed` qui remplace `IDLE_TIMEOUT = 500` par `7200` dans `telegram-bot-api/HttpServer.h` avant la compilation (l'original est gardé dans `Dockerfile.orig`).
- Image `v2-telegram-bot-api:idle7200-real`, aussi tagguée `latest`. Ancienne image : `pre-idle-patch`.
- `scripts/start_server.ps1` : journal serveur (`--verbosity=3 --log=/data/server.log`, rotation à 100 Mo).

## 4. Défauts trouvés par les tests réels et corrigés
1. Le parseur de playlist acceptait du HTML comme « segment ».
2. Un MP4 tronqué avec son en-tête intact passait la validation.
3. Les épisodes en échec restaient « en cours » dans la file et `next_retry_at` valait la fin de la fenêtre de 24 h : les reprises ne pouvaient pas avoir lieu.
4. La miniature ne cherchait jamais dans la vidéo (mauvais outil pour lire la durée).
5. Le cache de miniature acceptait n'importe quel fichier non vide.
6. `test_downloader.py` utilisait la porte d'autorisation supprimée (8 échecs préexistants).
7. Boucle infinie dans mon code de détection de coupure (cause circulaire), attrapée par les tests.
8. Encodage d'un emoji dans un envoi via `curl` (approche abandonnée, voir §6).

## 5. Fiche anime et miniature
Contenu tiré de la page anime de la source : titre alternatif (anglais), titre original (romaji et japonais), type, statut, date de début, genres, studio, synopsis. Non fournis par la source, donc omis : origine, thème, simulcast, saison. La miniature est l'affiche de la page (`og:image`) ; à défaut, une image extraite de la vidéo.

## 6. Coupure de connexion à ~500 s : enquête et résultat
- Symptôme : `Server disconnected` ou `Empty reply from server` après ~500 s d'envoi, alors que la vidéo apparaît sur le canal quelques minutes plus tard. Conséquence : deux vidéos en double lors de mes premières tentatives (supprimées par le propriétaire).
- Pistes écartées : proxy de ports Docker (même coupure depuis le réseau du serveur), taille du fichier (dépend de la durée), délai du client (relevé de 600 à 7200 s, la coupure restait à ~500 s).
- Cause prouvée : `IDLE_TIMEOUT = 500` en dur dans `telegram-bot-api/HttpServer.h` (dépôt officiel, commit e3e9dd8). Le journal serveur détaillé montre `Idle timeout expired` exactement 500 s après le début de la requête. Aucune option en ligne de commande ne le règle.
- Erreur de ma part : la première « reconstruction » n'a jamais appliqué le patch (script d'édition échoué en silence), ce qui a faussé mes conclusions pendant quelques essais. Détecté par le journal de build (pas de `sed`), puis corrigé.
- Test de connexion muette (sur ancienne et nouvelle image) : INCONCLUSIVE, aucune des deux n'a fermé la connexion en 640 à 760 s, ce test ne reproduit donc pas la situation réelle.
- Correction : image reconstruite avec le vrai patch. Épisode 41 : envoi de ~570 s (plus de 500 s), aucune nouvelle ligne `Idle timeout expired`, message ID retourné directement. PROVEN sur une mesure.
- Secours conservé : si la connexion tombe quand même, le programme n'envoie plus rien, cherche pendant 30 min le message par sa légende exacte (édition identique, non destructive), l'enregistre s'il le trouve, sinon marque « publication incertaine » (décision manuelle). PROVEN en réel sur les épisodes 42 à 45.
- Le conteneur `curl` essayé au milieu de l'enquête a été retiré du code et son image supprimée.

## 7. Épisode pas encore publié (`NOT_AVAILABLE_YET`)
Un lecteur YouTube sur la page (cas de l'épisode 49) n'est pas un flux téléchargeable. L'épisode reste `retry_wait`, revérifié toutes les 30 minutes sur 30 jours, sans compter comme échec. Dès que la source publie la vidéo, le worker la publie tout seul (test unitaire et cas réel du 49).

## 8. Tests
- v2_automation : 185 passés. 17 utilisent les vrais ffmpeg et ffprobe sur un serveur HLS local (téléchargement, reprise après 503, 404, interruption, MP4 tronqué, corrompu, sans audio, sans vidéo, miniature). Les autres couvrent l'orchestration : erreurs codées, cache, ordre miniature puis vidéo, déduplication, redémarrage, fiche anime, épisode pas encore publié, coupure de connexion. v1_poc : 21 passés. source_audit : 114 passés.
- E2E réels : épisodes 40 à 48 (voir §2).
- Test gros fichiers avec le serveur patché (`scripts/large_file_test.py`, envoi par la voie de production `V2TelegramClient.send_video`, vrais MP4 valides par boucle de l'épisode réel, message supprimé après chaque essai) :

| Taille | Durée d'envoi | Résultat | Message | Taille côté Telegram | Serveur |
|---|---|---|---|---|---|
| 50 Mio (essai du script) | 57 s | PASS | 123 | identique | RAS |
| 996 Mio | 961 s | PASS, sans coupure | 124 | identique | aucune nouvelle ligne `Idle timeout expired`, 0 redémarrage |
| 1507,7 Mio | 1440 s | PASS, sans coupure | 125 | identique | idem |
| 1804,8 Mio | 1766 s | PASS, sans coupure | 126 | identique | idem |

  Preuves : `v2_automation/output/evidence/large_file/size_1000.json`, `size_1500.json`, `size_1800.json`, `summary.json` (état du serveur avant et après chaque taille). Aucun test à 1900 Mio ou plus (plantage documenté par `V4_FINAL_CAPACITY_REPORT.md` sur l'ancien serveur, non retesté). Le plafond `limits.max_safe_publish_mib: 768` du pipeline n'a pas été modifié : décision en attente.
- Test gros fichiers avec l'ancien serveur non patché (`scripts/large_file_test.py`) : 293 Mo PASS (377 s, taille identique côté Telegram, message supprimé) ; 507 Mo et 1248 Mo en échec (coupure à 500 s, résultat inconnu). Au-delà de ~650 Mo avec le serveur patché : voir le tableau ci-dessus (PROVEN jusqu'à 1805 Mio). Les rapports V4 et V5 indiquent un plantage du serveur vers 900 Mio en multipart et une preuve jusqu'à 1800 Mio en `file://` ; le plafond `max_safe_publish_mib: 768` reste en place.
- Débit d'envoi mesuré : environ 0,8 Mo/s, soit 13 à 15 min pour un épisode de 650 à 700 Mo. Les envois très rapides de l'épisode 48 correspondaient probablement à un fichier déjà connu de Telegram (hypothèse).

## 9. Épisode 40 : version simplifiée (retrait de `curl`)
Le conteneur `curl` a été retiré : l'envoi repasse par python-telegram-bot en mode local (`file://`), avec 4 tests unitaires dédiés (URI `file://`, copie supprimée après succès ou rejet, copie conservée si la connexion tombe, détection de coupure sans boucle). Épisode 40 (réel, 21:40 à 21:48) : miniature 121, vidéo 122, 375 526 670 o, sha256 `5b29747d553ca0e75772d0000f488a877bf0f686280f1ac26228327f43079419`, envoi direct sans coupure. PROVEN pour le chemin simplifié, mais l'envoi (~7 min) était sous les 500 s : il ne teste pas la coupure, seul l'épisode 41 (~570 s) le fait.

## 9b. Arrêt brutal pendant un vrai envoi (kill) : PROVEN

Méthode : un vrai épisode non publié, processus tué de force (`Stop-Process -Force`) à un moment précis, puis relance, puis contrôle base et canal.

| Scénario | Épisode | Moment du kill | Résultat |
|---|---|---|---|
| B1 | 39 | pendant le téléchargement (54 segments reçus, rien de publié) | reprise complète après la correction ci-dessous ; une seule publication (127 puis 128) |
| B2 | 38 | après la miniature (129), pendant l'envoi de la vidéo, `run.py` tué | le serveur a poursuivi l'upload seul (requête vue dans son journal) ; la relance a refusé de renvoyer (« publication MANUELLE », alerte levée) ; la vidéo 130 est apparue ~5 min plus tard ; la reprise l'a retrouvée par sa légende, enregistrée, et a nettoyé le fichier du conteneur ; `run.py` répond ensuite « Already published » ; une seule vidéo |
| B3 | 37 | idem B2 mais le **worker** est tué | un second worker lancé dans les 90 s est refusé (`busy`, bail non expiré) ; un worker lancé après expiration reprend le bail (`stolen`), marque l'épisode « à vérifier » sans rien renvoyer ; ~2 min après l'apparition de la vidéo (134), le worker l'a retrouvée et enregistrée tout seul |

Bug trouvé par B1 et corrigé (`recovery.py`) : après un kill, la reprise remettait l'épisode en `queued`, mais son entrée dans la file restait « en cours » du processus tué, donc plus personne ne pouvait le réclamer (ni `run.py`, ni le worker). La reprise rend maintenant cette entrée à la file, aussi pour un état déjà bloqué (2 tests).

Ajouts issus de B2 et B3 :
- `recovery.reconcile_uncertain` : pour un épisode interrompu dont la miniature est publiée, cherche la vidéo juste après la miniature (légende exacte reconstruite, édition identique non destructive). Si elle existe, l'enregistre sans rien envoyer. Appelée au démarrage de la reprise, toutes les 2 minutes par le worker, et à la demande par la commande `python -m v2_automation.cli reconcile`. 6 tests.
- Alerte : un épisode « pas encore publié » (`NOT_AVAILABLE_YET`) n'envoie plus d'alerte de nouvelle tentative ; les vrais échecs en envoient toujours (test).
- Limite constatée : un worker relancé moins de 90 s après un kill est refusé tant que le bail n'a pas expiré.

## 9c. Plafond de taille
Décision du propriétaire (2026-09-20) : `limits.max_safe_publish_mib` passe de 768 à 1800 (1804,8 Mio prouvés sans coupure, aucune marge ; plantage documenté à 1900 Mio et plus sur l'ancien serveur).

## 10. Limitations connues
- Fichiers de test faits en bouclant un même épisode (vidéo valide mais répétitive) ; le plafond `max_safe_publish_mib: 768` du pipeline reste à ajuster (proposition : 1500 Mio prouvés moins une marge, soit environ 1280 Mio).
- Le débit d'envoi limite la durée (environ 15 min par épisode) : publier en tâche de fond avec le worker.
- `run.py` télécharge la page de l'épisode une fois de plus pour l'identifiant de l'anime.
- Le titre vient du `<title>` de la page (premier segment) ; la qualité de la hauteur validée (ex. `1080p`).
- Si la vidéo n'apparaît jamais après un arrêt, l'épisode reste `failed` « MANUELLE » (rien ne le renvoie automatiquement) ; les numéros 131 et 133 du canal ne correspondent à aucune publication du programme (à vérifier sur le canal).
- Un worker relancé moins de 90 s après un kill est refusé tant que le bail n'a pas expiré.
- Le dossier d'envoi du conteneur (`/data/v2_upload`) est nettoyé après confirmation du message.

## 11. Statut final
Chaîne complète URL vers Telegram PROVEN en réel sur 12 épisodes (37 à 48), avec fiche anime, déduplication, reprise, coupure de ~500 s corrigée à la source et secours en place. Gros fichiers PROVEN jusqu'à 1805 Mio ; arrêt brutal pendant un envoi PROVEN sur 3 scénarios réels. Plus aucune ligne NOT_TESTED. Tests : v2_automation 185, v1_poc 21, source_audit 114.
