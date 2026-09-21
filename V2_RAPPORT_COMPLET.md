# V2 — RAPPORT COMPLET (2026-09-21)

Ce document réunit tout ce qui a été fait, testé, mesuré, corrigé et ce qui reste ouvert. Il complète (sans les remplacer) :
`V2_IMPLEMENTATION_AUDIT.md` (état du dépôt avant travaux), `V2_IMPLEMENTATION_REPORT.md`, `V2_TEST_PLAN.md`, `V2_TEST_REPORT.md`,
`V2_LOCAL_BOT_API_POC_REPORT.md`. En cas d'écart, **ce rapport est le plus récent**.

Statuts : **TESTED** (exécuté avec preuve) · **MEASURED** (mesure réelle) · **NOT_EVALUATED** · **INCONCLUSIVE** · **BLOCKED**.

---

## 1. Résumé en 10 lignes

1. Le package `v2_automation` (le « V1 watcher » du prompt) a été étendu, sans second moteur de téléchargement, avec le côté « demandes utilisateurs » : identité média partagée, demandes, livraison privée, bot Telegram, multi-canal, audit, authentification optionnelle du panel.
2. Un média = une ligne `episodes` avec une clé `media_key` unique ; le watcher et tous les utilisateurs convergent sur le même job (un téléchargement, N livraisons).
3. Migration de la base de production **v3 → v4 appliquée** le 2026-09-21 (sauvegarde faite avant) : 1 487 épisodes, tous avec une clé unique.
4. Le bot utilisateur tourne **en production**, avec le bot `BestAnime32_bot` (déjà administrateur de `@spy_family_2025`).
5. Un bug que j'avais introduit (annuler une demande fabriquait des « échecs ») et une série d'incohérences du bot (un nombre tapé lançait une recherche, classement de la source non pertinent…) ont été **diagnostiqués et corrigés**.
6. Tests : **932** tests dans `v2_automation` (390 existants + 542 nouveaux) ; dernière exécution complète : **931 passés, 1 échec instable, corrigé depuis** (voir §9) ; `v1_poc` : 22/22.
7. Vérifié en réel : livraison privée Telegram (upload puis `file_id`), appartenance au vrai canal, recherche sur la vraie source, migration sur une copie de la production.
8. **Non vérifié** : la conversation du bot avec un vrai utilisateur après les dernières corrections (à toi de la refaire), la page `v2.html` dans un navigateur, l'API Bot standard (DNS), les gros fichiers avec le nouveau transport.
9. Aucun commit n'a été fait (53 fichiers modifiés ou ajoutés dans le dépôt, tout en local).
10. La conclusion « V2 terminée » n'est **pas** prononcée : voir §11.

---

## 2. Architecture obtenue

```
 V1 discovery (watcher, 30 min) ─┐                                    ┌─ canal principal (miniature + vidéo)
                                 ├─► media.ensure_media ─► episodes ─► DownloadManager ─┤  + copies vers les canaux supplémentaires
 V2 demandes utilisateurs ───────┘   (media_key UNIQUE)   queue_items  (moteur unique)  └─ livraison privée : file_id | copyMessage | upload
```

- **Identité** : `media_key = sha256(anime_key | saison | épisode | version)`. Sur la source chaque (titre, saison, langue) est une page distincte (`anime_key = postid:N`), donc VF ≠ VOSTFR par construction.
- **États média** (11 demandés) : vue nommée sur `episodes.status` (`validated`/`ready` → READY ; `failed` + `NOT_AVAILABLE_YET` → EXPIRED) + un vrai état `ready` pour les médias privés validés.
- **États de demande** (11) : PENDING, SEARCHING, FOUND, QUEUED, PROCESSING, WAITING_FOR_MEDIA, DELIVERING, COMPLETED, CANCELLED, EXPIRED, FAILED. Une saison = **une** demande parent + un item enfant par épisode.
- **Limite** : 1 demande active par utilisateur, garantie **par la base** (index unique partiel) et par le code.
- **Publication canal vs privé** : anime surveillé → publié dans le canal comme avant ; anime hors liste → **livraison privée uniquement** (`publish_channel = 0`), jamais ajouté à la liste surveillée. Un épisode « baseline » demandé par un utilisateur reste privé (il n'apparaît pas dans le canal).
- **Un seul worker** (bail existant) : `UserSide.tick()` est appelé dans la boucle du worker ; le bot fait son long-poll dans un thread du même processus.

## 3. Ce qui a été ajouté ou modifié

**Nouveaux modules** (`v2_automation/src/v2_automation/`) : `media.py` (identité, états, `ensure_media`, `discard_unstarted`), `catalog.py` (liste d'épisodes de la source), `requests_mgr.py` (demandes), `parser.py` (texte libre), `search.py` (recherche, pertinence), `membership.py` (accès canal), `delivery.py` (livraison privée), `telegram_publisher.py` (`TelegramPublisher` + `BotAPITransport` / `LocalBotAPITransport`), `channels.py` (multi-canal), `user_bot.py` (routeur + couche Telegram), `user_side.py`, `audit.py`, `web_auth.py`, `web_ui/v2.html`.

**Petits patchs ciblés** : `schema.py` (migration v4), `models.py`, `repo.py`, `db.py` (backfill + tolérance à la course de migration), `states.py` (`READY`), `downloader.py` (média privé → READY), `recovery.py`, `cleanup.py`, `worker.py`, `service.py`, `admin_telegram.py`, `web.py`, `cli.py`, `app_config.py`, `errors.py`, `config/config.yaml` (sections ajoutées en fin de fichier), `.env.example`, `web_ui/index.html` (lien « Demandes (V2) »).

**Scripts** : `scripts/run_user_bot_test.py`, `scripts/run_panel_test.py` (bot et panel sur une base de **test**, plus utilisés).

**Base de données v4** : colonnes `episodes.media_key/origin/publish_channel`, `animes.season` ; tables `users`, `requests`, `request_items`, `deliveries`, `conversations`, `audit_log` ; index uniques `ux_episodes_media_key`, `ux_requests_one_active`, `ux_deliveries_admin_open`.

## 4. Fonctionnalités demandées → statut

| Exigence du prompt | STATUS | Preuve |
|---|---|---|
| Audit du dépôt avant toute modif (§1) | **TESTED** (livrable) | `V2_IMPLEMENTATION_AUDIT.md` |
| Identité média déterministe, VF ≠ VOSTFR (§3-4) | **TESTED** | `test_core_media.py` |
| États média et demande explicites (§5-6) | **TESTED** | idem + `test_private_media.py` |
| Saison = 1 demande parent + jobs enfants (§7, §19) | **TESTED** | 1 requête / 3 jobs ; 48 jobs pour une saison réelle |
| 1 demande active par utilisateur (§8) | **TESTED** | refus au niveau base (`IntegrityError`) + message |
| Bot privé, résultat envoyé en privé (§9) | **TESTED** (transport réel pour la livraison ; conversation simulée) | voir §7 |
| Vérification d'accès canal, multi-canaux, à chaque interaction (§10-11) | **TESTED** + appel réel | `getChatMember(@spy_family_2025)` = `administrator` |
| Parser des requêtes (§12) | **TESTED** | 6 formulations → même requête |
| Recherche watched + hors liste sans ajout (§13) | **TESTED** + **MEASURED** live | 20 requêtes sur la vraie source |
| Saisons réellement disponibles, jamais inventées (§14) | **TESTED** | « saison 7 » → refusée avec la liste des saisons |
| Sélecteur d'épisode paginé, dernier épisode, version (§15-17) | **TESTED** | pagination 20/page, « dernier » = plus haut listé |
| Attente 20 min puis EXPIRED, horloge injectable (§18) | **TESTED** | 19 min attend, 21 min expiré, durée configurable |
| Média déjà prêt / publié → réutilisation, pas de re-téléchargement (§20-21) | **TESTED** | `file_id`, `copyMessage` |
| Crash recovery, aucun doublon (§22-23, §52) | **TESTED** | 8 scénarios (voir §8) |
| Livraison privée persistée, N utilisateurs = 1 download (§24-25, §51) | **TESTED** | vrai téléchargement ffmpeg : `seg_0.ts` lu 1 fois, 1 upload, 3 livraisons |
| Annulation sans casser les autres (§26) | **TESTED** | + correction du bug « échecs » (§6) |
| `/history` depuis la DB (§27) | **TESTED** | |
| Watcher V1 conservé et partagé (§28-29, §57) | **TESTED** | 390 tests existants verts ; watcher+user → 1 seul job (2 ordres + 8 threads) |
| Parallélisme inter-anime, file par anime (§30-32, §58) | **TESTED** (worker réel, traitement simulé) | 3 anime en parallèle, un échec ne bloque pas |
| Limitation dynamique (§31) | existant, **non modifié** | `scheduler.py` (disque, CPU, RAM, débit) |
| Aucune priorité utilisateur (§33) | **TESTED** par absence | aucune notion de VIP |
| `TelegramPublisher` transport-agnostique (§34-35) | **TESTED** | même `send_video`, refus > 50 Mo en standard, pas de plafond en local |
| Multi-canal indépendant du privé (§36-37, §59) | **TESTED** (transport simulé) | copie miniature+vidéo, panne d'un canal isolée |
| Stockage / cleanup J+14, fichier seul, rejouable (§38-40, §56) | **TESTED** | y compris média privé servi ensuite par `file_id` |
| Admin Telegram (§41) | **TESTED** (bot simulé) | ajout de `/requests /history /stats` + annulation confirmée |
| Panel web : auth, actions réelles, confirmations (§42-44, §63) | **TESTED** (API, base) | 24 tests ; rendu navigateur **NOT_EVALUATED** |
| Audit log de chaque action admin (§45) | **TESTED** | web + Telegram ; succès, échec, non confirmé |
| Configuration par env/config, rien en dur (§46) | **TESTED** | canaux, canaux requis, délais |
| Observabilité par identifiants (§47) | **TESTED** (partiel) | logs `[MEDIA] [REQUEST] [DELIVERY] [CHANNELS] [AUDIT]` avec `request/media/job/delivery` |
| Erreurs classifiées (§48) | **TESTED** | `SOURCE_NOT_FOUND`, `EPISODE_NOT_AVAILABLE`, `TIMEOUT`, `TELEGRAM_ERROR`, `STORAGE_ERROR`… |
| Retry contrôlé (§49) | **TESTED** | backoff, plafond, pas de boucle infinie |
| Local Bot API (§64) | **TESTED** (getMe, membership réels) ; gros fichiers **MEASURED** (campagnes antérieures) | `V2_LOCAL_BOT_API_POC_REPORT.md` |
| API Bot **standard** | **BLOCKED** | ce poste ne résout pas `api.telegram.org` |
| E2E Telegram réel de bout en bout | livraison **TESTED** ; chaîne complète **NOT_EVALUATED** | les deux moitiés sont testées séparément |

## 5. Décisions de conception (à connaître)

1. **Média privé** : jamais publié dans le canal ; le fichier validé attend en `READY`, la 1ʳᵉ livraison le passe `published` et déclenche les 14 jours.
2. **Réutilisation Telegram** : `file_id` d'une livraison précédente → sinon `copyMessage` depuis le canal → sinon fichier local. Un `file_id` est propre à un bot ; pour `copyMessage` le bot utilisateur doit être membre/admin du canal (c'est le cas de `BestAnime32_bot`).
3. **Livraison incertaine** (crash/timeout pendant l'envoi privé) : impossible de relire un chat privé → on choisit **zéro doublon** : statut `uncertain`, jamais renvoyé automatiquement, demande en échec avec un message clair ; l'utilisateur redemande et est servi **sans nouveau téléchargement** ; l'admin peut relancer (confirmation).
4. **Annulation** : un média privé jamais démarré est **supprimé** (pas marqué en échec) ; média du watcher / publié / en cours : conservé. Un téléchargement déjà commencé se termine.
5. **Épisode inexistant** : au-delà du dernier épisode listé + 3, le bot prévient (« le dernier disponible est le 127 ») et laisse choisir entre le dernier ou attendre ; l'attente de 20 min reste pour « juste le suivant ».
6. **Authentification du panel** : optionnelle (sur demande) ; sans mot de passe le panneau démarre en local (127.0.0.1) sans authentification ; dès que `ADMIN_WEB_PASSWORD(_HASH)` est défini, session signée + anti-force brute.
7. **Bot utilisateur** : `user_bot.use_channel_bot: true` → utilise le bot du canal (`BestAnime32_bot`) tant qu'aucun `USER_BOT_TOKEN` n'est défini (`@OtakuuVerse_bot` n'est pas configuré).

## 6. Incidents rencontrés et corrigés (chronologique)

| # | Problème | Cause | Correction |
|---|---|---|---|
| 1 | Test préexistant en échec (`test_config`) | placeholder `<COLLER_LE_TOKEN…>` jugé « vraie valeur » | le test reconnaît les placeholders `<…>` (non désactivé) |
| 2 | Log du bot contenait le **jeton** (URL httpx) | niveau de log httpx | loggers `httpx/httpcore/telegram` en WARNING, log supprimé et relancé (0 occurrence) ; jetons masqués aussi dans les erreurs du transport |
| 3 | Panel de test « cassé » : 48 échecs, « à traiter » | annuler une demande passait ses médias en `failed` | `media.discard_unstarted` supprime les médias privés jamais démarrés ; test de régression sur une saison de 48 épisodes |
| 4 | Confusion panel 8086 / production | le panel de test lisait une base de test vide | titre « BASE DE TEST », puis arrêt des deux processus de test ; production vérifiée intacte |
| 5 | Migration v4 : le worker planté au démarrage simultané | course entre processus (`executescript` commit avant) | `db.migrate` tolère « duplicate column / already exists » si un autre processus a appliqué la version |
| 6 | Bot : un nombre tapé (`2`, `3`, `4`) lançait une **recherche** | seul `await_number` acceptait un chiffre | `classify_text` : nombre / `ep N` / `dernier` = réponse à l'étape en cours ; `?`, emoji, 1 lettre = aide, **zéro appel source** |
| 7 | Bot : mauvais anime proposé (films d'abord, série principale absente) | ordre du site utilisé tel quel, 8 résultats sans type | lecture du **Type** (TV/MOVIE/SPECIAL/OVA/ONA) et du nombre d'épisodes, jusqu'à 5 pages lues (2 en parallèle), classement par pertinence, films et spéciaux à part, pagination |
| 8 | Bot : la liste surveillée masquait la source | `search()` renvoyait uniquement le local | fusion watched ⭐ + source |
| 9 | Bot : « one piece 1150 » attendait 20 min un épisode inexistant | pas de contrôle de plausibilité | avertissement + choix (§5.5) |
| 10 | Bot : titre répété, « conversation expirée » muette | messages | format unique, boutons périmés → « Recommencer » |
| 11 | Bot : « Spy x Family » / « My Hero Academia » non trouvés | mots optionnels (`x`, `my`, `no`…) exigés | liste de mots optionnels |
| 12 | Bot : « Mob Psycho 100 » lu comme épisode 100 | nombre final toujours pris pour un épisode | si une page porte exactement le titre complet, le nombre reste dans le titre |
| 13 | Panel V2 « vide » | vrai : aucune demande créée (voir 6) ; le panel n'affichait ni conversations ni utilisateurs | onglet **Activité du bot** (conversations, étape, dernière recherche) |
| 14 | JS de `v2.html` invalide (apostrophe) | détecté par `node --check` | corrigé + test qui valide le JavaScript |
| 15 | `test_site_feed.py` : plantage intermittent (violation d'accès) | la fixture fermait la connexion pendant qu'un thread de découverte écrivait encore | la fixture attend l'arrêt du scheduler avant `close()` ; 15 exécutions sur 15 propres |
| 16 | Erreur de manipulation : un `git stash` lancé par erreur | — | `git stash pop` immédiat ; tous les fichiers (dont vos 4 modifications en cours) sont revenus intacts |

## 7. Vérifications réelles (hors tests unitaires)

| Vérification | Résultat | Statut |
|---|---|---|
| Serveur Local Bot API (`doctor`) | joignable, « Bot API 10.3 », 46 Go libres | MEASURED |
| `LocalBotAPITransport.getMe` (bot canal et bot admin) | OK, 0,26 s / 0,14 s | TESTED |
| `getChatMember(@spy_family_2025, id opérateur)` | `administrator`, 0,48 s | TESTED |
| API standard `api.telegram.org` | échec DNS | BLOCKED |
| Recherche live (source réelle) | « bleach » : 10 résultats, série de 48 épisodes listés en 1,74 s ; 20 requêtes classées (One Piece 1179 ép., Naruto 220, Bleach 366, Detective Conan 1213…) | MEASURED |
| **Livraison privée réelle** (mire ffmpeg de 2 s → ton chat via `BestAnime32_bot`) | 1ʳᵉ : upload 2,71 s (message 5, `file_id` conservé) ; 2ᵉ : `file_id` 0,30 s, aucun octet renvoyé ; demande → COMPLETED | TESTED |
| Migration v3→v4 sur une **copie** de la production | 1 487 épisodes, 1 487 clés uniques ; 6 pages du panneau identiques à l'ancien code (26 anime aujourd'hui ; 19/31 publiés au moment du contrôle) | TESTED |
| Bot en production : premiers essais utilisateur | 2 utilisateurs, appartenance validée ; recherche, choix, demande, attente, annulation observés dans tes captures | observé |

## 8. Tests (chiffres)

- `v2_automation/tests/unit` : **932 tests** collectés. Avant : 390 (+1 échec préexistant). Nouveaux : `test_core_media` 27, `test_private_media` 4, `test_delivery` 17, `test_user_bot` 35, `test_channels_config` 8, `test_worker_user_side` 6, `test_core_pipeline_real` 2, `test_v2_admin` 24, **`test_bot_scenarios` 418**.
- `v1_poc` : 22 passés (code non modifié).
- **Dernière exécution complète** (seule, sans autre pytest) : 931 passés, 1 échec (`test_site_feed`, intermittent) ; après correction (incident 15) ce fichier passe 15/15. **Je n'ai pas relancé la suite complète après cette dernière correction.**
- Le **harnais de scénarios** (`bot_harness.py`) applique à chaque message 6 invariants : réponse obligatoire, `callback_data` ≤ 64 octets et de forme connue, aucun titre répété, résultats contenant les mots de la requête, aucune recherche pour un nombre ou du bruit, jamais deux demandes actives / conversation valide. Il utilise les **vraies pages** de la source capturées.
- Matrice : 32 saisies × 7 étapes de la conversation, 19 boutons × 7 étapes, watched / hors liste × épisode / dernier / saison, utilisateurs simultanés, sortie du canal en cours de parcours, boutons périmés, double appui, commandes au milieu d'un parcours.
- **Matrice de crash (§52)** : après téléchargement, avant publication, après publication (ACK enregistré → épisode terminé sans renvoi, plus de « FAILED manuel »), publication sans enregistrement (reste manuel), avant livraison privée (1 envoi), pendant/après livraison privée (`uncertain` / jamais renvoyé), pendant une copie multi-canal.
- Limites de ces tests : Telegram et le site source sont simulés aux frontières dans les tests automatiques ; seuls les contrôles du §7 sont réels.

## 8 bis. Phase « recherche fidèle au site + identité média + crash » (2026-09-21)

Détail complet et preuves : `V2_BOT_SEARCH_VERIFICATION.md`.
- **Recherche** : `search.py` réécrit sur le vrai protocole du site (2 moteurs Ajax Search Pro, VF asid 2 / VOSTFR asid 3, lus sur la page d'accueil) ; série → saison → version → épisode, chaque étape affichée ; noms propres (« Wakfu », « Carmen Sandiego ») ; Bestiale, Wakfu, Avatar, Attack on Titan trouvés. Vérifié moi-même : **73 recherches réelles, 18 parcours complets, 6 comparaisons avec les boîtes du site** ; corrigé au passage le classement (série principale d'abord) et le récapitulatif (saison visible).
- **Identité** : `media_key = source | anime_id | saison | épisode | version` (`media_ref` lisible), propagée partout (logs `media=`, nom de fichier, publications, historique) ; **claim atomique** (UPDATE compare-and-set) + verrou de fichier ; testé en threads et en processus séparés.
- **Watcher ≠ recherche** : 7 tests (aucune requête de recherche, baseline/incrémental, convergence dans les deux ordres et en simultané).
- **Crash réel** : 4 tests avec vrais processus tués + faux serveur Bot API HTTP : kill à 60 % → reprise → publié une fois ; kill après acceptation de la vidéo → jamais renvoyée (décision manuelle) ; redémarrage après publication → aucun renvoi.
- **Mesure** : le moteur actuel retélécharge **tous** les segments après un crash (correct mais lent). Réutilisation de segments **non implémentée** — à décider.

## 9. État de la production maintenant

- Base `data/v2.sqlite3` : **schéma v5** (sauvegardes `data/v2.sqlite3.bak_before_v4_20260921_1347` et `data/v2.sqlite3.bak_before_v5_20260921_1713`). Migration v5 d'abord essayée sur une **copie** (1645 épisodes : 0 `media_ref` vide, 0 doublon de clé, intégrité OK), puis appliquée en production après arrêt propre du worker ; contrôle : versions [1..5], 0 claim résiduel, panneau 200, `/api/bot-activity` OK, worker/panneau/admin relancés sur le nouveau code.
- Processus actifs : `run-worker` (avec bot utilisateur), `serve` (panneau http://127.0.0.1:8085), `run-admin` — tous sur le nouveau code.
- Dernier contrôle : `worker_alive = true`, 26 anime surveillés, 34 épisodes publiés, 0 à traiter, 2 utilisateurs vus par le bot.
- Panneau **sans mot de passe** (à ta demande) ; page V2 : http://127.0.0.1:8085/ui/v2.html (onglet par défaut « Activité du bot »).
- Bots de test (base `v2_userbot_test.sqlite3`, port 8086) : **arrêtés**.
- Le worker a été redémarré proprement (arrêt demandé, 3 publications de ~490 Mo terminées d'abord, pas de coupure).

## 10. Limites connues et points d'attention

1. Un média déjà publié dans le canal puis nettoyé ne peut plus être re-téléchargé (`DownloadManager` refuse un épisode ayant un `video_message_id`) : servi par `copyMessage` ; sinon erreur `STORAGE_ERROR` explicite pour l'admin.
2. Pas d'annulation d'un téléchargement **en cours** (il se termine).
3. Un canal configuré à la fois en id numérique (`TELEGRAM_CHANNEL_ID`) et en `@nom` (`TELEGRAM_CHANNELS`) serait vu comme deux canaux : le nommer pareil aux deux endroits.
4. « Republish » recrée volontairement un doublon dans le canal (confirmation obligatoire, journalisé).
5. (Corrigé) La recherche interroge maintenant les deux moteurs du site (VF + VOSTFR) et connaît les titres alternatifs. Reste : le site n'a aucune tolérance aux fautes (« one pice » ne donne rien) — le bot le dit, il ne devine pas.
6. Latence d'une recherche : ~1,5 s (une page) à ~8 s (One Piece : 5 pages lues) ; aucun indicateur « recherche en cours » n'est encore affiché.
7. Le panneau est ouvert sans mot de passe : acceptable en `127.0.0.1` uniquement ; définir `ADMIN_WEB_PASSWORD` avant toute exposition réseau.
8. Le bot ouvert à tout membre de `@spy_family_2025` : les demandes hors liste ne sont **pas** limitées en nombre total (une active par utilisateur seulement) ni en quota de téléchargement.
9. L'échec instable de `test_site_feed` montre qu'il reste des tests qui laissent des threads écrire après la fermeture de leur connexion ; seul ce fichier a été corrigé.

## 11. Ce qui reste avant de déclarer « V2 terminée » (critère §70)

- [ ] Refaire les parcours dans Telegram avec les corrections (ton retour) : « one piece 1150 », « Bleach » puis « 3 », « Black torch » puis « 2 », « ? », un anime hors liste jusqu'à la **réception de la vidéo** (le premier vrai E2E utilisateur : demande → téléchargement → livraison).
- [ ] Vérifier `v2.html` dans un navigateur (le JavaScript est validé par `node --check`, le rendu non).
- [x] Suite complète relancée seule : 1018 passés, 1 ignoré (puis bot/admin relancés après 2 petites corrections : 529 passés).
- [ ] API Bot standard : à tester depuis une machine qui résout `api.telegram.org`.
- [ ] Gros fichier envoyé par le **nouveau** `LocalBotAPITransport` (les 1800 MiB prouvés datent des campagnes V4/V5).
- [ ] Charge : plusieurs dizaines d'utilisateurs simultanés.
- [ ] Commit des changements (rien n'est commité).
- [ ] Optionnel : `USER_BOT_TOKEN` pour `@OtakuuVerse_bot` (aujourd'hui `BestAnime32_bot`).

## 12. Commandes utiles

```bash
# tests (depuis v2_automation/, un seul pytest à la fois)
python -m pytest tests/unit -q -p no:cacheprovider
# panneau : http://127.0.0.1:8085   page V2 : /ui/v2.html
# arrêt propre du worker : bouton du panneau, puis   schtasks /Run /TN V2AutomationWorker
# sauvegarde avant toute manipulation de base : copie de data/v2.sqlite3*
```

## 13. Fichiers de l'ensemble du travail

- **Rapports** (racine du dépôt) : `V2_RAPPORT_COMPLET.md` (celui-ci), `V2_IMPLEMENTATION_AUDIT.md`, `V2_IMPLEMENTATION_REPORT.md`, `V2_TEST_PLAN.md`, `V2_TEST_REPORT.md`, `V2_LOCAL_BOT_API_POC_REPORT.md`.
- **Code** : voir §3. **Tests** : `tests/unit/` (`v2support.py` et `bot_harness.py` sont des aides, pas des tests).
- **Mémoire de projet** : note sur l'état du déploiement dans le dossier mémoire de Claude.
