import AppKit
import Virtualization
@_cdecl("cl_vm_test_display")
public func testDisplay(_ callback: EventCallback, _ context: UnsafeMutableRawPointer?) -> UnsafeMutableRawPointer? {
    if #available(macOS 13.0, *) {
        let runner = Runner(callback: callback, context: context)
        let view = KeyboardProbeView()
        view.onFocus = { [weak runner] in runner?.report("INPUT:captured") }
        runner.view = view
        return Unmanaged.passRetained(runner).toOpaque()
    }
    return nil
}
@_cdecl("cl_vm_test_parent")
public func testParent(_ pointer: UnsafeMutableRawPointer) -> Bool {
    if #available(macOS 13.0, *) { return Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().view?.superview != nil }
    return false
}
@_cdecl("cl_vm_test_frame")
public func testFrame(_ pointer: UnsafeMutableRawPointer, _ values: UnsafeMutablePointer<Double>) {
    if #available(macOS 13.0, *), let frame = Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().view?.frame {
        values[0] = frame.origin.x; values[1] = frame.origin.y; values[2] = frame.width; values[3] = frame.height
    }
}

@available(macOS 13.0, *)
final class KeyboardProbeView: EmbeddedVMView {
    var keyCount: Int32 = 0
    var mouseCount: Int32 = 0
    override func mouseDown(with event: NSEvent) { mouseCount += 1; super.mouseDown(with: event) }
    override func keyDown(with event: NSEvent) { keyCount += 1 }
    override func keyUp(with event: NSEvent) {}
    override func flagsChanged(with event: NSEvent) {}
}
@_cdecl("cl_vm_test_keys")
public func testKeys(_ pointer: UnsafeMutableRawPointer) -> Int32 {
    if #available(macOS 13.0, *) {
        return (Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().view as? KeyboardProbeView)?.keyCount ?? -1
    }
    return -1
}
@_cdecl("cl_vm_test_post_escape")
public func postEscape(_ pointer: UnsafeMutableRawPointer) {
    if #available(macOS 13.0, *), let window = Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().view?.window,
       let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [],
        timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil,
        characters: "\u{1b}", charactersIgnoringModifiers: "\u{1b}", isARepeat: false, keyCode: 53) {
        NSApp.postEvent(event, atStart: false)
    }
}
@_cdecl("cl_vm_test_post_click")
public func postClick(_ pointer: UnsafeMutableRawPointer) {
    if #available(macOS 13.0, *), let view = Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().view,
       let window = view.window {
        let point = view.convert(NSPoint(x: view.bounds.midX, y: view.bounds.midY), to: nil)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            if let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
                timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber,
                context: nil, eventNumber: 1, clickCount: 1, pressure: 1) {
                NSApp.postEvent(event, atStart: false)
            }
        }
    }
}
@_cdecl("cl_vm_test_clicks")
public func testClicks(_ pointer: UnsafeMutableRawPointer) -> Int32 {
    if #available(macOS 13.0, *) {
        return (Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().view as? KeyboardProbeView)?.mouseCount ?? -1
    }
    return -1
}
