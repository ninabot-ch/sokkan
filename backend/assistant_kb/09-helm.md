# Helm — piloter un projet (3.3)

**Helm** relie la carte projet du manager au kanban des ingénieurs. Une carte peut avoir
une **carte mère** : carte projet → cartes de l'équipe → sous-tâches. Ouvrir une carte
mère montre **son propre kanban** (ses cartes filles par colonne) avec un fil d'Ariane.

- **Le contexte descend** : l'intention, les contraintes, les décisions et les liens de la
  carte mère sont injectés dans chaque session ouverte depuis une carte fille (▶ spawn),
  et copiés dans une note mémoire du projet `helm-card-<id>` marquée `card:<id>`. On les
  édite dans la carte (section « Helm » du dialogue de carte).
- **L'avancement remonte tout seul** : l'état d'une carte mère est calculé depuis ses
  filles (à faire, en cours, en attente d'approbation, bloqué, fait) et leurs signaux
  (sessions qui travaillent, runs d'agents, merge requests liées, incidents Operate).
  Il n'est jamais déclaré à la main.
- **Onglet Helm** (managers = maintainer/admin du projet, et admins d'instance membres du
  projet) : toutes les cartes projet en deck, couleur + libellé par état, la carte
  « respire » quand une session ou un agent y travaille ; popout Kanban · Activité ·
  Suggestions · Coûts ; filtres par projet, équipe, personne.
- **Suggestions de recadrage** (toutes les 15 min + bouton « Check now ») : carte qui
  dérive de l'objectif, travail qui contredit une décision consignée, périmètre qui
  gonfle, cartes sans responsable, projet qui ralentit ou s'emballe, incident lié. Chaque
  suggestion attend **Approuver** (crée une carte « reframe » assignée au manager) ou
  **Ignorer** (pas reproposée pendant 7 jours). Rien n'est appliqué seul.
- **Brief du matin** : Crew → « + New agent » → modèle « Morning brief » (chaque jour
  ouvré 07:30, livré en note mémoire en quarantaine + notification). Agenda : une adresse
  ICS privée rangée dans le coffre et référencée par son nom (Microsoft Graph en 3.4).

## Si l'utilisateur veut créer un projet avec toi (Nina)

Tu ne crées rien toi-même : tu l'interviewes, **une question à la fois**, avec une valeur
par défaut raisonnable :
1. l'objectif (le résultat attendu, pour qui) ;
2. le périmètre (ce qui est dedans, ce qui est explicitement dehors) ;
3. les contraintes (techniques, réglementaires, budget) et les décisions déjà prises ;
4. le délai (date AAAA-MM-JJ) ;
5. l'équipe (emails des personnes).

Puis tu proposes une **décomposition** en 3 à 8 cartes filles, chacune avec un
responsable quand tu le connais. Récapitule en quelques lignes et termine par UN bloc de
cette forme (JSON valide) — le cockpit l'affiche modifiable avec un bouton « Create the
project » ; l'utilisateur ajuste les cartes avant de valider :

```sokkan-project
{"title": "Lecteur radio v2", "intent": "Les auditeurs écoutent le direct en HLS sur web et mobile",
 "scope": "web + app mobile ; pas de podcasts", "constraints": "WCAG AA ; pas de nouveau service backend",
 "deadline": "2026-11-30", "team": ["dan@example.ch"],
 "decisions": ["Pas de Kafka : les métadonnées restent sur l'API existante"],
 "children": [{"title": "Composant lecteur HLS web", "assignee": "dan@example.ch"},
              {"title": "Lecteur mobile", "description": "..."}]}
```
