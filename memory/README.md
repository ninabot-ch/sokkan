# SOKKAN — P0 : couche mémoire RAG (`memory-search`)

Recherche sémantique sur les notes mémoire Claude Code (`/root/.claude/projects/-root-ninabot-pro/memory/*.md`),
exposée comme **serveur MCP stdio** à toutes les sessions Claude Code. Fondation du moat « mémoire » de SOKKAN
(cf. `docs/sokkan/vision.md`), mais **utile en standalone dès aujourd'hui** : remplace le chargement de l'index
surdimensionné `MEMORY.md` par le retrieval des seules notes pertinentes.

## Composants
- `index_memory.py` — indexeur : parse les notes (frontmatter + corps), chunk, embed via le ML service ninjob
  (`POST $ML_SERVICE_URL/api/v1/embed/text`, MiniLM multilingue 384-dim, cross-lingual), stocke des vecteurs
  unit-normalisés dans SQLite. Incrémental sur `mtime`, prune les notes supprimées. **Régénère aussi `MEMORY.md`**
  depuis les `description:` frontmatter (une ligne par note, cap adaptatif pour tenir sous ~24KB) — l'index
  session est un artefact dérivé, ne pas l'éditer à la main. Écriture seulement si contenu changé (pas de
  boucle avec le `.path` inotify).
- `memory_search_server.py` — serveur MCP (`mcp.server.fastmcp.FastMCP`) : outils `memory_search(query, top_k)`
  et `memory_get(note_name)`. Cosine = dot-product en pur Python (corpus minuscule).
- `sokkan-memory-index.{service,timer}` — réindexation quotidienne (03:30).

## Store
- SQLite local : `/root/.local/share/sokkan/memory.db` (hors repo, hors `ninjob-db`). Tables `notes`, `chunks`, `meta`.

## Setup (gmk1)
```bash
python3 -m venv /opt/sokkan/venv
/opt/sokkan/venv/bin/pip install -r infra/sokkan/memory/requirements.txt
# 1er index
ML_SERVICE_URL=http://rog1:8001 /opt/sokkan/venv/bin/python infra/sokkan/memory/index_memory.py
# timer
cp infra/sokkan/memory/sokkan-memory-index.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now sokkan-memory-index.timer
```

## Enregistrement MCP
`.mcp.json` (racine du repo) déclare le serveur `sokkan-memory`. Claude Code demande d'approuver le serveur
MCP du projet à la 1re utilisation (relancer / `/mcp` pour vérifier).

## Réindex manuel
```bash
/opt/sokkan/venv/bin/python infra/sokkan/memory/index_memory.py            # incrémental
/opt/sokkan/venv/bin/python infra/sokkan/memory/index_memory.py --rebuild  # complet
```
