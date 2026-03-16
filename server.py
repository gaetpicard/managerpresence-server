"""
ManagerPresence - Serveur de Licences
Déployé sur Render.com
Stockage persistant via JSONBin.io
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
import os
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import secrets
import string

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIGURATION
# ============================================================

# Token admin (à définir dans les variables d'environnement Render)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev_token_change_me")

# Email pour notifications (à définir dans les variables d'environnement)
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")

# JSONBin.io Configuration
JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY", "$2a$10$aQwJ5fJY55JpEBWs4saSh.u07YdpziWPFjhEmWRc6s.3WBvJ8bmVm")
JSONBIN_LICENCES_ID = os.environ.get("JSONBIN_LICENCES_ID", "69b82a75c3097a1dd52e2011")
JSONBIN_CODES_ID = os.environ.get("JSONBIN_CODES_ID", "69b82a9cb7ec241ddc73702b")
JSONBIN_URL = "https://api.jsonbin.io/v3/b"

# ============================================================
# DÉFINITION DES PLANS
# ============================================================

PLANS = {
    "trial": {
        "nom": "Essai gratuit",
        "duree_jours": 30,
        "fonctionnalites": ["tableau", "eleves", "creneaux", "export", "forum", "cadres_illimite", "import", "sms", "perso", "doc"],
        "max_cadres": 999
    },
    "standard": {
        "nom": "Standard",
        "fonctionnalites": ["tableau", "eleves", "creneaux", "export", "forum"],
        "max_cadres": 3
    },
    "premium": {
        "nom": "Premium",
        "fonctionnalites": ["tableau", "eleves", "creneaux", "export", "forum", "cadres_illimite", "import", "sms", "perso", "doc", "pwa", "support"],
        "max_cadres": 999
    }
}

# Types de codes d'activation
CODE_TYPES = {
    "PREMIUM_PERMANENT": {"plan": "premium", "jours": 36500, "prefixe": "PRM"},
    "PREMIUM_1AN": {"plan": "premium", "jours": 365, "prefixe": "PR1"},
    "STANDARD_1AN": {"plan": "standard", "jours": 365, "prefixe": "ST1"},
    "PROLONGATION_60J": {"plan": None, "jours": 60, "prefixe": "P60"},
    "PROLONGATION_30J": {"plan": None, "jours": 30, "prefixe": "P30"},
}

# ============================================================
# UTILITAIRES - STOCKAGE JSONBIN
# ============================================================

def jsonbin_headers():
    """Headers pour les requêtes JSONBin"""
    return {
        "X-Master-Key": JSONBIN_API_KEY,
        "Content-Type": "application/json"
    }

def charger_licences():
    """Charge les licences depuis JSONBin"""
    try:
        response = requests.get(
            f"{JSONBIN_URL}/{JSONBIN_LICENCES_ID}/latest",
            headers=jsonbin_headers()
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("record", {}).get("licences", {})
    except Exception as e:
        print(f"Erreur chargement licences: {e}")
    return {}

def sauvegarder_licences(licences):
    """Sauvegarde les licences dans JSONBin"""
    try:
        response = requests.put(
            f"{JSONBIN_URL}/{JSONBIN_LICENCES_ID}",
            headers=jsonbin_headers(),
            json={"licences": licences}
        )
        return response.status_code == 200
    except Exception as e:
        print(f"Erreur sauvegarde licences: {e}")
        return False

def charger_codes():
    """Charge les codes depuis JSONBin"""
    try:
        response = requests.get(
            f"{JSONBIN_URL}/{JSONBIN_CODES_ID}/latest",
            headers=jsonbin_headers()
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("record", {}).get("codes", {})
    except Exception as e:
        print(f"Erreur chargement codes: {e}")
    return {}

def sauvegarder_codes(codes):
    """Sauvegarde les codes dans JSONBin"""
    try:
        response = requests.put(
            f"{JSONBIN_URL}/{JSONBIN_CODES_ID}",
            headers=jsonbin_headers(),
            json={"codes": codes}
        )
        return response.status_code == 200
    except Exception as e:
        print(f"Erreur sauvegarde codes: {e}")
        return False

# ============================================================
# UTILITAIRES - NOTIFICATIONS
# ============================================================

def envoyer_notification(sujet, message):
    """Envoie un email de notification"""
    if not SMTP_PASSWORD or not SMTP_EMAIL:
        print(f"[NOTIFICATION] {sujet}: {message}")
        return False
    
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_EMAIL
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = f"[ManagerPresence] {sujet}"
        msg.attach(MIMEText(message, "plain"))
        
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Erreur envoi email: {e}")
        return False

# ============================================================
# UTILITAIRES - CODES
# ============================================================

def generer_code(prefixe):
    """Génère un code unique au format PRE-XXXX-XXXX"""
    chars = string.ascii_uppercase + string.digits
    partie1 = ''.join(secrets.choice(chars) for _ in range(4))
    partie2 = ''.join(secrets.choice(chars) for _ in range(4))
    return f"{prefixe}-{partie1}-{partie2}"

# ============================================================
# UTILITAIRES - LICENCES
# ============================================================

def calculer_jours_restants(date_expiration_str):
    """Calcule le nombre de jours restants avant expiration"""
    try:
        date_exp = datetime.fromisoformat(date_expiration_str.replace("Z", "+00:00"))
        if date_exp.tzinfo:
            date_exp = date_exp.replace(tzinfo=None)
        delta = date_exp - datetime.now()
        return max(0, delta.days)
    except:
        return 0

def creer_licence_trial(project_id, nom_structure=""):
    """Crée une nouvelle licence d'essai"""
    maintenant = datetime.now()
    expiration = maintenant + timedelta(days=PLANS["trial"]["duree_jours"])
    
    licence = {
        "projectId": project_id,
        "nomStructure": nom_structure,
        "dateInscription": maintenant.isoformat(),
        "dateExpiration": expiration.isoformat(),
        "plan": "trial",
        "actif": True,
        "fonctionnalites": PLANS["trial"]["fonctionnalites"],
        "maxCadres": PLANS["trial"]["max_cadres"],
        "message": f"Bienvenue ! Votre essai gratuit expire dans {PLANS['trial']['duree_jours']} jours."
    }
    
    # Notification
    envoyer_notification(
        "🆕 Nouvelle inscription",
        f"Nouveau client inscrit !\n\nProject ID: {project_id}\nStructure: {nom_structure or 'Non renseigné'}\nDate: {maintenant.strftime('%d/%m/%Y %H:%M')}\nExpiration essai: {expiration.strftime('%d/%m/%Y')}"
    )
    
    return licence

def formater_licence_response(licence):
    """Formate la licence pour la réponse API"""
    jours_restants = calculer_jours_restants(licence.get("dateExpiration", ""))
    est_actif = licence.get("actif", False) and jours_restants > 0
    
    # Message selon le statut
    if not est_actif:
        message = "Votre licence a expiré. Contactez-nous pour continuer à utiliser l'application."
    elif jours_restants <= 7:
        message = f"⚠️ Votre licence expire dans {jours_restants} jour(s) !"
    elif jours_restants <= 30 and licence.get("plan") == "trial":
        message = f"Votre essai gratuit expire dans {jours_restants} jours."
    else:
        message = licence.get("message", "")
    
    plan_info = PLANS.get(licence.get("plan", "trial"), PLANS["trial"])
    
    return {
        "projectId": licence.get("projectId"),
        "nomStructure": licence.get("nomStructure", ""),
        "plan": licence.get("plan", "trial"),
        "planNom": plan_info["nom"],
        "actif": est_actif,
        "dateExpiration": licence.get("dateExpiration"),
        "joursRestants": jours_restants,
        "fonctionnalites": licence.get("fonctionnalites", plan_info["fonctionnalites"]),
        "maxCadres": licence.get("maxCadres", plan_info["max_cadres"]),
        "message": message
    }

# ============================================================
# ROUTES PUBLIQUES
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    """Vérification que le serveur tourne"""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

@app.route("/licence/<project_id>", methods=["GET"])
def get_licence(project_id):
    """Récupère la licence d'un projet (crée un trial si inconnu)"""
    licences = charger_licences()
    
    if project_id not in licences:
        # Nouveau client → créer licence trial
        nom_structure = request.args.get("nom", "")
        licence = creer_licence_trial(project_id, nom_structure)
        licences[project_id] = licence
        sauvegarder_licences(licences)
    
    return jsonify(formater_licence_response(licences[project_id]))

@app.route("/licence/<project_id>/code", methods=["POST"])
def activer_code(project_id):
    """Active un code pour un projet"""
    data = request.get_json() or {}
    code = data.get("code", "").strip().upper()
    
    if not code:
        return jsonify({"error": "Code manquant"}), 400
    
    # Charger les codes
    codes = charger_codes()
    
    if code not in codes:
        return jsonify({"error": "Code invalide"}), 404
    
    code_info = codes[code]
    
    if code_info.get("utilise"):
        return jsonify({"error": "Code déjà utilisé"}), 400
    
    # Charger les licences
    licences = charger_licences()
    
    if project_id not in licences:
        licences[project_id] = creer_licence_trial(project_id)
    
    licence = licences[project_id]
    
    # Appliquer le code
    code_type = code_info.get("type")
    type_config = CODE_TYPES.get(code_type, {})
    
    if type_config.get("plan"):
        nouveau_plan = type_config["plan"]
        plan_config = PLANS[nouveau_plan]
        licence["plan"] = nouveau_plan
        licence["fonctionnalites"] = plan_config["fonctionnalites"]
        licence["maxCadres"] = plan_config["max_cadres"]
        licence["dateExpiration"] = (datetime.now() + timedelta(days=type_config["jours"])).isoformat()
    else:
        try:
            date_exp_actuelle = datetime.fromisoformat(licence["dateExpiration"].replace("Z", "+00:00"))
            if date_exp_actuelle.tzinfo:
                date_exp_actuelle = date_exp_actuelle.replace(tzinfo=None)
        except:
            date_exp_actuelle = datetime.now()
        
        if date_exp_actuelle < datetime.now():
            date_exp_actuelle = datetime.now()
        
        licence["dateExpiration"] = (date_exp_actuelle + timedelta(days=type_config["jours"])).isoformat()
    
    licence["actif"] = True
    licence["message"] = f"Code {code} activé avec succès !"
    
    # Marquer le code comme utilisé
    codes[code]["utilise"] = True
    codes[code]["utilise_par"] = project_id
    codes[code]["utilise_le"] = datetime.now().isoformat()
    
    # Sauvegarder
    sauvegarder_licences(licences)
    sauvegarder_codes(codes)
    
    # Notification
    envoyer_notification(
        "🎟️ Code activé",
        f"Un code a été activé !\n\nCode: {code}\nType: {code_type}\nProject ID: {project_id}\nStructure: {licence.get('nomStructure', 'N/A')}"
    )
    
    return jsonify({
        "success": True,
        "message": f"Code activé ! Vous êtes maintenant en plan {PLANS[licence['plan']]['nom']}.",
        "licence": formater_licence_response(licence)
    })

# ============================================================
# ROUTES ADMIN (protégées par token)
# ============================================================

def verifier_admin():
    """Vérifie le token admin dans les headers"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return token == ADMIN_TOKEN

@app.route("/admin/liste", methods=["GET"])
def admin_liste():
    """Liste toutes les licences"""
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    licences = charger_licences()
    liste = [formater_licence_response(l) for l in licences.values()]
    liste.sort(key=lambda x: x.get("dateExpiration", ""), reverse=True)
    
    return jsonify({"total": len(liste), "licences": liste})

@app.route("/admin/gencode", methods=["POST"])
def admin_gencode():
    """Génère un nouveau code d'activation"""
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    data = request.get_json() or {}
    code_type = data.get("type", "").upper()
    
    if code_type not in CODE_TYPES:
        return jsonify({"error": f"Type invalide. Types: {list(CODE_TYPES.keys())}"}), 400
    
    config = CODE_TYPES[code_type]
    codes = charger_codes()
    
    nouveau_code = generer_code(config["prefixe"])
    while nouveau_code in codes:
        nouveau_code = generer_code(config["prefixe"])
    
    codes[nouveau_code] = {
        "type": code_type,
        "cree_le": datetime.now().isoformat(),
        "utilise": False
    }
    
    sauvegarder_codes(codes)
    
    return jsonify({
        "code": nouveau_code,
        "type": code_type,
        "effet": f"{config.get('plan', 'Prolongation')} - {config['jours']} jours"
    })

@app.route("/admin/codes", methods=["GET"])
def admin_codes():
    """Liste tous les codes"""
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    codes = charger_codes()
    liste = [{"code": c, **info} for c, info in codes.items()]
    liste.sort(key=lambda x: x.get("cree_le", ""), reverse=True)
    
    return jsonify({"total": len(liste), "codes": liste})

@app.route("/licence/<project_id>", methods=["POST"])
def admin_update_licence(project_id):
    """Met à jour une licence (admin)"""
    if not verifier_admin():
        return jsonify({"error": "Non autorisé"}), 401
    
    data = request.get_json() or {}
    licences = charger_licences()
    
    if project_id not in licences:
        return jsonify({"error": "Licence non trouvée"}), 404
    
    licence = licences[project_id]
    
    if "plan" in data and data["plan"] in PLANS:
        nouveau_plan = data["plan"]
        plan_config = PLANS[nouveau_plan]
        licence["plan"] = nouveau_plan
        licence["fonctionnalites"] = plan_config["fonctionnalites"]
        licence["maxCadres"] = plan_config["max_cadres"]
    
    if "actif" in data:
        licence["actif"] = bool(data["actif"])
    
    if "dateExpiration" in data:
        licence["dateExpiration"] = data["dateExpiration"]
    
    if "joursSupplementaires" in data:
        try:
            date_exp = datetime.fromisoformat(licence["dateExpiration"].replace("Z", "+00:00"))
            if date_exp.tzinfo:
                date_exp = date_exp.replace(tzinfo=None)
        except:
            date_exp = datetime.now()
        
        if date_exp < datetime.now():
            date_exp = datetime.now()
        
        licence["dateExpiration"] = (date_exp + timedelta(days=int(data["joursSupplementaires"]))).isoformat()
    
    if "nomStructure" in data:
        licence["nomStructure"] = data["nomStructure"]
    
    if "message" in data:
        licence["message"] = data["message"]
    
    sauvegarder_licences(licences)
    
    return jsonify({"success": True, "licence": formater_licence_response(licence)})

# ============================================================
# DÉMARRAGE
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
