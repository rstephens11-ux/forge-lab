#!/usr/bin/env bash
# Build the standalone Forge Lab .app with PyInstaller.
# Requires: brew install pyinstaller python-tk@3.14   (both already on this Mac)
set -euo pipefail

cd "$(dirname "$0")"

# --windowed = real .app, no terminal window.
# --noconfirm because --clean clears PyInstaller's cache, NOT dist/, so without it
# every rebuild fails on a non-empty dist/.
# ⚠️ The Studio modules are imported INSIDE functions and via sys.path tricks (audio/), so name
# them explicitly; the rig scripts under audio/ are DATA the client copies to forge, so they
# must ship as files, not just as compiled modules.
# ⚠️ BUILD FROM THE SPEC, never from forge_lab.py. `pyinstaller <flags> forge_lab.py` REWRITES
# "Forge Lab.spec" from the flags on every run, which silently deleted the Local Network plist
# key + bundle id below — the 2026-10-10 install came out as CFBundleIdentifier "Forge Lab" with
# no NSLocalNetworkUsageDescription. The spec carries every former flag (paths, hidden imports,
# datas, icon, windowed). Add new modules/data THERE.
pyinstaller --clean --noconfirm "Forge Lab.spec"
grep -q NSLocalNetworkUsageDescription "dist/Forge Lab.app/Contents/Info.plist" || {
  echo "BUILD BROKEN: Info.plist lacks NSLocalNetworkUsageDescription (forge would be unreachable)" >&2
  exit 1; }

# ⚠️ Local Network permission (macOS 15+/26): the BUNDLE in "Forge Lab.spec" carries
# NSLocalNetworkUsageDescription + a reverse-DNS bundle id. Without them macOS silently
# blocks every LAN call to forge with errno 65 "No route to host" — no prompt, nothing in
# Settings. Don't "simplify" the spec by dropping them (cost a broken RENDER STILL button).
echo ""
echo "Built:   dist/Forge Lab.app"

# Rebuilds keep the grant only if the signature is stable; ad-hoc sign here so the bundle
# is at least sealed, then install.
codesign --force --deep --sign - "dist/Forge Lab.app" 2>/dev/null || true
echo "Install: cp -R \"dist/Forge Lab.app\" /Applications/"