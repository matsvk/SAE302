/*
 * conducteurs.js - Phase 5 P4 : page "Conducteurs" du gestionnaire.
 *
 * Liste tous les comptes (GET /api/utilisateurs, réservé au gestionnaire par le
 * serveur) avec le camion de chaque conducteur, et permet de lui attribuer un
 * autre camion (PUT /api/utilisateurs/<id>/camion). L'état de chaque camion
 * (GET /api/camions) est mis à jour toutes les 10 s.
 *
 * Comme partout : c'est le serveur qui décide. Un conducteur qui ouvrirait cette
 * page (ou appellerait l'API à la main) reçoit 403 : rien n'est caché ici.
 * Les textes venus du serveur sont affichés avec textContent, jamais innerHTML.
 */

"use strict";

const PERIODE_ETATS_MS = 10000;
const HORS_LIGNE_S = 30;          // même valeur que sur la carte et dans le serveur UDP
const NOMS_ETATS = { a_quai: "À quai", en_route: "En route", livraison: "Livraison", termine: "Terminé" };

let moi = null;
let comptes = [];                 // réponse de /api/utilisateurs
let camions = [];                 // réponse de /api/camions
let texteRecherche = "";

const message = document.getElementById("message");

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

/** {texte, classe} de l'état d'un camion, comme sur la carte. */
function etatCamion(id) {
    const c = camions.find(function (x) { return x.camion_id === id; });
    if (c === undefined || c.lat === null || c.lat === undefined) {
        return { texte: "Aucune position", classe: "hors-ligne" };
    }
    const age = (Date.now() - Date.parse(c.recu_le)) / 1000;
    if (!(age <= HORS_LIGNE_S)) {
        return { texte: "Hors ligne", classe: "hors-ligne" };
    }
    return { texte: NOMS_ETATS[c.etat] || c.etat, classe: "etat-" + c.etat };
}

/** Écrit le nombre et accorde le texte à côté : "1 gestionnaire", "2 gestionnaires". */
function nombre(id, n, singulier, pluriel) {
    document.getElementById(id).textContent = n;
    document.getElementById(id).nextElementSibling.textContent = n > 1 ? pluriel : singulier;
}

function afficherResume() {
    const conducteurs = comptes.filter(function (u) { return u.role === "conducteur"; });
    nombre("nb-conducteurs", conducteurs.length, "conducteur", "conducteurs");
    nombre("nb-gestionnaires", comptes.length - conducteurs.length, "gestionnaire", "gestionnaires");
    const attribues = new Set(conducteurs.map(function (u) { return u.camion_id; }));
    const libres = camions.filter(function (c) { return !attribues.has(c.camion_id); })
        .map(function (c) { return c.camion_id; });
    nombre("nb-sans-conducteur", libres.length, "camion sans conducteur", "camions sans conducteur");
    document.getElementById("liste-sans-conducteur").textContent =
        libres.length ? libres.join(", ") : "tous les camions ont un conducteur";
}

/** Liste déroulante des camions pour un conducteur. Un changement est enregistré tout de suite. */
function choixCamion(compte) {
    const choix = element("select", "choix-camion");
    choix.setAttribute("aria-label", "Camion de " + compte.email);
    for (const c of camions) {
        const option = element("option", "", c.camion_id + (c.immatriculation ? " · " + c.immatriculation : ""));
        option.value = c.camion_id;
        choix.appendChild(option);
    }
    choix.value = compte.camion_id;
    choix.addEventListener("change", async function () {
        const ancien = compte.camion_id;
        const nouveau = choix.value;
        choix.disabled = true;
        const r = await api("PUT", "/api/utilisateurs/" + encodeURIComponent(compte.id) + "/camion",
                            { camion_id: nouveau });
        choix.disabled = false;
        if (r.statut === 200) {
            compte.camion_id = nouveau;
            afficherMessage(message, compte.email + " conduit maintenant " + nouveau + ".", "succes");
            afficherResume();
            majEtats();
            return;
        }
        if (r.statut === 401) {
            window.location.href = "/connexion";
            return;
        }
        choix.value = ancien;               // on revient à l'ancien camion
        const texte = r.donnees.erreur || "erreur " + r.statut;
        afficherMessage(message, "Modification refusée : " + texte + ".", "erreur");
    });
    return choix;
}

function afficherTableau() {
    const corps = document.getElementById("lignes");
    corps.textContent = "";
    const affiches = comptes.filter(function (u) {
        const texte = (u.email + " " + (u.camion_id || "")).toLowerCase();
        return texteRecherche === "" || texte.includes(texteRecherche);
    });
    for (const compte of affiches) {
        const tr = document.createElement("tr");
        tr.dataset.camion = compte.camion_id || "";

        const tdCompte = element("td");
        const blocCompte = element("div", "cellule-compte");
        blocCompte.appendChild(element("div", "avatar petit", compte.email.charAt(0).toUpperCase()));
        blocCompte.appendChild(element("span", "", compte.email));
        if (moi && compte.email === moi.email) {
            blocCompte.appendChild(element("span", "vous", "vous"));
        }
        tdCompte.appendChild(blocCompte);
        tr.appendChild(tdCompte);

        const tdRole = element("td");
        tdRole.appendChild(element("span", "badge role-" + compte.role,
                                   compte.role === "gestionnaire" ? "Gestionnaire" : "Conducteur"));
        tr.appendChild(tdRole);

        const tdCamion = element("td");
        if (compte.role === "conducteur") {
            tdCamion.appendChild(choixCamion(compte));
        } else {
            tdCamion.appendChild(element("span", "discret", "Toute la flotte"));
        }
        tr.appendChild(tdCamion);

        tr.appendChild(element("td", "cellule-etat"));
        corps.appendChild(tr);
    }
    document.getElementById("vide").hidden = affiches.length > 0;
    majEtats();
}

/** Met à jour la colonne "État du camion" sans toucher aux listes déroulantes. */
function majEtats() {
    for (const tr of document.querySelectorAll("#lignes tr")) {
        const select = tr.querySelector("select");
        const id = select ? select.value : "";
        const cellule = tr.querySelector(".cellule-etat");
        cellule.textContent = "";
        if (id) {
            const etat = etatCamion(id);
            cellule.appendChild(element("span", "badge " + etat.classe, etat.texte));
        } else {
            cellule.appendChild(element("span", "discret", "-"));
        }
    }
}

async function chargerCamions() {
    const r = await api("GET", "/api/camions");
    if (r.statut === 401) {
        window.location.href = "/connexion";
        return false;
    }
    if (r.statut === 200 && Array.isArray(r.donnees)) {
        camions = r.donnees;
    }
    return true;
}

(async function () {
    moi = await chargerCompte();
    if (moi === null) {
        return;
    }
    if (moi.role !== "gestionnaire") {       // normalement déjà bloqué par le serveur
        window.location.href = "/carte";
        return;
    }
    const [r] = await Promise.all([api("GET", "/api/utilisateurs"), chargerCamions()]);
    if (r.statut !== 200 || !Array.isArray(r.donnees)) {
        afficherMessage(message, "Impossible de charger les comptes (erreur " + r.statut + ").", "erreur");
        return;
    }
    comptes = r.donnees;
    afficherResume();
    afficherTableau();

    const recherche = document.getElementById("recherche");
    recherche.addEventListener("input", function () {
        texteRecherche = recherche.value.trim().toLowerCase();
        afficherTableau();
    });

    setInterval(async function () {
        if (await chargerCamions()) {
            afficherResume();
            majEtats();
        }
    }, PERIODE_ETATS_MS);
})();
