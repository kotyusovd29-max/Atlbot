#!/bin/bash
# Поднимает приложение отдельной сессией, чтобы оно не умерло вместе с SSH деплоя.
set -u
cd /home/kotusov/projects/pr1

PORT=8000
proxy=$(grep -R "pr1.kotusov" -n /etc/nginx /etc/caddy 2>/dev/null | grep -oE '127\.0\.0\.1:[0-9]+|localhost:[0-9]+' | head -1 | cut -d: -f2 || true)
if [ -n "${proxy:-}" ]; then
  PORT="$proxy"
fi
echo "using port $PORT"
export PORT

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
    if "http.server" in raw or raw.strip().endswith("app.py") or "/app.py" in raw:
        try:
            os.kill(pid, signal.SIGTERM)
            print("stopped", pid)
        except Exception as exc:
            print("cannot stop", pid, exc)
PY

sleep 1
PORT="$PORT" python3 - << 'PY'
import os, subprocess
log = open("/home/kotusov/projects/pr1/server.log", "a")
env = os.environ.copy()
proc = subprocess.Popen(
    ["/usr/bin/python3", "/home/kotusov/projects/pr1/app.py"],
    cwd="/home/kotusov/projects/pr1",
    env=env,
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
)
print("started", proc.pid)
open("/home/kotusov/projects/pr1/app.pid", "w").write(str(proc.pid))
PY

sleep 1
pid=$(cat /home/kotusov/projects/pr1/app.pid)
if ! kill -0 "$pid" 2>/dev/null; then
  echo "process died immediately"
  tail -40 /home/kotusov/projects/pr1/server.log || true
  exit 1
fi

ok=0
for _ in 1 2 3 4 5 6; do
  body=$(curl -sS -m 3 -H "Content-Type: application/json" \
    -d '{"email":"dmitryikotusov@f5.com.ru","password":"dmitryikotusov@f5.com.ru"}' \
    "http://127.0.0.1:${PORT}/api/login" || true)
  echo "login-check: $body"
  case "$body" in
    *'"ok": true'*) ok=1; break ;;
  esac
  sleep 1
done

if [ "$ok" != 1 ]; then
  echo "----- server.log -----"
  tail -40 /home/kotusov/projects/pr1/server.log || true
  exit 1
fi
# Процесс в новой сессии должен пережить закрытие SSH.
if ! kill -0 "$pid" 2>/dev/null; then
  echo "process disappeared after check"
  exit 1
fi
echo "APP_OK pid=$pid port=$PORT"
