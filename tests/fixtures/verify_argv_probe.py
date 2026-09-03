"""Emit argv as JSON for the PowerShell verification-wrapper compatibility gate."""

from __future__ import annotations

import json
import sys


print(json.dumps(sys.argv[1:], ensure_ascii=False))
