import Cocoa
import WebKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var service: Process?
    private var startedService = false
    private var port: Int = 0
    private var token: String = ""
    private var injected = false
    private var logHandle: FileHandle?

    private var supportDir: URL!
    private var instanceHome: URL!
    private var discoveryURL: URL!

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        makeWindow()
        supportDir = FileManager.default.urls(for: .applicationSupportDirectory,
                                               in: .userDomainMask).first!
            .appendingPathComponent("Hengjian", isDirectory: true)
        instanceHome = supportDir.appendingPathComponent("instance", isDirectory: true)
        discoveryURL = instanceHome.appendingPathComponent("discovery.json")
        try? FileManager.default.createDirectory(at: instanceHome,
                                                 withIntermediateDirectories: true)
        startOrAttach()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        if startedService, let process = service, process.isRunning {
            process.terminate()
        }
        try? logHandle?.close()
    }

    private func buildMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 衡鉴·点将台",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 衡鉴·点将台",
                        action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = menu
    }

    private func makeWindow() {
        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "衡鉴·点将台"
        window.minSize = NSSize(width: 900, height: 640)
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - service lifecycle

    private func startOrAttach() {
        if let found = readDiscovery(), healthy(port: found.0, token: found.1) {
            attach(port: found.0, token: found.1)
            return
        }
        try? FileManager.default.removeItem(at: discoveryURL)
        startService()
    }

    private func readDiscovery() -> (Int, String)? {
        guard let data = try? Data(contentsOf: discoveryURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = object["port"] as? Int,
              let token = object["token"] as? String,
              port > 0, !token.isEmpty else { return nil }
        return (port, token)
    }

    private func healthy(port: Int, token: String) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        var healthy = false
        let semaphore = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, response, _ in
            healthy = (response as? HTTPURLResponse)?.statusCode == 200
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 3)
        return healthy
    }

    private func pythonPath() -> String? {
        for candidate in ["/opt/homebrew/bin/python3.12",
                           "/opt/homebrew/bin/python3.13",
                           "/opt/homebrew/bin/python3.11",
                           "/opt/homebrew/bin/python3.10",
                           "/usr/local/bin/python3.12",
                           "/usr/local/bin/python3",
                           "/opt/homebrew/bin/python3",
                           "/usr/bin/python3"] {
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }

    private func startService() {
        guard let resourceURL = Bundle.main.resourceURL else {
            fail("应用资源缺失。")
            return
        }
        let runtime = resourceURL.appendingPathComponent("runtime", isDirectory: true)
        let bundled = resourceURL.appendingPathComponent("bin/fusion-server")
        let hasBundled = FileManager.default.isExecutableFile(atPath: bundled.path)
        guard hasBundled || FileManager.default.fileExists(atPath: runtime.path) else {
            fail("运行时资源缺失。")
            return
        }
        let python = hasBundled ? nil : pythonPath()
        if !hasBundled && python == nil {
            fail("未找到 Python 3。请安装 Python 3.10+，或使用内置运行时版本。")
            return
        }

        let process = Process()
        if hasBundled {
            process.executableURL = bundled
            process.currentDirectoryURL = resourceURL
            process.arguments = [
                "--home", instanceHome.path,
                "--port", "0",
                "--discovery-file", discoveryURL.path,
                "--sync",
            ]
        } else {
            process.executableURL = URL(fileURLWithPath: python!)
            process.currentDirectoryURL = runtime
            process.arguments = [
                "-m", "fusion.server",
                "--home", instanceHome.path,
                "--port", "0",
                "--discovery-file", discoveryURL.path,
                "--sync",
            ]
        }
        var environment = ProcessInfo.processInfo.environment
        if !hasBundled {
            environment["PYTHONPATH"] = runtime.path + ":" + runtime.appendingPathComponent("scripts").path
            environment["PYTHONUNBUFFERED"] = "1"
            environment["PYTHONUTF8"] = "1"
        }
        environment["DUTANG_PUBLIC_REGISTRATION"] = "1"
        environment["LANG"] = environment["LANG"] ?? "en_US.UTF-8"
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (environment["PATH"] ?? "")
        process.environment = environment

        let logURL = supportDir.appendingPathComponent("service.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        logHandle = try? FileHandle(forWritingTo: logURL)
        _ = try? logHandle?.seekToEnd()
        process.standardOutput = logHandle
        process.standardError = logHandle

        note(hasBundled ? "bundled=\(bundled.path)" : "python=\(python ?? "none") runtime=\(runtime.path)")
        note("home=\(instanceHome.path) discovery=\(discoveryURL.path)")
        do {
            try process.run()
            note("spawned pid=\(process.processIdentifier)")
        } catch {
            note("spawn failed: \(error.localizedDescription)")
            fail("启动本地服务失败：\(error.localizedDescription)")
            return
        }
        process.terminationHandler = { [weak self] proc in
            self?.note("service exited status=\(proc.terminationStatus)")
        }
        service = process
        startedService = true
        pollForService(attempts: 80)
    }

    private func pollForService(attempts: Int) {
        if let found = readDiscovery(), healthy(port: found.0, token: found.1) {
            attach(port: found.0, token: found.1)
            return
        }
        if attempts <= 0 {
            fail("本地服务启动超时。请查看 ~/Library/Logs 或应用支持目录下的 service.log。")
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            self?.pollForService(attempts: attempts - 1)
        }
    }

    private func attach(port: Int, token: String) {
        self.port = port
        self.token = token
        guard let url = URL(string: "http://127.0.0.1:\(port)/") else { return }
        note("attach \(url.absoluteString)")
        webView.load(URLRequest(url: url))
    }

    private func note(_ message: String) {
        let url = supportDir.appendingPathComponent("app.log")
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: url) {
            _ = try? handle.seekToEnd()
            handle.write((message + "\n").data(using: .utf8)!)
            try? handle.close()
        }
    }

    private func fail(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "衡鉴·点将台无法启动"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "退出")
        alert.runModal()
        NSApp.terminate(nil)
    }

    // MARK: - web view

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        note("didFail \(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        note("didFailProvisional \(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        note("didFinish \(webView.url?.absoluteString ?? "?")")
        guard !injected, !token.isEmpty else { return }
        injected = true
        let escaped = token
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = """
        (function() {
          var input = document.getElementById('token');
          if (input) {
            input.value = "\(escaped)";
            input.dispatchEvent(new Event('input', { bubbles: true }));
          }
          var refresh = document.getElementById('refresh');
          if (refresh) { refresh.click(); }
        })();
        """
        webView.evaluateJavaScript(script, completionHandler: nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
