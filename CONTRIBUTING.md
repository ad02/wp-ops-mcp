# Contributing

## Ground rules

1. **Never commit credentials, client page content, or `sites.json`.** `.gitignore`
   covers them; check `git status` before every commit anyway.
2. **Tests first.** Every fix needs a test that fails before it and passes after.
   `pytest -q` must be green before you open a PR.
3. **Test against staging, never a live client site.** The prod write-guard exists for a
   reason; do not routinely pass `allow_prod=true`.

## Two traps that have already cost us time

- **Test helper name collisions.** Appending a helper (`_gw`, `_write`) whose name
  already exists in that test file silently rebinds it for the whole module and breaks
  unrelated tests. Grep the file before adding a helper.
- **Stubs that are looser than the real thing.** A stub accepting `**kwargs` hid a real
  `TypeError` (`json=` vs `json_body=`) until it hit a live site. Mirror the real
  signature exactly, keyword-only arguments included.

## Verifying a change against a real site

Unit tests cannot catch REST contract drift or PHP errors. For anything touching the
site-side plugin or the REST gateway, test against a staging install and say so in the
PR. The plugin has no PHP linting in CI - run `php -l` on the server before installing.
