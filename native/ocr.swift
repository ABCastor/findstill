// Local Apple Vision OCR. Geometry is normalized to the EXIF-oriented image,
// with origin at top-left, x right and y down. No page warping is performed.
import Foundation
import Vision
import ImageIO
import CoreGraphics

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func point(_ p: CGPoint) -> [String: Double] {
    ["x": Double(p.x), "y": Double(1 - p.y)]
}

func box(_ r: CGRect) -> [String: Double] {
    ["x": Double(r.minX), "y": Double(1 - r.maxY),
     "width": Double(r.width), "height": Double(r.height)]
}

guard CommandLine.arguments.count == 2 else { fail("usage: ocr IMAGE_PATH") }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
      let width = props[kCGImagePropertyPixelWidth] as? Int,
      let height = props[kCGImagePropertyPixelHeight] as? Int,
      width > 0, height > 0, width <= 12000, height <= 12000,
      Int64(width) * Int64(height) <= 60_000_000 else {
    fail("Image cannot be decoded or exceeds the 12000-edge / 60-million-pixel limit")
}
let exifOrientation = (props[kCGImagePropertyOrientation] as? Int) ?? 1
let swapsAxes = (5...8).contains(exifOrientation)
let orientedWidth = swapsAxes ? height : width
let orientedHeight = swapsAxes ? width : height
// ImageIO applies EXIF, including mirrored orientations, before Vision sees pixels.
let options: [CFString: Any] = [
    kCGImageSourceCreateThumbnailFromImageAlways: true,
    kCGImageSourceCreateThumbnailWithTransform: true,
    kCGImageSourceThumbnailMaxPixelSize: 6000,
    kCGImageSourceShouldCacheImmediately: true
]
guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else {
    fail("ImageIO could not decode image pixels")
}
let started = Date()
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
let handler = VNImageRequestHandler(cgImage: image, orientation: .up, options: [:])
do { try handler.perform([request]) } catch { fail("Apple Vision OCR failed: \(error.localizedDescription)") }
let lines: [[String: Any]] = (request.results ?? []).compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    return ["text": candidate.string, "confidence": Double(candidate.confidence),
            "source": "apple-vision", "bounding_box": box(observation.boundingBox)]
}
var document: Any = NSNull()
var warnings: [String] = []
if #available(macOS 12.0, *) {
    let segmentation = VNDetectDocumentSegmentationRequest()
    do {
        try handler.perform([segmentation])
        if let page = segmentation.results?.first {
            document = ["source": "apple-vision-document-segmentation",
                        "confidence": Double(page.confidence),
                        "bounding_box": box(page.boundingBox),
                        "corners": ["top_left": point(page.topLeft), "top_right": point(page.topRight),
                                    "bottom_right": point(page.bottomRight), "bottom_left": point(page.bottomLeft)]]
        }
    } catch { warnings.append("Document boundary detection failed; text OCR is available") }
}
let result: [String: Any] = [
    "schema_version": 1, "source": "apple-vision",
    "confidence_semantics": "Vision recognition scores, not calibrated probabilities of correctness",
    "coordinate_space": "normalized_exif_oriented_image_top_left_x_right_y_down",
    "image": ["width": orientedWidth, "height": orientedHeight,
              "original_exif_orientation": exifOrientation,
              "processed_width": image.width, "processed_height": image.height],
    "recognition": ["level": "accurate", "languages": ["en-US"], "language_correction": false],
    "lines": lines, "document": document, "warnings": warnings,
    "processing_seconds": Date().timeIntervalSince(started)
]
do {
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
} catch { fail("Could not encode OCR result: \(error.localizedDescription)") }
