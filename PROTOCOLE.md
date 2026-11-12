# Protocole de communication camions → serveur

SAÉ 3.02 – Suivi de flotte géolocalisée – version 1 du protocole

Ce document décrit les messages échangés entre les camions (`simulateur.py`,
`envoi.py`) et le serveur (`serveur_udp.py`), les vérifications faites à la
réception et les codes d'erreur renvoyés.

## 1. Transport

- UDP, port 5005 par défaut (`serveur_config.json` côté serveur,
  `config.json` côté simulateur).
- Un message = un datagramme, contenu en **JSON encodé en UTF-8**.
- Taille maximale acceptée : **512 octets**. Un message de position fait
  environ 165 octets.
- Chaque camion envoie sa position **toutes les 3 secondes**
  (`periode_envoi` dans `config.json`).

Pourquoi utiliser UDP : une position n'est utile que quelques secondes. Si un message
est perdu, le suivant le remplace 3 secondes plus tard, donc il n'est pas
nécessaire de le renvoyer. UDP évite aussi de maintenir une connexion par
camion, ce qui compte sur une liaison mobile (4G) qui coupe souvent. En
contrepartie, UDP ne garantit ni la livraison, ni l'ordre, ni l'absence de
doublons : le numéro de séquence `seq` sert à gérer ces trois cas côté serveur.

## 2. Message de position (camion → serveur)

Exemple :

    {"v":1,"type":"position","id":"CAM-01","seq":42,"horodatage":"2026-11-02T21:30:12.250Z",
     "lat":48.947312,"lon":2.247851,"vitesse":72.4,"cap":135.0,"etat":"en_route"}

| Champ | Type | Contraintes | Description |
|---|---|---|---|
| `v` | entier | = 1 | version du protocole |
| `type` | texte | = `position` | type de message |
| `id` | texte | lettres, chiffres, `-` et `_`, 1 à 32 caractères | identifiant du camion |
| `seq` | entier | ≥ 0 | numéro de séquence, +1 à chaque message, repart à 0 au redémarrage du camion |
| `horodatage` | texte | date ISO 8601 (`Z` = UTC) | moment de la mesure |
| `lat` | nombre | -90 à 90 | latitude en degrés décimaux (6 décimales, environ 10 cm) |
| `lon` | nombre | -180 à 180 | longitude en degrés décimaux |
| `vitesse` | nombre | 0 à 200 | vitesse en km/h |
| `cap` | nombre | 0 inclus à 360 exclu | direction, 0 = nord, 90 = est |
| `etat` | texte | voir ci-dessous | état du camion |

États possibles : `a_quai` (arrêté au dépôt), `en_route`, `livraison`
(arrêté à destination), `termine` (fin de service).

Tous les champs sont obligatoires et aucun autre champ n'est accepté. Les
booléens (`true`/`false`) ne sont pas acceptés à la place des nombres. Un
horodatage sans fuseau horaire est considéré comme étant en UTC.

Le format est vérifié par la même fonction des deux côtés : `valider()` dans
`envoi.py`. Le camion vérifie ce qu'il envoie, le serveur vérifie ce qu'il
reçoit.

## 3. Vérifications faites par le serveur

Le serveur ne fait jamais confiance à ce qu'il reçoit : en UDP, n'importe qui
sur le réseau peut envoyer un datagramme sur le port 5005. Les vérifications
sont faites dans cet ordre, et la première qui échoue arrête le traitement :

| Ordre | Vérification | Si elle échoue |
|---|---|---|
| 1 | taille du datagramme ≤ 512 octets | rejet `trop_long` |
| 2 | UTF-8 et JSON lisibles | rejet `json_illisible` |
| 3 | format du message (champs, types, bornes, état, horodatage) | rejet `format_invalide` |
| 4 | camion présent dans `camions_autorises` | rejet `non_autorise` |
| 5 | numéro de séquence (doublon ou message en retard) | message **ignoré**, sans réponse |
| 6 | cohérence avec la position précédente | rejet `position_incoherente` |

Si la liste `camions_autorises` est vide dans `serveur_config.json`, tous les
identifiants sont acceptés (le serveur l'indique au démarrage).

## 4. Réponse d'erreur (serveur → camion)

Le serveur **n'acquitte pas** les messages corrects : un accusé de réception
doublerait le trafic pour rien, puisqu'un message perdu est de toute façon
remplacé par le suivant. Il répond seulement quand un message est refusé :

    {"v": 1, "type": "erreur", "code": "non_autorise",
     "detail": "camion INTRUS-99 absent de camions_autorises", "seq": 6}

- `seq` reprend le numéro du message refusé s'il était lisible, sinon il vaut `null`.
- `detail` fait au plus 150 caractères. Les caractères de contrôle (retours à
  la ligne...) sont remplacés par `?`, pour qu'un message piégé ne puisse pas
  fabriquer de fausses lignes dans les logs.

### Codes d'erreur

| Code | Cause | Exemple |
|---|---|---|
| `trop_long` | datagramme de plus de 512 octets | message rempli de données inutiles |
| `json_illisible` | pas de l'UTF-8 ou pas du JSON | `Bonjour`, JSON tronqué |
| `format_invalide` | message mal formé : champ manquant ou en trop, mauvais type, valeur hors limites, état inconnu, horodatage invalide | `"lat": 95`, `"seq": "7"`, `"etat": "vole"`, id contenant `'` ou `;` |
| `non_autorise` | camion inconnu : message correct mais `id` absent de `camions_autorises` | `"id": "INTRUS-99"` |
| `position_incoherente` | le camion aurait roulé à plus de 200 km/h depuis sa position précédente | Villetaneuse → Marseille en 3 s |

### Limitation des réponses

Les réponses d'erreur sont limitées à **5 par seconde**, tous expéditeurs
confondus (`max_reponses_par_seconde`, 0 = jamais de réponse). Sans cette
limite, quelqu'un qui usurpe l'adresse IP d'une victime pourrait se servir du
serveur pour l'inonder de réponses. On peut aussi désactiver complètement les
réponses avec `"reponses_erreur": false`.

Dans tous les cas, le refus est écrit dans les logs (ligne `REJET <code>`) et
compté dans le bilan du serveur.

## 5. Suivi des camions par le serveur

Le serveur garde pour chaque camion le dernier `seq` accepté et la dernière
position acceptée.

| Situation | Détection | Action |
|---|---|---|
| message normal | `seq` = dernier + 1 | accepté |
| messages perdus | `seq` > dernier + 1 | accepté, l'écart est compté comme perdu et noté dans les logs |
| doublon ou message en retard | `seq` ≤ dernier | ignoré (compté), pas de réponse : c'est un comportement normal d'UDP, pas une attaque |
| camion redémarré | `seq` = 0 alors que le dernier était > 0 | accepté, le suivi repart de zéro |
| déplacement impossible | plus de 50 m parcourus à plus de 200 km/h, calculé avec les horodatages des messages | rejet `position_incoherente` |
| changement d'adresse | IP ou port différents du message précédent | accepté, noté dans les logs |
| camion silencieux | aucun message depuis 30 s (`delai_silence`) | alerte dans les logs, puis « de nouveau en ligne » quand il revient |

La cohérence des positions utilise les horodatages des messages et non l'heure
de réception : le contrôle reste juste quand le simulateur est accéléré.

Un seul socket UDP reçoit les messages de tous les camions. Trois threads se
partagent le travail :

- **réception** : lit les datagrammes et les place dans une file d'attente ;
- **traitement** : vérifie chaque message et met à jour l'état du camion ;
- **surveillance** : repère les camions silencieux et affiche un bilan régulier.

La réception n'est donc jamais bloquée par le traitement. Si la file d'attente
est pleine (10 000 messages), les nouveaux datagrammes sont abandonnés et comptés.

## 6. Tests

`test_protocole.py` envoie une série de faux messages au serveur (mal formés,
piégés, incohérents) et vérifie que chacun est refusé, et que les vrais
messages passent toujours :

    python3 test_protocole.py                              (format seul, sans réseau)
    python3 test_protocole.py --serveur serveur_udp.py     (avec le vrai serveur)
    python3 test_protocole.py --demo --hote <ip> --pause 1 (démonstration en soutenance)

## 7. Sécurité : limites connues

Ce protocole **n'est ni chiffré ni authentifié**. N'importe qui sur le chemin
réseau peut lire les positions. Quelqu'un qui connaît l'identifiant d'un
camion peut aussi envoyer un message bien formé et cohérent en son nom. Les
vérifications rendent la falsification difficile, pas impossible.

D'autres limites sont connues :

- un ancien message avec `seq` = 0, rejoué, est pris pour un redémarrage du
  camion et remet son suivi à zéro ;
- un horodatage très loin dans le futur ou dans le passé est accepté.

Pistes d'amélioration :

- ajouter une signature **HMAC-SHA256** à chaque message, avec une clé
  secrète par camion (protège contre les faux messages) ;
- utiliser **DTLS** (TLS pour UDP) pour chiffrer en plus ;
- faire passer les camions par un **VPN** ;
- comparer les horodatages pour reconnaître un vrai redémarrage, et refuser
  les horodatages trop éloignés de l'heure du serveur.

## 8. Évolution du protocole

Le champ `v` permet de faire évoluer le format : un message d'une autre version
est refusé (`format_invalide`) au lieu d'être mal interprété. Le champ `type`
permettra d'ajouter d'autres messages plus tard (alerte, fin de livraison...).
