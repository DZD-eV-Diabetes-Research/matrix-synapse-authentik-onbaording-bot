# matrix-synapse-authentik-onbaording-bot
A matrix.org synapse bot that welcomes and invites new users to pre configured rooms via the admin api based on authentik groups


# todo

* room_admin_attr - ongoing - next: testing first implentation
* room_encryption
* Update Synpase user attributes from authentik account
* Define custom rooms with only membership mapped to a usergroup
* for all api_clients* take pagination into account

# Features

* Welcome new users with messages
* Mirror Authentik groups as Synapse rooms
  * Auto create/un-block rooms when group is created/enabled
  * Auto remove rooms when group is deleted/disabled ("soft"-delete supported; Kick users and block room)
  * Selective group based on Authentik attribute
  * Membership mirroring (Add or kick user in Synapse room if added/removed from authentik group)
  * Selective membership mirroring based on group membership or attribute.
  * Logout disabled/deleted users
