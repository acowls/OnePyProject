"""Remove template resource records before pymppwriter clones project data.

This adapter is specific to pymppwriter 0.4.3's internal structures. MPP
export remains experimental; other unknown template streams can still contain
data, and Microsoft Project must be used to verify exported files.
"""
from __future__ import annotations

import struct

from pymppwriter import blocks
from pymppwriter.writer import PRJ


def _assemble(rows, record_key, meta_key):
    data = bytearray()
    metas = []
    for row in rows:
        meta = bytearray(row[meta_key])
        if len(meta) < 8:
            raise ValueError("Cannot safely rebuild template resource records")
        struct.pack_into("<I", meta, 4, len(data))
        data.extend(row[record_key])
        metas.append(bytes(meta))
    return bytes(data), metas


def _write_fixed(writer, base, rows, header, header2):
    data, meta = _assemble(rows, "rec", "meta")
    data2, meta2 = _assemble(rows, "rec2", "meta2")
    writer._set(f"{base}/FixedData", data)
    writer._set(f"{base}/FixedMeta", blocks.build_fixed_meta(header, meta, len(data)))
    writer._set(f"{base}/Fixed2Data", data2)
    writer._set(f"{base}/Fixed2Meta", blocks.build_fixed_meta(header2, meta2, len(data2)))


def strip_template_resources(writer) -> None:
    """Retain system records but never clone real resources from the template."""
    kept = []
    removed = []
    for row in writer.rsc_rows:
        record = row["rec"]
        if len(record) > 16 and len(record) >= 4 and struct.unpack_from("<I", record)[0] != 0:
            removed.append(row)
        else:
            kept.append(row)
    if not removed:
        return
    if writer.rsc_proto is None or writer.cal_cols is None:
        raise ValueError("Template resources cannot be cleared safely; select an empty MPP template")

    # The library's resource writer assumes its template has only stubs and the
    # special UID-0 resource. A populated template violates that assumption.
    writer.rsc_rows = kept
    resource_base = f"{PRJ}/TBkndRsc"
    _write_fixed(writer, resource_base, kept, writer.rsc_meta_hdr, writer.rsc_meta2_hdr)
    variables = [(struct.unpack_from("<I", row["rec"], 0)[0], kind, payload)
                 for row in kept if len(row["rec"]) > 16 for kind, payload in row["var"]]
    meta, data = blocks.build_var_blocks(writer.rsc_var_hdr, variables, blocks.RESOURCE_FIELD_HI)
    writer._set(f"{resource_base}/VarMeta", meta)
    writer._set(f"{resource_base}/Var2Data", data)
    for key, count in ((blocks.PROPS_RESOURCE_RECORD_COUNT, len(kept)),
                       (blocks.PROPS_RESOURCE_RECORD_COUNT + 1, len(kept) + 2)):
        if key in writer.props:
            writer.props[key] = struct.pack("<I", count)

    # Resource-specific calendars from the template would otherwise refer to
    # the old resource IDs, including IDs reused by the exported project.
    uid_col, _, resource_col = writer.cal_cols
    calendar_rows = []
    removed_calendar_uids = set()
    retained_calendar_uids = set()
    for row in writer.cal_rows:
        if len(row["rec"]) != 12:
            calendar_rows.append(row)
            continue
        values = struct.unpack("<3i", row["rec"])
        if values[resource_col] > 0:
            removed_calendar_uids.add(values[uid_col])
        else:
            retained_calendar_uids.add(values[uid_col])
            calendar_rows.append(row)
    removed_calendar_uids -= retained_calendar_uids
    writer.cal_rows = calendar_rows
    writer.cal_var_entries = [entry for entry in writer.cal_var_entries if entry[0] not in removed_calendar_uids]
    calendar_base = f"{PRJ}/TBkndCal"
    _write_fixed(writer, calendar_base, calendar_rows, writer.cal_meta_hdr, writer.cal_meta2_hdr)
    meta, data = blocks.build_var_blocks(writer.cal_var_hdr, writer.cal_var_entries, writer.cal_var_hi)
    writer._set(f"{calendar_base}/VarMeta", meta)
    writer._set(f"{calendar_base}/Var2Data", data)
