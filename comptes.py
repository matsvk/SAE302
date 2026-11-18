"""
comptes.py - Phase 4 P1 : inscription des utilisateurs du site, mot de passe haché avec bcrypt.

Le mot de passe n'est jamais enregistré : seul son hash bcrypt va dans la table
utilisateurs (colonne mot_de_passe, CHAR(60), voir sql/02_schema.sql). bcrypt :
  - ajoute un sel aléatoire à chaque hash : deux comptes avec le même mot de
    passe n'ont pas le même hash, et les tables de hash précalculées ne servent à rien ;
  - est volontairement lent (coût 12 = 2^12 tours, environ 0,2 s) : si la base
    est volée, tester des millions de mots de passe devient très long.

Règles de l'inscription :
  - email : format simple vérifié, 254 caractères max, enregistré en minuscules ;
  - mot de passe : 12 caractères minimum, 72 octets maximum (bcrypt ignore la suite) ;
  - rôle "conducteur" : rattaché à un camion qui existe dans la table camions ;
  - rôle "gestionnaire" : rattaché à aucun camion (il voit toute la flotte).
La base revérifie tout ça (CHECK et clé étrangère) : dernière barrière si le code change.

Sécurité : le site web (Phase 5) n'appellera inscrire() qu'avec role="conducteur".
Un compte gestionnaire se crée seulement en ligne de commande, sur le serveur :
sinon n'importe qui pourrait s'inscrire gestionnaire et voir toute la flotte.

Toutes les requêtes sont paramétrées (pas d'injection SQL possible).

Usage (dans le dossier du projet) :
    python comptes.py -c serveur_config.local.json inscrire conducteur jean@exemple.fr --camion CAM-01
    python comptes.py -c serveur_config.local.json inscrire gestionnaire chef@exemple.fr
    python comptes.py -c serveur_config.local.json verifier jean@exemple.fr
Le mot de passe est demandé au clavier (il ne s'affiche pas et ne reste pas
dans l'historique du terminal).
"""

import argparse
import getpass
import json
import re
import sys

try:
    import bcrypt
except ImportError:
    bcrypt = None

from base import DEFAUTS_BDD, ERREURS_CONNEXION, ErreurBase, _resume, lire_secret, pymysql

COUT_BCRYPT = 12          # 2^12 tours ; chaque +1 double le temps de calcul
LONGUEUR_MIN = 12         # caractères
OCTETS_MAX = 72           # limite de bcrypt : au-delà, les octets sont ignorés
ROLES = ("gestionnaire", "conducteur")
FORMAT_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

CODE_DOUBLON = 1062       # email déjà utilisé (clé unique uq_utilisateurs_email)
CODE_CLE_ETRANGERE = 1452 # camion absent de la table camions

# Hash d'un mot de passe quelconque : sert quand l'email n'existe pas, pour que
# la vérification prenne le même temps (sinon on devinerait quels emails existent)
_HASH_FACTICE = None


class ErreurInscription(ValueError):
    """Inscription refusée : le message peut être montré à l'utilisateur."""


# ---------------------------------------------------------------------------
# Vérifications et hachage
# ---------------------------------------------------------------------------

def normaliser_email(email):
    email = email.strip().lower()
    if len(email) > 254 or not FORMAT_EMAIL.fullmatch(email):
        raise ErreurInscription("adresse e-mail invalide")
    return email


def verifier_regles_mot_de_passe(mdp, email):
    if len(mdp) < LONGUEUR_MIN:
        raise ErreurInscription(f"mot de passe trop court ({LONGUEUR_MIN} caractères minimum)")
    if len(mdp.encode("utf-8")) > OCTETS_MAX:
        raise ErreurInscription(f"mot de passe trop long ({OCTETS_MAX} octets maximum)")
    if mdp.strip().lower() == email:
        raise ErreurInscription("le mot de passe ne doit pas être l'adresse e-mail")


def hacher(mdp):
    """Mot de passe -> hash bcrypt de 60 caractères ($2b$12$...)."""
    if bcrypt is None:
        raise ErreurBase("bcrypt n'est pas installé (pip install bcrypt)")
    return bcrypt.hashpw(mdp.encode("utf-8"), bcrypt.gensalt(rounds=COUT_BCRYPT)).decode("ascii")


def mot_de_passe_correct(mdp, hash_enregistre):
    if bcrypt is None:
        raise ErreurBase("bcrypt n'est pas installé (pip install bcrypt)")
    return bcrypt.checkpw(mdp.encode("utf-8"), hash_enregistre.encode("ascii"))


# ---------------------------------------------------------------------------
# Accès à la base
# ---------------------------------------------------------------------------

def connecter(conf):
    """Connexion courte à MariaDB. conf = section "bdd" d'un fichier de config.
    conf["cle_secret"] choisit le mot de passe lu dans .bdd_secret : par défaut
    FLOTTE_BDD_MDP ; le site web sur la VM utilisera FLOTTE_WEB_MDP (compte flotte_web)."""
    if pymysql is None:
        raise ErreurBase("PyMySQL n'est pas installé (pip install pymysql)")
    try:
        return pymysql.connect(
            host=conf["hote"], port=int(conf["port"]), user=conf["utilisateur"],
            password=lire_secret(conf, conf.get("cle_secret", "FLOTTE_BDD_MDP")),
            database=conf["base"], charset="utf8mb4",
            connect_timeout=5, read_timeout=10, write_timeout=10, autocommit=False,
        )
    except ERREURS_CONNEXION as e:
        raise ErreurBase(_resume(e)) from None


def inscrire(conf, email, mdp, role="conducteur", camion_id=None):
    """Crée un compte. Renvoie son id. Lève ErreurInscription si la demande est
    refusée, ErreurBase si la base est inaccessible."""
    email = normaliser_email(email)
    if role not in ROLES:
        raise ErreurInscription(f"rôle inconnu : {role}")
    if role == "conducteur" and not camion_id:
        raise ErreurInscription("un conducteur doit être rattaché à un camion")
    if role == "gestionnaire" and camion_id:
        raise ErreurInscription("un gestionnaire n'est rattaché à aucun camion")
    verifier_regles_mot_de_passe(mdp, email)

    hash_mdp = hacher(mdp)            # avant la connexion : le calcul prend ~0,2 s
    cnx = connecter(conf)
    try:
        with cnx.cursor() as cur:
            cur.execute(
                "INSERT INTO utilisateurs (email, mot_de_passe, role, camion_id) "
                "VALUES (%s, %s, %s, %s)",
                (email, hash_mdp, role, camion_id),
            )
            nouvel_id = cur.lastrowid
        cnx.commit()
        return nouvel_id
    except pymysql.err.IntegrityError as e:
        cnx.rollback()
        if e.args[0] == CODE_DOUBLON:
            raise ErreurInscription("un compte existe déjà avec cette adresse e-mail") from None
        if e.args[0] == CODE_CLE_ETRANGERE:
            raise ErreurInscription(f"camion inconnu : {camion_id}") from None
        raise ErreurInscription(f"inscription refusée par la base ({_resume(e)})") from None
    except ERREURS_CONNEXION as e:
        cnx.rollback()
        raise ErreurBase(_resume(e)) from None
    finally:
        cnx.close()


def verifier(conf, email, mdp):
    """Renvoie le compte (dict sans le hash) si email + mot de passe sont bons,
    sinon None. Sert de test ici ; la connexion au site (P2) s'appuiera dessus."""
    global _HASH_FACTICE
    try:
        email = normaliser_email(email)
    except ErreurInscription:
        return None
    cnx = connecter(conf)
    try:
        with cnx.cursor() as cur:
            cur.execute("SELECT id, email, mot_de_passe, role, camion_id FROM utilisateurs "
                        "WHERE email = %s", (email,))
            ligne = cur.fetchone()
        cnx.commit()
    except ERREURS_CONNEXION as e:
        raise ErreurBase(_resume(e)) from None
    finally:
        cnx.close()

    if ligne is None:
        if _HASH_FACTICE is None:
            _HASH_FACTICE = hacher("mot-de-passe-factice")
        mot_de_passe_correct(mdp, _HASH_FACTICE)   # même durée qu'un vrai compte
        return None
    id_, email, hash_mdp, role, camion_id = ligne
    if not mot_de_passe_correct(mdp, hash_mdp):
        return None
    return {"id": id_, "email": email, "role": role, "camion_id": camion_id}


# ---------------------------------------------------------------------------
# Ligne de commande (tests, création du premier gestionnaire)
# ---------------------------------------------------------------------------

def demander_mot_de_passe():
    mdp = getpass.getpass("Mot de passe : ")
    if getpass.getpass("Confirmer le mot de passe : ") != mdp:
        raise ErreurInscription("les deux mots de passe sont différents")
    return mdp


def main():
    parser = argparse.ArgumentParser(description="Comptes du site (inscription, bcrypt)")
    parser.add_argument("-c", "--config", default="serveur_config.json")
    sous = parser.add_subparsers(dest="commande", required=True)

    p_ins = sous.add_parser("inscrire", help="crée un compte")
    p_ins.add_argument("role", choices=ROLES)
    p_ins.add_argument("email")
    p_ins.add_argument("--camion", help="id du camion (obligatoire pour un conducteur)")

    p_ver = sous.add_parser("verifier", help="teste un email + mot de passe")
    p_ver.add_argument("email")
    args = parser.parse_args()

    try:
        with open(args.config, encoding="utf-8") as f:
            conf = {**DEFAUTS_BDD, **json.load(f).get("bdd", {})}
    except (OSError, ValueError) as e:
        print(f"configuration illisible : {e}", file=sys.stderr)
        return 2

    try:
        if args.commande == "inscrire":
            nouvel_id = inscrire(conf, args.email, demander_mot_de_passe(), args.role, args.camion)
            print(f"compte créé : id {nouvel_id}, {args.email.strip().lower()} ({args.role})")
        else:
            compte = verifier(conf, args.email, getpass.getpass("Mot de passe : "))
            if compte:
                print(f"mot de passe correct : {compte}")
            else:
                print("email ou mot de passe incorrect")
                return 1
    except ErreurInscription as e:
        print(f"inscription refusée : {e}", file=sys.stderr)
        return 1
    except ErreurBase as e:
        print(f"base inaccessible : {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
