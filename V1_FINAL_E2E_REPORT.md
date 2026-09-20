# V1 Final E2E Report — mode automatique

Date : 2026-09-20. Portée : surveillance → détection → files → téléchargement → validation → miniature → Telegram → nettoyage.
Statuts : PROVEN / FAILED / BLOCKED / NOT_TESTED / INCONCLUSIVE.

## 1. Executive Summary

Le système tourne maintenant seul : un worker unique lance toutes les 30 minutes la vérification des anime actifs, crée un job uniquement pour un épisode réellement nouveau, traite les épisodes dans l'ordre par anime (plusieurs anime en parallèle, selon CPU / RAM / disque / débit), publie la miniature puis la vidéo, enregistre `published_at` et planifie le nettoyage du fichier local à J+14.

Preuve réelle (worker + scheduler + vrai téléchargement + vrai FFmpeg + vrai Local Bot API + vrai Telegram + vraie base) : l'épisode 36 de Bleach a été détecté automatiquement comme nouveau, mis en file, téléchargé, validé, publié (miniature 136, vidéo 137) sans aucune commande manuelle, puis un crash du worker et un redémarrage n'ont ni doublonné ni perdu quoi que ce soit.

**Statut final : NOT FINALIZED.** 20 des 24 critères du §47 sont démontrés ; les 4 autres ne le sont que partiellement, parce qu'ils demandent du temps réel (scheduler 30 min réel, retry 24 h réel, nettoyage J+14 réel) ou un deuxième anime (téléchargements parallèles réels) ; ils sont listés au §24. L'administration Telegram et les notifications ont été confirmées en réel par le propriétaire le 2026-09-20 (`/status` répond, message d'alerte de test reçu). Aucune case n'est déclarée PASS sans exécution.

## 2. Existing Components Reused (aucune réécriture)

`DownloadManager` (chaîne d'état, retry 24 h, recovery, publication miniature → vidéo), `V2TelegramClient` / `Publisher` / `wait_for_message`, `QueueManager`, `repo` (états, dédup UNIQUE), `DynamicScheduler` (déjà écrit, mais non branché), `cleanup`, `recovery`, `alerts`, `service` (admin partagé), `web` (FastAPI), `admin_telegram`, les parseurs éprouvés de `source_audit` (`parse_anime_page`, `build_episode_key`, `build_anime_key`), `HttpClient`, `download_and_mux`, `extract_thumbnail`. Aucune deuxième file, aucun deuxième scheduler de concurrence, aucune deuxième machine à états (le seul état « baseline » réutilise `discovered`, déjà existant).

**GAP analysis (au départ)** : scheduler 30 min MISSING ; modèle anime PARTIAL ; détection MISSING côté V2 ; FIFO PARTIAL (défaut) ; concurrence dynamique PARTIAL (non branchée) ; panel/Telegram PARTIAL ; le reste PROVEN (rapport `V1_REAL_E2E_REPORT.md`).

## 3. Automatic Scheduler

- Nouveau `discovery.DiscoveryScheduler`, appelé à chaque tour de la boucle du worker (`worker.run_worker`), vérifications sur un petit pool de threads (une source lente ne bloque jamais les téléchargements).
- Un contrôle est réservé par un `UPDATE` atomique sur `animes.last_checked_at` : jamais deux vérifications simultanées du même anime (autre tick, autre processus, force-check pendant un contrôle : fusionnés). Anime A, B, C sont vérifiés indépendamment.
- Un plantage de la surveillance n'arrête pas les téléchargements (test) ; une alerte `scheduler_problem` / `discovery_error` est levée.
- **Un seul worker** : bail `leases` inchangé (voir §14 et §15).
- Statut : **PROVEN** (tests + E2E réel en intervalle court).

## 4. 30-Minute Polling

- Production : `source.poll_interval_seconds: 1800`. Intervalle court `test_poll_interval_seconds: 10` **uniquement** si la variable d'environnement `V2_TEST_MODE=1` est posée (jamais mélangés : test dédié `test_interval_is_30_minutes_in_production_and_short_only_in_test_mode`).
- E2E réel exécuté avec `V2_TEST_MODE=1` pour ce seul processus (contrôle toutes les ~10 s).
- Statut : logique **PROVEN** ; une vraie attente de 30 minutes d'affilée n'a pas été faite : **NOT_TESTED** (par consigne, on ne les attend pas ; les calculs d'échéance sont testés avec 1800 s et 1801 s).

## 5. Episode Discovery

- Modèle `animes` (une seule source de vérité) : `anime_key, title, source_url, enabled, language, created_at, updated_at, last_checked_at, last_successful_check_at` (+ `last_check_error`, `force_check`). Migration v3 testée depuis une base v1.
- Ajout par URL : validation (hôte autorisé, forme `/anime/<nom>/`), identification par une lecture de page (clé `postid:N`), sauvegarde, activation. **Aucun job créé** par l'ajout.
- Premier contrôle = **baseline** : les épisodes déjà listés sont enregistrés (`discovered`), jamais mis en file (option `backfill_latest_on_first_check` pour en prendre N). Un job n'existe que pour un épisode apparu après.
- Nouveaux épisodes d'un anime mis en file en ordre croissant de numéro.
- Statut : **PROVEN** (23 tests + E2E réel : baseline 36 épisodes, 0 job).

## 6. Deduplication

Identité = clé canonique `build_episode_key(url)` (stable) ; contraintes UNIQUE en base ; recherche avant création. Scénarios testés : check #1 puis #2 puis #3 avec le même épisode → 1 épisode, 1 job ; redémarrage entre deux contrôles ; même clé sur deux anime différents distinguée ; cycle hebdomadaire (E01 connu, E02 apparaît → uniquement E02). Réel : après détection du 36, tous les contrôles suivants voient « known » (1 seul épisode 36, 1 seule paire de messages).
Statut : **PROVEN**.

## 7. Per-Anime Queues

Règle corrigée dans `repo.next_heads` : au plus **un épisode actif par anime**, dans l'ordre du **numéro d'épisode** (la position dans la file départage). E02 ne double ni un E01 en cours, ni un E01 en attente de retry ; un épisode plus ancien détecté tard (E36) passe avant un plus récent qui attend sa source (E49). Les entrées de file sont libérées aux états finaux (publié, échec, doublon), sinon elles bloquaient l'anime ; la reprise nettoie l'état laissé par d'anciens runs.
Statut : **PROVEN** (8 tests + intégration + E2E réel : le 36 est passé devant le 49 en attente).

## 8. Parallel Downloads

`DynamicScheduler` branché dans la boucle du worker : capacité = allocation dynamique − épisodes actifs. Signaux : nombre d'anime en attente, disque libre (garde-fou), débit observé (boost ×2 plafonné), **CPU et RAM** (au-dessus de 90 % : un seul téléchargement). Publications Telegram bornées séparément (sémaphore `max_concurrent_publications`). `queues.max_concurrent_downloads` porté à 3.
Tests d'intégration avec un vrai worker : 3 anime simultanés (3 actifs) ; A E01→E02→E03 en ordre pendant que B avance ; CPU saturé → 1 à la fois ; disque bas → aucun démarrage.
Statut : **PROVEN** avec un traitement simulé ; **le parallélisme de plusieurs anime en téléchargement réel n'a pas été exécuté** (un seul anime est configuré) : NOT_TESTED en réel.

## 9. Download

Downloader inchangé et déjà éprouvé. Épisode 36 (réel) : 364 724 237 octets, 74 segments, 86,6 s, MP4 vérifié (existence, taille, intégrité paquets), sha256 enregistré. Cache MP4 réutilisé seulement s'il est valide.
Statut : **PROVEN**.

## 10. Validation

ffprobe (format, durée 1480,5 s, vidéo, audio, résolution 1600x900) ; aucun média invalide n'est publié (VALIDATION_FAILED, tests avec vrais fichiers corrompus / tronqués / sans audio / sans vidéo).
Statut : **PROVEN**.

## 11. Thumbnail

Affiche de la source (`og:image`, 103 894 octets, JPEG vérifié) ; repli sur une image extraite de la vidéo. Fiche anime en légende (titres, type, statut, début, genres, studio, synopsis, `[@otakuu_verse]`).
Statut : **PROVEN**.

## 12. Telegram Publishing

Ordre miniature puis vidéo ; `thumbnail_message_id` et `video_message_id` persistés ; description = format défini (titre, épisode, VOSTFR, qualité mesurée 900p), sans aucune URL ; la miniature n'est jamais reposée, la vidéo jamais renvoyée si un id existe. Réel : épisode 36 → miniature **136**, vidéo **137** ; le message 137 existe avec la légende exacte (sonde non destructive « Message is not modified »).
Statut : **PROVEN**.

## 13. Retry 24H

Historique conservé : `attempt_count`, `first_attempt_at`, `last_attempt_at`, `next_retry_at`, `last_error`, backoff 1 à 60 min dans la fenêtre de 24 h, puis FAILED et alerte. Source non prête (`NOT_AVAILABLE_YET`, épisode 49) : l'épisode reste en attente 30 jours sans alerte répétée, contrôle toutes les 30 min.
Statut : **PROVEN** en tests et sur le cas réel du 49 ; la fenêtre de 24 h complète n'a pas été attendue en réel : NOT_TESTED en réel.

## 14. Crash Recovery

Réel (session précédente et celle-ci) : kill pendant le téléchargement (reprise, une publication), kill pendant l'envoi de la vidéo (`run.py` et worker) : aucun renvoi, la vidéo publiée par le serveur est retrouvée automatiquement ; miniature jamais republiée après un crash entre miniature et vidéo. Bug corrigé : entrée de file restée « en cours » après un kill.
Statut : **PROVEN**.

## 15. Restart Recovery

Réel : worker A tué en pleine activité ; un worker B lancé aussitôt est refusé (`busy`, bail non expiré) ; après expiration, un nouveau worker reprend le bail, aucun doublon (épisode 36 : 1 ligne, 2 publications), 13 publiés, 35 en baseline, le 49 toujours en attente, la surveillance reprend (`last_checked_at` avancé). Aussi : la base partagée entre threads est maintenant sérialisée (voir §21).
Statut : **PROVEN**.

## 16. Cleanup +14 Days

`cleanup_at = published_at + 14 j`, renseigné à la publication (E2E : 36 → 2026-10-04T06:52:40Z) et rattrapé par la reprise pour les anciens épisodes. Le nettoyage ne supprime que le fichier local (vidéo + miniature) ; conserve métadonnées, `published_at`, message ids, historique ; jamais de suppression Telegram ; l'âge se mesure sur `published_at` (jamais `updated_at`) ; sans `published_at` : `CLEANUP_BLOCKED` + alerte. Tests avec de vrais fichiers : 15 jours → supprimé, 13 jours → conservé, `updated_at` ancien mais publié hier → conservé, sans `published_at` → bloqué.
Statut : **PROVEN** avec de vrais fichiers ; un vrai nettoyage à J+14 sur la production n'est pas encore échu : NOT_TESTED.

## 17. Web Admin

Ajouts : `GET/POST /api/animes` (ajout par URL, liste avec dates de contrôle), `/edit`, `/force-check`, `/enable`, `/disable`, `GET /api/jobs` (anime, épisode, progression x/7), `GET /api/system` (CPU, RAM, disque, réseau, worker, intervalle, pause) ; pause / reprise / annulation / retry sur la vraie base. Tableau de bord : système, anime surveillés, jobs actifs, alertes, épisodes ; tout texte venant de la source est échappé ; JavaScript validé avec Node. Réel : les endpoints répondent avec les vraies données ; aucun jeton dans les réponses.
Statut : **PROVEN** (20 tests + vérification réelle des API). Le rendu visuel dans un navigateur n'a pas été vérifié.

## 18. Telegram Admin

Ajouts : `/anime add <url>`, `/anime <clé> check|on|off|title`, `/check <clé>`, `/jobs`, `/system` en plus de `/status /queue /errors /capacity /alerts /pause /resume /retry /cancel` ; réservé aux `ADMIN_TELEGRAM_IDS` (testé : un non-admin est ignoré). Confirmation réelle (2026-09-20) : `ADMIN_TELEGRAM_IDS` configuré, bot d'administration lancé (`run-admin`), `/status` répond au propriétaire. Les autres commandes (`/anime add`, `/check`, `/jobs`, `/system`, `/pause`…) ont été testées avec un faux bot, pas encore en réel.
Statut : **PROVEN** (`/status` réel, autres commandes en tests).

## 19. Alerts

Alerte de nouvel épisode enregistrée à la détection (réel : `new_episode` ep 50) ; erreurs de surveillance et du scheduler dédupliquées par `(type, clé)` et limitées à une notification par heure ; un épisode « pas encore publié » ne génère plus d'alertes ; disque bas → `low_disk`. **Défaut corrigé** : les alertes partaient dans le canal public ; elles ne vont plus qu'aux conversations des administrateurs (sans administrateur : base seulement).
Confirmation réelle (2026-09-20) : un message d'alerte de test envoyé par le dispatcher est arrivé dans la conversation privée de l'administrateur (jamais dans le canal).
Statut : enregistrement, déduplication et envoi réel à l'administrateur **PROVEN**.

## 20. Security

- Serveur Local Bot API : journal détaillé (`--verbosity=3`) retiré, car il écrit le jeton en clair ; ancien journal supprimé ; `start_server.ps1` mis en garde.
- Scan du dépôt (jeton et hash API, hors `.env`) : 3 anciens fichiers de preuve contenaient un secret, expurgés sur place ; re-scan : 0.
- **Incident de session** : mes sorties de commande ont affiché le jeton du bot dans cette conversation (journal serveur, nom de dossier du serveur). **Le jeton doit être renouvelé** (BotFather `/revoke`, mise à jour de `.env` des trois projets, reconnexion du Local Bot API).
- Les logs de l'application n'affichent aucune URL ni secret (test).
Statut : **PROVEN** pour le dépôt ; **jeton à renouveler par le propriétaire**.

## 21. Regression Tests

Avant : v2_automation 185, v1_poc 21, source_audit 114. Après : **237**, **21**, **114**, tous verts. `tests/e2e/test_e2e_live.py` n'a pas été lancé (il publierait une vidéo de sonde dans le canal).
Un test existant a été adapté : la liste de migrations attendue depuis une base v1 est passée de `[2]` à `[2, 3]` (nouvelle migration, avec vérification des colonnes ajoutées).
Défauts trouvés par les tests et corrigés : entrées de file jamais libérées ; un épisode en attente laissait passer le suivant ; deux épisodes du même anime pouvaient tourner ensemble ; ordre par arrivée au lieu du numéro d'épisode ; connexion SQLite partagée entre threads (commit et execute concurrents : `cannot commit - no transaction is active`, `API misuse`, qui pouvaient tuer la boucle du worker) → connexion sérialisée ; gabarit HTML du tableau de bord (échappement `\'` du JavaScript) ; alertes envoyées au canal public.

## 22. E2E Evidence

Épisode 36 (job 50), enchaînement automatique (journal `data/logs/v2_automation.log`) :

| Étape | Heure (local) | Preuve |
|---|---|---|
| Baseline (worker démarré 07:44:10) | 07:44:12 | 36 épisodes connus, 0 job, 12 déjà publiés |
| Nouveau détecté | 07:44:47 | `[DISCOVERY] new=1`, `[QUEUE] episode=36 job=50` |
| Source / rendition / playlist | 07:44:51 | 2 renditions, 1600x900, 74 segments |
| Téléchargement | 07:46:23 | 364 724 237 octets en 86,6 s |
| Validation | 07:46:23 | mp4, 1480,5 s, 1600x900 |
| Miniature | 07:46:27 | 103 894 octets (affiche source) |
| Telegram | 07:52:40 | miniature 136, vidéo 137 |
| Publié | 07:52:40 | `published_at` 2026-09-20T06:52:40Z, `cleanup_at` 2026-10-04T06:52:40Z, file libérée, alerte `new_episode` |

Base : 49 épisodes, 13 `cleanup_pending`, 35 `discovered` (baseline), 1 `retry_wait` (49). Sonde Telegram du message 137 : légende exacte. Un test de kill + reprise a suivi (§15). Sources de preuves : `v2_automation/output/evidence/`, `data/v2.sqlite3`.

## 23. Known Limitations

- Aucune vraie attente de 30 minutes, ni de fenêtre de 24 h, ni de J+14 (calculs prouvés, durées non attendues).
- Un seul anime configuré : le parallélisme entre anime est prouvé avec un traitement simulé dans un vrai worker, pas en téléchargements réels multiples.
- Administration Telegram : seul `/status` est confirmé en réel ; les autres commandes le sont avec un faux bot.
- Disque de la machine à 92 % (40 Go libres) : le garde-fou de 5 Go tient, mais l'espace est à surveiller.
- Un worker relancé moins de 90 s après un arrêt brutal est refusé tant que le bail n'a pas expiré.
- Le débit d'envoi vers Telegram (~0,8 Mo/s mesuré plus tôt) fait durer chaque épisode de 6 à 15 minutes.
- Le rendu visuel du tableau de bord n'a pas été contrôlé dans un navigateur.
- Le panneau web n'a pas d'authentification (liaison locale 127.0.0.1 uniquement, comme avant).

## 24. Final Status

**NOT FINALIZED** (règle du §47 : une case non démontrée interdit FINALIZED).

| Fonctionnalité | Statut | Preuve |
|---|---|---|
| Scheduler 30 min | PROVEN (logique) / NOT_TESTED (30 min réelles) | tests `test_discovery`, `test_auto_worker` ; E2E réel en intervalle court |
| Anime management | PROVEN | migration v3, `service`, web, tests |
| Discovery | PROVEN | E2E réel épisode 36 |
| Deduplication | PROVEN | 3 contrôles + redémarrage, réel et tests |
| Queue par anime | PROVEN | `test_fifo_parallel`, E2E |
| FIFO | PROVEN | 36 avant 49, E02 après E01 |
| Parallel anime | PROVEN (simulé, worker réel) / NOT_TESTED (téléchargements réels) | `test_auto_worker` |
| Download | PROVEN | 364 724 237 o, réel |
| Validation | PROVEN | ffprobe réel, tests |
| Thumbnail | PROVEN | 103 894 o, réel |
| Telegram | PROVEN | 136 / 137 |
| Retry 24h | PROVEN (tests, cas 49 réel) / NOT_TESTED (24 h réelles) | tests, épisode 49 |
| Crash recovery | PROVEN | 3 scénarios réels |
| Restart recovery | PROVEN | worker A/B/C réels |
| Cleanup +14j | PROVEN (fichiers réels, tests) / NOT_TESTED (J+14 réel) | `test_cleanup` |
| Web Admin | PROVEN (API réelles) | `test_web`, appels réels |
| Telegram Admin | PROVEN (`/status` réel ; autres commandes en tests) | `test_admin_telegram`, confirmation du propriétaire |
| Alerts | PROVEN (base + envoi réel à l'administrateur) | `test_alerts`, `new_episode`, message de test reçu |
| Security | PROVEN (dépôt) ; jeton à renouveler | scan, journal serveur |

**Pour passer à FINALIZED** : (1) rejouer en réel `/anime add`, `/check`, `/jobs`, `/system` ; (2) renouveler le jeton du bot ; (3) laisser tourner le worker en production (30 min) au moins un cycle complet et ajouter un deuxième anime ; (4) constater un nettoyage à J+14 et une fenêtre de retry de 24 h.

## 25. Addendum — refonte du panneau Telegram (2026-09-20)

Problème constaté sur la capture du `/status` : jargon brut (`cleanup pending=13 · retry wait=1`), « 49 épisodes » incluant 35 épisodes connus (baseline), limites périmées (800 / 900 Mio, serveur « unknown »), 5 alertes ouvertes invisibles et toutes périmées, aucune information sur l'activité en cours, boutons techniques, aucune navigation.

Réalisé (choix de l'utilisateur : accueil complet par sections, les quatre types de notifications) :
- **Accueil** (`/status`, `/menu`, `/start`) : worker, prochain contrôle, en cours avec progression, en attente (avec la raison en français), derniers publiés, compteurs exacts (baseline exclue), à traiter, disque / CPU / RAM ; heures locales, « il y a X min », « mis à jour HH:MM:SS ». Chaque `/status` envoie un **nouveau** message (jamais d'édition identique) ; la navigation par boutons édite en place et répond « ✓ Déjà à jour » quand rien ne change.
- **Écrans** : Jobs (annuler / relancer, « tout relancer » seulement s'il y a des échecs), Anime (contrôler maintenant, pause / reprise, détail, ➕ ajout par URL), Alertes (titres lisibles, ✅ OK, tout acquitter), Système (barres CPU / RAM / disque, réseau, worker, serveur Telegram, taille max), Notifications (4 interrupteurs). Menu « / » du bot renseigné en français.
- **Alertes** : les messages informatifs (publié, nouvel épisode) sont envoyés mais ne restent plus « ouverts » ; les alertes de problème d'un épisode se ferment quand il est publié ; un rattrapage au démarrage ferme les alertes périmées (5 sur 5 fermées sur la base réelle).
- **Notifications privées** (jamais dans le canal), chacune désactivable : épisode publié (« Bleach E36 publié · 348 Mo »), nouvel épisode détecté, problèmes (sans répétition), résumé quotidien à l'heure locale `monitoring.daily_summary_hour` (9 h par défaut, une seule fois par jour, même après un redémarrage). La clé de configuration est sous `monitoring` (et non `notifications`) pour ne pas modifier la structure de configuration.
- **Sécurité** : tout texte venant de la source est échappé avant envoi en HTML ; données de boutons ≤ 64 octets (testé).
- **Tests** : v2_automation 237 → **269** (nouveaux : `test_admin_views`, `test_notifier`, navigation et actions dans `test_admin_telegram`). Trois tests existants ont dû évoluer avec le nouveau contrat : l'ancien texte de `/status` (« aperçu », « retry wait=1 »), une assertion de l'écran système (« Worker »), et « un nouvel épisode reste ouvert » (désormais informatif, non ouvert). Aucun test n'a été supprimé.
- Statut : logique et rendu **PROVEN** (tests + aperçu réel de chaque écran à partir de la vraie base) ; **rendu dans Telegram : à confirmer par l'utilisateur** (capture après la refonte).
- Limites : l'envoi des notifications « publié », « nouvel épisode » et du résumé quotidien dépend du worker (actuellement arrêté) ; le message de démonstration « épisode publié » n'a pas encore été envoyé.

## 27. Addendum — refonte du panneau web (http://127.0.0.1:8085)
- **Avant** : page cassée (JavaScript en erreur, tableau vide), six liens ouvrant du JSON brut, données périmées (capacités 800/900 Mio, « serveur unknown »), fausse alerte de santé « file bloquée depuis 1023 min ».
- **Après** : huit pages en français (Vue d'ensemble, Épisodes, File, Anime, Erreurs, Capacités, Santé, Réglages), menu latéral (barre du bas sur téléphone), thème sombre à un seul accent, tiroir de détail d'épisode, confirmation en ligne des actions destructrices, états de chargement / vide / erreur, actualisation automatique (suspendue onglet caché). Fichiers : `v2_automation/src/v2_automation/web_ui/{index.html,app.css,app.js}` ; contexte de design : `v2_automation/PRODUCT.md`, `DESIGN.md`.
- **Données corrigées** (`web_data.py`, mêmes sources que le bot Telegram) : santé sans fausse alerte (un épisode qui attend sa source ou une file en pause n'est plus « bloqué »), disque lu en direct, capacités en direct (serveur Telegram joignable, plafond 1 800 Mio, plus gros envoi prouvé 1 805 Mio, envois lus depuis les preuves), compteurs sans la baseline (35 épisodes « déjà connus » à part), aucune URL brute affichée. Le JSON reste sous `/api/*` pour les machines.
- **Sécurité** : les actions POST venant d'une origine autre que localhost sont refusées (403) ; le texte issu de la source est échappé (`html` + `esc`).
- **Tests** : v2_automation 277 → **290** (nouveau `test_web_ui.py` : 13 tests ; deux tests de `test_web.py` adaptés, car la page intégrée a été remplacée par des fichiers statiques). Aucun test supprimé.
- **Vérification** : rendu réel contrôlé sur ordinateur et à 375 px (Vue d'ensemble, Épisodes, File, Anime, Erreurs, Capacités, Santé, Réglages, Plus, tiroir) ; `detect.mjs` : un avertissement (transition de largeur de la jauge), corrigé ; le détecteur a tourné en mode dégradé (analyseur HTML/CSS indisponible), son résultat est donc un minimum et non une preuve.
- Limites : les actions « Relancer », « Annuler », « Acquitter » et l'ajout d'un anime par URL sont testés en unitaire mais pas cliqués sur la vraie base ; « Contrôler maintenant » reste grisé pour l'anime actuel (sans URL source enregistrée) ; les données « en cours » ne se remplissent que lorsque le worker tourne (actuellement arrêté).

## 28. Addendum — contrôle du worker depuis les panneaux
- **Arrêt propre** : bouton « Arrêter le worker » (panneau web, avec confirmation en ligne) et « ⏹ Arrêter le worker » (Telegram, avec message de confirmation). Il pose la demande `worker_stop` dans la table `control` ; le worker ne démarre plus de job, laisse finir ceux en cours (aucune publication coupée, battement de cœur maintenu pendant l'attente), libère son verrou, efface la demande et sort avec le code 0. Un arrêt demandé ne déclenche pas d'alerte « worker arrêté ». Une demande restée d'un run précédent est effacée au démarrage suivant.
- **Démarrage** : bouton « Démarrer le worker » (web et Telegram) → `schtasks /Run /TN V2AutomationWorker`. Les tâches planifiées (worker, panneau web, bot admin ; ouverture de session + relance après plantage ; pas de relance après un arrêt propre) s'installent avec `v2_automation/scripts/install_tasks.ps1` (sans droits administrateur). **Non installées par moi** : c'est un réglage persistant du poste, à lancer par l'utilisateur.
- **Vérifié en réel** : démarrage du worker, `POST /api/worker/stop`, sortie en quelques secondes (`stop_reason: requested`), verrou libéré, demande effacée. Le démarrage par tâche planifiée n'a pas été essayé de bout en bout (tâche non installée) : testé avec un lanceur simulé.
- **Tests** : 290 → **297** (`test_worker_control.py`, 6 ; 1 test Telegram ajouté). Aucun test supprimé ni modifié.
- Limite : après un arrêt brutal du processus (kill), le verrou reste « actif » jusqu'à son expiration (≈ 90 s) ; le panneau l'affiche comme actif pendant ce délai.

# Global Watcher Validation

**Correction d'interprétation.** Bleach n'était qu'un exemple : le watcher est **global**. Inspection avant modification : `due_animes` / `claim_check` chargeaient déjà TOUS les anime actifs (aucun anime codé en dur), le diff « source ↔ base » se fait par clé d'épisode canonique, un job est créé par nouvel épisode, `next_heads` donne un job par anime en parallèle et l'ordre strict à l'intérieur d'un anime. **Ce qui manquait** : la notion de *cycle global* (un anime avait sa propre horloge de 30 min), les logs `[WATCHER]`, le résumé de cycle, et les preuves multi-anime. Corrigé uniquement cela.

**Modes documentés** (`discovery.py`, sans nouvelle règle) : *bootstrap* = premier contrôle réussi d'un anime (épisodes déjà listés connus, aucun job, sauf `backfill_latest_on_first_check` = N) ; *incremental* = tout contrôle suivant (seul ce que la base ne connaît pas devient job).

**Changements** : cycle global atomique (`claim_cycle`, clé `watcher:last_cycle_at`) qui visite tous les anime actifs toutes les `poll_interval_seconds` (**1800 s en production**, 10 s uniquement avec `V2_TEST_MODE=1`, même code) ; un anime ajouté ou forcé est visité tout de suite ; logs `[WATCHER] cycle_started / anime_count= / checking anime= / cycle_finished checked_animes= new_episodes= jobs_created= errors=` et `[DISCOVERY] discovered_count= new_count= (mode=…)` ; résumé du dernier cycle conservé (`watcher:last_cycle`) et affiché dans le panneau web (« Dernier cycle »).

**Deux défauts réels trouvés en prouvant le parallélisme, corrigés :**
1. *Connexion SQLite partagée* : sous jobs parallèles, une lecture pouvait renvoyer « rien » ou un ancien statut (une transition d'état perdue → « interdit: validating → publishing_thumbnail »). Reproduit sur l'ancienne connexion : **104 anomalies / 1 800 lectures**, 0 après correctif (`db.SafeConnection` lit les résultats sous verrou). Test de non-régression ajouté.
2. *Dossier de segments partagé* : tous les téléchargements parallèles écrivaient dans le même `download_work/segments/seg_XXXXX.ts` (mélange possible des vidéos, puis `FileNotFoundError` quand le premier terminait). Désormais un dossier par épisode (`ep_<id>`). Test ajouté.
(Aussi : le filtre de masquage des tokens plantait sur un log à argument dict — corrigé.)

| Critère | Preuve |
|---|---|
| Watcher global, tous les anime actifs parcourus | tests + E2E réel : 6 anime vérifiés à chaque cycle |
| Nouveaux épisodes détectés automatiquement, plusieurs dans un cycle | E2E réel : cycle 2 → `new_episodes=3 jobs_created=3` (3 anime différents) ; test A/B/C (B E06, C E11, rien pour A) |
| « Aucun nouvel épisode » n'est pas une erreur | 103 cycles réels à `new_episodes=0 errors=0` |
| Un anime en échec n'arrête pas les autres | test (503 sur un anime, 4 autres vérifiés) |
| FIFO dans un anime, parallèle entre anime | tests (E01 avant E02 ; A, B, C ensemble) ; E2E réel : 3 jobs démarrés ensemble |
| Retry 24 h ne bloque pas les autres anime | test ; E2E réel : un épisode en `retry_wait` (manifeste source 404, 3 essais) pendant que 2 autres anime étaient publiés |
| Cleanup global J+14, fichiers seulement | test : 3 anime nettoyés (15 j), 2 conservés (3 j), ids de messages Telegram inchangés |
| Redémarrage / déduplication | test : 2ᵉ scheduler sur la même base → 0 doublon ; 1 seul cycle par intervalle même à 2 processus |
| Scheduler de production = 30 min | `poll_interval_s` = 1800 (test) ; test court seulement avec `V2_TEST_MODE=1` |

**E2E réel** (`scripts/global_watcher_e2e.py`, preuves : `output/evidence/global_watcher/`) : 6 anime réels de la source ; l'événement « nouvel épisode » est produit en présentant la source dans deux états (dernier épisode masqué au cycle 1 = baseline, visible ensuite). Cycle 1 : 6 vérifiés, 0 job. Cycle 2 : 6 vérifiés, **3 nouveaux, 3 jobs**. Épisodes publiés : 2 (~440–470 Mo chacun, miniature puis vidéo) ; 1 en retry (source 404) ; 1 essai supplémentaire (`DOWNLOAD_FAILED` dû au défaut n°2, puis réussi). Le watcher a continué : 105 cycles pendant les téléchargements. **Livraison** : base temporaire, envoi dans le chat privé de l'administrateur (bot admin), pas dans le canal — choix que j'aurais dû annoncer avant.

**Limites honnêtes.** Les deux vidéos de ce run ont été téléchargées **avant** le correctif n°2 : leur contenu n'est pas garanti (validation de durée OK seulement). Le parallélisme réel avec téléchargements distincts n'est donc pas encore prouvé proprement ; retry 24 h complet et cleanup J+14 réels restent prouvés par tests, pas par le temps.

**Conclusion : détection du watcher global — PROVEN (sources réelles). GLOBAL WATCHER — PROVEN au sens complet : NON, en attente d'un second run réel après correctif.** Tests : 310 (v2_automation).

# Global Watcher Validation — run 2 (canal public, base de production)

Règles du propriétaire appliquées : (1) uniquement les épisodes sortis **aujourd'hui** (depuis minuit, heure locale), jamais les anciens ni hier, jamais de doublon au cycle suivant ; (2) pas de miniature sans vidéo accessible, réessais silencieux, **un** signalement après 20 min ; (3) tout épisode détecté est publié, sans plafond ; (4) le cycle ne s'arrête jamais ; (5) publication dès qu'un épisode est prêt, **une seule à la fois**, dans l'ordre d'arrivée, miniature + vidéo en entier ; (6) un problème sur un anime ne bloque pas les autres.

**Changements** : `release_date.py` (formats « 3 seconds ago » et « September 12, 2026 », aussi en français ; doute = pas aujourd'hui) ; rattrapage du jour au premier contrôle (`source.catchup_today`) ; `PublishGate` (porte équitable, libérée dans tous les cas) à la place du verrou simple ; alerte « Vidéo inaccessible depuis N min » (`downloads.persistent_error_alert_minutes: 20`, une par épisode, jamais pour « pas encore prêt ») à la place de l'alerte au premier échec ; historique de cycles (20 derniers) ; panneaux web et Telegram mis à jour (Aujourd'hui, prochain cycle, écran/section Cycles).
**Défaut réel corrigé en prouvant la règle 4** : la limite de téléchargements dynamique ne comptait que les anime *en attente* ; un épisode détecté pendant un téléchargement attendait donc la fin du précédent quand un seul anime était actif. Elle compte maintenant les anime en attente **et** en cours (`QueueManager.busy_anime_count`).
**Nettoyage** : 6 messages du premier test supprimés du chat privé (ids 12–17) ; fichiers temporaires supprimés.

**Test réel, production, canal public** — 4 anime ajoutés par le panneau (3 accessibles + Black Torch dont la source renvoie 404) :
| Anime | Épisode du jour | Résultat |
|---|---|---|
| The Ogre's Bride | E12 | publié 11:47:03 — miniature 150, vidéo 151 (424 Mo) |
| Rich Girl Caretaker | E12 | publié 11:53:29 — miniature 152, vidéo 153 (391 Mo) |
| MAO | E25 | publié 12:00:25 — miniature 154, vidéo 155 (432 Mo) |
| Black Torch | E12 | source 404 : **rien posté**, réessais automatiques (5 en 20 min), signalement unique à 12:08 (« Vidéo inaccessible depuis 20 min ») visible dans le panneau web et poussé au bot d'administration |
- Détection : à l'ajout, chaque anime a été visité tout de suite (`mode=bootstrap+catchup`, `baseline=11/24/11/11`, `catchup=1` chacun) ; 4 jobs créés, aucun épisode ancien publié.
- Parallélisme réel : 3 téléchargements simultanés (autorisés=3) ; les publications sont passées **une par une, dans l'ordre de fin de téléchargement** (ids consécutifs 150-151, 152-153, 154-155 : aucun message intercalé). L'épisode en 404 n'a bloqué personne.
- Fichiers vérifiés avec ffprobe : h264 1920 px + aac, durées 1451 / 1491 / 1530 s, tailles identiques à la base.
- **Cycles réels de 30 min** (1800 s, sans mode test), alors que des envois étaient en cours : 10:44:33, 11:14:35, 11:44:38, 12:14:40 (UTC) — 4 cycles espacés de 30 min ; les deux derniers vérifient **5 anime**, `mode=incremental`, `known=12/25/12/12/48`, `new_episodes=0 errors=0` : **aucun épisode déjà détecté n'est redétecté** (même celui de Black Torch encore en réessai). Aucun nouvel épisode n'est sorti pendant l'observation : « aucun nouveau » est un résultat normal.
- Tests : 361 (v2_automation) ; nouveaux : `test_release_date`, `test_catchup_today`, `test_publication_rules` ; trois anciens tests adaptés sur le contrat modifié (alerte au premier échec → après 20 min ; libellé « Prochain cycle » ; bouton Cycles), aucun supprimé.

**Limites honnêtes** : la vidéo de The Ogre's Bride avait été téléchargée par mon premier test puis **réutilisée depuis le cache** (validée par ffprobe, taille identique) ; la fenêtre de 24 h de réessais et le nettoyage à J+14 restent prouvés par tests, pas par le temps ; le watcher détecte les épisodes des anime **surveillés**, il ne découvre pas d'anime inconnus ; l'arrivée effective du push Telegram du signalement n'a pas été vue à l'écran (aucune erreur d'envoi dans les logs).

**Conclusion : GLOBAL WATCHER — PROVEN** (détection globale, rattrapage du jour, parallélisme réel, ordre de publication, cycles de 30 min réels, aucun doublon, isolement des erreurs). Restent non prouvés par le temps réel : retry 24 h complet, nettoyage J+14.

# Site-wide detection, Black Torch E12 (cause), file glissante — run 3

## 1. La détection ne voyait pas les anime inconnus (corrigé)
Constat (13 h 19) : deux épisodes « il y a 1 minute » sur le site (Iron Wok Jan! E12, Digimon Beatbreak E48) n'apparaissaient nulle part. Le programme fonctionnait (cycles de 30 min sans erreur) mais ne visitait **que les 5 pages d'anime configurées** : il ne lisait jamais le flux « derniers épisodes » de l'accueil, pourtant validé dans `source_audit` (Strategy A) et prévu (`source.max_pages: 3`). Correctif (`site_feed.py`, `source.site_feed_enabled`) : à chaque cycle, lecture de l'accueil + pages 2-3, tri des épisodes **du jour**, anime inconnu → ajouté (marqué « ajouté depuis le site ») puis premier contrôle immédiat (rattrapage du jour), épisode déjà connu → rien (aucun doublon).
Preuve réelle : cycle de 13 h 45 → `site_feed pages=3 entries=96 today=2 new_anime=2 new_episodes=2` ; les deux épisodes ont été téléchargés et publiés (Iron Wok Jan! E12 : messages 160/161 ; Digimon Beatbreak E48 : 162/163). Cycle de 14 h 15 → `today=2 new_anime=0 new_episodes=0 already_known=2` : rien n'est redétecté.

## 2. Black Torch E12 — cause établie
Dans un vrai navigateur, le lecteur par défaut (myTV → voembed.net) affiche « Video playback error » et son fichier répond 404 (constaté 13 h 34, plus de 2 h après la mise en ligne). Le **lecteur Stape** servait le même épisode en MP4 direct (310 Mo, `206 video/mp4`). Notre programme ne lisait que myTV et ne tentait jamais les 3 autres lecteurs (MOON, VOE : coquilles JavaScript, non pris en charge ; Stape : adresse décodable).
Correctifs : `players.py` (repli sur les autres lecteurs, lecteur utilisé enregistré et affiché), chemin « MP4 direct » dans `downloader.py`, nouvelle situation « vidéo en préparation par la source » (`SOURCE_VIDEO_PROCESSING` : réessai toutes les 3 min, 24 h de fenêtre, un signalement à 20 min).
Preuve réelle : Black Torch E12 récupéré via **Stape** (324 867 418 octets en 670 s), validé (1920×1080, 1 424,9 s), publié 14:19:07 (miniature 164, vidéo 165).
Défaut trouvé en chemin : la validation refusait ce MP4 pour un simple avertissement ffprobe « Referenced QT chapter track not found » (rc=0, paquets vidéo sains) → `BENIGN_WARNINGS` dans `v1_poc/validator.py` (les vraies erreurs échouent toujours ; test ajouté). Le fichier déjà téléchargé a été réutilisé (aucun 3ᵉ téléchargement).

## 3. File glissante de 3 téléchargements
La place d'un anime restait occupée jusqu'à la fin de son **envoi** : le 4ᵉ attendait. `process_episode(…, on_downloaded=…)` libère maintenant la place dès la fin du téléchargement + validation ; pool de threads élargi ; l'arrêt propre attend aussi les envois. Tests : les 3 premiers démarrent ensemble, le 4ᵉ dès qu'un téléchargement est fini (envoi en cours), jamais plus de 3 téléchargements simultanés, ordre de détection respecté. Une seule publication à la fois dans le canal reste vraie.

## 4. Autres défauts corrigés
- Notifications « nouvel épisode » perdues quand plusieurs threads les envoyaient ensemble (« This event loop is already running ») → envoi sérialisé (test).
- « Relancer » (panneau, Telegram) ne relançait pas tout de suite (ancienne heure de nouvelle tentative conservée) → effacée (test).
- Le bot d'administration ne journalise plus comme une erreur un bouton pressé pendant qu'il était éteint.

## 5. Panneaux
Web : « Cycles de surveillance » avec ligne « Site : N du jour · N nouveaux anime · N déjà connus », badge « ajouté depuis le site », colonne « Aujourd'hui », lecteur utilisé dans le détail d'un épisode. Telegram : écran Cycles avec ligne « 🌐 site », liste des anime avec 🌐 pour ceux ajoutés automatiquement, message d'ajout explicite sur le rattrapage.

## 6. Limites honnêtes
- **Disque : 37 Go libres sur 476 (93 %)**. Tout publier (15-30 épisodes/jour, fichiers gardés 14 jours) dépasse 100 Go : les téléchargements se suspendront sous 5 Gio (alerte « Disque presque plein »). Décision à prendre : raccourcir la conservation (règle inchangée à ce jour) ou libérer de l'espace.
- MOON et VOE ne sont pas pris en charge (pages qui demandent un navigateur) ; seul Stape sert de repli.
- Le débit du lecteur Stape est faible (~0,5 Mo/s : 11 min pour 310 Mo).
- Rien n'a encore été observé sur plusieurs cycles avec de nombreux anime du jour à la fois (cette journée : 2 nouveaux anime seulement au premier cycle du flux).

# Décisions et suites de la session (récapitulatif)

## Conservation des fichiers : 14 jours (maintenue)
Un passage à 3 jours a été essayé puis **annulé à la demande du propriétaire** : `publication.cleanup_after_days` reste à **14**. Signification : le **fichier vidéo local** est supprimé de l'ordinateur 14 jours après sa publication, mais **la vidéo reste toujours disponible dans le canal Telegram** (les messages ne sont jamais supprimés). Les dates de nettoyage des 19 épisodes déjà publiés ont été remises à publication + 14 j. Point d'attention inchangé : `C:` a ~33 Go libres (94 % utilisé) ; à ~450 Mo par épisode, l'espace demande une surveillance (le programme suspend les téléchargements sous 5 Gio libres et alerte).

## Dépôt GitHub
Code poussé sur https://github.com/lyon32/AnimeTech.git (branche `main`, un commit) avec `.gitignore` (secrets, données, médias, journaux, PDF personnels) et `README.md`. Les `.env`, bases, vidéos, preuves (`output/`) et le prompt de travail ne sont pas versionnés ; l'identifiant Telegram réel a été remplacé par un exemple dans `.env.example`. La mention « Claude » comme co-auteur a été retirée du commit (l'affichage « Contributors » de GitHub est mis en cache et peut mettre quelques heures à se corriger).

## Chronologie de la session (tout ce qui a été fait)
1. **Panneau web refait** (8 pages en français, thème sombre, mobile) avec des données corrigées (santé sans fausse alerte, capacités en direct, compteurs sans baseline), refus des actions venant d'un autre site.
2. **Contrôle du worker depuis les panneaux** : arrêt propre (les jobs en cours se terminent), démarrage par tâche planifiée Windows (`install_tasks.ps1`).
3. **Watcher global** : un cycle de 30 min visite tous les anime actifs ; logs `[WATCHER]` ; résumé et historique de cycles ; « aucun nouvel épisode » n'est pas une erreur. Défauts trouvés et corrigés : lectures SQLite erronées en parallèle (104 anomalies mesurées → 0), dossier de segments partagé entre téléchargements.
4. **Règles de publication** : uniquement le jour même (jamais hier), aucun doublon, pas de miniature sans vidéo accessible, réessais silencieux et un signalement après 20 min, publication un épisode à la fois dans l'ordre d'arrivée (porte équitable), un problème sur un anime ne bloque pas les autres, cycle jamais interrompu.
5. **Test réel dans le canal** : 3 épisodes publiés dans l'ordre sans intercalage, un 404 sans rien poster puis signalement à 20 min, 4 cycles réels à 30 min sans redétection.
6. **Détection de tout le site** (flux « derniers épisodes ») : anime inconnus ajoutés et publiés (Iron Wok Jan!, Digimon Beatbreak), aucun doublon aux cycles suivants.
7. **Black Torch E12** : cause = seul le lecteur myTV était lu et son fichier répondait 404 ; repli sur Stape (MP4 direct) ; avertissement ffprobe bénin ignoré ; publié à 14:19.
8. **File glissante de 3 téléchargements** (la place est libérée dès la fin du téléchargement, pas de l'envoi).
9. **Correctifs annexes** : notifications perdues quand plusieurs partaient ensemble, « Relancer » immédiat, bouton pressé pendant que le bot était éteint, filtre de masquage des jetons.
10. **Tests** : v2_automation 384, v1_poc 21 (source_audit non relancé, non modifié).

## Reste à faire / non prouvé dans la durée
- Fenêtre de 24 h de réessais et nettoyage réel à J+14 : prouvés par tests, pas encore par le temps réel.
- Beaucoup d'anime du jour en même temps (quinzaine) : jamais observé de bout en bout ; MOON et VOE non pris en charge comme lecteurs de repli.
- Espace disque : à surveiller (~450 Mo par épisode).
