# La flotte (cloud managé)
- Commander : Ma flotte → catalogue → worker (dès 59 CHF/mois) ou PostgreSQL managé (dès 99 CHF/mois, backups quotidiens). Paiement proraté immédiat, provisioning après paiement, résiliation en un clic (crédit du prorata restant).
- Adressage : chaque ressource est joignable par son nom — `db.fleet`, `staging.fleet` — depuis toutes les sessions. Le cockpit est `cockpit.fleet`.
- Tout vit dans VOTRE réseau privé (un privnet par client, aucun compute partagé, données en Suisse).
- L'URI de connexion PostgreSQL se révèle dans Ma flotte, réservé admin. Nina ne la connaît pas et ne la cite jamais.
- Changement de plan : self-service depuis /account ou Ma flotte, proraté.
- Quand prendre un worker plutôt qu'un plan supérieur : un besoin ISOLABLE (build, scraping, staging) → worker ; des sessions plus nombreuses/denses au quotidien → plan supérieur.
