# Live Teams checks (real tenant)

Everything in `tests/test_teams*.py` runs against a simulator. These tests run against
**Microsoft** — skipped unless `SOKKAN_TEAMS_LIVE=1`. Run them on the day the tenant exists
(docs/enterprise/TEAMS.md « Day of the tenant »):

```bash
export SOKKAN_TEAMS_LIVE=1
export SOKKAN_TEAMS_APP_ID=… SOKKAN_TEAMS_TENANT_ID=… SOKKAN_TEAMS_APP_PASSWORD="$(cat teams-app-secret)"
export SOKKAN_TEAMS_LIVE_INSTANCE=https://sokkan.example.ch   # the instance Teams calls
export SOKKAN_TEAMS_LIVE_CHANNEL='19:…@thread.tacv2'          # a channel mapped to a project
export SOKKAN_TEAMS_LIVE_ADMIN_COOKIE=…                        # sokkan_session of an instance admin
# optional
export SOKKAN_TEAMS_LIVE_USER=test.user@tenant.example        # calendarView
export SOKKAN_TEAMS_LIVE_USER_OID=…                           # presence
export SOKKAN_TEAMS_LIVE_EXPECT_ROLES=Calendars.Read          # what admin consent granted
export SOKKAN_TEAMS_LIVE_SERVICE_URL=…                        # else: the last one Teams sent
python -m pytest tests/teams_live -v -s                        # automatic checks
SOKKAN_TEAMS_LIVE_MANUAL=1 python -m pytest tests/teams_live -v -s -m manual   # guided
```

| Test | Proves |
|---|---|
| `bot_framework_openid_metadata_and_keys` | issuer, JWKS, a key endorsed for `msteams` |
| `app_token_for_the_bot_connector` | single-tenant client credentials work (aud, tid, appid) |
| `app_token_for_graph_carries_only_the_granted_roles` | no Graph permission beyond the doc's two |
| `graph_calendar_of_a_test_user` / `graph_presence…` | the brief's Graph reads (optional permissions) |
| `manifest_of_this_app_is_valid` | the package for this app id / host passes the v1.17 schema |
| `instance_refuses_an_unsigned_activity` | the door: 401 without a Bot Framework JWT |
| `instance_admin_state_is_ready` | feature on, nothing missing, a channel mapped |
| `proactive_post_and_update_in_the_channel` | a new thread + PUT update (what proactive approvals do) |
| manual: mention, approve desktop / mobile, decision, proactive from the cockpit | the invoke payloads of real clients, four-eyes, thread links, card replaced after a cockpit decision |

Visual checks the tests cannot see (note them in the POC report): the card renders at full
width, Approve / Refuse are colored, a second viewer's card shows « Approved by … » after a
refresh, Teams' HTML around the @mention is cleaned (the answer quotes no `<at>` tag).
