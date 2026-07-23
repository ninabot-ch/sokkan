# Nouveautés — 1.2 « Open helm » et 1.3 « Companion » (juillet 2026)

**Multi-modèles (1.2)** : les sessions tournent sur le moteur Claude Code, mais le
modèle derrière est configurable — Profil → Modèle → « Other provider » accepte
tout endpoint compatible API Anthropic : Kimi (Moonshot), GLM (Z.AI), DeepSeek,
ou un modèle local derrière un proxy LiteLLM/Ollama. Presets fournis ; appliqué
à chaque nouvelle session, sans redémarrage. (Sur une instance en inférence
incluse, le modèle est géré par l'opérateur — pas de bascule depuis le cockpit.)

**Mémoire (1.2)** :
- `priority: high` dans le frontmatter d'une note = boost au recall, ★ dans le
  cockpit, en tête du MEMORY.md généré. À réserver aux faits durables
  (conventions, contraintes dures).
- Bouton **✎ digest** (onglet Memory/KB) : spawne une session qui condense la
  mémoire + l'historique git récent dans une note `project-status`.
- Bouton **⬡ graph** : le graphe des `[[wikilinks]]` entre notes, cliquable.

**Exemple prêt à l'emploi (1.2)** : `examples/fastapi-notes/` dans le repo — une
petite API FastAPI + ses notes mémoire + un script qui seed 3 cartes kanban.
Idéal pour montrer le recall en 2 minutes.

**CLI compagnon (1.3)** : `pipx install "git+https://github.com/ninabot-ch/sokkan"`
→ commande `sokkan` : `login`, `spawn "tâche"`, `status`, `sessions`, `board`,
`card --spawn`, `mem "question"`, `note`, `digest`, `health`. Auth par token
local ; les approbations restent dans le cockpit — la frontière HITL ne bouge pas.

**Divers** : `scripts/doctor.sh` (diagnostic d'installation en lecture seule),
images arm64 vérifiées en CI, GitHub Discussions ouvertes sur le repo.

**Nina & souveraineté** : sur les instances SOKKAN Cloud, l'inférence de Nina
(cet assistant) est **obligatoirement** servie par l'API IA d'Infomaniak,
hébergée en Suisse — règle de la maison : dès que l'assistante a accès au
contexte d'un client, l'inférence suisse est non négociable. Le contenu des
échanges n'est ni conservé ni utilisé pour entraîner. En self-host, Nina
utilise le modèle que vous configurez.
