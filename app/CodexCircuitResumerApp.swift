import AppKit
import Foundation
import SwiftUI

private let appSupport = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/CodexCircuitResumer", isDirectory: true)
private let stateURL = appSupport.appendingPathComponent("state.json")
private let configURL = appSupport.appendingPathComponent("config.json")
private let providersURL = appSupport.appendingPathComponent("providers.json")
private let pauseURL = appSupport.appendingPathComponent("PAUSED")
private let watcherLogURL = appSupport.appendingPathComponent("logs/watcher.log")
private let launchdErrorURL = appSupport.appendingPathComponent("logs/launchd.err.log")
private let launchAgentURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/LaunchAgents/com.local.codex-circuit-resumer.plist")
private let ccSwitchLogURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".cc-switch/logs/cc-switch.log")
private let ccSwitchDBURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".cc-switch/cc-switch.db")
private let codexBinaryURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".local/bin/codex")
private let nativeCodexBinaryURL = URL(fileURLWithPath: "/Applications/ChatGPT.app/Contents/Resources/codex")

private struct CommandResult: Sendable {
    let code: Int32
    let output: String
}

private struct RefreshPayload: Sendable {
    let stateData: Data?
    let eventData: Data?
    let providerData: Data?
    let launchAgentLoaded: Bool
}

private enum Page: String, CaseIterable, Identifiable {
    case overview = "总览"
    case channels = "渠道"
    case events = "事件"
    case settings = "设置"
    case system = "系统"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .overview: return "gauge.with.dots.needle.50percent"
        case .channels: return "point.3.connected.trianglepath.dotted"
        case .events: return "waveform.path.ecg"
        case .settings: return "slider.horizontal.3"
        case .system: return "wrench.and.screwdriver"
        }
    }
}

private extension View {
    @ViewBuilder
    func withoutSystemFocusRing() -> some View {
        if #available(macOS 14.0, *) {
            focusEffectDisabled()
        } else {
            self
        }
    }
}

private struct ProviderSnapshot: Decodable {
    let generatedAt: Int
    let providers: [ProviderInfo]
    let error: String?
    let totalStandardCost24hUSD: Double?
    let totalStandardCostAllUSD: Double?
    let totalEstimatedActualCost24hUSD: Double?
    let totalEstimatedActualCostAllUSD: Double?
    let totalStandardCost24hCNY: Double?
    let totalStandardCostAllCNY: Double?
    let totalEstimatedActualCost24hCNY: Double?
    let totalEstimatedActualCostAllCNY: Double?
    let usdCnyRate: Double?
    let exchangeRateUpdatedAt: Int?
    let exchangeRateSource: String?
    let estimatedProviderCount: Int?
    let appSummaries: [String: ProviderAppSummary]?

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case providers
        case error
        case totalStandardCost24hUSD = "total_standard_cost_24h_usd"
        case totalStandardCostAllUSD = "total_standard_cost_all_usd"
        case totalEstimatedActualCost24hUSD = "total_estimated_actual_cost_24h_usd"
        case totalEstimatedActualCostAllUSD = "total_estimated_actual_cost_all_usd"
        case totalStandardCost24hCNY = "total_standard_cost_24h_cny"
        case totalStandardCostAllCNY = "total_standard_cost_all_cny"
        case totalEstimatedActualCost24hCNY = "total_estimated_actual_cost_24h_cny"
        case totalEstimatedActualCostAllCNY = "total_estimated_actual_cost_all_cny"
        case usdCnyRate = "usd_cny_rate"
        case exchangeRateUpdatedAt = "exchange_rate_updated_at"
        case exchangeRateSource = "exchange_rate_source"
        case estimatedProviderCount = "estimated_provider_count"
        case appSummaries = "app_summaries"
    }
}

private struct ProviderAppSummary: Decodable {
    let providerCount: Int
    let estimatedProviderCount: Int
    let totalStandardCost24hUSD: Double
    let totalStandardCostAllUSD: Double
    let totalEstimatedActualCost24hUSD: Double
    let totalEstimatedActualCostAllUSD: Double
    let totalStandardCost24hCNY: Double?
    let totalStandardCostAllCNY: Double?
    let totalEstimatedActualCost24hCNY: Double?
    let totalEstimatedActualCostAllCNY: Double?
    let ccSwitchStandardCostTodayUSD: Double?
    let ccSwitchStandardCostAllUSD: Double?
    let relayStandardCostTodayUSD: Double?
    let relayStandardCostAllUSD: Double?
    let verifiedRelayStandardCostTodayUSD: Double?
    let estimatedRelayActualCostTodayUSD: Double?
    let unpricedRelayStandardCostTodayUSD: Double?
    let ccSwitchStandardCostTodayCNY: Double?
    let ccSwitchStandardCostAllCNY: Double?
    let relayStandardCostTodayCNY: Double?
    let relayStandardCostAllCNY: Double?
    let verifiedRelayStandardCostTodayCNY: Double?
    let estimatedRelayActualCostTodayCNY: Double?
    let unpricedRelayStandardCostTodayCNY: Double?

    enum CodingKeys: String, CodingKey {
        case providerCount = "provider_count"
        case estimatedProviderCount = "estimated_provider_count"
        case totalStandardCost24hUSD = "total_standard_cost_24h_usd"
        case totalStandardCostAllUSD = "total_standard_cost_all_usd"
        case totalEstimatedActualCost24hUSD = "total_estimated_actual_cost_24h_usd"
        case totalEstimatedActualCostAllUSD = "total_estimated_actual_cost_all_usd"
        case totalStandardCost24hCNY = "total_standard_cost_24h_cny"
        case totalStandardCostAllCNY = "total_standard_cost_all_cny"
        case totalEstimatedActualCost24hCNY = "total_estimated_actual_cost_24h_cny"
        case totalEstimatedActualCostAllCNY = "total_estimated_actual_cost_all_cny"
        case ccSwitchStandardCostTodayUSD = "cc_switch_standard_cost_today_usd"
        case ccSwitchStandardCostAllUSD = "cc_switch_standard_cost_all_usd"
        case relayStandardCostTodayUSD = "relay_standard_cost_today_usd"
        case relayStandardCostAllUSD = "relay_standard_cost_all_usd"
        case verifiedRelayStandardCostTodayUSD = "verified_relay_standard_cost_today_usd"
        case estimatedRelayActualCostTodayUSD = "estimated_relay_actual_cost_today_usd"
        case unpricedRelayStandardCostTodayUSD = "unpriced_relay_standard_cost_today_usd"
        case ccSwitchStandardCostTodayCNY = "cc_switch_standard_cost_today_cny"
        case ccSwitchStandardCostAllCNY = "cc_switch_standard_cost_all_cny"
        case relayStandardCostTodayCNY = "relay_standard_cost_today_cny"
        case relayStandardCostAllCNY = "relay_standard_cost_all_cny"
        case verifiedRelayStandardCostTodayCNY = "verified_relay_standard_cost_today_cny"
        case estimatedRelayActualCostTodayCNY = "estimated_relay_actual_cost_today_cny"
        case unpricedRelayStandardCostTodayCNY = "unpriced_relay_standard_cost_today_cny"
    }
}

private struct ProviderInfo: Identifiable, Decodable, Hashable {
    let id: String
    let name: String
    let appType: String?
    let displayOrder: Int?
    let sortIndex: Int?
    let host: String
    let isCurrent: Bool
    let inFailoverQueue: Bool
    let multiplier: String
    let multiplierSource: String
    let remarkedMultiplier: String?
    let remarkedMultiplierSource: String?
    let actualMultiplier: Double?
    let actualMultiplierSource: String?
    let actualMultiplierState: String?
    let multiplierChanged: Bool?
    let usability: String
    let requests24h: Int
    let successRate24h: Double?
    let avgLatencyMs: Double?
    let lastModel: String?
    let errorClass: String
    let lastError: String
    let balance: String
    let balanceState: String
    let failureReason: String?
    let circuitState: String?
    let consecutiveFailures: Int?
    let temporarilyBypassed: Bool?
    let standardCost24hUSD: Double?
    let estimatedActualCost24hUSD: Double?
    let standardCost24hCNY: Double?
    let estimatedActualCost24hCNY: Double?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case appType = "app_type"
        case displayOrder = "display_order"
        case sortIndex = "sort_index"
        case host
        case isCurrent = "is_current"
        case inFailoverQueue = "in_failover_queue"
        case multiplier
        case multiplierSource = "multiplier_source"
        case remarkedMultiplier = "remarked_multiplier"
        case remarkedMultiplierSource = "remarked_multiplier_source"
        case actualMultiplier = "actual_multiplier"
        case actualMultiplierSource = "actual_multiplier_source"
        case actualMultiplierState = "actual_multiplier_state"
        case multiplierChanged = "multiplier_changed"
        case usability
        case requests24h = "requests_24h"
        case successRate24h = "success_rate_24h"
        case avgLatencyMs = "avg_latency_ms"
        case lastModel = "last_model"
        case errorClass = "error_class"
        case lastError = "last_error"
        case balance
        case balanceState = "balance_state"
        case failureReason = "failure_reason"
        case circuitState = "circuit_state"
        case consecutiveFailures = "consecutive_failures"
        case temporarilyBypassed = "temporarily_bypassed"
        case standardCost24hUSD = "standard_cost_24h_usd"
        case estimatedActualCost24hUSD = "estimated_actual_cost_24h_usd"
        case standardCost24hCNY = "standard_cost_24h_cny"
        case estimatedActualCost24hCNY = "estimated_actual_cost_24h_cny"
    }

    var statusTitle: String {
        switch usability {
        case "usable": return "可用"
        case "degraded": return "降级"
        case "unavailable": return "不可用"
        default: return "未测"
        }
    }

    var statusColor: Color {
        switch usability {
        case "usable": return Palette.success
        case "degraded": return Palette.warning
        case "unavailable": return Palette.danger
        default: return Palette.muted
        }
    }

    var routeTitle: String {
        if temporarilyBypassed == true { return "临时避让" }
        if isCurrent { return "当前" }
        if inFailoverQueue { return "候补" }
        return "未入队"
    }

    var orderTitle: String { "#\((displayOrder ?? sortIndex ?? 0) + 1)" }

    var remarkedMultiplierText: String { "x\(remarkedMultiplier ?? multiplier)" }

    var actualMultiplierText: String {
        guard let actualMultiplier else {
            return actualMultiplierState == "error" ? "查询失败" : "无法验证"
        }
        return String(format: "x%.3g", actualMultiplier)
    }

    var estimatedCostText: String {
        guard let estimatedActualCost24hCNY else { return "—" }
        return String(format: "¥%.4f", estimatedActualCost24hCNY)
    }

    var reasonText: String { failureReason ?? (errorClass.isEmpty ? "正常" : errorClass) }

    var successText: String {
        guard let successRate24h else { return "未测" }
        return String(format: "%.1f%% / %d", successRate24h, requests24h)
    }

    var latencyText: String {
        guard let avgLatencyMs else { return "—" }
        if avgLatencyMs >= 1000 {
            return String(format: "%.1fs", avgLatencyMs / 1000.0)
        }
        return String(format: "%.0fms", avgLatencyMs)
    }
}

private struct WatcherEvent: Identifiable, Hashable {
    let id = UUID()
    let time: String
    let level: String
    let message: String

    var color: Color {
        if message.contains("熔断") || level == "WARNING" { return Palette.warning }
        if level == "ERROR" || message.contains("无法") { return Palette.danger }
        if message.contains("恢复") || message.contains("续接") { return Palette.success }
        return Palette.muted
    }
}

private struct DiagnosticItem: Identifiable, Hashable {
    let id = UUID()
    let name: String
    let detail: String
    let healthy: Bool
}

private enum Palette {
    static let canvas = Color(red: 0.945, green: 0.953, blue: 0.961)
    static let surface = Color.white
    static let ink = Color(red: 0.075, green: 0.086, blue: 0.106)
    static let muted = Color(red: 0.37, green: 0.40, blue: 0.44)
    static let line = Color(red: 0.82, green: 0.84, blue: 0.87)
    static let accent = Color(red: 0.00, green: 0.43, blue: 0.50)
    static let success = Color(red: 0.07, green: 0.49, blue: 0.32)
    static let warning = Color(red: 0.75, green: 0.40, blue: 0.06)
    static let danger = Color(red: 0.70, green: 0.16, blue: 0.18)
}

@MainActor
private final class AppModel: ObservableObject {
    @Published var page: Page = .overview
    @Published var phase = "idle"
    @Published var lastEvent = "等待 CC Switch 熔断事件"
    @Published var lastEventAt: Int = 0
    @Published var resumeCount = 0
    @Published var queueCount = 0
    @Published var isPaused = false
    @Published var isLoaded = false
    @Published var isWorking = false
    @Published var events: [WatcherEvent] = []
    @Published var diagnostics: [DiagnosticItem] = []
    @Published var diagnosticSummary = "尚未运行自检"
    @Published var toast: String?
    @Published var errorMessage: String?
    @Published var inspectSummary = "尚未检测"
    @Published var providers: [ProviderInfo] = []
    @Published var selectedChannelApp = "codex"
    @Published var providerAppSummaries: [String: ProviderAppSummary] = [:]
    @Published var providersGeneratedAt = 0
    @Published var providersError: String?
    @Published var totalStandardCost24hUSD = 0.0
    @Published var totalStandardCostAllUSD = 0.0
    @Published var totalEstimatedActualCost24hUSD = 0.0
    @Published var totalEstimatedActualCostAllUSD = 0.0
    @Published var totalStandardCost24hCNY = 0.0
    @Published var totalStandardCostAllCNY = 0.0
    @Published var totalEstimatedActualCost24hCNY = 0.0
    @Published var totalEstimatedActualCostAllCNY = 0.0
    @Published var usdCnyRate = 7.2
    @Published var exchangeRateUpdatedAt = 0
    @Published var exchangeRateSource = "fallback"
    @Published var estimatedProviderCount = 0
    @Published var scheduledRetryCount = 0
    @Published var nextRetryAt = 0
    @Published var nextRetryTitle = ""
    @Published var nextRetryAttempt = 0
    @Published var nextRetryReason = ""
    @Published var inflightCount = 0
    @Published var blockedCount = 0
    @Published var lastSuccessAt = 0
    @Published var lastSuccessTitle = ""
    @Published var heartbeatAge: Int?
    @Published var daemonHealthy = false
    @Published var proxyRouteStatus = "unchecked"
    @Published var proxyRouteDetail = "尚未检查 Codex 是否经过 CC Switch"

    @Published var graceSeconds = 20
    @Published var idleSeconds = 240
    @Published var maxCandidates = 8
    @Published var notificationsEnabled = true
    @Published var modelRetryEnabled = true
    @Published var capacityReasoningFallbackEnabled = true
    @Published var capacityReasoningPromoteEnabled = true
    @Published var retryUntilSuccess = true
    @Published var retryBaseSeconds = 60
    @Published var retryMaxSeconds = 900
    @Published var retryJitterSeconds = 15
    @Published var retryMaxAttempts = 50
    @Published var retryWatchHours = 24
    @Published var resumePrompt = "继续。上次因中转站熔断或上游临时故障中断，请从中断处继续，不要重复已经完成的工作。"
    @Published var settingsDirty = false

    private var configObject: [String: Any] = [:]
    private var didLoadSettings = false
    private var didBegin = false
    private var refreshTask: Task<Void, Never>?
    private var refreshRequestTask: Task<Void, Never>?
    private var refreshGeneration = 0

    deinit {
        refreshTask?.cancel()
        refreshRequestTask?.cancel()
    }

    var filteredProviders: [ProviderInfo] {
        providers.filter { ($0.appType ?? "codex") == selectedChannelApp }
    }
    var selectedAppSummary: ProviderAppSummary? { providerAppSummaries[selectedChannelApp] }
    var usableProviderCount: Int { filteredProviders.filter { $0.usability == "usable" }.count }
    var queueProviderCount: Int { filteredProviders.filter { $0.inFailoverQueue }.count }
    var currentProviderName: String { filteredProviders.first(where: { $0.isCurrent })?.name ?? "未识别" }
    var problemProviderCount: Int { filteredProviders.filter { $0.usability != "usable" }.count }
    var changedMultiplierCount: Int { filteredProviders.filter { $0.multiplierChanged == true }.count }
    var selectedStandardCost24hUSD: Double { selectedAppSummary?.totalStandardCost24hUSD ?? 0 }
    var selectedStandardCostAllUSD: Double { selectedAppSummary?.totalStandardCostAllUSD ?? 0 }
    var selectedSwitchTodayUSD: Double { selectedAppSummary?.ccSwitchStandardCostTodayUSD ?? 0 }
    var selectedSwitchAllUSD: Double { selectedAppSummary?.ccSwitchStandardCostAllUSD ?? 0 }
    var selectedRelayTodayUSD: Double { selectedAppSummary?.relayStandardCostTodayUSD ?? 0 }
    var selectedVerifiedRelayStandardTodayUSD: Double { selectedAppSummary?.verifiedRelayStandardCostTodayUSD ?? 0 }
    var selectedEstimatedRelayActualTodayUSD: Double { selectedAppSummary?.estimatedRelayActualCostTodayUSD ?? 0 }
    var selectedUnpricedRelayTodayUSD: Double { selectedAppSummary?.unpricedRelayStandardCostTodayUSD ?? 0 }
    var selectedSessionStandardTodayUSD: Double { max(0, selectedSwitchTodayUSD - selectedRelayTodayUSD) }
    var selectedStandardCost24hCNY: Double { selectedAppSummary?.totalStandardCost24hCNY ?? selectedStandardCost24hUSD * usdCnyRate }
    var selectedStandardCostAllCNY: Double { selectedAppSummary?.totalStandardCostAllCNY ?? selectedStandardCostAllUSD * usdCnyRate }
    var selectedSwitchTodayCNY: Double { selectedAppSummary?.ccSwitchStandardCostTodayCNY ?? selectedSwitchTodayUSD * usdCnyRate }
    var selectedSwitchAllCNY: Double { selectedAppSummary?.ccSwitchStandardCostAllCNY ?? selectedSwitchAllUSD * usdCnyRate }
    var selectedRelayTodayCNY: Double { selectedAppSummary?.relayStandardCostTodayCNY ?? selectedRelayTodayUSD * usdCnyRate }
    var selectedVerifiedRelayStandardTodayCNY: Double { selectedAppSummary?.verifiedRelayStandardCostTodayCNY ?? selectedVerifiedRelayStandardTodayUSD * usdCnyRate }
    var selectedEstimatedRelayActualTodayCNY: Double { selectedAppSummary?.estimatedRelayActualCostTodayCNY ?? selectedEstimatedRelayActualTodayUSD * usdCnyRate }
    var selectedUnpricedRelayTodayCNY: Double { selectedAppSummary?.unpricedRelayStandardCostTodayCNY ?? selectedUnpricedRelayTodayUSD * usdCnyRate }
    var selectedSessionStandardTodayCNY: Double { max(0, selectedSwitchTodayCNY - selectedRelayTodayCNY) }

    var relayPricingCoverageText: String {
        guard selectedRelayTodayUSD > 0 else { return "今天还没有中转 API 费用" }
        let percent = selectedVerifiedRelayStandardTodayUSD / selectedRelayTodayUSD * 100
        return String(format: "已覆盖中转原价 %.1f%%，不是完整合计", percent)
    }

    var costReconciliationText: String {
        if selectedSessionStandardTodayCNY > 0.01 {
            return String(
                format: "Switch 今天 ¥%.2f = 中转 API 原价 ¥%.2f + Codex 会话标准估值 ¥%.2f；会话估值不是中转站扣款。",
                selectedSwitchTodayCNY, selectedRelayTodayCNY, selectedSessionStandardTodayCNY
            )
        }
        return String(format: "Switch 今天 ¥%.2f；其中可归到中转 API 的原价是 ¥%.2f。", selectedSwitchTodayCNY, selectedRelayTodayCNY)
    }

    var estimatedCost24hText: String {
        (selectedAppSummary?.estimatedProviderCount ?? 0) > 0
            ? String(
                format: "¥%.2f",
                selectedAppSummary?.totalEstimatedActualCost24hCNY
                    ?? (selectedAppSummary?.totalEstimatedActualCost24hUSD ?? 0) * usdCnyRate
            )
            : "待查询"
    }

    var estimatedCostAllText: String {
        (selectedAppSummary?.estimatedProviderCount ?? 0) > 0
            ? String(
                format: "¥%.2f",
                selectedAppSummary?.totalEstimatedActualCostAllCNY
                    ?? (selectedAppSummary?.totalEstimatedActualCostAllUSD ?? 0) * usdCnyRate
            )
            : "待查询"
    }

    var exchangeRateText: String {
        let source = exchangeRateSource == "live" ? "自动更新" : (exchangeRateSource == "cache" ? "断网沿用缓存" : "安全备用值")
        guard exchangeRateUpdatedAt > 0 else {
            return String(format: "1 美元 = ¥%.4f · %@", usdCnyRate, source)
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "MM-dd HH:mm"
        let time = formatter.string(from: Date(timeIntervalSince1970: TimeInterval(exchangeRateUpdatedAt)))
        return String(format: "1 美元 = ¥%.4f · %@ · %@", usdCnyRate, source, time)
    }

    var nextRetrySummary: String {
        guard nextRetryAt > 0 else { return "暂无待重试" }
        let remaining = max(0, nextRetryAt - Int(Date().timeIntervalSince1970))
        let time = remaining <= 0 ? "即将执行" : (remaining < 60 ? "\(remaining) 秒后" : "\((remaining + 59) / 60) 分钟后")
        return "\(time) · 第 \(max(1, nextRetryAttempt)) 次 · \(nextRetryTitle)"
    }

    var watcherDetail: String {
        if proxyRouteStatus == "unavailable" || proxyRouteStatus == "error" { return proxyRouteDetail }
        if scheduledRetryCount > 0 { return "下一次自动动作：\(nextRetrySummary)" }
        if lastSuccessAt > 0 && !lastSuccessTitle.isEmpty { return "\(heartbeatDetail)；最近完成：\(lastSuccessTitle)" }
        return "\(heartbeatDetail)；关闭本窗口不影响运行。"
    }

    var healthTitle: String {
        if !isLoaded { return "未运行" }
        if isPaused { return "已暂停" }
        if blockedCount > 0 { return "需人工处理" }
        if !daemonHealthy { return "后台无响应" }
        return "健康"
    }

    var healthColor: Color {
        if !isLoaded || isPaused { return Palette.muted }
        if blockedCount > 0 || !daemonHealthy { return Palette.danger }
        return Palette.success
    }

    var heartbeatDetail: String {
        guard let heartbeatAge else { return "尚未收到后台心跳" }
        return daemonHealthy ? "最近心跳 \(heartbeatAge) 秒前" : "心跳已中断 \(heartbeatAge) 秒"
    }

    var phaseTitle: String {
        if isPaused { return "已暂停" }
        switch phase {
        case "circuit_open": return "线路熔断中"
        case "recovery_grace": return "线路恢复观察中"
        case "resuming": return "正在续接任务"
        default: return isLoaded ? "守望中" : "未启动"
        }
    }

    var phaseColor: Color {
        if isPaused || !isLoaded { return Palette.muted }
        if !daemonHealthy || blockedCount > 0 { return Palette.danger }
        switch phase {
        case "circuit_open": return Palette.danger
        case "recovery_grace": return Palette.warning
        case "resuming": return Palette.accent
        default: return Palette.success
        }
    }

    var lastEventTime: String {
        guard lastEventAt > 0 else { return "尚无记录" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "MM-dd HH:mm:ss"
        return formatter.string(from: Date(timeIntervalSince1970: TimeInterval(lastEventAt)))
    }

    func begin() {
        if didBegin { return }
        didBegin = true
        refresh()
        loadSettings()
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let self else { return }
                await self.refreshInBackground()
            }
        }
    }

    func refresh() {
        refreshGeneration += 1
        let generation = refreshGeneration
        refreshRequestTask?.cancel()
        refreshRequestTask = Task { [weak self] in
            guard let self else { return }
            let payload = await self.loadRefreshPayload()
            guard !Task.isCancelled else { return }
            self.apply(payload, generation: generation)
        }
    }

    private func refreshInBackground() async {
        let generation = refreshGeneration
        let payload = await loadRefreshPayload()
        guard !Task.isCancelled else { return }
        apply(payload, generation: generation)
    }

    private func loadRefreshPayload() async -> RefreshPayload {
        await Task.detached(priority: .utility) {
            RefreshPayload(
                stateData: try? Data(contentsOf: stateURL),
                eventData: readTailData(watcherLogURL, maxBytes: 128 * 1024),
                providerData: try? Data(contentsOf: providersURL),
                launchAgentLoaded: launchAgentRunningSync()
            )
        }.value
    }

    private func apply(_ payload: RefreshPayload, generation: Int) {
        guard generation == refreshGeneration else { return }
        let state = decodeJSON(payload.stateData)
        phase = state["phase"] as? String ?? "idle"
        lastEvent = state["last_event"] as? String ?? "等待 CC Switch 熔断事件"
        lastEventAt = state["last_event_at"] as? Int ?? 0
        resumeCount = state["resume_count"] as? Int ?? 0
        let activeQueueCount = (state["queue"] as? [[String: Any]])?.count ?? 0
        let backlogCount = (state["candidate_backlog"] as? [[String: Any]])?.count ?? 0
        queueCount = activeQueueCount + backlogCount
        let scheduled = state["scheduled_retries"] as? [[String: Any]] ?? []
        scheduledRetryCount = state["scheduled_retry_count"] as? Int ?? scheduled.count
        if let next = scheduled.min(by: { ($0["not_before"] as? Int ?? Int.max) < ($1["not_before"] as? Int ?? Int.max) }) {
            nextRetryAt = next["not_before"] as? Int ?? 0
            nextRetryTitle = next["title"] as? String ?? "Codex 对话"
            nextRetryAttempt = next["attempt"] as? Int ?? 1
            nextRetryReason = next["error"] as? String ?? ""
        } else {
            nextRetryAt = 0
            nextRetryTitle = ""
            nextRetryAttempt = 0
            nextRetryReason = ""
        }
        inflightCount = (state["inflight"] as? [String: Any])?.count ?? 0
        blockedCount = (state["blocked"] as? [String: Any])?.count ?? 0
        lastSuccessAt = state["last_success_at"] as? Int ?? 0
        lastSuccessTitle = state["last_success_title"] as? String ?? ""
        let heartbeatAt = state["heartbeat_at"] as? Int ?? 0
        heartbeatAge = heartbeatAt > 0 ? max(0, Int(Date().timeIntervalSince1970) - heartbeatAt) : nil
        isPaused = FileManager.default.fileExists(atPath: pauseURL.path)
        isLoaded = payload.launchAgentLoaded
        daemonHealthy = isLoaded && (heartbeatAge ?? Int.max) <= 30
        proxyRouteStatus = state["proxy_route_status"] as? String ?? "unchecked"
        proxyRouteDetail = state["proxy_route_detail"] as? String ?? "尚未检查 Codex 是否经过 CC Switch"
        events = readEvents(from: payload.eventData)
        readProviderSnapshot(from: payload.providerData)
    }

    func toggleWatcher() {
        performControl(isPaused || !isLoaded ? "start" : "pause") {
            self.toast = self.isPaused || !self.isLoaded ? "守望器已启动" : "守望器已暂停"
        }
    }

    func inspectNow() {
        guard let daemonPath = Bundle.main.path(forResource: "daemon", ofType: "py") else {
            errorMessage = "App 内缺少 daemon.py，请重新安装。"
            return
        }
        isWorking = true
        Task {
            let result = await runDetached("/usr/bin/python3", [daemonPath, "--inspect"])
            isWorking = false
            if result.code != 0 {
                errorMessage = result.output.isEmpty ? "检测失败，打开日志查看原因。" : result.output
                return
            }
            guard let data = result.output.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                errorMessage = "检测结果无法解析。"
                return
            }
            let candidates = (object["current_candidates"] as? [[String: Any]])?.count ?? 0
            let pending = (object["pending_active_threads"] as? [[String: Any]])?.count ?? 0
            inspectSummary = candidates == 0 && pending == 0
                ? "当前没有需要续接的任务"
                : "可续接 \(candidates) 条，观察中 \(pending) 条"
            toast = "检测完成"
        }
    }

    func runDiagnostics() {
        isWorking = true
        Task {
            let launchd = await runDetached("/bin/launchctl", ["print", "gui/\(getuid())/com.local.codex-circuit-resumer"])
            let codexURL = resolvedCodexBinaryURL()
            let codex = await runDetached(codexURL.path, ["--version"])
            let login = await runDetached(codexURL.path, ["login", "status"])
            let power = await runDetached("/usr/bin/pmset", ["-g", "batt"])
            let stderrSize = fileSize(launchdErrorURL)
            let overnightAwake = launchd.output.contains("/usr/bin/caffeinate") && launchd.output.contains("-s")
            let onACPower = power.output.contains("AC Power")
            diagnostics = [
                DiagnosticItem(name: "后台守望器", detail: launchd.code == 0 ? "LaunchAgent 正在运行" : "LaunchAgent 未运行", healthy: launchd.code == 0),
                DiagnosticItem(name: "后台心跳", detail: heartbeatDetail, healthy: daemonHealthy),
                DiagnosticItem(name: "接电整夜运行", detail: overnightAwake && onACPower ? "已接电并阻止系统睡眠；请保持开盖" : "请接上电源并保持开盖，电池或合盖仍会睡眠", healthy: overnightAwake && onACPower),
                DiagnosticItem(name: "CC Switch 日志", detail: ccSwitchLogURL.path, healthy: FileManager.default.fileExists(atPath: ccSwitchLogURL.path)),
                DiagnosticItem(name: "CC Switch 数据库", detail: ccSwitchDBURL.path, healthy: FileManager.default.fileExists(atPath: ccSwitchDBURL.path)),
                DiagnosticItem(name: "Codex CLI", detail: codex.code == 0 ? "\(codex.output.trimmingCharacters(in: .whitespacesAndNewlines)) · \(codexURL.path)" : "无法启动 Codex CLI", healthy: codex.code == 0),
                DiagnosticItem(name: "Codex 登录", detail: login.code == 0 ? login.output.trimmingCharacters(in: .whitespacesAndNewlines) : "登录已失效，请运行 codex login", healthy: login.code == 0),
                DiagnosticItem(
                    name: "CC Switch 故障转移路由",
                    detail: proxyRouteDetail,
                    healthy: ["ready", "repaired"].contains(proxyRouteStatus)
                ),
                DiagnosticItem(name: "后台错误日志", detail: stderrSize == 0 ? "空，没有后台错误" : "\(stderrSize) bytes，请打开日志检查", healthy: stderrSize == 0),
            ]
            let healthyCount = diagnostics.filter(\.healthy).count
            diagnosticSummary = healthyCount == diagnostics.count
                ? "\(healthyCount)/\(diagnostics.count) 项正常"
                : "\(healthyCount)/\(diagnostics.count) 项正常，发现问题"
            isWorking = false
            toast = "自检完成"
        }
    }

    func runSleepCheck() {
        guard let controlPath = Bundle.main.path(forResource: "control", ofType: "sh") else {
            errorMessage = "App 内缺少 control.sh，请重新安装。"
            return
        }
        isWorking = true
        Task {
            let result = await runDetached(controlPath, ["sleep-check"])
            isWorking = false
            if result.code != 0 {
                errorMessage = result.output.isEmpty ? "睡前检查失败，请打开日志查看原因。" : result.output
                return
            }
            let lines = result.output.split(separator: "\n").map(String.init)
            diagnosticSummary = lines.first ?? "睡前检查完成"
            diagnostics = lines.dropFirst().compactMap { line in
                guard line.hasPrefix("✓ ") || line.hasPrefix("! ") else { return nil }
                let healthy = line.hasPrefix("✓ ")
                let body = String(line.dropFirst(2))
                let parts = body.split(separator: "：", maxSplits: 1, omittingEmptySubsequences: false)
                return DiagnosticItem(
                    name: parts.first.map(String.init) ?? "检查项",
                    detail: parts.dropFirst().first.map(String.init) ?? body,
                    healthy: healthy
                )
            }
            page = .system
            toast = diagnosticSummary
        }
    }

    func loadSettings() {
        let object = readJSON(configURL)
        configObject = object
        graceSeconds = object["recovery_grace_seconds"] as? Int ?? 20
        idleSeconds = object["active_thread_idle_seconds"] as? Int ?? 240
        maxCandidates = object["max_candidates_per_incident"] as? Int ?? 8
        notificationsEnabled = object["notify"] as? Bool ?? true
        modelRetryEnabled = object["model_retry_enabled"] as? Bool ?? true
        capacityReasoningFallbackEnabled = object["capacity_reasoning_fallback_enabled"] as? Bool ?? true
        capacityReasoningPromoteEnabled = object["capacity_reasoning_promote_enabled"] as? Bool ?? true
        retryUntilSuccess = object["model_retry_until_success"] as? Bool ?? true
        retryBaseSeconds = object["model_retry_base_seconds"] as? Int ?? 60
        retryMaxSeconds = object["model_retry_max_seconds"] as? Int ?? 900
        retryJitterSeconds = object["model_retry_jitter_seconds"] as? Int ?? 15
        retryMaxAttempts = object["model_retry_max_attempts"] as? Int ?? 50
        retryWatchHours = object["model_retry_watch_hours"] as? Int ?? 24
        resumePrompt = object["resume_prompt"] as? String ?? resumePrompt
        settingsDirty = false
        didLoadSettings = true
    }

    func markSettingsDirty() {
        if didLoadSettings { settingsDirty = true }
    }

    func saveSettings() {
        guard (5...180).contains(graceSeconds) else {
            errorMessage = "恢复等待时间必须在 5 到 180 秒之间。"
            return
        }
        guard (60...900).contains(idleSeconds) else {
            errorMessage = "无响应保护必须在 60 到 900 秒之间。"
            return
        }
        guard (1...16).contains(maxCandidates) else {
            errorMessage = "单批任务上限必须在 1 到 16 之间。"
            return
        }
        guard (30...900).contains(retryBaseSeconds) else {
            errorMessage = "首次重试等待必须在 30 到 900 秒之间。"
            return
        }
        guard (60...3600).contains(retryMaxSeconds) else {
            errorMessage = "最大重试间隔必须在 60 到 3600 秒之间。"
            return
        }
        guard retryMaxSeconds >= retryBaseSeconds else {
            errorMessage = "最大重试间隔不能小于首次重试等待。"
            return
        }
        guard (0...120).contains(retryJitterSeconds) else {
            errorMessage = "错峰随机等待必须在 0 到 120 秒之间。"
            return
        }
        guard retryUntilSuccess || (1...50).contains(retryMaxAttempts) else {
            errorMessage = "最大重试次数必须在 1 到 50 次之间。"
            return
        }
        guard (1...72).contains(retryWatchHours) else {
            errorMessage = "守候时长必须在 1 到 72 小时之间。"
            return
        }
        guard !resumePrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            errorMessage = "续接内容不能为空。"
            return
        }

        configObject["recovery_grace_seconds"] = graceSeconds
        configObject["active_thread_idle_seconds"] = idleSeconds
        configObject["max_candidates_per_incident"] = maxCandidates
        configObject["notify"] = notificationsEnabled
        configObject["model_retry_enabled"] = modelRetryEnabled
        configObject["capacity_reasoning_fallback_enabled"] = capacityReasoningFallbackEnabled
        configObject["capacity_reasoning_promote_enabled"] = capacityReasoningPromoteEnabled
        configObject["model_retry_until_success"] = retryUntilSuccess
        configObject["model_retry_base_seconds"] = retryBaseSeconds
        configObject["model_retry_max_seconds"] = retryMaxSeconds
        configObject["model_retry_jitter_seconds"] = retryJitterSeconds
        configObject["model_retry_max_attempts"] = retryMaxAttempts
        configObject["model_retry_watch_hours"] = retryWatchHours
        configObject["resume_prompt"] = resumePrompt
        do {
            let data = try JSONSerialization.data(withJSONObject: configObject, options: [.prettyPrinted, .sortedKeys])
            try FileManager.default.createDirectory(at: appSupport, withIntermediateDirectories: true)
            try data.write(to: configURL, options: .atomic)
            settingsDirty = false
            performControl("start") { self.toast = "设置已保存并生效" }
        } catch {
            errorMessage = "保存失败：\(error.localizedDescription)"
        }
    }

    func resetSettings() {
        graceSeconds = 20
        idleSeconds = 240
        maxCandidates = 8
        notificationsEnabled = true
        modelRetryEnabled = true
        capacityReasoningFallbackEnabled = true
        capacityReasoningPromoteEnabled = true
        retryUntilSuccess = true
        retryBaseSeconds = 60
        retryMaxSeconds = 900
        retryJitterSeconds = 15
        retryMaxAttempts = 50
        retryWatchHours = 24
        resumePrompt = "继续。上次因中转站熔断或上游临时故障中断，请从中断处继续，不要重复已经完成的工作。"
        settingsDirty = true
    }

    func refreshBalances() {
        guard let daemonPath = Bundle.main.path(forResource: "daemon", ofType: "py") else {
            errorMessage = "App 内缺少 daemon.py，请重新安装。"
            return
        }
        isWorking = true
        Task {
            let result = await runDetached("/usr/bin/python3", [daemonPath, "--providers", "--probe-balances", "--probe-billing", "--probe-exchange"])
            isWorking = false
            if result.code != 0 {
                errorMessage = result.output.isEmpty ? "渠道数据刷新失败，请打开日志查看原因。" : result.output
                return
            }
            readProviderSnapshot()
            toast = "真实倍率、余额和费用已刷新"
        }
    }

    func openLogs() { NSWorkspace.shared.open(appSupport.appendingPathComponent("logs", isDirectory: true)) }
    func openCodex() { openApplication(displayName: "Codex", bundleIdentifier: "com.openai.codex", fallbackPaths: ["/Applications/ChatGPT.app", "~/Applications/ChatGPT.app"]) }
    func openCCSwitch() { openApplication(displayName: "CC Switch", bundleIdentifier: "com.ccswitch.desktop", fallbackPaths: ["/Applications/CC Switch.app", "~/Applications/CC Switch.app"]) }

    private func openApplication(displayName: String, bundleIdentifier: String, fallbackPaths: [String]) {
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleIdentifier) {
            NSWorkspace.shared.openApplication(at: url, configuration: .init())
            return
        }
        for path in fallbackPaths {
            let expanded = (path as NSString).expandingTildeInPath
            let url = URL(fileURLWithPath: expanded)
            if FileManager.default.fileExists(atPath: url.path) {
                NSWorkspace.shared.openApplication(at: url, configuration: .init())
                return
            }
        }
        errorMessage = "找不到 \(displayName)，请确认已经安装。"
    }

    func copyDiagnostics() {
        let lines = diagnostics.map { "\($0.healthy ? "正常" : "异常") | \($0.name) | \($0.detail)" }
        let text = (["Codex 熔断续聊诊断", "状态：\(phaseTitle)", "最后事件：\(lastEvent)"] + lines).joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        toast = "诊断信息已复制"
    }

    private func performControl(_ action: String, completion: @escaping () -> Void) {
        guard let controlPath = Bundle.main.path(forResource: "control", ofType: "sh") else {
            errorMessage = "App 内缺少 control.sh，请重新安装。"
            return
        }
        isWorking = true
        Task {
            let result = await runDetached(controlPath, [action])
            isWorking = false
            if result.code == 0 {
                try? await Task.sleep(nanoseconds: 350_000_000)
                refresh()
                completion()
            } else {
                errorMessage = result.output.isEmpty ? "操作失败，请打开日志。" : result.output
            }
        }
    }

    private func decodeJSON(_ data: Data?) -> [String: Any] {
        guard let data,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return object
    }

    private func readJSON(_ url: URL) -> [String: Any] {
        decodeJSON(try? Data(contentsOf: url))
    }

    private func resolvedCodexBinaryURL() -> URL {
        if let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex") {
            let discovered = appURL.appendingPathComponent("Contents/Resources/codex")
            if FileManager.default.isExecutableFile(atPath: discovered.path) { return discovered }
        }
        return FileManager.default.isExecutableFile(atPath: nativeCodexBinaryURL.path) ? nativeCodexBinaryURL : codexBinaryURL
    }

    private func readProviderSnapshot(from data: Data? = nil) {
        guard let data = data ?? (try? Data(contentsOf: providersURL)) else { return }
        do {
            let snapshot = try JSONDecoder().decode(ProviderSnapshot.self, from: data)
            providers = snapshot.providers
            providerAppSummaries = snapshot.appSummaries ?? [:]
            providersGeneratedAt = snapshot.generatedAt
            providersError = snapshot.error
            totalStandardCost24hUSD = snapshot.totalStandardCost24hUSD ?? 0
            totalStandardCostAllUSD = snapshot.totalStandardCostAllUSD ?? 0
            totalEstimatedActualCost24hUSD = snapshot.totalEstimatedActualCost24hUSD ?? 0
            totalEstimatedActualCostAllUSD = snapshot.totalEstimatedActualCostAllUSD ?? 0
            totalStandardCost24hCNY = snapshot.totalStandardCost24hCNY ?? 0
            totalStandardCostAllCNY = snapshot.totalStandardCostAllCNY ?? 0
            totalEstimatedActualCost24hCNY = snapshot.totalEstimatedActualCost24hCNY ?? 0
            totalEstimatedActualCostAllCNY = snapshot.totalEstimatedActualCostAllCNY ?? 0
            usdCnyRate = snapshot.usdCnyRate ?? 7.2
            exchangeRateUpdatedAt = snapshot.exchangeRateUpdatedAt ?? 0
            exchangeRateSource = snapshot.exchangeRateSource ?? "fallback"
            estimatedProviderCount = snapshot.estimatedProviderCount ?? 0
        } catch {
            providersError = "渠道快照无法解析"
        }
    }

    private func launchAgentRunning() -> Bool {
        launchAgentRunningSync()
    }

    private func readEvents(from data: Data? = nil) -> [WatcherEvent] {
        guard let data = data ?? (try? Data(contentsOf: watcherLogURL)),
              let text = String(data: data, encoding: .utf8) else { return [] }
        let pattern = try? NSRegularExpression(pattern: "^(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}),\\d+ (INFO|WARNING|ERROR) (.*)$")
        return text.split(separator: "\n").suffix(80).reversed().compactMap { raw in
            let line = String(raw)
            let range = NSRange(line.startIndex..<line.endIndex, in: line)
            guard let match = pattern?.firstMatch(in: line, range: range), match.numberOfRanges == 4,
                  let timeRange = Range(match.range(at: 1), in: line),
                  let levelRange = Range(match.range(at: 2), in: line),
                  let messageRange = Range(match.range(at: 3), in: line) else { return nil }
            let fullTime = String(line[timeRange])
            return WatcherEvent(time: String(fullTime.suffix(8)), level: String(line[levelRange]), message: String(line[messageRange]))
        }
    }

    private func fileSize(_ url: URL) -> Int64 {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        return (attributes?[.size] as? NSNumber)?.int64Value ?? 0
    }

    private func runDetached(_ executable: String, _ arguments: [String]) async -> CommandResult {
        await Task.detached(priority: .userInitiated) {
            runCommand(executable, arguments)
        }.value
    }
}

private func runCommand(_ executable: String, _ arguments: [String]) -> CommandResult {
    let process = Process()
    let pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.standardOutput = pipe
    process.standardError = pipe
    var environment = ProcessInfo.processInfo.environment
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    environment["PATH"] = "\(home)/.local/bin:\(home)/.hermes/node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    process.environment = environment
    do {
        try process.run()
        process.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return CommandResult(code: process.terminationStatus, output: String(data: data, encoding: .utf8) ?? "")
    } catch {
        return CommandResult(code: 127, output: error.localizedDescription)
    }
}

private func launchAgentRunningSync() -> Bool {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    process.arguments = ["print", "gui/\(getuid())/com.local.codex-circuit-resumer"]
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        process.waitUntilExit()
        return process.terminationStatus == 0
    } catch { return false }
}

private func readTailData(_ url: URL, maxBytes: Int) -> Data? {
    guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
    defer { try? handle.close() }
    let size = (try? handle.seekToEnd()) ?? 0
    let offset = size > UInt64(maxBytes) ? size - UInt64(maxBytes) : 0
    try? handle.seek(toOffset: offset)
    return try? handle.readToEnd()
}

@main
private struct CodexCircuitResumerApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup("Codex 熔断续聊") {
            RootView(model: model)
                .frame(minWidth: 820, minHeight: 570)
                .background(Palette.canvas)
                .task { model.begin() }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 920, height: 640)
        .commands {
            CommandGroup(replacing: .newItem) { }
        }
    }
}

private struct RootView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        HStack(spacing: 0) {
            Sidebar(model: model)
            Rectangle().fill(Palette.line).frame(width: 1)
            Group {
                switch model.page {
                case .overview: OverviewView(model: model)
                case .channels: ChannelsView(model: model)
                case .events: EventsView(model: model)
                case .settings: SettingsView(model: model)
                case .system: SystemView(model: model)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .foregroundStyle(Palette.ink)
        .overlay(alignment: .bottomTrailing) {
            if let toast = model.toast {
                ToastView(text: toast)
                    .padding(18)
                    .task {
                        try? await Task.sleep(nanoseconds: 2_000_000_000)
                        if model.toast == toast { model.toast = nil }
                    }
            }
        }
        .alert("操作未完成", isPresented: Binding(
            get: { model.errorMessage != nil },
            set: { if !$0 { model.errorMessage = nil } }
        )) {
            Button("好", role: .cancel) { model.errorMessage = nil }
        } message: {
            Text(model.errorMessage ?? "未知错误")
        }
    }
}

private struct Sidebar: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 6).fill(Palette.ink)
                    Image(systemName: "arrow.trianglehead.2.clockwise.rotate.90")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Color.white)
                }
                .frame(width: 34, height: 34)
                VStack(alignment: .leading, spacing: 1) {
                    Text("熔断续聊").font(.system(size: 16, weight: .semibold))
                    Text("CODEX GUARD").font(.system(size: 9, weight: .medium, design: .monospaced)).foregroundStyle(Palette.muted)
                }
            }
            .padding(.horizontal, 18)
            .padding(.top, 22)
            .padding(.bottom, 24)

            VStack(spacing: 4) {
                ForEach(Page.allCases) { page in
                    Button {
                        model.page = page
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: page.icon).frame(width: 19)
                            Text(page.rawValue)
                            Spacer()
                        }
                        .font(.system(size: 13, weight: model.page == page ? .semibold : .regular))
                        .padding(.horizontal, 12)
                        .frame(height: 38)
                        .background(model.page == page ? Palette.ink : Color.clear)
                        .foregroundStyle(model.page == page ? Color.white : Palette.ink)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                    .buttonStyle(.plain)
                    .withoutSystemFocusRing()
                    .accessibilityIdentifier("nav-\(page.rawValue)")
                }
            }
            .padding(.horizontal, 10)

            Spacer()

            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 7) {
                    Circle().fill(model.phaseColor).frame(width: 8, height: 8)
                    Text(model.phaseTitle).font(.system(size: 12, weight: .medium))
                }
                Text("后台独立运行，关闭窗口不影响守望。")
                    .font(.system(size: 10))
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
                Text("VERSION \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "—")")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(Palette.muted)
            }
            .padding(16)
        }
        .frame(width: 176)
        .background(Color.white.opacity(0.68))
    }
}

private struct PageHeader: View {
    let title: String
    let subtitle: String
    @ObservedObject var model: AppModel

    var body: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 25, weight: .bold))
                Text(subtitle).font(.system(size: 12)).foregroundStyle(Palette.muted)
            }
            Spacer()
            HStack(spacing: 8) {
                Circle().fill(model.phaseColor).frame(width: 9, height: 9)
                Text(model.phaseTitle).font(.system(size: 12, weight: .semibold))
            }
            .padding(.horizontal, 11)
            .frame(height: 30)
            .background(Palette.surface)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Palette.line))
            .clipShape(RoundedRectangle(cornerRadius: 6))
        }
    }
}

private struct OverviewView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                PageHeader(title: "线路控制台", subtitle: "恢复后自动接回中断的 Codex 对话", model: model)
                PipelineView(model: model)
                MetricsView(model: model)
                ControlBand(model: model)
                RecentEventsSection(model: model)
            }
            .padding(26)
        }
    }
}

private struct PipelineView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        HStack(spacing: 0) {
            PipelineStep(icon: "antenna.radiowaves.left.and.right", title: "CC Switch", detail: model.phase == "circuit_open" ? "熔断已打开" : "日志持续监听", color: model.phase == "circuit_open" ? Palette.danger : Palette.success)
            PipelineConnector(active: model.phase != "circuit_open")
            PipelineStep(icon: "timer", title: "恢复保护", detail: model.phase == "recovery_grace" ? "等待线路稳定" : "宽限 \(model.graceSeconds) 秒", color: model.phase == "recovery_grace" ? Palette.warning : Palette.accent)
            PipelineConnector(active: model.phase == "resuming" || model.phase == "idle")
            PipelineStep(
                icon: "bubble.left.and.bubble.right",
                title: "Codex 续接",
                detail: model.scheduledRetryCount > 0 ? model.nextRetrySummary : (model.queueCount > 0 ? "队列 \(model.queueCount) 条" : "按原对话恢复"),
                color: model.scheduledRetryCount > 0 ? Palette.warning : (model.phase == "resuming" ? Palette.accent : Palette.success)
            )
        }
        .padding(.horizontal, 18)
        .frame(height: 92)
        .background(Palette.surface)
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }
}

private struct PipelineStep: View {
    let icon: String
    let title: String
    let detail: String
    let color: Color

    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 6).fill(color.opacity(0.12))
                Image(systemName: icon).font(.system(size: 17, weight: .semibold)).foregroundStyle(color)
            }
            .frame(width: 38, height: 38)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(detail).font(.system(size: 10)).foregroundStyle(Palette.muted).lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct PipelineConnector: View {
    let active: Bool
    var body: some View {
        HStack(spacing: 3) {
            Rectangle().fill(active ? Palette.success : Palette.line).frame(height: 2)
            Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold)).foregroundStyle(active ? Palette.success : Palette.line)
        }
        .frame(width: 44)
        .padding(.horizontal, 6)
    }
}

private struct MetricsView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        HStack(spacing: 10) {
            MetricCell(label: "后台健康", value: model.healthTitle, color: model.healthColor, compact: true)
            MetricCell(label: "正在续接", value: "\(model.inflightCount)", color: model.inflightCount > 0 ? Palette.accent : Palette.ink)
            MetricCell(label: "待处理队列", value: "\(model.queueCount + model.scheduledRetryCount)", color: model.queueCount + model.scheduledRetryCount > 0 ? Palette.warning : Palette.ink)
            MetricCell(label: "需人工处理", value: "\(model.blockedCount)", color: model.blockedCount > 0 ? Palette.danger : Palette.ink)
        }
    }
}

private struct MetricCell: View {
    let label: String
    let value: String
    let color: Color
    var compact = false
    var detail: String? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(label).font(.system(size: 10, weight: .medium)).foregroundStyle(Palette.muted)
            Text(value)
                .font(.system(size: compact ? 15 : 22, weight: .bold, design: compact ? .monospaced : .default))
                .foregroundStyle(color)
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            if let detail {
                Text(detail)
                    .font(.system(size: 9))
                    .foregroundStyle(Palette.muted)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(13)
        .frame(maxWidth: .infinity, minHeight: detail == nil ? 70 : 92, alignment: .leading)
        .background(Palette.surface)
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Palette.line))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

private struct ControlBand: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("自动守望").font(.system(size: 14, weight: .semibold))
                    Text(model.isPaused ? "已暂停，不会扫描或续接任务。" : model.watcherDetail)
                        .font(.system(size: 11)).foregroundStyle(Palette.muted)
                        .lineLimit(2)
                }
                Spacer()
                Button {
                    model.toggleWatcher()
                } label: {
                    Label(model.isPaused || !model.isLoaded ? "启动守望" : "暂停守望", systemImage: model.isPaused || !model.isLoaded ? "play.fill" : "pause.fill")
                        .frame(minWidth: 94)
                }
                .buttonStyle(PrimaryActionStyle(color: model.isPaused || !model.isLoaded ? Palette.success : Palette.ink))
                .disabled(model.isWorking)
                .accessibilityIdentifier("toggle-watcher")
            }
            .padding(15)
            Divider()
            HStack(spacing: 10) {
                ToolButton(title: "立即检测", icon: "magnifyingglass", action: model.inspectNow, identifier: "inspect-now")
                ToolButton(title: "睡前检查", icon: "moon.stars", action: model.runSleepCheck, identifier: "sleep-check")
                ToolButton(title: "运行自检", icon: "checkmark.shield", action: model.runDiagnostics, identifier: "run-diagnostics")
                ToolButton(title: "打开 Codex", icon: "bubble.left", action: model.openCodex, identifier: "open-codex")
                ToolButton(title: "打开日志", icon: "doc.text.magnifyingglass", action: model.openLogs, identifier: "open-logs")
                Spacer()
                if model.isWorking { ProgressView().controlSize(.small) }
            }
            .padding(12)
        }
        .background(Palette.surface)
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }
}

private struct ToolButton: View {
    let title: String
    let icon: String
    let action: () -> Void
    let identifier: String
    var body: some View {
        Button(action: action) {
            Label(title, systemImage: icon).font(.system(size: 11, weight: .medium))
        }
        .buttonStyle(SecondaryActionStyle())
        .accessibilityIdentifier(identifier)
    }
}

private struct RecentEventsSection: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("最近事件").font(.system(size: 14, weight: .semibold))
                Spacer()
                Text(model.inspectSummary).font(.system(size: 10)).foregroundStyle(Palette.muted)
            }
            if model.events.isEmpty {
                EmptyEvents()
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(model.events.prefix(4).enumerated()), id: \.element.id) { index, event in
                        EventRow(event: event)
                        if index < min(model.events.count, 4) - 1 { Divider().padding(.leading, 35) }
                    }
                }
                .background(Palette.surface)
                .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
                .clipShape(RoundedRectangle(cornerRadius: 7))
            }
        }
    }
}

private struct ChannelsView: View {
    @ObservedObject var model: AppModel

    private var generatedText: String {
        guard model.providersGeneratedAt > 0 else { return "尚未生成快照" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "MM-dd HH:mm:ss"
        return formatter.string(from: Date(timeIntervalSince1970: TimeInterval(model.providersGeneratedAt)))
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                PageHeader(title: "渠道体检", subtitle: "严格按 CC Switch 排列顺序，核对备注倍率、网站真实倍率和费用", model: model)

                Picker("渠道类型", selection: $model.selectedChannelApp) {
                    Text("Codex").tag("codex")
                    Text("Claude 桌面端").tag("claude-desktop")
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 360)

                HStack(spacing: 10) {
                    MetricCell(label: "可用渠道", value: "\(model.usableProviderCount)", color: model.usableProviderCount > 0 ? Palette.success : Palette.danger)
                    MetricCell(label: "当前渠道", value: model.currentProviderName, color: Palette.ink, compact: true)
                    MetricCell(label: "候补队列", value: "\(model.queueProviderCount)", color: Palette.accent)
                    MetricCell(label: "未测/降级/故障", value: "\(model.problemProviderCount)", color: model.problemProviderCount > 0 ? Palette.warning : Palette.ink)
                }

                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("金额对账").font(.system(size: 11, weight: .semibold))
                        Text(model.costReconciliationText)
                            .font(.system(size: 10))
                            .foregroundStyle(Palette.muted)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 5) {
                        ProviderPill(text: String(format: "Switch 历史标准价 ¥%.2f", model.selectedSwitchAllCNY), color: Palette.muted)
                        ProviderPill(
                            text: "真实倍率变化 \(model.changedMultiplierCount) 个",
                            color: model.changedMultiplierCount > 0 ? Palette.warning : Palette.success
                        )
                    }
                }
                .padding(.horizontal, 2)

                HStack(spacing: 10) {
                    MetricCell(
                        label: "CC Switch · 今天总成本（00:00 起）",
                        value: String(format: "¥%.2f", model.selectedSwitchTodayCNY),
                        color: Palette.ink,
                        compact: true,
                        detail: "与 Switch“当天”页面同口径"
                    )
                    MetricCell(
                        label: "其中中转 API · 折前（00:00 起）",
                        value: String(format: "¥%.2f", model.selectedRelayTodayCNY),
                        color: Palette.ink,
                        compact: true,
                        detail: "只算经过代理的 API 请求"
                    )
                    MetricCell(
                        label: "已核实倍率部分 · 折后",
                        value: String(format: "¥%.2f", model.selectedEstimatedRelayActualTodayCNY),
                        color: Palette.success,
                        compact: true,
                        detail: model.relayPricingCoverageText
                    )
                    MetricCell(
                        label: "暂时无法折算 · 折前",
                        value: String(format: "¥%.2f", model.selectedUnpricedRelayTodayCNY),
                        color: model.selectedUnpricedRelayTodayCNY > 0.01 ? Palette.warning : Palette.success,
                        compact: true,
                        detail: "缺真实倍率或渠道已删除，不硬猜"
                    )
                }

                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("快照时间 \(generatedText) · \(model.exchangeRateText)").font(.system(size: 11, weight: .medium))
                        Text(model.providersError ?? "金额每 30 秒按 CC Switch 日志刷新；真实倍率查询不发送模型请求、不消耗额度，实付金额按网站当前倍率估算。")
                            .font(.system(size: 10))
                            .foregroundStyle(model.providersError == nil ? Palette.muted : Palette.danger)
                    }
                    Spacer()
                    Button { model.refreshBalances() } label: { Label("刷新倍率/费用", systemImage: "arrow.clockwise") }
                        .buttonStyle(SecondaryActionStyle())
                        .disabled(model.isWorking)
                        .accessibilityIdentifier("refresh-balances")
                    ToolButton(title: "打开 CC Switch", icon: "switch.2", action: model.openCCSwitch, identifier: "channels-open-cc-switch")
                }
                .padding(14)
                .background(Palette.surface)
                .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
                .clipShape(RoundedRectangle(cornerRadius: 7))

                if model.filteredProviders.isEmpty {
                    HStack(spacing: 10) {
                        Image(systemName: "tray").foregroundStyle(Palette.muted)
                        Text("还没有渠道快照。启动守望器后会自动生成，也可以点刷新倍率/费用。")
                            .font(.system(size: 11))
                            .foregroundStyle(Palette.muted)
                        Spacer()
                    }
                    .padding(15)
                    .background(Palette.surface)
                    .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
                    .clipShape(RoundedRectangle(cornerRadius: 7))
                } else {
                    VStack(spacing: 0) {
                        ForEach(Array(model.filteredProviders.enumerated()), id: \.element.id) { index, provider in
                            ProviderRow(provider: provider)
                            if index < model.filteredProviders.count - 1 { Divider().padding(.leading, 18) }
                        }
                    }
                    .background(Palette.surface)
                    .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
                    .clipShape(RoundedRectangle(cornerRadius: 7))
                }
            }
            .padding(26)
        }
    }
}

private struct ProviderRow: View {
    let provider: ProviderInfo

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                Circle().fill(provider.statusColor).frame(width: 9, height: 9).padding(.top, 5)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 7) {
                        ProviderPill(text: provider.orderTitle, color: Palette.muted)
                        Text(provider.name).font(.system(size: 13, weight: .semibold))
                        ProviderPill(text: provider.routeTitle, color: provider.temporarilyBypassed == true ? Palette.warning : (provider.isCurrent ? Palette.accent : Palette.muted))
                        ProviderPill(text: provider.statusTitle, color: provider.statusColor)
                    }
                    Text(provider.host).font(.system(size: 10, design: .monospaced)).foregroundStyle(Palette.muted).lineLimit(1)
                }
                Spacer()
                HStack(spacing: 18) {
                    VStack(alignment: .trailing, spacing: 3) {
                        Text(provider.remarkedMultiplierText).font(.system(size: 15, weight: .bold, design: .monospaced))
                        Text("备注倍率 · \(provider.remarkedMultiplierSource ?? provider.multiplierSource)").font(.system(size: 9)).foregroundStyle(Palette.muted)
                    }
                    VStack(alignment: .trailing, spacing: 3) {
                        Text(provider.actualMultiplierText)
                            .font(.system(size: 17, weight: .bold, design: .monospaced))
                            .foregroundStyle(provider.multiplierChanged == true ? Palette.warning : (provider.actualMultiplier == nil ? Palette.muted : Palette.success))
                        Text("真实倍率 · \(provider.actualMultiplierSource ?? "无法验证")").font(.system(size: 9)).foregroundStyle(Palette.muted)
                    }
                }
            }

            if provider.multiplierChanged == true {
                Text("倍率已变化：CC Switch 备注 \(provider.remarkedMultiplierText) → 网站当前 \(provider.actualMultiplierText)")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Palette.warning)
            }

            HStack(spacing: 10) {
                ProviderField(title: "实际模型", value: provider.lastModel ?? "未识别")
                ProviderField(title: "24h 成功率", value: provider.successText)
                ProviderField(title: "平均延迟", value: provider.latencyText)
                ProviderField(title: "24h 估算实付", value: provider.estimatedCostText, color: Palette.success)
                ProviderField(title: "余额", value: provider.balance, color: provider.balanceState == "empty" ? Palette.danger : Palette.ink)
                ProviderField(title: "状态原因", value: provider.reasonText, color: provider.usability == "unavailable" ? Palette.danger : (provider.usability == "degraded" ? Palette.warning : Palette.ink))
            }

            if !provider.lastError.isEmpty {
                Text(provider.lastError)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(Palette.muted)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
    }
}

private struct ProviderField: View {
    let title: String
    let value: String
    var color: Color = Palette.ink

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.system(size: 9, weight: .medium)).foregroundStyle(Palette.muted)
            Text(value)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(color)
                .lineLimit(1)
                .minimumScaleFactor(0.72)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ProviderPill: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.system(size: 9, weight: .semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 7)
            .frame(height: 18)
            .background(color.opacity(0.10))
            .clipShape(RoundedRectangle(cornerRadius: 5))
    }
}

private struct EventsView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "事件记录", subtitle: "熔断、恢复、续接与后台运行记录", model: model)
            HStack {
                Text("最近 \(model.events.count) 条").font(.system(size: 11)).foregroundStyle(Palette.muted)
                Spacer()
                Button { model.openLogs() } label: { Label("打开完整日志", systemImage: "arrow.up.forward.app") }
                    .buttonStyle(SecondaryActionStyle())
            }
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(Array(model.events.enumerated()), id: \.element.id) { index, event in
                        EventRow(event: event)
                        if index < model.events.count - 1 { Divider().padding(.leading, 35) }
                    }
                }
            }
            .background(Palette.surface)
            .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
            .clipShape(RoundedRectangle(cornerRadius: 7))
        }
        .padding(26)
    }
}

private struct EventRow: View {
    let event: WatcherEvent
    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            Circle().fill(event.color).frame(width: 8, height: 8).padding(.top, 4)
            VStack(alignment: .leading, spacing: 3) {
                Text(event.message).font(.system(size: 11, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                Text(event.time).font(.system(size: 9, design: .monospaced)).foregroundStyle(Palette.muted)
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
    }
}

private struct EmptyEvents: View {
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark.circle").foregroundStyle(Palette.success)
            Text("暂无异常事件，守望器正在等待下一次熔断。")
                .font(.system(size: 11)).foregroundStyle(Palette.muted)
            Spacer()
        }
        .padding(15)
        .background(Palette.surface)
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }
}

private struct SettingsView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                PageHeader(title: "恢复设置", subtitle: "保持默认值即可；线路特别脆弱时再调整", model: model)
                SettingsSection(title: "时间保护") {
                    NumberSettingRow(title: "恢复等待", detail: "CC Switch 恢复后，等待线路稳定再续接。", value: $model.graceSeconds, range: 5...180, suffix: "秒")
                        .onChange(of: model.graceSeconds) { _ in model.markSettingsDirty() }
                    Divider()
                    NumberSettingRow(title: "无响应保护", detail: "任务没有明确报错时，至少沉默多久才视为中断。", value: $model.idleSeconds, range: 60...900, suffix: "秒")
                        .onChange(of: model.idleSeconds) { _ in model.markSettingsDirty() }
                    Divider()
                    NumberSettingRow(title: "单批任务上限", detail: "候选较多时自动分批，超过上限的任务会安全排队，不会丢失。", value: $model.maxCandidates, range: 1...16, suffix: "条")
                        .onChange(of: model.maxCandidates) { _ in model.markSettingsDirty() }
                }
                SettingsSection(title: "无人值守") {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("模型满载自动重试").font(.system(size: 12, weight: .medium))
                            Text("Codex 遇到模型满载、429、502、503、504、超时或 OpenResty HTML 400 时，按原对话自动退避续接；HTML 400 会临时避开故障 P1。")
                                .font(.system(size: 10)).foregroundStyle(Palette.muted)
                        }
                        Spacer()
                        Toggle("", isOn: $model.modelRetryEnabled).labelsHidden().toggleStyle(.switch)
                            .onChange(of: model.modelRetryEnabled) { _ in model.markSettingsDirty() }
                    }
                    .padding(14)
                    Divider()
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("模型满载自动降档").font(.system(size: 12, weight: .medium))
                            Text("高档位满了就自动改用中，再改用轻度；只影响这次自动续接，不改平时设置。")
                                .font(.system(size: 10)).foregroundStyle(Palette.muted)
                        }
                        Spacer()
                        Toggle("", isOn: $model.capacityReasoningFallbackEnabled).labelsHidden().toggleStyle(.switch)
                            .onChange(of: model.capacityReasoningFallbackEnabled) { _ in model.markSettingsDirty() }
                    }
                    .padding(14)
                    Divider()
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("高档位恢复后自动升回").font(.system(size: 12, weight: .medium))
                            Text("降档后每 15 分钟再试原来的高档位；高档位恢复就自动用回去，仍满则继续等待。")
                                .font(.system(size: 10)).foregroundStyle(Palette.muted)
                        }
                        Spacer()
                        Toggle("", isOn: $model.capacityReasoningPromoteEnabled).labelsHidden().toggleStyle(.switch)
                            .onChange(of: model.capacityReasoningPromoteEnabled) { _ in model.markSettingsDirty() }
                    }
                    .padding(14)
                    Divider()
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("持续守候到成功").font(.system(size: 12, weight: .medium))
                            Text("整夜模式：不按次数停止；成功完成、你手动继续或关闭开关时自动取消。")
                                .font(.system(size: 10)).foregroundStyle(Palette.muted)
                        }
                        Spacer()
                        Toggle("", isOn: $model.retryUntilSuccess).labelsHidden().toggleStyle(.switch)
                            .onChange(of: model.retryUntilSuccess) { _ in model.markSettingsDirty() }
                    }
                    .padding(14)
                    Divider()
                    NumberSettingRow(title: "首次重试等待", detail: "第一次发现模型满载或上游 503 后等待多久再试。", value: $model.retryBaseSeconds, range: 30...900, suffix: "秒")
                        .onChange(of: model.retryBaseSeconds) { _ in model.markSettingsDirty() }
                    Divider()
                    NumberSettingRow(title: "最大重试间隔", detail: "指数退避的上限，线路差时不要设太低。", value: $model.retryMaxSeconds, range: 60...3600, suffix: "秒")
                        .onChange(of: model.retryMaxSeconds) { _ in model.markSettingsDirty() }
                    Divider()
                    NumberSettingRow(title: "错峰随机等待", detail: "每次重试会在退避时间上随机加一点等待，避免多个任务同时冲击低倍率渠道。", value: $model.retryJitterSeconds, range: 0...120, suffix: "秒")
                        .onChange(of: model.retryJitterSeconds) { _ in model.markSettingsDirty() }
                    Divider()
                    NumberSettingRow(title: "最大重试次数", detail: "同一条对话夜间最多自动续接多少次。", value: $model.retryMaxAttempts, range: 1...50, suffix: "次")
                        .onChange(of: model.retryMaxAttempts) { _ in model.markSettingsDirty() }
                        .disabled(model.retryUntilSuccess)
                        .opacity(model.retryUntilSuccess ? 0.48 : 1)
                    Divider()
                    NumberSettingRow(title: "守候时长", detail: "只盯最近这段时间内新出现的中断，避免翻旧任务。", value: $model.retryWatchHours, range: 1...72, suffix: "小时")
                        .onChange(of: model.retryWatchHours) { _ in model.markSettingsDirty() }
                }
                SettingsSection(title: "行为") {
                    HStack {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("系统通知").font(.system(size: 12, weight: .medium))
                            Text("自动续接任务后显示 macOS 通知。")
                                .font(.system(size: 10)).foregroundStyle(Palette.muted)
                        }
                        Spacer()
                        Toggle("", isOn: $model.notificationsEnabled).labelsHidden().toggleStyle(.switch)
                            .onChange(of: model.notificationsEnabled) { _ in model.markSettingsDirty() }
                    }
                    .padding(14)
                    Divider()
                    VStack(alignment: .leading, spacing: 8) {
                        Text("续接内容").font(.system(size: 12, weight: .medium))
                        TextEditor(text: $model.resumePrompt)
                            .font(.system(size: 11))
                            .frame(minHeight: 70)
                            .padding(7)
                            .background(Palette.canvas)
                            .overlay(RoundedRectangle(cornerRadius: 5).stroke(Palette.line))
                            .clipShape(RoundedRectangle(cornerRadius: 5))
                            .onChange(of: model.resumePrompt) { _ in model.markSettingsDirty() }
                    }
                    .padding(14)
                }
                HStack {
                    Button("恢复默认") { model.resetSettings() }.buttonStyle(SecondaryActionStyle())
                    Spacer()
                    if model.settingsDirty { Text("有未保存的修改").font(.system(size: 10)).foregroundStyle(Palette.warning) }
                    Button { model.saveSettings() } label: { Label("保存并应用", systemImage: "checkmark") }
                        .buttonStyle(PrimaryActionStyle(color: Palette.accent))
                        .disabled(!model.settingsDirty || model.isWorking)
                        .accessibilityIdentifier("save-settings")
                }
            }
            .padding(26)
        }
    }
}

private struct SettingsSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased()).font(.system(size: 9, weight: .semibold, design: .monospaced)).foregroundStyle(Palette.muted)
            VStack(spacing: 0) { content() }
                .background(Palette.surface)
                .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
                .clipShape(RoundedRectangle(cornerRadius: 7))
        }
    }
}

private struct NumberSettingRow: View {
    let title: String
    let detail: String
    @Binding var value: Int
    let range: ClosedRange<Int>
    let suffix: String
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 12, weight: .medium))
                Text(detail).font(.system(size: 10)).foregroundStyle(Palette.muted)
            }
            Spacer()
            Stepper(value: $value, in: range) {
                Text("\(value) \(suffix)").font(.system(size: 11, weight: .semibold, design: .monospaced)).frame(width: 72, alignment: .trailing)
            }
            .frame(width: 150)
        }
        .padding(14)
    }
}

private struct SystemView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                PageHeader(title: "系统自检", subtitle: "检查后台、防休眠、CC Switch、Codex CLI 与错误日志", model: model)
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(model.diagnosticSummary).font(.system(size: 15, weight: .semibold))
                        Text("自检只读取状态，不会发送续接消息。").font(.system(size: 10)).foregroundStyle(Palette.muted)
                    }
                    Spacer()
                    Button { model.runDiagnostics() } label: { Label("运行自检", systemImage: "checkmark.shield") }
                        .buttonStyle(PrimaryActionStyle(color: Palette.accent))
                        .disabled(model.isWorking)
                        .accessibilityIdentifier("system-diagnostics")
                }
                .padding(15)
                .background(Palette.surface)
                .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
                .clipShape(RoundedRectangle(cornerRadius: 7))

                if !model.diagnostics.isEmpty {
                    VStack(spacing: 0) {
                        ForEach(Array(model.diagnostics.enumerated()), id: \.element.id) { index, item in
                            DiagnosticRow(item: item)
                            if index < model.diagnostics.count - 1 { Divider().padding(.leading, 42) }
                        }
                    }
                    .background(Palette.surface)
                    .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
                    .clipShape(RoundedRectangle(cornerRadius: 7))
                }

                VStack(alignment: .leading, spacing: 9) {
                    Text("工具").font(.system(size: 9, weight: .semibold, design: .monospaced)).foregroundStyle(Palette.muted)
                    HStack(spacing: 10) {
                        ToolButton(title: "打开 CC Switch", icon: "switch.2", action: model.openCCSwitch, identifier: "open-cc-switch")
                        ToolButton(title: "打开 Codex", icon: "bubble.left", action: model.openCodex, identifier: "system-open-codex")
                        ToolButton(title: "打开日志", icon: "folder", action: model.openLogs, identifier: "system-open-logs")
                        Button { model.copyDiagnostics() } label: { Label("复制诊断", systemImage: "doc.on.doc") }
                            .buttonStyle(SecondaryActionStyle())
                            .disabled(model.diagnostics.isEmpty)
                    }
                }
            }
            .padding(26)
        }
    }
}

private struct DiagnosticRow: View {
    let item: DiagnosticItem
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: item.healthy ? "checkmark.circle.fill" : "xmark.octagon.fill")
                .font(.system(size: 17)).foregroundStyle(item.healthy ? Palette.success : Palette.danger)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.name).font(.system(size: 12, weight: .semibold))
                Text(item.detail).font(.system(size: 9, design: .monospaced)).foregroundStyle(Palette.muted).lineLimit(2)
            }
            Spacer()
            Text(item.healthy ? "正常" : "异常").font(.system(size: 10, weight: .semibold)).foregroundStyle(item.healthy ? Palette.success : Palette.danger)
        }
        .padding(13)
    }
}

private struct PrimaryActionStyle: ButtonStyle {
    let color: Color
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(Color.white)
            .padding(.horizontal, 13)
            .frame(height: 32)
            .background(color.opacity(configuration.isPressed ? 0.78 : 1))
            .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

private struct SecondaryActionStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(Palette.ink)
            .padding(.horizontal, 11)
            .frame(height: 30)
            .background(configuration.isPressed ? Palette.line.opacity(0.65) : Palette.surface)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Palette.line))
            .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

private struct ToastView: View {
    let text: String
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Palette.success)
            Text(text).font(.system(size: 11, weight: .semibold))
        }
        .padding(.horizontal, 13)
        .frame(height: 38)
        .background(Palette.surface)
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(Palette.line))
        .clipShape(RoundedRectangle(cornerRadius: 7))
        .shadow(color: Color.black.opacity(0.10), radius: 12, y: 4)
    }
}
