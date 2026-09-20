from __future__ import annotations

def main() -> int:
    print(
        "Embedded or obfuscated API keys are no longer supported. "
        "Configure the provider through the ReqPilotX runtime setup UI "
        "or an operating-system environment variable."
    )
    return 2

if __name__ == '__main__':
    raise SystemExit(main())
