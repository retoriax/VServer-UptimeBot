import os
import json
import re
import asyncio
import paramiko
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# === Load environment and constants ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))
SSH_USERNAME = os.getenv("SSH_USERNAME")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
SETTINGS_FILE = "settings.txt"

# === Settings logic (all config in settings.txt) ===
def get_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
    except Exception:
        settings = {"periodic_running": True, "interval": 60, "ips": {}, "active_ip": None}
        set_settings(settings)
        return settings
    # Ensure interval is present and saved if missing
    if "interval" not in settings:
        settings["interval"] = 60
        set_settings(settings)
    return settings

def set_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

def get_ip():
    return get_settings().get("active_ip")

def set_ip(ip):
    s = get_settings()
    s["active_ip"] = ip.strip()
    if "ips" not in s:
        s["ips"] = {}
    if ip.strip() not in s["ips"]:
        s["ips"][ip.strip()] = {}
    set_settings(s)

def get_container(ip):
    return get_settings().get("ips", {}).get(ip, {}).get("container")

def set_container(ip, container):
    s = get_settings()
    if "ips" not in s:
        s["ips"] = {}
    if ip not in s["ips"]:
        s["ips"][ip] = {}
    s["ips"][ip]["container"] = container
    set_settings(s)

# === SSH helpers ===
def ssh_uptime(ip):
    key = paramiko.Ed25519Key.from_private_key_file(PRIVATE_KEY_PATH)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username=SSH_USERNAME, pkey=key, timeout=5)
    stdin, stdout, stderr = ssh.exec_command('uptime')
    output = stdout.read().decode().strip()
    ssh.close()
    return output

# Hilfsfunktion für beliebige SSH-Kommandos
def ssh_command(ip, command):
    key = paramiko.Ed25519Key.from_private_key_file(PRIVATE_KEY_PATH)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username=SSH_USERNAME, pkey=key, timeout=5)
    stdin, stdout, stderr = ssh.exec_command(command)
    output = stdout.read().decode().strip()
    error = stderr.read().decode().strip()
    ssh.close()
    return output, error


# === Prune output folders if /dev/vdb < 20G free ===
def prune_output_folders(ip):
    # Führt das Pruning-Skript per SSH aus
    script = r'''
avail=$(df -BG /dev/vdb | awk 'NR==2{gsub("G","",$4); print $4}')
if [ "$avail" -lt 20 ]; then
  cd /mnt/output || exit 1
  keep_steps=$(ls -1d stage1_*step_* 2>/dev/null | grep -v '_encoder$' | sed -n 's/.*_step_\([0-9]*\)$/\1/p' | sort -n | tail -2)
  keep_dirs=""
  for step in $keep_steps; do
    keep_dirs="$keep_dirs $(ls -d stage1_*step_${step} 2>/dev/null)"
    keep_dirs="$keep_dirs $(ls -d stage1_*step_${step}_encoder 2>/dev/null)"
  done
  for d in stage1_*step_*; do
    [ ! -d "$d" ] && continue
    skip=0
    for k in $keep_dirs; do
      [ "$d" = "$k" ] && skip=1
    done
    [ $skip -eq 0 ] && rm -rf "$d"
  done
fi
'''
    # Führe das Skript aus
    out, err = ssh_command(ip, script)
    return out, err

# === Telegram Commands ===
async def setip(update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        ip = context.args[0]
        set_ip(ip)
        # Optional: Containername direkt mitgeben
        if len(context.args) > 1:
            container = context.args[1]
            set_container(ip, container)
            await update.message.reply_text(f"IP wurde gesetzt auf {ip} und Container auf {container}")
        else:
            await update.message.reply_text(f"IP wurde gesetzt auf {ip}. Bitte Container mit /setcontainer <name> setzen.")
    else:
        await update.message.reply_text("Bitte IP angeben: /setip 1.2.3.4 [container]")

# /setcontainer Command
async def setcontainer(update, context: ContextTypes.DEFAULT_TYPE):
    ip = get_ip()
    if not ip:
        await update.message.reply_text("Bitte zuerst eine IP setzen: /setip <ip>")
        return
    if not context.args:
        await update.message.reply_text("Bitte Containername angeben: /setcontainer <containername>")
        return
    container = context.args[0]
    set_container(ip, container)
    await update.message.reply_text(f"Container für {ip} wurde gesetzt auf {container}")

# === /prune Command ===
async def prune_command(update, context: ContextTypes.DEFAULT_TYPE):
    ip = get_ip()
    if not ip:
        await update.message.reply_text("Bitte zuerst eine IP setzen: /setip <ip>")
        return
    try:
        out, err = prune_output_folders(ip)
        if err:
            msg = f"Fehler beim Pruning: {err}"
        else:
            msg = "Pruning durchgeführt."
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Fehler beim Pruning: {e}")

async def status(update, context: ContextTypes.DEFAULT_TYPE):
    ip = get_ip()
    if not ip:
        await update.message.reply_text("Bitte zuerst eine IP setzen: /setip <ip>")
        return
    try:
        info = ssh_uptime(ip)
        docker_ps, _ = ssh_command(ip, 'docker ps')
        df_h, _ = ssh_command(ip, 'df -h')
        # Header + /dev/vdb Zeile anzeigen
        lines = df_h.splitlines()
        header = lines[0] if lines else ""
        vdb = next((line for line in lines if "/dev/vdb" in line), None)
        df_vdb = f"{header}\n{vdb}" if vdb else f"{header}\n(nicht gefunden)"
        # Container-Logs
        container = get_container(ip)
        if container:
            logs, _ = ssh_command(ip, f'docker logs --tail 20 {container}')
            logs_html = logs.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
            logs_block = f"<b>docker logs {container} (letzte 20 Zeilen)</b>\n<pre>{logs_html}</pre>"
        else:
            logs_block = "<i>Kein Container gesetzt. Mit /setcontainer <name> setzen.</i>"

        vdb_warn = ""
        if vdb:
            try:
                parts = vdb.split()
                use_str = next((p for p in parts if p.endswith("%")), None)
                if use_str and int(use_str.strip("%")) > 80:
                    vdb_warn = "<b>⚠️ WARNING: /dev/vdb Belegung über 80%! Please make space!</b>\n"
            except Exception:
                pass

        msg = (
            f"<b>VServer {ip} ist ONLINE</b>\n"
            f"<b>Uptime:</b> <code>{info}</code>\n\n"
            f"<b>docker ps</b>\n<pre>{docker_ps}</pre>\n"
            f"<b>df -h /dev/vdb</b>\n<pre>{df_vdb}</pre>\n"
            f"{vdb_warn}{logs_block}"
        )
    except Exception as e:
        msg = f"VServer {ip} ist OFFLINE! Fehler: {e}"
    await update.message.reply_text(msg, parse_mode='HTML')
# === Telegram Commands ===
async def logs(update, context: ContextTypes.DEFAULT_TYPE):
    ip = get_ip()
    if not ip:
        await update.message.reply_text("Bitte zuerst eine IP setzen: /setip <ip>")
        return
    if not context.args:
        await update.message.reply_text("Bitte Containername angeben: /logs <containername>")
        return
    container = context.args[0]
    try:
        logs, error = ssh_command(ip, f'docker logs --tail 2000 {container}')
        if error:
            msg = f"Fehler beim Abrufen der Logs: {error}"
        else:
            # Telegram Nachrichten sind limitiert, daher ggf. kürzen
            if len(logs) > 3500:
                logs = logs[-3500:]
            # Escape < und > für HTML
            clean_logs = re.sub(r"\|[^|]+?\|", "", logs)
            logs_html = clean_logs.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
            msg = f"Logs von {container}:\n<pre>{logs_html}</pre>"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"Fehler: {e}")

# === /output Command ===
async def output_command(update, context: ContextTypes.DEFAULT_TYPE):
    ip = get_ip()
    if not ip:
        await update.message.reply_text("Bitte zuerst eine IP setzen: /setip <ip>")
        return
    try:
        out, err = ssh_command(ip, 'ls -lh /mnt/output')
        if err:
            msg = f"Fehler beim Ausführen von ls: {err}"
        else:
            # Escape < und > für HTML
            clean_out = re.sub(r"\|[^|]+?\|", "", out)
            out_html = clean_out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
            msg = f"<b>ls -lh /mnt/output</b>\n<pre>{out_html}</pre>"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"Fehler: {e}")

# === /settings Command ===
async def settings_command(update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings()
    ip = get_ip()
    container = get_container(ip) if ip else None
    msg = (
        f"<b>Aktuelle Einstellungen:</b>\n"
        f"<b>IP:</b> {ip if ip else 'Nicht gesetzt'}\n"
        f"<b>Container:</b> {container if container else 'Nicht gesetzt'}\n"
        f"<b>Intervall:</b> {settings.get('interval', 60)} Sekunden\n"
        f"<b>Periodische Statusmeldungen:</b> {'aktiv' if settings.get('periodic_running', True) else 'pausiert'}"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

# === Periodischer Check (Background Task) ===

periodic_task = None

def get_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        # Default: periodic_running True, interval or 60
        return {"periodic_running": True, "interval": 60}

def set_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

def get_periodic_running():
    return get_settings().get("periodic_running", True)

def set_periodic_running(value: bool):
    s = get_settings()
    s["periodic_running"] = value
    set_settings(s)

def get_interval():
    return get_settings().get("interval", 60)

def set_interval(value: int):
    s = get_settings()
    s["interval"] = value
    set_settings(s)

def set_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

def get_periodic_running():
    return get_settings().get("periodic_running", True)

def set_periodic_running(value: bool):
    s = get_settings()
    s["periodic_running"] = value
    set_settings(s)

async def periodic_check(app):
    while True:
        ip = get_ip()
        interval = get_interval()
        if not ip:
            await asyncio.sleep(interval)
            continue
        try:
            info = ssh_uptime(ip)
            docker_ps, _ = ssh_command(ip, 'docker ps')
            df_h, _ = ssh_command(ip, 'df -h')
            lines = df_h.splitlines()
            header = lines[0] if lines else ""
            vdb = next((line for line in lines if "/dev/vdb" in line), None)
            df_vdb = f"{header}\n{vdb}" if vdb else f"{header}\n(nicht gefunden)"

            # Prune output folders if needed
            try:
                prune_output_folders(ip)
            except Exception as e:
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"Fehler beim Pruning: {e}")
            container = get_container(ip)
            container_running = False
            if container:
                # Prüfe, ob der Container läuft
                ps_out, _ = ssh_command(ip, f'docker ps --format "{{{{.Names}}}}"')
                running_names = [name.strip() for name in ps_out.splitlines()]
                if container in running_names:
                    container_running = True
                logs, _ = ssh_command(ip, f'docker logs --tail 20 {container}')
                logs_html = logs.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
                logs_block = f"<b>docker logs {container} (letzte 20 Zeilen)</b>\n<pre>{logs_html}</pre>"
            else:
                logs_block = "<i>Kein Container gesetzt. Mit /setcontainer &lt;name&gt; setzen.</i>"

            # Alert, wenn Container nicht läuft
            if container and not container_running:
                alert_msg = (
                    f"<b>🚨 Container DOWN!</b>\n"
                    f"<b>Container <code>{container}</code> läuft NICHT auf {ip}!</b>\n"
                    f"Bitte prüfen!"
                )
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=alert_msg, parse_mode='HTML')

            if get_periodic_running():
                msg = (
                    f"<b>VServer {ip} ist ONLINE</b>\n"
                    f"<b>Uptime:</b> <code>{info}</code>\n\n"
                    f"<b>docker ps</b>\n<pre>{docker_ps}</pre>\n"
                    f"<b>df -h /dev/vdb</b>\n<pre>{df_vdb}</pre>\n"
                    f"{logs_block}"
                )
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
        except Exception as e:
            msg = (
                "<b>🚨🚨🚨 SERVER OFFLINE! 🚨🚨🚨</b>\n"
                f"<b>VServer {ip} ist OFFLINE!</b>\n"
                f"<b>Fehler:</b> <code>{e}</code>\n"
                "<b>BITTE SOFORT PRÜFEN!</b>"
            )
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
        await asyncio.sleep(interval)

# === /help Command ===

async def interval_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Bitte gib das Intervall in Sekunden an: /interval <sekunden>")
        return
    seconds = int(context.args[0])
    if seconds < 10:
        await update.message.reply_text("Das Intervall muss mindestens 10 Sekunden betragen.")
        return
    set_interval(seconds)
    await update.message.reply_text(f"Intervall wurde auf {seconds} Sekunden gesetzt.")

async def help_command(update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "/help - Diese Hilfe\n"
        "/setip <ip> [container] - Setze die Ziel-IP (und optional Container)\n"
        "/setcontainer <container> - Setze den Container für die aktuelle IP\n"
        "/status - Zeige Status, docker ps, df -h, docker logs\n"
        "/logs <container> - Zeige letzte 50 Zeilen Docker Logs\n"
        "/output - Zeige ls -lh /mnt/output\n"
        "/stop - Pausiere periodische Statusmeldungen (Warnung bei OFFLINE kommt trotzdem)\n"
        "/resume - Setze periodische Statusmeldungen fort\n"
        "/interval <sekunden> - Setze das Intervall für die Statusprüfung\n"
        "/settings - Zeige aktuelle Einstellungen"
        "/prune - Prune output folders wenn /dev/vdb < 20G frei\n"
    )
    await update.message.reply_text(msg)

# === /stop Command ===
async def stop_command(update, context: ContextTypes.DEFAULT_TYPE):
    set_periodic_running(False)
    await update.message.reply_text("Periodische Statusmeldungen gestoppt.")

# === /resume Command ===
async def resume_command(update, context: ContextTypes.DEFAULT_TYPE):
    set_periodic_running(True)
    await update.message.reply_text("Periodische Statusmeldungen werden fortgesetzt.")


# === Main für python-telegram-bot v22+ ===

import logging
logging.basicConfig(level=logging.INFO)

# Fix für 'event loop is already running' unter Windows/Python 3.12 (z.B. in Jupyter, VSCode, etc.)
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setip", setip))
    app.add_handler(CommandHandler("setcontainer", setcontainer))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("output", output_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("interval", interval_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("prune", prune_command))
    # Periodischen Check als Background-Task starten
    global periodic_task
    periodic_task = asyncio.create_task(periodic_check(app))
    # Beim Start: User auffordern, eine IP zu setzen, falls keine gesetzt ist
    if get_ip() is None:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="Bitte setze zuerst eine IP mit /setip <ip>")
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())