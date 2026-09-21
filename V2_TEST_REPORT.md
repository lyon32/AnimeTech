# V2_TEST_REPORT — résultats réels (2026-09-21)

Chaque ligne = une exécution réelle décrite ici. Statuts : TESTED · MEASURED · NOT_EVALUATED · INCONCLUSIVE · BLOCKED.
Plan et cartographie prompt→tests : `V2_TEST_PLAN.md`.

## 1. Non-régression (règle 66)

| Suite | Avant mes modifications | Après (exécution finale) |
|---|---|---|
| `v1_poc` (`python -m pytest tests -q`) | 22 passés | 22 passés (`v1_poc` non modifié) |
| `v2_automation/tests/unit` (watcher + panel + bot admin existants) | 390 passés, **1 échec préexistant** | **511 passés, 0 échec** (exécution finale seule, 7 min 25) |

- L'échec préexistant `test_config.py::test_no_real_secrets_in_repo_examples` venait du placeholder `<COLLER_LE_TOKEN_DE_BOTFATHER_ICI>` (commit 957b6f8) que l'heuristique du test
  (« contient un x ») jugeait « vraie valeur ». Cause corrigée dans le **test** en reconnaissant explicitement les placeholders `<…>` (un vrai jeton n'en contient jamais) — le test n'est pas désactivé et vérifie toujours les trois `.env.example`.
- Deux tests existants ont été **adaptés, pas contournés** : `test_db.py::test_migrations_v1_to_v2_upgrade_path` attendait la liste de migrations `[2, 3]` (désormais `[2, 3, 4]`, et vérifie en plus le backfill de `media_key`).
- Flakiness observée (préexistante) : lors de deux exécutions **concurrentes** (CPU saturé), `test_site_feed.py` a fait « access violation » au démontage : la fixture ferme la connexion pendant qu'un thread de découverte s'exécute encore. Non reproduit en exécution seule (3×9 tests + suite complète verts) ; non corrigé (hors périmètre), à traiter séparément.
- Aucun test V1/watcher n'a été supprimé, ignoré ni affaibli ; aucune cause de régression à corriger n'est apparue.
- **Base de production** : non touchée (tous les tests utilisent des bases temporaires ; vérifié : `data/v2.sqlite3` est toujours au schéma v3).

## 2. Nouveaux tests (120)

| Fichier | Tests | Portée |
|---|---|---|
| `test_core_media.py` | 26 | media_key, états, dédup, requêtes, saison, versions, expiration (horloge contrôlée), watcher+utilisateurs (2 ordres + 8 threads), annulation, historique |
| `test_private_media.py` | 4 | média privé : `READY`, aucun envoi canal, file FIFO libérée, machine d'états |
| `test_delivery.py` | 17 | livraison privée, `file_id`/copie/upload, matrice de crash, échecs classifiés, backoff, transports |
| `test_user_bot.py` | 35 | parser, recherche (page réelle capturée), membership, conversation complète |
| `test_channels_config.py` | 8 | multi-canal, configuration par environnement |
| `test_worker_user_side.py` | 6 | vraie boucle worker (3 anime en parallèle, un échec n'en bloque pas d'autre), cleanup J+14 |
| `test_core_pipeline_real.py` | 2 | **binaires réels** : vrai téléchargement HLS (ffmpeg) + vraie validation ffprobe + livraisons |
| `test_v2_admin.py` | 22 | authentification, actions réelles du panel et du bot admin, confirmations, audit |

## 3. Réponses aux tests demandés (§51-64)

| § | Test | Résultat |
|---|---|---|
| 51 | A, B, C → média X | `download_count = 1`, `delivery_count = 3` ; événements `[MEDIA] created, joined, joined` ; avec **vrai téléchargement** : `seg_0.ts` lu **1 fois**, **1 upload**, 3 livraisons envoyées — **TESTED** |
| 52 | crash ×5 | 8 scénarios, aucun doublon (voir `V2_TEST_PLAN.md`) — **TESTED**. Cas « pendant l'envoi privé » : `uncertain`, **pas de renvoi** (choix documenté) |
| 53 | saison 3 épisodes | `request_count = 1`, `media_jobs = 3`, FIFO E01 d'abord — **TESTED** |
| 54 | E01 VF / VOSTFR | 2 médias, 2 clés ; 5 utilisateurs sur E01 VF → 1 téléchargement — **TESTED** |
| 55 | expiration 20 min | 19 min : attend ; 21 min : `EXPIRED` ; utilisateur libéré ; durée configurable — **TESTED** (horloge injectée, aucune attente réelle) |
| 56 | cleanup J+14 | 13 j 23 h : conservé ; 14 j : fichier supprimé ; ligne DB, `video_message_id`, `publications` conservés ; rejouable ; fenêtre configurable — **TESTED** |
| 57 | V1 + V2 concurrents | un seul job dans les 2 ordres et sous 8 threads — **TESTED** |
| 58 | plusieurs anime | A E01 / B E05 / C E08 : 3 jobs, `max_running = 3`, un anime en retry ne bloque pas les autres — **TESTED** (worker réel, traitement simulé par un temps d'attente) |
| 59 | multi-canal | canaux A et B (aucun codé en dur) ; miniature puis vidéo copiées ; panne d'un canal isolée ; jamais copié 2 fois — **TESTED** (transport simulé) |
| 60 | membership | membre / non-membre / quitte / revient / vérification impossible (jamais un « oui ») — **TESTED** ; **appel réel** `getChatMember(@spy_family_2025, <id opérateur>)` = `administrator` (0,48 s) — **TESTED** |
| 61 | recherche | 5 formulations → même requête normalisée ; « One Piece » seul → demande la saison — **TESTED** ; **recherche live** `bleach` sur la source : 10 résultats, série « BLEACH: Sennen Kessen-hen » → 48 épisodes listés (1→48), 1,74 s — **MEASURED** |
| 62 | historique | `/history` lit la DB (`✅ / ⏳ / 🚫`) — **TESTED** |
| 63 | panel | login, rate-limit, cookie forgé/expiré, dashboard des demandes, **retry, pause, resume, force-check, désactiver/réactiver un anime, download-now, send-to-user, republish, annulation, relance de livraison** : chaque action vérifiée **dans la base**, confirmations 428, audit — **TESTED**. Rendu visuel de `v2.html` dans un navigateur — **NOT_EVALUATED** |
| 64 | Local Bot API | voir `V2_LOCAL_BOT_API_POC_REPORT.md` : `LocalBotAPITransport.getMe` réel **TESTED**, gros fichiers **MEASURED** (campagnes antérieures), API standard **BLOCKED** (DNS), envoi réel par le nouveau transport **NOT_EVALUATED** |

## 4. Sondes réelles exécutées (lecture seule, aucun message envoyé, jeton jamais affiché)

```
serveur Local Bot API .......... getMe OK (0,26 s / 0,14 s), version "Bot API 10.3"          MEASURED
bots configurés ................ BestAnime32_bot (canal), botadmin32_bot (admin) ; USER_BOT_TOKEN absent
api.telegram.org (standard) .... getaddrinfo failed (DNS)                                     BLOCKED
getChatMember @spy_family_2025 . administrator (0,48 s)                                       TESTED
recherche live voir-anime.to ... 10 résultats, listing de 48 épisodes en 1,74 s              MEASURED
```

## 4 bis. E2E Telegram RÉEL avec `BestAnime32_bot` (décision de l'opérateur : ce bot sert de bot utilisateur)
Mire ffmpeg de 2 s (29 694 o, ffprobe VALID), demande créée par `RequestManager`, livraison par le vrai `LocalBotAPITransport` (serveur local, `docker cp` + `file://`) vers le chat de l'opérateur :
```
livraison 1 : sent, méthode upload,  message_id 5, file_id conservé, 2,71 s, demande -> COMPLETED      TESTED
livraison 2 (même média) : sent, méthode file_id, 0,30 s (aucun octet renvoyé)                        TESTED
appartenance @spy_family_2025 par ce bot : administrator                                              TESTED
```
Limites : base temporaire (pas la production), téléchargement source non rejoué (couvert par `test_core_pipeline_real.py`), petit fichier ; la boucle `getUpdates` du bot (conversation) n'a pas été lancée sur Telegram.

## 5. Ce que ces tests NE prouvent PAS
- La **conversation** sur Telegram réel (`run_user_bot`, boutons) : **NOT_EVALUATED** (routeur testé avec un Outbox simulé). `@OtakuuVerse_bot` lui-même n'est pas utilisé : `config.yaml` → `user_bot.use_channel_bot: true` fait servir `BestAnime32_bot`.
- Le comportement de l'API Bot **standard** distante (poste sans résolution DNS de api.telegram.org) : **BLOCKED**.
- Le transport de gros fichiers par le **nouveau** `LocalBotAPITransport` (les mesures 1800 MiB datent des campagnes V4/V5 sur `V2TelegramClient`) : **NOT_EVALUATED**.
- La charge (dizaines d'utilisateurs simultanés sur un vrai serveur) : **NOT_EVALUATED**.
- Le rendu de `web_ui/v2.html` dans un navigateur : **NOT_EVALUATED**.
- Le « parallélisme » du worker est prouvé avec un traitement simulé (temps d'attente) ; le vrai `DownloadManager` a été exercé en mono-épisode avec ffmpeg réel.
