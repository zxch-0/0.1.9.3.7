# 📥 Discord Form Bot — `/home` → **START** → formulaire → DM aux admins

Un bot Discord **entièrement en anglais** (interface, messages, embeds) qui fait exactement une chose, et la fait bien :

```
membre : /home
   ↓
carte éphémère (lui seul la voit) avec un GROS bouton vert  ▶️ START
   ↓ clic
modale Discord native avec 2 zones de texte :
   • Infos you got
   • Your Discord ID to send u results      (optionnel → par défaut = l'ID du membre)
   ↓ clic sur « Send »
le bot envoie un embed complet en MP à CHAQUE admin configuré
   ↓
le membre reçoit : ✅ Sent. An admin received your form `K7QZ` and will DM you the results shortly.
```

Côté admin, chaque MP reçoit 3 boutons :

| Bouton | Effet |
| --- | --- |
| ✅ **Mark as handled** | Verrouille le formulaire : les copies des **autres** admins se mettent à jour (« Handled by X », boutons désactivés) → personne ne travaille deux fois sur la même demande. Le membre est prévenu en MP (désactivable avec `NOTIFY_ON_HANDLED=false`). |
| ✉️ **Send a note** | Ouvre une modale ; le texte est envoyé **au nom du bot** au membre (le pseudo de l'admin n'apparaît nulle part). |
| 🗑️ **Hide** | Supprime la copie de cet admin uniquement. |

Tout est pensé pour le **niveau gratuit** de Render : pas de base de données, pas de site à héberger, aucun *privileged intent* à activer.

---

## Sommaire

1. [Ce qui est dans le repo](#1-ce-qui-est-dans-le-repo)
2. [Étape 1 — Créer l'application Discord](#2-étape-1--créer-lapplication-discord)
3. [Étape 2 — Récupérer les IDs (admin + serveur)](#3-étape-2--récupérer-les-ids-admin--serveur)
4. [Étape 3 — Tester en local (recommandé)](#4-étape-3--tester-en-local-recommandé)
5. [Étape 4 — Déployer gratuitement sur Render](#5-étape-4--déployer-gratuitement-sur-render)
6. [Étape 5 — Empêcher le bot de s'endormir (UptimeRobot)](#6-étape-5--empêcher-le-bot-de-sendormir-uptimerobot)
7. [S'en servir](#7-sen-servir)
8. [Personnaliser sans toucher au code](#8-personnaliser-sans-toucher-au-code)
9. [Sécurité, limites honnêtes](#9-sécurité-limites-honnêtes)
10. [Dépannage](#10-dépannage)
11. [Commandes de dev](#11-commandes-de-dev)

---

## 1. Ce qui est dans le repo

```
main.py                  point d'entrée (Render : python main.py)
formbot/
  config.py              lecture des variables d'env + parsing/validation des IDs  (sans Discord → testable)
  texts.py               TOUTES les phrases anglaises du bot, surchargeables par env
  store.py               état : liste d'admins, formulaires, verrou « déjà traité », anti-spam
  views.py               la carte START, la modale, les boutons des admins (DynamicItems)
  delivery.py            construit l'embed admin, vérifie l'ID tapé, envoie les MP
  client.py              le Bot, la sync des slash commands, la prod d'erreur
  cog_home.py            /home
  cog_admin.py           /adminform (gérer l'équipe sans redéployer)
  health.py              mini serveur HTTP /health (le keep-alive Render)
  main.py                supervision : reconnexion, backoff, arrêt propre sur SIGTERM
tests/                   77 tests (logique + payloads Discord + parcours complet sur faux objets)
render.yaml              blueprint Render (déploiement en 1 clic)
Dockerfile, compose      optionnel, si tu préfères Docker
.env.example             modèle de config commenté
ops/keep-alive.yml         plan B de keep-alive via GitHub Actions (à copier dans .github/workflows/)
```

---

## 2. Étape 1 — Créer l'application Discord

1. Va sur <https://discord.com/developers/applications> → **New Application** → nomme-la (ce nom sera le pseudo du bot) → **Create**.
2. Onglet **Bot** :
   - **Reset Token** → **Copy** → garde-le dans un porte-clefs. *On ne peut le voir qu'une fois.*
   - **Server Members Intent**, **Message Content Intent**, **Guild Presences** : laisse tout **OFF**. Ce bot n'a besoin d'aucun intent privilégié (il passe par l'API REST pour vérifier un ID). C'est aussi pour ça qu'il ne lit pas tes MP.
   - Décoche **Public Bot** si tu ne veux pas que n'importe qui puisse l'ajouter à son serveur.
3. Onglet **General Information** → copie l'**Application ID** (c'est le `client_id` de l'URL d'invitation).
4. Onglet **OAuth2 → OAuth2 URL Generator** :
   - Scopes : **bot** + **applications.commands** *(oublier `applications.commands` = « Unknown application command », erreur n° 1 des débutants)*
   - Bot Permissions : **View Channels**, **Send Messages**, **Embed Links**, **Attach Files**
   - Copie l'URL générée. Elle ressemble à :

     ```
     https://discord.com/api/oauth2/authorize?client_id=TON_APPLICATION_ID&permissions=52224&scope=bot%20applications.commands
     ```

5. Ouvre cette URL dans un onglet → choisis ton serveur → **Autoriser**. Le bot apparaît « hors ligne » : c'est normal, il ne se connecte qu'une fois déployé (ou lancé en local).

> 💡 Aucune **Action** n'est nécessaire dans le portail : les slash commands sont créées par le bot lui-même au démarrage.

---

## 3. Étape 2 — Récupérer les IDs (admin + serveur)

Dans le client Discord :

1. **Paramètres utilisateur → Avancé → Mode développeur : ON**.
2. Clic droit sur le (ou les) profil(s) admin → **Copier l'ID** → ce sont tes `ADMIN_USER_IDS`.
3. Clic droit sur le nom du serveur → **Copier l'ID du serveur** → c'est ton `GUILD_ID` (optionnel, mais rend `/home` disponible immédiatement).

Un ID Discord = 17-20 chiffres. Si tu colles un pseudo (`bob`, `bob#1234`), le bot ne peut pas le résoudre : il garde l'ID du membre qui a rempli le formulaire et **prévient l'admin dans le MP** — aucune demande n'est perdue.

---

## 4. Étape 3 — Tester en local (recommandé)

```bash
git clone https://github.com/TON-COMpte/TON-REPO.git
cd TON-REPO

python3 -m venv .venv && source .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env       # puis remplis DISCORD_TOKEN, ADMIN_USER_IDS, GUILD_ID
python main.py --selftest  # vérifie config + UI sans se connecter à Discord
python main.py             # démarre le bot (Ctrl+C pour arrêter)
```

Tu dois voir :

```
health server listening on http://0.0.0.0:10000/health
connecting to Discord…
slash commands: guild 123456789012345678: 2 command(s) · global: 2 command(s)
ready as FormBot#1234 (id 999…) · 1 server(s) · 2 admin(s) · cooldown 30s
```

Puis, dans Discord : `/home` → START → envoie → vérifie le MP reçu. Et fais `python main.py --selftest` / `<https://localhost:10000/health>` pour voir l'état.

Autres vérifications rapides :

```bash
curl -s localhost:10000/health | python3 -m json.tool   # JSON strict, pas de NaN
curl -s -o /dev/null -w "%{http_code}\n" localhost:10000/ready   # 200 = connecté, 503 = en cours
pytest -q                                                  # 77 passed
```

---

## 5. Étape 4 — Déployer gratuitement sur Render

### 5.a — Méthode « Blueprint » (1 clic, recommandé)

1. **Pousse ce repo sur GitHub** (le compte Render doit pouvoir y accéder) :

   ```bash
   git add -A && git commit -m "Discord form bot (/home + START + admin DMs)"
   git branch -M main
   git remote add origin https://github.com/TON-COMPTE/TON-REPO.git
   git push -u origin main
   ```

2. <https://dashboard.render.com> → **New +** → **Blueprint** → *Connect repo* → choisis le repo → Render détecte automatiquement **`render.yaml`** → **Next**.
3. Il te demande les 3 valeurs marquées `sync: false` : `DISCORD_TOKEN`, `ADMIN_USER_IDS`, `GUILD_ID` → remplit-les → **Apply Blueprint**.

   ⚠️ `DISCORD_TOKEN` doit être le **token du Bot** (chaîne longue type `MTA1…`), **pas** le *Client Secret* de l'onglet General. C'est l'erreur n° 2.

C'est fini : le build lance `pip install -r requirements.txt`, puis `python main.py`, et le *health check* `GET /health` valide le déploiement.

### 5.b — Méthode manuelle (si tu préfères cliquer partout)

**New + → Web Service** (surtout **pas** *Background Worker* : les workers ne sont pas disponibles en gratuit et ne peuvent pas être pingés — c'est précisément pour ça que le bot expose une URL de santé) :

| Champ | Valeur |
| --- | --- |
| Repo | le tien, branche `main` |
| Root Directory | *(vide)* |
| Runtime | **Python** |
| Build command | `pip install -r requirements.txt` |
| Start command | `python main.py` |
| Instance type | **Free** (512 Mo / 0,1 CPU) |
| Region | Frankfurt (le plus proche de la France en gratuit) |
| Health check path | `/health` |

Puis **Environment** → ajoute : `DISCORD_TOKEN`, `ADMIN_USER_IDS`, `GUILD_ID` *(optionnel mais utile)*, `FORM_TITLE` *(optionnel)* → **Create Web Service**.

Enfin **Settings → Signals & Health Checks** : laisse `Health Check Path = /health`. Si le processus tourne mais que Discord n'est pas joignable, `/ready` renvoie 503 — utile si tu veux un redémarrage forcé : tu peux temporairement mettre `/ready` pour voir, puis remettre `/health` (sinon Render redémarre en boucle pendant les grosses maintenance Discord).

### 5.c — Vérifier que ça tourne

- **Events** : `Build succeeded` → `Live on ...`
- **Logs** : tu dois y lire `ready as …`.
- Ouvre `https://TON-SERVICE.onrender.com/health` dans un navigateur → page de statut + JSON :

  ```json
  {"status":"ok","ready":true,"user":"FormBot#1234","servers":1,"admins":2,"forms_total":0}
  ```

- Dans Discord : `/adminform test` → tous les admins reçoivent un MP de confirmation. Si ça répond « they cannot be DM'd », l'admin a les MP fermés (Paramètres → Paramètres utilisateur → **Confidentialité du serveur → Messages directs**).
- `/home` → START → envoie → le formulaire arrive. 🎉

---

## 6. Étape 5 — Empêcher le bot de s'endormir (UptimeRobot)

### Pourquoi

Un service Render **gratuit** se met en veille **après 15 minutes sans trafic entrant**, et son réveil prend **~1 minute** (cold start). Or un bot Discord a besoin d'une connexion *persistante* au gateway :

- pendant la veille → `/home` répond « **The application did not respond from this app, try again later.** »
- les formulaires envoyés pendant la nuit ne sont pas perdus, mais les MP aux admins partent au réveil du process.

La parade standard (et gratuite) : un moniteur externe qui tape sur `/health` **toutes les 5 minutes**, ce qui compte comme trafic entrant → le conteneur ne dort jamais, donc le gateway reste connecté.

### Le site gratuit : UptimeRobot

1. <https://uptimerobot.com> → **Sign Up** (gratuit, sans carte) → vérifie l'email.
2. **Add New → HTTP(s) monitor**.
3. Remplis :
   - **Friendly Name** : `discord-bot-keepalive`
   - **URL** : `https://TON-SERVICE.onrender.com/health` ← mets bien `/health`, pas la racine
   - **Choosing an operating system / interval** : l'intervalle du plan gratuit est **5 minutes** (non réglable)
   - **Timeout** : laisse le défaut
4. **Add Monitor** → le statut passe en *Up* en quelques secondes.

C'est tout : les 5 min < aux 15 min de Render, donc la veille n'arrive plus.

Si un jour le monitoring tombe (panne UptimeRobot, monitor en pause car non connecté depuis longtemps), le service s'endort → le prochain ping le réveille, avec ~1 min d'indisponibilité.

### Plan B (déjà fourni) : GitHub Actions

`.github/workflows/keep-alive.yml` fait la même chose avec le planificateur de GitHub — gratuit, sans compte tiers, et **autorisé pour un usage commercial** (le plan gratuit d'UptimeRobot est réservé à un usage personnel/non commercial depuis fin 2024, d'où ce plan B).

Le fichier est fourni dans `ops/keep-alive.yml` (hors de `.github/` : un déploiement par GitHub App sans la permission *workflows* ne peut pas écrire dans `.github/workflows/`). Pour l'activer :

```bash
mkdir -p .github/workflows
cp ops/keep-alive.yml .github/workflows/keep-alive.yml
git add .github/workflows/keep-alive.yml && git commit -m "ci: keep the bot awake" && git push
```

Activation : **Settings → Secrets and variables → Actions → Variables** → nouvelle variable `HEALTH_URL` = `https://TON-SERVICE.onrender.com/health`. Le workflow se déclenche toutes les 8 minutes (le cron de GitHub n'est pas chronométriquement exact ; 8 min garde une marge de sécurité sous les 15 min). Tu peux cumuler les deux, c'est même le plus robuste.

Autres alternatives gratuites qui marchent : **cron-job.org**, **HetrixTools** (1 min), **StatusCake**, ou ton propre `cron`/`systemd timer` avec `curl`. Ce qui ne marche **pas** : un second service Render qui ping le premier (les deux dorment), et `self-ping` interne.

### Les 3 limites à connaître (sans bullshit)

1. **750 heures d'instance gratuites / mois pour tout le workspace.** Un service éveillé 24 h/24 = ~744 h/mois : ça passe **pour un seul** service. Ajoute un second service gratuit always-on et Render **suspend les deux** jusqu'au mois suivant. Si tu dépasses, la facture ne grimpe pas : le service est juste mis en pause.
2. **Ce n'est pas « supporté » par Render.** Rien d'interdit explicitement, mais un ping-keeper n'est pas une garantie de service ; pour un truc critique, le palier payant (512 Mo à ~1 $/mois ou Starter 7 $/mois) supprime la veille et le cold start.
3. **Les formulaires reçus pendant une micro-coupure** : le bot ne peut pas répondre à une interaction de plus de 3 s. Si le service dormait, Discord affiche « The application did not respond » et le membre doit relancer `/home`. Le `store` local ne sert pas à rejouer les formulaires : il sert au verrouillage anti-doublon.

---

## 7. S'en servir

**Côté membre**

- `/home` dans n'importe quel salon *(ou en MP avec le bot)* → la carte apparaît, **éphémère** : personne d'autre ne la voit.
- **▶️ START** ouvre la modale. Il remplit « Infos you got », met son ID (ou laisse vide), puis **Send**.
- Réponse : `✅ Sent. An admin received your form K7QZ and will DM you the results shortly.`
- Anti-spam : un formulaire par `FORM_COOLDOWN_SECONDS` (30 s par défaut), sinon le bot renvoie le temps restant.

**Côté admin**

- Un embed arrive en MP : qui a demandé, où, depuis quand, l'état du service, l'ID vérifié ou non, le contenu (et une pièce jointe `.txt` si c'est trop long pour un embed).
- Boutons **✅ Mark as handled / ✉️ Send a note / 🗑️ Hide** (voir tableau en tête de README).
- `/adminform list` : qui reçoit les formulaires + réglages courants.
- `/adminform test` : vérifie que les MP passent (à faire **après chaque déploiement**).
- `/adminform add @quelqu'un` / `remove` : change l'équipe **sans redéployer** (voir la note ci-dessous).
- `/adminform info` : instantané de config + état du runtime (le token est masqué) — parfait pour le debug à distance.
- `/adminform reset` : revient à la liste `ADMIN_USER_IDS` de l'environnement.

> **Note importante sur `/adminform add/remove`** : Render gratuit n'a **pas de disque persistant**. Les ajouts sont écrits dans `data/state.json` (donc conservés tant que le conteneur vit, y compris après un redémarrage), mais **un redéploiement efface tout** et la vérité redevient `ADMIN_USER_IDS`. Garde donc la liste des vrais admins dans la variable d'environnement, et utilise `/adminform add` pour les dépannages ponctuels.

**Le code du formulaire** (`F` `K` `7` `Q` `Z`…) sert de référence : les admins peuvent se dire « traité K7QZ », et il est repris dans la note envoyée au membre.

---

## 8. Personnaliser sans toucher au code

Tout passe par des variables d'environnement (les phrases du **bot** restent en anglais — le README est la seule chose en français 🙂). Ajoute-les dans **Render → Environment**, ou dans ton `.env` en local.

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | **requis**. Token du Bot (`DISCORD_BOT_TOKEN` est accepté aussi). |
| `ADMIN_USER_IDS` | *(vide)* | **requis**. Liste des MP de destination (IDs séparés par des virgules/espaces/points-virgules). |
| `GUILD_ID` | *(vide)* | Enregistre les commandes sur ce serveur (instantané). Vide = global (jusqu'à 1 h). |
| `FORM_TITLE` | `Intake form` | Titre de la carte et « activité » du bot. |
| `FORM_HEADLINE` | `Need something handled?` | Gros titre (`###`) de la carte. |
| `FORM_BLURB` | *(texte par défaut)* | Paragraphe sous le titre. Markdown autorisé, `\n` pour un saut de ligne. |
| `FORM_START_LABEL` | `START` | Texte du gros bouton. |
| `FORM_START_EMOJI` | `▶️` | Émoji du bouton (`none` pour retirer). Mets un émoji custom `:nom:123` → il faut la permission *Use External Emojis*. |
| `EMBED_COLOUR` | `5865F2` | Couleur (hex sans `#`). |
| `FORM_BANNER_URL` | *(vide)* | Image dans la carte. |
| `FORM_FIELD_1_LABEL` | `Infos you got` | 1ʳᵉ champ (45 car. max, limite Discord). |
| `FORM_FIELD_2_LABEL` | `Your Discord ID to send u results` | 2ᵈ champ. |
| `FORM_FIELD_*_PLACEHOLDER` | *(exemples)* | Texte fantôme dans les zones. |
| `FORM_MAX_INFOS` | `2000` | Longueur max du 1ᵉʳ champ (plafond Discord : 4000). |
| `FORM_MIN_INFOS` | `3` | Refuse « ab », « ? », etc. |
| `FORM_COOLDOWN_SECONDS` | `30` | Anti-spam par membre. `0` = désactivé. |
| `FORM_MODAL_TIMEOUT_MINUTES` | `15` | Temps de remplissage avant expiration. |
| `FORM_PAGES` | `1` | **1 à 5** : ajoute des « pages » (navigation ◀ Back / Next ▶ sur la carte). |
| `FORM_PAGE2`…`FORM_PAGE5` | — | Titre de chaque page. |
| `FORM_FIELDS_P2`…`P5` | — | Champs d'une page : `cle:Label:short\|long:required:max_length:placeholder`, séparés par `;`.<br>Ex. `proof:Proof link:short:no:250;notes:Notes:long:no:1000` |
| `NOTIFY_ON_HANDLED` | `true` | MP automatique au membre quand un admin verrouille. |
| `SYNC_ON_START` | `true` | Sync des commandes à chaque démarrage. |
| `PERSIST_STATE` / `DATA_DIR` | `true` / `data` | Écriture de `state.json` (verrou anti-doublon + admins ajoutés). |
| `MAX_STORED_SUBMISSIONS` | `200` | Nombre de formulaires gardés en mémoire/disque. |
| `HEALTH_ENABLED` | `true` | Mini serveur HTTP (le keep-alive). Ne le coupe **pas** sur le plan gratuit. |
| `PORT` | `10000` | Fourni automatiquement par Render. |
| `PING_TOKEN` | *(vide)* | Active `POST /admin/ping` avec en-tête `Authorization: Bearer …`. |
| `LOG_LEVEL` | `info` | `debug` pour voir chaque ping et chaque formulaire. |

Exemples :

- **Formulaire « 2 pages »** (page 2 = lien de preuve + notes) :

  ```
  FORM_PAGES=2
  FORM_PAGE2=Proof & extras
  FORM_FIELDS_P2=proof_link:Link to your proof:short:no:250;notes:Any other notes:long:no:1000
  ```

  Page 1 = les 2 cases prévues par ton cahier des charges, et chaque champ des pages suivantes est ajouté à l'embed admin avec son libellé.

- **Textes** : `FORM_HEADLINE=Report a leak`, `FORM_BLURB=Paste everything you have.\nWe answer in DM.` → le bouton reste `▶️ START`, la logique ne bouge pas.

Envie de toucher au code ? Les phrases sont toutes dans [`formbot/texts.py`](formbot/texts.py), la logique métier dans [`formbot/views.py`](formbot/views.py) / [`formbot/delivery.py`](formbot/delivery.py), et l'ajout d'un nouveau bouton se résume à un `DynamicItem` (le `custom_id` encode l'état, donc rien à réenregistrer au démarrage).

---

## 9. Sécurité, limites honnêtes

- **Le bot ne lit rien** : aucun intent de contenu de message. Il n'écoute que les interactions qu'on lui envoie.
- **Anti-injection** : le texte du formulaire part dans un bloc \`\`\` avec les backticks neutralisés (`\`` + `​\``) → impossible de casser le bloc ou de simuler un champ ; `@everyone`/`@here` ne pinguent pas (embed + `AllowedMentions.none()`).
- **Anti-abus du bouton admin** : chaque clic vérifie `interaction.user.id in admins`. Le MP ne peut pas être « transféré » à un tiers, mais on re-vérifie quand même ; un inconnu qui clique obtient « Only the admin team can use these buttons. ».
- **Anti-spam** : cooldown + `@app_commands.checks.cooldown(2, 20)` sur `/home`, et re-vérification au moment de l'envoi (deux modales ouvertes en parallèle ne passent pas).
- **Secrets** : le token n'est jamais loggé (`mask()`), jamais affiché par `/adminform info` (`format_env_value` ne montre que 2 premiers/2 derniers caractères).
- **Anti-doublon admin** : verrou dans le store, un seul gagnant, même si deux admins cliquent à la milliseconde près.
- **ID invalide** : si l'ID tapé ne correspond à personne de joignable, le bot **ne jette pas** la demande — il garde l'ID du soumetteur et écrit une ligne d'avertissement dans l'embed admin.
- **RGPD / ToS** : le store garde `data/state.json` (contenu des formulaires, IDs). Sur Render gratuit ce fichier est éphémère, mais si tu déplaces le bot ailleurs, traite-le comme une donnée perso : durée de conservation, purge, info aux membres. Et rappelle-toi que spammer des MP automatiquement est contraire aux règles Discord — ici seul le membre qui remplit le formulaire est contacté, par un humain.
- **Formatage Discord** : label de modale ≤ 45 car., 5 champs max par modale, bouton ≤ 80 car., embed ≤ 4096 car. → le bot tronque proprement partout, et les tests `tests/test_payloads.py` vérifient précisément ces limites (c'est le genre de bug qui n'apparaît qu'en prod).

---

## 10. Dépannage

| Symptôme | Cause probable | Fix |
| --- | --- | --- |
| « The application did not respond from this app » | Le service Render dort (le ping ne passe plus) ou vient de se lancer | Ouvre `<url>/health` dans le navigateur (réveil ~1 min), vérifie le monitor UptimeRobot, puis `/adminform info` |
| `/home` inconnu (slash grisée) | Le scope `applications.commands` manque, ou sync globale en cours, ou le bot n'est pas sur le serveur | Ré-invite avec les 2 scopes ; mets `GUILD_ID` ; attends 1 h / relance le service |
| `401 Unauthorized` au démarrage, `Discord rejected the token` | Token du *Client Secret* au lieu du token Bot, ou token expiré après Reset | Render → Environment → nouveau `DISCORD_TOKEN` (Bot → Reset Token), puis **Redeploy** |
| `Process exited with status 1` en boucle au build | Python version / dépendances | Vérifie le log de build ; `PYTHON_VERSION=3.12.6` dans le blueprint ; en dernier recours `docker build` local |
| Le membre est prévenu mais aucun admin ne reçoit de MP | `ADMIN_USER_IDS` vide/faux, ou MP désactivés côté admin, ou bot bloqué | `/adminform list`, `/adminform test` ; dans les logs : `cannot be DM'd` ; l'admin doit autoriser les MP du serveur |
| « no admin could be reached » dans la réponse au membre | Cas ci-dessus, détecté et dit honnêtement | Règle les MP, puis le formulaire est rejouable via `/adminform test` |
| Boutons des admins qui ne répondent plus | Redéploiement → `state.json` perdu → l'admin clique un ancien MP : le bot répond « I have no record of form … » | C'est voulu ; les nouveaux formulaires ont de nouveaux boutons. Ne pas demander aux admins de garder d'anciennes copies |
| `/ready` renvoie 503 pendant des minutes | Discord en maintenance / gateway en reconnexion | Les logs `formbot.main` montrent le backoff ; si tu avais mis `Health Check Path=/ready`, remets `/health` |
| Le service ne démarre pas : `no healthy upstream` | Port non lié / `PORT` ignoré | Ne jamais forcer `PORT` dans Render : le bot écoute sur `os.environ["PORT"]` |
| `PyNaCl is not installed, voice will NOT be supported` | Avertissement discord.py sur la partie vocale | Ignore-le : le bot ne fait pas de voix |
| Je veux voir chaque ping/formulaire | — | `LOG_LEVEL=debug` puis Redeploy |

Deux filets de sécurité déjà dans le code : si Discord casse, le superviseur relance avec backoff exponentiel (2 s → 2 min, max 12 échecs consécutifs puis sortie franche pour que Render affiche l'erreur) ; et sur `SIGTERM` (redéploiement) le gateway est fermé proprement, donc le bot passe immédiatement « offline » au lieu de rester faux-vert.

---

## 11. Commandes de dev

```bash
pytest -q                       # 77 tests : logique, payloads Discord, parcours complet sur faux objets
python main.py --selftest       # vérifie config/UI sans réseau
ruff check .                    # (pip install ruff) lint
python -m venv .venv && source .venv/bin/activate
docker compose up --build       # ou Dockerfile, si tu veux tourner ailleurs que sur Render
```

Le test le plus utile quand tu modifies le flow : `tests/test_flow.py` — il rejoue `/home` → START → modale → envoi → verrouillage → note avec de faux objets Discord, donc tu vois tout de suite si un bouton ne dispatch plus ou si l'ID d'un membre part au mauvais endroit.

---

### Résumé express (les 5 choses à ne pas rater)

1. `DISCORD_TOKEN` = token **Bot**, pas le client secret.
2. Invitation avec **bot + applications.commands** (sinon `/home` n'existe pas).
3. Render : **Web Service** gratuit (jamais Worker) + `python main.py` + `Health Check Path = /health`.
4. **UptimeRobot** toutes les 5 min sur `…/health` (+ le workflow GitHub Actions en bonus).
5. `ADMIN_USER_IDS` = la source de vérité (le disque Render est volatil) ; vérifie avec `/adminform test`.
