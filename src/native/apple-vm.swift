import AppKit
import Virtualization
import Security
import Darwin

private func copyDiskImage(_ source: URL, to destination: URL, report: @escaping (String) -> Void) throws {
    // APFS clones are copy-on-write, so the VM gets an independent writable disk
    // without eagerly copying all of the base image's virtual size.
    if clonefile(source.path, destination.path, 0) == 0 {
        report("DISK_PROGRESS:100")
        return
    }

    let sourceFD = open(source.path, O_RDONLY)
    guard sourceFD >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    defer { close(sourceFD) }
    let destinationFD = open(destination.path, O_WRONLY | O_CREAT | O_EXCL, mode_t(0o600))
    guard destinationFD >= 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
    defer { close(destinationFD) }

    var fileInfo = stat()
    guard fstat(sourceFD, &fileInfo) == 0, ftruncate(destinationFD, fileInfo.st_size) == 0 else {
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }
    let size = fileInfo.st_size
    guard size > 0 else { report("DISK_PROGRESS:100"); return }

    var lastPercent = -1
    func progress(_ offset: off_t) {
        let percent = min(100, Int(offset * 100 / size))
        if percent != lastPercent {
            lastPercent = percent
            report("DISK_PROGRESS:\(percent)")
        }
    }
    func copyRange(_ start: off_t, _ end: off_t) throws {
        let bufferSize = 8 * 1024 * 1024
        let buffer = UnsafeMutableRawPointer.allocate(byteCount: bufferSize, alignment: 4096)
        defer { buffer.deallocate() }
        var offset = start
        while offset < end {
            let count = Int(min(off_t(bufferSize), end - offset))
            let bytesRead = pread(sourceFD, buffer, count, offset)
            guard bytesRead > 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
            var written = 0
            while written < bytesRead {
                let bytesWritten = pwrite(destinationFD, buffer.advanced(by: written), bytesRead - written, offset + off_t(written))
                guard bytesWritten > 0 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
                written += bytesWritten
            }
            offset += off_t(bytesRead)
            progress(offset)
        }
    }

    var position: off_t = 0
    while position < size {
        let dataStart = lseek(sourceFD, position, SEEK_DATA)
        if dataStart == -1 {
            if errno == ENXIO { break } // Remaining bytes are a sparse hole.
            if errno == EINVAL || errno == ENOTSUP {
                // Some filesystems do not support sparse extent queries.
                try copyRange(position, size)
                position = size
                break
            }
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        if dataStart > position { progress(dataStart) }
        let dataEnd = lseek(sourceFD, dataStart, SEEK_HOLE)
        if dataEnd == -1 && (errno == EINVAL || errno == ENOTSUP) {
            try copyRange(position, size)
            position = size
            break
        }
        guard dataEnd >= dataStart else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        try copyRange(dataStart, min(dataEnd, size))
        position = dataEnd
        progress(position)
    }
    progress(size)
}

struct Configuration: Decodable {
    let name: String
    let cpus: Int
    let memory: UInt64
    let iso: String?
    let disk: String?
    let kernel: String?
    let initramfs: String?
    let directory: String
}

public typealias EventCallback = @convention(c) (UnsafeMutableRawPointer?, UnsafePointer<CChar>?) -> Void

@available(macOS 13.0, *)
class EmbeddedVMView: VZVirtualMachineView {
    var onFocus: (() -> Void)?
    override var acceptsFirstResponder: Bool { true }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        onFocus?()
        super.mouseDown(with: event)
    }
    override func rightMouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        onFocus?()
        super.rightMouseDown(with: event)
    }
}

@available(macOS 13.0, *)
final class Runner: NSObject, VZVirtualMachineDelegate {
    var vm: VZVirtualMachine?
    var view: VZVirtualMachineView?
    var serialInput: FileHandle?
    var serialPipe: Pipe?
    var serialOutput: Pipe?
    var keyboardMonitor: Any?
    var inputCaptured = false
    var didReportKeyboardEvent = false
    var didReportWindowMismatch = false
    var canceled = false
    var finished = false
    let callback: EventCallback
    let context: UnsafeMutableRawPointer?

    init(callback: @escaping EventCallback, context: UnsafeMutableRawPointer?) {
        self.callback = callback
        self.context = context
    }
    func report(_ message: String) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { self.report(message) }
            return
        }
        if finished && message != "STATE:stopped" { return }
        message.withCString { callback(context, $0) }
    }
    func begin(_ json: String) {
        do {
            let options = try JSONDecoder().decode(Configuration.self, from: Data(json.utf8))
            // Large disk copies must not block GTK/AppKit's shared main thread.
            DispatchQueue.global().async {
                do {
                    if let source = options.disk {
                        self.report("Preparing temporary disk image…")
                        try copyDiskImage(URL(fileURLWithPath: source), to:
                            URL(fileURLWithPath: options.directory).appendingPathComponent("root.img")) { message in
                                self.report(message)
                            }
                    }
                    DispatchQueue.main.async {
                        if self.canceled { self.finish(); return }
                        self.configure(options)
                    }
                } catch { DispatchQueue.main.async { self.fail(error) } }
            }
        } catch { fail(error) }
    }
    func configure(_ options: Configuration) {
        do {
            let config = VZVirtualMachineConfiguration()
            config.cpuCount = options.cpus
            config.memorySize = options.memory
            config.platform = VZGenericPlatformConfiguration()
            if let iso = options.iso {
                let loader = VZEFIBootLoader()
                loader.variableStore = try VZEFIVariableStore(creatingVariableStoreAt:
                    URL(fileURLWithPath: options.directory).appendingPathComponent("efi-vars"))
                config.bootLoader = loader
                let attachment = try VZDiskImageStorageDeviceAttachment(url: URL(fileURLWithPath: iso), readOnly: true)
                config.storageDevices = [VZUSBMassStorageDeviceConfiguration(attachment: attachment)]
            } else {
                let loader = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: options.kernel!))
                loader.commandLine = "root=/dev/vda rw rootwait console=hvc0"
                if let initramfs = options.initramfs {
                    loader.initialRamdiskURL = URL(fileURLWithPath: initramfs)
                }
                config.bootLoader = loader
                let disk = URL(fileURLWithPath: options.directory).appendingPathComponent("root.img")
                let attachment = try VZDiskImageStorageDeviceAttachment(url: disk, readOnly: false)
                config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]
            }
            let network = VZVirtioNetworkDeviceConfiguration()
            network.attachment = VZNATNetworkDeviceAttachment()
            config.networkDevices = [network]
            config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
            let graphics = VZVirtioGraphicsDeviceConfiguration()
            graphics.scanouts = [VZVirtioGraphicsScanoutConfiguration(widthInPixels: 1280, heightInPixels: 800)]
            config.graphicsDevices = [graphics]
            config.keyboards = [VZUSBKeyboardConfiguration()]
            config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
            let input = Pipe()
            let output = Pipe()
            serialPipe = input
            serialOutput = output
            serialInput = input.fileHandleForWriting
            let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
            serial.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: input.fileHandleForReading,
                                                               fileHandleForWriting: output.fileHandleForWriting)
            config.serialPorts = [serial]
            output.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if !data.isEmpty { self.report("SERIAL:" + data.base64EncodedString()) }
            }
            try config.validate()
            // Virtualization.framework starts the VM in-process, so there is no
            // shell command to show. Emit the complete native launch configuration
            // through the same callback used by the VM Log page.
            report("LAUNCH: Apple Virtualization.framework (native API; no shell command)")
            report("LAUNCH: VM name: \(options.name)")
            report("LAUNCH: CPUs: \(options.cpus)")
            report("LAUNCH: Memory: \(options.memory) bytes")
            if let iso = options.iso {
                report("LAUNCH: Boot: EFI from ISO \(iso)")
                report("LAUNCH: EFI variable store: \(options.directory)/efi-vars")
                report("LAUNCH: Storage: USB mass storage, read-only, \(iso)")
            } else {
                report("LAUNCH: Boot: Linux kernel \(options.kernel!)")
                report("LAUNCH: Kernel command line: root=/dev/vda rw rootwait console=hvc0")
                report("LAUNCH: Initramfs: \(options.initramfs ?? "(none)")")
                report("LAUNCH: Disk source: \(options.disk!)")
                report("LAUNCH: Writable VM disk: \(options.directory)/root.img (virtio block, read-write)")
            }
            report("LAUNCH: Network: virtio NAT")
            report("LAUNCH: Entropy: virtio RNG")
            report("LAUNCH: Graphics: virtio, 1280x800")
            report("LAUNCH: Input: USB keyboard and screen-coordinate pointing device")
            report("LAUNCH: Serial: virtio console")
            vm = VZVirtualMachine(configuration: config)
            vm!.delegate = self
            let display = EmbeddedVMView()
            display.onFocus = { [weak self] in self?.report("INPUT:captured") }
            display.virtualMachine = vm
            display.capturesSystemKeys = true
            view = display
            vm!.start { result in
                switch result {
                case .success:
                    if self.canceled { self.stop() }
                    else { self.report("STATE:running") }
                case .failure(let error): self.fail(error)
                }
            }
        } catch { fail(error) }
    }
    func place(_ windowPointer: UnsafeMutableRawPointer?, _ x: Double, _ y: Double,
               _ width: Double, _ height: Double) {
        guard let view = view else { return }
        guard let pointer = windowPointer, width > 0, height > 0 else { detach(); return }
        let window = Unmanaged<NSWindow>.fromOpaque(pointer).takeUnretainedValue()
        guard let parent = window.contentView else { detach(); return }
        let frame = NSRect(x: x, y: parent.isFlipped ? y : parent.bounds.height - y - height,
                           width: width, height: height)
        if view.superview !== parent {
            detach()
            parent.addSubview(view, positioned: .above, relativeTo: nil)
        }
        if view.frame != frame { view.frame = frame }
    }
    func detach() {
        guard let view = view else { return }
        focus(false)
        if let responder = view.window?.firstResponder as? NSView, responder.isDescendant(of: view) {
            view.window?.makeFirstResponder(view.superview)
        }
        view.removeFromSuperview()
    }
    func focus(_ active: Bool) {
        inputCaptured = active
        if active, let view = view, let window = view.window {
            let accepted = window.makeFirstResponder(view)
            report("INPUT: capture requested; first responder \(window.firstResponder === view ? "is" : "is not") AVF view")
            if !accepted || window.firstResponder !== view {
                report("INPUT: AVF view did not retain keyboard focus")
            }
            if keyboardMonitor == nil {
                keyboardMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .keyUp, .flagsChanged]) {
                    [weak self] event in
                    guard let self = self, self.inputCaptured else { return event }
                    guard let view = self.view, let window = view.window else { return event }
                    guard event.windowNumber == window.windowNumber else {
                        if event.type == .keyDown && !self.didReportWindowMismatch {
                            self.didReportWindowMismatch = true
                            self.report("INPUT: key event belongs to window \(event.windowNumber); AVF view window is \(window.windowNumber)")
                        }
                        return event
                    }
                    switch event.type {
                    case .keyDown:
                        if !self.didReportKeyboardEvent {
                            self.didReportKeyboardEvent = true
                            self.report("INPUT: first key event reached AVF capture (key content omitted)")
                        }
                        view.keyDown(with: event)
                    case .keyUp: view.keyUp(with: event)
                    case .flagsChanged: view.flagsChanged(with: event)
                    default: return event
                    }
                    return nil
                }
            }
        } else {
            if let keyboardMonitor = keyboardMonitor {
                NSEvent.removeMonitor(keyboardMonitor)
                self.keyboardMonitor = nil
            }
            if let view = view, let window = view.window {
                window.makeFirstResponder(view.superview)
            }
        }
    }
    func key(_ code: UInt16, _ pressed: Bool, _ isModifier: Bool, _ flags: UInt64,
             _ characters: String, _ unmodifiedCharacters: String) {
        guard inputCaptured, let view = view, let window = view.window else { return }
        if window.firstResponder !== view { _ = window.makeFirstResponder(view) }
        if let original = NSApp.currentEvent, original.windowNumber == window.windowNumber,
           (original.type == .keyDown || original.type == .keyUp || original.type == .flagsChanged) {
            switch original.type {
            case .keyDown: view.keyDown(with: original)
            case .keyUp: view.keyUp(with: original)
            case .flagsChanged: view.flagsChanged(with: original)
            default: break
            }
            return
        }

        let source = CGEventSource(stateID: .hidSystemState)
        guard let cgEvent = CGEvent(keyboardEventSource: source, virtualKey: code,
                                    keyDown: pressed) else { return }
        cgEvent.type = isModifier ? .flagsChanged : (pressed ? .keyDown : .keyUp)
        cgEvent.flags = CGEventFlags(rawValue: flags)
        let unicode = Array(characters.utf16)
        if !unicode.isEmpty {
            cgEvent.keyboardSetUnicodeString(stringLength: unicode.count, unicodeString: unicode)
        }
        guard let event = NSEvent(cgEvent: cgEvent) else { return }
        switch event.type {
        case .keyDown: view.keyDown(with: event)
        case .keyUp: view.keyUp(with: event)
        case .flagsChanged: view.flagsChanged(with: event)
        default: break
        }
    }
    func command(_ line: String) {
        do {
            switch line {
            case "poweroff": try vm?.requestStop()
            case "stop": stop()
            default:
                if line.hasPrefix("serial:"), let data = Data(base64Encoded: String(line.dropFirst(7))) {
                    try serialInput?.write(contentsOf: data)
                }
            }
        } catch { report("Control failed: \(error.localizedDescription)") }
    }
    func stop() {
        canceled = true
        detach()
        guard let vm = vm else { return } // Copy/configuration completion will finish the run.
        if vm.canStop {
            vm.stop { error in
                if let error = error { self.fail(error) }
                else { self.finish() }
            }
        } else if vm.state == .stopped || vm.state == .error { finish() }
        // A VM still starting is stopped by its start completion handler.
    }
    func finish() {
        guard !finished else { return }
        finished = true
        detach()
        serialOutput?.fileHandleForReading.readabilityHandler = nil
        view?.virtualMachine = nil
        view = nil
        vm?.delegate = nil
        vm = nil
        report("STATE:stopped")
    }
    func guestDidStop(_ virtualMachine: VZVirtualMachine) { finish() }
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) { fail(error) }
    func fail(_ error: Error) {
        report("ERROR:" + error.localizedDescription)
        finish()
    }
}

// Small C ABI consumed by ctypes. All VM/view entry points run on GTK's main thread.
@_cdecl("cl_vm_probe")
public func probe() -> UnsafeMutablePointer<CChar>? {
    if #available(macOS 13.0, *) {
        guard let task = SecTaskCreateFromSelf(nil),
              let entitlement = SecTaskCopyValueForEntitlement(task, "com.apple.security.virtualization" as CFString, nil),
              CFEqual(entitlement, kCFBooleanTrue) else {
            return strdup("{\"error\":\"The app needs the virtualization entitlement. Start development builds with ./run.sh.\"}")
        }
        if !VZVirtualMachine.isSupported { return strdup("{\"error\":\"Virtualization is not supported on this Mac\"}") }
        return strdup("{\"min_cpus\":\(VZVirtualMachineConfiguration.minimumAllowedCPUCount),\"max_cpus\":\(VZVirtualMachineConfiguration.maximumAllowedCPUCount),\"min_memory\":\(VZVirtualMachineConfiguration.minimumAllowedMemorySize),\"max_memory\":\(VZVirtualMachineConfiguration.maximumAllowedMemorySize)}")
    }
    return strdup("{\"error\":\"Apple Virtualization requires macOS 13 or later\"}")
}
@_cdecl("cl_vm_free")
public func freeString(_ string: UnsafeMutableRawPointer?) { free(string) }

@_cdecl("cl_vm_create")
public func create(_ json: UnsafePointer<CChar>, _ callback: EventCallback,
                   _ context: UnsafeMutableRawPointer?) -> UnsafeMutableRawPointer? {
    if #available(macOS 13.0, *) {
        let runner = Runner(callback: callback, context: context)
        let pointer = Unmanaged.passRetained(runner).toOpaque()
        runner.begin(String(cString: json))
        return pointer
    }
    return nil
}
@_cdecl("cl_vm_command")
public func command(_ pointer: UnsafeMutableRawPointer, _ command: UnsafePointer<CChar>) {
    if #available(macOS 13.0, *) { Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().command(String(cString: command)) }
}
@_cdecl("cl_vm_place")
public func place(_ pointer: UnsafeMutableRawPointer, _ window: UnsafeMutableRawPointer?,
                  _ x: Double, _ y: Double, _ width: Double, _ height: Double) {
    if #available(macOS 13.0, *) { Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().place(window, x, y, width, height) }
}
@_cdecl("cl_vm_release")
public func release(_ pointer: UnsafeMutableRawPointer) {
    if #available(macOS 13.0, *) { Unmanaged<Runner>.fromOpaque(pointer).release() }
}
@_cdecl("cl_vm_focus")
public func focus(_ pointer: UnsafeMutableRawPointer, _ active: Bool) {
    if #available(macOS 13.0, *) { Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().focus(active) }
}
@_cdecl("cl_vm_key")
public func key(_ pointer: UnsafeMutableRawPointer, _ code: UInt16, _ pressed: Bool, _ isModifier: Bool,
                _ flags: UInt64, _ characters: UnsafePointer<CChar>, _ unmodified: UnsafePointer<CChar>) {
    if #available(macOS 13.0, *) {
        Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().key(
            code, pressed, isModifier, flags, String(cString: characters), String(cString: unmodified))
    }
}
// CoreWLAN hides SSIDs until the host app has Location authorization.
import CoreLocation
import CoreWLAN

private final class WiFiAccess: NSObject, CLLocationManagerDelegate {
    static let shared = WiFiAccess()
    private let manager = CLLocationManager()
    private var requested = false

    override init() {
        super.init()
        manager.delegate = self
    }

    func result(_ request: Bool) -> String {
        let status = manager.authorizationStatus
        var state = "unavailable"
        var ssid: String? = nil
        if status == .authorizedAlways {
            state = "authorized"
            ssid = CWWiFiClient.shared().interface()?.ssid()
        } else if status == .notDetermined && CLLocationManager.locationServicesEnabled() {
            state = "pending"
            if request && !requested {
                requested = true
                manager.requestWhenInUseAuthorization()
            }
        } else {
            state = "denied"
        }
        let data = try! JSONSerialization.data(withJSONObject: ["status": state, "ssid": ssid ?? ""])
        return String(decoding: data, as: UTF8.self)
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {}
}

@_cdecl("cl_wifi_status")
public func wifiStatus(_ request: Bool) -> UnsafeMutablePointer<CChar>? {
    var result = "{}"
    if Thread.isMainThread {
        result = WiFiAccess.shared.result(request)
    } else {
        DispatchQueue.main.sync { result = WiFiAccess.shared.result(request) }
    }
    return strdup(result)
}
