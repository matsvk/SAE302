/*
 * carte.js - page d'accueil après connexion.
 * Provisoire : la carte Leaflet sera ajoutée à l'étape suivante (P2).
 */

"use strict";

(async function () {
    const moi = await chargerCompte();
    if (moi === null) {
        return;
    }
    const infos = document.getElementById("infos");
    if (moi.role === "gestionnaire") {
        document.getElementById("lien-inscription").hidden = false;
        infos.textContent = "Compte gestionnaire : vous verrez toute la flotte.";
    } else {
        infos.textContent = "Compte conducteur : vous verrez uniquement le camion " + moi.camion_id + ".";
    }
})();
