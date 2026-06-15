const { app, BrowserWindow, ipcMain, Menu, net } = require('electron')
const { autoUpdater } = require('electron-updater')
const { spawn } = require('child_process')
const path = require('path')
const fs = require('fs')

function logToMachineCode(level, message, context = {}) {
  try {
    const body = JSON.stringify({
      level,
      message,
      source: 'electron-main',
      context: { timestamp: new Date().toISOString(), ...context },
    })
    const machineUrl = process.env.VITE_MACHINE_URL || 'http://localhost:8001'
    const req = net.request({ method: 'POST', url: `${machineUrl}/api/frontend-log` })
    req.setHeader('Content-Type', 'application/json')
    req.on('error', () => {}) // silently ignore if machine_code isn't running yet
    req.write(body)
    req.end()
  } catch {}
}


const isDev = process.env.NODE_ENV === 'development'

// Never auto-download — GitLab Package Registry rejects the HEAD/Range preflight
// that electron-updater sends before starting a download, causing ERR_ABORTED.
// Instead we check silently and let the user trigger the download from the banner.
autoUpdater.autoDownload = false
autoUpdater.autoInstallOnAppQuit = true

// Route electron-updater's internal logs to machine_code.
// Loguru treats the message as a format string — avoid JSON (curly braces break it).
// Use key=value pairs instead so the full content is visible in the log.
function fmtUpdaterMsg(msg) {
  if (msg instanceof Error) return msg.message || String(msg)
  if (typeof msg === 'object' && msg !== null)
    return Object.entries(msg).map(([k, v]) => `${k}=${v}`).join(' ')
  return String(msg)
}
autoUpdater.logger = {
  info:  (msg) => logToMachineCode('info',    `[updater] ${fmtUpdaterMsg(msg)}`),
  warn:  (msg) => logToMachineCode('warning', `[updater] ${fmtUpdaterMsg(msg)}`),
  error: (msg) => logToMachineCode('error',   `[updater] ${fmtUpdaterMsg(msg)}`),
  debug: () => {},
}

// Load PAT baked into the bundle by CI (gitignored, never committed).
try {
  const { token } = require('./update-config.json')
  if (token) autoUpdater.requestHeaders = { 'PRIVATE-TOKEN': token }
} catch {}

let mainWindow = null
let machineController = null
let lastUpdateStatus = null
let updaterInitialised = false  // guard: listeners registered only once

if (!app.requestSingleInstanceLock()) {
  app.quit()
}

app.on('second-instance', () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.focus()
  }
})

function getMachineControllerBinary() {
  if (app.isPackaged) {
    // Inside the installed AppImage
    return path.join(process.resourcesPath, 'machine_controller', 'machine_controller')
  }
  // app:test mode: binary built locally by PyInstaller
  return path.join(__dirname, '../../machine_code/dist/machine_controller/machine_controller')
}

function ensureEnvFile(workDir) {
  // Pre-create the logs directory so loguru can write there on first launch.
  fs.mkdirSync(path.join(workDir, 'logs'), { recursive: true })

  const envPath = path.join(workDir, '.env')
  if (!fs.existsSync(envPath)) {
    fs.writeFileSync(envPath,
      '# Set this to the unique ID of this physical machine.\nMACHINE_ID=0\n',
      'utf8'
    )
    console.log('[machine_controller] created default .env at', envPath)
  }
}

function startMachineController() {
  if (isDev) return  // In dev, machine_code is run manually via uvicorn

  const binary = getMachineControllerBinary()
  if (!fs.existsSync(binary)) {
    console.error('[machine_controller] binary not found:', binary)
    return
  }

  const workDir = app.getPath('userData')
  fs.mkdirSync(workDir, { recursive: true })
  ensureEnvFile(workDir)

  // Loguru already writes a rotating machine_app.log to logs/ inside workDir,
  // so stdout does not need to be re-captured here — that would just produce a
  // duplicate file with ANSI colour codes and no rotation.
  // stderr is still captured separately for crash tracebacks that bypass loguru.
  const stderrPath = path.join(app.getPath('logs'), 'machine_controller_stderr.log')
  const stderrStream = fs.createWriteStream(stderrPath, { flags: 'a' })

  machineController = spawn(binary, [], { cwd: workDir, stdio: ['ignore', 'ignore', 'pipe'] })
  machineController.stderr.pipe(stderrStream)
  machineController.on('exit', (code, signal) => {
    console.log(`[machine_controller] exited code=${code} signal=${signal}`)
    logToMachineCode('warning', 'machine_controller process exited', { code, signal })
    machineController = null
  })

  console.log('[machine_controller] started pid', machineController.pid)
  logToMachineCode('info', 'machine_controller started', { pid: machineController.pid })
}

function stopMachineController() {
  if (machineController) {
    machineController.kill('SIGTERM')
    machineController = null
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  })

  mainWindow.maximize()

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'))
  }

  setupAutoUpdater()
}

// Returns a user-facing message, or null if the error is expected/non-actionable
// (no release published yet, GitLab aborts requests to missing packages).
function friendlyUpdateError(msg) {
  if (msg.includes('ERR_ABORTED') || msg.includes('404') || msg.includes('Not Found'))
    return null  // no release on server yet — normal during development, not shown to user
  if (msg.includes('ENOTFOUND') || msg.includes('ERR_CONNECTION') || msg.includes('ERR_ADDRESS'))
    return 'Update check failed — server unreachable'
  if (msg.includes('401') || msg.includes('403') || msg.includes('Unauthorized'))
    return 'Update check failed — authentication error'
  return msg
}

function setupAutoUpdater() {
  if (isDev) return
  if (updaterInitialised) return
  updaterInitialised = true

  // When running via `electron .` (app:test), the app is not packaged so electron-updater
  // can't find app-update.yml. Point it at our dev config so the check still runs.
  if (!app.isPackaged) {
    autoUpdater.updateConfigPath = path.join(__dirname, 'dev-app-update.yml')
    autoUpdater.forceDevUpdateConfig = true
  }

  autoUpdater.on('checking-for-update', () => {
    logToMachineCode('info', 'Auto-update: checking for update')
    sendUpdateStatus({ status: 'checking' })
  })

  autoUpdater.on('update-available', (info) => {
    logToMachineCode('info', `Auto-update: update available — v${info.version}`)
    sendUpdateStatus({ status: 'available', version: info.version })
  })

  autoUpdater.on('update-not-available', (info) => {
    logToMachineCode('info', `Auto-update: already up to date — v${info.version}`)
    sendUpdateStatus({ status: 'not-available' })
  })

  autoUpdater.on('download-progress', (progress) => {
    sendUpdateStatus({
      status: 'downloading',
      percent: Math.round(progress.percent),
      bytesPerSecond: progress.bytesPerSecond,
    })
  })

  autoUpdater.on('update-downloaded', (info) => {
    logToMachineCode('info', `Auto-update: download complete — v${info.version}`)
    sendUpdateStatus({ status: 'downloaded', version: info.version })
  })

  autoUpdater.on('error', (err) => {
    const raw = err?.message || String(err)
    const message = friendlyUpdateError(raw)
    if (message) {
      logToMachineCode('error', `Auto-update: error — ${raw}`)
      sendUpdateStatus({ status: 'error', message })
    } else {
      logToMachineCode('info', `Auto-update: suppressed non-actionable error — ${raw}`)
    }
  })

  // Use checkForUpdates (not checkForUpdatesAndNotify) — the notify variant sends
  // extra native-notification preflight requests that GitLab Package Registry aborts.
  setTimeout(() => {
    autoUpdater.checkForUpdates().catch((err) => {
      const raw = err?.message || String(err)
      const level = friendlyUpdateError(raw) ? 'error' : 'info'
      logToMachineCode(level, `Auto-update: check failed — ${raw}`)
    })
  }, 3000)
}

function sendUpdateStatus(data) {
  // Only persist states the user needs to act on so reloads don't resurface dismissed banners.
  if (data.status === 'available' || data.status === 'downloaded' || data.status === 'downloading') {
    lastUpdateStatus = data
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('update-status', data)
  }
}

// Renderer can pull the last actionable update status on mount (avoids push race condition).
ipcMain.handle('get-update-status', () => lastUpdateStatus)

// Renderer can trigger download (user clicks Download in the banner)
ipcMain.on('download-update', () => {
  autoUpdater.downloadUpdate().catch((err) => {
    const raw = err?.message || String(err)
    logToMachineCode('error', `Auto-update: download failed — ${raw}`)
    const message = friendlyUpdateError(raw)
    if (message) sendUpdateStatus({ status: 'error', message })
  })
})

// Renderer can trigger install-and-restart
ipcMain.on('install-update', () => {
  autoUpdater.quitAndInstall()
})

// Renderer can query app version
ipcMain.handle('get-version', () => app.getVersion())

app.whenReady().then(() => {
  Menu.setApplicationMenu(null)

  if (!isDev) {
    const appImagePath = path.join(app.getPath('home'), 'medicinestrip-ai', 'MedicineStrip-AI.AppImage')
    app.setLoginItemSettings({ openAtLogin: true, path: appImagePath })
  }

  startMachineController()
  createWindow()
})

app.on('will-quit', stopMachineController)

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})
