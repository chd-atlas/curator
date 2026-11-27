#!/bin/bash
# Start a local llama-server, answer one batch of requests with `chd-curator ask`, stop the server.
#
#   serve-and-ask.sh --site <site.yaml> --role <role> --model-file <gguf> \
#                    [--server-args "<llama-server flags>"] -- <arguments for chd-curator ask>
#
# `--role` and the `ask` arguments follow `chd-curator ask`; this script adds `--role` and `--site`
# itself (a generated one-role site), so do not repeat them after `--`. The generated site is the
# given site's entry for the role with ONLY `settings.base_url` replaced, so the answers carry the
# same `model_key` as the site you wrote. The server's flags are the role's
# `settings.server_args` (a string, or a list of words), which are part of that `model_key`, so the
# flags the register qualifies are the flags that run; `--server-args`, when given, must be the
# same flags. Exit code: `ask`'s; 2 for a bad argument (a site role with no `server_args`, a
# `--server-args` that differs), before anything starts; 1 when the server dies or is not healthy
# in time (the reason and the log's tail go to stderr). The server is always stopped (TERM, then
# KILL after a grace period).
#
# Beside the answers file it writes `<answers>.local-usage.json`, `{cpus, seconds, job_id}`: the
# CPUs the task held (SLURM_CPUS_ON_NODE, else SLURM_CPUS_PER_TASK, else nproc), the seconds from
# the server's start to its stop, and the job id (SLURM_JOB_ID, else LSB_JOBID, else null).
# `chd-curator report --local-usage` reads it: a local model's core-hours come only from it.
#
# Run it as `bash "$(chd-curator job-script serve-and-ask.sh)" ...`; it does not rely on its mode.
# Linux only: a server's state is read from /proc.
#
# Environment (all optional unless stated):
#   LLAMA_SERVER         the llama-server executable (default: llama-server on PATH)
#   CHD_CURATOR          the chd-curator executable (default: chd-curator on PATH)
#   CHD_CURATOR_PYTHON   the interpreter that runs `python -m chd_curator.jobs` (default: the one
#                        in chd-curator's shebang, else python3)
#   SERVE_SLOTS          parallel requests the server handles (default 4)
#   SERVE_CTX_PER_SLOT   context tokens per slot (default 8192); the server gets -c slots*this
#   SERVE_THREADS        server threads (default: nproc)
#   SERVE_WAIT_SECONDS   how long to wait for /health (default 1200)
#   SERVE_POLL_SECONDS   pause between health polls (default 5)
#   SERVE_GRACE_SECONDS  seconds between TERM and KILL (default 10)
#   SERVE_WORKDIR        where the generated site and server.log go (default: a temp directory,
#                        removed on success and kept on failure)
#
# Why each guard exists (the 2026-11-26 CPU benchmark's ledger):
#   --load-mode none     a model memory-mapped from a network filesystem generated at 0.08-0.9
#                        tokens/s; loading it into memory fixed that (2-18 tokens/s per slot).
#                        This llama.cpp spells no-mmap `--load-mode none`; `--no-mmap` made the
#                        server exit at once.
#   --reasoning on|off   the server's default, `auto`, can turn thinking on under a template that
#                        is meant to run without it; the mode is always stated.
#   liveness via /proc   a server that exited and is not yet reaped is a zombie; `kill -0` says it
#                        is alive. The benchmark's client, which had no liveness check, waited its whole 1,200 s
#                        on a server that had exited.

server_alive() {
  # Succeeds only for a live process: a missing one and a zombie (state Z, or X) are dead.
  local pid="$1" line rest state
  if [[ -r "/proc/$pid/stat" ]]; then
    read -r line < "/proc/$pid/stat" 2>/dev/null || return 1
    rest="${line##*) }"
    state="${rest%% *}"
  else
    state="$(ps -o stat= -p "$pid" 2>/dev/null | cut -c1)"
  fi
  [[ -n "$state" && "$state" != "Z" && "$state" != "X" ]]
}

stop_ask() {
  # Stop a running `ask` (TERM, then KILL after the grace period) and reap it.
  local pid="${ASK_PID:-}" ticks
  [[ -n "$pid" ]] || return 0
  if server_alive "$pid"; then
    kill -TERM "$pid" 2>/dev/null
    ticks=$(( ${SERVE_GRACE_SECONDS:-10} * 10 ))
    while (( ticks > 0 )) && server_alive "$pid"; do
      sleep 0.1
      ticks=$(( ticks - 1 ))
    done
    server_alive "$pid" && kill -KILL "$pid" 2>/dev/null
  fi
  wait "$pid" 2>/dev/null
  ASK_PID=""
}

cleanup() {
  stop_ask
  stop_server
}

stop_server() {
  local pid="${SERVER_PID:-}" ticks
  [[ -n "$pid" ]] || return 0
  if server_alive "$pid"; then
    kill -TERM "$pid" 2>/dev/null
    ticks=$(( ${SERVE_GRACE_SECONDS:-10} * 10 ))
    while (( ticks > 0 )) && server_alive "$pid"; do
      sleep 0.1
      ticks=$(( ticks - 1 ))
    done
    server_alive "$pid" && kill -KILL "$pid" 2>/dev/null
  fi
  wait "$pid" 2>/dev/null
  SERVER_PID=""
}

usage_error() {
  echo "serve-and-ask.sh: $1" >&2
  echo "usage: serve-and-ask.sh --site SITE --role ROLE --model-file GGUF [--server-args \"FLAGS\"] -- ASK_ARGS" >&2
  exit 2
}

now_ms() {
  local ns
  ns="$(date +%s%N)"
  echo $(( ns / 1000000 ))
}

server_log_tail() {
  [[ -f "$WORKDIR/server.log" ]] && { echo "--- last lines of $WORKDIR/server.log ---" >&2; tail -n 20 "$WORKDIR/server.log" >&2; }
}

main() {
  set -uo pipefail
  local site="" role="" model="" server_args="" have_args=0
  local -a ask_args=()
  while (( $# > 0 )); do
    case "$1" in
      --site) site="${2:-}"; shift 2 || usage_error "--site needs a value" ;;
      --role) role="${2:-}"; shift 2 || usage_error "--role needs a value" ;;
      --model-file) model="${2:-}"; shift 2 || usage_error "--model-file needs a value" ;;
      --server-args) server_args="${2:-}"; have_args=1; shift 2 || usage_error "--server-args needs a value" ;;
      --) shift; ask_args=("$@"); break ;;
      *) usage_error "unknown argument $1" ;;
    esac
  done
  [[ -n "$site" ]] || usage_error "--site is required"
  [[ -n "$role" ]] || usage_error "--role is required"
  [[ -n "$model" ]] || usage_error "--model-file is required"

  local cli="${CHD_CURATOR:-chd-curator}" py="${CHD_CURATOR_PYTHON:-}" first exe
  if [[ -z "$py" ]]; then
    exe="$(command -v "$cli" 2>/dev/null || true)"
    first=""
    [[ -n "$exe" ]] && IFS= read -r first < "$exe" 2>/dev/null
    if [[ "$first" =~ ^#!.*python ]]; then py="${first#\#!}"; py="${py%% *}"; else py="python3"; fi
  fi

  # The flags that run are the site's: they are part of the model_key the register qualifies.
  local site_args
  site_args="$("$py" -m chd_curator.jobs server-args "$site" "$role")" \
    || usage_error "the site's $role role needs settings.server_args (see the header)"
  if (( have_args )); then
    local -a given=() used=()
    read -ra given <<< "$server_args"
    read -ra used <<< "$site_args"
    [[ "${given[*]-}" == "${used[*]-}" ]] \
      || usage_error "--server-args \"$server_args\" differs from the site's settings.server_args \"$site_args\""
  fi
  server_args="$site_args"
  [[ " $server_args " =~ [[:space:]]--load-mode[[:space:]=]none[[:space:]] ]] \
    || usage_error "settings.server_args must contain '--load-mode none' (see the header)"
  [[ " $server_args " =~ [[:space:]]--reasoning[[:space:]=](on|off)[[:space:]] ]] \
    || usage_error "settings.server_args must state '--reasoning on' or '--reasoning off', never auto"

  local answers="" i
  for (( i = 0; i < ${#ask_args[@]}; i++ )); do
    case "${ask_args[i]}" in
      --answers) answers="${ask_args[i+1]:-}" ;;
      --answers=*) answers="${ask_args[i]#--answers=}" ;;
    esac
  done
  local cpus="${SLURM_CPUS_ON_NODE:-${SLURM_CPUS_PER_TASK:-$(nproc)}}"
  local job_id="${SLURM_JOB_ID:-${LSB_JOBID:-}}"

  WORKDIR="${SERVE_WORKDIR:-}"
  local own_workdir=0
  if [[ -z "$WORKDIR" ]]; then WORKDIR="$(mktemp -d)"; own_workdir=1; fi
  mkdir -p "$WORKDIR" || usage_error "cannot create $WORKDIR"
  SERVER_PID=""
  ASK_PID=""
  trap cleanup EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  # The server and ask talk over loopback; a cluster's proxy must not intercept that.
  export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost"
  export no_proxy="${no_proxy:+$no_proxy,}127.0.0.1,localhost"

  local port
  port="$("$py" -m chd_curator.jobs port)" || { echo "serve-and-ask.sh: no free port" >&2; return 1; }
  # Generated before the server starts: a site that does not load costs no model load.
  "$py" -m chd_curator.jobs site "$site" "$role" "http://127.0.0.1:$port/v1" "$WORKDIR/site.yaml" || return 2

  local slots="${SERVE_SLOTS:-4}" ctx="${SERVE_CTX_PER_SLOT:-8192}" threads="${SERVE_THREADS:-$(nproc)}"
  local started_ms
  started_ms="$(now_ms)"
  # shellcheck disable=SC2086  # the flags are one word-split string by design
  "${LLAMA_SERVER:-llama-server}" -m "$model" -c $(( slots * ctx )) -np "$slots" -t "$threads" \
    --host 127.0.0.1 --port "$port" $server_args > "$WORKDIR/server.log" 2>&1 &
  SERVER_PID=$!

  local waited_from=$SECONDS
  while true; do
    if ! server_alive "$SERVER_PID"; then
      echo "serve-and-ask.sh: the server process exited before it became healthy" >&2
      server_log_tail
      return 1
    fi
    if "$py" -m chd_curator.jobs health "http://127.0.0.1:$port/health"; then break; fi
    if (( SECONDS - waited_from >= ${SERVE_WAIT_SECONDS:-1200} )); then
      echo "serve-and-ask.sh: the server was not healthy after ${SERVE_WAIT_SECONDS:-1200} s" >&2
      server_log_tail
      return 1
    fi
    sleep "${SERVE_POLL_SECONDS:-5}"
  done
  echo "serve-and-ask.sh: server healthy after $(( SECONDS - waited_from )) s on port $port" >&2

  # ask runs in the background and is waited on: bash runs a trap only between foreground
  # commands, so a TERM sent to this script would otherwise wait for ask to end. A signal now
  # interrupts `wait`, the trap exits, and the EXIT cleanup stops ask and then the server.
  # ${arr[@]+...} keeps an empty argument list valid under `set -u` on bash < 4.4.
  "$cli" ask --role "$role" --site "$WORKDIR/site.yaml" ${ask_args[@]+"${ask_args[@]}"} &
  ASK_PID=$!
  wait "$ASK_PID"
  local rc=$?
  ASK_PID=""
  stop_server
  local held_ms=$(( $(now_ms) - started_ms ))
  if [[ -n "$answers" ]]; then
    "$py" -m chd_curator.jobs local-usage "$answers.local-usage.json" "$cpus" \
      "$(( held_ms / 1000 )).$(printf '%03d' $(( held_ms % 1000 )))" "$job_id" \
      || echo "serve-and-ask.sh: could not write $answers.local-usage.json" >&2
  fi
  if (( rc == 0 && own_workdir )); then rm -rf "$WORKDIR"; fi
  return "$rc"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
  exit $?
fi
