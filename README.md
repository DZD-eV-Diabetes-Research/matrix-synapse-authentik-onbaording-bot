# matrix-synapse-authentik-onbaording-bot
A matrix.org synapse bot that welcomes and invites new users to pre configured rooms via the admin api based on authentik groups


# todo

* room_admin_attr
* room_encryption
* Block unmapped rooms
  * unblock remapped rooms
* Logout disabled/deleted users
* Update Synpase user attributes from authentik account

# Features

* Welcome new users with messages
* Mirror Authentik groups as Synapse rooms
  * Auto create/un-block rooms when group is created/enabled
  * Auto remove rooms when group is deleted/disabled ("soft"-delete supported; Kick users and block room)
  * Selective group based on Authentik attribute
  * Membership mirroring (Add or kick user in Synapse room if added/removed from authentik group)
  * Selective membership mirroring based on group membership or attribute.
