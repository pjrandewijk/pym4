# pym4

[![CI](https://github.com/pjrandewijk/pym4/actions/workflows/ci.yml/badge.svg)](https://github.com/pjrandewijk/pym4/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pym4?label=PyPI)](https://pypi.org/project/pym4/)

A GNU m4 1.4 compatible macro processor written in pure Python: no compiler,
no `m4.exe`, no dependencies.

pym4 follows GNU m4's expansion model (rescanning, argument collection, quoting,
comments, diversions) and implements the GNU builtins, including `eval`,
`regexp`/`patsubst` with GNU regex semantics, `translit`, `format`, `include`
and `M4PATH`. The shell builtins `syscmd` and `esyscmd` are disabled on purpose.

It was written to run [Circuit Macros](https://ctan.org/pkg/circuit-macros)
without installing GNU m4, and is one part of a pure-Python toolchain:

| Package | Replaces | Role |
|---|---|---|
| **pym4** | GNU m4 | m4 → pic |
| [pppic](https://github.com/pjrandewijk/pppic) | dpic -p | pic → PSTricks |
| [m4cmpy](https://github.com/pjrandewijk/m4cmpy) | m4cm.py | m4 → PDF / PNG / JPG / SVG / EPS |

## Install

```
pip install pym4
```

## Use

Command line (a subset of GNU m4's options):

```
pym4 [-I DIR] [-D NAME[=VALUE]] [-U NAME] [-Q] [-o OUTPUT] [FILE ...]
pym4 -I path/to/circuit_macros examples/circuit.m4
```

Python:

```python
from pym4 import M4Lite, expand

print(expand("define(`greet', `Hello, $1!')greet(`world')"))

m4 = M4Lite(include_dirs=["path/to/circuit_macros"])
pic = m4.expand(open("circuit.m4").read(), "circuit.m4")
```

## Tests

```
python -m unittest discover -s tests -v
```

CI also checks that pym4 gives the same output as GNU m4 for the files in
[examples](examples), including a Circuit Macros diagram.

## Licence

GPL-3.0-or-later; see [LICENSE](LICENSE). pym4 reproduces the behaviour of
GNU m4, Copyright (C) Free Software Foundation, Inc.
