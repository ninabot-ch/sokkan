# Signal de supervision (heartbeat)

## A quoi ça sert

Le boîtier SOKKAN peut envoyer à Ninabot Sàrl un petit message de vie, par exemple toutes les 15 minutes, afin que nous détections une panne (boîtier éteint, sauvegarde en échec, disque presque plein) avant que vous ne la constatiez. Le message ne contient aucun contenu de votre entreprise.

## Activation et désactivation

- **Désactivé par défaut** : rien n'est envoyé tant que la variable `SOKKAN_HEARTBEAT_URL` est vide.
- Il est activé uniquement si l'opérateur renseigne `SOKKAN_HEARTBEAT_URL` (et, si besoin, `SOKKAN_HEARTBEAT_TOKEN`, `SOKKAN_INSTANCE_ID`) dans le fichier d'environnement du boîtier.
- Pour le désactiver à tout moment : `SOKKAN_HEARTBEAT=0` (prioritaire sur l'URL), ou vider `SOKKAN_HEARTBEAT_URL`, puis redémarrer l'api (`docker compose up -d api`).
- Fréquence : `SOKKAN_HEARTBEAT_INTERVAL_S` (défaut 900 secondes, minimum 60). Premier envoi 60 secondes après le démarrage. Un échec d'envoi est silencieux et n'affecte pas le fonctionnement.

## Champs envoyés (liste exhaustive)

| Champ | Type | Exemple | Ce que cela révèle | Pourquoi nous en avons besoin |
|---|---|---|---|---|
| `schema` | entier | `1` | Version du format du message | Faire évoluer le format sans rien casser |
| `instance` | texte | `client-acme-01` | Nom opaque choisi par l'opérateur, ou identifiant aléatoire généré sur le boîtier | Savoir de quel boîtier vient le signal, sans nom d'entreprise obligatoire |
| `ts` | entier | `1762876800` | Heure d'émission (epoch, UTC) | Détecter l'absence de signal |
| `version` | texte | `3.5.1` | Version logicielle installée | Planifier les mises à jour, savoir quel correctif s'applique |
| `commit` | texte | `a1b2c3d` | Révision exacte du code livré (vide si inconnue) | Reproduire un comportement précis |
| `edition` | texte | `enterprise` | Édition du produit | Appliquer les bonnes consignes de support |
| `uptime_s` | entier | `86400` | Durée de fonctionnement du service api | Repérer des redémarrages inattendus |
| `health` | objet | `{"api": true, "db": true, "inference": null}` | Services joignables (api, base de données, moteur d'inférence ; `null` = non mesuré) | Voir quel composant est en panne |
| `backup_age_s` | entier ou null | `7200` | Ancienneté de la dernière sauvegarde réussie | Vérifier que vos sauvegardes tournent |
| `backup_ok` | booléen ou null | `true` | Dernière tentative de sauvegarde réussie ou non | Idem |
| `disk_free_pct` | nombre ou null | `42.5` | Pourcentage d'espace libre du volume de données | Prévenir une saturation du disque |
| `disk_free_gb` | nombre ou null | `210.3` | Espace libre en Go | Idem |
| `memory_review_score` | nombre ou null | `92` | Note de santé de la mémoire documentaire (un seul chiffre) | Détecter une dégradation de la mémoire sans en lire le contenu |
| `support_tunnel_open` | booléen | `false` | Le tunnel de support est-il ouvert | Vous et nous savons si un accès est actif |

## Ce qui n'est jamais envoyé

Nom de machine, adresse IP, adresse e-mail, noms ou nombre de projets, de sessions, de notes ou d'utilisateurs, contenu de conversation ou de document, clés et secrets. L'adresse IP source apparaît seulement dans les journaux techniques du serveur récepteur, comme pour toute requête HTTPS.

## Voir exactement ce qui part

Dans le cockpit (administrateur) ou en ligne de commande :

```
curl -s -H "Authorization: Bearer <jeton admin>" https://<votre-boitier>/api/appliance/heartbeat
```

La réponse contient `state` (activé ou non, hôte destinataire, dernier envoi) et `payload` : le message exact qui serait envoyé.

## Points ouverts

- Durée de conservation des signaux chez Ninabot Sàrl : à définir contractuellement.
- Hébergement, localisation et accès au serveur récepteur : à préciser dans le contrat.
