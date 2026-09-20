# Product

## Register
product

## Users
Un seul utilisateur : le propriétaire d'un pipeline automatique qui publie des épisodes d'anime dans un canal Telegram public. Il consulte le panneau plusieurs fois par jour, sur ordinateur et sur téléphone, pour savoir en quelques secondes si tout tourne, et il intervient seulement quand quelque chose bloque (relancer un épisode, ajouter un anime, mettre la file en pause). Ce n'est pas un développeur au moment où il ouvre le panneau : il veut des phrases, pas des identifiants ni du JSON.

## Product Purpose
Le panneau web d'administration (local, 127.0.0.1) rend visible et pilotable le pipeline source → téléchargement → validation → miniature → Telegram → nettoyage. Réussite : voir l'état global sans rien cliquer, agir en un ou deux clics, ne jamais lire de donnée brute. Les mêmes chiffres que le bot Telegram d'administration.

## Brand Personality
Sobre, précis, rassurant. Une salle de contrôle calme : elle ne crie que lorsqu'il y a un vrai problème. Voix : française, directe, verbes d'action, aucun jargon technique dans l'interface principale.

## Anti-references
- Le JSON brut ou les identifiants techniques affichés comme contenu.
- Le tableau de bord « SaaS » générique : grosses cartes de métriques identiques, dégradés, halos, emoji en guise d'icônes.
- Les fenêtres modales pour des tâches simples, les alertes qui clignotent.

## Design Principles
1. L'état d'abord : une phrase de statut global toujours visible, avant tout détail.
2. Une donnée = une phrase lisible (« il y a 41 min », « 348 Mo », « en attente de la source »), jamais la valeur brute.
3. Un seul accent (émeraude) pour l'action et le bon état ; ambre et rouge réservés à ce qui demande de l'attention.
4. Agir sur place : les actions sont à côté de l'objet, la confirmation est intégrée à la ligne.
5. Même contenu sur ordinateur et téléphone, recomposé (menu latéral → barre du bas, tableaux → cartes).

## Accessibility & Inclusion
Contraste AA minimum, focus clavier visible, navigation complète au clavier, lecteurs d'écran (rôles, `aria-live` pour les mises à jour), `prefers-reduced-motion` respecté, l'état n'est jamais porté par la seule couleur (libellé + icône).
