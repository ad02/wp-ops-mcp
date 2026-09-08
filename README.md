# wp-ops-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant read and edit
**WordPress** sites over the REST API - including **Divi 4** (shortcode-based),
**Divi 5** (block-based) and **Gutenberg** page content.

Works with any MCP client: Claude Code, Claude Desktop, Cursor, OpenCode.

**45 tools · 1014 tests · HTTPS only, no SSH**

---

## Why this is interesting

Letting an LLM edit a page builder's content is mostly a *safety* problem. Divi 4 stores
layouts as deeply nested shortcodes and Divi 5 as block comments; both are easy to parse
approximately and very easy to corrupt. This server is built around not corrupting them.

**The round-trip gate.** Content is parsed to a tree and re-serialized *before* any edit
is attempted. If the output is not byte-identical to the input, the tool **refuses the
page entirely**. The parser is not trusted to be complete - it is only trusted to be
honest about what it fully understands.

**Edits never touch the live page.** `wp_edit_page` duplicates the page to a draft, edits
the draft, and verifies by reading back. A separate `wp_publish_swap` promotes it, and
WordPress keeps a revision. A failed edit leaves the live page untouched.

**Writes are preview-first.** Every mutating tool defaults to `dry_run=true` and returns
what *would* change. Production sites additionally require an explicit `allow_prod=true`.

**Theme file writes route through WordPress core.** `wp_edit_theme_plugin_file()`
loopback-requests the site after writing and restores the file if the site fatals. This
was verified by deliberately writing broken PHP to a live staging site: the tool errored,
the file reverted byte-identically, and the site stayed up.

## What it can do

| Area | Tools |
|---|---|
| Content | find/read/extract pages, structural edits, draft duplicate, publish swap, slugs |
| Builders | Divi 4 shortcodes, Divi 5 blocks, Gutenberg - auto-detected per site |
| SEO | title/description/canonical/noindex across SEOPress, Rank Math and Yoast |
| Site | settings, arbitrary options, health, discovery |
| Structure | menus, taxonomies, terms, users, comments, media |
| Advanced | ACF fields, plugin activation, theme file read/write |

## Design notes worth reading

- **A page that cannot be round-tripped is refused, not "best-efforted".** A fleet scan of
  125 real pages found 120 with no builder markup at all - those parse to a single text
  node, so structural ops correctly find nothing to target rather than guessing.
- **The site-side plugin is deliberately tiny** (~350 lines, 7 routes, every one
  capability-gated). Core REST does the heavy lifting; the plugin only adds what core
  cannot do - draft duplication, builder/SEO meta, ACF, options, cache purge, theme files.
- **Two independent verification nets on writes**: the plugin reports which keys it
  applied *and* the value is re-read from the site. A partial apply is an error, not a
  silent success.
- **Environment is explicit, not guessed.** Deriving "is this production?" from the
  install name misread real staging installs, so an explicit value always wins.

## Setup

```bash
git clone https://github.com/<you>/wp-ops-mcp && cd wp-ops-mcp
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
```

1. Zip `wp_ops_mcp/plugin/wp-ops-connect/` and install it on the target site.
2. Create an administrator user there and generate an **Application Password**
   (administrator is required for `unfiltered_html`, or WordPress strips iframes and
   inline styles from content you write).
3. Copy `data/wp_ops_mcp/credentials.example.json` to `credentials.json` and fill it in.
4. Register the server:

```bash
claude mcp add wp-ops \
  --env WPOPS_TRANSPORT=rest \
  --env WPOPS_CREDENTIALS=/abs/path/to/data/wp_ops_mcp/credentials.json \
  -- python -m wp_ops_mcp.server
```

Docker: `docker build -t wp-ops-mcp . && docker run --rm -i -v /abs/creds:/creds:ro wp-ops-mcp`

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `WPOPS_TRANSPORT` | `auto` | `rest` = HTTPS only (recommended) |
| `WPOPS_CREDENTIALS` | `data/wp_ops_mcp/credentials.json` | Use an **absolute** path - the default is relative to the working directory, which your MCP client chooses |
| `WPOPS_TIMEOUT` | `45` | Seconds. Divi 5 pages are large; raise it if you see timeouts |
| `WPOPS_USER_AGENT` | `WP-Ops-MCP/1.0` | Override per site if a CDN rule blocks or redirects it |

## Known limits

- **Classic (non-builder) pages are append-only** - they round-trip safely but expose no
  addressable elements, so only insert operations apply.
- **Theme files cannot be created**, only edited - core's self-reverting editor only
  handles files a theme already registers.
- **Rank Math `noindex` writes the whole robots array**, so clearing it drops any
  `nofollow`/`noarchive` on that page.
- **No plugin/theme updates, cache purge, or snapshot/rollback yet.** Cache purge is the
  most useful missing piece: a correct write can look like a failure while a host serves
  a cached page.

## Tests

`pytest -q` - 1014 pass. Two round-trip regression tests skip because their fixtures are
real site content and are not distributed.

Unit tests do not catch REST contract drift or PHP errors; anything touching the
site-side plugin needs a live staging test. See `CONTRIBUTING.md`.

## Licence

MIT.
