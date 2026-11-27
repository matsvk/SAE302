/*
 * connexion.js - page de connexion.
 *
 * Envoie l'e-mail et le mot de passe à POST /api/connexion. Si c'est bon, le
 * serveur pose le cookie de session et on part sur la carte. Sinon on affiche
 * le message du serveur, qui est volontairement le même pour un e-mail inconnu
 * et pour un mauvais mot de passe.
 */

"use strict";

const formulaire = document.getElementById("formulaire");
const champEmail = document.getElementById("email");
const champMdp = document.getElementById("mot_de_passe");
const bouton = document.getElementById("bouton");
const message = document.getElementById("message");

champEmail.focus();

formulaire.addEventListener("submit", async function (evenement) {
    evenement.preventDefault();          // pas d'envoi classique du formulaire
    afficherMessage(message, "");

    const email = champEmail.value.trim();
    const mdp = champMdp.value;
    if (email === "" || mdp === "") {
        afficherMessage(message, "Saisissez votre adresse e-mail et votre mot de passe.", "erreur");
        return;
    }

    bouton.disabled = true;
    bouton.textContent = "Connexion...";
    const r = await api("POST", "/api/connexion", { email: email, mot_de_passe: mdp });

    if (r.statut === 200) {
        window.location.href = "/carte";
        return;
    }

    // 401 : identifiants faux, 429 : trop d'essais, 503 : base indisponible...
    let texte = r.donnees.erreur || "Connexion impossible (erreur " + r.statut + ").";
    texte = texte.charAt(0).toUpperCase() + texte.slice(1) + ".";
    afficherMessage(message, texte, "erreur");
    champMdp.value = "";
    champMdp.focus();
    bouton.disabled = false;
    bouton.textContent = "Se connecter";
});

/*
 * Invitation à installer l'appli sur iPhone / iPad.
 * Affichée seulement dans Safari sur iOS, quand le site n'est pas DÉJÀ ouvert
 * depuis l'écran d'accueil (navigator.standalone vaut alors true).
 * Le choix « ne plus afficher » est gardé dans le navigateur (localStorage).
 */
(function () {
    const invite = document.getElementById("invite-installation");
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent)
        || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);   // iPad récents
    const dejaInstallee = window.navigator.standalone === true
        || window.matchMedia("(display-mode: standalone)").matches;
    let masquee = false;
    try {
        masquee = localStorage.getItem("invite-installation") === "masquee";
    } catch (e) {
        // navigation privée : on affiche quand même
    }
    if (!ios || dejaInstallee || masquee) {
        return;
    }
    invite.hidden = false;
    document.getElementById("fermer-invite").addEventListener("click", function () {
        invite.hidden = true;
        try {
            localStorage.setItem("invite-installation", "masquee");
        } catch (e) {
            // rien : l'invitation reviendra à la prochaine visite
        }
    });
})();
