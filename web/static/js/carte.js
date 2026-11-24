/*
 * carte.js - Phase 5 P2 : carte Leaflet avec les marqueurs des camions.
 *
 * Toutes les 3 s, la page demande GET /api/camions, qui renvoie la dernière
 * position de chaque camion VISIBLE par le compte connecté :
 *   - gestionnaire : toute la flotte ;
 *   - conducteur   : uniquement son camion (le filtre est fait par le serveur,
 *                    dans la requête SQL : rien n'est caché côté navigateur).
 * Chaque camion a un marqueur dont la couleur dépend de son état, avec une
 * flèche tournée dans sa direction (cap). Un camion dont le serveur n'a rien
 * reçu depuis 30 s est affiché "hors ligne" (grisé).
 *
 * Sécurité : les textes venus du serveur sont toujours affichés avec
 * textContent (jamais innerHTML), y compris dans les marqueurs et les bulles.
 */

"use strict";

const PERIODE_MS = 3000;          // même rythme que l'envoi des camions
const HORS_LIGNE_S = 30;          // même valeur que delai_silence du serveur UDP
const CENTRE_DEPART = [48.86, 2.35];   // Paris, avant la première position
const ZOOM_DEPART = 10;

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

    // si l'utilisateur déplace la carte lui-même, on arrête de la recentrer
    carte.on("dragstart", function () {
        if (compte.role === "conducteur") {
            document.getElementById("suivre").checked = false;
        }
    });

    boucle();
})();
