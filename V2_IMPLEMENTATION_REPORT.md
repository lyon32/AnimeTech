# V2_IMPLEMENTATION_REPORT

Base : `main` @ 6f03cce (+ 4 fichiers d'exploitation non commités, conservés tels quels). Audit préalable : `V2_IMPLEMENTATION_AUDIT.md`.
Statuts : TESTED · MEASURED · NOT_EVALUATED · INCONCLUSIVE · BLOCKED. Preuves : `V2_TEST_REPORT.md` (résultats) et `V2_TEST_PLAN.md` (carte des tests).

## 1. Principe retenu
Le package existant `v2_automation` **est** le « V1 GLOBAL WATCHER » du prompt (surveillance 30 min, files, publication canal, panneau, bot admin).
Le côté « V2 USER REQUESTS » y a été ajouté (même DB, même worker, même moteur de téléchargement). **Aucun second downloader** : une demande
utilisateur ne fait qu'appeler `media.ensure_media()`, le même point d'entrée que le watcher.

```
 V1 discovery ─┐                                             ┌─ canal (miniature + vidéo, + copies canaux supplémentaires)
               ├─► media.ensure_media ─► episodes/queue_items ─► DownloadManager ─► READY/PUBLISHED ─┤
 V2 requests  ─┘        (media_key UNIQUE)                     (un seul moteur)                       └─ livraison privée (file_id | copy | upload)
```

## 2. EXISTING → DESIRED → GAP → CHANGE → TEST, par phase

| Phase | EXISTING | DESIRED | GAP | CHANGE | TEST |
|---|---|---|---|---|---|
| 1 Audit | — | audit sourcé | — | `V2_IMPLEMENTATION_AUDIT.md` | — |
| 2 Identité | `UNIQUE(source, episode_key)` (URL) | `media_key` déterministe (anime+saison+épisode+version) | pas de clé partagée watcher/utilisateur | `media.py` (`compute_media_key`, `key_for`), colonne `episodes.media_key` UNIQUE, backfill à la migration v4 | `test_core_media.py::test_media_key_*`, `test_db.py` |
| 3 Modèle média persistant | 19 états `episodes.status` | 11 états nommés dont `EXPIRED` | noms/`EXPIRED` | `MediaState` = **vue** sur `status` (rien à migrer ; `validated/ready → READY`, `failed+NOT_AVAILABLE_YET → EXPIRED`), + état réel `ready` (média privé validé) | `test_media_state_view_and_expired`, `test_private_media.py` |
| 4 Requests | aucune | 11 états, parent/enfants, 1 active/utilisateur | tout | tables `users/requests/request_items/deliveries`, `requests_mgr.py`, index unique partiel `ux_requests_one_active` | `test_core_media.py` |
| 5 Déduplication | dédup watcher seulement | 1 job pour N demandeurs | — | `ensure_media` (course-safe : `IntegrityError` → reprend la ligne existante) | 3 users→1 download ; 8 threads/8 connexions → 1 job |
| 6 Files | `next_heads`, FIFO par anime | inchangé + média privé | fin de vie d'un média privé | `queues.py` **non modifié** ; `downloader` s'arrête à `READY` si `publish_channel=0` et libère la file ; boucle worker : `UserSide.tick()` | `test_private_media.py`, `test_worker_user_side.py` |
| 7 Crash recovery | SAFE/RISKY, réconciliation partielle | reprise sans doublon | « ACK enregistré, ligne épisode en retard » → FAILED manuel | recovery : complète l'épisode **depuis l'enregistrement** (aucun renvoi) ; `ready` sans fichier → re-file ; livraisons `sending` → `uncertain` ; copies canal `sending` → `uncertain` | matrice de crash (`test_delivery.py`) |
| 8-9 Bot + membership | bot admin seulement | bot utilisateur privé, accès canal(s) | tout | `user_bot.py` (routeur sans Telegram + couche réelle), `membership.py` (vérifié à **chaque** interaction, jamais mis en cache) | `test_user_bot.py` |
| 10 Recherche/parser | aucune | formulations, saisons/versions réelles, hors liste sans ajout | tout | `parser.py`, `search.py` (liste surveillée d'abord, puis recherche source), `catalog.py` | `test_user_bot.py` + page réelle capturée + **recherche live** (rapport de test) |
| 11 Livraison privée | aucune | persistée, 1 par item, sans doublon | tout | `delivery.py` : `sending` avant l'appel, `sent` après ; `uncertain` jamais renvoyé automatiquement ; backoff borné | `test_delivery.py` |
| 12 Multi-canal | 1 canal, publié par le moteur | N canaux configurables, branches indépendantes | tout | `telegram.channels` / `TELEGRAM_CHANNELS`, `channels.py` (copie miniature+vidéo par `copyMessage`, idempotente) | `test_channels_config.py` |
| 13 Local Bot API | `V2TelegramClient` soudé au conteneur | abstraction transport | tout | `telegram_publisher.py` : `TelegramPublisher` + `BotAPITransport` / `LocalBotAPITransport` (même `send_video`) | `test_delivery.py` (transports), sondes réelles (rapport) |
| 14 Admin Telegram | 14 commandes | + `/requests /history /stats`, journal d'audit | audit, demandes | `audit.py` (un point de passage : `service.py`), commandes, annulation de demande avec confirmation | `test_v2_admin.py` |
| 15 Panel web | 45 routes, local | auth, demandes/utilisateurs/Telegram/audit, actions dangereuses confirmées | pas d'auth | `web_auth.py` (PBKDF2, cookie signé HttpOnly+Strict, anti-force brute, **refus de démarrer sans mot de passe**), 13 routes, page `/ui/v2.html` | `test_v2_admin.py` |
| 16 Cleanup | J+14, fichier seul | idem + média privé | preuve de publication d'un média privé | `cleanup.py` : « publié » = message canal **ou** `file_id` livré | `test_worker_user_side.py` |
| 17-18 | | | | ce rapport, plan, rapport de tests | suite complète |

## 3. Décisions de conception (à connaître)

1. **Chaque (titre, saison, langue) est une page distincte sur la source** (source_audit, TESTED). `anime_key` porte donc déjà saison+version ; `season`
   (`animes.season`, 0 si inconnue) et `version` restent dans la clé. Les « saisons disponibles » ne sont pas listées par la source : elles se déduisent des
   **résultats de recherche** (numéro lu dans le titre uniquement — jamais inventé). Un titre à page unique sans numéro accepte « saison 1 ».
2. **Média privé** (anime hors liste surveillée) : `publish_channel = 0` → **rien n'est posté dans le canal** ; le fichier validé attend en `READY`, la 1ʳᵉ livraison
   le passe en `published` et démarre le compte à rebours de 14 jours. Un anime surveillé garde sa publication canal comme avant (`publish_channel = 1`).
3. **Réutilisation Telegram** : `file_id` d'une livraison précédente → sinon `copyMessage` depuis le canal → sinon envoi du fichier local. Un `file_id` est propre à un bot :
   seuls les `file_id` du bot utilisateur sont réutilisés ; pour `copyMessage`, **le bot utilisateur doit être membre/admin du canal** (sinon repli sur le fichier local, puis erreur explicite).
4. **Livraison incertaine** (crash/timeout pendant l'envoi privé) : Telegram ne permet pas de relire un chat privé ; on choisit **zéro doublon** plutôt que « toujours livré » :
   statut `uncertain`, demande `FAILED` avec message clair, l'utilisateur redemande et est servi **sans nouveau téléchargement**. L'admin peut relancer (confirmation).
5. **Annulation** : le job n'est annulé que s'il attend encore en file, a été créé par un utilisateur, n'est pas publié dans un canal et qu'**aucune autre demande active** ne l'attend.
   Un téléchargement déjà en cours se termine (le fichier reste en cache).
6. **Attente 20 min** : « épisode non listé » ou « vidéo pas prête (`NOT_AVAILABLE_YET` / `SOURCE_VIDEO_PROCESSING`) » → `WAITING_FOR_MEDIA` ; l'échéance (`requests.wait_timeout_seconds` / `REQUEST_WAIT_TIMEOUT`, horloge injectable) court depuis la création.
   Le job du média, lui, continue pour le watcher et les autres utilisateurs.
7. **Priorité** : aucune (pas de VIP/premium).

## 4. Statut fonctionnel (critère §70)

| Capacité | STATUS | Preuve |
|---|---|---|
| V1 watcher fonctionne | **TESTED** | 390 tests du watcher inchangés verts ; convergence watcher↔utilisateur (`test_core_media.py`) ; E2E historiques V1 (rapports du repository) |
| Bot V2 (conversation) | **TESTED** (transport Telegram simulé) | `test_user_bot.py` 35 tests |
| Livraison privée sur Telegram réel | **TESTED** (BestAnime32_bot → chat opérateur : upload puis `file_id`) | `V2_TEST_REPORT.md` §4 bis |
| Conversation du bot sur Telegram réel | **NOT_EVALUATED** | boucle `run_user_bot` non lancée ; `use_channel_bot: true` |
| Recherche / hors liste surveillée | **TESTED** + **MEASURED** live | unitaires + page réelle capturée + 1 recherche live (`bleach` → 10 résultats, 48 épisodes listés, 1,74 s) |
| Saison / épisode / dernier épisode / VF / VOSTFR | **TESTED** | `test_user_bot.py`, `test_core_media.py` |
| 1 request active par utilisateur | **TESTED** | y compris refus au niveau base (`IntegrityError`) |
| Expiration 20 min | **TESTED** (horloge contrôlée) | 19 min = attend, 21 min = `EXPIRED`, durée configurable |
| Déduplication / 1 download / N utilisateurs | **TESTED** | y compris avec **vrai téléchargement** ffmpeg (`origin.hits["seg_0.ts"] == 1`) |
| Livraison privée | **TESTED** (Telegram simulé) ; réel **BLOCKED** | `test_delivery.py`, `test_core_pipeline_real.py` |
| Historique utilisateur | **TESTED** | `/history` lu en base |
| Crash recovery, aucun doublon | **TESTED** | matrice §52 (7 scénarios) |
| Files par anime, parallélisme inter-anime | **TESTED** | worker réel : 3 anime en parallèle, un échec ne bloque pas |
| V1 et V2 = même moteur | **TESTED** | un seul `ensure_media`, un seul `DownloadManager` |
| Multi-canal | **TESTED** (transport simulé) | `test_channels_config.py` |
| Telegram Bot API standard | **BLOCKED** (DNS : ce poste ne résout pas api.telegram.org) ; refus > 50 Mo **TESTED** | rapport Local Bot API |
| Local Bot API | **TESTED** (getMe réel, membership réel) ; gros fichiers **MEASURED** (campagnes antérieures, non refaites) | `V2_LOCAL_BOT_API_POC_REPORT.md` |
| Cleanup J+14 | **TESTED** | horloge contrôlée, DB + réf. Telegram conservées, rejouable |
| Admin Telegram | **TESTED** (bot simulé) | `test_v2_admin.py` |
| Panel web (auth + actions réelles) | **TESTED** | chaque action vérifiée dans la DB ; page `v2.html` servie mais **interface non exercée dans un navigateur** → **NOT_EVALUATED** pour le rendu |
| Sécurité (auth, secrets, audit) | **TESTED** | 401, session forgée/expirée, anti-force brute, jeton masqué dans les erreurs |
| Audit logs | **TESTED** | web + Telegram, succès / échec / non confirmé |
| Monitoring | **TESTED** (existant) + endpoints `/api/stats`, `/api/system`, `/api/telegram` | |
| E2E complet (source réelle → téléchargement → Telegram réel) | **NOT_EVALUATED** | les deux moitiés sont testées séparément |

## 5. Ce qui n'est PAS fait / limites connues
- Pas d'E2E Telegram réel côté utilisateur (token manquant) ; l'API standard n'a pas pu être jointe depuis ce poste.
- `Republish` recrée volontairement un doublon dans le canal (action dangereuse, confirmation obligatoire, journalisée).
- Un canal configuré à la fois comme `TELEGRAM_CHANNEL_ID` (id numérique) et dans `TELEGRAM_CHANNELS` (@nom) serait vu comme deux canaux : listez-le de la même façon aux deux endroits.
- Un média déjà **publié dans le canal puis nettoyé** ne peut plus être re-téléchargé (`DownloadManager` refuse un épisode ayant un `video_message_id`) : servi par `copyMessage` (bot membre du canal) — sinon erreur `STORAGE_ERROR` explicite pour l'admin.
- `DownloadManager` n'a pas de point d'annulation en cours de téléchargement (voir décision 5).
- L'interface `v2.html` est fonctionnelle par construction (appels API testés) mais n'a pas été ouverte dans un navigateur.

## 6. Déploiement — à lire avant de redémarrer
1. **La base de production (`data/v2.sqlite3`) est encore au schéma v3 : aucune migration réelle n'a été appliquée** (tous les tests utilisent des bases temporaires). Au prochain démarrage du worker avec ce code, la migration v4 s'applique (ajout de colonnes/tables + `media_key` sur chaque épisode existant). **Faites une copie de `v2.sqlite3*` avant** (le dépôt le fait déjà : `*.bak_before_*`).
2. Les processus `pythonw` actuellement lancés exécutent l'ancien code en mémoire ; rien n'a changé pour eux tant qu'ils ne sont pas redémarrés.
3. **Changement d'exploitation** : `serve` **refuse de démarrer** sans `ADMIN_WEB_PASSWORD` (ou `ADMIN_WEB_PASSWORD_HASH`) dans `.env`. Sinon : `V2_WEB_ALLOW_NO_AUTH=1` pour un essai local explicite.
4. Bot utilisateur : par défaut `BestAnime32_bot` (`user_bot.use_channel_bot: true`, déjà admin du canal). Pour `@OtakuuVerse_bot` : `USER_BOT_TOKEN=` ; ce bot doit être membre/admin de `@spy_family_2025` (nécessaire à la vérification d'appartenance **et** à `copyMessage`).
5. Nouveaux réglages (`config.yaml` : `requests`, `user_bot`, `telegram.channels` ; `.env.example` : `USER_BOT_TOKEN`, `TELEGRAM_CHANNELS`, `REQUIRED_CHANNELS`, `REQUEST_WAIT_TIMEOUT`, …).

## 7. Fichiers ajoutés / modifiés
Nouveaux : `media.py catalog.py requests_mgr.py parser.py search.py membership.py delivery.py telegram_publisher.py channels.py user_bot.py user_side.py audit.py web_auth.py web_ui/v2.html`
Modifiés (petits patchs ciblés) : `schema.py` (v4), `models.py`, `repo.py` (clé média à l'insertion), `db.py` (backfill), `states.py` (`READY`), `downloader.py` (média privé → `READY`), `recovery.py`, `cleanup.py`, `worker.py` (`UserSide`), `service.py` (fonctions admin V2 + audit), `admin_telegram.py`, `web.py`, `cli.py`, `app_config.py`, `errors.py`, `config/config.yaml` (sections ajoutées en fin de fichier), `.env.example`, `web_ui/index.html` (lien).
Tests : `test_core_media.py test_private_media.py test_delivery.py test_user_bot.py test_channels_config.py test_worker_user_side.py test_core_pipeline_real.py test_v2_admin.py` (+ `v2support.py`) ; `test_db.py` (v4) et `test_config.py` (placeholders `<…>`) ajustés.
