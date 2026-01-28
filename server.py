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
from flask import Flask, request, send_file, send_from_directory, jsonify
from flask_cors import CORS
import google.generativeai as genai

# ============================================================
# CONFIGURATION
# ============================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = "gemini-2.0-flash"

# Init Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

# Init Flask
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# ============================================================
# NETEXIAL AGENT FUNCTIONS
# ============================================================

def load_xml_template(xml_content):
    """Charge le template XML et extrait les champs."""
    root = ET.fromstring(xml_content)
    
    fields = []
    champs = root.find("champs")
    if champs:
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
            if valeurs:
                field["valeurs_possibles"] = [v.text.strip() for v in valeurs.findall("valeur") if v.text]
            fields.append(field)
    
    return fields, root


def load_source_file(content, filename):
    """Charge le fichier source (image, PDF, texte)."""
    mime_type, _ = mimetypes.guess_type(filename)
    
    info = {
        "filename": filename,
        "mime_type": mime_type or "application/octet-stream",
        "content": None,
        "type": "unknown"
    }
    
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
    """Construit le prompt pour Gemini."""
    fields_desc = ""
    for f in fields:
        fields_desc += f"\n- {f['nom']} ({f['type_donnee']}): {f['description']}"
        if f['valeurs_possibles']:
            fields_desc += f" [Valeurs: {', '.join(f['valeurs_possibles'])}]"
        if f['obligatoire']:
            fields_desc += " [OBLIGATOIRE]"
        if f['multiple']:
            fields_desc += " [MULTIPLE]"
    
    return f"""Analyse ce document et extrais les informations suivantes.

CHAMPS À EXTRAIRE:{fields_desc}

INSTRUCTIONS:
1. Pour chaque champ, donne:
   - valeur_defaut: la valeur trouvée ou déduite
   - type_source: "texte_explicite" | "analyse_visuelle" | "inference" | "non_trouve"
   - reference: où tu as trouvé l'information (ex: "haut de l'image", "ligne 3")
   - explication: pourquoi cette valeur a été choisie

2. Si valeurs_possibles existe, choisis UNIQUEMENT parmi ces valeurs

3. Si MULTIPLE est défini, retourne une liste de valeurs

4. Si l'information n'est pas clairement visible ou explicitement présente dans le document:
   - tu peux tenter une inference UNIQUEMENT si elle est raisonnable, cohérente et justifiable
   - si aucune inference fiable n'est possible, indique "non_trouve"
   - ne fournis jamais une valeur arbitraire ou inventée uniquement pour répondre

5. Si le champ est OBLIGATOIRE et que l'information n'est pas trouvée:
   - fais une inference prudente
   - explique clairement ton raisonnement et son niveau d'incertitude


RÉPONDS UNIQUEMENT EN JSON:
{{
  "extractions": [
    {{"nom": "...", "valeur_defaut": "...", "type_source": "...", "reference": "...", "explication": "..."}}
  ],
  "analyse_globale": "description du document"
}}"""


def extract_with_gemini(fields, source_info):
    """Appelle Gemini pour extraire les données."""
    prompt = build_prompt(fields)
    
    if source_info["type"] in ["image", "pdf"]:
        content = [
            {"mime_type": source_info["mime_type"], "data": source_info["content"]},
            prompt
        ]
    else:
        content = [f"{prompt}\n\nCONTENU DU DOCUMENT:\n```\n{source_info['content']}\n```"]
    
    print(f"🤖 Analyse en cours avec Gemini pour {source_info['filename']}...")
    response = model.generate_content(content)
    
    # Nettoyer le JSON
    text = response.text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    
    return json.loads(text.strip())


def fill_xml(template_content, extractions, source_filename):
    """Remplit le XML avec les données extraites."""
    root = ET.fromstring(template_content)
    
    # Mapping des extractions
    ext_map = {e["nom"]: e for e in extractions.get("extractions", [])}
    
    # Remplir chaque champ
    champs = root.find("champs")
    if champs:
        for champ in champs.findall("champ"):
            nom = champ.findtext("nom", "").strip()
            if nom in ext_map:
                ext = ext_map[nom]
                
                # Valeur par défaut
                val_def = champ.find("valeur_defaut")
                if val_def is not None:
                    v = ext.get("valeur_defaut", "")
                    val_def.text = ", ".join(v) if isinstance(v, list) else str(v)
                
                # Source detection
                src = champ.find("source_detection")
                if src is not None:
                    for tag in ["type_source", "reference", "explication"]:
                        elem = src.find(tag)
                        if elem is not None:
                            elem.text = ext.get(tag, "")
    
    # Ajouter métadonnées
    meta = ET.SubElement(root, "metadata")
    ET.SubElement(meta, "date_extraction").text = datetime.now().isoformat()
    ET.SubElement(meta, "agent").text = "ARGOS / NETEXIAL Agent"
    ET.SubElement(meta, "fichier_source").text = source_filename
    ET.SubElement(meta, "analyse_globale").text = extractions.get("analyse_globale", "")
    
    # Formatter
    xml_str = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(xml_str).toprettyxml(indent="  ")
    pretty = '\n'.join(line for line in pretty.split('\n') if line.strip())
    
    return pretty


# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    """Sert le fichier HTML."""
    return send_from_directory('.', 'argos.html')


@app.route('/api/extract', methods=['POST'])
def extract():
    """Endpoint d'extraction simple (1 fichier)."""
    try:
        # Récupérer les fichiers
        if 'source' not in request.files or 'template' not in request.files:
            return {"error": "Fichiers source et template requis"}, 400
        
        source_file = request.files['source']
        template_file = request.files['template']
        
        print(f"📄 Source: {source_file.filename}")
        print(f"📋 Template: {template_file.filename}")
        
        # Charger le template
        template_content = template_file.read().decode('utf-8')
        fields, root = load_xml_template(template_content)
        print(f"   → {len(fields)} champs à extraire")
        
        # Charger la source
        source_content = source_file.read()
        source_info = load_source_file(source_content, source_file.filename)
        print(f"   → Type: {source_info['type']}")
        
        # Extraction avec Gemini
        extractions = extract_with_gemini(fields, source_info)
        print(f"   → {len(extractions.get('extractions', []))} champs extraits")
        
        # Remplir le XML
        result_xml = fill_xml(template_content, extractions, source_file.filename)
        
        # Sauvegarder temporairement et renvoyer
        output_path = Path(tempfile.gettempdir()) / f"argos_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(result_xml)
        
        print(f"✅ Terminé: {output_path}")
        
        return send_file(
            output_path,
            mimetype='application/xml',
            as_attachment=True,
            download_name=output_path.name
        )
        
    except Exception as e:
        print(f"❌ Erreur: {e}")
        return {"error": str(e)}, 500


@app.route('/api/extract-batch', methods=['POST'])
def extract_batch():
    """Endpoint d'extraction batch (plusieurs fichiers)."""
    try:
        # Récupérer le template
        if 'template' not in request.files:
            return {"error": "Template XML requis"}, 400
        
        template_file = request.files['template']
        template_content = template_file.read().decode('utf-8')
        fields, _ = load_xml_template(template_content)
        
        print(f"📋 Template: {template_file.filename}")
        print(f"   → {len(fields)} champs à extraire")
        
        # Récupérer tous les fichiers source
        source_files = request.files.getlist('sources')
        if not source_files:
            return {"error": "Au moins un fichier source requis"}, 400
        
        print(f"📁 {len(source_files)} fichiers à traiter")
        
        # Créer un dossier temporaire pour les résultats
        temp_dir = Path(tempfile.gettempdir()) / f"argos_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        temp_dir.mkdir(exist_ok=True)
        
        results = []
        errors = []
        
        for i, source_file in enumerate(source_files, 1):
            try:
                print(f"\n[{i}/{len(source_files)}] Traitement de {source_file.filename}...")
                
                # Charger la source
                source_content = source_file.read()
                source_info = load_source_file(source_content, source_file.filename)
                
                # Extraction avec Gemini
                extractions = extract_with_gemini(fields, source_info)
                
                # Remplir le XML
                result_xml = fill_xml(template_content, extractions, source_file.filename)
                
                # Nom du fichier de sortie
                base_name = Path(source_file.filename).stem
                output_name = f"{base_name}_extracted.xml"
                output_path = temp_dir / output_name
                
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(result_xml)
                
                results.append({
                    "source": source_file.filename,
                    "output": output_name,
                    "status": "success",
                    "fields_extracted": len(extractions.get('extractions', []))
                })
                print(f"   ✅ {output_name}")
                
            except Exception as e:
                error_msg = str(e)
                errors.append({
                    "source": source_file.filename,
                    "error": error_msg
                })
                results.append({
                    "source": source_file.filename,
                    "status": "error",
                    "error": error_msg
                })
                print(f"   ❌ Erreur: {error_msg}")
        
        # Créer le fichier ZIP
        zip_path = Path(tempfile.gettempdir()) / f"argos_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Ajouter tous les XML générés
            for xml_file in temp_dir.glob("*.xml"):
                zipf.write(xml_file, xml_file.name)
            
            # Ajouter un rapport JSON
            report = {
                "date": datetime.now().isoformat(),
                "template": template_file.filename,
                "total_files": len(source_files),
                "success": len([r for r in results if r["status"] == "success"]),
                "errors": len(errors),
                "results": results
            }
            zipf.writestr("_rapport.json", json.dumps(report, indent=2, ensure_ascii=False))
        
        # Nettoyer le dossier temporaire
        for f in temp_dir.glob("*"):
            f.unlink()
        temp_dir.rmdir()
        
        print(f"\n✅ Batch terminé: {len(results) - len(errors)}/{len(results)} succès")
        
        return send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"argos_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )
        
    except Exception as e:
        print(f"❌ Erreur batch: {e}")
        return {"error": str(e)}, 500


# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("=" * 50)
    print("🚀 ARGOS Server")
    print("=" * 50)
    print("   URL: http://localhost:5000")
    print("   Mode simple: POST /api/extract")
    print("   Mode batch:  POST /api/extract-batch")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=5000, debug=True)