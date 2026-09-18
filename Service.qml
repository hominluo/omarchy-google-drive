import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

Item {
  id: root

  property var settings: ({})

  property bool installed: false
  property bool authenticated: false
  property bool syncing: false
  property bool timerEnabled: false
  property bool browseMounted: false
  property bool browseStale: false
  property bool browseEnabled: false
  property bool syncBaseline: false
  property string statusText: "Checking…"
  property string folderPath: ""
  property string mountPath: ""
  property int selectedCount: 0
  property bool rootFiles: true
  property double localBytes: 0
  property bool localBytesApprox: false
  property double usedBytes: 0
  property double quotaBytes: 0
  property bool quotaKnown: false
  property string lastResult: ""
  property double lastFinishedTs: 0
  property string warning: ""
  property string lastError: ""
  property string actionStatus: ""

  property var folders: []
  property double staleBytes: 0
  property int rootFileCount: 0
  property double rootFileBytes: 0
  property bool foldersLoading: false
  property string foldersError: ""

  // Optimistic view of a toggle the backend has not confirmed yet, so the
  // switch does not visibly bounce back while rclone catches up.
  property var pendingFolders: ({})
  property int _desiredBrowse: -1
  property int _desiredTimer: -1

  readonly property string homePath: Quickshell.env("HOME") || ""
  readonly property string remoteName: String(setting("remoteName", "gdrive")).trim()
  readonly property string configuredFolder: expandHome(String(setting("folderPath", "~/Google Drive")))
  readonly property string configuredMount: expandHome(String(setting("browseMountPath", "~/GDrive-Browse")))
  readonly property int syncIntervalMin: intSetting("syncIntervalMin", 10, 1, 1440)
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 30, 10, 3600)
  readonly property bool busy: controlProcess.running
  readonly property bool browseActive: _desiredBrowse === -1 ? browseMounted : (_desiredBrowse === 1)
  readonly property bool autoSyncActive: _desiredTimer === -1 ? timerEnabled : (_desiredTimer === 1)

  readonly property string helperPath: {
    var value = String(Qt.resolvedUrl("gdrive-sync.py"))
    if (value.indexOf("file://") === 0) value = value.substring(7)
    return decodeURIComponent(value)
  }

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  function expandHome(path) {
    var value = String(path || "")
    if (value === "~") return homePath
    if (value.indexOf("~/") === 0) return homePath + value.substring(1)
    return value
  }

  function elide(text, limit) {
    var value = String(text || "").replace(/\s+/g, " ").trim()
    var cap = limit === undefined ? 180 : limit
    return value.length > cap ? value.substring(0, cap - 1) + "…" : value
  }

  function baseArgs(command) {
    return ["python3", helperPath, command]
  }

  // sync/timer/browse write the systemd units, so they need the configured
  // paths to bake into them.
  function unitArgs(command) {
    return baseArgs(command).concat([
      "--remote", remoteName,
      "--folder", configuredFolder,
      "--mount", configuredMount
    ])
  }

  function refresh() {
    if (statusProcess.running || helperPath === "") return
    statusProcess.command = baseArgs("status").concat([
      "--remote", remoteName,
      "--folder", configuredFolder,
      "--mount", configuredMount
    ])
    statusProcess.running = true
    statusWatchdog.restart()
  }

  function refreshFolders() {
    if (foldersProcess.running || helperPath === "" || !authenticated) return
    foldersLoading = true
    foldersProcess.command = baseArgs("folders").concat([
      "--remote", remoteName,
      "--folder", configuredFolder
    ])
    foldersProcess.running = true
    foldersWatchdog.restart()
  }

  function applyStatus(raw) {
    var parsed = Model.parseStatus(raw)
    if (parsed.ok === false && parsed.lastError) {
      lastError = parsed.lastError
      return
    }
    installed = parsed.installed === true
    authenticated = parsed.authenticated === true
    syncing = parsed.syncing === true
    browseMounted = parsed.browseMounted === true
    browseStale = parsed.browseStale === true
    browseEnabled = parsed.browseEnabled === true
    timerEnabled = parsed.timerEnabled === true
    syncBaseline = parsed.baseline === true
    statusText = String(parsed.statusText || "Unavailable")
    folderPath = String(parsed.folderPath || configuredFolder)
    mountPath = String(parsed.mountPath || configuredMount)
    selectedCount = Number(parsed.selectedCount || 0)
    rootFiles = parsed.rootFiles !== false
    localBytes = Number(parsed.localBytes || 0)
    localBytesApprox = parsed.localBytesApprox === true
    usedBytes = Number(parsed.usedBytes || 0)
    quotaBytes = Number(parsed.quotaBytes || 0)
    quotaKnown = parsed.quotaKnown === true
    lastResult = String(parsed.lastResult || "")
    lastFinishedTs = Number(parsed.lastFinishedTs || 0)
    warning = String(parsed.warning || "")
    lastError = String(parsed.lastError || "")

    if (_desiredBrowse !== -1 && browseMounted === (_desiredBrowse === 1)) _desiredBrowse = -1
    if (_desiredTimer !== -1 && timerEnabled === (_desiredTimer === 1)) _desiredTimer = -1
    if (authenticated && folders.length === 0 && !foldersLoading) refreshFolders()
  }

  function applyFolders(raw) {
    var parsed = Model.parseFolders(raw)
    if (parsed.ok === false) {
      foldersError = parsed.lastError || "Could not list Google Drive folders"
      return
    }
    folders = parsed.folders
    staleBytes = parsed.staleBytes
    rootFileCount = Number(parsed.rootFileCount || 0)
    rootFileBytes = Number(parsed.rootFileBytes || 0)
    rootFiles = parsed.rootFiles !== false
    foldersError = ""
    pendingFolders = ({})
  }

  function isSelected(folder) {
    if (!folder) return false
    var pending = pendingFolders[folder.name]
    return pending === undefined ? folder.selected === true : pending === true
  }

  function toggleFolder(folder) {
    if (!folder || busy) return
    var name = String(folder.name)
    var next = !isSelected(folder)
    var copy = {}
    for (var key in pendingFolders) copy[key] = pendingFolders[key]
    copy[name] = next
    pendingFolders = copy

    note(next ? "Adding " + name + "…" : "Removing " + name + " from sync…")
    // `--add=NAME`: a folder called "-Something" must not read as an option.
    runControl(baseArgs("select").concat([(next ? "--add=" : "--remove=") + name]), function () {
      root.refreshFolders()
      root.refresh()
    })
  }

  function setRootFiles(enabled) {
    if (busy) return
    note(enabled ? "Including root files…" : "Excluding root files…")
    runControl(baseArgs("select").concat([enabled ? "--root-files" : "--no-root-files"]), function () {
      root.refreshFolders()
      root.refresh()
    })
  }

  function syncNow(resync) {
    if (busy || syncing) return
    note("Starting sync…")
    var args = unitArgs("sync")
    if (resync === true) args.push("--resync")
    runControl(args, function () {
      root.syncing = true
      settleTimer.ticks = 0
      settleTimer.restart()
    })
  }

  function setAutoSync(enabled) {
    if (busy) return
    _desiredTimer = enabled ? 1 : 0
    note(enabled ? "Enabling automatic sync…" : "Pausing automatic sync…")
    var args = unitArgs("timer").concat([enabled ? "--enable" : "--disable"])
    if (enabled) args = args.concat(["--interval", String(syncIntervalMin)])
    runControl(args, function () { root.refresh() })
  }

  function toggleBrowse() {
    if (busy) return
    var turningOn = !browseActive
    _desiredBrowse = turningOn ? 1 : 0
    note(turningOn ? "Mounting browse folder…" : "Unmounting browse folder…")
    runControl(unitArgs("browse").concat([turningOn ? "--enable" : "--disable"]),
               function () { root.refresh() })
  }

  function cleanupStale() {
    if (busy || staleBytes <= 0) return
    note("Verifying against Drive before deleting…")
    runControl(baseArgs("cleanup").concat([
      "--remote", remoteName, "--folder", configuredFolder
    ]), function () {
      root.refreshFolders()
      root.refresh()
    })
  }

  function openFolder() {
    if (folderPath !== "") Quickshell.execDetached(["uwsm-app", "--", "nautilus", fileUri(folderPath)])
  }

  function openBrowse() {
    if (browseActive && mountPath !== "")
      Quickshell.execDetached(["uwsm-app", "--", "nautilus", fileUri(mountPath)])
    else
      Quickshell.execDetached(["omarchy-launch-browser", "https://drive.google.com/drive/my-drive"])
  }

  function fileUri(path) {
    var parts = String(path || "").split("/")
    for (var i = 0; i < parts.length; i++) parts[i] = encodeURIComponent(parts[i])
    return "file://" + parts.join("/")
  }

  function note(text) {
    actionStatus = text
    actionStatusTimer.restart()
  }

  property var _onControlDone: null

  function runControl(command, onDone) {
    _onControlDone = onDone || null
    controlProcess.command = command
    controlProcess.running = true
    controlWatchdog.restart()
  }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // While a sync is in flight, poll faster so the panel tracks it.
  Timer {
    id: syncPoll
    interval: 3000
    repeat: true
    running: root.syncing
    onTriggered: root.refresh()
  }

  Timer {
    id: settleTimer
    property int ticks: 0
    interval: 1600
    repeat: true
    running: false
    onTriggered: {
      ticks += 1
      root.refresh()
      if (ticks >= 6) { ticks = 0; stop() }
    }
  }

  Timer {
    id: actionStatusTimer
    interval: 3200
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  Timer {
    id: statusWatchdog
    interval: 45000
    repeat: false
    onTriggered: {
      if (!statusProcess.running) return
      statusProcess.running = false
      root.lastError = "Google Drive status check timed out"
    }
  }

  Timer {
    id: foldersWatchdog
    interval: 120000
    repeat: false
    onTriggered: {
      if (!foldersProcess.running) return
      foldersProcess.running = false
      root.foldersLoading = false
      root.foldersError = "Listing Google Drive folders timed out"
    }
  }

  Timer {
    id: controlWatchdog
    interval: 1800000
    repeat: false
    onTriggered: {
      if (!controlProcess.running) return
      controlProcess.running = false
      root._desiredBrowse = -1
      root._desiredTimer = -1
      root.lastError = "The Google Drive command timed out"
      root.note(root.lastError)
    }
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusStdout; waitForEnd: true }
    stderr: StdioCollector { id: statusStderr; waitForEnd: true }
    onExited: function (exitCode) {
      statusWatchdog.stop()
      if (exitCode === 0) root.applyStatus(String(statusStdout.text || ""))
      else root.lastError = root.elide(String(statusStderr.text || "") || "Could not read Google Drive status")
    }
  }

  Process {
    id: foldersProcess
    running: false
    command: []
    stdout: StdioCollector { id: foldersStdout; waitForEnd: true }
    stderr: StdioCollector { id: foldersStderr; waitForEnd: true }
    onExited: function (exitCode) {
      foldersWatchdog.stop()
      root.foldersLoading = false
      if (exitCode === 0) root.applyFolders(String(foldersStdout.text || ""))
      else root.foldersError = root.elide(String(foldersStderr.text || "") || "Could not list Google Drive folders")
    }
  }

  Process {
    id: controlProcess
    running: false
    command: []
    stdout: StdioCollector { id: controlStdout; waitForEnd: true }
    stderr: StdioCollector { id: controlStderr; waitForEnd: true }
    onExited: function (exitCode) {
      controlWatchdog.stop()
      var stderrText = String(controlStderr.text || "")
      if (exitCode !== 0) {
        root._desiredBrowse = -1
        root._desiredTimer = -1
        root.pendingFolders = ({})
        root.lastError = root.elide(stderrText || "The Google Drive command failed")
        root.note(root.lastError)
      } else {
        root.lastError = ""
      }
      var callback = root._onControlDone
      root._onControlDone = null
      if (callback) Qt.callLater(callback)
    }
  }
}
