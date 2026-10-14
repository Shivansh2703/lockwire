# Contributing

Thanks for helping. Bug reports and small fixes are welcome without ceremony.

## Setup

```bash
uv sync
uv run pytest -q tests/test_<module>.py   # tests for the module you touched
uv run ruff check src tests               # lint
uv run ruff format src tests              # format (line length 100)
```

Python 3.12 or newer. Add a test that fails without your change.

## Sign off your commits (DCO)

Every commit needs a `Signed-off-by` line, certifying the
[Developer Certificate of Origin 1.1](https://developercertificate.org/):

```bash
git commit -s -m "your message"
```

## Before a big change

Maintainers may ask for changes to some modules to be discussed in an issue first.
Changes to the lockfile schema are breaking changes and are always discussed first.
