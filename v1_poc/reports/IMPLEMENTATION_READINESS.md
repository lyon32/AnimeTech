# IMPLEMENTATION_READINESS — V1 COMPLETION + V2 TELEGRAM AUTOMATION

Date: 2026-09-18
Auteur: audit auto (Phase 0, ne modifie rien)
Projet hote: `C:\Users\DELL\Desktop\Anime`

## 1. Perimetre de l'audit

Sources de verite consultee (interdites de re-decouverte — reutilisables telles quelles):

- `source_audit/docs/SOURCE_AUDIT_FINAL_CLOSURE_REPORT.md` (Verdict FINAL: PROVEN)
- `source_audit/docs/SOURCE_TECHNICAL_REPORT.md`
- `source_audit/docs/MASTER_PLAN.md`
- `source_audit/docs/SOURCE_PERMISSION_VERIFICATION.md`
- `v1_poc/reports/V1_POC_REPORT.md` (Verdict: PROVEN, bloque BOT 50 MiB)
- `v1_poc/reports/V1_POC_CLOSURE_REPORT.md`
- `v2_local_bot_api_poc/output/reports/V2_LOCAL_BOT_API_POC_REPORT.md` (Verdict: PROVEN)

## 2. Environnement machine (mesures du jour)

| Element | Valeur | Statut |
|---|---|---|
| Python | 3.14.7 | OK |
| python-telegram-bot | 22.8 | OK (supporte base_url local) |
| psutil | 7.2.2 | OK |
| httpx / python-dotenv / PyYAML | installes | OK |
| `source_audit` (package pip) | importe par v1_poc via `file://../../source_audit` | OK |
| Docker Desktop | 29.2.1 WSL2 | OK |
| Image `v2-telegram-bot-api:latest` | presente (189MB) | OK |
| Conteneur `v2-telegram-bot-api` | **en cours d'execution** (local, 127.0.0.1:8081/8082) | OK |
| Volume `v2-telegram-bot-api-data` | presente | OK |
| RAM totale | 23,8 Go | OK |
| Disque C: libre | ~47,6 Gio (~51,1 Go) | **serve de budget aux grands fichiers** |
| Git | aucun repo (`source_audit`, `v1_poc`, `v2_local_bot_api_poc` non versions) | NOT_TESTED |
| ffmpeg / ffprobe | requis pour downloader/validator | a verifier |

## 3. Baseline de tests (executee aujourd'hui, sans modification)

- `source_audit` unit: **114 passed** (1,37s). Integration: 24 passed + 23 network skipped (per SOURCE closure error).
- `v1_poc` unit: **21 passed** (3,15s).

## 4. Matrice de readiness (STATUT: EXISTANT / FONCTIONNEL / PROVEN / ABSENT / BROKEN / NOT_TESTED / BLOQUE)

### 4.1 Module detection / ingestion source — source_audit (PROVEN, REUTILISABLE)

| Fonctionnalite | Fichier | Statut |
|---|---|---|
| HTTP client (retry, robots, throttling, cache-aware) | `src/source_audit/fetch/http_client.py` | EXISTANT, PROVEN |
| Discovery homepage + `/page/N/` | `analysis/homepage.py::parse_homepage` | EXISTANT, PROVEN |
| Parcours anime (metadata, saisons) | `analysis/anime.py::parse_anime_page` | EXISTANT, PROVEN |
| Parcours episode (miroir embeds) | `analysis/episode.py::parse_episode_page` | EXISTANT, PROVEN |
| Parcours embed / player (HLS master) | `analysis/player.py::parse_embed_page` | EXISTANT, PROVEN |
| Manifest HLS (renditions) | `analysis/media.py::parse_hls_master_manifest` | EXISTANT, PROVEN |
| Identite / cles anime_key + episode_key / canon URL | `analysis/identity.py` | EXISTANT, PROVEN |
| Detection langue (suffixe/prefixe + badge VF + confidence) | `analysis/homepage.py::detect_language` | EXISTANT, PROVEN |
| Signal cache (TTL homepage >= 138 min, pages anime >= 231 min) | `detection/cache_signal.py` | EXISTANT, PROVEN |
| Dedup croise (canon URL, slug anime, media hash) | `deduplication.py` | EXISTANT, PROVEN |
| Ordre intra-anime strict (E01 avant E02) | `ordering.py` | EXISTANT, PROVEN |
| Restart recovery / reprise | `restart_recovery.py` | EXISTANT, PROVEN |
| Fingerprint structure + structure_change gate | `detection/fingerprint.py` | EXISTANT, PROVEN |
| Models (Confidence, HomepageEntry, Language, ...) | `models.py` | EXISTANT, PROVEN |

### 4.2 Pipeline de telechargement / validation — v1_poc (PROVEN, REUTILISABLE)

| Fonctionnalite | Fichier | Statut |
|---|---|---|
| Orchestrateur de bout en bout | `pipeline.py::run_end_to_end` | EXISTANT, PROVEN |
| ECG/status (PASS/FAIL/BLOCKED/SKIPPED) | `pipeline.py` | EXISTANT |
| Extraction source (embeds -> player -> HLS) | `source_client.py` | EXISTANT, PROVEN |
| Telechargement segments HLS + mux MP4 + mesures | `downloader.py` | EXISTANT, PROVEN |
| Validation ffprobe (codecs, continuite, duree) | `validator.py` | EXISTANT, PROVEN |
| SHA-256 fichier + excerpt 45s | `validator.py::truncate_copy` | EXISTANT, PROVEN |
| Client Telegram (sendVideo, verify delivree) | `telegram_client.py` | EXISTANT, PROVEN |
| Classification erreurs Telegram (FILE_TOO_LARGE, ...) | `telegram_client.py::classify_error` | EXISTANT, PROVEN |
| Legende (anime/episode/langue/resolution) | `telegram_client.py::caption_for` | EXISTANT |
| Etat publie (JSON PublishedState) | `state.py` | EXISTANT, PROVEN |
| Config / secrets / evidence / logs | `config.py`, `env.py`, `evidence.py`, `logging_config.py` | EXISTANT |

### 4.3 Local Bot API — v2_local_bot_api_poc (PROVEN)

| Fonctionnalite | Statut |
|---|---|
| Serveur local `telegram-bot-api` 10.3 (Docker) | PROVEN (build officiel, commit e3e9dd8) |
| Mode `--local` (upload direct, bypass cloud 50 MiB) | PROVEN |
| Upload 10 / 100 / 300 / 500 / 700 MiB | PROVEN |
| Upload video reelle 554 319 971 B (Black Torch 10 VF) | PROVEN |
| getFile absolu + sha256sum cote serveur | PROVEN |
| Pas de dedup serveur par contenu (message_id distinct) | PROVEN |
| Persistance restart + stats 8082 | PROVEN |
| **Limite officielle 2000 MB (2 GB)** | **NOT_TESTED** (disque ~33,5 Go libre a l'epoque) |
| Campagne grands fichiers (700/1024/1500/1900/2000/2001) | **AUDIT -> OBJECTIF Phase 6** |
| /file (download) en mode local | 404 -> download via getFile+sha256sum (documented) |

### 4.4 BLOCS MANQUANTS pour la V2 (A CONSTRUIRE)

| Build-block | Statut | Reconstruction |
|---|---|---|
| DB SQLite (migrations, audio_tables) | ABSENT | Phase 1 |
| Schema DB : episodes, publications, files, queue | ABSENT | Phase 1 (UNIQUE(source, episode_key), UNIQUE(episode_id, publication_type)) |
| Configuration centralisee + capacites bot (upload_limit enracine) | PARTIEL (config.py v1) | Phase 1 |
| Machine a etats + etats THUMBNAIL_* + FAILED/RETRY_WAIT/STRUCTURE_CHANGED | ABSENT | Phase 5 |
| Dedup renforcee (episode_key + source + canonical_url + animes + hash) | PARTIEL (v1 dedup JSON) | Phase 5 |
| Files par anime (FIFO strict intra, independant inter) | ABSENT (v1: single-shot) | Phase 2 |
| Scheduler concurrence dynamique (pas MAX_WORKERS=10 statique) | ABSENT | Phase 2 |
| Disk guard (avant telechargement) | ABSENT | Phase 2/3 |
| DownloadManager (etat DOWNLOADED distinct de VALIDATING) | ABSENT | Phase 3 |
| Publication: message thumbnail PUIS message video | ABSENT | Phase 4 |
| Retry wipe 24h puis FAILED + notif admin | ABSENT | Phase 3 |
| Admin panel web (FastAPI) | ABSENT | Phase 7 |
| Admin Telegram (InlineKeyboard, confirmations, multi-ADMIN_TELEGRAM_IDS) | ABSENT | Phase 8 |
| Monitoring (queue depth, disk, debits, failures) | V2 stats 8082 partial | Phase 9 |
| Cleanup 14 jours (conserve messages Telegram) | ABSENT | Phase 10 |
| Restart/recovery complete (resume DOWNLOADED, pas de re-publication thumbnail, reconciliation post-video) | PARTIEL (source_audit.restart_recovery) | Phase 11 |
| Logs + preuves executions consolidees | PARTIEL | Phase 12 |
| Tests E2E + big-file campaign | ABSENT | Phase 6 + 13 |

## 5. Constats / risques reveles par l'audit

1. **SECRETS DANS .env.example** — `v1_poc/.env.example` ET `v2_local_bot_api_poc/.env.example` contiennent
   des VALEURS REELLES (TELEGRAM_BOT_TOKEN / API_ID / API_HASH) au lieu de placeholders, et ces fichiers
   sont explicitement EXCLUS du .gitignore (`!*.env.example`). **A corriger en Phase 1** (ne jamais exposer le token).
2. **Pas de repo git** — aucun historique de conservation. Les preuves d'execution doivent rester les sources de verite.
3. **Mojibake docstring (cosmetique uniquement)** — `source_audit/src/source_audit/analysis/homepage.py` contient
   des caracteres `�?"` (em-dash corrompu) dans des docstrings. Aucun impact TDD (114 unit tests passent). Seul
   config.yaml du projet = `v1_poc/config/config.yaml` (propre). source_audit n'est PAS modifie (regle).
4. **Disque C: ~47,6 Gio libres** — budget pour Phase 6 (2000 MB+). Contraintes a gerer: suppression des blobs locaux
   ET suppression cote serveur (/data du conteneur) apres chaque test via getFile->rm, sinon espace sature.
5. **2000 MB = hypothese** (limite documentee), jamais testee. Decision a trancher en Phase 6 :
   DOCUMENTED_LIMIT / PROVEN_LIMIT / FIRST_FAILED_SIZE. Si refus > 2000 MB verifie, pas de workaround non documente :
   la limite devient un BLOCKED/FAILED avec notification admin.
6. **`state.py` v1 = JSON file** (1 episode max) — a remplacer par SQLite multi-episode en Phase 1 sans perdre la compat
   (reconciliation: republier/verifier les episodes deja publies = resilience V1 pas a re-tester).
7. **Structure-change gate** : `fingerprint.py` DOIT rester actif entre discovery et download (block STRUCTURE_CHANGED).
8. **Media et droits** : droits declares sur l'integralite des videos (V1 closure). Pour la campagne Phase 6, blobs
   synthetiques autorises ; un seul fichier video conforme Hors-ligne (Black Torch eps 10 VF, deja upload msg 26) disponible.

## 6. Plan de phases (automatique)

| Phase | Contenu | Base reutilisee |
|---|---|---|
| 1 | Config centralisee + capacites bot + DB SQLite (schema queues/publications) + .env sanitaire | config.py v1, .env, Docker v2 |
| 2 | Files par anime (FIFO) + scheduler dynamique + disk guard | ordering.py, cache_signal.py |
| 3 | DownloadManager (telechargement+validation+retry 24h->FAILED) | downloader.py, validator.py |
| 4 | Publication thumbail->video, etats PUBLISHING_THUMBNAIL/THUMBNAIL_PUBLISHED/PUBLISHING_VIDEO | telegram_client.py |
| 5 | Machine a etats complete + dedup renforcee (DB UNIQUE) | deduplication.py, identity.py |
| 6 | Campagne grands fichiers 700/1024/1500/1900/2000/2001 MB + decisions de limites | run_upload_tests.py v2 |
| 7 | Panel admin web (FastAPI, /queues /anime /errors /history /dashboard) | — |
| 8 | Admin Telegram (InlineKeyboard + confirmations, ADMIN_TELEGRAM_IDS) | telegram_client.py |
| 9 | Monitoring / metriques / dashboards | stats 8082 v2 |
| 10 | Cleanup 14 jours (conserve messages) | — |
| 11 | Restart/recovery + reconciliation crash publication | restart_recovery.py |
| 12 | Logs + preuves d'execution consolidees | evidence.py, logging_config.py |
| 13 | E2E complet (CI-like) | — |
| 14 | V2_FINAL_IMPLEMENTATION_REPORT.md + V2_TEST_MATRIX.md + V2_KNOWN_LIMITATIONS.md | — |

Tout est reconstruisible a partir des composants EXISTANT/PROVEN identifies ci-dessus. BLOQUE pour rien.