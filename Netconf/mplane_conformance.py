"""Build conformance config + remote RPC payloads from M-Plane Excel workbooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mplane_control as mp

MPLANE_REMOTE_RPC_DIR = "/var/tmp/conformance/mplane_rpc"
MPLANE_REMOTE_TEMPLATE_DIR = "/var/tmp/conformance/mplane_templates"


def cc_row_enabled(row: dict[str, Any]) -> bool:
    v = row.get("enabled")
    if v is None:
        return True
    if isinstance(v, bool):
        return bool(v)
    s = str(v).strip().upper()
    if not s:
        return True
    return s in ("ON", "1", "TRUE", "YES", "Y", "O", "ENABLE", "ENABLED")


def _arr_entry(val: Any) -> list[dict[str, str]]:
    return [{"0": "" if val is None else str(val).strip()}]


def _compression_fields(method: str, iq: str) -> tuple[str, str, str]:
    m = (method or "").strip().upper()
    if m in ("NO COMP", "NO_COMPRESSION", "NONE", ""):
        return "NO_COMPRESSION", (iq or "16"), "STATIC"
    if m in ("BFP", "BLOCK_FLOATING_POINT"):
        return "BLOCK_FLOATING_POINT", (iq or "9"), "BLOCK_FLOATING_POINT"
    if m == "EXPONENT" or m.isdigit():
        return "BLOCK_FLOATING_POINT", (iq or "9"), "BLOCK_FLOATING_POINT"
    return method or "BLOCK_FLOATING_POINT", (iq or "9"), "BLOCK_FLOATING_POINT"


def build_uplane_config_from_pdsch(
    pdsch_xml: str,
    merged: dict[str, Any],
    cc_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """JSON uplane-configurations for legacy 31101/31102 template jq paths."""
    ep_names = mp.extract_nth_tag_values(pdsch_xml, "low-level-tx-endpoint", limit=16)
    eaxc_ids = mp.extract_nth_tag_values(pdsch_xml, "eaxc-id", limit=16)
    scs_vals = mp.extract_nth_tag_values(pdsch_xml, "scs", limit=16)
    prb_vals = mp.extract_nth_tag_values(pdsch_xml, "number-of-prb", limit=16)
    iq_vals = mp.extract_nth_tag_values(pdsch_xml, "iq-bitwidth", limit=16)
    comp_vals = mp.extract_nth_tag_values(pdsch_xml, "compression-method", limit=16)
    if not comp_vals:
        comp_vals = mp.extract_nth_tag_values(pdsch_xml, "compression-type", limit=16)
    typ_vals = mp.extract_nth_tag_values(pdsch_xml, "type", limit=16)
    center_hz = mp.extract_nth_tag_values(pdsch_xml, "center-of-channel-bandwidth", limit=16)
    bw_vals = mp.extract_nth_tag_values(pdsch_xml, "channel-bandwidth", limit=16)
    gain_vals = mp.extract_nth_tag_values(pdsch_xml, "gain", limit=16)
    pe_vals = mp.extract_nth_tag_values(pdsch_xml, "processing-element", limit=16)
    tx_car = mp.extract_nth_tag_values(pdsch_xml, "tx-array-carrier", limit=16)
    link_names = mp.extract_nth_tag_values(pdsch_xml, "low-level-tx-links", limit=16)
    if not link_names:
        link_names = [f"link_{i}" for i in range(len(ep_names))]

    n = max(len(ep_names), len(eaxc_ids), 1)
    tx_ep: dict[str, list] = {
        "name": [],
        "endpoint_id": [],
        "compression_method": [],
        "compression_bit": [],
        "compression_type": [],
        "Sub_Carrier_Spacing": [],
        "Number_Of_PRB": [],
    }
    tx_carriers: dict[str, list] = {
        "name": [],
        "Center_frequency": [],
        "BandWidth": [],
        "pattern": [],
        "Gain": [],
        "Tech": [],
    }
    tx_links: dict[str, list] = {
        "name": [],
        "endpoints": [],
        "tx_array_carriers": [],
        "processing_element": [],
    }

    pattern = "TDD" if str(merged.get("frame_structure", merged.get("frame-structure", ""))).upper() == "TDD" else "FDD"
    tech = str(merged.get("tech", merged.get("duplex", "TDD")) or "TDD")

    for i in range(n):
        ep = ep_names[i] if i < len(ep_names) else f"ep_{i}"
        eaxc = eaxc_ids[i] if i < len(eaxc_ids) else str(i)
        scs = scs_vals[i] if i < len(scs_vals) else "30"
        prb = prb_vals[i] if i < len(prb_vals) else "273"
        iq = iq_vals[i] if i < len(iq_vals) else "16"
        comp_m = comp_vals[i] if i < len(comp_vals) else ""
        cmethod, cbit, ctype = _compression_fields(comp_m, iq)
        scs_num = str(scs).upper().replace("KHZ_", "").replace("KHZ", "").strip() or "30"
        mhz = mp.hz_to_mhz_string(center_hz[i]) if i < len(center_hz) else ""
        if not mhz and i < len(cc_rows):
            mhz = str(cc_rows[i].get("dl_mhz") or "").strip()
        bw_mhz = mp.hz_to_mhz_string(bw_vals[i]) if i < len(bw_vals) else ""
        if not bw_mhz and i < len(cc_rows):
            bw_mhz = str(cc_rows[i].get("bw") or "").strip()
        car = tx_car[i] if i < len(tx_car) else f"tx_{i}"
        pe = pe_vals[i] if i < len(pe_vals) else str(merged.get("pe_name") or "")
        gain = gain_vals[i] if i < len(gain_vals) else "0"
        typ = typ_vals[i] if i < len(typ_vals) else "LTE"

        tx_ep["name"].append(_arr_entry(ep))
        tx_ep["endpoint_id"].append(_arr_entry(eaxc))
        tx_ep["compression_method"].append(_arr_entry(cmethod))
        tx_ep["compression_bit"].append(_arr_entry(cbit))
        tx_ep["compression_type"].append(_arr_entry(ctype))
        tx_ep["Sub_Carrier_Spacing"].append(_arr_entry(scs_num))
        tx_ep["Number_Of_PRB"].append(_arr_entry(prb))

        tx_carriers["name"].append(_arr_entry(car))
        tx_carriers["Center_frequency"].append(_arr_entry(mhz or "0"))
        tx_carriers["BandWidth"].append(_arr_entry(bw_mhz or "100"))
        tx_carriers["pattern"].append(_arr_entry(pattern))
        tx_carriers["Gain"].append(_arr_entry(gain))
        tx_carriers["Tech"].append(_arr_entry(tech))

        lname = link_names[i] if i < len(link_names) else f"link_{i}"
        tx_links["name"].append(_arr_entry(lname))
        tx_links["endpoints"].append(_arr_entry(ep))
        tx_links["tx_array_carriers"].append(_arr_entry(car))
        tx_links["processing_element"].append(_arr_entry(pe))

    fs = str(merged.get("frame_structure", merged.get("frame-structure", "TDD")) or "TDD")
    return {
        "frame-structure": fs,
        "cp-type": str(merged.get("cp_type", merged.get("cp-type", "NORMAL")) or "NORMAL"),
        "cp-length": str(merged.get("cp_length", merged.get("cp-length", "160")) or "160"),
        "cp-length-other": str(merged.get("cp_length_other", merged.get("cp-length-other", "144")) or "144"),
        "downlink-radio-frame-offset": str(merged.get("alpha", merged.get("downlink-radio-frame-offset", "0")) or "0"),
        "n-ta-offset": str(merged.get("n_ta", merged.get("n-ta-offset", "0")) or "0"),
        "tdd-pattern": str(merged.get("tdd_pattern", merged.get("tdd-pattern", "")) or ""),
        "number-of-dl-symbol": str(merged.get("num_dl", merged.get("number-of-dl-symbol", "10")) or "10"),
        "number-of-ul-symbol": str(merged.get("num_ul", merged.get("number-of-ul-symbol", "2")) or "2"),
        "tdd-scs": str(merged.get("tdd_scs", merged.get("tdd-scs", "30")) or "30"),
        "tdd-pattern-id": str(merged.get("tdd_pattern_id", merged.get("tdd-pattern-id", "1")) or "1"),
        "PDSCH_configurations": {
            "low-level-tx-endpoints": tx_ep,
            "tx_array_carriers": tx_carriers,
            "low-level-tx-links": tx_links,
        },
    }


@dataclass
class MplaneConformanceBundle:
    rpc: dict[str, str]
    uplane_config: dict[str, Any]
    merged: dict[str, Any] = field(default_factory=dict)
    duplicate_pdsch_rpc: str = ""
    warnings: list[str] = field(default_factory=list)
    remote_files: dict[str, str] = field(default_factory=dict)


def finalize_mplane_conformance_bundle(
    rpc: dict[str, str],
    *,
    merged: dict[str, Any],
    cc_rows: list[dict[str, Any]],
    warnings: list[str] | None = None,
    duplicate_eaxc: bool = False,
    apply_physical_cc: bool = True,
) -> MplaneConformanceBundle:
    """Finalize RPC dict into a conformance upload bundle (strip wrappers, optional CC rows)."""
    warns = list(warnings or [])
    rpc = dict(rpc)

    if apply_physical_cc and cc_rows:
        pdsch, pusch, prach, cc_warns = mp.apply_physical_cc_rows(
            rpc.get("PDSCH", ""),
            rpc.get("PUSCH", ""),
            rpc.get("PRACH", ""),
            cc_rows,
        )
        warns.extend(cc_warns)
        rpc["PDSCH"], rpc["PUSCH"], rpc["PRACH"] = pdsch, pusch, prach

    for name in mp.SEND_ORDER:
        body = (rpc.get(name) or "").strip()
        if body:
            rpc[name] = mp.strip_rpc_wrapper_for_netconf_cli(body)

    pdsch_before_off_rows = (rpc.get("PDSCH") or "").strip()

    duplicate_pdsch = ""
    if duplicate_eaxc:
        dup_full, dw = mp.duplicate_pdsch_eaxc_id(
            pdsch_before_off_rows or (rpc.get("PDSCH") or ""),
            from_index=0,
            to_index=1,
        )
        warns.extend(dw)
        if dup_full.strip():
            duplicate_pdsch = dup_full.strip()

    uplane = build_uplane_config_from_pdsch(rpc.get("PDSCH", ""), merged, cc_rows)

    remote_files: dict[str, str] = {}
    for name in mp.SEND_ORDER:
        body = (rpc.get(name) or "").strip()
        if not body:
            continue
        slug = name.replace(" ", "_")
        remote_files[name] = f"{MPLANE_REMOTE_RPC_DIR}/{slug}.xml"

    if duplicate_pdsch.strip():
        remote_files["PDSCH-duplicate-eaxc"] = f"{MPLANE_REMOTE_RPC_DIR}/PDSCH-duplicate-eaxc.xml"

    return MplaneConformanceBundle(
        rpc=rpc,
        uplane_config=uplane,
        merged=dict(merged),
        duplicate_pdsch_rpc=duplicate_pdsch,
        warnings=warns,
        remote_files=remote_files,
    )


def prepare_mplane_conformance_bundle_from_gui(
    rpc: dict[str, str],
    *,
    merged: dict[str, Any],
    duplicate_eaxc: bool = False,
    warnings: list[str] | None = None,
) -> MplaneConformanceBundle:
    """Build conformance bundle from GUI-built RPC payloads (M-Plane Control tab is source of truth)."""
    return finalize_mplane_conformance_bundle(
        rpc,
        merged=merged,
        cc_rows=[],
        warnings=warnings,
        duplicate_eaxc=duplicate_eaxc,
        apply_physical_cc=False,
    )


def prepare_mplane_conformance_bundle(
    xlsx_path: str | Path,
    *,
    to_du_if_name: str = "",
    to_du_vlan: str = "",
    duplicate_eaxc: bool = False,
) -> MplaneConformanceBundle:
    """Load xlsx and build RPC payloads (same pipeline as GUI Apply)."""
    warnings: list[str] = []
    rpc, baselines, merged, cc_rows, tables, load_warns = mp.load_workbook_payloads(xlsx_path)
    warnings.extend(load_warns)

    rpc = dict(rpc)
    for sheet in ("PDSCH", "PUSCH", "PRACH"):
        rpc[sheet] = mp.uncomment_endpoint_rows((rpc.get(sheet) or ""), sheet)
    rpc["ACTIVE"] = mp.uncomment_active_rows((rpc.get("ACTIVE") or ""))

    live = {k: ("" if v is None else str(v).strip()) for k, v in merged.items()}

    for name in mp.SEND_ORDER:
        body = (rpc.get(name) or "").strip()
        if not body:
            continue
        rpc[name] = mp.apply_global_baselines(body, baselines, live)
        if name == "CUplane-interface":
            rpc[name] = mp.ensure_cuplane_interface_fields(rpc[name], live)
        elif name == "Processing-element":
            rpc[name] = mp.ensure_processing_element_fields(rpc[name], live)

    for sheet in ("PDSCH", "PUSCH", "PRACH"):
        headers, rows = tables.get(sheet, ([], []))
        if not headers or not rows:
            continue
        body = (rpc.get(sheet) or "").strip()
        if not body:
            continue
        if "low-level-tx-endpoints" in body or "low-level-rx-endpoints" in body:
            new_xml, tw = mp.apply_acorn_control_details_to_rpc(body, sheet, headers, rows)
        else:
            new_xml, tw = mp.apply_full_table_to_rpc(body, sheet, headers, rows)
        rpc[sheet] = new_xml
        warnings.extend(tw)

    pusch_body = (rpc.get("PUSCH") or "").strip()
    prach_body = (rpc.get("PRACH") or "").strip()
    if pusch_body and prach_body:
        prach_body, pr_warns = mp.omit_prach_rx_endpoints_present_in_pusch(prach_body, pusch_body)
        rpc["PRACH"] = prach_body
        warnings.extend(pr_warns)

    off_rows = [i + 1 for i, row in enumerate(cc_rows) if not cc_row_enabled(row)]
    if not off_rows:
        flags = merged.get("_detail_cc_on_flags")
        if isinstance(flags, list):
            off_rows = [i + 1 for i, on in enumerate(flags) if not on]

    active_body, act_warns = mp.sync_active_carrier_names_from_tables(
        rpc.get("ACTIVE", ""),
        tables.get("PDSCH", ([], [])),
        tables.get("PUSCH", ([], [])),
    )
    rpc["ACTIVE"] = active_body
    warnings.extend(act_warns)

    pdsch, pusch, prach, cc_warns = mp.apply_physical_cc_rows(
        rpc.get("PDSCH", ""),
        rpc.get("PUSCH", ""),
        rpc.get("PRACH", ""),
        cc_rows,
    )
    warnings.extend(cc_warns)
    rpc["PDSCH"], rpc["PUSCH"], rpc["PRACH"] = pdsch, pusch, prach

    if off_rows:
        for sheet in ("PDSCH", "PUSCH", "PRACH"):
            rpc[sheet] = mp.comment_out_endpoint_rows((rpc.get(sheet) or ""), sheet, off_rows)
        rpc["ACTIVE"] = mp.comment_out_active_rows((rpc.get("ACTIVE") or ""), off_rows)

    return finalize_mplane_conformance_bundle(
        rpc,
        merged=merged,
        cc_rows=cc_rows,
        warnings=warnings,
        duplicate_eaxc=duplicate_eaxc,
        apply_physical_cc=False,
    )


def mplane_config_json_entries(bundle: MplaneConformanceBundle) -> dict[str, Any]:
    """Keys merged into remote ORU --config JSON."""
    files = dict(bundle.remote_files)
    steps = [n for n in mp.SEND_ORDER if n in files]
    out: dict[str, Any] = {
        "mplane-rpc-mode": "xlsx",
        "mplane-rpc-directory": MPLANE_REMOTE_RPC_DIR,
        "mplane-rpc-steps": steps,
        "mplane-rpc-files": files,
        "uplane-configurations": bundle.uplane_config,
    }
    if bundle.duplicate_pdsch_rpc.strip():
        out["mplane-negative-duplicate-pdsch"] = files.get(
            "PDSCH-duplicate-eaxc", f"{MPLANE_REMOTE_RPC_DIR}/PDSCH-duplicate-eaxc.xml"
        )
    return out
