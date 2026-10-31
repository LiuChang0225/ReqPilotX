import { AppLink } from "../navigation";
import { ProductLogo } from "./ProductLogo";
import { useUiCopy } from "../../host/HostLocaleContext";

export function Brand({ compact = false }: { compact?: boolean }) {
  const l = useUiCopy();
  return (
    <AppLink
      aria-label={l("shell.productHome", "ReqPilotX 首页")}
      className={`brand-lockup${compact ? " is-compact" : ""}`}
      to="/"
    >
      <ProductLogo variant="lockup" className="brand-logo-lockup" />
      <ProductLogo variant="symbol" className="brand-logo-symbol" />
    </AppLink>
  );
}
