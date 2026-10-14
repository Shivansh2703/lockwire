import { useState } from "react";

import { IconButton } from "./kit";
import { currentTheme, toggleTheme, type Theme } from "./theme";

// The label is the control's accessible name ("Light theme" / "Dark theme"), as before.
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => currentTheme());
  return (
    <IconButton
      className="kit-iconbtn theme-toggle"
      icon={theme === "dark" ? "sun" : "moon"}
      label={theme === "dark" ? "Light theme" : "Dark theme"}
      onClick={() => setTheme(toggleTheme())}
    />
  );
}
