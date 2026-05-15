import os
import sys
import time
import atexit

import RPi.GPIO as GPIO

# [설정] 핀 번호 및 상태 파일
PIN = 26
STATE_FILE = os.path.expanduser("~/last_state_dc.txt")
LOCK_FILE = "/tmp/dc_relay_control.lock"


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        file.write(state)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return file.read().strip() or "OFF"
    return "OFF"


def create_single_instance_lock():
    """동일 스크립트 중복 실행 방지."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as file:
                pid_text = file.read().strip()
            pid = int(pid_text)
            os.kill(pid, 0)
            print(f"이미 실행 중인 프로세스가 있습니다. (PID: {pid})")
            print("기존 프로세스를 종료한 후 다시 실행하세요.")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            # stale lock 이거나 접근 불가한 경우 새로 덮어씀
            pass

    with open(LOCK_FILE, "w", encoding="utf-8") as file:
        file.write(str(os.getpid()))


def remove_single_instance_lock():
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


def gpio_cleanup():
    try:
        GPIO.cleanup(PIN)
    except RuntimeError:
        # setup 이전/실패 상태일 수 있음
        pass


def show_header(current_state):
    os.system("clear")
    print("=" * 55)
    print(" [ DC 56V POWER CONTROL SYSTEM ] ".center(55))
    print("=" * 55)
    print("  ▶ 제어 모드    :  DC RELAY (Active High)")
    print(f"  ▶ 연결 핀(BCM) :  GPIO {PIN} (물리 37번)")
    print(f"  ▶ 현재 상태    :  [\033[1;36m {current_state} \033[0m]")
    print("-" * 55)
    print("  사용 가능 명령어: [on] [off] [timer] [exit]")
    print("-" * 55)


def control(action, state_ref):
    value = GPIO.HIGH if action == "on" else GPIO.LOW
    GPIO.output(PIN, value)
    state_ref["current"] = action.upper()
    save_state(state_ref["current"])
    print(f"\n  [알림] 상태가 [\033[1;33m {state_ref['current']} \033[0m] 로 변경되었습니다.")


def countdown_sleep(seconds, label):
    for i in range(int(seconds), 0, -1):
        sys.stdout.write(f"\r    ㄴ {label} 유지 중... [ {i}초 남음 ]   ")
        sys.stdout.flush()
        time.sleep(1)
    print(f"\r    ㄴ {label} 단계 완료!                    ")


def setup_gpio(initial_state):
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    initial_level = GPIO.HIGH if initial_state == "ON" else GPIO.LOW
    GPIO.setup(PIN, GPIO.OUT, initial=initial_level)


def main():
    create_single_instance_lock()
    atexit.register(remove_single_instance_lock)
    atexit.register(gpio_cleanup)

    current_state = load_state()
    try:
        setup_gpio(current_state)
    except Exception as error:
        error_text = str(error)
        if "GPIO busy" in error_text:
            print("GPIO busy 에러: 다른 프로세스가 GPIO 핀을 사용 중입니다.")
            print("확인 명령어:")
            print("  ps -ef | rg 'dc_relay|ac_relay|python'")
            print("  sudo pkill -f dc_relay.py")
            print("  sudo pkill -f ac_relay.py")
        else:
            print(f"GPIO 초기화 실패: {error_text}")
        sys.exit(1)

    state_ref = {"current": current_state}
    show_header(state_ref["current"])

    try:
        while True:
            cmd = input("\n  DC 명령 입력 >> ").lower().strip()

            if cmd == "exit":
                print("\n  프로그램을 종료합니다. (현재 전원 유지)")
                break

            if cmd in ("on", "off"):
                control(cmd, state_ref)
                continue

            if cmd == "timer":
                try:
                    on_t = float(input("  - ON 시간(초): "))
                    off_t = float(input("  - OFF 시간(초): "))
                    rep = int(input("  - 반복 횟수(0=무한): "))

                    print(f"\n  [타이머 모드 시작: {rep}회 반복]")
                    cnt = 0
                    while True:
                        cnt += 1
                        if rep != 0 and cnt > rep:
                            break

                        print(f"\n  ({cnt}회차 사이클 진행 중)")
                        control("on", state_ref)
                        countdown_sleep(on_t, "ON")

                        control("off", state_ref)
                        if rep == 0 or cnt < rep:
                            countdown_sleep(off_t, "OFF")
                    print("\n  [타이머 종료] 모든 사이클이 완료되었습니다.")
                except ValueError:
                    print("  ! 오류: 숫자만 입력해 주세요.")
                continue

            print("  ! 알 수 없는 명령입니다. [on/off/timer/exit] 중에서 입력하세요.")
    except KeyboardInterrupt:
        print("\n\n  [중단] 사용자에 의해 강제 종료되었습니다.")


if __name__ == "__main__":
    main()
