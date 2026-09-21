import schaefflerLogoUrl from "../../assets/schaeffler-logo.svg?url";
import { AppLink } from "../navigation";

export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <AppLink
      aria-label="Schaeffler ReqPilotX"
      className={`brand-lockup${compact ? " is-compact" : ""}`}
      to="/"
    >
      <img
        src={schaefflerLogoUrl}
        alt="Schaeffler"
        width={283}
        height={71}
      />
      <span className="brand-rule" aria-hidden="true" />
      <span className="brand-words">
        <strong>ReqPilotX</strong>
        {!compact && <small>Requirement Intelligence</small>}
      </span>
    </AppLink>
  );
}
