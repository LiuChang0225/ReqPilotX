import {
  ArrowRight,
  ChartNoAxesCombined,
  Check,
  ChevronDown,
  ChevronRight,
  ClipboardCheck,
  Download,
  Ellipsis,
  ExternalLink,
  Eye,
  FileOutput,
  FileSpreadsheet,
  FileText,
  Folder,
  FolderKanban,
  GitBranch,
  House,
  ListTree,
  LockKeyhole,
  Maximize2,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  Plus,
  RefreshCw,
  Search,
  SearchCheck,
  Settings,
  Sparkles,
  Square,
  Sun,
  Trash2,
  Undo2,
  Upload,
  Wrench,
  X,
  type LucideIcon,
  type LucideProps,
} from "lucide-react";

export type IconName =
  | "arrow"
  | "analysis"
  | "check"
  | "chevron"
  | "chevron-down"
  | "close"
  | "document"
  | "download"
  | "evidence"
  | "export"
  | "external"
  | "eye"
  | "folder"
  | "projects"
  | "knowledge"
  | "home"
  | "matrix"
  | "lock"
  | "maximize"
  | "moon"
  | "more"
  | "panel-close"
  | "panel-open"
  | "play"
  | "plus"
  | "refresh"
  | "search"
  | "tree"
  | "requirement"
  | "settings"
  | "spark"
  | "split"
  | "stop"
  | "sun"
  | "trash"
  | "undo"
  | "upload"
  | "wrench";

const icons: Record<IconName, LucideIcon> = {
  arrow: ArrowRight,
  analysis: ChartNoAxesCombined,
  check: Check,
  chevron: ChevronRight,
  "chevron-down": ChevronDown,
  close: X,
  document: FileText,
  download: Download,
  evidence: SearchCheck,
  export: FileOutput,
  external: ExternalLink,
  eye: Eye,
  folder: Folder,
  projects: FolderKanban,
  knowledge: GitBranch,
  home: House,
  matrix: FileSpreadsheet,
  lock: LockKeyhole,
  maximize: Maximize2,
  moon: Moon,
  more: Ellipsis,
  "panel-close": PanelLeftClose,
  "panel-open": PanelLeftOpen,
  play: Play,
  plus: Plus,
  refresh: RefreshCw,
  search: Search,
  tree: ListTree,
  requirement: ClipboardCheck,
  settings: Settings,
  spark: Sparkles,
  split: GitBranch,
  stop: Square,
  sun: Sun,
  trash: Trash2,
  undo: Undo2,
  upload: Upload,
  wrench: Wrench,
};

interface IconProps extends Omit<LucideProps, "name"> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 18, ...props }: IconProps) {
  const Component = icons[name];
  return (
    <Component
      aria-hidden="true"
      focusable="false"
      size={size}
      strokeWidth={1.8}
      {...props}
    />
  );
}
