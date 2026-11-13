"""
Schema change detection for spreadsheet re-uploads.

When a file is uploaded again under the same name with different content,
its column layout is compared with the version already stored. Anything that
could break saved charts or change answers (a column added, removed, renamed,
retyped or moved, a sheet added or removed) is reported before the old
version is replaced. Row-count changes are reported but are not a schema
change.

Pure functions: no database, easy to test.
"""
from __future__ import annotations

Schema = dict[str, list[tuple[int, str, str]]]   # sheet name -> [(pos, column name, type)]


def _key(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def compare(old: Schema, new: Schema,
            old_rows: dict[str, int] | None = None,
            new_rows: dict[str, int] | None = None) -> dict:
    """
    Returns {"changed": bool, "sheets_added": [...], "sheets_removed": [...],
             "sheets": [{"sheet", "added", "removed", "renamed",
                         "type_changed", "reordered", "rows_before", "rows_after"}]}
    """
    old_rows, new_rows = old_rows or {}, new_rows or {}
    out = {
        "changed": False,
        "sheets_added":   [s for s in new if s not in old],
        "sheets_removed": [s for s in old if s not in new],
        "sheets": [],
    }
    if out["sheets_added"] or out["sheets_removed"]:
        out["changed"] = True

    for sheet in (s for s in new if s in old):
        before = {_key(n): (pos, n, t) for pos, n, t in old[sheet]}
        after  = {_key(n): (pos, n, t) for pos, n, t in new[sheet]}

        removed = [before[k] for k in before if k not in after]
        added   = [after[k] for k in after if k not in before]

        # A removed and an added column in the same position with the same
        # type is most likely a rename.
        renamed = []
        for r in list(removed):
            match = next((a for a in added if a[0] == r[0] and a[2] == r[2]), None)
            if match:
                renamed.append({"from": r[1], "to": match[1], "type": r[2]})
                removed.remove(r)
                added.remove(match)

        type_changed = [
            {"name": after[k][1], "from": before[k][2], "to": after[k][2]}
            for k in before if k in after and before[k][2] != after[k][2]
        ]
        common = [k for k in before if k in after]
        order_before = sorted(common, key=lambda k: before[k][0])
        order_after  = sorted(common, key=lambda k: after[k][0])
        reordered = order_before != order_after

        entry = {
            "sheet": sheet,
            "added":   [{"name": n, "type": t} for _, n, t in added],
            "removed": [{"name": n, "type": t} for _, n, t in removed],
            "renamed": renamed,
            "type_changed": type_changed,
            "reordered": reordered,
            "rows_before": old_rows.get(sheet),
            "rows_after":  new_rows.get(sheet),
        }
        if added or removed or renamed or type_changed or reordered:
            out["changed"] = True
        out["sheets"].append(entry)
    return out


def summary(diff: dict) -> list[str]:
    """Plain-English lines for the upload screen."""
    lines: list[str] = []
    label = lambda s: f'Sheet "{s}"' if s else "The file"
    for s in diff["sheets_added"]:
        lines.append(f"{label(s)} is new.")
    for s in diff["sheets_removed"]:
        lines.append(f"{label(s)} is no longer in the file.")
    for e in diff["sheets"]:
        who = label(e["sheet"])
        for c in e["added"]:
            lines.append(f'{who}: new column "{c["name"]}" ({c["type"]}).')
        for c in e["removed"]:
            lines.append(f'{who}: column "{c["name"]}" was removed.')
        for c in e["renamed"]:
            lines.append(f'{who}: column "{c["from"]}" looks renamed to "{c["to"]}".')
        for c in e["type_changed"]:
            lines.append(f'{who}: column "{c["name"]}" changed from {c["from"]} to {c["to"]}.')
        if e["reordered"]:
            lines.append(f"{who}: columns are in a different order.")
        if (e["rows_before"] is not None and e["rows_after"] is not None
                and e["rows_before"] != e["rows_after"]):
            lines.append(f'{who}: rows {e["rows_before"]:,} → {e["rows_after"]:,}.')
    return lines
