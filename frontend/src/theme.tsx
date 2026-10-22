/* ЭТАП 9: ThemeProvider — localStorage.vilka_theme + prefers-color-scheme. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactElement, ReactNode } from "react";

/* ЭТАП9 коммит12: курация тем — 4 боевые темы. Старые значения localStorage
   ("phantom","zeroday") не проходят isTheme -> падают в дефолт (violet/ghost). */
export const THEMES = ["violet", "axis", "tactical", "ghost"] as const;
export type ThemeName = (typeof THEMES)[number];

const STORAGE_KEY = "vilka_theme";

function isTheme(v: string | null): v is ThemeName {
  return v !== null && (THEMES as readonly string[]).includes(v);
}

function systemDefault(): ThemeName {
  try {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "ghost" : "violet";
  } catch {
    return "violet";
  }
}

interface ThemeCtx {
  theme: ThemeName;
  setTheme: (t: ThemeName) => void;
}

const Ctx = createContext<ThemeCtx>({ theme: "violet", setTheme: () => undefined });

export function ThemeProvider({ children }: { children: ReactNode }): ReactElement {
  const [theme, setThemeState] = useState<ThemeName>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      return isTheme(saved) ? saved : systemDefault();
    } catch {
      return "violet";
    }
  });

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* приватный режим — молча игнорируем */
    }
  }, [theme]);

  const setTheme = useCallback((t: ThemeName) => setThemeState(t), []);
  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  return useContext(Ctx);
}
