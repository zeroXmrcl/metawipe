# Contributing

Thanks for helping. A few ground rules keep metawipe small and trustworthy:

- **Standard library only.** No runtime dependencies, ever.
- **No silent half-cleaning.** If a file variant can't be cleaned completely, refuse it with a clear message.
- **Never print metadata values.** `inspect` reports names, sizes and yes/no flags only.
- **Every format change comes with a test** that builds a sample in `tests/samples.py`, injects metadata, wipes it and checks the media payload is unchanged.

Run the tests:

```sh
python -m unittest discover -s tests -t . -v
```

Bug reports are most useful with `metawipe inspect --json FILE` output (it contains no metadata values) and, if you can share it, a small sample file that does not contain personal data.
