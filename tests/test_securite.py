"""
test_securite.py - Phase 4 : tests de sécurité de l'API web (web.py).

Le script attaque l'API comme le ferait un utilisateur malveillant et vérifie
qu'elle résiste. Trois familles de tests :

  1. mauvais mot de passe : message identique pour un e-mail inconnu et un
     mauvais mot de passe, blocage après plusieurs échecs, entrées bizarres ;
  2. conducteur qui tente de voir un autre camion : il ne doit jamais recevoir
     les données d'un autre camion, ni passer gestionnaire (en changeant son
     cookie, en s'attribuant un camion, en créant un compte...) ;
  3. injection SQL : des morceaux de SQL dans l'e-mail, le mot de passe,
     l'identifiant de camion et les paramètres ; les tables doivent rester
     intactes et aucun message d'erreur SQL ne doit sortir.

Le script crée lui-même des comptes de test (test-securite-xxxxxx-...@exemple.fr)
et les supprime à la fin. Les camions et les positions ne sont pas modifiés.

À lancer sur la machine où tourne web.py (la VM), depuis la racine du projet :
    python3 -m tests.test_securite                 (lance web.py s'il ne tourne pas déjà)
    python3 -m tests.test_securite --pause 1       (soutenance : une pause entre chaque test)
    python3 -m tests.test_securite --garder        (garde les comptes de test pour regarder)

Dépendances : celles de web.py (flask, bcrypt, pymysql).
Code de retour : 0 si tout passe, 1 sinon.
"""

import argparse
import base64
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from web import comptes
from web.web import charger_config

DOSSIER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # racine du projet
NOM_COOKIE = "flotte_session"

# Ce qui ne doit JAMAIS apparaître dans une réponse : trace Python, erreur SQL...
FUITE = re.compile(r"Traceback|pymysql|mysql|mariadb|SQL syntax|IntegrityError|"
                   r"OperationalError|File \"", re.IGNORECASE)

INJECTIONS_EMAIL = [
    "' OR '1'='1",
    "' OR 1=1 -- ",
    "admin'#",
    "x' UNION SELECT id, email, mot_de_passe, role, camion_id FROM utilisateurs -- ",
    "'; DROP TABLE utilisateurs; --",
]


# ---------------------------------------------------------------------------
# Petit client HTTP qui garde le cookie de session
# ---------------------------------------------------------------------------

class Reponse:
    def __init__(self, statut, corps, entetes, duree):
        self.statut = statut
        self.texte = corps.decode("utf-8", "replace")
        self.entetes = entetes
        self.duree = duree
        try:
            self.json = json.loads(self.texte)
        except ValueError:
            self.json = None

    @property
    def erreur(self):
        return self.json.get("erreur", "") if isinstance(self.json, dict) else ""

    def __str__(self):
        return f"HTTP {self.statut} {self.texte.strip()[:90]}"


class Client:
    toutes_les_reponses = []          # pour vérifier à la fin qu'aucune ne fuit d'info

    def __init__(self, url, cookie=None):
        self.url = url.rstrip("/")
        self.cookie = cookie

    def requete(self, methode, chemin, json_=None, brut=None, type_contenu=None):
        entetes = {}
        donnees = None
        if json_ is not None:
            donnees = json.dumps(json_).encode("utf-8")
            entetes["Content-Type"] = "application/json"
        elif brut is not None:
            donnees = brut
            entetes["Content-Type"] = type_contenu
        if self.cookie:
            entetes["Cookie"] = f"{NOM_COOKIE}={self.cookie}"
        req = urllib.request.Request(self.url + chemin, data=donnees, method=methode, headers=entetes)
        debut = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=20) as rep:
                statut, corps, h = rep.status, rep.read(), rep.headers
        except urllib.error.HTTPError as e:
            statut, corps, h = e.code, e.read(), e.headers
        r = Reponse(statut, corps, h, time.perf_counter() - debut)
        self._lire_cookie(h)
        Client.toutes_les_reponses.append((f"{methode} {chemin}", r))
        return r

    def _lire_cookie(self, entetes):
        for ligne in entetes.get_all("Set-Cookie") or []:
            if not ligne.startswith(NOM_COOKIE + "="):
                continue
            valeur = ligne.split(";", 1)[0].split("=", 1)[1]
            efface = not valeur or "01 Jan 1970" in ligne or "Max-Age=0" in ligne
            self.cookie = None if efface else valeur

    def get(self, chemin):
        return self.requete("GET", chemin)

    def post(self, chemin, json_=None, **kw):
        return self.requete("POST", chemin, json_=json_, **kw)

    def put(self, chemin, json_=None):
        return self.requete("PUT", chemin, json_=json_)

    def connexion(self, email, mdp):
        return self.post("/api/connexion", {"email": email, "mot_de_passe": mdp})


def camions_des_positions(r):
    """Réponse de /api/positions -> ensemble des camions présents (None si pas une réponse valide)."""
    if r.statut != 200 or not isinstance(r.json, dict) or not isinstance(r.json.get("positions"), list):
        return None
    return {p.get("camion_id") for p in r.json["positions"]}


def chemin_camion(camion_id, suite="/positions"):
    return f"/api/camions/{urllib.parse.quote(camion_id, safe='')}{suite}"


def falsifier_cookie(cookie, nouvel_uid):
    """Change "uid" dans un cookie de session Flask en gardant l'ancienne signature :
    c'est ce que tenterait un conducteur pour se faire passer pour le gestionnaire.
    (Le contenu d'un cookie Flask est lisible, seule la signature le protège.)"""
    compresse = cookie.startswith(".")
    charge, reste = (cookie[1:] if compresse else cookie).split(".", 1)
    brut = base64.urlsafe_b64decode(charge + "=" * (-len(charge) % 4))
    if compresse:
        brut = zlib.decompress(brut)
    contenu = json.loads(brut)
    contenu["uid"] = nouvel_uid
    nouveau = json.dumps(contenu, separators=(",", ":")).encode("utf-8")
    if compresse:
        nouveau = zlib.compress(nouveau)
    charge = base64.urlsafe_b64encode(nouveau).rstrip(b"=").decode("ascii")
    return ("." if compresse else "") + charge + "." + reste


# ---------------------------------------------------------------------------
# Déroulement des tests
# ---------------------------------------------------------------------------

class Bilan:
    def __init__(self, pause):
        self.pause = pause
        self.echecs = 0
        self.avertissements = 0
        self.nb = 0

    def section(self, titre):
        print(f"\n== {titre} ==")

    def verifier(self, nom, ok, detail="", optionnel=False):
        self.nb += 1
        if ok:
            statut = "OK"
        elif optionnel:
            statut = "AVERT"
            self.avertissements += 1
        else:
            statut = "ECHEC"
            self.echecs += 1
        detail = str(detail).replace("\n", " ")
        print(f"  {statut:<6} {nom:<62} {detail[:95]}")
        if self.pause:
            time.sleep(self.pause)
        return ok


def lancer_tests(url, conf, b, garder):
    conf_bdd = conf["bdd"]
    max_echecs = conf["securite"]["echecs_avant_blocage"]
    jeton = secrets.token_hex(3)
    prefixe = f"test-securite-{jeton}"
    mdp = "Mdp-de-test-" + secrets.token_urlsafe(9)

    def email(nom):
        return f"{prefixe}-{nom}@exemple.fr"

    # --- préparation : deux camions, des comptes de test
    cnx = comptes.connecter(conf_bdd)
    try:
        with cnx.cursor() as cur:
            cur.execute("SELECT id FROM camions ORDER BY id LIMIT 2")
            ids = [l[0] for l in cur.fetchall()]
            cur.execute("SELECT (SELECT COUNT(*) FROM camions), (SELECT COUNT(*) FROM positions)")
            nb_camions, nb_positions = cur.fetchone()
        cnx.commit()
    finally:
        cnx.close()
    if len(ids) < 2:
        print("il faut au moins 2 camions dans la table camions (relancer creer_bdd.sh)")
        return
    mon_camion, autre_camion = ids

    print(f"préparation : comptes de test {prefixe}-...@exemple.fr "
          f"(conducteur de {mon_camion}, l'autre camion est {autre_camion})")
    uid = {}
    for nom, role, camion in [("conducteur", "conducteur", mon_camion),
                              ("gestionnaire", "gestionnaire", None),
                              ("essais", "conducteur", mon_camion),
                              ("blocage", "conducteur", mon_camion),
                              ("bizarre", "conducteur", mon_camion)]:
        uid[nom] = comptes.inscrire(conf_bdd, email(nom), mdp, role, camion)

    try:
        _tests(url, b, email, mdp, uid, mon_camion, autre_camion, max_echecs,
               conf_bdd, nb_camions, nb_positions)
    finally:
        if garder:
            print(f"\ncomptes de test gardés ({prefixe}-...), mot de passe : {mdp}")
        else:
            cnx = comptes.connecter(conf_bdd)
            try:
                with cnx.cursor() as cur:
                    n = cur.execute("DELETE FROM utilisateurs WHERE email LIKE %s", (prefixe + "%",))
                cnx.commit()
            finally:
                cnx.close()
            print(f"\nnettoyage : {n} compte(s) de test supprimé(s)")


def _tests(url, b, email, mdp, uid, mon_camion, autre_camion, max_echecs,
           conf_bdd, nb_camions, nb_positions):

    # =====================================================================
    b.section("1. Mauvais mot de passe")
    # =====================================================================
    c = Client(url)
    r = c.connexion(email("conducteur"), mdp)
    b.verifier("référence : bon e-mail + bon mot de passe -> accepté",
               r.statut == 200 and c.cookie, r)

    r_faux = Client(url).connexion(email("essais"), mdp + "x")
    b.verifier("mauvais mot de passe -> 401", r_faux.statut == 401, r_faux)

    r_inconnu = Client(url).connexion(email("inconnu-" + secrets.token_hex(3)), mdp)
    b.verifier("e-mail inconnu -> 401", r_inconnu.statut == 401, r_inconnu)
    b.verifier("même message pour « e-mail inconnu » et « mauvais mot de passe »",
               r_faux.erreur and r_faux.erreur == r_inconnu.erreur,
               f"« {r_faux.erreur} » / « {r_inconnu.erreur} »")
    b.verifier("aucun cookie de session donné après un échec",
               not any(NOM_COOKIE in (l or "") for l in (r_faux.entetes.get_all("Set-Cookie") or [])),
               "pas de Set-Cookie" if r_faux.statut == 401 else r_faux)

    r = Client(url).connexion(email("bizarre"), "")
    b.verifier("mot de passe vide -> 401", r.statut == 401, r)
    r = Client(url).connexion(email("bizarre"), "A" * 5000)
    b.verifier("mot de passe de 5000 caractères -> refusé sans planter", r.statut in (400, 401, 413), r)
    r = Client(url).post("/api/connexion", {"email": email("bizarre"), "mot_de_passe": 123456})
    b.verifier("mot de passe qui n'est pas un texte (nombre) -> 400", r.statut == 400, r)
    r = Client(url).post("/api/connexion", {"email": email("bizarre"), "mot_de_passe": None})
    b.verifier("mot de passe null -> 400", r.statut == 400, r)
    r = Client(url).post("/api/connexion", {"email": email("bizarre")})
    b.verifier("champ mot_de_passe absent -> 400", r.statut == 400, r)
    r = Client(url).connexion(email("conducteur").upper(), mdp)
    b.verifier("e-mail en majuscules + bon mot de passe -> accepté (même compte)", r.statut == 200, r)

    # Blocage : N échecs, puis même le BON mot de passe est refusé
    statuts = [Client(url).connexion(email("blocage"), f"faux-{i}").statut for i in range(max_echecs)]
    r = Client(url).connexion(email("blocage"), mdp)
    b.verifier(f"{max_echecs} échecs puis le BON mot de passe -> bloqué (429)",
               r.statut == 429 and all(s == 401 for s in statuts),
               f"échecs : {statuts} puis {r}")

    # Temps de réponse : un e-mail inconnu doit coûter autant qu'un mauvais mot de passe,
    # sinon on peut deviner quels e-mails ont un compte (bcrypt calculé dans les deux cas)
    t_faux = [Client(url).connexion(email("essais"), f"faux-t{i}").duree for i in range(3)]
    t_inconnu = [Client(url).connexion(email(f"absent-{secrets.token_hex(3)}"), "faux").duree
                 for _ in range(3)]
    m1, m2 = statistics.median(t_faux), statistics.median(t_inconnu)
    b.verifier("même temps de réponse : e-mail inconnu / mauvais mot de passe",
               0.5 <= m1 / m2 <= 2.0, f"{m1 * 1000:.0f} ms / {m2 * 1000:.0f} ms", optionnel=True)

    # =====================================================================
    b.section("2. Sans session, session falsifiée ou volée")
    # =====================================================================
    anonyme = Client(url)
    for chemin in ("/api/moi", "/api/camions", chemin_camion(mon_camion), "/api/positions",
                   "/api/utilisateurs"):
        r = anonyme.get(chemin)
        b.verifier(f"sans connexion : GET {chemin} -> 401", r.statut == 401, r)

    conducteur = Client(url)
    r = conducteur.connexion(email("conducteur"), mdp)
    set_cookie = " ".join(r.entetes.get_all("Set-Cookie") or [])
    b.verifier("cookie de session HttpOnly (illisible par le JavaScript)", "HttpOnly" in set_cookie,
               set_cookie[:95])
    b.verifier("cookie de session SameSite=Strict (protection CSRF)", "SameSite=Strict" in set_cookie,
               set_cookie[:95])

    r = Client(url).post("/api/connexion", brut=urllib.parse.urlencode(
        {"email": email("conducteur"), "mot_de_passe": mdp}).encode(),
        type_contenu="application/x-www-form-urlencoded")
    b.verifier("connexion envoyée par un formulaire HTML (CSRF) -> 415", r.statut == 415, r)

    try:
        faux = falsifier_cookie(conducteur.cookie, uid["gestionnaire"])
        detail_faux = "uid du gestionnaire mis dans le cookie du conducteur"
    except Exception:
        faux = conducteur.cookie[:-2] + ("AA" if not conducteur.cookie.endswith("AA") else "BB")
        detail_faux = "cookie modifié"
    pirate = Client(url, cookie=faux)
    r = pirate.get("/api/utilisateurs")
    b.verifier("cookie modifié pour devenir gestionnaire -> refusé (401)", r.statut == 401,
               f"{detail_faux} : {r}")
    r = pirate.get("/api/moi")
    b.verifier("cookie modifié : /api/moi ne connaît personne", r.statut == 401, r)

    vole = Client(url)
    vole.connexion(email("conducteur"), mdp)
    copie = vole.cookie
    vole.post("/api/deconnexion")
    r = Client(url, cookie=copie).get("/api/moi")
    b.verifier("cookie copié puis déconnexion : la copie ne marche plus", r.statut == 401, r)

    # =====================================================================
    b.section(f"3. Conducteur de {mon_camion} qui tente de voir un autre camion")
    # =====================================================================
    r = conducteur.get("/api/moi")
    b.verifier(f"/api/moi : conducteur rattaché à {mon_camion}",
               r.statut == 200 and r.json.get("camion_id") == mon_camion, r)

    r = conducteur.get("/api/camions")
    vus = [l.get("camion_id") for l in r.json] if isinstance(r.json, list) else None
    b.verifier(f"/api/camions ne renvoie que {mon_camion}", r.statut == 200 and vus == [mon_camion],
               f"camions reçus : {vus}")
    r = conducteur.get(f"/api/camions?camion_id={autre_camion}")
    vus = [l.get("camion_id") for l in r.json] if isinstance(r.json, list) else None
    b.verifier(f"/api/camions?camion_id={autre_camion} : paramètre ignoré",
               vus == [mon_camion], f"camions reçus : {vus}")

    for suite, nom in (("", ""), (f"&camion_id={autre_camion}", f" (avec ?camion_id={autre_camion})")):
        r = conducteur.get("/api/positions?minutes=1440" + suite)
        vus = camions_des_positions(r)
        b.verifier(f"/api/positions : seulement {mon_camion}{nom}",
                   r.statut == 200 and vus is not None and vus <= {mon_camion},
                   f"camions reçus : {sorted(vus) if vus is not None else r}")

    r = conducteur.get(chemin_camion(mon_camion))
    b.verifier(f"historique de {mon_camion} (son camion) -> 200", r.statut == 200, r)
    r = conducteur.get(chemin_camion(autre_camion))
    b.verifier(f"historique de {autre_camion} -> 403", r.statut == 403, r)
    r = conducteur.get(chemin_camion(autre_camion.lower()))
    b.verifier(f"historique de {autre_camion.lower()} (minuscules) -> refusé",
               r.statut in (400, 403), r)
    r_existe_pas = conducteur.get(chemin_camion("CAM-INEXISTANT"))
    b.verifier("camion qui n'existe pas -> même réponse (403) : la flotte n'est pas révélée",
               r_existe_pas.statut == 403 and r_existe_pas.erreur == conducteur.get(
                   chemin_camion(autre_camion)).erreur, r_existe_pas)
    r = conducteur.get(chemin_camion(autre_camion, "/positions?minutes=1440"))
    b.verifier(f"historique de {autre_camion} sur 24 h -> 403", r.statut == 403, r)

    r = conducteur.get("/api/utilisateurs")
    b.verifier("liste des comptes -> 403 (réservé gestionnaire)", r.statut == 403, r)
    r = conducteur.post("/api/utilisateurs", {"email": email("cree-par-conducteur"),
                                              "mot_de_passe": mdp, "camion_id": autre_camion})
    b.verifier(f"créer un compte conducteur de {autre_camion} -> 403", r.statut == 403, r)
    r = conducteur.put(f"/api/utilisateurs/{uid['conducteur']}/camion", {"camion_id": autre_camion})
    b.verifier(f"s'attribuer {autre_camion} lui-même -> 403", r.statut == 403, r)
    r = conducteur.get("/api/moi")
    b.verifier(f"après ces tentatives, toujours rattaché à {mon_camion}",
               r.statut == 200 and r.json.get("camion_id") == mon_camion, r)
    r = conducteur.get(chemin_camion(autre_camion))
    b.verifier(f"et toujours aucun accès à {autre_camion}", r.statut == 403, r)

    # =====================================================================
    b.section("4. Injection SQL")
    # =====================================================================
    for piege in INJECTIONS_EMAIL:
        essai = Client(url)
        r = essai.connexion(piege, "' OR '1'='1")
        b.verifier(f"connexion avec e-mail {piege[:40]!r}", r.statut == 401 and not essai.cookie, r)
    r = Client(url).connexion(email("conducteur"), "' OR '1'='1' -- ")
    b.verifier("bon e-mail + mot de passe « ' OR '1'='1' -- » -> 401", r.statut == 401, r)

    for piege in (f"{mon_camion}' OR '1'='1", f"{autre_camion}' -- ", f"{mon_camion};DROP TABLE positions",
                  f"{mon_camion}' UNION SELECT * FROM utilisateurs -- "):
        r = conducteur.get(chemin_camion(piege))
        b.verifier(f"identifiant de camion {piege[:42]!r} -> 400", r.statut == 400, r)
    for piege in ("' OR '1'='1", f"{mon_camion}' OR camion_id LIKE '%"):
        r = conducteur.get("/api/camions?camion_id=" + urllib.parse.quote(piege))
        vus = [l.get("camion_id") for l in r.json] if isinstance(r.json, list) else None
        b.verifier(f"/api/camions?camion_id={piege[:30]!r} -> toujours {mon_camion} seul",
                   vus == [mon_camion], f"camions reçus : {vus}")
    for piege in ("60 OR 1=1", "1; DROP TABLE positions", "1)--", "-1", "99999999999999999999"):
        r = conducteur.get(chemin_camion(mon_camion, "/positions?minutes=" + urllib.parse.quote(piege)))
        b.verifier(f"paramètre minutes={piege!r} -> 400", r.statut == 400, r)

    for nom, piege in (("minutes", "10 OR 1=1"), ("minutes", "10)--"), ("apres", "0 OR 1=1"),
                       ("apres", "-1"), ("apres", "1;DROP TABLE positions")):
        r = conducteur.get(f"/api/positions?{nom}=" + urllib.parse.quote(piege))
        b.verifier(f"/api/positions?{nom}={piege!r} -> 400", r.statut == 400, r)

    gestionnaire = Client(url)
    r = gestionnaire.connexion(email("gestionnaire"), mdp)
    b.verifier("le gestionnaire de test se connecte", r.statut == 200, r)

    # Le filtre de /api/positions sert-il vraiment ? Seulement vérifiable s'il y a des
    # positions récentes d'un autre camion (simulateur lancé) : sinon avertissement.
    vus_gestionnaire = camions_des_positions(gestionnaire.get("/api/positions?minutes=1440")) or set()
    vus_conducteur = camions_des_positions(conducteur.get("/api/positions?minutes=1440")) or set()
    autres = vus_gestionnaire - {mon_camion}
    if autres:
        b.verifier(f"/api/positions : le gestionnaire voit {len(vus_gestionnaire)} camion(s), "
                   f"le conducteur aucun autre que {mon_camion}",
                   not (vus_conducteur & autres), f"gestionnaire : {sorted(vus_gestionnaire)}, "
                   f"conducteur : {sorted(vus_conducteur)}")
    else:
        b.verifier("/api/positions : positions récentes d'autres camions pour comparer", False,
                   "aucune sur 24 h : lancer le simulateur pour que ce test compte", optionnel=True)

    email_piege = email("x');DROP/**/TABLE/**/utilisateurs;--")
    r = gestionnaire.post("/api/utilisateurs", {"email": email_piege, "mot_de_passe": mdp,
                                                "camion_id": mon_camion})
    liste = gestionnaire.get("/api/utilisateurs")
    emails = [u.get("email") for u in liste.json] if isinstance(liste.json, list) else []
    b.verifier("compte créé avec un e-mail contenant du SQL : enregistré tel quel",
               r.statut == 201 and email_piege.lower() in emails, r)

    r = gestionnaire.put(f"/api/utilisateurs/{uid['essais']}/camion",
                         {"camion_id": f"{autre_camion}' OR '1'='1"})
    b.verifier("attribuer le camion « ...' OR '1'='1 » -> 400", r.statut == 400, r)
    r = gestionnaire.post("/api/utilisateurs", {"email": email("role"), "mot_de_passe": mdp,
                                                "camion_id": mon_camion, "role": "gestionnaire"})
    liste = gestionnaire.get("/api/utilisateurs")
    role = next((u.get("role") for u in (liste.json or []) if u.get("email") == email("role")), None)
    b.verifier("champ « role: gestionnaire » ajouté à la création -> ignoré (conducteur)",
               r.statut == 201 and role == "conducteur", f"rôle enregistré : {role}")

    r = gestionnaire.get("/api/camions")
    b.verifier(f"le gestionnaire voit toute la flotte ({nb_camions} camions)",
               isinstance(r.json, list) and len(r.json) == nb_camions,
               f"{len(r.json) if isinstance(r.json, list) else r} camion(s)")

    # Les tables sont-elles intactes ? (vérifié directement dans la base)
    cnx = comptes.connecter(conf_bdd)
    try:
        with cnx.cursor() as cur:
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")
            tables = sorted(l[0] for l in cur.fetchall())
            cur.execute("SELECT (SELECT COUNT(*) FROM camions), (SELECT COUNT(*) FROM positions)")
            apres = cur.fetchone()
        cnx.commit()
    finally:
        cnx.close()
    b.verifier("les tables existent toujours (camions, positions, utilisateurs)",
               {"camions", "positions", "utilisateurs"} <= set(tables), ", ".join(tables))
    # (des positions peuvent s'AJOUTER pendant le test si le simulateur tourne)
    b.verifier("camions inchangés et aucune position supprimée",
               apres[0] == nb_camions and apres[1] >= nb_positions,
               f"camions {nb_camions} -> {apres[0]}, positions {nb_positions} -> {apres[1]}")

    # =====================================================================
    b.section("5. Rien ne fuit dans les réponses")
    # =====================================================================
    erreurs_500 = [(q, r) for q, r in Client.toutes_les_reponses if r.statut >= 500]
    b.verifier("aucune erreur 500 pendant les tests", not erreurs_500,
               f"{len(erreurs_500)} : {erreurs_500[0][0]} -> {erreurs_500[0][1]}" if erreurs_500
               else f"{len(Client.toutes_les_reponses)} requêtes")
    fuites = [(q, r) for q, r in Client.toutes_les_reponses if FUITE.search(r.texte)]
    b.verifier("aucune trace Python ni erreur SQL dans les réponses", not fuites,
               f"{fuites[0][0]} -> {fuites[0][1]}" if fuites else "")
    sans_entetes = [q for q, r in Client.toutes_les_reponses
                    if r.entetes.get("X-Content-Type-Options") != "nosniff"
                    or r.entetes.get("Cache-Control") != "no-store"]
    b.verifier("en-têtes de sécurité sur toutes les réponses (nosniff, no-store)", not sans_entetes,
               sans_entetes[0] if sans_entetes else "")
    r = Client(url).get("/api/moi")
    b.verifier("l'API répond toujours normalement après tous les tests", r.statut == 401, r)


# ---------------------------------------------------------------------------

def api_repond(url):
    try:
        Client(url).get("/api/moi")
        return True
    except (urllib.error.URLError, OSError):
        return False


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Tests de sécurité de l'API web")
    parser.add_argument("-c", "--config", default="web/web_config.json")
    parser.add_argument("--url", help="adresse de l'API (défaut : celle de web_config.json)")
    parser.add_argument("--pause", type=float, default=0, help="pause entre les tests (s), pour la soutenance")
    parser.add_argument("--garder", action="store_true", help="ne pas supprimer les comptes de test")
    args = parser.parse_args()

    try:
        conf = charger_config(args.config)
    except ValueError as e:
        print(f"configuration : {e}", file=sys.stderr)
        return 2
    url = args.url or f"http://{conf['ecoute']['hote']}:{conf['ecoute']['port']}"

    proc = None
    if not api_repond(url):
        print(f"l'API ne répond pas sur {url} : lancement de web.py pour le test")
        proc = subprocess.Popen([sys.executable, "-m", "web.web", "-c", args.config], cwd=DOSSIER,
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for _ in range(50):
            time.sleep(0.2)
            if api_repond(url) or proc.poll() is not None:
                break
        if not api_repond(url):
            print("impossible de lancer web.py (lancer « python3 -m web.web » pour voir l'erreur)")
            return 1

    b = Bilan(args.pause)
    debut = time.monotonic()
    try:
        lancer_tests(url, conf, b, args.garder)
    except comptes.ErreurBase as e:
        print(f"base inaccessible : {e}")
        return 1
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=5)

    print(f"\n{'SUCCÈS' if b.echecs == 0 else 'ÉCHEC'} : {b.nb} tests, {b.echecs} échec(s), "
          f"{b.avertissements} avertissement(s) ({time.monotonic() - debut:.1f} s)")
    return 0 if b.echecs == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
