#!/usr/bin/env python3
"""
Castrol Leads Mailer
Analiza conversaciones de Malena (ElevenLabs) y envia leads calientes por email.
"""

import os
import json
import base64
import requests
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pathlib import Path as _Path
_env = _Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
AGENT_ID           = "agent_8301kqmf5wh2fcgb4gbrgzadqg1g"  # Malena - Castrol prospectador
EMAIL_FROM         = "jbado@surtite.com"
EMAIL_TO           = "marcelo.otero@carrau.com.uy"
GMAIL_SCOPES       = ["https://www.googleapis.com/auth/gmail.send"]

BASE_DIR      = Path(__file__).parent
STATE_FILE    = BASE_DIR / "processed_conversations.json"
CREDS_FILE    = BASE_DIR / "credentials.json"   # Google OAuth2 client credentials
TOKEN_FILE    = BASE_DIR / "token.json"          # Auto-generado tras primer login

# ── Estado: conversaciones ya procesadas ──────────────────────────────────────
def load_processed() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()

def save_processed(ids: set):
    STATE_FILE.write_text(json.dumps(list(ids), indent=2))

# ── ElevenLabs ────────────────────────────────────────────────────────────────
def fetch_conversations(agent_id: str, page_size: int = 30) -> list:
    url = "https://api.elevenlabs.io/v1/convai/conversations"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    params  = {"agent_id": agent_id, "page_size": page_size}
    r = requests.get(url, headers=headers, params=params)
    r.raise_for_status()
    return r.json().get("conversations", [])

def fetch_transcript(conversation_id: str) -> str:
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()
    turns = data.get("transcript", [])
    lines = []
    for t in turns:
        role = "Malena" if t.get("role") == "agent" else "Cliente"
        msg  = (t.get("message") or "").strip()
        if msg and msg.lower() != "none":
            lines.append(f"{role}: {msg}")
    return "\n".join(lines)

# ── Claude: análisis de intención ─────────────────────────────────────────────
def analyze_lead(transcript: str, taller_name: str):
    """
    Retorna dict con análisis si hay intención de compra, None si no.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Eres un analista de ventas B2B. Analiza esta transcripción de una llamada telefónica de prospección de lubricantes Castrol a un taller mecánico llamado "{taller_name}".

TRANSCRIPCIÓN:
{transcript}

Determina:
1. ¿Hay intención de compra? (SÍ / NO)
2. Nivel: ALTA / MEDIA / BAJA (solo si es SÍ)
3. Nombre real del taller: buscalo en la primera frase que dice Malena (ej: "¡Hola! Coto Motos Soy Malena..." → el taller es "Coto Motos"). Si no aparece, usá "{taller_name}".
4. Dirección o ubicación mencionada (si no se mencionó, escribí "No capturada — confirmar antes de visitar")
5. Proveedor actual (si se menciona)
6. Horario disponible para visita (si no se mencionó, escribí "No capturado — confirmar con el cliente")
7. Otros datos de contacto (nombre del dueño, teléfono)
8. Resumen en 2-3 oraciones para el vendedor de calle
9. Tip táctico: una recomendación concreta para cerrar la venta en la visita

Responde SOLO en JSON con esta estructura exacta:
{{
  "tiene_intencion": true/false,
  "nivel": "ALTA|MEDIA|BAJA|null",
  "nombre_taller": "nombre real del taller",
  "direccion": "dirección o 'No capturada — confirmar antes de visitar'",
  "proveedor_actual": "nombre o null",
  "horario_visita": "descripción o 'No capturado — confirmar con el cliente'",
  "contacto": "nombre del dueño y teléfono si se mencionaron, o null",
  "resumen": "texto",
  "tip_vendedor": "texto"
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Extraer JSON aunque venga con backticks
    if "```" in raw:
        raw = raw.split("```")[1].replace("json", "").strip()

    analysis = json.loads(raw)
    if not analysis.get("tiene_intencion"):
        return None

    analysis["taller"] = analysis.get("nombre_taller") or taller_name
    return analysis

# ── Gmail ─────────────────────────────────────────────────────────────────────
def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)

def build_email_html(leads: list[dict]) -> str:
    fecha = datetime.now().strftime("%d de %B de %Y")
    cards = ""
    for i, lead in enumerate(leads, 1):
        nivel_color = "#c8102e" if lead["nivel"] == "ALTA" else "#e67e22"
        direccion_val = lead.get("direccion") or "No capturada — confirmar antes de visitar"
        direccion_color = "#c0392b" if "No capturada" in direccion_val else "#27ae60"
        proveedor = f"<li><strong>Proveedor actual:</strong> {lead['proveedor_actual']}</li>" if lead.get("proveedor_actual") else ""
        horario   = f"<li><strong>Horario para visita:</strong> {lead.get('horario_visita', 'No capturado — confirmar con el cliente')}</li>"
        contacto  = f"<li><strong>Contacto (dueño/teléfono):</strong> {lead['contacto']}</li>" if lead.get("contacto") else ""
        tip       = f'<p style="background:#fff3cd;padding:8px;border-radius:4px;font-size:13px;">⚡ <strong>Tip:</strong> {lead["tip_vendedor"]}</p>' if lead.get("tip_vendedor") else ""

        cards += f"""
        <table style="width:100%;background:#fff8f8;border-left:4px solid {nivel_color};margin-bottom:20px;border-radius:4px;">
          <tr><td style="padding:14px;">
            <h3 style="margin:0 0 6px 0;color:#c8102e;">🏪 {lead['taller']}</h3>
            <p style="font-size:16px;font-weight:bold;color:{direccion_color};margin:4px 0;">📍 {direccion_val}</p>
            <p><strong>Intención:</strong> <span style="color:green;font-weight:bold;">✅ SÍ — {lead['nivel']}</span></p>
            <p>{lead['resumen']}</p>
            <ul>
              {proveedor}
              {horario}
              {contacto}
            </ul>
            {tip}
          </td></tr>
        </table>"""

    return f"""<html><body style="font-family:Arial,sans-serif;color:#222;max-width:700px;margin:auto;padding:20px;">
    <h2 style="color:#c8102e;">🔥 Leads en caliente — Castrol Prospectador</h2>
    <p style="color:#666;font-size:13px;">{fecha} · {len(leads)} lead(s) con intención de compra</p>
    <hr style="border:1px solid #c8102e;">
    {cards}
    <p style="font-size:11px;color:#aaa;margin-top:30px;">Generado automáticamente · Agente Malena - Castrol prospectador</p>
    </body></html>"""

def send_email(service, subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅ Email enviado a {EMAIL_TO}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Iniciando análisis de conversaciones...")

    processed = load_processed()
    conversations = fetch_conversations(AGENT_ID)
    new_convs = [c for c in conversations if c["conversation_id"] not in processed]

    if not new_convs:
        print("Sin conversaciones nuevas.")
        return

    print(f"{len(new_convs)} conversación(es) nueva(s) a analizar.")

    leads = []
    for conv in new_convs:
        cid        = conv["conversation_id"]
        msg_count  = conv.get("message_count", 0)
        status     = conv.get("status", "")
        taller     = "Taller no identificado"

        metadata = conv.get("metadata", {})
        if metadata.get("caller_id"):
            taller = metadata.get("caller_id")

        processed.add(cid)

        if status != "done" or msg_count < 3:
            print(f"  ⏭  {cid} — omitido (status={status}, msgs={msg_count})")
            continue

        print(f"  🔍 Analizando {cid} ({msg_count} mensajes)...")
        transcript = fetch_transcript(cid)

        if not transcript:
            continue

        # Intentar extraer nombre del taller del primer saludo de Malena
        # Formato: "Malena: ¡Hola! NOMBRE_TALLER Soy Malena..."
        for line in transcript.split("\n"):
            if line.startswith("Malena:") and "Soy Malena" in line:
                parts = line.replace("Malena:", "").replace("¡Hola!", "").strip()
                nombre = parts.split("Soy Malena")[0].strip().rstrip(",").strip()
                if nombre:
                    taller = nombre
                break

        lead = analyze_lead(transcript, taller)
        if lead:
            leads.append(lead)
            print(f"    ✅ Lead detectado: {lead.get('taller', taller)} — {lead['nivel']}")
        else:
            print(f"    ➖ Sin intención de compra")

    save_processed(processed)

    if not leads:
        print("No hay leads con intención de compra en este lote.")
        return

    print(f"\n📧 Enviando reporte con {len(leads)} lead(s)...")
    subject   = f"🔥 {len(leads)} lead(s) en caliente — Castrol | {datetime.now():%d/%m/%Y}"
    html_body = build_email_html(leads)
    service   = get_gmail_service()
    send_email(service, subject, html_body)


if __name__ == "__main__":
    main()
