# V2_TEST_PLAN — plan de tests du CORE MEDIA ENGINE + bot utilisateur + admin

Commande unique (depuis `v2_automation/`) : `python -m pytest tests/unit -q -p no:cacheprovider`
V1 (POC) : `cd v1_poc && python -m pytest tests -q`.  Règle 66 : V1 puis V2 après chaque grande modification.

Convention : chaque test s'exécute sur une **vraie base SQLite** et les **vrais parseurs** de la source. Seuls deux bords réseau sont
remplacés : le site source (une chaîne HTML) et Telegram (un transport qui enregistre les appels). **Aucun de ces tests ne prétend
prouver un aller-retour Telegram réel** : voir §C.

## A. Tests unitaires

| Sujet (prompt §) | Fichier · tests | Ce qui est vérifié |
|---|---|---|
| Parser (§12, §61) | `test_user_bot.py` · `test_all_formulations_normalise_to_the_same_query`, `test_parser_extracts_season_episode_version`, `test_title_ending_with_a_number_keeps_a_fallback` | « one piece 1150 / E1150 / Episode 1150 / ep 1150 / e.1150 » → même requête normalisée ; casse, accents ; saison, version, « dernier » |
| media_key (§3-4) | `test_core_media.py` · `test_media_key_*` | déterministe, insensible à la casse ; VF ≠ VOSTFR ; épisode/anime/saison distincts ; film sans numéro → URL |
| Transitions d'état (§5-6) | `test_core_media.py::test_media_state_view_and_expired`, `test_request_transitions` ; `test_states.py` ; `test_private_media.py::test_ready_state_machine` | 11 états média (vue sur `episodes.status`), 11 états de demande, transitions interdites refusées |
| Déduplication (§3, §25) | `test_core_media.py::test_three_users_one_media_one_download` | 3 utilisateurs → 1 ligne média, 1 job en file, 3 items, événements `created, joined, joined` |
| Request manager (§7-8, §18-19, §26-27) | `test_core_media.py` (limite 1 active, saison = 1 requête, attente/expiration, dernier épisode, annulation, historique) | y compris limite appliquée **par la base** (index unique partiel) |
| Queue manager (§30-32) | `test_queues_scheduler.py`, `test_fifo_parallel.py`, `test_core_media.py::test_season_is_one_request_with_one_job_per_episode` | FIFO par anime, tête unique, parallélisme inter-anime |
| Cleanup (§39, §56) | `test_cleanup.py`, `test_worker_user_side.py::test_cleanup_*` | J+14 sur horloge contrôlée, fichier seul supprimé, DB + réf. Telegram conservées, rejouable après restart |
| Permissions (§44) | `test_v2_admin.py` (allowlist Telegram, 401 sans session, session forgée/expirée) | |
| Membership (§10-11, §60) | `test_user_bot.py::test_non_member_*`, `test_member_of_any_*`, `test_user_who_leaves_*`, `test_membership_that_cannot_be_checked_*`, `test_no_required_channel_*` | membre / non-membre / quitte / revient / vérification impossible ≠ « oui » |
| Erreurs classifiées (§48) | `test_core_media.py::test_transient_source_error_*`, `test_delivery.py::test_blocked_bot_*` | codes `TIMEOUT`, `TELEGRAM_ERROR`, `STORAGE_ERROR`, `EPISODE_NOT_AVAILABLE`… |
| Retry contrôlé (§49) | `test_delivery.py::test_transient_error_is_retried_with_backoff_then_exhausted` | `attempt_count`, backoff, plafond, pas de boucle infinie |

## B. Tests d'intégration

| Sujet | Fichier | Portée |
|---|---|---|
| DB + migration v1→v4 + backfill `media_key` | `test_db.py::test_migrations_v1_to_v2_upgrade_path` | base d'un ancien schéma migrée en place |
| Watcher + utilisateurs = un seul job (§57) | `test_core_media.py::test_watcher_detects_then_user_requests_same_media`, `test_user_requests_then_watcher_detects_same_media`, `test_concurrent_users_never_create_two_jobs` (8 threads, 8 connexions) | les deux ordres + course |
| Downloader + storage + état READY | `test_private_media.py` | média privé : téléchargement, validation, arrêt à READY, file libérée |
| Abstraction Telegram | `test_delivery.py::test_standard_api_refuses_a_file_over_50mb…`, `test_publisher_is_transport_agnostic`, `test_bot_token_is_scrubbed_from_errors` | même `send_video`, deux transports |
| Livraison privée (§24-25) | `test_delivery.py` | N utilisateurs = 1 upload + N envois ; copie depuis le canal ; `file_id` après nettoyage |
| Crash recovery (§22-23, §52) | `test_delivery.py` (matrice) | voir ci-dessous |
| Multi-canal (§36-37, §59) | `test_channels_config.py` | copie miniature+vidéo vers chaque canal, une branche en panne n'arrête pas l'autre |
| Configuration (§10, §46) | `test_channels_config.py::test_env_overrides_*` | canaux, canaux requis, délais depuis l'environnement |
| Worker + côté utilisateur (§30, §58) | `test_worker_user_side.py` | 3 anime en parallèle, un anime en échec ne bloque pas les autres |
| Bot complet (§14-18, §27) | `test_user_bot.py` | recherche, saisons réelles, version, sélecteur paginé, dernier épisode, refus 2ᵉ demande, notifications |
| Panel + admin Telegram (§41-45, §63) | `test_v2_admin.py` | login, actions qui modifient réellement la DB, confirmations, audit |

### Matrice de crash (§52)
| Point de crash | Test | Attendu |
|---|---|---|
| après téléchargement | `test_crash_after_download_or_before_publication_reuses_the_file[downloaded]` | fichier réutilisé (0 re-téléchargement), publié 1 fois |
| avant publication | `…[validated]` | idem |
| après publication (ACK enregistré) | `test_crash_after_publication_completes_from_the_record_without_resending` | épisode terminé depuis l'enregistrement, **0 envoi**, plus de « FAILED manuel » |
| après publication sans enregistrement | `test_crash_publishing_without_a_record_still_goes_to_manual…` | jamais de renvoi automatique |
| avant livraison privée | `test_crash_before_private_delivery_sends_once_after_restart` | 1 envoi, jamais 2 |
| pendant/après livraison privée | `test_crash_during_private_delivery_is_uncertain_and_never_auto_resent`, `test_restart_does_not_resend_a_sent_delivery` | `uncertain` sans renvoi ; `sent` jamais renvoyé |
| pendant une copie multi-canal | `test_crash_mid_copy_is_uncertain_and_never_copied_again` | idem |

## C. E2E

| Niveau | Fichier | Statut |
|---|---|---|
| **Binaires réels** : source (page factice) → requête/watcher → **vrai** téléchargement HLS (ffmpeg) depuis une origine locale → **vrai** ffprobe → publication canal / READY → livraisons | `test_core_pipeline_real.py` (contenu = mire ffmpeg générée localement, aucune redistribution) | exécuté ; Telegram = transport enregistreur |
| **Telegram réel** : envoi privé à un compte de test par `@OtakuuVerse_bot` | — | **BLOCKED** : `USER_BOT_TOKEN` absent de `.env` (les deux bots configurés sont `BestAnime32_bot` et `botadmin32_bot`) |
| Sondes réelles en lecture seule (getMe local, getChatMember réel du canal requis) | script `probe_real.py` (voir `V2_TEST_REPORT.md`) | exécuté le 2026-09-21 |

## D. Local Bot API (§64) — voir `V2_LOCAL_BOT_API_POC_REPORT.md`
