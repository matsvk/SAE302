/*
 * carte.js - Phase 5 : carte Leaflet des camions, mise à jour toutes les 3 s.
 *
 * Toutes les 3 s, la page interroge l'API, qui ne renvoie que les camions
 * VISIBLES par le compte connecté :
 *   - gestionnaire : toute la flotte ;
 *   - conducteur   : uniquement son camion (le filtre est fait par le serveur,
 *                    dans la requête SQL : rien n'est caché côté navigateur).
 *
 * P2 - GET /api/camions : dernière position de chaque camion. Chaque camion a un
 * marqueur dont la couleur dépend de son état, avec une flèche tournée dans sa
 * direction (cap). Un camion dont le serveur n'a rien reçu depuis 30 s est
 * affiché "hors ligne" (grisé).
 *
 * P3 - GET /api/positions : trajet récent de chaque camion (trait sur la carte).
 * Le premier appel charge les N dernières minutes ; ensuite la page envoie
 * ?apres=<dernier_id> et ne reçoit que les nouvelles positions, qu'elle ajoute
 * au bout du trait. Les points plus vieux que N minutes sont retirés.
 *
 * P4 - deux vues sur la même page, selon le rôle renvoyé par /api/moi :
 *   - gestionnaire : compteurs par état (qui servent aussi de filtres), recherche
 *     par camion, plaque ou conducteur, nom du conducteur de chaque camion
 *     (GET /api/utilisateurs, réservé au gestionnaire par le serveur) ;
 *   - conducteur : tableau de bord de SON camion (vitesse, état, direction,
 *     dernier message, distance parcourue sur le trajet affiché).
 * Choisir la vue dans le navigateur n'est qu'une question d'affichage : un
 * conducteur qui afficherait la vue gestionnaire ne recevrait toujours que son
 * camion, et /api/utilisateurs lui répondrait 403.
 *
 * Sécurité : les textes venus du serveur sont toujours affichés avec
 * textContent (jamais innerHTML), y compris dans les marqueurs et les bulles.
 */

"use strict";

const PERIODE_MS = 3000;          // même rythme que l'envoi des camions
const HORS_LIGNE_S = 30;          // même valeur que delai_silence du serveur UDP
const CENTRE_DEPART = [48.86, 2.35];   // Paris, avant la première position
const ZOOM_DEPART = 10;
const DUREE_TRACE_DEFAUT = 10;    // minutes de trajet affichées (0 = aucun)
const MAX_APPELS_POSITIONS = 10;  // si l'historique est long, il arrive en plusieurs morceaux
const PERIODE_CONDUCTEURS_MS = 60000;   // la liste des comptes change rarement

const NOMS_ETATS = {
    a_quai: "À quai",
    en_route: "En route",
    livraison: "Livraison",
    termine: "Terminé",
    hors_ligne: "Hors ligne",
};
const POINTS_CARDINAUX = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"];

let carte = null;
let compte = null;
let estGestionnaire = false;
const marqueurs = {};             // id du camion -> marqueur Leaflet
let premierCadrage = true;        // cadrer la carte sur les camions au 1er chargement
let camionChoisi = null;          // camion cliqué dans la liste (gestionnaire)
let derniersCamions = [];         // dernière réponse de /api/camions

const traces = {};                // id du camion -> {ligne: trait Leaflet, points: [{t, lat, lon}]}
let dernierId = 0;                // curseur de /api/positions : dernière ligne déjà reçue
let minutesTrace = DUREE_TRACE_DEFAUT;
let generationTraces = 0;         // change à chaque remise à zéro (réponses en retard ignorées)
let camionsConnus = null;         // ids des camions visibles au rafraîchissement précédent

let filtreCategorie = null;       // gestionnaire : "en_route", "livraison", "a_quai", "hors_ligne" ou null
let texteRecherche = "";
let conducteursParCamion = {};    // gestionnaire : id du camion -> [e-mails des conducteurs]
let prochainChargementConducteurs = 0;

// ---------------------------------------------------------------------------
// Petites fonctions d'affichage
// ---------------------------------------------------------------------------

/** Secondes écoulées depuis la dernière réception par le serveur. */
function ageSecondes(camion) {
    if (!camion.recu_le) {
        return Infinity;
    }
    return (Date.now() - Date.parse(camion.recu_le)) / 1000;
}

function estHorsLigne(camion) {
    return ageSecondes(camion) > HORS_LIGNE_S;
}

function aUnePosition(camion) {
    return camion.lat !== null && camion.lat !== undefined;
}

/** Catégorie affichée : l'état envoyé par le camion, sauf s'il ne répond plus.
 *  "termine" (fin de service) est rangé avec "à quai" : le camion est arrêté au dépôt. */
function categorie(camion) {
    if (!aUnePosition(camion) || estHorsLigne(camion)) {
        return "hors_ligne";
    }
    return camion.etat === "termine" ? "a_quai" : camion.etat;
}

function nomEtat(camion) {
    if (!aUnePosition(camion)) {
        return "Aucune position";
    }
    return estHorsLigne(camion) ? NOMS_ETATS.hors_ligne : (NOMS_ETATS[camion.etat] || camion.etat);
}

/** "il y a 5 s", "il y a 3 min", "il y a 2 h"... */
function depuis(camion) {
    const s = ageSecondes(camion);
    if (!isFinite(s)) {
        return "jamais";
    }
    if (s < 60) {
        return "il y a " + Math.max(0, Math.round(s)) + " s";
    }
    if (s < 3600) {
        return "il y a " + Math.round(s / 60) + " min";
    }
    if (s < 86400) {
        return "il y a " + Math.round(s / 3600) + " h";
    }
    return "le " + new Date(camion.recu_le).toLocaleDateString("fr-FR");
}

function heureLocale(texteIso) {
    return texteIso ? new Date(texteIso).toLocaleTimeString("fr-FR") : "-";
}

/** 213 -> "213° SO" */
function direction(cap) {
    const c = Number(cap) || 0;
    return Math.round(c) + "° " + POINTS_CARDINAUX[Math.round(c / 45) % 8];
}

/** Distance en km entre deux points (formule de haversine, comme mouvement.py). */
function distanceKm(a, b) {
    const rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad;
    const dLon = (b.lon - a.lon) * rad;
    const h = Math.sin(dLat / 2) ** 2
        + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Crée un élément avec une classe et un texte (toujours via textContent). */
function element(balise, classe, texte) {
    const e = document.createElement(balise);
    if (classe) {
        e.className = classe;
    }
    if (texte !== undefined) {
        e.textContent = texte;
    }
    return e;
}

function classeEtat(camion) {
    const cat = categorie(camion);
    return cat === "hors_ligne" ? "hors-ligne" : "etat-" + camion.etat;
}

/** Texte "jean@exemple.fr" (ou "2 conducteurs", "aucun conducteur") pour un camion. */
function texteConducteurs(id) {
    const liste = conducteursParCamion[id] || [];
    if (liste.length === 0) {
        return "Aucun conducteur";
    }
    return liste.length === 1 ? liste[0] : liste[0] + " (+" + (liste.length - 1) + ")";
}

/** Filtres du gestionnaire : état (tuile) et texte recherché. */
function correspond(camion) {
    if (!estGestionnaire) {
        return true;
    }
    if (filtreCategorie !== null && categorie(camion) !== filtreCategorie) {
        return false;
    }
    if (texteRecherche === "") {
        return true;
    }
    const cherche = [camion.camion_id, camion.immatriculation || ""]
        .concat(conducteursParCamion[camion.camion_id] || [])
        .join(" ").toLowerCase();
    return cherche.includes(texteRecherche);
}

// ---------------------------------------------------------------------------
// Marqueurs
// ---------------------------------------------------------------------------

/** Icône du marqueur : une pastille colorée avec une flèche (cap) et le nom du camion. */
function creerIcone(camion) {
    const bloc = element("div", "marqueur " + classeEtat(camion)
        + (camion.camion_id === camionChoisi ? " choisi" : ""));
    const fleche = element("div", "marqueur-fleche");
    // le cap est appliqué par JavaScript (CSSOM) : autorisé par la CSP,
    // contrairement à un attribut style="..." écrit dans le HTML
    fleche.style.transform = "rotate(" + Number(camion.cap || 0) + "deg)";
    bloc.appendChild(fleche);
    bloc.appendChild(element("span", "marqueur-nom", camion.camion_id));
    return L.divIcon({
        html: bloc,
        className: "icone-camion",      // remplace le style par défaut de Leaflet
        iconSize: [30, 30],
        iconAnchor: [15, 15],
        popupAnchor: [0, -16],
    });
}

/** Contenu de la bulle qui s'ouvre au clic sur un marqueur. */
function creerBulle(camion) {
    const bloc = element("div", "bulle");
    const titre = element("div", "bulle-titre");
    titre.appendChild(element("b", "", camion.camion_id));
    if (camion.immatriculation) {
        titre.appendChild(element("span", "", " · " + camion.immatriculation));
    }
    titre.appendChild(element("span", "badge " + classeEtat(camion), nomEtat(camion)));
    bloc.appendChild(titre);

    const lignes = [
        ["Vitesse", Number(camion.vitesse).toFixed(1) + " km/h"],
        ["Direction", direction(camion.cap)],
        ["Position", Number(camion.lat).toFixed(5) + ", " + Number(camion.lon).toFixed(5)],
        ["Reçu", heureLocale(camion.recu_le) + " (" + depuis(camion) + ")"],
    ];
    if (estGestionnaire) {
        lignes.push(["Conducteur", texteConducteurs(camion.camion_id)]);
    }
    const table = element("table", "bulle-table");
    for (const [nom, valeur] of lignes) {
        const tr = document.createElement("tr");
        tr.appendChild(element("th", "", nom));
        tr.appendChild(element("td", "", valeur));
        table.appendChild(tr);
    }
    bloc.appendChild(table);
    return bloc;
}

function mettreAJourMarqueur(camion) {
    const position = [Number(camion.lat), Number(camion.lon)];
    let m = marqueurs[camion.camion_id];
    if (m === undefined) {
        m = L.marker(position, { title: camion.camion_id, keyboard: true });
        m.bindPopup(creerBulle(camion));
        marqueurs[camion.camion_id] = m;
    } else {
        m.setLatLng(position);
        m.setPopupContent(creerBulle(camion));
    }
    m.setIcon(creerIcone(camion));
    m.setZIndexOffset(estHorsLigne(camion) ? 0 : 1000);   // camions actifs au-dessus
}

/** Retire les marqueurs des camions qui ne sont plus dans la réponse. */
function retirerAbsents(ids) {
    for (const id of Object.keys(marqueurs)) {
        if (!ids.has(id)) {
            carte.removeLayer(marqueurs[id]);
            delete marqueurs[id];
        }
    }
}

/** Affiche sur la carte seulement les camions qui passent les filtres. */
function appliquerFiltreCarte() {
    const parId = {};
    for (const c of derniersCamions) {
        parId[c.camion_id] = c;
    }
    for (const id of Object.keys(marqueurs)) {
        const visible = parId[id] !== undefined && correspond(parId[id]);
        if (visible && !carte.hasLayer(marqueurs[id])) {
            marqueurs[id].addTo(carte);
        } else if (!visible && carte.hasLayer(marqueurs[id])) {
            carte.removeLayer(marqueurs[id]);
        }
    }
    for (const id of Object.keys(traces)) {
        const trait = traces[id].ligne.getElement();
        if (trait) {
            trait.classList.toggle("trace-masquee", parId[id] === undefined || !correspond(parId[id]));
        }
    }
}

// ---------------------------------------------------------------------------
// Trajets (Phase 5 P3)
// ---------------------------------------------------------------------------

/** Efface tous les trajets ; le prochain rafraîchissement les recharge en entier. */
function viderTraces() {
    for (const id of Object.keys(traces)) {
        carte.removeLayer(traces[id].ligne);
        delete traces[id];
    }
    dernierId = 0;
    generationTraces += 1;
}

function ajouterPoint(p) {
    let trace = traces[p.camion_id];
    if (trace === undefined) {
        trace = {
            points: [],
            ligne: L.polyline([], { className: "trace", interactive: false }).addTo(carte),
        };
        traces[p.camion_id] = trace;
    }
    const point = { t: Date.parse(p.horodatage), lat: Number(p.lat), lon: Number(p.lon) };
    // presque toujours à la fin ; sinon (message arrivé en retard) à sa place
    let i = trace.points.length;
    while (i > 0 && trace.points[i - 1].t > point.t) {
        i -= 1;
    }
    trace.points.splice(i, 0, point);
}

/** Demande les nouvelles positions à l'API. Renvoie le statut HTTP (200 si tout va bien). */
async function chargerTraces() {
    const generation = generationTraces;
    for (let appel = 0; appel < MAX_APPELS_POSITIONS; appel++) {
        const r = await api("GET", "/api/positions?minutes=" + minutesTrace + "&apres=" + dernierId);
        if (generation !== generationTraces) {
            return 200;                      // réglage changé entre-temps : réponse périmée
        }
        if (r.statut !== 200 || !Array.isArray(r.donnees.positions)) {
            return r.statut;
        }
        for (const p of r.donnees.positions) {
            ajouterPoint(p);
        }
        dernierId = r.donnees.dernier_id;
        if (r.donnees.complet) {
            break;
        }
    }
    return 200;
}

/** Coupe les trajets à N minutes et met à jour les traits sur la carte. */
function dessinerTraces(camionsParId) {
    const duree = minutesTrace * 60 * 1000;
    for (const id of Object.keys(traces)) {
        const trace = traces[id];
        if (trace.points.length > 0) {
            // N minutes avant la position la plus récente du camion (son horodatage à lui :
            // reste juste si le simulateur est accéléré ou si l'horloge du PC est fausse)
            const limite = trace.points[trace.points.length - 1].t - duree;
            let debut = 0;
            while (debut < trace.points.length && trace.points[debut].t < limite) {
                debut += 1;
            }
            trace.points.splice(0, debut);
        }
        trace.ligne.setLatLngs(trace.points.map(function (p) { return [p.lat, p.lon]; }));
        const camion = camionsParId[id];
        const trait = trace.ligne.getElement();
        if (trait) {
            trait.classList.toggle("trace-hors-ligne", !camion || estHorsLigne(camion));
        }
    }
}

/** Longueur (km) et durée (min) du trajet affiché d'un camion, ou null. */
function bilanTrace(id) {
    const trace = traces[id];
    if (!trace || trace.points.length < 2) {
        return null;
    }
    let km = 0;
    for (let i = 1; i < trace.points.length; i++) {
        km += distanceKm(trace.points[i - 1], trace.points[i]);
    }
    const minutes = (trace.points[trace.points.length - 1].t - trace.points[0].t) / 60000;
    return { km: km, minutes: minutes };
}

function memesCamions(a, b) {
    return a.size === b.size && [...a].every(function (id) { return b.has(id); });
}

// ---------------------------------------------------------------------------
// Vue gestionnaire : compteurs, filtres, liste
// ---------------------------------------------------------------------------

/** Liste des comptes (gestionnaire seulement) : qui conduit quel camion. */
async function chargerConducteurs() {
    if (!estGestionnaire || Date.now() < prochainChargementConducteurs) {
        return;
    }
    prochainChargementConducteurs = Date.now() + PERIODE_CONDUCTEURS_MS;
    const r = await api("GET", "/api/utilisateurs");
    if (r.statut !== 200 || !Array.isArray(r.donnees)) {
        return;                              // pas grave : la carte marche sans les noms
    }
    const parCamion = {};
    for (const u of r.donnees) {
        if (u.role === "conducteur" && u.camion_id) {
            (parCamion[u.camion_id] = parCamion[u.camion_id] || []).push(u.email);
        }
    }
    conducteursParCamion = parCamion;
}

function afficherTuiles(camions) {
    const nombres = { en_route: 0, livraison: 0, a_quai: 0, hors_ligne: 0 };
    for (const c of camions) {
        nombres[categorie(c)] = (nombres[categorie(c)] || 0) + 1;
    }
    for (const cat of Object.keys(nombres)) {
        document.getElementById("nb-" + cat).textContent = nombres[cat];
    }
    for (const tuile of document.querySelectorAll(".tuile")) {
        const active = tuile.dataset.categorie === filtreCategorie;
        tuile.classList.toggle("active", active);
        tuile.setAttribute("aria-pressed", active ? "true" : "false");
    }
    const filtreActif = document.getElementById("filtre-actif");
    filtreActif.hidden = filtreCategorie === null && texteRecherche === "";
    const morceaux = [];
    if (filtreCategorie !== null) {
        morceaux.push(NOMS_ETATS[filtreCategorie]);
    }
    if (texteRecherche !== "") {
        morceaux.push("« " + texteRecherche + " »");
    }
    document.getElementById("filtre-texte").textContent = "Filtre : " + morceaux.join(", ");
}

function choisirCamion(camion) {
    camionChoisi = camion.camion_id;
    carte.setView([Number(camion.lat), Number(camion.lon)], Math.max(carte.getZoom(), 14));
    const m = marqueurs[camion.camion_id];
    if (m && carte.hasLayer(m)) {
        m.openPopup();
    }
    afficherVue();
}

function afficherListe(camions) {
    const liste = document.getElementById("liste-camions");
    liste.textContent = "";
    const affiches = camions.filter(correspond);
    for (const camion of affiches) {
        const li = element("li", "camion" + (camion.camion_id === camionChoisi ? " choisi" : ""));
        const avecPosition = aUnePosition(camion);

        const haut = element("div", "camion-haut");
        haut.appendChild(element("i", "pastille " + classeEtat(camion)));
        haut.appendChild(element("b", "", camion.camion_id));
        if (camion.immatriculation) {
            haut.appendChild(element("span", "camion-immat", camion.immatriculation));
        }
        li.appendChild(haut);

        let detail;
        if (!avecPosition) {
            detail = "Aucune position reçue";
        } else if (estHorsLigne(camion)) {
            detail = "Hors ligne · dernière position " + depuis(camion);
        } else {
            detail = nomEtat(camion) + " · " + Number(camion.vitesse).toFixed(0) + " km/h · " + depuis(camion);
        }
        li.appendChild(element("div", "camion-detail", detail));
        li.appendChild(element("div", "camion-conducteur", texteConducteurs(camion.camion_id)));

        if (avecPosition) {
            li.tabIndex = 0;
            li.setAttribute("role", "button");
            li.addEventListener("click", function () { choisirCamion(camion); });
            li.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    choisirCamion(camion);
                }
            });
        }
        liste.appendChild(li);
    }
    const vide = document.getElementById("liste-vide");
    vide.hidden = affiches.length > 0;
    vide.textContent = camions.length === 0 ? "Aucun camion dans la flotte." : "Aucun camion ne correspond.";
}

// ---------------------------------------------------------------------------
// Vue conducteur : tableau de bord de son camion
// ---------------------------------------------------------------------------

function afficherTableauBord(camion) {
    const vide = document.getElementById("tb-vide");
    if (camion === undefined) {
        vide.textContent = "Aucun camion ne vous est attribué. Contactez votre gestionnaire.";
        vide.hidden = false;
        return;
    }
    document.getElementById("tb-camion").textContent = camion.camion_id;
    document.getElementById("tb-immat").textContent = camion.immatriculation || "";
    const etat = document.getElementById("tb-etat");
    etat.textContent = nomEtat(camion);
    etat.className = "badge " + classeEtat(camion);

    const avecPosition = aUnePosition(camion);
    vide.textContent = "Votre camion n'a encore envoyé aucune position.";
    vide.hidden = avecPosition;
    const roule = avecPosition && !estHorsLigne(camion) && camion.etat === "en_route";
    document.getElementById("tb-vitesse").textContent = roule ? Number(camion.vitesse).toFixed(0) : "0";
    document.getElementById("tb-direction").textContent = roule ? direction(camion.cap) : "-";
    document.getElementById("tb-recu").textContent = avecPosition
        ? heureLocale(camion.recu_le) + " (" + depuis(camion) + ")" : "-";
    document.getElementById("tb-position").textContent = avecPosition
        ? Number(camion.lat).toFixed(5) + ", " + Number(camion.lon).toFixed(5) : "-";

    const bilan = minutesTrace > 0 ? bilanTrace(camion.camion_id) : null;
    document.getElementById("tb-trajet").textContent = bilan === null ? "-"
        : bilan.km.toFixed(1).replace(".", ",") + " km en " + Math.max(1, Math.round(bilan.minutes)) + " min";
}

// ---------------------------------------------------------------------------
// Affichage complet et rafraîchissement
// ---------------------------------------------------------------------------

/** Redessine le panneau avec les dernières données (sans rien redemander au serveur). */
function afficherVue() {
    const camions = derniersCamions;
    if (estGestionnaire) {
        afficherTuiles(camions);
        afficherListe(camions);
        appliquerFiltreCarte();
    } else {
        afficherTableauBord(camions[0]);
    }
    const enLigne = camions.filter(function (c) { return categorie(c) !== "hors_ligne"; }).length;
    document.getElementById("compteur").textContent = estGestionnaire
        ? camions.length + " camion" + (camions.length > 1 ? "s" : "") + " · " + enLigne + " en ligne"
        : (enLigne ? "en ligne" : "hors ligne");
}

function cadrer(avecPosition) {
    if (avecPosition.length === 0) {
        return;
    }
    if (!estGestionnaire) {
        if (premierCadrage || document.getElementById("suivre").checked) {
            const c = avecPosition[0];
            carte.setView([Number(c.lat), Number(c.lon)], premierCadrage ? 14 : carte.getZoom());
        }
    } else if (premierCadrage) {
        const zone = L.latLngBounds(avecPosition.map(function (c) {
            return [Number(c.lat), Number(c.lon)];
        }));
        carte.fitBounds(zone, { padding: [40, 40], maxZoom: 14 });
    }
    premierCadrage = false;
}

function signalerErreur(statut, quoi) {
    const bandeau = document.getElementById("bandeau");
    bandeau.textContent = statut === 0
        ? "Serveur injoignable, nouvel essai dans 3 s…"
        : quoi + " indisponible (erreur " + statut + "), nouvel essai dans 3 s…";
    bandeau.hidden = false;
}

async function rafraichir() {
    await chargerConducteurs();
    const r = await api("GET", "/api/camions");
    if (r.statut === 401) {                  // session expirée ou déconnexion ailleurs
        window.location.href = "/connexion";
        return;
    }
    if (r.statut !== 200 || !Array.isArray(r.donnees)) {
        signalerErreur(r.statut, "Serveur");
        return;                              // on garde l'affichage précédent
    }
    document.getElementById("bandeau").hidden = true;

    const camions = r.donnees;
    derniersCamions = camions;
    const avecPosition = camions.filter(aUnePosition);
    for (const camion of avecPosition) {
        mettreAJourMarqueur(camion);
    }
    retirerAbsents(new Set(avecPosition.map(function (c) { return c.camion_id; })));

    // Trajets. Si les camions visibles ont changé (conducteur rattaché à un autre
    // camion, camion ajouté...), on recharge tout depuis le début.
    const ids = new Set(camions.map(function (c) { return c.camion_id; }));
    if (camionsConnus !== null && !memesCamions(ids, camionsConnus)) {
        viderTraces();
    }
    camionsConnus = ids;
    if (minutesTrace > 0) {
        const statut = await chargerTraces();
        if (statut === 401) {
            window.location.href = "/connexion";
            return;
        }
        if (statut !== 200) {
            signalerErreur(statut, "Trajets");
        }
        const parId = {};
        for (const c of camions) {
            parId[c.camion_id] = c;
        }
        dessinerTraces(parId);
    }

    afficherVue();
    if (!estGestionnaire) {
        for (const m of Object.values(marqueurs)) {
            if (!carte.hasLayer(m)) {
                m.addTo(carte);
            }
        }
    }
    cadrer(avecPosition.filter(correspond));
    document.getElementById("maj").textContent =
        "Mise à jour toutes les 3 s · dernière à " + new Date().toLocaleTimeString("fr-FR");
}

/** Boucle : la requête suivante part 3 s APRÈS la fin de la précédente
 *  (si le serveur est lent, les requêtes ne s'empilent pas). */
async function boucle() {
    try {
        await rafraichir();
    } catch (e) {
        console.error(e);
    }
    setTimeout(boucle, PERIODE_MS);
}

// ---------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------

function brancherFiltres() {
    for (const tuile of document.querySelectorAll(".tuile")) {
        tuile.addEventListener("click", function () {
            const cat = tuile.dataset.categorie;
            filtreCategorie = filtreCategorie === cat ? null : cat;   // 2e clic : on retire le filtre
            afficherVue();
        });
    }
    const recherche = document.getElementById("recherche");
    recherche.addEventListener("input", function () {
        texteRecherche = recherche.value.trim().toLowerCase();
        afficherVue();
    });
    document.getElementById("filtre-effacer").addEventListener("click", function () {
        filtreCategorie = null;
        texteRecherche = "";
        recherche.value = "";
        afficherVue();
    });
}

function brancherDureeTrace() {
    // Durée du trajet affiché : gardée dans le navigateur pour la prochaine visite
    const choix = document.getElementById("duree-trace");
    try {
        const memorise = window.localStorage.getItem("duree-trace");
        if (memorise !== null && [...choix.options].some(function (o) { return o.value === memorise; })) {
            choix.value = memorise;
        }
    } catch (e) {
        // stockage indisponible (navigation privée...) : on garde la valeur par défaut
    }
    minutesTrace = Number(choix.value);
    choix.addEventListener("change", function () {
        minutesTrace = Number(choix.value);
        viderTraces();                       // rechargé au prochain rafraîchissement (3 s max)
        afficherVue();
        try {
            window.localStorage.setItem("duree-trace", choix.value);
        } catch (e) {
            // tant pis, le choix ne sera pas retenu
        }
    });
}

(async function () {
    compte = await chargerCompte();
    if (compte === null) {
        return;
    }
    estGestionnaire = compte.role === "gestionnaire";
    document.getElementById("titre-liste").textContent = estGestionnaire ? "Flotte" : "Mon camion";
    document.getElementById(estGestionnaire ? "vue-gestionnaire" : "vue-conducteur").hidden = false;
    document.body.classList.add(estGestionnaire ? "vue-gestionnaire" : "vue-conducteur");

    carte = L.map("carte").setView(CENTRE_DEPART, ZOOM_DEPART);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(carte);

    // si le conducteur déplace la carte lui-même, on arrête de la recentrer
    carte.on("dragstart", function () {
        if (!estGestionnaire) {
            document.getElementById("suivre").checked = false;
        }
    });

    if (estGestionnaire) {
        brancherFiltres();
    }
    brancherDureeTrace();
    boucle();
})();
