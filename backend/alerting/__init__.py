"""alerting — SOKKAN 3.5 « Operate › Alerts »: a rule engine in the spirit of ElastAlert
(threshold, any, frequency, spike, flatline, change, new_term, cardinality, absence,
anomaly) over Prometheus, Loki, Elasticsearch / OpenSearch and SOKKAN's own events, with a
backtest the UI draws, routing to Telegram / Teams / Slack / e-mail / webhook / PagerDuty and
human-gated actions (incident, agent, runbook, diagnosis session).

Modules: durations (\"5m\" → 300), store (sqlite alerting.db), sources (adapters + query
builder), engine (rule types, backtest), state (alert state machine), channels (delivery),
templates (ready-made rules), scheduler (evaluation loop, one leader), api (HTTP routes).
"""
