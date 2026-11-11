"""
serveur_udp.py - Phase 2 : serveur de réception des positions des camions.

P1 : recevoir les messages UDP et lire le JSON
P2 : ne pas croire aveuglément ce qui arrive (validation, camions autorisés,
     cohérence des positions, codes d'erreur renvoyés à l'expéditeur)
P3 : gérer plusieurs camions en même temps et ajouter des logs

Fonctionnement :
  - un thread "reception" lit les datagrammes UDP et les met dans une file ;
  - un thread "traitement" vérifie chaque message et met à jour l'état de chaque camion ;
  - un thread "surveillance" repère les camions qui n'envoient plus rien
    et affiche un bilan régulièrement.
Comme ça, la réception n'est jamais bloquée par le traitement, même avec
beaucoup de camions qui envoient en même temps.

Les réglages sont lus dans serveur_config.json (valeurs par défaut si le
fichier n'existe pas). Tous les événements sont écrits dans la console et
dans le fichier de logs.

Vérifications P2, dans l'ordre (le premier problème trouvé arrête le traitement) :
  1. taille du datagramme (512 octets max)                  -> code "trop_long"
  2. UTF-8 + JSON lisible                                    -> code "json_illisible"
  3. format du message : envoi.valider(), le même code que
     les camions (champs, types, bornes, état, horodatage)   -> code "format_invalide"
  4. camion présent dans "camions_autorises" (liste vide =
     tous les camions acceptés)                              -> code "non_autorise"
  5. numéro de séquence : doublon ou message en retard -> ignoré (sans réponse :
     c'est un comportement normal d'UDP, pas une attaque)
  6. cohérence avec la position précédente : distance / temps écoulé
     (d'après les horodatages) > "vitesse_max_plausible"   -> code "position_incoherente"

Si "reponses_erreur" vaut true, le serveur renvoie à l'expéditeur un petit
datagramme JSON qui explique le refus, par exemple :
    {"v":1,"type":"erreur","code":"format_invalide","detail":"champ manquant : lat","seq":42}
Ces réponses sont limitées à "max_reponses_par_seconde" (tous expéditeurs
confondus) : sinon quelqu'un qui usurpe l'adresse IP d'une victime pourrait se
servir du serveur pour l'inonder de réponses.

Limite connue : sans clé secrète partagée (signature HMAC par exemple), rien
n'empêche quelqu'un d'envoyer un message bien formé, cohérent et au nom d'un
camion autorisé. Ces vérifications rendent la falsification difficile, pas impossible.

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
from collections import Counter
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from envoi import FORMAT_ID, TAILLE_MAX as TAILLE_MAX_MESSAGE, ErreurMessage, valider
from mouvement import haversine

TAILLE_LECTURE = 2048      # on lit plus que la taille max pour repérer les messages trop longs
MARGE_GPS = 50.0           # m : en dessous, un écart de position n'est jamais jugé incohérent

# Valeurs utilisées si une clé est absente de serveur_config.json
DEFAUTS = {
    "ecoute": {"hote": "0.0.0.0", "port": 5005},
    "delai_silence": 30,           # secondes sans message avant de signaler un camion
    "intervalle_stats": 60,        # secondes entre deux bilans (0 = pas de bilan)
    "taille_file": 10000,          # messages max en attente de traitement
    "logs": {"fichier": "logs/serveur.log", "niveau": "INFO",
             "taille_max_mo": 5, "nb_fichiers": 3},
    # P2 : validation et codes d'erreur
    "camions_autorises": [],       # liste vide = tous les camions acceptés
    "reponses_erreur": True,       # renvoyer un datagramme d'erreur à l'expéditeur
    "max_reponses_par_seconde": 5, # 0 = jamais de réponse
    "vitesse_max_plausible": 200,  # km/h, entre deux positions successives
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
    for ident in resultat["camions_autorises"]:
        if not isinstance(ident, str) or not FORMAT_ID.fullmatch(ident):
            raise ErreurConfig(f"« camions_autorises » : identifiant invalide {ident!r}")
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
        self.derniere_position = None        # (lat, lon, instant) du dernier message accepté
        self.silencieux = False
        self.rejetes = 0                     # messages refusés pour position incohérente


camions = {}                    # id du camion -> Camion
verrou = threading.Lock()       # protège "camions" et "stats" (utilisés par plusieurs threads)
file_messages = None            # file d'attente des datagrammes, créée dans main()
arret = threading.Event()       # mis à True pour arrêter tous les threads
stats = {"recus": 0, "acceptes": 0, "file_pleine": 0, "reponses": 0, "reponses_limitees": 0}
rejets = Counter()              # code d'erreur -> nombre de messages refusés
limiteur = None                 # Limiteur des réponses d'erreur, créé dans main()


# ---------------------------------------------------------------------------
# Thread 1 : réception UDP (P1)
# ---------------------------------------------------------------------------

def reception(sock):
    while not arret.is_set():
        try:
            data, addr = sock.recvfrom(TAILLE_LECTURE)
        except socket.timeout:
            continue                 # permet de vérifier "arret" chaque seconde
        except OSError as e:
            # sous Windows, un datagramme plus grand que TAILLE_LECTURE lève une
            # erreur au lieu d'être tronqué : on la note et on continue
            if not arret.is_set():
                log.warning("REJET trop_long : erreur de réception (%s)", e)
                with verrou:
                    rejets["trop_long"] += 1
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
# Thread 2 : vérification des messages et suivi des camions (P1 + P2 + P3)
# ---------------------------------------------------------------------------

def traitement(sock):
    while not arret.is_set():
        try:
            data, addr = file_messages.get(timeout=1)
        except queue.Empty:
            continue
        try:
            traiter_message(data, addr, sock)
        except Exception:
            # un message bizarre ne doit jamais arrêter le serveur
            log.exception("erreur pendant le traitement d'un message de %s", addr)
        finally:
            file_messages.task_done()


class Limiteur:
    """Seau à jetons : au plus `par_seconde` réponses par seconde en moyenne."""

    def __init__(self, par_seconde):
        self.par_seconde = par_seconde
        self.jetons = float(par_seconde)
        self.dernier = time.monotonic()
        self._verrou = threading.Lock()

    def autoriser(self):
        if self.par_seconde <= 0:
            return False
        with self._verrou:
            maintenant = time.monotonic()
            self.jetons = min(self.par_seconde, self.jetons + (maintenant - self.dernier) * self.par_seconde)
            self.dernier = maintenant
            if self.jetons >= 1:
                self.jetons -= 1
                return True
            return False


def propre(texte, longueur=150):
    """Texte venu du réseau -> texte sans retour à la ligne ni caractère de contrôle,
    pour qu'un message piégé ne puisse pas fabriquer de fausses lignes dans les logs."""
    texte = "".join(c if c.isprintable() else "?" for c in str(texte))
    return texte if len(texte) <= longueur else texte[:longueur - 3] + "..."


def rejeter(sock, addr, code, detail, seq=None):
    """Refuse un message : log, statistiques et (si autorisé) réponse à l'expéditeur."""
    detail = propre(detail)
    with verrou:
        rejets[code] += 1
    log.warning("REJET %s de %s:%d : %s", code, addr[0], addr[1], detail)
    if not conf["reponses_erreur"]:
        return
    if not limiteur.autoriser():
        with verrou:
            stats["reponses_limitees"] += 1
        return
    reponse = {"v": 1, "type": "erreur", "code": code, "detail": detail, "seq": seq}
    try:
        sock.sendto(json.dumps(reponse, ensure_ascii=False).encode("utf-8"), addr)
        with verrou:
            stats["reponses"] += 1
    except OSError as e:
        log.debug("réponse d'erreur non envoyée à %s:%d : %s", addr[0], addr[1], e)


def lire_horodatage(texte):
    """ISO 8601 -> datetime UTC. Sans fuseau indiqué, on considère que c'est de l'UTC
    (sinon comparer une date avec et une date sans fuseau ferait planter)."""
    d = datetime.fromisoformat(texte.replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def traiter_message(data, addr, sock):
    with verrou:
        stats["recus"] += 1

    # --- 1. taille
    if len(data) > TAILLE_MAX_MESSAGE:
        return rejeter(sock, addr, "trop_long", f"{len(data)} octets (max {TAILLE_MAX_MESSAGE})")

    # --- 2. lecture du JSON (P1)
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return rejeter(sock, addr, "json_illisible", f"datagramme de {len(data)} octets")

    seq = message.get("seq") if isinstance(message, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int):
        seq = None                   # renvoyé dans la réponse d'erreur seulement s'il est valide

    # --- 3. format complet du message (même fonction que côté camion)
    try:
        valider(message)
    except ErreurMessage as e:
        return rejeter(sock, addr, "format_invalide", e, seq)

    # --- 4. camion autorisé ?
    id_camion = message["id"]
    autorises = conf["camions_autorises"]
    if autorises and id_camion not in autorises:
        return rejeter(sock, addr, "non_autorise", f"camion {id_camion} absent de camions_autorises", seq)

    lat, lon = message["lat"], message["lon"]
    instant = lire_horodatage(message["horodatage"])
    refus = None

    with verrou:
        camion = camions.get(id_camion)
        redemarrage = False

        # --- 5. numéro de séquence : UDP peut perdre, dupliquer ou désordonner
        if camion is not None and camion.dernier_seq is not None:
            attendu = camion.dernier_seq + 1
            if seq == 0 and camion.dernier_seq > 0:
                redemarrage = True
            elif seq < attendu:
                camion.ignores += 1
                log.warning("[%s] message ignoré (seq %d déjà reçu ou en retard, dernier = %d)",
                            id_camion, seq, camion.dernier_seq)
                return

        # --- 6. cohérence avec la dernière position acceptée. On utilise les
        # horodatages des messages (pas l'heure de réception) : ça reste juste
        # quand le simulateur est accéléré. Pas de contrôle après un redémarrage.
        if camion is not None and camion.derniere_position is not None and not redemarrage:
            lat0, lon0, instant0 = camion.derniere_position
            distance = haversine((lat0, lon0), (lat, lon))
            duree = (instant - instant0).total_seconds()
            v_max = conf["vitesse_max_plausible"] / 3.6
            if distance > MARGE_GPS and (duree <= 0 or distance / duree > v_max):
                camion.rejetes += 1
                vitesse = "infinie" if duree <= 0 else f"{distance / duree * 3.6:.0f} km/h"
                refus = (f"{distance / 1000:.1f} km en {duree:.1f} s ({vitesse}, "
                         f"max {conf['vitesse_max_plausible']} km/h)")

        if refus is None:
            # --- message accepté : mise à jour de l'état du camion
            if camion is None:
                camion = Camion(id_camion, addr)
                camions[id_camion] = camion
                log.info("[%s] nouveau camion depuis %s:%d", id_camion, addr[0], addr[1])
            elif camion.adresse != addr:
                log.info("[%s] change d'adresse : %s:%d -> %s:%d", id_camion,
                         camion.adresse[0], camion.adresse[1], addr[0], addr[1])
                camion.adresse = addr
            if redemarrage:
                log.info("[%s] seq repart à 0 : camion redémarré", id_camion)
            elif camion.dernier_seq is not None and seq > camion.dernier_seq + 1:
                manquants = seq - camion.dernier_seq - 1
                camion.perdus += manquants
                log.warning("[%s] %d message(s) perdu(s) (seq %d à %d)",
                            id_camion, manquants, camion.dernier_seq + 1, seq - 1)

            camion.dernier_seq = seq
            camion.derniere_reception = time.monotonic()
            if camion.silencieux:
                camion.silencieux = False
                log.info("[%s] de nouveau en ligne", id_camion)
            camion.nb_messages += 1
            camion.derniere_position = (lat, lon, instant)
            stats["acceptes"] += 1

    # (hors du verrou : rejeter() le reprend)
    if refus is not None:
        return rejeter(sock, addr, "position_incoherente", f"[{id_camion}] {refus}", seq)

    log.info("[%s] seq=%d lat=%.6f lon=%.6f vitesse=%.1f etat=%s", id_camion, seq,
             lat, lon, message["vitesse"], message["etat"])


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
        log.info("bilan : %d message(s) reçu(s), %d accepté(s), %d rejeté(s), %d abandonné(s) (file pleine), "
                 "%d réponse(s) d'erreur (+%d limitée(s)), %d camion(s) dont %d actif(s)",
                 stats["recus"], stats["acceptes"], sum(rejets.values()), stats["file_pleine"],
                 stats["reponses"], stats["reponses_limitees"], len(camions), actifs)
        if rejets:
            log.info("  rejets par code : %s",
                     ", ".join(f"{code}={n}" for code, n in sorted(rejets.items())))
        for c in sorted(camions.values(), key=lambda c: c.id):
            log.info("  %-10s %5d msg  %3d perdu(s)  %3d ignoré(s)  %3d incohérent(s)  %s",
                     c.id, c.nb_messages, c.perdus, c.ignores, c.rejetes,
                     "silencieux" if c.silencieux else "actif")


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def main():
    global conf, file_messages, limiteur

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
    limiteur = Limiteur(conf["max_reponses_par_seconde"])

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
        threading.Thread(target=traitement, args=(sock,), name="traitement", daemon=True),
        threading.Thread(target=surveillance, name="surveillance", daemon=True),
    ]
    for t in threads:
        t.start()

    log.info("serveur UDP démarré sur %s:%d (config : %s, logs : %s)",
             hote, port, args.config, conf["logs"]["fichier"])
    if conf["camions_autorises"]:
        log.info("%d camion(s) autorisé(s) : %s", len(conf["camions_autorises"]),
                 ", ".join(conf["camions_autorises"]))
    else:
        log.warning("camions_autorises est vide : tous les identifiants sont acceptés")

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