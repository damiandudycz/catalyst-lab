import AppKit
import Virtualization
import Security

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
    var cancelled = false
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
                        try FileManager.default.copyItem(at: URL(fileURLWithPath: source), to:
                            URL(fileURLWithPath: options.directory).appendingPathComponent("root.img"))
                    }
                    DispatchQueue.main.async {
                        if self.cancelled { self.finish(); return }
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
                    if self.cancelled { self.stop() }
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
        if let responder = view.window?.firstResponder as? NSView, responder.isDescendant(of: view) {
            view.window?.makeFirstResponder(view.superview)
        }
        view.removeFromSuperview()
    }
    func focus(_ active: Bool) {
        guard let view = view, let window = view.window else { return }
        window.makeFirstResponder(active ? view : view.superview)
    }
    func key(_ code: UInt16, _ pressed: Bool, _ flags: UInt64, _ text: String) {
        guard let view = view, let window = view.window else { return }
        let modifiers: Set<UInt16> = [54, 55, 56, 57, 58, 59, 60, 61, 62]
        let type: NSEvent.EventType = modifiers.contains(code) ? .flagsChanged : (pressed ? .keyDown : .keyUp)
        guard let event = NSEvent.keyEvent(with: type, location: .zero,
            modifierFlags: NSEvent.ModifierFlags(rawValue: UInt(flags)), timestamp: ProcessInfo.processInfo.systemUptime,
            windowNumber: window.windowNumber, context: nil, characters: text,
            charactersIgnoringModifiers: text, isARepeat: false, keyCode: code) else { return }
        switch type {
        case .flagsChanged: view.flagsChanged(with: event)
        case .keyDown: view.keyDown(with: event)
        default: view.keyUp(with: event)
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
        cancelled = true
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
public func key(_ pointer: UnsafeMutableRawPointer, _ code: UInt16, _ pressed: Bool,
                _ flags: UInt64, _ text: UnsafePointer<CChar>) {
    if #available(macOS 13.0, *) {
        Unmanaged<Runner>.fromOpaque(pointer).takeUnretainedValue().key(code, pressed, flags, String(cString: text))
    }
}
