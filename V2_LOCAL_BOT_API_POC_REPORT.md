# V2_LOCAL_BOT_API_POC_REPORT — Bot API standard vs Local Bot API

Date : 2026-09-21. Chaque ligne porte un statut ; **rien n'est « TESTED » sans une exécution réelle décrite ici**.

## 1. Ce qui a été exécuté AUJOURD'HUI (session courante)

| Vérification | Commande / preuve | Résultat | Statut |
|---|---|---|---|
| Serveur Local Bot API joignable, version | `python -m v2_automation.cli doctor` | `local_bot_api_getme_ok: true`, `http_server_version: "Bot API 10.3"`, disque libre 46 050 820 096 o | **MEASURED** |
| `LocalBotAPITransport` (nouvelle classe) parle réellement au serveur local | `getMe` via `transport_from_config` avec le bot canal puis le bot admin (sonde en lecture seule) | OK, 0,255 s puis 0,135 s ; `base_url = http://127.0.0.1:8081/bot` (jeton absent du log) | **TESTED** (getMe seulement) |
| Vérification d'appartenance sur le **vrai** canal requis | `getChatMember(@spy_family_2025, <id opérateur>)` par le bot canal via le serveur local | `administrator`, 0,481 s | **TESTED** (chemin membership réel) |
| API standard `api.telegram.org` | `BotAPITransport.get_me()` depuis ce poste | **échec DNS** (`getaddrinfo failed`, kind `NETWORK`, 0,25 s) — ce poste ne résout pas api.telegram.org ; seul le serveur local (docker) sort vers Telegram | **BLOCKED** (réseau de l'environnement) |
| Refus > 50 Mo par l'API standard, pas de plafond en local | `test_standard_api_refuses_a_file_over_50mb_the_local_api_does_not` (fichier creux de 51 Mio, aucun réseau) | `FILE_TOO_LARGE` levé **avant** tout envoi ; `LocalBotAPITransport.max_upload_bytes is None` | **TESTED** (comportement du transport, pas de l'API distante) |
| Le reste du moteur ignore la différence | `test_publisher_is_transport_agnostic` : `TelegramPublisher.send_video(chat, media)` identique | OK | **TESTED** |
| Envoi réel d'un gros fichier par le NOUVEAU `LocalBotAPITransport` | — | non exécuté : cela enverrait un fichier de plusieurs centaines de Mio dans un vrai chat (mesures antérieures en `file://` : 1500 MiB ≈ 7 min). À faire avec un chat de test désigné par vous | **NOT_EVALUATED** |

## 2. Mesures antérieures présentes dans le repository (non refaites aujourd'hui)

Source : `v2_automation/output/docs/V4_FINAL_CAPACITY_REPORT.md`, `V5_SEGMENTED_UPLOAD_REPORT.md`, `output/evidence/campaign_phase6/`.
Méthode : `sendDocument` par chemin `file://` (`docker cp` dans le conteneur), fichiers synthétiques déterministes, SHA-256 côté serveur.

| Taille | Résultat | Durée d'envoi | Remarque |
|---|---|---|---|
| 50 MiB | PROVEN | 15,9 s | |
| 700 / 800 MiB (multipart, Phase 6) | PROVEN | ~15 min / 700 Mo | le **multipart** plante le serveur dès ~900 MiB |
| 1500 MiB (×2) | PROVEN | 419,9 s / 419,7 s | 2 exécutions indépendantes |
| 1600 / 1700 / 1800 MiB | PROVEN | 448,0 / 482,0 / 509,1 s | 1800 MiB = plus grande taille prouvée (config : `max_safe_publish_mib: 1800`) |
| 1900 / 1950 / 1990 / 2000 MiB | **CRASH** | 500 s (coupure) | aucune saturation mémoire/disque ; le worker du serveur meurt silencieusement à ~500 s |
| 2000 MiB en segments (1000+1000, 700+700+600, 600×3+200) | PROVEN, SHA-256 identique à la reconstitution | — | V5 ; le fichier unique de 2 GiB reste **non supporté** par le build `e3e9dd8` |

Correctif d'exploitation déjà en place (config) : image serveur patchée `IDLE_TIMEOUT=7200`, `telegram.upload_timeout_seconds: 7200`,
reprise sans renvoi après connexion coupée (`wait_for_message`).

Statut global « fichiers volumineux » : **MEASURED** (campagnes antérieures du repository) — **pas** re-mesuré par la V2 utilisateur.

## 3. Documentation demandée (§64) : taille · durée · upload · résultat · temps · erreurs · reprise

- taille / durée / résultat : tableau §2 ; erreurs : `FILE_TOO_LARGE` (API standard > 50 Mo, levée localement), déconnexion `Server disconnected` à 500 s (≥ 1900 MiB) ;
- reprise : `connection_lost()` → la copie `file://` est **conservée** sur le serveur, `wait_for_message` retrouve le message publié sans renvoyer ;
- privé vs canal : pour un utilisateur, un média déjà dans un canal est **copié** (`copyMessage`, aucun octet retransféré) ; sinon envoi du fichier local puis
  réutilisation du `file_id` pour les utilisateurs suivants (`test_cleaned_local_file_is_served_from_the_stored_file_id`).

## 4. À faire pour clore ce rapport
1. Fournir `USER_BOT_TOKEN` (`@OtakuuVerse_bot`) + un chat de test → envoi réel privé (petit fichier, puis 1 fichier > 50 Mo) → passer les lignes « NOT_EVALUATED » à TESTED/MEASURED.
2. Statut de l'API standard : à tester depuis une machine qui résout `api.telegram.org`.
