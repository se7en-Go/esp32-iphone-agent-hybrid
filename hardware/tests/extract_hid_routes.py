"""Compile the actual sketch HID command handlers with host transport doubles."""
from pathlib import Path

source = Path(__file__).parent.parent / "hid_bridge" / "hid_bridge.ino"
text = source.read_text()
start = text.index("static int8_t clamp8(")
end = text.index("static arb_state arb;")
print(f'#line {text[:start].count(chr(10)) + 1} "{source.resolve()}"')
print(text[start:end])
