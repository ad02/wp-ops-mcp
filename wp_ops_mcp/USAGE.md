# wp-ops-mcp - How to use it

Plain-English guide: how to set it up, how to add a website, what you can ask it to do,
and what it will refuse. No code required.

---

## 1. What this is

An MCP server that lets Claude (or Cursor/any MCP client) edit your WordPress sites
**over HTTPS only** - no SSH, no server access. It connects to the site the same way a
browser would: the WordPress REST API plus a small helper plugin we wrote.

It can: edit Divi 4 / Divi 5 / Gutenberg pages, publish with rollback, manage SEO fields,
upload media, manage menus, ACF fields, users, settings, taxonomies, comments and plugins.

It cannot: edit theme code or plugin code (that needs file access - deliberately out of
scope), install/update/delete plugins, or touch anything outside WordPress.

---

## 2. One-time setup on your machine

**Add the server to Claude Code.** Run this **from the worktree** so the import path
resolves (`cd D:\the internal workspace\.worktrees\wp-ops-server` first):

```
claude mcp add wp-ops --env PYTHONPATH=D:\the internal workspace\.worktrees\wp-ops-server --env WPOPS_TRANSPORT=rest -- "C:\Users\USER\AppData\Local\Programs\Python\Python312\python.exe" -m wp_ops_mcp.server
```

Notes:
- Use the **full path to Python 3.12** as shown - the plain `python` on PATH is a
  different environment and will fail to import.
- `PYTHONPATH` must point at the worktree root; the server is `wp_ops_mcp.server`
  and it starts on stdio (`mcp.run()`), which is what Claude Code expects.
- `WPOPS_TRANSPORT=rest` keeps it HTTPS-only: no SSH code is even imported. Drop it (or
  set `auto`) if you want the SSH fallback available for sites without credentials.

Check it worked: in a new Claude Code session, ask *"list the wp-ops tools"* - you should
see `wp_list_sites`, `wp_edit_page`, `wp_set_seo`, and ~30 more.

**Environment knobs** (optional, set in your shell or the MCP config):

| Variable | Default | What it does |
|---|---|---|
| `WPOPS_TRANSPORT` | `auto` | `rest` = HTTPS only (recommended, refuses if no credentials). `auto` = REST when credentials exist, else SSH. `wpcli` = force SSH (admin fallback). |
| `WPOPS_CREDENTIALS` | `data/wp_ops_mcp/credentials.json` | Where per-site logins live. |
| `WPOPS_TIMEOUT` | `45` | Seconds before a slow site is given up on. |

---

## 3. How to add a website (do this once per site)

Three steps. Takes about 3 minutes per site.

### Step 1 - Install the helper plugin

The plugin is at:
`wp_ops_mcp/plugin/wp-ops-connect.zip`

In the site's **wp-admin -> Plugins -> Add New -> Upload Plugin** -> choose that zip ->
Install -> **Activate**. (Updating later: upload the new zip and choose
"Replace current with uploaded".)

What the plugin does: four small REST endpoints WordPress core doesn't provide -
site/post info, safe page duplication, Divi cache purge, whitelisted meta, ACF values,
and arbitrary options. It has no admin screens, no settings, no external calls.

### Step 2 - Create the API user

**Users -> Add New**:
- Username: `wpops-mcp`
- Email: `wpops-mcp@example.com`
- Role: **Administrator** (required - a lower role makes WordPress strip iframes and
  scripts out of any content you save)

Then open that user's profile -> scroll to **Application Passwords** -> name it
`wp-ops-mcp` -> **Add New Application Password** -> copy the generated password.

### Step 3 - Save the credentials locally

Add an entry to `data/wp_ops_mcp/credentials.json` (create the file if it does not exist -
it is gitignored and never leaves your machine):

```json
{
  "examplestg": {
    "base_url": "https://examplestg.example.com",
    "username": "wpops-mcp",
    "app_password": "abcd efgh ijkl mnop qrst uvwx"
  },
  "anothersite": {
    "base_url": "https://anothersite.com",
    "username": "wpops-mcp",
    "app_password": "..."
  }
}
```

The key (`examplestg`) is the **WP Engine install name**. Spaces in the password are
fine - they get stripped automatically.

### Step 4 - Confirm it works

Ask Claude: *"discover the site examplestg"*

You should get back the WordPress version, theme, and which builder it found
(e.g. `divi` major `4`). If you get a refusal about credentials, re-check step 3.

---

## 4. Things you can ask for

Plain requests - Claude picks the right tools.

**Looking around**
- "List the pages on examplestg"
- "Show me the structure of the Sculptra page"
- "What plugins are active on examplestg?"
- "What are the SEO title and description on the BBL page?"

**Editing (always previews first)**
- "On examplestg, change the button on the Jet Peel page to say 'Book Now'"
- "Add an FAQ section to the bottom of the Sculptra page"
- "Move the image module to the end of that column"
- "Set the SEO title on the Botox page to 'Botox in Bend, OR | Deschutes Dermatology'"

**Content and media**
- "Create a draft blog post titled X with this content"
- "Upload this image and put it in the hero section"
- "Add the new Fillers page to the main navigation"

**Admin**
- "List the users on examplestg"
- "What is the blogname option set to?"
- "Show me the categories and how many posts each has"

**A normal editing flow looks like this:**
1. You ask for a change -> it shows you a **preview** (nothing written)
2. You say go -> it creates a **draft copy** of the page and edits that; the live page is
   untouched
3. You review the draft in wp-admin (it hands you the draft's name and ID)
4. You say publish -> it swaps the draft into the live page and keeps a WordPress revision
   so it can be rolled back

---

## 4b. The full tool list (43)

You never need to name these - ask in plain English and Claude picks. Listed so you know
the surface.

| Area | Tools |
|---|---|
| **Sites** | `wp_list_sites`, `wp_site_health`, `wp_discover_site` |
| **Read content** | `wp_get_content`, `wp_find_page`, `wp_extract_page` |
| **Edit content** | `wp_create_content`, `wp_edit_page`, `wp_publish_swap`, `wp_discard_draft` |
| **SEO** | `wp_get_seo`, `wp_set_seo` |
| **Media** | `wp_upload_media` |
| **Menus** | `wp_list_menus`, `wp_add_menu_item`, `wp_update_menu_item`, `wp_remove_menu_item` |
| **URLs / settings** | `wp_change_slug`, `wp_get_settings`, `wp_set_setting` |
| **ACF** | `wp_get_acf`, `wp_set_acf` |
| **Users** | `wp_list_users`, `wp_get_user`, `wp_create_user`, `wp_update_user`, `wp_delete_user` |
| **Options (any wp_option)** | `wp_list_options`, `wp_get_option`, `wp_set_option`, `wp_delete_option` |
| **Comments** | `wp_list_comments`, `wp_moderate_comment`, `wp_delete_comment` |
| **Taxonomies / terms** | `wp_list_taxonomies`, `wp_list_terms`, `wp_create_term`, `wp_update_term`, `wp_delete_term`, `wp_set_post_terms` |
| **Plugins** | `wp_list_plugins`, `wp_activate_plugin`, `wp_deactivate_plugin` |

## 5. Safety - what it does automatically

- **Nothing is written on the first try.** Every write tool previews unless you confirm.
- **Live pages are never edited directly.** Edits land on a duplicate; publishing is a
  separate, explicit step.
- **It refuses pages it cannot reproduce perfectly.** Before editing, it checks it can
  rebuild the page byte-for-byte. If not: "this page needs a human." It would rather do
  nothing than risk breaking a layout.
- **It refuses Divi dynamic content** (`@ET-DC@` fields) - the thing that once corrupted
  a client site.
- **Ambiguity stops it.** "Change the button" when there are three buttons gets you a
  refusal listing the candidates, never a guess.
- **It cannot disable itself.** Deactivating the helper plugin is refused - that would
  sever its own connection to the site.
- **Production sites need an explicit flag.** Any site whose install name does not look
  like staging refuses writes unless the request says `allow_prod`.
- **Writes are verified.** After writing, it reads the value back and tells you if the
  site stored something different.

---

## 6. What it will refuse (and why)

| Refusal | Reason |
|---|---|
| "this page needs a human" | The page has markup we cannot reproduce byte-for-byte. Edit it in the builder. |
| "@ET-DC@ dynamic content (read-only)" | Divi dynamic fields; editing them corrupts pages. |
| "2 matches for ... (need exactly 1)" | Be specific, or use the address the extract tool showed you. |
| "builder 'elementor' not editable" | We support Divi 4/5 and Gutenberg only. |
| "requires the REST transport" | That site has no credentials yet - do section 3. |
| "PluginSelfLockout" | You asked it to deactivate its own helper plugin. |
| "wp-ops-connect X < required Y" | The site's plugin is out of date - upload the new zip. |

---

## 7. Known limits (read before trusting it with something important)

- **No activity logging yet.** There is currently no per-write audit trail. This is the
  outstanding item before client sites should be edited by anyone but you.
- **Theme/plugin CODE cannot be edited** - by design (needs file access).
- **Plugins:** can activate/deactivate, cannot install/update/delete.
- **Page slug changes get no automatic redirect** (WordPress only does that for posts) -
  the tool warns you; add a redirect yourself if the page had traffic.
- **Settings:** any option can be written, including dangerous ones like `siteurl`. There
  is no allowlist - the preview step is your safety net.
- **Listings cap at 100 items per page** (1000 max for posts, flagged when truncated).
- **Local only.** This runs on your machine. Hosting it for the team is a later step.

---

## 8. If something goes wrong

- **A write failed halfway:** the result tells you `write_landed: true/false`. If true,
  the site changed even though the check failed - re-read the item to see its real state.
- **A page looks wrong after publishing:** WordPress kept a revision. Ask for the previous
  revision to be restored, or restore it in wp-admin -> the page -> Revisions.
- **A draft is left over:** ask it to discard the draft by ID, or delete it in wp-admin
  (they are named `<page title> [wpops draft]`).
- **Timeouts:** some WordPress admin endpoints are genuinely slow. Raise `WPOPS_TIMEOUT`.

---

## 9. Where things live

| What | Where |
|---|---|
| The server code | `wp_ops_mcp/` |
| The helper plugin (deploy this) | `wp_ops_mcp/plugin/wp-ops-connect.zip` |
| Your site logins | `data/wp_ops_mcp/credentials.json` (gitignored) |
| Status, tool list, limitations | `wp_ops_mcp/README.md` |
| Design specs and build plans | `docs/superpowers/specs/`, `docs/superpowers/plans/` |
