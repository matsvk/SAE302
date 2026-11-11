"""
test_protocole.py - tests de la validation des messages reçus.

En UDP, n'importe qui sur le réseau peut envoyer un datagramme sur le port 5005 :
le serveur ne doit donc jamais croire aveuglément ce qu'il reçoit. Ce script
envoie une batterie de faux messages (mal formés, piégés, incohérents) et
vérifie que chacun est bien refusé, et que les vrais messages passent toujours.

Deux niveaux de test :
  1. hors réseau : appelle directement envoi.decoder() sur chaque cas (< 1 s) ;
  2. serveur réel ("boîte noire") : le script lance le serveur, lui envoie les
     datagrammes un par un et lit ce qu'il affiche pour savoir s'il a accepté
     ou refusé chacun. Il détecte aussi les plantages (et relance le serveur).

Comment le script sait si le serveur a refusé un message (niveau 2) :
  - une ligne affichée contenant REJET, ERREUR, INVALIDE, REFUS, IGNORÉ,
    DOUBLON, NON-JSON, "sans identifiant" ou "!!" ;
  - ou une réponse d'erreur UDP renvoyée par le serveur ("type":"erreur") ;
  - ou rien du tout (message ignoré en silence).
Une trace d'exception (Traceback) compte comme un ÉCHEC, même si le serveur
survit : c'est un bug, pas un refus.

Pour serveur_udp.py, le script fabrique une config de test temporaire à partir
de serveur_config.json : logs dans un dossier temporaire (ton logs/serveur.log
n'est pas touché), pas de bilan périodique, et les id de test ajoutés à
camions_autorises (sinon les messages valides de test seraient refusés).

Catégories de tests :
  protocole  format des messages : bloquant aux deux niveaux
  serveur    ce que seul le serveur peut voir (camion non autorisé, doublon,
             téléportation...) : bloquant au niveau 2
  optionnel  bonnes idées mais pas exigées : avertissement, bloquant avec --strict

Usage :
    python test_protocole.py                               (niveau 1 seulement)
    python test_protocole.py --serveur serveur_udp.py      (niveaux 1 et 2)
    python test_protocole.py --serveur "envoi.py ecoute"   (teste le récepteur de test)
    python test_protocole.py --serveur serveur_udp.py --strict
    python test_protocole.py --demo --hote 192.168.1.20 --pause 1
        (soutenance : envoie les pièges à un serveur déjà lancé, on regarde sa console)

Code de retour : 0 si tout passe, 1 sinon (utilisable dans un script ou en CI).
"""

import argparse
import json
import os
import queue
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from itertools import count

from envoi import (CHAMPS, FORMAT_ID, TAILLE_MAX, ErreurMessage, construire_message, decoder,
                   encoder, horodatage_iso)

ACCEPTE, REJETE, NON_ENVOYE = "accepté", "rejeté", "non envoyé"
PLANTAGE, EXCEPTION, INJECTION = "PLANTAGE", "EXCEPTION", "INJECTION"
SUPPRIME = object()

MOTS_REJET = re.compile(r"rejet|erreur|invalide|refus|ignor|doublon|non-json|sans identifiant|!!",
                        re.IGNORECASE)
MARQUEUR_INJECTION = "FAUX-LOG-INJECTE"
ID_INTRUS = "INTRUS-99"         # bien formé, mais jamais autorisé
ATTENTE_MAX = 0.4    # s : délai max pour que le serveur réagisse à un datagramme
CALME = 0.1          # s : après une 1re ligne, on attend encore un peu les suivantes

DOSSIER = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Fabrication des messages de test
# ---------------------------------------------------------------------------

@dataclass
class Cas:
    nom: str
    etapes: list                  # [(octets, résultat attendu), ...]
    categorie: str = "protocole"  # "protocole", "serveur" ou "optionnel" (voir en tête)


_numero = count(1)
IDS_TEST = set()                  # id bien formés utilisés : à autoriser côté serveur


def message(**modifs):
    """Message valide fabriqué par construire_message() (le même code que les
    camions), puis modifié champ par champ. valeur=SUPPRIME enlève le champ.
    Chaque appel prend un id différent pour ne pas fausser le suivi des seq."""
    m = construire_message(f"TEST-{next(_numero):03d}", 0, 48.956800, 2.341700, 52.0, 90.0, "en_route")
    for champ, valeur in modifs.items():
        if valeur is SUPPRIME:
            del m[champ]
        else:
            m[champ] = valeur
    ident = m.get("id")
    if isinstance(ident, str) and FORMAT_ID.fullmatch(ident) and ident != ID_INTRUS:
        IDS_TEST.add(ident)
    return m


def octets(m):
    """dict -> JSON SANS validation (contrairement à encoder()) : c'est le but."""
    return json.dumps(m, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def un(nom, donnees, attendu=REJETE):
    return Cas(nom, [(octets(donnees) if isinstance(donnees, dict) else donnees, attendu)])


def pile_a_la_taille(taille):
    """Message valide complété par des espaces (JSON valide) jusqu'à `taille` octets."""
    d = octets(message())
    return d[:-1] + b" " * (taille - len(d)) + b"}"


def catalogue():
    c = []

    # --- ce qui DOIT passer (sinon le serveur refuse les vrais camions)
    c.append(un("valide : message de référence", message(), ACCEPTE))
    for etat in ("a_quai", "livraison", "termine"):
        c.append(un(f"valide : état {etat}", message(etat=etat, vitesse=0.0), ACCEPTE))
    c.append(un("valide : bornes lat=90 lon=-180", message(lat=90.0, lon=-180.0), ACCEPTE))
    c.append(un("valide : cap 359.9 et vitesse 200", message(cap=359.9, vitesse=200.0), ACCEPTE))
    c.append(un("valide : nombres entiers (48, 2, 0)", message(lat=48, lon=2, vitesse=0, cap=0), ACCEPTE))
    c.append(un(f"valide : exactement {TAILLE_MAX} octets", pile_a_la_taille(TAILLE_MAX), ACCEPTE))

    # --- datagrammes qui ne sont pas du JSON exploitable
    c.append(un("datagramme vide", b""))
    c.append(un("texte quelconque", b"bonjour serveur"))
    c.append(un("JSON tronqué", octets(message())[:-5]))
    c.append(un("UTF-8 invalide", b'{"id":"\xff\xfe"}'))
    c.append(un("BOM UTF-8 devant le JSON", b"\xef\xbb\xbf" + octets(message())))
    c.append(un("JSON liste au lieu d'objet", b"[1,2,3]"))
    c.append(un("JSON null", b"null"))
    c.append(un("JSON nombre seul", b"42"))
    c.append(un("JSON chaîne seule", b'"position"'))
    c.append(un("imbrication [[[...]]] x200", b"[" * 200 + b"]" * 200))
    c.append(un(f"trop long : {TAILLE_MAX + 1} octets", pile_a_la_taille(TAILLE_MAX + 1)))
    c.append(un("2048 octets binaires", bytes(range(256)) * 8))
    c.append(un("datagramme géant (60 000 octets)", b"A" * 60000))

    # --- structure du message
    for champ in CHAMPS:
        c.append(un(f"champ manquant : {champ}", message(**{champ: SUPPRIME})))
    c.append(un("champ en trop : admin=true", message(admin=True)))
    c.append(un("version 2", message(v=2)))
    c.append(un('version "1" (chaîne)', message(v="1")))
    c.append(un('type "commande"', message(type="commande")))
    c.append(un('type "POSITION" (majuscules)', message(type="POSITION")))

    # --- identifiant (souvent réutilisé dans des noms de fichiers, du HTML, du SQL...)
    c.append(un("id vide", message(id="")))
    c.append(un("id de 33 caractères", message(id="A" * 33)))
    c.append(un("id avec espace", message(id="CAM 01")))
    c.append(un("id avec retour à la ligne final", message(id="CAM-01\n")))
    c.append(un("id avec accent", message(id="CAMÉ-01")))
    c.append(un("id injection SQL", message(id="CAM-01';DROP TABLE camions;--")))
    c.append(un("id injection HTML", message(id="<script>alert(1)</script>")))
    c.append(un("id chemin ../../etc/passwd", message(id="../../etc/passwd")))
    c.append(un("id nombre", message(id=123)))

    # --- numéro de séquence
    c.append(un("seq négatif", message(seq=-1)))
    c.append(un("seq décimal 1.5", message(seq=1.5)))
    c.append(un('seq chaîne "7"', message(seq="7")))
    c.append(un("seq booléen true", message(seq=True)))

    # --- position
    c.append(un("latitude 91", message(lat=91.0)))
    c.append(un("latitude -90.5", message(lat=-90.5)))
    c.append(un("longitude 180.5", message(lon=180.5)))
    c.append(un('latitude chaîne "48.95"', message(lat="48.95")))
    c.append(un("latitude null", message(lat=None)))
    c.append(un("latitude NaN", message(lat=float("nan"))))
    c.append(un("latitude Infinity", message(lat=float("inf"))))
    c.append(un("latitude booléen", message(lat=True)))
    c.append(un("latitude liste [48.9, 2.3]", message(lat=[48.9, 2.3])))

    # --- vitesse, cap, état, horodatage
    c.append(un("vitesse négative", message(vitesse=-0.1)))
    c.append(un("vitesse 200.1 km/h", message(vitesse=200.1)))
    c.append(un("vitesse 1e400 (infini)",
                octets(message()).replace(b'"vitesse":52.0', b'"vitesse":1e400')))
    c.append(un("cap 360", message(cap=360.0)))
    c.append(un("cap négatif", message(cap=-0.1)))
    c.append(un('état "vole"', message(etat="vole")))
    c.append(un("état vide", message(etat="")))
    c.append(un('état "EN_ROUTE" (majuscules)', message(etat="EN_ROUTE")))
    c.append(un('horodatage "hier"', message(horodatage="hier")))
    c.append(un("horodatage vide", message(horodatage="")))
    c.append(un("horodatage nombre (timestamp Unix)", message(horodatage=1730000000)))
    c.append(un("horodatage date impossible", message(horodatage="2026-13-45T25:61:00Z")))

    # --- ce que seul le serveur peut voir : chaque message est bien formé, c'est
    # l'expéditeur ou l'ENCHAÎNEMENT qui est suspect. envoi.decoder() ne garde
    # pas d'état : c'est au serveur de suivre chaque camion.
    t = time.time()
    c.append(Cas("camion non autorisé (INTRUS-99)", [(octets(message(id=ID_INTRUS)), REJETE)], "serveur"))

    m = message(id="SRV-A", seq=5)
    c.append(Cas("doublon : même datagramme rejoué", [(octets(m), ACCEPTE), (octets(m), REJETE)], "serveur"))

    c.append(Cas("retard : seq 10 puis seq 9", [
        (octets(message(id="SRV-B", seq=10)), ACCEPTE),
        (octets(message(id="SRV-B", seq=9)), REJETE)], "serveur"))

    c.append(Cas("trou dans seq (0 puis 5) : accepté", [
        (octets(message(id="SRV-C", seq=0)), ACCEPTE),
        (octets(message(id="SRV-C", seq=5, horodatage=horodatage_iso(t + 15))), ACCEPTE)], "serveur"))

    c.append(Cas("téléportation : Villetaneuse -> Marseille en 3 s", [
        (octets(message(id="SRV-D", seq=0, horodatage=horodatage_iso(t))), ACCEPTE),
        (octets(message(id="SRV-D", seq=1, lat=43.296500, lon=5.369800,
                        horodatage=horodatage_iso(t + 3))), REJETE)], "serveur"))

    c.append(Cas("trajet normal : 100 m en 3 s accepté", [
        (octets(message(id="SRV-E", seq=0, horodatage=horodatage_iso(t))), ACCEPTE),
        (octets(message(id="SRV-E", seq=1, lat=48.957700, horodatage=horodatage_iso(t + 3))), ACCEPTE)],
        "serveur"))

    # Horodatages écrits différemment mais valides : le serveur doit savoir les
    # comparer entre eux sans planter (date avec / sans fuseau horaire)
    c.append(Cas("horodatage sans fuseau puis avec +02:00", [
        (octets(message(id="SRV-F", seq=0, horodatage="2026-11-11T10:00:00.000")), ACCEPTE),
        (octets(message(id="SRV-F", seq=1, horodatage="2026-11-11T12:00:03.000+02:00")), ACCEPTE)],
        "serveur"))

    # Injection dans les logs : un retour à la ligne dans un champ ne doit pas
    # permettre de fabriquer une fausse ligne de log (voir MARQUEUR_INJECTION)
    c.append(Cas("injection de fausse ligne dans les logs",
                 [(octets(message(etat=f"vole\n{MARQUEUR_INJECTION} [CAM-01] arrivé à destination")),
                   REJETE)], "serveur"))

    # Avec --acceleration, le simulateur envoie des horodatages en avance sur
    # l'heure réelle : un contrôle "pas dans le futur" doit garder une grosse marge.
    c.append(Cas("horodatage dans 1 an", [
        (octets(message(horodatage=horodatage_iso(t + 365 * 86400))), REJETE)], "optionnel"))
    c.append(Cas("horodatage en l'an 2000", [
        (octets(message(horodatage="2000-01-01T00:00:00.000Z")), REJETE)], "optionnel"))
    return c


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------

def apercu(texte, n=70):
    if isinstance(texte, bytes):
        texte = texte.decode("utf-8", "replace")
    texte = texte.replace("\n", " ")
    return texte if len(texte) <= n else texte[:n - 1] + "…"


def ligne_resultat(statut, nom, obtenu, detail, attendu):
    suite = "" if obtenu == attendu else f"   (attendu : {attendu})"
    print(f"  {statut:<6} {nom:<48} {obtenu:<9} {apercu(detail)}{suite}")


# ---------------------------------------------------------------------------
# Niveau 1 : envoi.decoder() hors réseau
# ---------------------------------------------------------------------------

def tests_aller_retour():
    """Ce que fabriquent les camions doit toujours passer la validation."""
    erreurs = []
    exemples = [
        (48.956800, 2.341700, 0.0, 0.0, "a_quai"),
        (48.7590123456, 2.351, 89.97, 359.96, "en_route"),   # cap arrondi à 360.0 -> 0.0
        (49.0097, 2.5479, 110.0, -10.0, "livraison"),         # cap négatif -> 350
        (-33.8688, 151.2093, 200.0, 720.0, "termine"),
    ]
    for lat, lon, v, c, e in exemples:
        m = construire_message("CAM-01", 12, lat, lon, v, c, e)
        try:
            if decoder(encoder(m)) != m:
                erreurs.append(f"aller-retour modifie le message {m}")
        except ErreurMessage as ex:
            erreurs.append(f"construire_message({lat}, {lon}, {v}, {c}, {e}) refusé : {ex}")
    return erreurs


def tester_hors_reseau(liste):
    print("\n== 1. Validation hors réseau : envoi.decoder() ==")
    echecs = 0
    for cas in liste:
        if cas.categorie != "protocole":
            continue
        donnees, attendu = cas.etapes[0]
        try:
            decoder(donnees)
            obtenu, detail = ACCEPTE, ""
        except ErreurMessage as e:
            obtenu, detail = REJETE, str(e)
        except Exception as e:      # tout autre exception = le serveur planterait
            obtenu, detail = PLANTAGE, f"{type(e).__name__} : {e}"
        ok = obtenu == attendu
        echecs += not ok
        ligne_resultat("OK" if ok else "ECHEC", cas.nom, obtenu, detail, attendu)

    for err in tests_aller_retour():
        echecs += 1
        print(f"  ECHEC  {err}")
    print(f"  -> {echecs} échec(s)")
    return echecs


# ---------------------------------------------------------------------------
# Niveau 2 : le vrai serveur, en boîte noire
# ---------------------------------------------------------------------------

class ServeurTeste:
    """Lance le serveur dans un sous-processus et lit ce qu'il affiche."""

    def __init__(self, commande, hote, port):
        self.commande = commande
        self.proc = None
        self.lignes = queue.Queue()
        self.historique = deque(maxlen=30)
        infos = socket.getaddrinfo(hote, port, type=socket.SOCK_DGRAM)
        famille, _, _, _, self.adresse = infos[0]
        self.sock = socket.socket(famille, socket.SOCK_DGRAM)
        self.sock.setblocking(False)         # pour lire les réponses d'erreur sans attendre

    def demarrer(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(self.commande, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, env=env, cwd=DOSSIER)
        threading.Thread(target=self._lire, args=(self.proc,), daemon=True).start()
        try:
            self.lignes.get(timeout=5)      # bannière de démarrage
        except queue.Empty:
            pass
        time.sleep(0.3)
        self._vider()
        if self.proc.poll() is not None:
            raise RuntimeError("le serveur s'est arrêté au démarrage (port déjà pris ?) :\n    "
                               + "\n    ".join(self.historique))

    def _lire(self, proc):
        for brut in proc.stdout:
            ligne = brut.decode("utf-8", "replace").rstrip()
            self.historique.append(ligne)
            self.lignes.put(ligne)

    def _vider(self):
        while not self.lignes.empty():
            self.lignes.get_nowait()

    def vivant(self):
        return self.proc is not None and self.proc.poll() is None

    def soumettre(self, donnees):
        """Envoie un datagramme et renvoie (verdict, ce que le serveur a affiché ou répondu)."""
        self._vider()
        self._reponses()                     # jette d'éventuelles réponses en retard
        try:
            self.sock.sendto(donnees, self.adresse)
        except OSError as e:
            return NON_ENVOYE, f"refusé par le système d'exploitation ({e})"
        lignes = []
        limite = time.monotonic() + ATTENTE_MAX
        while True:
            attente = CALME if lignes else limite - time.monotonic()
            if attente <= 0:
                break
            try:
                lignes.append(self.lignes.get(timeout=attente))
            except queue.Empty:
                break
        reponses = self._reponses()

        trace = any(l.lstrip().startswith("Traceback") for l in lignes)
        if trace:
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if not self.vivant():
            cause = next((l for l in reversed(self.historique)
                          if "Error" in l or "Exception" in l), "processus terminé")
            return PLANTAGE, cause
        if trace:
            # le serveur a survécu (il attrape l'exception) mais c'est un bug
            cause = next((l for l in reversed(lignes) if "Error" in l or "Exception" in l), lignes[-1])
            return EXCEPTION, cause
        injectee = next((l for l in lignes if l.startswith(MARQUEUR_INJECTION)), None)
        if injectee:
            return INJECTION, f"fausse ligne dans les logs : {injectee}"

        texte = " | ".join(lignes + [f"réponse UDP : {r}" for r in reponses])
        if not texte:
            return REJETE, "(aucune réaction : ignoré en silence)"
        refuse = MOTS_REJET.search(" | ".join(lignes)) or any('"type":"erreur"' in r.replace(" ", "")
                                                             for r in reponses)
        return (REJETE if refuse else ACCEPTE), texte

    def _reponses(self):
        """Datagrammes renvoyés par le serveur depuis la dernière lecture."""
        recus = []
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except (BlockingIOError, socket.timeout):
                return recus
            except OSError:          # Windows : ICMP "port inaccessible" remonté ici
                return recus
            recus.append(data.decode("utf-8", "replace"))

    def arreter(self):
        if self.vivant():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.sock.close()


def construire_commande(texte):
    morceaux = shlex.split(texte, posix=(os.name != "nt"))
    if morceaux and morceaux[0].endswith(".py"):
        morceaux = [sys.executable, "-u"] + morceaux
    return morceaux


def preparer_config_serveur(commande, dossier_tmp):
    """Pour serveur_udp.py lancé sans -c : écrit une config de test à partir de
    serveur_config.json et l'ajoute à la commande. Renvoie (commande, port d'écoute)."""
    scripts = [m for m in commande if m.endswith(".py")]
    if (not scripts or os.path.basename(scripts[0]) != "serveur_udp.py"
            or {"-c", "--config"} & set(commande)):
        return commande, None
    try:
        with open(os.path.join(DOSSIER, "serveur_config.json"), encoding="utf-8") as f:
            conf = json.load(f)
    except FileNotFoundError:
        conf = {}
    conf["camions_autorises"] = sorted(IDS_TEST)   # INTRUS-99 n'y est jamais
    conf["intervalle_stats"] = 0                   # pas de bilan au milieu des tests
    conf["delai_silence"] = 3600                   # pas d'alerte "camion silencieux"
    conf["max_reponses_par_seconde"] = 1000        # une réponse par refus pendant le test
    conf.setdefault("logs", {})["fichier"] = os.path.join(dossier_tmp, "serveur.log")
    conf["logs"]["niveau"] = "INFO"
    chemin = os.path.join(dossier_tmp, "serveur_config_test.json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    print(f"  config de test : {chemin} ({len(IDS_TEST)} id de test autorisés, logs hors projet)")
    return commande + ["-c", chemin], (conf.get("ecoute") or {}).get("port")


def tester_serveur(liste, commande, hote, port, strict):
    print(f"\n== 2. Serveur réel : {' '.join(commande)} ==")
    with tempfile.TemporaryDirectory(prefix="test_protocole_") as tmp:
        commande, port_conf = preparer_config_serveur(commande, tmp)
        port = port or port_conf or port_config()
        print(f"  datagrammes envoyés vers {hote}:{port}")
        return _tester_serveur(liste, commande, hote, port, strict)


def _tester_serveur(liste, commande, hote, port, strict):
    srv = ServeurTeste(commande, hote, port)
    try:
        srv.demarrer()
    except (RuntimeError, OSError) as e:
        print(f"  impossible de lancer le serveur : {e}")
        return 1, 0

    echecs = avertissements = bugs = 0
    try:
        for cas in liste:
            ok, obtenu, detail, attendu = True, None, "", None
            for donnees, attendu in cas.etapes:
                obtenu, detail = srv.soumettre(donnees)
                if obtenu != attendu:
                    ok = False
                    break
            grave = obtenu in (PLANTAGE, EXCEPTION, INJECTION)
            bugs += grave

            if obtenu == NON_ENVOYE:
                statut = "SAUTE"
            elif ok:
                statut = "OK"
            elif cas.categorie == "optionnel" and not strict and not grave:
                statut = "AVERT"
                avertissements += 1
            else:
                statut = "ECHEC"
                echecs += 1
            ligne_resultat(statut, cas.nom, obtenu, detail, attendu)

            if not srv.vivant():
                print("         -> le serveur est mort, relance pour la suite des tests")
                srv.demarrer()
    except RuntimeError as e:
        print(f"  arrêt des tests : {e}")
        echecs += 1
    finally:
        srv.arreter()

    print(f"  -> {echecs} échec(s) dont {bugs} plantage(s)/exception(s)/injection(s), "
          f"{avertissements} avertissement(s) (tests optionnels)")
    return echecs, avertissements


# ---------------------------------------------------------------------------
# Mode démonstration (soutenance)
# ---------------------------------------------------------------------------

def demo(liste, hote, port, pause):
    """Envoie tous les pièges à un serveur déjà lancé, sans rien vérifier :
    on regarde sa console réagir (et on affiche ses réponses d'erreur UDP)."""
    infos = socket.getaddrinfo(hote, port, type=socket.SOCK_DGRAM)
    famille, _, _, _, adresse = infos[0]
    with socket.socket(famille, socket.SOCK_DGRAM) as sock:
        for n, cas in enumerate(liste, 1):
            for donnees, attendu in cas.etapes:
                print(f"[{n:02d}] {cas.nom:<48} (doit être {attendu}) {len(donnees):>5} o  {apercu(donnees, 50)}")
                try:
                    sock.sendto(donnees, adresse)
                except OSError as e:
                    print(f"      non envoyé : {e}")
                time.sleep(pause)
                sock.settimeout(0.05)
                try:
                    while True:
                        print(f"      <- réponse : {apercu(sock.recvfrom(4096)[0], 90)}")
                except OSError:      # plus rien à lire (timeout) ou ICMP sous Windows
                    pass
    print(f"{sum(len(c.etapes) for c in liste)} datagrammes envoyés à {hote}:{port}")


# ---------------------------------------------------------------------------

def port_config():
    try:
        with open(os.path.join(DOSSIER, "config.json"), encoding="utf-8") as f:
            return int(json.load(f)["serveur"]["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return 5005


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")     # console Windows

    parser = argparse.ArgumentParser(description="Envoie de faux messages pour tester la validation")
    parser.add_argument("--serveur", help='commande du serveur à tester, ex. "serveur_udp.py"')
    parser.add_argument("--hote", default="127.0.0.1")
    parser.add_argument("--port", type=int, help="défaut : port de serveur_config.json ou config.json")
    parser.add_argument("--strict", action="store_true",
                        help="les tests de cohérence (doublon, téléportation...) deviennent bloquants")
    parser.add_argument("--demo", action="store_true",
                        help="envoie seulement les pièges à un serveur déjà lancé")
    parser.add_argument("--pause", type=float, default=0.5, help="pause entre envois en mode --demo (s)")
    args = parser.parse_args()

    liste = catalogue()
    if args.demo:
        demo(liste, args.hote, args.port or port_config(), args.pause)
        return 0

    debut = time.monotonic()
    echecs = tester_hors_reseau(liste)
    if args.serveur:
        e, _ = tester_serveur(liste, construire_commande(args.serveur), args.hote, args.port, args.strict)
        echecs += e
    else:
        print("\n(niveau 2 non lancé : ajouter --serveur serveur_udp.py pour tester le vrai serveur)")

    print(f"\n{'SUCCÈS' if echecs == 0 else 'ÉCHEC'} : {echecs} test(s) en échec "
          f"({time.monotonic() - debut:.1f} s)")
    return 0 if echecs == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
