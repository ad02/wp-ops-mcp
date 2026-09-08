# wp_ops_mcp - WP-Ops WordPress Operations MCP server

FastMCP server giving AI agents safe, builder-aware access to the WP fleet
(248+ installs across hostacct1-6) over dual transport (WP REST + WP-CLI via WPE SSH).

**Canonical branch: `feat/wp-ops-mcp-server`** in worktree `.worktrees/wp-ops-server`.
Do NOT build this project in the main d:/the internal workspace working dir (history entanglement - see
memory `project_wp_ops_mcp_goal`). Goal doc: `goals/wp-ops-mcp-server.md`.

## Status

| Phase | What | State |
|---|---|---|
| 1 | Registry, transports (rest.py / wpcli.py), health, server + wp_list_sites/wp_site_health | BUILT (eed0f417) |
| 2 | wp_discover_site, builders/detect (Divi4/5/Elementor/Gutenberg), SQLite profiles | BUILT |
| 3 | Builder-aware content: wp_get_content + wp_create_content (Divi4 et_pb + Gutenberg; dry-run default; allow_prod gate; base64+eval-file writes) | BUILT (48f77f63), live-verified dermwellstg |
| 3b | SSH connection pooling, wp_update_content, wp_trash_content, batch runner | TODO |
| 4a | Edit layer: parsers divi4/divi5/gutenberg, core-6 ops, draft-duplicate EditOps, 5 MCP tools (wp_find_page/wp_extract_page/wp_edit_page/wp_publish_swap/wp_discard_draft), corpus harness | code complete + Divi 4 live cycle validated |
| 4b-transport | REST team path: credentials store, authenticated WPRestClient.request, RestContentGateway (full gateway surface), wp-ops-connect plugin 1.0.0 (107 lines, 4 capability-gated endpoints), WPOPS_TRANSPORT selection w/ lazy SSH, bootstrap helper | BUILT + LIVE-VALIDATED on examplestg (REST-only: publish/rollback, kses iframe byte-identical, @ET-DC@ refusal, gutenberg cycle) |
| 5a | SEO meta: adapters (seopress/rank-math/yoast), SeoOps w/ partial-apply + readback nets, wp_get_seo/wp_set_seo, create-time seo, plugin v1.1.0 | BUILT + LIVE-VALIDATED on examplestg (rank-math detected; set/get/refusal cycle green) |
| 5b | Media upload: wp_upload_media (png/jpg/gif/webp, 10MB cap, svg refused, sanitize + alt), image block kind in divi4+gutenberg renderers | BUILT + LIVE-VALIDATED on examplestg (upload/alt/place-in-page/delete 5/5) |
| 5c | Op-set completion: move_element (identity-based, divi structural rules, atomic) + gutenberg insert_section via renderer | BUILT + LIVE-VALIDATED on examplestg (9/9: module move in draft, gb insert+reorder) |
| 5d | Menus: wp_list_menus/wp_add_menu_item/wp_update_menu_item/wp_remove_menu_item (single-match resolution, target validation, readback verify) | BUILT + LIVE-VALIDATED on examplestg (add/rename/remove, menu restored identical) |
| 5e | Settings + slugs: wp_change_slug (honest redirect semantics - WP only 301s published POSTS; page renames orphan old URLs and the tool WARNS), wp_get_settings/wp_set_setting (7-key allowlist, tolerant verify vs WP escaping) | BUILT + LIVE-VALIDATED on examplestg (slug change verified; zero-drift settings write) |
| 5f | REST discovery + pagination: wp_discover_site over /info (no SSH), synthetic Sites for credentialed installs absent from sites.json, list_posts pagination (10-page cap, truncated flag) | BUILT + LIVE-VALIDATED on examplestg (FULL tool layer discover->find->extract->edit-preview, zero SSH) |
| 5i | ACF: plugin v1.2.0 /acf endpoints (get_fields/update_field, leading-underscore reject), gateway + AcfOps (tolerant readback, partial on skipped), wp_get_acf/wp_set_acf | BUILT + LIVE-VALIDATED on examplestg (ACF 6.3.11 + plugin v1.2.0: real banner_* fields written and read back exactly; protected-key guard fired - _wp_page_template skipped -> partial) |
| 5j | Admin CRUD (FULL PARITY): users list/get/create/update/delete, arbitrary wp_options list/get/set/delete via plugin v1.3.0 (manage_options), comment moderation; 12 tools | BUILT + LIVE-VALIDATED on examplestg (users 5/5 incl. delete+reassign; options 11/11 throwaway create/verify/delete/noop; comments read-verified) |
| 5k | Content types: post-type discovery lifts the page/post-only limit (any REST-enabled CPT), taxonomies + terms (list/create/update/delete, assign to post with set-comparison readback); 6 tools | BUILT + LIVE-VALIDATED on examplestg (10/10: term lifecycle, assignment, silent-no-op surfaced as TermAssignmentMismatch, CPT 'project' routed via discovery) |
| 5l | Plugins: wp_list_plugins / wp_activate_plugin / wp_deactivate_plugin (identifier resolution, readback verify, noop path, SELF-LOCKOUT guard refusing to deactivate wp-ops-connect) | BUILT + LIVE-VALIDATED on examplestg (11/11: full toggle cycle restored to starting state; self-lockout refused all identifier forms) |
| 5h | Divi 5 validation (no new code - the shared block parser already served it) | LIVE-VALIDATED on divi5demostg (11/11: real 14KB + 124KB Divi 5 pages round-trip byte-identical; full staged-edit -> publish -> byte-identical rollback cycle) |
| next | 5j admin CRUD (users/options/comments), 5k taxonomies+CPTs, 5l plugins; then 5g logging (pre-prod gate), 5h Divi 5, FINAL hosting |  |

Tests: 989 passing + 1 skipped (TDD) under tests/wp_ops_mcp in this worktree.

## Layout

- `server.py` - FastMCP entrypoint + tools
- `registry.py`, `discover.py`, `health.py` - fleet registry, site discovery, health checks
- `transport/` - rest.py (app-password REST), wpcli.py (WPE SSH gateway; quoting gotchas regression-tested)
- `builders/` - detect.py, per-builder renderers (divi4, gutenberg), edit-layer parsers (divi4_parse, block_parse for divi5+gutenberg), edit_ops.py (op applier); Elementor still raises UnsupportedBuilderError
- `ops/` - content.py (slugify, plan_creates, ContentOps), wpcli_gateway.py, edit.py (draft-duplicate EditOps orchestration), rest_gateway.py (REST gateway), credentials.py (per-install app-password store)
- `verify/` - probe.py (public-URL health probe run after publish_swap)
- `profiles/` - SQLite SiteProfile store (data/wp_ops_mcp/profiles.db, gitignored)
- `scripts/wpops_pull_corpus.py` - pulls real page content into the round-trip corpus harness
- `plugin/wp-ops-connect/` - the site-side micro-plugin (REST helpers; deploy via MainWP per-site on instruction)
- `scripts/wpops_bootstrap_app_password.py` - prints one-time per-site REST bootstrap (runs nothing)
- `docs/pipeline-design.md` - content_publish pipeline draft (Monday -> GDoc -> draft on site)

## Team transport (REST)

- Per-install credentials: `data/wp_ops_mcp/credentials.json` (gitignored) `{install: {base_url, username, app_password}}`; env `WPOPS_CREDENTIALS` overrides the path.
- `WPOPS_TRANSPORT` = `auto` (default; REST when credentials exist, else SSH fallback) | `rest` (REST-only, fail-closed - the cloud deployment mode; SSH code is never imported) | `wpcli` (admin fallback).
- Site setup: install `plugin/wp-ops-connect`, create the app-password user (`scripts/wpops_bootstrap_app_password.py <install>` prints the commands), fill credentials.json.

## Known limitations

- **Activity logging NOT yet wired** - the edit tools do not yet write to the activity-log API. This is required before any production edit (What/Why/Verified per-site trail).
- **No dry_run on wp_publish_swap / wp_discard_draft** - spec deviation. These promote/delete immediately when called (the prod gate still applies). A staged preview exists one step upstream: wp_edit_page dry_run=true shows the change before it is staged onto a draft.
- **Divi 5 note**: validated over the SSH admin transport (the plugin is not installed on divi5demostg); the edit layer is transport-agnostic and REST is separately proven on examplestg. Fleet has no Divi 5 sites yet.
- **Live validation remaining**: Divi 5 publish cycle (no fleet Divi 5 site yet; divi5demostg available), >=15-site round-trip corpus (10 pages on 1 site done), VB open-check sign-off. DONE 2026-07-24 on examplestg via REST-only: Divi 4 publish/rollback byte-identical, kses iframe byte-identical, @ET-DC@ refusal, Gutenberg cycle.
- **Plugin limits**: install / update / delete are deliberately OUT of scope (they pull code onto client sites - that stays with the plugin-whitelist + MainWP process); deactivating wp-ops-connect is refused (it would sever this tool's own control plane); network/multisite activation untested; a plugin toggle interrupted by a network failure can leave the site mid-change - the tool reports `verified` before that point, so the last verified state is the recovery target.
- **Content-type limits**: CPTs must be `show_in_rest` registered; type/taxonomy discovery is cached per gateway instance (a type registered mid-session needs a new instance); term delete is permanent (WP has no trash for terms) and unassigns from every post; assigning a taxonomy a post type does not register returns TermAssignmentMismatch rather than a false success.
- **Slow-endpoint note**: REST default timeout is 45s (WPOPS_TIMEOUT overrides) - live measurement showed /wp/v2/taxonomies?context=edit at 18.6s on a WPE staging.
- **Admin CRUD limits (full-parity, deliberate)**: wp_set_option writes ANY option incl. siteurl/home (no allowlist - blast radius accepted; dry_run + prod-gate are the contract); option LIST returns autoloaded names only (absence from the list is NOT proof an option is absent - get it by name); newly created options are autoloaded; wp_delete_user is permanent and reassigns their content to `reassign` (0 = deleted with them); wp_delete_comment defaults to trash (force=true is permanent).
- **ACF limits**: values only (field GROUP/schema registration out of scope - that's config, not content); ACF-active sites only (clean refusal otherwise); leading-underscore selectors rejected (would let update_field's raw-meta fallback overwrite protected core keys like _wp_page_template); needs plugin v1.2.0.
- **REST discovery limits**: /info v1.1 can't report php_version/plugin_count/multisite/active_theme_version (empty on REST-discovered profiles); Elementor undetectable via /info (REST sites are the Divi/GB fleet); wp_get_content listing has no truncated flag (only wp_find_page does); a site with an exact multiple of 100 posts over-flags truncated (safe direction).
- **Settings/slug limits**: permalink STRUCTURE not REST-exposed (out of scope); page slug renames get NO WP-core redirect (tool warns; add manual redirect if the page had traffic); settings allowlist = title/description/timezone/posts_per_page/show_on_front/page_on_front/page_for_posts; url+email excluded by design.
- **Menu limits**: 100 items/menu readback cap (larger menus risk false readback-miss); nested drag-reorder beyond parent+position not modeled.
- **Op-set limits**: Divi column moves refused by design (layout-width math); et_pb_row_inner effectively immovable; insert_module still divi4-only; multi-node inserts return the FIRST inserted node's address (chain further inserts with position "end", not the returned address).
- **Media limits**: images only (svg refused - script vector), 10MB cap, local file paths as source (remote URLs deferred), REST-transport only.
- **SEO limits**: rank-math noindex unsupported (array-valued robots meta - clean refusal); SEO tools REST-transport only.
- **REST-site discovery gap**: wp_discover_site is still SSH-based; REST-only sites need a profile seeded from the plugin /info endpoint (follow-up) before the MCP tool layer serves them; the gateway/EditOps path works today.
- **Cloud hosting (Stage 2)**: FastMCP HTTP + per-member tokens on Nerv/fleet-VM - NEVER carries SSH keys or the wpcli transport (enforced by lazy imports + fail-closed WPOPS_TRANSPORT).
