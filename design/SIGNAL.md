# Signal — design de Triat

Document unique (remplace `DESIGN.md`, `NOTHING-OS.md`, `REFONTE.md`, désormais dans `archive/`).
Les valeurs ne sont **pas** dans ce fichier : elles vivent dans [`tokens.json`](tokens.json).

## Principes

1. **Noir, puis lumière.** Fond noir ; la hiérarchie passe par la quantité de blanc (voiles de 4 à 16 %), jamais par la teinte.
2. **Un signal, deux usages séparés.** Rouge = en direct (piste en cours, segment SponsorBlock, appareil connecté). Blanc = sélection (puce, nav, interrupteur actifs : fond blanc, texte noir). Ne jamais les mélanger.
3. **Le point est le motif.** Progression, volume, pochettes de repli, spectre et chargement utilisent des points.
4. **Trois voix typographiques.** Doto pour l'affiche (3 tailles), Geist pour la lecture, Geist Mono pour les données. Inter et JetBrains Mono ne sont que des replis.
5. **Panneau carré, commande ronde.** Surfaces à 4 px (bureau) ou 8 dp (mobile) ; commandes en pilule ou en cercle.
6. **Calme mécanique.** Mouvements courts, sans rebond, entrées en `ease-out`.
7. **Le contenu d'abord.** Une pochette ou un titre sale abîme plus le design que n'importe quel token.

## Règles de rédaction et de couleur

- **Tutoiement partout** : « Mixes pour toi », « Tape un titre ». Jamais de vouvoiement, sur le bureau comme sur Android.
- **Rouge = en direct, blanc = sélection.** Le rouge n'est jamais du texte de moins de 18 px (utiliser `signal-ink`).
- **Texte** : `ink`, `ink-2` et `ink-3` respectent 4,5:1 sur `ground` et `veil-2` (testé). `ink-4` est décoratif ou réservé aux textes de 18 px et plus. `disabled` vise 3:1 pour l'UI.
- **Focus clavier** : anneau `focus` de 2 px, décalage 2 px, sur tout élément focalisable (`*:focus-visible`).

## Chaîne d'outils

- `design/tokens.json` : source unique (couleurs, typo, espacements, rayons, mouvement).
- `python tools/gen_tokens.py` écrit le bloc `:root` entre `BEGIN/END GENERATED TOKENS` de `data/style.css` ; `--check` échoue si le fichier est périmé.
- `data/style.css` est la **seule** feuille de style. Aucune couleur en dur hors du bloc généré (testé).
- Mode clair : valeurs préparées dans `tokens.json` (`color_light`), non chargées (V2).
