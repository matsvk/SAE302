/*
 * carte.js - Phase 5 P2 et P3 : carte Leaflet des camions, mise à jour toutes les 3 s.
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

const NOMS_ETATS = {
    a_quai: "À quai",
    en_route: "En route",
    livraison: "Livraison",
    termine: "Terminé",
};

let carte = null;
let compte = null;
const marqueurs = {};             // id du camion -> marqueur Leaflet
let premierCadrage = true;        // cadrer la carte sur les camions au 1er chargement
let camionChoisi = null;          // camion cliqué dans la liste (gestionnaire)

const traces = {};                // id du camion -> {ligne: trait Leaflet, points: [{t, lat, lon}]}
let dernierId = 0;                // curseur de /api/positions : dernière ligne déjà reçue
let minutesTrace = DUREE_TRACE_DEFAUT;
let generationTraces = 0;         // change à chaque remise à zéro (réponses en retard ignorées)
let camionsConnus = null;         // ids des camions visibles au rafraîchissement précédent

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
    return estHorsLigne(camion) ? "hors-ligne" : "etat-" + camion.etat;
}

// ---------------------------------------------------------------------------
// Marqueurs
// ---------------------------------------------------------------------------

/** Icône du marqueur : une pastille colorée avec une flèche (cap) et le nom du camion. */
function creerIcone(camion) {
    const bloc = element("div", "marqueur " + classeEtat(camion));
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
    bloc.appendChild(titre);

    const lignes = [
        ["État", estHorsLigne(camion) ? "Hors ligne" : (NOMS_ETATS[camion.etat] || camion.etat)],
        ["Vitesse", Number(camion.vitesse).toFixed(1) + " km/h"],
        ["Direction", Math.round(Number(camion.cap)) + "°"],
        ["Position", Number(camion.lat).toFixed(5) + ", " + Number(camion.lon).toFixed(5)],
        ["Reçu", heureLocale(camion.recu_le) + " (" + depuis(camion) + ")"],
    ];
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
        m.addTo(carte);
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

function memesCamions(a, b) {
    return a.size === b.size && [...a].every(function (id) { return b.has(id); });
}

// ---------------------------------------------------------------------------
// Liste à gauche
// ---------------------------------------------------------------------------

function afficherListe(camions) {
    const liste = document.getElementById("liste-camions");
    liste.textContent = "";
    for (const camion of camions) {
        const li = element("li", "camion" + (camion.camion_id === camionChoisi ? " choisi" : ""));
        const avecPosition = camion.lat !== null && camion.lat !== undefined;

        const haut = element("div", "camion-haut");
        haut.appendChild(element("i", "pastille " + (avecPosition ? classeEtat(camion) : "hors-ligne")));
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
            detail = (NOMS_ETATS[camion.etat] || camion.etat) + " · "
                + Number(camion.vitesse).toFixed(0) + " km/h · " + depuis(camion);
        }
        li.appendChild(element("div", "camion-detail", detail));

        if (avecPosition) {
            li.tabIndex = 0;
            li.setAttribute("role", "button");
            const choisir = function () {
                camionChoisi = camion.camion_id;
                carte.setView([Number(camion.lat), Number(camion.lon)], Math.max(carte.getZoom(), 14));
                marqueurs[camion.camion_id].openPopup();
                afficherListe(camions);
            };
            li.addEventListener("click", choisir);
            li.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    choisir();
                }
            });
        }
        liste.appendChild(li);
    }
    document.getElementById("liste-vide").hidden = camions.length > 0;

    const actifs = camions.filter(function (c) {
        return c.lat !== null && !estHorsLigne(c);
    }).length;
    document.getElementById("compteur").textContent =
        camions.length + " camion" + (camions.length > 1 ? "s" : "") + " · " + actifs + " en ligne";
}

// ---------------------------------------------------------------------------
// Rafraîchissement
// ---------------------------------------------------------------------------

function cadrer(avecPosition) {
    if (avecPosition.length === 0) {
        return;
    }
    if (compte.role === "conducteur") {
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

async function rafraichir() {
    const r = await api("GET", "/api/camions");
    const bandeau = document.getElementById("bandeau");

    if (r.statut === 401) {                  // session expirée ou déconnexion ailleurs
        window.location.href = "/connexion";
        return;
    }
    if (r.statut !== 200 || !Array.isArray(r.donnees)) {
        bandeau.textContent = r.statut === 0
            ? "Serveur injoignable, nouvel essai dans 3 s…"
            : "Erreur du serveur (" + r.statut + "), nouvel essai dans 3 s…";
        bandeau.hidden = false;
        return;                              // on garde l'affichage précédent
    }
    bandeau.hidden = true;

    const camions = r.donnees;
    const avecPosition = camions.filter(function (c) {
        return c.lat !== null && c.lat !== undefined;
    });
    for (const camion of avecPosition) {
        mettreAJourMarqueur(camion);
    }
    retirerAbsents(new Set(avecPosition.map(function (c) { return c.camion_id; })));
    afficherListe(camions);
    cadrer(avecPosition);

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
            bandeau.textContent = statut === 0
                ? "Serveur injoignable, nouvel essai dans 3 s…"
                : "Trajets indisponibles (erreur " + statut + "), nouvel essai dans 3 s…";
            bandeau.hidden = false;
        }
        const parId = {};
        for (const c of camions) {
            parId[c.camion_id] = c;
        }
        dessinerTraces(parId);
    }
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

(async function () {
    compte = await chargerCompte();
    if (compte === null) {
        return;
    }
    if (compte.role === "gestionnaire") {
        document.getElementById("lien-inscription").hidden = false;
        document.getElementById("titre-liste").textContent = "Flotte";
    } else {
        document.getElementById("titre-liste").textContent = "Mon camion";
        document.getElementById("case-suivi").hidden = false;
    }

    carte = L.map("carte").setView(CENTRE_DEPART, ZOOM_DEPART);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(carte);

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
        try {
            window.localStorage.setItem("duree-trace", choix.value);
        } catch (e) {
            // tant pis, le choix ne sera pas retenu
        }
    });

    // si l'utilisateur déplace la carte lui-même, on arrête de la recentrer
    carte.on("dragstart", function () {
        if (compte.role === "conducteur") {
            document.getElementById("suivre").checked = false;
        }
    });

    boucle();
})();
