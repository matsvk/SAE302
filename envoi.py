"""
envoi.py - P3 (phase 1) : envoi des messages en UDP.

Chaque camion envoie régulièrement sa position au serveur dans un datagramme
UDP. Le contenu est du JSON encodé en UTF-8, un message = un datagramme.
Le format exact (champs, codes d'erreur) est défini dans protocole.py et
expliqué dans PROTOCOLE.md.

Exemple de message :
    {"v":1,"type":"position","id":"CAM-01","seq":42,
     "horodatage":"2026-11-02T21:30:12.250Z","lat":48.947312,"lon":2.247851,
     "vitesse":72.4,"cap":135.0,"etat":"en_route"}

Ce fichier contient aussi un petit récepteur pour tester sans le serveur :
    python envoi.py ecoute --port 5005
    python envoi.py test --hote 127.0.0.1 --port 5005
"""

import argparse
import logging
import socket
import threading
import time

# Le format est défini dans protocole.py ; on le ré-exporte ici pour que
# "from envoi import construire_message" continue de marcher.
from protocole import (ETATS, TAILLE_MAX, VERSION, ErreurMessage,  # noqa: F401
                       construire_message, decoder, encoder, horodatage_iso, valider)

log = logging.getLogger(__name__)


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
# Récepteur de test (sans le serveur)
# ---------------------------------------------------------------------------

def ecouter(port, hote="0.0.0.0", duree=None):
    """Affiche les messages reçus et compte les pertes/désordres par camion."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((hote, port))
    sock.settimeout(1.0)   # pour que Ctrl+C soit pris en compte, même sous Windows
    print(f"en écoute sur {hote}:{port} (Ctrl+C pour arrêter)", flush=True)

    dernier_seq = {}
    stats = {"recus": 0, "invalides": 0, "pertes": 0, "desordre": 0}
    fin = time.monotonic() + duree if duree else None
    try:
        while fin is None or time.monotonic() < fin:
            try:
                data, adr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:   # particularité Windows, sans conséquence
                continue
            try:
                msg = decoder(data)
            except ErreurMessage as e:
                stats["invalides"] += 1
                print(f"!! message invalide de {adr[0]} : {e}", flush=True)
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
                  f"cap {msg['cap']:5.1f}  {msg['etat']:<9} <- {adr[0]}{remarque}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print(f"\nreçus : {stats['recus']}  invalides : {stats['invalides']}  "
          f"perdus : {stats['pertes']}  hors ordre : {stats['desordre']}  "
          f"camions : {len(dernier_seq)}", flush=True)
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Envoi UDP et récepteur de test")
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
        m = construire_message("CAM-01", 0, 48.956800, 2.341700, 0.0, 0.0, "a_quai")
        with EmetteurUDP(args.hote, args.port) as em:
            em.envoyer(m)
        print(f"envoyé ({len(encoder(m))} octets) : {encoder(m).decode()}")
