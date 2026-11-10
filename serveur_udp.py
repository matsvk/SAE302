"""
serveur_udp.py - Phase 2 : serveur de réception des positions des camions.

P1 : recevoir les messages UDP et lire le JSON
P3 : gérer plusieurs camions en même temps et ajouter des logs

Fonctionnement :
  - un thread "reception" lit les datagrammes UDP et les met dans une file ;
  - un thread "traitement" lit le JSON et met à jour l'état de chaque camion ;
  - un thread "surveillance" repère les camions qui n'envoient plus rien
    et affiche un bilan régulièrement.
Comme ça, la réception n'est jamais bloquée par le traitement, même avec
beaucoup de camions qui envoient en même temps.

Les réglages sont lus dans serveur_config.json (valeurs par défaut si le
fichier n'existe pas). Tous les événements sont écrits dans la console et
dans le fichier de logs.

Usage :
    python serveur_udp.py
    python serveur_udp.py -c autre_config.json
"""

import argparse
import copy
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

TAILLE_MAX = 2048          # taille max d'un datagramme lu (octets)

# Valeurs utilisées si une clé est absente de serveur_config.json
DEFAUTS = {
    "ecoute": {"hote": "0.0.0.0", "port": 5005},
    "delai_silence": 30,           # secondes sans message avant de signaler un camion
    "intervalle_stats": 60,        # secondes entre deux bilans (0 = pas de bilan)
    "taille_file": 10000,          # messages max en attente de traitement
    "logs": {"fichier": "logs/serveur.log", "niveau": "INFO",
             "taille_max_mo": 5, "nb_fichiers": 3},
    # Clés réservées à la phase 2 P2 (validation et codes d'erreur) :
    # lues et vérifiées ici, mais pas encore utilisées par le serveur.
    "camions_autorises": [],
    "reponses_erreur": True,
    "max_reponses_par_seconde": 5,
    "vitesse_max_plausible": 200,
}

log = logging.getLogger("serveur")
conf = {}                       # configuration chargée au démarrage (voir main)


class ErreurConfig(Exception):
    pass


# ---------------------------------------------------------------------------
# Lecture de la configuration
# ---------------------------------------------------------------------------

def _nombre(valeur, nom, mini, maxi):
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
        raise ErreurConfig(f"« {nom} » doit être un nombre")
    if not mini <= valeur <= maxi:
        raise ErreurConfig(f"« {nom} » doit être entre {mini} et {maxi} (valeur : {valeur})")


def charger_config(chemin):
    resultat = copy.deepcopy(DEFAUTS)
    try:
        with open(chemin, encoding="utf-8") as f:
            brut = json.load(f)
    except FileNotFoundError:
        print(f"{chemin} introuvable, valeurs par défaut utilisées", file=sys.stderr)
        return resultat
    except json.JSONDecodeError as e:
        raise ErreurConfig(f"{chemin} n'est pas un JSON valide (ligne {e.lineno} : {e.msg})") from None
    if not isinstance(brut, dict):
        raise ErreurConfig(f"{chemin} doit contenir un objet JSON")

    # On refuse les clés inconnues : une faute de frappe ne doit pas passer en silence
    for cle, valeur in brut.items():
        if cle not in DEFAUTS:
            raise ErreurConfig(f"clé inconnue « {cle} »")
        if isinstance(DEFAUTS[cle], dict):
            if not isinstance(valeur, dict):
                raise ErreurConfig(f"« {cle} » doit être un objet")
            inconnues = set(valeur) - set(DEFAUTS[cle])
            if inconnues:
                raise ErreurConfig(f"clé(s) inconnue(s) dans « {cle} » : {sorted(inconnues)}")
            resultat[cle].update(valeur)
        else:
            resultat[cle] = valeur

    # Vérification des valeurs
    if not isinstance(resultat["ecoute"]["hote"], str):
        raise ErreurConfig("« ecoute.hote » doit être une adresse IP (texte)")
    _nombre(resultat["ecoute"]["port"], "ecoute.port", 1, 65535)
    _nombre(resultat["delai_silence"], "delai_silence", 1, 3600)
    _nombre(resultat["intervalle_stats"], "intervalle_stats", 0, 86400)
    _nombre(resultat["taille_file"], "taille_file", 1, 1_000_000)
    _nombre(resultat["logs"]["taille_max_mo"], "logs.taille_max_mo", 1, 1000)
    _nombre(resultat["logs"]["nb_fichiers"], "logs.nb_fichiers", 0, 100)
    if resultat["logs"]["niveau"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ErreurConfig("« logs.niveau » doit valoir DEBUG, INFO, WARNING ou ERROR")
    if not isinstance(resultat["camions_autorises"], list):
        raise ErreurConfig("« camions_autorises » doit être une liste")
    if not isinstance(resultat["reponses_erreur"], bool):
        raise ErreurConfig("« reponses_erreur » doit valoir true ou false")
    _nombre(resultat["max_reponses_par_seconde"], "max_reponses_par_seconde", 0, 1000)
    _nombre(resultat["vitesse_max_plausible"], "vitesse_max_plausible", 1, 500)
    return resultat


# ---------------------------------------------------------------------------
# Logs : console + fichier avec rotation
# ---------------------------------------------------------------------------

def configurer_logs(c):
    dossier = os.path.dirname(c["fichier"])
    if dossier:
        os.makedirs(dossier, exist_ok=True)
    format_log = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(format_log)

    fichier = RotatingFileHandler(c["fichier"], maxBytes=int(c["taille_max_mo"] * 1024 * 1024),
                                  backupCount=int(c["nb_fichiers"]), encoding="utf-8")
    fichier.setFormatter(format_log)

    log.setLevel(c["niveau"])
    log.addHandler(console)
    log.addHandler(fichier)


# ---------------------------------------------------------------------------
# État d'un camion
# ---------------------------------------------------------------------------

class Camion:
    """Ce que le serveur sait d'un camion."""

    def __init__(self, identifiant, adresse):
        self.id = identifiant
        self.adresse = adresse              # (ip, port) du dernier message
        self.nb_messages = 0
        self.dernier_seq = None
        self.perdus = 0                      # messages jamais arrivés (trous dans seq)
        self.ignores = 0                     # doublons ou messages arrivés en retard
        self.derniere_reception = time.monotonic()
        self.derniere_position = None        # (lat, lon, horodatage)
        self.silencieux = False


camions = {}                    # id du camion -> Camion
verrou = threading.Lock()       # protège "camions" et "stats" (utilisés par plusieurs threads)
file_messages = None            # file d'attente des datagrammes, créée dans main()
arret = threading.Event()       # mis à True pour arrêter tous les threads
stats = {"recus": 0, "non_json": 0, "sans_id": 0, "file_pleine": 0}


# ---------------------------------------------------------------------------
# Thread 1 : réception UDP (P1)
# ---------------------------------------------------------------------------

def reception(sock):
    while not arret.is_set():
        try:
            data, addr = sock.recvfrom(TAILLE_MAX)
        except socket.timeout:
            continue                 # permet de vérifier "arret" chaque seconde
        except OSError as e:
            if not arret.is_set():
                log.error("erreur de réception : %s", e)
            continue
        try:
            file_messages.put_nowait((data, addr))
        except queue.Full:
            # serveur débordé : on jette le message plutôt que de bloquer la réception
            with verrou:
                stats["file_pleine"] += 1
                premier = stats["file_pleine"] == 1
            if premier:
                log.warning("file pleine (%d messages en attente) : messages abandonnés",
                            conf["taille_file"])


# ---------------------------------------------------------------------------
# Thread 2 : lecture du JSON et suivi des camions (P1 + P3)
# ---------------------------------------------------------------------------

def traitement():
    while not arret.is_set():
        try:
            data, addr = file_messages.get(timeout=1)
        except queue.Empty:
            continue
        try:
            traiter_message(data, addr)
        except Exception:
            # un message bizarre ne doit jamais arrêter le serveur
            log.exception("erreur pendant le traitement d'un message de %s", addr)
        finally:
            file_messages.task_done()


def traiter_message(data, addr):
    with verrou:
        stats["recus"] += 1

    # --- lecture du JSON (P1)
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        with verrou:
            stats["non_json"] += 1
        log.warning("message non-JSON reçu de %s:%d (%d octets)", addr[0], addr[1], len(data))
        return
    if not isinstance(message, dict) or "id" not in message:
        with verrou:
            stats["sans_id"] += 1
        log.warning("message sans identifiant de camion reçu de %s:%d", addr[0], addr[1])
        return
    # La vérification complète des champs, des camions autorisés et les codes
    # d'erreur sont faits en P2 (clés camions_autorises, reponses_erreur...).

    id_camion = str(message["id"])
    seq = message.get("seq")

    with verrou:
        # --- nouveau camion
        camion = camions.get(id_camion)
        if camion is None:
            camion = Camion(id_camion, addr)
            camions[id_camion] = camion
            log.info("[%s] nouveau camion depuis %s:%d", id_camion, addr[0], addr[1])
        elif camion.adresse != addr:
            log.info("[%s] change d'adresse : %s:%d -> %s:%d", id_camion,
                     camion.adresse[0], camion.adresse[1], addr[0], addr[1])
            camion.adresse = addr

        # --- camion qui revient après un silence
        camion.derniere_reception = time.monotonic()
        if camion.silencieux:
            camion.silencieux = False
            log.info("[%s] de nouveau en ligne", id_camion)

        # --- numéro de séquence : UDP peut perdre, dupliquer ou désordonner
        if isinstance(seq, int) and camion.dernier_seq is not None:
            attendu = camion.dernier_seq + 1
            if seq == 0 and camion.dernier_seq > 0:
                log.info("[%s] seq repart à 0 : camion redémarré", id_camion)
            elif seq < attendu:
                camion.ignores += 1
                log.warning("[%s] message ignoré (seq %d déjà reçu ou en retard, dernier = %d)",
                            id_camion, seq, camion.dernier_seq)
                return
            elif seq > attendu:
                manquants = seq - attendu
                camion.perdus += manquants
                log.warning("[%s] %d message(s) perdu(s) (seq %d à %d)",
                            id_camion, manquants, attendu, seq - 1)
        if isinstance(seq, int):
            camion.dernier_seq = seq

        # --- enregistrement de la position
        camion.nb_messages += 1
        camion.derniere_position = (message.get("lat"), message.get("lon"), message.get("horodatage"))

    log.info("[%s] seq=%s lat=%s lon=%s vitesse=%s etat=%s", id_camion, seq,
             message.get("lat"), message.get("lon"), message.get("vitesse"), message.get("etat"))


# ---------------------------------------------------------------------------
# Thread 3 : surveillance et bilans (P3)
# ---------------------------------------------------------------------------

def surveillance():
    delai = conf["delai_silence"]
    intervalle = conf["intervalle_stats"]
    prochain_bilan = time.monotonic() + intervalle
    while not arret.wait(1):
        maintenant = time.monotonic()
        with verrou:
            for camion in camions.values():
                if not camion.silencieux and maintenant - camion.derniere_reception > delai:
                    camion.silencieux = True
                    log.warning("[%s] aucun message depuis %g s", camion.id, delai)
        if intervalle and maintenant >= prochain_bilan:
            bilan()
            prochain_bilan += intervalle


def bilan():
    with verrou:
        actifs = sum(1 for c in camions.values() if not c.silencieux)
        log.info("bilan : %d message(s) reçu(s), %d non-JSON, %d sans id, %d abandonné(s) (file pleine), "
                 "%d camion(s) dont %d actif(s)", stats["recus"], stats["non_json"], stats["sans_id"],
                 stats["file_pleine"], len(camions), actifs)
        for c in sorted(camions.values(), key=lambda c: c.id):
            log.info("  %-10s %5d msg  %3d perdu(s)  %3d ignoré(s)  %s",
                     c.id, c.nb_messages, c.perdus, c.ignores,
                     "silencieux" if c.silencieux else "actif")


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def main():
    global conf, file_messages

    parser = argparse.ArgumentParser(description="Serveur UDP du suivi de flotte (SAÉ 3.02)")
    parser.add_argument("-c", "--config", default="serveur_config.json",
                        help="fichier de configuration (défaut : serveur_config.json)")
    args = parser.parse_args()

    try:
        conf = charger_config(args.config)
    except ErreurConfig as e:
        print(f"Erreur de configuration : {e}", file=sys.stderr)
        return 2
    configurer_logs(conf["logs"])
    file_messages = queue.Queue(maxsize=int(conf["taille_file"]))

    hote, port = conf["ecoute"]["hote"], int(conf["ecoute"]["port"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((hote, port))
    except OSError as e:
        log.error("impossible d'écouter sur %s:%d : %s (serveur déjà lancé ?)", hote, port, e)
        return 1
    sock.settimeout(1)

    threads = [
        threading.Thread(target=reception, args=(sock,), name="reception", daemon=True),
        threading.Thread(target=traitement, name="traitement", daemon=True),
        threading.Thread(target=surveillance, name="surveillance", daemon=True),
    ]
    for t in threads:
        t.start()

    log.info("serveur UDP démarré sur %s:%d (config : %s, logs : %s)",
             hote, port, args.config, conf["logs"]["fichier"])

    try:
        while all(t.is_alive() for t in threads):
            time.sleep(1)
        log.error("un thread s'est arrêté, arrêt du serveur")
    except KeyboardInterrupt:
        log.info("Ctrl+C, arrêt du serveur...")
    finally:
        arret.set()
        for t in threads:
            t.join(timeout=2)
        sock.close()
        bilan()
    return 0


if __name__ == "__main__":
    sys.exit(main())