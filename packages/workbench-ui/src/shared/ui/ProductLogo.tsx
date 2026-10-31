import lockupGreenUrl from "../../assets/green-compass-v7/ReqPilotX-lockup-green.svg?url";
import lockupGreenReverseUrl from "../../assets/green-compass-v7/ReqPilotX-lockup-green-reverse.svg?url";
import symbolGreenUrl from "../../assets/green-compass-v7/ReqPilotX-symbol-green.svg?url";
import symbolGreenReverseUrl from "../../assets/green-compass-v7/ReqPilotX-symbol-green-reverse.svg?url";

type ProductLogoVariant = "lockup" | "symbol";

export function ProductLogo({
  variant,
  label,
  className,
}: {
  variant: ProductLogoVariant;
  label?: string;
  className?: string;
}) {
  const [lightUrl, darkUrl] =
    variant === "symbol"
      ? [symbolGreenUrl, symbolGreenReverseUrl]
      : [lockupGreenUrl, lockupGreenReverseUrl];
  const size = variant === "symbol" ? 100 : 369;
  return (
    <span
      className={`product-logo product-logo--${variant}${className ? ` ${className}` : ""}`}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <img
        className="product-logo-light"
        src={lightUrl}
        alt=""
        width={size}
        height={100}
      />
      <img
        className="product-logo-dark"
        src={darkUrl}
        alt=""
        width={size}
        height={100}
      />
    </span>
  );
}
