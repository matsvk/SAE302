"""
routes.py - P1 : récupérer de vrais trajets routiers avec OSRM.

On interroge l'API OSRM (Open Source Routing Machine) pour avoir l'itinéraire
routier entre deux points GPS. Avec les bons paramètres, OSRM renvoie :
  - la géométrie complète du trajet (la liste des points qui suivent la route) ;
  - pour chaque petit segment, sa distance et sa durée, ce qui donne la vitesse
    "normale" sur ce tronçon (plus rapide sur autoroute qu'en ville).

ATTENTION : OSRM travaille en (longitude, latitude), alors que dans tout le
reste du projet on utilise (latitude, longitude). La conversion se fait ici.

Le serveur public de démo (router.project-osrm.org) est limité (1 requête/s,
pas fait pour un usage intensif). Du coup :
  - chaque trajet obtenu est gardé en cache dans un fichier JSON, on ne le
    redemande pas à chaque lancement ;
  - si OSRM ne répond pas, on peut se rabattre sur une ligne droite pour que
    la simulation tourne quand même (avec un avertissement).
"""

import argparse
import hashlib
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from mouvement import haversine, interpoler

log = logging.getLogger(__name__)

URL_OSRM_DEFAUT = "https://router.project-osrm.org"
USER_AGENT = "SAE302-suivi-flotte/1.0 (projet etudiant BUT R&T)"
INTERVALLE_MIN = 1.0        # s minimum entre deux requêtes (règle du serveur de démo)
LONGUEUR_MIN = 0.5          # m, deux points plus proches = même point
VITESSE_MIN_REF = 5 / 3.6   # bornes des vitesses de référence (m/s)
VITESSE_MAX_REF = 130 / 3.6

_verrou_requetes = threading.Lock()
_derniere_requete = 0.0


class ErreurOSRM(Exception):
    """Serveur OSRM injoignable, réponse invalide ou pas d'itinéraire trouvé."""


@dataclass
class Trajet:
    points: list        # [(lat, lon), ...]
    vitesses: list      # vitesse de référence de chaque segment en m/s (len(points) - 1 valeurs)
    distance: float     # longueur totale (m)
    duree: float        # durée estimée par OSRM (s), profil voiture
    source: str = "osrm"   # "osrm", "cache" ou "ligne_droite"

    def vers_dict(self):
        return {
            "points": [list(p) for p in self.points],
            "vitesses": self.vitesses,
            "distance": self.distance,
            "duree": self.duree,
        }

    @classmethod
    def depuis_dict(cls, d, source="cache"):
        points = [(float(p[0]), float(p[1])) for p in d["points"]]
        vitesses = [float(v) for v in d["vitesses"]]
        if len(points) < 2 or len(vitesses) != len(points) - 1:
            raise ValueError("trajet incohérent")
        return cls(points, vitesses, float(d["distance"]), float(d["duree"]), source)


# ---------------------------------------------------------------------------
# Requête OSRM
# ---------------------------------------------------------------------------

def construire_url(depart, arrivee, base=URL_OSRM_DEFAUT):
    # OSRM veut longitude,latitude !
    coords = f"{depart[1]:.6f},{depart[0]:.6f};{arrivee[1]:.6f},{arrivee[0]:.6f}"
    params = urllib.parse.urlencode({
        "overview": "full",                   # géométrie complète, pas simplifiée
        "geometries": "geojson",              # liste de [lon, lat] (pas de polyline encodée à décoder)
        "annotations": "distance,duration",   # distance et durée de chaque segment
        "steps": "false",                     # pas besoin des instructions de navigation
    })
    return f"{base.rstrip('/')}/route/v1/driving/{coords}?{params}"


def _respecter_intervalle():
    """Attend si la requête précédente date de moins d'une seconde."""
    global _derniere_requete
    with _verrou_requetes:
        attente = _derniere_requete + INTERVALLE_MIN - time.monotonic()
        if attente > 0:
            time.sleep(attente)
        _derniere_requete = time.monotonic()


def analyser_reponse(donnees):
    """Transforme la réponse JSON d'OSRM en objet Trajet."""
    code = donnees.get("code")
    if code != "Ok":
        raise ErreurOSRM(f"OSRM a répondu {code} : {donnees.get('message', 'pas de détail')}")
    routes = donnees.get("routes") or []
    if not routes:
        raise ErreurOSRM("aucun itinéraire dans la réponse")
    route = routes[0]

    try:
        coords = route["geometry"]["coordinates"]
        points_bruts = [(float(lat), float(lon)) for lon, lat in coords]   # inversion lon/lat
    except (KeyError, TypeError, ValueError) as e:
        raise ErreurOSRM(f"géométrie absente ou mal formée ({e})") from e
    if len(points_bruts) < 2:
        raise ErreurOSRM("géométrie trop courte")

    distance_osrm = float(route.get("distance") or 0)
    duree_osrm = float(route.get("duration") or 0)
    v_moyenne = distance_osrm / duree_osrm if duree_osrm > 0 else 50 / 3.6

    # Une annotation par "leg" (ici un seul, de départ à arrivée), on concatène par sécurité
    dist_seg, duree_seg = [], []
    for leg in route.get("legs") or []:
        ann = leg.get("annotation") or {}
        dist_seg.extend(ann.get("distance") or [])
        duree_seg.extend(ann.get("duration") or [])
    annotations_ok = len(dist_seg) == len(duree_seg) == len(points_bruts) - 1
    if not annotations_ok:
        log.warning("annotations OSRM absentes ou incohérentes : vitesse moyenne utilisée partout")

    # On reconstruit la liste en sautant les points en double
    points = [points_bruts[0]]
    vitesses = []
    for i in range(1, len(points_bruts)):
        p = points_bruts[i]
        if haversine(points[-1], p) < LONGUEUR_MIN:
            continue
        if annotations_ok and duree_seg[i - 1] > 0:
            v = dist_seg[i - 1] / duree_seg[i - 1]
        else:
            v = v_moyenne
        vitesses.append(min(max(v, VITESSE_MIN_REF), VITESSE_MAX_REF))
        points.append(p)

    if len(points) < 2:
        raise ErreurOSRM("itinéraire de longueur nulle (départ et arrivée confondus ?)")

    distance = sum(haversine(a, b) for a, b in zip(points, points[1:]))
    return Trajet(points, vitesses, distance, duree_osrm, "osrm")


def interroger_osrm(depart, arrivee, base=URL_OSRM_DEFAUT, timeout=10.0, tentatives=3):
    """Demande l'itinéraire à OSRM. Réessaie en cas de souci réseau
    (attente 1 s puis 2 s), mais pas si la requête elle-même est refusée."""
    url = construire_url(depart, arrivee, base)
    derniere_erreur = None

    for essai in range(1, tentatives + 1):
        _respecter_intervalle()
        log.debug("requête OSRM (essai %d) : %s", essai, url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as rep:
                donnees = json.loads(rep.read().decode("utf-8"))
            return analyser_reponse(donnees)

        except urllib.error.HTTPError as e:
            # En cas d'erreur 400, OSRM renvoie quand même du JSON avec la raison
            try:
                corps = json.loads(e.read().decode("utf-8"))
            except (ValueError, OSError):
                corps = {}
            if e.code == 400:
                raise ErreurOSRM(f"requête refusée : {corps.get('code', '?')} - "
                                 f"{corps.get('message', '')}") from e
            derniere_erreur = f"HTTP {e.code}"      # 429 (trop de requêtes), 5xx...
        except (urllib.error.URLError, OSError) as e:   # pas de réseau, DNS, timeout
            derniere_erreur = str(getattr(e, "reason", e))
        except ValueError:                              # réponse qui n'est pas du JSON
            derniere_erreur = "réponse illisible (pas du JSON)"

        if essai < tentatives:
            pause = 2 ** (essai - 1)
            log.warning("OSRM : échec (%s), nouvel essai dans %d s", derniere_erreur, pause)
            time.sleep(pause)

    raise ErreurOSRM(f"serveur OSRM injoignable après {tentatives} essais ({derniere_erreur})")


# ---------------------------------------------------------------------------
# Solution de repli : ligne droite
# ---------------------------------------------------------------------------

def ligne_droite(depart, arrivee, vitesse_kmh=50.0, pas=50.0):
    """Trajet en ligne droite découpé tous les `pas` mètres.
    Ne suit pas les routes : seulement pour pouvoir tester sans OSRM."""
    d = haversine(depart, arrivee)
    if d < LONGUEUR_MIN:
        raise ValueError("départ et arrivée confondus")
    n = max(1, math.ceil(d / pas))
    points = [interpoler(depart, arrivee, k / n) for k in range(n + 1)]
    v = vitesse_kmh / 3.6
    return Trajet(points, [v] * n, d, d / v, "ligne_droite")


# ---------------------------------------------------------------------------
# Cache disque
# ---------------------------------------------------------------------------

def _chemin_cache(dossier, depart, arrivee, base):
    # Clé = serveur + coordonnées arrondies à ~1 m
    cle = f"{base.rstrip('/')}|{depart[0]:.5f},{depart[1]:.5f}|{arrivee[0]:.5f},{arrivee[1]:.5f}"
    nom = hashlib.sha1(cle.encode("utf-8")).hexdigest()[:16]
    return os.path.join(dossier, f"trajet_{nom}.json")


def _lire_cache(chemin):
    try:
        with open(chemin, encoding="utf-8") as f:
            return Trajet.depuis_dict(json.load(f), source="cache")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("fichier de cache %s illisible (%s), ignoré", chemin, e)
        return None


def _ecrire_cache(chemin, trajet, depart, arrivee):
    contenu = {"depart": list(depart), "arrivee": list(arrivee),
               "cree_le": time.strftime("%Y-%m-%d %H:%M:%S"), **trajet.vers_dict()}
    try:
        os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)
        # écriture dans un fichier temporaire puis renommage : pas de fichier à moitié écrit
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(contenu, f)
        os.replace(tmp, chemin)
    except OSError as e:
        log.warning("impossible d'écrire le cache %s (%s)", chemin, e)


# ---------------------------------------------------------------------------
# Fonction principale du module
# ---------------------------------------------------------------------------

def obtenir_trajet(depart, arrivee, base=URL_OSRM_DEFAUT, timeout=10.0,
                   dossier_cache="cache_trajets", ligne_droite_si_echec=True):
    """Renvoie le trajet routier de `depart` à `arrivee` (tuples (lat, lon)).
    Ordre : cache -> OSRM (puis mise en cache) -> ligne droite si autorisé."""
    chemin = None
    if dossier_cache:
        chemin = _chemin_cache(dossier_cache, depart, arrivee, base)
        trajet = _lire_cache(chemin)
        if trajet is not None:
            log.info("trajet %s -> %s lu dans le cache (%.1f km)", depart, arrivee, trajet.distance / 1000)
            return trajet

    try:
        trajet = interroger_osrm(depart, arrivee, base, timeout)
    except ErreurOSRM as e:
        if not ligne_droite_si_echec:
            raise
        log.warning("%s -> trajet en LIGNE DROITE utilisé à la place (non mis en cache)", e)
        return ligne_droite(depart, arrivee)

    if chemin:
        _ecrire_cache(chemin, trajet, depart, arrivee)
    log.info("trajet %s -> %s obtenu par OSRM : %.1f km, %d points, ~%.0f min",
             depart, arrivee, trajet.distance / 1000, len(trajet.points), trajet.duree / 60)
    return trajet


if __name__ == "__main__":
    # Test : python routes.py 48.9472 2.2467 48.7590 2.3510
    parser = argparse.ArgumentParser(description="Teste la récupération d'un trajet OSRM")
    parser.add_argument("lat1", type=float)
    parser.add_argument("lon1", type=float)
    parser.add_argument("lat2", type=float)
    parser.add_argument("lon2", type=float)
    parser.add_argument("--url", default=URL_OSRM_DEFAUT)
    parser.add_argument("--sans-cache", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    t = obtenir_trajet((args.lat1, args.lon1), (args.lat2, args.lon2), base=args.url,
                       dossier_cache=None if args.sans_cache else "cache_trajets")
    vitesses = [v * 3.6 for v in t.vitesses]
    print(f"source      : {t.source}")
    print(f"distance    : {t.distance / 1000:.2f} km")
    print(f"durée OSRM  : {t.duree / 60:.1f} min")
    print(f"points      : {len(t.points)}")
    print(f"vitesses    : de {min(vitesses):.0f} à {max(vitesses):.0f} km/h selon les tronçons")
    print(f"début       : {t.points[0]}  fin : {t.points[-1]}")
