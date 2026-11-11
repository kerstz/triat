# Logo Triat

Trois points pleins de même diamètre sur une grille de 7 par 7 (viewBox 128, pas 16) : deux à gauche, un à droite, un triangle de lecture. Les centres sont en (48, 32), (48, 96) et (96, 64), rayon 12. Le centre de gravité du triangle tombe sur l'axe du cadre, ce qui corrige le centre optique.

## Quelle variante pour quel usage

| Fichier | Usage |
|---|---|
| `triat-mark.svg` | marque Papier sur transparent, pour fond sombre |
| `triat-mark-dark.svg` | marque Encre sur transparent, pour fond clair |
| `triat-mark-mono.svg` | marque en `currentColor`, pour usage inline et symbolique |
| `triat-app-icon.svg` | icône d'app sombre, référence 512 (bureau, dépôt, page web) |
| `triat-app-icon-light.svg` | icône d'app claire |
| `triat-app-icon-32.svg`, `triat-app-icon-16.svg` | petites tailles, sans matrice, coordonnées entières |
| `png/` | rendus de l'icône sombre en 16, 32, 48, 64, 128, 256 et 512 (16 et 32 viennent des variantes small) |

Palette : Encre `#000000`, Papier `#F2F2F2`. Matrice de petits points : `#2A2A2A` sur Encre, `#D6D6D2` sur Papier.

## Règles

- Pas de couleur d'accent fixe. Le logo reste noir et blanc.
- Zone de protection : un diamètre de point (24 unités sur 128) autour de la marque.
- Ne pas déformer, pivoter, ni recolorer. Sous 48 px, utiliser les variantes small.
- Aucun texte dans les fichiers : pas de police à embarquer.
