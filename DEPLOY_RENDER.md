# Déploiement Render

Ce projet est prêt pour Render Free en mode Web Service.

## Fichiers Render

- `render.yaml` définit le service web.
- `bot.py` démarre Flask sur le port fourni par Render.
- Telegram polling tourne dans un thread séparé.
- Routes disponibles:
  - `/`
  - `/health`

## Variables D'environnement

Dans Render, ajoute ces variables dans `Environment`:

```text
TELEGRAM_BOT_TOKEN
CLOUDINARY_CLOUD_NAME
CLOUDINARY_API_KEY
CLOUDINARY_API_SECRET
CLOUDINARY_FOLDER
GITHUB_TOKEN
GITHUB_REPO_OWNER
GITHUB_REPO_NAME
GITHUB_BRANCH
PUBLIC_CATALOG_URL
```

Valeurs recommandées:

```text
CLOUDINARY_FOLDER=telegram-meta-catalog
GITHUB_BRANCH=main
```

`config.json` n'est plus utilisé.

## Création Du Service

1. Pousse le dépôt sur GitHub.
2. Va dans Render.
3. Clique sur `New +`.
4. Choisis `Web Service`.
5. Connecte le dépôt.
6. Render détectera `render.yaml`.
7. Ajoute les variables d'environnement.
8. Déploie.

## Commandes Render

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
python bot.py
```

Health check:

```text
/health
```

## GitHub Pages

Pour rendre `meta_catalog.csv` public:

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

Le CSV sera accessible via `PUBLIC_CATALOG_URL`, par exemple:

```text
https://monusername.github.io/catalogue-whatsapp/meta_catalog.csv
```

## Render Free

Render Free peut mettre le service en veille. La route `/health` permet à Render de vérifier que le service répond. Quand le service redémarre, Flask démarre d'abord, puis le polling Telegram démarre dans un thread.
