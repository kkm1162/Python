# -*- coding: utf-8 -*-
"""Scapy PCAP builder script deployed to the remote Linux host."""

PCAP_BUILDER_SCRIPT = r"""
import sys, json, base64, os, struct, random, time
from scapy.all import Ether, Dot1Q, IP, TCP, UDP, Raw, PcapWriter

def random_ip():
    return ".".join(str(random.randint(1, 254)) for _ in range(4))

def build_5_point_pattern(min_size, max_size):
    # 랜덤이 아닌 반복 패턴:
    # min, 25%, 50%, 75%, max 포인트를 순환한다.
    span = max_size - min_size
    if span <= 0:
        return [min_size]

    q1 = min_size + int(span * 0.25)
    q2 = min_size + int(span * 0.50)
    q3 = min_size + int(span * 0.75)
    points = [min_size, q1, q2, q3, max_size]

    # 반올림/정수화 과정에서 중복이 생길 수 있어 정리
    uniq_points = []
    for p in points:
        if p not in uniq_points:
            uniq_points.append(p)
    return uniq_points

def resolve_size_pattern(config):
    raw = config.get('size_pattern')
    if isinstance(raw, list):
        normalized = []
        for v in raw:
            try:
                n = int(v)
            except Exception:
                continue
            if n >= 64 and n not in normalized:
                normalized.append(n)
        if normalized:
            return normalized

    mode = config.get('pkt_mode', 'Fixed')
    if mode == 'Standard Random':
        return build_5_point_pattern(64, 1500)
    elif mode == 'Jumbo Random':
        return build_5_point_pattern(64, 9000)
    return [int(config.get('pkt_size', 64))]

def build_packet(config, pkt_size):
    atype = config.get('attack_type', '').upper()
    src_mac = config.get('src_mac', '00:00:00:00:00:01')
    dst_mac = config.get('dst_mac', 'ff:ff:ff:ff:ff:ff')
    vlan_id = config.get('vlan_id', '')
    has_vlan = bool(vlan_id and str(vlan_id).strip().isdigit())
    l2_len = 18 if has_vlan else 14
    ecpri_payload_len = max(0, pkt_size - l2_len - 4)

    if has_vlan:
        vid = int(str(vlan_id).strip())
        l2_ecpri = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid, type=0xAEFE)
        l2_ptp = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid, type=0x88F7)
        l2_ip = Ether(src=src_mac, dst=dst_mac) / Dot1Q(vlan=vid)
    else:
        l2_ecpri = Ether(src=src_mac, dst=dst_mac, type=0xAEFE)
        l2_ptp = Ether(src=src_mac, dst=dst_mac, type=0x88F7)
        l2_ip = Ether(src=src_mac, dst=dst_mac)

    if 'PRACH' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x02, ecpri_payload_len)
        rtc_seq = struct.pack('!HH', 0x0001, 0x0000)
        oran_hdr = b'\x00\x00\x00\x00\x01\x03'
        pad_len = max(0, ecpri_payload_len - len(rtc_seq) - len(oran_hdr))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + rtc_seq + oran_hdr + (b'\x00' * pad_len))

    elif 'C-PLANE' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x02, ecpri_payload_len)
        rtc_seq = struct.pack('!HH', 0x0001, 0x0000)
        oran_hdr = b'\x80\x00\x00\x00\x01\x01'
        pad_len = max(0, ecpri_payload_len - len(rtc_seq) - len(oran_hdr))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + rtc_seq + oran_hdr + (b'\x00' * pad_len))

    elif 'U-PLANE' in atype:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x00, ecpri_payload_len)
        pc_seq = struct.pack('!HH', 0x0001, 0x0000)
        pad_len = max(0, ecpri_payload_len - len(pc_seq))
        pkt = l2_ecpri / Raw(load=ecpri_hdr + pc_seq + (b'\x00' * pad_len))

    elif 'PTP' in atype:
        ptp_hdr = b'\x00\x02\x00\x2c' + b'\x00' * 40
        pad_len = max(0, pkt_size - l2_len - len(ptp_hdr))
        pkt = l2_ptp / Raw(load=ptp_hdr + (b'\x00' * pad_len))

    elif 'TCP SYN' in atype:
        # 실제 TCP SYN 핸드셰이크와 유사한 구조:
        # flags=SYN, Window=14600, Options(MSS/SACK/Timestamp/NOP/WScale), payload 없음
        src_ip = config.get('src_ip', '192.168.11.100')
        dst_ip = config.get('dst_ip', '192.168.11.2')
        dst_port = int(config.get('dst_port', 80) or 80)
        synack_only = bool(config.get('tcp_synack_only'))
        tcp_flags = 'SA' if synack_only else 'S'
        sport = random.randint(1024, 65535)
        seq = random.randint(0, 0xFFFFFFFF)
        tsval = random.randint(1, 0xFFFFFFFF)
        tcp_kwargs = dict(
            sport=sport,
            dport=dst_port,
            seq=seq,
            ack=0,
            flags=tcp_flags,
            window=14600,
        )
        # Scapy 버전별 SAckOK 표현 차이 대응
        for sack_val in (b'', '', None):
            try:
                tcp_opts = [
                    ('MSS', 1460),
                    ('SAckOK', sack_val),
                    ('Timestamp', (tsval, 0)),
                    ('NOP', None),
                    ('WScale', 4),
                ]
                base_pkt = l2_ip / IP(src=src_ip, dst=dst_ip) / TCP(options=tcp_opts, **tcp_kwargs)
                # 옵션 직렬화 검증
                bytes(base_pkt)
                break
            except Exception:
                base_pkt = None
        if base_pkt is None:
            base_pkt = l2_ip / IP(src=src_ip, dst=dst_ip) / TCP(**tcp_kwargs)
        # SYN(또는 SYN-ACK only)는 TCP payload를 넣지 않음 (Wireshark Len=0)
        pkt = base_pkt

    elif 'NETCONF' in atype:
        src_ip = config.get('src_ip', '192.168.11.100')
        dst_ip = config.get('dst_ip', '192.168.11.2')
        dst_port = int(config.get('dst_port', 830) or 830)
        # tcp_synack_only: Wireshark Conversation Completeness에서
        # SYN-ACK만 Present(1), RST/FIN/Data/ACK/SYN은 Absent(0)가 되도록
        # TCP flags=SYN+ACK 이고 payload(Len=0)를 넣지 않는다.
        synack_only = bool(config.get('tcp_synack_only'))
        tcp_flags = 'SA' if synack_only else 'S'
        base_pkt = l2_ip / IP(src=src_ip, dst=dst_ip) / TCP(dport=dst_port, flags=tcp_flags)
        if synack_only:
            pkt = base_pkt
        else:
            pad_len = max(0, pkt_size - len(base_pkt))
            pkt = base_pkt / Raw(load=b'\x00' * pad_len)

    elif 'GTP' in atype:
        src_ip = config.get('src_ip', '192.168.11.100')
        dst_ip = config.get('dst_ip', '192.168.11.2')
        base_pkt = l2_ip / IP(src=src_ip, dst=dst_ip) / UDP(dport=2152) / Raw(b'\x30\xff\x00\x14\x00\x00\x00\x00')
        pad_len = max(0, pkt_size - len(base_pkt))
        pkt = base_pkt / Raw(load=b'\x00' * pad_len)

    else:
        ecpri_hdr = struct.pack('!BBH', 0x10, 0x02, ecpri_payload_len)
        pkt = l2_ecpri / Raw(load=ecpri_hdr + (b'\x00' * ecpri_payload_len))

    return pkt

def apply_mutations(pkt, config):
    if not config.get('mutation_enable'):
        return pkt

    rand_mac = config.get('rand_mac')
    rand_ip_flag = config.get('rand_ip')
    rand_vlan = config.get('rand_vlan')
    rand_ethertype = config.get('rand_ethertype')
    malformed_ecpri = config.get('malformed_ecpri')
    invalid_length = config.get('invalid_length')
    rand_l4_port = config.get('rand_l4_port')

    if rand_mac and pkt.haslayer(Ether):
        pkt[Ether].src = "02:%02x:%02x:%02x:%02x:%02x" % (
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255)
        )

    if rand_vlan and pkt.haslayer(Dot1Q):
        pkt[Dot1Q].vlan = random.randint(1, 4094)

    if rand_ethertype:
        if pkt.haslayer(Dot1Q):
            pkt[Dot1Q].type = random.choice([0x1234, 0x88B5, 0x9999, 0xFFFF])
        elif pkt.haslayer(Ether):
            pkt[Ether].type = random.choice([0x1234, 0x88B5, 0x9999, 0xFFFF])

    if rand_ip_flag and pkt.haslayer(IP):
        pkt[IP].src = random_ip()
        del pkt[IP].chksum

    if rand_l4_port:
        if pkt.haslayer(TCP):
            pkt[TCP].sport = random.randint(1024, 65535)
            del pkt[TCP].chksum
        elif pkt.haslayer(UDP):
            pkt[UDP].sport = random.randint(1024, 65535)
            if hasattr(pkt[UDP], 'chksum'):
                del pkt[UDP].chksum

    # invalid_length / malformed_ecpri 는 "캡처 프레임 길이"를 바꾸지 않고
    # 헤더 안의 length 필드만 실제 크기와 불일치하도록 변조한다.
    if invalid_length:
        actual_l3 = None
        if pkt.haslayer(IP):
            # IP Total Length 를 실제 L3 크기와 다른 값으로 설정
            actual_l3 = len(pkt[IP])
            wrong = actual_l3
            while wrong == actual_l3:
                wrong = random.choice([0, 1, 20, 40, 65535, random.randint(1, 65535)])
            pkt[IP].len = wrong
            del pkt[IP].chksum
            if pkt.haslayer(TCP) and hasattr(pkt[TCP], 'chksum'):
                del pkt[TCP].chksum
            if pkt.haslayer(UDP):
                # UDP Length 도 실제와 불일치시킴
                actual_udp = len(pkt[UDP])
                udp_wrong = actual_udp
                while udp_wrong == actual_udp:
                    udp_wrong = random.choice([0, 1, 8, 65535, random.randint(1, 65535)])
                pkt[UDP].len = udp_wrong
                if hasattr(pkt[UDP], 'chksum'):
                    del pkt[UDP].chksum

    raw_bytes = bytearray(bytes(pkt))
    l2_hlen = 18 if pkt.haslayer(Dot1Q) else 14

    if malformed_ecpri and len(raw_bytes) > l2_hlen + 4:
        # eCPRI common header: byte0/1 변조
        raw_bytes[l2_hlen] = 0xFF
        raw_bytes[l2_hlen + 1] = 0xFF

    if invalid_length and not pkt.haslayer(IP) and len(raw_bytes) > l2_hlen + 4:
        # eCPRI/L2 payload: common header payload length(2B)를 실제와 다르게
        actual_payload = max(0, len(raw_bytes) - l2_hlen - 4)
        wrong = actual_payload
        while wrong == actual_payload:
            wrong = random.choice([0, 1, 0xFFFF, random.randint(0, 0xFFFF)])
        raw_bytes[l2_hlen + 2] = (wrong >> 8) & 0xFF
        raw_bytes[l2_hlen + 3] = wrong & 0xFF

    return Ether(bytes(raw_bytes))

try:
    config = json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
    rate_gbps = float(config['rate'])
    pcap_ms = float(config['pcap_ms'])

    size_pattern = resolve_size_pattern(config)
    sample_size = size_pattern[0]
    bytes_per_ms = (rate_gbps * 1000000000 / 8) / 1000
    num_pkts = int((bytes_per_ms / max(sample_size, 64)) * pcap_ms)

    if num_pkts < 1:
        num_pkts = 1

    full_path = os.path.join(config['pcap_path'], config['pcap_name'])

    if not os.path.exists(config['pcap_path']):
        os.makedirs(config['pcap_path'], exist_ok=True)

    writer = PcapWriter(full_path, append=False, sync=True, nano=True)
    pcap_duration_sec = max(pcap_ms, 0.001) / 1000.0
    ts_step = pcap_duration_sec / max(num_pkts, 1)
    base_ts = time.time()

    try:
        for i in range(num_pkts):
            pkt_size = size_pattern[i % len(size_pattern)]
            base_pkt = build_packet(config, pkt_size)
            final_pkt = apply_mutations(base_pkt, config)
            # 다른 PCAP 플레이어에서도 1ms 버스트 의도를 해석할 수 있도록
            # 패킷 타임스탬프를 0~pcap_ms 구간에 균등 분포시킨다.
            final_pkt.time = base_ts + (i * ts_step)
            writer.write(final_pkt)
    finally:
        writer.close()

    print(json.dumps({
        'status': 'success',
        'file': full_path,
        'count': num_pkts,
        'duration_ms': pcap_ms,
        'timestamp_step_ns': int(ts_step * 1_000_000_000),
        'size_pattern': size_pattern
    }))

except Exception as e:
    print(json.dumps({'status': 'error', 'message': str(e)}))

"""
