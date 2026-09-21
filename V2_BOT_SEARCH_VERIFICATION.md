# V2 — Vérification de la recherche du bot (réalisée moi-même, en direct sur voir-anime.to)

Date : 2026-09-21. Tout ce qui suit a été exécuté **contre le vrai site** avec le **vrai routeur du bot** (mêmes classes que la production ; seul Telegram est remplacé par une boîte d'envoi en mémoire).

## 1. Comment le site cherche (compris avant de coder)
- Deux moteurs *Ajax Search Pro* distincts : boîte « Rechercher en VOSTFR… » (asid 3) et boîte « Rechercher en VF… » (asid 2), interrogés par `POST /wp-admin/admin-ajax.php` (`action=ajaxsearchpro_search`, `aspp`, `asid`, `asp_inst_id`, `options`).
- Catalogues différents : Bestiale, Wakfu, Carmen Sandiego n'existent qu'en VF ; « avatar » donne un titre VF et un titre VOSTFR différents.
- La version = le moteur qui répond (« Wakfu S2 » ne porte pas « VF »). Une saison = une page. Titres alternatifs indexés. Réponse plafonnée (~17), sans pagination.
- Frappe réelle dans la boîte VF du navigateur intégré : le site émet bien ce `POST` (200). Le volet du navigateur étant masqué, l'affichage déroulant n'a pas pu être lu ; la comparaison ci-dessous rejoue donc la requête du site depuis la page elle-même (même origine, mêmes cookies).

## 2. Comparaison côte à côte : boîtes du site (navigateur) ↔ bot
| Requête | Boîte VF du site | Boîte VOSTFR du site | Ce que propose le bot | Verdict |
|---|---|---|---|---|
| bestiale | Bestiale (VF) | — | Bestiale 🇫🇷 | identique |
| wakfu | Wakfu S1, S2, S3, S4, Specials | — | Wakfu 🇫🇷 · 4 saisons + Films & spéciaux (1) | identique |
| avatar | Avatar, Le Dernier Maître De L’air | Quanzhi Gaoshou (+2, 3, Dianfeng Rongyao, Specials) | Avatar… 🇫🇷 ; Quanzhi Gaoshou… 🇯🇵 (autre titre) ; Films & spéciaux (1) | identique |
| carmen sandiego | S1, S2, S3 (VF) | — | Carmen Sandiego 🇫🇷 · 3 saisons | identique |
| attack on titan | Shingeki no Kyojin (VF) + 6 suites | Shingeki no Kyojin + 6 suites + OVA | Shingeki no Kyojin 🇫🇷🇯🇵 · autre titre (+ 2, 3…) | identique |
| kimetsu no yaiba | Kimetsu no Yaiba, 2, 3, 4, Mugen Ressha-hen, film | idem | Kimetsu no Yaiba 🇫🇷🇯🇵, 2, 3, 4, Mugen Ressha-hen | identique |

## 3. 73 recherches réelles (première réponse du bot)
| Requête | Premières propositions du bot |
|---|---|
| `bestiale` | Bestiale 🇫🇷 |
| `wakfu` | Wakfu 🇫🇷 · 4 saisons ; Films & spéciaux (1) |
| `carmen sandiego` | Carmen Sandiego 🇫🇷 · 3 saisons |
| `avatar` | Avatar, Le Dernier Maître De L’air 🇫🇷 ; Quanzhi Gaoshou 🇯🇵 · autre titre ; Quanzhi Gaoshou 3 🇯🇵 · autre titre |
| `Avatar, Le Dernier Maître De L’air` | Avatar, Le Dernier Maître De L’air 🇫🇷 |
| `attack on titan` | Shingeki no Kyojin 🇫🇷🇯🇵 · autre titre ; Shingeki no Kyojin 3 🇫🇷🇯🇵 · autre titre ; Shingeki no Kyojin 2 🇫🇷🇯🇵 · autre titre |
| `shingeki no kyojin` | Shingeki! Kyojin Chuugakkou 🇯🇵 ; Shingeki no Kyojin 🇫🇷🇯🇵 ; Shingeki no Kyojin: Kakusei no Houkou 🇫🇷🇯🇵 |
| `kimetsu no yaiba` | Kimetsu no Yaiba 🇫🇷🇯🇵 ; Kimetsu no Yaiba 2 🇫🇷🇯🇵 ; Kimetsu no Yaiba 3 🇫🇷🇯🇵 |
| `demon slayer` | Demon Slayer Kimetsu no Yaiba – Pillar M 🇯🇵 ; Kimetsu no Yaiba 🇫🇷🇯🇵 · autre titre ; Kimetsu no Yaiba 2 🇫🇷🇯🇵 · autre titre |
| `one piece` | One Piece 🇫🇷🇯🇵 ; ONE PIECE HEROINES 🇯🇵 ; ONE PIECE FAN LETTER 🇯🇵 |
| `naruto` | Naruto 🇫🇷🇯🇵 ; Naruto x UT 🇯🇵 ; Naruto: Shippuuden 🇫🇷🇯🇵 |
| `naruto shippuden` | Naruto: Shippuuden 🇫🇷🇯🇵 · autre titre ; Films & spéciaux (5) |
| `bleach` | Bleach 🇫🇷🇯🇵 ; Bleach Kai 🇫🇷🇯🇵 ; Bleach: Memories of Nobody 🇫🇷🇯🇵 |
| `spy x family` | SPY×FAMILY 🇫🇷🇯🇵 ; SPY×FAMILY 3 🇫🇷🇯🇵 ; SPY×FAMILY CODE: White 🇫🇷🇯🇵 |
| `spy family` | SPY×FAMILY 🇫🇷🇯🇵 ; SPY×FAMILY 3 🇫🇷🇯🇵 ; SPY×FAMILY CODE: White 🇫🇷🇯🇵 |
| `mob psycho 100` | Mob Psycho 100 🇫🇷🇯🇵 ; Mob Psycho 100 II 🇫🇷🇯🇵 ; Mob Psycho 100 III 🇫🇷🇯🇵 |
| `my hero academia` | Boku no Hero Academia No. 170+1: More 🇫🇷🇯🇵 ; Boku no Hero Academia 7 🇫🇷🇯🇵 ; Boku no Hero Academia 5 🇫🇷🇯🇵 |
| `boku no hero` | Boku no Hero Academia: UA Heroes Battle 🇫🇷🇯🇵 ; Boku no Hero Academia 8 🇫🇷🇯🇵 ; Boku no Hero Academia No. 170+1: More 🇫🇷🇯🇵 |
| `jujutsu kaisen` | Jujutsu Kaisen 🇫🇷🇯🇵 ; Jujutsu Kaisen 3 🇫🇷🇯🇵 ; Jujutsu Kaisen 2 🇫🇷🇯🇵 |
| `sword art online` | Sword Art Online 🇫🇷🇯🇵 ; Sword Art Online Alternative: Gun Gale O 🇫🇷🇯🇵 ; Sword Art Online Alternative: Gun Gale O 🇯🇵 |
| `death note` | Death Note 🇫🇷🇯🇵 ; Death Note (HD) 🇫🇷🇯🇵 |
| `fullmetal alchemist` | Fullmetal Alchemist 🇫🇷🇯🇵 ; Fullmetal Alchemist: The Sacred Star of  🇯🇵 ; Fullmetal Alchemist: Brotherhood 🇫🇷🇯🇵 |
| `hunter x hunter` | Hunter x Hunter: Phantom Rouge 🇯🇵 ; Hunter x Hunter 🇫🇷🇯🇵 ; Hunter x Hunter: The Last Mission 🇯🇵 |
| `dragon ball` | Dragon Ball 🇫🇷🇯🇵 ; Dragon Ball Super: Broly 🇫🇷🇯🇵 ; Dragon Ball Super: Beerus 🇯🇵 |
| `dragon ball super` | Dragon Ball Super 🇫🇷🇯🇵 ; Dragon Ball Super: Super Hero 🇯🇵 ; Dragon Ball Super: Broly 🇫🇷🇯🇵 |
| `one punch man` | One Punch Man 🇫🇷🇯🇵 ; One Punch Man: Road to Hero 🇯🇵 ; One Punch Man 3 🇫🇷🇯🇵 |
| `tokyo ghoul` | Tokyo Ghoul 🇫🇷🇯🇵 ; Tokyo Ghoul √A 🇫🇷🇯🇵 ; Tokyo Ghoul:re 2 🇫🇷🇯🇵 |
| `fairy tail` | Fairy Tail 🇫🇷🇯🇵 ; Fairy Tail: 100 Years Quest 🇫🇷🇯🇵 ; Fairy Tail: Dragon Cry 🇯🇵 |
| `black clover` | Black Clover 🇫🇷🇯🇵 ; Black Clover 2 🇯🇵 ; Black Clover: Sword of the Wizard King 🇫🇷🇯🇵 |
| `chainsaw man` | Chainsaw Man 🇫🇷🇯🇵 ; Films & spéciaux (1) |
| `frieren` | Sousou no Frieren 🇫🇷🇯🇵 ; Sousou no Frieren 2 🇫🇷🇯🇵 |
| `solo leveling` | Solo Leveling 🇫🇷🇯🇵 ; Solo Leveling 2 🇫🇷🇯🇵 |
| `oshi no ko` | Oshi no Ko 🇫🇷🇯🇵 ; Oshi no Ko 3 🇫🇷🇯🇵 ; Oshi no Ko 2 🇫🇷🇯🇵 |
| `vinland saga` | Vinland Saga 🇫🇷🇯🇵 ; Vinland Saga 2 🇫🇷🇯🇵 |
| `haikyuu` | Haikyuu!! 🇫🇷🇯🇵 ; Haikyuu!! 2 🇫🇷🇯🇵 ; Haikyuu 4 !! TO THE TOP 🇫🇷🇯🇵 |
| `re zero` | Re:Zero kara Hajimeru Isekai Seikatsu 🇫🇷🇯🇵 · 4 saisons ; Re:ZERO -Starting Life in Another World- 🇫🇷🇯🇵 ; Ga-Rei-Zero 🇯🇵 · autre titre |
| `konosuba` | Kono Subarashii Sekai ni Bakuen wo! 🇫🇷🇯🇵 · autre titre ; Kono Subarashii Sekai ni Shukufuku wo! 🇫🇷🇯🇵 · autre titre ; Kono Subarashii Sekai ni Shukufuku wo! 3 🇫🇷🇯🇵 · autre titre |
| `steins gate` | Steins;Gate 🇫🇷🇯🇵 ; Steins;Gate: Fuka Ryouiki no Déjà vu 🇫🇷🇯🇵 ; Steins;Gate 0 🇯🇵 |
| `cowboy bebop` | Cowboy Bebop 🇫🇷🇯🇵 ; Cowboy Bebop: Tengoku no Tobira 🇫🇷🇯🇵 |
| `evangelion` | Evangelion: 3.0+1.0 Thrice Upon A Time 🇫🇷🇯🇵 ; Evangelion: 3.0 You Can (Not) Redo 🇫🇷🇯🇵 ; Evangelion: 1.0 You Are (Not) Alone 🇫🇷🇯🇵 |
| `gintama` | Gintama 🇯🇵 ; Gintama: Shinyaku Benizakura-hen 🇯🇵 ; Gintama: Kanketsu-hen – Yorozuya yo Eien 🇯🇵 |
| `detective conan` | Détective Conan 🇫🇷🇯🇵 ; Detective Conan: The Culprit Hanzawa 🇯🇵 ; Detective Conan: Zero’s Tea Time 🇫🇷🇯🇵 |
| `digimon` | Digimon Beatbreak 🇯🇵 ; Digimon Tamers 🇫🇷 ; Digimon Adventure (1999) 🇫🇷🇯🇵 |
| `pokemon` | Pokemon 🇫🇷🇯🇵 ; Pokémon Evolutions 🇫🇷🇯🇵 ; Pokemon (2019) 🇫🇷🇯🇵 |
| `yu-gi-oh` | Yu☆Gi☆Oh! Go Rush!! 🇯🇵 ; Yu-Gi-Oh! 3D: Bonds Beyond Time 🇫🇷🇯🇵 ; Yu☆Gi☆Oh! SEVENS 🇯🇵 |
| `saint seiya` | Saint Seiya 🇫🇷🇯🇵 ; Saint Seiya: The Hades Chapter – Sanctua 🇫🇷🇯🇵 ; Saint Seiya: The Hades Chapter – Inferno 🇫🇷🇯🇵 |
| `les chevaliers du zodiaque` | Saint Seiya 🇫🇷🇯🇵 · autre titre ; Saint Seiya: Legend of Sanctuary 🇫🇷🇯🇵 · autre titre ; Saint Seiya: The Hades Chapter – Inferno 🇫🇷🇯🇵 · autre titre |
| `ghibli` | Mimi wo Sumaseba 🇫🇷🇯🇵 · autre titre |
| `your name` | Kimi no Na wa. 🇫🇷🇯🇵 · autre titre ; Bleach: Fade to Black – Kimi no Na wo Yo 🇫🇷🇯🇵 · autre titre |
| `violet evergarden` | Violet Evergarden 🇫🇷🇯🇵 ; Violet Evergarden Gaiden: Eien to Jidou  🇫🇷🇯🇵 ; Violet Evergarden: Kitto “Ai” wo Shiru H 🇫🇷🇯🇵 |
| `the promised neverland` | Yakusoku no Neverland 🇫🇷🇯🇵 · 2 saisons · autre titre |
| `dr stone` | Dr. STONE 🇫🇷🇯🇵 ; Dr. STONE 2 : STONE WARS 🇫🇷🇯🇵 ; Dr. STONE 4 : SCIENCE FUTURE 🇫🇷🇯🇵 |
| `fire force` | Enen no Shouboutai 🇫🇷🇯🇵 · autre titre ; Enen no Shouboutai 3 🇫🇷🇯🇵 · autre titre ; Enen no Shouboutai: Ni no Shou 🇫🇷🇯🇵 · autre titre |
| `blue lock` | Blue Lock 🇫🇷🇯🇵 ; Blue Lock 2 🇫🇷🇯🇵 ; Blue Lock: EPISODE Nagi 🇫🇷🇯🇵 |
| `bocchi` | Bocchi the Rock! 🇫🇷🇯🇵 ; Loner Life in Another World 🇫🇷🇯🇵 · autre titre ; Hitoribocchi no Marumaru Seikatsu 🇯🇵 · autre titre |
| `lycoris recoil` | Lycoris Recoil 🇫🇷🇯🇵 ; Lycoris Recoil: Friends are thieves of t 🇯🇵 |
| `kaguya` | Kaguya-sama wa Kokurasetai: Tensai-tachi 🇫🇷🇯🇵 ; Kaguya-sama: Love Is War -Stairway to Ad 🇯🇵 ; Kaguya-sama wa Kokurasetai: Ultra Romant 🇫🇷🇯🇵 |
| `takagi` | Karakai Jouzu no Takagi-san 2 🇫🇷🇯🇵 ; Karakai Jouzu no Takagi-san 🇯🇵 ; Karakai Jouzu no Takagi-san: Water Slide 🇯🇵 |
| `cyberpunk` | Cyberpunk: Edgerunners 🇫🇷🇯🇵 |
| `akira` | Akira 🇫🇷🇯🇵 ; My Status as an Assassin Obviously Excee 🇫🇷🇯🇵 · autre titre |
| `berserk` | Berserk 🇯🇵 ; Berserk: Ougon Jidaihen II – Doldrey Kou 🇫🇷🇯🇵 ; Berserk: Ougon Jidaihen III – Kourin 🇫🇷🇯🇵 |
| `zzzzqqq` | — aucun résultat (message d'aide) — |
| `xx` | Watari-kun’s xx is About to Collapse 🇯🇵 ; xxxHOLiC 🇫🇷🇯🇵 · autre titre ; Sukasuka 🇯🇵 · autre titre |
| `a` | — non compris (message d'aide) — |
| `un anime qui n existe pas` | — aucun résultat (message d'aide) — |
| `l'attaque des titans` | Shingeki no Kyojin 🇫🇷🇯🇵 · autre titre ; Shingeki no Kyojin 3 🇫🇷🇯🇵 · autre titre ; Shingeki no Kyojin 2 🇫🇷🇯🇵 · autre titre |
| `one pice` | — aucun résultat (message d'aide) — |
| `narutoo` | — aucun résultat (message d'aide) — |
| `dragonball` | Dragon Ball 🇫🇷🇯🇵 · autre titre ; Dragon Ball Z 🇫🇷🇯🇵 · autre titre ; Dragon Ball GT 🇫🇷🇯🇵 · autre titre |
| `saison 2 wakfu` | Wakfu 🇫🇷 · 4 saisons ; Films & spéciaux (1) |
| `wakfu saison 3` | Wakfu 🇫🇷 · 4 saisons ; Films & spéciaux (1) |
| `naruto episode 5` | Naruto 🇫🇷🇯🇵 ; Naruto x UT 🇯🇵 ; Naruto: Shippuuden 🇫🇷🇯🇵 |
| `dernier épisode one piece` | One Piece 🇫🇷🇯🇵 ; ONE PIECE HEROINES 🇯🇵 ; ONE PIECE FAN LETTER 🇯🇵 |

Aucune erreur, aucune réponse vide sans message. Transcriptions complètes des boutons : `V2_verify_raw.md` (à la racine).

## 4. 18 parcours complets (recherche → série → saison → version → épisode → demande)
Chaque étape est affichée, même avec un seul choix (« seule saison / version disponible »), avec le fil d'Ariane. Transcription complète : `V2_flows_raw.md`.

| Parcours | Résultat |
|---|---|
| bestiale → VF → 3 | ✅ demande « Bestiale E3 — VF » |
| wakfu → Saison 2 → VF → 5 (VF seul) | ✅ « Wakfu · Saison 2 · E5 — VF » (la saison choisie reste visible) |
| carmen sandiego → Saison 1 → VF → 2 | ✅ |
| avatar → VF → 1 | ✅ |
| avatar → Quanzhi Gaoshou → VOSTFR → dernier | ✅ E12 |
| attack on titan (titre alternatif) → VOSTFR → 4 | ✅ |
| kimetsu no yaiba → VF → 5 | ✅ |
| one piece → VOSTFR → 1150 (1179 ép.) | ✅ |
| your name (film) → VOSTFR | ✅ s'arrête à « Quel épisode ? » (1 épisode) |
| naruto → Films & spéciaux | ✅ liste séparée |
| détective conan (accent) → VF → 3 | ✅ |
| l'attaque des titans (apostrophe, titre français) → VF → 2 | ✅ |
| « narutoo » puis « naruto » → VOSTFR → 10 | ✅ (la faute donne un message d'aide, pas de choix inventé) |
| bleach saison 1 (pré-rempli inexistant) | ✅ « La saison 1 n'existe pas pour Bleach. Disponible : Saison unique. » |
| mob psycho 100 → dernier | ✅ E12 |
| death note → 99999 | ✅ refusé : « le dernier épisode disponible est le 37 » |
| spy x family 2 → VOSTFR → 1 | ✅ |
| re zero (4 saisons) → Saison 4 | ✅ |

## 5. Ce que la vérification a trouvé et que j'ai corrigé
1. **Classement** : « shingeki no kyojin », « hunter x hunter » plaçaient un film/spin-off avant la série principale (le mot optionnel « no » empêchait la correspondance exacte). Corrigé (`title_match`), re-vérifié en direct : la série principale passe en premier (dragon ball, hunter x hunter, shingeki no kyojin).
2. **Récapitulatif de demande** : « Wakfu E5 — VF » oubliait la saison choisie. Maintenant « Wakfu · Saison 2 · E5 — VF ».

## 6. Limites connues (honnêtes)
- Le site n'a **aucune tolérance aux fautes** : « one pice », « narutoo » ne donnent rien (le bot le dit et invite à réessayer) ; le bot ne devine pas.
- Réponse du site plafonnée (~17) : le bot avertit « précisez le titre » quand le plafond est atteint.
- Une entrée « Kimetsu no Yaiba 2 » reste une entrée distincte (le site en fait une page à part, sans mention de saison) : jamais transformée en « saison 2 » par déduction.
- Pour les requêtes larges (« naruto », « one piece »), l'ordre des séries secondaires suit celui du site.
- Affichage déroulant des boîtes du site non lu visuellement (volet masqué) : comparaison faite via la requête de la page.

## 7. Autres tests de cette phase
- Suite complète : **1018 passed, 1 skipped** (lancée seule) + relance des suites bot/admin après corrections : 529 passed.
- Watcher ≠ recherche (`test_watcher_vs_search.py`, 7 tests) : cycle sans aucune requête de recherche ; baseline puis incrémental ; convergence dans les deux ordres et en simultané → une seule `media_key`, un seul job ; `origin` visible.
- Reprise après crash, vrais processus + vrai HTTP (`test_crash_resume_real.py`, 4 tests) : kill à 60 % (segments comptés) → recovery → fin → publié **une fois** ; kill après acceptation de la vidéo par le serveur mais avant l'enregistrement → **jamais renvoyée** (statut `failed`, décision manuelle) ; redémarrage après publication → aucun renvoi.
- **Mesure honnête** : le moteur de téléchargement actuel (`v1_poc.download_segments`) **retélécharge tous les segments** après un crash (constaté dans le code : écriture directe, aucune réutilisation) ; le test montre 12 segments servis avant kill + 20 après (32 pour 20) avec ce comportement, contre ≤ 21 avec réutilisation. La correction reste correcte (jamais de fichier partiel pris pour valide) mais coûte du temps ; la réutilisation de segments n'est **pas** implémentée dans le moteur (NON FAIT, à décider).
- Le test de crash utilise une fonction de téléchargement segment par segment de test (pas de ffmpeg sur cette machine dans le PATH) et le vrai `V2TelegramClient` contre un faux serveur Bot API HTTP local.
