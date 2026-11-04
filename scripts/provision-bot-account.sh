#!/usr/bin/env bash
# provision-bot-account.sh: create the onbot's Matrix account on a Matrix 2.0 / MAS stack and mint
# the access token it needs.
#
# Why this exists: under Matrix 2.0 auth is delegated to MAS (Matrix Authentication Service). You
# cannot create the bot via Synapse's register_new_matrix_user nor get a token via the password
# /login endpoint (SSO-only stacks have no password login at all). Instead the bot is created in MAS
# and handed a long-lived *compatibility token* that both acts as the bot user on the client-server
# API AND carries the Synapse admin scope (urn:synapse:admin:*), exactly the single token onbot
# shares between its CS and admin clients (see onbot/app.py:build_matrix_token_provider).
#
# This wraps `mas-cli` in the Matrix stack's docker-compose project. Run it ON the docker host (or
# anywhere `docker compose exec mas` reaches the MAS container).
#
# The token never appears on a command line (it is handed to curl on stdin) and is never printed in
# full to the terminal: it goes to stdout only with --quiet, or into a file created with mode 0600.
#
# WIZARD:      run with no args -> interactive prompts (sensible defaults pre-filled).
# AUTOMATION:  pass every answer as a flag and add --yes to skip all prompts. With --quiet only the
#              bare token is printed to stdout, e.g.
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
#       --synapse-admin    Grant the token the Synapse admin scope (default: on, onbot needs it).
#       --no-synapse-admin Issue a plain user token without admin scope (rooms/DMs only).
#       --reissue-only     Do NOT (re)create the user, only issue a fresh token for it.
#       --revoke-previous  Before issuing the new token, end ALL existing sessions of the user
#                          (`mas-cli manage kill-sessions`): every token issued earlier stops
#                          working, including the one a running onbot uses, until you deploy the
#                          new one. The wizard asks; with --yes the default is to keep them.
#       --keep-previous    Do not end existing sessions (the default with --yes).
#       --write-token-to F Write the bare token to file F, created with mode 0600 (an existing file
#                          is replaced). "-" means stdout.
#   -y, --yes              Non-interactive: never prompt, use flags/defaults, fail if ambiguous.
#                          Needs --quiet or --write-token-to, otherwise the token would be lost.
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
REVOKE_PREVIOUS=""   # empty: ask in the wizard, keep with --yes
WRITE_TOKEN_TO=""
ASSUME_YES=false
QUIET=false

log() { printf '%s\n' "$*" >&2; }   # everything human-facing goes to stderr so stdout is clean
die() { log "ERROR: $*"; exit 1; }

usage() { sed -n '2,48p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        -m|--matrix-dir)   MATRIX_DIR="$2"; shift 2 ;;
        -u|--username)     USERNAME="$2"; shift 2 ;;
        -d|--display-name) DISPLAY_NAME="$2"; shift 2 ;;
        --synapse-admin)   SYNAPSE_ADMIN=true; shift ;;
        --no-synapse-admin) SYNAPSE_ADMIN=false; shift ;;
        --reissue-only)    REISSUE_ONLY=true; shift ;;
        --revoke-previous) REVOKE_PREVIOUS=true; shift ;;
        --keep-previous)   REVOKE_PREVIOUS=false; shift ;;
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

# ask <var> <prompt> <default>: prompt on stderr, read from the terminal, fall back to default
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

# env_value <file> <key>: the last `KEY=value` (or `export KEY=value`) in a compose .env file,
# with one level of surrounding quotes removed. The file is read as data and never executed.
env_value() {
    local file="$1" key="$2" value
    value="$(sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*//p" "$file" | tail -n 1)"
    value="${value%$'\r'}"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s' "$value"
}

# mask <token>: enough to recognise a token, not enough to use it
mask() { printf '%s...(%d chars)' "${1:0:8}" "${#1}"; }

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
    || die "'$MATRIX_DIR' has no .env + docker-compose.yml. Is it the compose/11_matrix dir?"

# run docker compose inside the stack's project directory
dc() { ( cd "$MATRIX_DIR" && docker compose "$@" ); }

# grep -q reads from here-strings, not pipes: it exits at the first match, and under pipefail the
# writer's SIGPIPE would turn a match into a failure.
services="$(dc config --services 2>/dev/null || true)"
grep -qx "mas" <<<"$services" \
    || die "the compose project in '$MATRIX_DIR' has no 'mas' service. Not a Matrix 2.0/MAS stack?"

# read server identity from the stack's .env (for the MXID + the config snippet)
MATRIX_SERVER_NAME="$(env_value "$MATRIX_DIR/.env" MATRIX_SERVER_NAME)"
SYNAPSE_DOMAIN="$(env_value "$MATRIX_DIR/.env" SYNAPSE_DOMAIN)"
[ -n "$MATRIX_SERVER_NAME" ] || die "MATRIX_SERVER_NAME not set in $MATRIX_DIR/.env"

# ---------------------------------------------------------------------------- collect answers
if [ "$INTERACTIVE" = true ]; then
    log "== onbot account provisioning =="
    log "Matrix stack:  $MATRIX_DIR  (server_name: $MATRIX_SERVER_NAME)"
    log ""
    ask       USERNAME      "Bot username (localpart of the Matrix ID)" "$USERNAME"
    ask       DISPLAY_NAME  "Bot display name"                          "$DISPLAY_NAME"
    ask_yesno SYNAPSE_ADMIN "Grant Synapse admin scope (onbot needs it)" "$SYNAPSE_ADMIN"
    if [ -z "$REVOKE_PREVIOUS" ]; then
        log ""
        log "Ending the user's existing sessions revokes every token issued to it before, including"
        log "the one a running onbot uses: it stops working until you deploy the new token."
        ask_yesno REVOKE_PREVIOUS "End all existing sessions of this user first" true
    fi
    if [ -z "$WRITE_TOKEN_TO" ] && [ "$QUIET" = false ]; then
        ask WRITE_TOKEN_TO "Write the token to this file (mode 0600)" "./onbot-bot-token"
    fi
fi
REVOKE_PREVIOUS="${REVOKE_PREVIOUS:-false}"

# The token is shown masked only, so it must go somewhere.
if [ "$QUIET" = false ] && [ -z "$WRITE_TOKEN_TO" ]; then
    die "nowhere to put the token: pass --write-token-to FILE (or '-' for stdout), or --quiet."
fi

MXID="@${USERNAME}:${MATRIX_SERVER_NAME}"
SERVER_URL="${SYNAPSE_DOMAIN:+https://${SYNAPSE_DOMAIN}/}"

if [ "$INTERACTIVE" = true ]; then
    log ""
    log "About to provision:"
    log "  user id:          $MXID"
    log "  display name:     $DISPLAY_NAME"
    log "  synapse admin:    $SYNAPSE_ADMIN"
    log "  reissue only:     $REISSUE_ONLY"
    log "  revoke previous:  $REVOKE_PREVIOUS"
    log "  token file:       $WRITE_TOKEN_TO"
    ask_yesno _CONFIRM "Proceed?" true
    [ "$_CONFIRM" = true ] || die "aborted."
fi

# ---------------------------------------------------------------------------- 1) create user
if [ "$REISSUE_ONLY" = false ]; then
    log "Creating user '$MXID' in MAS (SSO-only: no password, the bot uses a token)..."
    if ! reg_out="$(dc exec -T mas mas-cli manage register-user --yes --display-name "$DISPLAY_NAME" "$USERNAME" 2>&1)"; then
        # mas-cli refuses to recreate an existing user; that is fine and idempotent for us.
        if grep -qiE 'exist|already|in use|taken|not available' <<<"$reg_out"; then
            log "user '$USERNAME' already exists, reusing it."
        else
            log "$reg_out"
            die "register-user failed (see message above). Use --reissue-only if the user exists."
        fi
    fi
fi

# ---------------------------------------------------------------------------- 2) revoke old tokens
# Before issuing, so the new token is the only valid one afterwards.
if [ "$REVOKE_PREVIOUS" = true ]; then
    log "Ending all existing sessions of '$USERNAME' (earlier tokens stop working)..."
    if ! kill_out="$(dc exec -T mas mas-cli manage kill-sessions "$USERNAME" 2>&1)"; then
        log "$kill_out"
        die "kill-sessions failed (see message above); no new token was issued."
    fi
    log "$kill_out"
fi

# ---------------------------------------------------------------------------- 3) issue token
admin_flag=()
if [ "$SYNAPSE_ADMIN" = true ]; then
    admin_flag=(--yes-i-want-to-grant-synapse-admin-privileges)
fi
log "Issuing a compatibility token for '$USERNAME' (synapse admin: $SYNAPSE_ADMIN)..."
token_out="$(dc exec -T mas mas-cli manage issue-compatibility-token "${admin_flag[@]}" "$USERNAME" 2>&1 || true)"
TOKEN="$(printf '%s' "$token_out" | grep -oE 'mct_[A-Za-z0-9]+' | head -n1 || true)"
if [ -z "$TOKEN" ]; then
    # mas-cli printed no token, so its output holds nothing secret
    log "$token_out"
    die "could not parse an mct_ token from mas-cli output. Does user '$USERNAME' exist?"
fi

# ---------------------------------------------------------------------------- 4) best-effort verify
# The token acts on the CS API immediately; MAS provisions the user on Synapse asynchronously, so
# /whoami may lag for a second or two. Non-fatal. The Authorization header goes to curl on stdin
# (`-H @-`), so the token is on no command line, neither on the host nor in the container.
verified=false
if dc exec -T synapse sh -c 'command -v curl >/dev/null 2>&1'; then
    for _ in 1 2 3 4 5; do
        who="$(printf 'Authorization: Bearer %s\n' "$TOKEN" \
            | dc exec -T synapse curl -fsS -H @- \
                "http://localhost:8008/_matrix/client/v3/account/whoami" 2>/dev/null || true)"
        if grep -qF "\"$MXID\"" <<<"$who"; then
            log "verified: token authenticates as $MXID."
            verified=true
            break
        fi
        sleep 2
    done
    [ "$verified" = true ] || log "warning: could not verify the token via /whoami (see above); it may still work."
fi

# ---------------------------------------------------------------------------- 5) output
if [ -n "$WRITE_TOKEN_TO" ]; then
    if [ "$WRITE_TOKEN_TO" = "-" ]; then
        printf '%s\n' "$TOKEN"
    else
        # mktemp creates the file with mode 0600, so the token is never readable by others, not
        # even for a moment; the rename then replaces any existing file (and its permissions).
        token_dir="$(dirname -- "$WRITE_TOKEN_TO")"
        tmp_file="$(mktemp "$token_dir/.onbot-token.XXXXXX")"
        printf '%s\n' "$TOKEN" > "$tmp_file"
        mv -f -- "$tmp_file" "$WRITE_TOKEN_TO"
        log "token written to $WRITE_TOKEN_TO (mode 0600)"
    fi
fi

if [ "$QUIET" = true ]; then
    printf '%s\n' "$TOKEN"
    exit 0
fi

if [ -n "$WRITE_TOKEN_TO" ] && [ "$WRITE_TOKEN_TO" != "-" ]; then
    token_hint="contents of $WRITE_TOKEN_TO"
else
    token_hint="the token printed on stdout"
fi

cat >&2 <<EOF

============================================================================
 Bot account ready:  $MXID
============================================================================

Put these into the onbot config (under synapse_server). The token is the BARE
value, do NOT add a "Bearer" prefix (the client adds it). Use the ${token_hint}
for bot_access_token, or pass it as ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN.

synapse_server:
  server_name: ${MATRIX_SERVER_NAME}
  server_url: ${SERVER_URL:-https://<your-synapse-domain>/}
  bot_user_id: "${MXID}"
  bot_access_token: <$(mask "$TOKEN")>

Notes:
  * The token carries $( [ "$SYNAPSE_ADMIN" = true ] && echo "the Synapse admin scope" || echo "NO admin scope, onbot's admin-API calls will 403" ).
  * Rotate anytime, ending all older sessions of the user:
      $0 -m "$MATRIX_DIR" -u "$USERNAME" --reissue-only --revoke-previous --write-token-to FILE
  * For real MAS session revocation in the lifecycle module, also configure the
    top-level 'mas_admin' block (needs a client_credentials client in mas-config.yaml).
EOF
