#!/bin/bash
#SBATCH --job-name=llama-build
#SBATCH --nodes=1
#SBATCH --ntasks=1
# Build llama.cpp's server from a source tree that is already on disk, and check a model's sha256.
# A compute node has no git and no internet, so clone the pinned release on a login node first.
#
#   sbatch [--partition=... --account=... --cpus-per-task=... --mem=... --time=...] llama-build.sbatch
#
# Scheduler options go on the sbatch command line; this file names no partition, account or path.
# Get its path with `chd-curator job-script llama-build.sbatch`.
#
# Environment:
#   LLAMA_SRC            required; the llama.cpp checkout
#   HF_UI_VERSION        the web-UI asset version fetched into LLAMA_SRC on the login node
#                        (default b12246, the release the 2026-11-26 benchmark pinned; it must
#                        be the value used when the assets were fetched)
#   MODEL_FILE, MODEL_SHA256   optional pair: the model and its expected sha256; a mismatch
#                        exits 3
#   BUILD_MODULES        space-separated modules to load first (compiler, cmake); optional
#   BUILD_LOG_DIR        where `llama-server --help` is saved (default: the current directory)
#
# Why it is built this way (the benchmark's ledger): the server embeds web-UI assets that a
# compute node cannot download, and in a shallow clone llama.cpp resolves their version to "b1",
# the wrong assets, which failed the first build in 28 s; HF_UI_VERSION pins it. GGML_BACKEND_DL
# with all CPU variants picks the CPU's best kernels at run time, so one build serves every node.
set -euo pipefail
LLAMA_SRC="${LLAMA_SRC:?LLAMA_SRC: the llama.cpp checkout}"
if [[ -n "${MODEL_FILE:-}" ]]; then : "${MODEL_SHA256:?MODEL_SHA256: expected sha256 of MODEL_FILE}"; fi

if [[ -n "${BUILD_MODULES:-}" ]]; then
  module purge
  # shellcheck disable=SC2086  # a list of module names
  module load $BUILD_MODULES
fi
echo "node $(hostname); $(lscpu | grep 'Model name' || true)"

export HF_UI_VERSION="${HF_UI_VERSION:-b12246}"
LOG_DIR="$(cd "${BUILD_LOG_DIR:-.}" && pwd)"
cd "$LLAMA_SRC"
cmake -B build -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DBUILD_SHARED_LIBS=ON \
  -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
cmake --build build -j "${SLURM_CPUS_PER_TASK:-8}" --target llama-server
ls build/bin/
build/bin/llama-server --version 2>&1 | tail -3
build/bin/llama-server --help > "$LOG_DIR/llama-server-help.txt" 2>&1 || true

if [[ -n "${MODEL_FILE:-}" ]]; then
  got="$(sha256sum "$MODEL_FILE" | cut -d' ' -f1)"
  if [[ "$got" != "$MODEL_SHA256" ]]; then
    echo "MODEL SHA256 MISMATCH: got $got expected $MODEL_SHA256" >&2
    exit 3
  fi
  echo "model sha256 OK"
fi
echo "BUILD_OK; the server is $LLAMA_SRC/build/bin/llama-server"
