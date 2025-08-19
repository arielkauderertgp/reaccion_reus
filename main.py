import os
import re
import json
from cachetools import TTLCache
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from slack_sdk.errors import SlackApiError

# === ENV ===
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SVC_JSON = os.environ["GOOGLE_SVC_JSON"]

app = App(token=SLACK_BOT_TOKEN)

# === Google Sheets ===
def sheets_service():
    data = json.loads(GOOGLE_SVC_JSON)
    creds = Credentials.from_service_account_info(
        data,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)

SVC = sheets_service()

# Cache para no recargar el sheet en cada evento
cache_map = TTLCache(maxsize=1, ttl=60)

def get_mapping():
    # Hoja: Hoja 1  |  A:F = Dominio, Cliente, ClientChannelID, PodChannelID, ManagerSlackID, Activo
    if "map" in cache_map:
        return cache_map["map"]
    sheet = SVC.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Hoja 1!A1:F999"
    ).execute()
    values = sheet.get("values", [])
    if not values:
        return []
    hdr = values[0]
    rows = [dict(zip(hdr, r)) for r in values[1:]]
    cache_map["map"] = rows
    return rows

def collect_text_from_blocks(blocks):
    """Extrae texto plano de bloques (section, rich_text, etc.)."""
    out = []
    if not isinstance(blocks, list):
        return ""
    for b in blocks:
        # section con text
        txt = b.get("text", {})
        if isinstance(txt, dict) and txt.get("type") == "mrkdwn" and txt.get("text"):
            out.append(txt["text"])
        # rich_text
        if b.get("type") == "rich_text":
            for el in b.get("elements", []):
                if el.get("type") == "rich_text_section":
                    for e in el.get("elements", []):
                        if e.get("type") == "text" and e.get("text"):
                            out.append(e["text"])
                        if e.get("type") == "link" and e.get("url"):
                            out.append(e["url"])
    return "\n".join(out)

def get_full_text(msg):
    base = msg.get("text") or ""
    blocks_txt = collect_text_from_blocks(msg.get("blocks"))
    attachments_txt = ""
    for att in msg.get("attachments", []) or []:
        if isinstance(att, dict) and att.get("text"):
            attachments_txt += "\n" + att["text"]
    full = "\n".join([t for t in [base, blocks_txt, attachments_txt] if t])
    return full

def extract_domain_from_text(text: str):
    """
    Busca el dominio en la línea "Desde qué mail salió la reunión".
    Soporta linkeos tipo <mailto:user@dominio|user@dominio>.
    """
    for line in text.splitlines():
        if "Desde qué mail salió la reunión" in line:
            # busca patrón user@dominio dentro de la línea siguiente también
            # (muchas veces el correo queda en la línea de abajo)
            pass
    # Si no apareció en esa línea, buscamos el primer mail que aparezca después
    # del encabezado "Desde qué mail salió la reunión"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "Desde qué mail salió la reunión" in line:
            # inspecciona la misma y las siguientes 3 líneas
            for j in range(i, min(i+4, len(lines))):
                m = re.search(r'<mailto:[^@]+@([^>|]+)[^>]*>|[^<\s]+@([^\s>]+)', lines[j])
                if m:
                    domain = (m.group(1) or m.group(2)).strip().lower()
                    # limpia posibles caracteres raros finales
                    domain = re.sub(r'[>\|\)\]\.,;:]+$', '', domain)
                    return domain
            break
    # fallback: primer correo del texto completo
    m = re.search(r'<mailto:[^@]+@([^>|]+)[^>]*>|[^<\s]+@([^\s>]+)', text)
    if m:
        return (m.group(1) or m.group(2)).strip().lower()
    return None

def fetch_message_or_reply(client, channel, ts):
    """
    Intenta traer el mensaje:
    - primero como mensaje 'top-level' (conversations_history)
    - si no aparece, intenta como reply (conversations_replies)
    """
    try:
        resp = client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
        msgs = resp.get("messages", [])
        if msgs and msgs[0].get("ts") == ts:
            return msgs[0]
    except SlackApiError:
        pass
    # intenta como reply de un hilo
    try:
        resp2 = client.conversations_replies(channel=channel, ts=ts, limit=1)
        msgs2 = resp2.get("messages", [])
        if msgs2:
            return msgs2[0]
    except SlackApiError:
        pass
    return None

@app.event("reaction_added")
def handle_reaction_added(body, client, logger):
    try:
        event = body.get("event", {})
        user = event.get("user")
        item = event.get("item", {})
        source_channel = item.get("channel")
        ts = item.get("ts")
        emoji = event.get("reaction")

        logger.info(f"[reaction_added] user={user} emoji=:{emoji}: channel={source_channel} ts={ts}")

        # 1) Mensaje original (soporta hilo)
        msg = fetch_message_or_reply(client, source_channel, ts)
        if not msg:
            logger.info("No se pudo obtener el mensaje (ni top-level ni reply).")
            return

        full_text = get_full_text(msg)
        if not full_text:
            logger.info("El mensaje no tiene texto para analizar (text/blocks vacíos).")
            return

        domain = extract_domain_from_text(full_text)
        logger.info(f"Texto detectado len={len(full_text)} / dominio={domain}")
        if not domain:
            logger.info("No se encontró dominio en el mensaje.")
            return

        # 2) Buscar dominio en mapeo
        mapping = get_mapping()
        row = next((r for r in mapping if r.get("Dominio", "").lower() == domain), None)
        if not row:
            logger.info(f"Dominio {domain} no está en el mapeo.")
            return

        # 3) Validaciones
        if row.get("Activo", "").strip().lower() != "true":
            logger.info(f"Cliente {row.get('Cliente')} no está activo.")
            return

        if user != (row.get("ManagerSlackID", "") or "").strip():
            logger.info(f"Usuario {user} no autorizado para cliente {row.get('Cliente')} (esperado {row.get('ManagerSlackID')}).")
            return

        # 4) Canal destino (solo cliente)
        client_channel = (row.get("ClientChannelID", "") or "").strip()
        if not client_channel:
            logger.info(f"Cliente {row.get('Cliente')} no tiene canal asignado.")
            return

        # Asegúrate de que el bot esté invitado al canal destino
        try:
            client.chat_postMessage(channel=client_channel, text=full_text)
            logger.info(f"Enviado SOLO a canal cliente {client_channel} para {row.get('Cliente')}.")
        except SlackApiError as e:
            logger.error(f"Error enviando a canal destino {client_channel}: {e.response.data}")

    except Exception as e:
        logger.error(f"Error en handle_reaction_added: {e}")

if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
