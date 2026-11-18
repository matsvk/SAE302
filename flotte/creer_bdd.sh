#!/usr/bin/env bash
# creer_bdd.sh - crée la base MariaDB du suivi de flotte (SAÉ 3.02, Phase 3)
#
#   1. base "flotte" + les 3 tables + la vue      (sql/01_base.sql, sql/02_schema.sql)
#   2. les camions de la flotte                   (sql/03_donnees.sql)
#   3. deux comptes MariaDB avec le minimum de droits :
#        - le serveur UDP (flotte_serveur) : lit camions, ajoute des positions
#        - le site web    (flotte_web)     : lit tout, gère les utilisateurs (Phase 4)
#   4. le fichier .bdd_secret (mots de passe générés au hasard, lisible
#      seulement par toi) : lu par base.py, jamais envoyé sur GitHub
#
# Usage (sur la machine où tourne MariaDB) :
#   sudo bash creer_bdd.sh
#   sudo bash creer_bdd.sh --utilisateur matis --depuis 100.%     (serveur UDP sur une autre machine)
#   sudo bash creer_bdd.sh --reinitialiser                        (efface TOUT et recrée)
#
# Relançable sans risque : les tables existantes et leurs données sont gardées,
# les mots de passe déjà présents dans .bdd_secret sont réutilisés.

set -euo pipefail

DOSSIER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRET="$DOSSIER/.bdd_secret"
BASE="flotte"
UTIL_SERVEUR="flotte_serveur"
UTIL_WEB="flotte_web"
HOTE_CLIENT="localhost"
REINIT=0

erreur() { echo "ERREUR : $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --utilisateur)   UTIL_SERVEUR="${2:?--utilisateur demande un nom}"; shift 2 ;;
        --depuis)        HOTE_CLIENT="${2:?--depuis demande une adresse}"; shift 2 ;;
        --reinitialiser) REINIT=1; shift ;;
        -h|--help)       sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)               erreur "option inconnue : $1 (voir --help)" ;;
    esac
done

# Ces valeurs finissent dans du SQL : on n'accepte que des caractères sans danger
[[ "$UTIL_SERVEUR" =~ ^[A-Za-z0-9_]{1,32}$ ]] || erreur "nom d'utilisateur invalide : $UTIL_SERVEUR"
[[ "$HOTE_CLIENT" =~ ^[A-Za-z0-9.%:_/-]{1,255}$ ]] || erreur "hôte invalide : $HOTE_CLIENT"

[ "$(id -u)" -eq 0 ] || erreur "à lancer en root : sudo bash $0"
CLIENT="$(command -v mariadb || command -v mysql || true)"
[ -n "$CLIENT" ] || erreur "client MariaDB introuvable (sudo apt install mariadb-server)"
sql() { "$CLIENT" --protocol=socket -u root "$@"; }
sql -e "SELECT 1" >/dev/null 2>&1 || erreur "MariaDB ne répond pas (sudo systemctl start mariadb)"

for f in 01_base.sql 02_schema.sql 03_donnees.sql; do
    [ -f "$DOSSIER/sql/$f" ] || erreur "fichier manquant : sql/$f"
done

# --- mots de passe : réutilisés s'ils existent déjà, sinon tirés au hasard
lire()   { [ -f "$SECRET" ] && sed -n "s/^$1=//p" "$SECRET" | head -n 1 || true; }
hasard() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1" || true; }

MDP_SERVEUR="$(lire FLOTTE_BDD_MDP)";  MDP_SERVEUR="${MDP_SERVEUR:-$(hasard 32)}"
MDP_WEB="$(lire FLOTTE_WEB_MDP)";      MDP_WEB="${MDP_WEB:-$(hasard 32)}"
CLE_SESSION="$(lire FLOTTE_SESSION_CLE)"; CLE_SESSION="${CLE_SESSION:-$(hasard 64)}"
for v in "$MDP_SERVEUR" "$MDP_WEB" "$CLE_SESSION"; do
    [[ "$v" =~ ^[A-Za-z0-9]{32,}$ ]] || erreur "$SECRET contient une valeur invalide (supprime-le et relance)"
done

# --- 0. réinitialisation (sur demande)
if [ "$REINIT" -eq 1 ]; then
    echo "ATTENTION : la base « $BASE » va être supprimée avec TOUTES ses positions."
    read -r -p "Taper « oui » pour confirmer : " reponse
    [ "$reponse" = "oui" ] || erreur "annulé"
    sql -e "DROP DATABASE IF EXISTS $BASE"
    echo "- base supprimée"
fi

# --- 1 et 2. base, tables, vue, camions
sql < "$DOSSIER/sql/01_base.sql"
sql < "$DOSSIER/sql/02_schema.sql"
sql < "$DOSSIER/sql/03_donnees.sql"
echo "- base « $BASE » : tables camions, positions, utilisateurs et vue v_derniere_position prêtes"

# --- 3. comptes et droits (principe du moindre privilège)
# Le serveur UDP ne peut ni modifier ni supprimer une position, ni toucher aux
# comptes du site : même si son mot de passe fuit, l'historique reste intact.
sql <<SQL
CREATE USER IF NOT EXISTS '$UTIL_SERVEUR'@'$HOTE_CLIENT' IDENTIFIED BY '$MDP_SERVEUR';
ALTER USER '$UTIL_SERVEUR'@'$HOTE_CLIENT' IDENTIFIED BY '$MDP_SERVEUR';
GRANT SELECT         ON $BASE.camions             TO '$UTIL_SERVEUR'@'$HOTE_CLIENT';
GRANT SELECT, INSERT ON $BASE.positions           TO '$UTIL_SERVEUR'@'$HOTE_CLIENT';
GRANT SELECT         ON $BASE.v_derniere_position TO '$UTIL_SERVEUR'@'$HOTE_CLIENT';

CREATE USER IF NOT EXISTS '$UTIL_WEB'@'localhost' IDENTIFIED BY '$MDP_WEB';
ALTER USER '$UTIL_WEB'@'localhost' IDENTIFIED BY '$MDP_WEB';
GRANT SELECT, INSERT, UPDATE         ON $BASE.camions             TO '$UTIL_WEB'@'localhost';
GRANT SELECT                         ON $BASE.positions           TO '$UTIL_WEB'@'localhost';
GRANT SELECT                         ON $BASE.v_derniere_position TO '$UTIL_WEB'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON $BASE.utilisateurs        TO '$UTIL_WEB'@'localhost';
FLUSH PRIVILEGES;
SQL
echo "- compte serveur UDP : '$UTIL_SERVEUR'@'$HOTE_CLIENT' (SELECT camions, SELECT+INSERT positions)"
echo "- compte site web    : '$UTIL_WEB'@'localhost' (lecture + gestion des utilisateurs)"

# --- 4. fichier secret, lisible seulement par son propriétaire
umask 077
cat > "$SECRET" <<EOF
# Généré par creer_bdd.sh le $(date '+%Y-%m-%d %H:%M:%S'). NE PAS mettre sur GitHub.
FLOTTE_BDD_MDP=$MDP_SERVEUR
FLOTTE_WEB_MDP=$MDP_WEB
FLOTTE_SESSION_CLE=$CLE_SESSION
EOF
chmod 600 "$SECRET"
# sudo : le fichier appartient à l'utilisateur qui a lancé sudo, pas à root,
# sinon serveur_udp.py (lancé sans sudo) ne pourrait pas le lire
if [ -n "${SUDO_USER:-}" ]; then
    chown "$SUDO_USER": "$SECRET"
fi
echo "- mots de passe dans $SECRET (droits 600)"

if [ -f "$DOSSIER/.gitignore" ] && ! grep -qxF ".bdd_secret" "$DOSSIER/.gitignore"; then
    echo ".bdd_secret" >> "$DOSSIER/.gitignore"
    echo "- .bdd_secret ajouté à .gitignore"
fi

# --- vérification
echo
sql -t -e "SELECT id, immatriculation, actif FROM $BASE.camions ORDER BY id;
           SELECT COUNT(*) AS positions_enregistrees FROM $BASE.positions;"

if [ "$HOTE_CLIENT" != "localhost" ]; then
    cat <<EOF

Le serveur UDP se connecte depuis une autre machine ($HOTE_CLIENT) :
  - MariaDB doit écouter sur le réseau : dans /etc/mysql/mariadb.conf.d/50-server.cnf
    mettre « bind-address = 0.0.0.0 » (ou l'IP de cette machine) puis
    sudo systemctl restart mariadb ;
  - copier .bdd_secret à côté de serveur_udp.py sur l'autre machine (chmod 600).
EOF
fi
echo
echo "Terminé. Test : python3 base.py"
