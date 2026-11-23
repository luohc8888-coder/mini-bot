import { useEffect, useState } from "react";

export type Theme = "dark" | "light";

const THEME_STORAGE_KEY = "mini-harness-theme";
const listeners = new Set<(theme: Theme) => void>();
let currentTheme: Theme | null = null;

export function getInitialTheme(): Theme {
  if (currentTheme) {
    return currentTheme;
  }
  if (typeof window === "undefined") {
    return "dark";
  }
  try {
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === "dark" || saved === "light") {
      currentTheme = saved;
      return saved;
    }
  } catch {
    // localStorage 不可用时回退到默认夜间主题。
  }
  currentTheme = "dark";
  return "dark";
}

export function applyTheme(theme: Theme): void {
  currentTheme = theme;
  if (typeof document !== "undefined") {
    document.documentElement.dataset.theme = theme;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", theme === "light" ? "#f4f7fb" : "#070b13");
  }
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // 主题仍然应用到当前页面，只是不持久化。
  }
  for (const listener of listeners) {
    listener(theme);
  }
}

export function useTheme(): [Theme, (next: Theme | ((prev: Theme) => Theme)) => void] {
  const [theme, setThemeState] = useState<Theme>(getInitialTheme);

  useEffect(() => {
    const handleChange = (next: Theme) => setThemeState(next);
    listeners.add(handleChange);
    return () => {
      listeners.delete(handleChange);
    };
  }, []);

  const setTheme = (next: Theme | ((prev: Theme) => Theme)) => {
    const resolved =
      typeof next === "function" ? next(currentTheme ?? getInitialTheme()) : next;
    applyTheme(resolved);
  };

  return [theme, setTheme];
}
