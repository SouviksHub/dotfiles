#!/data/data/com.termux/files/usr/bin/bash
# Turn an old Android phone into a local LLM node for agents.
# Run inside Termux (F-Droid/GitHub build):  bash ~/dotfiles/android/bootstrap.sh
#
# Overrides (env vars):
#   MODEL_URL=<gguf url>   force a specific model instead of the RAM-based pick
#   CTX=<n>                context window
#   LAN=1                  bind the server to 0.0.0.0 instead of 127.0.0.1
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CONF_DIR="$HOME/.config/local-ai"
MODEL_DIR="$HOME/models"

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ "${PREFIX:-}" == *com.termux* ]] || die "Not running inside Termux."

# ---------------------------------------------------------------- packages
log "Updating Termux packages"
pkg update -y
pkg upgrade -y -o Dpkg::Options::=--force-confnew
pkg install -y git curl jq python openssh tmux termux-services termux-api

if ! command -v llama-server >/dev/null; then
  log "Installing llama.cpp"
  if ! pkg install -y llama-cpp; then
    warn "llama-cpp package unavailable; building from source (10-40 min on old SoCs)"
    pkg install -y clang cmake make
    src="$HOME/src/llama.cpp"
    [[ -d "$src" ]] || git clone --depth 1 https://github.com/ggml-org/llama.cpp "$src"
    cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF
    cmake --build "$src/build" -j"$(nproc)" --target llama-server llama-cli
    ln -sf "$src/build/bin/llama-server" "$PREFIX/bin/llama-server"
    ln -sf "$src/build/bin/llama-cli" "$PREFIX/bin/llama-cli"
  fi
fi

# ---------------------------------------------------------------- hardware
arch="$(uname -m)"
mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
mem_mib=$(( mem_kb / 1024 ))
free_mib="$(df -Pm "$HOME" | awk 'NR==2 {print $4}')"

# Performance cores: those whose max frequency equals the highest on the SoC.
# Running on LITTLE cores too makes token generation slower, not faster.
big_cores=0
top=0
for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/cpuinfo_max_freq; do
  [[ -r "$f" ]] || continue
  v="$(<"$f")"
  (( v > top )) && top=$v
done
for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/cpuinfo_max_freq; do
  [[ -r "$f" ]] || continue
  (( $(<"$f") * 10 >= top * 8 )) && big_cores=$(( big_cores + 1 ))
done
(( big_cores >= 2 )) || big_cores="$(nproc)"
threads=$(( big_cores > 4 ? 4 : big_cores ))

log "arch=$arch  ram=${mem_mib}MiB  free_disk=${free_mib}MiB  threads=$threads"
[[ "$arch" == "aarch64" ]] || warn "32-bit/non-ARM64 CPU: expect very slow inference; capping at 1.5B."

# ---------------------------------------------------------------- model pick
# MemTotal is ~10-15% below the marketed RAM, and Android keeps ~1.5 GiB for
# itself, so the model must fit in roughly (MemTotal - 1.5 GiB).
hf="https://huggingface.co/bartowski"
if [[ -n "${MODEL_URL:-}" ]]; then
  url="$MODEL_URL"; need=0; ctx="${CTX:-4096}"
elif (( mem_mib < 2600 )); then
  url="$hf/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf"; need=500;  ctx=2048
elif (( mem_mib < 5000 )) || [[ "$arch" != "aarch64" ]]; then
  url="$hf/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"; need=1100; ctx=4096
elif (( mem_mib < 7500 )); then
  url="$hf/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf";     need=2000; ctx=4096
else
  url="$hf/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf";     need=4700; ctx=4096
fi
ctx="${CTX:-$ctx}"
model="$MODEL_DIR/$(basename "${url%%\?*}")"

(( free_mib > need + 500 )) || die "Need ~$((need + 500))MiB free storage, have ${free_mib}MiB."

mkdir -p "$MODEL_DIR"
log "Downloading $(basename "$model") (resumable)"
curl -fL --retry 5 -C - -o "$model" "$url"
[[ "$(head -c4 "$model")" == "GGUF" ]] || die "$model is not a GGUF file (bad URL or truncated download)."

# ---------------------------------------------------------------- config
mkdir -p "$CONF_DIR"
env_file="$CONF_DIR/env"
if [[ -f "$env_file" ]] && grep -q '^API_KEY=' "$env_file"; then
  api_key="$(sed -n 's/^API_KEY=//p' "$env_file")"
else
  api_key="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
cat > "$env_file" <<EOF
MODEL=$model
THREADS=$threads
CTX=$ctx
HOST=$([[ "${LAN:-0}" == 1 ]] && echo 0.0.0.0 || echo 127.0.0.1)
PORT=8080
API_KEY=$api_key
EOF
chmod 600 "$env_file"

# ---------------------------------------------------------------- binaries
for f in "$DIR"/bin/*; do
  chmod +x "$f"
  ln -sf "$f" "$PREFIX/bin/$(basename "$f")"
done
mkdir -p "$HOME/agents"
cp -n "$DIR"/agents/*.py "$HOME/agents/" 2>/dev/null || true

# ---------------------------------------------------------------- supervision
# runit (termux-services) restarts llama-server if Android's LMK kills it.
svc="$PREFIX/var/service/llama"
mkdir -p "$svc/log"
cat > "$svc/run" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
exec ai-server
EOF
ln -sf "$PREFIX/share/termux-services/svlogger" "$svc/log/run"
chmod +x "$svc/run"

# Termux:Boot autostart (requires the Termux:Boot add-on, opened once).
mkdir -p "$HOME/.termux/boot"
cp "$DIR/boot/start-ai" "$HOME/.termux/boot/start-ai"
chmod +x "$HOME/.termux/boot/start-ai"

# ---------------------------------------------------------------- dotfiles
bash "$DIR/../install.sh"

log "Done."
cat <<EOF

Next:
  1. Close and reopen Termux (starts the runit supervisor), then:
       sv-enable llama && sv up llama
  2. Check:     ai-status
  3. Run agent: agent-run python ~/agents/hello_agent.py "How much battery is left?"

API: http://$(sed -n 's/^HOST=//p' "$env_file"):8080/v1  (OpenAI-compatible)
Key: stored in $env_file
Read android/README.md for the phantom-process-killer and battery fixes.
EOF
