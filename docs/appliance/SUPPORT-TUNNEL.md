# Tunnel de support

## Ce que c'est

Un accès distant temporaire que Ninabot Sàrl peut utiliser pour dépanner votre boîtier SOKKAN. Il repose sur Tailscale (WireGuard) : le boîtier établit lui-même une connexion sortante ; aucun port entrant n'est ouvert sur votre réseau.

## Règles

- **Fermé par défaut.** Le service Tailscale est arrêté tant que vous ne l'ouvrez pas.
- **Vous seul l'ouvrez**, pour une durée bornée, depuis le cockpit (Setup) ou en ligne de commande sur le boîtier :
  `sudo /opt/sokkan/scripts/appliance/support-tunnel.sh open 2` (2 heures).
- **Durée maximale : 8 heures par ouverture** (valeur par défaut, réductible via `SOKKAN_SUPPORT_MAX_HOURS`, jamais augmentée par nous à distance). Une ouverture est toujours bornée par une échéance.
- **Fermeture automatique** à l'échéance (contrôle chaque minute), et à chaque redémarrage du boîtier. Si le tunnel est trouvé actif alors qu'il devrait être fermé, il est coupé et l'événement est enregistré.

## Journal

Chaque ouverture, fermeture, expiration, échec ou coupure forcée est inscrite dans le journal d'audit SOKKAN, avec l'auteur, l'heure, la durée et l'échéance. Lecture : cockpit, Operate, Journal, actions `support.tunnel.*` (`opened`, `closed`, `expired`, `forced_closed`, `open_failed`, `request`). Les inscriptions sont reprises par l'api dans les 30 secondes, y compris pour une fermeture automatique.

## Ce que le fournisseur peut et ne peut pas faire

- **Tunnel ouvert** : connexion ssh au boîtier avec le compte de support, pour diagnostiquer et corriger. Les actions du support sur le boîtier restent celles d'un administrateur système ; leur contenu n'est pas enregistré par ce mécanisme.
- **Tunnel fermé** : aucun accès, aucune connexion possible.
- Le fournisseur ne peut pas ouvrir le tunnel lui-même.

## Couper immédiatement

Cockpit : bouton de fermeture. Ou sur le boîtier : `sudo /opt/sokkan/scripts/appliance/support-tunnel.sh close`. En dernier recours : débrancher le boîtier (le tunnel est fermé au redémarrage).

## Vérifier qu'il est fermé

Sur le boîtier : `tailscale status` doit répondre que Tailscale est arrêté, et `sudo /opt/sokkan/scripts/appliance/support-tunnel.sh status` doit afficher `"open": false`.

## Non couvert

Le trafic normal de l'application (connexions utilisateurs, appels sortants configurés par vous), le signal de supervision (voir HEARTBEAT.md), l'accès physique au boîtier, et toute intervention hors du tunnel (par exemple une session de dépannage que vous menez vous-même en partage d'écran).

## Point ouvert

Le bouton du cockpit est livré par l'API (`POST /api/appliance/support`) ; l'interface (bouton dans Setup) fait l'objet d'un lot séparé. D'ici là, utiliser la commande ci-dessus.
