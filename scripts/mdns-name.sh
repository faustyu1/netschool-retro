#!/bin/sh
# Объявляет имя <имя>.local в локальной сети (mDNS/Bonjour) с текущим IP этого Mac,
# чтобы телефон открывал сайт по http://<имя>.local. Следит за сменой IP.
# Использование: scripts/mdns-name.sh [имя]   (по умолчанию netschool)
NAME="${1:-netschool}"
pid=""
last=""
trap '[ -n "$pid" ] && kill "$pid" 2>/dev/null' EXIT INT TERM
while true; do
  ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
  if [ -n "$ip" ] && { [ "$ip" != "$last" ] || ! kill -0 "$pid" 2>/dev/null; }; then
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    dns-sd -P "$NAME" _http._tcp local 80 "$NAME.local" "$ip" >/dev/null &
    pid=$!
    last=$ip
  fi
  sleep 30
done
