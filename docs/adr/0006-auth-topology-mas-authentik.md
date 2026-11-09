# ADR-0006: Auth topology: Authentik as upstream IdP to MAS

- **Status:** Accepted (2026-06-19)
- **Context:** The revived bot targets Matrix 2.0. The auth chain is:
  *Matrix client → MAS (the Matrix-facing OAuth2/OIDC provider) → Authentik (upstream IdP)*.

## Decision

Treat **MAS** as the Matrix-facing authorization server and **Authentik** as an upstream IdP to
MAS. This reshapes scope:

- **No account pre-creation.** MAS auto-provisions Matrix accounts on first login, so the bot no
  longer pre-creates accounts (a whole category of legacy code and risk is deleted). Sync scope
  reduces to **group→room projection + membership + power levels** (plus quarantined lifecycle).
- **MXID mapping is owned by MAS.** The bot must compute a user's MXID using the **same localpart
  template MAS uses** (derived from the Authentik claim, e.g. `preferred_username`/`sub`). The
  legacy `authentik_username_mapping_attribute` must be defined to **agree with** MAS config, not
  guessed. This is the critical integration contract: get it wrong and users won't match.
- **Bot credentials under MAS:** an admin token for the Synapse Admin API plus a bot user for the
  CS API, obtained via `mas-cli manage issue-compatibility-token` (near-term) or OAuth2
  client-credentials + refresh (future). See ADR-0007 and `auth/token_provider.py`.

## Open questions (tracked in BATTLE_PLAN.md §7, both answered since, see below)

- Does MAS propagate Authentik deactivation (revoke sessions / lock account), or must the
  lifecycle module enforce it? (Defines Phase 5 scope.)
- What exact localpart rule will MAS use for Authentik-provisioned accounts?

## Consequences

- The identity-mapping code (`onbot/identity.py`) is a contract test target: it must match
  MAS's localpart template exactly.

## Updates

- **Deactivation (answered 2026-06-23, Phase 7b):** MAS does not propagate an upstream disable, and
  the Synapse admin API cannot end a MAS session. The lifecycle module enforces offboarding through
  the MAS admin API ([ADR-0005](0005-quarantine-lifecycle.md)).
- **Localpart rule:** onbot does not have to know it in advance any more. With `mas_admin` it resolves
  each Authentik user through the MAS upstream link (by subject), otherwise through the
  `authentik pk -> MXID` binding recorded at first sight; the mapping attribute is only the first
  guess for users never seen (review decision D8, session S9).
- **Bot credentials (review S11, RUN-11):** the OAuth2 client-credentials option was removed. MAS does
  not grant the Matrix API and Synapse admin scopes to `client_credentials`, so it never worked; the
  bot uses a MAS compatibility token (`scripts/provision-bot-account.sh`). The MAS admin API (`mas_admin`)
  still uses client credentials.
