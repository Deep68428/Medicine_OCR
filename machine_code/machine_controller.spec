# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# OpenVINO has a plugin/IR reader system — collect everything or it silently fails at runtime.
# mvIMPACT must be collected explicitly or its Python package files won't exist inside the bundle.
for pkg in ('openvino', 'rapidocr_onnxruntime', 'cv2', 'shapely', 'mvIMPACT'):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# Strip bundled model files and unused data — these are either downloaded at runtime
# or not used by this project.
#   - rapidocr_onnxruntime/models/: ONNX models (~16MB), downloaded on first use
#   - cv2/data/: Haar cascade classifiers (~9MB), not used in this project
_exclude_prefixes = ('rapidocr_onnxruntime/models/', 'cv2/data/')
datas = [
    (src, dst) for src, dst in datas
    if not any(p in src.replace('\\', '/') for p in _exclude_prefixes)
]

# ImpactAcquire native libs are already installed on the warehouse machine under
# /opt/ImpactAcquire — no need to bundle them and bloat the binary by ~90MB.
# Only the Python binding (.so) is needed and that comes via collect_all('mvIMPACT').

# uvicorn discovers its loops/protocols at runtime via importlib — list them explicitly.
hiddenimports += [
    'uvicorn.logging',
    'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio', 'uvicorn.loops.uvloop',
    'uvicorn.protocols',
    'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
    'uvicorn.protocols.websockets.websockets_impl', 'uvicorn.protocols.websockets.wsproto_impl',
    'uvicorn.lifespan', 'uvicorn.lifespan.on', 'uvicorn.lifespan.off',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='machine_controller',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='machine_controller',
)
