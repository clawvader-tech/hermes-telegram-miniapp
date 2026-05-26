import { useState, useEffect, useRef } from "react";
import { Activity, BarChart3, Bot, Clock, FileText, KeyRound, MessageSquare, Package, Settings, Terminal, Users } from "lucide-react";
import StatusPage from "@/pages/StatusPage";
import ChatPage from "@/pages/ChatPage";
import AgentsPage from "@/pages/AgentsPage";
import ConfigPage from "@/pages/ConfigPage";
import EnvPage from "@/pages/EnvPage";
import SessionsPage from "@/pages/SessionsPage";
import LogsPage from "@/pages/LogsPage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import CronPage from "@/pages/CronPage";
import SkillsPage from "@/pages/SkillsPage";
import CRMPage from "@/pages/CRMPage";

const NAV_ITEMS = [
  { id: "chat", label: "Chat", icon: Terminal },
  { id: "crm", label: "CRM", icon: Users },
  { id: "status", label: "Status", icon: Activity },
  { id: "agents", label: "Agents", icon: Bot },
  { id: "sessions", label: "Sessions", icon: MessageSquare },
  { id: "analytics", label: "Analytics", icon: BarChart3 },
  { id: "logs", label: "Logs", icon: FileText },
  { id: "cron", label: "Cron", icon: Clock },
  { id: "skills", label: "Skills", icon: Package },
  { id: "config", label: "Config", icon: Settings },
  { id: "env", label: "Keys", icon: KeyRound },
] as const;

type PageId = (typeof NAV_ITEMS)[number]["id"];

const PAGE_COMPONENTS: Record<PageId, React.FC> = {
  chat: ChatPage,
  crm: CRMPage,
  status: StatusPage,
  agents: AgentsPage,
  sessions: SessionsPage,
  analytics: AnalyticsPage,
  logs: LogsPage,
  cron: CronPage,
  skills: SkillsPage,
  config: ConfigPage,
  env: EnvPage,
};

// Pages that need full height (chat)
const FULL_HEIGHT_PAGES = new Set(["chat", "crm"]);

export default function App() {
  const [page, setPage] = useState<PageId>("chat");
  const [animKey, setAnimKey] = useState(0);
  const initialRef = useRef(true);

  useEffect(() => {
    if (initialRef.current) {
      initialRef.current = false;
      return;
    }
    setAnimKey((k) => k + 1);
  }, [page]);

  const PageComponent = PAGE_COMPONENTS[page];
  const isFullHeight = FULL_HEIGHT_PAGES.has(page);

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <div className="noise-overlay" />
      <div className="warm-glow" />

      {/* Header */}
      <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur-sm">
        <div className="mx-auto flex h-12 max-w-[1400px] items-stretch">
          <div className="flex items-center border-r border-border px-5 shrink-0">
            <span className="font-collapse text-xl font-bold tracking-wider uppercase blend-lighter">
              Hermes<br className="hidden sm:inline" /><span className="sm:hidden"> </span>Agent
            </span>
          </div>

          <nav className="flex items-stretch overflow-x-auto scrollbar-none">
            {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => setPage(id)}
                className={`group relative inline-flex items-center gap-1.5 border-r border-border px-4 py-2 font-display text-[0.8rem] tracking-[0.12em] uppercase whitespace-nowrap transition-colors cursor-pointer shrink-0 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring ${
                  page === id
                    ? "text-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                {label}
                <span className="absolute inset-0 bg-foreground pointer-events-none transition-opacity duration-150 group-hover:opacity-5 opacity-0" />
                {page === id && (
                  <span className="absolute bottom-0 left-0 right-0 h-px bg-foreground" />
                )}
              </button>
            ))}
          </nav>

          <div className="ml-auto flex items-center px-4 text-muted-foreground">
            <span className="font-display text-[0.7rem] tracking-[0.15em] uppercase opacity-50">
              Web UI
            </span>
          </div>
        </div>
      </header>

      <main
        key={animKey}
        className={`relative z-2 mx-auto w-full max-w-[1400px] ${isFullHeight ? "flex-1" : "flex-1 px-6 py-8"}`}
        style={isFullHeight ? undefined : { animation: "fade-in 150ms ease-out" }}
      >
        <PageComponent />
      </main>

      <footer className="relative z-2 border-t border-border">
        <div className="mx-auto flex max-w-[1400px] items-center justify-between px-6 py-3">
          <span className="font-display text-[0.8rem] tracking-[0.12em] uppercase opacity-50">
            Hermes Agent
          </span>
          <span className="font-display text-[0.7rem] tracking-[0.15em] uppercase text-foreground/40">
            Nous Research
          </span>
        </div>
      </footer>
    </div>
  );
}
