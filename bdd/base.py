"""
base.py - Phase 3 : accès à MariaDB pour le serveur.

    P3 : insérer les positions reçues
    P4 : requête "dernière position par camion" + base indisponible

Bibliothèque utilisée : PyMySQL (100 % Python, compatible MariaDB).
    Ubuntu :  sudo apt install python3-pymysql
    ailleurs : pip install pymysql

Toutes les requêtes sont PARAMÉTRÉES (les valeurs passent à part, jamais
collées dans le texte SQL) : c'est ce qui empêche les injections SQL.

Gestion de la base indisponible : le serveur ne doit jamais s'arrêter ni
bloquer parce que MariaDB est en panne. Les positions passent donc par
EcrivainPositions, un thread qui :
  - garde les positions en mémoire en attendant de pouvoir les écrire
    (tampon limité : au-delà, les plus anciennes sont abandonnées et comptées) ;
  - écrit par paquets (beaucoup plus rapide qu'une requête par position) ;
  - si la base ne répond plus, réessaie de plus en plus espacé (1, 2, 4... 30 s)
    puis écrit tout le retard dès qu'elle revient.

Test rapide (affiche la dernière position de chaque camion), depuis la racine du projet :
    python3 -m bdd.base
"""

import argparse
import collections
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

try:
    import pymysql
except ImportError:          # le serveur peut tourner sans base (bdd.active = false)
    pymysql = None

log = logging.getLogger("base")

DEFAUTS_BDD = {
    "active": False,
    "hote": "127.0.0.1",
    "port": 3306,
    "base": "flotte",
    "utilisateur": "flotte_serveur",
    "fichier_secret": ".bdd_secret",     # créé par bdd/creer_bdd.sh, à la racine du projet
    "taille_tampon": 50000,              # positions gardées en mémoire si la base est en panne
    "taille_paquet": 200,                # positions écrites par requête
    "rafraichir_camions": 60,            # s entre deux relectures de la table camions
}

REQUETE_INSERTION = (
    "INSERT IGNORE INTO positions "
    "(camion_id, seq, horodatage, lat, lon, vitesse, cap, etat, recu_le, ip_source) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
# INSERT IGNORE + clé unique (camion_id, horodatage, seq) : si un paquet est
# renvoyé après une coupure, les positions déjà enregistrées sont ignorées
# au lieu de créer des doublons. Attention : avec IGNORE, MariaDB ne bloque pas
# non plus les autres erreurs (clé étrangère, CHECK...), il les transforme en
# avertissements. On lit donc ces avertissements (SHOW WARNINGS) pour séparer
# les vrais doublons (code 1062) des positions refusées.
CODE_DOUBLON = 1062

# Même requête que la vue v_derniere_position (bdd/sql/02_schema.sql), écrite ici
# pour pouvoir l'afficher et l'expliquer dans le rapport.
REQUETE_DERNIERE_POSITION = """
SELECT c.id AS camion_id, c.immatriculation, c.actif,
       p.seq, p.horodatage, p.lat, p.lon, p.vitesse, p.cap, p.etat, p.recu_le
FROM camions c
LEFT JOIN positions p ON p.id = (
    SELECT p2.id FROM positions p2
    WHERE p2.camion_id = c.id
    ORDER BY p2.horodatage DESC, p2.seq DESC
    LIMIT 1
)
ORDER BY c.id
""".strip()

# Erreurs qui empêchent d'écrire pour l'instant : base arrêtée, connexion perdue,
# mot de passe refusé, table absente ou droits manquants. On réessaiera plus tard
# (le temps de redémarrer MariaDB ou de relancer creer_bdd.sh).
if pymysql:
    ERREURS_CONNEXION = (pymysql.err.OperationalError, pymysql.err.InterfaceError,
                         pymysql.err.InternalError, pymysql.err.ProgrammingError, OSError)
else:
    ERREURS_CONNEXION = ()


class ErreurBase(Exception):
    pass


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------

def lire_secret(conf, cle="FLOTTE_BDD_MDP"):
    """Lit un secret (mot de passe SQL, clé de session) : variable d'environnement
    du même nom en priorité, sinon le fichier secret créé par creer_bdd.sh.
    Les secrets ne sont jamais écrits dans les fichiers de config (qui vont sur GitHub)."""
    valeur = os.environ.get(cle)
    if valeur:
        return valeur
    chemin = conf["fichier_secret"]
    try:
        with open(chemin, encoding="utf-8") as f:
            for ligne in f:
                nom, _, valeur = ligne.strip().partition("=")
                if nom == cle and valeur:
                    return valeur
    except FileNotFoundError:
        raise ErreurBase(f"secret {cle} introuvable : ni variable d'environnement, ni fichier {chemin} "
                         f"(lancer d'abord : sudo bash creer_bdd.sh)") from None
    except OSError as e:
        raise ErreurBase(f"impossible de lire {chemin} : {e}") from None
    raise ErreurBase(f"{chemin} ne contient pas de ligne {cle}=... (relancer : sudo bash creer_bdd.sh)")


def lire_mot_de_passe(conf):
    return lire_secret(conf, "FLOTTE_BDD_MDP")


class Base:
    """Une connexion à MariaDB. Pas partageable entre threads : chaque thread a la sienne."""

    def __init__(self, conf):
        if pymysql is None:
            raise ErreurBase("PyMySQL n'est pas installé (sudo apt install python3-pymysql)")
        self.conf = conf
        self.cnx = None

    @property
    def connectee(self):
        return self.cnx is not None

    def connecter(self):
        self.fermer()
        self.cnx = pymysql.connect(
            host=self.conf["hote"], port=int(self.conf["port"]),
            user=self.conf["utilisateur"], password=lire_mot_de_passe(self.conf),
            database=self.conf["base"], charset="utf8mb4",
            connect_timeout=5, read_timeout=10, write_timeout=10,
            autocommit=False,
            # tout en UTC ; et garder tous les avertissements d'un paquet (64 par défaut)
            init_command="SET time_zone = '+00:00', max_error_count = 65535",
        )

    def fermer(self):
        if self.cnx is not None:
            try:
                self.cnx.close()
            except Exception:
                pass
            self.cnx = None

    def _curseur(self):
        if self.cnx is None:
            self.connecter()
        return self.cnx.cursor()

    # -- lectures --------------------------------------------------------------

    def camions_autorises(self):
        with self._curseur() as cur:
            cur.execute("SELECT id FROM camions WHERE actif = TRUE")
            ids = {ligne[0] for ligne in cur.fetchall()}
        self.cnx.commit()   # termine la transaction de lecture
        return ids

    def dernieres_positions(self):
        """Phase 3 P4 : liste de dicts, un par camion (positions vides si jamais reçu)."""
        with self._curseur() as cur:
            cur.execute(REQUETE_DERNIERE_POSITION)
            colonnes = [d[0] for d in cur.description]
            lignes = [dict(zip(colonnes, ligne)) for ligne in cur.fetchall()]
        self.cnx.commit()
        return lignes

    # -- écriture --------------------------------------------------------------

    def inserer_positions(self, lignes):
        """Insère un paquet de positions en une seule transaction.
        Renvoie (ajoutées, doublons, refusées, {message d'erreur: nombre})."""
        try:
            with self._curseur() as cur:
                cur.executemany(REQUETE_INSERTION, lignes)
                ajoutees = cur.rowcount
                doublons, refus = 0, {}
                if ajoutees < len(lignes):
                    cur.execute("SHOW WARNINGS")
                    for _niveau, code, message in cur.fetchall():
                        if code == CODE_DOUBLON:
                            doublons += 1
                        else:
                            cle = f"{code} {message}"[:150]
                            refus[cle] = refus.get(cle, 0) + 1
            self.cnx.commit()
            refusees = len(lignes) - ajoutees - doublons
            return ajoutees, doublons, refusees, refus
        except Exception:
            try:
                if self.cnx:
                    self.cnx.rollback()
            except Exception:
                pass
            raise


def ligne_position(msg, ip, recu_le):
    """Message validé (dict) -> tuple pour REQUETE_INSERTION.
    Les dates sont converties en datetime UTC sans fuseau (format DATETIME de MariaDB)."""
    horodatage = datetime.fromisoformat(msg["horodatage"].replace("Z", "+00:00"))
    horodatage = horodatage.astimezone(timezone.utc).replace(tzinfo=None)
    recu = datetime.fromtimestamp(recu_le, tz=timezone.utc).replace(tzinfo=None)
    return (msg["id"], msg["seq"], horodatage, msg["lat"], msg["lon"], msg["vitesse"],
            msg["cap"], msg["etat"], recu, ip)


# ---------------------------------------------------------------------------
# Écriture en tâche de fond, résistante aux pannes de la base
# ---------------------------------------------------------------------------

class EcrivainPositions(threading.Thread):

    ATTENTE_MAX = 30.0   # s entre deux tentatives de reconnexion, au maximum

    def __init__(self, conf, intervalle=1.0):
        super().__init__(name="bdd", daemon=True)
        self.base = Base(conf)
        self.taille_tampon = int(conf["taille_tampon"])
        self.taille_paquet = int(conf["taille_paquet"])
        self.intervalle = intervalle
        self.tampon = collections.deque()
        self.verrou = threading.Lock()
        self.arret = threading.Event()
        self.base_ok = None            # None = pas encore essayé
        self._attente = 1.0
        self._prochain_essai = 0.0
        self.stats = {"ecrites": 0, "doublons": 0, "abandonnees": 0, "rejetees": 0,
                      "coupures": 0, "max_en_attente": 0}

    # -- appelé par le thread de traitement : ne bloque jamais -------------------

    def ajouter(self, msg, ip, recu_le):
        ligne = ligne_position(msg, ip, recu_le)
        with self.verrou:
            if len(self.tampon) >= self.taille_tampon:
                self.tampon.popleft()           # on sacrifie la plus ancienne
                self.stats["abandonnees"] += 1
                n = self.stats["abandonnees"]
                if n == 1 or n % 1000 == 0:
                    log.error("tampon plein (%d positions) : %d position(s) ancienne(s) abandonnée(s)",
                              self.taille_tampon, n)
            self.tampon.append(ligne)
            self.stats["max_en_attente"] = max(self.stats["max_en_attente"], len(self.tampon))

    @property
    def en_attente(self):
        with self.verrou:
            return len(self.tampon)

    # -- thread ------------------------------------------------------------------

    def run(self):
        while not self.arret.wait(self.intervalle):
            try:
                self._vider()
            except Exception as e:      # imprévu : on ne laisse surtout pas mourir le thread
                log.exception("erreur inattendue pendant l'écriture en base")
                self._panne(e)
        # arrêt : dernière tentative pour ne rien perdre
        self._prochain_essai = 0
        self._vider()
        reste = self.en_attente
        if reste:
            log.error("arrêt avec %d position(s) non enregistrée(s) (base indisponible)", reste)
        self.base.fermer()

    def arreter(self):
        self.arret.set()
        self.join(timeout=15)

    def _vider(self):
        """Écrit le tampon par paquets. S'arrête à la première panne de la base."""
        while True:
            with self.verrou:
                paquet = [self.tampon[i] for i in range(min(self.taille_paquet, len(self.tampon)))]
            if not paquet:
                return
            if not self.base.connectee:
                if time.monotonic() < self._prochain_essai:
                    return
                if not self._connecter():
                    return
            try:
                ajoutees, doublons, refusees, refus = self.base.inserer_positions(paquet)
            except ERREURS_CONNEXION as e:
                self._panne(e)
                return
            for message, nombre in refus.items():
                # ex. camion absent de la table camions (clé étrangère), valeur hors CHECK
                log.error("%d position(s) refusée(s) par la base : %s", nombre, message)

            with self.verrou:
                for _ in paquet:
                    self.tampon.popleft()
                self.stats["ecrites"] += ajoutees
                self.stats["rejetees"] += refusees
                self.stats["doublons"] += doublons
            if self.base_ok is False:
                log.info("base de nouveau disponible : retard en cours d'écriture")
            self.base_ok = True
            self._attente = 1.0

    def _connecter(self):
        try:
            self.base.connecter()
            return True
        except ErreurBase as e:
            self._panne(e)
            return False
        except ERREURS_CONNEXION as e:
            self._panne(e)
            return False

    def _panne(self, erreur):
        self.base.fermer()
        if self.base_ok is not False:
            self.stats["coupures"] += 1
            log.error("base de données indisponible (%s) : positions gardées en mémoire, "
                      "nouvel essai dans %.0f s", _resume(erreur), self._attente)
        else:
            log.warning("base toujours indisponible (%s), %d position(s) en attente, "
                        "nouvel essai dans %.0f s", _resume(erreur), self.en_attente, self._attente)
        self.base_ok = False
        self._prochain_essai = time.monotonic() + self._attente
        self._attente = min(self.ATTENTE_MAX, self._attente * 2)


def _resume(erreur):
    """Message d'erreur court et lisible ((2003, "Can't connect...") -> "2003 Can't connect...")."""
    if getattr(erreur, "args", None) and len(erreur.args) == 2 and isinstance(erreur.args[0], int):
        return f"{erreur.args[0]} {erreur.args[1]}"[:120]
    return str(erreur)[:120]


def charger_camions(conf):
    """Lit la liste des camions actifs (connexion courte). Lève ErreurBase si impossible."""
    base = Base(conf)
    try:
        return base.camions_autorises()
    except ERREURS_CONNEXION as e:
        raise ErreurBase(_resume(e)) from None
    finally:
        base.fermer()


# ---------------------------------------------------------------------------
# Test en ligne de commande : dernière position par camion
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Affiche la dernière position de chaque camion")
    parser.add_argument("-c", "--config", default="serveur/serveur_config.json")
    parser.add_argument("--sql", action="store_true", help="affiche aussi la requête utilisée")
    args = parser.parse_args()

    try:
        with open(args.config, encoding="utf-8") as f:
            conf = {**DEFAUTS_BDD, **json.load(f).get("bdd", {})}
    except (OSError, ValueError) as e:
        print(f"configuration illisible : {e}", file=sys.stderr)
        return 2
    if args.sql:
        print(REQUETE_DERNIERE_POSITION, "\n")

    base = Base(conf) if pymysql else None
    try:
        if base is None:
            raise ErreurBase("PyMySQL n'est pas installé (sudo apt install python3-pymysql)")
        debut = time.perf_counter()
        lignes = base.dernieres_positions()
        duree = (time.perf_counter() - debut) * 1000
    except (ErreurBase, *ERREURS_CONNEXION) as e:
        print(f"base inaccessible : {_resume(e)}", file=sys.stderr)
        return 1
    finally:
        if base:
            base.fermer()

    print(f"{'camion':<8} {'immat.':<10} {'horodatage (UTC)':<24} {'lat':>10} {'lon':>10} "
          f"{'km/h':>6} {'cap':>6}  état")
    for l in lignes:
        if l["horodatage"] is None:
            print(f"{l['camion_id']:<8} {l['immatriculation'] or '':<10} (aucune position reçue)")
        else:
            print(f"{l['camion_id']:<8} {l['immatriculation'] or '':<10} "
                  f"{l['horodatage'].isoformat(sep=' ', timespec='milliseconds'):<24} "
                  f"{l['lat']:>10} {l['lon']:>10} {l['vitesse']:>6} {l['cap']:>6}  {l['etat']}")
    print(f"\n{len(lignes)} camion(s), requête exécutée en {duree:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
