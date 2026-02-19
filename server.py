import os
import json
import base64
import mimetypes
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
from flask import Flask, request, send_file, send_from_directory, jsonify, session, redirect
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# ============================================================
# CONFIGURATION
# ============================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = "gemini-2.0-flash"
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

app = Flask(__name__, static_folder='.', static_url_path='')

# ============================================================
# FIX POUR RENDER.COM - Permet à Flask de reconnaître HTTPS
# ============================================================
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "argos-secret-key-change-in-prod")

# ============================================================
# CONFIGURATION COOKIES POUR HTTPS/RENDER
# ============================================================
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='None',
)

CORS(app, supports_credentials=True, origins=[
    "https://argos-vuzs.onrender.com",
    "http://localhost:5000",
    "http://127.0.0.1:5000"
])

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def load_xml_template(xml_content):
    root = ET.fromstring(xml_content)
    fields = []
    champs = root.find("champs")
    if champs is not None:  # Fix DeprecationWarning
        for champ in champs.findall("champ"):
            field = {
                "nom": champ.findtext("nom", "").strip(),
                "obligatoire": champ.findtext("obligatoire", "false") == "true",
                "multiple": champ.findtext("multiple", "false") == "true",
                "type_donnee": champ.findtext("type_donnee", "string"),
                "description": champ.findtext("description", ""),
                "valeurs_possibles": []
            }
            valeurs = champ.find("valeurs_possibles")
            if valeurs is not None:  # Fix DeprecationWarning
                field["valeurs_possibles"] = [v.text.strip() for v in valeurs.findall("valeur") if v.text]
            fields.append(field)
    return fields, root

def load_source_file(content, filename):
    mime_type, _ = mimetypes.guess_type(filename)
    info = {"filename": filename, "mime_type": mime_type or "application/octet-stream", "content": None, "type": "unknown"}
    if mime_type and mime_type.startswith("image/"):
        info["type"] = "image"
        info["content"] = base64.b64encode(content).decode()
    elif mime_type == "application/pdf":
        info["type"] = "pdf"
        info["content"] = base64.b64encode(content).decode()
    else:
        info["type"] = "text"
        info["content"] = content.decode("utf-8", errors="ignore")
    return info

def build_prompt(fields):
    fields_desc = ""
    for f in fields:
        fields_desc += f"\n- {f['nom']} ({f['type_donnee']}): {f['description']}"
        if f['valeurs_possibles']: fields_desc += f" [Valeurs: {', '.join(f['valeurs_possibles'])}]"
        if f['obligatoire']: fields_desc += " [OBLIGATOIRE]"
        if f['multiple']: fields_desc += " [MULTIPLE]"
    return f"""Analyse ce document et extrais les informations suivantes.
CHAMPS À EXTRAIRE:{fields_desc}
RÉPONDS UNIQUEMENT EN JSON:
{{"extractions": [{{"nom": "...", "valeur_defaut": "...", "type_source": "...", "reference": "...", "explication": "..."}}], "analyse_globale": "..."}}"""

def build_email_prompt(fields):
    fields_desc = ""
    for f in fields:
        fields_desc += f"\n- {f['nom']} ({f['type_donnee']}): {f['description']}"
        if f['valeurs_possibles']: fields_desc += f" [Valeurs: {', '.join(f['valeurs_possibles'])}]"
        if f['obligatoire']: fields_desc += " [OBLIGATOIRE]"
    return f"""Analyse cet email (message + pièces jointes) et extrais:
CHAMPS:{fields_desc}
RÉPONDS EN JSON: {{"extractions": [{{"nom": "...", "valeur_defaut": "...", "type_source": "email_corps|email_sujet|piece_jointe|inference", "reference": "...", "explication": "..."}}], "analyse_globale": "..."}}"""

def extract_with_gemini(fields, source_info):
    prompt = build_prompt(fields)
    if source_info["type"] in ["image", "pdf"]:
        content = [{"mime_type": source_info["mime_type"], "data": source_info["content"]}, prompt]
    else:
        content = [f"{prompt}\n\nDOCUMENT:\n{source_info['content']}"]
    response = model.generate_content(content)
    text = response.text.strip()
    for prefix in ["```json", "```"]: 
        if text.startswith(prefix): text = text[len(prefix):]
    if text.endswith("```"): text = text[:-3]
    return json.loads(text.strip())

def extract_email_with_gemini(fields, email_data):
    prompt = build_email_prompt(fields)
    content_parts = []
    email_text = f"De: {email_data['from']}\nSujet: {email_data['subject']}\nDate: {email_data['date']}\n\n{email_data['body']}"
    for att in email_data.get('attachments', []):
        if att['type'] in ['image', 'pdf']:
            content_parts.append({"mime_type": att['mime_type'], "data": att['content']})
        elif att['type'] == 'text':
            email_text += f"\n\nPJ ({att['filename']}):\n{att['content']}"
    content_parts.append(f"{prompt}\n\nEMAIL:\n{email_text}")
    response = model.generate_content(content_parts)
    text = response.text.strip()
    for prefix in ["```json", "```"]: 
        if text.startswith(prefix): text = text[len(prefix):]
    if text.endswith("```"): text = text[:-3]
    return json.loads(text.strip())

def fill_xml_simple(template_content, extractions):
    """XML simplifie : uniquement <nom> et <valeur_defaut>"""
    ext_map = {e["nom"]: e for e in extractions.get("extractions", [])}
    template_root = ET.fromstring(template_content)
    champs_order = []
    champs_elem = template_root.find("champs")
    if champs_elem is not None:
        for champ in champs_elem.findall("champ"):
            nom = champ.findtext("nom", "").strip()
            if nom:
                champs_order.append(nom)
    root = ET.Element("extractions")
    for nom in champs_order:
        ext = ext_map.get(nom, {})
        champ_el = ET.SubElement(root, "champ")
        ET.SubElement(champ_el, "nom").text = nom
        v = ext.get("valeur_defaut", "")
        ET.SubElement(champ_el, "valeur_defaut").text = ", ".join(v) if isinstance(v, list) else str(v)
    xml_str = ET.tostring(root, encoding="unicode")
    return minidom.parseString(xml_str).toprettyxml(indent="  ")

def fill_xml_complet(template_content, extractions, source_filename):
    """XML complet : template rempli avec toutes les balises + source_detection + metadata"""
    root = ET.fromstring(template_content)
    ext_map = {e["nom"]: e for e in extractions.get("extractions", [])}
    champs = root.find("champs")
    if champs is not None:
        for champ in champs.findall("champ"):
            nom = champ.findtext("nom", "").strip()
            if nom in ext_map:
                ext = ext_map[nom]
                val_def = champ.find("valeur_defaut")
                if val_def is not None:
                    v = ext.get("valeur_defaut", "")
                    val_def.text = ", ".join(v) if isinstance(v, list) else str(v)
                src = champ.find("source_detection")
                if src is not None:
                    for tag in ["type_source", "reference", "explication"]:
                        elem = src.find(tag)
                        if elem is not None:
                            elem.text = ext.get(tag, "")
    meta = ET.SubElement(root, "metadata_extraction")
    ET.SubElement(meta, "date_extraction").text = datetime.now().isoformat()
    ET.SubElement(meta, "agent").text = "ARGOS"
    ET.SubElement(meta, "source").text = source_filename
    ET.SubElement(meta, "analyse").text = extractions.get("analyse_globale", "")
    xml_str = ET.tostring(root, encoding="unicode")
    return minidom.parseString(xml_str).toprettyxml(indent="  ")

def fill_xml(template_content, extractions, source_filename):
    """Alias conserve pour la compatibilite avec les routes extract et extract-batch (retourne le complet)"""
    return fill_xml_complet(template_content, extractions, source_filename)

# ============================================================
# GMAIL FUNCTIONS
# ============================================================

def get_gmail_flow():
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:5000/oauth/callback")
    return Flow.from_client_config(
        {"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
                 "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                 "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [redirect_uri]}},
        scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)

def get_credentials_from_session():
    """Récupère les credentials depuis la session Flask"""
    creds_data = session.get('gmail_credentials')
    if not creds_data:
        return None
    
    creds = Credentials(
        token=creds_data['token'],
        refresh_token=creds_data['refresh_token'],
        token_uri=creds_data['token_uri'],
        client_id=creds_data['client_id'],
        client_secret=creds_data['client_secret'],
        scopes=creds_data['scopes']
    )
    
    # Refresh si expiré
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Mettre à jour la session avec le nouveau token
            session['gmail_credentials']['token'] = creds.token
            session.modified = True
        except Exception as e:
            print(f"[Gmail] Token refresh failed: {e}")
            return None
    
    return creds

def get_label_id(service, label_name):
    for label in service.users().labels().list(userId='me').execute().get('labels', []):
        if label['name'].lower() == label_name.lower(): return label['id']
    return None

def get_emails_from_label(service, label_name):
    label_id = get_label_id(service, label_name)
    if not label_id: return []
    emails = []
    for msg in service.users().messages().list(userId='me', labelIds=[label_id]).execute().get('messages', []):
        email = get_email_details(service, msg['id'])
        if email: emails.append(email)
    return emails

def get_email_details(service, msg_id):
    try:
        msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        data = {'id': msg_id, 'from': '', 'subject': '', 'date': '', 'body': '', 'attachments': []}
        for h in msg['payload'].get('headers', []):
            if h['name'].lower() == 'from': data['from'] = h['value']
            elif h['name'].lower() == 'subject': data['subject'] = h['value']
            elif h['name'].lower() == 'date': data['date'] = h['value']
        extract_parts(service, msg_id, msg['payload'], data)
        return data
    except: return None

def extract_parts(service, msg_id, payload, data):
    if 'parts' in payload:
        for part in payload['parts']: extract_parts(service, msg_id, part, data)
    else:
        body = payload.get('body', {})
        if body.get('data'):
            decoded = base64.urlsafe_b64decode(body['data']).decode('utf-8', errors='ignore')
            if payload.get('mimeType') == 'text/plain' and not data['body']: data['body'] = decoded
            elif payload.get('mimeType') == 'text/html' and not data['body']: data['body'] = decoded
        elif body.get('attachmentId') and payload.get('filename'):
            try:
                att = service.users().messages().attachments().get(userId='me', messageId=msg_id, id=body['attachmentId']).execute()
                file_data = base64.urlsafe_b64decode(att['data'])
                mime, _ = mimetypes.guess_type(payload['filename'])
                info = {'filename': payload['filename'], 'mime_type': mime or 'application/octet-stream', 'type': 'unknown', 'content': None, 'raw_bytes': file_data}
                if mime and mime.startswith('image/'): info['type'], info['content'] = 'image', base64.b64encode(file_data).decode()
                elif mime == 'application/pdf': info['type'], info['content'] = 'pdf', base64.b64encode(file_data).decode()
                else: info['type'], info['content'] = 'text', file_data.decode('utf-8', errors='ignore')
                data['attachments'].append(info)
            except: pass

# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index(): return send_from_directory('.', 'argos.html')

@app.route('/api/extract', methods=['POST'])
def extract():
    try:
        source_file, template_file = request.files.get('source'), request.files.get('template')
        if not source_file or not template_file: return {"error": "Fichiers requis"}, 400
        template_content = template_file.read().decode('utf-8')
        fields, _ = load_xml_template(template_content)
        source_info = load_source_file(source_file.read(), source_file.filename)
        extractions = extract_with_gemini(fields, source_info)
        xml_complet = fill_xml_complet(template_content, extractions, source_file.filename)
        xml_simple = fill_xml_simple(template_content, extractions)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        stem = Path(source_file.filename).stem
        zip_path = Path(tempfile.gettempdir()) / f"argos_{ts}.zip"
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr(f"{stem}_complet.xml", xml_complet)
            zf.writestr(f"{stem}_simple.xml", xml_simple)
        return send_file(zip_path, mimetype='application/zip', as_attachment=True, download_name=zip_path.name)
    except Exception as e: return {"error": str(e)}, 500

@app.route('/api/extract-batch', methods=['POST'])
def extract_batch():
    try:
        template_file = request.files.get('template')
        if not template_file: return {"error": "Template requis"}, 400
        template_content = template_file.read().decode('utf-8')
        fields, _ = load_xml_template(template_content)
        sources = request.files.getlist('sources')
        if not sources: return {"error": "Fichiers requis"}, 400
        temp_dir = Path(tempfile.gettempdir()) / f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        temp_dir.mkdir(exist_ok=True)
        results = []
        for sf in sources:
            try:
                source_info = load_source_file(sf.read(), sf.filename)
                extractions = extract_with_gemini(fields, source_info)
                stem = Path(sf.filename).stem
                file_dir = temp_dir / stem
                file_dir.mkdir(exist_ok=True)
                (file_dir / f"{stem}_complet.xml").write_text(fill_xml_complet(template_content, extractions, sf.filename), encoding='utf-8')
                (file_dir / f"{stem}_simple.xml").write_text(fill_xml_simple(template_content, extractions), encoding='utf-8')
                results.append({"source": sf.filename, "status": "success"})
            except Exception as e: results.append({"source": sf.filename, "status": "error", "error": str(e)})
        zip_path = Path(tempfile.gettempdir()) / f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for f in temp_dir.rglob("*"):
                if f.is_file(): zf.write(f, f.relative_to(temp_dir))
            zf.writestr("_rapport.json", json.dumps({"results": results}, indent=2))
        import shutil; shutil.rmtree(temp_dir, ignore_errors=True)
        return send_file(zip_path, mimetype='application/zip', as_attachment=True)
    except Exception as e: return {"error": str(e)}, 500

@app.route('/api/gmail/auth')
def gmail_auth():
    try:
        flow = get_gmail_flow()
        url, state = flow.authorization_url(access_type='offline', prompt='consent')
        session['oauth_state'] = state
        session.modified = True
        print(f"[OAuth] Auth initiated")
        return jsonify({"auth_url": url})
    except Exception as e: 
        print(f"[OAuth] Auth error: {e}")
        return {"error": str(e)}, 500

@app.route('/oauth/callback')
def oauth_callback():
    try:
        print(f"[OAuth] Callback received")
        
        code = request.args.get('code')
        if not code:
            error = request.args.get('error', 'No code received')
            print(f"[OAuth] Error: {error}")
            return redirect('/?gmail_auth=error')
        
        flow = get_gmail_flow()
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        
        # Stocker les credentials DIRECTEMENT dans la session
        session['gmail_credentials'] = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': list(creds.scopes) if creds.scopes else GMAIL_SCOPES
        }
        session.modified = True
        
        print(f"[OAuth] Success! Credentials stored in session")
        return redirect('/?gmail_auth=success')
        
    except Exception as e:
        print(f"[OAuth] Callback error: {e}")
        import traceback
        traceback.print_exc()
        return redirect('/?gmail_auth=error')

@app.route('/api/gmail/status')
def gmail_status():
    connected = 'gmail_credentials' in session and session['gmail_credentials'] is not None
    print(f"[Gmail Status] Connected: {connected}")
    return jsonify({"connected": connected})

@app.route('/api/gmail/disconnect')
def gmail_disconnect():
    session.pop('gmail_credentials', None)
    session.modified = True
    return jsonify({"success": True})

@app.route('/api/extract-emails', methods=['POST'])
def extract_emails():
    try:
        print("[Extract Emails] Starting...")
        
        # Récupérer les credentials depuis la session
        creds = get_credentials_from_session()
        if not creds:
            print("[Extract Emails] No credentials found")
            return {"error": "Non connecté à Gmail"}, 401
        
        print("[Extract Emails] Credentials OK")
        
        template_file = request.files.get('template')
        if not template_file: 
            return {"error": "Template requis"}, 400
        
        template_content = template_file.read().decode('utf-8')
        fields, _ = load_xml_template(template_content)
        label = request.form.get('label', 'ARGOS')
        
        print(f"[Extract Emails] Looking for label: {label}")
        
        service = build('gmail', 'v1', credentials=creds)
        emails = get_emails_from_label(service, label)
        
        if not emails: 
            print(f"[Extract Emails] No emails found in label '{label}'")
            return {"error": f"Aucun email dans le label '{label}'"}, 404
        
        print(f"[Extract Emails] Found {len(emails)} emails")
        
        temp_dir = Path(tempfile.gettempdir()) / f"emails_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        temp_dir.mkdir(exist_ok=True)
        results = []
        
        for email in emails:
            try:
                print(f"[Extract Emails] Processing: {email['subject'][:30]}...")
                extractions = extract_email_with_gemini(fields, email)
                safe_subj = "".join(c for c in email['subject'][:20] if c.isalnum() or c in ' -_').strip().replace(' ', '_') or 'email'
                folder_name = f"{safe_subj}_{email['id'][:6]}"
                email_dir = temp_dir / folder_name
                email_dir.mkdir(exist_ok=True)

                # Sauvegarder les deux XML
                xml_complet = fill_xml_complet(template_content, extractions, f"Email: {email['subject']}")
                xml_simple = fill_xml_simple(template_content, extractions)
                (email_dir / "extraction_complet.xml").write_text(xml_complet, encoding='utf-8')
                (email_dir / "extraction_simple.xml").write_text(xml_simple, encoding='utf-8')

                # Sauvegarder les pièces jointes
                att_count = 0
                for att in email.get('attachments', []):
                    try:
                        att_filename = att.get('filename', f'piece_jointe_{att_count}')
                        raw = att.get('raw_bytes')
                        if raw:
                            (email_dir / att_filename).write_bytes(raw)
                        elif att['type'] == 'text':
                            (email_dir / att_filename).write_text(att['content'], encoding='utf-8')
                        att_count += 1
                    except Exception as att_e:
                        print(f"[Extract Emails] Attachment save error: {att_e}")

                results.append({"id": email['id'], "subject": email['subject'], "status": "success", "attachments": att_count})
            except Exception as e:
                print(f"[Extract Emails] Error processing email: {e}")
                results.append({"id": email['id'], "subject": email['subject'], "status": "error", "error": str(e)})

        zip_path = Path(tempfile.gettempdir()) / f"emails_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for f in temp_dir.rglob('*'):
                if f.is_file():
                    zf.write(f, f.relative_to(temp_dir))
            zf.writestr("_rapport.json", json.dumps({"label": label, "total": len(emails), "results": results}, indent=2))

        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        print(f"[Extract Emails] Done! Returning ZIP")
        return send_file(zip_path, mimetype='application/zip', as_attachment=True)
        
    except Exception as e:
        print(f"[Extract Emails] Error: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}, 500

@app.route('/debug/session')
def debug_session():
    return jsonify({
        "has_credentials": 'gmail_credentials' in session,
        "session_keys": list(session.keys())
    })

if __name__ == '__main__':
    print("🚀 ARGOS Server - http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)