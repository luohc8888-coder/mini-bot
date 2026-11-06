import type { ExplorerViewMode } from "../types/architecture";

export type ArchitectureBadgeMode = "live" | "mock" | "loading" | "ready" | "unconfigured";

interface AppHeaderProps {
  workspaceRoot: string;
  viewMode: ExplorerViewMode;
  onViewChange: (viewMode: ExplorerViewMode) => void;
  subtitle?: string;
  architectureMode?: ArchitectureBadgeMode;
}

const ARCHITECTURE_BADGES: Record<
  ArchitectureBadgeMode,
  { label: string; className: string }
> = {
  live: { label: "LIVE LLM", className: "live-badge" },
  mock: { label: "MOCK FLOW", className: "mock-badge" },
  loading: { label: "ANALYZING", className: "live-badge" },
  ready: { label: "LLM READY", className: "live-badge" },
  unconfigured: { label: "NO LLM", className: "mock-badge" },
};

export function AppHeader({
  workspaceRoot,
  viewMode,
  onViewChange,
  subtitle,
  architectureMode = "mock",
}: AppHeaderProps) {
  const architectureBadge = ARCHITECTURE_BADGES[architectureMode];
  return (
    <header className="app-header">
      <div className="brand">
        <div className="brand__mark" aria-hidden="true">
          <span>MH</span>
        </div>
        <div className="brand__copy">
          <div className="brand__title">
            Mini Harness <span>/ Code Explorer</span>
          </div>
          <div className="brand__subtitle">
            {subtitle ??
              (viewMode === "architecture"
                ? "AI 驱动的项目架构理解工作台 · Phase 4.3"
                : "本地代码理解工作台 · Phase 4 Live")}
          </div>
        </div>
      </div>

      <div className="header-meta">
        <span className="workspace-chip">
          <span className="status-dot" />
          {workspaceRoot}
        </span>
        <div className="view-switch" role="group" aria-label="视图切换">
          <button
            type="button"
            className={`view-switch__button${
              viewMode === "architecture" ? " is-active" : ""
            }`}
            aria-pressed={viewMode === "architecture"}
            onClick={() => onViewChange("architecture")}
          >
            运行架构
          </button>
          <button
            type="button"
            className={`view-switch__button${
              viewMode === "structure" ? " is-active" : ""
            }`}
            aria-pressed={viewMode === "structure"}
            onClick={() => onViewChange("structure")}
          >
            代码结构
          </button>
        </div>
        {viewMode === "architecture" ? (
          <span className={architectureBadge.className}>
            {architectureBadge.label}
          </span>
        ) : (
          <span className="live-badge">LIVE API</span>
        )}
        <span className="version-chip">V3.0</span>
      </div>
    </header>
  );
}
