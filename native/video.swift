// Local video analysis with Apple frameworks only: frames (AVFoundation),
// speech (SpeechAnalyzer, on device) and on-screen text (Vision).
// Reads one explicit file; prints JSON on stdout; never prints media content to stderr.
//
//   video frames <video> <out-dir> <interval-seconds> <max-edge>
//   video transcribe <video> <work-dir> <locale> [locale ...]
//   video ocr <languages,comma,separated> <image> [image ...]
//   video locales
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO
import NaturalLanguage
import Speech
import UniformTypeIdentifiers
import Vision

func fail(_ message: String, _ code: Int32 = 1) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

func emit(_ object: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else {
        fail("Could not encode result")
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
}

func writeJPEG(_ image: CGImage, to url: URL) -> Bool {
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else {
        return false
    }
    CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
    return CGImageDestinationFinalize(destination)
}

func frames(_ path: String, _ out: String, _ interval: Double, _ edge: Int) async {
    let asset = AVURLAsset(url: URL(fileURLWithPath: path))
    guard let duration = try? await asset.load(.duration), duration.seconds.isFinite else {
        fail("Video cannot be read")
    }
    let tracks = (try? await asset.loadTracks(withMediaType: .video)) ?? []
    let audio = (try? await asset.loadTracks(withMediaType: .audio)) ?? []
    let seconds = max(0, duration.seconds)
    var times: [CMTime] = []
    var t = 0.0
    // The first sample sits slightly after zero; many clips open on a black frame.
    while t < seconds || times.isEmpty {
        times.append(CMTime(seconds: min(t == 0 ? min(0.2, seconds / 2) : t, max(0, seconds - 0.05)), preferredTimescale: 600))
        t += interval
    }
    var rows: [[String: Any]] = []
    if !tracks.isEmpty {
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: edge, height: edge)
        let tolerance = CMTime(seconds: min(0.5, interval / 4), preferredTimescale: 600)
        generator.requestedTimeToleranceBefore = tolerance
        generator.requestedTimeToleranceAfter = tolerance
        var index = 0
        for await result in generator.images(for: times) {
            index += 1
            guard let image = try? result.image else { continue }
            let actual = (try? result.actualTime.seconds) ?? result.requestedTime.seconds
            let url = URL(fileURLWithPath: out).appendingPathComponent(String(format: "frame-%06d.jpg", index))
            if writeJPEG(image, to: url) {
                rows.append(["time": actual, "path": url.path, "width": image.width, "height": image.height])
            }
        }
    }
    rows.sort { ($0["time"] as! Double) < ($1["time"] as! Double) }
    emit(["schema_version": 1, "duration": seconds, "has_video": !tracks.isEmpty, "has_audio": !audio.isEmpty, "frames": rows])
}

func exportAudio(_ path: String, _ work: String) async -> URL? {
    let asset = AVURLAsset(url: URL(fileURLWithPath: path))
    guard let tracks = try? await asset.loadTracks(withMediaType: .audio), !tracks.isEmpty,
          let session = AVAssetExportSession(asset: asset, presetName: AVAssetExportPresetAppleM4A) else { return nil }
    let url = URL(fileURLWithPath: work).appendingPathComponent("audio-\(UUID().uuidString).m4a")
    do { try await session.export(to: url, as: .m4a) } catch { return nil }
    return url
}

struct Segment { let start: Double; let end: Double; let text: String; let confidence: Double? }

func transcribe(_ url: URL, _ localeID: String) async -> (String?, [Segment], String?) {
    guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: localeID)) else {
        return (nil, [], "unsupported locale")
    }
    let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [],
                                        attributeOptions: [.audioTimeRange, .transcriptionConfidence])
    do {
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            try await request.downloadAndInstall()
        }
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let file = try AVAudioFile(forReading: url)
        let collector = Task { () throws -> [Segment] in
            var found: [Segment] = []
            for try await result in transcriber.results {
                if let segment = segment(from: result) { found.append(segment) }
            }
            return found
        }
        if let last = try await analyzer.analyzeSequence(from: file) {
            try await analyzer.finalizeAndFinish(through: last)
        } else {
            await analyzer.cancelAndFinishNow()
        }
        return (locale.identifier(.bcp47), try await collector.value, nil)
    } catch {
        return (locale.identifier(.bcp47), [], "transcription failed")
    }
}

func segment(from result: SpeechTranscriber.Result) -> Segment? {
    let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
    if text.isEmpty { return nil }
    var start = result.range.start.seconds
    var end = result.range.end.seconds
    var weighted = 0.0
    var total = 0.0
    for run in result.text.runs {
        if let range = run.audioTimeRange, !start.isFinite || !end.isFinite {
            start = min(start.isFinite ? start : range.start.seconds, range.start.seconds)
            end = max(end.isFinite ? end : range.end.seconds, range.end.seconds)
        }
        if let confidence = run.transcriptionConfidence {
            let weight = Double(result.text[run.range].characters.count)
            weighted += confidence * weight
            total += weight
        }
    }
    return Segment(start: start.isFinite ? start : 0, end: end.isFinite ? end : 0, text: text,
                   confidence: total > 0 ? weighted / total : nil)
}

func language(of text: String) -> [String: Any] {
    let recognizer = NLLanguageRecognizer()
    recognizer.processString(text)
    let hypotheses = recognizer.languageHypotheses(withMaximum: 5)
    var named: [String: Double] = [:]
    for (key, value) in hypotheses { named[key.rawValue] = value }
    return ["dominant": recognizer.dominantLanguage?.rawValue ?? NSNull(), "hypotheses": named]
}

func transcribeCommand(_ path: String, _ work: String, _ locales: [String]) async {
    guard let audio = await exportAudio(path, work) else {
        emit(["schema_version": 1, "has_audio": false, "candidates": []])
        return
    }
    defer { try? FileManager.default.removeItem(at: audio) }
    var candidates: [[String: Any]] = []
    for id in locales {
        let (resolved, segments, error) = await transcribe(audio, id)
        let text = segments.map(\.text).joined(separator: " ")
        let confidences = segments.compactMap(\.confidence)
        var rows: [[String: Any]] = []
        for item in segments {
            let row: [String: Any] = ["start": item.start, "end": item.end, "text": item.text,
                                      "confidence": item.confidence ?? NSNull()]
            rows.append(row)
        }
        var candidate: [String: Any] = [:]
        candidate["requested_locale"] = id
        candidate["locale"] = resolved ?? NSNull()
        candidate["error"] = error ?? NSNull()
        candidate["words"] = text.split(whereSeparator: { $0.isWhitespace }).count
        let mean: Any = confidences.isEmpty ? NSNull() : confidences.reduce(0, +) / Double(confidences.count)
        candidate["mean_confidence"] = mean
        candidate["language"] = language(of: text)
        candidate["segments"] = rows
        candidates.append(candidate)
    }
    emit(["schema_version": 1, "has_audio": true, "candidates": candidates])
}

func ocrCommand(_ languages: [String], _ paths: [String]) {
    var pages: [[String: Any]] = []
    for path in paths {
        guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            pages.append(["path": path, "lines": [], "error": "decode failed"])
            continue
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.recognitionLanguages = languages
        let handler = VNImageRequestHandler(cgImage: image, orientation: .up, options: [:])
        do { try handler.perform([request]) } catch {
            pages.append(["path": path, "lines": [], "error": "vision failed"])
            continue
        }
        let lines: [[String: Any]] = (request.results ?? []).compactMap { observation in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            return ["text": candidate.string, "confidence": Double(candidate.confidence)]
        }
        pages.append(["path": path, "lines": lines])
    }
    emit(["schema_version": 1, "pages": pages])
}

let args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { fail("usage: video frames|transcribe|ocr|locales ...", 2) }
switch command {
case "frames" where args.count == 5:
    guard let interval = Double(args[3]), interval > 0, let edge = Int(args[4]), edge >= 64 else { fail("bad frame arguments", 2) }
    await frames(args[1], args[2], interval, edge)
case "transcribe" where args.count >= 4:
    await transcribeCommand(args[1], args[2], Array(args[3...]))
case "ocr" where args.count >= 3:
    ocrCommand(args[1].split(separator: ",").map(String.init), Array(args[2...]))
case "locales":
    let supported = await SpeechTranscriber.supportedLocales.map { $0.identifier(.bcp47) }.sorted()
    let installed = await SpeechTranscriber.installedLocales.map { $0.identifier(.bcp47) }.sorted()
    emit(["supported": supported, "installed": installed])
default:
    fail("usage: video frames|transcribe|ocr|locales ...", 2)
}
