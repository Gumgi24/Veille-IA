# Veille IA

Application web autonome de veille des sources citées par les IA (remplace le workflow n8n).

Pour chaque **campagne**, l'application envoie une liste de **prompts** (CSV) aux API de
plusieurs fournisseurs — **OpenAI (GPT search)**, **Google Gemini** (google_search),
**Anthropic Claude** (web_search) et **xAI Grok** (web_search) — puis extrait **chaque
source citée** dans les réponses et l'enregistre : une ligne par URL, avec la réponse,
le modèle, la langue, le domaine et sa catégorie.

Fonctionnalités :

- **Campagnes** : import CSV des prompts (`Catégorie, Prompt, Langue, LOC`), planification
  (tous les N jours à HH:MM, date de fin), lancement manuel, pause/reprise.
- **Exécution hybride batch / direct** : les prompts **sans proxy** sont regroupés
  en un lot par fournisseur via leur **Batch API** (OpenAI, Anthropic, Gemini, xAI —
  ≈ −50% de coût, résultats sous 24h, souvent bien moins) ; les prompts **avec
  proxy** partent en direct (concurrence bornée par un semaphore par fournisseur,
  timeouts, retries). Si un batch échoue, ses prompts repassent automatiquement en
  direct. Les batchs en attente survivent à un redémarrage du serveur (repris au
  boot). Désactivable dans **Réglages → Mode batch**.
- **Proxy par prompt** : la colonne `LOC` (ex. `http://user:pass@ip:port`) route la
  requête API via ce proxy (mode direct uniquement — un batch s'exécute chez le
  fournisseur, sans contrôle de l'IP de sortie).
- **Résolution des redirections Gemini** : les URLs `vertexaisearch.cloud.google.com`
  sont résolues vers l'URL finale (l'originale est conservée en `URL_Originale`).
- **Export CSV persistant** : `Date, Réponse, Modèle, Prompt, Catégorie_Prompt,
  Langue, URL, Domaine, URL_Originale, Catégorie` — filtrable par modèle / langue /
  prompt / catégorie de prompt / catégorie de source.
- **Exports analytiques** : TCD Catégorie de source × Prompt en valeurs absolues ou
  en % (CSV), liste des prompts uniques triable par catégorie / prompt / modèle /
  langue (CSV et texte brut).
- **Catégories de domaines** : jeux réutilisables entre campagnes, import CSV
  (`Catégorie, Domaine`), duplication d'un jeu, catégorisation rapide des domaines
  encore inconnus depuis le dashboard.
- **Dashboard** : sunburst Catégorie → Domaine, tableaux interactifs (domaines,
  catégories, URLs, pivot Catégorie × Prompt), filtres croisés.
- **Traçabilité des erreurs** : journal par campagne/run (niveau, fournisseur, message,
  payload d'erreur complet), progression et annulation des runs en direct.
- **Clés API** : saisies dans l'interface (Réglages), stockées dans la base SQLite
  locale, jamais renvoyées au navigateur.

## Windows (poste local)

Installer [Python 3.10+](https://www.python.org/downloads/) (cocher *Add python.exe
to PATH*), cloner le dépôt, puis **double-cliquer `start-windows.bat`** — il crée
l'environnement, installe les dépendances, démarre le serveur et ouvre le navigateur
sur http://127.0.0.1:8000.

## Installation locale (dev, Linux/macOS)

```bash
git clone <repo> && cd Veille-IA
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Ouvrir http://127.0.0.1:8000 — aller dans **Réglages** pour saisir les clés API,
créer une campagne, importer le CSV de prompts, puis **Lancer maintenant**.

## Déploiement sur un VPS Ubuntu

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone <repo> /opt/veille-ia && cd /opt/veille-ia
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Service systemd (`/etc/systemd/system/veille-ia.service`) :

```ini
[Unit]
Description=Veille IA
After=network.target

[Service]
WorkingDirectory=/opt/veille-ia
ExecStart=/opt/veille-ia/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
Environment=TZ=Europe/Paris

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now veille-ia
```

Puis exposer via nginx/caddy (recommandé pour le HTTPS) :

```
server {
    listen 80;
    server_name veille.example.com;
    location / { proxy_pass http://127.0.0.1:8000; }
}
```

## Sécurité

- L'interface et l'API sont protégées par **HTTP Basic auth**. Identifiants par
  défaut : `admin` / `admin` — **à changer immédiatement** dans **Réglages →
  Sécurité** (un bandeau d'avertissement s'affiche tant que le mot de passe par
  défaut est actif).
- Chaque campagne a un **lien visiteur** en lecture seule :
  `https://…/share/<token>` (bouton « 🔗 Lien visiteur » dans la campagne). Ce
  lien donne accès au dashboard, aux résultats et aux exports CSV de cette seule
  campagne, sans authentification. Le bouton **↻** révoque le lien et en génère
  un nouveau.
- Basic auth transite en clair sur HTTP : sur un VPS public, servez l'app
  derrière HTTPS (nginx/caddy ci-dessus).

> ⚠️ Les horaires de planification utilisent l'heure locale du serveur — réglez `TZ`
> (ex. `Europe/Paris`) dans le service systemd.

## Données

Tout est stocké dans `data/veille.db` (SQLite, mode WAL). Sauvegarde = copier ce
fichier. Le dossier `data/` est ignoré par git.

## Formats CSV

**Prompts** (import campagne) — en-têtes insensibles à la casse/accents, séparateur `,` ou `;` :

| Catégorie | Prompt | Langue | LOC |
|---|---|---|---|
| Réputation | Quelles sont les polémiques autour de X ? | Français | http://user:pass@ip:port *(optionnel)* |

**Catégories de domaines** (import jeu de catégories) :

| Catégorie | Domaine |
|---|---|
| Presse Généraliste | lemonde.fr |

## Architecture

```
app/
  main.py       # API FastAPI + service des fichiers statiques
  db.py         # SQLite (campagnes, prompts, runs, résultats, batchs, événements, catégories)
  providers.py  # corps de requête + parsing par fournisseur (partagés direct/batch)
  batch.py      # adaptateurs Batch API (submit/poll/fetch/cancel) des 4 fournisseurs
  runner.py     # orchestration d'un run (hybride batch/direct, retries, résolution d'URLs)
  scheduler.py  # APScheduler : un cron par campagne active
static/         # frontend (vanilla JS + ECharts)
data/           # base SQLite (créée au premier lancement)
```
