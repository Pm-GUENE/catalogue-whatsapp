# Bot Telegram Catalogue Meta/WhatsApp

Bot Python pour recevoir des photos produit depuis Telegram, uploader les images sur Cloudinary, générer un CSV compatible Meta Commerce Manager et le publier sur GitHub Pages.

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Le bot n'utilise pas `config.json`. Toute la configuration se fait par variables d'environnement. Pour un lancement local, copie `.env.example` vers `.env`, puis exporte les variables dans ton terminal avant `python bot.py`.

- `TELEGRAM_BOT_TOKEN`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `GITHUB_TOKEN`
- `GITHUB_REPO_OWNER`
- `GITHUB_REPO_NAME`
- `GITHUB_BRANCH`
- `PUBLIC_CATALOG_URL`

`.env` est ignoré par Git et ne doit jamais être partagé.

## Lancement

```bash
python bot.py
```

Sur Render, utilise [DEPLOY_RENDER.md](C:/Users/pm_guene/Documents/bot/DEPLOY_RENDER.md).

## Commandes Telegram

- `/start` - affiche une aide rapide
- `/ajouter` - commence l'ajout d'un produit
- `/description` - permet aussi de transmettre l'annonce en commande texte
- `/export` - envoie `output/meta_catalog.csv`
- `/publier` - publie `output/meta_catalog.csv` sur GitHub via l'API
- `/stock` - consulte le catalogue, avec filtres optionnels
- `/supprimer` - supprime une ligne du CSV avec confirmation
- `/modifierprix` - modifie uniquement le prix d'un produit
- `/annuler` - annule l'ajout en cours
- `/aide` - affiche l'aide

## Workflow Telegram

1. Envoie `/ajouter`.
2. Le bot répond: `Envoie les photos du produit.`
3. Envoie une ou plusieurs photos.
4. Le bot répond: `Photos reçues. Envoie maintenant l’annonce fournisseur.`
5. Colle l'annonce produit directement dans le chat.
6. Le bot affiche un récapitulatif et demande: `Réponds OUI pour publier ou NON pour annuler.`
7. Réponds `OUI` pour uploader les photos sur Cloudinary, enregistrer le produit, ajouter la ligne CSV et publier le CSV sur GitHub.
8. Réponds `NON` pour annuler.

## Publication GitHub

Avant chaque ajout, suppression, modification de prix, export ou consultation `/stock`, le bot synchronise `meta_catalog.csv` depuis GitHub quand la configuration GitHub est disponible. Il ajoute ensuite la nouvelle ligne ou applique la modification sur ce CSV synchronisé, puis publie le fichier complet via l'API GitHub avec PyGithub. Il n'utilise pas `git` en ligne de commande.

Le fichier est publié à la racine du dépôt sous le nom:

```text
meta_catalog.csv
```

Si le fichier existe déjà, le bot récupère son SHA et le met à jour. Sinon, il le crée.

Commit message:

```text
Update Meta catalog CSV
```

En cas d'échec GitHub, le CSV local est conservé et les données ne sont pas perdues. `/stock` lit le CSV, pas `products.json`, pour rester fiable sur Render Free.

La commande `/publier` force la publication du CSV local sur GitHub.

Réponse succès:

```text
✅ Catalogue publié
🔗 URL
```

Réponse erreur:

```text
❌ Publication échouée : raison
```

## GitHub Pages

Le CSV sera accessible via `PUBLIC_CATALOG_URL`, par exemple:

```text
https://monusername.github.io/catalogue-whatsapp/meta_catalog.csv
```

Configuration GitHub Pages:

```text
GitHub → Repository → Settings → Pages
```

Source:

```text
Deploy from a branch
```

Branch:

```text
main
```

Folder:

```text
/root
```

## Format d'annonce recommandé

```text
Titre: Sac cuir noir
Prix: 15000 FCFA
Description: Sac neuf avec bandoulière réglable.
Marque: Ma boutique
Lien: https://example.com/produits/sac-cuir-noir
```

Le bot accepte aussi un texte plus libre: il utilisera la première ligne comme titre et tentera de détecter le prix.

## Parsing WhatsApp

Le bot ignore `Nouvel arrivage` et `Stock disponible`, puis extrait `title`, `brand`, `processor`, `ram`, `storage`, `screen`, `keyboard`, `security`, `autonomy`, `price` et `touchscreen`.

Normalisations:

- `200.000frs` devient `200000`
- `08gb` devient `8GB`
- `256gb ssd` devient `SSD 256GB`
- `tactile`, `touch` ou `touchscreen` active `touchscreen`
- `HP` est toujours normalisé en `HP`

## Description Meta

Le champ `description` exporté vers Meta est généré automatiquement en français dans cet ordre:

1. processeur
2. RAM
3. stockage
4. écran
5. clavier
6. sécurité
7. autonomie

Exemple:

```text
Intel Core i5 11e génération, RAM 16GB, SSD 256GB, écran 14 pouces Full HD tactile, clavier AZERTY rétroéclairé, lecteur d'empreintes digitales et reconnaissance faciale, excellente autonomie.
```

## ID Unique

Le bot ne remplace jamais un produit existant. Chaque publication confirmée avec `OUI` ajoute une nouvelle entrée dans `output/products.json` et une nouvelle ligne dans `output/meta_catalog.csv`.

L'ID est construit avec:

```text
marque + modèle + processeur + ram + stockage + tactile
```

Exemple:

```text
dell-latitude-7420-i5-11-16gb-256gb-touch
```

Si cet ID existe déjà, le bot ajoute un suffixe `-2`, `-3`, etc.

## Cloudinary Et CSV Meta

Au moment du `OUI`, le bot:

1. télécharge les photos Telegram
2. uploade les photos sur Cloudinary
3. utilise un `public_id` au format `catalogue/<product-id>/<numero>`
4. place la première image dans `image_link`
5. place les autres images dans `additional_image_link`, séparées par virgule
6. ajoute une ligne dans `output/meta_catalog.csv`

Colonnes CSV:

```text
id,title,description,availability,condition,price,brand,image_link,additional_image_link
```

Valeurs fixes:

- `availability`: `in stock`
- `condition`: `new`
- `price`: `<montant> XOF`

## Export Et Stock

`/export` envoie `output/meta_catalog.csv` dans Telegram et affiche le nombre total de produits. Si le fichier n'existe pas, le bot répond:

```text
Catalogue introuvable.
```

`/stock` affiche le catalogue depuis Telegram avec 20 produits maximum par page. Filtres disponibles:

```text
/stock dell
/stock hp
/stock lenovo
/stock 7420
```

La recherche se fait dans `title`, `id`, `brand` et `description`. Le bouton `Télécharger CSV` envoie le fichier `output/meta_catalog.csv`.

## Suppression

`/supprimer` demande l'ID du produit ou une partie du nom, puis cherche dans `output/meta_catalog.csv`. Le bot ne supprime jamais sans confirmation. En cas de confirmation, il supprime la ligne du CSV, conserve les images Cloudinary, envoie le CSV mis à jour et affiche le nombre de produits restants.

## Modification De Prix

`/modifierprix` demande l'ID ou le nom du produit, puis cherche dans `output/meta_catalog.csv`. En cas de confirmation, seule la valeur `price` est modifiée dans le CSV.

## Robustesse Et Logs

Le bot vérifie les erreurs courantes: variables d'environnement absentes ou incomplètes, token Telegram invalide, aucune photo envoyée, annonce vide, titre/prix/RAM/stockage manquant, problème Cloudinary, CSV introuvable, erreur Telegram et échec GitHub.

Les logs sont écrits dans `logs/bot.log`. Les secrets chargés depuis l'environnement sont masqués avant écriture dans les logs.

## Fichiers Générés

- `downloads/` contient les images téléchargées depuis Telegram.
- `output/products.json` contient les produits enregistrés.
- `output/meta_catalog.csv` contient le catalogue exporté.
- `logs/bot.log` contient les logs d'exécution sans secrets.
