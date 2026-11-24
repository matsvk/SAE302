/*
 * inscription.js - création d'un compte conducteur par un gestionnaire.
 *
 * Les règles sont vérifiées ici pour aider l'utilisateur (message tout de
 * suite), mais c'est le serveur qui décide : web.py et comptes.py revérifient
 * tout, et la base aussi. Un contrôle fait seulement dans le navigateur se
 * contourne facilement.
 */

"use strict";

const LONGUEUR_MIN = 12;     // mêmes règles que comptes.py
const OCTETS_MAX = 72;       // limite de bcrypt

const formulaire = document.getElementById("formulaire");
const champEmail = document.getElementById("email");
const choixCamion = document.getElementById("camion");
const champMdp = document.getElementById("mot_de_passe");
const champConfirmation = document.getElementById("confirmation");
const bouton = document.getElementById("bouton");
const message = document.getElementById("message");

/** Remplit la liste des camions avec ceux de la base (GET /api/camions). */
async function chargerCamions() {
    const r = await api("GET", "/api/camions");
    choixCamion.textContent = "";            // vide la liste
    if (r.statut !== 200 || !Array.isArray(r.donnees)) {
        afficherMessage(message, "Impossible de charger la liste des camions.", "erreur");
        return;
    }
    const vide = document.createElement("option");
    vide.value = "";
    vide.textContent = "-- choisir un camion --";
    choixCamion.appendChild(vide);
    for (const camion of r.donnees) {
        const option = document.createElement("option");
        option.value = camion.camion_id;
        option.textContent = camion.camion_id + (camion.immatriculation ? " · " + camion.immatriculation : "");
        choixCamion.appendChild(option);
    }
}

/** Renvoie un message d'erreur, ou "" si le formulaire est correct. */
function verifierFormulaire(email, camion, mdp, confirmation) {
    if (email === "" || !email.includes("@")) {
        return "Adresse e-mail invalide.";
    }
    if (camion === "") {
        return "Choisissez le camion du conducteur.";
    }
    if (mdp.length < LONGUEUR_MIN) {
        return "Mot de passe trop court (" + LONGUEUR_MIN + " caractères minimum).";
    }
    if (new TextEncoder().encode(mdp).length > OCTETS_MAX) {
        return "Mot de passe trop long (" + OCTETS_MAX + " octets maximum).";
    }
    if (mdp.trim().toLowerCase() === email.toLowerCase()) {
        return "Le mot de passe ne doit pas être l'adresse e-mail.";
    }
    if (mdp !== confirmation) {
        return "Les deux mots de passe sont différents.";
    }
    return "";
}

/** Indicateur sous le mot de passe : 4 barres, remplies selon la longueur.
 *  12 caractères = 3 barres (minimum demandé), 16 et plus = 4 barres. */
const barres = document.querySelectorAll(".force i");
const blocForce = document.querySelector(".force");
const aideMdp = document.getElementById("aide-mdp");

function majForce() {
    const n = champMdp.value.length;
    const remplies = n === 0 ? 0 : n < 6 ? 1 : n < LONGUEUR_MIN ? 2 : n < 16 ? 3 : 4;
    barres.forEach(function (barre, i) {
        barre.classList.toggle("rempli", i < remplies);
    });
    blocForce.classList.toggle("faible", n > 0 && n < LONGUEUR_MIN);
    if (n === 0) {
        aideMdp.textContent = LONGUEUR_MIN + " caractères minimum.";
    } else if (n < LONGUEUR_MIN) {
        aideMdp.textContent = n + " caractères : encore " + (LONGUEUR_MIN - n) + " au minimum.";
    } else {
        aideMdp.textContent = "✓ " + n + " caractères";
    }
    aideMdp.classList.toggle("ok", n >= LONGUEUR_MIN);
}

champMdp.addEventListener("input", majForce);

// "Annuler" vide le formulaire (bouton type="reset") : on remet aussi le reste à zéro
formulaire.addEventListener("reset", function () {
    afficherMessage(message, "");
    setTimeout(majForce, 0);      // après que le navigateur a vidé les champs
});

formulaire.addEventListener("submit", async function (evenement) {
    evenement.preventDefault();
    afficherMessage(message, "");

    const email = champEmail.value.trim();
    const camion = choixCamion.value;
    const mdp = champMdp.value;
    const erreur = verifierFormulaire(email, camion, mdp, champConfirmation.value);
    if (erreur) {
        afficherMessage(message, erreur, "erreur");
        return;
    }

    bouton.disabled = true;
    bouton.textContent = "Création...";
    const r = await api("POST", "/api/utilisateurs",
                        { email: email, mot_de_passe: mdp, camion_id: camion });
    bouton.disabled = false;
    bouton.textContent = "Créer le compte";

    if (r.statut === 201) {
        afficherMessage(message, "Compte créé : " + r.donnees.email + ", conducteur de "
                        + r.donnees.camion_id + ".", "succes");
        // on vide les champs sans passer par reset(), qui effacerait le message de succès
        champEmail.value = "";
        choixCamion.value = "";
        champMdp.value = "";
        champConfirmation.value = "";
        majForce();
        champEmail.focus();
        return;
    }
    if (r.statut === 401) {                  // session expirée entre-temps
        window.location.href = "/connexion";
        return;
    }
    // 400 : règle non respectée, 409 : e-mail déjà utilisé, 403, 503...
    let texte = r.donnees.erreur || "Création impossible (erreur " + r.statut + ").";
    texte = texte.charAt(0).toUpperCase() + texte.slice(1) + ".";
    afficherMessage(message, texte, "erreur");
});

(async function () {
    const moi = await chargerCompte();
    if (moi === null) {
        return;
    }
    if (moi.role !== "gestionnaire") {       // normalement déjà bloqué par le serveur
        window.location.href = "/carte";
        return;
    }
    await chargerCamions();
    champEmail.focus();
})();
