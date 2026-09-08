"""WP-CLI implementation of the content gateway (over the WPE SSH gateway).

Reads use `wp post list --format=json`. Creates use a base64 + `wp eval-file` path so
NO post data ever touches the shell command line — this sidesteps every WPE-gateway
quoting pitfall (single-quote mangling, newline collapse) for arbitrary titles/HTML.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import uuid

_SAFE_QUERY = re.compile(r"^[A-Za-z0-9_-]+$")
_ID_RE = re.compile(r"ID:(\d+)")
_ERR_RE = re.compile(r"ERR:(.*)")


def _pid(v) -> int:
    """Coerce a post id to int, rejecting non-integral input.

    int("42") -> 42, 42.0 -> 42; but 42.9, "42.9", "42; rm -rf /" all raise ValueError.
    """
    if isinstance(v, bool):  # bool is an int subclass; never a valid post id
        raise ValueError(f"invalid post id: {v!r}")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f"non-integral post id: {v!r}")
        return int(v)
    return int(v)


def build_list_args(post_type: str, query: str | None = None) -> str:
    # --posts_per_page=100: `wp post list` defaults to -1 (unbounded), which can
    # stream every post on large sites and hang the SSH gateway. Cap it so a find
    # is always bounded work.
    args = (f"post list --post_type={post_type} --post_status=any "
            f"--posts_per_page=100 "
            f"--fields=ID,post_title,post_name,post_status,post_type --format=json")
    if query and _SAFE_QUERY.match(query):
        args += f" --s={query}"
    return args


def _extract_json_array(blob: str) -> list:
    blob = blob.strip()
    start, end = blob.find("["), blob.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(blob[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def parse_post_list(stdout: str) -> list[dict]:
    return [
        {"id": p.get("ID"), "title": p.get("post_title"), "slug": p.get("post_name"),
         "status": p.get("post_status"), "type": p.get("post_type")}
        for p in _extract_json_array(stdout)
    ]


def build_create_command(install: str, post_type: str, title: str, slug: str,
                         status: str, content: str, meta: dict) -> str:
    """One-line, quote-free shell command that creates a post via base64 + eval-file."""
    payload = json.dumps({
        "post_type": post_type, "post_title": title, "post_name": slug,
        "post_status": status, "post_content": content, "meta": meta or {},
    })
    payload_b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    tag = f"create_{slug}_{uuid.uuid4().hex[:8]}"
    json_path = f"/tmp/wpops_{tag}.json"
    php_path = f"/tmp/wpops_{tag}.php"
    # PHP source contains quotes/$, but it is base64-encoded so none reach the shell.
    # wp_slash: wp_insert_post runs wp_unslash on the data, so unslashed backslashes
    # (e.g. Windows paths, regex in shortcodes) would be silently stripped without it.
    php = (
        f"<?php $d=json_decode(file_get_contents({json_path!r}),true);"
        # kses: eval-file runs as user id 0, so current_user_can('unfiltered_html')
        # is false and content_save_pre->wp_filter_post_kses would silently strip
        # iframes/svg/attrs (maps, video embeds) on write. Disable it for byte fidelity.
        "kses_remove_filters();"
        "$id=wp_insert_post(wp_slash(array("
        "'post_type'=>$d['post_type'],'post_title'=>$d['post_title'],"
        "'post_name'=>$d['post_name'],'post_status'=>$d['post_status'],"
        "'post_content'=>$d['post_content'])),true);"
        "if(is_wp_error($id)){echo 'ERR:'.$id->get_error_message();return;}"
        "if(!empty($d['meta'])){foreach($d['meta'] as $k=>$v){update_post_meta($id,$k,$v);}}"
        "echo 'ID:'.$id;"
    )
    php_b64 = base64.b64encode(php.encode("utf-8")).decode("ascii")
    return (
        f"cd ~/sites/{install} 2>/dev/null; "
        f"echo {payload_b64}|base64 -d>{json_path}; "
        f"echo {php_b64}|base64 -d>{php_path}; "
        f"wp eval-file {php_path}; "
        f"rm -f {json_path} {php_path}"
    )


def parse_create_output(stdout: str) -> int:
    err = _ERR_RE.search(stdout)
    if err:
        raise RuntimeError(f"WP create failed: {err.group(1).strip()}")
    m = _ID_RE.search(stdout)
    if not m:
        raise RuntimeError(f"WP create returned no id: {stdout.strip()[:200]!r}")
    return int(m.group(1))


_OK_RE = re.compile(r"OK:(\d+)")
_B64_RE = re.compile(r"B64:([A-Za-z0-9+/=]+)")


def _eval_file_command(install: str, tag: str, php: str,
                       payload_json: str | None = None) -> str:
    """One-line, quote-free eval-file runner (same pattern as build_create_command)."""
    php_b64 = base64.b64encode(php.encode("utf-8")).decode("ascii")
    php_path = f"/tmp/wpops_{tag}.php"
    parts = [f"cd ~/sites/{install} 2>/dev/null"]
    cleanup = [php_path]
    if payload_json is not None:
        json_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
        json_path = f"/tmp/wpops_{tag}.json"
        parts.append(f"echo {json_b64}|base64 -d>{json_path}")
        cleanup.append(json_path)
    parts.append(f"echo {php_b64}|base64 -d>{php_path}")
    parts.append(f"wp eval-file {php_path}")
    parts.append(f"rm -f {' '.join(cleanup)}")
    return "; ".join(parts)


def build_get_content_command(install: str, post_id: int) -> str:
    pid = _pid(post_id)
    # Existence check first, then explicit 'raw' context so no filters/shortcodes
    # rewrite the stored bytes -> exact-bytes contract for round-tripping.
    php = (f"<?php $id={pid};if(!get_post($id)){{echo 'ERR:post not found';return;}}"
           "$c=get_post_field('post_content',$id,'raw');"
           "echo 'B64:'.base64_encode($c);")
    return _eval_file_command(install, f"get{pid}_{uuid.uuid4().hex[:8]}", php)


def parse_get_content_output(stdout: str) -> str:
    err = _ERR_RE.search(stdout)
    if err:
        raise RuntimeError(f"WP get content failed: {err.group(1).strip()}")
    m = _B64_RE.search(stdout)
    if not m:
        raise RuntimeError(f"no B64 marker in output: {stdout.strip()[:200]!r}")
    try:
        return base64.b64decode(m.group(1)).decode("utf-8")
    except (binascii.Error, ValueError) as e:
        raise RuntimeError(f"malformed B64 output: {e}")


_INFO_RE = re.compile(r"INFO:(\{.*\})")


def build_get_post_info_command(install: str, post_id: int) -> str:
    """One-line, quote-free command echoing 'INFO:'.json_encode(name+status).

    Used by the draft-marker guard to confirm a target id is actually a wpops
    draft before a force-delete/overwrite. Same base64 + eval-file pattern as
    build_get_content_command, so no quotes reach the WPE gateway shell.
    """
    pid = _pid(post_id)
    php = (f"<?php $p=get_post({pid});"
           "if(!$p){echo 'ERR:post not found';return;}"
           "echo 'INFO:'.json_encode(array("
           "'name'=>$p->post_name,'status'=>$p->post_status));")
    return _eval_file_command(install, f"info{pid}_{uuid.uuid4().hex[:8]}", php)


def parse_get_post_info_output(stdout: str) -> dict:
    err = _ERR_RE.search(stdout)
    if err:
        raise RuntimeError(f"WP get post info failed: {err.group(1).strip()}")
    m = _INFO_RE.search(stdout)
    if not m:
        raise RuntimeError(f"no INFO marker in output: {stdout.strip()[:200]!r}")
    try:
        return json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"malformed INFO output: {e}")


def build_duplicate_command(install: str, post_id: int) -> str:
    pid = _pid(post_id)
    # wp_slash: wp_insert_post wp_unslashes its input, so copy the source content
    # slashed to avoid corrupting backslashes in the duplicate.
    php = (
        f"<?php $p=get_post({pid});"
        "if(!$p){echo 'ERR:post not found';return;}"
        # kses: see build_create_command; user-0 eval-file context strips
        # iframes/attrs on write, so disable before duplicating existing content.
        "kses_remove_filters();"
        "$id=wp_insert_post(wp_slash(array("
        "'post_type'=>$p->post_type,"
        "'post_title'=>$p->post_title.' [wpops draft]',"
        f"'post_name'=>$p->post_name.'-wpops-draft-{pid}',"
        "'post_status'=>'draft',"
        "'post_content'=>$p->post_content)),true);"
        "if(is_wp_error($id)){echo 'ERR:'.$id->get_error_message();return;}"
        f"$meta=get_post_meta({pid});"
        "foreach($meta as $k=>$vs){foreach($vs as $v){add_post_meta($id,$k,maybe_unserialize($v));}}"
        "echo 'ID:'.$id;"
    )
    return _eval_file_command(install, f"dup{pid}_{uuid.uuid4().hex[:8]}", php)


def build_update_content_command(install: str, post_id: int, content: str) -> str:
    pid = _pid(post_id)
    # Compute the tag ONCE: the PHP's json path and the shell-written json path must
    # match, and both must be unique per call to avoid /tmp collisions on the same id.
    tag = f"upd{pid}_{uuid.uuid4().hex[:8]}"
    json_path = f"/tmp/wpops_{tag}.json"
    payload = json.dumps({"post_content": content})
    php = (
        f"<?php $d=json_decode(file_get_contents({json_path!r}),true);"
        # kses: see build_create_command; user-0 eval-file context strips
        # iframes/attrs on write, so disable before updating content.
        "kses_remove_filters();"
        f"$r=wp_update_post(wp_slash(array('ID'=>{pid},'post_content'=>$d['post_content'])),true);"
        "if(is_wp_error($r)){echo 'ERR:'.$r->get_error_message();return;}"
        "echo 'OK:'.$r;"
    )
    return _eval_file_command(install, tag, php, payload_json=payload)


def parse_update_output(stdout: str) -> int:
    err = _ERR_RE.search(stdout)
    if err:
        raise RuntimeError(f"WP update failed: {err.group(1).strip()}")
    m = _OK_RE.search(stdout)
    if not m:
        raise RuntimeError(f"WP update returned no id: {stdout.strip()[:200]!r}")
    return int(m.group(1))


def build_delete_command(install: str, post_id: int) -> str:
    return f"cd ~/sites/{install} 2>/dev/null; wp post delete {_pid(post_id)} --force"


def build_purge_et_cache_command(install: str, post_id: int) -> str:
    return f"cd ~/sites/{install} 2>/dev/null; rm -rf wp-content/et-cache/{_pid(post_id)}"


class WPCliContentGateway:
    """Content gateway backed by WP-CLI over the WPE SSH gateway."""

    def __init__(self, transport):
        self.t = transport

    async def list_posts(self, post_type: str, query: str | None = None) -> list[dict]:
        res = await self.t.run(build_list_args(post_type, query))
        return parse_post_list(res.stdout)

    async def create_post(self, post_type: str, title: str, slug: str, status: str,
                          content: str, meta: dict) -> int:
        cmd = build_create_command(self.t.install, post_type, title, slug, status,
                                   content, meta)
        res = await self.t.run_raw(cmd)
        return parse_create_output(res.stdout)

    async def get_post_content(self, post_id: int) -> str:
        res = await self.t.run_raw(build_get_content_command(self.t.install, post_id))
        return parse_get_content_output(res.stdout)

    async def get_post_info(self, post_id: int) -> dict:
        res = await self.t.run_raw(build_get_post_info_command(self.t.install, post_id))
        return parse_get_post_info_output(res.stdout)

    async def duplicate_post(self, post_id: int) -> int:
        res = await self.t.run_raw(build_duplicate_command(self.t.install, post_id))
        return parse_create_output(res.stdout)

    async def update_post_content(self, post_id: int, content: str) -> int:
        res = await self.t.run_raw(
            build_update_content_command(self.t.install, post_id, content))
        return parse_update_output(res.stdout)

    async def delete_post(self, post_id: int) -> None:
        res = await self.t.run_raw(build_delete_command(self.t.install, post_id))
        # `wp post delete --force` prints "Success: ..." on success; anything else
        # (e.g. "Warning: post not found") means the delete did not happen.
        if "Success" not in res.stdout:
            raise RuntimeError(
                f"WP delete failed: stdout={res.stdout.strip()[:200]!r} "
                f"stderr={res.stderr.strip()[:200]!r}")

    async def purge_et_cache(self, post_id: int) -> None:
        res = await self.t.run_raw(build_purge_et_cache_command(self.t.install, post_id))
        if not res.success:
            raise RuntimeError(
                f"et-cache purge failed: stdout={res.stdout.strip()[:200]!r} "
                f"stderr={res.stderr.strip()[:200]!r}")
