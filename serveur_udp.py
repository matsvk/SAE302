"""
serveur.py - Phase 2 : serveur de réception des positions des camions.

P1 : recevoir les messages UDP et lire le JSON
P3 : gérer plusieurs camions en même temps et ajouter des logs

Fonctionnement :
  - un thread "reception" lit les datagrammes UDP et les met dans une file ;
  - un thread "traitement" lit le JSON et met à jour l'état de chaque camion ;
  - un thread "surveillance" repère les camions qui n'envoient plus rien
    et affiche un bilan régulièrement.
Comme ça, la réception n'est jamais bloquée par le traitement, même avec
beaucoup de camions qui envoient en même temps.

Tous les événements sont écrits dans la console et dans logs/serveur.log.

Usage :
    python serveur.py
"""

import json
import logging
import os
import queue
import socket
import threading
import time
from logging.handlers import RotatingFileHandler

HOST = "0.0.0.0"
PORT = 5005
TAILLE_MAX = 2048          # taille max d'un datagramme lu (octets)

DELAI_SILENCE = 30         # secondes sans message avant de signaler un camion
INTERVALLE_BILAN = 60      # secondes entre deux bilans dans les logs

DOSSIER_LOGS = "logs"
FICHIER_LOG = os.path.join(DOSSIER_LOGS, "serveur.log")


# ---------------------------------------------------------------------------
# Logs : console + fichier (5 Mo max, 3 anciens fichiers gardés)
# ---------------------------------------------------------------------------

def configurer_logs():
    os.makedirs(DOSSIER_LOGS, exist_ok=True)
    format_log = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(format_log)

    fichier = RotatingFileHandler(FICHIER_LOG, maxBytes=5 * 1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    fichier.setFormatter(format_log)

    log = logging.getLogger("serveur")
    log.setLevel(logging.INFO)
    log.addHandler(console)
    log.addHandler(fichier)
    return log


log = configurer_logs()


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
verrou = threading.Lock()       # protège "camions" (utilisé par plusieurs threads)
file_messages = queue.Queue()   # datagrammes reçus, en attente de traitement
arret = threading.Event()       # mis à True pour arrêter tous les threads
stats = {"recus": 0, "non_json": 0, "sans_id": 0}


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
        file_messages.put((data, addr))


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
    stats["recus"] += 1

    # --- lecture du JSON (P1)
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        stats["non_json"] += 1
        log.warning("message non-JSON reçu de %s:%d (%d octets)", addr[0], addr[1], len(data))
        return
    if not isinstance(message, dict) or "id" not in message:
        stats["sans_id"] += 1
        log.warning("message sans identifiant de camion reçu de %s:%d", addr[0], addr[1])
        return
    # La vérification complète des champs et les codes d'erreur sont faits en P2.

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
    prochain_bilan = time.monotonic() + INTERVALLE_BILAN
    while not arret.wait(1):
        maintenant = time.monotonic()
        with verrou:
            for camion in camions.values():
                if not camion.silencieux and maintenant - camion.derniere_reception > DELAI_SILENCE:
                    camion.silencieux = True
                    log.warning("[%s] aucun message depuis %d s", camion.id, DELAI_SILENCE)
        if maintenant >= prochain_bilan:
            bilan()
            prochain_bilan += INTERVALLE_BILAN


def bilan():
    with verrou:
        actifs = sum(1 for c in camions.values() if not c.silencieux)
        log.info("bilan : %d message(s) reçu(s), %d non-JSON, %d sans id, %d camion(s) dont %d actif(s)",
                 stats["recus"], stats["non_json"], stats["sans_id"], len(camions), actifs)
        for c in sorted(camions.values(), key=lambda c: c.id):
            log.info("  %-10s %5d msg  %3d perdu(s)  %3d ignoré(s)  %s",
                     c.id, c.nb_messages, c.perdus, c.ignores,
                     "silencieux" if c.silencieux else "actif")


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((HOST, PORT))
    except OSError as e:
        log.error("impossible d'écouter sur le port %d : %s (serveur déjà lancé ?)", PORT, e)
        return 1
    sock.settimeout(1)

    threads = [
        threading.Thread(target=reception, args=(sock,), name="reception", daemon=True),
        threading.Thread(target=traitement, name="traitement", daemon=True),
        threading.Thread(target=surveillance, name="surveillance", daemon=True),
    ]
    for t in threads:
        t.start()

    log.info("serveur UDP démarré sur %s:%d, logs dans %s", HOST, PORT, FICHIER_LOG)

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
    raise SystemExit(main())
