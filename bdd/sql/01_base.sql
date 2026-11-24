-- 01_base.sql - création de la base "flotte"
-- Lancé par creer_bdd.sh (en root). utf8mb4 = vrai UTF-8 (accents, emojis...).

CREATE DATABASE IF NOT EXISTS flotte
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
