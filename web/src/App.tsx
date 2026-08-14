import { useEffect } from "react";
import { languageDirection } from "@/lib/i18n";
import { useApp } from "@/lib/store";
import { Chat } from "./components/Chat";
import { Canvas, CanvasDragBridge } from "./components/Canvas";
import { Sidebar } from "./components/sidebar/Sidebar";
import { HistorySidebar } from "./components/HistorySidebar";
import { SettingsDrawer } from "./components/SettingsDrawer";

export default function App() {
  const mode = useApp((s) => s.mode);
  const history_open = useApp((s) => s.history_sidebar_open);
  const properties_open = useApp((s) => s.properties_sidebar_open);
  const design_focus = useApp((s) => s.design_focus_mode);
  const history_w = useApp((s) => s.history_sidebar_width);
  const chat_rail_w = useApp((s) => s.chat_rail_width);
  const properties_w = useApp((s) => s.properties_sidebar_width);
  const loadBackendInfo = useApp((s) => s.loadBackendInfo);
  const loadServerHistory = useApp((s) => s.loadServerHistory);
  const language = useApp((s) => s.ui_language);

  useEffect(() => {
    loadBackendInfo();
    void loadServerHistory();
  }, [loadBackendInfo, loadServerHistory]);

  useEffect(() => {
    document.documentElement.lang = language;
    document.documentElement.dir = languageDirection(language);
  }, [language]);

  if (mode === "chat") {
    const cols = history_open
      ? `${history_w}px minmax(0, 1fr)`
      : "minmax(0, 1fr)";
    return (
      <>
        <div
          className="app-shell grid h-dvh min-h-0 w-full overflow-hidden"
          style={{ gridTemplateColumns: cols }}
        >
          {history_open && <HistorySidebar />}
          <Chat variant="full" />
        </div>
        <SettingsDrawer />
      </>
    );
  }

  // Canvas mode: history? | chat rail | canvas | properties?
  // Each width comes from the store and is drag-resizable via the
  // ResizeHandle embedded in each panel.
  const focus = design_focus;
  const cols = [
    !focus && history_open ? `${history_w}px` : null,
    !focus ? `${chat_rail_w}px` : null,
    "minmax(0, 1fr)",
    properties_open ? `${properties_w}px` : null,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <>
      <div
        className="app-shell grid h-dvh min-h-0 w-full overflow-hidden"
        style={{ gridTemplateColumns: cols }}
      >
        {!focus && history_open && <HistorySidebar />}
        {!focus && <Chat variant="rail" />}
        <main className="relative h-full min-h-0 overflow-hidden">
          <CanvasDragBridge />
          <Canvas />
        </main>
        {properties_open && <Sidebar />}
      </div>
      <SettingsDrawer />
    </>
  );
}
