<?php
/**
 * Plugin Name: WP Ops Connect
 * Description: Minimal REST helpers for the WP-Ops wp-ops MCP: site/post info, safe draft duplicate, Divi cache purge, whitelisted builder + SEO meta, ACF fields, wp_options CRUD (manage_options only), theme file read/write (edit_themes only, via WordPress core's self-reverting editor). No UI, no external calls.
 * Version: 1.4.0
 * Author: the maintainer
 * License: GPL-2.0-or-later
 */
if ( ! defined( 'ABSPATH' ) ) { exit; }

define( 'WP_OPS_CONNECT_VERSION', '1.4.0' );

const WPOPS_META_WHITELIST = array(
    '_et_pb_use_builder', '_et_pb_page_layout', '_et_pb_old_content',
    '_et_pb_post_hide_nav', '_et_pb_side_nav', '_et_pb_built_for_post_type',
    '_seopress_titles_title', '_seopress_titles_desc',
    '_seopress_robots_canonical', '_seopress_robots_index',
    'rank_math_title', 'rank_math_description', 'rank_math_canonical_url',
    'rank_math_robots',
    '_yoast_wpseo_title', '_yoast_wpseo_metadesc',
    '_yoast_wpseo_canonical', '_yoast_wpseo_meta-robots-noindex',
);

function wpops_can() { return current_user_can( 'edit_others_pages' ); }

// Options are site configuration, not content: gate them on WP's own options capability, which is
// strictly higher than the content routes' edit_others_pages (an Editor cannot reach /option).
function wpops_can_admin() { return current_user_can( 'manage_options' ); }

// Theme FILES are executable PHP: a write here runs code on the site. Gate on edit_themes,
// WordPress's own capability for the Theme File Editor, and require that file editing has not
// been switched off (DISALLOW_FILE_EDIT / DISALLOW_FILE_MODS) - if the site owner disabled the
// editor, this route must not become a way around that.
function wpops_can_themes() {
    if ( ! current_user_can( 'edit_themes' ) ) { return false; }
    if ( defined( 'DISALLOW_FILE_EDIT' ) && DISALLOW_FILE_EDIT ) { return false; }
    if ( defined( 'DISALLOW_FILE_MODS' ) && DISALLOW_FILE_MODS ) { return false; }
    return true;
}

// Per-post least-privilege guard: post must exist, be a page/post, and be editable by the caller.
function wpops_editable( $pid ) {
    $p = get_post( $pid );
    if ( ! $p || ! in_array( $p->post_type, array( 'page', 'post' ), true ) ) { return null; }
    if ( ! current_user_can( 'edit_post', $p->ID ) ) { return null; }
    return $p;
}

// permission_callback is repeated per route (not merged from a shared $base) so the
// capability gate is visible on every endpoint and can't be dropped by array semantics.
add_action( 'rest_api_init', function () {
    register_rest_route( 'wpops/v1', '/info', array(
        'methods' => 'GET', 'callback' => 'wpops_info',
        'permission_callback' => 'wpops_can' ) );
    register_rest_route( 'wpops/v1', '/duplicate', array(
        'methods' => 'POST', 'callback' => 'wpops_duplicate',
        'permission_callback' => 'wpops_can' ) );
    register_rest_route( 'wpops/v1', '/purge-cache', array(
        'methods' => 'POST', 'callback' => 'wpops_purge',
        'permission_callback' => 'wpops_can' ) );
    register_rest_route( 'wpops/v1', '/meta', array(
        'methods' => 'GET, POST', 'callback' => 'wpops_meta',
        'permission_callback' => 'wpops_can' ) );
    register_rest_route( 'wpops/v1', '/acf', array(
        'methods' => 'GET, POST', 'callback' => 'wpops_acf',
        'permission_callback' => 'wpops_can' ) );
    register_rest_route( 'wpops/v1', '/option', array(
        'methods' => 'GET, POST, DELETE', 'callback' => 'wpops_option',
        'permission_callback' => 'wpops_can_admin' ) );
    register_rest_route( 'wpops/v1', '/theme-file', array(
        'methods' => 'GET, POST', 'callback' => 'wpops_theme_file',
        'permission_callback' => 'wpops_can_themes' ) );
} );

function wpops_info( WP_REST_Request $req ) {
    $pid = (int) $req->get_param( 'post_id' );
    if ( $pid ) {
        $p = wpops_editable( $pid );
        if ( ! $p ) { return new WP_Error( 'wpops_not_found', 'post not found or not editable', array( 'status' => 404 ) ); }
        return array( 'id' => $p->ID, 'type' => $p->post_type, 'slug' => $p->post_name,
                      'status' => $p->post_status, 'title' => $p->post_title );
    }
    $theme = wp_get_theme();
    $divi  = defined( 'ET_BUILDER_PRODUCT_VERSION' ) ? ET_BUILDER_PRODUCT_VERSION
           : ( ( 'Divi' === $theme->get_template() ) ? $theme->get( 'Version' ) : null );
    $seo   = defined( 'SEOPRESS_VERSION' ) ? 'seopress'
           : ( class_exists( 'RankMath' ) ? 'rank-math'
           : ( defined( 'WPSEO_VERSION' ) ? 'yoast' : null ) );
    $acf   = defined( 'ACF_VERSION' ) ? ACF_VERSION : ( function_exists( 'acf' ) ? true : null );
    return array( 'wp' => get_bloginfo( 'version' ), 'theme' => $theme->get_stylesheet(),
                  'divi' => $divi, 'seo_plugin' => $seo, 'acf' => $acf, 'plugin_version' => WP_OPS_CONNECT_VERSION );
}

function wpops_duplicate( WP_REST_Request $req ) {
    $p = wpops_editable( (int) $req->get_param( 'post_id' ) );
    if ( ! $p ) { return new WP_Error( 'wpops_not_found', 'post not found or not editable', array( 'status' => 404 ) ); }
    $id = wp_insert_post( array(
        'post_type'    => $p->post_type,
        'post_title'   => $p->post_title . ' [wpops draft]',
        'post_name'    => $p->post_name . '-wpops-draft-' . $p->ID,
        'post_status'  => 'draft',
        'post_content' => wp_slash( $p->post_content ),
    ), true );
    if ( is_wp_error( $id ) ) { return $id; }
    foreach ( get_post_meta( $p->ID ) as $k => $vs ) {
        foreach ( $vs as $v ) { add_post_meta( $id, $k, wp_slash( maybe_unserialize( $v ) ) ); }
    }
    return array( 'draft_id' => $id );
}

function wpops_purge( WP_REST_Request $req ) {
    $p = wpops_editable( (int) $req->get_param( 'post_id' ) );
    if ( ! $p ) { return new WP_Error( 'wpops_not_found', 'post not found or not editable', array( 'status' => 404 ) ); }
    if ( class_exists( 'ET_Core_PageResource' )
         && method_exists( 'ET_Core_PageResource', 'remove_static_resources' ) ) {
        ET_Core_PageResource::remove_static_resources( 'all', 'all', false, $p->ID );
    }
    $dir = WP_CONTENT_DIR . '/et-cache/' . $p->ID;
    if ( is_dir( $dir ) ) {
        global $wp_filesystem;
        if ( ! $wp_filesystem ) { require_once ABSPATH . 'wp-admin/includes/file.php'; WP_Filesystem(); }
        if ( $wp_filesystem ) { $wp_filesystem->delete( $dir, true ); }
    }
    clean_post_cache( $p->ID );
    return array( 'purged' => true );
}

function wpops_meta( WP_REST_Request $req ) {
    $p = wpops_editable( (int) $req->get_param( 'post_id' ) );
    if ( ! $p ) { return new WP_Error( 'wpops_not_found', 'post not found or not editable', array( 'status' => 404 ) ); }
    if ( 'GET' === $req->get_method() ) {
        $out = array();
        foreach ( WPOPS_META_WHITELIST as $k ) {
            $v = get_post_meta( $p->ID, $k, true );
            if ( '' !== $v ) { $out[ $k ] = $v; }
        }
        return array( 'meta' => $out );
    }
    $meta = $req->get_param( 'meta' );
    if ( ! is_array( $meta ) ) { return new WP_Error( 'wpops_bad_request', 'meta must be an object', array( 'status' => 400 ) ); }
    $applied = array();
    $skipped = array();
    $robot_directives = array( 'index', 'noindex', 'follow', 'nofollow', 'noarchive',
                               'noimageindex', 'nosnippet' );
    foreach ( $meta as $k => $v ) {
        if ( ! in_array( $k, WPOPS_META_WHITELIST, true ) ) { $skipped[] = $k; continue; }
        if ( 'rank_math_robots' === $k ) {
            if ( ! is_array( $v ) ) { $skipped[] = $k; continue; }
            $clean = array();
            foreach ( $v as $d ) {
                if ( is_string( $d ) && in_array( $d, $robot_directives, true ) ) { $clean[] = $d; }
            }
            if ( count( $clean ) !== count( $v ) ) { $skipped[] = $k; continue; }
            update_post_meta( $p->ID, $k, $clean );
            $applied[] = $k;
            continue;
        }
        if ( ! is_scalar( $v ) ) { $skipped[] = $k; continue; }
        update_post_meta( $p->ID, $k, wp_slash( (string) $v ) );
        $applied[] = $k;
    }
    // `skipped` makes a rejected key visible instead of a silently shorter applied list.
    return array( 'applied' => $applied, 'skipped' => $skipped );
}

// ACF field values via ACF's own API (update_field keeps field-key <-> _meta linkage).
// Same route gate (wpops_can = edit_others_pages) + per-post wpops_editable as /meta.
// Values are arbitrary JSON (repeaters/galleries) so they are NOT whitelisted; the gate is
// the caller's edit_post right on this specific page/post. 501 (not a fatal) when ACF is off.
function wpops_acf( WP_REST_Request $req ) {
    $p = wpops_editable( (int) $req->get_param( 'post_id' ) );
    if ( ! $p ) { return new WP_Error( 'wpops_not_found', 'post not found or not editable', array( 'status' => 404 ) ); }
    if ( ! function_exists( 'get_fields' ) ) { return new WP_Error( 'wpops_acf_inactive', 'ACF not active', array( 'status' => 501 ) ); }
    if ( 'GET' === $req->get_method() ) {
        $f = get_fields( $p->ID );
        return array( 'acf' => $f ? $f : new stdClass() );
    }
    $fields = $req->get_param( 'fields' );
    if ( ! is_array( $fields ) ) { return new WP_Error( 'wpops_bad_request', 'fields must be an object', array( 'status' => 400 ) ); }
    // Reject leading-underscore selectors: update_field falls through to raw update_metadata for an
    // unregistered selector, letting a caller clobber protected core meta. Real ACF names never lead with '_'.
    $applied = array();
    $skipped = array();
    foreach ( $fields as $k => $v ) {
        if ( strpos( (string) $k, '_' ) === 0 ) { $skipped[] = $k; continue; }
        update_field( $k, $v, $p->ID );
        $applied[] = $k;
    }
    $out = get_fields( $p->ID );
    return array( 'applied' => $applied, 'skipped' => $skipped, 'acf' => $out ? $out : new stdClass() );
}

// Arbitrary wp_options CRUD (GET list/read, POST write, DELETE remove) on one route, gated by
// wpops_can_admin = manage_options. Deliberately NOT allowlisted: the contract is "exactly what
// an administrator can already do on the Options screens", including siteurl/home. Operational
// safety (preview-by-default, prod gate) is the MCP layer's job, not a second capability check here.
function wpops_option( WP_REST_Request $req ) {
    $method = $req->get_method();
    $name   = $req->get_param( 'name' );
    // Listing is autoloaded names ONLY (wp_load_alloptions); non-autoloaded options stay gettable
    // by name. Names only - values are never bulk-dumped.
    if ( 'GET' === $method && null === $name ) {
        return array( 'names' => array_keys( wp_load_alloptions() ) );
    }
    if ( ! is_string( $name ) || '' === $name ) {
        return new WP_Error( 'wpops_bad_request', 'name must be a non-empty string', array( 'status' => 400 ) );
    }
    if ( 'DELETE' === $method ) {
        return array( 'name' => $name, 'deleted' => (bool) delete_option( $name ) );
    }
    if ( 'POST' === $method ) {
        // update_option() returns false when the stored value is unchanged, which is NOT a failure,
        // so ignore its return and report the value actually stored (re-read).
        update_option( $name, $req->get_param( 'value' ) );
        return array( 'name' => $name, 'value' => get_option( $name ), 'updated' => true );
    }
    // Sentinel default: an option may legitimately hold false/''/0, which a null/false default
    // could not be told apart from "not set".
    $sentinel = '__wpops_absent__';
    $v = get_option( $name, $sentinel );
    return array( 'name' => $name, 'value' => ( $v === $sentinel ? null : $v ),
                  'exists' => ( $v !== $sentinel ) );
}

/**
 * Theme file read/write. GET returns a file's contents; POST writes it.
 *
 * Writing a theme file executes code on the site, so this leans on WordPress core rather
 * than rolling our own writer: wp_edit_theme_plugin_file() validates the path, writes,
 * then makes a LOOPBACK request to the site and AUTOMATICALLY REVERTS the file if the
 * site fatals. That self-revert is the whole reason to route through core - a hand-rolled
 * file_put_contents() can white-screen a site with no way back in over REST.
 *
 * Extra guards on top of core:
 *  - child/active theme only by default; the parent theme needs an explicit flag
 *  - extension allowlist (no writing .htaccess, .ini, arbitrary binaries)
 *  - realpath containment, so ../../ cannot escape the theme directory
 *  - the previous contents are always returned, so the caller can revert deliberately
 */
function wpops_theme_file( WP_REST_Request $req ) {
    $rel   = $req->get_param( 'file' );
    $theme = $req->get_param( 'theme' );

    if ( ! is_string( $rel ) || '' === $rel ) {
        return new WP_Error( 'wpops_bad_request', 'file must be a non-empty relative path',
                             array( 'status' => 400 ) );
    }
    // Reject traversal and absolute paths before touching the filesystem.
    if ( strpos( $rel, "\0" ) !== false || preg_match( '#(^/|^[A-Za-z]:|\.\.)#', $rel ) ) {
        return new WP_Error( 'wpops_bad_path', 'file must be a relative path inside the theme, with no ".."',
                             array( 'status' => 400 ) );
    }
    $allowed_ext = array( 'php', 'css', 'js', 'json', 'txt', 'html', 'svg', 'md' );
    $ext = strtolower( pathinfo( $rel, PATHINFO_EXTENSION ) );
    if ( ! in_array( $ext, $allowed_ext, true ) ) {
        return new WP_Error( 'wpops_bad_ext', 'extension not allowed: ' . $ext,
                             array( 'status' => 400, 'allowed' => $allowed_ext ) );
    }

    $stylesheet = is_string( $theme ) && '' !== $theme ? $theme : get_stylesheet();
    $theme_obj  = wp_get_theme( $stylesheet );
    if ( ! $theme_obj->exists() ) {
        return new WP_Error( 'wpops_no_theme', 'theme not found: ' . $stylesheet, array( 'status' => 404 ) );
    }
    // Default to the ACTIVE (child) theme. Editing a parent theme is how upgrades get lost,
    // so it must be asked for explicitly.
    if ( $stylesheet !== get_stylesheet() && ! $req->get_param( 'allow_other_theme' ) ) {
        return new WP_Error( 'wpops_other_theme',
            'refusing to touch a non-active theme without allow_other_theme=true',
            array( 'status' => 400, 'active_theme' => get_stylesheet() ) );
    }

    $root = wp_normalize_path( $theme_obj->get_stylesheet_directory() );
    $path = wp_normalize_path( $root . '/' . ltrim( $rel, '/' ) );
    $real_root = realpath( $root );
    $real_dir  = realpath( dirname( $path ) );
    if ( false === $real_root || false === $real_dir
         || strpos( wp_normalize_path( $real_dir ), wp_normalize_path( $real_root ) ) !== 0 ) {
        return new WP_Error( 'wpops_outside_theme', 'resolved path is outside the theme directory',
                             array( 'status' => 400 ) );
    }

    $exists = file_exists( $path );
    $before = $exists ? file_get_contents( $path ) : null;

    if ( 'GET' === $req->get_method() ) {
        if ( ! $exists ) {
            return new WP_Error( 'wpops_no_file', 'file not found: ' . $rel, array( 'status' => 404 ) );
        }
        return array(
            'theme'    => $stylesheet,
            'file'     => $rel,
            'contents' => $before,
            'bytes'    => strlen( $before ),
            'writable' => is_writable( $path ),
        );
    }

    // ---- POST (write) -------------------------------------------------------
    $contents = $req->get_param( 'contents' );
    if ( ! is_string( $contents ) ) {
        return new WP_Error( 'wpops_bad_request', 'contents must be a string', array( 'status' => 400 ) );
    }
    // Creating files is NOT supported, and allow_create cannot change that: core's
    // wp_edit_theme_plugin_file() only edits files already registered in the theme and
    // answers "Sorry, that file cannot be edited." for anything else (verified live on
    // examplestg 2026-08-31). Writing a new file directly would mean giving up the
    // loopback fatal-check and auto-revert that make this route safe at all, so we say
    // no plainly instead of half-doing it.
    if ( ! $exists ) {
        return new WP_Error( 'wpops_no_file',
            'file does not exist. Creating theme files is not supported: the WordPress core '
            . 'self-reverting editor only edits existing theme files. Add the file via '
            . 'deploy/SFTP, then edit it here.',
            array( 'status' => 404 ) );
    }

    require_once ABSPATH . 'wp-admin/includes/file.php';
    // Core validates, writes, loopback-checks for a fatal, and reverts on failure.
    $result = wp_edit_theme_plugin_file( array(
        'file'      => $rel,
        'theme'     => $stylesheet,
        'newcontent'=> $contents,
        'nonce'     => wp_create_nonce( 'edit-theme_' . $stylesheet . '_' . $rel ),
    ) );

    if ( is_wp_error( $result ) ) {
        // Core already reverted the file. Report why, and confirm what is on disk now.
        $now = file_exists( $path ) ? file_get_contents( $path ) : null;
        return new WP_Error( 'wpops_write_failed', $result->get_error_message(), array(
            'status'          => 400,
            'code'            => $result->get_error_code(),
            'reverted'        => ( $now === $before ),
            'previous_bytes'  => null === $before ? 0 : strlen( $before ),
        ) );
    }

    $after = file_exists( $path ) ? file_get_contents( $path ) : null;
    return array(
        'theme'          => $stylesheet,
        'file'           => $rel,
        'written'        => true,
        'created'        => ! $exists,
        'bytes'          => null === $after ? 0 : strlen( $after ),
        'verified'       => ( $after === $contents ),   // readback, not a claim
        'previous'       => $before,                     // caller can revert with this
        'previous_bytes' => null === $before ? 0 : strlen( $before ),
    );
}
