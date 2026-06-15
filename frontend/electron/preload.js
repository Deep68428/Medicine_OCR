const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  // Listen for update status events from main process
  onUpdateStatus: (callback) => {
    const listener = (_, data) => callback(data)
    ipcRenderer.on('update-status', listener)
    return () => ipcRenderer.removeListener('update-status', listener)
  },
  // Trigger download (called when user clicks Download in the banner)
  downloadUpdate: () => ipcRenderer.send('download-update'),
  // Trigger install + restart
  installUpdate: () => ipcRenderer.send('install-update'),
  // Get current app version
  getVersion: () => ipcRenderer.invoke('get-version'),
  // Pull the last actionable update status (called on mount to avoid push race condition)
  getUpdateStatus: () => ipcRenderer.invoke('get-update-status'),
})
