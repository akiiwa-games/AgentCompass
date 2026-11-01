# Third-party notices

## usage-float

`skills/agent-compass/scripts/usage_core.py`, `usage_magpie.py` and the tests derived from `usage-float`'s `test_usage_safety.py`,
`test_claude_accounts.py` and `test_magpie_usage.py` are extracted from [usage-float](https://github.com/akiiwa-games/usage-float)
v1.3.5 (`usage_float.py`, `magpie_usage.py`). The provider fetchers, account discovery and cache writer were moved into the router
so that quota collection no longer depends on the optional Usage Float display plugin. The extracted files keep this notice; changes
are listed in the header of `usage_core.py`.

```text
MIT License

Copyright (c) 2026 usage-float contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
