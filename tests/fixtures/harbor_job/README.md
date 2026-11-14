# Harbor job fixtures

Source runs: `typo-full-major-k1-20261111T175544242789Z`, `typo-full-major-k1-20261111T202745550075Z`, and the live `fix-git-repro-20261114T230717751179Z` machine-check repro. Harbor 0.23.0. Approved 12 and 14 Nov 2026. redacted: api_key values and absolute host paths; trimmed: fields not read by the reader and the oversized setup exception log body.

The committed trajectory is the redacted pass sample, structure-trimmed with every step, message, and tool-call entry retained. Free-text values were replaced with `…`; role, type, and tool-name fields were kept. The untrimmed counts were 26 turns and 25 tool calls.

agent.kwargs rewritten to the product's settings shape; all Harbor-owned fields are as Harbor 0.23.0 wrote them. `exception.txt` is not included.

The `setup-failed-fix-git` trial is copied from the real Harbor trial `fix-git__jKxeyVV`. Its exception type and phase structure are unchanged; absolute paths are redacted and the exception message is trimmed to its first line.
