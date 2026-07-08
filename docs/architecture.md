# Architecture

Onbot targets Matrix 2.0. It assumes a
[Matrix Authentication Service (MAS)](https://element-hq.github.io/matrix-authentication-service/)
deployment with Authentik as the upstream identity provider, uses authenticated media, and drives
the Client-Server and admin APIs over a single async HTTP client.

## The MAS auth topology

The auth chain is Matrix client to MAS to Authentik
([ADR-0006](adr/0006-auth-topology-mas-authentik.md)):

```
                   logs in via                      upstream IdP
   Matrix client ───────────────▶  MAS  ◀───────────────────────  Authentik
        ▲                           │  (provisions Matrix accounts   (source of truth:
        │                           │   on first login)               users & groups)
        │ welcome DM,               │
        │ room membership      ┌────┴─────┐
        └──────────────────────│  Onbot   │── reads users/groups ──▶ Authentik API
                               └────┬─────┘
                                    └── Synapse Admin API + CS API ──▶ Synapse  ◀─ MAS
```

## Consequences that shape configuration

Three properties of this topology drive how you configure Onbot:

- **Onbot does not create accounts.** MAS auto-provisions a Matrix account the first time a user
  logs in through Authentik. Onbot's job is projection: Authentik groups into rooms, group
  membership into room membership, group and role attributes into power levels, plus the quarantined
  offboarding lifecycle.
- **The MXID localpart contract is critical.** Onbot computes a user's MXID
  (`@<localpart>:server_name`) from an Authentik attribute, and it must match the localpart template
  MAS uses when it provisions accounts from the same Authentik claim. Set
  `sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute` to agree with MAS.
  Get it wrong and Onbot's computed MXIDs will not match the real accounts, so nobody is added to
  rooms.
- **Lifecycle enforcement requires MAS.** When Authentik disables a user, MAS blocks new logins but
  existing Matrix sessions keep working, and the Synapse admin API cannot revoke a MAS-issued
  session, only MAS can ([ADR-0005](adr/0005-quarantine-lifecycle.md)). So to offboard a disabled
  user you must configure the `mas_admin` block. Without it, offboarding is a no-op against live
  sessions.

## Architecture decision records

The ADRs in [docs/adr/](adr/) capture the reasoning behind these choices:

- [0001](adr/0001-clean-slate-reuse-logic.md) clean slate, reuse the proven logic
- [0002](adr/0002-reconciliation-not-events.md) reconciliation as the primary sync model
- [0003](adr/0003-onboarding-is-event-driven.md) onboarding is event-driven
- [0004](adr/0004-modular-monolith.md) modular monolith
- [0005](adr/0005-quarantine-lifecycle.md) quarantined offboarding lifecycle
- [0006](adr/0006-auth-topology-mas-authentik.md) the MAS to Authentik auth topology
- [0007](adr/0007-async-one-http-base-client.md) one async HTTP base client
- [0008](adr/0008-matrix-client-library.md) the Matrix client library choice
- [0009](adr/0009-e2ee-stance.md) the end-to-end encryption stance

For intent see [GOALS.md](../GOALS.md); for the build plan see [BATTLE_PLAN.md](../BATTLE_PLAN.md).
