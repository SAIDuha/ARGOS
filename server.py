import os
import json
import base64
import mimetypes
import tempfile
import zipfile
import requests as http_requests
import re
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
try:
    import fitz  # pymupdf
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
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
# FIX POUR RENDER.COM
# ============================================================
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "argos-secret-key-change-in-prod")

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
# PARSING DU TEMPLATE XML — NOUVELLE ARCHITECTURE
# ============================================================

def parse_champ(champ_elem):
    """Parse un <champ> classique, compatible ancien ET nouveau format."""
    field = {
        "type": "champ",
        "nom": champ_elem.findtext("nom", "").strip(),
        "obligatoire": champ_elem.findtext("obligatoire", "false") == "true",
        "multiple": champ_elem.findtext("multiple", "false") == "true",
        "type_donnee": champ_elem.findtext("type_donnee", "string"),
        "description": champ_elem.findtext("description", ""),
        "valeurs_possibles": [],
        "descriptions_externes": []
    }

    valeurs_elem = champ_elem.find("valeurs_possibles")
    if valeurs_elem is not None:
        for child in valeurs_elem:
            if child.tag == "valeur":
                # Ancien format simple : <valeur>XS</valeur>
                if child.text and child.text.strip():
                    field["valeurs_possibles"].append({
                        "valeur": child.text.strip(),
                        "description": ""
                    })
            elif child.tag == "valeur_possible":
                # Nouveau format enrichi : <valeur_possible><valeur>XS</valeur><description>...</description></valeur_possible>
                v = child.findtext("valeur", "").strip()
                d = child.findtext("description", "").strip()
                if v:
                    field["valeurs_possibles"].append({"valeur": v, "description": d})

    descs_ext = champ_elem.find("Descriptions_Externes")
    if descs_ext is not None:
        for de in descs_ext.findall("Description_Externe"):
            if de.text and de.text.strip():
                field["descriptions_externes"].append(de.text.strip())

    return field


def parse_champRGRS(rgrs_elem):
    """Parse un <champRGRS> (groupe de sous-champs structurés)."""
    groupe = {
        "type": "groupe",
        "nom": rgrs_elem.findtext("Nom", "").strip(),
        "multiple": rgrs_elem.findtext("Multiple", "false").strip().lower() == "true",
        "description": rgrs_elem.findtext("Description", "").strip(),
        "sous_champs": [],
        "descriptions_externes": []
    }

    for rgr in rgrs_elem.findall("champRGR"):
        sous_champ = {
            "nom": rgr.findtext("Nom", "").strip(),
            "multiple": rgr.findtext("Multiple", "false").strip().lower() == "true",
            "description": rgr.findtext("Description", "").strip()
        }
        if sous_champ["nom"]:
            groupe["sous_champs"].append(sous_champ)

    descs_ext = rgrs_elem.find("Descriptions_Externes")
    if descs_ext is not None:
        for de in descs_ext.findall("Description_Externe"):
            if de.text and de.text.strip():
                groupe["descriptions_externes"].append(de.text.strip())

    return groupe


def load_xml_template(xml_content):
    """Parse le template XML — supporte champs classiques ET groupes champRGRS."""
    root = ET.fromstring(xml_content)
    fields = []
    champs_elem = root.find("champs")
    if champs_elem is not None:
        for child in champs_elem:
            if child.tag == "champ":
                f = parse_champ(child)
                if f["nom"]:
                    fields.append(f)
            elif child.tag == "champRGRS":
                g = parse_champRGRS(child)
                if g["nom"]:
                    fields.append(g)
    return fields, root


# ============================================================
# GESTION DES DESCRIPTIONS EXTERNES (URLs)
# ============================================================

def resolve_drive_url(url):
    """Convertit une URL Google Drive partagée en URL de téléchargement direct."""
    match = re.search(r'/file/d/([a-zA-Z0-9_-]+)', url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


def fetch_external_url(url):
    """
    Télécharge une ressource externe (image, PDF) et retourne
    un dict Gemini-compatible ou une chaîne de fallback.
    """
    try:
        resolved = resolve_drive_url(url)
        resp = http_requests.get(resolved, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            mime = resp.headers.get('content-type', '').split(';')[0].strip()
            if mime.startswith('image/'):
                return {"mime_type": mime, "data": base64.b64encode(resp.content).decode()}
            elif 'pdf' in mime:
                return {"mime_type": "application/pdf", "data": base64.b64encode(resp.content).decode()}
            else:
                return f"[Référence externe - {url}]"
        else:
            return f"[Référence externe non accessible ({resp.status_code}) - {url}]"
    except Exception as e:
        print(f"[External URL] Échec fetch {url}: {e}")
        return f"[Référence externe indisponible - {url}]"


def collect_external_parts(fields):
    """
    Collecte toutes les descriptions externes de tous les champs/groupes
    et retourne une liste de parts Gemini (images/PDFs ou texte).
    """
    parts = []
    for f in fields:
        for url in f.get("descriptions_externes", []):
            result = fetch_external_url(url)
            parts.append(result)
    return parts


# ============================================================
# CONSTRUCTION DES PROMPTS
# ============================================================

def build_fields_description(fields):
    """Génère la description textuelle des champs pour le prompt."""
    desc = ""
    for f in fields:
        if f["type"] == "champ":
            desc += f"\n- {f['nom']} ({f['type_donnee']}): {f['description']}"
            if f['valeurs_possibles']:
                vals_str = []
                for v in f['valeurs_possibles']:
                    entry = v['valeur']
                    if v['description']:
                        entry += f" ({v['description']})"
                    vals_str.append(entry)
                desc += f" [Valeurs autorisées: {', '.join(vals_str)}]"
            if f['obligatoire']:
                desc += " [OBLIGATOIRE]"
            if f['multiple']:
                desc += " [MULTIPLE]"
            if f['descriptions_externes']:
                desc += f" [Voir exemples visuels: {', '.join(f['descriptions_externes'])}]"

        elif f["type"] == "groupe":
            desc += f"\n- GROUPE '{f['nom']}' (multiple={'oui' if f['multiple'] else 'non'}): {f['description']}"
            desc += "\n  Sous-champs à extraire:"
            for sc in f['sous_champs']:
                desc += f"\n    * {sc['nom']}: {sc['description']}"
                if sc['multiple']:
                    desc += " [MULTIPLE]"
            if f['descriptions_externes']:
                desc += f"\n  [Voir exemples visuels: {', '.join(f['descriptions_externes'])}]"
    return desc



def build_json_format_doc(fields):
    """Génère la documentation du format JSON attendu en réponse."""
    champ_ex = '{"nom": "NOM_DU_CHAMP", "valeur_defaut": "VALEUR_REELLE_EXTRAITE", "type_source": "email_corps|email_sujet|piece_jointe|inference", "reference": "EXTRAIT_OU_LOCALISATION", "explication": "POURQUOI_CETTE_VALEUR"}'
    groupe_ex = '{"nom": "NOM_DU_GROUPE", "type": "groupe", "instances": [{"sous_champ1": "VALEUR1", "sous_champ2": "VALEUR2"}], "explication": "POURQUOI_CES_INSTANCES"}'

    has_groupes = any(f["type"] == "groupe" for f in fields)
    examples = [champ_ex]
    if has_groupes:
        examples.append(groupe_ex)

    return (
        '{"extractions": [' + ", ".join(examples) + '], "analyse_globale": "SYNTHESE_GLOBALE_DU_DOCUMENT"}\n\n'
        "IMPORTANT: Remplace CHAQUE valeur en MAJUSCULES par la vraie information extraite du document.\n"
        "Ne copie JAMAIS les placeholders en majuscules ni \"...\" comme valeur reelle.\n"
        "Un champ obligatoire NE PEUT PAS avoir une valeur vide ou un placeholder."
    )


def build_motif_list(fields):
    """
    Extrait la liste complète des motifs depuis le champ 'motif' du template.
    Retourne une chaîne formatée motif → définition.
    """
    for f in fields:
        if f.get("type") == "champ" and f.get("nom") == "motif":
            lines = []
            for v in f.get("valeurs_possibles", []):
                line = f"  • {v['valeur']}"
                if v.get("description"):
                    line += f" → {v['description']}"
                lines.append(line)
            return "\n".join(lines)
    return ""


def build_prompt(fields):
    fields_desc = build_fields_description(fields)
    motif_list = build_motif_list(fields)
    json_fmt = build_json_format_doc(fields)

    return f"""Analyse ce document et extrais les informations demandées.

══════════════════════════════════════════
ORDRE DE RAISONNEMENT OBLIGATOIRE
══════════════════════════════════════════
Tu dois raisonner dans CET ordre précis :

ÉTAPE 1 — IDENTIFIER LE MOTIF
  Lis attentivement le document.
  Trouve le motif qui correspond le mieux au sujet parmi cette liste officielle :
{motif_list}

ÉTAPE 2 — DÉDUIRE LE TYPE ET LA NATURE
  Chaque motif commence par un préfixe entre crochets, ex: [Demande commerciale] ou [Facturation].
  Ce préfixe te donne directement :
  - Si le préfixe contient "Demande", "Information", "Modification", "Interventions",
    "Paramétrage", "Qualité / HSE", "Suivi commande" → type_sollicitation = "Demande"
    et nature_demande = le texte du préfixe (sans les crochets).
  - Sinon (ex: "Facturation", "livraison non effectuée", "manquants / erreurs articles",
    "Mise en place", "Qualité / produit", "qualité / service", "Réclamation en doublon",
    "Relance Réclamation et/ou demande", "Suivi Client / Fin de contrat", "Sur stock")
    → type_sollicitation = "Réclamation" et nature_reclamation = le texte du préfixe.
  - nature_demande reste vide si c'est une Réclamation.
  - nature_reclamation reste vide si c'est une Demande.

ÉTAPE 3 — EXTRAIRE TOUS LES AUTRES CHAMPS
  Une fois motif, type_sollicitation et nature déterminés, extrais les autres champs :
{fields_desc}

══════════════════════════════════════════
RÈGLES GÉNÉRALES
══════════════════════════════════════════
- Respecte STRICTEMENT les valeurs autorisées pour chaque champ.
- Pour les GROUPES, retourne une instance par occurrence détectée.
- Si une information est absente, utilise une chaîne vide.

RÉPONDS UNIQUEMENT EN JSON valide :
{json_fmt}"""


def build_email_prompt(fields):
    fields_desc = build_fields_description(fields)
    motif_list = build_motif_list(fields)
    json_fmt = build_json_format_doc(fields)

    return f"""Analyse cet email (corps + pièces jointes) et extrais les informations demandées.

══════════════════════════════════════════
ORDRE DE RAISONNEMENT OBLIGATOIRE
══════════════════════════════════════════
Tu dois raisonner dans CET ordre précis :

ÉTAPE 1 — IDENTIFIER LE MOTIF
  Lis attentivement le corps de l'email et ses pièces jointes.
  Trouve le motif qui correspond le mieux au sujet parmi cette liste officielle :
{motif_list}

ÉTAPE 2 — DÉDUIRE LE TYPE ET LA NATURE
  Chaque motif commence par un préfixe entre crochets, ex: [Demande commerciale] ou [Facturation].
  Ce préfixe te donne directement :
  - Si le préfixe contient "Demande", "Information", "Modification", "Interventions",
    "Paramétrage", "Qualité / HSE", "Suivi commande" → type_sollicitation = "Demande"
    et nature_demande = le texte du préfixe (sans les crochets).
  - Sinon (ex: "Facturation", "livraison non effectuée", "manquants / erreurs articles",
    "Mise en place", "Qualité / produit", "qualité / service", "Réclamation en doublon",
    "Relance Réclamation et/ou demande", "Suivi Client / Fin de contrat", "Sur stock")
    → type_sollicitation = "Réclamation" et nature_reclamation = le texte du préfixe.
  - nature_demande reste vide si c'est une Réclamation.
  - nature_reclamation reste vide si c'est une Demande.

ÉTAPE 3 — EXTRAIRE TOUS LES AUTRES CHAMPS
  Une fois motif, type_sollicitation et nature déterminés, extrais les autres champs :
{fields_desc}

══════════════════════════════════════════
RÈGLES GÉNÉRALES
══════════════════════════════════════════
- type_source possible : email_corps | email_sujet | piece_jointe | inference
- Respecte STRICTEMENT les valeurs autorisées pour chaque champ.
- Pour les GROUPES, retourne une instance par occurrence détectée.
- Si une information est absente, utilise une chaîne vide.

RÉPONDS EN JSON valide :
{json_fmt}"""


# ============================================================
# EXTRACTION GEMINI
# ============================================================

def clean_json_response(text):
    text = text.strip()
    for prefix in ["```json", "```"]:
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())


def extract_with_gemini(fields, source_info):
    prompt = build_prompt(fields)
    content_parts = []

    # Ajouter les références externes (images/PDFs d'exemples)
    ext_parts = collect_external_parts(fields)
    for ep in ext_parts:
        if isinstance(ep, dict):
            content_parts.append(ep)
        else:
            # Texte fallback : on l'incorpore dans le prompt
            prompt = ep + "\n\n" + prompt

    # Ajouter le document source
    if source_info["type"] in ["image", "pdf"]:
        content_parts.append({"mime_type": source_info["mime_type"], "data": source_info["content"]})
    else:
        prompt = f"{prompt}\n\nDOCUMENT:\n{source_info['content']}"

    content_parts.append(prompt)
    response = model.generate_content(content_parts)
    return clean_json_response(response.text)


def extract_email_with_gemini(fields, email_data):
    prompt = build_email_prompt(fields)
    content_parts = []

    # Références externes
    ext_parts = collect_external_parts(fields)
    for ep in ext_parts:
        if isinstance(ep, dict):
            content_parts.append(ep)
        else:
            prompt = ep + "\n\n" + prompt

    # Corps de l'email
    email_text = f"De: {email_data['from']}\nSujet: {email_data['subject']}\nDate: {email_data['date']}\n\n{email_data['body']}"

    # Pièces jointes
    for att in email_data.get('attachments', []):
        if att['type'] in ['image', 'pdf']:
            content_parts.append({"mime_type": att['mime_type'], "data": att['content']})
        elif att['type'] == 'text':
            email_text += f"\n\nPJ ({att['filename']}):\n{att['content']}"

    content_parts.append(f"{prompt}\n\nEMAIL:\n{email_text}")
    response = model.generate_content(content_parts)
    return clean_json_response(response.text)


# ============================================================
# GÉNÉRATION DES XML DE SORTIE
# ============================================================

def build_ext_map(extractions):
    """Construit un dict nom -> extraction depuis la liste Gemini."""
    return {e["nom"]: e for e in extractions.get("extractions", [])}


def render_groupe_instances(groupe_el, instances):
    """Écrit les instances d'un groupe dans l'élément XML parent."""
    instances_el = ET.SubElement(groupe_el, "instances")
    for inst in instances:
        inst_el = ET.SubElement(instances_el, "instance")
        for k, v in inst.items():
            tag = re.sub(r'[^a-zA-Z0-9_\-]', '_', str(k))
            child = ET.SubElement(inst_el, tag)
            child.text = str(v) if v is not None else ""


def fill_xml_simple(template_content, extractions):
    """XML simplifié : <nom> + <valeur_defaut> pour les champs, instances pour les groupes."""
    ext_map = build_ext_map(extractions)
    template_root = ET.fromstring(template_content)
    root = ET.Element("extractions")

    champs_elem = template_root.find("champs")
    if champs_elem is not None:
        for child in champs_elem:
            if child.tag == "champ":
                nom = child.findtext("nom", "").strip()
                ext = ext_map.get(nom, {})
                el = ET.SubElement(root, "champ")
                ET.SubElement(el, "nom").text = nom
                v = ext.get("valeur_defaut", "")
                ET.SubElement(el, "valeur_defaut").text = ", ".join(v) if isinstance(v, list) else str(v)

            elif child.tag == "champRGRS":
                nom = child.findtext("Nom", "").strip()
                ext = ext_map.get(nom, {})
                el = ET.SubElement(root, "groupe")
                ET.SubElement(el, "nom").text = nom
                instances = ext.get("instances", [])
                if instances:
                    render_groupe_instances(el, instances)

    xml_str = ET.tostring(root, encoding="unicode")
    return minidom.parseString(xml_str).toprettyxml(indent="  ")


def fill_xml_complet(template_content, extractions, source_filename):
    """XML complet : template rempli avec source_detection + groupes + metadata."""
    root = ET.fromstring(template_content)
    ext_map = build_ext_map(extractions)

    champs_elem = root.find("champs")
    if champs_elem is not None:
        # Champs classiques
        for champ in champs_elem.findall("champ"):
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

        # Groupes champRGRS
        for rgrs in champs_elem.findall("champRGRS"):
            nom = rgrs.findtext("Nom", "").strip()
            if nom in ext_map:
                ext = ext_map[nom]
                instances = ext.get("instances", [])
                # Supprimer les anciennes instances si présentes
                old = rgrs.find("extractions_groupes")
                if old is not None:
                    rgrs.remove(old)
                # Ajouter le bloc d'extractions
                ext_el = ET.SubElement(rgrs, "extractions_groupes")
                ET.SubElement(ext_el, "explication").text = ext.get("explication", "")
                if instances:
                    render_groupe_instances(ext_el, instances)

    # Metadata globale
    meta = ET.SubElement(root, "metadata_extraction")
    ET.SubElement(meta, "date_extraction").text = datetime.now().isoformat()
    ET.SubElement(meta, "agent").text = "ARGOS"
    ET.SubElement(meta, "source").text = source_filename
    ET.SubElement(meta, "analyse").text = extractions.get("analyse_globale", "")

    xml_str = ET.tostring(root, encoding="unicode")
    return minidom.parseString(xml_str).toprettyxml(indent="  ")


def fill_xml(template_content, extractions, source_filename):
    """Alias de compatibilité."""
    return fill_xml_complet(template_content, extractions, source_filename)


# ============================================================
# HELPER FICHIER SOURCE
# ============================================================

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


# ============================================================
# GMAIL FUNCTIONS
# ============================================================

def get_gmail_flow():
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:5000/oauth/callback")
    return Flow.from_client_config(
        {"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
                 "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                 "token_uri": "https://oauth2.googleapis.com/token",
                 "redirect_uris": [redirect_uri]}},
        scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)


def get_credentials_from_session():
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
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            session['gmail_credentials']['token'] = creds.token
            session.modified = True
        except Exception as e:
            print(f"[Gmail] Token refresh failed: {e}")
            return None
    return creds


def get_label_id(service, label_name):
    for label in service.users().labels().list(userId='me').execute().get('labels', []):
        if label['name'].lower() == label_name.lower():
            return label['id']
    return None


def get_emails_from_label(service, label_name):
    label_id = get_label_id(service, label_name)
    if not label_id:
        return []
    emails = []
    for msg in service.users().messages().list(userId='me', labelIds=[label_id]).execute().get('messages', []):
        email = get_email_details(service, msg['id'])
        if email:
            emails.append(email)
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
    except:
        return None


def extract_parts(service, msg_id, payload, data):
    if 'parts' in payload:
        for part in payload['parts']:
            extract_parts(service, msg_id, part, data)
    else:
        body = payload.get('body', {})
        if body.get('data'):
            decoded = base64.urlsafe_b64decode(body['data']).decode('utf-8', errors='ignore')
            if payload.get('mimeType') == 'text/plain' and not data['body']:
                data['body'] = decoded
            elif payload.get('mimeType') == 'text/html' and not data['body']:
                data['body'] = decoded
        elif body.get('attachmentId') and payload.get('filename'):
            try:
                att = service.users().messages().attachments().get(userId='me', messageId=msg_id, id=body['attachmentId']).execute()
                file_data = base64.urlsafe_b64decode(att['data'])
                mime, _ = mimetypes.guess_type(payload['filename'])
                info = {'filename': payload['filename'], 'mime_type': mime or 'application/octet-stream',
                        'type': 'unknown', 'content': None, 'raw_bytes': file_data}
                if mime and mime.startswith('image/'):
                    info['type'], info['content'] = 'image', base64.b64encode(file_data).decode()
                elif mime == 'application/pdf':
                    info['type'], info['content'] = 'pdf', base64.b64encode(file_data).decode()
                else:
                    info['type'], info['content'] = 'text', file_data.decode('utf-8', errors='ignore')
                data['attachments'].append(info)
            except:
                pass


# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    return send_from_directory('.', 'argos.html')


@app.route('/api/extract', methods=['POST'])
def extract():
    try:
        source_file = request.files.get('source')
        template_file = request.files.get('template')
        if not source_file or not template_file:
            return {"error": "Fichiers requis"}, 400
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
    except Exception as e:
        return {"error": str(e)}, 500


def extract_pdf_pages_as_images(pdf_bytes, dpi=150):
    """
    Extrait chaque page d'un PDF en image PNG.
    Retourne une liste de dicts : {"filename": "page_01.png", "data": bytes, "index": i}
    """
    if not PYMUPDF_AVAILABLE:
        return []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            pages.append({
                "filename": f"page_{i+1:02d}.png",
                "data": img_bytes,
                "index": i  # 0-based
            })
        doc.close()
        return pages
    except Exception as e:
        print(f"[PDF→Images] Erreur: {e}")
        return []


def detect_sections_by_text(pdf_bytes):
    """
    Scanne toutes les pages du PDF avec PyMuPDF.
    Détecte les titres de section par leur texte exact.
    Retourne une liste ordonnée de sections trouvées :
    [
        {"field": "visuel_present", "page": 0, "y_top": float_pts, "y_bottom": float_pts},
        ...
    ]
    y_top/y_bottom sont en points PDF (coordonnées absolues sur la page).
    """
    if not PYMUPDF_AVAILABLE:
        return []

    # Mapping titre → nom de champ
    TITLE_TO_FIELD = {
        "visuel":              "visuel_present",
        "barème de mesures":   "bareme_mesures_present",
        "bareme de mesures":   "bareme_mesures_present",
        "détails technique":   "details_technique_present",
        "details technique":   "details_technique_present",
        "détail technique":    "details_technique_present",
        "detail technique":    "details_technique_present",
        "détails techniques":  "details_technique_present",
        "details techniques":  "details_technique_present",
    }

    # Titres séparateurs : pas extraits en image, servent de borne basse
    SEPARATOR_TITLES = {
        "matière", "matiere", "descriptif", "codes articles", "code articles",
        "coloris", "composition", "normes", "entretien", "personnalisation",
        "marquage", "nomenclature", "historique", "sommaire", "table des matières",
        "table des matieres", "référence", "reference", "tailles", "logo",
        "conditionnement", "certification", "lavage", "stockage",
    }

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    all_titles = []  # {"field", "page", "y_top", "page_height", "font_size"}

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_height = page.rect.height
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]

        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_text = ""
                line_y0 = line["bbox"][1]
                max_font_size = 0
                for span in line.get("spans", []):
                    line_text += span["text"]
                    max_font_size = max(max_font_size, span.get("size", 0))

                # Normaliser unicode (espaces insécables, accents composés, etc.)
                import unicodedata
                cleaned = unicodedata.normalize("NFKC", line_text)
                cleaned = cleaned.strip().rstrip(":").strip().lower()
                if not cleaned:
                    continue

                # Check section titles
                for title_pattern, field_name in TITLE_TO_FIELD.items():
                    if cleaned == title_pattern or cleaned.startswith(title_pattern + " "):
                        all_titles.append({
                            "field": field_name,
                            "page": page_idx,
                            "y_top": line_y0,
                            "page_height": page_height,
                            "font_size": max_font_size,
                            "raw_title": line_text.strip(),
                            "is_separator": False,
                        })
                        break
                else:
                    # Check separator titles
                    for sep in SEPARATOR_TITLES:
                        if cleaned == sep or cleaned.startswith(sep + " "):
                            all_titles.append({
                                "field": "_separator_" + sep.replace(" ", "_"),
                                "page": page_idx,
                                "y_top": line_y0,
                                "page_height": page_height,
                                "font_size": max_font_size,
                                "raw_title": line_text.strip(),
                                "is_separator": True,
                            })
                            break

    doc.close()

    if not all_titles:
        print("[detect_sections_by_text] Aucun titre trouvé dans le PDF")
        return []

    # Trier par (page, y_top)
    all_titles.sort(key=lambda t: (t["page"], t["y_top"]))
    titles_debug = [(t["raw_title"], "p%d" % (t["page"]+1), "y=%.1f" % t["y_top"], "fs=%.1f" % t["font_size"]) for t in all_titles]
    print(f"[detect_sections_by_text] Titres bruts trouvés: {titles_debug}")

    # ── CONSTRUCTION DES SECTIONS (multi-page aware) ──
    # Pour chaque titre de section, on cherche le prochain titre DANS TOUT LE DOCUMENT
    # et on génère un crop par page entre les deux.
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    num_pages = len(doc)
    page_heights = {i: doc[i].rect.height for i in range(num_pages)}
    doc.close()

    sections = []  # {"field", "crops": [{"page", "y_top", "y_bottom"}, ...]}

    for i, title in enumerate(all_titles):
        if title["field"].startswith("_separator_"):
            continue

        # Trouver le PROCHAIN titre dans le document (section ou séparateur)
        next_title = None
        for j in range(i + 1, len(all_titles)):
            next_title = all_titles[j]
            break

        start_page = title["page"]
        start_y = title["y_top"]

        if next_title and next_title["page"] == start_page:
            # Cas simple : prochain titre sur la même page
            end_page = start_page
            end_y = next_title["y_top"] - 5
        elif next_title and next_title["page"] > start_page:
            # Multi-page : la section va de cette page jusqu'à la page du prochain titre
            end_page = next_title["page"]
            end_y = next_title["y_top"] - 5
        else:
            # Dernier titre du document : va jusqu'en fin de page
            end_page = start_page
            end_y = title["page_height"]

        # Générer les crops page par page
        crops = []
        for pg in range(start_page, end_page + 1):
            if pg >= num_pages:
                break
            pg_height = page_heights.get(pg, title["page_height"])

            if pg == start_page and pg == end_page:
                # Tout sur une page
                crop_top = max(0, start_y - 5)
                crop_bottom = end_y
            elif pg == start_page:
                # Première page : du titre jusqu'en bas
                crop_top = max(0, start_y - 5)
                crop_bottom = pg_height
            elif pg == end_page:
                # Dernière page : du haut jusqu'au prochain titre
                crop_top = 0
                crop_bottom = end_y
            else:
                # Page intermédiaire : toute la page
                crop_top = 0
                crop_bottom = pg_height

            crop_height = crop_bottom - crop_top
            if crop_height >= 40:  # filtre mini
                crops.append({"page": pg, "y_top": crop_top, "y_bottom": crop_bottom})

        if crops:
            # Hauteur totale de la section (somme de tous les crops)
            total_height = sum(c["y_bottom"] - c["y_top"] for c in crops)
            sections.append({
                "field": title["field"],
                "crops": crops,
                "total_height": total_height,
            })

    # ── DÉDUPLICATION ──
    # Si un même field apparaît plusieurs fois (ex: sommaire + vraie section),
    # garder celui avec la plus grande hauteur totale
    best_sections = {}
    for s in sections:
        field = s["field"]
        if field not in best_sections or s["total_height"] > best_sections[field]["total_height"]:
            best_sections[field] = s

    final = list(best_sections.values())
    final.sort(key=lambda s: s["crops"][0]["page"])

    for s in final:
        crops_debug = ["p%d:%.0f-%.0f" % (c["page"]+1, c["y_top"], c["y_bottom"]) for c in s["crops"]]
        print(f"[detect_sections_by_text] {s['field']} → {crops_debug}")
    return final


def crop_pdf_section(pdf_bytes, page_index, y_top_pts, y_bottom_pts, dpi=220):
    """
    Extrait une portion verticale d'une page PDF via PyMuPDF.
    y_top_pts et y_bottom_pts sont en POINTS PDF (pas en pourcentage).
    Retourne les bytes PNG de la zone croppée.
    """
    if not PYMUPDF_AVAILABLE:
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[page_index]
        page_w = page.rect.width
        page_h = page.rect.height

        clip = fitz.Rect(0, max(0, y_top_pts), page_w, min(page_h, y_bottom_pts))

        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, clip=clip)
        img_bytes = pix.tobytes("png")
        doc.close()
        return img_bytes
    except Exception as e:
        print(f"[crop_pdf_section] Erreur: {e}")
        return None


# On garde l'ancienne fonction crop pour compatibilité (pourcentages)
def crop_pdf_page(pdf_bytes, page_index, y_top_pct, y_bottom_pct, dpi=220):
    if not PYMUPDF_AVAILABLE:
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[page_index]
        page_h = page.rect.height
        page_w = page.rect.width
        y_top    = page_h * (y_top_pct / 100.0)
        y_bottom = page_h * (y_bottom_pct / 100.0)
        clip = fitz.Rect(0, max(0, y_top), page_w, min(page_h, y_bottom))
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, clip=clip)
        img_bytes = pix.tobytes("png")
        doc.close()
        return img_bytes
    except Exception as e:
        print(f"[crop_pdf_page] Erreur: {e}")
        return None


# Mapping nom_champ → nom de fichier lisible
FT_IMAGE_NAMES = {
    "visuel_present": "visuel",
    "bareme_mesures_present": "bareme_mesures",
    "details_technique_present": "details_technique",
}


@app.route('/api/extract-ft', methods=['POST'])
def extract_ft():
    """
    Endpoint dédié aux Fiches Techniques.
    Retourne un ZIP contenant :
      - {stem}_complet.xml
      - {stem}_simple.xml
      - images/visuel.png, images/bareme_mesures.png, images/details_technique.png
        (crop basé sur la détection de titres par PyMuPDF — zéro Gemini pour les coordonnées)
    """
    try:
        source_file = request.files.get('source')
        template_file = request.files.get('template')
        if not source_file or not template_file:
            return {"error": "Fichiers requis"}, 400

        source_bytes = source_file.read()
        template_content = template_file.read().decode('utf-8')
        fields, _ = load_xml_template(template_content)
        source_info = load_source_file(source_bytes, source_file.filename)

        # 1. Extraction des données textuelles par Gemini
        extractions = extract_with_gemini(fields, source_info)
        stem = Path(source_file.filename).stem
        xml_complet = fill_xml_complet(template_content, extractions, source_file.filename)
        xml_simple = fill_xml_simple(template_content, extractions)

        # 2. Détection des sections par texte PyMuPDF + crop exact
        named_images = []
        mime_type, _ = mimetypes.guess_type(source_file.filename)
        if mime_type == "application/pdf" and PYMUPDF_AVAILABLE:
            sections = detect_sections_by_text(source_bytes)
            print(f"[extract-ft] Sections détectées: {len(sections)}")

            for section in sections:
                field = section["field"]
                friendly_name = FT_IMAGE_NAMES.get(field, field)
                crops = section["crops"]

                for ci, crop in enumerate(crops):
                    cropped = crop_pdf_section(
                        source_bytes,
                        crop["page"],
                        crop["y_top"],
                        crop["y_bottom"],
                        dpi=220
                    )
                    if cropped:
                        # 1 crop → visuel.png, multi-crop → visuel_1.png, visuel_2.png
                        suffix = "" if len(crops) == 1 else f"_{ci + 1}"
                        named_images.append({
                            "filename": f"{friendly_name}{suffix}.png",
                            "data": cropped
                        })

        # 3. Construction du ZIP
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_path = Path(tempfile.gettempdir()) / f"argos_ft_{ts}.zip"
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stem}_complet.xml", xml_complet)
            zf.writestr(f"{stem}_simple.xml", xml_simple)
            for img in named_images:
                zf.writestr(f"images/{img['filename']}", img['data'])

        return send_file(zip_path, mimetype='application/zip', as_attachment=True, download_name=zip_path.name)
    except Exception as e:
        print(f"[extract-ft] Erreur: {e}")
        import traceback; traceback.print_exc()
        return {"error": str(e)}, 500


@app.route('/api/extract-batch', methods=['POST'])
def extract_batch():
    try:
        template_file = request.files.get('template')
        if not template_file:
            return {"error": "Template requis"}, 400
        template_content = template_file.read().decode('utf-8')
        fields, _ = load_xml_template(template_content)
        sources = request.files.getlist('sources')
        if not sources:
            return {"error": "Fichiers requis"}, 400
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
            except Exception as e:
                results.append({"source": sf.filename, "status": "error", "error": str(e)})
        zip_path = Path(tempfile.gettempdir()) / f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for f in temp_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(temp_dir))
            zf.writestr("_rapport.json", json.dumps({"results": results}, indent=2))
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        return send_file(zip_path, mimetype='application/zip', as_attachment=True)
    except Exception as e:
        return {"error": str(e)}, 500


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
        session['gmail_credentials'] = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': list(creds.scopes) if creds.scopes else GMAIL_SCOPES
        }
        session.modified = True
        print(f"[OAuth] Success!")
        return redirect('/?gmail_auth=success')
    except Exception as e:
        print(f"[OAuth] Callback error: {e}")
        import traceback
        traceback.print_exc()
        return redirect('/?gmail_auth=error')


@app.route('/api/gmail/status')
def gmail_status():
    connected = 'gmail_credentials' in session and session['gmail_credentials'] is not None
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
        creds = get_credentials_from_session()
        if not creds:
            return {"error": "Non connecté à Gmail"}, 401

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

                xml_complet = fill_xml_complet(template_content, extractions, f"Email: {email['subject']}")
                xml_simple = fill_xml_simple(template_content, extractions)
                (email_dir / "extraction_complet.xml").write_text(xml_complet, encoding='utf-8')
                (email_dir / "extraction_simple.xml").write_text(xml_simple, encoding='utf-8')

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