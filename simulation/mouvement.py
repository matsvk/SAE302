"""
mouvement.py - P2 : faire avancer un camion le long d'un trajet à une vitesse réaliste.

Le trajet est une liste de points GPS (lat, lon). On calcule la vraie distance
entre deux points avec la formule de haversine (distance sur une sphère), puis
à chaque pas de temps on fait avancer le camion d'une distance = vitesse x temps.

Pour que la vitesse soit crédible on ne roule pas à vitesse constante :
  - chaque tronçon a une vitesse de référence (donnée par OSRM selon le type de
    route : autoroute, nationale, rue...), plafonnée par la vitesse max du camion ;
  - le camion accélère et freine progressivement (un poids lourd ne passe pas
    de 0 à 90 km/h d'un coup) ;
  - il anticipe : il ralentit avant un virage serré, avant une portion plus lente,
    et il s'arrête en douceur à l'arrivée ;
  - un petit aléa fait varier l'allure de temps en temps (trafic, conducteur).
"""

import bisect
import math
import random

R_TERRE = 6_371_000.0   # rayon moyen de la Terre (m)

# Ordres de grandeur pour un poids lourd
ACCELERATION = 0.6      # m/s²  accélération normale
DECELERATION = 1.2      # m/s²  freinage "confortable", sert à anticiper
FREINAGE_MAX = 3.0      # m/s²  freinage fort, utilisé seulement si besoin
PAS_INTERNE = 0.5       # s     pas de calcul interne (voir avancer())
FENETRE_VIRAGE = 15.0   # m     distance avant/après un point pour mesurer un virage
LONGUEUR_MIN = 0.5      # m     deux points plus proches que ça = même point
VITESSE_MIN_REF = 5 / 3.6


# ---------------------------------------------------------------------------
# Fonctions géographiques
# ---------------------------------------------------------------------------

def haversine(p1, p2):
    """Distance en mètres entre deux points (lat, lon) en degrés."""
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    # min() pour éviter un asin(1.0000000002) à cause des arrondis
    return 2 * R_TERRE * math.asin(min(1.0, math.sqrt(a)))


def cap(p1, p2):
    """Cap en degrés pour aller de p1 à p2 (0 = nord, 90 = est, 180 = sud, 270 = ouest)."""
    lat1, lat2 = math.radians(p1[0]), math.radians(p2[0])
    dlon = math.radians(p2[1] - p1[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def interpoler(p1, p2, f):
    """Point situé à la fraction f (0..1) entre p1 et p2.
    Interpolation linéaire en lat/lon : suffisant sur des segments de quelques
    dizaines de mètres comme ceux renvoyés par OSRM."""
    return (p1[0] + (p2[0] - p1[0]) * f, p1[1] + (p2[1] - p1[1]) * f)


def ecart_angulaire(c1, c2):
    """Différence entre deux caps, entre 0 et 180°."""
    d = abs(c1 - c2) % 360.0
    return 360.0 - d if d > 180.0 else d


def vitesse_virage(angle):
    """Vitesse max (m/s) pour un changement de direction de `angle` degrés.
    En dessous de 25° on considère que ce n'est pas un vrai virage.
    25° -> 65 km/h, 45° -> 45 km/h, 90° et plus -> 15 km/h (tourner à un carrefour)."""
    if angle < 25:
        return math.inf
    return max(15.0, 90.0 - angle) / 3.6


# ---------------------------------------------------------------------------
# Déplacement le long d'un trajet
# ---------------------------------------------------------------------------

class Deplacement:
    """Position et vitesse d'un véhicule qui suit un trajet du début à la fin."""

    def __init__(self, points, vitesse_max_kmh, vitesses_ref=None, rng=None):
        """
        points          : liste de (lat, lon), au moins 2 points distincts
        vitesse_max_kmh : vitesse max du véhicule
        vitesses_ref    : vitesse "normale" de chaque segment en m/s (len(points)-1),
                          None = vitesse max partout
        rng             : random.Random, pour avoir des simulations reproductibles
        """
        if vitesse_max_kmh <= 0:
            raise ValueError("la vitesse max doit être positive")
        if vitesses_ref is not None and len(vitesses_ref) != len(points) - 1:
            raise ValueError("il faut une vitesse de référence par segment")

        self.vmax = vitesse_max_kmh / 3.6
        self.rng = rng or random.Random()

        # On enlève les points en double (OSRM en renvoie parfois)
        self.points = [tuple(points[0])]
        self.vref = []
        for i in range(1, len(points)):
            if haversine(self.points[-1], points[i]) < LONGUEUR_MIN:
                continue
            v = vitesses_ref[i - 1] if vitesses_ref is not None else self.vmax
            self.vref.append(min(max(v, VITESSE_MIN_REF), self.vmax))
            self.points.append(tuple(points[i]))
        if len(self.points) < 2:
            raise ValueError("trajet trop court (tous les points sont confondus)")

        self.nb_segments = len(self.points) - 1

        # Distance cumulée depuis le départ jusqu'à chaque point
        self.cumul = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.cumul.append(self.cumul[-1] + haversine(a, b))
        self.longueur = self.cumul[-1]

        self.caps = [cap(a, b) for a, b in zip(self.points, self.points[1:])]

        # Vitesse max autorisée en passant par chaque point :
        # min(virage, vitesse du segment qui commence là). 0 au dernier point (arrêt).
        self.limites = [math.inf] * (self.nb_segments + 1)
        self.limites[-1] = 0.0
        self._calculer_limites()

        # Distance sur laquelle on regarde devant : au moins la distance
        # d'arrêt depuis la vitesse max, plus une marge.
        self.horizon = self.vmax ** 2 / (2 * DECELERATION) + 100.0

        # État courant
        self.distance = 0.0     # distance parcourue depuis le départ (m)
        self.vitesse = 0.0      # m/s
        self.indice = 0         # segment sur lequel on se trouve
        self.temps = 0.0        # temps écoulé depuis le départ (s)
        self.facteur = 1.0      # aléa sur l'allure
        self._prochain_alea = 0.0

    def _calculer_limites(self):
        # Pour mesurer un virage on compare le cap ~15 m avant et ~15 m après le
        # point, sinon les points très rapprochés d'OSRM donnent des angles faux.
        for j in range(1, self.nb_segments):
            i_av = max(0, bisect.bisect_right(self.cumul, self.cumul[j] - FENETRE_VIRAGE) - 1)
            i_ap = min(self.nb_segments, bisect.bisect_left(self.cumul, self.cumul[j] + FENETRE_VIRAGE))
            i_av = min(i_av, j - 1)
            i_ap = max(i_ap, j + 1)
            angle = ecart_angulaire(cap(self.points[i_av], self.points[j]),
                                    cap(self.points[j], self.points[i_ap]))
            self.limites[j] = min(vitesse_virage(angle), self.vref[j])

    # -- propriétés pratiques ------------------------------------------------

    @property
    def termine(self):
        return self.distance >= self.longueur

    @property
    def vitesse_kmh(self):
        return self.vitesse * 3.6

    @property
    def progression(self):
        """Avancement entre 0 et 1."""
        return self.distance / self.longueur

    def position(self):
        """Position actuelle (lat, lon)."""
        i = self.indice
        long_seg = self.cumul[i + 1] - self.cumul[i]
        f = (self.distance - self.cumul[i]) / long_seg
        return interpoler(self.points[i], self.points[i + 1], min(1.0, max(0.0, f)))

    def cap_actuel(self):
        return self.caps[self.indice]

    # -- calcul du mouvement ---------------------------------------------------

    def avancer(self, dt):
        """Fait avancer le véhicule de dt secondes.
        On découpe en petits pas de 0,5 s : si le simulateur est accéléré
        (dt = 20 s par exemple) le freinage reste correct."""
        if dt < 0:
            raise ValueError("dt doit être positif")
        restant = dt
        while restant > 1e-9 and not self.termine:
            h = min(PAS_INTERNE, restant)
            self._pas(h)
            restant -= h
            self.temps += h

    def _pas(self, h):
        self._mettre_a_jour_alea(h)
        cible = self._vitesse_cible()

        if self.vitesse < cible:
            self.vitesse = min(cible, self.vitesse + ACCELERATION * h)
        else:
            self.vitesse = max(cible, self.vitesse - FREINAGE_MAX * h)

        self.distance = min(self.longueur, self.distance + self.vitesse * h)

        # Arrivé (à moins d'un mètre près) : on se cale pile sur le point final
        if self.longueur - self.distance < 1.0:
            self.distance = self.longueur
            self.vitesse = 0.0

        # Mise à jour du segment courant (on ne fait qu'avancer, donc pas besoin de recherche)
        while self.indice < self.nb_segments - 1 and self.cumul[self.indice + 1] <= self.distance:
            self.indice += 1

    def _vitesse_cible(self):
        """Vitesse visée maintenant, en tenant compte de ce qui arrive devant.
        Pour chaque point à venir avec une limite L situé à une distance d,
        on doit rouler à au plus sqrt(L² + 2·a·d) pour pouvoir freiner à temps."""
        v = self.vref[self.indice] * self.facteur
        j = self.indice + 1
        while j <= self.nb_segments:
            d = self.cumul[j] - self.distance
            if d > self.horizon:
                break
            v = min(v, math.sqrt(self.limites[j] ** 2 + 2 * DECELERATION * max(d, 0.0)))
            j += 1
        return v

    def _mettre_a_jour_alea(self, h):
        # Toutes les 20 à 90 s, le camion change un peu d'allure (85 à 100 % de la vitesse normale)
        self._prochain_alea -= h
        if self._prochain_alea <= 0:
            self.facteur = self.rng.uniform(0.85, 1.0)
            self._prochain_alea = self.rng.uniform(20.0, 90.0)


if __name__ == "__main__":
    # Petit test : 5 km en ligne droite puis un virage à angle droit et 1 km
    a = (48.9000, 2.2000)
    b = (48.9000, 2.2683)     # ~5 km vers l'est
    c = (48.9090, 2.2683)     # ~1 km vers le nord
    pts = [interpoler(a, b, k / 100) for k in range(101)] + [interpoler(b, c, k / 20) for k in range(1, 21)]
    dep = Deplacement(pts, 90, rng=random.Random(1))
    print(f"longueur : {dep.longueur:.0f} m")
    while not dep.termine:
        dep.avancer(10)
        lat, lon = dep.position()
        print(f"t={dep.temps:5.0f} s  {lat:.6f},{lon:.6f}  {dep.vitesse_kmh:5.1f} km/h  cap {dep.cap_actuel():5.1f}  {dep.progression:6.1%}")
    print(f"vitesse moyenne : {dep.longueur / dep.temps * 3.6:.1f} km/h")
