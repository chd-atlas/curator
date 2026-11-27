#!/bin/bash
#SBATCH --job-name=chd-curator-ask
#SBATCH --nodes=1
#SBATCH --ntasks=1
# Answer one batch of `chd-curator ask` requests with a local llama-server inside one Slurm job.
#
#   sbatch [--partition=... --account=... --cpus-per-task=... --mem=... --time=...] \
#          cluster-ask.sbatch <arguments for chd-curator ask, without --role/--site>
#
# The scheduler options, and any file for the log (`--output=`), belong on the sbatch command
# line, not here: this file names no partition, account or path. Get its path with
# `chd-curator job-script cluster-ask.sbatch`. Run directly with bash it behaves the same.
#
# Environment (exported before sbatch; sbatch passes it on):
#   ASK_SITE, ASK_ROLE, MODEL_FILE   required; the same as serve-and-ask.sh's --site, --role
#                        and --model-file (the server's flags are the site role's server_args)
#   SERVER_ARGS          optional; passed as --server-args, which must equal the site's flags
#   LLAMA_SERVER         the llama-server built by llama-build.sbatch (default: on PATH)
#   CHD_CURATOR          the chd-curator executable (default: on PATH)
#   CLUSTER_MODULES      space-separated modules to load first (optional)
#   SERVE_*              serve-and-ask.sh's own variables; SERVE_THREADS defaults to
#                        SLURM_CPUS_PER_TASK here
set -uo pipefail
: "${ASK_SITE:?ASK_SITE: path to the site config}"
: "${ASK_ROLE:?ASK_ROLE: the role to answer}"
: "${MODEL_FILE:?MODEL_FILE: path to the .gguf}"

if [[ -n "${CLUSTER_MODULES:-}" ]]; then
  module purge
  # shellcheck disable=SC2086  # a list of module names
  module load $CLUSTER_MODULES
fi
unset PYTHONPATH
export SERVE_THREADS="${SERVE_THREADS:-${SLURM_CPUS_PER_TASK:-$(nproc)}}"

script="$("${CHD_CURATOR:-chd-curator}" job-script serve-and-ask.sh)" || exit 2
extra=()
[[ -n "${SERVER_ARGS:-}" ]] && extra=(--server-args "$SERVER_ARGS")
bash "$script" --site "$ASK_SITE" --role "$ASK_ROLE" --model-file "$MODEL_FILE" \
  ${extra[@]+"${extra[@]}"} -- "$@"
rc=$?
echo "CLUSTER_ASK_EXIT=$rc"
exit "$rc"
