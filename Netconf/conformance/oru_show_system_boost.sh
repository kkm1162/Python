#!/usr/bin/env bash
# O-RU trace/log 생성 가속: ALLOWED_IP(ORU)에 SSH 후 vtysh show system 을 주기 실행.
# GUI/Conformance worker 가 시험 시작 시 nohup, 종료 시 SIGTERM 으로 중지한다.
set -u

ORU_BOOST_IP="${ORU_BOOST_IP:?ORU_BOOST_IP required}"
ORU_BOOST_ID="${ORU_BOOST_ID:?ORU_BOOST_ID required}"
ORU_BOOST_PW="${ORU_BOOST_PW:?ORU_BOOST_PW required}"
ORU_BOOST_INTERVAL="${ORU_BOOST_INTERVAL:-1}"

_ssh_opts=(
	-o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null
	-o ConnectTimeout=5
	-o BatchMode=no
)

_cleanup() {
	trap - TERM INT
	jobs -pr | while read -r _jp; do
		[[ -n "$_jp" ]] && kill -TERM "$_jp" 2>/dev/null || true
	done
	wait 2>/dev/null || true
	exit 0
}
trap _cleanup TERM INT

while true; do
	sshpass -p "$ORU_BOOST_PW" ssh "${_ssh_opts[@]}" "${ORU_BOOST_ID}@${ORU_BOOST_IP}" \
		'vtysh -c "show system"' 2>/dev/null &
	_spid=$!
	wait "$_spid" 2>/dev/null || true
	sleep "$ORU_BOOST_INTERVAL"
done
