#!/bin/bash
# Запускает страницу пакетов ТТН так, чтобы её видел nginx на https://pr1.kotusov.f5-vibe.ru/
set -u
cd /home/kotusov/projects/pr1
loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true

PUBLIC_URL="https://pr1.kotusov.f5-vibe.ru/api/health"

python3 << 'PY'
import json, os
path = "/home/kotusov/projects/pr1/config.json"
cfg = {
    "bot_token": "",
    "webhook_secret": "",
    "public_url": "https://pr1.kotusov.f5-vibe.ru",
}
try:
    with open(path, encoding="utf-8") as f:
        old = json.load(f)
    if isinstance(old, dict):
        for key in cfg:
            if isinstance(old.get(key), str):
                cfg[key] = old[key]
except FileNotFoundError:
    pass
token = os.environ.get("BOT_TOKEN", "").strip()
if token:
    cfg["bot_token"] = token
    print("bot token updated, length", len(token))
elif cfg.get("bot_token"):
    print("BOT_TOKEN is empty, keeping token already on server, length", len(cfg["bot_token"]))
else:
    print("BOT_TOKEN is empty and server has no token; page will start, bot will not")
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
os.chmod(path, 0o600)
PY

stop_old() {
  python3 - << 'PY'
import os, signal, time
me, parent = os.getpid(), os.getppid()

def stop(sig):
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
        if "/home/kotusov/projects/pr1/app.py" in raw or ("http.server" in raw and "8000" in raw):
            try:
                os.kill(pid, sig)
                print("stopped", pid, sig)
            except Exception as exc:
                print("cannot stop", pid, exc)

stop(signal.SIGTERM)
time.sleep(1)
stop(signal.SIGKILL)
PY
}

start_on() {
  PORT="$1" python3 - << 'PY'
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
open("/home/kotusov/projects/pr1/app.pid", "w").write(str(proc.pid))
print("started", proc.pid, "port", env.get("PORT"))
PY
}

wait_local() {
  port="$1"
  i=0
  while [ "$i" -lt 8 ]; do
    body=$(curl -sS -m 3 "http://127.0.0.1:${port}/api/health" || true)
    echo "local:${port} ${body}"
    case "$body" in
      *'"ok": true'*) return 0 ;;
    esac
    i=$((i + 1))
    sleep 1
  done
  return 1
}

public_code() {
  curl -sk -m 8 -o /tmp/pr1-public.txt -w "%{http_code}" "$PUBLIC_URL" || true
}

echo "=== listeners ==="
(ss -tln || netstat -tln) 2>/dev/null | head -40 || true
echo "=== nginx config mentions ==="
grep -R "pr1.kotusov\|proxy_pass" /etc/nginx/sites-enabled /etc/nginx/sites-available 2>/dev/null | head -40 || true

upstream=$(grep -E "proxy_pass http://127.0.0.1:[0-9]+" /etc/nginx/sites-enabled/f5-kotusov-pr1 2>/dev/null | grep -oE '127\.0\.0\.1:[0-9]+' | head -1 | cut -d: -f2 || true)
if [ -z "${upstream:-}" ]; then
  upstream=8101
fi
echo "upstream port: $upstream"

stop_old
sleep 1
start_on "$upstream"
if ! wait_local "$upstream"; then
  echo "----- server.log -----"
  tail -40 /home/kotusov/projects/pr1/server.log || true
  exit 1
fi

code=$(public_code)
echo "public-after:${code} $(head -c 180 /tmp/pr1-public.txt 2>/dev/null || true)"
if [ "$code" != "200" ]; then
  echo "----- nginx error -----"
  tail -n 20 /var/log/nginx/error.log /var/log/nginx/*error*.log 2>/dev/null || true
  exit 1
fi
echo "APP_OK port=$upstream"
