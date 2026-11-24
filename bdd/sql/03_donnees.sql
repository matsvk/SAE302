-- 03_donnees.sql - les camions de la flotte (mêmes id que config.json)
-- Relançable : un camion déjà présent garde son état "actif" actuel.

USE flotte;

INSERT INTO camions (id, immatriculation, actif) VALUES
    ('CAM-01', 'FL-301-RT', TRUE),
    ('CAM-02', 'FL-302-RT', TRUE),
    ('CAM-03', 'FL-303-RT', TRUE),
    ('CAM-04', 'FL-304-RT', TRUE)
ON DUPLICATE KEY UPDATE immatriculation = VALUES(immatriculation);

-- Les comptes du site (table utilisateurs) seront créés en Phase 4 par le site
-- lui-même, qui calcule le hash bcrypt : on ne met jamais de hash "en dur" ici.
