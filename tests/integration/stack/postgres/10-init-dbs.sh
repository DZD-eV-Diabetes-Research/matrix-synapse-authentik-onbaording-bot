#!/bin/bash
# Postgres init: create the three databases the stack needs on one server.
# Synapse requires C collation/ctype (it refuses to start otherwise), so its DB is created
# from template0 with an explicit locale. MAS and Authentik are happy with the defaults.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
	CREATE DATABASE synapse
	  WITH ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
	CREATE DATABASE mas;
	CREATE DATABASE authentik;
EOSQL
