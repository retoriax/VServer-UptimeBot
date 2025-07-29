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


# === Settings logic (multi-server, each with name/settings, interval pro Server) ===
def get_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        settings = {
            "servers": {}
        }
        set_settings(settings)
        return settings

def set_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

def get_server(ip):
    s = get_settings()
    return s.get("servers", {}).get(ip)

def set_server(ip, data):
    s = get_settings()
    if "servers" not in s:
        s["servers"] = {}
    s["servers"][ip] = data
    set_settings(s)

def set_server_value(ip, key, value):
    s = get_settings()
    if "servers" not in s:
        s["servers"] = {}
    if ip not in s["servers"]:
        s["servers"][ip] = {}
    s["servers"][ip][key] = value
    set_settings(s)

def get_server_value(ip, key, default=None):
    s = get_settings()
    return s.get("servers", {}).get(ip, {}).get(key, default)

def get_all_servers():
    return get_settings().get("servers", {})

# Kompatibilität: Container für IP
def get_container(ip=None):
    return get_server_value(ip, "container")

def set_container(ip, container):
    set_server_value(ip, "container", container)

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


# === /add <ip> <name> Command ===
async def add_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Bitte nutze: /add <ip> <name>")
        return
    ip = context.args[0].strip()
    name = " ".join(context.args[1:]).strip()
    servers = get_all_servers()
    if ip in servers:
        await update.message.reply_text(f"{ip} existiert bereits. Nutze /sc oder /remove.")
        return
    set_server(ip, {"name": name})
    # Starte sofort einen eigenen Check-Task für diesen Server
    global periodic_tasks
    if 'periodic_tasks' not in globals():
        periodic_tasks = {}
    app = context.application
    periodic_tasks[ip] = asyncio.create_task(periodic_check_server(app, ip))
    await update.message.reply_text(f"VServer {ip} mit Name '{name}' hinzugefügt und Überwachung gestartet.")

# === /remove <ip> Command ===
async def remove_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Bitte nutze: /remove <name>")
        return
    name = context.args[0].strip()
    s = get_settings()
    servers = s.get("servers", {})
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    del servers[ip]
    s["servers"] = servers
    set_settings(s)
    await update.message.reply_text(f"VServer {name} ({ip}) wurde entfernt.")

# /sc <name> <container>
async def sc(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Bitte nutze: /sc <name> <container>")
        return
    name = context.args[0].strip()
    container = context.args[1].strip()
    servers = get_all_servers()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    set_container(ip, container)
    await update.message.reply_text(f"Container für {name} ({ip}) wurde gesetzt auf {container}")

# === /prune Command ===
async def prune_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Bitte nutze: /prune <name>")
        return
    name = context.args[0].strip()
    servers = get_all_servers()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
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

# /s <name>
async def s_command(update, context: ContextTypes.DEFAULT_TYPE):
    servers = get_all_servers()
    if not context.args or len(context.args) < 1:
        if not servers:
            await update.message.reply_text("Keine VServer eingetragen.")
            return
        for ip, srv in servers.items():
            name = srv.get('name', ip)
            try:
                info = ssh_uptime(ip)
                docker_ps, _ = ssh_command(ip, 'docker ps')
                df_h, _ = ssh_command(ip, 'df -h')
                lines = df_h.splitlines()
                header = lines[0] if lines else ""
                vdb = next((line for line in lines if "/dev/vdb" in line), None)
                df_vdb = f"{header}\n{vdb}" if vdb else f"{header}\n(nicht gefunden)"
                container = get_container(ip)
                if container:
                    logs, _ = ssh_command(ip, f'docker logs --tail 20 {container}')
                    logs_html = logs.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
                    logs_block = f"<b>docker logs {container} (letzte 20 Zeilen)</b>\n<pre>{logs_html}</pre>"
                else:
                    logs_block = "<i>Kein Container gesetzt. Mit /sc <name> setzen.</i>"
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
                    f"<b>VServer {name} ({ip}) ist ONLINE</b>\n"
                    f"<b>Uptime:</b> <code>{info}</code>\n\n"
                    f"<b>docker ps</b>\n<pre>{docker_ps}</pre>\n"
                    f"<b>df -h /dev/vdb</b>\n<pre>{df_vdb}</pre>\n"
                    f"{vdb_warn}{logs_block}"
                )
            except Exception as e:
                msg = f"VServer {name} ({ip}) ist OFFLINE! Fehler: {e}"
            await update.message.reply_text(msg, parse_mode='HTML')
        return
    name = context.args[0].strip()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    try:
        info = ssh_uptime(ip)
        docker_ps, _ = ssh_command(ip, 'docker ps')
        df_h, _ = ssh_command(ip, 'df -h')
        lines = df_h.splitlines()
        header = lines[0] if lines else ""
        vdb = next((line for line in lines if "/dev/vdb" in line), None)
        df_vdb = f"{header}\n{vdb}" if vdb else f"{header}\n(nicht gefunden)"
        container = get_container(ip)
        if container:
            logs, _ = ssh_command(ip, f'docker logs --tail 20 {container}')
            logs_html = logs.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
            logs_block = f"<b>docker logs {container} (letzte 20 Zeilen)</b>\n<pre>{logs_html}</pre>"
        else:
            logs_block = "<i>Kein Container gesetzt. Mit /sc <name> setzen.</i>"
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
            f"<b>VServer {name} ({ip}) ist ONLINE</b>\n"
            f"<b>Uptime:</b> <code>{info}</code>\n\n"
            f"<b>docker ps</b>\n<pre>{docker_ps}</pre>\n"
            f"<b>df -h /dev/vdb</b>\n<pre>{df_vdb}</pre>\n"
            f"{vdb_warn}{logs_block}"
        )
    except Exception as e:
        msg = f"VServer {name} ({ip}) ist OFFLINE! Fehler: {e}"
    await update.message.reply_text(msg, parse_mode='HTML')
# /logs <name> <container>
async def logs(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Bitte nutze: /logs <name> <container>")
        return
    name = context.args[0].strip()
    container = context.args[1].strip()
    servers = get_all_servers()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    try:
        logs, error = ssh_command(ip, f'docker logs --tail 2000 {container}')
        if error:
            msg = f"Fehler beim Abrufen der Logs: {error}"
        else:
            if len(logs) > 3500:
                logs = logs[-3500:]
            clean_logs = re.sub(r"\|[^|]+?\|", "", logs)
            logs_html = clean_logs.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
            msg = f"Logs von {container}:\n<pre>{logs_html}</pre>"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"Fehler: {e}")

# === /output Command ===
# /output <name>
async def output_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Bitte nutze: /output <name>")
        return
    name = context.args[0].strip()
    servers = get_all_servers()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    try:
        out, err = ssh_command(ip, 'ls -lh /mnt/output')
        if err:
            msg = f"Fehler beim Ausführen von ls: {err}"
        else:
            clean_out = re.sub(r"\|[^|]+?\|", "", out)
            out_html = clean_out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
            msg = f"<b>ls -lh /mnt/output</b>\n<pre>{out_html}</pre>"
        await update.message.reply_text(msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"Fehler: {e}")

# === /list Command ===
async def list_command(update, context: ContextTypes.DEFAULT_TYPE):
    servers = get_all_servers()
    if not servers:
        await update.message.reply_text("Keine VServer eingetragen.")
        return
    msg = "<b>Alle VServer:</b>\n" + "\n".join([
        f"{srv.get('name','-')} ({ip}) - Container: {srv.get('container','-')}" for ip, srv in servers.items()
    ])
    await update.message.reply_text(msg, parse_mode='HTML')

# === /settings Command ===
# /settings <name>
async def settings_command(update, context: ContextTypes.DEFAULT_TYPE):
    servers = get_all_servers()
    if not context.args or len(context.args) < 1:
        if not servers:
            await update.message.reply_text("Keine VServer eingetragen.")
            return
        msg = "<b>Alle Einstellungen:</b>\n"
        for ip, srv in servers.items():
            name = srv.get('name', '-')
            container = srv.get('container', '-')
            interval = srv.get('interval', 60)
            periodic = srv.get('periodic_running', True)
            msg += (
                f"\n<b>{name} ({ip})</b>\n"
                f"Container: {container}\n"
                f"Intervall: {interval} Sekunden\n"
                f"Periodische Statusmeldungen: {'aktiv' if periodic else 'pausiert'}\n"
            )
        await update.message.reply_text(msg, parse_mode='HTML')
        return
    name = context.args[0].strip()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    container = get_container(ip)
    interval = get_server_value(ip, "interval", 60)
    periodic = get_server_value(ip, "periodic_running", True)
    msg = (
        f"<b>Einstellungen für {name}:</b>\n"
        f"<b>IP:</b> {ip}\n"
        f"<b>Container:</b> {container if container else 'Nicht gesetzt'}\n"
        f"<b>Intervall:</b> {interval} Sekunden\n"
        f"<b>Periodische Statusmeldungen:</b> {'aktiv' if periodic else 'pausiert'}\n"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

# === Periodische Checks pro Server ===
periodic_tasks = {}

def get_periodic_running(ip=None):
    if ip:
        return get_server_value(ip, "periodic_running", True)
    return get_settings().get("periodic_running", True)

def set_periodic_running(value: bool):
    s = get_settings()
    s["periodic_running"] = value
    set_settings(s)

def get_server_interval(ip):
    # Hole Intervall für Server, fallback auf global
    return get_server_value(ip, "interval") or get_settings().get("interval", 60)

def set_server_interval(ip, value):
    set_server_value(ip, "interval", value)

async def periodic_check_server(app, ip):
    while True:
        interval = get_server_interval(ip)
        try:
            info = ssh_uptime(ip)
            docker_ps, _ = ssh_command(ip, 'docker ps')
            df_h, _ = ssh_command(ip, 'df -h')
            lines = df_h.splitlines()
            header = lines[0] if lines else ""
            vdb = next((line for line in lines if "/dev/vdb" in line), None)
            df_vdb = f"{header}\n{vdb}" if vdb else f"{header}\n(nicht gefunden)"
            try:
                prune_output_folders(ip)
            except Exception as e:
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"Fehler beim Pruning: {e}")
            # Servername holen
            servers = get_all_servers()
            name = servers.get(ip, {}).get('name', ip)
            container = get_container(ip)
            container_running = False
            if container:
                ps_out, _ = ssh_command(ip, f'docker ps --format "{{{{.Names}}}}"')
                running_names = [n.strip() for n in ps_out.splitlines()]
                if container in running_names:
                    container_running = True
                logs, _ = ssh_command(ip, f'docker logs --tail 20 {container}')
                logs_html = logs.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")
                logs_block = f"<b>docker logs {container} (letzte 20 Zeilen)</b>\n<pre>{logs_html}</pre>"
            else:
                logs_block = "<i>Kein Container gesetzt. Mit /sc &lt;name&gt; setzen.</i>"
            if container and not container_running:
                alert_msg = (
                    f"<b>🚨 Container DOWN!</b>\n"
                    f"<b>Container <code>{container}</code> läuft NICHT auf {name} ({ip})!</b>\n"
                    f"Bitte prüfen!"
                )
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=alert_msg, parse_mode='HTML')
            if get_periodic_running(ip):
                msg = (
                    f"<b>VServer {name} ({ip}) ist ONLINE</b>\n"
                    f"<b>Uptime:</b> <code>{info}</code>\n\n"
                    f"<b>docker ps</b>\n<pre>{docker_ps}</pre>\n"
                    f"<b>df -h /dev/vdb</b>\n<pre>{df_vdb}</pre>\n"
                    f"{logs_block}"
                )
                await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
        except Exception as e:
            # Servername holen
            servers = get_all_servers()
            name = servers.get(ip, {}).get('name', ip)
            msg = (
                "<b>🚨🚨🚨 SERVER OFFLINE! 🚨🚨🚨</b>\n"
                f"<b>VServer {name} ({ip}) ist OFFLINE!</b>\n"
                f"<b>Fehler:</b> <code>{e}</code>\n"
                "<b>BITTE SOFORT PRÜFEN!</b>"
            )
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
        await asyncio.sleep(interval)

# === /help Command ===

async def interval_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2 or not context.args[1].isdigit():
        await update.message.reply_text("Bitte nutze: /interval <name> <sekunden>")
        return
    name = context.args[0].strip()
    seconds = int(context.args[1])
    if seconds < 10:
        await update.message.reply_text("Das Intervall muss mindestens 10 Sekunden betragen.")
        return
    servers = get_all_servers()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    set_server_interval(ip, seconds)
    await update.message.reply_text(f"Intervall für {name} wurde auf {seconds} Sekunden gesetzt.")

async def help_command(update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "<b>🛠️ VServer UptimeBot Hilfe</b>\n\n"
        "<b>🔹 Allgemein</b>\n"
        "/help – Zeigt diese Hilfe\n"
        "/list – Zeigt alle eingetragenen Server\n\n"
        "<b>➕ Serververwaltung</b>\n"
        "/add &lt;ip&gt; &lt;name&gt; – Server hinzufügen und Überwachung starten\n"
        "/remove &lt;ip&gt; – Server entfernen (nach IP)\n\n"
        "<b>⚙️ Einstellungen</b>\n"
        "/settings – Zeigt Einstellungen aller Server\n"
        "/settings &lt;name&gt; – Zeigt Einstellungen für einen Server\n"
        "/interval &lt;name&gt; &lt;sekunden&gt; – Setzt das Prüfintervall für einen Server\n\n"
        "<b>📦 Container</b>\n"
        "/sc &lt;name&gt; &lt;container&gt; – Setzt den Container für einen Server\n"
        "/logs &lt;name&gt; &lt;container&gt; – Zeigt die letzten 2000 Zeilen Docker-Logs\n"
        "/output &lt;name&gt; – Zeigt <code>ls -lh /mnt/output</code> für den Server\n\n"
        "<b>🔄 Status & Wartung</b>\n"
        "/s &lt;name&gt; – Zeigt Status, Uptime, Container-Logs und Speicherplatz\n"
        "/prune &lt;name&gt; – Prune output folders, wenn /dev/vdb &lt; 20G frei\n\n"
        "<b>⏸️/▶️ Benachrichtigungen</b>\n"
        "/stop &lt;name&gt; – Pausiert periodische Statusmeldungen für einen Server\n"
        "/resume &lt;name&gt; – Setzt periodische Statusmeldungen für einen Server fort\n\n"
        "<i>Alle Kommandos sind serverbasiert. Namen und Container müssen exakt wie eingetragen angegeben werden.</i>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

# === /stop Command ===
async def stop_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Bitte nutze: /stop <name>")
        return
    name = context.args[0].strip()
    servers = get_all_servers()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    set_server_value(ip, "periodic_running", False)
    await update.message.reply_text(f"Periodische Statusmeldungen für {name} gestoppt.")

# === /resume Command ===
async def resume_command(update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Bitte nutze: /resume <name>")
        return
    name = context.args[0].strip()
    servers = get_all_servers()
    ip = next((ip for ip, srv in servers.items() if srv.get('name') == name), None)
    if not ip:
        await update.message.reply_text(f"Kein VServer mit Name '{name}' gefunden.")
        return
    set_server_value(ip, "periodic_running", True)
    await update.message.reply_text(f"Periodische Statusmeldungen für {name} werden fortgesetzt.")


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
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("sc", sc))
    app.add_handler(CommandHandler("s", s_command))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("output", output_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("interval", interval_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("prune", prune_command))

    # Für alle Server Tasks starten
    global periodic_tasks
    servers = get_all_servers()
    if not servers:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="Bitte füge einen Server mit /add <ip> <name> hinzu.")
    else:
        for ip in servers:
            periodic_tasks[ip] = asyncio.create_task(periodic_check_server(app, ip))

    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())