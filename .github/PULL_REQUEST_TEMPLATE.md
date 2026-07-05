## What changed and why

## How you tested it

Which self-tests did you run, and did you add a new case for this change?

```
python -m hopandhaul.<module> --selftest
```

## Checklist

- [ ] `ruff check .` passes
- [ ] Relevant module self-tests pass, and I added a case if I fixed a bug or added behavior
- [ ] No new runtime dependency added (or I explained above why this one is worth it)
- [ ] No secrets committed; any new key reads go through `_secrets.py`
- [ ] If this touches `server.py`: the 127.0.0.1 bind, Host-header allowlist, and static-file
      whitelist are still intact
