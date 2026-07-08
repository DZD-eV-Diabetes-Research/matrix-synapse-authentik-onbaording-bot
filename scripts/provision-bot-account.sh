#!/usr/bin/env bash
# provision-bot-account.sh — create the onbot's Matrix account on a Matrix 2.0 / MAS stack
# and mint the access token it needs for config.dev.yml.
#
# Why this exists: under Matrix 2.0 auth is delegated to MAS (Matrix Authentication
# Service). You cannot create the bot via Synapse's register_new_matrix_user nor get a
# token via the password /login endpoint (SSO-only stacks have no password login at all).
# Instead the bot is created in MAS and handed a long-lived *compatibility token* that both
# acts as the bot user on the client-server API AND carries the Synapse admin scope
# (urn:synapse:admin:*) — exactly the single token onbot shares between its CS + admin
# clients (see onbot/app.py:build_matrix_token_provider).
#
# This wraps `mas-cli` in the Matrix stack's docker-compose project. Run it ON the docker
# host (or anywhere `docker compose exec mas` reaches the MAS container).
#
# WIZARD:      run with no args -> interactive prompts (sensible defaults pre-filled).
# AUTOMATION:  pass every answer as a flag and add --yes to skip all prompts. With --quiet
#              only the bare token is printed to stdout, e.g.
#                  TOKEN=$(scripts/provision-bot-account.sh -m /srv/matrix -u dzd-bot -y -q)
#
# Usage:
#   provision-bot-account.sh [options]
#
# Options:
#   -m, --matrix-dir DIR   Matrix stack dir (has .env + docker-compose.yml + a `mas` service).
#                          Defaults to $ONBOT_MATRIX_DIR, then the current directory.
#   -u, --username NAME    Bot localpart (default: dzd-bot). MXID becomes @NAME:<server_name>.
#   -d, --display-name STR Matrix display name (default: "DZD Onboarding Bot").
#       --synapse-admin    Grant the token the Synapse admin scope (default: on — onbot needs it).
#       --no-synapse-admin Issue a plain user token without admin scope (rooms/DMs only).
#       --reissue-only     Do NOT (re)create the user, only issue a fresh token for it.
#       --write-token-to F Also write the bare token to file F (chmod 600). "-" means stdout.
#   -y, --yes              Non-interactive: never prompt, use flags/defaults, fail if ambiguous.
#   -q, --quiet            Print ONLY the token to stdout (all logs to stderr). Implies --yes.
#   -h, --help             Show this help.
#
# Exit status: 0 on success (token issued), non-zero otherwise.
set -euo pipefail

# ---------------------------------------------------------------------------- defaults / args
MATRIX_DIR="${ONBOT_MATRIX_DIR:-}"
USERNAME="dzd-bot"
DISPLAY_NAME="DZD Onboarding Bot"
SYNAPSE_ADMIN=true
REISSUE_ONLY=false
WRITE_TOKEN_TO=""
ASSUME_YES=false
QUIET=false

log() { printf '%s\n' "$*" >&2; }   # everything human-facing goes to stderr so stdout is clean
die() { log "ERROR: $*"; exit 1; }

usage() { sed -n '2,54p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        -m|--matrix-dir)   MATRIX_DIR="$2"; shift 2 ;;
        -u|--username)     USERNAME="$2"; shift 2 ;;
        -d|--display-name) DISPLAY_NAME="$2"; shift 2 ;;
        --synapse-admin)   SYNAPSE_ADMIN=true; shift ;;
        --no-synapse-admin) SYNAPSE_ADMIN=false; shift ;;
        --reissue-only)    REISSUE_ONLY=true; shift ;;
        --write-token-to)  WRITE_TOKEN_TO="$2"; shift 2 ;;
        -y|--yes)          ASSUME_YES=true; shift ;;
        -q|--quiet)        QUIET=true; ASSUME_YES=true; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 die "unknown option '$1' (see --help)" ;;
    esac
done

# interactive == a terminal is attached and the caller did not ask to skip prompts
INTERACTIVE=false
if [ "$ASSUME_YES" = false ] && [ -t 0 ] && [ -t 2 ]; then
    INTERACTIVE=true
fi

# ask <var> <prompt> <default>  — prompt on stderr, read from the terminal, fall back to default
ask() {
    local __var="$1" __prompt="$2" __default="$3" __reply=""
    if [ "$INTERACTIVE" = true ]; then
        read -r -p "$__prompt [$__default]: " __reply </dev/tty || true
    fi
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

ask_yesno() {  # ask_yesno <var> <prompt> <default-bool>
    local __var="$1" __prompt="$2" __default="$3" __hint __reply=""
    [ "$__default" = true ] && __hint="Y/n" || __hint="y/N"
    if [ "$INTERACTIVE" = true ]; then
        read -r -p "$__prompt [$__hint]: " __reply </dev/tty || true
    fi
    case "$(printf '%s' "$__reply" | tr '[:upper:]' '[:lower:]')" in
        y|yes) printf -v "$__var" true ;;
        n|no)  printf -v "$__var" false ;;
        *)     printf -v "$__var" '%s' "$__default" ;;
    esac
}

# ---------------------------------------------------------------------------- locate the stack
if [ -z "$MATRIX_DIR" ]; then
    # a bare "." is a reasonable default only if the cwd actually looks like the stack
    if [ -f "./.env" ] && [ -f "./docker-compose.yml" ]; then
        DEFAULT_DIR="$(pwd)"
    else
        DEFAULT_DIR=""
    fi
    if [ "$INTERACTIVE" = true ]; then
        ask MATRIX_DIR "Path to the Matrix stack dir (compose/11_matrix)" "$DEFAULT_DIR"
    else
        MATRIX_DIR="$DEFAULT_DIR"
    fi
fi
[ -n "$MATRIX_DIR" ] || die "no --matrix-dir given (and cwd is not a Matrix stack). See --help."
[ -d "$MATRIX_DIR" ] || die "matrix dir '$MATRIX_DIR' does not exist."
[ -f "$MATRIX_DIR/.env" ] && [ -f "$MATRIX_DIR/docker-compose.yml" ] \
    || die "'$MATRIX_DIR' has no .env + docker-compose.yml — is it the compose/11_matrix dir?"

# run docker compose inside the stack's project directory
dc() { ( cd "$MATRIX_DIR" && docker compose "$@" ); }

dc config --services 2>/dev/null | grep -qx "mas" \
    || die "the compose project in '$MATRIX_DIR' has no 'mas' service — not a Matrix 2.0/MAS stack?"

# read server identity from the stack's .env (for the MXID + the config snippet)
MATRIX_SERVER_NAME="$( ( cd "$MATRIX_DIR" && set -o allexport && . ./.env && printf '%s' "${MATRIX_SERVER_NAME:-}" ) )"
SYNAPSE_DOMAIN="$( ( cd "$MATRIX_DIR" && set -o allexport && . ./.env && printf '%s' "${SYNAPSE_DOMAIN:-}" ) )"
[ -n "$MATRIX_SERVER_NAME" ] || die "MATRIX_SERVER_NAME not set in $MATRIX_DIR/.env"

# ---------------------------------------------------------------------------- collect answers
if [ "$INTERACTIVE" = true ]; then
    log "== onbot account provisioning =="
    log "Matrix stack:  $MATRIX_DIR  (server_name: $MATRIX_SERVER_NAME)"
    log ""
    ask       USERNAME      "Bot username (localpart of the Matrix ID)" "$USERNAME"
    ask       DISPLAY_NAME  "Bot display name"                          "$DISPLAY_NAME"
    ask_yesno SYNAPSE_ADMIN "Grant Synapse admin scope (onbot needs it)" "$SYNAPSE_ADMIN"
fi

MXID="@${USERNAME}:${MATRIX_SERVER_NAME}"
SERVER_URL="${SYNAPSE_DOMAIN:+https://${SYNAPSE_DOMAIN}/}"

if [ "$INTERACTIVE" = true ]; then
    log ""
    log "About to provision:"
    log "  user id:        $MXID"
    log "  display name:   $DISPLAY_NAME"
    log "  synapse admin:  $SYNAPSE_ADMIN"
    log "  reissue only:   $REISSUE_ONLY"
    ask_yesno _CONFIRM "Proceed?" true
    [ "$_CONFIRM" = true ] || die "aborted."
fi

# ---------------------------------------------------------------------------- 1) create user
if [ "$REISSUE_ONLY" = false ]; then
    log "Creating user '$MXID' in MAS (SSO-only: no password — the bot uses a token)..."
    if ! reg_out="$(dc exec -T mas mas-cli manage register-user --yes --display-name "$DISPLAY_NAME" "$USERNAME" 2>&1)"; then
        # mas-cli refuses to recreate an existing user; that is fine and idempotent for us.
        if printf '%s' "$reg_out" | grep -qiE 'exist|already|in use|taken|not available'; then
            log "user '$USERNAME' already exists — reusing it."
        else
            log "$reg_out"
            die "register-user failed (see message above). Use --reissue-only if the user exists."
        fi
    fi
fi

# ---------------------------------------------------------------------------- 2) issue token
admin_flag=()
if [ "$SYNAPSE_ADMIN" = true ]; then
    admin_flag=(--yes-i-want-to-grant-synapse-admin-privileges)
fi
log "Issuing a compatibility token for '$USERNAME'${SYNAPSE_ADMIN:+ (synapse admin: $SYNAPSE_ADMIN)}..."
token_out="$(dc exec -T mas mas-cli manage issue-compatibility-token "${admin_flag[@]}" "$USERNAME" 2>&1 || true)"
TOKEN="$(printf '%s' "$token_out" | grep -oE 'mct_[A-Za-z0-9]+' | head -n1 || true)"
if [ -z "$TOKEN" ]; then
    log "$token_out"
    die "could not parse an mct_ token from mas-cli output — does user '$USERNAME' exist?"
fi

# ---------------------------------------------------------------------------- 3) best-effort verify
# The token acts on the CS API immediately; MAS provisions the user on Synapse asynchronously,
# so /whoami may lag for a second or two. Non-fatal.
if dc exec -T synapse sh -c 'command -v curl >/dev/null 2>&1'; then
    for _ in 1 2 3 4 5; do
        who="$(dc exec -T synapse curl -fsS -H "Authorization: Bearer ${TOKEN}" \
            "http://localhost:8008/_matrix/client/v3/account/whoami" 2>/dev/null || true)"
        if printf '%s' "$who" | grep -q "$MXID"; then
            log "verified: token authenticates as $MXID."
            break
        fi
        sleep 2
    done
fi

# ---------------------------------------------------------------------------- 4) output
if [ -n "$WRITE_TOKEN_TO" ]; then
    if [ "$WRITE_TOKEN_TO" = "-" ]; then
        printf '%s\n' "$TOKEN"
    else
        printf '%s\n' "$TOKEN" > "$WRITE_TOKEN_TO"
        chmod 600 "$WRITE_TOKEN_TO"
        log "token written to $WRITE_TOKEN_TO"
    fi
fi

if [ "$QUIET" = true ]; then
    printf '%s\n' "$TOKEN"
    exit 0
fi

cat >&2 <<EOF

============================================================================
 Bot account ready:  $MXID
============================================================================

Drop these into config.dev.yml (under synapse_server). The token is the BARE
value — do NOT add a "Bearer" prefix (the client adds it):

synapse_server:
  server_name: ${MATRIX_SERVER_NAME}
  server_url: ${SERVER_URL:-https://<your-synapse-domain>/}
  bot_user_id: "${MXID}"
  bot_access_token: ${TOKEN}

Notes:
  * The token carries $( [ "$SYNAPSE_ADMIN" = true ] && echo "the Synapse admin scope" || echo "NO admin scope — onbot's admin-API calls will 403" ).
  * Re-issue anytime (revokes nothing else):  $0 -m "$MATRIX_DIR" -u "$USERNAME" --reissue-only
  * For real MAS session revocation in the lifecycle module, also configure the
    top-level 'mas_admin' block (needs a client_credentials client in mas-config.yaml).
EOF
