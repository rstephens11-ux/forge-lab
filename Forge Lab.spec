# -*- mode: python ; coding: utf-8 -*-
# The Comfy SDK route (comfy_route.py) imports comfy_sdk inside functions, so PyInstaller
# can't see it: name the SDK and its transport. Absent SDK = the build still works and the
# app sends renders directly (comfy_route.enabled() is False).
from PyInstaller.utils.hooks import collect_submodules
try:
    _sdk_mods = collect_submodules('comfy_sdk') + collect_submodules('comfy_low')
except Exception:
    _sdk_mods = []


a = Analysis(
    ['forge_lab.py'],
    pathex=['.', 'audio'],
    binaries=[],
    datas=[('audio/rig_tts.py', 'audio'), ('audio/rig_align.py', 'audio'), ('audio/rig_asr.py', 'audio'), ('audio/rig_dialogue.py','audio'), ('audio/rig_asr_batch.py','audio'), ('audio/rig_fish.py','audio'), ('assets/fonts/Inter-Black.otf', 'assets/fonts'), ('assets/fonts/OFL-Inter.txt', 'assets/fonts')],
    hiddenimports=['forge_lab', 'forge_core', 'studio_pipeline', 'charpack', 'director', 'client', 'song', 'audio.client', 'dialogue', 'dialogue_ui', 'characters_ui', 'episode_ui', 'cards', 'cards_ui', 'weirdos', 'char_vocab', 'scene_engine', 'dialogue2_ui', 'voicelab', 'voices_ui', 'formats', 'quick', 'quick_ui', 'apps', 'apps_ui', 'writers_room', 'places', 'places_ui', 'dock', 'music', 'export_pack', 'nightshift', 'previews', 'series', 'licences', 'beats', 'finish', 'sfx', 'char_styles', 'comfy_route', 'picture_inbox', 'jobqueue', 'home_queue', 'home_page', 'dispatch'] + _sdk_mods,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Forge Lab',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/anvil-icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Forge Lab',
)
# ⚠️ NSLocalNetworkUsageDescription is MANDATORY. macOS 15+/26 silently blocks every LAN
# connection from an app whose Info.plist lacks this key — no prompt, no entry in
# System Settings → Privacy & Security → Local Network, just errno 65 "No route to host"
# for every request to forge, while the same curl from Terminal works. That is what broke
# RENDER STILL on 2026-10-10. Keep the key AND a reverse-DNS bundle_identifier: TCC keys
# the grant on the bundle id, so a generic one ("Forge Lab") loses it on every rebuild.
app = BUNDLE(
    coll,
    name='Forge Lab.app',
    icon='assets/anvil-icon.icns',
    bundle_identifier='com.ryanmichaelstephens.forgelab',
    info_plist={
        'NSLocalNetworkUsageDescription':
            'Forge Lab talks to your ComfyUI rig (forge) on the local network to render '
            'images and video.',
    },
)
