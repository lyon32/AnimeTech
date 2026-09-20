# Design — Panneau d'administration

Monde visuel : une salle de contrôle sobre, de nuit. Deux gris anthracite légèrement bleutés, texte doux, un seul accent émeraude ; l'ambre et le rouge n'apparaissent que pour l'attention.

## Couleurs (OKLCH, teinte de fond 250)
| Jeton | Valeur | Rôle |
|---|---|---|
| `--bg` | oklch(0.17 0.008 250) | fond de page |
| `--surface` | oklch(0.21 0.009 250) | panneaux |
| `--surface-2` | oklch(0.25 0.010 250) | survol, contrôles |
| `--line` | oklch(0.31 0.010 250) | filets 1px |
| `--text` | oklch(0.94 0.005 250) | texte principal |
| `--muted` | oklch(0.74 0.012 250) | texte secondaire (≥ 4,5:1 sur `--surface`) |
| `--accent` | oklch(0.78 0.15 160) | action, bon état |
| `--warn` | oklch(0.83 0.14 80) | attention |
| `--bad` | oklch(0.72 0.18 25) | à traiter |
| `--info` | oklch(0.77 0.09 240) | en cours |

## Typographie
Pile système (`Segoe UI Variable`, `system-ui`), chiffres tabulaires pour toute donnée. Échelle : 12 / 13 / 14 (base) / 16 / 20 / 26 px. Titre de page 26 px semi-gras, titres de section 14 px semi-gras (pas de sur-titres). Mesure ≤ 70 ch. Monospace uniquement pour les identifiants et détails techniques.

## Espacement et forme
Base 4 px : 4 / 8 / 12 / 16 / 24 / 32. Rayons 8 px (panneaux), 6 px (contrôles), pilule pour les badges. Profondeur : filets et un seul ombrage doux sur le tiroir.

## Composants
- **Badge d'état** : point + libellé, teinte selon le ton (ok / info / warn / bad / muted).
- **Bande de chiffres** : quatre valeurs séparées par des filets, pas de cartes.
- **Jauge** : piste 6 px, remplissage accent (ou ton du seuil).
- **Tableau** → cartes sous 760 px (`data-label`).
- **Tiroir de détail** à droite (feuille pleine largeur sur téléphone), fermeture Échap.
- **Confirmation en ligne** : le bouton devient « Confirmer ? » 4 s.
- **Toast** discret en bas, `aria-live="polite"`.

## Mouvement
Un seul moment : apparition de la page (opacité + 6 px, 200 ms, ease-out exponentiel). Désactivé sous `prefers-reduced-motion`.

## Icônes
Jeu unique dessiné en SVG, trait 1,6 px, arrondi, 20 px. Aucun emoji.

## Mise en page « tout dans l'écran » (bureau ≥ 60 rem de large et ≥ 38 rem de haut)
`.app`/`.col` ont la hauteur de la fenêtre : l'en-tête reste fixe et `main` défile seul. La Vue d'ensemble (`.overview`) remplit `main` : bande de 5 chiffres, puis 2 colonnes (`.ov-grid` 3fr/2fr) — gauche : En cours, En attente (4 max), Derniers publiés (8, défilement interne) ; droite : Système (jauges + cycle) et Anime surveillés (`.chips-grid`, une ligne de 32 px par anime, point d'état, défilement interne). Un état normal n'a pas de badge ; seuls « en pause » et les échecs sont signalés. Épisodes : tableau à défilement interne, en-têtes collants, pagination toujours visible. Anime : liste et cycles côte à côte dès 95 rem. File : couloirs en grille (`.lanes`). Erreurs et Santé : colonnes automatiques. Sous 60 rem (téléphone/tablette) : flux normal, page qui défile.
