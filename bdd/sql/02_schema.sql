-- 02_schema.sql - les 3 tables du suivi de flotte + la vue "dernière position"
--
--   camions      la liste des camions de la flotte
--   positions    toutes les positions envoyées par les camions (écrites par serveur_udp.py)
--   utilisateurs les comptes du site web (Phase 4)
--
-- MariaDB >= 10.2 (les CHECK sont vérifiés). Moteur InnoDB : obligatoire pour
-- les clés étrangères et les transactions (base.py écrit par paquets dans une
-- transaction).
--
-- Le script peut être relancé : CREATE TABLE IF NOT EXISTS ne touche pas aux
-- tables existantes. Pour tout reconstruire : sudo bash creer_bdd.sh --reinitialiser
--
-- Toutes les dates sont en UTC (base.py fait SET time_zone = '+00:00').

USE flotte;

-- ---------------------------------------------------------------------------
-- camions
-- ---------------------------------------------------------------------------
-- id : même format que dans le protocole (envoi.FORMAT_ID) : lettres, chiffres,
--      - et _, 1 à 32 caractères. Le CHECK le revérifie côté base.
-- actif = FALSE : le serveur refuse ses messages (code "non_autorise"), sans
--      redémarrage (serveur_udp.py relit la table toutes les 60 s). On désactive
--      un camion au lieu de le supprimer : ses positions restent dans l'historique.
CREATE TABLE IF NOT EXISTS camions (
    id               VARCHAR(32)  NOT NULL,
    immatriculation  VARCHAR(9)   NOT NULL,
    actif            BOOLEAN      NOT NULL DEFAULT TRUE,

    CONSTRAINT pk_camions PRIMARY KEY (id),
    CONSTRAINT uq_camions_immat UNIQUE (immatriculation),
    CONSTRAINT ck_camions_id    CHECK (id REGEXP '^[A-Za-z0-9_-]{1,32}$'),
    -- plaque française format SIV : AB-123-CD
    CONSTRAINT ck_camions_immat CHECK (immatriculation REGEXP '^[A-Z]{2}-[0-9]{3}-[A-Z]{2}$')
) ENGINE = InnoDB;

-- ---------------------------------------------------------------------------
-- positions
-- ---------------------------------------------------------------------------
-- Une ligne = un message "position" accepté par le serveur.
-- Les types suivent la précision du protocole :
--   lat/lon  6 décimales (~10 cm)  -> DECIMAL(9,6) : -180.000000 à 180.000000
--   vitesse, cap 1 décimale        -> DECIMAL(4,1) : jusqu'à 999.9
--   horodatage au millième         -> DATETIME(3)
-- DECIMAL plutôt que FLOAT : valeur exacte, pas d'arrondi binaire
-- (48.947312 reste 48.947312).
--
-- Clé unique (camion_id, horodatage, seq) : base.py utilise INSERT IGNORE, donc
-- un paquet réécrit après une coupure de la base ne crée pas de doublons.
-- seq seul ne suffit pas : il repart à 0 quand le camion redémarre.
--
-- Cette clé sert aussi d'index pour "dernière position d'un camion"
-- (WHERE camion_id = ? ORDER BY horodatage DESC, seq DESC LIMIT 1) :
-- MariaDB lit directement la dernière entrée de l'index, sans tri.
CREATE TABLE IF NOT EXISTS positions (
    id          BIGINT UNSIGNED  NOT NULL AUTO_INCREMENT,
    camion_id   VARCHAR(32)      NOT NULL,
    seq         INT UNSIGNED     NOT NULL,
    horodatage  DATETIME(3)      NOT NULL,      -- moment de la mesure (UTC, donné par le camion)
    lat         DECIMAL(9,6)     NOT NULL,
    lon         DECIMAL(9,6)     NOT NULL,
    vitesse     DECIMAL(4,1)     NOT NULL,      -- km/h
    cap         DECIMAL(4,1)     NOT NULL,      -- degrés, 0 = nord, 90 = est
    etat        ENUM('a_quai', 'en_route', 'livraison', 'termine') NOT NULL,
    recu_le     DATETIME(3)      NOT NULL,      -- moment de la réception (UTC, horloge du serveur)
    ip_source   VARCHAR(45)      NOT NULL,      -- 45 caractères : assez pour une IPv6

    CONSTRAINT pk_positions PRIMARY KEY (id),
    CONSTRAINT uq_positions_message UNIQUE (camion_id, horodatage, seq),
    -- RESTRICT : impossible de supprimer un camion qui a des positions
    -- (on le désactive) ; CASCADE : si on renomme un camion, ses positions suivent
    CONSTRAINT fk_positions_camion FOREIGN KEY (camion_id) REFERENCES camions (id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    -- mêmes bornes que envoi.valider() : dernière barrière si le code change
    CONSTRAINT ck_positions_lat     CHECK (lat BETWEEN -90 AND 90),
    CONSTRAINT ck_positions_lon     CHECK (lon BETWEEN -180 AND 180),
    CONSTRAINT ck_positions_vitesse CHECK (vitesse BETWEEN 0 AND 200),
    CONSTRAINT ck_positions_cap     CHECK (cap >= 0 AND cap < 360),

    -- historique de toute la flotte sur une période (carte, rapports)
    INDEX ix_positions_horodatage (horodatage)
) ENGINE = InnoDB;

-- ---------------------------------------------------------------------------
-- utilisateurs (Phase 4 : connexion au site)
-- ---------------------------------------------------------------------------
-- mot_de_passe : le hash bcrypt (60 caractères, commence par $2a$, $2b$ ou $2y$
--   puis le coût, ex. $2b$12$...). Jamais le mot de passe en clair : le CHECK
--   refuse tout ce qui n'a pas la forme d'un hash bcrypt.
-- Un conducteur est rattaché à un camion (il ne voit que celui-là), un
-- gestionnaire à aucun (il voit toute la flotte) : vérifié par ck_utilisateurs_role.
CREATE TABLE IF NOT EXISTS utilisateurs (
    id            INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    email         VARCHAR(254)  NOT NULL,      -- 254 = longueur max d'une adresse mail
    mot_de_passe  CHAR(60)      NOT NULL,
    role          ENUM('gestionnaire', 'conducteur') NOT NULL,
    camion_id     VARCHAR(32)   NULL,

    CONSTRAINT pk_utilisateurs PRIMARY KEY (id),
    -- collation utf8mb4_unicode_ci : Jean@IUT.fr et jean@iut.fr sont le même compte
    CONSTRAINT uq_utilisateurs_email UNIQUE (email),
    -- ON UPDATE RESTRICT (et pas CASCADE) : MariaDB interdit d'utiliser dans un
    -- CHECK une colonne modifiée par un CASCADE, or ck_utilisateurs_role en a besoin
    CONSTRAINT fk_utilisateurs_camion FOREIGN KEY (camion_id) REFERENCES camions (id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT ck_utilisateurs_email CHECK (email LIKE '_%@_%._%'),
    CONSTRAINT ck_utilisateurs_bcrypt CHECK (
        CHAR_LENGTH(mot_de_passe) = 60
        AND mot_de_passe REGEXP '^[$]2[aby][$][0-9]{2}[$][./A-Za-z0-9]{53}$'),
    CONSTRAINT ck_utilisateurs_role CHECK (
        (role = 'gestionnaire' AND camion_id IS NULL) OR
        (role = 'conducteur'   AND camion_id IS NOT NULL))
) ENGINE = InnoDB;

-- ---------------------------------------------------------------------------
-- Vue : dernière position de chaque camion
-- ---------------------------------------------------------------------------
-- Même requête que REQUETE_DERNIERE_POSITION dans base.py. LEFT JOIN : un camion
-- qui n'a encore rien envoyé apparaît quand même (colonnes de position à NULL).
-- SQL SECURITY INVOKER : la vue s'exécute avec les droits de celui qui la lit.
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_derniere_position AS
SELECT c.id AS camion_id, c.immatriculation, c.actif,
       p.seq, p.horodatage, p.lat, p.lon, p.vitesse, p.cap, p.etat, p.recu_le
FROM camions c
LEFT JOIN positions p ON p.id = (
    SELECT p2.id FROM positions p2
    WHERE p2.camion_id = c.id
    ORDER BY p2.horodatage DESC, p2.seq DESC
    LIMIT 1
);
