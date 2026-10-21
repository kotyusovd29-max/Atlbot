#!/bin/bash
# Останавливает всё, что заняло порт 8000, и поднимает приложение.
set -u
cd /home/kotusov/projects/pr1

python3 - << 'PY'
import os, signal
me = os.getpid()
parent = os.getppid()
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    pid = int(name)
    if pid in (me, parent):
        continue
    try:
        raw = open("/proc/%d/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode()
    except Exception:
        continue
    if "http.server" in raw or (raw.strip().endswith("app.py") or "/app.py" in raw):
        try:
            os.kill(pid, signal.SIGTERM)
            print("stopped", pid, raw[:120])
        except Exception as exc:
            print("cannot stop", pid, exc)
PY

sleep 1
nohup /usr/bin/python3 /home/kotusov/projects/pr1/app.py >> /home/kotusov/projects/pr1/server.log 2>&1 &
echo $! > /home/kotusov/projects/pr1/app.pid
sleep 1

ok=0
for _ in 1 2 3 4 5 6; do
  body=$(curl -sS -m 3 -H "Content-Type: application/json" \
    -d '{"email":"dmitryikotusov@f5.com.ru","password":"dmitryikotusov@f5.com.ru"}' \
    http://127.0.0.1:8000/api/login || true)
  echo "login-check: $body"
  case "$body" in
    *'"ok": true'*) ok=1; break ;;
  esac
  sleep 1
done

if [ "$ok" != 1 ]; then
  echo "----- server.log -----"
  tail -40 /home/kotusov/projects/pr1/server.log || true
  echo "----- python processes -----"
  ps -eo pid,args | grep -E "python|http.server" | grep -v grep || true
  exit 1
fi
echo "APP_OK"
