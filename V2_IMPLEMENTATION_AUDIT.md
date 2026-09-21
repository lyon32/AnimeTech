# V2_IMPLEMENTATION_AUDIT — état réel du repository avant la « V2 user requests »

Date : 2026-09-21 · Base : `main` @ 6f03cce + 4 fichiers modifiés non commités (`config.yaml`, `downloader.py`, `service.py`, `worker.py`, ~30 lignes, retouches d'exploitation).
Chaque conclusion cite le fichier lu. Rien ici n'est supposé.

## 0. Vocabulaire (piège de nommage)

| Dans le prompt | Dans le repository |
|---|---|
| « V1 GLOBAL WATCHER » | `v2_automation/` (le package s'appelle déjà *v2_automation* : watcher 30 min, files, publication canal, panneau web, bot admin). **C'est lui qu'il faut conserver.** |
| « V2 USER REQUESTS » | **n'existe pas** : à construire dans le même package (même DB, même worker). |
| `v1_poc/` | POC historique (22 tests verts). Réutilisé en lecture : `TelegramClient`, `validator`, `manifest`, `source_client`. |
| `source_audit/` | Parsers de la source (`parse_anime_page`, identité `postid:N`). Réutilisé. |

Décision : le nouveau code va dans `v2_automation/src/v2_automation/` (migration DB v4), pas dans un 2ᵉ package — sinon on créerait précisément le « deuxième système parallèle » que le prompt interdit.

## 1. Architecture actuelle

```
DiscoveryScheduler (discovery.py, cycle global 1800 s)  ──► episodes(discovered→queued) + queue_items
worker.run_worker (worker.py, bail unique `leases`)     ──► repo.next_heads → DownloadManager.process_episode
DownloadManager (downloader.py)  source → rendition → playlist → download → ffprobe → sha256 → thumbnail → sendPhoto → sendVideo(canal)
recovery.py (boot) · cleanup.py (14 j) · alerts.py/notifier.py · admin_telegram.py (2ᵉ bot) · web.py (FastAPI 127.0.0.1 + web_ui/)
```
Un seul processus worker (bail), pool de threads, une connexion SQLite partagée sérialisée (`db.SafeConnection`).

## 2. Composants réutilisables (tels quels)

- **Moteur de téléchargement** `DownloadManager` : unique, robuste (cache réutilisé si valide, garde disque, retry 24 h, dédoublonnage de publication `_sent()`), à ne pas dupliquer.
- **File FIFO par anime** `repo.next_heads` / `QueueManager` : « une tête par anime, ordre d'épisode », parallélisme inter-anime, pause globale, anime désactivable.
- **Ordonnanceur dynamique** `scheduler.py` : disque, CPU, RAM, débit EWMA, taille estimée (`next_estimated_bytes`) → couvre §31.
- **Bail unique** `worker.acquire_lease` (anti double worker).
- **Recovery** `recovery.run_recovery` : cas SAFE/RISKY, réconciliation `reconcile_uncertain` (retrouve le message vidéo sans re-publier).
- **Cleanup** `cleanup.run_cleanup` : `published_at + 14 j`, fichier seul, messages/DB conservés, ré-exécutable après restart (piloté par la DB).
- **Découverte** `discovery.check_anime`, `identify_anime`, `validate_source_url`.
- **Panneau web** (`web.py`, ~45 routes, `service.py` partagé avec le bot admin) et **bot admin** (`admin_telegram.py`, allowlist `ADMIN_TELEGRAM_IDS`, boutons inline, confirmations).
- **Erreurs classifiées** `errors.py` (codes `NOT_AVAILABLE_YET`, `SOURCE_VIDEO_PROCESSING`, `DOWNLOAD_FAILED`, `VALIDATION_FAILED`, `TELEGRAM_*_FAILED`, …).

## 3. Composants incomplets / absents (par rapport au prompt)

| Exigence | Constat dans le code |
|---|---|
| media_key déterministe (§3) | **Absent.** Identité = `UNIQUE(source, episode_key)` où `episode_key` = URL canonique de l'épisode (`source_audit.identity.build_episode_key`). Déterministe et unique, mais ce n'est pas un hash `anime+saison+épisode+version`. |
| États média (§5) | États existants plus fins (`discovered…cleaned`, `cleanup_blocked`, `structure_changed`, `skipped_dup`, `blocked`). **Pas de `EXPIRED`**, pas de nom `READY`. Voir §12 (mapping). |
| Request utilisateur (§6-8) | **Absent** : aucune table `users`, `requests`, `request_items`, `deliveries`. |
| Bot utilisateur (§9-17) | **Absent.** Seul `admin_telegram.py` (long polling, 2ᵉ bot `ADMIN_BOT_TOKEN`, allowlist stricte). |
| Membership canal (§10-11) | **Absent.** |
| Recherche hors liste (§13) | **Absent** (le site a une recherche `?s=` — pages brutes dans `source_audit/output/raw/search_*.html` — mais aucun parseur ni client). |
| Livraison privée (§24) | **Absent.** |
| `TelegramPublisher` abstrait (§34) | **Absent.** `V2TelegramClient` (hérite de `v1_poc.TelegramClient`) mélange transport standard / Local Bot API via `local_container` (`docker cp` + `file://`). Un seul `channel_id` (`TELEGRAM_CHANNEL_ID`). |
| Multi-canaux (§36, §59) | **Absent** (`AppConfig.channel_id: str`, `publication.channel_tag` unique). |
| `file_id` Telegram (§21) | `publications` ne stocke que `message_id` (+`chat_id`) ; **`file_id` du média n'est pas conservé** (seul celui de la photo est lu puis jeté, `publisher.publish_thumbnail`). |
| MediaStorageManager (§38) | Diffus : `DownloadManager._output_path/_media_dir`, colonnes `episodes.file_path/file_size/video_sha256/cleanup_at`. Pas de classe, pas de `expires_at` explicite (= `cleanup_at`). |
| Audit log admin (§45) | **Absent** (les actions passent par `service.py` sans trace : ni `admin_user_id`, ni résultat). |
| Auth panel web (§44) | **Absente** : `127.0.0.1` seulement + refus d'`Origin` non local. Aucun login. |
| Config env (§46) | Partielle : `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `ADMIN_TELEGRAM_IDS`, `ADMIN_BOT_TOKEN`, `TELEGRAM_API_BASE_URL`, `V2_TEST_MODE`. Manquent `required_channels`, `TELEGRAM_CHANNELS`, `REQUEST_WAIT_TIMEOUT`, `MAX_CONCURRENT_DOWNLOADS` en env. |
| Observabilité par IDs (§47) | Logs `job=<episode_id>` ; pas de `request_id`, `delivery_id`. |

## 4. Interfaces existantes clés

- `repo.upsert_episode(conn, Episode) -> (id, is_new)` ; `repo.transition(conn, id, status)` (garde `states._ALLOWED`) ; `repo.enqueue(conn, anime_key, id)` ; `repo.next_heads(conn, limit)`.
- `DownloadManager.process_episode(episode_id, on_downloaded=cb) -> status`.
- `service.requeue_episode / cancel_episode / set_paused / request_force_check / add_anime_from_url / dashboard`.
- `discovery.check_anime(conn, cfg, anime_key, fetch)` ; `identify_anime(cfg, url, fetch)`.

## 5. Base de données (SQLite WAL, schéma v3)

Tables : `episodes` (la « media » actuelle), `publications` (`UNIQUE(episode_id, publication_type)` = idempotence d'envoi), `queue_items` (`UNIQUE(anime_key, episode_id)`), `animes`, `control` (clé/valeur), `alerts`, `leases`, `bot_capacity`, `schema_migrations`.
Contraintes de dédup **déjà en base** : `UNIQUE(source, episode_key)`, `UNIQUE(source, canonical_episode_url)`.

**Fait structurant (source_audit, TESTED)** : sur voir-anime.to *chaque (titre, saison, langue) est une page anime distincte* avec son `postid:N` ; il n'existe ni sélecteur de saison ni bascule de langue. Donc `anime_key` = déjà « anime + saison + version ». Un même épisode VF et VOSTFR = deux `anime_key` → deux `episodes` → deux médias distincts (§4 déjà vrai côté données). Les « saisons disponibles » d'un titre ne sont **pas** listées par la source : elles se déduisent des résultats de recherche (pages sœurs) — heuristique à documenter, jamais inventée.

## 6. Scheduler existant
Discovery : cycle global atomique (`control watcher:last_cycle_at`), tous les anime actifs, 1800 s (`V2_TEST_MODE=1` pour raccourcir uniquement en test). Worker : boucle 3 s, `next_heads(capacity)`.

## 7. Queue existante
`queue_items` persistante ; FIFO par anime, tête = plus petit n° d'épisode actif ; reconstruite/réparée au boot (`recovery` 3/3b/3c).

## 8. Publisher existant
Miniature (photo + fiche) **puis** vidéo, vers **un** canal. `_sent()` empêche tout renvoi d'un message déjà enregistré ; connexion coupée → `wait_for_message` (sonde non destructive par re-légende).

## 9. Déduplication existante
Niveau **watcher** uniquement : clés d'épisode uniques, canonical URL, `episode.video_message_id` déjà posé ⇒ `EpisodeAlreadyDone`. Rien pour « plusieurs utilisateurs → un download » (pas d'utilisateurs).

## 10. Retry existant
`episodes.attempt_count / next_retry_at / retry_until_at / last_error` ; fenêtres : source pas prête 30 min/30 j, vidéo en préparation 3 min/24 h, échec dur → FAILED + alerte. Non bloquant pour les autres anime (`next_heads` ignore un retry non échu).

## 11. Problèmes identifiés

1. **Baseline tests** : `v1_poc` 22/22 ✔ ; `v2_automation/tests/unit` **390 passés / 1 échec préexistant** : `test_config.py::test_no_real_secrets_in_repo_examples` — le test juge que `<COLLER_LE_TOKEN_DE_BOTFATHER_ICI>` (committé en 957b6f8) « semble une vraie valeur ». Défaut du test/du placeholder, indépendant de ce travail ; à corriger séparément, **pas désactivé**.
2. **Recovery risqué** : un crash pendant `PUBLISHING_*` va en `FAILED` « manuel » ; `reconcile_uncertain` ne couvre que « miniature publiée, vidéo inconnue ». Cas « vidéo publiée, DB non commitée » couvert par `_sent()` seulement si `commit_publication` a eu lieu.
3. **Pas de `file_id`** conservé → livraison privée impossible par simple ré-utilisation ; il faut soit `copyMessage` depuis le canal, soit rejouer l'upload. Contrainte Telegram : un `file_id` est propre à un bot ; le bot utilisateur (@OtakuuVerse_bot) doit soit être membre/admin du canal pour `copyMessage`, soit re-téléverser depuis le fichier local (14 j de rétention).
4. `episode.language` est déduit (`detect_language`), pas issu d'un modèle « version » de première classe.
5. Panel : aucune authentification, aucun audit ; suffisant en local, insuffisant pour l'exigence §44-45.
6. Éléments non commités en cours (4 fichiers) : à ne pas écraser ; ils portent sur l'exploitation (dossier E:, retry, worker).

## 12. Modifications nécessaires (plan, ordre imposé)

**EXISTING → DESIRED → GAP → CHANGE → TEST** (résumé par phase ; détail dans `V2_IMPLEMENTATION_REPORT.md`).

| Phase | Gap | Change | Test |
|---|---|---|---|
| 2 media identity | pas de `media_key` | `media.py` : `media_key = sha256(anime_key|season|episode|version)` ; colonne `episodes.media_key UNIQUE` (migration v4, backfill) ; `MediaState` = vue nommée sur `episodes.status` (mapping ci-dessous), ajout `EXPIRED` | unit : déterminisme, VF≠VOSTFR, backfill |
| 3 modèle persistant | `EXPIRED`/`READY` | mapping : DISCOVERED=discovered/identified · QUEUED=queued · DOWNLOADING=downloading · DOWNLOADED=downloaded · VALIDATING=validating · **READY=validated** · PUBLISHING=publishing_* · PUBLISHED=published/cleanup_* /cleaned · FAILED=failed/structure_changed/blocked · RETRY_WAIT=retry_wait · EXPIRED (nouveau) | unit : transitions |
| 4 request | tables absentes | `users`, `requests`, `request_items`, `deliveries` (v4) ; 1 request active/utilisateur | unit + DB |
| 5 dédup | — | `ensure_media()` unique point d'entrée (V1 & V2) : `INSERT … ON CONFLICT(media_key)` | 3 users → 1 job, 3 deliveries |
| 6 queue | — | réutilise `queue_items`/`next_heads` sans changement ; V2 n'appelle que `ensure_media` | test V1+V2 concurrent |
| 7 recovery | états de livraison | étendre `run_recovery` : deliveries `sending` → vérif, requests `waiting` → expiration | crash matrix |
| 8-11 | bot, membership, parser, livraison | modules `user_bot/`, `membership.py`, `parser.py`, `delivery.py` | unit + intégration |
| 12-13 | multi-canal, transport | `TelegramPublisher` (+`BotAPITransport`/`LocalBotAPITransport`), `telegram.channels` | intégration |
| 14-15 | audit log, auth | table `audit_log`, décorateur sur `service.*`, auth panel | tests d'actions réelles |
| 16 | cleanup | inchangé (déjà `published_at+14j`), test horloge contrôlée | unit |

## 13. Ce qui ne peut PAS être prouvé sans intervention de l'utilisateur

- **E2E Telegram réel** (envoi privé à un compte de test, membership réelle de `@spy_family_2025`) : nécessite un compte de test qui a démarré `@OtakuuVerse_bot` et le token de ce bot. Marqué `BLOCKED` tant que non fourni.
- **Local Bot API gros fichiers** : le POC existe (`v2_local_bot_api_poc`, rapports V4/V5 : 1804 MiB prouvés en `file://`) — réutilisé comme mesures historiques, à re-mesurer si demandé.
- **Contenu de test « autorisé »** : le seul E2E réel existant télécharge depuis la source voir-anime.to (rapports V1). Pour V2 il faut un contenu dont la redistribution est autorisée (fixture locale HLS générée par ffmpeg — fournie avec `source_audit/tools/ffmpeg`).
