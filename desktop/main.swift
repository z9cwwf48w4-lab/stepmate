// StepMate.app — 原生壳：WKWebView + 托管 Python 后端
// 编译: swiftc -O -swift-version 5 -target arm64-apple-macos13.0 -sdk $SDK \
//         -framework AppKit -framework WebKit -o StepMate.app/Contents/MacOS/StepMate main.swift
import AppKit
import WebKit

let fm = FileManager.default
let supportDir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
let dataDir = supportDir.appendingPathComponent("StepMate")
try? fm.createDirectory(at: dataDir, withIntermediateDirectories: true)
let logFile = dataDir.appendingPathComponent("app.log")

// ---------- 日志（统一脱敏出口，权限 0600） ----------
enum Log {
    static var secrets: [String] = []
    static func mask(_ s: String) -> String {
        secrets.reduce(s) { $0.replacingOccurrences(of: $1, with: "<token>") }
    }
    static func write(_ s: String) {
        let line = "[\(Date().description)] \(mask(s))\n"
        let path = logFile.path
        if let h = try? FileHandle(forWritingTo: logFile) {
            h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
        } else {
            try? line.data(using: .utf8)?.write(to: logFile)
            try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
        }
    }
}

// ---------- 资源：项目根目录 + python 解释器 ----------
let resPath = Bundle.main.resourcePath ?? ""
let rootPath = ((try? String(contentsOfFile: resPath + "/stepbuddy-root.txt", encoding: .utf8)) ?? "")
    .trimmingCharacters(in: .whitespacesAndNewlines)
let pythonPath = ((try? String(contentsOfFile: resPath + "/stepbuddy-python.txt", encoding: .utf8)) ?? "/usr/bin/python3")
    .trimmingCharacters(in: .whitespacesAndNewlines)

var ownsProcess = false
var backendPort = 0
let backendProc = Process()

func healthzOk(port: Int) -> Bool {
    guard let url = URL(string: "http://127.0.0.1:\(port)/healthz") else { return false }
    var req = URLRequest(url: url); req.timeoutInterval = 2
    var ok = false
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: req) { data, resp, _ in
        if let h = resp as? HTTPURLResponse, h.statusCode == 200,
           let body = String(data: data ?? Data(), encoding: .utf8),
           body.contains("stepmate") { ok = true }
        sem.signal()
    }.resume()
    sem.wait()
    return ok
}

func startBackend() -> Bool {
    // 1) 复用已有同款服务（校验 /healthz 身份，退出时不误杀）
    if healthzOk(port: 8787) {
        backendPort = 8787; ownsProcess = false
        Log.write("复用 8787 已有服务")
        return true
    }
    guard !rootPath.isEmpty, fm.fileExists(atPath: pythonPath) else {
        Log.write("资源缺失 root=\(rootPath) python=\(pythonPath)")
        return false
    }
    // 2) 动态端口启动，避免探测竞态
    let p = Process()
    p.executableURL = URL(fileURLWithPath: pythonPath)
    p.arguments = ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "0"]
    p.currentDirectoryURL = URL(fileURLWithPath: rootPath)
    p.environment = [
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": NSHomeDirectory(),
        "TMPDIR": NSTemporaryDirectory(),
        "LANG": "en_US.UTF-8",
        "STEPBUDDY_PARENT_PID": String(getpid()),
        "STEPBUDDY_LOG": logFile.path,
    ]
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { Log.write("启动失败: \(error)"); return false }
    ownsProcess = true

    // 3) 从启动横幅解析真实端口
    let fh = pipe.fileHandleForReading
    var buf = Data()
    let deadline = Date().addingTimeInterval(30)
    while Date() < deadline && backendPort == 0 {
        let d = fh.availableData
        if d.isEmpty { usleep(100_000); continue }
        buf.append(d)
        guard let s = String(data: buf, encoding: .utf8) else { continue }
        for line in s.components(separatedBy: "\n") {
            if let r = line.range(of: "http://127.0.0.1:") {
                let digits = line[r.upperBound...].prefix { $0.isNumber }
                if let n = Int(digits), n > 0 { backendPort = n; break }
            }
        }
        if backendPort == 0, p.isRunning == false { Log.write("后端进程提前退出"); return false }
    }
    guard backendPort > 0 else { Log.write("30s 内未解析到端口"); return false }

    // 4) 等健康检查通过
    for _ in 0..<20 {
        if healthzOk(port: backendPort) { Log.write("后端就绪 port=\(backendPort)"); return true }
        usleep(500_000)
    }
    Log.write("后端端口 \(backendPort) 健康检查未通过")
    return false
}

func stopBackendIfOwned() {
    guard ownsProcess, backendProc.isRunning else { return }
    backendProc.terminate()
    Log.write("已停止自启的后端进程")
}

// ---------- 窗口与 WebView ----------
let paperColor = NSColor(red: 0.968, green: 0.949, blue: 0.906, alpha: 1) // #f7f2e7

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webview: WKWebView!

    func applicationDidFinishLaunching(_ n: Notification) {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = WKWebsiteDataStore.default()
        webview = WKWebView(frame: .zero, configuration: cfg)
        webview.navigationDelegate = self
        webview.uiDelegate = self
        webview.underPageBackgroundColor = paperColor
        webview.setValue(false, forKey: "drawsBackground")

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "StepMate 步步伴"
        window.minSize = NSSize(width: 760, height: 560)
        window.contentView = webview
        window.setFrameAutosaveName("StepMateMain")
        window.center()
        window.makeKeyAndOrderFront(nil)

        buildMenu()
        loadLaunchPage()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let ok = self != nil && startBackend()
            DispatchQueue.main.async {
                guard let self = self else { return }
                if ok {
                    self.webview.load(URLRequest(url: URL(string: "http://127.0.0.1:\(backendPort)/")!))
                } else {
                    self.loadErrorPage()
                }
            }
        }
    }

    func buildMenu() {
        let main = NSMenu()
        // 应用菜单
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "退出 StepMate", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appItem.submenu = appMenu
        // 编辑菜单（Cmd+C/V 必需）
        let editItem = NSMenuItem(); main.addItem(editItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(NSMenuItem(title: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = editMenu
        // 视图菜单
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let viewMenu = NSMenu(title: "视图")
        viewMenu.addItem(NSMenuItem(title: "重新加载", action: #selector(WKWebView.reload(_:)), keyEquivalent: "r"))
        viewItem.submenu = viewMenu
        NSApp.mainMenu = main
    }

    func loadLaunchPage() {
        webview.loadHTMLString("""
        <html><body style="display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
        background:#f7f2e7;font-family:-apple-system,'PingFang SC';color:#3c352a;">
        <div style="text-align:center"><div style="font-size:40px">🌱</div>
        <div style="margin-top:12px;font-size:15px">正在启动本地 AI 后端…</div>
        <div style="margin-top:6px;font-size:12px;color:#8a7f6a">首次冷启动约需数秒</div></div></body></html>
        """,
        baseURL: nil)
    }

    func loadErrorPage() {
        webview.loadHTMLString("""
        <html><body style="display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
        background:#f7f2e7;font-family:-apple-system,'PingFang SC';color:#3c352a;">
        <div style="text-align:center;max-width:420px"><div style="font-size:40px">⚠️</div>
        <div style="margin-top:12px;font-size:15px">本地后端启动失败</div>
        <div style="margin-top:6px;font-size:12px;color:#8a7f6a">日志: ~/Library/Application Support/StepMate/app.log</div>
        <a href="stepmate-retry:go" style="display:inline-block;margin-top:20px;padding:10px 28px;border-radius:999px;
        background:#d96f2b;color:#fff;text-decoration:none;font-size:14px">重试</a></div></body></html>
        """,
        baseURL: nil)
    }

    func retry() {
        backendPort = 0
        loadLaunchPage()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let ok = self != nil && startBackend()
            DispatchQueue.main.async {
                guard let self = self else { return }
                if ok { self.webview.load(URLRequest(url: URL(string: "http://127.0.0.1:\(backendPort)/")!)) }
                else { self.loadErrorPage() }
            }
        }
    }

    // 导航策略：自定义 scheme=重试；外部链接交系统浏览器；其余放行
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 preferences: WKWebpagePreferences, decisionHandler: @escaping (WKNavigationActionPolicy, WKWebpagePreferences) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel, preferences); return }
        if url.scheme == "stepmate-retry" {
            decisionHandler(.cancel, preferences)
            retry()
            return
        }
        if let host = url.host, !host.hasPrefix("127.0.0.1"), !host.hasPrefix("localhost") {
            decisionHandler(.cancel, preferences)
            NSWorkspace.shared.open(url)
            return
        }
        decisionHandler(.allow, preferences)
    }

    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
        Log.write("页面加载完成: \(w.url?.absoluteString ?? "-")")
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.runModal(); completionHandler()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ n: Notification) { stopBackendIfOwned() }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
