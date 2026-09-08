"""ACF (Advanced Custom Fields) value get/set over a REST content gateway.

Unlike SEO meta, ACF values are arbitrary JSON - a string headline, an int counter, a
bool toggle, a list for a repeater/gallery - so nothing is coerced to str on the way in
or out; the plugin writes each field through ACF's own ``update_field`` (correct
field-key linkage) and re-reads with ``get_fields``. This module is the thin
orchestration layer: it presence-gates ACF, previews on dry-run, and verifies a real
write by reading the values back - the same never-raises contract as SeoOps/SiteOps
(every method returns ``{"action": ...}``, so a bad plugin/write is a value the MCP
tool returns, not an exception to unwind).

The one wrinkle is verification. ACF NORMALIZES values as they round-trip through post
meta: a true/false field written as ``True`` reads back as ``1`` (or ``""`` for false),
a number written as an int can read back as its string form, and the ints in a
relationship/gallery array can come back as strings. A naive ``==`` would then flag a
perfectly-landed write as a mismatch. ``_acf_equal`` absorbs exactly that storage
normalization so ``verified: true`` stays trustworthy instead of brittle.
"""
from __future__ import annotations


class AcfError(RuntimeError):
    """Refused ACF write (fields not an object, or no fields provided)."""


_MISSING = object()
"""Sentinel for "this key is absent from the readback".

NOT None: ``_acf_equal`` compares bools by truthiness, so a key missing from the
readback would fetch as ``None`` and ``_acf_equal(None, False)`` would be True -
a silently-dropped write of literal ``False`` would verify as landed. Absence is a
distinct outcome from any value, so it gets a distinct marker.
"""


def _acf_equal(a, b) -> bool:
    """Tolerant equality that absorbs ACF's meta-storage normalization.

    Applied in order, so the first matching rule wins:

      1. **bool vs anything** - compared by truthiness, with ``0``/``""`` (and
         ``False``) falsey and ``1`` (and ``True``) truthy. This makes ``True == 1``
         and ``False == 0 == ""`` verify - ACF stores a true/false field as 1/0. It is
         checked FIRST because ``bool`` subclasses ``int``: without this branch ``True``
         would fall into the scalar rule and ``str(True) == "True" != "1"`` would wrongly
         mismatch.
      2. **both scalars** (str/int/float, neither a bool) - compared as ``str(a) ==
         str(b)``, so ``5 == "5"`` verifies without coercing types either way.
      3. **both lists** - equal length and ``_acf_equal`` element-by-element (repeaters,
         galleries, relationship arrays whose ids may read back as strings).
      4. **both dicts** - identical key sets and ``_acf_equal`` per value (ACF groups
         and repeater rows).
      5. **otherwise** - plain ``==`` (covers ``None``, and any genuinely mixed types
         like a list vs a dict, which are not equal).

    Truthiness in rule 1 is deliberately broad (any non-empty value is truthy); in
    practice one side of a bool comparison is a real ``bool`` written by the caller and
    the other is ACF's 1/0/"" storage form, so the surprising cases (``2 == True``)
    don't arise from a real field round-trip.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (str, int, float)) and isinstance(b, (str, int, float)):
        return str(a) == str(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_acf_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_acf_equal(a[k], b[k]) for k in a)
    return a == b


class AcfOps:
    """Orchestrates ACF value get/set over a REST content gateway.

    The gateway must expose ``ensure_acf_capable``, ``get_acf_fields`` and
    ``set_acf_fields`` (RestContentGateway does; the SSH/wpcli gateway does not - the
    tool layer refuses that transport before getting here). The constructor does NOT
    probe capability: every method surfaces errors as ``{"action": "error", ...}`` and
    NEVER raises to the caller.

    ``set`` verifies a real write by reading each written value back through
    ``_acf_equal``. Two carry-overs make the result honest rather than optimistic:
      - **skipped** - the plugin refuses leading-underscore selectors (they'd address a
        field's ``_meta`` key, not the field) and reports them under ``skipped``. Those
        keys are excluded from the readback check (they were never written); the write
        still succeeds but comes back as ``action: "partial"`` (not ``"applied"``) with
        ``skipped`` plus a ``warning`` naming them, so a partly-applied write is never
        mistaken for a clean one at a glance.
      - **AcfReadbackMismatch** - any written (non-skipped) key that is ABSENT from the
        readback, or present with a value that is not ``_acf_equal`` to what we sent,
        fails the whole set, naming the field and both values. Absence is checked with a
        sentinel rather than ``acf.get(k)``, because a defaulted ``None`` would compare
        equal to a written ``False`` under the bool rule and let a dropped write pass.
    """

    def __init__(self, gateway):
        self.gw = gateway

    async def get(self, post_id: int) -> dict:
        try:
            await self.gw.ensure_acf_capable()
            acf = await self.gw.get_acf_fields(post_id)
            return {"action": "ok", "acf": acf}
        except Exception as e:
            # BROAD except (not RestError-only): a malformed 200 that trips a KeyError
            # inside the gateway must become an error dict, not cross the tool boundary.
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

    async def set(self, post_id: int, fields: dict, dry_run: bool = True) -> dict:
        try:
            if not isinstance(fields, dict):
                raise AcfError("fields must be an object")
            if not fields:
                raise AcfError("no ACF fields provided")
            await self.gw.ensure_acf_capable()
            if dry_run:
                return {"action": "preview", "fields": dict(fields)}
            result = await self.gw.set_acf_fields(post_id, fields)
            acf, skipped = result["acf"], result["skipped"]
            # Readback: verify every field we intended to write EXCEPT the ones the
            # plugin skipped (those were never written, so absent-from-acf is expected).
            for k, v in fields.items():
                if k in skipped:
                    continue
                got = acf.get(k, _MISSING)
                if got is _MISSING or not _acf_equal(got, v):
                    shown = "<absent>" if got is _MISSING else repr(got)
                    return {"action": "error",
                            "error": f"AcfReadbackMismatch: {k} wrote {v!r} read {shown}"}
            out = {"action": "partial" if skipped else "applied",
                   "applied": result["applied"],
                   "skipped": skipped, "verified": True, "acf": acf}
            if skipped:
                out["warning"] = (f"{len(skipped)} field(s) skipped (leading-underscore "
                                  f"selectors rejected): {skipped}")
            return out
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}
