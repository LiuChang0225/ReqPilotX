import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type Theme = "light" | "dark";

const copy = {
  workspace: "需求工作台",
  projects: "项目管理",
  documents: "项目管理",
  prepare: "需求提取",
  review: "审阅确认",
  ai: "需求分析",
  matrix: "矩阵导出",
  exports: "矩阵导出",
  settings: "管理员设置",
} as const;

type CopyKey = keyof typeof copy;

interface PreferencesContextValue {
  theme: Theme;
  toggleTheme: () => void;
  t: (key: CopyKey) => string;
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null);

function initialTheme(): Theme {
  return localStorage.getItem("reqx-theme") === "dark" ? "dark" : "light";
}

export function PreferencesProvider({ children }: PropsWithChildren) {
  const [theme, updateTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.lang = "zh-CN";
    document.documentElement.dataset.theme = theme;
    localStorage.removeItem("reqx-language");
  }, [theme]);

  const toggleTheme = useCallback(() => {
    updateTheme((current) => {
      const next = current === "light" ? "dark" : "light";
      localStorage.setItem("reqx-theme", next);
      document.documentElement.dataset.theme = next;
      return next;
    });
  }, []);

  const value = useMemo<PreferencesContextValue>(
    () => ({
      theme,
      toggleTheme,
      t: (key) => copy[key],
    }),
    [theme, toggleTheme],
  );

  return (
    <PreferencesContext.Provider value={value}>
      {children}
    </PreferencesContext.Provider>
  );
}

export function usePreferences(): PreferencesContextValue {
  const context = useContext(PreferencesContext);
  if (!context) {
    throw new Error("usePreferences must be used inside PreferencesProvider");
  }
  return context;
}
