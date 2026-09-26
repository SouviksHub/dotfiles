# Old Android phone → local LLM agent node

Termux + llama.cpp `llama-server` exposes an **OpenAI-compatible API** on the phone.
Any agent that speaks the OpenAI API (custom scripts, LangChain, smolagents, aider, …)
points at it via `OPENAI_BASE_URL`. No root needed.

```
 agent (python) ──HTTP /v1/chat/completions──▶ llama-server ──▶ GGUF model (mmap)
      ▲                                           ▲
  agent-run (injects URL + key)          runit supervises; Termux:Boot starts it at boot
```

## 1. Phone prep (once)

1. Install **Termux**, **Termux:API**, **Termux:Boot** from **F-Droid** or GitHub releases.
   The Play Store build is outdated and its packages break. All three must come from the same source (signing keys must match).
2. Open Termux:Boot once so Android registers it.
3. Settings → Apps → Termux → Battery → **Unrestricted**. Disable any OEM "app killer"
   (Xiaomi/Huawei/Samsung are aggressive; see dontkillmyapp.com).
4. **Android 12+ phantom process killer** — kills Termux child processes over a limit of 32, with signal 9.
   - Android 14+: Developer options → *Disable child process restrictions*.
   - Android 12/13, from a PC with `adb`:
     ```
     adb shell "/system/bin/device_config set_sync_disabled_for_tests persistent"
     adb shell "/system/bin/device_config put activity_manager max_phantom_processes 2147483647"
     ```
5. Keep it plugged in. If the phone is running 24/7, cap the charge (many OEM ROMs have "protect battery / 80%") —
   a lithium cell held at 100% and 40 °C swells.

## 2. Install

```bash
pkg install -y git
git clone https://github.com/souvikshub/dotfiles ~/dotfiles
bash ~/dotfiles/android/bootstrap.sh
# restart Termux, then:
sv-enable llama && sv up llama
ai-status
```

`bootstrap.sh` checks RAM, CPU architecture, performance cores and free storage, then picks:

| MemTotal      | Model (Q4_K_M)          | Size   | Expected speed*   |
|---------------|-------------------------|--------|-------------------|
| < 2.6 GiB     | Qwen2.5-0.5B-Instruct   | 0.4 GB | 15–30 tok/s       |
| < 5 GiB       | Qwen2.5-1.5B-Instruct   | 1.0 GB | 6–15 tok/s        |
| < 6.5 GiB     | Qwen2.5-3B-Instruct     | 1.9 GB | 4–8 tok/s         |
| 6.5–11 GiB (8 GB phones) | Qwen2.5-3B-Instruct, 8K ctx | 1.9 GB | 4–8 tok/s |
| ≥ 11 GiB, or `BIG=1` | Qwen2.5-7B-Instruct | 4.7 GB | 2–4 tok/s         |

\*Generation on a 2018–2021 Snapdragon 8xx. Generation speed is limited by memory bandwidth (every token reads
all the weights once), so `tok/s ≈ bandwidth / model_bytes`. Older mid-range SoCs run at about half these speeds.

Overrides: `MODEL_URL=<gguf url> CTX=8192 LAN=1 bash bootstrap.sh`.

**8 GB phones:** an "8 GB" phone reports about 7.2–7.7 GiB of MemTotal, and Android plus background services keep about 3 GiB of that.
A 7B model with its KV cache needs about 5 GiB, so it does fit, but the low-memory killer shuts it down whenever
the phone is under memory pressure. At 2–4 tok/s, one agent step (read prompt → tool call → answer)
also takes minutes. The default is therefore 3B with an 8K context. Opt into 7B with `BIG=1 bash bootstrap.sh`, and only
on a phone that does nothing but run the server (debloated, no other apps).

**Agents are limited by prompt processing, not generation.** Every step re-sends the system prompt, the tool schemas and the history.
`llama-server` reuses the KV cache for a shared prompt prefix, so keep the system prompt and tool list
**byte-identical across calls** and only append to the end. Then each step processes only the new tokens.

## 3. Run agents

```bash
agent-run python ~/agents/hello_agent.py "How much battery is left?"
```

`agent-run` exports `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `OPENAI_API_KEY` and `OPENAI_MODEL`. Put your own agents
in `~/agents/` and start them the same way. `hello_agent.py` is a stdlib-only tool-calling loop; its file
tools are sandboxed to `~/agents/workspace`.

To keep agents running in the background, use `tmux new -d -s agent 'agent-run python my_agent.py'`
or add a runit service next to `$PREFIX/var/service/llama`.

**Python dependencies:** packages with native extensions (`pydantic-core`, `numpy`, `tiktoken`) need
`pkg install rust clang` and long compiles, or prebuilt versions from `pkg install python-numpy` etc. If an agent framework
won't install, run it inside `proot-distro install debian` (a glibc userland, so manylinux wheels install normally) and
keep llama-server native in Termux. Running proot costs about 10–30% on syscalls but nothing on inference.

## 4. Use the phone from other machines

Default bind is `127.0.0.1`. To serve the LAN:

```bash
sed -i 's/^HOST=.*/HOST=0.0.0.0/' ~/.config/local-ai/env && sv restart llama
curl http://<phone-ip>:8080/v1/models -H "Authorization: Bearer $(sed -n 's/^API_KEY=//p' ~/.config/local-ai/env)"
```

Every `/v1` request needs the API key. Anything outside your LAN should go through SSH, not an open port:

```bash
# phone: add your laptop's key to ~/.ssh/authorized_keys, then `sshd` (port 8022)
ssh -p 8022 -N -L 8080:127.0.0.1:8080 <phone-ip>   # laptop now sees the model on localhost:8080
```

Tailscale's Android app provides the same tunnel without port forwarding.

## Troubleshooting

| Symptom                                   | Cause / fix                                                         |
|-------------------------------------------|---------------------------------------------------------------------|
| Server dies with `Killed` / signal 9       | Phantom process killer (step 1.4) or the low-memory killer: use a smaller model or a lower `CTX` |
| Speed drops after 1–2 min                 | Thermal throttling. Remove the case, add a fan, lower `THREADS` in the env file |
| Slower with more threads                  | LITTLE cores are slowing the big ones down. Keep `THREADS` ≤ number of performance cores |
| Tool calls come back as plain text         | The model is too small for reliable tool use. 1.5B is the practical minimum; 3B+ is much better |
| Logs                                      | `$PREFIX/var/log/sv/llama/current`                                  |
