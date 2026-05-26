from __future__ import annotations

import base64
import sys

def obfuscate(plain: str) -> str:
    # Simple XOR with key 42, then base64 encode
    xor_bytes = bytes([b ^ 42 for b in plain.encode('utf-8')])
    return base64.b64encode(xor_bytes).decode('utf-8')

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python obfuscate_key.py <YOUR_DASHSCOPE_API_KEY>")
        sys.exit(1)
        
    plain_key = sys.argv[1].strip()
    cipher = obfuscate(plain_key)
    print("\n--- Obfuscated Key Generated ---")
    print(f"Obfuscated String:\n{cipher}")
    print("\nPaste this string into the OB_DASHSCOPE_API_KEY variable in prototype/app_desktop.py.")
    print("--------------------------------\n")

if __name__ == '__main__':
    main()
