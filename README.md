# SAÉ 3.02 – Suivi de flotte géolocalisée

BUT R&T 2e année – IUT de Villetaneuse – 2026-2027

Des camions simulés envoient leur position en UDP à un serveur, qui l'enregistre
dans MariaDB. Un site web (Flask) affiche la flotte : le gestionnaire voit tous
les camions, chaque conducteur voit seulement le sien.

## Organisation des dossiers

```text
SAE302/
├── README.md
├── .gitignore
├── .bdd_secret               mots de passe SQL (créé par bdd/creer_bdd.sh, jamais sur GitHub)
│
├── docs/
│   └── PROTOCOLE.md          format des messages camion -> serveur, codes d'erreur
│
├── simulation/               PHASE 1 : les camions
│   ├── simulateur.py         lance tous les camions (un thread par camion)
│   ├── routes.py             trajets réels avec OSRM + cache
│   ├── mouvement.py          déplacement réaliste (haversine)
│   ├── envoi.py              format des messages + envoi UDP (aussi utilisé par le serveur)
│   ├── config.json           lieux, camions, période d'envoi (3 s)
│   └── cache_trajets/        (créé tout seul)
│
├── serveur/                  PHASE 2 : réception des positions
│   ├── serveur_udp.py        réception UDP, vérifications, logs, écriture en base
│   ├── serveur_config.json   port, camions autorisés, base, logs
│   └── logs/                 (créé tout seul)
│
├── bdd/                      PHASE 3 : base de données
│   ├── creer_bdd.sh          crée la base, les comptes SQL et .bdd_secret
│   ├── base.py               accès MariaDB (insertion, dernière position par camion)
│   └── sql/                  01_base.sql, 02_schema.sql, 03_donnees.sql
│
├── web/                      PHASES 4 et 5 : site et comptes
│   ├── web.py                API Flask + pages
│   ├── comptes.py            inscription / vérification des mots de passe (bcrypt)
│   ├── web_config.json
│   ├── templates/            pages HTML (connexion, carte, conducteurs, inscription)
│   └── static/               css/, js/ (un fichier par page + commun.js), img/, lib/leaflet/
│
└── tests/
    ├── test_protocole.py     faux messages envoyés au serveur UDP
    └── test_securite.py      attaques sur l'API web (mots de passe, droits, injection SQL)
```

## Règle importante : tout se lance depuis la racine du projet

Les fichiers d'un dossier utilisent ceux des autres dossiers
(`serveur_udp.py` utilise `bdd/base.py` et `simulation/envoi.py`, par exemple).
Il faut donc toujours se placer dans `SAE302/` et lancer avec `python -m dossier.fichier`
(sans `.py`). Sous Linux, remplacer `python` par `python3`.

| Ce qu'on veut faire | Commande (depuis `SAE302/`) |
|---|---|
| Créer la base (une fois, sur la VM) | `sudo bash bdd/creer_bdd.sh` |
| Lancer les camions | `python -m simulation.simulateur` |
| Lancer les camions en accéléré 1 min | `python -m simulation.simulateur --acceleration 20 --duree 60` |
| Récepteur de test (sans le vrai serveur) | `python -m simulation.envoi ecoute --port 5005` |
| Lancer le serveur UDP | `python -m serveur.serveur_udp` |
| Dernière position de chaque camion | `python -m bdd.base` |
| Créer un compte gestionnaire | `python -m web.comptes inscrire gestionnaire chef@exemple.fr` |
| Lancer le site | `python -m web.web` puis ouvrir http://127.0.0.1:8080 |
| Tester le protocole (sans réseau) | `python -m tests.test_protocole` |
| Tester le protocole avec le vrai serveur | `python -m tests.test_protocole --serveur serveur/serveur_udp.py` |
| Tester la sécurité du site | `python -m tests.test_securite` |

Une autre config se donne avec `-c`, par exemple
`python -m serveur.serveur_udp -c serveur/serveur_config.local.json`.

Si on lance un fichier directement (`python serveur/serveur_udp.py`), Python
répond `ModuleNotFoundError: No module named 'bdd'` : il faut utiliser `-m`.

## Le site web

Une fois `python -m web.web` lancé, ouvrir http://127.0.0.1:8080 (ou l'adresse
de la VM). Le premier compte gestionnaire se crée en ligne de commande (voir le
tableau plus haut) ; les comptes conducteurs se créent ensuite depuis le site.

| Page | Gestionnaire | Conducteur |
|---|---|---|
| `/connexion` | connexion | connexion |
| `/carte` | toute la flotte : compteurs par état (cliquables pour filtrer), recherche par camion, plaque ou conducteur, nom du conducteur de chaque camion | tableau de bord de **son** camion : vitesse, état, direction, dernier message, distance parcourue |
| `/conducteurs` | liste des comptes, changement du camion d'un conducteur | refusée (renvoyé vers `/carte`) |
| `/inscription` | création d'un compte conducteur | refusée (renvoyé vers `/carte`) |

La carte se met à jour toutes les 3 s : marqueur de chaque camion (couleur =
état, flèche = direction) et trait du trajet récent (10 min, 30 min ou 1 h).
Un camion qui n'a rien envoyé depuis 30 s est affiché hors ligne.

Les droits sont vérifiés **par le serveur**, pas par la page : l'API ne renvoie
à un conducteur que les données de son camion (le filtre est dans la requête
SQL). Les pages ne contiennent aucune donnée, elles appellent l'API :

| Route | Rôle | Contenu |
|---|---|---|
| `POST /api/connexion`, `POST /api/deconnexion`, `GET /api/moi` | tous | session |
| `GET /api/camions` | tous | dernière position des camions visibles |
| `GET /api/positions?minutes=10&apres=0` | tous | positions récentes des camions visibles ; avec `apres`, seulement les nouvelles (rafraîchissement) |
| `GET /api/camions/<id>/positions?minutes=60` | tous | historique d'un camion (403 si ce n'est pas le sien) |
| `GET /api/utilisateurs`, `POST /api/utilisateurs`, `PUT /api/utilisateurs/<id>/camion` | gestionnaire | comptes et attribution des camions |

`python -m tests.test_securite` vérifie tout ça automatiquement (mauvais mot
de passe, conducteur qui tente de voir un autre camion, pages réservées,
injection SQL...). Le lancer avec le serveur UDP et le simulateur en marche
pour que tous les tests comptent.

## Dépendances

- Python 3.10 ou plus
- Simulation et serveur sans base : bibliothèque standard uniquement
- Base de données : `pymysql`
- Site web : `flask`, `bcrypt`, `pymysql`

Sur la VM (Ubuntu) : `sudo apt install python3-flask python3-bcrypt python3-pymysql`
(ou `pip3 install --user flask bcrypt pymysql`).

## Git : fichiers à ne jamais pousser

- `.bdd_secret` (mots de passe) et les configs `*.local.json` : déjà dans `.gitignore`.
- Les fichiers `…:Zone.Identifier` : Windows les crée quand on copie dans WSL un
  fichier téléchargé. Leur nom contient `:`, ce qui empêche ensuite tout
  `git pull` sous Windows. Ils sont dans `.gitignore` ; ne jamais les forcer avec `git add -f`.
