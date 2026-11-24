/*
 * commun.js - fonctions utilisées par toutes les pages.
 *
 * Toutes les données viennent de l'API (web.py) en JSON. On les affiche
 * toujours avec textContent, jamais avec innerHTML : un texte venu du serveur
 * (par exemple une adresse e-mail piégée) ne peut donc pas devenir du HTML ou
 * du JavaScript dans la page (protection contre le XSS).
 */

"use strict";

/**
 * Appelle l'API et renvoie {statut, donnees}.
 * statut = 0 si le serveur n'a pas pu être joint.
 */
async function api(methode, chemin, corps) {
    const options = {
        method: methode,
        headers: {},
        credentials: "same-origin",     // envoie le cookie de session
    };
    if (corps !== undefined) {
        // l'API exige du JSON (protection contre les formulaires d'autres sites)
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(corps);
    }
    let reponse;
    try {
        reponse = await fetch(chemin, options);
    } catch (e) {
        return { statut: 0, donnees: { erreur: "serveur injoignable, vérifiez votre connexion" } };
    }
    let donnees = {};
    try {
        donnees = await reponse.json();
    } catch (e) {
        // réponse vide ou pas du JSON : on garde un objet vide
    }
    return { statut: reponse.status, donnees: donnees };
}

/** Affiche un message (type = "erreur" ou "succes"), ou le cache si texte est vide. */
function afficherMessage(element, texte, type) {
    element.textContent = texte || "";
    element.classList.remove("erreur", "succes");
    if (type) {
        element.classList.add(type);
    }
    element.hidden = !texte;
}

/**
 * Pour les pages réservées aux comptes connectés : vérifie la session,
 * remplit l'en-tête et branche le bouton de déconnexion.
 * Renvoie le compte ({email, role, camion_id}), ou null (redirection en cours).
 */
async function chargerCompte() {
    const r = await api("GET", "/api/moi");
    if (r.statut === 401) {
        window.location.href = "/connexion";
        return null;
    }
    if (r.statut !== 200) {
        return null;
    }
    const moi = r.donnees;
    const zone = document.getElementById("utilisateur");
    if (zone) {
        zone.textContent = moi.email;
    }
    const role = document.getElementById("role");
    if (role) {
        role.textContent = moi.role === "gestionnaire" ? "Gestionnaire" : "Conducteur · " + moi.camion_id;
    }
    const avatar = document.getElementById("avatar");
    if (avatar) {
        avatar.textContent = moi.email.charAt(0).toUpperCase();   // initiale de l'e-mail
    }
    const bouton = document.getElementById("deconnexion");
    if (bouton) {
        bouton.addEventListener("click", async function () {
            bouton.disabled = true;
            await api("POST", "/api/deconnexion");
            window.location.href = "/connexion";
        });
    }
    return moi;
}
