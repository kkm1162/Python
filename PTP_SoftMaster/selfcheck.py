# Soft PTP Master — quick self-check (no NIC required)

from ptp_codec import (
    FLAG_FREQ_TRACEABLE,
    FLAG_TIME_TRACEABLE,
    FLAG_UTC_OFFSET_VALID,
    MSG_ANNOUNCE,
    MSG_DELAY_REQ,
    MSG_FOLLOW_UP,
    MSG_SYNC,
    PtpHeader,
    Timestamp,
    build_announce,
    build_delay_resp,
    build_follow_up,
    build_sync,
    parse_header,
    unpack_timestamp,
)
from soft_master import (
    MasterConfig,
    build_l2_frame,
    interval_to_log_message_interval,
    rate_per_sec_to_interval,
)


def main() -> None:
    hdr = PtpHeader(
        message_type=MSG_SYNC,
        domain=24,
        source_clock_id=b"\x02\x00\x00\xff\xfe\x00\x00\x01",
        sequence_id=7,
    )
    t1 = Timestamp(1_700_000_000, 123456789)
    sync = build_sync(hdr, Timestamp(0, 0), two_step=True)
    fu = build_follow_up(hdr, t1)
    assert parse_header(sync).message_type == MSG_SYNC
    assert parse_header(fu).message_type == MSG_FOLLOW_UP
    assert unpack_timestamp(fu, 34).nanoseconds == 123456789

    dreq_hdr = PtpHeader(
        message_type=MSG_DELAY_REQ,
        domain=24,
        source_clock_id=b"\xaa\xbb\xcc\xff\xfe\x11\x22\x33",
        source_port=1,
        sequence_id=7,
    )
    t4 = t1.add_ns(50_000)
    resp = build_delay_resp(hdr, t4, dreq_hdr.source_clock_id, dreq_hdr.source_port)
    rh = parse_header(resp)
    assert rh.message_type == 0x9
    assert unpack_timestamp(resp, 34).nanoseconds == t4.nanoseconds

    ann = build_announce(
        hdr,
        current_utc_offset=37,
        priority1=128,
        priority2=255,
        clock_class=6,
        clock_accuracy=0x21,
        time_source=0xA0,
        time_traceable=True,
        freq_traceable=True,
    )
    ah = parse_header(ann)
    assert ah.message_type == MSG_ANNOUNCE
    assert len(ann) == 64
    assert ah.flags & FLAG_UTC_OFFSET_VALID
    assert ah.flags & FLAG_TIME_TRACEABLE
    assert ah.flags & FLAG_FREQ_TRACEABLE
    # Announce body after 34 hdr + 10 origin + 2 utc + 1 reserved
    assert ann[47] == 128  # priority1
    assert ann[48] == 6  # clockClass
    assert ann[49] == 0x21  # clockAccuracy

    assert abs(rate_per_sec_to_interval(32.0) - (1.0 / 32.0)) < 1e-12
    assert interval_to_log_message_interval(1.0 / 8.0) == -3  # log2(1/8)

    cfg = MasterConfig(domain=24, sync_per_sec=32, announce_per_sec=8, two_step=False)
    assert cfg.two_step is False
    assert abs(cfg.sync_interval_s - 1.0 / 32.0) < 1e-12
    assert cfg.effective_dst_mac() == "01:1b:19:00:00:00"
    cfg.use_link_local_mcast = True
    assert cfg.effective_dst_mac() == "01:80:c2:00:00:0e"

    d = cfg.to_dict()
    cfg2 = MasterConfig.from_dict(d)
    assert cfg2.domain == 24
    assert cfg2.use_link_local_mcast is True

    # Sync PTP body 44B + Eth 14B = 58B → must pad to 60B
    sync_body = build_sync(hdr, t1, two_step=False)
    frame = build_l2_frame("01:1b:19:00:00:00", "02:00:00:00:00:01", sync_body)
    assert len(frame) >= 60
    assert frame[12:14] == b"\x88\xf7"

    print("ptp_codec / soft_master self-check OK")


if __name__ == "__main__":
    main()
