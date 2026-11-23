import type { ExplorerViewMode } from "../types/architecture";
import { useTheme } from "../theme";

export type ArchitectureBadgeMode =
  | "live"
  | "loading"
  | "ready"
  | "unconfigured";

interface AppHeaderProps {
  workspaceRoot: string;
  viewMode: ExplorerViewMode;
  onViewChange: (viewMode: ExplorerViewMode) => void;
  onOpenSearch?: () => void;
  subtitle?: string;
  architectureMode?: ArchitectureBadgeMode;
}

const ARCHITECTURE_BADGES: Record<
  ArchitectureBadgeMode,
  { label: string; className: string }
> = {
  live: { label: "LIVE LLM", className: "live-badge" },
  loading: { label: "ANALYZING", className: "live-badge" },
  ready: { label: "LLM READY", className: "live-badge" },
  unconfigured: { label: "NO LLM", className: "mock-badge" },
};

function workspaceShortName(workspaceRoot: string): string {
  const trimmed = workspaceRoot.replace(/[\\/]+$/, "");
  const parts = trimmed.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? workspaceRoot;
}

export function AppHeader({
  workspaceRoot,
  viewMode,
  onViewChange,
  onOpenSearch,
  subtitle,
  architectureMode = "ready",
}: AppHeaderProps) {
  const architectureBadge = ARCHITECTURE_BADGES[architectureMode];
  const [theme, setTheme] = useTheme();
  const shortWorkspace = workspaceShortName(workspaceRoot);

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
                ? "AI 驱动的项目架构理解工作台"
                : "本地 AST 代码结构探索工作台")}
          </div>
        </div>
      </div>

      <div className="header-meta">
        <span className="workspace-chip" title={workspaceRoot}>
          <span className="status-dot" />
          <span className="workspace-chip__text">{shortWorkspace}</span>
        </span>
        {onOpenSearch ? (
          <button
            type="button"
            className="search-trigger"
            onClick={onOpenSearch}
            title="全局搜索代码符号 (⌘K / Ctrl+K)"
          >
            <span aria-hidden="true">⌕</span>
            <span>搜索符号</span>
            <kbd>⌘K</kbd>
          </button>
        ) : null}
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
        <button
          type="button"
          className="theme-toggle"
          onClick={() =>
            setTheme((current) => (current === "dark" ? "light" : "dark"))
          }
          aria-label={theme === "dark" ? "切换到日光模式" : "切换到夜间模式"}
          aria-pressed={theme === "light"}
          title={theme === "dark" ? "切换到日光模式" : "切换到夜间模式"}
        >
          <span className="theme-toggle__icon" aria-hidden="true">
            {theme === "dark" ? "☀" : "☾"}
          </span>
          <span>{theme === "dark" ? "日光模式" : "夜间模式"}</span>
        </button>
        <span className="version-chip">V3.0</span>
      </div>
    </header>
  );
}
