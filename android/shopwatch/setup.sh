#!/data/data/com.termux/files/usr/bin/bash
# Install shopwatch on the phone. Run in Termux after android/bootstrap.sh.
#
# Detection runs inside a proot Debian because onnxruntime and opencv ship
# manylinux aarch64 wheels (glibc); Termux is bionic and would need hour-long
# source builds. proot only adds overhead to syscalls; the compute runs natively.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SW="$HOME/shopwatch"
log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ "${PREFIX:-}" == *com.termux* ]] || die "Run inside Termux."
[[ "$(uname -m)" == aarch64 ]] || die "Needs a 64-bit ARM phone (onnxruntime has no armv7 wheel)."
case "$DIR" in "$HOME"/*) ;; *) die "Clone the dotfiles under \$HOME (proot sees it as /root)." ;; esac
rel="${DIR#"$HOME"/}"

pkg install -y proot-distro
[[ -d "$PREFIX/var/lib/proot-distro/installed-rootfs/debian" ]] || proot-distro install debian
deb() { proot-distro login debian --termux-home --shared-tmp -- bash -lc "$1"; }

log "Installing Debian packages (ffmpeg, python)"
deb "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
     python3 python3-venv ffmpeg tzdata ca-certificates >/dev/null"

log "Creating venv with onnxruntime + opencv"
mkdir -p "$SW"
deb "[ -x /root/shopwatch/venv/bin/python ] || python3 -m venv /root/shopwatch/venv
     /root/shopwatch/venv/bin/pip install -q --upgrade pip
     /root/shopwatch/venv/bin/pip install -q numpy onnxruntime opencv-python-headless"

if [[ ! -f "$SW/yolo11n-320.onnx" || ! -f "$SW/yolo11n-pose-320.onnx" ]]; then
  log "Exporting YOLO11n detector + pose models to ONNX (one-off, ~1 GB temp download)"
  deb "set -e; cd /tmp && python3 -m venv exp && exp/bin/pip install -q ultralytics onnx onnxslim
       for m in yolo11n yolo11n-pose; do
         exp/bin/yolo export model=\$m.pt format=onnx imgsz=320 opset=17 simplify=True
         mv \$m.onnx /root/shopwatch/\$m-320.onnx; rm -f \$m.pt
       done
       rm -rf exp"
fi

log "Self-test of the counter rules"
deb "cd /root/$rel && /root/shopwatch/venv/bin/python -m unittest -q test_cashwatch"

if [[ ! -f "$SW/config.toml" ]]; then
  cp "$DIR/config.example.toml" "$SW/config.toml"
  chmod 600 "$SW/config.toml"
  log "Edit $SW/config.toml (camera URLs, hours, Telegram), then: sv-enable shopwatch"
fi

svc="$PREFIX/var/service/shopwatch"
mkdir -p "$svc/log"
cat > "$svc/run" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
exec proot-distro login debian --termux-home --shared-tmp -- \\
  /root/shopwatch/venv/bin/python -u /root/$rel/shopwatch.py /root/shopwatch/config.toml
EOF
chmod +x "$svc/run"
ln -sf "$PREFIX/share/termux-services/svlogger" "$svc/log/run"
touch "$svc/down"   # stays stopped until the config is filled in and it's enabled

log "Done. Test in the foreground first:"
echo "  proot-distro login debian --termux-home -- /root/shopwatch/venv/bin/python /root/$rel/shopwatch.py /root/shopwatch/config.toml"
echo "Then: sv-enable shopwatch    logs: tail -f \$PREFIX/var/log/sv/shopwatch/current"
