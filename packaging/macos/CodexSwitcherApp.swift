// Codex 多模型切换器 —— 原生 macOS 窗口应用
//
// 为什么不用浏览器：这样才是真正的 App —— 自己的窗口、自己的 Dock 图标、
// 不占浏览器标签页，也不受浏览器扩展/隐私设置影响。
//
// 它做的事：
//   1. 启动时先看有没有已经在跑的图形界面（读 gui.json），有就直接用
//   2. 没有就调用 launch.sh 把协议桥和图形界面拉起来，等端口就绪
//   3. 用 WKWebView 把界面装进原生窗口里显示
//   4. 退出时默认保留后台服务（第三方平台的协议桥还要用），菜单里可以手动停

import Cocoa
import WebKit

let appTitle = "Codex 多模型切换器"

func homeDirectory() -> String {
    return ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
}

func stateDirectory() -> URL {
    if let custom = ProcessInfo.processInfo.environment["CODEX_HOME"], !custom.isEmpty {
        return URL(fileURLWithPath: custom).appendingPathComponent("model-switcher")
    }
    return URL(fileURLWithPath: homeDirectory()).appendingPathComponent(".codex/model-switcher")
}

func runtimeDirectory() -> URL {
    // 1) 优先用 App 自带的那份（这样单独下载 .app 也能跑，不需要先 clone 仓库）
    if let resources = Bundle.main.resourceURL {
        let bundled = resources.appendingPathComponent("runtime")
        if FileManager.default.fileExists(atPath: bundled.appendingPathComponent("codex_switcher").path) {
            return bundled
        }
    }
    // 2) 其次是 install.sh 安装的位置。
    //    ~/Documents 属于受保护目录，从访达启动的 App 无权执行那里的脚本。
    if let custom = ProcessInfo.processInfo.environment["CODEX_SWITCHER_HOME"], !custom.isEmpty {
        return URL(fileURLWithPath: custom)
    }
    return URL(fileURLWithPath: homeDirectory()).appendingPathComponent(".local/share/codex-switcher")
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusField: NSTextField!
    private var overlay: NSView!
    private var pollTimer: Timer?
    private var startedAt = Date()
    private var triedExisting = false
    private var loadedURL: URL?

    // ------------------------------------------------------------ 生命周期

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        startOrReuse()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    // ---------------------------------------------------------------- 界面

    private func buildWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1180, height: 840)
        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered,
                          defer: false)
        window.title = appTitle
        window.minSize = NSSize(width: 880, height: 600)
        window.center()
        window.titlebarAppearsTransparent = false

        let container = NSView(frame: frame)
        container.autoresizingMask = [.width, .height]

        let configuration = WKWebViewConfiguration()
        configuration.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: frame, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        container.addSubview(webView)

        // 启动阶段的提示层（服务起来之后移除）
        overlay = NSView(frame: frame)
        overlay.autoresizingMask = [.width, .height]
        overlay.wantsLayer = true
        overlay.layer?.backgroundColor = NSColor(calibratedWhite: 0.06, alpha: 1).cgColor

        statusField = NSTextField(labelWithString: "正在启动后台服务…")
        statusField.font = NSFont.systemFont(ofSize: 15, weight: .medium)
        statusField.textColor = NSColor(calibratedWhite: 0.85, alpha: 1)
        statusField.alignment = .center
        statusField.frame = NSRect(x: 40, y: frame.height / 2 - 40, width: frame.width - 80, height: 24)
        statusField.autoresizingMask = [.width, .minYMargin, .maxYMargin]
        overlay.addSubview(statusField)
        container.addSubview(overlay)

        window.contentView = container
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 " + appTitle,
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "隐藏 " + appTitle,
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "退出 " + appTitle,
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        let viewMenuItem = NSMenuItem()
        let viewMenu = NSMenu(title: "视图")
        viewMenu.addItem(withTitle: "重新载入",
                         action: #selector(reloadPage), keyEquivalent: "r")
        viewMenu.addItem(withTitle: "在浏览器中打开",
                         action: #selector(openInBrowser), keyEquivalent: "b")
        viewMenuItem.submenu = viewMenu
        mainMenu.addItem(viewMenuItem)

        let serviceMenuItem = NSMenuItem()
        let serviceMenu = NSMenu(title: "服务")
        serviceMenu.addItem(withTitle: "重启后台服务",
                            action: #selector(restartServices), keyEquivalent: "")
        serviceMenu.addItem(withTitle: "停止后台服务",
                            action: #selector(stopServices), keyEquivalent: "")
        serviceMenuItem.submenu = serviceMenu
        mainMenu.addItem(serviceMenuItem)

        NSApp.mainMenu = mainMenu
    }

    // ------------------------------------------------------------ 启动流程

    private func startOrReuse() {
        startedAt = Date()
        triedExisting = true
        if let url = urlFromStateFile() {
            load(url)
            return
        }
        launchServices()
    }

    private func urlFromStateFile() -> URL? {
        let file = stateDirectory().appendingPathComponent("gui.json")
        guard let data = try? Data(contentsOf: file),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let port = object["port"] as? Int,
              let token = object["token"] as? String,
              !token.isEmpty
        else { return nil }
        return URL(string: "http://127.0.0.1:\(port)/?t=\(token)")
    }

    private func launchServices() {
        let launcher = runtimeDirectory().appendingPathComponent("packaging/macos/launch.sh")
        guard FileManager.default.isExecutableFile(atPath: launcher.path) else {
            showFailure("找不到启动脚本：\n\(launcher.path)\n\n请先在仓库目录里执行 bash install.sh")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        // --no-browser：界面装在本窗口里，不要再弹一个浏览器
        process.arguments = ["-c", "nohup \"\(launcher.path)\" --no-browser >/dev/null 2>&1 &"]
        do {
            try process.run()
        } catch {
            showFailure("启动失败：\(error.localizedDescription)")
            return
        }
        pollForService()
    }

    private func pollForService() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            if let url = self.urlFromStateFile() {
                timer.invalidate()
                self.load(url)
                return
            }
            let waited = Date().timeIntervalSince(self.startedAt)
            if waited > 25 {
                timer.invalidate()
                self.showFailure("后台服务启动超时（25 秒）。\n\n"
                                 + "可以查看日志了解原因：\n"
                                 + stateDirectory().appendingPathComponent("launch.log").path)
            } else if waited > 3 {
                self.statusField.stringValue = "正在启动后台服务…（已等待 \(Int(waited)) 秒）"
            }
        }
    }

    private func load(_ url: URL) {
        loadedURL = url
        statusField.stringValue = "正在载入界面…"
        webView.load(URLRequest(url: url))
    }

    private func showFailure(_ message: String) {
        statusField.stringValue = message
        statusField.maximumNumberOfLines = 0
    }

    // -------------------------------------------------------- WKNavigation

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        overlay?.removeFromSuperview()
        overlay = nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handleLoadFailure(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        handleLoadFailure(error)
    }

    private func handleLoadFailure(_ error: Error) {
        // 记录的端口可能已经失效：改成自己把服务拉起来再试一次
        if triedExisting {
            triedExisting = false
            statusField.stringValue = "界面没有响应，正在重新启动后台服务…"
            launchServices()
            return
        }
        showFailure("界面载入失败：\(error.localizedDescription)")
    }

    // ---------------------------------------------------------------- 菜单

    @objc private func reloadPage() {
        if let url = loadedURL {
            webView.load(URLRequest(url: url))
        } else {
            startOrReuse()
        }
    }

    @objc private func openInBrowser() {
        if let url = loadedURL {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func restartServices() {
        performStop(silent: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.startOrReuse()
        }
    }

    @objc private func stopServices() {
        performStop(silent: false)
    }

    private func performStop(silent: Bool) {
        let gui = stateDirectory().appendingPathComponent("gui.json")
        let bridge = stateDirectory().appendingPathComponent("bridge.pid")
        var stopped: [String] = []
        for file in [gui, bridge] {
            guard let data = try? Data(contentsOf: file),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let pid = object["pid"] as? Int, pid > 1
            else { continue }
            if kill(pid_t(pid), SIGTERM) == 0 {
                stopped.append(String(pid))
            }
            try? FileManager.default.removeItem(at: file)
        }
        pollTimer?.invalidate()
        if !silent {
            let alert = NSAlert()
            alert.messageText = stopped.isEmpty ? "没有正在运行的后台服务" : "已停止后台服务"
            alert.informativeText = stopped.isEmpty
                ? "图形界面和协议桥都没有在运行。"
                : "已结束进程：" + stopped.joined(separator: "、")
            alert.runModal()
        }
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
