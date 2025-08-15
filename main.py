import os
import re
import json
from cachetools import TTLCache
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# === ENV ===
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SVC_JSON = os.environ["GOOGLE_SVC_JSON"]

# === Slack App ===
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
    """
    Lee la hoja:
    Hoja 1 -> columnas:
      Dominio | Cliente | ClientChannelID | PodChannelID | ManagerSlackID | Activo
    """
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

def extract_domain(text: str):
    """
    Busca el dominio en la línea "Desde qué mail salió la reunión:"
    """
    for line in text.splitlines():
        if "Desde qué mail salió la reunión" in line:
            match = re.search(r'@([\w\.-]+)', line)
            if match:
                return match.group(1).lower().strip()
    return None

def fetch_message(client, channel, ts):
    resp = client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
    msgs = resp.get("messages", [])
    return msgs[0] if msgs else None

@app.event("reaction_added")
def handle_reaction_added(body, client, logger):
    try:
        event = body.get("event", {})
        user = event.get("user")
        item = event.get("item", {})
        source_channel = item.get("channel")
        ts = item.get("ts")

        # 1) Mensaje original
        msg = fetch_message(client, source_channel, ts)
        if not msg:
            return

        text = msg.get("text", "")
        domain = extract_domain(text)
        if not domain:
            logger.info("No se encontró dominio en el mensaje")
            return

        # 2) Buscar dominio en mapeo
        mapping = get_mapping()
        row = next((r for r in mapping if r.get("Dominio", "").lower() == domain), None)
        if not row:
            logger.info(f"Dominio {domain} no está en el mapeo")
            return

        # 3) Validaciones
        if row.get("Activo", "").strip().lower() != "true":
            logger.info(f"Cliente {row.get('Cliente')} no está activo")
            return

        if user != row.get("ManagerSlackID", "").strip():
            logger.info(f"Usuario {user} no autorizado para cliente {row.get('Cliente')}")
            return

        # 4) Canal destino (solo cliente)
        client_channel = row.get("ClientChannelID", "").strip()
        if not client_channel:
            logger.info(f"Cliente {row.get('Cliente')} no tiene canal asignado")
            return

        client.chat_postMessage(channel=client_channel, text=text)
        logger.info(f"Enviado SOLO a canal cliente {client_channel} para {row.get('Cliente')}")

    except Exception as e:
        logger.error(f"Error en handle_reaction_added: {e}")

if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
