"""
envoi.py - P3 : format des messages et envoi en UDP.

Chaque camion envoie régulièrement sa position au serveur dans un datagramme
UDP. Le contenu est du JSON encodé en UTF-8, un message = un datagramme.

Exemple de message (sur une seule ligne en vrai) :
    {"v":1,"type":"position","id":"CAM-01","seq":42,
     "horodatage":"2026-11-02T21:30:12.250Z","lat":48.947312,"lon":2.247851,
     "vitesse":72.4,"cap":135.0,"etat":"en_route"}

  v          version du format (pour pouvoir le faire évoluer)
  type       type de message (pour l'instant uniquement "position")
  id         identifiant du camion : lettres, chiffres, - et _ (32 max)
  seq        numéro de séquence, +1 à chaque message du camion. UDP ne garantit
             ni la livraison ni l'ordre : le serveur s'en sert pour repérer les
             pertes, les doublons et les messages arrivés en retard
  horodatage date/heure UTC de la mesure, format ISO 8601 en millisecondes
  lat, lon   position en degrés décimaux, 6 décimales (~10 cm)
  vitesse    km/h, 1 décimale
  cap        direction en degrés (0 = nord, 90 = est), 1 décimale
  etat       "a_quai", "en_route", "livraison" ou "termine"

Un message fait environ 180 octets, très loin de la limite fixée à 512 octets
(on reste sous ~1 400 octets pour ne jamais être fragmenté au niveau IP).

Ce fichier contient aussi un petit récepteur pour tester sans le vrai serveur :
    python envoi.py ecoute --port 5005
    python envoi.py test --hote 127.0.0.1 --port 5005
"""

import argparse
import json
import logging
import re
import socket
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

VERSION = 1
TAILLE_MAX = 512
ETATS = ("a_quai", "en_route", "livraison", "termine")
FORMAT_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# champ -> type(s) attendu(s)
CHAMPS = {
    "v": int, "type": str, "id": str, "seq": int, "horodatage": str,
    "lat": (int, float), "lon": (int, float), "vitesse": (int, float),
    "cap": (int, float), "etat": str,
}


class ErreurMessage(ValueError):
    """Message mal formé (à l'envoi comme à la réception)."""


# ---------------------------------------------------------------------------
# Format des messages
# ---------------------------------------------------------------------------

def horodatage_iso(t=None):
    """Timestamp Unix -> '2026-11-02T21:30:12.250Z' (UTC). t=None : maintenant."""
    d = datetime.now(timezone.utc) if t is None else datetime.fromtimestamp(t, tz=timezone.utc)
    return d.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def construire_message(id_camion, seq, lat, lon, vitesse_kmh, cap, etat, t=None):
    return {
        "v": VERSION,
        "type": "position",
        "id": str(id_camion),
        "seq": int(seq),
        "horodatage": horodatage_iso(t),
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "vitesse": round(float(vitesse_kmh), 1),
        "cap": round(float(cap), 1) % 360.0,
        "etat": etat,
    }


def valider(msg):
    """Vérifie un message champ par champ. Lève ErreurMessage si un problème est trouvé.
    Côté serveur c'est important : on ne fait jamais confiance à ce qui arrive du réseau."""
    if not isinstance(msg, dict):
        raise ErreurMessage("le message doit être un objet JSON")
    for champ, typ in CHAMPS.items():
        if champ not in msg:
            raise ErreurMessage(f"champ manquant : {champ}")
        # bool est une sous-classe de int en Python, on l'exclut explicitement
        if isinstance(msg[champ], bool) or not isinstance(msg[champ], typ):
            raise ErreurMessage(f"type invalide pour le champ {champ}")
    inconnus = set(msg) - set(CHAMPS)
    if inconnus:
        raise ErreurMessage(f"champ(s) inconnu(s) : {', '.join(sorted(inconnus))}")

    if msg["v"] != VERSION:
        raise ErreurMessage(f"version {msg['v']} non gérée")
    if msg["type"] != "position":
        raise ErreurMessage(f"type de message inconnu : {msg['type']}")
    if not FORMAT_ID.match(msg["id"]):
        raise ErreurMessage("identifiant de camion invalide")
    if msg["seq"] < 0:
        raise ErreurMessage("numéro de séquence négatif")
    if not -90 <= msg["lat"] <= 90 or not -180 <= msg["lon"] <= 180:
        raise ErreurMessage("coordonnées hors limites")
    if not 0 <= msg["vitesse"] <= 200:
        raise ErreurMessage("vitesse incohérente")
    if not 0 <= msg["cap"] < 360:
        raise ErreurMessage("cap hors de [0, 360[")
    if msg["etat"] not in ETATS:
        raise ErreurMessage(f"état inconnu : {msg['etat']}")
    try:
        datetime.fromisoformat(msg["horodatage"].replace("Z", "+00:00"))
    except ValueError:
        raise ErreurMessage("horodatage invalide") from None


def encoder(msg):
    """dict -> octets prêts à envoyer."""
    valider(msg)
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > TAILLE_MAX:
        raise ErreurMessage(f"message trop long ({len(data)} octets)")
    return data


def decoder(data):
    """octets reçus -> dict validé. Lève ErreurMessage si invalide."""
    if len(data) > TAILLE_MAX:
        raise ErreurMessage(f"datagramme trop long ({len(data)} octets)")
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ErreurMessage(f"JSON illisible ({e})") from None
    valider(msg)
    return msg


# ---------------------------------------------------------------------------
# Émission UDP
# ---------------------------------------------------------------------------

class EmetteurUDP:
    """Socket UDP partagée par tous les camions (sendto est utilisable depuis
    plusieurs threads). Garde des compteurs pour les statistiques."""

    def __init__(self, hote, port):
        # getaddrinfo : accepte une IPv4, une IPv6 ou un nom de machine
        try:
            infos = socket.getaddrinfo(hote, port, type=socket.SOCK_DGRAM)
        except socket.gaierror as e:
            raise OSError(f"adresse du serveur introuvable : {hote} ({e})") from e
        famille, _, _, _, self.adresse = infos[0]
        self.sock = socket.socket(famille, socket.SOCK_DGRAM)
        self._verrou = threading.Lock()
        self.nb_envoyes = 0
        self.nb_erreurs = 0
        self.octets = 0

    def envoyer(self, msg):
        """Envoie un message. Renvoie False si l'envoi a échoué (réseau),
        sans planter : un camion qui perd le réseau continue de rouler."""
        data = encoder(msg)   # une erreur ici = bug dans notre code, on la laisse remonter
        try:
            self.sock.sendto(data, self.adresse)
        except OSError as e:
            with self._verrou:
                self.nb_erreurs += 1
            log.warning("échec d'envoi UDP vers %s : %s", self.adresse, e)
            return False
        with self._verrou:
            self.nb_envoyes += 1
            self.octets += len(data)
        return True

    def fermer(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fermer()


# ---------------------------------------------------------------------------
# Récepteur de test (en attendant le vrai serveur)
# ---------------------------------------------------------------------------

def ecouter(port, hote="0.0.0.0", duree=None):
    """Affiche les messages reçus et compte les pertes/désordres par camion."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((hote, port))
    sock.settimeout(1.0)   # pour que Ctrl+C soit pris en compte, même sous Windows
    print(f"en écoute sur {hote}:{port} (Ctrl+C pour arrêter)")

    dernier_seq = {}
    stats = {"recus": 0, "invalides": 0, "pertes": 0, "desordre": 0}
    fin = time.monotonic() + duree if duree else None
    try:
        while fin is None or time.monotonic() < fin:
            try:
                data, adr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            try:
                msg = decoder(data)
            except ErreurMessage as e:
                stats["invalides"] += 1
                print(f"!! message invalide de {adr[0]} : {e}")
                continue

            stats["recus"] += 1
            precedent = dernier_seq.get(msg["id"])
            remarque = ""
            if precedent is not None:
                if msg["seq"] > precedent + 1:
                    stats["pertes"] += msg["seq"] - precedent - 1
                    remarque = f"  ({msg['seq'] - precedent - 1} perdu(s))"
                elif msg["seq"] <= precedent:
                    stats["desordre"] += 1
                    remarque = "  (doublon ou en retard)"
            dernier_seq[msg["id"]] = max(msg["seq"], precedent if precedent is not None else -1)

            print(f"{msg['horodatage']}  {msg['id']:<8} #{msg['seq']:<5} "
                  f"{msg['lat']:10.6f} {msg['lon']:10.6f}  {msg['vitesse']:5.1f} km/h  "
                  f"cap {msg['cap']:5.1f}  {msg['etat']:<9} <- {adr[0]}{remarque}")
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print(f"\nreçus : {stats['recus']}  invalides : {stats['invalides']}  "
          f"perdus : {stats['pertes']}  hors ordre : {stats['desordre']}  "
          f"camions : {len(dernier_seq)}")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Format des messages et envoi UDP")
    sous = parser.add_subparsers(dest="commande", required=True)

    p_ecoute = sous.add_parser("ecoute", help="récepteur de test : affiche les messages reçus")
    p_ecoute.add_argument("--port", type=int, default=5005)
    p_ecoute.add_argument("--hote", default="0.0.0.0")
    p_ecoute.add_argument("--duree", type=float, default=None, help="arrêt auto après N secondes")

    p_test = sous.add_parser("test", help="envoie un message de test")
    p_test.add_argument("--hote", default="127.0.0.1")
    p_test.add_argument("--port", type=int, default=5005)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.commande == "ecoute":
        ecouter(args.port, args.hote, args.duree)
    else:
        m = construire_message("TEST-01", 0, 48.956800, 2.341700, 0.0, 0.0, "a_quai")
        with EmetteurUDP(args.hote, args.port) as em:
            em.envoyer(m)
        print(f"envoyé ({len(encoder(m))} octets) : {encoder(m).decode()}")
