// Lädt die Originaldateien von Fotos-Einträgen direkt über PhotoKit (inkl. Download aus iCloud).
// Aufruf (über die App-Hülle, damit macOS die Foto-Berechtigung dieser App zuordnet):
//   open -W -n PhotoFetch.app --args <uuids.txt> <zielverzeichnis> [parallel=6] [stall-sekunden=90]
// Schreibt je Eintrag {uuid}.<ext> (Original), {uuid}.mov (Live-Video), {uuid}_edited.<ext> (bearbeitet)
// sowie fetch.log und am Ende fetch.done (JSON mit Zählern und Fehlern).
import Foundation
import Photos
import UniformTypeIdentifiers

// Als unsichtbare App ohne Fenster würde macOS den Prozess nach kurzer Zeit drosseln ("App Nap") und seine
// Netzwerkzugriffe hintanstellen. Diese Aktivität meldet dem System: vom Nutzer gestartet, nicht drosseln.
let activity = ProcessInfo.processInfo.beginActivity(
  options: [.userInitiated, .latencyCritical, .idleSystemSleepDisabled, .suddenTerminationDisabled, .automaticTerminationDisabled],
  reason: "Originalfotos aus iCloud laden")

let args = CommandLine.arguments
guard args.count >= 3 else { FileHandle.standardError.write("Aufruf: <uuids.txt> <ziel> [parallel]\n".data(using: .utf8)!); exit(2) }
let outDir = URL(fileURLWithPath: args[2])
let parallel = args.count > 3 ? Int(args[3]) ?? 6 : 6
let stall = TimeInterval(args.count > 4 ? Int(args[4]) ?? 90 : 90)  // Sekunden ohne Fortschritt, bis eine Datei abgebrochen wird
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
let logURL = outDir.appendingPathComponent("fetch.log")
FileManager.default.createFile(atPath: logURL.path, contents: nil)
let logHandle = try! FileHandle(forWritingTo: logURL)
let lock = NSLock()
func log(_ s: String) { lock.lock(); logHandle.write((s + "\n").data(using: .utf8)!); lock.unlock() }

func finish(_ payload: [String: Any], code: Int32) -> Never {
  let data = try! JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted])
  try? data.write(to: outDir.appendingPathComponent("fetch.done"))
  exit(code)
}

guard let list = try? String(contentsOfFile: args[1], encoding: .utf8) else { finish(["error": "UUID-Liste nicht lesbar"], code: 2) }
let uuids = list.split(whereSeparator: \.isNewline).map { String($0).trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
// Ohne UUIDs gibt es nichts zu tun – niemals "alles" laden.
guard !uuids.isEmpty else { finish(["error": "leere UUID-Liste"], code: 2) }

func suffix(for type: PHAssetResourceType) -> String? {
  switch type {
  case .photo, .video: return ""
  case .pairedVideo: return ""            // Live-Video: eigene Endung (.mov) unterscheidet es vom Bild
  case .fullSizePhoto, .fullSizeVideo, .fullSizePairedVideo: return "_edited"
  default: return nil                      // Anpassungsdaten, Basisbilder usw. brauchen wir nicht
  }
}

PHPhotoLibrary.requestAuthorization(for: .readWrite) { status in
  guard status == .authorized else { finish(["error": "kein Zugriff auf Fotos (Status \(status.rawValue))"], code: 1) }
  DispatchQueue.global(qos: .userInitiated).async {
    let fetched = PHAsset.fetchAssets(withLocalIdentifiers: uuids.map { $0 + "/L0/001" }, options: {
      let o = PHFetchOptions(); o.includeHiddenAssets = true; return o
    }())
    var assets: [String: PHAsset] = [:]
    fetched.enumerateObjects { a, _, _ in assets[String(a.localIdentifier.prefix(36))] = a }
    var errors: [String] = uuids.filter { assets[$0] == nil }.map { "\($0): in Fotos nicht gefunden" }
    var files = 0
    let slots = DispatchSemaphore(value: parallel)
    let group = DispatchGroup()
    let manager = PHAssetResourceManager.default()

    // Ein Auftrag je Datei. iCloud liefert einzelne Dateien manchmal gar nicht aus; ohne Zeitlimit würde so eine
    // Datei den ganzen Stapel aufhalten. Deshalb: kein Fortschritt für `stall` Sekunden -> abbrechen und als Fehler
    // melden (der Gesamtlauf versucht sie später erneut).
    // Das Limit zählt erst ab dem ersten empfangenen Byte: macOS lädt Originale nacheinander, wartende Aufträge
    // stehen oft minutenlang ohne Fortschritt in der Schlange und dürfen deshalb nicht abgebrochen werden.
    final class Job {
      let label: String, tmp: URL, target: URL
      var last = Date(), done = false, started = false, handle: FileHandle?, id: PHAssetResourceDataRequestID = 0
      init(label: String, tmp: URL, target: URL) { self.label = label; self.tmp = tmp; self.target = target }
    }
    var jobs: [Job] = []
    func complete(_ job: Job, _ error: String?) {
      lock.lock()
      if job.done { lock.unlock(); return }
      job.done = true
      try? job.handle?.close()
      if let error = error { try? FileManager.default.removeItem(at: job.tmp); errors.append("\(job.label): \(error)") }
      else { try? FileManager.default.moveItem(at: job.tmp, to: job.target); files += 1 }
      lock.unlock()
      log(error == nil ? "ok \(job.target.lastPathComponent)" : "FEHLER \(job.label): \(error!)")
      slots.signal(); group.leave()
    }
    let watchdog = DispatchSource.makeTimerSource(queue: DispatchQueue.global())
    watchdog.schedule(deadline: .now() + 5, repeating: 5)
    watchdog.setEventHandler {
      lock.lock(); let stuck = jobs.filter { !$0.done && $0.started && Date().timeIntervalSince($0.last) > stall }; lock.unlock()
      for job in stuck {
        manager.cancelDataRequest(job.id)
        complete(job, "kein Fortschritt seit \(Int(stall)) s, abgebrochen")
      }
    }
    watchdog.resume()

    for (uuid, asset) in assets {
      for res in PHAssetResource.assetResources(for: asset) {
        guard let suf = suffix(for: res.type) else { continue }
        // Manche Apps speichern ohne Dateiendung; dann die Endung aus dem Dateityp ableiten (z. B. mov).
        var ext = (res.originalFilename as NSString).pathExtension
        if ext.isEmpty { ext = UTType(res.uniformTypeIdentifier)?.preferredFilenameExtension ?? "bin" }
        let target = outDir.appendingPathComponent("\(uuid)\(suf).\(ext)")
        if FileManager.default.fileExists(atPath: target.path) { lock.lock(); files += 1; lock.unlock(); continue }
        let tmp = outDir.appendingPathComponent(".\(uuid)\(suf).\(ext).part")
        try? FileManager.default.removeItem(at: tmp)
        FileManager.default.createFile(atPath: tmp.path, contents: nil)
        let job = Job(label: "\(uuid) \(res.originalFilename)", tmp: tmp, target: target)
        job.handle = try? FileHandle(forWritingTo: tmp)
        slots.wait(); group.enter()
        let opts = PHAssetResourceRequestOptions(); opts.isNetworkAccessAllowed = true
        opts.progressHandler = { _ in lock.lock(); job.last = Date(); job.started = true; lock.unlock() }
        lock.lock(); job.last = Date(); jobs.append(job); lock.unlock()
        job.id = manager.requestData(for: res, options: opts, dataReceivedHandler: { data in
          lock.lock(); job.last = Date(); job.started = true; let open = !job.done; lock.unlock()
          if open { job.handle?.write(data) }
        }, completionHandler: { err in complete(job, err?.localizedDescription) })
      }
    }
    group.wait()
    finish(["requested": uuids.count, "found": assets.count, "files": files, "errors": errors], code: errors.isEmpty ? 0 : 3)
  }
}
dispatchMain()
