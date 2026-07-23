# Le cockpit, écran par écran
- **Sessions** : rail des sessions vivantes + grille de chat multi-panneaux. Les tool calls, demandes de permission et questions s'affichent en widgets natifs (boutons) — rien d'irréversible sans clic. Une session = un agent Claude Code travaillant dans le workspace.
- **Board** : kanban. Une carte décrit une tâche (description, priorité, échéance, checklist) ; « ▶ spawn » transforme la carte en session pré-contextée : la description sème une recherche mémoire, l'agent propose un plan et attend le go.
- **Mémoire** : la base RAG du projet — notes, liens [[...]], backlinks, stats, et un terrain d'essai de recherche qui montre exactement ce qu'une session rappellerait.
- **Coûts** : tokens et coût estimé par jour et par session, agrégés depuis les transcripts. Fenêtres 1j/7j/30j.
- **Journal** : audit de toutes les actions (qui a spawné/déplacé/supprimé quoi).
- **Ma flotte** (cloud) : ressources actives (workers, PostgreSQL managé), état en direct, catalogue pour commander (admin requis), terminal de maintenance (admin/owner, audité).
- **Profil → Modèle** : mode d'inférence (BYOK ou incluse par crédits), solde et usage.
- Rôles : viewer < dev < admin < owner. Spawner/prompter = dev ; gérer les users et commander la flotte = admin.
