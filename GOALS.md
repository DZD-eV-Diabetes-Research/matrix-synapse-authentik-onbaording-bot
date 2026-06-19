# Project Goals — Onbot

*What this project aims to achieve.* This document captures **all** goals of the project, gathered from the
README, the source code, the configuration model and its descriptions, and inline comments/TODOs. It is a
statement of intent — it deliberately makes **no claim about what is or isn't implemented yet**. The *how* and
*in what order* lives in [BATTLE_PLAN.md](BATTLE_PLAN.md).

---

## 0. Vision

Keep a Matrix (Synapse) homeserver continuously and automatically in sync with an
[Authentik](https://goauthentik.io/) identity provider, and give every new user a friendly, guided onboarding
into the chat — so that an organization's chat structure (rooms, memberships, permissions) is a faithful,
hands-off projection of its central user directory.

The two pillars:
1. **Sync** — Authentik is the single source of truth; Matrix mirrors it.
2. **Onboard/Info-Bot** — every provisioned user is welcomed and guided into the right rooms and gets important infos.

---

## 1. User ↔ account synchronization

- **G1.1** Treat Authentik as the authoritative source of users; reflect Authentik accounts into Matrix.
- **G1.2** Map an Authentik user to a deterministic Matrix user ID (MXID), with the username source being
  configurable (a fixed Authentik field or a nested/custom attribute path).
- **G1.3** Ensure synced users are members of the bot-managed parent space.
- **G1.4** Support updating Matrix user attributes from the linked Authentik account (e.g. profile data). *(stated TODO)*

## 2. Group → room mirroring

- **G2.1** Mirror each relevant Authentik group as a Matrix room.
- **G2.2** Automatically **create** a room when its mapped Authentik group appears/is enabled.
- **G2.3** Automatically **unblock** a previously blocked room when its group reappears/is re-enabled.
- **G2.4** Automatically **retire** a room when its mapped Authentik group disappears/is disabled:
  - **G2.4a** Soft-delete: kick all members and block the room.
  - **G2.4b** Hard-delete (optional): purge/delete the room entirely.
- **G2.5** Be **selective** about which groups become rooms, via configurable filters:
  - by a required Authentik attribute (e.g. `is_chatroom: true`),
  - by group-name prefix,
  - by parentage (only children of given group UIDs),
  - by an explicit group-ID ignore list.

## 3. Room membership mirroring

- **G3.1** Keep Matrix room membership in step with Authentik group membership: add a user to a room when they
  join the mapped group.
- **G3.2** Remove (kick) a user from a room when they leave the mapped group (optional/toggleable).
- **G3.3** Make membership mirroring **selective** — scope which users are synced by:
  - Authentik path(s),
  - required user attributes,
  - membership in specific group IDs,
  - a user ignore list (Authentik-side and Matrix-side).
- **G3.4** Support rooms whose **membership only** is mapped to a user group (membership-driven rooms,
  decoupled from the group→room creation rules). *(stated TODO)*

## 4. Onboarding & welcome & Info

- **G4.1** Create a direct (1:1) room with each new user.
- **G4.2** Send a configurable sequence of welcome/onboarding messages.
- **G4.3** Make welcome delivery **idempotent** — each message is sent once per user and never re-sent.
- **G4.4** Guide users on chat basics and (critically) on securing their encryption key backup.
- **G4.5** Place onboarding direct rooms within the managed space where appropriate.
- **G4.6** Have a controll room for defined admins, in which messages can be send to resend to all users thought the bot e.g. "Maintenance today at 11:30 to 12:00". (Not sure about this idea. maybe we can find a better mechanism to solve this problem)

## 5. Spaces & room organization

- **G5.1** Optionally gather all managed group-rooms under a dedicated **parent Matrix space**.
- **G5.2** Create that parent space automatically if it does not exist (optional/toggleable).
- **G5.3** Configure the space's alias, display name, topic, avatar, and creation parameters.

## 6. Room appearance & attributes

- **G6.1** Derive a room's **alias** from a configurable Authentik attribute, with an optional prefix.
- **G6.2** Derive a room's **name** from a configurable Authentik attribute, with an optional prefix.
- **G6.3** Derive a room's **topic** from a configurable Authentik attribute, with an optional prefix.
- **G6.4** Set a room's **avatar** from a URL held in a configurable Authentik attribute.
- **G6.5** Continuously **keep room name/topic updated** to match the Authentik source when it changes
  (optional/toggleable).
- **G6.6** Allow arbitrary room **creation parameters** (preset, visibility, federation, etc.), both as
  defaults and pulled from a per-group Authentik attribute (as JSON).
- **G6.7** Allow **per-group overrides** of any room setting, keyed by Authentik group primary key.
- **G6.8** Set the **bot's own avatar** on startup from a configured URL.
- **G6.9** Set the **space's** name/topic/avatar (space attribute upkeep). *(partially noted as TODO)*

## 7. Encryption

- **G7.1** Create group rooms with **end-to-end encryption** enabled (toggleable, default on).
- **G7.2** Persist/manage the bot's encryption keys/state, with an optional passphrase to encrypt the key store.
- **G7.3** Treat room encryption configuration as a first-class, manageable concern. *(stated TODO: `room_encryption`)*

## 8. Permissions / power levels

- **G8.1** Set per-room Matrix **power levels** for users based on Authentik group custom attributes
  (an integer 0–100 carried on a group).
- **G8.2** Grant Matrix **room admin** to Authentik superusers (optional/toggleable).
- **G8.3** On conflicting power-level rules across multiple group memberships, apply the **highest** value.
- **G8.4** **Withdraw** elevated power levels when the granting Authentik group membership is lost
  (stated as a desired future capability — current behavior leaves them sticky).
- **G8.5** Support a room-admin-from-attribute mechanism. *(stated TODO: `room_admin_attr`)*

## 9. Account lifecycle / offboarding

- **G9.1** Detect Matrix accounts whose Authentik account is **disabled or deleted**.
- **G9.2** **Log out** such users (revoke their sessions/devices) so they are locked out of chat.
- **G9.3** **Deactivate** such accounts after a configurable cooldown delay (to absorb accidental disables).
- **G9.4** **Delete/erase** such accounts after a further configurable delay (optional).
- **G9.5** Optionally **delete the user's uploaded media** on account deletion, to meet data-protection rules.
- **G9.6** Support **re-enabling** a user (and their onboarding direct room) if they return before deletion.

## 10. Media handling

- **G10.1** Upload remote media (avatars) into the Matrix media repository.
- **G10.2** **Deduplicate** uploads — avoid re-uploading media already present (content/URL hashing).

## 11. Configuration & operability

- **G11.1** Be fully configurable via a **YAML file** and via **environment variables** (env overrides).
- **G11.2** **Generate** a documented, example configuration file on demand.
- **G11.3** Provide rich, self-documenting config descriptions and examples for every option.
- **G11.4** Let the bot reach Synapse via an **internal URL**, so the Synapse Admin API need not be public.
- **G11.5** Run on a **configurable polling/sync interval**.
- **G11.6** Configurable **log level**.
- **G11.7** Persist runtime state to a configurable **storage directory**.
- **G11.8** Authenticate to Synapse (Matrix + Admin APIs) and to Authentik via tokens.
- **G11.9** Handle **API pagination** across all clients so large directories are fully processed.
  *(stated TODO: "for all api_clients take pagination into account")*

## 12. Scoping & safety controls

- **G12.1** Ignore lists for Matrix users, Authentik users, and Authentik group IDs (protect admin/system
  accounts and rooms from automated management).
- **G12.2** Every destructive or sweeping behavior (kick, block, delete, deactivate) is **opt-in/toggleable**.
- **G12.3** Cooldown delays on destructive account actions to mitigate accidental changes upstream.

---

## 13. Quality & platform goals

Beyond features, the revived project targets:

- **Q1** Run against the **modern Matrix protocol** and Matrix 2.0 features (next-gen auth / MAS, authenticated
  media, sliding sync).
- **Q2** Integrate cleanly with **MAS using Authentik as an upstream IdP** (auto-provisioned accounts;
  MXID mapping consistent with MAS).
- **Q3** Be **tidy and maintainable** — clear architecture, no dead code, no secrets in the repo.
- **Q4** Be **well-tested** — unit, contract, and integration tests (against a live Matrix/MAS/Authentik stack).
- **Q5** Use **current, pinned dependencies** and a modern packaging/build setup.
- **Q6** Be **safely operable** — idempotent behavior, graceful shutdown, structured logging, dry-run for
  destructive actions, and clear documentation.
</content>
