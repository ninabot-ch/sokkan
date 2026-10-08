# Dépannage courant & escalade
- « Ma session ne démarre pas » : vérifier Setup › Engines (mode configuré ? solde > 0 en mode inclus ? clé valide en BYOK ?).
- « La session a oublié X » : la note existe-t-elle dans Mémoire ? La recherche la remonte-t-elle ? Sinon, écrire la note (un fait, une bonne description).
- « Je ne vois pas Operate › Infra → Ma flotte » : réservé au cloud managé, et la commande de ressources requiert le rôle admin.
- « No project yet » après la connexion SSO : l'instance ne vous liste pas et aucun projet ne vous donne de rôle (3.4.1, `SOKKAN_DEFAULT_ROLE=none` en édition enterprise). Un admin vous accorde un rôle dans Setup › Organization › Projects & teams (à vous, ou à une équipe que votre connexion porte) — rien d'autre à faire de votre côté.
- Mise à jour : le cockpit vérifie chaque jour et l'indique dans Setup › Organization.
- Ce que Nina ne fait pas : accéder au code du workspace, lire ou citer des credentials/URIs/variables d'env, exécuter des actions. Pour ces sujets : les écrans du cockpit, ou un humain.
- Escalade : hello@sokkan.ch — le fondateur répond. Pendant un essai, toute question est bienvenue.
