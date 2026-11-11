"""
simulateur.py - P4 : lancer plusieurs camions en même temps à partir d'un fichier de config.

Chaque camion tourne dans son propre thread :
    à quai -> en route (aller) -> livraison -> en route (retour) -> à quai ... (si "boucle")
    à quai -> en route -> termine                                        (sinon)
et envoie sa position au serveur en UDP toutes les `periode_envoi` secondes.

Les trajets sont récupérés au démarrage (OSRM ou cache), un par un pour
respecter la limite du serveur public, avant de lancer les camions.

Usage :
    python simulateur.py                         (lit config.json)
    python simulateur.py -c autre_config.json
    python simulateur.py --acceleration 10       (le temps simulé va 10x plus vite)
    python simulateur.py --duree 300             (s'arrête au bout de 5 min)
"""

import argparse
import json
import logging
import random
import re
import sys
import threading
import time

from envoi import EmetteurUDP, construire_message
from mouvement import Deplacement, haversine
from routes import URL_OSRM_DEFAUT, ErreurOSRM, obtenir_trajet

log = logging.getLogger("simulateur")

DEFAUTS = {
    "serveur": {"hote": "127.0.0.1", "port": 5005},
    "osrm": {"url": URL_OSRM_DEFAUT, "timeout": 10, "dossier_cache": "cache_trajets",
             "ligne_droite_si_echec": True},
    "simulation": {"periode_envoi": 2.0, "acceleration": 1.0, "temps_livraison": 120,
                   "affichage": 10, "graine": None},
}
FORMAT_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ErreurConfig(Exception):
    pass


# ---------------------------------------------------------------------------
# Lecture et vérification de la configuration
# ---------------------------------------------------------------------------

def _nombre(valeur, nom, mini=None, maxi=None):
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
        raise ErreurConfig(f"{nom} doit être un nombre")
    if (mini is not None and valeur < mini) or (maxi is not None and valeur > maxi):
        raise ErreurConfig(f"{nom} doit être entre {mini} et {maxi} (valeur : {valeur})")
    return valeur


def _point(valeur, lieux, contexte):
    """Accepte [lat, lon] ou le nom d'un lieu défini dans "lieux"."""
    if isinstance(valeur, str):
        if valeur not in lieux:
            raise ErreurConfig(f"{contexte} : lieu inconnu « {valeur} »")
        return lieux[valeur]
    if not isinstance(valeur, (list, tuple)) or len(valeur) != 2:
        raise ErreurConfig(f"{contexte} : un point s'écrit [latitude, longitude] ou avec le nom d'un lieu")
    lat = _nombre(valeur[0], f"{contexte} (latitude)", -90, 90)
    lon = _nombre(valeur[1], f"{contexte} (longitude)", -180, 180)
    return (float(lat), float(lon))


def charger_config(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            brut = json.load(f)
    except FileNotFoundError:
        raise ErreurConfig(f"fichier de configuration introuvable : {chemin}") from None
    except json.JSONDecodeError as e:
        raise ErreurConfig(f"{chemin} n'est pas un JSON valide (ligne {e.lineno}, colonne {e.colno} : {e.msg})") from None
    if not isinstance(brut, dict):
        raise ErreurConfig("la configuration doit être un objet JSON")

    # Sections avec valeurs par défaut
    conf = {}
    for section, defaut in DEFAUTS.items():
        perso = brut.get(section, {})
        if not isinstance(perso, dict):
            raise ErreurConfig(f"la section « {section} » doit être un objet")
        inconnues = set(perso) - set(defaut)
        if inconnues:
            raise ErreurConfig(f"section « {section} » : clé(s) inconnue(s) {sorted(inconnues)}")
        conf[section] = {**defaut, **perso}

    _nombre(conf["serveur"]["port"], "serveur.port", 1, 65535)
    conf["serveur"]["port"] = int(conf["serveur"]["port"])
    _nombre(conf["osrm"]["timeout"], "osrm.timeout", 1, 120)
    sim = conf["simulation"]
    _nombre(sim["periode_envoi"], "simulation.periode_envoi", 0.1, 3600)
    _nombre(sim["acceleration"], "simulation.acceleration", 0.1, 1000)
    _nombre(sim["temps_livraison"], "simulation.temps_livraison", 0, 86400)
    _nombre(sim["affichage"], "simulation.affichage", 0, 3600)

    lieux = {}
    for nom, valeur in (brut.get("lieux") or {}).items():
        lieux[nom] = _point(valeur, {}, f"lieu « {nom} »")

    camions = brut.get("camions")
    if not isinstance(camions, list) or not camions:
        raise ErreurConfig("il faut au moins un camion dans la liste « camions »")

    conf["camions"] = []
    ids = set()
    for n, c in enumerate(camions, start=1):
        if not isinstance(c, dict):
            raise ErreurConfig(f"camion n°{n} : doit être un objet")
        ident = c.get("id")
        ctx = f"camion {ident or 'n°' + str(n)}"
        if not isinstance(ident, str) or not FORMAT_ID.fullmatch(ident):
            raise ErreurConfig(f"{ctx} : id obligatoire (lettres, chiffres, - et _, 32 caractères max)")
        if ident in ids:
            raise ErreurConfig(f"{ctx} : id déjà utilisé par un autre camion")
        ids.add(ident)
        if "depart" not in c or "arrivee" not in c:
            raise ErreurConfig(f"{ctx} : « depart » et « arrivee » sont obligatoires")

        depart = _point(c["depart"], lieux, f"{ctx}, départ")
        arrivee = _point(c["arrivee"], lieux, f"{ctx}, arrivée")
        if haversine(depart, arrivee) < 100:
            raise ErreurConfig(f"{ctx} : départ et arrivée sont quasiment au même endroit")

        boucle = c.get("boucle", True)
        if not isinstance(boucle, bool):
            raise ErreurConfig(f"{ctx} : « boucle » doit valoir true ou false")

        conf["camions"].append({
            "id": ident,
            "depart": depart,
            "arrivee": arrivee,
            "vitesse_max_kmh": float(_nombre(c.get("vitesse_max_kmh", 90), f"{ctx}, vitesse_max_kmh", 10, 130)),
            "boucle": boucle,
            "decalage_depart": float(_nombre(c.get("decalage_depart", 0), f"{ctx}, decalage_depart", 0, 86400)),
        })
    return conf


# ---------------------------------------------------------------------------
# Un camion = un thread
# ---------------------------------------------------------------------------

class CamionSimule(threading.Thread):

    def __init__(self, conf, trajets, emetteur, sim, arret, t0, rng):
        super().__init__(name=conf["id"], daemon=True)
        self.id = conf["id"]
        self.vmax = conf["vitesse_max_kmh"]
        self.boucle = conf["boucle"]
        self.decalage = conf["decalage_depart"]
        self.trajets = trajets          # [aller] ou [aller, retour]
        self.emetteur = emetteur
        self.periode = sim["periode_envoi"]
        self.acceleration = sim["acceleration"]
        self.temps_livraison = sim["temps_livraison"]
        self.arret = arret              # threading.Event partagé : demande d'arrêt
        self.t0 = t0                    # heure réelle du lancement (timestamp Unix)
        self.rng = rng

        self.temps_sim = 0.0
        self.seq = 0
        self.nb_trajets = 0
        self.erreur = None

        # Dernier état publié (lu par le thread principal pour l'affichage)
        self._verrou = threading.Lock()
        self.etat = "a_quai"
        self.vitesse_kmh = 0.0
        self.progression = 0.0

    def run(self):
        try:
            self._boucle()
        except Exception as e:   # on ne veut pas qu'un thread meure en silence
            self.erreur = e
            log.exception("[%s] erreur, camion arrêté", self.id)

    def _boucle(self):
        dt_sim = self.periode * self.acceleration
        etat = "a_quai"
        attente = self.decalage                 # temps simulé restant à quai / en livraison
        dep = None                               # Deplacement en cours
        n = 0                                    # indice du trajet courant (aller/retour)
        position, cap = self.trajets[0].points[0], 0.0

        # Petit décalage aléatoire (temps réel) pour que tous les camions
        # n'envoient pas leurs datagrammes exactement au même moment
        if self.arret.wait(self.rng.uniform(0, self.periode)):
            return
        prochain = time.monotonic()

        while True:
            # --- envoi de l'état courant
            if dep is not None:
                position, cap = dep.position(), dep.cap_actuel()
            self._publier(etat, position, dep.vitesse_kmh if dep and etat == "en_route" else 0.0,
                          cap, dep.progression if dep else 0.0)
            if etat == "termine":
                return

            # --- attente de la période suivante (en temps réel)
            prochain += self.periode
            pause = prochain - time.monotonic()
            if pause < 0:
                prochain = time.monotonic()   # en retard (machine chargée) : on se recale
                pause = 0
            if self.arret.wait(pause):
                return

            # --- évolution pendant dt_sim secondes de temps simulé
            self.temps_sim += dt_sim
            if etat in ("a_quai", "livraison"):
                attente -= dt_sim
                if attente <= 0:
                    trajet = self.trajets[n % len(self.trajets)]
                    dep = Deplacement(trajet.points, self.vmax, trajet.vitesses, rng=self.rng)
                    dep.avancer(-attente)     # le temps "en trop" sert déjà à rouler
                    etat = "en_route"
                    log.info("[%s] départ, trajet de %.1f km (%s)", self.id,
                             dep.longueur / 1000, trajet.source)
            elif etat == "en_route":
                dep.avancer(dt_sim)
                if dep.termine:
                    n += 1
                    self.nb_trajets += 1
                    log.info("[%s] arrivé après %.0f min (moyenne %.0f km/h)", self.id,
                             dep.temps / 60, dep.longueur / dep.temps * 3.6)
                    if self.boucle:
                        etat, attente = "livraison", self.temps_livraison
                    else:
                        etat = "termine"

    def _publier(self, etat, position, vitesse_kmh, cap, progression):
        msg = construire_message(self.id, self.seq, position[0], position[1],
                                 vitesse_kmh, cap, etat, t=self.t0 + self.temps_sim)
        self.emetteur.envoyer(msg)
        self.seq += 1
        with self._verrou:
            self.etat, self.vitesse_kmh, self.progression = etat, vitesse_kmh, progression

    def resume(self):
        with self._verrou:
            return self.etat, self.vitesse_kmh, self.progression


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def preparer_trajets(conf):
    """Récupère aller (et retour si boucle) pour chaque camion. Un camion dont
    le trajet n'a pas pu être obtenu est retiré, les autres continuent."""
    o = conf["osrm"]
    resultat = {}
    for c in conf["camions"]:
        etapes = [(c["depart"], c["arrivee"])]
        if c["boucle"]:
            # on redemande le retour à OSRM plutôt que d'inverser l'aller :
            # avec les sens uniques et les bretelles, ce n'est pas le même chemin
            etapes.append((c["arrivee"], c["depart"]))
        try:
            resultat[c["id"]] = [
                obtenir_trajet(a, b, base=o["url"], timeout=o["timeout"],
                               dossier_cache=o["dossier_cache"],
                               ligne_droite_si_echec=o["ligne_droite_si_echec"])
                for a, b in etapes
            ]
        except (ErreurOSRM, ValueError) as e:
            log.error("[%s] pas de trajet (%s) : camion ignoré", c["id"], e)
    return resultat


def afficher_etat(camions):
    for cam in camions:
        etat, v, prog = cam.resume()
        log.info("  %-10s %-9s %5.1f km/h  %5.1f %%  (%d msg)", cam.id, etat, v, prog * 100, cam.seq)


def main():
    parser = argparse.ArgumentParser(description="Simulateur de flotte de camions (SAÉ 3.02)")
    parser.add_argument("-c", "--config", default="config.json")
    parser.add_argument("--acceleration", type=float, help="remplace simulation.acceleration")
    parser.add_argument("--duree", type=float, help="durée max de la simulation en secondes réelles")
    parser.add_argument("-v", "--verbeux", action="store_true", help="affiche les détails (debug)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbeux else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    try:
        conf = charger_config(args.config)
    except ErreurConfig as e:
        log.error("configuration : %s", e)
        return 2
    sim = conf["simulation"]
    if args.acceleration is not None:
        if not 0.1 <= args.acceleration <= 1000:
            log.error("--acceleration doit être entre 0.1 et 1000")
            return 2
        sim["acceleration"] = args.acceleration

    log.info("récupération des trajets pour %d camion(s)...", len(conf["camions"]))
    trajets = preparer_trajets(conf)
    if not trajets:
        log.error("aucun trajet disponible, arrêt")
        return 1

    srv = conf["serveur"]
    try:
        emetteur = EmetteurUDP(srv["hote"], srv["port"])
    except OSError as e:
        log.error("%s", e)
        return 1

    arret = threading.Event()
    t0 = time.time()
    graine = sim["graine"]
    camions = []
    for c in conf["camions"]:
        if c["id"] not in trajets:
            continue
        # une graine par camion : reproductible si "graine" est fixée, sinon aléatoire
        rng = random.Random(None if graine is None else f"{graine}-{c['id']}")
        camions.append(CamionSimule(c, trajets[c["id"]], emetteur, sim, arret, t0, rng))

    log.info("%d camion(s) -> %s:%d, un message toutes les %.1f s, temps x%g",
             len(camions), srv["hote"], srv["port"], sim["periode_envoi"], sim["acceleration"])
    for cam in camions:
        cam.start()

    debut = time.monotonic()
    prochain_affichage = debut + sim["affichage"] if sim["affichage"] else None
    try:
        while any(cam.is_alive() for cam in camions):
            for cam in camions:
                cam.join(timeout=0.2)
            maintenant = time.monotonic()
            if args.duree is not None and maintenant - debut >= args.duree:
                log.info("durée atteinte, arrêt")
                break
            if prochain_affichage and maintenant >= prochain_affichage:
                afficher_etat(camions)
                prochain_affichage += sim["affichage"]
    except KeyboardInterrupt:
        log.info("Ctrl+C, arrêt en cours...")
    finally:
        arret.set()
        for cam in camions:
            cam.join(timeout=2)
        emetteur.fermer()

    log.info("terminé : %d messages envoyés (%d octets), %d échec(s) d'envoi",
             emetteur.nb_envoyes, emetteur.octets, emetteur.nb_erreurs)
    for cam in camions:
        log.info("  %-10s %d message(s), %d trajet(s) terminé(s)%s", cam.id, cam.seq,
                 cam.nb_trajets, f", ERREUR : {cam.erreur}" if cam.erreur else "")
    return 1 if any(cam.erreur for cam in camions) else 0


if __name__ == "__main__":
    sys.exit(main())
