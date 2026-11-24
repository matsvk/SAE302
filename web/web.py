"""
web.py - Phase 4 : API web avec connexion, sessions et droits (Flask).

    P1 : inscription avec bcrypt  -> comptes.py (réutilisé ici)
    P2 : connexion et sessions
    P3 : chaque conducteur est lié à son camion, filtrage CÔTÉ SERVEUR

Deux rôles (table utilisateurs, voir bdd/sql/02_schema.sql) :
    gestionnaire  voit toute la flotte, crée les comptes conducteurs et leur
                  attribue un camion
    conducteur    rattaché à UN camion (obligatoire, vérifié par la base) :
                  ne voit que celui-là

Pourquoi les conducteurs ne s'inscrivent pas eux-mêmes : comme c'est le
conducteur qui choisirait son camion, n'importe qui pourrait créer un compte
"conducteur de CAM-02" et suivre ce camion. Le lien conducteur <-> camion est
donc toujours décidé par un gestionnaire (ou en ligne de commande avec comptes.py).

Routes (tout en JSON) :
    POST /api/connexion                       {"email", "mot_de_passe"}
    POST /api/deconnexion
    GET  /api/moi
    GET  /api/camions                         dernière position des camions visibles
    GET  /api/camions/<id>/positions?minutes=60
    GET  /api/utilisateurs                    (gestionnaire)
    POST /api/utilisateurs                    (gestionnaire) {"email", "mot_de_passe", "camion_id"}
    PUT  /api/utilisateurs/<id>/camion        (gestionnaire) {"camion_id"}

Le filtrage est fait dans les requêtes SQL : un conducteur ne reçoit jamais les
données des autres camions, il n'y a rien à "cacher" côté navigateur.

Pages (Phase 5) : HTML + JavaScript dans web/templates/ et web/static/, qui appellent l'API
    GET  /                 -> /carte si connecté, sinon /connexion
    GET  /connexion        page de connexion (tout le monde)
    GET  /inscription      création d'un compte conducteur (gestionnaire seulement)
    GET  /carte            page d'accueil après connexion (carte Leaflet : étape suivante)
Les droits des pages sont vérifiés ici, côté serveur : un conducteur qui tape
/inscription est renvoyé vers /carte sans jamais recevoir la page.

Lancement (depuis la racine du projet) :  python3 -m web.web     (lit web/web_config.json)
"""

import argparse
import copy
import hashlib
import json
import logging
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from decimal import Decimal
from functools import wraps

from flask import Flask, g, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException

from bdd.base import DEFAUTS_BDD, ErreurBase, lire_secret, pymysql
from web import comptes
from web.comptes import ErreurInscription

log = logging.getLogger("web")

FORMAT_CAMION = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

DEFAUTS = {
    "ecoute": {"hote": "127.0.0.1", "port": 8080},
    "bdd": {**DEFAUTS_BDD, "utilisateur": "flotte_web", "cle_secret": "FLOTTE_WEB_MDP"},
    "session": {"inactivite_minutes": 120, "duree_max_heures": 12, "cookie_secure": False},
    "securite": {"echecs_avant_blocage": 5, "blocage_minutes": 15},
}


class ErreurApi(Exception):
    def __init__(self, statut, message):
        super().__init__(message)
        self.statut = statut
        self.message = message


def en_json(valeur):
    """Valeurs MariaDB -> JSON (Decimal -> nombre, date -> texte ISO en UTC)."""
    if isinstance(valeur, Decimal):
        return float(valeur)
    if isinstance(valeur, datetime):
        return valeur.isoformat(timespec="milliseconds") + "Z"
    return valeur


def ligne_json(ligne):
    return {cle: en_json(v) for cle, v in ligne.items()}


def empreinte(hash_mdp):
    """Petite empreinte du hash, gardée dans la session : si le mot de passe change,
    elle change aussi et les anciennes sessions sont refusées. Le hash lui-même ne
    va jamais dans le cookie (le contenu d'un cookie Flask est lisible)."""
    return hashlib.sha256(hash_mdp.encode("ascii")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Protection contre la force brute (en mémoire)
# ---------------------------------------------------------------------------

class CompteurEchecs:
    """Compte les mots de passe faux par adresse e-mail. Après N échecs, l'adresse
    est bloquée quelques minutes, même avec le bon mot de passe ensuite.
    Gardé en mémoire (la table utilisateurs n'a pas de colonne pour ça) : un
    redémarrage du site remet les compteurs à zéro."""

    def __init__(self, max_echecs, blocage_s):
        self.max_echecs = max_echecs
        self.blocage_s = blocage_s
        self.verrou = threading.Lock()
        self.echecs = {}        # email -> nombre d'échecs
        self.bloque = {}        # email -> fin du blocage (time.monotonic())

    def secondes_restantes(self, email):
        with self.verrou:
            fin = self.bloque.get(email)
            if fin is None:
                return 0
            if time.monotonic() >= fin:
                del self.bloque[email]
                return 0
            return fin - time.monotonic()

    def echec(self, email):
        with self.verrou:
            if len(self.echecs) > 100_000:      # garde-fou mémoire
                self.echecs.clear()
            n = self.echecs.get(email, 0) + 1
            if n >= self.max_echecs:
                self.bloque[email] = time.monotonic() + self.blocage_s
                n = 0
            self.echecs[email] = n

    def reussite(self, email):
        with self.verrou:
            self.echecs.pop(email, None)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def creer_app(conf):
    app = Flask(__name__)
    conf_bdd = conf["bdd"]
    app.config.update(
        SECRET_KEY=lire_secret(conf_bdd, "FLOTTE_SESSION_CLE"),   # créée par creer_bdd.sh
        SESSION_COOKIE_NAME="flotte_session",
        SESSION_COOKIE_HTTPONLY=True,       # illisible par le JavaScript (vol par XSS)
        SESSION_COOKIE_SAMESITE="Strict",   # jamais envoyé depuis un autre site (CSRF)
        SESSION_COOKIE_SECURE=conf["session"]["cookie_secure"],   # true dès qu'on est en HTTPS
        PERMANENT_SESSION_LIFETIME=conf["session"]["inactivite_minutes"] * 60,
        SESSION_REFRESH_EACH_REQUEST=True,  # l'expiration repart à chaque requête
        MAX_CONTENT_LENGTH=4096,
    )
    # accents gardés tels quels dans le JSON (Flask 2.2+ : app.json ; avant : JSON_AS_ASCII)
    if hasattr(app, "json"):
        app.json.ensure_ascii = False
    else:
        app.config["JSON_AS_ASCII"] = False
    duree_max = conf["session"]["duree_max_heures"] * 3600
    secu = conf["securite"]
    echecs = CompteurEchecs(secu["echecs_avant_blocage"], secu["blocage_minutes"] * 60)
    sessions_fermees = set()          # identifiants des sessions déconnectées
    verrou_sessions = threading.Lock()

    # -- base : une connexion par requête, avec le compte flotte_web -------------

    def bdd():
        if "cnx" not in g:
            try:
                g.cnx = comptes.connecter(conf_bdd)
            except ErreurBase as e:
                log.error("base de données inaccessible : %s", e)
                raise ErreurApi(503, "service momentanément indisponible") from None
        return g.cnx

    def sql(requete, parametres=(), un=False, ecriture=False):
        """Requête PARAMÉTRÉE : les valeurs partent à part, jamais collées dans le
        texte SQL. C'est ce qui rend les injections SQL impossibles."""
        cnx = bdd()
        with cnx.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(requete, parametres)
            resultat = cur.fetchone() if un else cur.fetchall()
            nb = cur.rowcount
        cnx.commit()
        return nb if ecriture else resultat

    @app.teardown_appcontext
    def fermer_bdd(_exc):
        cnx = g.pop("cnx", None)
        if cnx is not None:
            try:
                cnx.close()
            except Exception:
                pass

    def lire_json(*champs):
        # On exige du JSON : un formulaire d'un autre site (CSRF) ne peut pas en envoyer
        if not request.is_json:
            raise ErreurApi(415, "le corps de la requête doit être du JSON (Content-Type: application/json)")
        donnees = request.get_json(silent=True)
        if not isinstance(donnees, dict):
            raise ErreurApi(400, "JSON invalide : un objet {...} est attendu")
        for champ in champs:
            if champ not in donnees:
                raise ErreurApi(400, f"champ « {champ} » manquant")
        return donnees

    # -- P2 : qui est connecté ? ---------------------------------------------------

    @app.before_request
    def charger_utilisateur():
        g.utilisateur = None
        uid = session.get("uid")
        if uid is None:
            return
        trop_vieille = not isinstance(session.get("debut"), (int, float)) \
            or time.time() - session["debut"] > duree_max
        with verrou_sessions:
            fermee = session.get("sid") in sessions_fermees
        if trop_vieille or fermee:
            session.clear()
            return
        # Le compte est relu à chaque requête : un camion réattribué ou un mot de
        # passe changé prend effet tout de suite, sans attendre une reconnexion.
        u = sql("SELECT id, email, mot_de_passe, role, camion_id FROM utilisateurs WHERE id = %s",
                (uid,), un=True)
        if u is None or empreinte(u["mot_de_passe"]) != session.get("empreinte"):
            session.clear()       # compte supprimé ou mot de passe changé
            return
        del u["mot_de_passe"]
        g.utilisateur = u

    def connexion_requise(fonction):
        @wraps(fonction)
        def verifiee(*args, **kwargs):
            if g.utilisateur is None:
                raise ErreurApi(401, "connexion requise")
            return fonction(*args, **kwargs)
        return verifiee

    def gestionnaire_requis(fonction):
        @wraps(fonction)
        @connexion_requise
        def verifiee(*args, **kwargs):
            if g.utilisateur["role"] != "gestionnaire":
                log.warning("accès refusé : %s (%s) -> %s %s", g.utilisateur["email"],
                            g.utilisateur["role"], request.method, request.path)
                raise ErreurApi(403, "accès réservé aux gestionnaires")
            return fonction(*args, **kwargs)
        return verifiee

    # -- P2 : connexion / déconnexion -------------------------------------------------

    @app.post("/api/connexion")
    def connexion():
        d = lire_json("email", "mot_de_passe")
        if not isinstance(d["email"], str) or not isinstance(d["mot_de_passe"], str):
            raise ErreurApi(400, "email et mot_de_passe doivent être des textes")
        email = d["email"].strip().lower()[:254]
        mdp = d["mot_de_passe"]

        reste = echecs.secondes_restantes(email)
        if reste:
            log.warning("connexion à une adresse bloquée : %s depuis %s", email, request.remote_addr)
            raise ErreurApi(429, f"trop de tentatives, réessayez dans {int(reste // 60) + 1} min")

        try:
            if len(mdp.encode("utf-8")) > comptes.OCTETS_MAX:
                # bcrypt refuse plus de 72 octets : forcément faux. On fait quand même
                # un calcul bcrypt pour que la réponse prenne le même temps.
                comptes.verifier(conf_bdd, email, "x")
                compte = None
            else:
                compte = comptes.verifier(conf_bdd, email, mdp)   # bcrypt, même pour un email inconnu
        except ErreurBase as e:
            log.error("base de données inaccessible : %s", e)
            raise ErreurApi(503, "service momentanément indisponible") from None

        if compte is None:
            echecs.echec(email)
            log.warning("connexion refusée : %r depuis %s", email, request.remote_addr)
            # même message dans tous les cas : on ne dit pas si c'est l'email ou le mot de passe
            raise ErreurApi(401, "adresse e-mail ou mot de passe incorrect")

        echecs.reussite(email)
        h = sql("SELECT mot_de_passe FROM utilisateurs WHERE id = %s", (compte["id"],), un=True)
        session.clear()                       # nouvelle session (contre la fixation de session)
        session.permanent = True
        session["uid"] = compte["id"]
        session["sid"] = secrets.token_hex(16)
        session["debut"] = time.time()
        session["empreinte"] = empreinte(h["mot_de_passe"])
        log.info("connexion : %s (%s) depuis %s", compte["email"], compte["role"], request.remote_addr)
        return jsonify({"email": compte["email"], "role": compte["role"], "camion_id": compte["camion_id"]})

    @app.post("/api/deconnexion")
    def deconnexion():
        if session.get("sid"):
            # le cookie est noté comme fermé : une copie (volée) ne sert plus à rien
            with verrou_sessions:
                if len(sessions_fermees) > 100_000:
                    sessions_fermees.clear()
                sessions_fermees.add(session["sid"])
        if g.utilisateur:
            log.info("déconnexion : %s", g.utilisateur["email"])
        session.clear()
        return jsonify({"message": "déconnecté"})

    @app.get("/api/moi")
    @connexion_requise
    def moi():
        u = g.utilisateur
        return jsonify({"email": u["email"], "role": u["role"], "camion_id": u["camion_id"]})

    # -- P3 : données filtrées selon le compte -------------------------------------------

    def camions_visibles(u):
        """None = toute la flotte (gestionnaire) ; sinon le seul camion du conducteur."""
        if u["role"] == "gestionnaire":
            return None
        return {u["camion_id"]}

    @app.get("/api/camions")
    @connexion_requise
    def camions():
        visibles = camions_visibles(g.utilisateur)
        if visibles is None:
            lignes = sql("SELECT * FROM v_derniere_position ORDER BY camion_id")
        else:
            # le filtre est DANS la requête : les autres camions ne quittent jamais la base
            lignes = sql("SELECT * FROM v_derniere_position WHERE camion_id = %s",
                         (g.utilisateur["camion_id"],))
        return jsonify([ligne_json(l) for l in lignes])

    @app.get("/api/camions/<camion_id>/positions")
    @connexion_requise
    def historique(camion_id):
        if not FORMAT_CAMION.fullmatch(camion_id):
            raise ErreurApi(400, "identifiant de camion invalide")
        visibles = camions_visibles(g.utilisateur)
        if visibles is not None and camion_id not in visibles:
            # même réponse que le camion existe ou non : on ne révèle pas la flotte
            log.warning("accès refusé : %s (conducteur de %s) a demandé %s", g.utilisateur["email"],
                        g.utilisateur["camion_id"], camion_id)
            raise ErreurApi(403, "vous n'avez pas accès à ce camion")
        try:
            minutes = int(request.args.get("minutes", "60"))
        except ValueError:
            raise ErreurApi(400, "minutes doit être un nombre entier") from None
        if not 1 <= minutes <= 1440:
            raise ErreurApi(400, "minutes doit être entre 1 et 1440")
        if sql("SELECT id FROM camions WHERE id = %s", (camion_id,), un=True) is None:
            raise ErreurApi(404, "camion inconnu")
        # dates en UTC dans la base : on compare avec UTC_TIMESTAMP()
        lignes = sql("SELECT seq, horodatage, lat, lon, vitesse, cap, etat FROM positions "
                     "WHERE camion_id = %s AND horodatage >= UTC_TIMESTAMP(3) - INTERVAL %s MINUTE "
                     "ORDER BY horodatage DESC, seq DESC LIMIT 2000", (camion_id, minutes))
        return jsonify({"camion_id": camion_id, "minutes": minutes,
                        "positions": [ligne_json(l) for l in lignes]})

    # -- P3 : le gestionnaire lie chaque conducteur à son camion ----------------------------

    @app.get("/api/utilisateurs")
    @gestionnaire_requis
    def lister_utilisateurs():
        lignes = sql("SELECT id, email, role, camion_id FROM utilisateurs ORDER BY role, email")
        return jsonify(lignes)          # jamais les hash

    @app.post("/api/utilisateurs")
    @gestionnaire_requis
    def creer_conducteur():
        d = lire_json("email", "mot_de_passe", "camion_id")
        if not all(isinstance(d[c], str) for c in ("email", "mot_de_passe", "camion_id")):
            raise ErreurApi(400, "email, mot_de_passe et camion_id doivent être des textes")
        if not FORMAT_CAMION.fullmatch(d["camion_id"]):
            raise ErreurApi(400, "identifiant de camion invalide")
        try:
            # rôle écrit en dur : l'API ne crée jamais de gestionnaire
            nouvel_id = comptes.inscrire(conf_bdd, d["email"], d["mot_de_passe"],
                                         role="conducteur", camion_id=d["camion_id"])
        except ErreurInscription as e:
            statut = 409 if "existe déjà" in str(e) else 400
            raise ErreurApi(statut, str(e)) from None
        except ErreurBase as e:
            log.error("base de données inaccessible : %s", e)
            raise ErreurApi(503, "service momentanément indisponible") from None
        email = d["email"].strip().lower()
        log.info("gestionnaire %s : conducteur %s créé sur %s", g.utilisateur["email"], email, d["camion_id"])
        return jsonify({"id": nouvel_id, "email": email, "role": "conducteur",
                        "camion_id": d["camion_id"]}), 201

    @app.put("/api/utilisateurs/<int:uid>/camion")
    @gestionnaire_requis
    def attribuer_camion(uid):
        d = lire_json("camion_id")
        camion_id = d["camion_id"]
        if not isinstance(camion_id, str) or not FORMAT_CAMION.fullmatch(camion_id):
            raise ErreurApi(400, "camion_id : identifiant de camion attendu")
        u = sql("SELECT role FROM utilisateurs WHERE id = %s", (uid,), un=True)
        if u is None:
            raise ErreurApi(404, "compte inconnu")
        if u["role"] != "conducteur":
            raise ErreurApi(400, "un gestionnaire n'est rattaché à aucun camion")
        if sql("SELECT id FROM camions WHERE id = %s", (camion_id,), un=True) is None:
            raise ErreurApi(404, "camion inconnu")
        sql("UPDATE utilisateurs SET camion_id = %s WHERE id = %s", (camion_id, uid), ecriture=True)
        log.info("gestionnaire %s : compte %d -> %s", g.utilisateur["email"], uid, camion_id)
        return jsonify({"id": uid, "camion_id": camion_id})

    # -- Phase 5 : pages HTML -----------------------------------------------------------------
    # Les pages ne contiennent aucune donnée : le JavaScript les demande ensuite à
    # l'API, qui applique les droits. La vérification ici sert à ne pas montrer
    # une page à quelqu'un qui n'y a pas droit.

    @app.get("/")
    def accueil():
        return redirect("/carte" if g.utilisateur else "/connexion")

    @app.get("/connexion")
    def page_connexion():
        if g.utilisateur:                     # déjà connecté
            return redirect("/carte")
        return render_template("connexion.html")

    @app.get("/inscription")
    def page_inscription():
        if g.utilisateur is None:
            return redirect("/connexion")
        if g.utilisateur["role"] != "gestionnaire":
            log.warning("accès refusé : %s (conducteur) -> page /inscription", g.utilisateur["email"])
            return redirect("/carte")
        return render_template("inscription.html")

    @app.get("/carte")
    def page_carte():
        if g.utilisateur is None:
            return redirect("/connexion")
        return render_template("carte.html")

    # -- erreurs et en-têtes de sécurité ---------------------------------------------------

    @app.errorhandler(ErreurApi)
    def erreur_api(e):
        return jsonify({"erreur": e.message}), e.statut

    @app.errorhandler(HTTPException)
    def erreur_http(e):
        messages = {404: "adresse inconnue", 405: "méthode non autorisée", 413: "requête trop volumineuse"}
        return jsonify({"erreur": messages.get(e.code, e.name)}), e.code

    @app.errorhandler(Exception)
    def erreur_interne(e):
        # le détail va dans les logs, jamais dans la réponse
        log.exception("erreur interne sur %s %s", request.method, request.path)
        return jsonify({"erreur": "erreur interne"}), 500

    @app.after_request
    def entetes_securite(reponse):
        reponse.headers["X-Content-Type-Options"] = "nosniff"
        reponse.headers["X-Frame-Options"] = "DENY"
        reponse.headers["Referrer-Policy"] = "no-referrer"
        reponse.headers["Cache-Control"] = "no-store"
        if reponse.mimetype == "text/html":
            # Pages : uniquement nos propres fichiers. Pas de script ni de style écrit
            # dans le HTML (inline) : un code injecté dans la page ne s'exécuterait pas.
            # (P2 : ajouter les tuiles OpenStreetMap dans img-src pour la carte.)
            reponse.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        else:
            reponse.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return reponse

    return app


# ---------------------------------------------------------------------------

def charger_config(chemin):
    conf = copy.deepcopy(DEFAUTS)
    try:
        with open(chemin, encoding="utf-8") as f:
            brut = json.load(f)
    except FileNotFoundError:
        return conf
    except json.JSONDecodeError as e:
        raise ValueError(f"{chemin} n'est pas un JSON valide (ligne {e.lineno} : {e.msg})") from None
    for section, valeurs in brut.items():
        if section not in conf or not isinstance(valeurs, dict) or set(valeurs) - set(conf[section]):
            raise ValueError(f"{chemin} : section ou clé inconnue dans « {section} »")
        conf[section].update(valeurs)
    return conf


def main():
    parser = argparse.ArgumentParser(description="API web du suivi de flotte (phase 4)")
    parser.add_argument("-c", "--config", default="web/web_config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    try:
        conf = charger_config(args.config)
        app = creer_app(conf)
    except (ValueError, ErreurBase) as e:
        log.error("%s", e)
        return 2
    e = conf["ecoute"]
    log.info("API web sur http://%s:%d (base %s, compte %s)", e["hote"], e["port"],
             conf["bdd"]["base"], conf["bdd"]["utilisateur"])
    app.run(host=e["hote"], port=int(e["port"]), debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
