import AppKit
import Foundation

guard CommandLine.arguments.count == 3,
      let size = Int(CommandLine.arguments[1]) else {
    fputs("usage: make_icon.swift SIZE OUTPUT\n", stderr)
    exit(2)
}

let output = CommandLine.arguments[2]
guard let canvas = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: size,
    pixelsHigh: size,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
), let context = NSGraphicsContext(bitmapImageRep: canvas) else {
    exit(1)
}
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context

let rect = NSRect(x: 0, y: 0, width: size, height: size)
NSColor(calibratedRed: 0.07, green: 0.08, blue: 0.10, alpha: 1).setFill()
NSBezierPath(roundedRect: rect.insetBy(dx: CGFloat(size) * 0.06, dy: CGFloat(size) * 0.06), xRadius: CGFloat(size) * 0.19, yRadius: CGFloat(size) * 0.19).fill()

let trackWidth = CGFloat(size) * 0.11
let centerY = CGFloat(size) * 0.52
let leftX = CGFloat(size) * 0.25
let rightX = CGFloat(size) * 0.75

NSColor(calibratedRed: 0.10, green: 0.76, blue: 0.52, alpha: 1).setStroke()
let track = NSBezierPath()
track.lineWidth = trackWidth
track.lineCapStyle = .round
track.move(to: NSPoint(x: leftX, y: centerY))
track.line(to: NSPoint(x: rightX, y: centerY))
track.stroke()

NSColor.white.setFill()
for x in [leftX, CGFloat(size) * 0.5, rightX] {
    NSBezierPath(ovalIn: NSRect(x: x - CGFloat(size) * 0.075, y: centerY - CGFloat(size) * 0.075, width: CGFloat(size) * 0.15, height: CGFloat(size) * 0.15)).fill()
}

NSColor(calibratedRed: 0.00, green: 0.43, blue: 0.50, alpha: 1).setStroke()
let arrow = NSBezierPath()
arrow.lineWidth = CGFloat(size) * 0.055
arrow.lineCapStyle = .round
arrow.lineJoinStyle = .round
arrow.move(to: NSPoint(x: CGFloat(size) * 0.42, y: CGFloat(size) * 0.72))
arrow.line(to: NSPoint(x: CGFloat(size) * 0.58, y: CGFloat(size) * 0.72))
arrow.line(to: NSPoint(x: CGFloat(size) * 0.53, y: CGFloat(size) * 0.78))
arrow.move(to: NSPoint(x: CGFloat(size) * 0.58, y: CGFloat(size) * 0.72))
arrow.line(to: NSPoint(x: CGFloat(size) * 0.53, y: CGFloat(size) * 0.66))
arrow.stroke()

NSGraphicsContext.restoreGraphicsState()
guard let png = canvas.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: output))
