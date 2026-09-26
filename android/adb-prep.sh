#!/usr/bin/env bash
# Strip Android down to a headless appliance over USB ADB. Run on a PC.
# Needs no root, and every change can be undone (pm enable / Settings).
#
#   ./adb-prep.sh list                 dump enabled packages to packages.txt
#   (edit packages.txt: keep ONLY the lines you want DISABLED)
#   ./adb-prep.sh apply packages.txt   apply system tweaks + disable those packages
#   ./adb-prep.sh undo  packages.txt   re-enable them
#
# Phone: Settings → About → tap "Build number" 7× → Developer options → USB debugging.
set -euo pipefail
a() { adb shell "$@"; }

# Packages that break the phone or this setup if disabled.
PROTECT='^(android|com\.android\.(systemui|settings|phone|providers\..*|shell|networkstack.*|wifi.*|permissioncontroller|packageinstaller|inputmethod.*|launcher3|bluetooth|nfc|se|server\..*|externalstorage|documentsui)|com\.google\.android\.(gms|gsf|webview|networkstack.*|permissioncontroller|packageinstaller|ext\.services|modulemetadata)|com\.termux.*|com\.tplink\..*|.*\.(ims|telephony|qualcomm\.qti\.(ims|telephony).*)|org\.telegram\..*)$'

case "${1:-}" in
list)
  a pm list packages -e | sed 's/^package://' | sort | grep -Ev "$PROTECT" > packages.txt
  echo "Wrote $(wc -l < packages.txt) candidates to packages.txt (protected ones excluded)."
  echo "Delete every line you want to KEEP, then: $0 apply packages.txt"
  ;;
apply)
  f="${2:?packages file}"
  echo "== system tweaks"
  # Android 12/13 phantom process killer (Android 14+: Developer options toggle).
  a /system/bin/device_config set_sync_disabled_for_tests persistent || true
  a /system/bin/device_config put activity_manager max_phantom_processes 2147483647 || true
  a settings put global settings_enable_monitor_phantom_procs false || true
  # Exempt Termux from Doze and background restrictions.
  a cmd deviceidle whitelist +com.termux
  a cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow
  a cmd appops set com.termux RUN_IN_BACKGROUND allow
  # Wi-Fi never sleeps; no animations; no auto-updates hijacking the CPU.
  a settings put global wifi_sleep_policy 2 || true
  for k in window_animation_scale transition_animation_scale animator_duration_scale; do
    a settings put global "$k" 0
  done
  a settings put global auto_update_apps 0 2>/dev/null || true
  echo "== disabling $(grep -cv '^\s*$' "$f") packages"
  while read -r p; do
    [[ -z "$p" || "$p" =~ $PROTECT ]] && continue
    printf '%-60s ' "$p"; a pm disable-user --user 0 "$p" 2>&1 | tail -1
  done < "$f"
  echo "Reboot the phone, then check that Termux services come back up by themselves."
  ;;
undo)
  while read -r p; do [[ -n "$p" ]] && a pm enable "$p" >/dev/null && echo "enabled $p"; done < "${2:?packages file}"
  ;;
*) sed -n '2,12p' "$0"; exit 1 ;;
esac
